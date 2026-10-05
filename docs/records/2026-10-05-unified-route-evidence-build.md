Status: record

# Unified-slice route evidence: build record

Plan: `docs/plans/2026-10-05-unified-route-evidence.md` rev 2.1 (Codex plan gate GO). Branch `feat/unified-route-evidence`, off engine main `98f6c03e`. Built tests first.

## Commits

1. `da1e9ceb` tests: the acceptance tests and the three allowed test edits, all red before the implementation.
2. `d0cb3a21` feature: shared executed-backend rule, operator-to-backend map, truthful and monotonic compiled flags, `assemble_node_evidence`, census and seam-list updates.
3. `106078e7` pure move: the source-shaped output reconstruction moves to `_unified_slice_evidence.py`.
4. Docs commit: CHANGELOG entry and this record.

## New tests (71)

- `tests/physical/test_unified_route_evidence.py` (49): exact seven-key evidence per operator and for a mixed table, idle rules on the production lane (oracle poisoned, output compared byte-for-byte with a lane-off run), the all-unparseable date_shift case at the coordinator seam plus the existing `RowErrorsFailedError` parity, batch accumulation at the seam for bucket_perturb and date_shift, the 50,000-valid-then-null production case, the direct `run_operator` group_key monotonic test, `calls` for 50,001 rows and 1 row.
- `tests/physical/test_unified_slice_evidence_unit.py` (19): `assemble_node_evidence` as a pure seam. Timing attribution with two same-strategy columns and shuffled records, every invariant raise, idle kernels not raising, and the shared-rule test (one monkeypatch of `_chunked_evidence.executed_backend` is seen by both routes).
- `tests/sentry/test_unified_backend_map.py` (3): the map keys equal `ALLOWED_OPERATOR_IDS`.

## Red before

Run on commit 1's tree against the unmodified source: 73 failed, 194 passed, 1 skipped across the new files plus the three edited files.

- 48 of the 73 are the new production-lane and seam tests failing on the missing keys (`planned_backend`, `executed_backend`, `calls`, `elapsed_ms`).
- 19 unit tests and the 3 sentry tests fail on a missing module or attribute (`_unified_slice_evidence`, `BACKEND_BY_OPERATOR_ID`, `executed_backend`).
- The three allowed edits fail for the plan's stated reasons: the faker whole-dict assertions in `test_unified_slice_parity.py` and `test_unified_slice_faker.py` (new keys absent), and the `empty` and `all_null` shapes in `test_shadow_date_shift.py` (flag still `True`).
- The one new test that already passed: `test_group_key_flag_is_monotonic_across_run_operator_calls[empty_then_populated]` (the old overwrite is correct for that order).

## Green after

- New 71 tests: all pass.
- `tests/physical` plus `tests/sentry`: 3686 passed, 2 skipped (companion venv).
- `tests/unit/execution` plus the `tests/native` chunked and dispatch evidence files (admission, entry-evidence, dispatch, dispatch_faker, nondet parity): 500 passed.
- `tests/parity/native` gate files (phase2, e2e certification, c1 faker, phase3 c1) plus all of `tests/unit/execution` and the chunked evidence tests: 6386 passed, 4 skipped, 2 failed (see Stopped on).
- `ruff check`, `ruff format`, `mypy src`: clean.

## Mutants (hand-applied one at a time, then reverted)

All killed.

| Mutant | First failing test |
|---|---|
| Faker mapped to `rust_companion` | `test_exact_evidence_per_operator[native_faker_select]` |
| idle treated as planned | `test_empty_table_is_idle_for_value_dependent_kernels[native_bucket_perturb]` |
| bucket_perturb spy dropped (flag `True`) | same idle test |
| date_shift spy dropped (flag `True`) | `test_empty_table_is_idle_for_value_dependent_kernels[native_date_shift]` |
| overwrite instead of accumulate, bucket_perturb | `test_compiled_flag_accumulates_across_batches[valued_then_idle-native_bucket_perturb]` |
| overwrite instead of accumulate, date_shift | `...[valued_then_idle-native_date_shift]` |
| overwrite instead of accumulate, group_key | `test_group_key_flag_is_monotonic_across_run_operator_calls[populated_then_empty]` |
| raise on idle group_key | `test_empty_table_is_idle_for_value_dependent_kernels[native_group_key]` |
| `calls` constant 1 | `test_all_unparseable_date_shift_is_idle_at_the_seam` |
| timings joined by strategy only | `test_elapsed_ms_is_attributed_by_strategy_and_column` (both parametrizations) |
| bijection check skipped | `test_a_missing_timing_record_raises` |
| D7 dropped | `test_a_d7_miss_raises[hash]` |

The first strategy-only mutant I tried also broke the column-keyed lookup, so it failed everywhere. I redid it as a faithful strategy-only join and recorded that result.

## Pure-move diff check (commit 3)

The moved block is the comment and code from `# CHANGE 2 (hardened D9 fix)` through `outputs = {...}`. After dedenting 4 spaces and renaming `physical_table.nodes` to `nodes` and `candidate.table` to `table`, a line diff against the pre-move file is empty. Differences by design: the added `return outputs`, and the three lines that stay in the caller (`bridge_t0 = time.perf_counter()`, `frame = candidate.source_frame`, `masked_table = shadow_result.outputs[...]`), which are now keyword arguments. The caller keeps the clock, so `test_unified_slice_timings.py`'s clock patch on `_unified_slice` still applies unchanged. `candidate.source_frame` is still handed over once and mutated in place by the callee, as before.

`_unified_slice.py` is now 595 lines, below the 600 goal, so its census entry and the owed-split note are deleted.

## Judgment calls

- `executed_backend(planned_backend, *, native_admitted, kernel_idle)` is the shared helper's signature. The chunked caller passes `c.column in idle`. The unified assembler calls it through the module attribute, which is what lets one monkeypatch reach both routes.
- Idle is computed in the assembler as "planned backend is not `arrow_python` and no compiled call ran". Arrow operators never count as idle, so redact, truncate and passthrough stay `arrow_python` with `compiled_kernel_executed=False`.
- `_unified_slice_evidence.py` is in `DELIBERATELY_CONNECTED_MODULES` (the seam-disconnection sentry). It imports physical types only under `TYPE_CHECKING`, and the sentry's regex counts that. The plan's step 6 anticipated this edit.
- The D7 operator set moved with the validation, from `_unified_slice.py` to the evidence module. Nothing else referenced it.
- Whole-dict test edits keep the exact-dict form: `elapsed_ms` is popped and asserted as a float of at least zero, then the remaining six keys are compared exactly.
- The compatibility contract lists no unified-slice node keys, so it is unchanged.

## Stopped on

Two existing tests outside the plan's allowed edit list fail, because they compare quality metrics between two runs and `elapsed_ms` is wall time:

- `tests/unit/execution/test_auto_chunk_dispatcher.py::test_non_routed_jobs_have_equal_quality_metrics_for_any_valid_knobs` compares the whole `quality_metrics` dict of two runs.
- `tests/unit/execution/test_b6b_modes.py::test_a_lazy_table_below_the_threshold_is_resolved_before_the_unified_slice` compares the whole `unified_slice_activation` leaf of two runs.

Plan fact 5 said three tests would need updating and missed these two. Both only assert that two runs agree, so the fix is to drop `elapsed_ms` from each node before comparing (a one-line normalizer per test). I did not make that edit because the plan forbids edits beyond its list. Every value, schema and parity assertion in them would stay as is.
