# Agent Operating Guide — decoy-engine

You are working inside `forge-engine`, the pure-Python data engine for the Decoy product family. Both the CLI (`forge/`) and the platform (`forge-platform/`) import this as a library — they never contain masking or generation logic.

Read this before touching code. Re-read the relevant section whenever you're unsure what's expected.

## Environment

- **OS:** Windows 11
- **Editor:** VS Code (Claude Code extension)
- **Shell:** PowerShell. Use `;` to chain (not `&&`), `$env:VAR` for env vars, `\` for paths. The Bash tool is available for POSIX scripts.
- **Python:** 3.10 (`requires-python = ">=3.10"` in `pyproject.toml`).
- **Package manager:** **`pip`** with editable installs (`pip install -e .[dev]`). Hatchling build backend. **Do not introduce `uv`** without an explicit ask.
- **Tests:** `pytest`. Live under `tests/`, mirroring the source layout (`tests/graph/`, `tests/storm/`, `tests/transforms/`, `tests/parity/`).
- **Lint/format/types:** not configured in pyproject.toml today. Don't add ruff / black / pyright unilaterally.
- **Docs:** Sphinx + sphinx-autoapi under `docs/`. Built by `.github/workflows/docs.yml` on push to main.
- **Pre-commit hooks:** not configured. Don't pass `--no-verify` unless the user explicitly asks.

## The Repo Family

- `forge` — Decoy CLI (Typer + Rich). Imports `decoy-engine`.
- **`forge-engine` (you are here)** — Pure-Python data engine. Pandas / Polars / DuckDB hybrid substrate.
- `forge-platform` — FastAPI backend + React/Vite dashboard. Imports `decoy-engine` in-process. Hosts the cross-repo `ROADMAP.md`.
- `forge-web` — Next.js marketing + docs site.

## The Workflow

```
spec  ->  plan  ->  execute  ->  verify  ->  commit
```

| Step    | Owner    | Your role                                                   |
|---------|----------|-------------------------------------------------------------|
| Spec    | User     | Ask sharp clarifying questions. Don't start coding.         |
| Plan    | Shared   | Translate spec into `PLAN.md` / roadmap items.              |
| Execute | You      | One task at a time. Read first, write second.               |
| Verify  | You      | Tests + manual check. Be honest about failures.             |
| Commit  | You      | Small atomic commits with clear messages.                   |

## Required Reading Before You Touch Code

In this order:

1. **`PLAN.md`** (this repo) — current focus + active task.
2. **`CLAUDE.md`** (this repo) — repo orientation.
3. **`../forge-platform/ROADMAP.md`** — cross-repo source of truth for what to build next.
4. **`../forge-platform/GLOSSARY.md`** — Decoy vocabulary + `forge -> decoy` rename status.
5. **`docs/adr/`** — engine architecture decision records. Read the relevant ADR before re-litigating a settled question (Polars+DuckDB hybrid, ExecutionContext two-resolver shape, etc.).
6. **The files you're about to modify** + their immediate callers/callees.

## Your Role: Junior Dev With Good Instincts

- **No unilateral architectural decisions.** Engine changes affect both CLI and platform — propose, then wait.
- **No new dependencies on a whim.** The engine has a tight dependency surface (pandas, polars, duckdb, pyarrow, faker, pyyaml, psutil, pydantic). New deps need a strong justification.
- **No refactors you weren't asked to do.** Note them in PLAN.md's "Backlog / Future."
- **Verify before claiming done.** Engine tests run fast — no excuse to skip them.
- **Ask when stuck.** Two failed attempts at the same thing means stop and ask.

## Execution Rules

### One task at a time
Pick a single task from `PLAN.md`. Complete it end-to-end (code -> tests -> commit) before starting the next.

### Read before writing
Before adding a transform / op / strategy, search `src/decoy_engine/` to see if something similar exists. Engine has registries (`transforms/registry.py`, `graph/ops/__init__.py`) — register new ops there, don't fork the dispatch.

### Match the existing style
- 4-space indent, type hints on public functions, dataclasses for value types where idiomatic.
- Engine ops conform to the `apply(inputs, config, ctx) -> result` protocol with `KIND`, `NATIVE_ENGINE`, `INPUT_ARITY`, `OUTPUT_KIND` module-level constants.
- Strategies extend `BaseMaskingStrategy` with `mask(value, ctx) -> value` semantics.
- Don't bypass the `ExecutionContext` two-resolver design (ADR-0002) — `derive_key` and `pipeline_derive_key` are intentionally distinct.

### Tests are not optional
- New op / transform / strategy gets unit tests under `tests/`.
- Parity tests live in `tests/parity/` — pandas-vs-polars semantic divergences get documented in `tests/parity/SEMANTIC_DIFFERENCES.md` rather than papered over.
- Bug fixes get a regression test that fails before the fix and passes after.
- `pytest -x --tb=short` for fast-fail debugging; full suite before commit.

### Benchmarking discipline
- Every new op / transform / connector ships with a benchmark per `BENCHMARKING_GUIDE.md`.
- Four tiers (smoke / regression / engineering-correctness / marketing). Smoke + regression run in CI.
- Don't add benchmark numbers without running the benchmark and recording the machine spec.

### Commit discipline
- **Small commits.** One logical change per commit.
- **Message format:** lowercase prefix + imperative summary, matching recent history. Common: `feat:` / `fix:` / `refactor:` / `test:` / `docs:` / engine-specific: `graph:` / `mask:` / `storm:` / `engine:`.
- **No emojis in commit messages, branch names, or PR titles.** ASCII only. Unicode is fine in markdown prose.
- **No em-dashes (`—`) anywhere** — banned per user preference. Use colons, hyphens, paragraph breaks, or parens.
- **Commit after each working step.**
- **Never `git push --force`** without explicit user approval. **Never merge to `main`** without explicit user instruction.

## What "Done" Means

- [ ] Code is written and minimal.
- [ ] Tests exist and pass: `pytest`.
- [ ] Public API surface (anything in `decoy_engine.__init__.__all__`) is documented enough for Sphinx autoapi to render usefully.
- [ ] If a new op/strategy: registered in the appropriate registry + appears in the engine's public surface as appropriate.
- [ ] Benchmark added or extended if perf-relevant.
- [ ] Committed with a clear message.
- [ ] `PLAN.md` updated.

## What You Don't Do

- **Don't add dependencies without asking.**
- **Don't suppress errors to make tests pass.** Fix the cause.
- **Don't write code you didn't run.**
- **Don't fabricate API signatures.** Check `__init__.py` exports.
- **Don't refactor uninstructed.**
- **Don't expand scope.**
- **Don't claim verification you didn't perform.**

## When You're Stuck

1. Re-read `PLAN.md` + the file you're modifying + the relevant ADR.
2. Run the failing thing and read the actual error.
3. Try one focused alternative.
4. Stop and ask.

## Repo-Specific Notes — decoy-engine

- **Public surface:** declared in `decoy_engine/__init__.__all__`. Adding anything new requires considering both the CLI and platform consumers.
- **Logger protocol:** `decoy_engine.context.Logger` (narrative) + `StructuredEvents` (optional, used by the platform's `JobLogger`). Engine code uses `ctx.logger` and the `emit_*` safe helpers — never imports `logging` directly inside ops.
- **Graph runner:** `src/decoy_engine/graph/runner.py`. Owns step boundary emission + per-node config-summary emission. Op dispatch via `OPS[kind]` from `graph/ops/__init__.py`.
- **Op contract:** `apply(inputs, config, ctx)`. Multi-output ops set `OUTPUT_KIND="split"` + `OUTPUT_PORTS=(...)` and return a dict keyed by port (today: `if_router` with `pass` / `fail`).
- **Hybrid substrate:** `engine: hybrid` is the default. `engine: pandas` / `engine: polars` are opt-outs. Don't write pandas-only ops without a polars equivalent unless the op is fundamentally row-oriented.
- **Pre-customer status:** no production deployments handling real customer data. Architecture changes carry zero rollout risk. Ship aggressively, but verify aggressively too.
