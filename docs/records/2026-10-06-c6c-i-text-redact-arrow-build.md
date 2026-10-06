Status: record
Rules consulted: 00-universal, development-loop, testing, risk-and-exceptions

# C6c-i build record: text_redact as an Arrow operator on both native routes

Plan: `docs/plans/2026-10-06-c6c-i-text-redact-arrow.md` (rev 3, Codex GO). Branch `feat/c6c-i-text-redact-arrow`, based on engine main `2adc4d52`. Four commits: acceptance tests (red), implementation, existing-test and docs updates, this record.

## 1. What changed, per plan item

| Item | Change |
|---|---|
| 3a Kernel | `native_text_redact(array, *, detectors, token, label_token)` in `native/_kernels_scalar.py`. It imports the oracle's `iter_spans` and `_splice` (not copies), keeps null as null, applies `str()` to a non-string cell, and passes `list(detectors)` or `None` without re-applying the empty-means-all rule. Output is `pa.string()`. |
| 3b Registry, params, step | `OperatorSpec` for `text_redact` (`native_text_redact`, kernel shape, `ARROW_PYTHON`, no required kernel, no positive evidence, string-only resident type, `null_on_empty` assembly, no routed diagnostics). `TextRedactParams` plus a resolver branch in `native/_operator_params.py` that owns the empty-list-means-all rule and reads the default token from the oracle's `_DEFAULT_TOKEN`. A step branch in `native/_operator_step.py` returns `StepResult(out, None)`. |
| 3b Unified binding | `_TEXT_REDACT` id constant and a `_UNKEYED_PARAMS` entry in `physical/_shadow_operators.py`; `TextRedactParams` added to the unkeyed narrowing in `physical/_shadow_bindings._key_binding`. |
| 3c Admission | `text_redact_config_rejection(name, provider_config)` in `native/_operator_config_rejections.py` with the three coded reasons. Wired into `native/_requirements.py::_config_gate_rejection` and `native/_plan.py::_config_rejection`. `text_redact` added to `_STRING_SOURCE_STRATEGIES` in `native/_real_type_admission.py`, so the chunked downgrade code is `text_redact_source_type_not_string:{col}:{type}`. |
| 3d Output type | `text_redact_pinned_columns(configured)` in `native/_chunked_schema_rule.py`, called inside `build_schema_rule`. It pins an admitted config (and no `when:`) to `string`; a rejected config keeps its oracle type. |
| 3e Evidence | No code. Evidence follows from the registry entry: `arrow_python` planned and executed, `compiled_kernel_executed` false, no `kernel_idle` entry, ordinary call counters. |
| 3f Docs | CHANGELOG entry, compatibility-contract paragraph, a dated note in the rust-coverage audit record. The cross-repo roadmap lives in decoy-platform and is not edited here. |
| Seam sentry | Entry for `native/_kernels_scalar.py` in `permitted_non_physical` (`tests/sentry/test_physical_seam_disconnection.py:501-504`), import-direction checks unchanged. All other changed non-physical modules were already listed. |
| Resolver sentry | `_offences` in `tests/native/test_r1b_resolver_single_source.py` now also flags the literal `"[REDACTED]"` in the three route adapters. |

## 2. Red-before and green-before evidence

Run on the commit with tests only (source untouched), through `pytest-one`.

| File | Python 3.11 (companion) | Python 3.10 |
|---|---|---|
| `tests/native/test_c6c_i_text_redact_kernel.py` | collection error: `ImportError: cannot import name 'text_redact_config_rejection'` (whole file red) | same |
| `tests/native/test_c6c_i_text_redact_chunked.py` | 282 failed, 16 passed | 281 failed, 16 passed, 1 skipped |
| `tests/physical/test_c6c_i_text_redact_unified.py` | 96 failed, 8 passed | 94 failed, 8 passed, 2 skipped |

Green-before tests (24 on 3.11, all stayed green after the change):

- Chunked, 16: `test_excluded_configs_stay_on_the_oracle_and_leave_the_column_unchanged` (4), `test_a_rejected_text_redact_config_is_not_string_pinned` (6), `test_a_text_redact_column_with_a_when_predicate_is_not_string_pinned`, `test_a_when_predicate_keeps_text_redact_off_the_native_route`, `test_output_is_reproducible_and_independent_of_the_mask_key_and_job_seed`, `test_the_auto_router_equals_the_full_frame_run_for_string_and_non_string_sources[int64]`, `test_the_real_type_gate_names_text_redact_and_every_non_string_type` (the generic gate function already formats any strategy name), `test_a_rejected_config_over_an_int64_source_keeps_the_oracle_type_when_chunked`.
- Unified, 8: `test_an_excluded_config_declines_the_lane_and_leaves_the_column_unchanged` (4), `test_a_non_string_source_declines_the_lane_and_equals_the_oracle` (2), `test_a_when_predicate_declines_the_lane_and_leaves_output_unchanged`, `test_output_is_reproducible_and_independent_of_the_mask_key_and_job_seed`.

Everything else in the three files is red on the base, including every parity case, the table-level lift, the exact eligibility codes, the registry and resolver facts, the `text_redact_source_type_not_string` code and the evidence tests. Plan section 5 lists tests 4, 5 and 8 as green-before on the oracle path. That holds for the oracle-output halves (an int64 or large_string source declines and equals the oracle, `when:` stays off native, determinism). The assertions that name the new `text_redact_source_type_not_string` code, and the chunked pin of an all-null column, cannot pass on the base and are red-before.

Two test-authoring slips were fixed before the red run was recorded: an int64 source with a null made pandas widen the chunk to double on the base, and an auto-chunk case with a null in an integer column is routed full-frame by the planner. Both sources were made null-free.

## 3. Existing test edits

No assertion was weakened. Each edit adds the new operator to a table that enumerates operators, or swaps the strategy a test used as "has no native operator" for one that still has none.

| File:line | Old | New | Why the expectation is unchanged |
|---|---|---|---|
| `tests/native/test_operator_tables_snapshot.py:61` | `ALLOWED_OPERATOR_IDS == _NINE_IDS` | `_NINE_IDS \| {"native_text_redact"} == ALLOWED_OPERATOR_IDS` | The nine ids are unchanged; the tenth is added. |
| `:81` | (backend map, 9 entries) | adds `"native_text_redact": "arrow_python"` | Same. |
| `:130`, `:142` | `SLICE_STRATEGIES == _NINE_STRATEGIES`, 9-entry id map | adds `"text_redact"` and `"native_text_redact"` | Same. |
| `:147` | test name `..._has_eight_keys_...` | `..._has_nine_keys_...` | Name tracks the count; `:157` adds `"text_redact": {string}`. |
| `:176`, `:201` | native kernel set; null-on-empty set `{bucket_perturb}` | add `"text_redact"` to each | The registry derives both; the other members are unchanged. |
| `tests/native/test_native_plan_config_aware.py:304`, `:325` | `..._exactly_the_four_native_kernels`, accepted set of four | renamed `..._the_native_kernels_a_bare_config_accepts`; set adds `"text_redact"` | A bare text_redact column has no config the gate rejects, so it is accepted. Every other member is unchanged. |
| `tests/native/test_r1b_resolver_single_source.py:28` | `node.value == "REDACTED" or node.value == "month"` | `node.value in ("REDACTED", "[REDACTED]", "month")` | Strictly wider check, as the plan requires. |
| `tests/sentry/test_unified_backend_map.py:39` | 9-entry backend map | adds `"native_text_redact": ARROW_PYTHON` | Same as the snapshot. |
| `tests/sentry/test_module_size.py:96` | `_requirements.py` census 641 | 644 | The file grew by 3 lines; the gate asks for a visible bump. Over 600 and under 700. |
| `tests/sentry/test_physical_seam_disconnection.py:501-504` | (no entry) | entry for `native/_kernels_scalar.py` | Plan 7 round-3 LOW. |
| `tests/unit/execution/_auto_chunk_strategies.py:165,184-187,192,203,216` | `text_redact` in `REFUSAL`, not in `NATIVE_KEYS`, `STRING_OUTPUT_KEYS`, `PLANNED_BACKEND` | moved into the three native tables, removed from `REFUSAL` | This file classifies each chunk-admitted strategy by the live admission set (its own docstring says so). `test_output_contract_per_strategy[text_redact-*]` still asserts the same dispatcher, legacy and full-frame contract. |
| `tests/unit/execution/test_multi_table_run.py:250-294` | `_failing_text_redact` patches the `text_redact` handler; tables use `text_redact` | `_failing_text_mask`, `text_mask` with a namespace | The tests need a strategy whose handler every route reaches. text_redact now has a native operator and skips the handler. text_mask has none. Both tests keep their error-order assertions. |
| `tests/parity/native/test_phase2_gate.py:430-438` | the non-admitted column is `text_redact` | `text_mask` with a namespace | The test proves that one non-admitted column keeps the whole table on the oracle. It needs a strategy with no native operator. |

## 4. Mutation results (plan test 9)

Hand-run: apply one mutation, run the three new test files on Python 3.11, restore the source from the pre-mutation text, confirm a clean tree. 26 mutants, 26 killed, 0 survived, 0 equivalent. Output of the run is reproduced in the table; "killed by" names the first failing tests.

| Mutant | Mutation | Result | Killed by |
|---|---|---|---|
| M1a | label_token dropped in the resolver | killed | `test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs`, `test_a_text_redact_only_table_runs_the_unified_lane`, `test_native_chunked_equals_oracle_chunked` (+3 more) |
| M1b | label_token dropped in the step | killed | `test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs`, `test_a_text_redact_only_table_runs_the_unified_lane`, `test_native_chunked_equals_oracle_chunked` (+2 more) |
| M1c | label_token dropped in the kernel | killed | `test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs`, `test_a_text_redact_only_table_runs_the_unified_lane`, `test_native_chunked_equals_oracle_chunked` (+3 more) |
| M2 | empty-list-means-all dropped | killed | `test_native_chunked_equals_oracle_chunked`, `test_the_public_empty_detector_list_redacts_the_same_hit_the_empty_tuple_leaves`, `test_the_resolver_applies_the_oracles_normalization_once` (+1 more) |
| M2b | kernel re-applies empty-means-all | killed | `test_a_mixed_hash_and_text_redact_table_runs_the_unified_lane`, `test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs`, `test_a_string_source_all_null_column_is_a_string_on_the_chunked_route` (+3 more) |
| M3 | null check inverted | killed | `test_a_mixed_hash_and_text_redact_table_runs_the_unified_lane`, `test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs`, `test_a_named_detector_subset_and_an_unknown_id_behave_like_the_oracle` (+3 more) |
| M4 | str() coercion dropped | killed | `test_the_kernel_stringifies_non_string_cells_and_keeps_nulls_null` |
| M5a | ner gate removed | killed | `test_a_rejected_text_redact_config_is_not_string_pinned`, `test_the_eligibility_report_gives_the_exact_code_for_each_excluded_config`, `test_the_predicate_names_each_excluded_config` (+1 more) |
| M5b | token gate removed | killed | `test_a_rejected_config_over_an_int64_source_keeps_the_oracle_type_when_chunked`, `test_a_rejected_text_redact_config_is_not_string_pinned`, `test_excluded_configs_stay_on_the_oracle_and_leave_the_column_unchanged` (+3 more) |
| M5c | detectors gate removed | killed | `test_a_rejected_text_redact_config_is_not_string_pinned`, `test_an_excluded_config_declines_the_lane_and_leaves_the_column_unchanged`, `test_excluded_configs_stay_on_the_oracle_and_leave_the_column_unchanged` (+3 more) |
| M5d | requirements dispatcher wiring removed | killed | `test_an_excluded_config_declines_the_lane_and_leaves_the_column_unchanged`, `test_excluded_configs_stay_on_the_oracle_and_leave_the_column_unchanged`, `test_the_requirement_resolver_holds_every_excluded_config_on_the_oracle` |
| M5e | eligibility dispatcher wiring removed | killed | `test_the_eligibility_report_gives_the_exact_code_for_each_excluded_config` |
| M5f | string-source gate removed | killed | `test_a_large_string_source_takes_the_oracle_leg`, `test_an_int64_source_takes_the_oracle_leg_with_the_real_type_code`, `test_the_auto_router_equals_the_full_frame_run_for_string_and_non_string_sources` |
| M6a | pin classifier admits rejected configs | killed | `test_a_rejected_config_over_an_int64_source_keeps_the_oracle_type_when_chunked`, `test_a_rejected_text_redact_config_is_not_string_pinned` |
| M6b | pin classifier ignores when: | killed | `test_a_text_redact_column_with_a_when_predicate_is_not_string_pinned` |
| M6c | pin classifier not wired into the schema rule | killed | `test_an_admitted_text_redact_config_is_string_pinned`, `test_empty_all_null_and_valued_chunks_interleaved_are_identical`, `test_native_chunked_equals_oracle_chunked` |
| M7a | registry assembly tokenizing instead of null_on_empty | killed | `test_the_registry_entry_matches_the_plan`, `test_unified_route_equals_the_oracle` |
| M7b | unified _UNKEYED_PARAMS entry removed | killed | `test_a_mixed_hash_and_text_redact_table_runs_the_unified_lane`, `test_a_text_redact_only_table_runs_the_unified_lane`, `test_a_text_redact_only_table_runs_with_the_compiled_companion_absent` (+3 more) |
| M7c | binding narrowing drops TextRedactParams | killed | `test_the_unified_adapter_binds_text_redact_as_an_unkeyed_operator` |
| M7d | step claims a compiled kernel ran | killed | `test_a_mixed_hash_and_text_redact_table_runs_the_unified_lane`, `test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs`, `test_a_text_redact_only_table_runs_the_unified_lane` (+3 more) |
| M7e | default token changed | killed | `test_a_mixed_hash_and_text_redact_table_runs_the_unified_lane`, `test_a_text_redact_only_table_runs_with_the_compiled_companion_absent`, `test_a_when_predicate_keeps_text_redact_off_the_native_route` (+3 more) |
| M7f | detectors not stringified | killed | `test_the_resolver_applies_the_oracles_normalization_once` |
| M7g | kernel ignores the token | killed | `test_a_named_detector_subset_and_an_unknown_id_behave_like_the_oracle`, `test_detectors_none_runs_every_detector_and_an_empty_tuple_runs_none`, `test_native_chunked_equals_oracle_chunked` (+3 more) |
| M7h | kernel runs every detector regardless of the subset | killed | `test_a_named_detector_subset_and_an_unknown_id_behave_like_the_oracle`, `test_detectors_none_runs_every_detector_and_an_empty_tuple_runs_none`, `test_native_chunked_equals_oracle_chunked` (+2 more) |
| M7i | kernel leaves a tuple of detectors as a non-list | killed | `test_native_chunked_equals_oracle_chunked`, `test_unified_route_equals_the_oracle` |
| M2c | kernel treats an empty tuple as all detectors | killed | `test_detectors_none_runs_every_detector_and_an_empty_tuple_runs_none` |


The plan's required list is covered: `label_token` dropped (M1a, M1b, M1c), empty-list-means-all dropped (M2), null check inverted (M3), `str()` dropped (M4), the 3c gates removed one at a time (M5a to M5f, including both dispatchers and the string-source gate), and the pin classifier admitting rejected configs (M6a, killed by a rejected non-string-token config over an int64 source).

M2b (`list(detectors) or None` in the kernel) is killed because it raises on `None`, not through its own semantics; M2c is the semantic version (`if detectors`) and is killed by the direct kernel contract test.

## 5. Module line counts (newline count, the sentry's own measure)

| Module | Before | After |
|---|---|---|
| `_operator_registry.py` | 186 | 197 |
| `native/_chunked_schema_rule.py` | 237 | 267 |
| `native/_kernels_scalar.py` | 116 | 151 |
| `native/_operator_config_rejections.py` | 268 | 287 |
| `native/_operator_params.py` | 218 | 238 |
| `native/_operator_step.py` | 287 | 297 |
| `native/_plan.py` | 412 | 418 |
| `native/_real_type_admission.py` | 143 | 148 |
| `native/_requirements.py` | 641 | 644 |
| `physical/_shadow_bindings.py` | 306 | 307 |
| `physical/_shadow_operators.py` | 329 | 332 |


Only `_requirements.py` is over 600, and its census entry was bumped from 641 to 644. No new module was added.

## 6. Final test counts

Commands: `/home/cam/bin/pytest-one <python> <path> -q`, one process at a time, at the final commit.

| Path | Python 3.11 (companion present) | Python 3.10 CI mirror (no companion) |
|---|---|---|
| `tests/sentry` | 2340 passed, 1 skipped | 2340 passed, 1 skipped |
| `tests/native` | 5516 passed, 1 skipped | 4248 passed, 1256 skipped |
| `tests/physical` | 1548 passed, 1 skipped | 1084 passed, 465 skipped |
| `tests/unit/execution` | 6375 passed, 4 skipped | 5233 passed, 1146 skipped |
| `tests/parity/native` | 104 passed, 59 xfailed | 40 passed, 64 skipped, 59 xfailed |
| `tests/perf/test_throughput_budgets.py` | 4 passed | 4 passed |
| New tests (kernel, chunked, unified files) | 500 passed | 497 passed, 3 skipped |

No failures in any row. The skips on 3.10 are the existing companion-gated tests plus three of the new ones (`NEEDS_COMPANION`: the hash-plus-faker lift on each route and the hash-plus-text_redact unified case). Every new test that does not need the companion passes on both interpreters.

Extra check outside the required list (3.11 only): the other 14 test files that mention text_redact (`tests/unit/plan`, `tests/unit/storm`, `tests/unit/quality`, `tests/unit/providers_v2`, `tests/parity/test_out_of_core_*`, `tests/parity/test_chunked_substrate_parity.py`, `tests/integration/test_text_redact_e2e.py`, `tests/security/test_text_redact_security.py`, `tests/property/test_mask_invariants.py`): 369 passed, 21 skipped, 0 failed.

Lint at each commit: `ruff check src tests`, `ruff format --check src tests` and `mypy src` (3.10 mirror) all clean.

Not run: the full suite, `tests/perf` beyond the throughput budgets file, and the Codex, dennis and ci-mirror gates the plan lists after the build. No coverage measurement was taken; test strength on the changed units is the hand mutation run in section 4 only.

## 7. Judgment calls

- The existing tests that failed after the change (7 pin or fixture tests in `tests/native`, 4 + 2 + 1 in `tests/unit/execution` and `tests/parity/native`) were all consequences of text_redact becoming native, which the plan states as its purpose. I edited them as listed in section 3 rather than stopping. Each edit adds the new operator or swaps a non-native stand-in. A reviewer should confirm this reading.
- The pin classifier is config-only and ignores the source type, which is the plan's reading ("add text_redact to the string pin only when its config passes 3c"). A text_redact column over a non-string source with an admitted config is therefore pinned to `string` on the oracle chunked leg too. Its output is always strings or nulls, so the pin never changes a value; it changes the type of an empty or all-null chunk to `string`. dennis measured `null` for both on base `run_mask_chunked`; the earlier `double` note for empty chunks was not reproduced there.
- `ner` has no end-to-end run because spaCy is optional and plan compile rejects `ner` without it. Its coded reason is tested through the predicate, the eligibility report and the requirement resolver (`python_only`), which is where both dispatchers read it.
- Test 2a's "companion absent" case patches `native_kernel_availability` in `_unified_slice_admission` to report no kernels. The 3.10 mirror has no companion at all, and the same test passes there for free.
- The kernel does not treat a float NaN in a non-string Arrow array as null (pandas `isna` would). Admission requires a string source, so this is only reachable by calling the kernel directly, and the plan's kernel contract names Arrow nulls only.
- The default-token constant is imported from the oracle handler module into `_operator_params.py`, which pulls pandas into that module's import. `_operator_step` and the date_shift defaults the resolver already imports carry pandas too, and no sentry forbids it.

## dennis gate (round 1: GO, 0 BLOCKER / 0 HIGH / 0 MEDIUM / 2 LOW)

- L1, docs: the "zero-row chunk was `double`" claim did not reproduce on base `run_mask_chunked` (it gave `null`). CHANGELOG and this record now cite the measured `null` behavior. The compatibility contract's "`null` or `double`" wording stays, since it is not wrong.
- L2, design (carried forward, not fixed here): the text_redact silent pass-through rules are encoded in three places (`_operator_config_rejections.py`, the resolver, `_chunked_group_key.py:153-161`). `_chunked_group_key` cannot simply call `text_redact_config_rejection`: that predicate also rejects `ner`, and an `ner` config still stringifies its column rather than passing it through. The follow-up is a shared `text_redact_passes_source_through(cfg)` helper used by both, recorded for C6c-ii.
- Pre-existing, noted by dennis: the oracle stringifies an int64 source with nulls as `'1.0'` but a null-free chunk as `'1'`, so whole-frame and chunked can differ for sparse-null integer columns under text_redact. Not changed by this slice (non-string sources stay on the oracle).
