# B8 native unconfigured passthrough: build record

Status: record

Date: 2026-10-02. Plan: `docs/plans/2026-10-01-native-unconfigured-passthrough.md` (revision 4.1, Codex plan-gated; not edited by this record). Branch `feat/native-unconfigured-passthrough`, rebased onto engine main `64e44dac`, which already includes B2 (the dispatcher auto-chunk lane, PR #188). B7 has not landed, so the plan's B7 items are not applied. Builder: Sonnet. dennis and the Codex final gate have not run.

## Status in one paragraph

The slice is built and every acceptance test, guard test and benchmark bar passes. It is not merge-ready: six existing B2 tests fail, and the cause is a real divergence between the plan and the code. A source table that carries a stored pandas index column (a `__index_level_0__` column plus pandas schema metadata, which is what `pa.Table.from_pandas` writes for a non-range index) is now admitted to the native route, which yields that column as an output column. The oracle route drops it, because pandas consumes it as the index. The plan's guarantee 2 (output identical to the oracle route) does not hold for those sources, and B2's pinned guarantee 3(a) (a stored index column is absent from every lane's output) is broken. The plan did not foresee this because the B2 tests relied on the unconfigured-column veto to keep such sources on the oracle route. Details and options are under "Divergence".

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
| the commit holding this record | This record and the benchmark artifact. |

## Red before

At `7c6d6133` (tests only), in the companion venv: 291 failed, 1011 passed across the eleven test files the commit touches, plus a collection error for `tests/native/test_column_access_surfaces.py` because `decoy_engine.execution._column_access` did not exist. Per file: `test_unconfigured_passthrough_types.py` 135 failed (every one asserts native admission and gets `reroute_reason="uncovered_columns:['x']..."`; test 3 and test 4); `test_chunked_entry_rev9_shapes.py` 70; `test_chunked_entry_rev9_read.py` 30; `test_unconfigured_passthrough.py` 38; `test_unconfigured_passthrough_read.py` 8; `test_chunked_entry_rev9_nfkc.py` 3; `test_chunked_entry_rev9_adapter.py` 2; `test_auto_chunk_dispatcher.py` 2; one each in `test_chunked_entry_evidence.py`, `test_chunked_entry_values_schema.py` and `test_auto_chunk_output_contract.py`. The failure reasons checked by reading the output: the `uncovered_columns` veto (acceptance 1, 2, 3, 4, 9 and the rewrites), missing attributes `_chunked_entry.enforce_output_projection` and `known_output_columns` (the warning-call and cross-check tests), `ArrowNotImplementedError` at call time for `dense_union`, `sparse_union` and `run_end_encoded<string_view>` (acceptance 5), the false-positive read set (acceptance 6a: `chunked_passthrough_value_unrepresentable` for a column named `redact` or `person_first_name`), and the missing composite read-set entries (6c: `{'email','last_name'} <= set()`; the `composite_custom` bundle case already passes today because the broad scan catches it, as the plan says). Guard tests that pin unchanged behavior passed before and after (genuine readers in 6b, `real_type_rejection` reasons, the policy-keyword default).

## Final test results

Command, from the worktree, with the companion venv and the worktree `src` first on `PYTHONPATH`:

`PYTHONPATH=$PWD/src:$PWD /home/cam/bin/pytest-one /home/cam/.cache/decoy-native-venv/bin/python tests -q --tb=short -p no:randomly`

Result at `11d913f4`: 7 failed, 19245 passed, 142 skipped, 21 deselected, 59 xfailed, 61 warnings in 1551.77s (25:51). The seven failures:

- six B2 tests in `tests/unit/execution/test_auto_chunk_output_contract.py`, all caused by B8 (divergence 1). They pass on the base: `test_auto_chunk_output_contract.py -k index` gives 9 passed on the pre-B8 `src`.
- `tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client`: `ModuleNotFoundError: No module named 'google'` in the companion venv. It fails the same way on the pre-B8 `src`, so it is environmental and unrelated.

Targeted subsets run during the build: `tests/native tests/sentry` 5896 passed, 2 skipped (before the last six tests were added); `tests/unit/execution tests/parity` 4811 passed, 6 failed, 10 skipped, 59 xfailed (the six B2 failures below).

## Lint and types

`ruff format --check src tests testflight scripts` and `ruff check src tests testflight scripts` clean (ruff 0.15.22 from the main repo venv; CI pins 0.15.14). `mypy src/decoy_engine testflight`: no issues in 502 source files (mypy 2.3.0; CI pins 2.1.0). Module-size ratchets unchanged: `_requirements.py` stays at 648 lines, `_dispatch.py` 488 to 495 and `_chunked_entry.py` 453 to 484, all under the 600 goal; `_column_access.py` is a new module under the cap.

## Coverage

Command: the eight B8-relevant test files under `coverage run --branch` (coverage 7.16.2 from a scratch `--target` directory outside the repo, so no project dependency changed), reported for the seven changed units:

| Unit | Line / branch | Uncovered lines that B8 touched |
|---|---|---|
| `_column_access.py` (new) | 98% line | 89 and 173 at measurement time; both covered by the two tests added in `9d8182a4` |
| `_chunked_carry.py` | 91% | none (misses at 92-94, 107-111, 137-139 are unchanged code) |
| `native/_dispatch.py` | 90% | none (misses are the unchanged faker-source and probe branches) |
| `native/_chunked_entry.py` | 97% | none (289-290 and 334 are unchanged) |
| `native/_chunk_masking.py` | 88% | none |
| `native/_real_type_admission.py` | 86% | none |
| `native/_requirements.py` | 85% | none |

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

## Divergence

1. **Stored pandas index columns (blocks merge).** Six existing B2 tests fail, none edited: `test_auto_chunk_output_contract.py::test_pandas_origin_sources_take_the_dispatcher_lane[RangeIndex_start_and_step|named_RangeIndex-parquet_read_back|from_pandas]` (four) and `test_stored_non_range_index_column_is_absent_on_every_lane[parquet_read_back|from_pandas]` (two). A source from `pa.Table.from_pandas(df)` with a non-range index, or with `preserve_index=True`, holds an extra column (`__index_level_0__`, or the index name) and pandas schema metadata that names it. Before B8 that column counted as uncovered, so the table took the oracle route, where the stock adapter's `to_pandas` consumed the column as the index and `from_pandas(preserve_index=False)` dropped it. After B8 the column is "unconfigured", so the native route yields it as an output column. A direct run confirms it: for `df` with a string index, the native route yields `['r', 't', '__index_level_0__']` and the forced oracle route yields `['r', 't']`. This breaks plan guarantee 2 and B2's guarantee 3(a). Options for a plan patch: (a) keep the veto, with a coded reason such as `pandas_index_column:<name>`, when the first chunk's schema metadata (`b"pandas"`, key `index_columns`) names a stored index column; (b) make the native route drop those columns the way the oracle route does. Option (a) is smaller and leaves the stored-index handling to roadmap item PARQUET-INDEX. I did not implement either, because the plan and the B2 contract both need a decision.
2. The plan's acceptance 6(a) names a column after a provider as `first_name` beside a first-name Faker column. The Faker column's provider string is `person_first_name`, so a column named `first_name` never collided with the old scan. The test uses `person_first_name`, the real provider value.
3. Acceptance 6(b) lists the sibling strategies "unconfigured and configured". A `group_by`, `anchor` or similar reference must name a configured column (the plan compiler rejects it otherwise), so the sibling cases are configured-only. The `when:` cases cover both. The sibling cases use shapes valid for the strategy (a dictionary with a null for `group_by`, a far `date32` for `anchor`) rather than the `time64[ns]` shape, which the strategies reject on valid values.
4. The "before" benchmark side is `49a8a5a7` (equal to main `64e44dac`), not `0bb196fa`; see Benchmark.
5. `column_access` treats an unknown strategy name like a strategy with no sibling field (generic declaration, `coherent_with` and the sibling keys only). Such a config fails plan compilation before it can run; the choice lets the renamed read test keep its original strategy name `x`.
6. `column_access` decides whether an entry is composite with the default provider registry. `run_mask_chunked` accepts a caller registry, and a custom registry's composite provider that the default registry does not know would be treated as a scalar. Every composite is refused on `run_mask_chunked` today (`strategy_not_chunk_safe` or `composite_requires_bundle_path`), so no such column can complete. If a later change admits composites, `read_set` must receive the run's registry.
7. `SURFACE_DECLARATIONS` keys are `scalar:<strategy>`, `composite:<provider>` and `surface:<name>`; the plan says only "keyed by surface name". G1 and G2 read the `scalar:` and `composite:` subsets.
8. The read set now also compares a candidate's NFKC form with the touched names (`c in touched or nfkc(c) in touched`); the old scan did the same for predicate names. This over-approximates, as before.

## Left for the gates and the merge

dennis and the Codex final gate; the decision on divergence 1; the ledger row for `B8-BENCH-2026-10-02-1M`; the ROADMAP and shipped-log entries, which the plan ties to the merge; a rebase onto B7 if it lands first (its `uncovered_columns` test rewrites and section 11 configuration (d) note).
