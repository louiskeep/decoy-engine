Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, observability-and-resilience, code-review.

# C8-iii-a: `when:` for text_redact, bucket_perturb and date_shift on both native routes

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. It follows C8-i (#214, chunked `when`) and C8-ii (#217, unified `when`), which admitted hash, redact, truncate and deterministic categorical. Branch `feat/c8-iii-a-when-operators` off engine main `2ed9eb4c`. Risk R2: wider admission on the default-on routes.

C8-iii was split into three slices after a code survey (2026-10-07):
- **this slice:** three more value-keyed operators;
- **C8-iii-b:** numeric predicate references on auto-chunked tables, which is a real chunk-stability defect today, shown below;
- **C8-iii-c:** moving the raw-dict path onto the closed grammar. That is an owner decision kept open in C8-i.

## 1. Goal and scope

A `when:` column whose strategy is text_redact, bucket_perturb or date_shift runs natively on the chunked and unified routes. Its output, row errors, warnings and metrics must be identical to the oracle.

**In scope**, each with its existing native config gate unchanged:
- **text_redact:** a string source, no NER;
- **bucket_perturb:** a string source with an explicit `date_format` that is a strftime pattern. Pandas' special format names `mixed` and `ISO8601` are excluded (3a-ii);
- **date_shift:** a string source with an explicit `date_format`, no `group_by`, and not windowed_date.

**Out of scope:**
- positional categorical and positional Faker. They are keyed on the row ordinal, and under the oracle gate a handler sees SUBSET ordinals (`ctx.row_offset + i`; `gated_context` never changes `row_offset`, `execution/_exact_int_faker.py:16-27`). So select-then-scatter cannot reproduce them;
- deterministic Faker. The native routes build every Faker pool up front (`native/_chunk_masking.py:~261-310`), while the oracle builds it inside `handler.run`, which a zero-match gate never reaches (`_when_gate.py:207-208`). A pool-build failure would surface natively but not on the oracle. That needs its own design;
- group_key, text_mask, code_set, top_code and windowed_date. These keep their rejections.

## 2. Established facts (survey 2026-10-07)

**The oracle gate** (`_when_gate.py`):
- zero matches return `df` before `handler.run`, with no warning (`:207-208`);
- the handler runs on `df.loc[mask].copy()` (`:211-213`);
- row errors are remapped to full-table positions (`:219-221`);
- `.loc` write-back (`:222`).

**text_redact** (`_strategies/_text_redact.py`):
- one `iter_spans` call per cell (`:150-156`), so it is value-keyed;
- writes an object Series (`:187`), and string targets stay the same dtype;
- returns `df, []`, so it adds no row errors and no warnings (`:188`);
- the native gate declines `ner` (`native/_operator_config_rejections.py:261-276`).

**bucket_perturb** (`_strategies/_bucket_perturb.py`, `transforms/bucket_perturb.py`):
- value-keyed per row;
- format detection runs only when `date_format` is unset (`:150`), and native requires an explicit format (`_operator_config_rejections.py:87-89`);
- `astype(object)`, and unparseable and null cells are kept (`:162-170`);
- no row errors and no warnings;
- the chunked step retypes an all-null or empty bucket_perturb output to Arrow `null` (`native/_chunk_masking.py:~176-180`);
- today `when` is rejected with `chunked_bucket_perturb_when_not_supported` (`_chunked_bucket_perturb.py:199-221`) and with `when_predicate_not_native`.

**date_shift** (`_strategies/_date_shift.py`):
- value-keyed when `group_by` is unset and the format is explicit. With `group_by` it reads a pre-mask sibling snapshot, and compile rejects that case with `date_shift_group_by_with_when_unsupported` (`plan/_checks_date_shift.py:201-205`);
- unparseable and null cells restore the source value (`:215-217`);
- **row errors:** each non-null unparseable cell records `RowError(trigger="format_error")` at its subset index (`:218-226`), and the gate remaps the index.

**Native masked step** (`native/_operator_step.py:~410-431`):
- select-then-scatter;
- **gap:** it drops `result.format_error_positions` (the unmasked path sets it, `:398`; the field is at `:232`). Those positions would be subset-relative;
- `native/_chunk_masking.py:193-199` raises if positions exist without a channel.

**Admission:**
- `ADMITTED_WHEN_STRATEGIES = {hash, redact, truncate, categorical}` (`native/_when_admission.py:38`), and all other strategies get `when_predicate_not_native:<col>` (`:130-134`);
- all three operators in scope already have `unified_resident_types=_STRING_ONLY` (`_operator_registry.py:138, 184, 206`);
- the unified route computes the `when` mask once on the full frame (`_unified_slice_when.py:73-98`), and its reconstruction replays the oracle `.loc` write-back (C8-ii).

**C8-iii-b evidence** (not fixed here). With `x == 9007199254740992` and `v = 2**53 + 1`:
- the whole frame `[v, None, v, 1]` widens to float64 and gives mask `[T, F, T, F]`;
- a null-free chunk `[v, v]` stays int64 and gives `[F, F]`;
- the chunk `[None, v]` gives `[F, T]`.

The planner's string-only reference rule (`native/_when_admission.planner_relaxed_when_columns`, `:188-217`) is what keeps such tables off auto-chunking today.

## 3. Decisions

**3a. Admission.**
- `ADMITTED_WHEN_STRATEGIES` gains text_redact, bucket_perturb and date_shift.
- Each still passes its own existing native config gate (`_column_rejection`), so NER text_redact, implicit-format bucket_perturb and date_shift, date_shift with `group_by`, and windowed_date keep declining with today's codes.
- `chunked_bucket_perturb_when_not_supported` stays for configs this slice does not admit. The builder lists every rejection site touched.

**3a-ii. Fix at the source: bucket_perturb special formats (Codex round 1, HIGH 2).**

`bucket_perturb_config_rejection` (`native/_operator_config_rejections.py:63-`) rejects `%z`/`%Z` but admits pandas' special format names `mixed` and `ISO8601`. Codex reproduced the case: with selected values `2024-01-15T12:00:00+01:00` and `2024-07-15T12:00:00+02:00` (ordinary seasonal offsets), the oracle succeeds while native execution raises a `ValueError` when it builds the `DatetimeIndex`. Splitting the values across batches changes the failure. This is an EXISTING defect on today's unmasked native bucket_perturb route, not something `when` introduces.

The gate therefore rejects `date_format` values `mixed` and `ISO8601` with a new code, `bucket_perturb_special_date_format:<col>`, for masked and unmasked columns alike. Those columns run on the oracle, which handles them, and output equals main's oracle output. date_shift's gate already excludes them, so the two gates become consistent.

**3b. Row errors through the masked step.**
- `run_kernel_step_masked` carries `format_error_positions`, remapped from subset positions to chunk or batch positions: `selected_positions[p]`, where `selected_positions = np.flatnonzero(mask)`.
- Rows the predicate does not select never produce row errors, which matches the oracle: the handler only sees the subset.
- **Attribution (Codex round 1, HIGH 1).** The remapped positions are local to the chunk or batch.
  - **Chunked route:** row errors stay CHUNK-LOCAL, exactly as the chunked oracle reports them. The gate remaps into its input frame, which is the chunk, and `drain_row_errors` and `native/_chunked_row_errors.format_error_records` keep that local index.
  - **Unified route:** the batch offset is added exactly ONCE, by the existing `rebase_row_errors`.

**3c. Degenerate outputs (Codex round 1, MEDIUM 3).**
- The intermediate all-null or empty retype inside `_mask_chunk_native` stays.
- Admitted `when:` columns are string-pinned by C8's shared output normalization (`when_pinned_columns` then `normalize_chunk`). The EMITTED chunked schema is therefore `string` on both legs, even when the column is all-null or empty.
- The unified route reconciles through `_shadow_assembly` and the C8-ii replay.
- Test 5 asserts the emitted schemas after normalization, not the intermediate types.

**3d. Planner.** `planner_relaxed_when_columns` keeps its string-only reference rule. The three operators are relaxed only under the same conditions as the existing four. Numeric references are C8-iii-b.

**3e. Logs.** No data values in logs. The sentry is unchanged.

## 4. Acceptance tests (written first; never weakened)

Differential = lane-on against an explicit lane-off run on the same route, comparing:
- tables byte-equal, including schema and `b"pandas"` metadata;
- warnings;
- row errors, including indices and triggers;
- metrics, excluding timings and the activation leaf.

Admitted cases poison the oracle fallback, so any reroute fails the test.

1. **Matrix on both routes:**
   - the three operators;
   - selectivity of 0, partial and all;
   - selected and unselected nulls;
   - references: the target, a string sibling, `in` / `not in`;
   - several chunks or batches, ragged sizes, and an empty table.
2. **date_shift row errors:**
   - unparseable values in selected rows give row errors whose indices equal the oracle's on the same route: chunk-local on the chunked route, table-global on unified;
   - unparseable values in unselected rows give no row errors and keep their source values;
   - a LATER failing chunk with a nonzero `base_row_offset` and a sparse selection, including fail-before-yield behavior;
   - unified errors across several batches.
3. **text_redact** (Codex round 1, LOW 4: custom spec objects are NOT wired through the strategy, so this slice claims no support for them):
   - empty detector list means all detectors;
   - unknown detector IDs are skipped;
   - overlapping spans;
   - detector-order tie resolution with `label_token`;
   - literal replacement tokens;
   - spans in selected and unselected rows (unselected rows unchanged).
4. **bucket_perturb:**
   - explicit strftime formats are admitted, and only selected rows are bucketed;
   - `mixed` and `ISO8601` decline with the new code, both masked and UNMASKED, and their outputs equal lane-off on both routes. Cases: mixed-offset values together in one batch, split across chunks and batches, and under a zero-match gate.
5. **Degenerate outputs:** an all-null source, an empty table, a chunk where every selected row is null, and selected nulls beside unselected values. Each case checks the EMITTED schema after normalization (string-pinned on chunked) and the unified reconstruction, including pandas string metadata.
6. **Declines unchanged**, outcomes equal to lane-off:
   - NER text_redact;
   - implicit-format bucket_perturb and date_shift;
   - date_shift with `group_by` (the compile rejection);
   - windowed_date;
   - positional categorical, positional Faker and deterministic Faker under `when`;
   - group_key, text_mask, code_set and top_code under `when`.
7. **Old decline tests** (from the survey list: `test_c6c_i_text_redact_chunked.py:307, 388`, `test_chunked_date_shift_admission.py:474`, `test_chunked_bucket_perturb_admission.py:276`, `test_bucket_perturb_chunked.py:481, 492`, `test_c8_i_when_declines.py:117`, `test_planner_mutation_kills.py:148`, `test_c8_ii_unified_when.py:733, 740`):
   - each changes ONLY where the config is now admitted;
   - every decline that still applies keeps its test;
   - the record lists each change.
8. **Testflight:** STOP if a fingerprint moves.
9. **Sentries; mutation** on the admission set, the position remap and the degenerate retype; a **perf record** at 1M rows, 10% selectivity, one per operator.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| Row errors at the wrong index or for unselected rows | 3b remap; test 2 |
| Degenerate output type drift | 3c; test 5 |
| An operator with a non-value-keyed config admitted | Existing config gates plus scope; test 6 |
| A dropped decline test | Test 7 rule |

Rollback: revert the merge commit.

## 6. Review log

- **Codex plan gate, round 1: REVISE** (2 HIGH, 1 MEDIUM, 1 LOW). Rev 2:
  - **HIGH 1:** chunked row errors stay chunk-local, and unified adds the batch offset once (3b, test 2).
  - **HIGH 2:** bucket_perturb's special formats `mixed` and `ISO8601` are rejected at the shared config gate for masked AND unmasked columns. This fixes an existing native defect at its source (3a-ii, test 4).
  - **MEDIUM 3:** emitted schemas are asserted after C8's string-pin normalization (3c, test 5).
  - **LOW 4:** no claim of custom-spec support; explicit detector cases (test 3).
