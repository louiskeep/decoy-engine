# CONFIG-EXPLICIT E2d build record

Status: record

Slice: roadmap row 22. `run_mask_pipeline_chunked`, `run_mask_chunked` and
`run_native_or_oracle_chunked` refuse an undeclared table or column
(`source_file.undeclared_table`, `source_file.undeclared_column`) before any chunk is profiled,
compiled or masked. Branch `feat/config-explicit-e2d-chunked`, based on main `c96e2855`
(E1 #409, E2a #441 and E2c #471 are ancestors). Not stacked: E2c is already on main.

## What changed in code

- `execution/_chunked.py`: `run_mask_pipeline_chunked` takes the required keyword-only
  `source_schema` and calls `prepare_declared_sources(DeclarationRequest.for_chunks(...))` after
  the subset refusal and before `_oracle_preflight`. File is 600 lines (was 590), so no census entry.
- `execution/_chunked_oracle.py`: `_oracle_preflight` and `_oracle_preflight_admitted` take a
  required `declared`. Right after the first chunk is pulled and the duplicate-field refusal, the
  rest of the stream goes through `declared.checked_stream(...)`: the first chunk is checked now,
  later chunks lazily before each masks. Nothing is profiled, compiled or wrapped before it.
- `execution/native/_chunked_entry.py` and `native/_dispatch.py`: `run_mask_chunked` and
  `run_native_or_oracle_chunked` prepare the config-level capability (`for_chunks(config, table,
  None)`) and pass it to `_run_chunked`, which requires `declared`. Signatures of the two public
  functions do not change.
- `_pipeline_auto_chunk._run_legacy` passes `source_schema=source.schema`;
  `MaskPipelineChunkedAdapter.run` takes and forwards `source_schema`.
- `profile/_declared_columns.py`: `DeclarationInput.within` and the `source_schema` columns kept by
  `PreparedDeclaredSources`, so a chunk column outside the caller's `source_schema` is refused with
  `source_file.undeclared_column`; `checked_stream(table, first, rest)` is the one place that turns
  a pulled first chunk into names (`arrow_columns`, which drops a stored pandas index).
- Docs: `docs/compatibility-contract.md` (section 3.4), `changelog.d/config-explicit-e2d.md`.
- Review round 1: a chunk column that the config lists but `source_schema` leaves out now says
  "add it to source_schema" (same code, still value-free); the comment in `_chunked_oracle.py`
  names `checked_stream` as the innermost wrapper; `scripts/audit/stage2d_native_dispatch.py`
  passes `source_schema`.

## Deviations from the handoff (re-anchored on main)

1. E2a's `PreparedDeclaredSources` did not keep the `source_schema` columns or check chunks
   against them (the handoff describes it as doing so). E2d adds `within` and the schema memory in
   `profile/_declared_columns.py`, a file outside the E2d Files list, because AT-E2d-2 needs it.
2. `checked_stream` is new. The handoff calls `arrow_columns` and `check_names` from
   `_chunked_oracle.py`; `arrow_columns` is internal to the profile package, so the oracle goes
   through one public method instead of a private import (defect class 6). The sentry looks for
   `checked_stream(` instead of `arrow_columns(first.schema)` plus `check_names(`.
3. Fixture churn is larger than the handoff estimate. Calls in 27 test files (not 11) needed a
   declaration, a `source_schema` or a retarget. The codemod `source-schema` covered 154 calls;
   hand rows and entry-by-value call sites were fixed by hand. A test moved below the gate only
   where its subject is how an unconfigured or undeclared column behaves (see "Retargets").
4. `tests/native/_b8_support.py` holds the below-gate helpers (`run_mask_chunked_below_gate`,
   `run_native_or_oracle_chunked_below_gate`, `run_mask_pipeline_chunked_below_gate`). All three
   come from one function: it binds the call against the public entry's own signature, then runs
   the real public entry with `prepare_declared_sources` patched in that entry's module to
   return a `below_gate` capability, for the duration of the eager call. No helper calls
   `_run_chunked` or re-implements a pre-gate step, so the file-access binding, subset refusal,
   thread check, progress accounting and signature are the production ones.
   `tests/_declared_schema.py` gains `chunked_entry_kwargs` for tests that loop over the entries.
5. The `declare-columns` probe was run over the failing set; its frames named only helper files
   and B8 tests, so no config literal was edited by the codemod. Tests whose config was merely
   incomplete (dup-name matrix, categorical weights) got hand-written passthrough declarations.

## Retargets (assertions unchanged)

Review round 1 (Dennis) found that most retargeted tests passed on the public entries. Every
test call was put back on the public entry and run there; a test stayed below the gate only when
the public entry refused it with `source_file.undeclared_column` (or `undeclared_table`) because
the test's subject is how an unconfigured or undeclared column behaves. As built, 148 references
to a below-gate helper sat in the tests. Now 90 remain: 5 are the wrappers in the two support
modules and 4 are selectors that choose a form per case; the rest sit in the tests below.

Prescribed by the handoff, unchanged: `run_one` and `run_pair` in `tests/native/_b8_support.py`.

Support modules. `_rev9_support.run_entry` / `run_public` and `_c8_i_support.run_native` /
`run_public_oracle` are public again. Each module also has a `*_below_gate` form, used by the
tests listed here. `_c8_i_support.run_oracle_leg` takes `below_gate=` so its leg matches the
native leg it is compared with.

Below the gate, whole test. Each test's chunks carry a column the config leaves out, so the
public entry raises `source_file.undeclared_column`:

- `test_c8_i_when_baseline`: `test_the_oracle_types_a_when_column_by_chunk_contents`.
- `test_c8_i_when_declines`: `test_a_non_admitted_strategy_or_config_declines_with_the_legacy_code`,
  `test_a_non_string_target_declines_with_the_legacy_code`,
  `test_a_numeric_reference_is_admitted_and_equals_the_chunked_oracle`,
  `test_a_raw_predicate_outside_the_grammar_is_rejected_by_both_entry_points`.
- `test_c8_i_when_mask`: `test_a_conversion_error_in_a_referenced_column_matches_the_oracle_leg`.
- `test_c8_i_when_parity`: `test_native_chunked_equals_the_oracle_leg_and_the_whole_frame_run`,
  `test_degenerate_chunks_keep_the_string_type_and_values`,
  `test_unselected_rows_keep_their_source_value_and_selected_rows_change`,
  `test_a_zero_match_chunk_makes_no_kernel_call_and_counts_nothing`,
  `test_a_zero_row_chunk_is_a_zero_match_chunk`,
  `test_an_all_zero_match_run_counts_no_kernel_call_at_all`.
- `test_chunked_entry_evidence`: `test_unconfigured_admitted_reports_planned_and_executed_native_backends`,
  `test_unconfigured_columns_are_not_listed_in_the_evidence` (the other nine `_run` users are public).
- `test_chunked_entry_rev9_adapter`: `test_adapter_frame_has_the_source_column_names_and_order`,
  `test_timing_records_have_the_oracles_structure`,
  `test_undeclared_output_columns_error_is_raised_at_the_first_next`,
  `test_warning_details_match_the_public_oracle_per_chunk`.
- `test_chunked_entry_rev9_nfkc`: `test_duplicate_source_column_names_raise_the_oracle_refusal`,
  `test_nfkc_literal_does_not_read_the_column`, `test_nfkc_spelled_predicate_is_rejected_at_compile`.
- `test_chunked_entry_rev9_read`: `test_predicate_the_tokenizer_rejects_is_rejected_at_compile_and_reads_every_column`,
  `test_string_literal_equal_to_a_column_name_keeps_that_column_carried`,
  `test_two_read_columns_the_error_names_the_one_that_holds_the_value`,
  `test_unread_passthrough_stays_carried_while_a_masked_column_is_read`.
- `test_chunked_entry_values_schema`: `test_unconfigured_passthrough_column_is_the_source_column`.
- `test_chunked_group_key_admission`: `test_an_unconfigured_sibling_cannot_be_named_by_group_by`;
  `test_a_self_anchor_declines_with_its_own_distinct_reason` compares against the oracle on the
  data it feeds `run_one`, so its oracle leg is below the gate too.
- `test_composite_admission`: `test_a_composite_with_a_when_still_generates_every_output`,
  `test_a_configured_stored_index_column_is_refused_before_any_chunk`,
  `test_a_read_when_column_keeps_a_big_int_passthrough_exact`,
  `test_a_read_when_column_still_raises_for_an_unrepresentable_value`,
  `test_caller_only_composite_fails_closed_before_any_output`,
  `test_error_policy_reroute_reason_names_only_the_unconfigured_columns`,
  `test_handler_written_columns_are_never_carried_or_schema_passthrough`,
  `test_row_error_chunk_reports_generated_values_for_written_columns`,
  `test_a_stored_index_change_between_chunks_is_schema_drift_on_both_routes` and
  `test_stored_index_drift_is_raised_by_both_public_entries` (the stored pandas index is a chunk
  column no config lists).
- `test_native_dispatch`: `test_uncovered_column_reroutes_to_oracle`,
  `test_second_chunk_introducing_an_unconfigured_column_fails_coded_after_first_chunk_consumed`.
- `test_unconfigured_passthrough`: `test_configured_column_missing_from_the_source_keeps_todays_reason`,
  `test_cross_check_downgrades_when_the_two_unconfigured_sets_disagree`,
  `test_later_all_null_chunk_of_a_type_with_no_cast_from_null`, `test_legacy_entry_keeps_the_veto`,
  `test_native_route_computes_the_warning_with_no_sink_given`,
  `test_native_route_refuses_through_the_projection_call_if_admitted_under_error`,
  `test_unset_policy_follows_the_release_phase`,
  `test_drift_in_an_unconfigured_column_raises_on_both_routes`,
  `test_leading_null_type_of_an_unconfigured_column_raises_on_both_routes`.
- `test_unconfigured_passthrough_read`: `test_an_out_of_grammar_reader_is_rejected_at_compile`,
  `test_genuine_reader_keeps_its_route_listing_and_coded_error`.
- `tests/security/test_fk_key_value_free_errors.py`:
  `test_chunked_read_passthrough_unrepresentable_value_is_value_free`.
- `tests/unit/execution/test_chunked_mutation_kills.py`:
  `test_unconfigured_error_policy_threaded_to_each_chunk` (the `note` column is unconfigured).

Below the gate, unconfigured case only (the configured case runs on the public entry):
`test_chunked_entry_rev9_read` `test_predicate_read_column_matches_the_public_oracle`,
`test_backtick_predicate_is_rejected_at_compile`, `test_unicode_predicate_names_are_rejected_at_compile`,
`test_refused_value_in_chunk_two_is_coded`; `test_chunked_entry_rev9_shapes`
`test_carried_passthrough_is_returned_exactly_on_both_routes`, `test_both_routes_yield_equal_tables`,
`test_public_oracle_behavior_is_unchanged`, `test_min_int64_is_yielded_itself_and_the_oracle_nulls_it`,
`test_row_error_chunk_sink_result_carries_the_source_column`; `test_chunked_entry_side_channels`
`test_compatibility_rejection_is_identical_on_both_entry_points[table_unknown]` (the unknown table
is refused by the gate first, with `source_file.undeclared_table`).

Below the gate for a different reason: `tests/unit/execution/test_chunked.py`
`TestChunkedCompatibility::test_unknown_table_rejected`. Through the public
`run_mask_pipeline_chunked(table="nope")` the call now raises `PipelineValidationError` with
`source_file.undeclared_table`, so `chunked_table_unknown` cannot be reached from any public
entry; AT-E2d-2 covers the public behaviour. The test keeps the compatibility check's own
refusal (defence in depth).

Back on the public entries (no edit beyond the import, and `source_schema` on the pipeline entry):
`test_c6b_i_text_mask_chunked`, `test_c8_iii_d2_native_positional_when`,
`test_chunked_categorical_admission`, `test_chunked_nondet_categorical_admission`,
`test_chunked_nondet_faker_admission`, `test_chunked_entry_rev7`, `test_chunked_entry_rev8`,
`test_chunked_entry_rev9_profile`, `test_column_access_surfaces` and every other test of the files
above that is not listed. The three entries take the same test as before.

Declared the missing columns or sources: `test_duplicate_field_names_matrix.py`,
`test_categorical_weight_admission_routes.py`, `test_bucket_perturb_chunked.py` and
`test_code_set_chunked.py` (the parent-only config also drops the child source).

Pass `source_schema` only: the adapter tests, `test_c8_iii_c_boundaries`,
`test_derived_route_matrix`, `test_subset_run_refused`, `test_engine_transforms_{exact,routes}`,
`tests/fileaccess/test_policy_routes.py`, `tests/secret_providers/*`.

## Red first

Stub step: `source_schema` accepted and ignored, `_oracle_preflight` not yet gated. The new
tests in `tests/unit/profile/test_declaration_gate.py` then gave 12 failed, 22 passed:
undeclared table (3 entries), undeclared `source_schema` (pipeline), undeclared first-chunk column
(3), column first appearing in chunk 3 (3), a chunk column outside `source_schema`, and the legacy
auto-chunk lane. `test_chunked_requires_source_schema` passes on the stub because the keyword is
already required there; on the E2c base it fails with `DID NOT RAISE` (the call succeeds).

## Mutations (head 87e2430b, each reverted)

| Mutation | Red test |
|---|---|
| drop the `checked_stream` call in `_oracle_preflight_admitted` | first-chunk and chunk-three cases (all entries), outside-`source_schema` case, both sentry walks |
| move that call below `compile_plan` | first-chunk cases (all entries), both sentry walks |
| give `source_schema` a default | `test_chunked_requires_source_schema` |
| drop the `within` intersection | `test_pipeline_entry_refuses_a_chunk_column_outside_source_schema` |
| `first.schema.names` instead of `arrow_columns` | `test_fixed_schema_capture_survives` |
| drop the `source_schema` forward in `MaskPipelineChunkedAdapter.run` | `test_chunked_adapter_forwards_source_schema` |
| wrap `chunks` in a generator before `_run_chunked` | `test_fixed_schema_capture_survives` |
| (review round 1) check each later chunk after the adapter masked it and before the yield, per-chunk wrap off | `test_chunked_refuses_a_column_that_first_appears_in_chunk_three[pipeline]`: `len(masked) == 2` fails with three masked calls (the old `len(got) <= 2` passed this mutant) |
| (review round 1) same, native loop, per-chunk wrap off | `[mask]` and `[native_or_oracle]`: the native drift guard refuses first, so the test sees `NativeChunkSchemaDriftError` instead of the declaration refusal |
| (review round 1) `within` text always the passthrough hint | `test_pipeline_entry_refuses_a_chunk_column_outside_source_schema` |

## Test results

`scripts/dev/gates.sh --keep-going --baseline` on a Linux VM at head e0c90dcf (selection of the
changed test files plus the always-on pins, not the full suite): env, ruff, format, mypy, layers
and licenses pass; tests 34880 passed, 0 failed (5302 skipped). The sentry step had 2 failures,
both the secret-material census, whose `_oracle_preflight_admitted` line number moved; fixed in
87e2430b by `scripts/secret_material_census.py --write` (two line numbers, verdicts kept) and
re-run green with `test_module_size`, `source_config` and the file-access entry-point sentry
(948 passed). The baseline rerun on main was not waited for.

Review round 1 (head after merging main `72b75489`): the 26 retargeted test files, 3060 passed and
142 skipped, the same count as built, with 90 helper references left instead of 148. The other
users of the three support modules plus `test_declaration_gate.py`: 1443 passed, 1076 skipped.
`tests/sentry` and `tests/unit/profile`: 5390 passed, 10 failed, all in
`tests/sentry/layout/test_import_layers.py` because import-linter is not in the vm-run venv (the
layers step of `gates.sh` owns that check).
