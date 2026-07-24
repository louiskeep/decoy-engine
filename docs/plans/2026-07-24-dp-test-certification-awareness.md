# Plan: DP test certification-awareness (CI fix #4)

Status: PLAN v2 (Opus-authored 2026-07-24, Codex plan-reviewed REVISE -> revised; build-ready). The
"Codex plan-review revisions" section at the end OVERRIDES the original Design/Acceptance where they conflict; read it as the spec.
Owner: engine PR #110 (integration/engine-0.5.0). Scope: engine test suite + CI + one conftest hook.

## Context

Pushing the accumulated local engine main to remote CI for the first time (PR #110)
red-lined the `regression-gate` job (`pytest tests -m "not benchmark and not
packaging and not codspeed"`, ci.yml:120). The DPS-CODEC DP tests call
`fit_dp_snapshot` expecting success, but `regression-gate` installs a dependency
profile whose fingerprint (`7c9d0f3f...`) is NOT a certified `_CERTIFIED_STACKS`
member, so the fit gate refuses with `dp_stack_uncertified` and the tests error.

This is by design, not a bug in the gate: DP fit only completes in the exact
certified proof-stack profile (77-dist `dev+lint+vault` on 3.10.20 ->
`895b9a20`), because the DP guarantee pins the tested library set ("stability-1
by construction"). The full test suite needs extra `ml`/`geo` extras that change
the fingerprint, so DP fit-success tests cannot run there. The tests were only
ever validated in the certified env locally, never against the CI matrix.

## Facts established (read from the tree, 2026-07-24)

- Fit-success files (call `fit_dp_snapshot` expecting success): `tests/unit/quality/test_dp.py`
  (46 calls, many classes), `tests/unit/quality/test_dp_flag_e2e.py` (3),
  `tests/unit/generation/test_generate_dp_contract.py` (4), `tests/unit/plan/test_generation_plan.py` (1),
  `tests/unit/plan/test_serialize.py` (1). 55 call sites.
- None of these five files assert `dp_stack_uncertified` or monkeypatch the cert
  gate (grep = 0). So they carry NO fail-closed refusal tests: marking them
  certified-only cannot drop fail-closed coverage.
- Fail-closed coverage is env-independent and lives elsewhere: `test_dp_provenance.py`
  (membership + refusal) and `testflight/test_dp_acceptance.py`
  (`test_dp_fit_fails_closed_off_the_certified_stack`, monkeypatched, runs on every env).
- Param-validation tests (e.g. `TestConfigValidation`, bad epsilon -> `DpError`)
  hit the engine's own parameter check BEFORE the cert gate, so they pass in a
  non-certified env too; running them only in the certified job is harmless.
- The certified job `dps-dependency-matrix.yml` currently runs ONLY
  `test_dp_provenance.py`, `test_carriers.py`, `test_dp_budget.py`. It does NOT
  run the five fit-success files. So any fix MUST add a certified run of them, or
  they would never execute in CI (false green).
- `tests/conftest.py` has no hooks today.

## Design (recommended)

A pytest marker `dp_certified` plus a `conftest.py` collection hook that SKIPS
marked items when the running env is not a certified DP proof-stack, and a new
step in the certified `dps-dependency-matrix` job that RUNS the marked tests in
the certified profile.

Why skip-in-conftest over `-m "not dp_certified"` deselection: the tests are
still collected everywhere and reported as SKIPPED with a reason in the
non-certified jobs (visible, self-documenting), and no per-job `-m` line has to
be edited. The env decides, automatically.

### 1. Certification predicate (single source of truth)

Add `is_certified_dp_env() -> bool` to a shared, non-test module the conftest and
tests can import (proposed: a small helper `tests/_dp_cert.py`, or a public
function on `dp_provenance` if we want it reusable outside tests). It computes:

```
key = (dp_provenance.current_platform(), dp_provenance.current_cpython())
members = dp_provenance._CERTIFIED_STACKS.get(key)
return members is not None and dp_provenance.compute_lock_fingerprint(
    dp_provenance.installed_distribution_set()) in members
```

This is the same predicate already used ad hoc in `testflight/test_dp_acceptance.py`;
consolidate to one definition and have the acceptance test import it too (remove
the duplicate `_is_certified`).

### 2. Marker + conftest skip hook

- Register the marker in `pyproject.toml` `[tool.pytest.ini_options].markers`:
  `"dp_certified: requires the certified DP proof-stack (fit_dp_snapshot only
  completes in the 77-dist dev+lint+vault 3.10.20 profile); skipped elsewhere"`.
- In `tests/conftest.py` add:
  ```
  def pytest_collection_modifyitems(config, items):
      if is_certified_dp_env():
          return
      skip = pytest.mark.skip(reason="requires the certified DP proof-stack; this env is not it")
      for item in items:
          if item.get_closest_marker("dp_certified"):
              item.add_marker(skip)
  ```

### 3. Mark the fit-success tests

- DP-specific files (whole file is DP fit-success/param-validation) get a
  module-level `pytestmark = pytest.mark.dp_certified`:
  `test_dp.py`, `test_dp_flag_e2e.py`, `test_generate_dp_contract.py`.
- General files with a single DP-fit test among unrelated tests
  (`test_generation_plan.py`, `test_serialize.py`): mark ONLY the DP-fit test
  function/class with `@pytest.mark.dp_certified`, NOT the module, so their
  non-DP tests keep running in every job. Identify the exact test in each during
  build (1 fit call each).

### 4. Run the marked tests in the certified job

Add a step to `dps-dependency-matrix.yml` (which already installs the certified
77-dist profile) that runs the fit-success suites and FAILS if none are
collected (guards against silent deselection / a stale marker):

```
- name: DP fit-success suite (certified profile)
  run: uv run pytest -m dp_certified tests/unit/quality/test_dp.py
       tests/unit/quality/test_dp_flag_e2e.py
       tests/unit/generation/test_generate_dp_contract.py
       tests/unit/plan/test_generation_plan.py tests/unit/plan/test_serialize.py
       -q -p no:cacheprovider
- name: Guard - dp_certified suite was not empty
  run: test "$(uv run pytest -m dp_certified <same paths> --collect-only -q | tail -1)" # assert N>0 collected
```
(Exact non-empty guard finalized at build; intent: fail if 0 tests ran.)

## Observable behavior (definition of done)

- Certified 77-dist env (local or the dps-dependency-matrix job): the five files'
  fit-success tests RUN and PASS. `is_certified_dp_env()` returns True.
- Any non-certified env (regression-gate, local dev in a random venv): the
  `dp_certified` items are COLLECTED and SKIPPED with a clear reason; the job has
  zero DP fit failures. Param-validation and fail-closed tests are unaffected.
- Fail-closed coverage (`test_dp_provenance`, `test_dp_acceptance` refusal arm)
  still RUNS on every env.
- No non-DP test in `test_generation_plan.py` / `test_serialize.py` is skipped in
  the full-suite jobs.

## Known failure modes to guard against

1. False green: marked tests deselected everywhere and never run in a certified
   job. Guard: the new certified-job step + a non-empty-collection assertion.
2. Over-marking a general file at module level, silently narrowing non-DP
   coverage to the certified job only. Guard: mark general files at function level;
   verify their non-DP tests still run under `-m "not dp_certified"`-equivalent.
3. Predicate wrong (skips in the certified env too, or never skips). Guard: an
   acceptance check that the predicate is True in the 77-dist env and False in a
   perturbed env.
4. A NEW fit-success test added later without the marker reds the full-suite job
   again. Accept as residual (the marker is opt-in); a follow-up could add a
   guard that any test importing `fit_dp_snapshot` must be marked.

## Acceptance tests (author before/with the change; red now, green after)

A1. In the certified 77-dist env: `pytest -m dp_certified <the 5 files>` collects
    N>0 and all pass. RED before (they error `dp_stack_uncertified` if the env is
    wrong / GREEN after when run in the certified profile).
A2. Simulated non-certified env: force `is_certified_dp_env()` False (monkeypatch
    the predicate or the fingerprint) and assert a representative `dp_certified`
    test is skipped, while a non-marked param-validation test and the fail-closed
    refusal test still run. New meta-test in `tests/unit/quality/` (env-independent,
    deterministic).
A3. The `dps-dependency-matrix` job's new step is non-empty (collected > 0).
A4. Full local run in a NON-certified venv (e.g. `--extra dev` 71-dist, which is
    uncertified): `pytest tests -m "not benchmark and not packaging and not codspeed"`
    has zero DP `dp_stack_uncertified` errors (the reproduction of the CI red;
    RED before the fix, GREEN after).

## Verification

- Local: build the 71-dist `--extra dev` env (uncertified) and run A4 -> the DP
  fit failures are gone (skipped). Build the 77-dist certified env and run A1 ->
  they run and pass. Run the new meta-test A2 in both.
- CI: re-run PR #110; `regression-gate` green (DP tests skipped), the certified
  `dps-dependency-matrix` job runs+passes the fit-success suites.

## Out of scope

- The residual "a new unmarked fit test reds CI" guard (failure mode 4) — note as
  a follow-up, do not build here.
- Any change to the DP certification design itself (the single-profile pinning is
  intentional and stays).

## Codex plan-review revisions (2026-07-24) — REQUIRED, override the above on conflict

Codex verdict REVISE. Each finding and the corrected spec:

### R1 (P1) — Predicate wraps the real gate; never raises during collection
`is_certified_dp_env()` must NOT re-read private `_CERTIFIED_STACKS`. Implement as a
test-private wrapper around the REAL gate, so it is single-source and cannot break
collection:
```
def is_certified_dp_env() -> bool:
    try:
        dp_provenance.check_fit_environment()
        return True
    except Exception:            # ProvenanceError -> uncertified; any other error
        return False             # during collection must also degrade to "skip", never crash
```
Put it in `tests/_dp_cert.py`; `tests/conftest.py` and `testflight/test_dp_acceptance.py`
import it (drop the duplicate `_is_certified`). No new PUBLIC library API.

### R2 (P1) — Item-level marking ONLY; keep off-stack coverage. Corrected inventory
Do NOT module-mark. Mark ONLY the individual tests that call `fit_dp_snapshot` with
VALID params expecting a SUCCESSFUL fit (cross the cert gate). Leave running off-stack:
param/signature/schema validation (fails before the gate), serialization, budget,
artifact-shape, and fail-closed tests. Verified pointers from Codex:
- `test_dp.py`: has validation that runs BEFORE `check_fit_environment` (dp.py:316;
  test_dp.py:170 `TestConfigValidation`). Do NOT module-mark. Mark only the
  fit-success classes/tests (e.g. `TestDpFitIsIndependentOfExactSnapshot`,
  `TestArtifactShape`, `TestCategoricalRelease`, `TestUnseededStatisticalMechanism`,
  `TestRecordwiseNormalization`, `TestReleaseIds` — confirm each item calls a real
  successful fit during build).
- `test_generate_dp_contract.py`: has MANY pure declaration/serialization/budget/
  artifact-shape/fail-closed tests, incl. uncertified-record REJECTION (test:370, :899).
  Do NOT module-mark. For synthetic generation-contract artifacts that today read
  `current_provenance()`, switch them to a FROZEN certified manifest record so they
  stay runnable off-stack; mark only the tests that must actually fit on the stack.
- `test_dp_flag_e2e.py`: has a pure schema-freezing test (test:209) not needing a fit.
  Mark only the fit-crossing items.
- `test_generation_plan.py`: ALL 3 tests fit via `_dp_fit_mixed()` (test:69). Mark all 3.
- `test_serialize.py`: 7 tests fit via `_compiled_dp_plan` / `_compiled_dp_plan_two_snapshots`;
  only `test_plan_from_yaml_without_generation_block_round_trips_none` (test:295) is
  independent. Mark the 7, not the module.
Build step: enumerate item-by-item (pytest --collect-only + read each), mark the
fit-crossing set, and record the final list in the PR.

### R3 (P1) — A real, unconditional, execution-proving certified gate
Add a dedicated single-row job to `ci.yml` (NOT the path-filtered dps-dependency-matrix,
which can fail to trigger on a later DP regression), pinned to Python 3.10.20, installing
the certified 77-dist `dev+lint+vault` profile. It must:
1. Assert the env is certified first: `python -c "from decoy_engine.quality.dp_provenance import check_fit_environment; check_fit_environment()"` (fails the job loudly if the profile drifts).
2. Run `pytest -m dp_certified <the 5 files> --junitxml=dp.xml`.
3. Assert from that SAME run that tests EXECUTED, not skipped: parse dp.xml and fail
   unless `passed > 0 and skipped == 0 and errors == 0 and failures == 0`. A collect-only
   count is insufficient (cannot distinguish executed from skipped).

### R4 (P1) — Fix the pre-existing provenance test so A4 goes green
`test_dp_provenance.py::test_pinned_row_matches_this_env_when_this_env_is_certified`
(test:149) skips only when the (platform, cpython) KEY is absent; on an uncertified
3.10.20 host the key exists, so it asserts a mismatched fingerprint and FAILS. Change its
guard to the full predicate (`if not is_certified_dp_env(): skip`) OR mark it `dp_certified`.
Add it to the certified execution inventory.

### R5 (P2) — A2 tests the marking DECISION, not via runtime monkeypatch
`pytest_collection_modifyitems` runs before test bodies/fixtures, so a normal test cannot
monkeypatch the predicate to observe skipping. Instead factor the per-item decision into a
PURE helper, e.g. `should_skip_dp(item_has_marker: bool, certified: bool) -> bool`, and
unit-test that helper directly (deterministic, env-independent). Optionally add one
`pytester`/subprocess test that runs a tiny marked test under a forced-uncertified predicate
and asserts it is skipped. Drop A2's "non-marked param test under module mark" wording (no
module mark exists now).

### Revised acceptance tests
- A1 (unchanged intent): in the certified 77-dist env, the marked set runs and passes.
- A2 (revised, R5): pure-helper unit test of `should_skip_dp`; plus optional pytester check.
- A3 (revised, R3): the certified job asserts `passed>0, skipped=0` via JUnit XML, not a
  collection count.
- A4 (revised, R4): in an uncertified 3.10.20 env, `pytest tests -m "not benchmark and not
  packaging and not codspeed"` has ZERO `dp_stack_uncertified` errors AND the provenance
  test above is fixed so it does not fail either.
- A5 (new): the certified gate is unconditional on PRs (no path filter can skip it).

### Build order
1. `tests/_dp_cert.py` predicate (R1) + register `dp_certified` marker in pyproject.
2. `tests/conftest.py` hook using the predicate + the pure `should_skip_dp` helper (R1,R5).
3. Item-by-item marking inventory across the 5 files (R2); frozen-record refactor for the
   off-stack generation-contract artifact tests (R2).
4. Fix the provenance test (R4).
5. New unconditional certified 3.10.20 gate in ci.yml with the JUnit execution assertion (R3).
6. Acceptance tests A2 (pure helper) + verify A1/A3/A4/A5.
7. Verify locally in BOTH a 71-dist `--extra dev` (uncertified) env and the 77-dist certified
   env; then push and confirm the CI matrix.
