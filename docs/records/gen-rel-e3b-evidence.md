# GEN-REL E3b build evidence

Status: built on `feat/gen-rel-e3b`. The owner question of "Plan notes and deviations", item 1
(per-parent aggregation of a text-typed numeric child column), is closed by the owner ruling of
2026-11-13 (option (c)); see "Owner ruling (2026-11-13)" at the end. No census number,
threshold, pin, baseline or allowlist rose.

Spec: `docs/plans/gen-rel.md` on `origin/plan/gen-rel` (`26a83542`), slice row 4 (per-parent
`derived_aggregate` with `from_table` and `via`, every op, childless identity). The plan stays
on the plan branch.

## Heads and environment

- Slice start: `feat/gen-rel-e3a` at `e91b848c` (slice 3; it holds slices 1 and 2 and
  `origin/main` `c0961a17`). Main is not merged into this branch.
- Commits: `0a128971` red acceptance tests (source as at `e91b848c`), `0424513e` the feature,
  `9ed7e17f` one added test cell (no source change). Final source head: `0424513e`;
  `9ed7e17f` changes one test file only. Every mutation and gate total below belongs to the
  head named beside it. This record is added in a docs-only commit after `9ed7e17f`.
- Gates: Python 3.10.20 (a temporary venv `/home/user/venv-e3b-310`, synced with
  `uv sync --frozen --python 3.10 --extra dev --extra lint --extra vault --extra cloud` as CI
  does; the existing `.venv` was not changed), PyArrow 24.0.0, DuckDB 1.5.4, pandas 2.3.3,
  NumPy 2.2.6. The `layers` step ran in `.venv-layers`.
- Red-first run, sweeps and mutations: Python 3.11.18 (the `.venv`), PyArrow 24.0.0, DuckDB
  1.5.4, pandas 2.3.3.

## Red first (commit `0a128971`, source unchanged from `e91b848c`)

`uv run --no-sync pytest -q -p no:cacheprovider -p no:xdist --continue-on-collection-errors
<the 9 files below> -rA --tb=line`: 85 failed, 584 passed, no collection error.

| File | Red |
| --- | --- |
| `tests/unit/plan/checks/test_generate_columns.py` | 7 |
| `tests/unit/config/test_generate_options.py` | 3 |
| `tests/unit/generation/test_numeric_typing.py` | 2 |
| `tests/parity/test_numeric_typing_readers.py` | 2 |
| `tests/sentry/test_numeric_reader_registry.py` | 1 |
| `tests/sentry/defects/test_generate_input_edges.py` | 2 |
| `tests/unit/generation/relational/test_aggregate.py` (new) | 62 |
| `tests/unit/transforms/test_derived_aggregate.py` | 1 |
| `tests/unit/generation/relational/test_routes.py` | 5 |

The files are listed in the plan's E3b red-first order: the strict-schema cells (the child
column is accepted with no same-named parent column, a missing child column, a missing `via`
and an unknown `from_table` are refused); the unrelated same-named parent Arrow-type cells and
the descriptor cells; every op and the childless identity; then the read-before-fill readers,
placeholder return, reproducibility and the pinned cells. The new module is imported inside
the one cell that calls it, so every other cell of the new file fails on its own reason.

Reasons:

- Absent API: `generation/relational/_aggregate.py` (`with_aggregates`); the reason
  `config.generate_aggregate_read_before_fill`; the reader classification
  `derived_aggregate.via`; the two new resolver callers in the sentry's inventory and the two
  per-parent descriptors in its cross-table list.
- Wrong outcome: validate refuses `from_table` and `via` as unknown options (the per-parent
  configs, the reader cells, the null and lookup-key cells); the raw config that reaches the
  run is a scalar aggregate over the parent's own `amount`, so the totals are the parent column
  concatenated as text (`'100101...109'`), `mean` fails with `TypeError`, and the parent
  `amount` is typed `string`; the plan check refuses the child column with
  `derived_aggregate_missing_column_ref` (or accepts a parent column of that name); the
  resolver gives `derived_aggregate.column` a same-table inline edge and `via` none, so the
  reproducibility report misses the child column and foreign key; pinned compile and execute
  accept the column.

Green on the base by design (10 cells), each tied to a mutation in the table below:

| Cell | Why green on the base | Mutation |
| --- | --- | --- |
| `TestAggregatePlanCheckKeepsOnlyItsOwnRules::test_a_generate_column_keeps_its_op_rule` | the op rule is unchanged | K1 |
| `TestAggregatePlanCheckKeepsOnlyItsOwnRules::test_a_masked_column_keeps_its_missing_ref_refusal` | the masked rule is unchanged | K2 |
| `test_a_scalar_aggregate_still_depends_on_its_sibling` | the scalar edge is unchanged | R1 |
| `test_at10_the_hand_built_aggregate_plan_is_otherwise_executable` | non-vacuity precondition of the hand-built plan | A10d |
| `test_at12_no_value_is_nan_and_every_missing_value_is_a_null[sum]` and `[count]` | the scalar sum/count over the parent column has no null | G4 |
| `test_at12_the_returned_parent_table_holds_the_typed_filled_values[count-arrow_type1]` | the scalar count is a non-null int | A3 |
| `test_the_acceptance_cells_hold_on_the_registered_descriptors[windowed_date-anchor]` | E3a's anchor cells, now one parameter of the per-descriptor cell | N1 (and E3a's M1) |
| `test_the_fixture_has_childless_orders_null_keys_and_null_amounts` | fixture non-vacuity (childless orders, null keys, null amounts) | F1 |

No other new cell passed on the base.

Test changes after the red commit (none weakened):

- `test_at12_an_output_type_on_the_aggregate_is_applied_to_the_filled_values` used
  `output_type: float`, which is not an output type kind (the kinds are `int`, `decimal(n)`,
  `date`, `bool`; the run path does not validate it). It now uses `decimal(2)` and also
  validates the config.
- `test_the_aggregate_lookup_key_stays_numeric_on_both_sides` read the child foreign key from
  `numeric_plan`, which lists only candidate roots, never a `reference` column. It now checks
  the planned root and the generated Arrow types of both ends.
- The sentry's resolver-caller inventory names `_reads_aggregate`, the function in
  `config/_generate_relational.py` that calls the resolver, instead of its caller
  `aggregate_problems`.
- `9ed7e17f` adds `test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan` (five ops):
  the base fixture has no parent whose lines all hold a null amount, so no op produced a
  pandas NaN and the NaN mutant (A2) had no witness.

## Pre-existing tests whose expectation changed

| Test | Before | Now | Why |
| --- | --- | --- | --- |
| `tests/unit/plan/checks/test_generate_columns.py::test_reference_key_earlier_accepted_later_and_missing_refused[derived_aggregate.column]` | a missing input is `derived_aggregate_missing_column_ref` | `generate_column_forward_reference` | the plan moves target existence of a generate aggregate's input to the one strict check (with `from_table` it is a column of another table) |
| `tests/unit/config/test_generate_options.py::test_the_registered_inputs_are_exactly_these` | `derived_aggregate.column` is `column`, inline | `column_in_named_table` with context `from_table`, `post_fill`; `derived_aggregate.via` the same | the plan's E3b registrations |
| `tests/unit/config/test_generate_option_catalog.py::test_keys_checked_at_plan_compile_are_listed_as_required` | optional keys `("output_type",)` | `("from_table", "via", "output_type")` | two new optional keys |
| `tests/sentry/defects/test_generate_input_edges.py` | one cross-table descriptor, four anchor cells, four resolver callers | three cross-table descriptors with their own cells (the anchor cells unchanged), six resolver callers | the descriptor-mutation guard covers every cross-table descriptor |
| `tests/sentry/defects/test_generate_option_reads.py` | 20 reader files | 21: `config/_generate_relational.py`, which reads `from_table` and `via` | `test_every_registry_key_is_read_somewhere` needs a reader of each new key; the post-pass reads them through the resolver, not by literal key |

No masked-output test changed, and no expected output value was edited anywhere.

## Mutations (final source head `0424513e`, run on `9ed7e17f`, detached worktree, 42 of 42 red)

Each was applied once in a detached worktree of `9ed7e17f` (whose source is `0424513e`'s) that
imports its own `src` (`PYTHONPATH=<worktree>/src`, checked by printing
`decoy_engine.__file__`), its named tests run (Python 3.11.18, the `.venv`), and the file
restored; every anchor matched exactly once, and the worktree was clean at the end and is
removed. No witness test starts a child process. The statuses are read from the harness's
results file, one `RED` per mutation, not from a count of log lines. S1's first anchor (the
admission table row) did not model an admission (the column falls through to another decline);
it was replaced by a direct admit and rerun alone, red. No source changed after the run.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| N1 | numeric typing resolves a reader to (consumer table, option value) (sibling-only) | `generation/_numeric_typing.py` | 11: `test_a_cross_table_anchor_is_a_reader_of_the_parent_column_only`, `test_a_parent_anchor_does_not_make_a_same_named_child_column_text`, `test_a_per_parent_aggregate_is_a_reader_of_the_child_column_only` ... |
| A1 | a childless parent's mean is filled with 0 | `generation/relational/_aggregate.py` | 4: `test_at12_a_childless_parent_gets_the_identity_of_its_op[mean]`, `test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan[mean]`, `test_at12_each_op_matches_a_pandas_groupby_over_a_left_join[mean]` ... |
| A2 | a NaN from pandas is written as NaN, not null | `generation/relational/_aggregate.py` | 3: `test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan[max]`, `test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan[mean]`, `test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan[min]` |
| A3 | the placeholder table is returned | `generation/synthesize.py` | 23: `test_at12_a_childless_parent_gets_the_identity_of_its_op[count]`, `test_at12_a_childless_parent_gets_the_identity_of_its_op[sum]`, `test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan[count]` ... |
| E-derived.expression | omitted edge: the read-before-fill check skips derived.expression | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[derived.expression]` |
| E-formula.references | omitted edge: the read-before-fill check skips formula.references | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[formula.references]` |
| E-grouped_series.group_by | omitted edge: the read-before-fill check skips grouped_series.group_by | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[grouped_series.group_by]` |
| E-grouped_series.order_by | omitted edge: the read-before-fill check skips grouped_series.order_by | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[grouped_series.order_by]` |
| E-group_key.group_by | omitted edge: the read-before-fill check skips group_key.group_by | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[group_key.group_by]` |
| E-windowed_date.anchor | omitted edge: the read-before-fill check skips windowed_date.anchor | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[windowed_date.anchor]` |
| E-windowed_date.anchor (anchor_via) | omitted edge: the read-before-fill check skips windowed_date.anchor (anchor_via) | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[windowed_date.anchor (anchor_via)]` |
| E-statistical.condition_on | omitted edge: the read-before-fill check skips statistical.condition_on | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[statistical.condition_on]` |
| E-derived_aggregate.column (scalar) | omitted edge: the read-before-fill check skips derived_aggregate.column (scalar) | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[derived_aggregate.column (scalar)]` |
| E-derived_aggregate.column (per-parent) | omitted edge: the read-before-fill check skips derived_aggregate.column (per-parent) | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[derived_aggregate.column (per-parent)]` |
| E-reference.reference_column | omitted edge: a reference to a per-parent aggregate is not checked | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[reference.reference_column]` |
| P1 | class table: the post-pass runs before the child table exists | `generation/synthesize.py` | 33: `test_at12_a_childless_parent_gets_the_identity_of_its_op[count]`, `test_at12_a_childless_parent_gets_the_identity_of_its_op[max]`, `test_at12_a_childless_parent_gets_the_identity_of_its_op[mean]` ... |
| D1 | descriptor-as-sibling: derived_aggregate.column is a same-table column | `config/_generate_options.py` | 48: `test_a_per_parent_aggregate_is_a_reader_of_the_child_column_only`, `test_a_per_parent_aggregate_reads_its_child_table_after_it_is_filled`, `test_a_per_parent_aggregate_reads_the_child_column_and_not_the_parents` ... |
| D2 | descriptor-as-sibling: derived_aggregate.via is a same-table column | `config/_generate_options.py` | 8: `test_a_per_parent_aggregate_reads_its_child_table_after_it_is_filled`, `test_an_aggregate_through_a_foreign_key_that_is_not_reproducible_is_excluded`, `test_per_parent_column_is_accepted_with_no_same_named_parent_column` ... |
| A10a | AT-10: the relational shape is guarded at pinned compile only | `generation/_pinned_compile.py` | 1: `test_at10_pinned_execute_refuses_a_per_parent_aggregate_before_any_draw` |
| A10c | AT-10: the pinned predicate does not know the per-parent form | `generation/_pinned_compile.py` | 2: `test_at10_pinned_compile_refuses_a_per_parent_aggregate`, `test_at10_pinned_execute_refuses_a_per_parent_aggregate_before_any_draw` |
| A10d | AT-10: the pinned predicate refuses every derived_aggregate | `generation/_pinned_compile.py` | 1: `test_at10_the_hand_built_aggregate_plan_is_otherwise_executable` |
| A14 | AT-14: the read-before-fill code is not registered unreleased | `reasons/config/_part2.py` | 1: `test_at14_the_read_before_fill_code_is_registered_unreleased_in_the_config_catalog` |
| V1 | via may reference any table | `config/_generate_relational.py` | 1: `test_at12_via_must_reference_this_parent_table` |
| V2 | the parent key may be declared after the aggregate | `config/_generate_relational.py` | 1: `test_at12_the_parent_key_must_be_declared_before_the_aggregate` |
| V3 | the per-parent key does not go through the one parent-key predicate | `config/_generate_relational.py` | 4: `test_at12_a_parent_key_that_is_not_a_non_null_sequence_is_refused[not-a-sequence]`, `test_at12_a_parent_key_that_is_not_a_non_null_sequence_is_refused[nullable]`, `test_at12_a_parent_key_that_is_not_a_non_null_sequence_is_refused[repeated]` ... |
| V4 | null_probability on a per-parent aggregate is accepted | `config/_generate_relational.py` | 1: `test_at12_null_probability_on_a_per_parent_aggregate_is_refused` |
| V5 | a via through a self-reference is not refused as one | `config/_generate_relational.py` | 1: `test_at12_via_through_a_self_reference_is_refused` |
| V6 | via without from_table is accepted | `config/_generate_relational.py` | 1: `test_at12_via_needs_from_table_and_from_table_needs_via` |
| V7 | from_table without via is accepted | `config/_generate_relational.py` | 1: `test_at12_via_needs_from_table_and_from_table_needs_via` |
| V8 | the pipeline validator never calls the aggregate checks | `config/_pipeline.py` | 19: `test_at12_a_new_column_input_is_refused_before_fill_without_naming_it`, `test_at12_a_parent_key_that_is_not_a_non_null_sequence_is_refused[not-a-sequence]`, `test_at12_a_parent_key_that_is_not_a_non_null_sequence_is_refused[nullable]` ... |
| V9 | every derived_aggregate column is treated as per-parent | `config/_generate_relational.py` | 4: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[derived_aggregate.column (scalar)]`, `test_at12_the_same_reader_of_an_ordinary_column_validates[derived_aggregate.column (scalar)]`, `test_at1_near_neighbor_bytes_do_not_move[pinned-same_table_date_and_scalar_aggregate]` ... |
| G1 | the filled column is typed by inference, not by the whole-table builder | `generation/relational/_aggregate.py` | 1: `test_at12_an_output_type_on_the_aggregate_is_applied_to_the_filled_values` |
| G2 | the groups are not reindexed to the parent keys (row order of the groups) | `generation/relational/_aggregate.py` | 19: `test_at12_a_childless_parent_gets_the_identity_of_its_op[count]`, `test_at12_a_childless_parent_gets_the_identity_of_its_op[max]`, `test_at12_a_childless_parent_gets_the_identity_of_its_op[mean]` ... |
| G3 | each group is aggregated over the whole child column | `generation/relational/_aggregate.py` | 5: `test_at12_each_op_matches_a_pandas_groupby_over_a_left_join[count]`, `test_at12_each_op_matches_a_pandas_groupby_over_a_left_join[max]`, `test_at12_each_op_matches_a_pandas_groupby_over_a_left_join[mean]` ... |
| G4 | a childless sum or count is null (COALESCE dropped) | `generation/relational/_aggregate.py` | 6: `test_at12_a_childless_parent_gets_the_identity_of_its_op[count]`, `test_at12_a_childless_parent_gets_the_identity_of_its_op[sum]`, `test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan[count]` ... |
| G5 | the via reader is classified numeric-unsafe | `generation/_numeric_typing.py` | 3: `test_at12_the_unrelated_parent_amount_keeps_its_numeric_arrow_type`, `test_the_aggregate_lookup_key_stays_numeric_on_both_sides`, `test_the_per_parent_aggregate_inputs_are_classified` |
| K1 | the aggregate check drops its op rule for a generate column | `plan/_checks_derived_aggregate.py` | 1: `test_a_generate_column_keeps_its_op_rule` |
| K2 | the aggregate check drops the masked missing-ref rule | `plan/_checks_derived_aggregate.py` | 1: `test_a_masked_column_keeps_its_missing_ref_refusal` |
| K3 | the aggregate check refuses a generate column whose input is not a sibling | `plan/_checks_derived_aggregate.py` | 5: `test_a_generate_column_input_is_not_this_checks_rule`, `test_per_parent_column_is_accepted_with_no_same_named_parent_column`, `test_per_parent_column_may_be_declared_anywhere_in_the_child` ... |
| R1 | a companion-keyed input without its companion yields no edge | `config/_generate_relational.py` | 2: `test_a_per_parent_aggregate_reads_its_child_table_after_it_is_filled`, `test_a_scalar_aggregate_still_depends_on_its_sibling` |
| F1 | fixture: null_probability injects no nulls | `generation/synthesize.py` | 1: `test_the_fixture_has_childless_orders_null_keys_and_null_amounts` |
| S1 | streaming admits derived_aggregate | `generation/_admission.py` | 1: `test_at12_streaming_admission_declines_the_aggregate_and_the_run_stays_resident` |

The plan's E3b mutations: N1 restores sibling-only numeric-input resolution (`(consumer table,
option value)` instead of the resolved source); it turns the AT-12 Arrow-type cells red
(`test_aggregate.py::test_at12_the_unrelated_parent_amount_keeps_its_numeric_arrow_type`,
`test_numeric_typing_readers.py::test_at12_the_unrelated_parent_amount_keeps_its_numeric_arrow_type`, the
unit and sentry cells) and, on this integrated E3 head, the AT-11 cell
`test_routes.py::test_at11_the_unrelated_child_column_keeps_its_numeric_arrow_type`. A1 fills a
childless `mean` with 0, A3 returns the placeholder table, and the eleven `E-*` mutants each
remove one realistic resolved input edge from the read-before-fill check (each red on its own
reader cell). The per-AT mutants: D1 and D2 (descriptor-as-sibling, one per descriptor), A1
(childless-mean), A2 (NaN), A3 (placeholder-table), the `E-*` set (omitted-edge). The class
table's mutation (move the post-pass before the child) is P1. A10a/A10c/A10d are the pinned
cells (guard at compile only, predicate without the per-parent form, predicate refusing every
aggregate), S1 the streaming decline, A14 the registry cell. V1 to V9 cover the validation
matrix, G1 to G5 the post-pass and the `via` reader classification, K1 to K3 the old aggregate
plan check, and R1 and F1 the cells green on the base by design.

The sentry also runs the descriptor mutation in process on every run:
`test_each_cross_table_descriptor_turned_back_to_column_fails_every_cell` swaps each of the
three cross-table descriptors for a same-table `column` and requires every acceptance cell of
that descriptor to fail.

## What the slice does

- `derived_aggregate` takes `from_table` (a child table) and `via` (the child's `reference`
  column back to this table). `column` is then a column of the child. The registry declares
  `derived_aggregate.column` and `derived_aggregate.via` as `column_in_named_table` with
  context `from_table` and phase `post_fill`; without `from_table`, `column` keeps the
  same-table, earlier, inline rule. No resolver code changed: slice 3's resolver already
  implemented the descriptor.
- `generation/relational/_aggregate.py` (169 lines): `aggregate_values` gives the column loop
  null placeholders for a per-parent aggregate and otherwise calls the scalar generator it is
  handed; `with_aggregates(config, tables)` runs once after the last table is drawn. Per
  aggregate it reads the resolved `column` and `via` edges, takes the parent key from the
  `via` column's `reference_column`, runs one pandas `groupby` over the child's non-null
  foreign keys (each group through `apply_derived_aggregate`, the scalar form's op), reindexes
  to the parent keys in row order, writes 0 for a childless `sum` or `count` and null for a
  childless `mean`, `min` or `max`, turns a pandas NaN into null, and rebuilds the parent
  table's column through `arrow_table` (so `output_type` applies). It returns the input
  mapping itself when the config has no per-parent aggregate.
- `config/_generate_relational.py`: `is_per_parent_aggregate` (the one shape test, used by
  validation, the column loop, the post-pass and pinned mode) and `aggregate_problems`, called
  by the `PipelineConfig` validator beside `parent_lookup_problems`. It refuses: `via` without
  `from_table` (`generate_option_unknown`); `from_table` without `via` (`missing`);
  `null_probability` on the aggregate (`generate_per_parent_null_conflict`); a `via` through a
  self-reference (`generate_parent_lookup_self_reference`); a `via` that is not a `reference`
  to this table, or whose key is not an earlier non-null `sequence`
  (`generate_parent_lookup_key_not_unique`, through `parent_key_problem`); and any column of
  any table whose resolved input, or whose `reference_table`/`reference_column`, names a
  per-parent aggregate (`generate_aggregate_read_before_fill`).
- `plan/_checks_derived_aggregate.py` keeps the op rule for both kinds and the missing-column
  rule for masked columns only; the input of a generate aggregate is the strict check's.
- Numeric typing classifies `derived_aggregate.via` as numeric-safe (the foreign key is grouped
  and matched to the parent key by equality). The reproducibility report needed no code: the
  new edges reach it through the resolver.
- Pinned mode: `relational_option_unsupported` also returns True for a per-parent aggregate, so
  compile raises `plan.pinned_relational_option_unsupported` and execute the run-time twin.
- Streaming admission already declines every `derived_aggregate`; the whole-table route is the
  only one that runs it, and `_generate_tables_from_config` returns
  `with_aggregates(config, out)`.

### What moved out of `synthesize.py`

Nothing moved. `generation/synthesize.py` went from 598 to 600 lines: one import, the dispatch
line (`aggregate_values(generate_derived_aggregate_column, ...)`, which keeps the scalar
generator called by `synthesize`'s own name because the Phase F parity witnesses patch
`synthesize.generate_derived_aggregate_column`) and its one-line comment; `return out` became
`return with_aggregates(config, out)`.

## Plan notes and deviations

1. **Closed (owner ruling 2026-11-13, option (c); see the section at the end).** **Owner
   question: aggregating a text-typed numeric child column.** The plan keeps numeric
   typing's one reader policy, where `derived_aggregate.column` is numeric-unsafe ("aggregate
   input coercion is not verified for typed input"). A `sequence` or bounded `faker` int
   column read by an aggregate therefore stays text, exactly as for the scalar form. On this
   head a per-parent `sum` over such a child column concatenates text per parent
   (`'82'`, `'9463'`), and `mean`, or a `sum` beside a childless parent (text values and the
   integer 0 in one column), raises an uncoded `TypeError` / `ArrowTypeError`. The scalar form
   on the base already gives `'1234'` for `sum` and raises for `mean` over the same columns
   (pre-existing). Numeric child columns (`distribution`, `categorical` or `constant` numbers,
   `output_type` columns) aggregate correctly, and the docs say so. Fixing it needs a decision
   the plan does not make:
   - (a) classify `derived_aggregate.column` numeric-safe: the scalar form's input column and
     result change type, which moves the AT-1 near-neighbor digest
     `same_table_date_and_scalar_aggregate` (a byte change outside the named cells);
   - (b) classify the per-parent form only, which needs numeric typing's reader identity to
     tell the two forms apart (for example by the edge's phase), a change to the frozen
     "consumer kind and option" keying;
   - (c) keep this head and refuse, at validate or plan compile, a per-parent aggregate whose
     child column is not numeric, with a new code;
   - (d) keep this head as built and record the limitation (V2-LOG).
   Nothing here builds any of them.
2. **Lookup refusals sit at `via`.** The plan names the code for a `via` whose key is not
   unique and is silent on where it points; every lookup refusal points at `via`, as slice 3's
   point at `anchor_via`. `from_table` naming the parent itself is refused as a lookup through
   a self-reference (the `via` can only reference its own table then).
3. **`via` without `from_table` and `from_table` without `via`.** The plan does not name these.
   `via` alone is `generate_option_unknown` (the option means nothing without its table, like
   `root_count` on a reference to another table); `from_table` alone is `missing` at `via`. The
   registry cannot express "`via` is required with `from_table`" (`CONDITIONAL_REQUIRED` keys on
   fixed selector values), so the catalog lists both as optional.
4. **A `reference` to a per-parent aggregate is refused too.** The plan's list of readers is
   the resolver's inputs. A `reference` whose `reference_column` is a per-parent aggregate
   copies the placeholder while its table is drawn, so `aggregate_problems` refuses it with the
   same code, from the reference column's own keys (not a second option table).
5. **The `via` direction rule is a validate rule, not a plan-compile rule.** The plan says the
   older aggregate checker keeps "op, type and `via` direction rules" and also that validation
   refuses a wrong `via` with the config code. The config code is raised by the
   `PipelineConfig` validator; `plan/_checks_derived_aggregate.py` keeps the op rule (and the
   masked rule). A raw config that skips validation is V2-LOG, as for slices 1 to 3.
6. **The validator always runs.** Slice 3's `_parent_lookups_valid` returned early unless some
   column had `anchor_via`. It now builds its table list for every config and calls both
   checks; neither does anything for a config without `anchor_via`, `from_table`, `via` or a
   per-parent aggregate. The alternative was a set of option names in the validator.
7. **`_reproducibility.py`, `plan/checks/_generate_columns.py` and `_pinned_execute.py` are in
   the row.** The first two changed only in a docstring sentence (the new edges reach them
   through the resolver); `_pinned_execute.py` is unchanged because execute already re-runs
   the one predicate with `at_execute=True`.
8. Handoff against plan: the handoff's step 7 has the builder open the PR; the orchestrator
   opens it. The handoff's `uv sync --frozen --extra dev` is replaced by the CI-like Python
   3.10 venv. Neither changes a guarantee.

## Files outside the plan's slice row

- `src/decoy_engine/config/__init__.py`: exports `aggregate_problems` and
  `is_per_parent_aggregate` (generation and pinned mode use the public config API).
- `src/decoy_engine/config/_pipeline.py`: the validator call (the Placement table puts the
  relational validator call there).
- `CODEMAP.md`: the `config/` and Generation rows name the per-parent aggregate.
- `tests/unit/generation/relational/_support.py`: the per-parent fixture builders.
- `tests/unit/config/test_generate_option_catalog.py` and
  `tests/sentry/defects/test_generate_option_reads.py`: see "Pre-existing tests whose
  expectation changed".
- `docs/records/gen-rel-e3b-evidence.md` (this file).

`docs/capability-matrix.md` is unchanged: `scripts/gen_capability_matrix.py` writes the same
file (the slice adds no strategy). `config/` keeps 25 direct modules and `generation/` gains
no direct module (the new file is under `generation/relational/`).

`REGISTRY_VERSION`, the minor fixtures, `consumer-contract.json`, the release pin and the docs
version sentence are untouched, and the one new code is `since=UNRELEASED`.

## New reason codes

| Identity | Raised as | Phase |
| --- | --- | --- |
| `config.generate_aggregate_read_before_fill` | `PipelineConfig` validation error | configuration |

Reused: `config.generate_parent_lookup_key_not_unique`,
`config.generate_parent_lookup_self_reference`, `config.generate_per_parent_null_conflict`,
`config.generate_option_unknown`, `plan.generate_column_forward_reference`,
`plan.pinned_relational_option_unsupported`, `runtime.pinned_relational_option_unsupported`.

## Line counts

`generation/synthesize.py` 600 (598 at the slice start; goal 600), `config/_generate_options.py`
561 (554), `plan/checks/_generate_columns.py` 507 (506), `config/_pipeline.py` 498 (500),
`config/_generate_relational.py` 413 (318), `generation/_pinned_compile.py` 399 (394),
`generation/_numeric_typing.py` 352 (349), `generation/_reproducibility.py` 219 (218),
`generation/relational/_aggregate.py` 169 (new), `reasons/config/_part2.py` 149 (143),
`plan/_checks_derived_aggregate.py` 123 (128). Touched files at or above 550:
`generation/synthesize.py` 600 and `config/_generate_options.py` 561.

## Gates (head `9ed7e17f`; source is `0424513e`'s)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests <T1 paths>` on `9ed7e17f`, Python 3.10.20
(`DECOY_VENV=/home/user/venv-e3b-310`), with `origin/main` fetched at `051b84cb`.

| Step | Branch (`9ed7e17f`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5331 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 10410 passed, 0 failed, 113 skipped (10400 in the parallel pass, 10 in the serial pass) |

- Failure sets: branch 0, main 0, branch-only 0. Every branch step passed, so the gate script
  did not rerun `origin/main`. There is no branch failure.
- AT-1: no existing digest, golden or snapshot moved, and no expected output value was edited.
  The E1 near-neighbor digests (which include a same-table date and a scalar aggregate), the
  Phase F oracle and witnesses (which wrap `synthesize.generate_derived_aggregate_column`), the
  determinism goldens, the draw-site goldens and the old-input-bytes pins are in the T1 set and
  pass unchanged. A new AT-1 cell puts a per-parent aggregate beside the E1 family and checks
  the family's digest.
- Masked output: `transforms/derived_aggregate.py` is unchanged, and the mask-side
  `derived_aggregate` tests (`tests/unit/transforms/test_derived_aggregate.py`, including the
  real-run-path cells, `tests/physical/test_shadow_full_frame.py`,
  `tests/unit/execution/test_technique_class.py`, `test_created_target_contract.py`,
  `test_ooc_cross_row_decline_message.py`, `test_multi_table_gates.py`, the native column-access
  and chunked-entry cells) pass unchanged. Masked output bytes did not move; the only mask-side
  rule touched is the plan check, whose masked branch is unchanged.
- Checks on `9ed7e17f`, by exit code:
  - `python scripts/dev/registry_renumber.py --check --main c0961a17` (the main this branch
    holds): exit 0, `registry consistent`.
  - `python scripts/dev/registry_renumber.py --check --main origin/main`: exit 2,
    `main-not-merged`. `origin/main` is at `051b84cb`, past `c0961a17`; by the owner's
    instruction main is merged once, through slice 1, so it is not merged here.
  - `python scripts/dev/ratchet_check.py --base c0961a17`: exit 0.
  - `python scripts/dev/ratchet_check.py --base origin/main`: exit 1, for the same reason. The
    two findings are the ones slice 3 reported, keys main has since removed from its own
    baselines (`f29-producer-inventory`:
    `execution/out_of_core/_reorder_budget.py::require_disk::kw::'out_of_core_reorder_budget_too_small'`;
    `secret-material-census`: `decoy_engine/vault.py::vault_writer_for_config::job_seed`); this
    branch touches neither file. The pattern is unchanged from slice 3. The `sentry` gate step
    runs the same script with the default base (the merge base) and passed.
  - `python scripts/dev/check_fragment.py --base origin/main`: exit 0.
  - `python scripts/release/reason_release_pin.py --check`: exit 0, `reason release pin current`.
  - `python scripts/gen_capability_matrix.py`: no change.
- Before the gate, a sweep of `tests/sentry`, `tests/unit/config`, `tests/unit/plan`,
  `tests/unit/generation`, `tests/unit/transforms`, `tests/unit/reasons` and the reason and
  public-API tests on the uncommitted feature (Python 3.11, 4 workers) found the catalog and
  option-reads expectation changes listed above, a "Required when" cell in the
  `docs/strategies.md` option table that the doc-table test cannot read (fixed in the doc),
  and the fragment, not yet written; `tests/sentry/layout/test_import_layers.py` failed there
  only because import-linter is not in the `.venv` (it runs in the `layers` step).

T1 paths (80: slice 3's 78 and 2 more):

```
tests/acceptance/test_f06_route_cells.py
tests/acceptance/test_f06_route_parity.py
tests/generation
tests/integration/golden
tests/integration/golden/test_determinism_invariants.py
tests/integration/golden/test_fidelity_report_golden.py
tests/native/test_c8_iii_d2_native_positional_when.py
tests/native/test_capabilities.py
tests/native/test_chunked_entry_rev9_read.py
tests/native/test_chunked_entry_side_channels.py
tests/native/test_column_access_surfaces.py
tests/native/test_determinism_goldens.py
tests/native/test_determinism_protocol.py
tests/native/test_draw_site_providers_golden.py
tests/native/test_draw_sites_catalogue_pinned.py
tests/native/test_unconfigured_passthrough_read.py
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
tests/physical/test_shadow_full_frame.py
tests/physical/test_shadow_generation.py
tests/secret_providers/test_keyed_columns.py
tests/unit/config
tests/unit/determinism/test_derive_vectors.py
tests/unit/execution/test_created_target_contract.py
tests/unit/execution/test_dgrn_windowed_date.py
tests/unit/execution/test_group_key_chunked.py
tests/unit/execution/test_multi_table_gates.py
tests/unit/execution/test_ooc_cross_row_decline_message.py
tests/unit/execution/test_probe.py
tests/unit/execution/test_probe_scale.py
tests/unit/execution/test_probe_scale_kills.py
tests/unit/execution/test_r3_failure_publication_trace.py
tests/unit/execution/test_r3_public_signature_freeze.py
tests/unit/execution/test_r3_route_executors.py
tests/unit/execution/test_r3_within_route_baseline.py
tests/unit/execution/test_technique_class.py
tests/unit/execution/test_text_mask_chunked.py
tests/unit/expressions
tests/unit/generation
tests/unit/generation/test_generate_determinism_ratchets.py
tests/unit/generation/unique/test_gen_unique_old_input_bytes.py
tests/unit/generators
tests/unit/plan
tests/unit/providers_v2/test_distribution_behavior_metadata.py
tests/unit/reasons
tests/unit/relationships
tests/unit/run_result
tests/unit/scripts
tests/unit/strategy_catalog
tests/unit/test_de02_keyprovider.py
tests/unit/test_numpy_generic_timedelta.py
tests/unit/test_public_api.py
tests/unit/test_public_api_v2_choke_points.py
tests/unit/test_r3_10_generation_key_contract.py
tests/unit/test_reason_manifest_policy.py
tests/unit/test_reason_registry.py
tests/unit/test_reason_release_pin.py
tests/unit/test_registry_renumber.py
tests/unit/test_v2_generation.py
tests/unit/test_wave_b_definition_owners.py
tests/unit/transforms
```

Added to slice 3's list: `tests/physical/test_shadow_full_frame.py` (the mask-side
`derived_aggregate` shadow cells) and `tests/unit/execution/test_multi_table_gates.py` (it
loads `_multi_table_sentry_fixtures.py`, which declares `derived_aggregate` columns). The
other `derived_aggregate`, numeric-typing, reproducibility, plan-check, parity, pinned,
streamed and snapshot hits are already inside the list (`tests/unit/generation`,
`tests/unit/plan`, `tests/unit/transforms`, `tests/unit/config`, `tests/parity/...`,
`tests/native/...`). Every test file the slice adds or changes is inside those paths or in
`tests/sentry`. The `sentry` step runs all of `tests/sentry` (the input-edge and option-reads
sentries, the numeric reader registry, the parent-lookup and pool choke points, module size,
fan-out, table-kind, seam and the reason registry, pin, fixture and policy sentries); the
`layers` step runs `tests/sentry/layout/test_import_layers.py`.

## Record commit

This record is added in a docs-only commit on top of `9ed7e17f`. On that commit the `ruff`,
`format` and `sentry` gate steps and the five checks above were run again; the results are in
the slice report. The `tests`, `mypy`, `layers` and `licenses` steps were not rerun on it: the
commit changes no Python file.

## Not run

- The full suite. `tests/unit/execution` beyond the listed files, the rest of `tests/parity`,
  `tests/acceptance` beyond the gate pins, `tests/integration` beyond `golden`, `tests/native`
  beyond the listed files, `tests/perf` and `testflight` were not run.
- The gate steps under Python 3.11 (the red-first run, the sweep and the mutations used 3.11,
  the gates 3.10). A rerun of `origin/main` (no branch step failed). The gates against a branch
  that holds `origin/main` `051b84cb`.
- The full gate on the docs-only commit that adds this record; see "Record commit".

## V2-LOG

- (Closed: the owner answered "Plan notes and deviations", item 1, with (c) on 2026-11-13.)
- A raw config compiled with `compile_plan` does not pass through the `PipelineConfig`
  validators, as for slices 1 to 3. For such a config a `via` that does not reference the
  parent, a non-unique parent key, `null_probability` on the aggregate or a reader of the
  aggregate is not refused: the post-pass then counts no children (a wrong `via`), groups by
  the repeated key, overwrites the nulls, or the reader sees the null placeholder. A `via` or
  child column the plan check let through but that does not resolve raises a typed
  `ValueError` naming the table and column only (no registered code).
- An all-null aggregate column (every parent childless under `mean`, `min` or `max`, or a
  parent table with no rows) is typed `null` by Arrow inference unless `output_type` is
  declared, as for every other generated column without a declared type.
- The per-parent aggregate, like the scalar form, follows pandas for a group whose values are
  all null: `sum` is 0 and `count` 0, where SQL `SUM` gives null.
- `tests/unit/generation/relational/test_routes.py` is 1,133 lines after this slice.
- `generation/synthesize.py` is at the 600-line goal. GEN-INHERIT adds a dispatch for
  `inherit`; it will need to move code out first (`_topo_sort` is patched by
  `tests/parity/test_phase_f_f0_invariants.py` through `synthesize`, and the scalar aggregate
  and `_reference` are wrapped by the Phase F witnesses through `synthesize`, so those names
  must stay importable from it).

## Owner ruling (2026-11-13)

**Ruling.** A per-parent aggregate (`derived_aggregate` with `from_table` and `via`) whose child
column is not numeric under numeric typing is refused at config check time, before any data is
read, with one new code, `config.generate_aggregate_input_not_numeric` (`since=UNRELEASED`,
named in the slice's one fragment `changelog.d/gen-rel-e3b.md` in slash form). `sequence` and
`faker` integer columns are included, since numeric typing reads them as text when an
aggregate reads them. Numeric typing and its reader policy are unchanged, and the single-table
aggregate is untouched.

**Commits.** `b583bf4e` red cells (source as at `ffecd335`), `cde526ab` the change (final
source head), then this docs-only record.

**What the numeric decision reuses.** Config may not import generation (`.importlinter`: config
is tier 4, generation tier 3), so numeric typing's decision was moved down one layer, not
copied. Its non-candidate type rule (the body of `generated()` inside `numeric_plan`: text for
`sequence` and `faker`, the categories' type, the constant's type, float for `distribution`,
None otherwise) is now `config.generated_arrow_type` in `config/_generate_type_checks.py`,
with the three helpers it used moved verbatim beside it (`categorical_arrow_type`, formerly
`generation/_admission._categorical_type`; `constant_arrow_type` and `parametric_arrow_type`,
formerly in `generation/kinds/_constant.py` and `_parametric.py`, which now re-export them).
Numeric typing's `_reference_root` is now `config.reference_root` in
`config/_generate_relational.py`. `generation/_numeric_typing.py` calls both
(`generated()` returns `generated_arrow_type(col)` for a non-candidate; `planned()` and the
member map use `reference_root`), and admission calls `categorical_arrow_type`: one rule, two
readers, no behavior change (every digest, golden and snapshot in the T1 set is unchanged).

The validator check (`_input_numeric` in `config/_generate_relational.py`, called from
`_aggregate_problem`, so from `aggregate_problems` in the `PipelineConfig` validator
`_parent_lookups_valid`) follows `reference_root` from the child column and asks
`numeric_as_text_read(column)` (`config/_generate_type_checks.py`): the declared `output_type`
when it parses (a number kind is `int` or `decimal(n)`, the existing `_NUMBER` group the
`output_type` registry already uses for `sequence` and `distribution`), else whether
`generated_arrow_type` is an integer or floating type. This is exactly numeric typing's
`planned()` for a column that is not a candidate (`output_arrow_type(root) or generated(root)`),
and an aggregate input is never a candidate: `READERS["derived_aggregate.column"]` is
numeric-unsafe, so `numeric_plan` drops every reference group an aggregate reads. The witness
`test_the_verdict_matches_the_type_numeric_typing_gives_an_aggregate_input` runs a same-table
aggregate over each cell's column and checks that validation accepts exactly the columns the
run emits as numbers. Two consequences of "not numeric under numeric typing", both needed for
"a validated config never reaches the text join":

- A column type numeric typing does not type (`derived`, `formula`, `lookup`, `statistical`,
  `windowed_date`, `grouped_series`, `group_key`, a scalar `derived_aggregate`, or a reference
  to a column that is not generated) is refused unless it declares an `int` or `decimal(n)`
  `output_type`. Before the ruling a `derived` child such as `lid * 2` aggregated correctly
  (Arrow inference typed it int64); it now needs `output_type`. `statistical` and
  `windowed_date` cannot declare a number `output_type`, so they cannot be a per-parent input.
- A `date` or `bool` `output_type`, and a `bool` categorical or constant, are not numeric.
- An input that is itself a per-parent aggregate is left to `generate_aggregate_read_before_fill`.

**The code.** Identity `config.generate_aggregate_input_not_numeric`, registered in
`reasons/config/_part2.py` (config catalog, `PipelineValidationError`), `since=UNRELEASED`.
It is a pydantic line error at `tables.<i>.generate_columns.<j>.column` with `input` None,
raised from the validator outside any `except` (`__cause__` and `__context__` None). Message:
the registered summary ("Generate per-parent aggregate reads a child column that is not
numeric; give it a numeric output_type or aggregate a numeric column.") followed by
`Table '<from_table>', column '<column>', aggregate '<aggregate column>'.`. No value and no type
name is quoted. `problem_details` (`config/_generate_options.py`) takes an optional third,
value-free item for these names; every other problem keeps the summary alone.

**Red first (`b583bf4e` on `ffecd335`'s source).** `.venv` Python 3.11.18:
`pytest tests/unit/generation/relational/test_aggregate_input.py`: 12 failed, 12 passed.
Red, each "DID NOT RAISE ValidationError" (validation accepted the config) or, for the verdict
cells, `assert True is False`: `test_a_child_column_that_is_not_numeric_is_refused_at_validate`
[sequence], [faker-int], [categorical-text]; `test_the_refusal_holds_for_every_op` [sum],
[mean]; `test_the_refusal_names_the_table_column_and_aggregate_and_no_value`;
`test_a_column_numeric_typing_does_not_type_needs_a_numeric_output_type`;
`test_a_date_output_type_is_not_numeric`;
`test_a_reference_child_column_is_judged_by_the_column_it_copies`;
`test_the_verdict_matches_the_type_numeric_typing_gives_an_aggregate_input` [sequence],
[faker-int], [categorical-text]. Green on the base by design (the accepted cells): 
`test_a_numeric_child_column_is_accepted_and_aggregated`,
`test_a_childless_parent_sums_to_zero_over_an_accepted_column` and the verdict cell, each for
[distribution], [categorical-number], [constant-number], [output_type]; all twelve are red
under X2 (every per-parent input refused), the `output_type` ones also under X3.

**Test changes after the red commit (none weakened).** `test_aggregate.py`'s
`_grandparent_reader` (`customers.spend` over an `orders` column) aggregated `orders.amount`, a
plain `sequence`; the ruling refuses that input, so the fixture's `orders.amount` now declares
`output_type: int` (the assertions are unchanged; without it
`test_at12_the_same_reader_of_an_ordinary_column_validates[derived_aggregate.column
(per-parent)]` is refused by the new code). `tests/unit/plan/checks/test_generate_columns.py`
imports `categorical_arrow_type` from `decoy_engine.config` instead of the removed
`_admission._categorical_type`.

**Mutations (final source head `cde526ab`, detached worktree `/home/user/wt-e3b-r`, 51 of 51
red).** The 42 recorded mutations (V1, V3 and V5 re-anchored with one more indent level, now
inside `if via is not None:`; same edits) plus nine new, each applied once in a detached
worktree of `cde526ab` that imports its own `src` (`decoy_engine.__file__` printed as
`/home/user/wt-e3b-r/src/decoy_engine/__init__.py`), its named tests run (Python 3.11.18, the
`.venv`), and restored; every anchor matched once; the worktree was clean at the end and is
removed. Statuses are read from the harness's results JSON: 51 entries, 51 `RED`. No witness
starts a child process, so no mutation was applied in the checkout.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| X1 | the non-numeric input refusal is dropped | `config/_generate_relational.py` | 11: `test_a_child_column_that_is_not_numeric_is_refused_at_validate[sequence]`, `[faker-int]`, `[categorical-text]` ... |
| X2 | every per-parent input is refused | `config/_generate_relational.py` | 13: `test_a_numeric_child_column_is_accepted_and_aggregated[distribution]`, `[categorical-number]`, `[constant-number]` ... |
| X3 | a declared output_type is ignored | `config/_generate_type_checks.py` | 2: `test_a_numeric_child_column_is_accepted_and_aggregated[output_type]`, `test_a_childless_parent_sums_to_zero_over_an_accepted_column[output_type]` |
| X4 | sequence and faker child columns count as numbers | `config/_generate_relational.py` | 4: `test_a_child_column_that_is_not_numeric_is_refused_at_validate[sequence]`, `[faker-int]`, `test_the_verdict_matches_the_type_numeric_typing_gives_an_aggregate_input[sequence]` ... |
| X5 | a reference child column is judged as itself, not by the column it copies | `config/_generate_relational.py` | 1: `test_a_reference_child_column_is_judged_by_the_column_it_copies` |
| X6 | a column numeric typing does not type is accepted | `config/_generate_type_checks.py` | 1: `test_a_column_numeric_typing_does_not_type_needs_a_numeric_output_type` |
| X7 | the refusal message drops the table, column and aggregate names | `config/_generate_relational.py` | 1: `test_the_refusal_names_the_table_column_and_aggregate_and_no_value` |
| X8 | a decimal output_type is not a number kind | `config/_generate_type_checks.py` | 1: `test_a_column_numeric_typing_does_not_type_needs_a_numeric_output_type` |
| X9 | an input that is itself a per-parent aggregate is refused as non-numeric | `config/_generate_relational.py` | 1: `test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill[derived_aggregate.column (per-parent)]` |

X1 is the ruling's mutation (drop the refusal): the `sequence` cell goes red.

**Gates (head `cde526ab`, the final source head).** `GATES_WORKERS=4 scripts/dev/gates.sh
--keep-going --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <the 80 T1 paths
above>`, Python 3.10.20 (temporary venv `/home/user/venv-e3b-r-310`, synced with
`uv sync --frozen --python 3.10 --extra dev --extra lint --extra vault --extra cloud`, removed
after), `origin/main` fetched at `efedebc0`. The new test file is inside `tests/unit/generation`.

| Step | Branch (`cde526ab`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5332 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 10434 passed, 0 failed, 113 skipped (parallel and serial passes) |

Failure sets: branch 0, main 0, branch-only 0; every branch step passed, so the script did not
rerun `origin/main`. There is no branch failure.

**Script checks (on `cde526ab`, same venv), by exit code.**
`registry_renumber.py --check --main c0961a17`: 0 (`registry consistent`).
`registry_renumber.py --check --main origin/main`: 2 (`main-not-merged`; `origin/main` is now
`efedebc0`). `ratchet_check.py --base c0961a17`: 0. `ratchet_check.py --base origin/main`: 1,
the same two findings as before (`f29-producer-inventory` `_reorder_budget.py` key and
`secret-material-census` `vault.py` key, removed by main from its own baselines); the pattern is
unchanged. `check_fragment.py --base origin/main`: 0. `reason_release_pin.py --check`: 0
(`reason release pin current`).

**No digest, golden or snapshot moved,** and no expected output value was edited. Numeric
typing's decisions are byte-identical (the moved functions are verbatim); the near-neighbor
digests, determinism and draw-site goldens, Phase F oracle and witnesses and the numeric-typing
parity files in the T1 set pass unchanged.

**Line counts (touched source files).** `config/_generate_options.py` 571 (561),
`config/_generate_relational.py` 475 (413), `generation/_numeric_typing.py` 326 (352),
`generation/_admission.py` 292 (316), `config/_generate_type_checks.py` 291 (203),
`config/__init__.py` 198 (188), `reasons/config/_part2.py` 156 (149),
`generation/kinds/_parametric.py` 77, `generation/kinds/_constant.py` 40. At or above 550:
`config/_generate_options.py` 571 only. `generation/synthesize.py` is untouched (600). No
config module was added.

**Not run.** The full suite; the gate steps under Python 3.11 (red-first, sweep and mutations
used 3.11); a rerun of `origin/main`; gates against a branch holding `origin/main` `efedebc0`.
On the docs-only record commit only `ruff`, `format` and `sentry` were rerun.

**V2-LOG.**

- A raw config that bypasses `PipelineConfig` (for example `compile_plan` on a dict) still
  reaches the text join: on `cde526ab` a per-parent `sum` over a child `sequence` raises
  `ArrowTypeError` and `mean` `TypeError`, as for the other validate-only rules of slices 1
  to 3. No second guard was built (the plan does not require one).
- A `derived` child column with a numeric expression now needs a numeric `output_type` to be
  aggregated per parent; `statistical` and `windowed_date` cannot be per-parent inputs.
