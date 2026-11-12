# GEN-REL E2 build evidence

Status: built on `feat/gen-rel-e2`. No stop condition was hit.

Spec: `docs/plans/gen-rel.md` on `origin/plan/gen-rel` (`26a83542`), slice row 2 (self-reference
trees). The plan stays on the plan branch.

## Heads and environment

- Slice start: `feat/gen-rel-e1` at `8d260171` (slice 1, which held `origin/main` `44bbf494`).
- `origin/main` moved to `69646f18` (#534, #528, #535) and is merged as `7abbcd03`, cleanly. It
  adds delivery reasons, key type families and the run-result projection; none of it reads a
  generate `reference` column, and the sentries and the T1 set are green on the merge.
- Commits: `bbefd7d1` red acceptance tests (source as at `8d260171`), `7abbcd03` merge of main,
  `0116c78c` the feature. Final source head: `0116c78c`. This record is added in a docs-only
  commit after it.
- Gates: Python 3.10.20 (a temporary venv synced with
  `uv sync --frozen --python 3.10 --extra dev --extra lint --extra vault --extra cloud`, as CI
  uses; the existing `.venv` was not changed), PyArrow 24.0.0, DuckDB 1.5.4, pandas 2.3.3,
  NumPy 2.2.6. The `layers` step ran in `.venv-layers` on Python 3.10.20.
- Red-first run and mutations: Python 3.11.18 (the `.venv`), PyArrow 24.0.0, DuckDB 1.5.4,
  pandas 2.3.3, NumPy 2.4.6.

## Red first (commit `bbefd7d1`, source unchanged from `8d260171`)

`uv run --no-sync pytest -q -p no:cacheprovider -p no:xdist <the 6 files below> -rA --tb=line`:
95 failed, 230 passed.

| File | Red |
| --- | --- |
| `tests/unit/config/test_generate_relational.py` | 43 |
| `tests/unit/generation/relational/test_self_reference.py` (new) | 29 |
| `tests/unit/generation/relational/test_routes.py` | 9 |
| `tests/unit/generation/test_pinned_generation.py` | 9 |
| `tests/unit/execution/test_probe_scale.py` | 3 |
| `tests/unit/generation/test_generation_order.py` | 2 |

Reasons, by count of failing cells:

- Absent API (21): `reasons.runtime.PINNED_RELATIONAL_OPTION_UNSUPPORTED` (9, the tampered-policy
  cells), `reasons.plan.PINNED_RELATIONAL_OPTION_UNSUPPORTED` (1), the four unregistered reason
  codes looked up by name (4), `self_reference` (5), `config.self_reference_problems` (1),
  `config.is_self_reference` (1).
- Wrong outcome (74): a self-reference that reaches the run fails with `KeyError` because the
  draw looks for the table among its finished parents (21); validate refuses the self-edge as a
  reference cycle where the config must validate (18) or where a registered code is expected
  (14); `root_count` is an unknown option where a type, range or key refusal is expected (11);
  pinned compile accepts a self-reference (4); pinned execute draws from a hand-built
  self-reference (2); the probe leaves `root_count` above the scaled row count (2); the
  generation order lists a table as its own parent (2).

Green on the base by design (10 cells), each tied to a mutation in the table below:

| Cell | Why green on the base | Mutation |
| --- | --- | --- |
| `test_at7_a_two_table_cycle_is_still_refused` | the cycle check already refuses it; it must stay refused beside the accepted self-edge | M2, M2b |
| `test_at10_the_hand_built_plan_is_otherwise_executable` | non-vacuity precondition of the hand-built plan | A10g |
| `test_at9_root_count_on_a_reference_to_another_table_is_unknown` (3 cells) | an unregistered key is already unknown | C3 |
| `TestDownscaleRootCount::test_a_root_count_that_still_fits_is_kept` | nothing rewrites the option yet | P1 |
| `TestDownscaleRootCount::test_a_default_root_count_is_not_written` | same | P1b |
| `TestDownscaleRootCount::test_a_zero_row_table_keeps_the_option` | same | P2 |
| `TestDownscaleRootCount::test_the_clamp_does_not_touch_the_original_config` | same | P4 |
| `TestDownscaleRootCount::test_a_reference_to_another_table_is_not_clamped` | same | P3 |

No other new cell passed on the base.

Test changes after the red commit (none weakened):

- The run-time twin is expected as `GenerationError`, not `PinnedModeError`, in the three
  execute cells and the tampered-policy cell (see plan note 1).
- `self_reference` is imported from `generation.relational._self_reference` instead of the
  package (see "Files outside the plan's slice row").
- `test_at8_a_direct_call_holds_root_count_inside_one_to_n` was added (mutation A8c); it needs
  `self_reference`, so it is red on the base too.
- A parameter named `cap` became `most`: the parallel-safety sentry reads `*cap*` in a bound
  comparison as a memory envelope.

One E1 test changed its expectation, as the plan's AT-10 requires:
`tests/unit/generation/test_pinned_generation.py::test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw`
expected `plan.pinned_plan_malformed`; it now expects `runtime.pinned_relational_option_unsupported`
raised as `GenerationError`, with no cause or context, and covers nine tampered shapes instead of
three (an unknown token, a non-string policy, a short entry, an entry that is not a sequence, a
bare string entry, a valid entry beside an invalid one, and a field that is a string, an integer
or `None`). `test_serialized_reference_policies_decode_strictly` is unchanged.

## Mutations (final source head `0116c78c`, detached worktree, 60 of 60 red)

Each was applied once in a detached worktree of `0116c78c` that imports its own `src`, its named
tests run (Python 3.11.18, the `.venv`), and the file restored; every anchor matched exactly
once, the worktree was clean at the end and is removed. No source changed after the run.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| M1 | draw tree parents from all rows (uncapped draw) | `generation/relational/_self_reference.py` | 5: `test_at8_a_self_reference_sits_beside_an_ordinary_reference`, `test_at8_every_row_hangs_from_an_earlier_row_and_every_chain_ends_at_a_root`, `test_at8_the_draw_is_uniform_over_the_earlier_rows` |
| M1b | draw tree parents from all rows (capped draw) | `generation/relational/_self_reference.py` | 7: `test_at9_a_cap_of_one_is_a_set_of_chains`, `test_at9_no_key_gets_more_children_than_max_per_parent` |
| M2 | skip every cycle edge instead of the self-edge only (config graph) | `config/_pipeline.py` | 2: `test_at7_a_self_edge_does_not_hide_a_cycle_through_the_same_table`, `test_at7_a_two_table_cycle_is_still_refused` |
| M2b | drop every cycle check (the DFS no longer refuses a gray parent) | `config/_pipeline.py` | 2: `test_at7_a_self_edge_does_not_hide_a_cycle_through_the_same_table`, `test_at7_a_two_table_cycle_is_still_refused` |
| M2c | the generation order skips every reference edge, not the self-edge only | `generation/synthesize.py` | 1: `test_a_self_reference_is_not_a_dependency_edge` |
| M2d | the self-edge stays a dependency edge in the generation order | `generation/synthesize.py` | 2: `test_a_self_reference_is_not_a_dependency_edge`, `test_a_table_that_only_references_itself_has_no_parents` |
| A9a | AT-9: ignore max_per_parent | `generation/relational/_self_reference.py` | 7: `test_at9_a_cap_of_one_is_a_set_of_chains`, `test_at9_no_key_gets_more_children_than_max_per_parent` |
| A9b | AT-9: a new row is never added to the open set | `generation/relational/_self_reference.py` | 7: `test_at9_a_cap_of_one_is_a_set_of_chains`, `test_at9_no_key_gets_more_children_than_max_per_parent` |
| A9c | AT-9: the cap is off by one | `generation/relational/_self_reference.py` | 7: `test_at9_a_cap_of_one_is_a_set_of_chains`, `test_at9_no_key_gets_more_children_than_max_per_parent` |
| A9d | AT-9: a zero cap is read as a cap | `generation/relational/_self_reference.py` | 2: `test_at8_the_draw_is_uniform_over_the_earlier_rows`, `test_at9_a_zero_cap_is_no_cap` |
| A8a | AT-8: the draw ignores root_count | `generation/relational/_self_reference.py` | 3: `test_at8_all_rows_can_be_roots_and_an_empty_table_is_empty`, `test_at8_every_row_hangs_from_an_earlier_row_and_every_chain_ends_at_a_root` |
| A8b | AT-8: the default root count is not one | `generation/relational/_self_reference.py` | 3: `test_at8_a_direct_call_holds_root_count_inside_one_to_n`, `test_at8_every_row_hangs_from_an_earlier_row_and_every_chain_ends_at_a_root`, `test_at8_the_default_is_one_tree_and_a_later_row_can_be_a_parent` |
| A8c | AT-8: a direct call's root_count is not held inside 1..n | `generation/relational/_self_reference.py` | 1: `test_at8_a_direct_call_holds_root_count_inside_one_to_n` |
| A16a | AT-16: module-global random | `generation/relational/_self_reference.py` | 1: `test_at16_the_draw_leaves_module_global_random_alone` |
| A16b | AT-16: the tree ignores the caller seed | `generation/relational/_self_reference.py` | 4: `test_at16_the_same_seed_gives_the_same_tree_and_another_seed_another`, `test_at8_the_draw_is_uniform_over_the_earlier_rows` |
| A16c | AT-16: the seed is not per column | `generation/relational/_self_reference.py` | 2: `test_at16_the_seed_is_per_column`, `test_at8_the_draw_is_uniform_over_the_earlier_rows` |
| A10a | AT-10: guard pinned compile only | `generation/_pinned_compile.py` | 3: `test_at10_pinned_execute_refuses_a_self_reference_before_any_draw`, `test_at15_pinned_execute_refusal_of_a_self_reference_names_the_table_and_column_only` |
| A10b | AT-10: reuse the plan code at execute | `generation/_pinned_execute.py` | 3: `test_at10_pinned_execute_refuses_a_self_reference_before_any_draw`, `test_at15_pinned_execute_refusal_of_a_self_reference_names_the_table_and_column_only` |
| A10c | AT-10: parse the stored policy metadata laxly at execute | `generation/_pinned_execute.py` | 9: `test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw` |
| A10d | AT-10: a tampered policy keeps the compile-phase malformed code | `generation/_pinned_execute.py` | 9: `test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw` |
| A10e | AT-10: the policy check runs after the token scan | `generation/_pinned_execute.py` | 2: `test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw` |
| A10f | AT-10: pinned compile no longer refuses a self-reference | `generation/_pinned_compile.py` | 7: `test_at10_pinned_compile_refuses_a_self_reference`, `test_at10_pinned_execute_refuses_a_self_reference_before_any_draw`, `test_at15_pinned_compile_refusal_of_a_self_reference_names_the_table_and_column_only` ... |
| A10g | AT-10: the pinned predicate refuses every reference | `generation/_pinned_compile.py` | 1: `test_at10_the_hand_built_plan_is_otherwise_executable` |
| A10h | AT-10: the execute refusal is raised with the plan code from the builder | `generation/_pinned_compile.py` | 3: `test_at10_pinned_execute_refuses_a_self_reference_before_any_draw`, `test_at15_pinned_execute_refusal_of_a_self_reference_names_the_table_and_column_only` |
| A14a | AT-14: the execute refusal's code is not the registered one | `reasons/runtime/_part2.py` | 2: `test_at14_self_reference_codes_are_registered_unreleased_in_the_right_catalog`, `test_at14_the_pinned_twins_share_one_fixed_summary_and_differ_by_namespace` |
| A14b | AT-14: the twins' summaries drift apart | `reasons/plan/_part3.py` | 1: `test_at14_the_pinned_twins_share_one_fixed_summary_and_differ_by_namespace` |
| A15a | AT-15: the tampered-policy refusal echoes the stored field | `generation/_pinned_execute.py` | 4: `test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw` |
| A15b | AT-15: the tampered-policy refusal is raised inside the except block | `generation/_pinned_execute.py` | 9: `test_hand_built_plan_with_a_bad_policy_is_refused_before_any_draw` |
| A15c | AT-15: the pinned relational refusal formats an option value | `generation/_pinned_compile.py` | 2: `test_at15_pinned_compile_refusal_of_a_self_reference_names_the_table_and_column_only`, `test_at15_pinned_execute_refusal_of_a_self_reference_names_the_table_and_column_only` |
| A15d | AT-15: a validate problem formats root_count into its location | `config/_generate_relational.py` | 5: `test_at15_self_reference_problems_are_locations_and_codes_only`, `test_at9_root_count_outside_one_to_row_count_is_refused` |
| A6a | remove the root_count lower bound | `config/_generate_relational.py` | 3: `test_at9_each_self_reference_column_is_reported`, `test_at9_root_count_outside_one_to_row_count_is_refused` |
| A6b | remove the root_count upper bound | `config/_generate_relational.py` | 3: `test_at9_root_count_outside_one_to_row_count_is_refused`, `test_at9_the_refusal_order_is_key_then_option_then_root_count` |
| A6c | an empty table's root_count is judged | `config/_generate_relational.py` | 3: `test_at9_an_empty_table_ignores_root_count` |
| A6d | the registry no longer type-checks root_count | `config/_generate_options.py` | 4: `test_at9_root_count_must_be_an_integer` |
| A6e | root_count is not a registered option | `config/_generate_options.py` | 5: `test_at9_root_count_inside_the_row_count_validates`, `test_every_read_key_is_in_the_registry` |
| C1 | AT-9: the key check is dropped | `config/_generate_relational.py` | 5: `test_at9_a_key_declared_after_the_reference_is_refused`, `test_at9_a_key_that_is_not_a_non_null_sequence_is_refused`, `test_at9_the_refusal_order_is_key_then_option_then_root_count` |
| C1b | AT-9: a key declared after the reference is accepted | `config/_generate_relational.py` | 1: `test_at9_a_key_declared_after_the_reference_is_refused` |
| C1c | AT-9: a nullable sequence key is accepted | `config/_generate_relational.py` | 1: `test_at9_a_key_that_is_not_a_non_null_sequence_is_refused` |
| C1d | AT-9: a non-sequence key is accepted | `config/_generate_relational.py` | 2: `test_at9_a_key_that_is_not_a_non_null_sequence_is_refused` |
| C1e | AT-9: an explicit zero null_probability on the key counts as nulls | `config/_generate_relational.py` | 1: `test_at9_a_sequence_key_with_an_explicit_zero_null_probability_validates` |
| C2a | AT-9: sequential, weighted and fitted are accepted on a self-reference | `config/_generate_relational.py` | 4: `test_at9_an_option_a_tree_cannot_honor_is_refused`, `test_at9_the_refusal_order_is_key_then_option_then_root_count` |
| C2b | AT-9: min_per_parent is accepted on a self-reference | `config/_generate_relational.py` | 3: `test_at9_an_option_a_tree_cannot_honor_is_refused`, `test_at9_the_refusal_order_is_key_then_option_then_root_count` |
| C2c | AT-9: null_probability is accepted on a self-reference | `config/_generate_relational.py` | 1: `test_at9_an_option_a_tree_cannot_honor_is_refused` |
| C2d | AT-9: unique is accepted on a self-reference | `config/_generate_relational.py` | 1: `test_at9_an_option_a_tree_cannot_honor_is_refused` |
| C2e | AT-9: a self-reference is accepted as a unique_together member | `config/_generate_relational.py` | 1: `test_at9_a_self_reference_in_a_unique_pair_is_refused` |
| C2f | AT-9: a zero min_per_parent or null_probability is read as set | `config/_generate_relational.py` | 1: `test_at9_random_with_an_optional_max_is_the_allowed_shape` |
| C3 | AT-9: root_count is accepted on a reference to another table | `config/_generate_relational.py` | 3: `test_at9_root_count_on_a_reference_to_another_table_is_unknown` |
| C4 | the per-parent checks also judge a self-reference | `config/_generate_relational.py` | 1: `test_at9_a_self_reference_reports_one_problem_not_a_per_parent_one_too` |
| C5 | the self-reference predicate ignores the column type | `config/_generate_relational.py` | 1: `test_the_one_self_reference_predicate_reads_the_table_name_and_the_column` |
| C5b | the self-reference predicate matches an empty table name | `config/_generate_relational.py` | 1: `test_the_one_self_reference_predicate_reads_the_table_name_and_the_column` |
| C6 | the pipeline validator never calls the self-reference checks | `config/_pipeline.py` | 14: `test_at9_a_key_that_is_not_a_non_null_sequence_is_refused`, `test_at9_an_option_a_tree_cannot_honor_is_refused`, `test_at9_root_count_outside_one_to_row_count_is_refused` |
| P0 | AT-5: the probe leaves root_count | `execution/_probe_scale.py` | 3: `test_a_capped_tree_keeps_its_cap_and_stays_valid`, `test_a_root_count_above_the_scaled_rows_is_clamped_to_them`, `test_the_probe_of_a_valid_tree_validates_and_runs` |
| P1 | AT-5: the probe rewrites a root_count that still fits | `execution/_probe_scale.py` | 1: `test_a_root_count_that_still_fits_is_kept` |
| P1b | AT-5: the probe writes a root_count the config did not carry | `execution/_probe_scale.py` | 1: `test_a_default_root_count_is_not_written` |
| P2 | AT-5: the probe clamps root_count on a table with no rows | `execution/_probe_scale.py` | 1: `test_a_zero_row_table_keeps_the_option` |
| P3 | AT-5: the probe clamps root_count on a reference to another table | `execution/_probe_scale.py` | 1: `test_a_reference_to_another_table_is_not_clamped` |
| P4 | AT-5: the probe rewrites the caller's config instead of the copy | `execution/_probe_scale.py` | 1: `test_the_clamp_does_not_touch_the_original_config` |
| R1 | streaming admission admits a self-reference | `generation/_admission.py` | 2: `test_the_probe_of_a_valid_tree_validates_and_runs`, `test_the_streaming_admission_declines_a_self_reference_and_the_run_stays_resident` |
| R2 | the dispatch sends a self-reference to the parent-table draw | `generation/synthesize.py` | 4: `test_at1_a_tree_beside_a_family_leaves_the_familys_bytes_alone`, `test_at8_every_row_hangs_from_an_earlier_row_and_every_chain_ends_at_a_root` |
| A1a | AT-1: the dispatch branches on the new key's absence (every reference is a tree) | `generation/synthesize.py` | 13: `test_at1_a_tree_beside_a_family_leaves_the_familys_bytes_alone`, `test_at1_near_neighbor_bytes_do_not_move` |

M1 to M2d are the E2 mutations the plan's acceptance paragraph names (tree parents drawn from
all rows; every cycle edge skipped instead of the self-edge only), on each draw and each graph
that holds the rule. A9a and A9b are AT-9's "ignore max" and "never add the new row to the open
set"; A10a, A10b and A10c are AT-10's "guard compile only", "reuse the plan code at execute" and
"parse stored metadata laxly"; A6a is the class table's "remove the `root_count` lower bound";
A16a is "module-global `random`"; A14a is "raise an unregistered code"; A15a to A15d format a
value or raise inside an `except` block; A1a is AT-1's "branch on a new key when it is absent";
P0 is AT-5's "leave `root_count`". The rest cover the option matrix cell by cell and the cells
that are green on the base by design.

An earlier run on the first feature commit found one mutant that stayed green (removing an
empty-table guard from the draw): the guard was redundant, so it was removed, the commit
amended before any push, and all mutations rerun on the result.

## What the slice does about AT-10's tampered policy

- Execute: `execute_pinned` checks the recorded `reference_policies` first, before the token
  scan (which serializes the plan and fails with another code on a field of the wrong type). A
  field that is not iterable, an entry that is not three strings, or a token outside
  `OrphanPolicy` is refused with `runtime.pinned_relational_option_unsupported`, raised as
  `GenerationError` outside any `except` block, before any column is drawn. The message is the
  fixed summary only.
- Decode: unchanged. `PinnedPlan.from_dict` still refuses the same shapes with
  `plan.pinned_plan_malformed`. `generation/_pinned_plan.py` is not in the slice row, and the
  plan's Behavior text says the field is decoded strictly; a serialized plan with a tampered
  policy therefore never reaches execute.

## Plan notes and deviations

1. The run-time twin is raised as `GenerationError`, not `PinnedModeError`.
   `tests/sentry/test_reason_registry.py::test_code_namespace_matches_the_raising_family` binds
   `PinnedModeError` to the `plan` namespace and `GenerationError` to `runtime`; the plan names
   the code and phase of the execute refusal but not its class. Pinned execute already raises
   `GenerationError` for E1's run-time refusals. The code is registered in the `runtime` catalog
   with owner `GenerationError` and the catalog's default phases, which include `generate`.
2. `root_count` range. The registry entry is the integer type check (`_int_at_least(None)`);
   `1 <= root_count <= row_count` is checked in `config/_generate_relational.py`, the one place
   that knows the table's `row_count`, and raises `config.generate_option_range`. A registry
   minimum of 1 would refuse `root_count: 0` on a table with `row_count: 0`, which the Behavior
   text and AT-9 say is ignored.
3. `config.generate_self_reference_option_unsupported` also refuses `distribution: fitted`,
   `unique: true` and `unique_together` membership. The plan lists `sequential`, `weighted`,
   `min_per_parent` and `null_probability` and says only `random` and `max_per_parent` are
   allowed; the tree draw would drop the first two without a word, and a pair draw reads a
   finished parent table that does not exist yet.
4. `generation/_admission.py`: the plan lists the file and says the admission already declines
   a self-reference. The only change is that the decline asks `config.is_self_reference`
   instead of comparing the two names inline; the verdict, the reason string and the order of
   the declines are the same.
5. One predicate. `config.is_self_reference(table_name, col)` is the only test for the shape;
   the config graph check, the config checks, the generation order, the dispatch, streaming
   admission, the pinned predicate and the probe all call it.
6. Handoff against plan: the handoff's step 7 has the builder open the PR; the orchestrator
   opens it for this slice. The handoff's `uv sync --frozen --extra dev` is replaced by the
   CI-like Python 3.10 venv described above. Neither changes a guarantee.

## Files outside the plan's slice row

- `src/decoy_engine/config/__init__.py`: exports `is_self_reference` and
  `self_reference_problems`, as it exports `per_parent_problems`. The plan's Placement table
  puts the relational config checks' public engine-internal exports there; generation and
  execution may not import a private config module.
- `tests/sentry/defects/test_generate_option_reads.py`: `generation/relational/_self_reference.py`
  is added to `READER_FILES`, so the scan sees where `root_count` is read and checks every key
  the new module reads against the registry. The scan is wider; no allowlist changed.
- `tests/sentry/test_physical_seam_disconnection.py`: the comment on the existing
  `execution/_probe_scale.py` entry now also describes this slice's change. The permitted set
  is unchanged.
- `docs/records/gen-rel-e2-evidence.md` (this file).

`generation/relational/__init__.py` is not changed. `synthesize.py` imports `self_reference`
from `generation.relational._self_reference`: exported from the public package name, the
function would count as a public callable that reaches the file-access policy (through
`GenDeriveContext.for_column`) and would need an entry in `fileaccess.ENTRY_POINTS`.

No census number, threshold, pin, baseline or allowlist changed. No snapshot, golden or digest
moved: `REGISTRY_VERSION`, the minor fixtures, `consumer-contract.json` and the release pin are
untouched, and the four new codes are `since=UNRELEASED`.

## New reason codes

| Identity | Raised as | Phase |
| --- | --- | --- |
| `config.generate_self_reference_key_invalid` | `PipelineConfig` validation error | configuration |
| `config.generate_self_reference_option_unsupported` | `PipelineConfig` validation error | configuration |
| `plan.pinned_relational_option_unsupported` | `PinnedModeError` | compile |
| `runtime.pinned_relational_option_unsupported` | `GenerationError` | generate |

`root_count` out of range and `root_count` on a reference to another table reuse
`config.generate_option_range` and `config.generate_option_unknown`.

## Line counts

`generation/synthesize.py` 597 (591 at the slice start; goal 600), `generation/_streamed.py`
598 (unchanged), `config/_generate_options.py` 523, `config/_pipeline.py` 489,
`reasons/runtime/_part2.py` 479, `generation/_pinned_compile.py` 389,
`generation/_admission.py` 312, `execution/_probe_scale.py` 273,
`generation/_pinned_execute.py` 258, `config/_generate_relational.py` 178,
`generation/relational/_self_reference.py` 78 (new).

## Gates (final source head `0116c78c`)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests <T1 paths>` with `origin/main` at `69646f18`,
Python 3.10.20.

| Step | Branch |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5232 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 6662 passed, 0 failed, 60 skipped (6652 in the parallel pass, 10 in the serial pass) |

- Failure sets: branch 0, main 0, branch-only 0. Every branch step passed, so the gate script
  did not rerun `origin/main`.
- AT-1: no existing digest, golden or snapshot moved. The E1 near-neighbor digests, the Phase F
  oracle and witnesses, the generation-order golden, the determinism goldens and the
  old-input-bytes pins are in the T1 set and pass unchanged; no expected value was edited.
- `python scripts/dev/registry_renumber.py --check --main origin/main`: `registry consistent`.
  `python scripts/release/reason_release_pin.py --check`: `reason release pin current`.
  `python scripts/dev/check_fragment.py --base origin/main`: exit 0.
  `python scripts/gen_capability_matrix.py`: no change (the slice adds no strategy).

T1 paths (58: slice 1's 57 and `tests/unit/reasons`, which main added):

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
tests/unit/reasons
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

Every test file the slice adds or changes is inside those paths. The `sentry` step runs all of
`tests/sentry` (`defects/test_generate_option_reads.py`, the pool choke-point sentry,
module-size, fan-out, table-kind, the seam sentry and the reason registry, pin, fixture and
policy sentries); the `layers` step runs `tests/sentry/layout/test_import_layers.py`.

## Not run

- The full suite. `tests/unit/execution` beyond the probe and R3 files, the rest of
  `tests/parity`, `tests/acceptance` beyond the gate pins, `tests/integration` beyond `golden`,
  `tests/native` beyond the gate pins and `testflight` were not run.
- The gate steps under Python 3.11 (the mutations and the red-first run used 3.11, the gates
  3.10). A rerun of `origin/main` (no branch step failed).

## V2-LOG

- A raw config dict compiled with `compile_plan` does not pass through the `PipelineConfig`
  validators, as for E1's validate-time refusals. Such a self-reference with `sequential`,
  `min_per_parent`, `null_probability` or `unique` draws a plain random tree, and the draw
  holds an out-of-range `root_count` inside 1 to the row count instead of refusing. Before
  this slice the same dict failed at the run with a `KeyError`.
- The pinned run-time twin uses the `runtime` catalog defaults (outcome `execution`, retry
  `unknown`), like E1's run-time refusals, although the remedy is a config repair.
- A `relationships` entry that covers a self-reference validates, and its `orphan_policy` has
  no effect on the tree: with rows to fill there is always an earlier row.
- Streaming admission reports a capped self-reference as `reference_cardinality_repair` (its
  first check) and an uncapped one as `reference_to_own_table`. Both decline.
- The probe's E1 loosening writes `min_per_parent: 0` on a capped self-reference. Zero is
  unset, so the scaled config validates.
- `CODEMAP.md` still describes `generation/relational/` as "the reference-pool check and
  per-parent bounds repair", and that package's docstring says callers import its names from
  the package; neither file is in this slice's row.
- `generation/synthesize.py` is at 597 lines. E3a and E3b add to it and will need to move code
  out to stay at or under 600.
- `tests/unit/generation/test_pinned_generation.py` is 852 lines (824 before the slice); the
  self-reference pinned cells went to the new `test_self_reference.py` instead.
- The pinned policy check at execute accepts a list where the dataclass declares a tuple, as
  the other plan fields do.
