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
- **bucket_perturb:** a string source with an explicit `date_format`;
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

**3b. Row errors through the masked step.**
- `run_kernel_step_masked` carries `format_error_positions`, remapped from subset positions to chunk or batch positions: `selected_positions[p]`, where `selected_positions = np.flatnonzero(mask)`.
- Rows the predicate does not select never produce row errors, which matches the oracle: the handler only sees the subset.
- Both routes then attribute the positions exactly as they do for unmasked date_shift: the chunk base on the chunked route, `row_offset` on unified.

**3c. Degenerate outputs.**
- On the chunked route, the all-null or empty retype for bucket_perturb applies to the FINAL masked column, the same rule as unmasked. Unselected rows keep their source values, so a column is all-null only if the source was.
- The unified route reconciles through `_shadow_assembly` as it already does.
- Test 5 pins both.

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
   - unparseable values in selected rows give row errors at full-table indices;
   - unparseable values in unselected rows give no row errors and keep their source values;
   - errors are checked across several chunks with nonzero chunk bases, and on unified across several batches.
3. **text_redact:** the detector set; custom detectors; a value with spans in selected rows and unselected rows (unselected rows unchanged).
4. **bucket_perturb:** the explicit-format admission; bucketed values in selected rows only.
5. **Degenerate outputs:** an all-null source, an empty table, and a chunk where every selected row is null. Each matches the oracle on each route.
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
