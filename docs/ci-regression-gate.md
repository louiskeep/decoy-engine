# CI regression gate

The `regression-gate` job in `.github/workflows/ci.yml` is the single
required check that vets every engine change across all correctness
families. It runs the whole non-benchmark test suite in one invocation
(`pytest tests -m "not benchmark"`), preceded by a routing guard that
fails the build if any named suite directory is renamed or removed
without updating the list. Running the suite once (rather than as N
per-family pytest calls) is faster and is safe against silently dropping
a directory; the routing guard makes the coverage explicit.

## Module-to-suite map

| Regression family | Suite path(s) | What it guards |
|---|---|---|
| relational | `tests/integration/golden/` | row-count, FK, join and groupby invariants on golden fixtures |
| parity | `tests/parity/` | pandas adapter equals polars adapter across strategies and graph ops |
| determinism | `tests/unit/determinism/`, `tests/integration/golden/` | key-derivation vectors, cross-process stability, namespace independence |
| golden | `tests/integration/golden/` | engine-v2 S1 golden fixture suite (CSVs plus manifests) |
| quality | `tests/unit/quality/`, `tests/snapshots/` | fidelity, diagnostic, policy, DCR and attack metrics plus frozen snapshots |
| sentry | `tests/sentry/` | source-policy guards: eval, mojibake, brand, stale paths, raw em-dash/arrow |
| security | `tests/security/` | redaction, expression scope, no PII in output |
| privacy | `tests/privacy/` | disclosure-risk and privacy-metric guards |

Overlaps are intentional: the golden suite is where relational and
determinism invariants are asserted, so those families share files. The
map documents the overlap rather than partitioning the files artificially.

## What is NOT in this gate

- Benchmarks (`tests/benchmark/`, pytest marker `benchmark`): informational,
  run on a separate workflow, never a regression gate (shared-runner perf
  numbers are noise).
- Substrate matrix (`engine-v2-substrate-matrix.yml`) and parity
  (`engine-v2-parity.yml`): separate required workflows, currently
  path-filtered to the execution and graph-op trees.
- Docs build (`docs.yml`): a build gate, not a data-behavior gate.

## Required checks (branch protection)

At merge time, `main` branch protection should require `regression-gate`,
`ruff`, `mypy`, and the parity and substrate checks (which pass when their
path filters skip them). Benchmark and docs jobs are not required.

## Verifying the gate ("routing verified")

1. The routing guard step passes (every named suite directory resolves),
   and it fails loudly with `MISSING SUITE DIR` when a directory is gone.
2. The full run collects every family in the map above.
3. A green PR shows the configured required checks green; a deliberately
   broken assertion turns `regression-gate` red.
4. This map and the comment block in `ci.yml` list the same families.
