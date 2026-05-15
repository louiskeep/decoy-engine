# Decoy Engine Codemap

## One-Line Project Summary

Shared Python data engine for Decoy masking, generation, graph pipelines, connectors, STORM profiling, and FORECAST recommendations.

## Tech Stack

| Area | Stack |
|---|---|
| Runtime | Python 3.10 |
| Data | pandas, Polars, DuckDB, PyArrow |
| Config | YAML, Pydantic where used |
| Tests | pytest |
| Consumers | `decoy` CLI and `decoy-platform` API |

## Entry Points

| Path | Purpose |
|---|---|
| `src/decoy_engine/__init__.py` | Public API exports |
| `src/decoy_engine/validation.py` | Public validation helper |
| `src/decoy_engine/graph/runner.py` | Current graph execution control center |
| `src/decoy_engine/masker/masker.py` | Legacy masking entry point |
| `src/decoy_engine/generators/generator.py` | Legacy generation entry point |

## Directory Map

| Path | What Lives Here |
|---|---|
| `src/decoy_engine/` | Engine package |
| `src/decoy_engine/_MAP.md` | Engine package navigation map |
| `src/decoy_engine/graph/` | Graph config, runner, conversion, ops |
| `src/decoy_engine/transforms/` | Masking strategies |
| `src/decoy_engine/generators/` | Synthetic data generation |
| `src/decoy_engine/connectors/` | Legacy IO and cloud/file connector SDK |
| `src/decoy_engine/storm/` | Profiling and detectors |
| `src/decoy_engine/forecast/` | Recommendation logic |
| `src/decoy_engine/internal/` | Private validators/helpers/cache/memory/integrity |
| `tests/` | Unit, integration, graph, storm, parity, benchmark tests |
| `docs/` | Local Sphinx docs where present; active planning is in `../decoy-platform/docs/` |
| `.pytest_cache/`, `__pycache__/`, `logs/`, `mappings/` | Ignore generated/runtime content |

## Where Do I Find...

| Task | Start Here |
|---|---|
| Current roadmap | `../decoy-platform/docs/ROADMAP.md` |
| Engine audit | `../decoy-platform/docs/audit/codebase-audit-map.md` |
| Remediation plan | `../decoy-platform/docs/audit/remediation-roadmap.md` |
| Public exports | `src/decoy_engine/__init__.py` |
| Graph ops | `src/decoy_engine/graph/ops/` |
| Graph runner | `src/decoy_engine/graph/runner.py` |
| Graph op registry | `src/decoy_engine/graph/ops/__init__.py` |
| Validation internals | `src/decoy_engine/internal/validator.py`, `src/decoy_engine/validation.py` |
| Masking transforms | `src/decoy_engine/transforms/` |
| Legacy masker | `src/decoy_engine/masker/` |
| Generation | `src/decoy_engine/generators/` |
| Connectors | `src/decoy_engine/connectors/`, `src/decoy_engine/sdk.py` |
| STORM | `src/decoy_engine/storm/` |
| FORECAST | `src/decoy_engine/forecast/` |
| Parity notes | `tests/parity/SEMANTIC_DIFFERENCES.md` |

## Conventions

| Situation | Convention |
|---|---|
| Add public API | Export deliberately from `__init__.__all__` |
| Add graph op | Add module under `graph/ops/`, declare constants, register in `ops/__init__.py`, test graph behavior |
| Add transform | Add strategy under `transforms/`, register if needed, test determinism/null/type behavior |
| Add connector | Implement engine contract first, test with fakes/contract tests |
| Add STORM detector | Add detector provenance, positive/negative tests, and FORECAST mapping if needed |
| Shared CLI/platform behavior | Implement here first, then wrap in CLI/platform |

## Gotchas

| Gotcha | Note |
|---|---|
| `graph/runner.py` is overlarge | Audit plan wants planner/executor/cache/events split |
| Validation mutates in places | Remediation wants explicit errors/warnings/normalized config |
| Expression safety is sensitive | Avoid adding direct `eval()` paths |
| Engine is library code | Do not import platform or CLI |
| Public stubs exist | Check capability docs before claiming production behavior |

## Ignore For Navigation

| Path | Reason |
|---|---|
| `.pytest_cache/`, `__pycache__/`, `.mypy_cache/`, `.ruff_cache/` | Generated |
| `logs/`, `mappings/` | Runtime output |
| `docs/_build/` | Generated docs |
| `tests/benchmark/calibration/results.md` | Read only for benchmark tasks |
