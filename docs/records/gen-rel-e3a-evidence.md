# GEN-REL E3a build evidence

Status: built on `feat/gen-rel-e3a`. One placement deviation is provisional and awaits the
owner's ruling (see "Plan notes and deviations", item 1). No other stop condition was hit.

Spec: `docs/plans/gen-rel.md` on `origin/plan/gen-rel` (`26a83542`), slice row 3 (parent-anchored
`windowed_date`, the typed parent lookup, five coded date failures, the central generate-input
registry). The plan stays on the plan branch.

## Heads and environment

- Slice start: `feat/gen-rel-e2` at `ff0ab08c` (slice 2; it holds slice 1 `9b1b78bf` and
  `origin/main` `c0961a17`). Main is not merged into this branch.
- Commits: `ed70e4a8` red acceptance tests (source as at `ff0ab08c`), `e692aab7` the feature,
  `acde38e1` one added test cell (no source change). Final source head: `e692aab7`; `acde38e1`
  changes one test file only. Every mutation and gate total below belongs to the head named
  beside it. This record is added in a docs-only commit after `acde38e1`.
- Gates: Python 3.10.20 (a temporary venv synced with
  `uv sync --frozen --python 3.10 --extra dev --extra lint --extra vault --extra cloud`, as CI
  uses; the existing `.venv` was not changed), PyArrow 24.0.0, DuckDB 1.5.4, pandas 2.3.3,
  NumPy 2.2.6. The `layers` step ran in `.venv-layers` on Python 3.10.20.
- Red-first run and mutations: Python 3.11.18 (the `.venv`), PyArrow 24.0.0, DuckDB 1.5.4,
  pandas 2.3.3, NumPy 2.4.6.

## Red first (commit `ed70e4a8`, source unchanged from `ff0ab08c`)

`uv run --no-sync pytest -q -p no:cacheprovider -p no:xdist --continue-on-collection-errors
<the 15 files below> -rA --tb=line`: 170 failed, 722 passed, 1 skipped, 2 collection errors.

| File | Red |
| --- | --- |
| `tests/unit/plan/checks/test_generate_columns.py` | 11 |
| `tests/unit/config/test_generate_options.py` | 14 |
| `tests/unit/generation/test_numeric_typing.py` | 3 |
| `tests/parity/test_numeric_typing_readers.py` | 3 |
| `tests/sentry/test_numeric_reader_registry.py` | whole file (collection error) |
| `tests/unit/generation/relational/test_parent_lookup.py` (new) | whole file (collection error) |
| `tests/unit/transforms/test_windowed_date.py` | 36 |
| `tests/unit/generation/test_fr1_windowed_date.py` | 26 |
| `tests/unit/test_numpy_generic_timedelta.py` | 4 |
| `tests/unit/generation/relational/test_routes.py` | 41 |
| `tests/unit/config/test_generate_relational.py` | 17 |
| `tests/unit/generation/test_pinned_generation.py` | 1 |
| `tests/unit/generation/test_fr0_reproducibility.py` | 4 |
| `tests/sentry/test_reason_registry.py` | 1 |
| `tests/sentry/defects/test_generate_input_edges.py` (new) | 9 |

The files are listed in the plan's E3a red-first order: the strict-schema cross-table cells,
the unrelated same-named child Arrow-type cells and the descriptor cells; the typed lookup;
the coded date failures at both range ends; the same-name anchor, route and refusal cells and
the two-seed fixture; then reproducibility, the reason registry and the sentries.

Reasons:

- Absent API: `decoy_engine.config.iter_generate_input_refs`, `GenerateInputRef`,
  `parent_lookup_problems` and `parent_key_problem`; `OptionSpec.ref_context_key` and
  `ref_phase`; `generation/relational/_parent_lookup.py` (`lookup_parent_values`,
  `ParentLookupResult`); `WindowedDateError.code`; the eight unregistered reason codes;
  `_shift_and_format` without a row position; the retired `READER_KEYS` and `_SIBLING_KEYS`
  still present (the two collection errors and the registry cells).
- Wrong outcome: validate refuses `anchor_via` as an unknown option where the config must
  validate or where a registered code is expected; a raw config that reaches the plan check is
  refused or accepted by the same-table rule (the cross-table anchor is treated as a sibling);
  numeric typing makes the same-named child column text; the reproducibility report reads the
  same-named child column and misses the parent anchor and the lookup key; streaming admits
  the column; pinned compile and execute accept it; a date failure is an uncoded
  `WindowedDateError` whose text names a row position; a missing anchor of a generate column
  is `windowed_date_missing_anchor_ref`; `WindowedDateError` is still on the `UNCLASSIFIED`
  list (the edited sentry list makes that cell red).

Green on the base by design (14 cells), each tied to a mutation in the table below:

| Cell | Why green on the base | Mutation |
| --- | --- | --- |
| `test_a_reader_of_the_lookup_key_in_its_own_table_still_decides` | a `group_key` reader already keeps the key text | N2 |
| `test_a_same_table_anchor_is_still_a_reader_of_its_sibling` | the same-table rule is unchanged | N3 |
| `test_meta_an_inline_parent_lookup_is_seen` (4 cells) | meta-tests of the sentry's own scan | T1, T2, T3, T5 |
| `test_meta_reading_a_pool_column_without_a_map_is_not_flagged` | same | T4 |
| `test_at11_anchor_via_belongs_to_windowed_date_only` | an unregistered key is already unknown | C6 |
| `test_at10_the_hand_built_date_plan_is_otherwise_executable` | non-vacuity precondition of the hand-built plan | A10d |
| `test_at11_a_windowed_read_does_not_serve_the_anchored_table` | a `reference` column already makes the table Class B | W1 |
| `test_an_anchored_date_over_reproducible_inputs_is_not_excluded` | nothing excludes it yet | A11d |
| `test_configs_without_a_cross_table_input_keep_the_old_report_edge_for_edge` | pins the old report | A11c |
| `TestWindowedDateCodedFailures::test_an_offset_that_lands_on_either_end_is_accepted` | the range was already inclusive | D2 |
| `TestWindowedDateCodedFailures::test_both_ends_of_the_supported_range_are_accepted` | same | D1 |

No other new cell passed on the base.

Test changes after the red commit (none weakened):

- The resolver lives in `config/_generate_relational.py` (deviation 1), so the sentry inventories
  that file for the definition and allows the descriptor fields inside the resolver's three
  functions only. Two sentry cells and a meta-test were added for it: the resolver holds no
  option name and no column type list, and no generate-side module outside the registry lists
  input option names.
- `WindowedDateError` takes no code argument: each failure is a class with a fixed code
  (deviation 3). The AT-14 cell builds the four classes instead of passing a code.
- Far-out anchors: pandas 2 parses `0000-01-01` and `9999-12-31`, so `0000-01-01` moved from
  the invalid cells to the out-of-range cells, and two nanosecond-precision cells were added
  for the strings pandas cannot hold.
- The reproducibility spy cell compiles its config before it installs the spy (the plan check
  calls the resolver too).
- A lookup cell now holds two null parent keys (mutation L2), the windowed-read cell also
  asserts the Class B reason (mutation W1), and `acde38e1` adds a cell for a non-reference
  column that carries reference keys (mutation C3).

## Pre-existing tests whose expectation changed

| Test | Before | Now | Why |
| --- | --- | --- | --- |
| `tests/unit/plan/checks/test_generate_columns.py::test_reference_key_earlier_accepted_later_and_missing_refused[windowed_date.anchor]` | a missing anchor is `windowed_date_missing_anchor_ref` | `generate_column_forward_reference` | the plan moves target existence of a generate column's anchor to the one strict check |
| `tests/unit/transforms/test_windowed_date.py::TestWindowedDatePlanCheck::test_missing_anchor_column_raises` (renamed `test_missing_anchor_of_a_generate_column_is_the_strict_checks_refusal`) | `check_windowed_date_refs` refuses a generate column's missing anchor and quotes its name | that check passes; `run_config_only_checks` refuses with `generate_column_forward_reference` and no quoted name | same |
| `tests/unit/generation/test_fr1_windowed_date.py::test_error_message_does_not_echo_the_value` | the text holds `row 0` | the text holds no `row`; the code is `windowed_date_anchor_invalid` | the plan removes row positions from date errors |
| `tests/unit/generation/test_fr1_windowed_date.py::test_bad_anchor_raises_on_core_and_legacy` (21 cells) | any `WindowedDateError` | the registered code, the fixed text and no cause or context, on both routes | stronger |
| `tests/unit/generation/test_fr1_windowed_date.py::test_window_overflow_and_out_of_range_result_raise` (4 cells) | any `WindowedDateError` | `runtime.windowed_date_offset_out_of_range` | stronger |
| `tests/unit/generation/test_pinned_generation.py::test_ordinary_generation_is_untouched_by_pinned_mode` | any `WindowedDateError` | code `windowed_date_anchor_invalid`, no cause or context | stronger |
| `tests/unit/test_numpy_generic_timedelta.py` (4 cells) | `_shift_and_format(anchor, offset, position)` | `_shift_and_format(anchor, offset)`; the refusal's code is asserted | the row position argument is gone |
| `tests/sentry/test_reason_registry.py` | `WindowedDateError` on `UNCLASSIFIED` | removed (the list shrinks) | the plan classifies the family |
| `tests/sentry/test_numeric_reader_registry.py` | walks `READER_KEYS` and `_SIBLING_KEYS` | walks `iter_generate_input_refs` over one realistic column per registered input | the plan retires both tables |
| `tests/unit/generation/kinds/test_gen_types_e1_registry.py::test_every_registry_names_the_new_types` | `kind in READER_KEYS` | a column of the new type does not make its table's numeric column text | `READER_KEYS` is gone; the cell keeps its meaning |

No masked-output test changed, and no expected output value was edited anywhere.

## Mutations (final source head `e692aab7`, detached worktree, 69 of 69 red)

Each was applied once in a detached worktree that imports its own `src`, its named tests run
(Python 3.11.18, the `.venv`), and the file restored; every anchor matched exactly once, the
worktree was clean at the end and is removed. 68 ran on `e692aab7`. C3 was not red there: its
only witness went through `PipelineConfig`, where the option registry refuses the stray keys
first. `acde38e1` adds a direct cell (a test file only) and C3 is red on `acde38e1`, whose
source is `e692aab7`'s. The statuses are read from the harness's results file, one `RED` per
mutation, not from a count of log lines. No source changed after the run.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| M1 | the cross-table ref_kind of windowed_date.anchor is changed to column | `config/_generate_options.py` | 34: `test_a_cross_table_anchor_is_a_reader_of_the_parent_column_only`, `test_a_date_anchored_to_a_parent_column_that_is_not_reproducible_is_excluded`, `test_a_parent_anchor_does_not_make_a_same_named_child_column_text` ... |
| M2 | numeric typing resolves a reader to (consumer table, option value) | `generation/_numeric_typing.py` | 5: `test_a_cross_table_anchor_is_a_reader_of_the_parent_column_only`, `test_a_parent_anchor_does_not_make_a_same_named_child_column_text`, `test_at11_the_unrelated_child_column_keeps_its_numeric_arrow_type` ... |
| M3 | a parent lookup is inlined in the date generator | `generation/_grouped_windowed_generators.py` | 3: `test_at11c_a_foreign_key_with_no_parent_is_the_lookups_refusal`, `test_at11c_a_present_parent_with_a_null_anchor_is_refused_not_nulled`, `test_only_the_parent_lookup_builds_a_key_to_row_map_over_a_pool_column` |
| M4 | a missing key is conflated with a present null | `generation/relational/_parent_lookup.py` | 7: `test_a_copying_caller_gets_the_null_and_the_type_and_reuses_the_missing_key_code`, `test_a_key_with_no_usable_identity_is_refused_as_missing_without_a_cause`, `test_a_missing_key_is_refused_even_when_every_value_is_null` ... |
| M4b | a present null is conflated with a null foreign key | `generation/relational/_parent_lookup.py` | 3: `test_a_copying_caller_gets_the_null_and_the_type_and_reuses_the_missing_key_code`, `test_a_present_parent_with_a_null_value_stays_a_present_null`, `test_at11c_a_present_parent_with_a_null_anchor_is_refused_not_nulled` |
| M5 | the lookup indexes the map with dict[fk] | `generation/relational/_parent_lookup.py` | 7: `test_a_copying_caller_gets_the_null_and_the_type_and_reuses_the_missing_key_code`, `test_a_key_with_no_usable_identity_is_refused_as_missing_without_a_cause`, `test_a_missing_key_is_refused_even_when_every_value_is_null` ... |
| M6 | the lookup refusal is raised inside the except block | `generation/relational/_parent_lookup.py` | 1: `test_a_key_with_no_usable_identity_is_refused_as_missing_without_a_cause` |
| M6b | the offset refusal is raised inside the except block | `transforms/windowed_date.py` | 4: `test_an_offset_that_crosses_either_end_is_offset_out_of_range`, `test_window_overflow_and_out_of_range_result_raise` |
| M6c | the anchor refusal is raised inside the except block | `transforms/windowed_date.py` | 7: `test_bad_anchor_raises_on_core_and_legacy`, `test_grammar_calendar_and_type_failures_are_anchor_invalid` |
| M7 | row positions are restored in date errors | `transforms/windowed_date.py` | 61: `test_a_string_anchor_outside_the_supported_range_is_out_of_range`, `test_a_timezone_aware_typed_anchor_is_anchor_invalid`, `test_a_typed_anchor_outside_the_supported_range_is_out_of_range` ... |
| M8 | one legal date is returned for both seeds (the date seed is fixed) | `generation/_grouped_windowed_generators.py` | 1: `test_at16_the_offsets_follow_the_seed_not_only_the_parents` |
| M8b | one legal date is returned for every row (constant offset) | `transforms/windowed_date.py` | 2: `test_at16_the_offsets_follow_the_seed_not_only_the_parents`, `test_at16_two_anchored_columns_draw_their_own_offsets` |
| M9 | streaming admits anchor_via | `generation/_admission.py` | 2: `test_at11_streaming_admission_declines_the_column_and_the_run_stays_resident`, `test_at11_streaming_declines_even_when_the_same_named_child_column_is_a_date` |
| A11a | AT-11: the same-named child column is the anchor | `generation/_grouped_windowed_generators.py` | 21: `test_at11_a_child_with_no_same_named_column_is_anchored_the_same_way`, `test_at11_a_null_foreign_key_gives_a_null_date`, `test_at11_a_typed_parent_anchor_is_read_with_its_type` ... |
| A16a | AT-16: the offsets come from module-global random | `transforms/windowed_date.py` | 2: `test_at11_the_same_seed_gives_the_same_dates_and_another_seed_others`, `test_at16_the_dates_do_not_read_module_global_random` |
| A16b | AT-16: two anchored columns share one offset stream | `generation/_grouped_windowed_generators.py` | 1: `test_at16_two_anchored_columns_draw_their_own_offsets` |
| A11b | AT-11b: the reproducibility graph keeps inputs same-table only | `generation/_reproducibility.py` | 3: `test_a_date_anchored_to_a_parent_column_that_is_not_reproducible_is_excluded`, `test_a_same_named_child_column_is_not_the_anchored_dates_input`, `test_the_acceptance_cells_hold_on_the_registered_descriptors` |
| A11c | AT-11b: a refactor drops an old dependency edge (order_by) | `generation/_reproducibility.py` | 1: `test_configs_without_a_cross_table_input_keep_the_old_report_edge_for_edge` |
| A11d | AT-11b: every input in another table counts as not reproducible | `generation/_reproducibility.py` | 1: `test_an_anchored_date_over_reproducible_inputs_is_not_excluded` |
| A10a | AT-10: anchor_via is guarded at pinned compile only | `generation/_pinned_compile.py` | 1: `test_at10_pinned_execute_refuses_an_anchored_date_before_any_draw` |
| A10b | AT-10: the plan code is reused at execute | `generation/_pinned_compile.py` | 1: `test_at10_pinned_execute_refuses_an_anchored_date_before_any_draw` |
| A10c | AT-10: pinned compile no longer refuses anchor_via | `generation/_pinned_compile.py` | 2: `test_at10_pinned_compile_refuses_an_anchored_date`, `test_at10_pinned_execute_refuses_an_anchored_date_before_any_draw` |
| A10d | AT-10: the pinned predicate refuses every windowed_date | `generation/_pinned_compile.py` | 1: `test_at10_the_hand_built_date_plan_is_otherwise_executable` |
| A14a | AT-14: a date refusal's code is not the registered one | `reasons/runtime/_part2.py` | 23: `test_a_timezone_aware_typed_anchor_is_anchor_invalid`, `test_at14_a_windowed_date_error_is_a_classified_family`, `test_at14_date_codes_are_registered_unreleased_in_the_right_catalog` ... |
| A14b | AT-14: WindowedDateError is not a classified family | `reason_adapter.py` | 3: `test_at14_a_windowed_date_error_is_a_classified_family`, `test_every_engine_exception_family_is_classified_or_listed`, `test_the_reason_adapter_reports_the_code_itself` |
| A15a | AT-15: the missing-key refusal formats the key | `generation/relational/_parent_lookup.py` | 3: `test_a_non_null_key_with_no_parent_is_refused`, `test_at11c_a_foreign_key_with_no_parent_is_the_lookups_refusal`, `test_refusals_name_the_child_table_and_column_only` |
| A15b | AT-15: the parent-null refusal drops the table and column names | `generation/_grouped_windowed_generators.py` | 1: `test_at11c_a_present_parent_with_a_null_anchor_is_refused_not_nulled` |
| C1 | the parent key predicate accepts a key that is not a sequence | `config/_generate_relational.py` | 6: `test_at11_a_parent_key_that_is_not_a_non_null_sequence_is_refused`, `test_at11_each_anchored_column_is_reported`, `test_at15_validate_refusals_of_an_anchored_date_carry_the_fixed_summary_and_no_input` ... |
| C1b | the parent key predicate accepts a nullable sequence | `config/_generate_relational.py` | 3: `test_at11_a_parent_key_that_is_not_a_non_null_sequence_is_refused`, `test_at9_a_key_that_is_not_a_non_null_sequence_is_refused`, `test_the_one_parent_key_predicate_returns_the_callers_code` |
| C1c | the parent key predicate ignores the caller's code | `config/_generate_relational.py` | 1: `test_the_one_parent_key_predicate_returns_the_callers_code` |
| C2 | a lookup through a self-reference is accepted | `config/_generate_relational.py` | 2: `test_at11_a_lookup_through_a_self_reference_is_refused`, `test_at15_validate_refusals_of_an_anchored_date_carry_the_fixed_summary_and_no_input` |
| C4 | the pipeline validator never calls the parent-lookup checks | `config/_pipeline.py` | 8: `test_at11_a_lookup_through_a_column_that_is_not_a_reference_is_refused`, `test_at11_a_lookup_through_a_self_reference_is_refused`, `test_at11_a_parent_key_that_is_not_a_non_null_sequence_is_refused` ... |
| C5 | the tree key no longer goes through the one predicate | `config/_generate_relational.py` | 2: `test_at9_a_key_that_is_not_a_non_null_sequence_is_refused`, `test_the_self_reference_key_rule_is_the_same_predicate` |
| C6 | anchor_via is accepted on every column type | `config/_generate_options.py` | 1: `test_at11_anchor_via_belongs_to_windowed_date_only` |
| C7 | anchor_via is not type-checked | `config/_generate_options.py` | 4: `test_anchor_via_is_a_windowed_date_option`, `test_at11_anchor_via_must_be_a_string` |
| R1 | the resolver ignores the companion key (sibling-only resolution) | `config/_generate_relational.py` | 33: `test_a_cross_table_anchor_is_a_reader_of_the_parent_column_only`, `test_an_anchor_whose_lookup_column_leads_nowhere_has_no_source_table`, `test_an_anchor_with_anchor_via_is_a_column_of_the_referenced_table` ... |
| R2 | a post-fill input must be generated earlier | `config/_generate_relational.py` | 1: `test_formula_references_are_read_after_the_table_is_filled` |
| R3 | a lookup column that is not a reference yields no anchor edge | `config/_generate_relational.py` | 3: `test_an_anchor_whose_lookup_column_leads_nowhere_has_no_source_table`, `test_anchor_via_that_is_not_a_reference_leads_to_no_table` |
| R4 | an input in another table must be 'earlier' | `config/_generate_relational.py` | 10: `test_an_anchor_whose_lookup_column_leads_nowhere_has_no_source_table`, `test_an_anchor_with_anchor_via_is_a_column_of_the_referenced_table`, `test_anchor_via_must_name_an_earlier_column_of_the_same_table` ... |
| R5 | a companion key without its value keeps the registered phase | `config/_generate_relational.py` | 1: `test_an_input_in_a_named_table_resolves_to_that_table` |
| S1 | the strict check accepts an input missing from its source table | `plan/checks/_generate_columns.py` | 5: `test_anchor_via_that_is_not_a_reference_leads_to_no_table`, `test_cross_table_anchor_is_not_satisfied_by_a_same_named_child_column`, `test_cross_table_anchor_missing_from_the_parent_is_refused` ... |
| S2 | the strict check no longer requires an earlier sibling | `plan/checks/_generate_columns.py` | 9: `test_anchor_via_must_name_an_earlier_column_of_the_same_table`, `test_anchor_without_anchor_via_keeps_the_same_table_rule`, `test_reference_key_earlier_accepted_later_and_missing_refused` ... |
| S3 | the input edges are judged before a formula's own reference rules | `plan/checks/_generate_columns.py` | 1: `test_formula_reference_to_no_sibling_refused` |
| N1 | anchor_via is classified numeric-unsafe | `generation/_numeric_typing.py` | 3: `test_at11_the_unrelated_child_column_keeps_its_numeric_arrow_type`, `test_the_lookup_key_of_an_anchored_date_stays_numeric`, `test_the_lookup_key_stays_numeric_on_both_sides` |
| N2 | a group_key label is classified numeric-safe | `generation/_numeric_typing.py` | 2: `test_a_reader_of_the_lookup_key_in_its_own_table_still_decides`, `test_each_reader_kind_decides_whether_the_sequence_is_numeric` |
| N3 | a windowed_date anchor is classified numeric-safe | `generation/_numeric_typing.py` | 4: `test_a_cross_table_anchor_is_a_reader_of_the_parent_column_only`, `test_a_same_table_anchor_is_still_a_reader_of_its_sibling`, `test_each_reader_kind_decides_whether_the_sequence_is_numeric` ... |
| N4 | an unclassified reader is treated as numeric-safe | `generation/_numeric_typing.py` | 1: `test_an_unclassified_reader_keeps_the_column_it_reads_text` |
| N5 | an unknown column type no longer keeps its table text | `generation/_numeric_typing.py` | 1: `test_an_unlisted_column_type_keeps_numeric_columns_text` |
| D1 | the supported anchor range excludes its ends | `transforms/windowed_date.py` | 4: `test_an_offset_that_crosses_either_end_is_offset_out_of_range`, `test_at11c_a_parent_anchor_on_either_end_of_the_range_is_accepted`, `test_both_ends_of_the_supported_range_are_accepted` ... |
| D2 | the supported result range excludes its ends | `transforms/windowed_date.py` | 4: `test_an_offset_that_lands_on_either_end_is_accepted`, `test_at11c_a_parent_anchor_on_either_end_of_the_range_is_accepted`, `test_both_ends_of_the_supported_range_are_accepted` ... |
| D3 | an out-of-range anchor is reported as invalid | `transforms/windowed_date.py` | 12: `test_a_string_anchor_outside_the_supported_range_is_out_of_range`, `test_a_typed_anchor_outside_the_supported_range_is_out_of_range`, `test_at11c_a_parent_anchor_outside_the_supported_range_is_refused` ... |
| D4 | a crossed offset is reported as an anchor out of range | `transforms/windowed_date.py` | 13: `test_an_offset_that_crosses_either_end_is_offset_out_of_range`, `test_at11c_an_offset_that_crosses_the_supported_range_is_refused`, `test_window_overflow_and_out_of_range_result_raise` ... |
| D5 | a date pandas cannot hold is never told apart from an impossible one | `transforms/windowed_date.py` | 2: `test_a_string_anchor_outside_the_supported_range_is_out_of_range` |
| D6 | an impossible date is reported as out of range | `transforms/windowed_date.py` | 3: `test_grammar_calendar_and_type_failures_are_anchor_invalid` |
| D7 | an impossible clock time is reported as out of range | `transforms/windowed_date.py` | 2: `test_grammar_calendar_and_type_failures_are_anchor_invalid` |
| G1 | a parent row with a null anchor gives a null date | `generation/_grouped_windowed_generators.py` | 1: `test_at11c_a_present_parent_with_a_null_anchor_is_refused_not_nulled` |
| G2 | a null foreign key is refused as a null parent anchor | `generation/_grouped_windowed_generators.py` | 2: `test_at11_a_null_foreign_key_gives_a_null_date`, `test_at11c_a_null_anchor_under_a_null_foreign_key_is_not_refused` |
| G3 | the table loop does not pass the sibling configs | `generation/synthesize.py` | 19: `test_at11_a_child_with_no_same_named_column_is_anchored_the_same_way`, `test_at11_a_null_foreign_key_gives_a_null_date`, `test_at11_a_typed_parent_anchor_is_read_with_its_type` ... |
| L1 | a repeated parent key is not refused | `generation/relational/_parent_lookup.py` | 5: `test_a_duplicate_parent_key_is_refused_even_when_no_child_asks_for_it`, `test_a_parent_key_that_occurs_twice_is_refused`, `test_at11c_a_repeated_parent_key_is_the_lookups_refusal` |
| L2 | a null parent key is a key | `generation/relational/_parent_lookup.py` | 1: `test_a_null_parent_key_is_never_a_match_and_never_a_duplicate` |
| L3 | the result type is inferred from the values | `generation/relational/_parent_lookup.py` | 2: `test_no_foreign_keys_give_no_values`, `test_the_parent_columns_arrow_type_is_kept` |
| L4 | parent keys are matched by identity of the raw value | `generation/relational/_parent_lookup.py` | 4: `test_a_dict_key_matches_whatever_its_item_order`, `test_each_foreign_key_gets_its_own_parents_value`, `test_list_and_dict_keys_are_matched_by_equality` |
| W1 | a windowed read treats a reference column as servable | `generation/_window_admission.py` | 1: `test_at11_a_windowed_read_does_not_serve_the_anchored_table` |
| T1 | sentry scan: dict comprehensions are not seen | `tests/sentry/defects/test_generate_input_edges.py` | 2: `test_meta_an_inline_parent_lookup_is_seen` |
| T2 | sentry scan: dict(...) calls are not seen | `tests/sentry/defects/test_generate_input_edges.py` | 1: `test_meta_an_inline_parent_lookup_is_seen` |
| T3 | sentry scan: index-building loops are not seen | `tests/sentry/defects/test_generate_input_edges.py` | 3: `test_meta_an_inline_parent_lookup_is_seen`, `test_meta_the_scan_sees_the_real_lookup`, `test_only_the_parent_lookup_builds_a_key_to_row_map_over_a_pool_column` |
| T4 | sentry scan: any loop over a pool column is flagged | `tests/sentry/defects/test_generate_input_edges.py` | 2: `test_meta_reading_a_pool_column_without_a_map_is_not_flagged`, `test_only_the_parent_lookup_builds_a_key_to_row_map_over_a_pool_column` |
| T5 | sentry scan: self.pools is not a pool | `tests/sentry/defects/test_generate_input_edges.py` | 1: `test_meta_an_inline_parent_lookup_is_seen` |
| C3 | a lookup through a column that is not a reference reads its reference_table anyway | `config/_generate_relational.py` | 1: `test_at11_a_lookup_through_a_column_that_is_not_a_reference_is_refused` |

M1 to M9 are the E3a mutations the plan's acceptance paragraph names: each cross-table
`ref_kind` changed to `column` (M1; `windowed_date.anchor` is the only cross-table registration
in this slice), numeric typing resolving to (consumer table, option value) (M2), an inlined
parent lookup (M3), missing conflated with present-null in both directions (M4, M4b), `dict[fk]`
(M5), a refusal raised inside `except` (M6, M6b, M6c), row positions restored (M7), one legal
date for both seeds (M8, M8b) and streaming admitting `anchor_via` (M9). M2 turns the AT-11
Arrow-type cell `test_at11_the_unrelated_child_column_keeps_its_numeric_arrow_type` red, as the
plan requires, with the unit and parity type cells. A11a, A16a, A11b, A11c, A10a and A10b are
the per-AT mutants (same-named child, module-global random, `_SIBLING_KEYS` same-table only, a
refactor that changes an old edge, guard compile only, reuse the plan code at execute). T1 to
T5 mutate the new sentry's own scan, for its meta cells. The rest cover the config matrix, the
resolver, the strict check, the date primitive and the lookup cell by cell, and the cells that
are green on the base by design.

The sentry also runs the descriptor mutation in process on every run:
`test_each_cross_table_descriptor_turned_back_to_column_fails_every_cell` swaps the registered
descriptor for a same-table `column` and requires all four acceptance cells (accepted without
a same-named child column, refused when missing from the parent, the child column's numeric
type, the reproducibility dependency) to fail.

## What the slice does

- `windowed_date` takes `anchor_via`, an earlier `reference` column of the same table; `anchor`
  is then a column of that reference's parent table. The generator reads the parent table and
  column from the resolved input edge, never from a sibling of that name, finds each row with
  the one parent lookup and hands the anchors to the existing window code, so the per-row seeds
  are the ones a same-table anchor uses.
- `generation/relational/_parent_lookup.py`: `lookup_parent_values(...) -> ParentLookupResult`
  (`values`, `found`, `dtype`). `found[i]` is False only for a null foreign key, so a present
  parent with a null value is `found` with a `None` value.
- `OptionSpec` has `ref_kind`, `ref_context_key` and `ref_phase`; `iter_generate_input_refs`
  resolves them to `GenerateInputRef` edges. `plan/checks/_generate_columns.py::_check_input_edges`
  is the one checker of table existence, column existence and order. Numeric typing and the
  reproducibility report read the same edges.
- `WindowedDateError` carries a registered code; its text has no row position, type name or
  value. It is off the `UNCLASSIFIED` list and in the reason adapter's coded families.
- Streaming admission declines a column with `anchor_via` (`windowed_date_anchor_via`). The
  pinned predicate refuses it at compile (`plan.pinned_relational_option_unsupported`) and at
  execute (the `runtime` twin, a `GenerationError`), both registered by slice 2.

### Descriptor variants and registrations: here and in E3b

Landed here:

- The machinery for every variant: `ref_kind` values `column`, `column_list`, `expression`,
  `column_via_reference` and `column_in_named_table`; `ref_context_key`; `ref_phase` `inline`
  and `post_fill`. The resolver implements all of them. `column_in_named_table` has no
  registration yet, so `tests/unit/config/test_generate_options.py` exercises it through a
  patched registry entry.
- Registrations: `windowed_date.anchor` as `column_via_reference` with context `anchor_via`;
  `windowed_date.anchor_via` as `column`; `formula.references` as `column_list` with phase
  `post_fill` (see deviation 4). The existing `column` and `expression` registrations are
  unchanged.

Deferred to E3b, whose row owns them: the `derived_aggregate` options `from_table` and `via`,
and `derived_aggregate.column` and `via` as `column_in_named_table` with context `from_table`
and phase `post_fill`. E3a's row has neither those option keys nor
`plan/_checks_derived_aggregate.py`, and E3b's red-first order starts with those cells.
`derived_aggregate.column` stays a same-table `column` here.

Main's `lookup` type registers no column input (`file`, `codeset`, `value_column` and
`weight_column` name a file's columns, not generated ones), so the registry walk yields no edge
for it and nothing special-cases it; a cell pins that.

### What replaced `READER_KEYS` and `_names_read`

`_numeric_typing._readers` walks `config.iter_generate_input_refs` once per run and indexes each
edge by its resolved `(source table, source column)`. A reader's identity is its column type
and option (`windowed_date.anchor`), looked up in the one `READERS` policy; a reader with no
entry, or a column type the option catalog does not list, keeps the column text. The
`derived.expression` decision stays per expression. `READERS` gained `windowed_date.anchor_via`
(safe: the foreign key is matched to the parent key by equality, and both have one planned
type). `_reproducibility._SIBLING_KEYS` and `_sibling_inputs` are gone the same way.

## Plan notes and deviations

1. **Placement of the resolver (provisional, awaiting the owner's ruling).** The plan's
   Placement table puts "all input descriptors and their public iterator" in
   `config/_generate_options.py`. That file is 539 lines at the slice start. With the
   descriptors alone (the two new `OptionSpec` fields, the wider docstring and the
   registrations) it is 554. `GenerateInputRef`, `iter_generate_input_refs` and its two
   helpers are 73 lines, which would make it about 630, over the 600 goal, and no census entry
   may be added. `config/` already holds 25 direct modules, the fan-out cap, so a new config
   module is not possible either. On the orchestrator's instruction the descriptors and every
   registration stay in `_generate_options.py`, the one registry, and the dataclass, the
   resolver and its helpers live in `config/_generate_relational.py` (318 lines), exported from
   `decoy_engine.config`; no consumer imports a private config module. The resolver holds no
   option name and no column type list: it walks `GENERATE_OPTIONS`, branches on `ref_kind`
   and follows a `reference` column to its `reference_table`. The sentry pins that (the exact
   set of string constants in the resolver's functions, and no module-level table of input
   option names in any generate-side module outside the registry). The function bodies do not
   depend on where they live, so moving them is a cut and paste plus two sentry constants.
   The alternatives (moving unrelated code out of `_generate_options.py`, or compressing the
   resolver) were not built.
2. **A lookup column that is not a `reference`.** The plan names two config codes and is silent
   on an `anchor_via` that names an earlier column of another type. Validate refuses it with
   `config.generate_parent_lookup_key_not_unique`, whose summary reads "a column that is not a
   reference to a sequence key with no null_probability". For a raw config that skips
   validation, the resolver gives the anchor an empty source table and the strict check
   refuses it with `plan.generate_column_forward_reference` at `anchor`. A lookup column that
   is missing or declared later is the strict check's refusal at `anchor_via`.
3. **One class per date failure.** A constructor that takes the code would need a new entry in
   the reason sentry's forwarding-constructor list (`ADAPTERS`), an allowlist. Instead
   `WindowedDateError` is the invalid-anchor failure with a class-level code, and
   `WindowedDateAnchorRangeError`, `WindowedDateOffsetRangeError` and
   `WindowedDateParentAnchorNullError` are subclasses with theirs. Every existing
   `except WindowedDateError` and `pytest.raises(WindowedDateError)` still matches. The
   catalog owner of all four codes is `WindowedDateError`.
4. **`formula.references` is registered** as `column_list` with phase `post_fill`. Numeric
   typing and the reproducibility report used to read that key from their own tables; with
   those tables gone the edge has to come from the registry, or a sequence read by a formula
   would turn numeric and move bytes. A post-fill input need not be generated earlier, which
   is what the formula post-pass does. The formula's own reference rules keep their codes
   (`generate_formula_reference_unknown` and the rest): `_check_formula` now runs before the
   input-edge check, so the strict check's existence test on those edges never fires first.
5. **A `derived` expression's inputs are the columns it reads** (the `col_ref` nodes of the
   compiled expression, as the plan check already resolved them). Numeric typing and the
   reproducibility report used a regular expression over the text. Numeric typing is
   unaffected (a name that is not a column read was already judged safe). The reproducibility
   report differs only for an expression that mentions an excluded sibling's name inside a
   string literal: it is no longer reported as depending on it. An expression that does not
   parse has no edges; such a column is refused at plan compile.
6. **The missing anchor of a generated column changes code**, from
   `plan.windowed_date_missing_anchor_ref` to `plan.generate_column_forward_reference`, as the
   plan directs ("target existence moved to GEN-STRICT"). The masked column keeps the old code.
7. **Date messages carry no names from the shared primitive.** `apply_windowed_date` knows
   neither table nor column, and it serves masking, the streamed core and the whole-table
   route, so the three shared failures are the fixed summary alone on every route. The
   parent-null refusal and the two lookup refusals are built in generation and name the child
   table and column.
8. **Generation order needs no new edge.** A `column_via_reference` source table is the parent
   of a `reference` column of the same table, which the order already puts first; a cell pins
   the order with the tables declared child first.
9. **`_pinned_execute.py` is in the row and unchanged.** Execute already re-runs
   `reject_unpinnable(..., at_execute=True)`, so widening the one predicate in
   `_pinned_compile.py` gives the run-time twin. `tests/unit/generation/test_pinned_generation.py`
   gains only the code assertion on the ordinary date error; the pinned cells for `anchor_via`
   are in `test_routes.py`.
10. **The windowed read (`generate_rows`)** is not in the plan's route list. A table with a
    valid `anchor_via` has a `reference` column, which makes it Class B there, so it is never
    served; a cell pins that reason.
11. **The parent-lookup choke-point cells** are in the new
    `tests/sentry/defects/test_generate_input_edges.py`, beside the option-input cells. The
    plan names that file for the option-input sentry and no file for the lookup sentry.
12. Handoff against plan: the handoff's step 7 has the builder open the PR; the orchestrator
    opens it. The handoff's `uv sync --frozen --extra dev` is replaced by the CI-like Python
    3.10 venv. Neither changes a guarantee.

## Files outside the plan's slice row

- `src/decoy_engine/config/_pipeline.py`: the `_parent_lookups_valid` validator that calls
  `parent_lookup_problems`. The plan's Placement table puts the relational validator call
  there; the config codes cannot be raised without it.
- `src/decoy_engine/reason_adapter.py`: one line adding `WindowedDateError` to the coded
  families. Without it the family cannot leave `UNCLASSIFIED`.
- `CODEMAP.md`: the `config/` row says what `_generate_relational.py` now holds, and the
  Generation row names the tree draw and the parent lookup.
- `tests/sentry/test_reason_registry.py`: the `UNCLASSIFIED` entry is removed (the list shrinks).
- `tests/sentry/test_sentry_manifest.py`: the new sentry file is listed.
- `tests/sentry/secret_material_census.json`: the recorded line of `apply_windowed_date`'s
  `seed` parameter moved from 280 to 311 (regenerated with
  `scripts/secret_material_census.py --write`; 258 entries before and after, no verdict changed).
- `tests/unit/generation/relational/_support.py`: the anchored-date fixture builders.
- `tests/unit/test_numpy_generic_timedelta.py` and
  `tests/unit/generation/kinds/test_gen_types_e1_registry.py`: see "Pre-existing tests whose
  expectation changed".
- `docs/records/gen-rel-e3a-evidence.md` (this file).

`docs/capability-matrix.md` is unchanged: `scripts/gen_capability_matrix.py` writes the same
file (the slice adds no strategy). `tests/sentry/defects/test_generate_option_reads.py` is
unchanged: `anchor_via` is read in three files it already scans, and the new lookup module
reads no column option.

No census number, threshold, pin, baseline or allowlist rose. `REGISTRY_VERSION`, the minor
fixtures, `consumer-contract.json`, the release pin and the docs version sentence are untouched,
and the eight new codes are `since=UNRELEASED`.

## New reason codes

| Identity | Raised as | Phase |
| --- | --- | --- |
| `config.generate_parent_lookup_key_not_unique` | `PipelineConfig` validation error | configuration |
| `config.generate_parent_lookup_self_reference` | `PipelineConfig` validation error | configuration |
| `runtime.parent_lookup_key_missing` | `GenerationError` | generate |
| `runtime.parent_lookup_key_not_unique` | `GenerationError` | generate |
| `runtime.windowed_date_anchor_invalid` | `WindowedDateError` | mask, generate |
| `runtime.windowed_date_anchor_out_of_range` | `WindowedDateAnchorRangeError` | mask, generate |
| `runtime.windowed_date_offset_out_of_range` | `WindowedDateOffsetRangeError` | mask, generate |
| `runtime.windowed_date_parent_anchor_null` | `WindowedDateParentAnchorNullError` | generate |

An `anchor_via` column in pinned mode reuses slice 2's
`plan.pinned_relational_option_unsupported` and `runtime.pinned_relational_option_unsupported`.

## Line counts

`generation/synthesize.py` 598 (595 at the slice start; goal 600), `generation/_streamed.py` 598
(unchanged), `config/_generate_options.py` 554 (539), `reasons/runtime/_part2.py` 520,
`plan/checks/_generate_columns.py` 506 (517), `config/_pipeline.py` 500,
`generation/_pinned_compile.py` 394, `transforms/windowed_date.py` 376 (343),
`generation/_numeric_typing.py` 349 (347), `config/_generate_relational.py` 318 (178),
`generation/_admission.py` 316, `generation/_reproducibility.py` 218 (227),
`generation/_grouped_windowed_generators.py` 208 (154),
`generation/relational/_parent_lookup.py` 114 (new).

## Gates (head `acde38e1`; source is `e692aab7`'s)

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests <T1 paths>` on `acde38e1`, Python 3.10.20, with
`origin/main` fetched at `75b81efe`.

| Step | Branch (`acde38e1`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5318 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 10263 passed, 0 failed, 113 skipped (10253 in the parallel pass, 10 in the serial pass) |

- Failure sets: branch 0, main 0, branch-only 0. Every branch step passed, so the gate script
  did not rerun `origin/main`. There is no branch failure.
- AT-1: no existing digest, golden or snapshot moved, and no expected output value was edited.
  The E1 near-neighbor digests (which include a same-table date and a scalar aggregate), the
  Phase F oracle and witnesses, the determinism goldens, the draw-site goldens and the
  old-input-bytes pins are in the T1 set and pass unchanged.
- Masked output: the mask-side `windowed_date` tests (`tests/unit/transforms`,
  `tests/unit/execution/test_dgrn_windowed_date.py` with its pinned known answers and chunked
  parity cells, `tests/native/test_determinism_goldens.py`,
  `tests/native/test_draw_site_providers_golden.py`, the chunked-entry and column-access
  cells) pass unchanged. Masked output bytes did not move; only the text and code of a
  failure changed.
- Checks on `acde38e1`, by exit code:
  - `python scripts/dev/registry_renumber.py --check --main c0961a17` (the main this branch
    holds): exit 0, `registry consistent`.
  - `python scripts/dev/registry_renumber.py --check --main origin/main`: exit 2,
    `main-not-merged`. `origin/main` moved to `75b81efe` during the build, six commits past
    `c0961a17` (#550, #539, #543, #549, #551, #511). By the owner's instruction main is merged
    once, through slice 1, so it is not merged here.
  - `python scripts/dev/ratchet_check.py --base origin/main`: exit 1, for the same reason. The
    two findings are keys main has since removed from its own baselines
    (`f29-producer-inventory`: `execution/out_of_core/_reorder_budget.py::require_disk`, by
    #551; `secret-material-census`: `vault.py::vault_writer_for_config::job_seed`, by #511);
    this branch touches neither file. With the default base (the merge base, `c0961a17`) and
    with `--base c0961a17` it exits 0. The `sentry` gate step runs the same script with the
    default base and passed.
  - `python scripts/dev/check_fragment.py --base origin/main`: exit 0.
  - `python scripts/release/reason_release_pin.py --check`: exit 0, `reason release pin current`.
  - `python scripts/gen_capability_matrix.py`: no change.
- An earlier run of the same gate command on `acde38e1` ran out of disk in the `tests` step
  (this builder's own pytest temp directories had filled the volume) and is not counted: its
  serial pass failed `tests/unit/scripts/test_fk_scale_infra.py::test_worker_emits_a_complete_record_at_small_n`
  with `No space left on device` in the log, and the baseline checkout could not be written.
  The directories were removed and the gate rerun from the start; the totals above are that
  rerun.
- Before the gate, a sweep of `tests/sentry` and the T1 paths on the uncommitted feature
  (Python 3.11, 4 workers) had one failure outside this slice's code:
  `tests/unit/transforms/test_grouped_series.py::TestGroupedSeriesMonotoneWalkValues::test_surrogate_group_label_does_not_crash`
  raised `Categorical categories must be unique` inside pandas `sort_values`. It passed alone,
  in three runs of `tests/unit/transforms` under xdist, on the slice start commit, and in the
  gate run above. It is recorded as an order-dependent flake (V2-LOG).

T1 paths (78: slice 2's 58 and 20 more):

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
tests/physical/test_shadow_generation.py
tests/secret_providers/test_keyed_columns.py
tests/unit/config
tests/unit/determinism/test_derive_vectors.py
tests/unit/execution/test_created_target_contract.py
tests/unit/execution/test_dgrn_windowed_date.py
tests/unit/execution/test_group_key_chunked.py
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

Added to slice 2's list: `tests/unit/transforms` and `tests/unit/expressions` (the date
primitive and the expression parser the resolver calls),
`tests/unit/test_numpy_generic_timedelta.py`, the
mask-side users of `windowed_date` (`tests/unit/execution/test_dgrn_windowed_date.py`,
`test_created_target_contract.py`, `test_group_key_chunked.py`,
`test_ooc_cross_row_decline_message.py`, `test_technique_class.py`, `test_text_mask_chunked.py`,
`tests/unit/test_de02_keyprovider.py`, `tests/unit/providers_v2/test_distribution_behavior_metadata.py`,
`tests/secret_providers/test_keyed_columns.py`), the native protocol and chunked-entry tests
that name the strategy, and `tests/physical/test_shadow_generation.py`. Every test file the
slice adds or changes is inside those paths or in `tests/sentry`. The `sentry` step runs all
of `tests/sentry` (the two new-or-rewritten registry sentries, `defects/test_generate_option_reads.py`,
the pool choke-point sentry, module-size, fan-out, table-kind, the seam sentry and the reason
registry, pin, fixture and policy sentries); the `layers` step runs
`tests/sentry/layout/test_import_layers.py`.

## Record commit

This record is added in a docs-only commit on top of `acde38e1`. On that commit the `ruff`,
`format` and `sentry` gate steps and the five checks above were run again; the results are in
the slice report. The `tests`, `mypy`, `layers` and `licenses` steps were not rerun on it: the
commit changes no Python file.

## Not run

- The full suite. `tests/unit/execution` beyond the probe, R3 and the listed mask-side files,
  the rest of `tests/parity`, `tests/acceptance` beyond the gate pins, `tests/integration`
  beyond `golden`, `tests/native` beyond the listed files, `tests/perf` and `testflight` were
  not run.
- The gate steps under Python 3.11 (the red-first run, the sweep and the mutations used 3.11,
  the gates 3.10). A rerun of `origin/main` (no branch step failed). The gates against a
  branch that holds `origin/main` `75b81efe`.
- The full gate on the docs-only commit that adds this record; see "Record commit".

## V2-LOG

- A raw config dict compiled with `compile_plan` does not pass through the `PipelineConfig`
  validators, as for E1's and E2's validate-time refusals. For such a config an `anchor_via`
  through a self-reference, or through a parent key that is not a non-null `sequence`, is not
  refused at validate. The run then refuses a repeated parent key with
  `runtime.parent_lookup_key_not_unique`; a lookup through a self-reference finds no finished
  parent table and is refused as `runtime.windowed_date_anchor_invalid`, which names the
  symptom, not the cause.
- The date and lookup refusals use the `runtime` catalog defaults (outcome `execution`, retry
  `unknown`), like E1's run-time refusals, although the remedy is a data or config repair.
- `WindowedDateError` and its subclasses do not round-trip through `pickle` (keyword-only
  constructor, like `GenerationError`). Nothing in the engine pickles an exception.
- `tests/sentry/_reason_baseline.py::FAMILY_NAMESPACE` has no `WindowedDateError` entry, so
  the namespace guard would not notice a `plan` code on that family. The catalog owner and the
  AT-14 cell pin the namespace today.
- `runtime.windowed_date_parent_anchor_null` and the lookup refusals stop at the first bad
  row and do not say how many rows are affected (a count is not allowed in the text).
- The streamed core's and the windowed read's own anchor resolution
  (`_admission.classify_windowed_date`, `_column_streams.WindowedDateStream`,
  `_window_admission._class_b_reason`) still reads `col["anchor"]` as a same-table name. All
  three are reached only by a column without `anchor_via` (the first declines it, the others
  never see it), so no edge is needed there, but they are not consumers of the resolver.
- The mask side has its own tables of sibling-reading strategy keys
  (`execution/_column_access.py`). They describe `provider_config`, not generate options, and
  the new sentry scans the generate side only.
- `plan/_checks_windowed_date.py` still builds its mask-kind message with the anchor name and
  the table's column list; `check_generate_columns` replaces that text with the fixed summary
  for every config it sees.
- An order-dependent pandas failure in
  `test_grouped_series.py::test_surrogate_group_label_does_not_crash` was seen once in a
  3.11 sweep (see "Gates").
- `tests/unit/generation/relational/test_routes.py` is 1,033 lines after this slice.
- `generation/synthesize.py` is at 598 lines. E3b adds a post-pass to it and will need to
  move code out to stay at or under 600. `config/_generate_options.py` is at 554; E3b's
  `from_table` and `via` registrations fit.
- `CODEMAP.md`'s Generation row and the docstring of `generation/relational/__init__.py` are
  ledger text; the package docstring still says callers import its names from the package,
  which the tree draw and the lookup do not (they are imported from their modules, as in E2).
