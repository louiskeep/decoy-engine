# B8 native unconfigured passthrough: build record

Status: record

Date: 2026-10-02. Plan: `docs/plans/2026-10-01-native-unconfigured-passthrough.md` (revision 4.1, Codex plan-gated; not edited by this record). Branch `feat/native-unconfigured-passthrough`, rebased onto engine main `64e44dac`, which already includes B2 (the dispatcher auto-chunk lane, PR #188). B7 has not landed, so the plan's B7 items are not applied. Builder: Sonnet. dennis and the Codex final gate have not run.

## Status in one paragraph

Plan revision 4.5.1 is built (Design 12.8, acceptance tests 21 to 24, tests 15, 16 and 20 brought to the plan text), on top of revisions 4.2 to 4.4. The branch was rebased onto origin/main `d2c56780` (B7, multi-table dispatch) and the B7 tests that encoded the `uncovered_columns` veto were rewritten as admitted cases (listed below). Every acceptance test, guard test and benchmark bar passes; the only failing test in the full suite is an environmental one that also fails on the base. One plan item could not be met as written (caller-only composite end to end, see Divergence 12). dennis round 3 and the Codex final gate have not run on this build.

## What shipped

Under the resolved `unconfigured_column_policy` `warn`, `run_mask_chunked` runs a table natively when the only obstacle was source columns the config does not cover. Each such column is carried as the source column; each chunk's `ExecutionResult.warnings` holds the oracle route's `undeclared_output_columns` warning, produced by calling `enforce_output_projection` on the native route. Under `error` the routing and the refusal are unchanged. Also built, per the plan: `execution/_column_access.py` (`ColumnAccess`, `column_access`, `SURFACE_DECLARATIONS`, `SIBLING_REFERENCE_KEYS`); `read_set` as the union of those declarations; the `unconfigured_policy` keyword on `plan_native_route` and `NativePreflight.unconfigured_passthrough`; the `unconfigured_set_mismatch:` cross-check; `real_type_rejection` building its resident table from the hash fields only; `_requirements._required_input_columns` iterating the shared constant. CHANGELOG entry under Unreleased and the compatibility contract §3.4 note are in.

## Commits

| SHA | What |
|---|---|
| `7c6d6133` | Tests first: acceptance tests, guard tests G1 to G6, the Design 9 rewrites and the B2 rewrites. No implementation. |
| `2d7d53af` | The implementation. |
| `e97ffba4` | CHANGELOG, compatibility note, the merge benchmark scripts. |
| `9d8182a4` | Two coverage tests (bytes-literal predicate, non-entry surfaces). |
| `11d913f4` | Six tests that kill the hand-mutant survivors, a docstring rewrap. |
| `cf606446` | First version of this record and the benchmark artifact. |
| `ab03cdf7`, `adbb34d0` | Plan revision 4.2 (coordinator). |
| tests commit after `adbb34d0` | Acceptance test 13 (red before the fix). |
| `feat(b8): drop stored pandas index fields` | Revision 4.2 implementation. |
| `91723379` | Tests first for revision 4.4: acceptance tests 14 to 20, G4/G5 corpus changes (red before). |
| `67d91920` | Design 12 implementation. |
| `913616c9` | `composite_provider_offenders` helper, optional-registry compatibility for two existing callers, `_chunked.py` census bump. |
| `8a14dbf5` | Four mutant-survivor tests. |
| `47985a4c` | Tests first for revision 4.5.1: acceptance tests 21 to 24, strengthened 15, 16, 20 (red before). |
| `fa1c52c5` | Design 12.8 implementation (access-flag split, required registry, public-entry drift, registry in route evidence). |
| `4f68a3e5` | Nested-child writes unit pin, CHANGELOG. |
| `1d9075e6` | B7 rebase fixes: multi-table contract tests, B7 benchmark script, `_chunked.py` census ratchet. |
| the commit holding this update | Record update for 4.5.1 and the B7 rebase. |

## Red before

At `7c6d6133` (tests only), in the companion venv: 291 failed, 1011 passed across the eleven test files the commit touches, plus a collection error for `tests/native/test_column_access_surfaces.py` because `decoy_engine.execution._column_access` did not exist. Per file: `test_unconfigured_passthrough_types.py` 135 failed (every one asserts native admission and gets `reroute_reason="uncovered_columns:['x']..."`; test 3 and test 4); `test_chunked_entry_rev9_shapes.py` 70; `test_chunked_entry_rev9_read.py` 30; `test_unconfigured_passthrough.py` 38; `test_unconfigured_passthrough_read.py` 8; `test_chunked_entry_rev9_nfkc.py` 3; `test_chunked_entry_rev9_adapter.py` 2; `test_auto_chunk_dispatcher.py` 2; one each in `test_chunked_entry_evidence.py`, `test_chunked_entry_values_schema.py` and `test_auto_chunk_output_contract.py`. The failure reasons checked by reading the output: the `uncovered_columns` veto (acceptance 1, 2, 3, 4, 9 and the rewrites), missing attributes `_chunked_entry.enforce_output_projection` and `known_output_columns` (the warning-call and cross-check tests), `ArrowNotImplementedError` at call time for `dense_union`, `sparse_union` and `run_end_encoded<string_view>` (acceptance 5), the false-positive read set (acceptance 6a: `chunked_passthrough_value_unrepresentable` for a column named `redact` or `person_first_name`), and the missing composite read-set entries (6c: `{'email','last_name'} <= set()`; the `composite_custom` bundle case already passes today because the broad scan catches it, as the plan says). Guard tests that pin unchanged behavior passed before and after (genuine readers in 6b, `real_type_rejection` reasons, the policy-keyword default).

## Final test results

Command, from the worktree, with the companion venv and the worktree `src` first on `PYTHONPATH`:

`PYTHONPATH=$PWD/src:$PWD /home/cam/bin/pytest-one /home/cam/.cache/decoy-native-venv/bin/python tests -q --tb=short -p no:randomly`

Final run, after the revision 4.2 implementation: 1 failed, 19256 passed, 142 skipped, 21 deselected, 59 xfailed, 60 warnings in 1549.49s (25:49). The one failure is `tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client` (`No module named 'google'` in the companion venv); it fails the same way on the pre-B8 `src`. The six B2 index tests in `test_auto_chunk_output_contract.py` pass unedited. The run before the 4.2 fix (at `11d913f4`) was 7 failed, 19245 passed: those six plus the same `google` test.

## Lint and types

`ruff format --check src tests testflight scripts` and `ruff check src tests testflight scripts` clean (ruff 0.15.22 from the main repo venv; CI pins 0.15.14). `mypy src/decoy_engine testflight`: no issues in 502 source files (mypy 2.3.0; CI pins 2.1.0). Module-size ratchets unchanged: `_requirements.py` stays at 648 lines, `_dispatch.py` 488 to 495 and `_chunked_entry.py` 453 to 484, all under the 600 goal; `_column_access.py` is a new module under the cap.

## Coverage

Command: the eight B8-relevant test files (472 passed) under `coverage run --branch` (coverage 7.16.2 from a scratch `--target` directory outside the repo), re-measured after revision 4.2:

| Unit | Line / branch |
|---|---|
| `_column_access.py` (new) | 100% line, 100% branch (0 missed; lines 89 and 173 are now covered) |
| `_chunked_carry.py` | 91%; misses at 92-94, 107-111, 137-139 are unchanged code |
| `native/_dispatch.py` | 90%; every B8 and 4.2 line covered, misses are unchanged faker and probe branches |
| `native/_chunked_entry.py` | 97%; misses 292-293 and 337 are unchanged |
| `native/_chunk_masking.py` | 89%; B8 and 4.2 lines covered |
| `native/_real_type_admission.py` | 86%; unchanged misses |
| `native/_requirements.py` | 85%; unchanged misses |

## Mutation

mutmut was not run: it is not installed in either venv and `scripts/tq_mutate.py` documents that it misgrades the pandas-heavy execution suites. Instead 42 mutants plus one sanity mutant were applied by hand with a small harness (`run_mutants.py`, kept in the session scratchpad), each in a scratch copy of `src` run against the B8 test files with `-x`. Units covered: the admission change in `plan_native_route` (5), the `real_type_rejection` resident schema (2), the unconfigured branch of `_mask_chunk_native` (2), the cross-check and the native warnings call in `_chunked_entry` (7), `predicate_names`, `_siblings`, every non-trivial `SURFACE_DECLARATIONS` entry including each composite branch, `column_access`, `touched_columns` (21), and `read_set` (2).

Pass 1: 37 killed, 6 survived. Pass 2, after `11d913f4`: all 43 killed.

| Survivor | Why it survived | Resolution |
|---|---|---|
| M15 native warnings call uses policy `warn` instead of `state.projection_policy` | no admitted table can reach the call under `error` | new backstop test: admission is forced to admit under `error`, the call must raise |
| M27 composite writes drop `coherent_with` | the names are normally inside the bundle or the canonical outputs | unit test with a coherent column outside the bundle |
| M28 composite reads nothing | `read_set` counts reads and writes alike, and the key column is also an output | unit test on `column_access(...).reads` |
| M35 unknown registry composite reads nothing | unreachable with the default registry | test that patches `_is_composite` |
| M36 own name not excluded | only matters for a passthrough entry that references itself | unit test |
| M39 NFKC match on the candidate name dropped | pandas cannot resolve a non-NFKC column name, so keeping the branch over-approximates (as before B8) | test pins the conservative behavior |

An earlier harness attempt was discarded: pytest's `pythonpath = ["src"]` put the unmutated worktree `src` ahead of the mutated copy, so mutant M01 survived. The harness was fixed to run from a scratch tree that holds the mutated `src` and symlinks to `tests` and `scripts`, and the sanity mutant (module cannot import) was added and is killed.

## Benchmark

Revision 4.2 adds one `frozenset` membership check per column per chunk to `_mask_chunk_native` and one `stored_index_fields` call at admission and cross-check. The hot path did not change materially, so the benchmark below was not re-run. The numbers are from the revision 4.1 build.

Run ID `B8-BENCH-2026-10-02-1M` (the ledger row in the sprint and testing ledger lives outside this repository and has not been added). Raw JSON: `docs/records/b8-bench-2026-10-02/b8-bench-1m.json`. Driver `scripts/bench-unconfigured-passthrough/bench_unconfigured_passthrough.py`, worker `bench_worker_unconfigured_passthrough.py`.

Setup as the plan's Design 11: 1,000,000 rows, 20 chunks of 50,000, four configured string columns and eight unconfigured columns (`Table.nbytes` 140,500,566; 80,000,000 with the four configured columns only), one discarded warmup round and seven measured rounds in a seeded shuffled order (seed 20261001; all seven orders are in the JSON), a fresh subprocess per trial under `flock /home/cam/.cache/pytest-one.lock`, `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`. Intel Core i5-7500, Python 3.11.15, pyarrow 25.0.1, pandas 2.3.3, engine 0.7.0, companion 0.1.0. The "before" side is the `src` tree at `49a8a5a7` (identical to main `64e44dac`, since only plan documents differ), used instead of the plan's `0bb196fa` because B2 landed in between and the pre-B8 `run_mask_chunked` is what this merge replaces.

| Config | Side | Unconfigured | Threads | p50 s | max s | peak RSS MiB |
|---|---|---|---|---|---|---|
| a1 | before | 8 | 1 | 10.361 | 10.624 | 493 |
| a4 | before | 8 | 4 | 10.530 | 11.044 | 498 |
| b1 | after | 8 | 1 | 1.519 | 1.573 | 471 |
| b4 | after | 8 | 4 | 0.810 | 0.849 | 475 |
| d_before | before | 0 | 1 | 1.488 | 1.585 | 395 |
| d_after | after | 0 | 1 | 1.465 | 1.501 | 395 |

All bars met: b1 beats a1 and b4 beats a4 at p50 and at max; d_after p50 is 0.98 times d_before p50 (bar: at most 1.05); every b1, b4 and d_after trial stayed under the 2 GiB ceiling (worst 475 MiB). Correctness held for every measured trial: output `Table.equals(check_metadata=True)` against a1's output (d configurations against d_before's), equal per-chunk warnings, `native_admitted` true with `reroute_reason` null for b1 and b4, an `uncovered_columns` reroute for a1 and a4. Configuration (e), `unconfigured_column_policy: error`, took the oracle route and raised `undeclared_output_columns` before any chunk. Measured p50 speedup on this workload: 6.8x at one thread (10.361 s to 1.519 s), 13x with four threads.

## Existing assertions rewritten

Design 9, each in the way the plan states:

- `test_chunked_entry_values_schema.py::test_unconfigured_passthrough_column_is_the_source_column`: parametrized `warn` (native, reason `None`, exact values and types kept) and `error` (oracle reason, `undeclared_output_columns` at the first `next()`).
- `test_chunked_entry_evidence.py`: case `schema_veto` is now config `r`, `t`, `q` over source `r`, `t`, reason containing `missing_configured_columns:['q']`. The new `unconfigured_admitted` check is a separate test, `test_unconfigured_admitted_reports_planned_and_executed_native_backends`, because the veto test's body asserts the oracle route.
- `test_chunked_entry_rev9_shapes.py::_expect_route` returns `route == "native"`.
- `test_chunked_entry_rev9_adapter.py::test_adapter_frame_has_the_source_column_names_and_order` forces the oracle with `companion_missing` and a hash column and asserts `crypto_extension_unavailable`; `test_timing_records_have_the_oracles_structure` takes `configured`, `unconfigured` and `forced_oracle`.
- `test_chunked_entry_rev9_read.py`: `test_read_set_scans_predicates` rows changed and added; `test_string_literal_equal_to_a_column_name_reads_that_column` renamed `..._keeps_that_column_carried`; `test_read_set_matches_string_values_in_other_columns_entries` renamed `test_read_set_matches_sibling_reference_fields_only`, with the declared-reader and composite cases added as new tests beside it.
- `test_chunked_entry_rev9_nfkc.py`: case `fullwidth_in_literal` removed from `_CASES` and moved to `test_nfkc_literal_does_not_read_the_column`; `test_string_literal_values_are_normalized_too` renamed `test_string_literal_values_are_not_read_normalized_or_not`.
- `tests/sentry/test_physical_seam_disconnection.py`: `_column_access.py` added to the permitted-edits list with a justification, as Design 10 says.

B2 rewrites (Design 8), exact names:

- `tests/unit/execution/test_auto_chunk_dispatcher.py::test_unconfigured_column_reroutes_with_uncovered_columns` is now `test_unconfigured_column_is_admitted_to_the_native_route`, parametrized on the companion: present gives `native_admitted` true, reason `None`, `h` on `rust_companion` and `r` on `arrow_python`, and no entry for `extra`; absent gives `crypto_extension_unavailable`. The `check_contract` identity assertion against the legacy lane is kept. A stale comment in `test_chunked_route_evidence_is_json_safe_deterministic_and_complete` was reworded; no assertion changed.
- `tests/unit/execution/test_auto_chunk_output_contract.py::test_passthrough_field_shapes[*-unconfigured_column]`: the configured and unconfigured variants now share one route assertion (native with the companion, oracle without). The `check_contract` and field assertions are unchanged.

## Revision 4.3 and 4.4 (dennis NO-GO remediation)

The dennis gate on the 4.2 build returned NO-GO. The blocker was a leak already on main: `check_chunked_compatibility` classified columns by strategy string, dispatch classifies by provider, so a composite provider on `redact`, `hash` or `passthrough` passed admission and `run_mask_chunked` returned the other bundle columns as source values.

Red before, at `91723379` (current code): `run_mask_chunked` with `composite_name_email` on `redact`, on `hash` and on `passthrough` returned `last_name` and `email` equal to the source (`SECRET-L1..3`, `S1..3`), route `oracle` with reason `non_scalar_node:composite:first_name`. The new test file `test_composite_admission.py` failed at collection because `handler_written_columns` and the registry-taking signatures did not exist.

Implemented: 12.1 `check_chunked_compatibility(config, *, table, registry)` refuses any provider that is a composite under the registry (`composite_provider_offenders` in `_column_access.py`), `_oracle_preflight` resolves the registry first, the planner passes its own; 12.2 `handler_written_columns` feeds `passthrough_columns` and `build_schema_rule`; 12.3 the registry is threaded through `column_access`, `read_set`, `touched_columns`, `plan_carry`, `passthrough_columns`, `build_schema_rule`, and a composite entry's `when:` names count as read; 12.4 `reject_config_references_stored_index` runs in `_oracle_preflight` right after the first-chunk pull; 12.5 `validate_chunk_schema` raises `native_chunk_schema_drift` when the stored-index set differs from chunk 0; 12.6 CHANGELOG (Security entry, stored-index entry, reroute-reason note, `reattach` and `normalize_chunk` sentence) and this record; 12.7 G5 corpus adds a composite provider on each of redact, hash and passthrough plus a `derived` with `when:`, and G4 pins the full call inventory of `PandasExecutionAdapter.run`. Acceptance test 20 runs the whole G5 corpus through both chunked entries.

Four existing B8-authored assertions moved to the new contract, each forced by the plan text and none weakened: `test_composite_outputs_are_never_carried` no longer expects the written columns in `carry.read` (12.2 removes them from the passthrough candidates altogether; they are still not carried and the reattached output is still the generated value); `test_composite_route_and_reason_on_run_mask_chunked_are_unchanged[*-faker]` now expects `strategy_not_chunk_safe` instead of `composite_requires_bundle_path` (12.1 refuses by provider before the pool warm-up); the B8 tests that call `column_access`, `read_set` and `plan_carry` pass the registry.

Test results after 4.4: 1 failed, 19922 passed, 168 skipped, 21 deselected, 59 xfailed in 26:46 (`PYTHONPATH=$PWD/src:$PWD /home/cam/bin/pytest-one /home/cam/.cache/decoy-native-venv/bin/python tests -q --tb=short -p no:randomly`). The one failure is the `google` import test (same on the base).

Coverage (branch, 13 test files, 820 passed): `_column_access.py` 100% line and branch, `native/_chunk_schema.py` 100%, `_pipeline_auto_chunk.py` 100%, `native/_chunked_schema_rule.py` 97% (misses 93-94 are the unchanged cast-error branch), `_chunked_oracle.py` 96%, `_chunked_carry.py` 92%, `_chunked.py` 86%, `_planner.py` 89%, `_transforms.py` 30% (whole module; the new `reject_config_references_stored_index` is fully covered). No uncovered line is one this revision changed.

Mutation (hand mutants, harness as above): 13 mutants on the Design 12 lines (M50 to M62). Nine were killed at once; four survived (M52 undeclarable carry fallback, M55 `everything` stored-index refusal, M58 own-name retention, M61 registry not reaching the compatibility check). Four new tests in `8a14dbf5` kill them; all 13 are killed.

Hot path: the benchmark was not re-run for 4.3/4.4. The only per-chunk additions are a stored-index set comparison in `validate_chunk_schema` and nothing in the masking loop; admission-time work is a set of `provider_is_composite` lookups per column.

## Revision 4.5.1 (dennis round 2)

dennis round 2 closed B1, H1, H2, M1, M3 and L1 and returned NO-GO on one HIGH that revision 4.4's own wording caused: `ColumnAccess.everything` meant both "reads unknown" and "writes unknown", so an unparsable `when:` emptied the carry and schema-rule passthrough sets and refused unreferenced stored-index fields.

Red before, at `47985a4c` (code of `4031bce4`): test 21(a) returned the unconfigured int64 `2^53+1` as `float64` (`big: double [9.007199254740992e+15, null, 3]`) behind `when: "r != b'zz'"`; 21(b) raised raw `ArrowInvalid` instead of `chunked_passthrough_value_unrepresentable`; 21(c) and 21(d) raised `config_references_stored_index` for fields the config never names; test 22 did not raise `TypeError`; test 23 showed `run_mask_pipeline_chunked` not detecting stored-index drift and a rebound composite name reported as `non_scalar_node:composite:s`; test 24 failed on the missing flags. 14 of the new or strengthened tests failed.

Implemented: `ColumnAccess` carries `reads_unknown` and `writes_unknown` (no `everything`); an unparsable `when:` or `derived` expression is an unknown read, an undeclared strategy (scalar, or the child of a `nested`) or a malformed bundle is an unknown write; `handler_written_columns` returns `None` only on an unknown write, `touched_columns` returns `None` only on an unknown read, so an unparsable predicate leaves every passthrough candidate in the carry and schema rule while `read_set` sends it through pandas and the schema rule restores the exact source field and value; `reject_config_references_stored_index` refuses only on a positive reference (a configured name or a declared read or write); `registry` is a required keyword on `check_chunked_compatibility`, `composite_provider_offenders` and `_legacy_route_evidence` (64 direct test call sites got `registry=get_default_registry()`); `stored_index_guard` in `_oracle_preflight` makes `run_mask_pipeline_chunked` raise `native_chunk_schema_drift` at the changed chunk; `plan_native_route`, `compile_native_plan` and `plan_column_backends` take the run registry and `_run_chunked` passes `state.registry`; CHANGELOG sentences corrected.

Test changes forced by the new contract (none weakened): `test_malformed_composite_bundle_reads_every_passthrough_column` became `..._leaves_no_passthrough_column` (an unresolvable bundle is an unknown write, so `handler_written_columns` is `None` and no candidate remains); `test_an_undeclarable_entry_refuses_any_stored_index_field` became `test_only_a_positive_reference_refuses_a_stored_index_field`; the `.everything` assertions use the two flags; test 20 now asserts Arrow types as well as values, with no `pytest.skip` (no corpus case ever hit it); test 16 compares `run_mask_chunked` with `run_mask_pipeline_chunked` under the rebound registry; test 15 adds the row-error path.

Mutation: nine mutants on the 4.5.1 lines (M70 to M78) plus M79 and M80 for the broken-`_union` check. The mutant that drops all or the composite's writes in `_union` (M79, M80) is killed by test 24(b), as the plan requires. M70 (keep only the first operand's writes) survived because only `nested` gives the second operand writes; a unit test for a `nested` joint_mask child kills it. All eleven are killed.

Coverage (branch, 11 test files, 881 passed): `_column_access.py` 100% line and branch, `_pipeline_auto_chunk.py` 100%, `native/_chunk_schema.py` 99% (the one partial branch, metadata differs but the stored-index set does not, is now covered by a test added after the measurement), `_chunked_schema_rule.py` 97%, `_chunked_oracle.py` 96%, `native/_dispatch.py` 92%, `native/_chunked_evidence.py` 92%, `_chunked_carry.py` 90%. The remaining misses are unchanged lines.

## B7 rebase (origin/main `d2c56780`)

The rebase conflicted only in CHANGELOG (both sides added entries at the top; both kept, B7 first). Two B7 tests failed after the rebase and no other: `tests/unit/execution/test_multi_table_contract.py::test_split_job_output_contract[unconfigured_column_warn_policy-*]` (four parameter sets) and `::test_an_unconfigured_column_reroutes_the_table_with_uncovered_columns`. Rewritten as Design 8 and 9 describe: `_expected_route` no longer treats `job.uncovered` specially (the companion decides, as for every other case), and the second test is now `test_an_unconfigured_column_is_admitted_to_the_native_route`, parametrized on the companion, asserting native admission with reason `None` when present and `crypto_extension_unavailable` (no `uncovered_columns`) when absent. B7 section 11 configuration (d) is the `extra` variant of `scripts/bench-multi-table/`: its correctness check no longer expects an `uncovered_columns` reroute for `d1` (every configuration now expects native admission), and both script docstrings say the variant no longer measures a rerouted job; B8's own benchmark covers the unconfigured case. The B7 benchmark was not re-run.

Final full suite after the rebase and 4.5.1: 1 failed, 20263 passed, 168 skipped, 21 deselected, 59 xfailed in 27:16; the one failure is the `google` import test (same on the base). One further test was added afterwards (the unrelated-metadata drift case) and passes.

## Divergence

1. **Stored pandas index columns (resolved by plan revision 4.2).** Resolution: option (b). `plan_native_route` excludes `stored_index_fields(first_schema)` from the uncovered set, `_mask_chunk_native` skips those names (`stored_index=`), and the cross-check subtracts them. Acceptance test 13 was written first and was red on the 4.1 code (the native chunk carried the index field); four hand mutants on the new lines (M43 to M46) are killed. Original finding: Six existing B2 tests fail, none edited: `test_auto_chunk_output_contract.py::test_pandas_origin_sources_take_the_dispatcher_lane[RangeIndex_start_and_step|named_RangeIndex-parquet_read_back|from_pandas]` (four) and `test_stored_non_range_index_column_is_absent_on_every_lane[parquet_read_back|from_pandas]` (two). A source from `pa.Table.from_pandas(df)` with a non-range index, or with `preserve_index=True`, holds an extra column (`__index_level_0__`, or the index name) and pandas schema metadata that names it. Before B8 that column counted as uncovered, so the table took the oracle route, where the stock adapter's `to_pandas` consumed the column as the index and `from_pandas(preserve_index=False)` dropped it. After B8 the column is "unconfigured", so the native route yields it as an output column. A direct run confirms it: for `df` with a string index, the native route yields `['r', 't', '__index_level_0__']` and the forced oracle route yields `['r', 't']`. This breaks plan guarantee 2 and B2's guarantee 3(a). Options for a plan patch: (a) keep the veto, with a coded reason such as `pandas_index_column:<name>`, when the first chunk's schema metadata (`b"pandas"`, key `index_columns`) names a stored index column; (b) make the native route drop those columns the way the oracle route does. Option (a) is smaller and leaves the stored-index handling to roadmap item PARQUET-INDEX. Option (b) was chosen and implemented.
2. The plan's acceptance 6(a) names a column after a provider as `first_name` beside a first-name Faker column. The Faker column's provider string is `person_first_name`, so a column named `first_name` never collided with the old scan. The test uses `person_first_name`, the real provider value.
3. Acceptance 6(b) lists the sibling strategies "unconfigured and configured". A `group_by`, `anchor` or similar reference must name a configured column (the plan compiler rejects it otherwise), so the sibling cases are configured-only. The `when:` cases cover both. The sibling cases use shapes valid for the strategy (a dictionary with a null for `group_by`, a far `date32` for `anchor`) rather than the `time64[ns]` shape, which the strategies reject on valid values.
4. The "before" benchmark side is `49a8a5a7` (equal to main `64e44dac`), not `0bb196fa`; see Benchmark.
5. `column_access` treats an unknown strategy name like a strategy with no sibling field (generic declaration, `coherent_with` and the sibling keys only). Such a config fails plan compilation before it can run; the choice lets the renamed read test keep its original strategy name `x`.
6. (Corrected in revision 4.3.) The earlier text said every composite was refused on `run_mask_chunked` and that `column_access` could use the default registry. Both were wrong: a composite provider on `redact`, `hash` or `passthrough` was admitted and leaked. `column_access` and the carry and schema-rule paths now take the run's registry, and both chunked entries refuse composite providers by provider (revision 4.3 section above).
7. `SURFACE_DECLARATIONS` keys are `scalar:<strategy>`, `composite:<provider>` and `surface:<name>`; the plan says only "keyed by surface name". G1 and G2 read the `scalar:` and `composite:` subsets.
8. The read set now also compares a candidate's NFKC form with the touched names (`c in touched or nfkc(c) in touched`); the old scan did the same for predicate names. This over-approximates, as before.

9. `check_chunked_compatibility` takes `registry` as an optional keyword (default registry when omitted), not the required keyword Design 12.1 names, because about 150 existing test call sites call it without one and may not be edited. Both production callers (`_oracle_preflight` and the planner) pass the registry explicitly, and the new tests assert the run registry reaches it. Likewise `_pipeline_auto_chunk._legacy_route_evidence` keeps an optional `registry` for an existing unit test. Every other function named in 12.3 takes a required registry.
10. The module-size census entry for `_chunked.py` moved from 608 to 614 (dense exception, under the 700 ceiling) for the composite refusal, after moving the refusal logic into `composite_provider_offenders`.
11. Acceptance 6(c)(iii) as written ("`c` in `read`") no longer holds because Design 12.2 removes written columns from the passthrough candidates; the test asserts they are not carried and not listed as read, and (iv) is unchanged.

12. Plan test 15 (and 24) asks for a caller-only custom composite to run end to end through `run_mask_chunked(registry=reg)`. That cannot complete: `compile_plan`, the composite wiring check, the seed envelope and `compile_native_plan` all resolve providers through the default registry, so a provider only the caller's registry knows fails with `unknown_provider` before any output, even with 12.1 patched out (`composite_wiring_inconsistent` for a coherent group). The test therefore asserts the fail-closed outcome (coded `PlanCompileError`, no sink append, no source value returned), on the normal and row-error variants; the registry-bound declarations for such a provider (`writes_unknown`, `handler_written_columns` is `None`, empty passthrough sets) are pinned at unit level. Routing every provider lookup in the compile path through the caller's registry is outside this slice.
13. `column_access` treats an undeclared scalar strategy as `writes_unknown` (Design 12.8), reversing deviation 5; the renamed read test that uses strategy `x` still passes because `touched_columns` only reacts to an unknown read.
14. Design 12.8 names `plan_native_route` and `_chunked_evidence`; their registry parameters (and `compile_native_plan`'s) are optional with a default-registry fallback, because their direct callers in the existing suite omit it. `_run_chunked` always passes `state.registry`.

## Left for the gates and the merge

dennis round 3 and the Codex final gate; the ledger row for `B8-BENCH-2026-10-02-1M`; the ROADMAP and shipped-log entries, which the plan ties to the merge; a rebase onto B7 if it lands first (its `uncovered_columns` test rewrites and section 11 configuration (d) note).
