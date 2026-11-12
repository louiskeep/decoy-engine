# Config-explicit E2b-f: fixture alignment build record

Status: record

Test-only slice, branch `feat/config-explicit-e2b-fixtures` from `origin/main` 7fc12e7f. No `src/` change.

## Heads

- E2b-f code commit: `2a15b51a` (on 7fc12e7f).
- Scratch tree for done-check 2: reverse-check commit `5bb379fc` with `2a15b51a` cherry-picked (clean, no merge of main), scratch head `d5282fc3`. Never pushed, removed after the run.

## Lines changed (24 added, 17 removed)

| File | Added | Removed |
|---|---|---|
| `tests/unit/execution/derived_visibility/test_duplicate_field_names_matrix.py` | 14 | 8 |
| `tests/_declared_schema.py` | 2 | 0 |
| `tests/native/test_composite_admission.py` | 2 | 2 |
| `tests/unit/profile/test_declared_columns.py` | 2 | 2 |
| `tests/acceptance/test_f06_parent_quarantine.py` | 1 | 1 |
| `tests/native/test_native_dispatch.py` | 1 | 1 |
| `tests/native/test_unconfigured_passthrough.py` | 1 | 1 |
| `tests/unit/profile/test_duplicate_header_readers.py` | 1 | 1 |
| `tests/parity/test_gap6_prefer_bounded_fk.py` | 0 | 1 |

## Done-check 1 (main plus fixtures, Linux VM)

The eight touched test files plus `tests/unit/execution/test_engine_transforms_routes.py`, in full: 437 passed, 12 skipped, 0 failed.

## Done-check 2 (5bb379fc plus 2a15b51a, Linux VM, the 189 ids)

108 passed, 81 failed. Every F id passes. The 81 failures are exactly the plan's other kinds:

| Kind | Count | Files |
|---|---|---|
| P, on purpose | 27 | `test_composite_admission.py` 12, `test_unconfigured_passthrough.py` 5, `test_chunked_entry_evidence.py` 2, `test_native_dispatch.py` 2, `test_engine_transforms_routes.py` 2 (`drop_column_missing`, `test_sequential_commits...`), `test_shadow_bindings.py` 1, `test_unified_slice_admission.py` 1, `test_engine_transforms_exact.py` 1, `test_declaration_gate.py` 1 |
| X, conflict | 45 | `test_dgrn_windowed_date.py` 25, `test_group_key_chunked.py` 19, `test_engine_transforms_routes.py::TestDerivedColumnMasking::test_strategy_sees_the_transform_frame_dtype` 1 |
| Lint | 9 | `tests/unit/scripts/test_dev_bootstrap_gates.py` 9 |

The two `test_engine_transforms_routes.py` P ids now fail on the reverse rule, not on pydantic errors, so the `declare_rest` fix works. No id fits none of the kinds.

Still-failing ids:

- `tests/native/test_chunked_entry_evidence.py::test_schema_drift_is_an_execution_error_and_a_decoy_error[missing-missing:['p']]`
- `tests/native/test_chunked_entry_evidence.py::test_vetoed_tables_report_planned_native_and_executed_oracle[schema_veto]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[direct-forced_oracle]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[direct-native_capable]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[direct-public_oracle]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[group_by_reference-forced_oracle]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[group_by_reference-native_capable]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[group_by_reference-public_oracle]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[passthrough_direct-forced_oracle]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[passthrough_direct-native_capable]`
- `tests/native/test_composite_admission.py::test_a_configured_stored_index_column_is_refused_before_any_chunk[passthrough_direct-public_oracle]`
- `tests/native/test_composite_admission.py::test_a_stored_index_change_between_chunks_is_schema_drift_on_both_routes[plain_then_index]`
- `tests/native/test_composite_admission.py::test_stored_index_drift_is_raised_by_both_public_entries[run_mask_chunked-plain_then_index]`
- `tests/native/test_composite_admission.py::test_stored_index_drift_is_raised_by_both_public_entries[run_mask_pipeline_chunked-plain_then_index]`
- `tests/native/test_native_dispatch.py::test_configured_column_missing_from_first_chunk_reroutes_and_reports_both_sides`
- `tests/native/test_native_dispatch.py::test_second_chunk_missing_a_configured_column_fails_coded_not_silently_dropped`
- `tests/native/test_unconfigured_passthrough.py::test_configured_column_missing_from_the_source_keeps_todays_reason[no_extra-error]`
- `tests/native/test_unconfigured_passthrough.py::test_configured_column_missing_from_the_source_keeps_todays_reason[no_extra-warn]`
- `tests/native/test_unconfigured_passthrough.py::test_configured_column_missing_from_the_source_keeps_todays_reason[with_extra-error]`
- `tests/native/test_unconfigured_passthrough.py::test_configured_column_missing_from_the_source_keeps_todays_reason[with_extra-warn]`
- `tests/native/test_unconfigured_passthrough.py::test_drift_in_an_unconfigured_column_raises_on_both_routes[dropped]`
- `tests/physical/test_shadow_bindings.py::test_faker_column_absent_from_source_declines_without_crashing`
- `tests/physical/test_unified_slice_admission.py::test_cheap_admission_declines_physical_named_index`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestBaseOffsetBoundary::test_chunk_range_exceeding_domain_raises_before_masking`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestBaseOffsetBoundary::test_max_domain_offset_admitted_by_chunked_route`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestBaseOffsetBoundary::test_small_positive_offset_matches_true_full_frame_suffix`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[early-1]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[early-500]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[early-7]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[late-1]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[late-500]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[late-7]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[uniform-1]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[uniform-500]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestEndToEndParity::test_chunked_equals_full_frame_across_matrix[uniform-7]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMaskPathStrictAnchorContract::test_bad_anchor_fails_closed_without_echoing_value[1650-01-01]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMaskPathStrictAnchorContract::test_bad_anchor_fails_closed_without_echoing_value[2024-01-01T00:00:00+02:00]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMaskPathStrictAnchorContract::test_bad_anchor_fails_closed_without_echoing_value[2300-01-01]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMaskPathStrictAnchorContract::test_invalid_calendar_anchor_traceback_excludes_value[2023-02-29]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMaskPathStrictAnchorContract::test_invalid_calendar_anchor_traceback_excludes_value[2024-02-30]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMaskPathStrictAnchorContract::test_invalid_calendar_anchor_traceback_excludes_value[2024-13-01]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMixedStrategyFixture::test_windowed_date_plus_hash_byte_identical[1]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMixedStrategyFixture::test_windowed_date_plus_hash_byte_identical[200]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestMixedStrategyFixture::test_windowed_date_plus_hash_byte_identical[9]`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestNullAnchorPropagation::test_all_null_tail_chunk_is_null_output`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestNullAnchorPropagation::test_manual_chunked_entrypoint_null_anchor_matches_full_frame`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestNullAnchorPropagation::test_null_anchor_is_null_output_on_both_routes`
- `tests/unit/execution/test_dgrn_windowed_date.py::TestWhenRejection::test_windowed_date_with_when_stays_full_frame_on_auto_route`
- `tests/unit/execution/test_engine_transforms_exact.py::TestDefectsThroughRoutes::test_stored_index_reference_without_transforms_fails_as_on_main`
- `tests/unit/execution/test_engine_transforms_routes.py::TestDerivedColumnMasking::test_strategy_sees_the_transform_frame_dtype`
- `tests/unit/execution/test_engine_transforms_routes.py::TestInvalidTransforms::test_full_frame_raises_and_writes_nothing[ops1-drop_column_missing]`
- `tests/unit/execution/test_engine_transforms_routes.py::TestInvalidTransforms::test_sequential_commits_and_publishes_nothing`
- `tests/unit/execution/test_group_key_chunked.py::TestCacheCollisionDecimalSignedZero::test_auto_route_selects_oracle_chunked_runner_never_invoked`
- `tests/unit/execution/test_group_key_chunked.py::TestCacheCollisionDecimalSignedZero::test_manual_entrypoint_raises_coded_error`
- `tests/unit/execution/test_group_key_chunked.py::TestCacheCollisionFloatSignedZero::test_auto_route_selects_oracle_chunked_runner_never_invoked`
- `tests/unit/execution/test_group_key_chunked.py::TestCacheCollisionFloatSignedZero::test_manual_entrypoint_raises_coded_error`
- `tests/unit/execution/test_group_key_chunked.py::TestEndToEndParity::test_chunked_equals_full_frame_across_chunk_sizes[1]`
- `tests/unit/execution/test_group_key_chunked.py::TestEndToEndParity::test_chunked_equals_full_frame_across_chunk_sizes[500]`
- `tests/unit/execution/test_group_key_chunked.py::TestEndToEndParity::test_chunked_equals_full_frame_across_chunk_sizes[7]`
- `tests/unit/execution/test_group_key_chunked.py::TestFkExclusion::test_group_by_sibling_is_fk_key_chunked_equals_full_frame`
- `tests/unit/execution/test_group_key_chunked.py::TestGroupByMaskedBeforeGroupKey::test_sibling_mask_runs_first_key_derives_from_masked_value`
- `tests/unit/execution/test_group_key_chunked.py::TestIntNullGroupBy::test_large_int_with_nulls_at_chunk_boundaries`
- `tests/unit/execution/test_group_key_chunked.py::TestLiteralPinnedKey::test_chunked_and_full_frame_both_equal_the_pinned_literal`
- `tests/unit/execution/test_group_key_chunked.py::TestNullSentinelPerDtype::test_nullable_int_group_by_with_nulls`
- `tests/unit/execution/test_group_key_chunked.py::TestNullSentinelPerDtype::test_string_group_by_with_nulls`
- `tests/unit/execution/test_group_key_chunked.py::TestOrderingRegressions::test_preceding_when_gated_sibling_mask_is_rejected`
- `tests/unit/execution/test_group_key_chunked.py::TestOrderingRegressions::test_safe_hash_mask_scheduled_before_is_admitted_chunked_equals_full_frame`
- `tests/unit/execution/test_group_key_chunked.py::TestOutputDtypeInvariance::test_all_null_chunk_vs_mixed_chunk_no_schema_mismatch`
- `tests/unit/execution/test_group_key_chunked.py::TestTextRedactMaskedGroupByRoutesToOracle::test_auto_route_falls_back_to_oracle[text_redact_pc0]`
- `tests/unit/execution/test_group_key_chunked.py::TestTextRedactMaskedGroupByRoutesToOracle::test_auto_route_falls_back_to_oracle[text_redact_pc1]`
- `tests/unit/execution/test_group_key_chunked.py::TestWhenRejection::test_auto_route_rejects_via_the_when_planner_gate`
- `tests/unit/profile/test_declaration_gate.py::test_pipeline_entry_refuses_a_chunk_column_outside_source_schema`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_fails_a_change_outside_the_old_code_globs_with_no_matching_test[_x0a_orphan.mk]`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_fails_a_change_outside_the_old_code_globs_with_no_matching_test[scripts/codeset_etl/_x0a_orphan.py]`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_fails_a_change_outside_the_old_code_globs_with_no_matching_test[scripts/codeset_etl/_x0a_orphan.sh]`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_fails_a_code_change_that_selects_no_tests`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_fails_a_code_file_renamed_into_docs_with_no_tests`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_fails_a_mixed_docs_and_code_change_with_no_tests`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_passes_a_change_made_only_of_documentation_paths`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_passes_a_docs_only_change_with_tests_not_applicable`
- `tests/unit/scripts/test_dev_bootstrap_gates.py::test_gates_treats_a_workflow_change_as_code`
