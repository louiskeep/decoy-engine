# CI regression gate

The `regression-gate` job in `.github/workflows/ci.yml` vets every engine change across all
correctness families. It runs the default test suite in one invocation (the `-m` expression
matches `PYTEST_ARGS` in `scripts/dev/gates.sh`), preceded by a routing guard that
fails the build if any named suite directory is renamed or removed
without updating the list. Running the suite once (rather than as N
per-family pytest calls) is faster and is safe against silently dropping
a directory; the routing guard makes the coverage explicit.

## Module-to-suite map

| Regression family | Suite path(s) | What it guards |
|---|---|---|
| relational | `tests/integration/golden/` | row-count, FK, join and groupby invariants on golden fixtures |
| parity | `tests/parity/` | pandas chunked equals pandas full-frame byte-for-byte across strategies |
| determinism | `tests/unit/determinism/`, `tests/integration/golden/` | key-derivation vectors, cross-process stability, namespace independence |
| golden | `tests/integration/golden/` | engine-v2 S1 golden fixture suite (CSVs plus manifests) |
| quality | `tests/unit/quality/`, `tests/snapshots/` | fidelity, diagnostic, policy, DCR and attack metrics plus frozen snapshots |
| sentry | `tests/sentry/` | source-policy guards: eval, mojibake, brand, stale paths, raw em-dash/arrow |
| security | `tests/security/` | redaction, expression scope, no PII in output |
| privacy | `tests/privacy/` | disclosure-risk and privacy-metric guards |
| perf guard | `tests/perf/` | throughput budgets and out-of-core memory sentinels; runs under `pytest -m "not benchmark"` but is NOT in the routing guard list (see ci.yml) |

Overlaps are intentional: the golden suite is where relational and
determinism invariants are asserted, so those families share files. The
map documents the overlap rather than partitioning the files artificially.

## What is NOT in this gate

- Benchmarks (`tests/benchmark/`, pytest marker `benchmark`): informational,
  run on a separate workflow, never a regression gate (shared-runner perf
  numbers are noise).
- Docs build (`docs.yml`): a build gate, not a data-behavior gate.

## Who runs what

The self-hosted gate (`gate-selfhosted.yml`, `scripts/dev/gates.sh`) is the merge gate: it runs
ruff, mypy, the layer contract and the full default suite on pushes to main and on dispatch. Hosted
`ci.yml` repeats `ruff`, `mypy`, `regression-gate` and `layers-gate` only on `schedule` and
`workflow_dispatch` (they carry `if: github.event_name != 'pull_request'`), so a PR does not run
them twice.

Every other lane runs on each pull request and nightly on main: the profile gates in `ci.yml`
(`packaging-gate`, `ml-gate`, `ner-gate`, `dp-certified-gate`, `determinism`),
`native-companion.yml` and the `docs.yml` build. `testflight.yml`, `dps-dependency-matrix.yml` and
`security.yml` run nightly. No PR or schedule trigger starts a self-hosted job.

The determinism harness (`-m determinism_harness`, one test of about 11 minutes) runs in the
`determinism` job. The default marker expression and `gates.sh` exclude it. If that job exceeds 20
minutes on a pull request, give it `if: github.event_name != 'pull_request'`, drop it from
`ci/required-jobs.toml`, and restore the old expression in `gates.sh` and `regression-gate`.

Two registries drive the sentry `tests/sentry/tooling/test_ci_lanes.py`:

- `ci/required-jobs.toml`: the jobs that must be green on a PR head (`merge`), the PR jobs that
  need not be (`merge_exempt`), and the profile markers a whole-suite run does not cover.
- `ci/unlaned.toml`: markers and workflows with no automatic lane (benchmark, codspeed), each with
  a reason. The list can only shrink.

The sentry fails when a registered marker has no automatic lane, a workflow has no automatic
trigger, a trigger has a path filter, a job lacks `timeout-minutes`, a PR workflow lacks the shared
concurrency block, `gates.sh` and `regression-gate` select different tests, or the registries and
workflows disagree.

`docs.yml` has no `push` trigger while deploy is off (`vars.DOCS_DEPLOY` unset). Enabling deploy
re-adds `push` to `main` in the same change.

Before each engine merge, run `scripts/dev/required_checks.py verify --sha <pr head> --tier merge`
with the engine venv Python and `GH_TOKEN` set. It reads the required list from the `origin/main`
tip, not the branch, and requires every merge job green on its first attempt (`--allow-rerun
WORKFLOW=REASON` logs a named exception for a pure download flake). `verify --tier nightly` says
whether last night's scheduled runs on main are fresh and green; `contexts` prints the check names
for branch protection.

Branch protection (requiring the `merge` jobs on `main`) is a repository setting and is not
applied by this change.

## Verifying the gate ("routing verified")

1. The routing guard step passes (every named suite directory resolves),
   and it fails loudly with `MISSING SUITE DIR` when a directory is gone.
2. The full run collects every family in the map above.
3. A green PR shows the configured required checks green; a deliberately
   broken assertion turns `regression-gate` red.
4. This map and the comment block in `ci.yml` list the same families.
