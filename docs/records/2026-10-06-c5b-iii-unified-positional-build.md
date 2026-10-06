Status: record (build, branch `feat/c5b-iii-unified-positional`, not merged)
Rules consulted: 00-universal, development-loop, testing, observability-and-resilience, risk-and-exceptions

# C5b-iii build record: position-keyed categorical and Faker on the unified route

Plan: `docs/plans/2026-10-06-c5b-iii-unified-positional.md` rev 2 (Codex GO). Base: engine main `3e8d259e` plus the plan commits. Build commits, oldest first: `d2923181` (acceptance tests), `97518491` (implementation and test migrations), `c8a0986f` (docs, coordinator trim, two more direct tests), then three test-only commits.

## What changed per plan item

**3a. Admission at the binding layer.** `physical/_shadow_bindings.py` gains `positional_categorical_bindable` and `positional_faker_bindable`. `execution_binding_for_slice_node` computes `positional` before the `fallback_policy` check and lets a positional node through.
- The categorical predicate holds when the slice is not deterministic, has no `when` and no vault, `prepare_positional_categorical` returns an artifact, the resident source is exactly `pa.string()`, and the table is in no FK relationship.
- The Faker predicate holds when `is_positional_faker_seed(plan_slice)`, the raw-config stage A (`positional_faker_config_for_column`) passes, and `_faker_pool_bindable` holds (when, vault, allowlist, `pool_native`, string source, FK).
- Everything else keeps today's decline. JC-5, `prepare_categorical`, `native_route_eligibility` and the chunked route are untouched.

**3b. Params and bindings.**
- Categorical: the existing `resolve_operator_params` path with the positional `PreparedCategorical`; `KeyBinding("mask_key", namespace)`.
- Faker: `positional_faker_params` (renamed from the private `_positional_faker_params`, now shared by both routes) builds `FakerParams(positional=True, selection_namespace=...)`; `KeyBinding("job_seed", selection_namespace)`; `PoolBinding` as before.
- `determinism_family` for a positional node comes from its own draw site (`mask.categorical_nondeterministic`, `mask.faker_nondeterministic`).
- `needs_index_kernel` is true for any `CategoricalParams`. `_bound_params` drops the determinism assertion for a `CategoricalParams`/index-kernel check, and asserts a non-empty job seed for a positional Faker. `KeyBinding` docs gain `job_seed`.

**3c. Offset and key threading.** `run_operator(row_offset=0)` forwards `row_offset` and, for a positional Faker only, `ctx.job_seed`. The coordinator passes its pre-increment `row_offset`.

**3d. Pools.** `_resolve_pool` reads the namespace from `FakerParams.namespace` for both identity and build.

**3e. Zero-row evidence.** `OperatorCallEvidence.rows_seen` is summed per batch; `assemble_node_evidence` requires positive kernel evidence unless `rows_seen == 0`.

**3f. Size.** The coordinator ends at 598 lines (see judgment call 4), so no census entry was needed.

**3g. Docs.** CHANGELOG entry, compatibility contract (categorical and Faker paragraphs), capability notes in `native/_capabilities.py`. Not done, see judgment call 8: the roadmap and the categorical draw-site mirror line.

Draw-site families used: `mask.categorical_nondeterministic` (family `source_keyed_hmac`, entropy root `mask_key`) and `mask.faker_nondeterministic` (family `source_keyed_hmac`, entropy root `job_seed`). The deterministic sites share the same family string, so `determinism_family` is the same value for all four.

## Red-before and green-before

Measured on a clean archive of the base commit (`git archive` of `d2923181`, which has base `src/` and the new tests) run under Python 3.11 with the native companion. Note: `pyproject.toml` puts the working tree's `src` first on `sys.path`, so a `PYTHONPATH` override does not give a base run; the base run has to come from a separate tree.
- 130 tests existed at that point: 90 failed, 40 passed.
- Failing (tests 1, 2, 3, 4, 5-zero-row, 8): all multi-batch, degenerate-shape, offset, key, pool-identity, default-lane, zero-row-evidence and direct-predicate tests.
- Passing on base (green-before, tests 5-7 plus the fixture guard): the non-empty-Faker-still-raises cases (3), deterministic zero-row evidence (3), the 16 decline cases, the two FK declines, the 11 route-verdict snapshots, the two multi-table and `when:` exclusion tests, and the distinct-keys fixture check.
- The route-verdict snapshot literals and the deterministic zero-row evidence literals were recorded on base and re-checked on base with the probe script before being pinned.
- Three tests were added after that run (job-seed guard, `rows_seen` sum, non-poolable registry) and were not run on base; they exercise code this slice adds.

## Test migrations (existing tests edited, none deleted or weakened)

Line numbers are in the base file.

| File:line (base) | Old | New | Why intent is preserved |
|---|---|---|---|
| `tests/physical/test_unified_slice_faker.py:519` | `_DECLINE_CASES["non_deterministic"]` | entry removed; `test_8b_non_deterministic_reuse_admits_and_runs_native` added | The case pinned the decline. It now asserts admission, lane-vs-lane-off parity and exact evidence. The other decline cases are unchanged. |
| `tests/physical/test_shadow_categorical.py:221` | `test_nondeterministic_categorical_declines_on_full_frame_binding` (binding is `None`) | `test_nondeterministic_categorical_binds_positionally_on_full_frame_binding` | Same boundary, now asserting the positional params, `mask_key` key binding and index-kernel need. The config-only eligibility rejection test just above it is unchanged. |
| `tests/physical/test_shadow_categorical.py:265` | `test_run_operator_asserts_categorical_determinism` (raises) | `test_run_operator_draws_a_positional_categorical_by_global_position` | Same entry point, now asserting the draw equals the pandas handler's, at offsets 0 and 5, with the evidence. Module docstring and one section header reworded. |
| `tests/native/test_chunked_nondet_faker_admission.py:682` | `test_the_unified_binding_never_binds_a_non_deterministic_faker` | `test_the_unified_binding_binds_a_non_deterministic_faker_keyed_on_the_job_seed` | Now asserts the binding and that `native_route_eligibility` still rejects. |
| `tests/native/test_chunked_nondet_faker_admission.py:697` | `test_the_unified_shadow_operator_keeps_its_determinism_assertion` (source-text pin: "positional" absent) | `test_the_unified_shadow_operator_draws_a_positional_faker_by_job_seed_and_offset` | The pin is replaced by a behavioral check against the scalar `derive_index` values. Section header reworded. |
| `tests/native/test_chunked_nondet_categorical_admission.py:371` | `test_the_physical_operator_assertion_is_unchanged` (source-text pin) | `test_the_unified_binding_admits_the_seeded_categorical_as_the_positional_variant` | The `prepare_categorical` decline (`:359`) and the eligibility rejection (`:366`) tests stay as they were, since both still hold. Section header reworded. |
| `tests/unit/execution/test_c1b_i_route_regression.py:363` | `TestNativeStillDeclines.test_physical_operator_assertion_is_unchanged` (source-text pin) | `test_the_full_frame_binding_admits_the_positional_variant_only` | Asserts the non-deterministic one binds positional and the deterministic one still binds source-keyed. Module docstring bullet e reworded. The two `prepare_categorical` tests in the class are unchanged. |
| `tests/physical/test_r1b_binding_markers.py:63` | `("categorical_positional", _POSITIONAL, False)` for `needs_index_kernel` | `True` | The one marker that changes meaning. `categorical_deterministic` for the same params stays `False` (line 109, unedited). |

No test outside this list changed. The deterministic parity suites, the R1b cross-route kwargs baseline and every other existing test ran unedited and green.

## Mutation check (by hand, after the build; every mutant restored, `git diff -- src` empty afterwards)

Harness: a script applied one string replacement, ran `tests/physical/test_unified_slice_positional.py` with `-x` (then the wider physical set if it survived), and restored the file.

| Mutant | Killer |
|---|---|
| M01 drop `row_offset` | `test_1_multi_batch_ragged_final_batch_with_boundary_nulls[cat_uniform]` |
| M02 post-increment offset | same |
| M03 Faker keys on `mask_key` | `test_1_multi_batch_...[faker_ns]` |
| M04 categorical keys on `job_seed` | `test_1_multi_batch_...[cat_uniform]` |
| M05 pool namespace from `key_binding` | `test_1_multi_batch_...[faker_no_ns]` |
| M06 `rows_seen` guard removed | `test_1_degenerate_shapes[faker_ns-zero_rows]` |
| M07 `rows_seen` not summed | `test_5_rows_seen_sums_across_the_batches_of_one_node` |
| M08 categorical predicate, determinism removed | `test_5_deterministic_zero_row_evidence_is_unchanged[det_categorical]` (also the direct clause test) |
| M09 categorical predicate, `when` removed | `test_8_each_positional_categorical_condition_is_enforced[when]` |
| M10 categorical predicate, vault removed | `...[vault]` |
| M11 categorical predicate, prepared artifact removed | `...[config_not_preparable_from_profile]` |
| M12 categorical predicate, string source removed | `...[int_source]` |
| M13 categorical predicate, FK removed | `test_8_the_fk_exclusion_holds_on_the_multi_table_binding_path[categorical]` |
| M14 Faker predicate, slice seed check removed | `test_8_each_positional_faker_condition_is_enforced[slice_deterministic]` |
| M15 Faker predicate, stage A config removed | `...[no_pool_size]` |
| M16 Faker predicate, `_faker_pool_bindable` removed | `...[int_source]` |
| M17 pool bindable, `when` removed | `...[when]` |
| M18 pool bindable, vault removed | `...[vault]` |
| M19 pool bindable, allowlist removed | `test_6_declines_to_the_oracle_unchanged[det_faker_provider_outside_allowlist]` |
| M20 pool bindable, `pool_native` removed | survived the first pass; killed by `test_8_a_provider_the_registry_marks_not_poolable_is_not_bindable`, added for it |
| M21 pool bindable, string source removed | `...[int_source]` |
| M22 pool bindable, FK removed | `test_8_the_fk_exclusion_holds_on_the_multi_table_binding_path[faker]` |
| M23 binder ignores the positional exception | `test_1_multi_batch_...[cat_uniform]` |
| M24 Faker key binding names `mask_key` | `test_8_an_admitted_node_binds_with_the_variant_parameters[faker]` |
| M25 Faker key binding uses the configured namespace | same |
| M26 `needs_index_kernel` drops positional categorical | `test_1_multi_batch_...[cat_uniform]` |
| M27 job-seed guard removed | `test_3_run_operator_refuses_a_positional_faker_without_a_job_seed` |
| M29 categorical binds through the value-keyed prepare | `test_1_multi_batch_...[cat_uniform]` |
| M30 Faker params built non-positional | `test_1_multi_batch_...[faker_ns]` |

**M28 (equivalent mutant).** Replacing the positional draw-site family with `caps.draw_family` survives everything. All four sites (`mask.faker_deterministic`, `mask.faker_nondeterministic`, `mask.categorical_deterministic`, `mask.categorical_nondeterministic`) carry the family string `source_keyed_hmac`, so the two expressions are equal today. The tests assert `determinism_family == "source_keyed_hmac"` for both variants; they cannot tell the two sources apart until a site's family changes.

A mutation score was not computed with a tool. Only the hand-picked mutants above were run; this is not generative property testing, and the parity tests are enumerated shapes, not Hypothesis tests.

## LOC (base `4cdb9641` to HEAD)

| Module | Before | After |
|---|---|---|
| `physical/_shadow_bindings.py` | 307 | 393 |
| `physical/_shadow_coordinator.py` | 599 | 598 |
| `physical/_shadow_operators.py` | 332 | 338 |
| `physical/_plan.py` | 233 | 235 |
| `_unified_slice_evidence.py` | 180 | 183 |
| `native/_operator_params.py` | 271 | 273 |
| `native/_faker_positional_admission.py` | 134 | 135 |
| `native/_capabilities.py` | 485 | 486 |
| `tests/physical/test_unified_slice_positional.py` (new) | 0 | 933 |

## Test counts on the final tree

Python 3.11 (`decoy-native-venv`, companion present):

| Directory | Result |
|---|---|
| `tests/sentry` | 2419 passed, 1 skipped (the record file adds one parametrized case; 2418 before it) |
| `tests/native` | 5922 passed, 1 skipped |
| `tests/physical` (includes the 133 new tests) | 1682 passed, 1 skipped |
| `tests/unit/execution` | 6386 passed, 4 skipped |
| `tests/parity/native` | 104 passed, 59 xfailed |
| `tests/perf/test_throughput_budgets.py` | 4 passed |

Python 3.10 (`decoy-ci-mirror-venv`, no native companion, so companion-dependent tests skip):

| Directory | Result |
|---|---|
| `tests/sentry` | 2419 passed, 1 skipped (the record file adds one parametrized case; 2418 before it) |
| `tests/native` | 4483 passed, 1427 skipped |
| `tests/physical` | 1143 passed, 540 skipped |
| `tests/unit/execution` | 5244 passed, 1146 skipped |
| `tests/parity/native` | 40 passed, 64 skipped, 59 xfailed |
| `tests/perf/test_throughput_budgets.py` | 4 passed |

New file on its own: 3.11, 133 passed; 3.10, 61 passed and 72 skipped (every parity, evidence and key test needs the companion). The 3.10 run therefore covers the predicates, the binding shape, the declines, the verdict snapshots and the unit-level evidence checks, not the lane output.

Lint before each commit: `ruff check src tests`, `ruff format --check src tests`, `mypy src`, all clean. The log sentry (`tests/sentry/test_log_interpolation.py`) is green; no log line was added.

## Judgment calls

1. **`rows_seen` defaults to `None`.** The plan says to sum an `int`. `tests/physical/test_unified_slice_evidence_unit.py::test_a_d7_miss_raises` builds a Faker record by hand with no row count and expects the hard failure, and it is outside the migration list. A default of 0 would exempt that record, so the field is `int | None`, summed from 0 by `run_operator`, and the guard exempts only an exact 0. A hand-built record that never counted reads as non-empty.
2. **`run_operator` reads `ctx.job_seed` only for a positional Faker.** About ten existing test modules pass `SimpleNamespace(mask_key=..., native_threads=...)` as the context. Reading `ctx.job_seed` unconditionally would break them. The values forwarded are the same for every operator that uses them.
3. **Stage-A helpers take `Mapping`.** `inputs.config` holds `mappingproxy` tables and columns, so `positional_faker_config_for_column` returned `None` for every column until its `isinstance(..., dict)` checks became `Mapping`. A `dict` is a `Mapping`, so the chunked route is unaffected.
4. **Coordinator at 598 lines, not a census entry.** `tests/physical/test_shadow_full_frame.py` and `test_shadow_mixed_fk.py` assert the coordinator is strictly under 600, which is stricter than the census sentry. At 600 they failed, so I tightened one comment and the guard rather than raise a cap.
5. **Predicates return `bool`; the binder recomputes the categorical artifact.** The direct predicate tests need a boolean. `prepare_positional_categorical` is cheap and runs twice for such a column.
6. **The Faker predicate checks both the slice and the raw config.** The slice decides determinism and cardinality mode, the raw config decides stage A. The `slice_deterministic` direct test shows a disagreement declines. The allowlist is checked twice (stage A and `_faker_pool_bindable`); the stage-A copy cannot be killed separately.
7. **Multi-batch lane tests narrow the batch size by patching `ShadowContext.from_key_provider`.** The lane builds its context with the 50k default and exposes no batch argument. The reference side is the real `run_pipeline(unified_slice_enabled=False)`, not the shadow helper's oracle, which would take the lane itself now.
8. **Not done.** The roadmap and shipped log live in `decoy-platform`, outside this worktree. A mirror line for `mask.categorical_nondeterministic` in the draw-site inventory would push `_determinism_protocol.py` over its exact census LOC (I tried it, the size sentry failed, and I reverted it). The Faker site already lists the native mirror `_operator_step.py:148`, which the unified route shares.
9. **Chunked verdict snapshot is companion-aware.** Without the companion an admitted column reroutes with `index_extension_unavailable`. The snapshot asserts that reason on the 3.10 mirror and the recorded values where the companion is present.
10. **A scratchpad side effect.** My first base run extracted `src/` into `scratchpad/base`, a directory from an earlier session that already held a checkout. Nothing in the repo was touched, but that scratch copy's `src/` now matches this base.
