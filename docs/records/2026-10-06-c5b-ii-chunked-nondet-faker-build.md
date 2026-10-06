# C5b-ii chunked non-deterministic REUSE Faker: build record

Status: record

Date: 2026-10-06. Plan: `docs/plans/2026-10-06-c5b-ii-chunked-nondet-faker.md` revision 4 (Codex round 4 GO). Branch `feat/c5b-ii-chunked-nondet-faker` off engine main `5095bb3d` (R1b and C6c-i merged). Risk R2. Gates pending at the time of writing: dennis, then Codex final. Nothing pushed or merged.

## What shipped, per plan section 3

- **3a, stage A (config only).** New `execution/native/_faker_positional_admission.py`: `PositionalFakerConfig` (the configured namespace only), `positional_faker_config_of_entry`, `positional_faker_config_for_column`, `is_positional_faker_entry` and `positional_faker_failures`. Admitted: a faker column that is not deterministic, REUSE or mode-absent, with an explicit `pool_size` and a provider in the C1 allowlist; the namespace is optional (`None` and `""` both mean unset). `allow_collisions` counts as deterministic, because the plan compiler turns it into `deterministic=True`, so it never reaches stage A. Nested and composite columns fail on the strategy and the allowlist. A REUSE column that fails stage A keeps `chunked_strategy_conditions_unmet`; its text names the missing input ("position-keyed faker requires an explicit pool_size ..." or "provider X is not in the chunked allowlist ...") and the "deferred to C5b-ii" wording is gone.
- **3b, consumers.** The compatibility veto (`_conditional_admission_failures`), `_static_route_decision`'s positional exception, `plan_column_backends` and the string pin all read stage A. The two route consumers share one new helper, `chunked_positional_column(config, table, strategy, column)`, which now also covers the seeded categorical that previously had its own copy in each site. `_requirements.py`, `prepare_categorical`, `_shadow_bindings` and `_shadow_operators` are untouched.
- **3c, stage B and the eager pool check.** A string source runs native; any other source takes the existing `faker_source_type_not_string` downgrade. `reject_nonstring_positional_pools(state, table=)` in `_chunk_masking.py` resolves every positional faker column's pool once (configured namespace, `job_seed`, the caller's registry) and raises `ExecutionError(code="chunked_faker_nondeterministic_pool_not_string")` for a non-string pool. It is called from `_run_chunked` right after the empty-input return and before `plan_native_route`, so it runs on both legs, whether or not the table is native-admitted, before any masking, normalization or write. The pools sit in `state.pool_cache`, so the leg that runs reuses them.
- **3d, `when:` gate.** `reject_nondeterministic_faker_when` (same module), called next to the categorical gate in `check_chunked_compatibility`. Code `chunked_faker_nondeterministic_when_not_supported`.
- **3e, params and the shared step.** `FakerParams` gains `positional` and `selection_namespace`. `resolve_params_by_column` takes a `table` keyword and, for a non-deterministic REUSE seed (`is_positional_faker_seed`, the oracle's own gate), resolves `selection_namespace` with the oracle's `faker_selection_namespace`. `run_kernel_step` gains `job_seed`; the positional branch asserts the pool, `job_seed` and selection namespace first, returns a typed empty array with `ran=False` for a zero-row source, and otherwise calls the new `sample_faker_array_positional` (dense `uint64` keys via `positional_key_array(..., code="faker_position_out_of_domain")`, `derive_index_batch(mask_key=job_seed)`, gather, nulls restored from the source). The index-array shape checks and the pool gather moved into two private helpers shared with `sample_faker_array`; the deterministic path is behavior-identical (the existing `test_dispatch_faker` malformed-kernel cases, `test_sample_faker_array` and the R1b cross-route baseline stay green, and a new test pins step equals direct sampler). `_mask_chunk_native` passes `job_seed`; evidence: a run sets `pool_select_executed` and counts a call; an idle zero-row chunk joins `kernel_idle` and is uncounted.
- **3f, output type.** `faker_positional_pinned_columns(configured)` in `_chunked_schema_rule.py`, unioned into `string_columns` by `build_schema_rule`, so the dispatcher entry, the oracle leg and the streamed sink pin identically with or without the companion. Tests assert the plan's type table literally.
- **3g, docs.** CHANGELOG entry, `docs/determinism.md`, `docs/strategies.md`, `docs/compatibility-contract.md`, the `_chunked.py` module docstring and code list, and `docs/native/draw-site-inventory.md` plus a `mirror_call_sites` entry on `mask.faker_nondeterministic` (the native mirror, `_operator_step.py:148`).

The platform repo's roadmap (C5b-ii shipped, C5b-iii added) is not touched here: it lives in another checkout. The caller needs to do that step.

## Red-before and green-before

Tests were written and committed first (`d19e1e85`) and run against the unchanged source.

- Python 3.11 (companion): 233 tests, 198 failed, 35 passed. Python 3.10 (no companion): 69 failed, 34 passed, 130 skipped.
- Red-before, for the stated reasons: every parity, offset, KAT, evidence, auto-route, split, leg-selection, `when:`, override-fail-closed, resolver and step test failed with the old veto text ("deferred to C5b-ii"), a missing module (`_faker_positional_admission`), a missing `sample_faker_array_positional`, or a `FakerParams`/`resolve_params_by_column` signature error.
- Green-before by design (they assert behavior that must not change): the plan's named cases (the pool-identity pair of test 3, the literal default-namespace check, test 6c, test 11) plus the negative halves of tests 4, 5 and 7 (a column that fails stage A still gets the retained veto before any chunk and never reaches the oracle), the child-key FK rejection, the deterministic-override downgrade, deterministic faker with `when:`, the whole-column mode text, the below-threshold split group and the deterministic step path.
- Added after the build, from the mutation pass, so they have no red-before: the `allow_collisions` case, the zero-row-with-no-`job_seed` step case, the whole-column-mode resolver case and the malformed-kernel cases of the positional sampler.

One deviation from the plan's list: the first draft of test 6c also asserted that the whole-frame rejection text names the provider, which fails on the base. That assertion lives in test 5 now, so 6c is green-before as the plan says.

## Existing-test edits

Each keeps its assertion intent; each pinned the old routing that this slice changes on purpose (plan section 1).

1. `tests/native/test_c5b_i_metadata_inventory.py:214` `test_chunked_faker_rejection_prose_is_truthful_per_mode`. Old: a complete REUSE entry (pool_size 10) must say "position-keyed" and "C5b-ii". New: the REUSE case is read from an entry without `pool_size` (a complete one is admitted and has no failure text), and asserts "position-keyed" present and "C5b-ii" absent; the `unique` case passes `pool_size=10`. Why: the deferral is gone.
2. `tests/native/test_chunked_categorical_admission.py:168` `test_faker_determinism_condition_is_unchanged`. Old: a faker without `deterministic` fails the veto. New: the same dict plus `cardinality_mode: "unique"`. Why: a non-deterministic REUSE faker with pool_size is now admitted; the whole-column modes still require `deterministic: true`.
3. `tests/native/test_dispatch_faker.py:186` `test_non_c1_faker_variant_stays_on_oracle`. Old: the `non_deterministic` and `deterministic_omitted` params must reroute to the oracle. New: those two params moved to a new `test_non_deterministic_reuse_faker_is_admitted_as_the_position_keyed_variant` (line 218, asserts admitted and `native_pool`, companion-only like its neighbours); the other params are unchanged. `:343` `test_one_non_c1_faker_column_reroutes_whole_table_not_just_that_column`: the second column was non-deterministic with a namespace and pool_size (now admitted), so it is now deterministic with `pool_size=None` (still non-C1); the assertions are unchanged.
4. `tests/unit/execution/test_faker_positional_nondet.py`, class `TestRoutingConstant` and `TestMultiTable`. `_route_config:578` gains a `pool_size` keyword. `_ROUTE_CASES:604` loses its two REUSE rows (`unique`, `match`, `scale` keep their counts and text); a new `test_a_reuse_column_without_a_pool_size_keeps_the_veto_and_runs_full_frame:647` keeps the REUSE routing pinned for a column the veto still rejects (classify_job `pandas_fallback`, `chunked_strategy_conditions_unmet`, end to end full-frame). `test_chunked_check_still_raises_the_same_code:693` drops the `reuse` param (it now passes the check). `TestMultiTable:1019` is renamed to `..._the_faker_table_runs_chunked_on_the_same_ordinals`: the dispatched list is now `["fk", "big", "big2"]` and all three tables compare to the split-off run without metadata (a dispatched table never carries pandas metadata); the value assertion against the whole-frame ordinals is unchanged.
5. `tests/sentry/test_module_size.py:88-91`: `_chunked.py` 619 to 626; new entry `native/_chunked_entry.py` 606. `tests/sentry/test_physical_seam_disconnection.py:288`: `_faker_positional_admission.py` joins the permitted list, as `_categorical_positional.py` did.

## Mutation check (plan test 13), by hand

Each mutant was applied to the committed source, the four new test files (plus named existing files where noted) were run on Python 3.11, and the file was restored (`git status` clean after every run). Killers are the first failing tests.

| Mutant | Result |
|---|---|
| M1 `mask_key` used instead of `job_seed` (step call) | killed: `test_native_chunked_equals_oracle_chunked[...]`, `test_the_key_is_job_seed_never_the_mask_key`, auto-route equality |
| M2 `row_offset` dropped (sampler keys from 0) | killed: base-offset and UINT64 KAT tests, split test |
| M3 configured namespace used for selection when None | killed: default-namespace parity cases, split test, KAT |
| M4 pool identity (cache key) computed on the selection namespace | Initially recorded as an equivalent survivor; dennis showed it is NOT equivalent: a sibling column whose configured namespace equals this column's default selection namespace caches its pool under that identity, so the mutant would draw from the sibling's pool. Now killed by `test_a_default_namespace_never_reuses_a_sibling_pool_keyed_by_the_same_string` (added at the gate). |
| M4b pool BUILD on the selection namespace (the plan's "pool built on the selection namespace") | killed: default-namespace parity cases, pool-equality, split test |
| M5 nulls not restored | killed: null-bearing parity cases, auto-route |
| M6 `ran` on zero rows | killed: `test_a_zero_row_source_returns_a_typed_empty_array_without_calling_the_kernel`, `test_a_run_reports_pool_select_per_non_empty_chunk` |
| M7 `native_admitted` guard restored around the pool validation | killed: the non-string-source, companion-absent and other-column override tests (the native-admitted case still passes, as expected) |
| M8 provider allowlist check dropped | killed: stage-A and 6c tests |
| M9 `allow_collisions` guard dropped | killed: `test_the_allow_collisions_alias_...` |
| M10 string pin dropped | killed: all-null and zero-row parity cases, 6b |
| M11 `when:` gate a no-op | killed: the `when:` tests |
| M12 step `job_seed is None` assertion dropped | killed: the zero-row-with-no-job-seed case (the sampler's own assertion covers non-empty sources, so this case is what pins the step's) |
| M13 zero-row positional counted as `pool_select` | killed: evidence test |
| M14 resolver gate ignores the cardinality mode | killed: whole-column-mode resolver test |
| M15 static-route and evidence consumers drop the faker branch | killed: parity, split |
| M16 sampler domain code renamed | killed: `test_a_range_past_the_uint64_domain_raises_the_faker_code` |
| M17 shared gather bounds check dropped | killed after adding the positional out-of-bounds case; the deterministic path was already killed by `test_dispatch_faker` |
| M18 pool validation accepts non-string pools | killed: all five 6d paths |
| M19 native route passes no `job_seed` | killed: parity, split |
| M20 resolver keys the default namespace on an empty table | killed: default-namespace parity, split |
| M21 `pool_size` requirement dropped | killed: stage-A tests |

Changed-unit coverage (line and branch, new tests plus `test_dispatch_faker`, `test_r1b_operator_step`, `test_sample_faker_array`, `test_c5b_i_metadata_inventory` and `test_faker_positional_nondet`, Python 3.11): `_faker_positional_admission.py` 100%, `_operator_step.py` 98% (the misses are the untouched text_redact branch). Every changed line in `_chunk_masking.py`, `_operator_params.py`, `_chunked_schema_rule.py`, `_chunked_evidence.py`, `_dispatch.py`, `_chunked_entry.py` and `_chunked.py` is covered; the remaining misses in those files are older code.

## Module size

| Module | Before | After |
|---|---|---|
| `execution/_chunked.py` | 619 | 626 (census updated) |
| `execution/native/_chunked_entry.py` | 600 | 606 (new census entry) |
| `execution/native/_chunk_masking.py` | 244 | 289 |
| `execution/native/_operator_step.py` | 297 | 371 |
| `execution/native/_operator_params.py` | 238 | 271 |
| `execution/native/_chunked_schema_rule.py` | 268 | 292 |
| `execution/native/_dispatch.py` | 576 | 572 |
| `execution/native/_chunked_evidence.py` | 230 | 228 |
| `execution/native/_draw_sites_gen_pool.py` | 117 | 118 |
| `execution/native/_faker_positional_admission.py` (new) | 0 | 134 |

`_chunked_entry.py` moved from exactly 600 to 606: the table-keyed params call (`+3` lines after formatting), the `job_seed` hand-off (`+1`), the eager check call (`+1`) and its import (`+1`). The check itself lives in `_chunk_masking.py`. The census entry is exact.

## Final test counts

Python 3.11 (`decoy-native-venv`, companion present), via `pytest-one`:

| Directory | Result |
|---|---|
| `tests/sentry` | 2346 passed, 1 skipped |
| new tests (4 files) | 241 passed |
| `tests/native` (includes the new tests) | 5757 passed, 1 skipped |
| `tests/physical` | 1548 passed, 1 skipped |
| `tests/parity/native` | 104 passed, 59 xfailed |
| `tests/perf/test_throughput_budgets.py` | 4 passed |
| `tests/unit/execution` | 6374 passed, 4 skipped |

Python 3.10 (`decoy-ci-mirror-venv`, no companion; companion-only tests skip):

| Directory | Result |
|---|---|
| `tests/sentry` | 2346 passed, 1 skipped |
| new tests (4 files) | 110 passed, 131 skipped |
| `tests/native` | 4356 passed, 1389 skipped |
| `tests/physical` | 1084 passed, 465 skipped |
| `tests/parity/native` | 40 passed, 64 skipped, 59 xfailed |
| `tests/perf/test_throughput_budgets.py` | 4 passed |
| `tests/unit/execution` | 5232 passed, 1146 skipped |

The 3.11 `physical`, `parity`, `perf` and `unit/execution` runs were taken at the feature commit `65391bb7`; the later commits touch only tests under `tests/native` and `tests/sentry`, so they were not repeated. A first 3.10 `unit/execution` run had one failure, `test_byte_estimate_routing.py::TestEndToEndWiring::test_width_change_flips_route_at_a_fixed_budget_and_row_count`, while another agent's suite was running on the same box. It is a byte-estimate routing test over a deterministic-Faker FK job, passes alone, and passed in the full rerun above, so it is recorded as a load-sensitive flake and not a regression. The same first run caught two of the new tests missing a companion marker (they need the compiled kernel to see the downgrade reason); both are fixed.

Lint before each commit: `ruff check src tests`, `ruff format --check src tests` and `mypy src` (3.10 mirror) are clean.

## Judgment calls

1. `faker_selection_namespace` is imported straight from `_strategies/_faker_positional.py` into `_operator_params.py`, which already imports `_strategies._text_redact`; nothing was moved or re-exported.
2. `chunked_positional_column` replaced the categorical-only copies in `_dispatch.py` and `_chunked_evidence.py`, so the faker branch is written once.
3. `reject_nonstring_positional_pools` takes the run state (duck-typed) and lives in `_chunk_masking.py` beside `pool_values_are_strings` and `_resolve_faker_pools`, to keep `_chunked_entry.py` growth to the call. Its gate is the seed-level `is_positional_faker_seed` (the oracle's own gate), because `check_chunked_compatibility` has already run and made every non-deterministic REUSE faker stage-A.
4. `resolve_params_by_column(table=None)` defaults to `None` so the existing R1b support helper needs no edit; a positional faker with no table is an assertion.
5. The new failure is an `ExecutionError`, like the other chunked runtime codes, not a `PlanCompileError`.
6. The legacy lane (`chunked_dispatcher_enabled=False`, which calls the public oracle `run_mask_pipeline_chunked`) does not run the eager pool check or the string pin. Both belong to `run_mask_chunked`. The plan names the two dispatcher legs only, so this is left as is; it is a kill-switch lane.
7. Test 6c's numeric-output case registers an integer-returning adapter under `address_zip` (a non-allowlisted string provider), because no built-in poolable provider returns numbers; the date case uses the real `person_dob`.

## dennis gate (round 1: GO, 0 BLOCKER / 0 HIGH / 1 MEDIUM / 3 LOW), fixed by the plan author

- MEDIUM, M4 not equivalent: collision parity test added (see the mutation table), and the record is corrected.
- LOW 1, legacy lane (`chunked_dispatcher_enabled=False`): accepted. Production reaches it only via platform `NATIVE_DISPATCHER_ENABLED=false`, and platform Phase-1 streaming never admits Faker. With real (string) providers it matches whole-frame, degenerate chunks included. It diverges only for a custom Python provider that shadows an allowlisted name, which is not plausible on the platform (DB providers are string-only).
- LOW 2, positional null-index check: added (`index_batch_null_mask_mismatch`), with a malformed-kernel test case.
- LOW 3, design: per-strategy positional branches in `_faker_positional_admission.chunked_positional_column` and `_chunked._conditional_admission_failures`. Carried to the refactor track: a positional flag on `OperatorSpec` before a third position-keyed operator.
