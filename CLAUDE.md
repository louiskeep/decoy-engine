# forge-engine — Claude Context

Shared Python data engine. The only repo that contains data manipulation logic. Both `forge` (CLI) and `forge-platform` (API) import this as a library — neither contains masking or generation code.

## Repo structure

```
src/forge_engine/
├── __init__.py          ← PUBLIC API — the only contract CLI/platform devs depend on
├── pipeline.py          ← Pipeline class (entry point for masking)
├── config.py            ← PipelineConfig (Pydantic v2, loads YAML)
├── context.py           ← ExecutionContext, Logger Protocol, TelemetryClient Protocol
├── exceptions.py        ← ForgeError and subclasses
├── transforms/          ← 8 masking strategies (faker, hash, redact, map, shuffle, passthrough, date_shift, formula)
├── generators/          ← DataGenerator, ColumnGenerator, RelationshipHandler
├── connectors/          ← CSV, fixed-width, database I/O
├── schema/              ← SchemaInspector (stub)
├── license/             ← LicenseVerifier (stub)
└── internal/            ← PRIVATE: base classes, integrity, validators, logging, helpers
tests/
├── unit/                ← per-module unit tests
└── integration/         ← full Pipeline and DataGenerator tests
```

## What is NOT in this repo

- CLI commands → `forge` repo
- HTTP endpoints, auth, jobs, scheduling → `forge-platform` repo
- Marketing website → `forge-web` repo

If a task involves terminal UX, HTTP, or databases — stop, it belongs in a different repo.

## Public API rule

**Only names in `__init__.py.__all__` are public.** Everything under `internal/` is private and can change without a major version bump. When adding new public exports, add them to `__all__` explicitly.

## Setup

```bash
pip install -e .
```

## Run tests

```bash
pytest tests/                    # all tests
pytest tests/unit/               # unit only (fast)
pytest tests/integration/        # integration only
pytest -x                        # stop on first failure
pytest -k "test_faker"           # run matching tests
```

## Branch workflow

**Never commit directly to `main`.** All work goes on a feature branch.

```bash
git checkout -b feature/your-feature-name
# do work, commit
# open PR → wait for approval before merging to main
```

Branch naming: `feature/`, `fix/`, `chore/`
