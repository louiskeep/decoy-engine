# Flaky-test quarantine policy

A flaky test passes and fails on the same code. Left in the blocking gate it
does two kinds of damage: it blocks honest PRs, and it trains everyone to
re-run red CI without reading it, which is how a real regression slips through.
This is the policy for getting a flaky test out of the gate fast without losing
track of it.

## Prefer a fix over a quarantine

Quarantine is a stopgap, not a destination. Most flakiness in this repo comes
from timing assertions (a `time.sleep` plus a wall-clock `assert`); those
should be rewritten to assert the invariant structurally rather than by a timing
margin. TEST-6 is the worked example: the concurrency defer test
(`decoy-platform tests/test_h3_concurrency.py`) keeps its wall-clock version but
adds a deterministic `_claim_next_job`-level assertion that proves the same
behavior with no sleep. Reach for quarantine only when the fix is not immediate
and the flake is blocking others now.

## Never quarantine a safety-critical test

Tests that guard crypto, referential integrity, PII handling, or any
fail-closed privacy path are **ineligible** for quarantine. Decoy's whole
promise is that these hold, so dropping one from the blocking gate for even a
day is a real coverage hole in exactly the place a regression would do the most
harm. If such a test flakes, fix it immediately or revert the change that made
it flake. Do not mark it `flaky`.

## How to quarantine

1. Mark the test `@pytest.mark.flaky` (registered in `pyproject.toml`).
2. In the marker's reason or a comment, link a tracking issue and the date.
3. Open the tracking issue: the failing test, an observed failure, and the
   suspected cause.

That is all the gate needs. `flaky` tests are excluded from the blocking
regression gate (`ci.yml` runs `-m "... and not flaky"`) so they stop gating
merges, and the non-blocking `flaky-quarantine` CI job keeps running them so
they stay visible. A quarantined test is still executed on every push; it just
cannot fail the workflow.

## De-quarantine SLA

A quarantined test is fixed (preferably a deterministic rewrite) or deleted
within **two weeks**. A test that can neither be trusted nor fixed is worse than
no test: it is coverage theater. If two weeks pass with no fix, delete it and
file the coverage gap as its own issue, so the gap is honest rather than hidden
behind a green-but-skipped check.

## How this fits the rest of the suite

- **Marker segregation (already in place).** The `benchmark` and `codspeed`
  microbenchmarks run outside the blocking gate by marker. (`perf` baselines,
  despite the name, DO run in the gate.) So the heaviest timing tests already do
  not gate merges. `flaky` is for a test that runs in the gating suite and
  proves intermittent there.
- **Mergify auto-detection (pending app install).** `.mergify.yml` +
  `CAM-STEPS.md` §2 wire Mergify's JUnit-based flaky-test detection; the gate
  already uploads `junit-results.xml` for it. Once the Mergify app is installed,
  detection is automatic. The manual `flaky` marker is the complement an
  engineer uses in the moment, before or alongside that automation.

## Platform

The platform repo (`decoy-platform`) has no marker or CI-rerun convention yet
and its main backend CI is currently red for an unrelated pre-existing reason.
Extend the same `flaky` marker + non-blocking-job convention there once that CI
is green; until then a platform flake is handled case by case.
