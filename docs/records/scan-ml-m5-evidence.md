Status: record

# SCAN-ML M5: post-scan route guard

Date: 2026-11-12. Branch: `feat/scanml-m5`, started from engine main `426e6d05`, merged with main
`8f9fdfab` in `96bf249b` and with main `7fc12e7f` in `91a0e975` (see "Merge with main"). Plan: SCAN-ML
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
  in the RESOLVE and COMPILE phases), mapped in `reason_adapter._FIXED`. It was first built at a
  numbered registry minor with its own fixture; since main retired numbered minors (#506) it is
  declared `since=UNRELEASED`, the registry version, `consumer-contract.json` and `docs/reasons.md`
  are main's, and no fixture exists (see "Unreleased conversion").
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
each decided by the owner (relayed by the orchestrator) on 2026-11-12 after the first build stopped:

1. Option A for the resident auto-chunk lane, the owner's choice. On main, `run_pipeline` with `post_scan.integrity: true`
   still takes the auto-chunk route (the output is kept resident, `_chunked_output_sink.py`), and that
   lane called the public `run_mask_chunked` and `run_mask_pipeline_chunked`. With the guard first in
   those entries, as the plan says, `run_pipeline` would have refused every auto-chunked model
   post-scan job instead of running the check (contradicting plan 5.2 and AT-M4e). The guard stays
   first in the public entries; the lane now calls guard-free private cores, each of which runs
   everything its public entry runs except the guard: `native.chunked._core.mask_chunked` (subset
   check, thread check, declaration gate, `count_rows` over `_run_chunked`) and
   `_chunked_oracle._mask_pipeline_chunked` (subset check, declaration gate, then
   `_chunked_oracle._mask_declared`, the preflight and per-chunk loop the public entry also calls).
   Both cores reach the declaration gate through the entry modules (`_chunked_entry.` and
   `_chunked.prepare_declared_sources`), so the gate seam #495's below-gate test helpers patch is the
   one the lane uses. The native core is a new private module, `execution/native/chunked/_core.py`
   (owner-approved after the merge pushed `native/_chunked_entry.py` to 703 lines against its 689
   ceiling; CODEMAP's native row already names `execution/native/chunked/` as the target for new
   native modules, so no fan-out, size or baseline number moved and no CODEMAP row was needed). The sentry asserts that no `execution/_pipeline*.py` module calls a
   guarded public chunked entry, and AT-M5a asserts that a model post-scan config through either
   auto-chunk lane does not raise and gives the same output as regex-only integrity.
2. A fifth guarded entry, `run_native_or_oracle_chunked` (see "Owner decision").
3. Wrapper threading. Making `config` required on `run_sequential` and `run_fk_out_of_core` also
   makes it required on the wrappers that forward to them: `run_sequential_route`,
   `run_out_of_core_route` (its `config` was `dict | None = None`), `PandasExecutionAdapter.run_sequential`
   and the physical `SequentialAdapter.run` and `OutOfCoreAdapter.run`. None has a default.
   The physical shadow coordinator, which is not on a product path and publishes nothing, passes
   `config={}` to `OutOfCoreAdapter.run`.

Test callers that passed no config before now pass `config={}`. That keeps their behavior exactly
(before, `run_sequential` read no config and `run_fk_out_of_core` saw `None`, which its vault and
validator paths treat like an empty mapping when no vault writer is set).

## Owner decision

`run_native_or_oracle_chunked` (`native/_dispatch.py`) is public (exported from
`decoy_engine.execution.native`, listed in `docs/compatibility-contract.md` section 1), takes a config,
masks chunk by chunk and runs no post-scan. The plan names four entries and is silent on it. The owner
confirmed on 2026-11-12 that it is guarded as a fifth entry; it is covered by AT-M5a and listed in the
sentry's GUARDED table. No resident `run_pipeline` path reaches it (no `src/` caller;
`git grep -n run_native_or_oracle_chunked -- src` shows only its definition and the package export),
and guarding it broke no existing test. Should the decision change: remove the guard call in
`run_native_or_oracle_chunked`, move its row from GUARDED to EXEMPT in
`tests/sentry/test_post_scan_route_guard.py` with a reason, and drop it from `ENTRIES` in
`tests/unit/execution/test_scanml_m5_route_guard.py`.

### Existing tests changed by option A (spy targets, no assertion changed)

Several tests observed or injected into `run_pipeline`'s auto-chunk lane by patching the public
entries. The lane now calls the private cores, so each patch target moved to the function the lane
calls; every assertion is unchanged. `_chunked_entry.run_mask_chunked` became
`native.chunked._core.mask_chunked`, and `_chunked.run_mask_pipeline_chunked` became
`_chunked_oracle._mask_pipeline_chunked`, in: `tests/unit/execution/_auto_chunk_support.py`
(`spy_lanes`, keys kept), `_b6a_support.py` (`ChunkSpy`), `test_auto_chunk_dispatcher.py`,
`test_auto_chunk_routing.py` (one spy and the two `bomb` patches, so they keep guarding the
legacy lane), `test_auto_chunk_units.py`, `test_b6a_output.py`, `test_b6b_equality.py`,
`test_b6b_failures.py`, `tests/parity/test_phase_f_f2_mixed_parity.py`,
`test_phase_f_f2_mixed_publish.py` and `tests/physical/test_shadow_chunked.py`. After the merge, the
same retarget for three negative spies #495's main carries in `tests/unit/execution/test_group_key_chunked.py`
(each patches the oracle entry around `run_pipeline` and asserts it is never called, beside
`auto_chunk.mode == "full_frame"`): `TestCacheCollisionFloatSignedZero::test_auto_route_selects_oracle_chunked_runner_never_invoked`,
`TestCacheCollisionDecimalSignedZero::test_auto_route_selects_oracle_chunked_runner_never_invoked` and
`TestTextRedactMaskedGroupByRoutesToOracle::test_auto_route_falls_back_to_oracle`. #495's tests that
call the entries directly (the below-gate helpers in `tests/native/`) stay on the public entries. Patches of the
physical drivers' calls (`test_lossless_forwarding.py`) and the `b1_as_the_lane` helper keep the
public entries, because those paths still call them. `tests/unit/config/test_scan_ml_reasons.py`
asserted that M5's reason did not exist yet (an M1 tripwire); it now asserts the reason exists
with `since` equal to the pending value `"unreleased"`.

## Merge with main

`96bf249b` merges main `8f9fdfab` (#495, the declared-columns gate on the three chunked entries)
because it conflicted in `execution/_chunked.py`, `native/_chunked_entry.py` and
`native/_dispatch.py`. Both sides are kept:

- `_chunked.py`: imports kept from both; the entry body is the post-scan guard, the subset check,
  #495's `prepare_declared_sources(DeclarationRequest.for_chunks(config, table, source_schema))`, then
  `_chunked_oracle._mask_declared` (main's preflight and loop, moved so the file stays under 600).
- `native/_chunked_entry.py`: main's `run_mask_chunked` body, unchanged, with the guard (and its
  local import) in front. `_chunked_entry.py` is 684 lines against its 689 ceiling.
- `native/_dispatch.py`: imports only (both kept); `run_native_or_oracle_chunked` runs the guard,
  then main's subset, transforms and declaration steps.
- `#495`'s sentry `tests/sentry/source_config/test_declaration_gate.py` requires the gate inside each
  public entry's own body before its reader; that holds, and it runs after the post-scan guard.

Order in the five entries after the merge: `run_sequential` and `run_fk_out_of_core`: post-scan
guard, `declared_route_guard` (#495 does not touch them). `run_mask_pipeline_chunked`: post-scan
guard, subset check, declaration gate, preflight. `run_mask_chunked`: post-scan guard, subset
check, thread check, declaration gate, `_run_chunked`. `run_native_or_oracle_chunked`: post-scan
guard, subset check, transforms refusal, declaration gate, `_run_chunked`. AT-M5a passes the oracle
entry its `source_schema`; the guard fires before it is used. The secret-material census, re-derived
with `scripts/secret_material_census.py --write`, equals main's.

Second merge: `91a0e975` merges main `7fc12e7f` (#492 moves the classifier stack into the base
install, #481 registers the FPE fallback codes at the minor this slice had claimed, #497 adds the
canary scan entry) because main moved again before the push. Conflicts: `docs/reasons.md` and
`tests/unit/test_reason_registry.py`, both sides kept. `scripts/dev/registry_renumber.py --main
origin/main` then moved `route.post_scan_models_unsupported` to the next free numbered minor
(`004cc7f6`, superseded by the unreleased conversion below). The merge left two of main's FPE test assertions under the M5 test; `6aa0547d` puts
them back in the FPE test, and every other line main added in `8f9fdfab..7fc12e7f` is present.

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

## Mutations

Each mutation applied once, its tests run, then reverted; no commit carries one. Command for each:
`.venv/bin/python -m pytest -q -p no:randomly -n 2 tests/unit/execution/test_scanml_m5_route_guard.py
tests/sentry/test_post_scan_route_guard.py`. Run on `06bbf9e7`, on the first merge head `96bf249b`,
and on the final source head `6aa0547d`. The counts were the same on all three:

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
- M-M5a4 (option A), the dispatcher lane calls the public `run_mask_chunked` again (on the merged
  head: `_core.mask_chunked(` replaced by `_core._chunked_entry.run_mask_chunked(` in
  `_pipeline_auto_chunk.py`): 2 failed
  (`test_run_pipeline_auto_chunk_lane_runs_model_post_scan_configs_as_on_main[dispatcher]`, sentry
  `test_run_pipeline_modules_never_call_a_guarded_chunked_entry`).

## Checks

```
uv sync --frozen --extra dev
GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <183 paths>
```

Final run: head `6aa0547d`, baseline origin/main `7fc12e7f`, tree clean. The 183 paths: every
`tests/**/test_*.py` that `git grep` finds for `run_sequential`, `run_fk_out_of_core`,
`run_sequential_route`, `run_out_of_core_route`, `OutOfCoreAdapter`, `SequentialAdapter`,
`run_mask_chunked`, `run_mask_pipeline_chunked`, `run_native_or_oracle_chunked`, `_chunked_entry`,
`_chunked_oracle`, `chunked._core`, `_native_core`, `PostScanRouteError`,
`post_scan_models_unsupported` and `prepare_declared_sources` (minus `tests/perf/`), plus the
acceptance file, `tests/sentry` (module size, reason registry and minor fixtures included),
`tests/unit/test_reason_registry.py`, `tests/unit/test_public_api.py`,
`tests/unit/progress/test_consumer_contract.py`, `tests/unit/execution/test_subset_run_guard_entries.py`
and `tests/unit/config/test_scan_ml_reasons.py`.

| Step | Branch | Main |
|---|---|---|
| ruff, format, mypy | not run: `.venv/bin/ruff` and `mypy` are not in the `dev` extra (rc 127 on both) | same |
| sentry | 5065 passed, 3 failed, 21 errors | 5035 passed, 3 failed, 21 errors |
| layers | 15 passed | skipped (passed on branch) |
| licenses | 63 passed | skipped (passed on branch) |
| tests | 14746 passed, 2 failed, 21 errors, 2717 skipped | 14675 passed, 2 failed, 21 errors, 2717 skipped |

Failing ids, identical on branch and main (24 each, no branch-only id):
`tests/sentry/parallel/test_serial_resources.py::test_the_required_serial_registry_is_marked` and the
`tests/sentry/test_outbound_clients.py` set (no `google` package in this venv). No main failure
passes on the branch. Collected count in the tests step: branch 17486 against main 17415 (+71, the
new tests). The run on `8f91d3ea` against `8f9fdfab` had the same 24 failing ids on both sides and
no branch-only id.

Earlier runs on this branch: on `17562e7c` against `426e6d05` one branch-only id, the M1 tripwire
`tests/unit/config/test_scan_ml_reasons.py::test_the_slice_adds_exactly_these_identities_and_no_later_slice_code`
(updated in `ca37c508`); on the merge head `96bf249b` against `8f9fdfab` one branch-only id,
`tests/sentry/test_physical_seam_disconnection.py::test_production_execution_modules_are_byte_identical_to_origin_main`,
because the two new `execution/native/chunked/` files were not in that sentry's path list of
permitted parent-level execution changes. `8f91d3ea` adds them with a reason, the way earlier
slices added theirs (a path list, no number).

Lint, separately, because the `dev` extra has no linters:

```
uv sync --frozen --extra dev --extra lint
GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy tests/unit/execution/test_scanml_m5_route_guard.py
```

On `6aa0547d`: ruff PASS, format PASS, mypy 4 errors on the branch and the same 4 on main (no
branch-only error).

Module sizes on the final head (census ceilings unchanged): `native/_chunked_entry.py` 684 of 689,
`native/_dispatch.py` 674 of 688, `_pandas_adapter.py` 694 of 700, `_sequential.py` 666 of 680,
`_pipeline_route_exec.py` 614 of 632, `_chunked.py` 587 and `_chunked_oracle.py` 575 (under the
600 goal), `native/chunked/_core.py` 50, `__init__.py` 600 (goal, no entry). `execution/native`
keeps 55 direct modules (the new module is in a sub-package). The layer gate passes. The
secret-material census, re-derived, equals main's.

## NER tests

The `ner-gate` steps, in a separate venv (`UV_PROJECT_ENVIRONMENT=<venv-ner> uv sync --frozen
--extra dev --extra ner`): the URL and SHA-256 from `NER_MODEL_WHEELS[("en_core_web_sm", "3.8.0")]`,
`curl --fail` download, `sha256sum --check` OK, `uv pip install --no-deps <wheel>`. Then
`DECOY_REQUIRE_NER=1 pytest -m ner -n 2`, on `6aa0547d` (venv re-synced after #492): 39 passed,
10 skipped. Every skip is a module-level import skip for another optional dependency (google,
paramiko, hvac, mimesis, the wheel builder, mutmut); no NER test skipped. On `a9642755`, before
#492, it was 38 passed and 14 skipped (lightgbm and joblib were not installed).

## TEST SCOPE

```
Tiers:       T0, T1, T3 because public execution signatures change and a reason code is added
Named tests: tests/unit/execution/test_scanml_m5_route_guard.py (AT-M5a),
             tests/sentry/test_post_scan_route_guard.py,
             tests/unit/test_reason_registry.py::test_scanml_m5_route_refusal_is_registered_with_a_pinned_fixture
Selection:   scripts/dev/gates.sh --baseline
Plus:        git grep hits in tests/ for the changed signatures (183 paths, tests/perf excluded)
Sentries:    tests/sentry (module size, reason registry, minor fixtures, baseline registry, manifest)
Triggers:    public execution signatures (required config), new reason code, registry minor bump
Mutations:   M-M5a -> refusal and guard-first tests; M-M5a2 -> out-of-core forwarding tests;
             M-M5a3 -> fixed-summary tests; M-M5a4 -> auto-chunk lane test and resident-lane sentry
Full suite:  trigger public execution signatures plus reason registry, orchestrator runs it
```

## Not run

- The full suite (orchestrator) and the perf suite.
- ruff and mypy in the exact `--extra dev` sync (not installed there); run with `--extra lint`.
- Tests needing google, paramiko, hvac or mimesis (not installed).
- The final gates run was on `6aa0547d`; the record commit after it changes docs only, and
  `tests/sentry` was rerun on that head.
- No mutation drops the declaration gate from the lane cores; that gate is #495's, and its own tests
  run through `run_pipeline` (in the selection above).

## V2-LOG

- `src/decoy_engine/verification/postmask/_route_guard.py:60`: a non-Mapping `config` (for example
  `None` from an untyped caller) fails with `AttributeError` inside `post_scan_integrity_requested`
  rather than a typed refusal.
- `src/decoy_engine/execution/physical/_shadow_coordinator.py:252`: the shadow OOC compare passes
  `config={}`; it publishes nothing, but a shadow run of a model post-scan job is not refused.
- `src/decoy_engine/execution/native/chunked/_core.py:24` and
  `src/decoy_engine/execution/_chunked_oracle.py` (`_mask_pipeline_chunked`): each core repeats its
  entry's pre-read steps (subset check, thread check, declaration gate) beside the entry, so a new
  pre-read step added to an entry must be added to its core too; no sentry compares the two lists.

## Third and fourth merges, and the unreleased conversion (2026-11-12)

`45ca33ac` merges main `44bbf494` (#506 release-stamped registry, #514 created-column contract,
#517, #525 and #526 post-scan work, #524, #494 and others), because the owner asked for M5 on the
new main and for the registry conversion. Conflicts and resolution:

- `execution/_chunked.py`: imports, both kept (main's `prepare_declared_execution_sources` beside
  the route-guard import; `prepare_declared_sources` is no longer imported there); the docstring
  keeps main's wording plus the post-scan sentence. The entry body is the post-scan guard, the
  subset check, main's `prepare_declared_execution_sources(..., registry=registry)`, then
  `_chunked_oracle._mask_declared` (unchanged since the earlier merge; it holds main's preflight
  and loop, which this merge did not change).
- `execution/native/_dispatch.py`: imports, both kept.
- The guard-free cores follow main's new gate: `native/chunked/_core.mask_chunked` and
  `_chunked_oracle._mask_pipeline_chunked` now call `prepare_declared_execution_sources` through
  their entry modules, passing the run's `registry`, exactly as `run_mask_chunked` and
  `run_mask_pipeline_chunked` do on main. Re-diffed after the merge: each core runs every pre-read
  step its public entry runs (subset check, thread check for the native one, the declaration gate
  with registry) except the post-scan guard, which stays the first statement of all five entries.
- Sizes: `native/_chunked_entry.py` 688 of 689, `native/_dispatch.py` 677 of 688, `_chunked.py`
  587, `_chunked_oracle.py` 576, `native/chunked/_core.py` 52. No ceiling moved. The secret-material
  census, re-derived with `scripts/secret_material_census.py --write`, is unchanged.

Unreleased conversion: `scripts/dev/registry_renumber.py --to-unreleased --main origin/main` first
refused (`unconverted docs/reasons.md`, the M5 registry test naming the numbered fixture, and two
lines of this record naming it). By hand: the M5 registry test now asserts `since == UNRELEASED`
with no fixture (`test_scanml_m5_route_refusal_is_registered_as_pending`), the `docs/reasons.md`
paragraph naming the fixture is removed (main's text), and this record stops naming the file.
The tool then converted the catalog declaration to `since=UNRELEASED`, the tripwire and the docs
to the pending form, reset `REGISTRY_VERSION`, `docs/reasons.md` and `consumer-contract.json` to
main's 1.24.0, and deleted the fixture. `--check --main origin/main` prints "registry consistent",
exit 0. The changelog fragment names the code as `route/post_scan_models_unsupported`.

`6689efce` merges main `873686d7` (#534, delivery reason codes), which landed during the checks;
no conflict. After it: `--check` exit 0; `tests/sentry`, the registry, release-pin, consumer
contract, tripwire, M5 acceptance, delivery, reasons and compat-corpus tests rerun (only the 23
`test_outbound_clients` ids fail, no `google`). PR #528 was not on main at that point.

Reruns on `13df1b9f` (the converted head before `6689efce`):

- Mutations M-M5a (4 red), M-M5a2 (2 red), M-M5a3 (11 red), M-M5a4 (2 red), the same ids as
  listed under "Mutations".
- `GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <191 paths>`
  against main `44bbf494` (same recipe as before, plus `prepare_declared_execution_sources` and
  `tests/unit/test_reason_release_pin.py`): sentry 5182 passed, 3 failed, 21 errors (main 5152, 3,
  21); layers 15 passed; licenses 63 passed; tests 14930 passed, 2 failed, 21 errors, 2717 skipped
  (main 14859, 2, 21, 2717). Failing ids identical, 24 each, no branch-only id, no main failure
  passing; collected 17670 against 17599 (+71, the M5 tests). ruff, format, mypy rc 127 (not in
  the dev extra); with `--extra lint`: ruff PASS, format PASS, mypy the same 4 errors as main.
- NER gate (venv re-synced, wheel SHA-256 OK): `DECOY_REQUIRE_NER=1 pytest -m ner -n 2`: 39 passed,
  10 skipped (import skips for other extras only).
- `pytest tests/integration/compat_corpus`: 5 passed, 1 skipped (the pre-GA real-artifact
  capture skip), on `13df1b9f` and again on `6689efce`.
