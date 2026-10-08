Status: record

# C8-iii-c build record: every `when:` predicate goes through the closed grammar

Plan: `docs/plans/2026-10-07-c8-iii-c-rawdict-when.md` (rev 3, Codex plan gate GO). Branch `feat/c8-iii-c-rawdict-when` off engine main `4a08f570`. Not pushed, not merged.

Rules consulted: 00-universal, testing, feature-dev, security (`/home/cam/dev-rules`).

## 1. Test inventory (plan test 6), written before any implementation or test change

### Method

Two passes over the tree at base, both before the first edit.

1. **Static.** An AST scan of every `tests/**/*.py` for a `when` dict value or `when=` keyword that is a string literal failing `parse_when`, plus string arguments to `_eval_predicate` and `predicate_names`. It finds literals only; predicates passed through a helper or a variable (`_when("s", expr)`, `make_config`) are invisible to it.
2. **Dynamic.** A throwaway pytest plugin (kept out of the repo) wrapped four funnels and logged any out-of-grammar `when` that reached them, with the test id: `ColumnSeed.__init__`, `_when_gate._eval_predicate`, `_column_access.predicate_names`, and the compile-time `when` check on the raw config. The whole `tests/` tree ran on Python 3.11 through `~/bin/pytest-one`. Baseline result of that run: 25368 passed, 172 skipped, 59 xfailed, 1 failed (the known `test_profile_gcs_source_via_mocked_client`, `No module named 'google'`). 3019 funnel hits across 237 distinct tests.

A test is **expected to fail after the change** when its predicate reached compile (raw config) or `ColumnSeed` plus the gate (187 tests, Appendix A). It is **expected to stay green unmodified** when the predicate reached only a helper such as `predicate_names`, `read_set` or `build_schema_rule` (50 tests, Appendix B), plus the helper-level static hits below. The post-change run must match this prediction; any other failure is a finding.

### Groups, what each protects, disposition

Dispositions as the plan defines them: (a) rewrite to a grammar predicate, (b) turn into a rejection test, (c) keep or relocate the lower-level defensive coverage unchanged.

| # | Test(s) | Out-of-grammar predicate | Protects | Disposition |
|---|---|---|---|---|
| 1 | `test_c8_i_when_auto_route.py::test_a_raw_dict_predicate_outside_the_grammar_stays_full_frame` x3 | `p.notnull()`, `p == s`, `p.isna() == False` | A raw-dict predicate outside the grammar keeps the table full-frame with `when_predicate_not_chunk_stable` and equal output | (b). The shape was the point. The route reason stays covered by the sibling `n > 3` test (grammar, numeric reference). |
| 2 | same file, `test_a_split_job_routes_an_admitted_when_table_chunked_and_the_other_full_frame` | `g.notnull()` on table B | Route: A dispatches chunked, B stays out of dispatch; `r` output equal to the split-off run on both tables | (a). B gets an int column `n` and `when: n > 3` (numeric reference, still not chunk-stable). Assert the same dispatch list, `native_admitted` on A and `r` parity. Selection on B is not a protected property (both runs share the predicate). |
| 3 | same file, `test_one_declined_when_column_keeps_the_whole_table_full_frame` | `p.notnull()` on a truncate column | Route: one declined `when:` column beside an admitted one keeps the whole table full-frame; outputs equal | (a). Keep mixed-route coverage with grammar predicates: `truncate(p)` with `p == 'x'` (admitted) and `redact(s)` with `p == 'x'`, which declines `when_predicate_reads_masked_column:s:p` because `p` runs first. Same assertions (mode full_frame, equal outputs). |
| 4 | `test_c8_i_when_declines.py::test_a_raw_predicate_outside_the_grammar_declines_and_equals_today` x4 | `p.notnull()`, `p == u`, `len(p) == 1`, `p + 'a' == 'xa'` | (i) `when_native_rejection` returns `when_predicate_outside_native_subset:s` for a seed outside the subset; (ii) end to end the table runs on the oracle and equals today | Split. (c) the admission half stays unchanged (it calls the helper directly). (b) the end-to-end half becomes a `PlanCompileError(when_outside_closed_grammar)` assertion on the entry points. |
| 5 | `test_chunked_entry_evidence.py::test_vetoed_tables_report_planned_native_and_executed_oracle[when_veto]` | `p + 0 > 2` | A vetoed table reports planned native backends, executed `pandas_oracle` on all chunks, JSON-safe evidence | (a). Veto from a grammar predicate that declines admission: `truncate(t)` gated by `r == 'x'` while `r` is masked first (`when_predicate_reads_masked_column:t:r`). Assertions on planned/executed backends and call counts unchanged; only the expected reason string changes. |
| 6 | `test_chunked_entry_rev9_catalogue.py::test_type_catalogue_read_column_matches_the_oracle` x108 | `x.notnull()` over the 54 pyarrow type factories, with and without null | The read column goes through pandas; output `s` and `x` equal the public oracle's; a refusal is the oracle's own exception | (a), predicate `x != 'q'`. A probe over all 108 type/null pairs shows the same public-oracle outcome table as `x.notnull()` (108 succeed, same 26 refusals by class). Selection differs on null rows (`!= 'q'` selects them), but the test compares entry to oracle on identical predicates and never asserts which rows are selected. The 26 refusal cases never reach compile and are untouched (Appendix B). |
| 7 | `test_chunked_entry_rev9_nfkc.py::test_nfkc_spelled_predicate_reads_the_column_like_the_oracle` x10 | `ｘ > 4`, `ﬁle > 4`, backtick forms, `ﬁle.notnull()` | A predicate spelled with NFKC-equivalent characters reads the same column as the oracle, so the masked column is never left unmasked | (b) plus (c). The grammar's `IDENT` is ASCII, so none of these spellings can reach evaluation: they become compile rejections. The read-set property is kept at helper level: add `predicate_names` / `read_set` assertions that the spelled forms resolve to the real name (the existing `test_string_literal_values_are_not_read_normalized_or_not` and `test_read_set_scans_predicates` stay). |
| 8 | `test_chunked_entry_rev9_profile.py::test_read_fk_key_keeps_the_refusal` x2 | `id.notnull()` | A read FK key of int64/uint64 keeps `FK_KEY_DTYPE_UNSUPPORTED_CODE` and the guard sees exactly `{id}` | (a), predicate `id != 0` (numeric key, evaluates). Assertions unchanged. |
| 9 | `test_chunked_entry_rev9_read.py`: `test_predicate_read_column_matches_the_public_oracle` x8 | `x > 4`, `` `x` > 4``, `` `my col` > 4`` (+ configured, forced oracle) | The read column is listed in `pandas_read_passthrough`, restored as the source column, output equals the oracle | (a) for `x > 4` (grammar already, no change). (b) plus (c) for the three backtick forms: compile rejection, and the read-set coverage of names with spaces stays in `test_read_set_scans_predicates`. |
| 10 | same file, refusal-in-chunk tests: `test_refused_value_in_chunk_two_is_coded` x4, `..._zero_...` (helper), `test_two_read_columns_the_error_names_the_one_that_holds_the_value`, `test_custom_adapter_*`, `test_stock_adapter_subclass_is_not_wrapped_either`, `test_strategy_handler_failure_is_the_same_object`, `test_masked_column_conversion_failure_is_not_wrapped` | `x.notnull()`, `a.notnull() and b.notnull()`, `x + 0 > 1` | A read column holding a value pandas refuses fails with the coded error at the right chunk, with the original exception as cause, unwrapped for custom adapters and handlers | (a): `x != 'q'` (and `a != 'q' and b != 'q'`). The refusal happens at conversion, before evaluation, so the chunk index, code and cause are unchanged. `x + 0 > 1` becomes `x > 1` over the same int column (read set and conversion path equal; the old form existed only to force the oracle leg, and the grammar route reads the same column). |
| 11 | same file, `test_unicode_predicate_names_are_read` x6, `test_predicate_the_tokenizer_rejects_reads_every_passthrough_column` | `café == 4`, `Δ == 4`, `名字 == 'b'`, `` `unterminated`` | Non-ASCII and unparsable predicate text still puts the right passthrough columns in the read set | (b) plus (c). Compile rejection for the end-to-end shape. The read-set claims already live in helper tests (`test_read_set_scans_predicates`, `test_read_set_tokenizer_failure_reads_every_passthrough_column`, `test_read_set_undecodable_string_literal_reads_every_passthrough_column`), which call `read_set` directly and stay unchanged. |
| 12 | same file, helper hits (14, Appendix B): `test_read_set_*`, `test_refused_value_in_chunk_zero_*`, `test_read_nested_passthrough_is_coded_at_chunk_zero` x3, `test_identical_profile_failures_are_attributed_in_source_order` | `tags.notnull()`, `t.notnull()`, `` `oops``, `s == f'{x}'`, unicode names | Conservative read sets; refusal at chunk zero from the profile peek | (c). Unchanged: they call `read_set` / `predicate_names` directly, or fail in the profile step before compile. Verify after the change. |
| 13 | `test_composite_admission.py::test_a_composite_with_an_unparsable_when_still_generates_every_output` x2 | `first_name != b'zz'` on a fixed composite | Composite outputs are still generated (all three columns) when reads are unknown; the row-error variant leaks nothing | (a) plus (c). The bytes form selects every row and marks reads unknown. Rewrite to `first_name != 'zz'`: no value equals `zz` on the fixture, so selection is every row, and the composite writes the same set. The reads-unknown conservative path keeps its direct coverage in `test_access_flags_for_each_reads_and_writes_combination` (helper, unchanged). |
| 14 | same file, `test_unparsable_when_keeps_a_big_int_passthrough_exact`, `..._still_raises_for_an_unrepresentable_value` | `r != b'zz'` | `2**53+1` nullable int is restored exact when the predicate makes every passthrough column a read column; an unrepresentable `time64` value in a read column still raises the coded error | (a) plus (c). The predicate must keep reading the passthrough columns: `r != 'zz' and big != 5` (selection: every row on the fixture; public oracle still float64), `r != 'zz' and t != 'zz'`. The reads-unknown route is covered at helper level by `test_reads_unknown_keeps_candidates_and_writes_unknown_empties_them` (unchanged, `read_set`/`plan_carry`). |
| 15 | same file, `test_unparsable_when_does_not_refuse_an_unrelated_stored_index`, `..._naming_a_named_stored_index_runs_on_the_reconstructed_index`, `..._naming_an_unnamed_stored_index_keeps_the_typed_error` | `s != b'zz'`, `id != b'zz'`, `__index_level_0__ != b'zz'` | Stored pandas index columns: an unrelated index is not refused, a named one is reconstructed, an unnamed one keeps `when_expression_error` | (a) for the first two: `s != 'zz'`, `id != 'zz'` (selection: all rows; `id` is the named index, read through the same path). (b) for the third: `__index_level_0__` is a dunder and is rejected by the grammar, so the typed error becomes the compile rejection. |
| 16 | `test_unconfigured_passthrough_read.py::test_genuine_reader_keeps_its_route_listing_and_coded_error` x12 | `x.notnull()`, `` `x`.notnull()``, `ｘ.notnull()`, each with and without a configured passthrough, plain and companion-missing | A genuine reader of a passthrough column keeps its listing in `pandas_read_passthrough` and the coded error at the bad chunk, with the expected reroute reason | (a) for `when_bare` (`x != 'q'`; reason becomes native-admitted or `crypto_extension_unavailable` when forced; assertions on listing and coded error unchanged, reroute expectation updated with the route it truly takes). (b) for the backtick and NFKC variants (compile rejection). |
| 17 | `test_c6c_i_text_redact_unified.py::test_an_unadmitted_when_predicate_declines_the_lane_and_equals_the_oracle` | `0 < p < 3` (chained comparison) | An unadmitted `when` declines the unified lane and equals the oracle | (b). The chained comparison was the point. The unified decline coverage with grammar predicates stays in `test_c8_ii_unified_when.py` (reads-masked and not-native cases). |
| 18 | `test_c8_ii_unified_when.py::test_4_a_predicate_outside_the_closed_grammar_declines` x2 | `s == c`, `s.notnull()` | Out-of-grammar predicate declines the unified lane | (b) compile rejection; the admission-level decline (`when_columns_admitted` is False) is kept as a direct helper assertion (plan test 3(b)). |
| 19 | `tests/security/test_when_eval_scope.py::TestNumexprScopeClamp` x4 | `@pd.compat.os.system(...) > 0`, `n.__class__ == 1`, `import os`, `unknown_module.attr == 1` | A hostile predicate never executes: scope walks, dunder access, statements and unknown modules are blocked | (b). Now blocked earlier: the backstop raises `when_outside_closed_grammar` before pandas is called. The clamp itself (`engine`, `local_dict`, `global_dict`) is asserted by the new eval-spy test (plan 4a) on an accepted predicate. |
| 20 | `test_when_gate_mutation_kills.py`: `test_local_dict_clamp_blocks_at_local_scope_walk`, `test_global_dict_clamp_blocks_at_global_scope_walk`, `test_pandas_not_boolean_attributes_strategy`; `test_when_predicate.py::test_when_non_bool_series_raises_when_expression_not_boolean` | `@strategy == 'redact'`, `@TYPE_CHECKING`, `n + 1` | The two scope clamps (local and global dict) and the `when_expression_not_boolean` branch with strategy attribution | (b) for the `@` and `n + 1` forms (grammar rejection with the typed code and strategy). The clamps move to the eval-spy test (4a). The not-boolean branch moves to 4a's injected non-boolean result on an accepted predicate, asserting code and strategy. No branch loses a test. |
| 21 | `unit/execution/test_bucket_perturb_chunked.py::TestAdmissionBoundary::test_when_auto_route_falls_back_to_full_frame` | `'2000' < d < '2030'` | Auto-chunk route falls back to full-frame for a bucket_perturb column with a `when` | (a), `d > '2000' and d < '2030'` (same selection and same read set). Route expectation re-verified at migration; if native admission now keeps it chunked, the assertion keeps its intent through a grammar predicate that still declines (record the change). |
| 22 | `unit/execution/test_categorical_seeded_nondet.py::TestHandlerFrameOrdinal::test_pipeline_when_and_unmatched_rows_are_reproducible` | `keep` (bare boolean-like column) | Seeded non-deterministic categorical is reproducible across runs when a `when` leaves some rows unmatched | (a), `keep == 1` if `keep` holds 0/1 ints (selection identical); verified against the fixture. |
| 23 | Helper-level static hits that never reach compile: `test_planner_mutation_kills.py` x2 (`x`, `y`), `test_dgrn_windowed_date.py::test_column_missing_name_key_falls_back_to_placeholder` and `test_text_mask_chunked.py::test_column_missing_name_key_falls_back_to_placeholder` (`1 == 1`), `test_chunked_bucket_perturb_admission.py::...rejected_with_its_exact_code[predicate_outside_grammar]` (`d.notnull()`), `test_c8_i_when_config.py` x2 (already grammar-rejection tests) | various | Gate and classifier branches called directly with raw config dicts | (c). Unchanged. |
| 24 | `test_c6c_i_text_redact_chunked.py::test_an_admitted_text_redact_config_with_an_unadmitted_when_is_not_string_pinned`, `test_chunked_date_shift_types_errors.py::...[when_outside_grammar]`, `test_column_access_surfaces.py::test_g3_mixed_surface_precedence`, `test_composite_admission.py` access-flag and reads-unknown tests | `0 < p < 3`, `d.notnull()`, `` `oops``, `s != b'zz'` | Schema-pin rule, access flags and reads-unknown fail-closed restoration for raw seeds | (c). Unchanged: the helpers take the raw entry and never compile. These are the retained-defense coverage the plan names (conservative read sets for backticks and f-strings, `2**53` under conservative reads). |

Every disposition (a) rewrite records, in section 4 once built, the selection, route, read set and dtype it preserved on its fixture and what asserts it. Disposition (b) tests keep the old test name, or a name that says "rejected", and assert the typed code. No test is deleted.


### Appendix A: tests where an out-of-grammar `when` reached compile or the gate (expected to fail after the change)

- `tests/native/test_c8_i_when_auto_route.py` (5 cases)
  - `test_a_raw_dict_predicate_outside_the_grammar_stays_full_frame` x3
  - `test_a_split_job_routes_an_admitted_when_table_chunked_and_the_other_full_frame`
  - `test_one_declined_when_column_keeps_the_whole_table_full_frame`
- `tests/native/test_c8_i_when_declines.py` (4 cases)
  - `test_a_raw_predicate_outside_the_grammar_declines_and_equals_today` x4
- `tests/native/test_chunked_entry_evidence.py` (1 cases)
  - `test_vetoed_tables_report_planned_native_and_executed_oracle`
- `tests/native/test_chunked_entry_rev9_catalogue.py` (108 cases)
  - `test_type_catalogue_read_column_matches_the_oracle` x108
- `tests/native/test_chunked_entry_rev9_nfkc.py` (10 cases)
  - `test_nfkc_spelled_predicate_reads_the_column_like_the_oracle` x10
- `tests/native/test_chunked_entry_rev9_profile.py` (2 cases)
  - `test_read_fk_key_keeps_the_refusal` x2
- `tests/native/test_chunked_entry_rev9_read.py` (25 cases)
  - `test_custom_adapter_converting_a_refused_passthrough_raises_raw`
  - `test_custom_adapter_failure_is_the_same_object`
  - `test_masked_column_conversion_failure_is_not_wrapped`
  - `test_predicate_read_column_matches_the_public_oracle` x8
  - `test_predicate_the_tokenizer_rejects_reads_every_passthrough_column`
  - `test_refused_value_in_chunk_two_is_coded` x4
  - `test_stock_adapter_subclass_is_not_wrapped_either`
  - `test_strategy_handler_failure_is_the_same_object`
  - `test_two_read_columns_the_error_names_the_one_that_holds_the_value`
  - `test_unicode_predicate_names_are_read` x6
- `tests/native/test_composite_admission.py` (7 cases)
  - `test_a_composite_with_an_unparsable_when_still_generates_every_output` x2
  - `test_unparsable_when_does_not_refuse_an_unrelated_stored_index`
  - `test_unparsable_when_keeps_a_big_int_passthrough_exact`
  - `test_unparsable_when_naming_a_named_stored_index_runs_on_the_reconstructed_index`
  - `test_unparsable_when_naming_an_unnamed_stored_index_keeps_the_typed_error`
  - `test_unparsable_when_still_raises_for_an_unrepresentable_value`
- `tests/native/test_unconfigured_passthrough_read.py` (12 cases)
  - `test_genuine_reader_keeps_its_route_listing_and_coded_error` x12
- `tests/physical/test_c6c_i_text_redact_unified.py` (1 cases)
  - `test_an_unadmitted_when_predicate_declines_the_lane_and_equals_the_oracle`
- `tests/physical/test_c8_ii_unified_when.py` (2 cases)
  - `test_4_a_predicate_outside_the_closed_grammar_declines` x2
- `tests/security/test_when_eval_scope.py` (4 cases)
  - `TestNumexprScopeClamp::test_when_at_var_scope_walk_blocked`
  - `TestNumexprScopeClamp::test_when_dunder_attribute_access_blocked`
  - `TestNumexprScopeClamp::test_when_import_statement_blocked`
  - `TestNumexprScopeClamp::test_when_unknown_name_raises_not_evaluated_via_python`
- `tests/unit/execution/test_bucket_perturb_chunked.py` (1 cases)
  - `TestAdmissionBoundary::test_when_auto_route_falls_back_to_full_frame`
- `tests/unit/execution/test_categorical_seeded_nondet.py` (1 cases)
  - `TestHandlerFrameOrdinal::test_pipeline_when_and_unmatched_rows_are_reproducible`
- `tests/unit/execution/test_when_gate_mutation_kills.py` (3 cases)
  - `test_global_dict_clamp_blocks_at_global_scope_walk`
  - `test_local_dict_clamp_blocks_at_local_scope_walk`
  - `test_pandas_not_boolean_attributes_strategy`
- `tests/unit/execution/test_when_predicate.py` (1 cases)
  - `TestErrorHandling::test_when_non_bool_series_raises_when_expression_not_boolean`

### Appendix B: tests where an out-of-grammar `when` reached a helper only (expected to stay green unmodified)

- `tests/native/test_c6c_i_text_redact_chunked.py` (1 cases)
  - `test_an_admitted_text_redact_config_with_an_unadmitted_when_is_not_string_pinned`
- `tests/native/test_chunked_date_shift_types_errors.py` (1 cases)
  - `test_a_non_admissible_date_shift_is_not_pinned`
- `tests/native/test_chunked_entry_rev9_catalogue.py` (26 cases)
  - `test_type_catalogue_read_column_matches_the_oracle` x26
- `tests/native/test_chunked_entry_rev9_nfkc.py` (1 cases)
  - `test_string_literal_values_are_not_read_normalized_or_not`
- `tests/native/test_chunked_entry_rev9_read.py` (14 cases)
  - `test_identical_profile_failures_are_attributed_in_source_order`
  - `test_read_nested_passthrough_is_coded_at_chunk_zero` x3
  - `test_read_set_scans_predicates` x7
  - `test_read_set_tokenizer_failure_reads_every_passthrough_column`
  - `test_read_set_undecodable_string_literal_reads_every_passthrough_column`
  - `test_refused_value_in_chunk_zero_is_coded_at_call_time`
- `tests/native/test_column_access_surfaces.py` (1 cases)
  - `test_g3_mixed_surface_precedence`
- `tests/native/test_composite_admission.py` (5 cases)
  - `test_a_configured_stored_index_column_is_refused_before_any_chunk` x3
  - `test_access_flags_for_each_reads_and_writes_combination`
  - `test_reads_unknown_keeps_candidates_and_writes_unknown_empties_them`
- `tests/native/test_unconfigured_passthrough_read.py` (1 cases)
  - `test_bytes_literal_in_a_predicate_reads_every_passthrough_column`
