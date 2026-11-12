# GEN-REL E1 build evidence

Status: built on `feat/gen-rel-e1`. Both stop conditions are closed (see "Stop conditions" and
"Owner rulings (2026-11-12)"). The source head after the owner rulings is `27858cc3`; the mutation table and the
gate totals that describe it are in the owner-rulings section. Two later merges of `origin/main`
moved the final source head to `87d8f8a6`; see "Main merges (2026-11-12)".

Spec: `docs/plans/gen-rel.md` on `origin/plan/gen-rel` (`26a83542`), slice row 1. The plan stays
on the plan branch.

## Heads and environment

- Slice start: `feat/reg-release-e2` at `44a27b12` (REG-RELEASE E2, which held `origin/main`
  `7fc12e7f`).
- Base after the merges: `origin/main` `4bb95063` (merged as `b3c4ada`) and
  `origin/feat/reg-release-e2` `c26f8a19` (merged as `f10a82b`). Both merges were clean.
- Commits: `928b35a` red acceptance tests, `d8a7099` move-only refactor, `55c73d2` the feature,
  `b3c4ada` merge of main, `4e46141` foundation-tier placement, `4e6ca06` stronger witnesses,
  `f10a82b` merge of the base slice.
- First build source head: `f10a82b`. The first mutation run and the T1 gates in "Gates" ran
  on it. Final source head: `27858cc3`, after the owner rulings; its mutation rerun and gates
  are in "Owner rulings (2026-11-12)". This record is added in docs-only commits.
- `python -V`: Python 3.11.18 (the `.venv`, synced with
  `uv sync --frozen --extra dev --extra lint --extra vault --extra cloud`, because ruff and mypy
  are in the `lint` extra). PyArrow 24.0.0, DuckDB 1.5.4, pandas 2.3.3, NumPy 2.4.6, Faker 40.23.0.
  The `layers` step ran in `.venv-layers` on Python 3.10.20.
- Entry conditions: `OptionSpec.ref_kind` is still consumed by
  `plan/checks/_generate_columns.py::_check_forward_references` (checked at the start and after
  each merge). The gating test passes on the base: a relationship that names generate tables
  validates, compiles and pin-compiles under all four policies.

## Red first (commit `928b35a`, source unchanged from `44a27b12`)

`uv run pytest -q -p no:cacheprovider <the 12 files below> -rA`: 139 failed, 233 passed. The
`layers` step, which owns `tests/sentry/layout/test_import_layers.py`, failed 3 (the reviewed
snapshot names `_capacity`, the config did not).

| File | Red |
| --- | --- |
| `tests/sentry/defects/test_reference_pool_choke_point.py` | 6 |
| `tests/unit/config/test_generate_relational.py` | 12 |
| `tests/unit/execution/test_probe_scale.py` | 6 |
| `tests/unit/execution/test_probe_scale_kills.py` | 2 |
| `tests/unit/generation/pool/test_capacity_null_reusable.py` | 3 |
| `tests/unit/generation/relational/test_cardinality.py` | 27 |
| `tests/unit/generation/relational/test_routes.py` | 59 |
| `tests/unit/generation/test_fr0_streamed_entry.py` | 2 |
| `tests/unit/generation/test_pinned_generation.py` | 13 |
| `tests/unit/generation/test_pinned_with_generation.py` | 1 |
| `tests/unit/relationships/test_multi_parent_fk.py` | 1 |
| `tests/unit/relationships/test_orphan_fk_policy_dedup.py` | 7 |

Reasons, by count of failing cells:

- Absent API: `decoy_engine.generation.relational` (20), `_reference(..., policy=)` (16),
  `PinnedPlan.reference_policies` (15), `decoy_engine._capacity` (6), the ten unregistered
  reason codes (27), `relationships.normalize_orphan_policy_lookup` (3),
  `config.per_parent_problems` (1), the choke-point sentry's real-tree cells (5).
- Wrong outcome: no refusal at validate for conflicting, null-paired, infeasible or fitted
  bounds (10); no run-time refusal for an empty parent under `fail`, a weights mismatch or
  infeasible bounds (23); no warning under `warn` (4); max broken on 17 of 200 seeds; raw list
  and dict parent keys raise `TypeError` (4); policy tokens echoed by `_graph.py` (6); the probe
  left bounds unscaled (8).

Green on the base by design, each tied to a mutation in the table below: the gate cells (a
precondition), the AT-1 byte witnesses and near neighbors (A1a to A1e), the equality and
zero-value boundary cells (A1c3, A3a to A3c, B1, B2), `fitted` without a bound (A6f4), an empty
child under `fail` (A6c), the probe cells whose bounds need no change (A5a, A5e to A5h), the
sentry's seeded-bypass cells (they test the scanner on a seeded source), and
`test_at3_equal_bounds_put_every_parent_at_exactly_that_count`, which the old best-effort repair
happened to satisfy (A3a, A3b).

Test changes after the red commit: the repair witnesses became three parametrized cells with
stronger inputs (the first two did not see a dropped shuffle); `tracemalloc` moved to a
`serial`-marked fill test (the parallel-safety sentry requires the mark for a memory envelope);
the 200-seed cells call a `_repair` helper; the probe copy-kill uses a categorical key; one
direct `per_parent_problems` assertion was added for `fitted` without a bound; and the
import-layer snapshot places `_capacity` mid-tier. None was weakened to pass.

## Mutations (first run on `f10a82b`, detached worktree, 53 of 53 red)

Each was applied once, its named tests run, and the file restored; the worktree was clean at the
end and is removed.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| M1 | restore the with-replacement fill | `generation/relational/_cardinality.py` | 2: `test_at4_max_holds_over_200_seeds` |
| M2 | drop the runtime feasibility check | `generation/relational/_cardinality.py` | 8: `test_at15_runtime_refusal_names_the_table_and_column_only`, `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_min_above_capacity_refuses_at_run` ... |
| M3a | remove the pool-check call from the whole-table draw (_reference) | `generation/_synthesize_derived.py` | 6: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_at6_fail_refuses_an_empty_parent`, `test_the_helper_is_defined_once_and_called_only_by_the_listed_routes` ... |
| M3b | remove the pool-check call from the streamed draw (ReferenceStream.__init__) | `generation/_column_streams.py` | 8: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_at6_fail_refuses_an_empty_parent`, `test_reference_pool_check_runs_once_per_stream_not_once_per_batch` ... |
| M3c | remove the pinned route's policy hand-off (_pinned_execute._run) | `generation/_pinned_execute.py` | 4: `test_at6_fail_refuses_an_empty_parent`, `test_at6_pinned_warn_logs_once_without_a_callback`, `test_pinned_execute_honors_fail_for_an_empty_parent` ... |
| M4 | use a Python assert for the final count check | `generation/relational/_cardinality.py` | 2: `test_at4_the_final_check_is_an_explicit_branch_not_a_python_assert`, `test_the_relational_package_has_one_emitter_keyed_counters_and_no_assert` |
| M5 | use a raw parent value as a counter key | `generation/relational/_cardinality.py` | 4: `test_at4_v1_caller_repairs_unhashable_keys_to_exact_bounds`, `test_at4_v2_caller_repairs_unhashable_keys_to_exact_bounds` |
| M6 | a second relationship parser in generation | `generation/relational/_cardinality.py` | 2: `test_at6r_generation_reads_policies_through_the_relationship_normalizer`, `test_generation_never_parses_a_relationship_policy_itself` |
| M7 | a direct warning log call instead of the emitter | `generation/relational/_cardinality.py` | 6: `test_at6_each_child_of_one_relationship_gets_its_own_policy`, `test_at6_warn_keeps_nulls_and_emits_exactly_one_warning`, `test_empty_parent_policy_tokens` ... |
| M8 | generic empty-parent handling before fitted handling | `generation/_synthesize_derived.py` | 7: `test_at6f_an_empty_fitted_pool_keeps_the_plan_refusal_under_every_policy`, `test_the_whole_table_draw_checks_the_pool_before_any_value` |
| M9 | policy-token interpolation in _graph.py | `relationships/_graph.py` | 3: `test_an_invalid_policy_is_refused_without_echoing_it` |
| M9b | policy-token interpolation in _graph.py (duplicate and conflict builders) | `relationships/_graph.py` | 3: `test_a_duplicate_names_neither_policy`, `test_same_parent_same_child_different_policies_raises`, `test_conflicting_policies_raise` |
| A1a | AT-1: reseed the draw (changes draw order) | `generation/_synthesize_derived.py` | 8: `test_at1_near_neighbor_bytes_do_not_move` |
| A1a2 | AT-1: reorder a repair shuffle (changes draw order inside the moved repair) | `generation/relational/_cardinality.py` | 2: `test_at1_repair_without_a_fill_is_byte_identical_to_the_base` |
| A1b | AT-1: compiled policy metadata enters seed derivation | `generation/_pinned_execute.py` | 4: `test_at1_a_non_empty_reference_ignores_the_policy_on_every_route`, `test_at1_near_neighbor_bytes_do_not_move`, `test_reference_policy_is_plan_metadata_not_a_column_or_seed_input` |
| A1c | AT-1: zero bound read as a bound at run | `generation/relational/_cardinality.py` | 18: `test_at1_near_neighbor_bytes_do_not_move`, `test_at3_check_reference_pool_feasibility_boundaries` |
| A1c2 | AT-1: zero bound enters the repair | `generation/_synthesize_derived.py` | 1: `test_at1_unbounded_reference_never_enters_the_repair` |
| A1c3 | AT-2: zero max read as a ceiling at validate | `config/_generate_relational.py` | 2: `test_at2_consistent_bounds_validate` |
| A1d | AT-1: branch on the new plan key when it is absent | `generation/_pinned_plan.py` | 2: `test_at6_pinned_plan_carries_the_policy_and_its_fingerprint_follows_it`, `test_plan_without_reference_policies_keeps_its_serialized_form` |
| A1e | AT-1: a policy changes a non-empty draw | `generation/relational/_cardinality.py` | 6: `test_at1_a_non_empty_reference_ignores_the_policy_on_every_route`, `test_at1_near_neighbor_bytes_do_not_move` |
| A3a | AT-3: refuse the min equality boundary at run | `generation/relational/_cardinality.py` | 5: `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_equal_bounds_put_every_parent_at_exactly_that_count` |
| A3b | AT-3: refuse the max equality boundary at run | `generation/relational/_cardinality.py` | 5: `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_equal_bounds_put_every_parent_at_exactly_that_count` |
| A3c | AT-3: refuse the max equality boundary at validate | `config/_generate_relational.py` | 1: `test_at3_equality_is_feasible_and_min_waits_for_the_run` |
| A4a | AT-4: the run checks row count instead of distinct keys | `generation/_synthesize_derived.py` | 1: `test_at4_the_run_checks_distinct_keys_not_row_count` |
| A4b | AT-4: a multiset of all free slots (allocation proportional to max) | `generation/relational/_cardinality.py` | 1: `test_at4_the_fill_allocates_by_rows_and_parents_not_by_max_times_parents` |
| A5a | AT-5: strip the bounds in the probe | `execution/_probe_scale.py` | 12: `test_a_full_size_probe_loosens_nothing`, `test_an_empty_scaled_child_drops_the_minimum_only`, `test_other_entries_are_skipped_not_a_loop_abort` ... |
| A5b | AT-5: keep the bounds unscaled in the probe | `execution/_probe_scale.py` | 10: `test_an_empty_scaled_child_drops_the_minimum_only`, `test_other_entries_are_skipped_not_a_loop_abort`, `test_the_rewrite_does_not_touch_the_original_config` ... |
| A5c | AT-5: apply the sequence formula to every parent | `execution/_probe_scale.py` | 2: `test_a_parent_with_unknown_distinct_keys_gets_the_widest_bounds` |
| A5d | AT-5: float ceiling | `execution/_probe_scale.py` | 1: `test_the_ceiling_is_exact_above_float_precision` |
| A5e | AT-5: an empty scaled parent is divided by | `execution/_probe_scale.py` | 1: `test_an_empty_scaled_parent_leaves_the_bounds_alone` |
| A5f | AT-5: rewrite a reference that has no positive bound | `execution/_probe_scale.py` | 2: `test_a_reference_without_a_positive_bound_is_not_rewritten` |
| A5g | AT-5: rewrite the caller's config instead of the copy | `execution/_probe_scale.py` | 1: `test_the_rewrite_does_not_touch_the_original_config` |
| A5h | AT-5: a parent outside the config gets the widest bounds | `execution/_probe_scale.py` | 1: `test_a_parent_outside_the_config_leaves_the_bounds_alone` |
| A6a | AT-6: the fingerprint ignores the policy | `generation/_pinned_plan.py` | 2: `test_at6_pinned_plan_carries_the_policy_and_its_fingerprint_follows_it`, `test_reference_policy_is_plan_metadata_not_a_column_or_seed_input` |
| A6b | AT-6: the normalizer reads the first child only | `relationships/_graph.py` | 1: `test_at6_each_child_of_one_relationship_gets_its_own_policy` |
| A6c | AT-6: an empty child is refused under fail | `generation/relational/_cardinality.py` | 2: `test_at6_an_empty_child_under_fail_is_not_refused`, `test_empty_parent_policy_tokens` |
| A6f1 | AT-6f: fitted ignores bounds at run | `generation/_synthesize_derived.py` | 2: `test_at6f_a_bound_on_a_fitted_reference_refuses_at_run` |
| A6f2 | AT-6f: fitted bounds refuse an empty pool before the allocator | `generation/relational/_cardinality.py` | 10: `test_at6f_an_empty_fitted_pool_keeps_the_plan_refusal_under_every_policy` |
| A6f3 | AT-6f: fitted with a bound validates | `config/_generate_relational.py` | 3: `test_at6f_a_positive_bound_with_fitted_is_refused`, `test_the_refusal_order_is_conflict_then_fitted_then_nulls_then_ceiling` |
| A6f4 | AT-6f: fitted without a positive bound is refused | `config/_generate_relational.py` | 1: `test_at6f_fitted_without_a_positive_bound_validates` |
| A14 | AT-14: raise a code the registry does not hold | `reasons/runtime/_part2.py` | 5: `test_at14_new_codes_are_registered_unreleased_in_the_right_catalog`, `test_at6_fail_refuses_an_empty_parent` |
| A15a | AT-15: format a count into a refusal | `generation/relational/_cardinality.py` | 12: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_at15_runtime_refusal_names_the_table_and_column_only`, `test_at6_fail_refuses_an_empty_parent` ... |
| A15b | AT-15: raise the refusal inside an except block | `generation/relational/_cardinality.py` | 7: `test_at15_runtime_refusal_names_the_table_and_column_only`, `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_min_above_capacity_refuses_at_run` |
| A15c | AT-15: format the bounds into a validate problem | `config/_generate_relational.py` | 3: `test_at15_validate_refusals_carry_the_fixed_summary_and_no_input`, `test_at2_min_above_max_is_refused`, `test_problems_are_locations_and_codes_only` |
| B1 | AT-2b: an explicit zero null_probability counts as set | `config/_generate_relational.py` | 2: `test_at2b_an_explicit_zero_is_off` |
| B2 | validate judges an empty parent or child against the ceiling | `config/_generate_relational.py` | 1: `test_an_empty_parent_or_child_skips_the_ceiling` |
| B3 | null conflict dropped at validate | `config/_generate_relational.py` | 3: `test_at2b_null_probability_with_a_positive_bound_is_refused`, `test_the_refusal_order_is_conflict_then_fitted_then_nulls_then_ceiling` |
| B4 | a new parent-key reader outside the listed draws | `generation/_column_streams.py` | 1: `test_parent_keys_are_read_only_by_the_listed_draws` |
| B5 | weights mismatch tolerated (uniform fallback restored in the helper) | `generation/relational/_cardinality.py` | 5: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_weights_are_checked_against_the_distinct_key_count` |
| B6 | the pool facade stops delegating to the foundation predicate | `generation/pool/_capacity.py` | 2: `test_config_and_the_pool_facade_share_the_foundation_predicate`, `test_the_pool_export_is_a_facade_over_the_foundation_leaf` |
| B7 | the validate ceiling bypasses the shared predicate | `config/_generate_relational.py` | 2: `test_config_and_the_pool_facade_share_the_foundation_predicate`, `test_the_ceiling_goes_through_the_predicate_with_max_times_row_count` |
| B8 | pinned policy decoded laxly | `generation/_pinned_plan.py` | 3: `test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw`, `test_serialized_reference_policies_decode_strictly` |
| B9 | the streamed pool check runs per batch | `generation/_column_streams.py` | 3: `test_reference_pool_check_runs_once_per_stream_not_once_per_batch`, `test_the_helper_is_defined_once_and_called_only_by_the_listed_routes`, `test_the_streamed_draw_checks_once_at_construction` |

All 53 were rerun on the final source head `27858cc3` with the same result (see "Owner rulings
(2026-11-12)").

M1 to M9b are the E1 mutations the plan's acceptance paragraph names; A* are the per-test "red
on" mutants and class-table mutations; B* cover cells that are green on the base by design.

## Re-pinned and changed fixtures

Only fixtures inside the plan's intentional-change cells changed.

| Path | Config | Route | Change | Reason |
| --- | --- | --- | --- | --- |
| `tests/unit/generation/unique/test_gen_unique_old_input_bytes.py` | `reference_shapes` (`child.bounded`, `min_per_parent: 1`, `max_per_parent: 5`) | v2 whole-table | `cells` digest `d2d6fef3...` to `893c24d3...` | Positive bound whose repair fill ran. Per-column hashes on the base and the head differ in `child.bounded` only; `config_json` and `fingerprints` are unchanged |
| `tests/unit/generation/test_pinned_generation.py` | `_config` (`alpha_orders.customer`) | pinned | `min_per_parent: 1` and `null_probability: 0.1` removed, `max_per_parent: 3` kept | 25 children over 40 parents made the minimum infeasible, and nulls beside a bound are refused. No value pin depended on it |
| `tests/parity/_f_audit_cells.py` | `cfg_reference_repair` | v2 whole-table (audit legacy cell) | `max_per_parent` 4 to 5 | 14 children over three parent keys were infeasible at 4 |
| `tests/parity/test_phase_f_f0_invariants.py` | `test_whole_table_quantities_are_not_prefix_stable` | v2 whole-table | bounds 2..5 to 5..14 | Infeasible over three distinct parent keys at 17 and 40 rows |
| `tests/parity/test_phase_f_f0_witness.py`, `tests/parity/test_phase_f_fr0_witness.py` | `child.ref_weighted` | v2 whole-table and streamed core | `weights` `[1, 2, 3]` to `[1, 2]` | The three-row parent draws two distinct keys, so the fixture ran on the removed uniform fallback |
| `tests/unit/test_v2_generation.py` | `test_weighted_size_mismatch_falls_back_to_uniform` | v2 whole-table | renamed; asserts `runtime.reference_weights_length_mismatch` | Weights mismatch is refused |

No golden, snapshot or oracle digest moved on the V1 `ColumnGenerator` route in the tests run:
none of them pins a V1 bounded reference whose fill executes. The Phase F oracle
(`tests/parity/_f0_expected.py`, fixture `reference`, `r_bounded`) did not move: its draw needs
no fill. Pinned-plan fingerprints change only for a plan whose config has a relationship that
covers a reference column; no checked-in fingerprint is in that set.

## Files outside the plan's slice row

- `tests/sentry/test_module_size.py`, `tests/sentry/baselines/module-size.json`: the
  `generators/columns.py` census entry is deleted (658 to 574 lines).
- `tests/sentry/test_sentry_manifest.py`: registers the new sentry.
- `tests/unit/generation/relational/__init__.py`, `_support.py`: package marker and shared
  builders for the new tests.
- The fixture files in the table above that the row does not list
  (`test_gen_unique_old_input_bytes.py`, the four `tests/parity` files, `test_v2_generation.py`).
- `docs/records/gen-rel-e1-evidence.md` (this file).

`config/_generate_unique.py` is not changed. The plan's Behavior text says it imports the
foundation leaf; the slice row does not list it, and an existing test pins its patch point on
the pool facade. The pool facade now delegates to `decoy_engine._capacity` at call time, so the
unique checks, the new relational checks and the pool all observe one predicate (tested by
patching the foundation function). The `.importlinter` ignore for that import stays as it was.

## Stop conditions

Both are closed.

1. Closed by the slice-0 fix that is on main (#506, squash `7d2cd2d`): all three files pass on
   `27858cc3`. As first reported: seventeen REG-RELEASE E2 tests build a copy of the live tree
   and assume its registry holds no pending identity: `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts`
   (3 cells, `--release-ready` must pass first), twelve `test_at8_*` cells of
   `tests/unit/scripts/test_stamp_release_compile.py` and two cells of
   `tests/unit/scripts/test_release_checks_fragments.py` (the copy has its own fragments, so the
   ten new identities have none: "no changelog fragment names it"). All 74 tests of those three
   files pass on `origin/feat/reg-release-e2` `c26f8a19`; the first slice that adds a reason
   turns these red. They were left red: changing another slice's fixtures is an owner call.
2. Closed by owner ruling 1 (PR #523 comment, 2026-11-12): the entry is added. As first
   reported:
   `tests/sentry/test_physical_seam_disconnection.py::test_production_execution_modules_are_byte_identical_to_origin_main`
   requires every changed file under `src/decoy_engine/execution` to be on its permitted list.
   The plan changes `execution/_probe_scale.py` and also says no allowlist rises. The entry was
   not added.

A third finding came up after the first report and is closed by owner ruling 2:
`tests/sentry/test_table_kind_classifier.py::test_inline_generate_truth_tests_only_go_down`
counted the four `.get("generate_columns") or []` loops the slice added (two modules with no
baseline, and `execution/_probe_scale.py` at 4 against 2).

## Gates (first build, `f10a82b`)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests <T1 paths>` on `f10a82b`, baseline `origin/main`
`4bb95063` in a detached worktree with the same venv.

| Step | Branch | Main |
| --- | --- | --- |
| ruff | pass | not rerun |
| format | pass | not rerun |
| mypy | 1 error | the same 1 error |
| sentry | 5142 passed, 1 failed, 2 skipped | 5082 passed, 0 failed |
| layers | 15 passed | not rerun |
| licenses | 63 passed | not rerun |
| tests | 6428 passed, 17 failed, 79 skipped | 6145 passed, 0 failed |

- Shared with main: mypy `src/decoy_engine/quality/dp_provenance.py:517` (no `tomli` stub under
  Python 3.11; a 3.10 venv is clean).
- Branch-only, 18 ids, all in the two stop conditions above: the 17 release-stamp cells and the
  execution byte-identity sentry. No other test, sentry or lint step fails.
- `python scripts/dev/registry_renumber.py --check --main origin/main`: `registry consistent`.
  `python scripts/release/reason_release_pin.py --check`: `reason release pin current`.
  `python scripts/dev/check_fragment.py --base origin/main`: exit 0.
  `python scripts/gen_capability_matrix.py`: no change (E1 adds no strategy).

These totals describe `f10a82b` under Python 3.11; the totals for the final source head are in
"Owner rulings (2026-11-12)".

T1 paths (57):

```
tests/acceptance/test_f06_route_cells.py
tests/acceptance/test_f06_route_parity.py
tests/generation
tests/integration/golden
tests/integration/golden/test_determinism_invariants.py
tests/integration/golden/test_fidelity_report_golden.py
tests/native/test_determinism_goldens.py
tests/native/test_draw_site_providers_golden.py
tests/native/test_draw_sites_catalogue_pinned.py
tests/parity/test_numeric_typing_fk.py
tests/parity/test_numeric_typing_readers.py
tests/parity/test_phase_f_audit_matrix.py
tests/parity/test_phase_f_audit_no_legacy.py
tests/parity/test_phase_f_audit_vacuity.py
tests/parity/test_phase_f_audit_zero_companion.py
tests/parity/test_phase_f_f0_comparator.py
tests/parity/test_phase_f_f0_composite_repeatability.py
tests/parity/test_phase_f_f0_invariants.py
tests/parity/test_phase_f_f0_matrix.py
tests/parity/test_phase_f_f0_oracle.py
tests/parity/test_phase_f_f0_witness.py
tests/parity/test_phase_f_f2_mixed_admission.py
tests/parity/test_phase_f_f2_mixed_backend_witness.py
tests/parity/test_phase_f_f2_mixed_parity.py
tests/parity/test_phase_f_f2_mixed_publish.py
tests/parity/test_phase_f_f2_mixed_sweep.py
tests/parity/test_phase_f_fr0_mixed_stitch.py
tests/parity/test_phase_f_fr0_refreeze.py
tests/parity/test_phase_f_fr0_route_independence.py
tests/parity/test_phase_f_fr0_witness.py
tests/unit/config
tests/unit/determinism/test_derive_vectors.py
tests/unit/execution/test_probe.py
tests/unit/execution/test_probe_scale.py
tests/unit/execution/test_probe_scale_kills.py
tests/unit/execution/test_r3_failure_publication_trace.py
tests/unit/execution/test_r3_public_signature_freeze.py
tests/unit/execution/test_r3_route_executors.py
tests/unit/execution/test_r3_within_route_baseline.py
tests/unit/generation
tests/unit/generation/test_generate_determinism_ratchets.py
tests/unit/generation/unique/test_gen_unique_old_input_bytes.py
tests/unit/generators
tests/unit/plan
tests/unit/relationships
tests/unit/run_result
tests/unit/scripts
tests/unit/strategy_catalog
tests/unit/test_public_api.py
tests/unit/test_public_api_v2_choke_points.py
tests/unit/test_r3_10_generation_key_contract.py
tests/unit/test_reason_manifest_policy.py
tests/unit/test_reason_registry.py
tests/unit/test_reason_release_pin.py
tests/unit/test_registry_renumber.py
tests/unit/test_v2_generation.py
tests/unit/test_wave_b_definition_owners.py
```

The `sentry` step runs all of `tests/sentry` (the new
`defects/test_reference_pool_choke_point.py`, `defects/test_generate_option_reads.py`,
module-size, fan-out and the reason registry, pin, fixture and policy sentries); the `layers`
step runs `tests/sentry/layout/test_import_layers.py`.

## Owner rulings (2026-11-12)

Two rulings on PR #523, and the orchestrator's decision on the helper's placement. They are the
only changes after the first build.

1. Seam sentry: yes. `tests/sentry/test_physical_seam_disconnection.py` gets one
   `permitted_non_physical` entry for `src/decoy_engine/execution/_probe_scale.py`, with a
   comment that says what changed and that the module imports nothing from
   `execution.physical`. `GUARDED_MODULES` and `DELIBERATELY_CONNECTED_MODULES` are unchanged.
2. Table-kind sentry: add a helper, never raise the baseline, never reword code past the sentry.
   `config/_tables.py` (the sentry's owner module, now 493 lines) gains
   `generate_columns_of(table: Mapping[str, Any]) -> list[Any]`, whose body is
   `table.get("generate_columns") or []`. It is exported from `decoy_engine.config` beside
   `classify_table_kinds`.

What changed for ruling 2:

- The four loops the slice added call the helper: `config/_generate_relational.py`
  (`per_parent_problems`), `generation/relational/_cardinality.py` (the policy lookup) and the
  two in `execution/_probe_scale.py::_loosen_per_parent_bounds`. A missing key, `None` and an
  empty list still give an empty loop, and a non-empty list is the same object. Each site keeps
  its own handling of a table that is not a dict.
- Approved deviation from the plan's file list: the helper and its test
  (`tests/unit/config/test_generate_columns_of.py`) are not in the plan's slice row, and
  `config/_tables.py`, `config/__init__.py` and the sentry's two baseline files change for it.
- `classify_table_kinds` reads the key through the helper
  (`"generate" if generate_columns_of(table) else "mask"`). This is a one-line,
  behavior-preserving change made so the owner module's count does not rise: the helper's body
  is itself a counted truth test, and with the classifier's inline read the owner would hold two
  against a baseline of 1. It now holds exactly one. Four cells of the helper's test pin the
  classifier's output for a missing key, `None`, an empty list and a non-empty list; they pass
  before and after the change.
- The two older counted sites in `execution/_probe_scale.py` (`downscale_config` and
  `downscale_job`, both `if not table.get("generate_columns")`) also call the helper, with the
  same truthiness. That module's count is now zero, so its entry is deleted from
  `TRUTH_TEST_BASELINE` and from `tests/sentry/baselines/table-kind-truth-tests.json` (2 to
  absent). No number rose and no module was added.
- Imports: `generation` and `execution` import the helper from the public `decoy_engine.config`
  package, as they already do for `value_key` and `post_scan_integrity_requested`;
  `_generate_relational.py` imports it from its sibling `config._tables`. `.importlinter` is
  unchanged.
- No changelog change: the helper is engine-internal and the commits that add it are `test` and
  `refactor`; `changelog.d/gen-rel-e1.md` already covers the slice.

Heads:

- `origin/main` moved to `44bbf494` (#527, #533, #524, #526) and is merged as `6aa51efa`. One
  conflict, in `CODEMAP.md`, resolved as the union of both rows (the branch's `config/` row and
  main's `plan/` row). #524 adds `declares_unique` and `plan/checks/_relationship_keys.py`;
  nothing in them reads `generate_columns` in a counted spelling, and the sentries and the T1
  set are green on the merge.
- `3d5f03ae` red helper test, `8aa8157d` the helper and the call sites, `27858cc3` the seam
  sentry entry. Final source head: `27858cc3` (the last `src/` change is `8aa8157d`).

Red first (`3d5f03ae`, source as merged at `6aa51efa`): 5 failed, 4 passed. The five helper
cells fail because `decoy_engine.config` has no `generate_columns_of`; the four classifier cells
pass by design, since they pin behavior that must not move.

### Mutation rerun (final source head `27858cc3`, detached worktree, 54 of 54 red)

All 53 recorded mutations and one new one for the helper (H1). Each was applied once in a
detached worktree that imports its own `src`, its named tests run (Python 3.11.18, the `.venv`),
and the file restored; the worktree was clean at the end and is removed. Every anchor matched
exactly once, and the red count of each of the 53 equals the first run's.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| M1 | restore the with-replacement fill | `generation/relational/_cardinality.py` | 2: `test_at4_max_holds_over_200_seeds` |
| M2 | drop the runtime feasibility check | `generation/relational/_cardinality.py` | 8: `test_at15_runtime_refusal_names_the_table_and_column_only`, `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_min_above_capacity_refuses_at_run` ... |
| M3a | remove the pool-check call from the whole-table draw (_reference) | `generation/_synthesize_derived.py` | 6: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_at6_fail_refuses_an_empty_parent`, `test_the_helper_is_defined_once_and_called_only_by_the_listed_routes` ... |
| M3b | remove the pool-check call from the streamed draw (ReferenceStream.__init__) | `generation/_column_streams.py` | 8: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_at6_fail_refuses_an_empty_parent`, `test_reference_pool_check_runs_once_per_stream_not_once_per_batch` ... |
| M3c | remove the pinned route's policy hand-off (_pinned_execute._run) | `generation/_pinned_execute.py` | 4: `test_at6_fail_refuses_an_empty_parent`, `test_at6_pinned_warn_logs_once_without_a_callback`, `test_pinned_execute_honors_fail_for_an_empty_parent` ... |
| M4 | use a Python assert for the final count check | `generation/relational/_cardinality.py` | 2: `test_at4_the_final_check_is_an_explicit_branch_not_a_python_assert`, `test_the_relational_package_has_one_emitter_keyed_counters_and_no_assert` |
| M5 | use a raw parent value as a counter key | `generation/relational/_cardinality.py` | 4: `test_at4_v1_caller_repairs_unhashable_keys_to_exact_bounds`, `test_at4_v2_caller_repairs_unhashable_keys_to_exact_bounds` |
| M6 | a second relationship parser in generation | `generation/relational/_cardinality.py` | 2: `test_at6r_generation_reads_policies_through_the_relationship_normalizer`, `test_generation_never_parses_a_relationship_policy_itself` |
| M7 | a direct warning log call instead of the emitter | `generation/relational/_cardinality.py` | 6: `test_at6_each_child_of_one_relationship_gets_its_own_policy`, `test_at6_warn_keeps_nulls_and_emits_exactly_one_warning`, `test_empty_parent_policy_tokens` ... |
| M8 | generic empty-parent handling before fitted handling | `generation/_synthesize_derived.py` | 7: `test_at6f_an_empty_fitted_pool_keeps_the_plan_refusal_under_every_policy`, `test_the_whole_table_draw_checks_the_pool_before_any_value` |
| M9 | policy-token interpolation in _graph.py | `relationships/_graph.py` | 3: `test_an_invalid_policy_is_refused_without_echoing_it` |
| M9b | policy-token interpolation in _graph.py (duplicate and conflict builders) | `relationships/_graph.py` | 3: `test_a_duplicate_names_neither_policy`, `test_conflicting_policies_raise`, `test_same_parent_same_child_different_policies_raises` |
| A1a | AT-1: reseed the draw (changes draw order) | `generation/_synthesize_derived.py` | 8: `test_at1_near_neighbor_bytes_do_not_move` |
| A1a2 | AT-1: reorder a repair shuffle (changes draw order inside the moved repair) | `generation/relational/_cardinality.py` | 2: `test_at1_repair_without_a_fill_is_byte_identical_to_the_base` |
| A1b | AT-1: compiled policy metadata enters seed derivation | `generation/_pinned_execute.py` | 4: `test_at1_a_non_empty_reference_ignores_the_policy_on_every_route`, `test_at1_near_neighbor_bytes_do_not_move`, `test_reference_policy_is_plan_metadata_not_a_column_or_seed_input` |
| A1c | AT-1: zero bound read as a bound at run | `generation/relational/_cardinality.py` | 18: `test_at1_near_neighbor_bytes_do_not_move`, `test_at3_check_reference_pool_feasibility_boundaries` |
| A1c2 | AT-1: zero bound enters the repair | `generation/_synthesize_derived.py` | 1: `test_at1_unbounded_reference_never_enters_the_repair` |
| A1c3 | AT-2: zero max read as a ceiling at validate | `config/_generate_relational.py` | 2: `test_at2_consistent_bounds_validate` |
| A1d | AT-1: branch on the new plan key when it is absent | `generation/_pinned_plan.py` | 2: `test_at6_pinned_plan_carries_the_policy_and_its_fingerprint_follows_it`, `test_plan_without_reference_policies_keeps_its_serialized_form` |
| A1e | AT-1: a policy changes a non-empty draw | `generation/relational/_cardinality.py` | 6: `test_at1_a_non_empty_reference_ignores_the_policy_on_every_route`, `test_at1_near_neighbor_bytes_do_not_move` |
| A3a | AT-3: refuse the min equality boundary at run | `generation/relational/_cardinality.py` | 5: `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_equal_bounds_put_every_parent_at_exactly_that_count` |
| A3b | AT-3: refuse the max equality boundary at run | `generation/relational/_cardinality.py` | 5: `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_equal_bounds_put_every_parent_at_exactly_that_count` |
| A3c | AT-3: refuse the max equality boundary at validate | `config/_generate_relational.py` | 1: `test_at3_equality_is_feasible_and_min_waits_for_the_run` |
| A4a | AT-4: the run checks row count instead of distinct keys | `generation/_synthesize_derived.py` | 1: `test_at4_the_run_checks_distinct_keys_not_row_count` |
| A4b | AT-4: a multiset of all free slots (allocation proportional to max) | `generation/relational/_cardinality.py` | 1: `test_at4_the_fill_allocates_by_rows_and_parents_not_by_max_times_parents` |
| A5a | AT-5: strip the bounds in the probe | `execution/_probe_scale.py` | 12: `test_a_full_size_probe_loosens_nothing`, `test_a_parent_with_unknown_distinct_keys_gets_the_widest_bounds`, `test_a_single_bound_writes_both_scaled_fields` ... |
| A5b | AT-5: keep the bounds unscaled in the probe | `execution/_probe_scale.py` | 10: `test_a_parent_with_unknown_distinct_keys_gets_the_widest_bounds`, `test_a_single_bound_writes_both_scaled_fields`, `test_an_empty_scaled_child_drops_the_minimum_only` ... |
| A5c | AT-5: apply the sequence formula to every parent | `execution/_probe_scale.py` | 2: `test_a_parent_with_unknown_distinct_keys_gets_the_widest_bounds` |
| A5d | AT-5: float ceiling | `execution/_probe_scale.py` | 1: `test_the_ceiling_is_exact_above_float_precision` |
| A5e | AT-5: an empty scaled parent is divided by | `execution/_probe_scale.py` | 1: `test_an_empty_scaled_parent_leaves_the_bounds_alone` |
| A5f | AT-5: rewrite a reference that has no positive bound | `execution/_probe_scale.py` | 2: `test_a_reference_without_a_positive_bound_is_not_rewritten` |
| A5g | AT-5: rewrite the caller's config instead of the copy | `execution/_probe_scale.py` | 1: `test_the_rewrite_does_not_touch_the_original_config` |
| A5h | AT-5: a parent outside the config gets the widest bounds | `execution/_probe_scale.py` | 1: `test_a_parent_outside_the_config_leaves_the_bounds_alone` |
| A6a | AT-6: the fingerprint ignores the policy | `generation/_pinned_plan.py` | 2: `test_at6_pinned_plan_carries_the_policy_and_its_fingerprint_follows_it`, `test_reference_policy_is_plan_metadata_not_a_column_or_seed_input` |
| A6b | AT-6: the normalizer reads the first child only | `relationships/_graph.py` | 1: `test_at6_each_child_of_one_relationship_gets_its_own_policy` |
| A6c | AT-6: an empty child is refused under fail | `generation/relational/_cardinality.py` | 2: `test_at6_an_empty_child_under_fail_is_not_refused`, `test_empty_parent_policy_tokens` |
| A6f1 | AT-6f: fitted ignores bounds at run | `generation/_synthesize_derived.py` | 2: `test_at6f_a_bound_on_a_fitted_reference_refuses_at_run` |
| A6f2 | AT-6f: fitted bounds refuse an empty pool before the allocator | `generation/relational/_cardinality.py` | 10: `test_at6f_an_empty_fitted_pool_keeps_the_plan_refusal_under_every_policy` |
| A6f3 | AT-6f: fitted with a bound validates | `config/_generate_relational.py` | 3: `test_at6f_a_positive_bound_with_fitted_is_refused`, `test_the_refusal_order_is_conflict_then_fitted_then_nulls_then_ceiling` |
| A6f4 | AT-6f: fitted without a positive bound is refused | `config/_generate_relational.py` | 1: `test_at6f_fitted_without_a_positive_bound_validates` |
| A14 | AT-14: raise a code the registry does not hold | `reasons/runtime/_part2.py` | 5: `test_at14_new_codes_are_registered_unreleased_in_the_right_catalog`, `test_at6_fail_refuses_an_empty_parent` |
| A15a | AT-15: format a count into a refusal | `generation/relational/_cardinality.py` | 12: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_at15_runtime_refusal_names_the_table_and_column_only`, `test_at6_fail_refuses_an_empty_parent` ... |
| A15b | AT-15: raise the refusal inside an except block | `generation/relational/_cardinality.py` | 7: `test_at15_runtime_refusal_names_the_table_and_column_only`, `test_at3_check_reference_pool_feasibility_boundaries`, `test_at3_min_above_capacity_refuses_at_run` |
| A15c | AT-15: format the bounds into a validate problem | `config/_generate_relational.py` | 3: `test_at15_validate_refusals_carry_the_fixed_summary_and_no_input`, `test_at2_min_above_max_is_refused`, `test_problems_are_locations_and_codes_only` |
| B1 | AT-2b: an explicit zero null_probability counts as set | `config/_generate_relational.py` | 2: `test_at2b_an_explicit_zero_is_off` |
| B2 | validate judges an empty parent or child against the ceiling | `config/_generate_relational.py` | 1: `test_an_empty_parent_or_child_skips_the_ceiling` |
| B3 | null conflict dropped at validate | `config/_generate_relational.py` | 3: `test_at2b_null_probability_with_a_positive_bound_is_refused`, `test_the_refusal_order_is_conflict_then_fitted_then_nulls_then_ceiling` |
| B4 | a new parent-key reader outside the listed draws | `generation/_column_streams.py` | 1: `test_parent_keys_are_read_only_by_the_listed_draws` |
| B5 | weights mismatch tolerated (uniform fallback restored in the helper) | `generation/relational/_cardinality.py` | 5: `test_at13_a_weights_length_mismatch_refuses_at_run`, `test_weights_are_checked_against_the_distinct_key_count` |
| B6 | the pool facade stops delegating to the foundation predicate | `generation/pool/_capacity.py` | 2: `test_config_and_the_pool_facade_share_the_foundation_predicate`, `test_the_pool_export_is_a_facade_over_the_foundation_leaf` |
| B7 | the validate ceiling bypasses the shared predicate | `config/_generate_relational.py` | 2: `test_config_and_the_pool_facade_share_the_foundation_predicate`, `test_the_ceiling_goes_through_the_predicate_with_max_times_row_count` |
| B8 | pinned policy decoded laxly | `generation/_pinned_plan.py` | 3: `test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw`, `test_serialized_reference_policies_decode_strictly` |
| B9 | the streamed pool check runs per batch | `generation/_column_streams.py` | 3: `test_reference_pool_check_runs_once_per_stream_not_once_per_batch`, `test_the_helper_is_defined_once_and_called_only_by_the_listed_routes`, `test_the_streamed_draw_checks_once_at_construction` |
| H1 | the generate-columns helper drops its empty-list default | `config/_tables.py` | 2: `test_a_table_without_generate_columns_gives_an_empty_list` |

### Gates (final source head `27858cc3`)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests <T1 paths>` with `origin/main` at `44bbf494`. The
T1 paths are the 57 listed above; `tests/unit/config` holds the new helper test and the `sentry`
step runs all of `tests/sentry`. Python 3.10.20 (a venv synced with
`uv sync --frozen --python 3.10 --extra dev --extra lint --extra vault --extra cloud`, as CI
uses), PyArrow 24.0.0, DuckDB 1.5.4, pandas 2.3.3, NumPy 2.2.6, Faker 40.23.0; the `layers` step
ran in `.venv-layers` on Python 3.10.20.

| Step | Branch |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5217 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 6520 passed, 0 failed, 60 skipped (6510 in the parallel pass, 10 in the serial pass) |

- Failure sets: branch 0, main 0, branch-only 0. Every branch step passed, so the gate script
  did not rerun `origin/main`.
- `test_production_execution_modules_are_byte_identical_to_origin_main` and
  `test_inline_generate_truth_tests_only_go_down` both pass, as does
  `test_baseline_holds_no_stale_entries`. No other test, sentry, type or lint step fails.
- The mypy `dp_provenance.py:517` error in the first build's table belongs to Python 3.11 only;
  under 3.10 mypy is clean.
- On the docs head that adds this section (a clean tree):
  `python scripts/dev/registry_renumber.py --check --main origin/main`: `registry consistent`.
  `python scripts/release/reason_release_pin.py --check`: `reason release pin current`.
  `python scripts/dev/check_fragment.py --base origin/main`: exit 0.
  `python scripts/gen_capability_matrix.py`: no change.
  The `ruff`, `format` and `sentry` steps were rerun there too, with the same totals.

Not run for the rulings: the full suite; the paths the "Not run" section lists; the unit steps
under Python 3.11 (the mutation rerun used 3.11, the gates 3.10); a rerun of `origin/main`
(no branch step failed).

## Main merges (2026-11-12)

Two merges of `origin/main` into the branch, each made once for one reported conflict. Every
total below names the head it was measured on.

### First merge: head `68034ff0` (main `353d98bf`)

T1 gates on `68034ff0`, Python 3.10.20:

| Step | Branch (`68034ff0`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5241 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 11765 passed, 0 failed, 62 skipped |

`registry_renumber.py --check --main origin/main`, `ratchet_check.py --base origin/main` and
`check_fragment.py --base origin/main` each exited 0 on that head.

### Second merge: head `7cf17860` (main `c0961a17`)

`origin/main` `c0961a17` brought GEN-TYPES E3 (`lookup` columns). Two files conflicted:

- `docs/reasons.md`: both paragraphs are kept, each on its own, in the order GEN-REL E1,
  GEN-TYPES E3, CONFIG-EXPLICIT E2b-r.
- `src/decoy_engine/generation/synthesize.py`: four regions, each resolved as the union. The
  imports hold main's `LookupList`, `lookup_digests` and `lookup_column_values` beside
  `reference_policies`; the docstring keeps main's `lookup_lists` paragraph and this branch's
  `on_warning` wording; the `_generate_column` call and signature take `lookup_lists`, then
  `reference_policy` and `on_warning`. Main's `lookup` branch, its `content_digests` hand-off
  to the null mask and this branch's reference keyword assembly merged without conflict and
  are unchanged.

Auto-merged files that both sides touch were read against both parents: `_admission.py`,
`_pinned_compile.py`, `_numeric_typing.py`, `_synthesize_derived.py`, `config/_tables.py`,
`config/_generate_options.py` and `CODEMAP.md`. A `lookup` column is resident-only in the
admission matrix and refused by the pinned compile, so it never reaches the streamed or
pinned reference routes; it has no parent pool and adds no caller of `_reference`,
`_generate_column` or `check_reference_pool`. `_plan_entry.py` passes `lookup_lists` through
`legacy_kwargs`, which `_streamed.py` forwards unchanged. No semantic adaptation was needed.

Follow-up commit `87d8f8a6` (`refactor(generation): move reference draw arguments to
relational`): the union left `synthesize.py` at 601 lines, one over the 600-line goal, and
main records no census entry for it. The keyword assembly for the reference draw (the fitted
fan-out, and the policy and warning sink when either is set) moved unchanged into
`generation/relational/_cardinality.py` as `reference_draw_extras`. `synthesize.py` is now 599
lines and `_cardinality.py` 299; `_streamed.py` is untouched by main and stays at 598. No
census number, cap, baseline or allowlist was added or raised.

Final source head: `87d8f8a6`.

Mutation rerun on `87d8f8a6` (detached worktree importing its own `src`, Python 3.10.20, removed
after): 55 of 55 red. The 54 recorded mutations kept their anchors (each matched exactly once)
and their red counts equal the previous run's. One new mutation covers the moved helper:

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| X1 | the whole-table route drops the policy from the reference draw arguments | `generation/relational/_cardinality.py` | 3: `test_at6_fail_refuses_an_empty_parent`, `test_at6_warn_keeps_nulls_and_emits_exactly_one_warning` |

T1 gates on `87d8f8a6`, Python 3.10.20, `GATES_WORKERS=4`, the 59 T1 paths plus
`tests/unit/reasons` (main's new reason tests); main's lookup tests sit inside
`tests/unit/generation`, `tests/unit/plan` and `tests/unit/config`:

| Step | Branch (`87d8f8a6`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5279 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 11909 passed, 0 failed, 62 skipped |

Failure sets: branch 0, main 0, branch-only 0; no step failed, so `origin/main` was not rerun.

Checks on `87d8f8a6` with `origin/main` at `c0961a17`:

- `python scripts/dev/registry_renumber.py --check --main origin/main`: exit 0.
- `python scripts/dev/ratchet_check.py --base origin/main`: exit 0.
- `python scripts/dev/check_fragment.py --base origin/main`: exit 0.
- `python scripts/release/reason_release_pin.py --check`: exit 0.

Not run for this merge: the full suite and the paths the "Not run" section lists.

## Not run

- The full suite. Before the merges, a wider sweep of `tests/unit/{generation, generators, config,
  plan, relationships}`, `tests/sentry`, `tests/integration/golden`, `tests/generation` and the
  Phase F parity files found the fixtures listed above and nothing else. `tests/unit/execution`
  beyond the probe files, the rest of `tests/parity`, `tests/acceptance` beyond the gate pins,
  `tests/integration` beyond `golden`, `tests/native` beyond the gate pins and `testflight` were
  not run.
- Python 3.10 for the unit steps.

## V2-LOG

- A `unique_together` pair member over an empty parent is refused by the pair draw's own
  uniqueness refusal, not by the orphan policy. `unique_pair` does not call the pool check.
- The V1 `ColumnGenerator` reference draw keeps its uniform fallback for mismatched weights and
  its null column for an empty parent; only the repair is shared. Infeasible bounds on that
  route surface as `runtime.reference_bounds_unsatisfied` from the repair's final check.
- A hand-built pinned plan with a malformed `reference_policies` entry is refused with the
  existing `plan.pinned_plan_malformed` at execute. The plan's AT-10 cell asks for the runtime
  twin, whose code is registered in E2.
- `generation/synthesize.py` is at 599 lines after the second main merge and
  `generation/_streamed.py` at 598; E2 and E3 add to `synthesize.py` and will need to move code
  out to stay at or under 600.
- `config/_generate_unique.py` still reaches the capacity rule through the pool facade (see
  above); switching it to the foundation leaf would also retire one `.importlinter` ignore.
- Runtime reference refusals use the `runtime` catalog defaults (outcome `execution`, retry
  `unknown`), as `uniqueness_impossible` does, although the remedy is a config repair.
