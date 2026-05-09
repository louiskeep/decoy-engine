# decoy-engine — Claude Context

Shared Python data engine. The only repo that contains data manipulation logic. Both `decoy` (CLI) and `decoy-platform` (API) import this as a library — neither contains masking or generation code.

## What's next?

The cross-repo roadmap lives in **[forge-platform/ROADMAP.md](../forge-platform/ROADMAP.md)**. Start there for "where should we go?" questions, then come back here for engine-specific guides and plans.

## Docs in this repo

We use two doc types. Distinguishing them keeps long-term plans aligned and short-term plans from rotting.

- **Guides** are durable specs describing target state. Filename: `*_GUIDE.md` (or `SHARED_ENGINE_ARCHITECTURE.md` — kept under that name because "architecture" reads well), repo root. Header carries `Status:` (`target` / `partial` / `superseded`) and `Last reviewed:`. When a feature ships, the implementer updates the relevant guide in the same PR.
- **Plans** are transient, scoped to a PR or sprint. Live in `plans/`, dated. Header carries `Status:` (`planning` / `in-progress` / `shipped` / `abandoned`), `Branch:`, and `References:` (the guides being implemented). Once a plan ships, it can be deleted — git history is the archive.

Orientation files (this `CLAUDE.md`, `dev-help.md`, `README.md`) are conventional contributor entry points and stay outside the guide/plan taxonomy. The cross-repo **ROADMAP.md** lives in `forge-platform/`.

## Comment style

Comments explain what a section / code block does in good detail, in **1–2 sentences**. Reach for more only when the block is genuinely complex — a state machine, a non-obvious algorithm, security-sensitive math, a workaround for a specific bug. Default mode: terse and to the point.

- **Yes:** `# Format inference is the whole point — pandas warns when it falls back to dateutil; suppress.`
- **No:** silent code with no context.
- **No:** restating what the next ten lines obviously do.

Comments live next to the surprise, not at the top of the file. If the non-obvious thing is the *why*, write that, not the *what*.

### Active guides

- [SHARED_ENGINE_ARCHITECTURE.md](SHARED_ENGINE_ARCHITECTURE.md) — engine architecture + the shared-library rationale. Includes the three-engine hybrid substrate (DuckDB / Polars / Pandas over Arrow). *(partial)*
- [STORM_FORECAST_GUIDE.md](STORM_FORECAST_GUIDE.md) — STORM (analysis) + FORECAST (recommender) module spec. *(partial)*
- [DISGUISES_GUIDE.md](DISGUISES_GUIDE.md) — Disguise YAML schema + the 8-bundle launch set spec. *(partial)*
- [PIPELINE_GRAPH_GUIDE.md](PIPELINE_GRAPH_GUIDE.md) — engine-side mirror of the cross-repo graph pipeline contract; `decoy_engine.graph` package. *(partial)*
- [CONNECTOR_SDK_CONTRACT.md](CONNECTOR_SDK_CONTRACT.md) — connectors return `pyarrow.Table`; runner converts at op boundaries. *(target)*
- [POLARS_FOR_PANDAS_USERS.md](POLARS_FOR_PANDAS_USERS.md) — contributor cheat sheet for the Polars relational ops. *(target)*

The `Logger` Protocol in `decoy_engine.context` is owned by the platform's [LOGGING_GUIDE.md](../forge-platform/LOGGING_GUIDE.md) (sections 4 + 5). Engine entry points emit through the Protocol; the platform's `JobLogger` adapts it to job-log persistence + companion structured tables.

## Repo structure

```
src/decoy_engine/
├── __init__.py          ← PUBLIC API — the only contract CLI/platform devs depend on
├── masker/              ← Masker class (entry point for masking)
├── context.py           ← ExecutionContext, Logger Protocol, TelemetryClient Protocol
├── exceptions.py        ← DecoyError and subclasses (ForgeError is a deprecated alias)
├── validation.py        ← validate_config public helper
├── transforms/          ← 8 masking strategies (faker, hash, redact, map, shuffle, passthrough, date_shift, formula)
├── generators/          ← DataGenerator, ColumnGenerator, RelationshipHandler
├── connectors/          ← CSV, fixed-width, database I/O
├── schema/              ← SchemaInspector (stub)
├── license/             ← LicenseVerifier (stub)
└── internal/            ← PRIVATE: base classes, integrity, validators, logging, helpers
tests/
├── unit/                ← per-module unit tests
└── integration/         ← full Masker and DataGenerator tests
```

## What is NOT in this repo

- CLI commands → `decoy` repo (formerly `forge`)
- HTTP endpoints, auth, jobs, scheduling → `decoy-platform` repo
- Marketing website → `decoy-web` repo

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
