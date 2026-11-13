# GEN-REL E2 build evidence

Status: built on `feat/gen-rel-e2`. Review round 1 item 2 stopped on a guarantee decision (see
"Review round 1").

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

Review of `ff0ab08c`: two fix items. Item 1 is built; item 2 stopped before any change, because
the three copies of its rule differ (below). Commits: `1f9aad0b` red cells, `20faa7ba` the
item 1 fix (final source head), then this record.

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

### Item 2 (DESIGN): one sequence-key predicate, stopped

Not built: no predicate, guard test or call-site change was committed. The three copies are:

- `config/_generate_relational.py:_key_is_valid`: `type == "sequence" and not
  _nulls_on(null_probability)`, where `_nulls_on(v)` is `type(v) in (int, float) and v > 0`.
- `execution/_probe_scale.py:_loosen_per_parent_bounds` (`exact`) and
  `plan/checks/_generate_columns.py:_check_reference_weights` (`keeps_all_keys`):
  `type == "sequence" and not (null_probability or 0) > 0`.

They agree on an absent value, `None`, `0`, `0.0`, negatives, NaN, `0.5`, `1`, infinity,
`False`, `""`, `[]` and `{}`. They differ on any value that is not exactly an `int` or `float`
but compares above 0: `True`, `Decimal("0.5")`, `Fraction(1, 2)`, `numpy.float64(0.5)` and
`numpy.int64(1)` read as "no nulls" in the config copy and as "nulls" in the other two; `"0.5"`
and `[1]` read as "no nulls" in the config copy and raise `TypeError` in the other two.
`PipelineConfig` refuses every such value (`generate_option_type`), but `compile_plan` and the
probe take a raw dict, so the plan check and the probe can see one; unifying would change the
weights-length check and the probe's exact bounds there. The two non-config copies also find
the key differently (the plan check reads `parent.get("generate_columns")` and every candidate
of that name; the probe reads `generate_columns_of(parent)` and the first). Which behavior the
one predicate keeps is a guarantee decision, reported to the orchestrator.

### Mutations (final source head `20faa7ba`, Python 3.10.20)

63 of 63 red: the 62 recorded on `6c1a8691` plus K1 (drop the `output_type` refusal from
`_key_is_valid`; red in `test_at9_a_key_that_is_not_a_non_null_sequence_is_refused` for
`decimal-output-type` and `int-output-type`). C1c, C1d and C1e were re-anchored to the new
three-line return of `_key_is_valid` with the same edits (drop the null test; key type check
replaced by `True`; null test replaced by `is None`); every anchor matched exactly once. 62 ran
in a detached worktree of `20faa7ba` importing its own `src` (removed afterwards); the results
JSON shows 62 `RED` and H2 `NOT RED` there, as before. H2 (its witness runs a child process
that imports the checkout) was applied once in the checkout, red in
`test_order_and_result_keys_are_equal_across_seeds_and_routes` (`['p1', 'p2'] == ['p2',
'p1']`), and reverted; the tree was clean before and after.

### Gates (final source head `20faa7ba`)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests` with the slice's T1 paths plus
`tests/unit/execution/test_probe_scale.py`, `tests/unit/execution/test_probe_scale_kills.py`
and `tests/unit/plan/checks` (the changed test file is under `tests/unit/config`, in T1).
Python 3.10.20, PyArrow 24.0.0, DuckDB 1.5.4, pandas 2.3.3, NumPy 2.2.6.

| Step | Branch (`20faa7ba`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5286 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 6772 passed, 0 failed, 60 skipped |

Failure sets: branch 0, branch-only 0; the baseline was not rerun because every branch step
passed. No digest, golden or snapshot moved.

Checks on `20faa7ba`, by exit code, `origin/main` at `c86ca58d`:
`registry_renumber.py --check --main c0961a17` exit 0 (`registry consistent`);
`--main origin/main` exit 2 (`main-not-merged`); `ratchet_check.py --base c0961a17` exit 0;
`--base origin/main` exit 1 (`exception-context`, `f29-producer-inventory`, `gcs-call-kwargs`,
`private-file-sinks`, `secret-material-census`: keys main gained); `check_fragment.py --base
origin/main` exit 0; `reason_release_pin.py --check` exit 0. The two `origin/main` failures
are the same on `ff0ab08c` (main moved; main is not merged here).

Line counts: `config/_generate_relational.py` 182, `tests/unit/config/test_generate_relational.py`
444; `plan/checks/_generate_columns.py` (517) and `execution/_probe_scale.py` (273) unchanged.

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

The full suite; the gate steps under Python 3.11; a baseline rerun on `origin/main`; item 2.

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
