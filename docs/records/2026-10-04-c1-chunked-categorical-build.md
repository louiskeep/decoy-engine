# C1 chunked dispatcher, deterministic categorical: build record

Status: record

Date: 2026-10-04. Plan: `docs/plans/2026-10-04-c1-chunked-categorical.md` revision 4 (Codex plan-gate GO). Branch `feat/c1-chunked-categorical` off engine main `f8c8bf36`. Built tests-first by a Sonnet build agent; not pushed, not merged. dennis and Codex final gates are still to run.

## What shipped

Native-admissible deterministic `categorical` runs on the native chunked route through the existing compiled index kernel. No new Rust. Categorical left `CHUNKED_ROUTE_VETOED_STRATEGIES`, so a table with a categorical column no longer goes whole to the oracle. A native-admissible column is pinned to `string` on both chunked legs.

Native-admissible means: `is_deterministic_categorical` (`deterministic: true` or `allow_collisions: true`), a namespace, non-empty all-string categories, weights `_build_cdf` can build, and a first-chunk `pa.string()` source.

Change sites (plan section 2 and 4):

| Plan item | Change |
|---|---|
| Site 5, preflight gate | `execution/_chunked_categorical.py` (new): `is_nondeterministic`, `conditional_failures`, `reject_nondeterministic`. `_chunked.py` calls it. New code `categorical_nondeterministic_not_chunk_safe`, strictly for a failed `is_deterministic_categorical`. Namespaceless, `from_profile` and missing-categories columns keep `chunked_strategy_conditions_unmet`. |
| Sites 1 to 4, veto | One edit to the set in `native/_requirements.py` lifts sites 1 to 4, because 2 to 4 read the set. A non-admissible column reaches the oracle through the existing `fallback_policy_not_native:<col>:python_only` reason. |
| Source-type admission | `native/_real_type_admission.py`: `categorical_source_type_rejection`, reason `categorical_source_type_not_string:<col>:<type>`, runs before masking. |
| Prepared artifact | `native/_categorical_prepared.py` (new): `prepare_categorical` (the one validation and CDF build; `categorical_config_rejection` now delegates to it) and `prepare_chunked_categoricals`. |
| Kernel call | `_chunk_masking.py`: categorical branch calls `native_categorical` per chunk and sets `compiled_kernel_executed`. `_dispatch.py` loads the index kernel for `faker` or `categorical`. |
| Pin | `_chunked_schema_rule.py` takes `categorical_columns`; `_chunked_entry.py` builds them once per run. |
| Evidence | `_chunked_evidence.py`: categorical plans `rust_companion`. |
| Docs | `CHANGELOG.md`, `docs/compatibility-contract.md`, docstrings. |

## Commits

| SHA | What |
|---|---|
| `69d2b798` | Fixture migration: oracle-forcing categorical fixtures moved to a still-vetoed `bucket_perturb` column (green before and after). |
| `5bcdda50` | Acceptance tests, red before implementation. |
| `93227203` | Implementation, CHANGELOG, compatibility contract, `test_b6a_output.py` migration, `_chunked.py` census 612 to 616. |
| `3946279a` | Tests that kill the five first-pass mutation survivors. |
| `c53df67e` | No-cover pragma on an unreachable guard, record stub. |
| `fd8a9fa6` | Docstring corrections (`_categorical_ext.py`, seam test). |
| `c78ecc47` | Seam-disconnection exact-diff gate allow-lists the two new modules. |
| the commit holding this record | This record. |

## Red before

At `5bcdda50`, companion venv, over the touched suites (new admission and parity files, `values_schema`, `phase3_eligibility`, `test_shadow_categorical`, the three auto-chunk files): 115 failed, 504 passed. By file: admission 24, parity 80, values_schema 2, phase3_eligibility 1, shadow_categorical 4, auto_chunk_output_contract 4. Baseline `f8c8bf36` on the same files: 0 failed.

Companion-absent venv (`.venv-decoy`, pyarrow 24): 118 failed, 310 passed, 191 skipped. That venv already fails 86 tests in these files on `f8c8bf36` (environment), so the new failures are the delta of 33 by name, in the new tests plus renamed ones.

## Final test results

Companion present (`/home/cam/.cache/decoy-native-venv`), `tests` whole, at `fd8a9fa6`: 2 failed, 20663 passed, 168 skipped, 21 deselected, 59 xfailed (28:21).
- `tests/sentry/test_physical_seam_disconnection.py::test_production_execution_modules_are_byte_identical_to_origin_main`: C1-caused, the two new modules were not allow-listed. Fixed in `c78ecc47`; `tests/sentry` re-run: 2227 passed, 1 skipped.
- `tests/unit/test_v2_cloud_sources.py::...test_profile_gcs_source_via_mocked_client`: no `google` module in this venv. Pre-existing and environmental (same failure in the B8 record).

Companion absent (`.venv-decoy`), `tests` with `--continue-on-collection-errors` (10 collection errors from missing `opendp` etc.), at `c78ecc47`: 843 failed, 18066 passed, 1790 skipped. The same command on `f8c8bf36`: 842 failed, 18067 passed. The failure-set delta is one test, `test_byte_estimate_routing.py::TestEndToEndWiring::test_width_change_flips_route_at_a_fixed_budget_and_row_count`, which passes 27 of 27 in isolation on both trees (load-sensitive flake, not C1). Nothing is fixed by C1 either. The venv is too broken to be a pass/fail gate, so the delta against main is the evidence.

New tests: 136 (`test_chunked_categorical_admission.py` 50 plus `test_chunked_categorical_parity.py` 86), all green with the companion. In the absent venv the native-result ones skip and the oracle-leg and routing ones run.

## Parity

Native chunked equals oracle chunked (`run_mask_chunked` with a vetoed `bucket_perturb` sibling forces the oracle leg; compared by `Table.equals(check_metadata=True)`, warnings, timing columns, vault entries, route labels). No assertion was loosened. Passing matrix:
- 5 content shapes (all-null, single-row, ragged, null block then valued, valued then null block) x uniform and weighted x chunk sizes 1, 7, 50,000 x `native_threads` 1 and 4: 60 cases.
- Empty chunk (4), null-typed later chunk (2), 100,003 rows in 3 chunks at 50,000 (2).
- Determinism: same source value maps to the same category in every chunk and equals the full-frame result. Output bytes identical at threads 1, 2, 4, 8.
- Types: `string` on every chunk of both legs, independent of chunk count (1, N, 50,000).
- Regressions that stay on the oracle with their exact oracle types: numeric categories, empty `int64` source with an all-string config (oracle gives `double`), `large_string` source, and a dictionary and an `int64` source (reroute before masking). Integer-with-nulls stays the existing fail-closed `null_bearing_int_unsupported` case.

No divergence found.

## Fixture-migration inventory (plan 4.9)

Oracle-forcing fixtures migrated to `bucket_perturb` (helper `force_oracle`, `FORCE_ORACLE_VALUE` in `_chunked_entry_support.py`): `_b8_support.py`, `test_chunked_entry_evidence.py` (3 sites), `test_chunked_entry_side_channels.py` (3), `test_chunked_entry_parity_matrix.py`, `test_chunked_entry_values_schema.py`, `test_unconfigured_passthrough.py` (3), `test_composite_admission.py` (2). The inventory turned up three sites the plan did not list: `tests/unit/execution/test_auto_chunk_dispatcher.py`, `test_auto_chunk_routing.py`, `test_b6a_output.py` (the hold-back and spill tests need a column typed late, which categorical no longer is). Genuine categorical tests changed: the provisional characterization test in `values_schema` became the pinned-type test, `phase3_eligibility` drops categorical from the full-frame-only list, `test_shadow_categorical`'s chunked seam test now admits admissible and declines the rest, `_auto_chunk_strategies.py` moves `categorical:deterministic` to the native keys. Kept as genuine: `test_chunked_entry_gate_findings.py` (int with nulls), `test_chunked.py`, `test_chunked_mutation_kills.py`, multi-table gate tests (non-deterministic categorical gating is unchanged).

## Lint and types

`ruff check` and `ruff format --check` on `src tests` clean (ruff 0.15.14, the CI pin). `mypy src/decoy_engine testflight`: 6 errors, all in untouched files (`quality/dp_budget.py` `opendp` stubs, `internal/base.py` `internal.logger`), none in C1 files. Touched modules stay under the cap: `_chunked.py` 616 (dense exception, census bumped), `_chunked_entry.py` 527, `_dispatch.py` 510, `_requirements.py` 648 (unchanged).

## Coverage

`coverage run --branch` (coverage 7.14.1 borrowed from `.venv-decoy`) over 499 tests in the eleven chunked and categorical files. Of 104 executable statements C1 added under `src`, 103 are covered. The one miss, the `table_seed is None` guard in `_chunked_entry._prepared_categoricals`, is unreachable (a validated mask table always has a seed envelope) and is marked `pragma: no cover`. New modules: `_chunked_categorical.py` 100% line and branch, `_categorical_prepared.py` 100% line and branch.

## Hand mutation (mutmut cannot grade these modules)

48 mutants applied one at a time, each run against the categorical, evidence and seam tests, source restored after each:
- gate (13): non-determinism predicate always true or false or ignoring `allow_collisions`, reject no-op, wrong code, wrong path, namespace, `from_profile` and categories checks dropped, gate and reject call dropped.
- routing and evidence (6): categorical back in the veto set, planned backend not `rust_companion`, index kernel not loaded, source-type check always passing, admitting `large_string`, check not called.
- prepared artifact (16): each of eight config checks dropped, CDF dropped or reversed, categories reversed, strategy, source-type, determinism and namespace filters in `prepare_chunked_categoricals`, artifact not stored, config gate always None.
- pin (5), execution (7), CDF rebuilt per chunk (1): pin dropped or widened, prepared mapping not threaded, CDF, categories, namespace, mask key, thread budget not forwarded, `compiled_kernel_executed` not set.

First pass: 5 survivors (`from_profile` check with explicit categories present, three predicates in `prepare_chunked_categoricals` that the public flow never reaches, and the thread budget). Four tests added in `3946279a` kill all five. Final: 0 survivors.

## Documentation gate receipt

CHANGELOG and compatibility-contract entries written by the builder in `93227203`. The barry docs agent ran against exact HEAD `3946279a`: it verified both entries against the diff, checked `docs/capability-matrix.md`, `strategies.md`, `determinism.md`, `recipes.md`, `what-we-cannot-prove.md`, `native/draw-site-inventory.md`, `native/supported-matrix.md`, `CODEMAP.md` and `README.md`, and made no edits (no stale statement found). The builder re-grepped active docs for `CHUNKED_ROUTE_VETOED`, `vetoed` and categorical routing claims and found none. Later commits (`c53df67e`, `fd8a9fa6`, `c78ecc47`, this record) change only a coverage pragma, two docstrings, a test allow-list and this record. Cross-repo follow-up for the caller: `decoy-platform/docs/ROADMAP.md` and the shipped log need the C1 entry (not editable from this repo).

## Deviations and notes

- The admission config gate still builds the CDF (it must, to return `categorical_weights_unbuildable_cdf`). It and the run's prepared artifact share one function, `prepare_categorical`, and the execution artifact is built once per run and reused for every chunk, so the build count does not scale with chunk count (test over the whole public run). The plan's "one build per column" literally is not achieved; the plan allowed this ("or refactor validation so it does not rebuild").
- The pin does not exclude `when:` columns (the plan is silent); casting a string-source, all-string-category column is lossless.
- The pin applies on the oracle leg whenever the column is native-admissible, including when another column vetoes the table.
- `large_string` and dictionary sources are not admitted and not pinned (plan: `pa.string()` only, matching full frame).
- `test_chunked_entry_evidence.py:254` mixed-table case moved to a vetoed `bucket_perturb` column; the admissible-categorical native evidence assertions live in the new admission file.
- The companion-absent venv fails about 840 tests on main; only delta-against-main evidence is meaningful there.
