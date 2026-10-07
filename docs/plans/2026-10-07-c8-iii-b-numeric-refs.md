Status: plan (rev 3, BUILD-READY: Codex plan gate GO in round 3)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, code-review.

# C8-iii-b: auto-chunk `when:` columns whose predicate reads non-string columns

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. This follows C8-i/ii/iii-a. Branch `feat/c8-iii-b-numeric-refs` off engine main `4a08f570`. Risk R2: a wider auto-chunk route on a default path.

## 1. Problem and the survey that narrowed it

The planner keeps a `when:` column off auto-chunking unless the target AND every reference is exactly `pa.string()` (`native/_when_admission.planner_relaxed_when_columns`, `:191-220`; the reason `when_predicate_not_chunk_stable` is emitted at `_planner.py:414`).

The concern behind the rule:
- the per-chunk `when` mask converts each chunk's references with `to_pandas_fk_safe` (`native/_when_mask.py:50-60`; the chunked oracle leg does the same at `_pandas_adapter.py:213`);
- an int64 reference with nulls in SOME chunks becomes float64 in those chunks and stays int64 in others;
- so a predicate near 2**53 can select differently per chunk than on the whole frame. The survey (2026-10-07) reproduced this: whole frame `[T,F,T,F]` against chunk `[F,F]`.

The survey also found that the auto-chunk route rejects any table with an integer column that has nulls, table-wide (`_planner.py:496-501`, `_runtime_source_rejections`). A lazy integer column with no footer null count is rejected as well (`:490`).
- **Ordering (rev 2, Codex round 1).** These are separate rejections, applied AFTER `planner_relaxed_when_columns` runs. A nullable integer or a dictionary reference can therefore reach the relax rule; the route is rejected afterwards.
- So the relax rule must NOT rely on that later gate. It independently requires a KNOWN-zero null count for integer and bool references (2a, 2b).
- The int-to-float64 widening described here is the DEFAULT, unprotected conversion. pandas nullable metadata (`Int64`, `boolean`) keeps a column in one nullable dtype in every chunk.

Measured with `to_pandas_fk_safe` under DEFAULT conversion (pandas 2.3.3, pyarrow 24.0.0). Nullable pandas metadata (`Int64`, `boolean`) is the exception noted above: it keeps one dtype per chunk.

| Reference type | Dtype stable across null-free and null-bearing chunks? |
|---|---|
| int, uint | No by default (int when null-free, float64 when null-bearing); admission independently requires a known-zero null count |
| float32, float64 | Yes |
| timestamp | Yes (datetime64) |
| date32 | Yes (object either way) |
| string | Yes |
| large_string | Yes |
| bool | No: bool when null-free, object when null-bearing |
| decimal, dictionary, nested | Reach this rule but stay outside its allow-list; the later runtime gate (`:504-510`) independently declines their tables |

So the string-only rule is stricter than the real hazard requires. The goal is to relax it to the reference types whose chunk conversion is provably stable, using the relax rule's own checks rather than any later gate.

## 2. Decision

**2a. The relaxed reference rule.**
- `planner_relaxed_when_columns` relaxes an admitted `when` column when every reference is in the source schema and each one is one of:
  - `pa.string()` or `pa.large_string()`;
  - float of any width;
  - EXACTLY these temporal types: timestamp (any unit, with or without a timezone), date32 and date64. `time32`, `time64` and `duration` are NOT admitted, even though `pa.types.is_temporal` includes them, because `duration[ns] == 1` raises in eval (Codex round 1). Any type not on this list declines.
  - signed or unsigned integer whose whole-column null count is KNOWN to be 0;
  - bool whose whole-column null count is KNOWN to be 0.
- The null counts come from `facts_for(...).null_count(name)`: the exact footer count for Parquet `LazySource`, and the exact `null_count` for a resident table. An UNKNOWN count (`None`) never relaxes an integer or bool reference.
- The target rule is unchanged: the target must still be `pa.string()`.
- Rules 1, 3 and 4 (strategy and config gate, closed grammar, no earlier writer) are unchanged.

**2b. Why the integer check is repeated.** The table-wide integer gate already holds today, but the relax rule checks the reference's null count itself. If that gate is ever loosened, this rule must not silently start relaxing integer references with nulls. Test 4 pins this.

**2c. Explicit chunked entry is unchanged.** It already admits numeric references and compares against the chunked oracle (`test_c8_i_when_declines.py:285`). Per-chunk evaluation there is the user's explicit choice, documented as such.

**2d. Multi-table split.** It uses the same rule per table (`_pipeline_multi_table.py:193`), with no extra change. There is a regression test: two tables with identically named references, where one table is null-free and dispatched and the other has nulls or an unknown count and is declined.

**2e. Docs.** The CHANGELOG, plus `docs/strategies.md`'s `when:` section listing which reference types auto-chunk.

## 3. Acceptance tests (written first; never weakened)

Every case runs on the auto route, compares lane-on (auto-chunked) output byte-for-byte against the WHOLE-FRAME oracle run, and asserts the route taken.

1. **Relaxed and auto-chunked:**
   - int64 and uint64 references, null-free, including values above 2**53 and uint64 above 2**63. The uint64 numexpr quirk is the same in both runs, so the outputs still match;
   - float64 references with NaN and nulls, using `!=`, `==` and `in`;
   - **Temporal, with EXPECTED masks pinned, not only output equality** (Codex round 1, because stable dtype does not imply that datetime-string coercion selects anything):
     - date32 and date64 (default conversion gives Python dates in object columns, and string literals are not coerced): equality and membership against a string literal pin ALL-FALSE masks, inequality pins ALL-TRUE, and ordering (`>`) pins an equal `when_expression_error` on both runs. A compound date-plus-string predicate (for example `d != '1970-01-01' and s == 'x'`) pins a strict-subset selection, exercising date-reference dispatch without changing oracle semantics.
     - tz-aware timestamps use offset-bearing literals. Naive timestamps use naive literals. Each asserts a STRICT-SUBSET selection (both selected and unselected rows).
   - null-free bool references;
   - large_string references;
   - mixed: one string reference plus one int reference;
   - float16, float32 and float64 at precision boundaries. For example, float32 `0.1` satisfies `r == 0.1` but not `r in [0.1]`; the result must equal the whole-frame result whichever way it goes;
   - narrow ints (int8, int16, uint8);
   - pandas nullable metadata (`Int64`, `boolean`, `Float64`);
   - an all-null chunk inside an otherwise populated float or timestamp reference;
   - **generated partitions:** varied chunk sizes and null placements. Each one compares both the mask and the final output against the whole-frame run.

   Each case runs on resident and Parquet-lazy sources, with several chunks and ragged sizes.
2. **Still declined, with the existing reason:**
   - a bool reference with nulls;
   - a bool or int reference whose null count is unknown (Parquet written without null-count statistics);
   - a dictionary reference;
   - a reference masked earlier (rule 4);
   - a predicate outside the grammar;
   - a non-string target;
   - `time32`, `time64` and `duration` references (not admitted).
3. **The planner reason** names exactly the still-declined columns.
4. **Defense in depth:** with the table-wide integer gate stubbed off, an int reference with nulls is still NOT relaxed.
5. **Old pins:**
   - `test_c8_i_when_auto_route.py:117-123` ("numeric reference stays full-frame") moves to a still-declined numeric case, such as a bool with nulls or an unknown null count, so its intent is kept;
   - `test_planner_mutation_kills.py:163, 181` and `test_c8_i_when_declines.py:321` change only where the reason set changes.

   The record lists every change.
6. **Testflight:** STOP if a fingerprint moves.
7. **Sentries;** mutation on 2a's type set and the null-count checks.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| A reference type whose chunk dtype varies gets relaxed | An explicit allow-list from measured conversions; bool and int require a KNOWN zero null count; test 2 |
| Unknown statistics treated as zero | `None` never relaxes; test 2 |
| The integer gate is loosened later | The reference's own null-count check; test 4 |
| Auto-chunk output differs from whole-frame | Test 1 compares against the whole frame, not the chunked oracle |

Rollback: revert the merge commit.

## 5. Review log

- **Codex plan gate, round 1: REVISE** (2 MEDIUM, 1 LOW). It found no mask-parity defect: 648 comparisons with zero mismatches, and the integer counterexample reproduced. Rev 2:
  - temporal tests pin the expected masks and the ordering errors;
  - an exact admitted-type list (timestamp, date32 and date64 only, with time and duration declined), plus precision, narrow-int, nullable-metadata, all-null-chunk and generated-partition cases;
  - section 1's gate ordering and default-conversion wording corrected;
  - a multi-table regression added.
- **Codex plan gate, round 2: REVISE** (1 MEDIUM, 1 LOW). 703 partition comparisons: 12 mismatches, all on excluded nullable int references. Rev 3:
  - date32/date64 expectations pinned to what the oracle actually does (all-false equality and membership, all-true inequality, ordering errors), plus a compound date-plus-string strict-subset case; timestamps keep the strict-subset requirement;
  - the section 1 dtype table is labeled as default conversion, the int and dictionary rows are corrected, and the "gates already applied" conclusion is removed.
- **Codex plan gate, round 3: GO** (high confidence). Both round-2 findings closed; 208 partition and predicate checks passed. Its note: an all-null date chunk can return all-false for ordering, but populated runs still raise consistently, so the run-level requirement holds.
