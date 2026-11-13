# GEN-REL E2 build evidence

Status: built on `feat/gen-rel-e2`, with both review round 1 items (see "Review round 1") and
slice 1's review fixes merged forward (see "Forward merge of review fixes (2026-11-13)").

Spec: `docs/plans/gen-rel.md` on `origin/plan/gen-rel` (`26a83542`), slice row 2 (self-reference
trees). The plan stays on the plan branch.

## Heads and environment

- Slice start: `feat/gen-rel-e1` at `8d260171` (slice 1, which held `origin/main` `44bbf494`).
- Post-merge base: `origin/feat/gen-rel-e1` at `68034ff0`, which holds `origin/main` `353d98bf`.
  It is merged forward once, as `e27b9485` ("Merge feat/gen-rel-e1 into feat/gen-rel-e2"). One
  conflict, in `docs/reasons.md`, resolved by keeping both paragraphs (this slice's and
  CONFIG-EXPLICIT E2b-r's). Main is not merged into this branch directly.
- Commits: `bbefd7d1` red acceptance tests (source as at `8d260171`), `92d8a006` the feature,
  `f0f74d83` this record's first version, `e27b9485` the forward merge. Final source head:
  `e27b9485`; every mutation and gate total below belongs to it unless a line says otherwise.
  This record is updated in a docs-only commit after it.
- Two earlier local merges of `origin/main` (at `69646f18` and `a3cb996f`) were made before the
  owner's instruction to merge main only through slice 1; they were never pushed and are
  discarded, and the feature and record commits were replayed on the red commit unchanged.
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

## Mutations (final source head `e27b9485`, detached worktree, 60 of 60 red)

Each was applied once in a detached worktree of `e27b9485` that imports its own `src`, its named
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

The same 60 were red, with the same failing tests, on the feature commit before the forward
merge. The first run of all found one mutant that stayed green (removing an empty-table guard
from the draw): the guard was redundant, so it was removed before any push and every mutation
rerun on the result.

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

`generation/synthesize.py` 597 on `e27b9485` and 595 on the final head `6c1a8691` (591 at the
slice start; goal 600; see "Forward merges"), `generation/_streamed.py`
598 (unchanged), `config/_generate_options.py` 523, `config/_pipeline.py` 473,
`reasons/runtime/_part2.py` 479, `generation/_pinned_compile.py` 389,
`generation/_admission.py` 312, `execution/_probe_scale.py` 273,
`generation/_pinned_execute.py` 258, `config/_generate_relational.py` 178,
`generation/relational/_self_reference.py` 78 (new).

## Gates (final source head `e27b9485`)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests <T1 paths>` on `e27b9485`, Python 3.10.20, with
`origin/main` fetched at `89bee526`.

| Step | Branch (`e27b9485`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5248 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 6679 passed, 0 failed, 60 skipped (6669 in the parallel pass, 10 in the serial pass) |

- Failure sets: branch 0, main 0, branch-only 0. Every branch step passed, so the gate script
  did not rerun `origin/main`.
- AT-1: no existing digest, golden or snapshot moved. The E1 near-neighbor digests, the Phase F
  oracle and witnesses, the generation-order golden, the determinism goldens and the
  old-input-bytes pins are in the T1 set and pass unchanged; no expected value was edited.
- Checks on `e27b9485`, by exit code:
  - `python scripts/dev/registry_renumber.py --check --main 353d98bf` (the main slice 1 holds):
    exit 0, `registry consistent`.
  - `python scripts/dev/registry_renumber.py --check --main origin/main`: exit 2,
    `main-not-merged`. `origin/main` moved to `89bee526` during the build (#538 the run-result
    progress collector, #544 `decoy.process_out_of_memory`), two commits past `353d98bf`. They
    change `reasons/decoy/_core.py`, `errors.py`, `reason_adapter.py`, `run_result/` and two
    fragments, none of which this slice touches. By the owner's instruction main is merged once,
    through slice 1, so it is not merged here.
  - `python scripts/dev/ratchet_check.py --base origin/main`: exit 0.
  - `python scripts/dev/check_fragment.py --base origin/main`: exit 0.
  - `python scripts/release/reason_release_pin.py --check`: exit 0, `reason release pin current`.
  - `python scripts/gen_capability_matrix.py`: no change (the slice adds no strategy).
- Before the forward merge, the same gate command on the feature commit (then `0116c78c`, on a
  since-discarded local merge of main at `69646f18`) gave sentry 5232 passed, tests 6662 passed
  (6652 and 10), layers 15, licenses 63, with no failure.

T1 paths (58: slice 1's 57 and `tests/unit/reasons`, which came in from main):

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

## Forward merges

Each total in this record belongs to the head named beside it. The latest section below
supersedes the sections above it for the final head.

### 2026-11-12, first: `e27b9485`

`origin/feat/gen-rel-e1` `68034ff0` (holding `origin/main` `353d98bf`) merged as `e27b9485`.
The "Mutations" and "Gates" sections above describe that head: 60 of 60 mutations red; sentry
5248 passed, tests 6679 passed, layers 15, licenses 63, no failure. Pushed as `a63b6577`.

### 2026-11-12, second: `6c1a8691` (final source head)

`origin/feat/gen-rel-e1` `9b1b78bf` (holding `origin/main` `c0961a17`, which brought GEN-TYPES
E3 `lookup` columns) merged as `6c1a8691`. Main is not merged into this branch directly.

Conflicts, all resolved as unions:

- `docs/reasons.md`: every paragraph kept (this slice's beside GEN-TYPES E3's).
- `reasons/plan/_part3.py`: the `lookup` codes and `pinned_relational_option_unsupported` are
  both kept; the relational code sits in the one `PinnedModeError` section, after
  `pinned_lookup_unsupported`.
- `generation/synthesize.py`: both import sets kept. The union was 605 lines, over the 600
  goal, and no census entry may be added. The reference-parent rule of `generation_order` (the
  loop that collects each table's parents, skips the self-edge and raises the typed error for
  a parent that is not a generate table) moved, unchanged, to
  `generation/relational/_self_reference.py` as `reference_parents`; `generation_order` calls
  it. `synthesize.py` is 595 lines, `_self_reference.py` 107. The self-reference dispatch, the
  `lookup` dispatch and slice 1's `reference_draw_extras` call are otherwise as each side
  wrote them.

Auto-merged files checked for interaction: `_pinned_compile.py` refuses a `lookup` column by
type before the relational shape test, and a `lookup` column is not a `reference`, so the
shape predicate, the probe clamp and the streaming decline are unaffected;
`test_generate_option_reads.py` lists both `plan/checks/_lookup_tables.py` and
`generation/relational/_self_reference.py`.

Mutations on `6c1a8691` (Python 3.11.18): 62 of 62 red. They are the 60 above, with M2c and
M2d applied to the moved `reference_parents`, plus two for the move. 61 ran in a detached
worktree importing its own `src`, removed afterwards. H1 (the helper drops the typed error for
a non-generate parent) is red there in
`test_generation_order_raises_for_a_reference_to_a_non_generate_table` and
`test_every_entry_raises_the_same_error_for_a_forged_plan`. H2 (the helper loses
first-reference order) is not visible in the worktree: its only witness,
`test_order_and_result_keys_are_equal_across_seeds_and_routes`, runs a child process that
imports the installed checkout, not the worktree's `src`, so the worktree run stayed green. H2
was therefore applied once in the checkout itself, shown red in that test
(`['p1', 'p2'] == ['p2', 'p1']`), and reverted; the tree was clean before and after.

Gates on `6c1a8691` (the same command and T1 paths, Python 3.10.20, a rebuilt temporary venv):

| Step | Branch (`6c1a8691`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5286 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 6769 passed, 0 failed, 60 skipped (6759 in the parallel pass, 10 in the serial pass) |

Failure sets: branch 0, main 0, branch-only 0; `origin/main` was not rerun. No digest, golden
or snapshot moved.

Checks on `6c1a8691`, by exit code, with `origin/main` at `c0961a17`:
`registry_renumber.py --check --main origin/main` exit 0 (`registry consistent`);
`ratchet_check.py --base origin/main` exit 0; `check_fragment.py --base origin/main` exit 0;
`reason_release_pin.py --check` exit 0 (`reason release pin current`);
`gen_capability_matrix.py` no change.

Not run on this head: the full suite, the gate steps under Python 3.11, a rerun of
`origin/main`.

## Review round 1 (2026-11-13)

Review of `ff0ab08c`: two fix items, both built. Commits: `1f9aad0b` item 1 red cells,
`20faa7ba` the item 1 fix, `e2b5ff9d` this record's first round 1 version (item 2 stopped),
`3a5cfdfd` item 2 red cells and guard, `35ec8c9f` the item 2 refactor (final source head),
`5b7e02a5` the guard listed in the sentry manifest (tests only), then this record.

### Item 1 (V1): a self-reference key that declares `output_type`

The tree copies the key's drawn values before the Arrow-boundary cast, and a reference cannot
declare `output_type`, so a cast key matched none of its foreign keys. Chosen fix: refuse.
`config/_generate_relational.py:_key_is_valid` now also requires that the key declares no
`output_type`; the refusal is the existing `config.generate_self_reference_key_invalid` at
`reference_column` (no new code). The module docstring, `docs/strategies.md` (the rule list and
the refusal table row) and `changelog.d/gen-rel-e2.md` say so.

Red first: `1f9aad0b` adds two cells to
`test_at9_a_key_that_is_not_a_non_null_sequence_is_refused` (`decimal-output-type`, the review's
example, and `int-output-type`) and `test_at9_output_type_is_refused_on_the_key_only` (another
column of the table may still declare `output_type`). Run on `ff0ab08c`'s source (Python
3.10.20): both new parametrized cells failed, `Failed: DID NOT RAISE ValidationError`; the
accept cell passed, as it should on both heads.

### Item 2 (DESIGN): one exact-sequence-key predicate

The rule "a `sequence` key with no `null_probability`, so every row is a distinct key" had
three copies: `config/_generate_relational.py:_key_is_valid`,
`execution/_probe_scale.py:_loosen_per_parent_bounds` (`exact`) and
`plan/checks/_generate_columns.py:_check_reference_weights` (`keeps_all_keys`). They differed
on rates that are not plain numbers, so the build stopped and reported before unifying.

Orchestrator ruling (2026-11-13): fail closed. One public predicate,
`is_exact_sequence_key(column: Mapping[str, Any]) -> bool` in `config/_generate_relational.py`,
exported from `decoy_engine.config`. True only for `type == "sequence"` with a
`null_probability` that is absent, `None`, or a `numbers.Real` that is not a bool and is not
above 0. Anything else reads as "may have nulls", the safe side for every caller: a
self-reference key is refused, the probe loosens to the widest bounds, the weights-length check
is not applied. It never raises. NaN: the three copies all read it as no nulls; a strict `<= 0`
would flip that, so, as the ruling directs, the agreed result is kept (`not rate > 0`).
`_key_is_valid` keeps item 1's `output_type` refusal beside the predicate; the probe and plan
callers do not need it. Each caller keeps its own way of finding the key column (the plan check
still iterates `parent.get("generate_columns")`; the table-kind sentry passes).

Old against new, by `null_probability` on a `sequence` column (`exact` = every row a distinct
key):

| `null_probability` | config copy | probe and plan copies | predicate |
| --- | --- | --- | --- |
| absent | exact | exact | exact |
| `None` | exact | exact | exact |
| `0` | exact | exact | exact |
| `0.0` | exact | exact | exact |
| `-0.5` | exact | exact | exact |
| `nan` | exact | exact | exact |
| `0.5` | not | not | not |
| `1` | not | not | not |
| `inf` | not | not | not |
| `True` | exact | not | not |
| `False` | exact | exact | not |
| `Decimal('0')` | exact | exact | not |
| `Decimal('0.5')` | exact | not | not |
| `Fraction(0, 1)` | exact | exact | exact |
| `Fraction(1, 2)` | exact | not | not |
| `np.float64(0.0)` | exact | exact | exact |
| `np.float64(0.5)` | exact | not | not |
| `np.int64(0)` | exact | exact | exact |
| `np.int64(1)` | exact | not | not |
| `np.False_` | exact | exact | not |
| `'0'` | exact | TypeError | not |
| `'0.5'` | exact | TypeError | not |
| `''` | exact | exact | not |
| `[]` | exact | exact | not |
| `[1]` | exact | TypeError | not |
| `{}` | exact | exact | not |

Only rates `PipelineConfig` refuses (`generate_option_type` or `generate_option_range`) change,
so a validated config is unaffected: the changes reach only raw-dict paths (`compile_plan` on an
unvalidated dict, the probe). Beyond the values where the copies already disagreed, failing
closed also changes values on which all three agreed as exact: `False`, `Decimal("0")`,
`numpy.False_`, `""`, `[]` and `{}` are now not exact. No existing test changed its expectation
and no output byte moved.

Callers: `_key_is_valid` (`is_exact_sequence_key(earlier) and "output_type" not in earlier`),
`_loosen_per_parent_bounds` (passes `is_exact_sequence_key(key)` to `_loosened_bounds`),
`_check_reference_weights` (`if is_exact_sequence_key(candidate) and len(weights) != rows`).
The seam sentry's comment on the `_probe_scale.py` entry names the predicate; the file still
imports nothing from `execution.physical`.

Guard: `tests/sentry/defects/test_sequence_key_predicate.py` (listed in the sentry manifest).
Scope: a function outside the predicate itself that reads the `null_probability` key and has a
`== "sequence"` comparison as an operand of an `and`. A dispatcher's own `if kind ==
"sequence"` or a `!= "sequence"` test is not flagged (`_column_streams.make_column_stream` and
`_window_admission._class_b_reason` read both and are correctly left alone); a restatement
spread across helpers or written without `and` is not seen. It also requires each of the three
callers to call the predicate. Meta-tests seed the copies' shapes (one expression, the split
read, a typed subscript, a method) and the accepted shapes.

Red first: `3a5cfdfd`, run on source `20faa7ba` (Python 3.10.20): 32 failed. 30 predicate cells
(`test_a_sequence_with_no_positive_real_rate_is_an_exact_key` x9,
`test_any_other_rate_may_hold_nulls_so_the_key_is_not_exact` x17,
`test_only_a_sequence_is_an_exact_key` x4) with `AttributeError: module 'decoy_engine.config'
has no attribute 'is_exact_sequence_key'`; `test_no_module_restates_the_exact_sequence_key_rule`
found exactly the three copies; `test_the_owner_defines_the_rule_and_the_listed_callers_use_it`
found no predicate. The meta-tests passed, as they test the scanner only.

### Mutations (final source head `35ec8c9f`, Python 3.10.20)

76 of 76 red: the 62 recorded on `6c1a8691`, item 1's K1, and 13 new ones.

- Re-anchored to the moved code, same intent: C1c (the null test dropped: `_key_is_valid`
  tests the type only), C1d (the type test dropped: `_key_is_valid` tests nulls only), C1e (a
  zero rate read as nulls: the predicate's last line returns `False`), K1 (the `output_type`
  refusal dropped). Every anchor matched exactly once.
- S1 a bool read as a number; S2 NaN read as nulls (`rate <= 0`); S3 the type test dropped;
  S4 a non-real rate compared (fails open or raises); S5 an absent rate read as nulls: each red
  in the predicate cells.
- Q1, Q2, Q3: each caller restates the rule inline: red in the guard. Q4 the probe treats every
  key as exact: red in `test_a_parent_with_unknown_distinct_keys_gets_the_widest_bounds`. Q5 the
  weights check treats every key as exact: red in
  `test_weights_length_not_checked_when_the_parent_can_drop_keys_or_is_not_a_sequence`. G3 the
  weights check stops calling the predicate: red in the guard's caller inventory.
- G1 the guard looks for `or` instead of `and`: red in `test_seeded_restatements_are_seen`. G2
  the guard exempts the whole owner file: red in `test_the_owner_function_alone_is_exempt`.

75 ran in a detached worktree of `35ec8c9f` importing its own `src` (removed afterwards); the
results JSON shows 75 `RED` and H2 `NOT RED` there, as before. H2 (its witness runs a child
process that imports the checkout) was applied once in the checkout, red in
`test_order_and_result_keys_are_equal_across_seeds_and_routes`, and reverted; the tree was clean
before and after. `5b7e02a5` changes only the sentry manifest, so the mutations hold for it.

### Gates

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests` with the slice's T1 paths plus
`tests/sentry/defects/test_sequence_key_predicate.py`, `tests/unit/execution/test_probe_scale.py`,
`tests/unit/execution/test_probe_scale_kills.py` and `tests/unit/plan/checks`. Python 3.10.20,
PyArrow 24.0.0, DuckDB 1.5.4, pandas 2.3.3, NumPy 2.2.6.

| Step | `20faa7ba` (item 1) | `35ec8c9f` | `5b7e02a5` (final) |
| --- | --- | --- | --- |
| ruff | pass | pass | pass |
| format | pass | pass | pass |
| mypy | pass | pass | pass |
| sentry | 5286 passed, 0 failed, 2 skipped | 5296 passed, 1 failed, 2 skipped | 5297 passed, 0 failed, 2 skipped |
| layers | 15 passed | 15 passed | 15 passed |
| licenses | 63 passed | 63 passed | 63 passed |
| tests | 6772 passed, 0 failed, 60 skipped | 6813 passed, 0 failed, 60 skipped | 6813 passed, 0 failed, 60 skipped |

The one failure on `35ec8c9f` was `test_sentry_manifest.py::test_manifest_lists_every_sentry_file_and_no_ghost`
(the new guard was not listed; `origin/main` passed it in the baseline rerun); `5b7e02a5` lists
it. Final head: failure sets branch 0, branch-only 0; the baseline was not rerun because every
branch step passed. No digest, golden or snapshot moved.

Checks on `5b7e02a5`, by exit code, `origin/main` at `c86ca58d`:
`registry_renumber.py --check --main c0961a17` exit 0 (`registry consistent`);
`--main origin/main` exit 2 (`main-not-merged`); `ratchet_check.py --base c0961a17` exit 0;
`--base origin/main` exit 1 (`exception-context`, `f29-producer-inventory`, `gcs-call-kwargs`,
`private-file-sinks`, `secret-material-census`: keys main gained); `check_fragment.py --base
origin/main` exit 0; `reason_release_pin.py --check` exit 0. The two `origin/main` failures are
the same on `ff0ab08c` (main moved; main is not merged here). The same six results held on
`20faa7ba`.

Line counts: `config/_generate_relational.py` 207, `execution/_probe_scale.py` 272,
`plan/checks/_generate_columns.py` 517, `config/__init__.py` 178,
`tests/unit/config/test_generate_relational.py` 519,
`tests/sentry/defects/test_sequence_key_predicate.py` 165 (new).

### V2-LOG (accepted from review)

- a. An all-root table (`root_count == row_count`, including one row with the default) gives a
  `null`-typed column, against the key-type sentence in `docs/strategies.md`.
- b. `weights` without `distribution: weighted` on a self-reference is accepted and ignored.
- c. `self_reference` has no run-time twin of the validate rules: an unvalidated dict drops
  options silently, a missing key gives a bare `KeyError`, a nullable key makes roots.
- d. `generation/synthesize.py` imports the private `relational._self_reference`;
  `reference_parents` is general ordering logic in the tree module; `CODEMAP.md` describes
  only per-parent bounds.
- e. The bounded-memory reservation in `generation/_streamed.py` does not count the tree's
  O(n) `open_rows` and `children` lists.
- f. Streaming admission declines a capped self-reference as `reference_cardinality_repair` and
  an uncapped one as `reference_to_own_table`; check `is_self_reference` first.
- g. `tests/unit/generation/test_pinned_generation.py` grew from 825 to 853 lines; move the
  relational-policy cells under `tests/unit/generation/relational/`.

### Not run

The full suite; the gate steps under Python 3.11; a baseline rerun on `origin/main` for the
final head (it ran for `35ec8c9f`'s failing sentry step only).

## Forward merge of review fixes (2026-11-13)

Slice 1's review fixes are merged forward. Main is not merged into this branch directly.

| Commit | Subject |
| --- | --- |
| `00770342` | `Merge feat/gen-rel-e1 into feat/gen-rel-e2` (parents `5d7462c4`, `872f097d`) |
| `92cce31d` | `fix(native): catalogue the self-reference tree draw` (final source head) |

### The merge

`origin/feat/gen-rel-e1` at `872f097d` holds `origin/main` `c86ca58d` (0.23.0 released), slice
1's review rounds 1 and 2 (`per_parent_bounds`, its exclusivity guard over `generation/`,
`generators/`, `execution/` and `config/`, and the `gen.reference` catalogue entry for the
repair). The venv was resynced (`uv sync --frozen --python 3.10 --extra dev --extra lint
--extra vault --extra cloud`; `decoy-engine` 0.22.0 to 0.23.0) and `decoy_engine.__file__` is
`/home/user/wt-e2/src/decoy_engine/__init__.py`.

Conflicts, each resolved as a union:

- `config/__init__.py` and `config/_generate_relational.py` (`__all__`): both sides' names,
  `is_exact_sequence_key`, `is_self_reference`, `per_parent_bounds`, `per_parent_problems`,
  `self_reference_problems`.
- `config/_pipeline.py`, `execution/_probe_scale.py`, `generation/_admission.py`: the import
  lines carry both sides' names. The bodies merged without a conflict: the probe's
  `_loosen_per_parent_bounds` reads the bounds through `per_parent_bounds` (slice 1) and the
  key through `is_exact_sequence_key` (this slice), and `_clamp_root_counts` is unchanged.
- `tests/sentry/test_physical_seam_disconnection.py`: the `_probe_scale.py` comment names
  `config.generate_columns_of`, `config.per_parent_bounds`, `config.is_self_reference` and
  `config.is_exact_sequence_key`.
- `tests/unit/config/test_generate_relational.py`: both appended blocks kept (this slice's
  AT-7, AT-9 and predicate cells, then slice 1's `per_parent_bounds` cells).

A non-conflicting break the merge exposed: main's release stamped every `UNRELEASED` code in
`reasons/plan/_part3.py`, so slice 1 dropped that file's `UNRELEASED` import, while this
slice's `pinned_relational_option_unsupported` in the same file is still `since=UNRELEASED`.
The import is kept (the union); without it the package does not import.

Auto-merged ledger files read against both parents: `docs/reasons.md`, `CODEMAP.md`,
`changelog.d/` (`gen-rel-e1.md` and `gen-rel-e2.md` both present),
`tests/sentry/test_sentry_manifest.py` (both `test_generate_option_reads.py` and
`test_sequence_key_predicate.py` listed), `tests/sentry/defects/test_generate_option_reads.py`
and `plan/checks/_generate_columns.py`.

Adaptations required by slice 1's bounds guard (a direct `min_per_parent`/`max_per_parent`
read in a scanned package fails it):

- `config/_generate_relational.py` `_tree_problem`: `_positive(col.get("min_per_parent"))`
  becomes `per_parent_bounds(col)[0]`. The same reading (`_positive` is what the helper
  applies), so every input refuses as before.
- `generation/relational/_self_reference.py` `self_reference`: `int(col.get("max_per_parent")
  or 0)` becomes `_, cap = per_parent_bounds(col)`. Every value a validated config can carry
  (absent, None, 0, a positive int) reads the same. On values the registry refuses, which only
  an unvalidated raw dict can carry, the helper's reading applies: `True`, `2.5` or `"3"` no
  longer become a cap (the tree is drawn uncapped), and a negative cap was already uncapped.
  This is the reading slice 1 chose for every other draw.

This slice's sequence-key guard stays green over slice 1's code (`test_sequence_key_predicate.py`
22 of 22 in the gate run). `generation/synthesize.py` is 595 lines, no census entry (slice 1
599, this slice 595 before the merge; slice 1 has not changed the file since `9b1b78bf`).

### The tree draw in the catalogue (owner ruling: option A)

Red on the merge head `00770342` (Python 3.10.20): `tests/native/test_draw_site_inventory_coverage.py`
and `tests/native/test_draw_sites_catalogue_pinned.py` gave 1 failed, 16 passed;
`test_static_scan_finds_no_uncatalogued_draw_site` reported
`['generation/relational/_self_reference.py']`.

Decision: the existing `gen.reference` site, not a new id. A self-reference column is kind
`reference`, which `GEN_KIND_TO_SITE` maps to `gen.reference`; its `random.Random` is seeded by
`GenDeriveContext.for_column(derive_key=..., column_config=col, fallback_seed=seed).base_int("py")`,
the derivation the entry records; it is one Random advanced across every row, so it is not
partitionable, and its identity is the column. It replaces the per-row choice for that column,
as the fitted fan-out allocator (already a `gen.reference` mirror) does. A new id would claim a
second stream that does not exist and would need its own provider, which nothing calls.

`gen.reference` in `execution/native/_draw_sites_gen.py`: `generation/relational/_self_reference.py:86`
(the `random.Random` construction) is appended to `mirror_call_sites`, and the notes now say
that a self-reference draws its tree from the column's own Random in place of the per-row
choice (`rng.randrange(row)` per non-root row, or `rng.randrange(len(open_rows))` under
`max_per_parent`), that root rows hold null and draw nothing, and that the pool is the table's
own key column. No other field, entry, order or map changed. No allowlist entry; the seam
sentry's whole-file `permitted_non_physical` entry for `_draw_sites_gen.py` is unchanged.

`tests/native/test_draw_sites_catalogue_pinned.py` re-pinned by its procedure: a
`Re-pinned for GEN-REL E2: ...` line above `_CATALOGUE_DIGEST` and a per-entry comment above
the `gen.reference` digest. On top of slice 1's pin: catalogue `49e1c138...` to `cf4f3f18...`
(`cf4f3f18a346d0ad4c6af2baddac955be80f1aa3501c3e80a627680a291ea240`), `gen.reference`
`a53519d7...` to `43afe9de...` (`43afe9de3ce00612a433759bb040bccdd178207cd330437f34ea595f9c3c5009`).
The 40 ids, their order, the other 39 entry digests and every map and enum digest are unchanged.
The catalogue is metadata: no draw, seed or output byte moved, and the determinism and
draw-site provider goldens pass unchanged.

### Mutations (final source head `92cce31d`, 79 of 79 red)

All 76 recorded mutations and three new ones. 78 ran in a detached worktree at `/home/user/`
importing its own `src` (Python 3.10.20, removed afterwards); the results JSON shows 78 `RED`
and H2 `NOT RED` there, as before (its witness runs a child process that imports the
checkout). H2 was applied once in `/home/user/wt-e2` at `92cce31d`, clean before and after: red
in `test_order_and_result_keys_are_equal_across_seeds_and_routes`.

- Re-anchored, same intent: C2b and C2f now anchor on `if per_parent_bounds(col)[0]:` (the
  adapted `min_per_parent` refusal). Every anchor matched exactly once.
- Red counts equal the run on `35ec8c9f` except A6e (`root_count` dropped from the registry):
  6 red, one more than before, because slice 1 added `config/_generate_relational.py` to the
  option-read sentry's `READER_FILES`, which now also sees that file's `root_count` read.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| F1 | drop the tree draw's mirror from the catalogue | `execution/native/_draw_sites_gen.py` | 1: `test_static_scan_finds_no_uncatalogued_draw_site` |
| F2 | the tree draw reads `max_per_parent` itself | `generation/relational/_self_reference.py` | 1: `test_per_parent_bounds_are_read_only_through_the_config_helper` |
| F3 | the self-reference config check reads `min_per_parent` itself | `config/_generate_relational.py` | 1: `test_per_parent_bounds_are_read_only_through_the_config_helper` |

### Gates (final source head `92cce31d`)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests` over the slice's T1 paths, `tests/unit/reasons`,
`tests/native/test_draw_site_inventory_coverage.py`, `tests/native/test_draw_sites_catalogue_pinned.py`,
`tests/unit/execution/test_probe_scale.py`, `tests/unit/execution/test_probe_scale_kills.py`,
`tests/unit/plan/checks`, the test files slice 1's fixes changed
(`tests/sentry/defects/test_generate_option_reads.py`, `tests/sentry/test_physical_seam_disconnection.py`;
`tests/unit/config` is a T1 path), `tests/sentry/defects/test_sequence_key_predicate.py` and the
other native files that read the catalogue (`test_draw_sites_assembly.py`,
`test_determinism_protocol.py`, `test_c1b_i_metadata_inventory.py`,
`test_c5b_i_metadata_inventory.py`, `test_capabilities.py`). Python 3.10.20, PyArrow 24.0.0,
DuckDB 1.5.4, pandas 2.3.3, NumPy 2.2.6; `origin/main` at `c86ca58d`.

| Step | Branch (`92cce31d`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5445 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 7089 passed, 0 failed, 60 skipped |

Failure sets: branch 0, baseline 0, branch-only 0; no step failed, so `origin/main` was not
rerun. `test_static_scan_finds_no_uncatalogued_draw_site` passed (the inventory file 12 of 12)
and `test_draw_sites_catalogue_pinned.py` passed 5 of 5. No digest, golden or snapshot of
generated output moved; the only re-pin is the catalogue metadata above.

### Script checks

On `92cce31d` (clean tree), `origin/main` at `c86ca58d`, which slice 1 holds:
`registry_renumber.py --check --main origin/main` exit 0 (`registry consistent`),
`ratchet_check.py --base origin/main` exit 0, `check_fragment.py --base origin/main` exit 0,
`reason_release_pin.py --check` exit 0 (`reason release pin current`). The changelog fragment
is unchanged: nothing a user sees changes for a validated config.

### Not run

The full suite; the gate steps under Python 3.11; a rerun of `origin/main` (no branch step
failed).

## Not run

- The full suite. `tests/unit/execution` beyond the probe and R3 files, the rest of
  `tests/parity`, `tests/acceptance` beyond the gate pins, `tests/integration` beyond `golden`,
  `tests/native` beyond the gate pins and `testflight` were not run.
- The gate steps under Python 3.11 (the mutations and the red-first run used 3.11, the gates
  3.10). A rerun of `origin/main` (no branch step failed). The gates against a branch that
  holds `origin/main` `89bee526`.

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
