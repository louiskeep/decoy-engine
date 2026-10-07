Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, code-review.

# C8-iii-b: auto-chunk `when:` columns whose predicate reads non-string columns

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. This follows C8-i/ii/iii-a. Branch `feat/c8-iii-b-numeric-refs` off engine main `4a08f570`. Risk R2: a wider auto-chunk route on a default path.

## 1. Problem and the survey that narrowed it

The planner keeps a `when:` column off auto-chunking unless the target AND every reference is exactly `pa.string()` (`native/_when_admission.planner_relaxed_when_columns`, `:191-220`; the reason `when_predicate_not_chunk_stable` is emitted at `_planner.py:414`).

The concern behind the rule:
- the per-chunk `when` mask converts each chunk's references with `to_pandas_fk_safe` (`native/_when_mask.py:50-60`; the chunked oracle leg does the same at `_pandas_adapter.py:213`);
- an int64 reference with nulls in SOME chunks becomes float64 in those chunks and stays int64 in others;
- so a predicate near 2**53 can select differently per chunk than on the whole frame. The survey (2026-10-07) reproduced this: whole frame `[T,F,T,F]` against chunk `[F,F]`.

The survey also found that the auto-chunk route ALREADY declines any table with an integer column that has nulls, table-wide, through `_planner.py:496-501`. A lazy integer column with no footer null count declines as well (`:490`, via `_chunked_input` facts). So on every table that reaches the `when` rule, integer columns are null-free, and integer references therefore keep one dtype in every chunk.

Measured with `to_pandas_fk_safe` (pandas 2.3.3, pyarrow 24.0.0):

| Reference type | Dtype stable across null-free and null-bearing chunks? |
|---|---|
| int, uint | Yes, once null-free is guaranteed by the gate above |
| float32, float64 | Yes |
| timestamp | Yes (datetime64) |
| date32 | Yes (object either way) |
| string | Yes |
| large_string | Yes |
| bool | No: bool when null-free, object when null-bearing |
| decimal, dictionary, nested | Never reach this rule, because the runtime gate declines the table first (`:504-510`) |

So the string-only rule is stricter than the real hazard requires. The goal is to relax it to the reference types whose chunk conversion is provably stable, given the gates already applied.

## 2. Decision

**2a. The relaxed reference rule.**
- `planner_relaxed_when_columns` relaxes an admitted `when` column when every reference is in the source schema and each one is one of:
  - `pa.string()` or `pa.large_string()`;
  - float of any width;
  - temporal (timestamp of any unit and timezone, date32, date64);
  - signed or unsigned integer whose whole-column null count is KNOWN to be 0;
  - bool whose whole-column null count is KNOWN to be 0.
- The null counts come from `facts_for(...).null_count(name)`: the exact footer count for Parquet `LazySource`, and the exact `null_count` for a resident table. An UNKNOWN count (`None`) never relaxes an integer or bool reference.
- The target rule is unchanged: the target must still be `pa.string()`.
- Rules 1, 3 and 4 (strategy and config gate, closed grammar, no earlier writer) are unchanged.

**2b. Why the integer check is repeated.** The table-wide integer gate already holds today, but the relax rule checks the reference's null count itself. If that gate is ever loosened, this rule must not silently start relaxing integer references with nulls. Test 4 pins this.

**2c. Explicit chunked entry is unchanged.** It already admits numeric references and compares against the chunked oracle (`test_c8_i_when_declines.py:285`). Per-chunk evaluation there is the user's explicit choice, documented as such.

**2d. Multi-table split.** It uses the same rule per table (`_pipeline_multi_table.py:193`), with no extra change.

**2e. Docs.** The CHANGELOG, plus `docs/strategies.md`'s `when:` section listing which reference types auto-chunk.

## 3. Acceptance tests (written first; never weakened)

Every case runs on the auto route, compares lane-on (auto-chunked) output byte-for-byte against the WHOLE-FRAME oracle run, and asserts the route taken.

1. **Relaxed and auto-chunked:**
   - int64 and uint64 references, null-free, including values above 2**53 and uint64 above 2**63. The uint64 numexpr quirk is the same in both runs, so the outputs still match;
   - float64 references with NaN and nulls, using `!=`, `==` and `in`;
   - timestamp references, tz-aware and naive, and date32 references, compared against string literals;
   - null-free bool references;
   - large_string references;
   - mixed: one string reference plus one int reference.

   Each case runs on resident and Parquet-lazy sources, with several chunks and ragged sizes.
2. **Still declined, with the existing reason:**
   - a bool reference with nulls;
   - a bool or int reference whose null count is unknown (Parquet written without null-count statistics);
   - a dictionary reference;
   - a reference masked earlier (rule 4);
   - a predicate outside the grammar;
   - a non-string target.
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
