Status: record

# SCAN-ML M5: post-scan route guard

Date: 2026-11-12. Branch: `feat/scanml-m5`, started from engine main `426e6d05`. Plan: SCAN-ML
(`plan/scan-ml` at `e0ae4204`, `docs/plans/2026-11-07-scan-ml.md`), section 4.5 (route guard),
sections 4.8, 5, 6, 7 (AT-M5a), 11 to 14. Handoff: `handoffs/scan-ml-build`.

## Environment

- Python 3.11.18 (`.venv`, `uv sync --frozen --extra dev`), PyArrow 24.0.0, DuckDB 1.5.4.
- spaCy 3.8.14 with `en_core_web_sm` 3.8.0 in a separate venv for the NER run (below). The dev
  venv has no spaCy.

## Change

- `verification/postmask/_route_guard.py`: `PostScanRouteError(DecoyError)` and
  `refuse_unchecked_route(config, route)`. It refuses when `post_scan.integrity` is true and
  `post_scan.detectors` holds anything other than `regex` or `post_scan.ner_findings` is `fail`.
  `str(error)` and `message` are the registry summary; `route` and `setting` are attributes; the
  error is raised with an empty chain through `secret_providers._errors.raise_bare`.
- Reason `route.post_scan_models_unsupported` (owner `PostScanRouteError`, a configuration refusal
  in the RESOLVE and COMPILE phases, `since` 1.24.0), registry 1.23.0 to 1.24.0 (no `UNRELEASED`
  marker exists on main), pinned in `tests/fixtures/f29/reason-manifest-v1.24-scanml-m5.json`,
  mapped in `reason_adapter._FIXED`. `consumer-contract.json` regenerated (version lines only).
- The guard is the first statement of `run_sequential`, `run_fk_out_of_core`,
  `run_mask_pipeline_chunked`, native `run_mask_chunked` and `run_native_or_oracle_chunked`.
- `config` is a required keyword-only argument of `run_sequential`, `run_fk_out_of_core` and the
  wrappers listed under "Deviations"; `run_out_of_core_route` forwards its config.
- `PostScanRouteError` exported from `decoy_engine` (two docstring lines folded so `__init__.py`
  stays at 600 lines, no census entry); `docs/compatibility-contract.md` section 3.4 and
  `docs/reasons.md` updated; fragment `changelog.d/scan-ml-m5.md`.
- Sentry `tests/sentry/test_post_scan_route_guard.py`: entry list (GUARDED or EXEMPT for every
  config-taking public or `@entry_point` execution callable; guard first in each GUARDED one),
  caller check (every `src/` call of a plan-level entry or forwarding wrapper passes `config=`),
  and the resident-lane check (no `execution/_pipeline*.py` module calls a guarded public chunked
  entry), each with seeded mutants. Its EXEMPT table is registered as the new baseline
  `post-scan-route-exempt` (`scripts/dev/sentry_registry.toml`), as `test_baseline_registry` requires
  for an allowlist-shaped constant; the file is listed in `test_sentry_manifest.SENTRIES`.

## Deviations from the plan text

The plan (section 4.5, the route-guard bullet; section 12; AT-M5a) is the spec. Three points differ,
each decided by the owner through the orchestrator on 2026-11-12 after the first build stopped:

1. Option A for the resident auto-chunk lane. On main, `run_pipeline` with `post_scan.integrity: true`
   still takes the auto-chunk route (the output is kept resident, `_chunked_output_sink.py`), and that
   lane called the public `run_mask_chunked` and `run_mask_pipeline_chunked`. With the guard first in
   those entries, as the plan says, `run_pipeline` would have refused every auto-chunked model
   post-scan job instead of running the check (contradicting plan 5.2 and AT-M4e). The guard stays
   first in the public entries; the lane now calls guard-free private cores:
   `native._chunked_entry._mask_chunked` (subset check, thread check, `count_rows` over
   `_run_chunked`) and `_chunked_oracle._mask_pipeline_chunked` (the former body of
   `run_mask_pipeline_chunked`). The sentry asserts that no `execution/_pipeline*.py` module calls a
   guarded public chunked entry, and AT-M5a asserts that a model post-scan config through either
   auto-chunk lane does not raise and gives the same output as regex-only integrity.
2. A fifth guarded entry, `run_native_or_oracle_chunked` (see "Open decision").
3. Wrapper threading. Making `config` required on `run_sequential` and `run_fk_out_of_core` also
   makes it required on the wrappers that forward to them: `run_sequential_route`,
   `run_out_of_core_route` (its `config` was `dict | None = None`), `PandasExecutionAdapter.run_sequential`
   and the physical `SequentialAdapter.run` and `OutOfCoreAdapter.run`. None has a default.
   The physical shadow coordinator, which is not on a product path and publishes nothing, passes
   `config={}` to `OutOfCoreAdapter.run`.

Test callers that passed no config before now pass `config={}`. That keeps their behavior exactly
(before, `run_sequential` read no config and `run_fk_out_of_core` saw `None`, which its vault and
validator paths treat like an empty mapping when no vault writer is set).

## Open decision

`run_native_or_oracle_chunked` (`native/_dispatch.py`) is public (exported from
`decoy_engine.execution.native`, listed in `docs/compatibility-contract.md` section 1), takes a config,
masks chunk by chunk and runs no post-scan. The plan names four entries and is silent on it. The owner
did not answer, so the stricter path was taken: it is guarded as a fifth entry, covered by AT-M5a and
listed in the sentry's GUARDED table. No resident `run_pipeline` path reaches it (no `src/` caller;
`git grep -n run_native_or_oracle_chunked -- src` shows only its definition and the package export),
and guarding it broke no existing test. To reverse: remove the guard call in
`run_native_or_oracle_chunked`, move its row from GUARDED to EXEMPT in
`tests/sentry/test_post_scan_route_guard.py` with a reason, and drop it from `ENTRIES` in
`tests/unit/execution/test_scanml_m5_route_guard.py`.

### Existing tests changed by option A (spy targets, no assertion changed)

Several tests observed or injected into `run_pipeline`'s auto-chunk lane by patching the public
entries. The lane now calls the private cores, so each patch target moved to the function the lane
calls; every assertion is unchanged. `_chunked_entry.run_mask_chunked` became
`_chunked_entry._mask_chunked`, and `_chunked.run_mask_pipeline_chunked` became
`_chunked_oracle._mask_pipeline_chunked`, in: `tests/unit/execution/_auto_chunk_support.py`
(`spy_lanes`, keys kept), `_b6a_support.py` (`ChunkSpy`), `test_auto_chunk_dispatcher.py`,
`test_auto_chunk_routing.py` (one spy and the two `bomb` patches, so they keep guarding the
legacy lane), `test_auto_chunk_units.py`, `test_b6a_output.py`, `test_b6b_equality.py`,
`test_b6b_failures.py`, `tests/parity/test_phase_f_f2_mixed_parity.py`,
`test_phase_f_f2_mixed_publish.py` and `tests/physical/test_shadow_chunked.py`. Patches of the
physical drivers' calls (`test_lossless_forwarding.py`) and the `b1_as_the_lane` helper keep the
public entries, because those paths still call them. `tests/unit/config/test_scan_ml_reasons.py`
asserted that M5's reason did not exist yet (an M1 tripwire); it now asserts the reason exists in
its own minor 1.24.0.

## Red first

Commit `dce80fa1` (tests only, no source stub) on `426e6d05`: 51 failed, 96 passed across
`tests/unit/execution/test_scanml_m5_route_guard.py`, `tests/sentry/test_post_scan_route_guard.py`,
`tests/unit/test_reason_registry.py`, `tests/unit/test_public_api.py` and
`tests/sentry/test_reason_minor_fixtures.py`. Every failure was an `AssertionError` (or pytest
`Failed`), none an import or collection error:

- 37 AT-M5a tests: "the post-scan route guard is missing" (the module does not exist).
- `test_plan_level_entries_and_their_wrappers_require_a_keyword_config`: "run_sequential takes no
  config".
- The two auto-chunk tests: "the guard-free dispatcher/oracle core is missing".
- Sentry: guard-first fails for each of the five entries; the entry list fails (`run_sequential`
  takes no config); the caller check lists `src/` calls without `config=`; the resident-lane check
  lists `_pipeline_auto_chunk.py` calls of the public entries.
- Registry: `reasons.find("route", "post_scan_models_unsupported")` is None; the minor-fixture
  sentry reports a fixture above the live registry minor; `test_public_api` misses
  `PostScanRouteError`.

After the red commit the two auto-chunk tests were corrected in their setup, not their assertions:
they now pass a sink (the pinned chunked-sink case) and expect the legacy lane's resident reason
`legacy_lane`; on the red commit they already failed at the "core is missing" assertion.

## Mutations (final source head `06bbf9e7`, each applied once, then reverted)

Command for each: `.venv/bin/python -m pytest -q -p no:randomly -n 2
tests/unit/execution/test_scanml_m5_route_guard.py tests/sentry/test_post_scan_route_guard.py`.

- M-M5a, drop the guard from `run_sequential`: 4 failed (the two `run_sequential` refusal cases,
  `test_refusal_text_is_the_fixed_summary_with_no_chain[run_sequential]`, sentry
  `test_guarded_entry_calls_the_guard_first[...run_sequential]`).
- M-M5a2, `run_out_of_core_route` passes `config={}` instead of its config: 2 failed
  (`test_out_of_core_route_forwards_its_config_to_the_guarded_core`,
  `test_out_of_core_route_with_regex_only_integrity_runs_as_today`).
- M-M5a3, interpolate the route into the exception text: 11 failed (the five
  `test_refusal_text_is_the_fixed_summary_with_no_chain` cases, the five
  `test_guard_refuses_each_model_post_scan_shape` cases,
  `test_error_is_public_and_pickles_to_the_same_identity`).
- M-M5a4 (option A), the dispatcher lane calls the public `run_mask_chunked` again: 2 failed
  (`test_run_pipeline_auto_chunk_lane_runs_model_post_scan_configs_as_on_main[dispatcher]`, sentry
  `test_run_pipeline_modules_never_call_a_guarded_chunked_entry`).

No commit carries a mutation. Later commits touch only tests, a sentry baseline and docs.

## Checks

```
uv sync --frozen --extra dev
GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <182 paths>
```

Head `17562e7c`, baseline origin/main `426e6d05`. The 182 paths: every `tests/**/test_*.py` that
`git grep` finds for `run_sequential`, `run_fk_out_of_core`, `run_sequential_route`,
`run_out_of_core_route`, `OutOfCoreAdapter`, `SequentialAdapter`, `run_mask_chunked`,
`run_mask_pipeline_chunked`, `run_native_or_oracle_chunked`, `_chunked_entry`, `_chunked_oracle`,
`PostScanRouteError` and `post_scan_models_unsupported` (minus `tests/perf/`), plus the acceptance
file, `tests/sentry` (module size, reason registry and minor fixtures included),
`tests/unit/test_reason_registry.py`, `tests/unit/test_public_api.py`,
`tests/unit/progress/test_consumer_contract.py` and `tests/unit/execution/test_subset_run_guard_entries.py`.

| Step | Branch | Main |
|---|---|---|
| ruff, format, mypy | not run: `.venv/bin/ruff` and `mypy` are not in the `dev` extra (rc 127 on both) | same |
| sentry | 5034 passed, 3 failed, 21 errors | 5014 passed, 3 failed, 21 errors |
| layers | 15 passed | skipped (passed on branch) |
| licenses | 63 passed | skipped (passed on branch) |
| tests | 14661 passed, 3 failed, 21 errors, 2717 skipped | 14601 passed, 2 failed, 21 errors, 2717 skipped |

Failing ids on both: `tests/sentry/parallel/test_serial_resources.py::test_the_required_serial_registry_is_marked`
and the `tests/sentry/test_outbound_clients.py` set (no `google` package in this venv). Branch only:
`tests/unit/config/test_scan_ml_reasons.py::test_the_slice_adds_exactly_these_identities_and_no_later_slice_code`
(the M1 tripwire above), fixed in `ca37c508` and passing there (16 passed in that file). No main
failure passes on the branch. Collected count in the tests step: branch 17402 against main 17341 (+61, the new tests).

Lint, separately, because the `dev` extra has no linters:

```
uv sync --frozen --extra dev --extra lint
GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy tests/unit/execution/test_scanml_m5_route_guard.py
```

ruff PASS, format PASS, mypy 4 errors on the branch and the same 4 on main (no branch-only error).

Module sizes after the change (census ceilings unchanged): `_chunked_entry.py` 688 of 689,
`_pandas_adapter.py` 694 of 700, `_sequential.py` 666 of 680, `_dispatch.py` 671 of 688,
`_pipeline_route_exec.py` 614 of 632, `__init__.py` 600 (goal, no entry); the secret-material
census re-derives unchanged.

## NER tests

The `ner-gate` steps, in a separate venv (`UV_PROJECT_ENVIRONMENT=<venv-ner> uv sync --frozen
--extra dev --extra ner`): the URL and SHA-256 from `NER_MODEL_WHEELS[("en_core_web_sm", "3.8.0")]`,
`curl --fail` download, `sha256sum --check` OK, `uv pip install --no-deps <wheel>`. Then
`DECOY_REQUIRE_NER=1 pytest -m ner -n 2`: 38 passed, 14 skipped. Every skip is a module-level
import skip for another optional dependency (google, paramiko, hvac, lightgbm, joblib, mimesis, the
wheel builder, mutmut); no NER test skipped.

## TEST SCOPE

```
Tiers:       T0, T1, T3 because public execution signatures change and a reason code is added
Named tests: tests/unit/execution/test_scanml_m5_route_guard.py (AT-M5a),
             tests/sentry/test_post_scan_route_guard.py,
             tests/unit/test_reason_registry.py::test_scanml_m5_route_refusal_is_registered_with_a_pinned_fixture
Selection:   scripts/dev/gates.sh --baseline
Plus:        git grep hits in tests/ for the changed signatures (182 paths, tests/perf excluded)
Sentries:    tests/sentry (module size, reason registry, minor fixtures, baseline registry, manifest)
Triggers:    public execution signatures (required config), new reason code, registry minor bump
Mutations:   M-M5a -> refusal and guard-first tests; M-M5a2 -> out-of-core forwarding tests;
             M-M5a3 -> fixed-summary tests; M-M5a4 -> auto-chunk lane test and resident-lane sentry
Full suite:  trigger public execution signatures plus reason registry, orchestrator runs it
```

## Not run

- The full suite (orchestrator) and the perf suite.
- ruff and mypy in the exact `--extra dev` sync (not installed there); run with `--extra lint`.
- Tests needing google, paramiko, hvac, lightgbm, joblib or mimesis (not installed).
- The gates tests step ran on `17562e7c`; `ca37c508` and this record change one test file and docs.

## V2-LOG

- `src/decoy_engine/verification/postmask/_route_guard.py:60`: a non-Mapping `config` (for example
  `None` from an untyped caller) fails with `AttributeError` inside `post_scan_integrity_requested`
  rather than a typed refusal.
- `src/decoy_engine/execution/physical/_shadow_coordinator.py:252`: the shadow OOC compare passes
  `config={}`; it publishes nothing, but a shadow run of a model post-scan job is not refused.
