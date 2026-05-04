# Forge — Shared Engine Architecture Plan

> **Companion to:** BUILD_PLAN.md, REPO_ARCHITECTURE_PLAN.md
> **Purpose:** Solves the "doubling work between CLI and Platform" problem by introducing a shared engine library that both consume.
> **Read this before:** Writing any code that could plausibly live in either the CLI or the Platform.
> **Last updated:** [date]

---

## TL;DR

You don't have two codebases. You have **three** — a shared engine and two thin wrappers around it.

```
forge-engine   →  Python library. Does the actual data work. (Public, BUSL)
forge          →  CLI wrapper. Imports forge-engine. (Public, BUSL)
forge-platform →  Web app wrapper. Imports forge-engine. (Private)
```

When you add a new mask transform, connector, or YAML feature, you add it to **`forge-engine` once**. The CLI and Platform both pick it up automatically on the next engine version bump.

Result: **no doubling on data work**. The CLI and Platform only diverge on interface-specific code (terminal UX vs. web UX), which is naturally separate anyway.

This pattern is what dbt, Prefect, Dagster, Meltano, and Sentry all use. It's the proven architecture for free-tool-plus-paid-platform companies.

---

## The Problem This Solves

Without a shared engine, you face this every time you ship a feature:

1. Add a new mask transform → write it in the CLI codebase
2. Realize the Platform needs it too → copy/paste it to the Platform codebase
3. Fix a bug in the transform → fix it in two places
4. CLI YAML format diverges slightly from Platform format → users get confused
5. Six months later, the two implementations have drifted in subtle ways → support nightmare

This is the classic "two implementations of the same logic" tax. The engine pattern eliminates it.

---

## The Three-Layer Architecture

### Layer 1: `forge-engine` — The Shared Brain

A Python library that does all the actual data work. Knows nothing about terminals, HTTP, web UIs, or users. Just data in, data out.

**Public API surface** (what the CLI and Platform call):

```python
from forge_engine import (
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

### Layer 2: `forge` — The CLI Wrapper

A thin Typer-based CLI that imports `forge-engine` and exposes it as terminal commands.

```python
# src/forge/cli/run.py
import typer
from forge_engine import Pipeline, PipelineConfig, ExecutionContext
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

### Layer 3: `forge-platform` — The Web Wrapper

A FastAPI backend + Next.js frontend that imports the same `forge-engine` and exposes it via HTTP and a web UI.

```python
# api/runs.py
from fastapi import APIRouter, Depends
from forge_engine import Pipeline, PipelineConfig, ExecutionContext
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

## What Goes Where: The Decision Rules

This is the most important section. When you're about to write a new piece of code, ask:

### "Does this manipulate or describe data?" → Engine

- New mask transform → engine
- New synthetic data generator → engine
- New connector (source or destination) → engine
- YAML schema field → engine
- Validation rule for a YAML field → engine
- Schema introspection → engine
- Referential integrity logic → engine
- Pipeline execution → engine
- License JWT verification → engine

### "Does this involve a terminal?" → CLI only

- Typer command definition → CLI
- Rich-formatted terminal output → CLI
- Progress bar in a TTY → CLI
- Interactive prompts (`forge init`) → CLI
- Local config file (`~/.forge/`) → CLI
- Update check messages → CLI
- ASCII art → CLI
- Tab completion scripts → CLI
- Exit code handling → CLI

### "Does this involve multiple users, persistence, or HTTP?" → Platform only

- FastAPI routes → Platform
- Database models → Platform
- Scheduler / cron jobs → Platform
- Audit log → Platform
- RBAC → Platform
- Auth / sessions → Platform
- Stripe integration → Platform
- License *issuance* (signing) → Platform
- Web UI → Platform
- Email notifications → Platform
- Multi-tenant logic → Platform

### Gray areas (and how to resolve them)

**Logging.** Engine defines a `Logger` *interface* (abstract base class or Protocol). CLI provides a `RichLogger` implementation that prints to stderr. Platform provides a `StructuredLogger` that writes JSON to its log pipeline. Engine never imports either implementation.

**Telemetry.** Same pattern. Engine defines a `TelemetryClient` interface. CLI implements one that POSTs to your analytics endpoint with opt-in. Platform implements one that writes to its internal events table.

**License verification.** This is engine code. Both CLI and Platform need to verify the same JWTs the same way, using the same public key. Putting it in the engine ensures perfect parity. License *issuance* (signing JWTs with the private key) lives in Platform.

**Configuration loading.** YAML parsing is engine. Reading the YAML file from disk is CLI. Reading the YAML from a database row is Platform. The engine accepts a `PipelineConfig` object; the wrappers handle "where does this config come from."

**Errors.** Engine raises typed exceptions (`PipelineValidationError`, `ConnectorAuthError`, `LicenseExpiredError`). CLI catches them and renders friendly Rich-formatted output. Platform catches them and returns appropriate HTTP status codes.

**Caching.** Connector connection pooling is engine. Caching pipeline run results is Platform-only (CLI doesn't cache across runs).

---

## The Engine API Contract

The `forge-engine` public API is a **contract**. Both the CLI and Platform depend on it. Treating it casually is how you create maintenance pain. Treating it as an API is how you stay sane.

### Versioning

- **SemVer strict.** Breaking changes only on major versions.
- **Deprecation policy.** New behavior introduced as a flag or alternate function. Old behavior deprecated for one major version, then removed.
- **The `__init__.py` is the contract.** Anything exported from `forge_engine` is public. Anything imported from `forge_engine.internal.*` is private and can change at any time.

### What "public API" means

```python
# forge_engine/__init__.py — this IS the public API
from forge_engine.pipeline import Pipeline
from forge_engine.config import PipelineConfig
from forge_engine.registry import MaskRegistry, ConnectorRegistry
from forge_engine.context import ExecutionContext, Logger, TelemetryClient
from forge_engine.exceptions import (
    ForgeError,
    PipelineValidationError,
    ConnectorAuthError,
    LicenseExpiredError,
    ...
)
from forge_engine.license import LicenseVerifier, License
from forge_engine.schema import SchemaInspector

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

## Repo Structure (Updated)

The full picture:

```
github.com/forgeio/forge-engine    ← Shared library (PUBLIC, BUSL)
github.com/forgeio/forge           ← CLI (PUBLIC, BUSL)
github.com/forgeio/forge-platform  ← Platform (PRIVATE)
github.com/forgeio/forge-web       ← Marketing site + docs (PUBLIC)
```

### `forge-engine` skeleton

```
forge-engine/
├── .github/workflows/
│   ├── test.yml                  # multi-OS, multi-Python matrix
│   ├── release.yml               # PyPI publish on tag
│   └── lint.yml
├── src/
│   └── forge_engine/
│       ├── __init__.py           # public API, controlled exports
│       ├── pipeline.py           # Pipeline class
│       ├── config.py             # PipelineConfig (Pydantic)
│       ├── context.py            # ExecutionContext, Logger interface, TelemetryClient interface
│       ├── exceptions.py         # all public exception types
│       ├── license/
│       │   ├── __init__.py
│       │   ├── verifier.py       # JWT verification
│       │   └── public_key.py     # embedded public key constant
│       ├── transforms/
│       │   ├── __init__.py
│       │   ├── registry.py       # MaskRegistry
│       │   ├── faker_based.py    # email, name, phone, etc.
│       │   ├── fpe.py            # format-preserving encryption
│       │   ├── hashing.py
│       │   ├── date_shift.py
│       │   └── ...
│       ├── generators/
│       │   ├── __init__.py
│       │   ├── registry.py
│       │   └── ...
│       ├── connectors/
│       │   ├── __init__.py
│       │   ├── registry.py
│       │   ├── base.py           # ConnectorBase abstract class
│       │   ├── postgres.py
│       │   ├── mysql.py
│       │   ├── s3.py
│       │   ├── snowflake.py
│       │   └── ...
│       ├── schema/
│       │   ├── __init__.py
│       │   └── inspector.py      # SchemaInspector
│       └── internal/             # private, not part of API contract
│           ├── execution.py
│           └── ...
├── tests/
├── pyproject.toml
├── README.md
├── LICENSE.md
├── CHANGELOG.md
└── CONTRIBUTING.md
```

### `forge` (CLI) skeleton

```
forge/
├── pyproject.toml                # depends on forge-engine
├── src/
│   └── forge/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli/                  # Typer commands
│       │   ├── run.py
│       │   ├── validate.py
│       │   ├── init.py
│       │   ├── demo.py
│       │   ├── connectors.py
│       │   ├── login.py
│       │   ├── schedule.py       # @require_business
│       │   └── ...
│       ├── ui/                   # Rich-based output
│       │   ├── logger.py         # implements forge_engine.context.Logger
│       │   ├── progress.py
│       │   ├── prompts.py        # interactive prompts
│       │   ├── tables.py
│       │   └── theme.py
│       ├── config/               # local config (~/.forge/)
│       │   ├── license.py        # license file storage
│       │   └── settings.py
│       ├── telemetry/            # implements forge_engine.context.TelemetryClient
│       │   └── client.py
│       ├── decorators/
│       │   └── require_business.py
│       └── version_check.py
└── tests/
```

### `forge-platform` skeleton

```
forge-platform/
├── api/                          # FastAPI
│   ├── main.py                   # depends on forge-engine
│   ├── auth/
│   ├── runs/
│   │   └── handler.py            # imports forge_engine.Pipeline
│   ├── pipelines/
│   ├── billing/
│   ├── licenses/
│   │   └── issuer.py             # signs JWTs with private key
│   ├── audit/
│   ├── teams/
│   ├── persistence/
│   │   ├── logger.py             # implements forge_engine.context.Logger
│   │   └── telemetry.py          # implements forge_engine.context.TelemetryClient
│   └── scheduler/
├── web/                          # Next.js
│   └── ...
├── deploy/
└── tests/
```

---

## Development Workflow

### Local development

When you're working on a CLI feature that needs an engine change:

```bash
# Clone all three side by side
git clone forgeio/forge-engine
git clone forgeio/forge
git clone forgeio/forge-platform

# Install engine in editable mode in both wrappers
cd forge
pip install -e ../forge-engine -e .

cd ../forge-platform
pip install -e ../forge-engine -e .
```

Now changes to `forge-engine` are picked up immediately by both wrappers. No publish/install dance during local dev.

### Release workflow

1. **Engine release.** Bump version in `forge-engine`, tag, push. CI publishes to PyPI.
2. **Wrapper updates.** Renovate or Dependabot opens PRs in `forge` and `forge-platform` bumping the `forge-engine` dependency. You review the PR, run CI, merge.
3. **Wrapper releases.** Bump CLI version, tag, push. CI publishes new CLI to PyPI. Bump platform version, deploy.

This sounds like more steps than monolith. It's not — it's the *same* steps, just more visible. In a monolith you'd still test the integration before shipping; this just makes the integration explicit.

### When to release the engine

- **Bug fix in a transform** → patch release of engine, both wrappers update on next sync
- **New transform / connector / generator** → minor release of engine, both wrappers update
- **Breaking YAML schema change** → major release of engine, coordinated update of both wrappers (rare)
- **Pure CLI UX improvement** → no engine release; just patch the CLI
- **Pure platform feature (web UI, scheduling)** → no engine release; just deploy platform

You'll find that **most of your releases are engine releases**. That's the intended outcome — it confirms the architecture is working.

---

## Migration Path (If You've Already Started Building)

If you've already built some CLI code, don't panic. Refactoring into the engine pattern is straightforward:

1. **Create the `forge-engine` repo.** Empty.
2. **Move data-work modules.** Find every module in your CLI that doesn't import Typer or Rich. Those are engine candidates. Move them to `forge-engine`.
3. **Define the engine's public API.** What do the moved modules expose? That's your initial `__init__.py`.
4. **Update CLI imports.** Replace `from forge.transforms import ...` with `from forge_engine.transforms import ...`.
5. **Add `forge-engine` as a dependency** in the CLI's `pyproject.toml`.
6. **Run the test suite.** It should pass without changes — the only thing that moved is where the code lives.

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
1. Write transform in `forge-engine/transforms/`
2. Tests in `forge-engine/tests/`
3. Release engine v1.4.0
4. Bump engine dependency in `forge` and `forge-platform`. Done.

### Adding a new connector

Same as transform. Engine-only work.

### Changing the YAML schema (e.g., adding a new optional field)

**With engine:**
1. Update Pydantic model in `forge-engine/config.py`
2. Add validation logic in engine
3. Bump engine version (minor — it's additive)
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

If `forge-engine` imports `typer` or `rich`, you've polluted the engine. The engine should depend only on data libraries (Pydantic, SQLAlchemy, pandas/polars if used, etc.) — never on UI libraries.

A useful rule: **the engine should be importable from a Jupyter notebook with no surprises.** If someone wants to use Forge as a Python library inside their own data pipeline, they should be able to. That's a feature, not a side effect.

### Mistake 3: Letting the engine API drift

Without discipline, the engine's "public API" becomes "everything anyone happened to import." Suddenly you can't refactor anything without breaking something. Mitigate by:

- Explicit `__all__` in `__init__.py`
- A single canonical doc page listing the public API
- Code review rule: "Did this PR change the public API? If so, version-bump consequences?"

### Mistake 4: Versioning the engine carelessly

If you ship breaking changes as patch releases, your wrappers will break in production. Treat the engine like a library *because it is one*. SemVer strictly. Deprecate before removing.

### Mistake 5: Building the engine after the wrappers

The temptation: build the CLI fast, build the platform fast, *then* refactor into an engine "when we have time." You won't have time. Build the engine from day one — even if it's tiny. The first version of `forge-engine` can be 200 lines. It just has to exist as a separate package so the boundary is real.

### Mistake 6: Not testing the engine in isolation

The engine should have its own test suite that doesn't require running the CLI or starting a web server. If your engine tests need to spin up a FastAPI app, the engine isn't really separate.

### Mistake 7: Letting the engine know about authentication

The engine doesn't authenticate users. It just verifies licenses (which are pre-issued). Authentication is a Platform concern (sessions, OAuth, etc.). The engine accepts an `ExecutionContext` that already has the verified license; how that license got there is the wrapper's problem.

---

## When You Add the Fourth Repo

Eventually you'll want a public `forge-examples` or `forge-recipes` repo. That's also a wrapper of the engine — it imports `forge-engine` and provides example pipelines and helper code. Same pattern, different shape.

You might also build a Python SDK someday — a package called `forge-sdk` that's just a thin user-facing API around the engine, distributed as a "use Forge in your own Python code" library. Same pattern again.

The engine is the foundation. Everything else is a wrapper.

---

## Critical Path Summary

1. **Phase 0:** Decide on the engine pattern (this doc) — done if you read this
2. **Day 1:** Create `forge-engine` repo (public, BUSL), even empty
3. **Day 2:** Create `forge` repo (public, BUSL), with `forge-engine` as a dependency
4. **Weeks 1–4:** Build CLI features by writing engine code first, CLI wrapper second
5. **Months 3–4 (after CLI launch):** Create `forge-platform` (private), import the same engine
6. **Always:** New features go in the engine first; wrappers follow

The single rule that makes this work: **before writing any new code, decide which of the three repos it belongs in.** If the answer is "both CLI and Platform," it's engine code. If the answer is "obviously just one," put it there. The 30-second decision saves hours of refactoring later.

---

## How This Changes the Build Plan

The original BUILD_PLAN.md treated the CLI as the unit of work for Phase 1. Update your mental model: **Phase 1 is "build the engine, with a CLI on top."** When you add a new mask transform in week 2, you're adding it to the engine. When you add YAML parsing in week 1, that's the engine. The CLI is the thinnest possible shell over the engine — Typer commands, Rich output, and almost nothing else.

Likewise, when Phase 4 starts (Business platform), you're not "rebuilding masking for the web." You're "wrapping the engine in a web UI." That distinction is the difference between 6 weeks of work and 6 months.

---

*End of architecture plan. The engine pattern is the single most important architectural decision in this project. Get it right and the rest of the system stays sane. Get it wrong and you'll feel the doubling-work pain for years.*
