# Decoy — Shared Engine Architecture

> **Status:** partial — the engine ships and is consumed by `decoy` (CLI) and `decoy-platform`. Public API stable. STORM/FORECAST modules added 2026-05-04. Disguises framework partial (see `DISGUISES_GUIDE.md`).
> **Last reviewed:** 2026-05-04
> **Purpose:** Solves the "doubling work between CLI and Platform" problem by introducing a shared engine library that both consume.
> **Read this before:** Writing any code that could plausibly live in either the CLI or the Platform.

---

## TL;DR

You don't have two codebases. You have **three** â€” a shared engine and two thin wrappers around it.

```
decoy-engine   â†’  Python library. Does the actual data work. (Public, BUSL)
forge          â†’  CLI wrapper. Imports decoy-engine. (Public, BUSL)
forge-platform â†’  Web app wrapper. Imports decoy-engine. (Private)
```

When you add a new mask transform, connector, or YAML feature, you add it to **`decoy-engine` once**. The CLI and Platform both pick it up automatically on the next engine version bump.

Result: **no doubling on data work**. The CLI and Platform only diverge on interface-specific code (terminal UX vs. web UX), which is naturally separate anyway.

This pattern is what dbt, Prefect, Dagster, Meltano, and Sentry all use. It's the proven architecture for free-tool-plus-paid-platform companies.

---

## The Problem This Solves

Without a shared engine, you face this every time you ship a feature:

1. Add a new mask transform â†’ write it in the CLI codebase
2. Realize the Platform needs it too â†’ copy/paste it to the Platform codebase
3. Fix a bug in the transform â†’ fix it in two places
4. CLI YAML format diverges slightly from Platform format â†’ users get confused
5. Six months later, the two implementations have drifted in subtle ways â†’ support nightmare

This is the classic "two implementations of the same logic" tax. The engine pattern eliminates it.

---

## The Three-Layer Architecture

### Layer 1: `decoy-engine` â€” The Shared Brain

A Python library that does all the actual data work. Knows nothing about terminals, HTTP, web UIs, or users. Just data in, data out.

**Public API surface** (what the CLI and Platform call):

```python
from decoy_engine import (
    Pipeline,           # in-memory pipeline representation
    PipelineConfig,     # Pydantic schema for YAML
    MaskRegistry,       # all available masks
    GeneratorRegistry,  # all synthetic data generators
    ConnectorRegistry,  # all source/destination connectors
    SchemaInspector,    # introspects sources, suggests masks
    LicenseVerifier,    # JWT-based license verification
    ExecutionContext,   # runtime context (logger, telemetry, license)
)

# Usage
config = PipelineConfig.from_yaml(yaml_path)
pipeline = Pipeline(config)
result = pipeline.run(context=ExecutionContext(...))
```

Anything a user could conceivably want to do with their data lives behind this API.

### Layer 2: `forge` â€” The CLI Wrapper

A thin Typer-based CLI that imports `decoy-engine` and exposes it as terminal commands.

```python
# src/forge/cli/run.py
import typer
from decoy_engine import Pipeline, PipelineConfig, ExecutionContext
from forge.ui import RichLogger, TerminalProgressBar

@app.command()
def run(yaml_path: Path, dry_run: bool = False):
    config = PipelineConfig.from_yaml(yaml_path)
    pipeline = Pipeline(config)

    context = ExecutionContext(
        logger=RichLogger(),
        progress=TerminalProgressBar(),
        license=load_local_license(),
    )

    if dry_run:
        pipeline.plan(context).print()
    else:
        pipeline.run(context)
```

The CLI's job is **interface translation**: it takes terminal-flavored input (CLI args, env vars, local files) and gives terminal-flavored output (Rich tables, progress bars, exit codes). The actual masking happens in the engine.

### Layer 3: `forge-platform` â€” The Web Wrapper

A FastAPI backend + Next.js frontend that imports the same `decoy-engine` and exposes it via HTTP and a web UI.

```python
# api/runs.py
from fastapi import APIRouter, Depends
from decoy_engine import Pipeline, PipelineConfig, ExecutionContext
from platform.persistence import save_run_record
from platform.logging import StructuredLogger
from platform.auth import get_current_user

router = APIRouter()

@router.post("/runs")
async def trigger_run(
    pipeline_id: str,
    user = Depends(get_current_user),
):
    config = PipelineConfig.from_db(pipeline_id)
    pipeline = Pipeline(config)

    context = ExecutionContext(
        logger=StructuredLogger(run_id=...),
        progress=NullProgressBar(),  # web UI polls run status separately
        license=user.org.license,
    )

    result = await pipeline.run_async(context)
    save_run_record(result, user)
    return {"run_id": result.id, "status": result.status}
```

Same engine, called differently. The Platform's job is **multi-user concerns**: persistence, scheduling, auth, audit, billing. None of that exists in the engine.

---

## Hybrid Engine Substrate

The engine internally runs a three-engine hybrid: DuckDB at the I/O boundary, Polars for relational ops, pandas for per-row Python (mask transforms, generators, STORM). All three share Apache Arrow as the in-memory substrate so the runner cache holds `pyarrow.Table` and conversion at op boundaries is zero-copy where possible.

```
   ┌────────────────┐  Arrow   ┌────────────────┐  Arrow  ┌────────────────┐
   │ source.file    ├──────────▶  filter / sort ├─────────▶  mask          │
   │ source.db      │          │  dedupe / etc. │         │  generate      │
   │  (DuckDB)      │          │   (Polars)     │         │   (Pandas)     │
   └────────────────┘          └────────────────┘         └───────┬────────┘
                                                                  │
   ┌────────────────┐  Arrow   ┌────────────────┐  Arrow  ┌───────▼────────┐
   │ target.file    ◀──────────┤  filter / sort ◀─────────┤ run_storm      │
   │ target.db      │          │  dedupe / etc. │         │  (Pandas)      │
   │  (DuckDB)      │          │   (Polars)     │         └────────────────┘
   └────────────────┘          └────────────────┘
```

Each op declares `NATIVE_ENGINE` in its module. The runner reads the declaration, materializes the cached Arrow into the op's preferred type at apply-time, and converts the result back to Arrow before caching. Eviction is eager — cache entries are released as soon as their last downstream consumer reads them, so peak memory is bounded by the in-flight working set rather than the full run lifetime.

**Engine boundary by op kind**:

| Engine | Ops | Why |
|---|---|---|
| **DuckDB** | source.file / source.db / target.file / target.db | Best spill-to-disk, native streaming for CSV / parquet, attachable scanners for SQL sources |
| **Polars** | filter / sort / dedupe / derive / drop_column / select_column / limit | Lazy planner, columnar SIMD, parallel by default |
| **Pandas** | mask / generate / run_storm | Per-row Python: Faker, scipy, sklearn — moving these off pandas buys nothing |

**Engine selection**: graphs declare `engine: pandas` or `engine: hybrid` at the YAML top level. Default is `hybrid` (per Phase 8). `engine: pandas` is the manual override — forces every op through its pandas fallback regardless of NATIVE_ENGINE declaration.

**When to use `engine: pandas` instead of the default**: per the Bug 5 calibration (`tests/benchmark/calibration/results.md`), hybrid uses ~1.3× more peak memory than pure pandas at typical scales due to per-op Polars intermediate materialization. For most workloads the difference is in the noise — same EC2 instance class either way — and hybrid runs ~2× faster. The exception is large jobs on memory-tight hosts:

> **Rule of thumb:** if a single pipeline processes more than ~80M rows on a host with less than 64 GB RAM, set `engine: pandas` in that pipeline's YAML. Hybrid mode is faster but uses more peak memory; on tight instances that delta can push the job into OOM. The engine emits a runtime advisory when peak RSS approaches 70% of system memory pointing at this override.

The advisory is RAM-relative (uses `psutil.virtual_memory().total`), so the threshold scales naturally to whatever EC2 instance the customer is running on. Override the threshold via `DECOY_MEMORY_WARN_THRESHOLD` (default 0.7).

**Cross-engine boundary perf**: the Arrow → Polars conversion uses `rechunk=False` to avoid copying string columns into Polars' default chunked layout. Measured impact at 10M HIPAA-shape rows: ~32% memory reduction on hybrid pipelines. See `tests/benchmark/calibration/results.md` for the full data.

For the why behind this and the migration path, see `plans/2026-05-10-polars-duckdb-hybrid-engine.md` and the companion implementation plan in the same directory. Cheat sheet for contributors switching mental models: `POLARS_FOR_PANDAS_USERS.md`.

---

## What Goes Where: The Decision Rules

This is the most important section. When you're about to write a new piece of code, ask:

### "Does this manipulate or describe data?" â†’ Engine

- New mask transform â†’ engine
- New synthetic data generator â†’ engine
- New connector (source or destination) â†’ engine
- YAML schema field â†’ engine
- Validation rule for a YAML field â†’ engine
- Schema introspection â†’ engine
- Referential integrity logic â†’ engine
- Pipeline execution â†’ engine
- License JWT verification â†’ engine

### "Does this involve a terminal?" â†’ CLI only

- Typer command definition â†’ CLI
- Rich-formatted terminal output â†’ CLI
- Progress bar in a TTY â†’ CLI
- Interactive prompts (`forge init`) â†’ CLI
- Local config file (`~/.forge/`) â†’ CLI
- Update check messages â†’ CLI
- ASCII art â†’ CLI
- Tab completion scripts â†’ CLI
- Exit code handling â†’ CLI

### "Does this involve multiple users, persistence, or HTTP?" â†’ Platform only

- FastAPI routes â†’ Platform
- Database models â†’ Platform
- Scheduler / cron jobs â†’ Platform
- Audit log â†’ Platform
- RBAC â†’ Platform
- Auth / sessions â†’ Platform
- Stripe integration â†’ Platform
- License *issuance* (signing) â†’ Platform
- Web UI â†’ Platform
- Email notifications â†’ Platform
- Multi-tenant logic â†’ Platform

### Gray areas (and how to resolve them)

**Logging.** Engine defines a `Logger` *interface* (abstract base class or Protocol). CLI provides a `RichLogger` implementation that prints to stderr. Platform provides a `StructuredLogger` that writes JSON to its log pipeline. Engine never imports either implementation.

**Telemetry.** Same pattern. Engine defines a `TelemetryClient` interface. CLI implements one that POSTs to your analytics endpoint with opt-in. Platform implements one that writes to its internal events table.

**License verification.** This is engine code. Both CLI and Platform need to verify the same JWTs the same way, using the same public key. Putting it in the engine ensures perfect parity. License *issuance* (signing JWTs with the private key) lives in Platform.

**Configuration loading.** YAML parsing is engine. Reading the YAML file from disk is CLI. Reading the YAML from a database row is Platform. The engine accepts a `PipelineConfig` object; the wrappers handle "where does this config come from."

**Errors.** Engine raises typed exceptions (`PipelineValidationError`, `ConnectorAuthError`, `LicenseExpiredError`). CLI catches them and renders friendly Rich-formatted output. Platform catches them and returns appropriate HTTP status codes.

**Caching.** Connector connection pooling is engine. Caching pipeline run results is Platform-only (CLI doesn't cache across runs).

---

## The Engine API Contract

The `decoy-engine` public API is a **contract**. Both the CLI and Platform depend on it. Treating it casually is how you create maintenance pain. Treating it as an API is how you stay sane.

### Versioning

- **SemVer strict.** Breaking changes only on major versions.
- **Deprecation policy.** New behavior introduced as a flag or alternate function. Old behavior deprecated for one major version, then removed.
- **The `__init__.py` is the contract.** Anything exported from `decoy_engine` is public. Anything imported from `decoy_engine.internal.*` is private and can change at any time.

### What "public API" means

```python
# decoy_engine/__init__.py â€” this IS the public API
from decoy_engine.pipeline import Pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.registry import MaskRegistry, ConnectorRegistry
from decoy_engine.context import ExecutionContext, Logger, TelemetryClient
from decoy_engine.exceptions import (
    ForgeError,
    PipelineValidationError,
    ConnectorAuthError,
    LicenseExpiredError,
    ...
)
from decoy_engine.license import LicenseVerifier, License
from decoy_engine.schema import SchemaInspector

__version__ = "1.0.0"
__all__ = [...]  # explicit
```

Anything not in `__all__` is private. The CLI and Platform must only import from the public surface. This rule is enforced by code review, eventually by static analysis.

### Backward compatibility rules

1. **Don't remove public functions/classes** without a major version bump.
2. **Don't change function signatures** without a major version bump (adding optional kwargs is OK).
3. **Don't change YAML schema** without a major version bump (adding optional fields is OK).
4. **Don't change exception types** raised by public functions without a major version bump.
5. **Adding new transforms, connectors, generators is always safe** (additive, no breaking change).

---

## ML Inference Boundary

The STORM detector layer is being extended with an ML stage (column classifier + cell-level NER) on top of the existing regex/name-hint detectors in `src/decoy_engine/storm/detectors.py`. See `decoy-platform/plans/2026-05-06-ml-sensitive-data-detection.md` for the full RFC. The non-negotiables for this engine:

1. **Inference is in-process, in this engine.** No third-party API calls. No data leaves the customer's deployment for classification. Hard constraint — clients run `decoy-platform` on their own servers, and we never touch their data; that contract must extend to ML detection.
2. **Model artifacts are lazily loaded on first ML-enabled scan.** spaCy `en_core_web_lg`, Presidio assets, and the LightGBM column-classifier weights are pulled at first run, not bundled in the engine image. Keeps the base install small.
3. **Regex-only fallback is mandatory.** The engine must start, load, and run the existing regex detector layer with zero ML artifacts present. If the lazy pull fails or the customer is air-gapped, STORM degrades to regex-only and surfaces that state in the detection trail rather than erroring out.
4. **No new public match type.** ML detectors emit the existing `DetectorMatch` shape (`src/decoy_engine/storm/types.py`). The aggregator layer ranks regex and ML matches together; regex wins on the entity types it covers.
5. **Air-gapped offline bundle is a follow-up.** Tracked separately. Until it lands, air-gapped deployments run regex-only and that is the documented behavior.

Adding ML to the engine does not change the public API contract above. The classifier and NER stages live inside `decoy_engine.storm.*` and are not re-exported from `__init__.py`; callers see the same `StormProfile` they always have, with richer `DetectorMatch` provenance inside it.

---

## Repo Structure (Updated)

The full picture:

```
github.com/forgeio/decoy-engine    â† Shared library (PUBLIC, BUSL)
github.com/forgeio/forge           â† CLI (PUBLIC, BUSL)
github.com/forgeio/forge-platform  â† Platform (PRIVATE)
github.com/forgeio/forge-web       â† Marketing site + docs (PUBLIC)
```

### `decoy-engine` skeleton

```
decoy-engine/
â”œâ”€â”€ .github/workflows/
â”‚   â”œâ”€â”€ test.yml                  # multi-OS, multi-Python matrix
â”‚   â”œâ”€â”€ release.yml               # PyPI publish on tag
â”‚   â””â”€â”€ lint.yml
â”œâ”€â”€ src/
â”‚   â””â”€â”€ decoy_engine/
â”‚       â”œâ”€â”€ __init__.py           # public API, controlled exports
â”‚       â”œâ”€â”€ pipeline.py           # Pipeline class
â”‚       â”œâ”€â”€ config.py             # PipelineConfig (Pydantic)
â”‚       â”œâ”€â”€ context.py            # ExecutionContext, Logger interface, TelemetryClient interface
â”‚       â”œâ”€â”€ exceptions.py         # all public exception types
â”‚       â”œâ”€â”€ license/
â”‚       â”‚   â”œâ”€â”€ __init__.py
â”‚       â”‚   â”œâ”€â”€ verifier.py       # JWT verification
â”‚       â”‚   â””â”€â”€ public_key.py     # embedded public key constant
â”‚       â”œâ”€â”€ transforms/
â”‚       â”‚   â”œâ”€â”€ __init__.py
â”‚       â”‚   â”œâ”€â”€ registry.py       # MaskRegistry
â”‚       â”‚   â”œâ”€â”€ faker_based.py    # email, name, phone, etc.
â”‚       â”‚   â”œâ”€â”€ fpe.py            # format-preserving encryption
â”‚       â”‚   â”œâ”€â”€ hashing.py
â”‚       â”‚   â”œâ”€â”€ date_shift.py
â”‚       â”‚   â””â”€â”€ ...
â”‚       â”œâ”€â”€ generators/
â”‚       â”‚   â”œâ”€â”€ __init__.py
â”‚       â”‚   â”œâ”€â”€ registry.py
â”‚       â”‚   â””â”€â”€ ...
â”‚       â”œâ”€â”€ connectors/
â”‚       â”‚   â”œâ”€â”€ __init__.py
â”‚       â”‚   â”œâ”€â”€ registry.py
â”‚       â”‚   â”œâ”€â”€ base.py           # ConnectorBase abstract class
â”‚       â”‚   â”œâ”€â”€ postgres.py
â”‚       â”‚   â”œâ”€â”€ mysql.py
â”‚       â”‚   â”œâ”€â”€ s3.py
â”‚       â”‚   â”œâ”€â”€ snowflake.py
â”‚       â”‚   â””â”€â”€ ...
â”‚       â”œâ”€â”€ schema/
â”‚       â”‚   â”œâ”€â”€ __init__.py
â”‚       â”‚   â””â”€â”€ inspector.py      # SchemaInspector
â”‚       â””â”€â”€ internal/             # private, not part of API contract
â”‚           â”œâ”€â”€ execution.py
â”‚           â””â”€â”€ ...
â”œâ”€â”€ tests/
â”œâ”€â”€ pyproject.toml
â”œâ”€â”€ README.md
â”œâ”€â”€ LICENSE.md
â”œâ”€â”€ CHANGELOG.md
â””â”€â”€ CONTRIBUTING.md
```

### `forge` (CLI) skeleton

```
forge/
â”œâ”€â”€ pyproject.toml                # depends on decoy-engine
â”œâ”€â”€ src/
â”‚   â””â”€â”€ forge/
â”‚       â”œâ”€â”€ __init__.py
â”‚       â”œâ”€â”€ __main__.py
â”‚       â”œâ”€â”€ cli/                  # Typer commands
â”‚       â”‚   â”œâ”€â”€ run.py
â”‚       â”‚   â”œâ”€â”€ validate.py
â”‚       â”‚   â”œâ”€â”€ init.py
â”‚       â”‚   â”œâ”€â”€ demo.py
â”‚       â”‚   â”œâ”€â”€ connectors.py
â”‚       â”‚   â”œâ”€â”€ login.py
â”‚       â”‚   â”œâ”€â”€ schedule.py       # @require_business
â”‚       â”‚   â””â”€â”€ ...
â”‚       â”œâ”€â”€ ui/                   # Rich-based output
â”‚       â”‚   â”œâ”€â”€ logger.py         # implements decoy_engine.context.Logger
â”‚       â”‚   â”œâ”€â”€ progress.py
â”‚       â”‚   â”œâ”€â”€ prompts.py        # interactive prompts
â”‚       â”‚   â”œâ”€â”€ tables.py
â”‚       â”‚   â””â”€â”€ theme.py
â”‚       â”œâ”€â”€ config/               # local config (~/.forge/)
â”‚       â”‚   â”œâ”€â”€ license.py        # license file storage
â”‚       â”‚   â””â”€â”€ settings.py
â”‚       â”œâ”€â”€ telemetry/            # implements decoy_engine.context.TelemetryClient
â”‚       â”‚   â””â”€â”€ client.py
â”‚       â”œâ”€â”€ decorators/
â”‚       â”‚   â””â”€â”€ require_business.py
â”‚       â””â”€â”€ version_check.py
â””â”€â”€ tests/
```

### `forge-platform` skeleton

```
forge-platform/
â”œâ”€â”€ api/                          # FastAPI
â”‚   â”œâ”€â”€ main.py                   # depends on decoy-engine
â”‚   â”œâ”€â”€ auth/
â”‚   â”œâ”€â”€ runs/
â”‚   â”‚   â””â”€â”€ handler.py            # imports decoy_engine.Pipeline
â”‚   â”œâ”€â”€ pipelines/
â”‚   â”œâ”€â”€ billing/
â”‚   â”œâ”€â”€ licenses/
â”‚   â”‚   â””â”€â”€ issuer.py             # signs JWTs with private key
â”‚   â”œâ”€â”€ audit/
â”‚   â”œâ”€â”€ teams/
â”‚   â”œâ”€â”€ persistence/
â”‚   â”‚   â”œâ”€â”€ logger.py             # implements decoy_engine.context.Logger
â”‚   â”‚   â””â”€â”€ telemetry.py          # implements decoy_engine.context.TelemetryClient
â”‚   â””â”€â”€ scheduler/
â”œâ”€â”€ web/                          # Next.js
â”‚   â””â”€â”€ ...
â”œâ”€â”€ deploy/
â””â”€â”€ tests/
```

---

## Development Workflow

### Local development

When you're working on a CLI feature that needs an engine change:

```bash
# Clone all three side by side
git clone forgeio/decoy-engine
git clone forgeio/forge
git clone forgeio/forge-platform

# Install engine in editable mode in both wrappers
cd forge
pip install -e ../decoy-engine -e .

cd ../forge-platform
pip install -e ../decoy-engine -e .
```

Now changes to `decoy-engine` are picked up immediately by both wrappers. No publish/install dance during local dev.

### Release workflow

1. **Engine release.** Bump version in `decoy-engine`, tag, push. CI publishes to PyPI.
2. **Wrapper updates.** Renovate or Dependabot opens PRs in `forge` and `forge-platform` bumping the `decoy-engine` dependency. You review the PR, run CI, merge.
3. **Wrapper releases.** Bump CLI version, tag, push. CI publishes new CLI to PyPI. Bump platform version, deploy.

This sounds like more steps than monolith. It's not â€” it's the *same* steps, just more visible. In a monolith you'd still test the integration before shipping; this just makes the integration explicit.

### When to release the engine

- **Bug fix in a transform** â†’ patch release of engine, both wrappers update on next sync
- **New transform / connector / generator** â†’ minor release of engine, both wrappers update
- **Breaking YAML schema change** â†’ major release of engine, coordinated update of both wrappers (rare)
- **Pure CLI UX improvement** â†’ no engine release; just patch the CLI
- **Pure platform feature (web UI, scheduling)** â†’ no engine release; just deploy platform

You'll find that **most of your releases are engine releases**. That's the intended outcome â€” it confirms the architecture is working.

---

## Migration Path (If You've Already Started Building)

If you've already built some CLI code, don't panic. Refactoring into the engine pattern is straightforward:

1. **Create the `decoy-engine` repo.** Empty.
2. **Move data-work modules.** Find every module in your CLI that doesn't import Typer or Rich. Those are engine candidates. Move them to `decoy-engine`.
3. **Define the engine's public API.** What do the moved modules expose? That's your initial `__init__.py`.
4. **Update CLI imports.** Replace `from forge.transforms import ...` with `from decoy_engine.transforms import ...`.
5. **Add `decoy-engine` as a dependency** in the CLI's `pyproject.toml`.
6. **Run the test suite.** It should pass without changes â€” the only thing that moved is where the code lives.

Doing this *now* (when your codebase is small) is hours of work. Doing it later (when it's big) is weeks. So if you've already started, do it before the codebase grows.

---

## Practical Examples

### Adding a new mask transform

**Before** (without engine):
1. Write transform in `forge` repo
2. Tests in `forge` repo
3. Copy transform to `forge-platform` repo (or create some sharing mechanism)
4. Tests in `forge-platform` repo
5. Keep them in sync forever

**With engine:**
1. Write transform in `decoy-engine/transforms/`
2. Tests in `decoy-engine/tests/`
3. Release engine v1.4.0
4. Bump engine dependency in `forge` and `forge-platform`. Done.

### Adding a new connector

Same as transform. Engine-only work.

### Changing the YAML schema (e.g., adding a new optional field)

**With engine:**
1. Update Pydantic model in `decoy-engine/config.py`
2. Add validation logic in engine
3. Bump engine version (minor â€” it's additive)
4. Update docs in `forge-web`
5. Both wrappers pick up the new field automatically

### Adding a Rich-formatted progress bar that shows mask completion percentage

CLI-only. Engine emits progress events through its `Logger` interface; CLI's `RichLogger` renders them as a bar; Platform's `StructuredLogger` writes them to its log pipeline. Engine doesn't change.

### Adding scheduled pipeline runs

Platform-only. The scheduler imports `Pipeline` from the engine and triggers runs on cron. Engine doesn't know it's being scheduled. CLI doesn't have access to the scheduler.

### Adding `@require_business` to a new command

CLI-only. The decorator is in the CLI codebase. The license verifier it calls lives in the engine.

---

## Common Mistakes to Avoid

### Mistake 1: "I'll just import platform code from the CLI for now"

Never. The CLI is public, the platform is private. Importing platform code into the CLI either leaks private code or breaks builds. If two pieces of code want to share something, they share it through the engine.

### Mistake 2: Putting CLI-specific code in the engine

If `decoy-engine` imports `typer` or `rich`, you've polluted the engine. The engine should depend only on data libraries (Pydantic, SQLAlchemy, pandas/polars if used, etc.) â€” never on UI libraries.

A useful rule: **the engine should be importable from a Jupyter notebook with no surprises.** If someone wants to use Forge as a Python library inside their own data pipeline, they should be able to. That's a feature, not a side effect.

### Mistake 3: Letting the engine API drift

Without discipline, the engine's "public API" becomes "everything anyone happened to import." Suddenly you can't refactor anything without breaking something. Mitigate by:

- Explicit `__all__` in `__init__.py`
- A single canonical doc page listing the public API
- Code review rule: "Did this PR change the public API? If so, version-bump consequences?"

### Mistake 4: Versioning the engine carelessly

If you ship breaking changes as patch releases, your wrappers will break in production. Treat the engine like a library *because it is one*. SemVer strictly. Deprecate before removing.

### Mistake 5: Building the engine after the wrappers

The temptation: build the CLI fast, build the platform fast, *then* refactor into an engine "when we have time." You won't have time. Build the engine from day one â€” even if it's tiny. The first version of `decoy-engine` can be 200 lines. It just has to exist as a separate package so the boundary is real.

### Mistake 6: Not testing the engine in isolation

The engine should have its own test suite that doesn't require running the CLI or starting a web server. If your engine tests need to spin up a FastAPI app, the engine isn't really separate.

### Mistake 7: Letting the engine know about authentication

The engine doesn't authenticate users. It just verifies licenses (which are pre-issued). Authentication is a Platform concern (sessions, OAuth, etc.). The engine accepts an `ExecutionContext` that already has the verified license; how that license got there is the wrapper's problem.

---

## When You Add the Fourth Repo

Eventually you'll want a public `forge-examples` or `forge-recipes` repo. That's also a wrapper of the engine â€” it imports `decoy-engine` and provides example pipelines and helper code. Same pattern, different shape.

You might also build a Python SDK someday â€” a package called `forge-sdk` that's just a thin user-facing API around the engine, distributed as a "use Forge in your own Python code" library. Same pattern again.

The engine is the foundation. Everything else is a wrapper.

---

## Critical Path Summary

1. **Phase 0:** Decide on the engine pattern (this doc) â€” done if you read this
2. **Day 1:** Create `decoy-engine` repo (public, BUSL), even empty
3. **Day 2:** Create `forge` repo (public, BUSL), with `decoy-engine` as a dependency
4. **Weeks 1â€“4:** Build CLI features by writing engine code first, CLI wrapper second
5. **Months 3â€“4 (after CLI launch):** Create `forge-platform` (private), import the same engine
6. **Always:** New features go in the engine first; wrappers follow

The single rule that makes this work: **before writing any new code, decide which of the three repos it belongs in.** If the answer is "both CLI and Platform," it's engine code. If the answer is "obviously just one," put it there. The 30-second decision saves hours of refactoring later.

---

## How This Changes the Build Plan

The original BUILD_PLAN.md treated the CLI as the unit of work for Phase 1. Update your mental model: **Phase 1 is "build the engine, with a CLI on top."** When you add a new mask transform in week 2, you're adding it to the engine. When you add YAML parsing in week 1, that's the engine. The CLI is the thinnest possible shell over the engine â€” Typer commands, Rich output, and almost nothing else.

Likewise, when Phase 4 starts (Business platform), you're not "rebuilding masking for the web." You're "wrapping the engine in a web UI." That distinction is the difference between 6 weeks of work and 6 months.

---

*End of architecture plan. The engine pattern is the single most important architectural decision in this project. Get it right and the rest of the system stays sane. Get it wrong and you'll feel the doubling-work pain for years.*
