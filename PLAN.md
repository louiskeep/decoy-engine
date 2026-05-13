# PLAN — decoy-engine

> Source of truth for **what the agent is working on right now** in `forge-engine` and the immediate decision log.
> Long-horizon "what to build next" lives cross-repo in [`../forge-platform/ROADMAP.md`](../forge-platform/ROADMAP.md). This file is the short-loop companion for engine-side work.

---

## Status

- **Project:** decoy-engine (Pandas / Polars / DuckDB hybrid masking + generation engine)
- **Stage:** building (pre-customer)
- **Current focus:** Per-node narrative emissions (just shipped: runner-level config-summary line).
- **Last updated:** 2026-05-12

---

## 1. Spec

**Product:** Shared Python data engine providing masking strategies, synthetic generation, STORM column profiling, FORECAST recommendations, graph-mode pipeline execution, and the Polars+DuckDB hybrid substrate. Single source of data-manipulation truth across CLI + platform.

**User:** The CLI binary (`forge/`) for terminal-driven runs, and the platform's job runner (`forge-platform/api/jobs/runner.py`) for orchestrated runs. Both import this in-process — no subprocess boundary.

**Success criteria:**
- Tens of millions of rows out of the box (4 GB RAM), hundreds of millions on a good machine (16 GB).
- Pandas-vs-polars parity tests pin every observable divergence in `tests/parity/SEMANTIC_DIFFERENCES.md`.
- New op / transform / strategy lands with a benchmark per `BENCHMARKING_GUIDE.md`.

**Non-goals:**
- No platform-level concerns (jobs, schedules, alerts, multi-tenant). Those live in `forge-platform/`.
- No CLI surface concerns. Those live in `forge/`.
- No DB extractor / target connectors (deferred post-2026-05-10 pivot).

---

## 2. Architecture & Stack

- **Language:** Python 3.10+
- **Build:** hatchling; `pip install -e .[dev]` for development.
- **Hard deps:** pandas, polars, duckdb, pyarrow, faker, pyyaml, psutil, pydantic.
- **Substrate:** Polars + DuckDB hybrid (ADR-0001). `engine: hybrid` is the default; `pandas` / `polars` are opt-outs.
- **Logger protocol:** `Logger` (narrative) + `StructuredEvents` (optional) from `context.py`. Implementations: stdlib (CLI fallback), `RichLogger` (CLI quiet/verbose), `JobLogger` (platform-side).
- **Tests:** pytest, mirrored layout under `tests/`.
- **Docs:** Sphinx + sphinx-autoapi published to GitHub Pages on push to main.

---

## 3. MVP Scope

Cross-repo MVP framing lives in `../forge-platform/ROADMAP.md`. Engine-specific anchors:

### Already shipped (recent)
- Item 65 — STORM `casing_pattern` + `format_pattern` extraction; uniform `preserve_format` on `BaseMaskingStrategy`; engine-side format preservation post-pass.
- Item 41 — Engine `walks` package (ER graph + 6 hazard detectors + FK inference + schema diff).
- Item 47 phases 1–8 — Polars+DuckDB hybrid substrate (Arrow runner cache, op-type registry, Polars relational ops, DuckDB source/sink, parity tests, default flip pandas -> hybrid).
- Per-node config-summary line in graph runner (`_summarize_node_config` in `graph/runner.py`) — gives Task History a meaningful narrative entry per node without editing every op.
- if_router two-output port support — engine already had it; canvas surface caught up.

### Active queue (cross-repo `ROADMAP.md` for full list)
- Item 24 — `FileSource` / `FileSink` SDK + `sub_pipeline` / `iterator` / `sql_run` ops (Sprint G).
- Item 63 — Cloud-source engine ops (`source.s3` / `source.gcs` / `source.sftp`).
- Item 15 — `reference` + `distribution` generate modes.
- Item 19 — `join` + `aggregate` graph ops (only if customer asks).

### Not in scope (today)
- DB source / target ops (deferred with the file-only pivot).
- Item 8 ML detection — lives partially in platform, partially in engine; ML is the long pole and not engine-driven yet.

---

## 4. Milestones

Milestones live in `../forge-platform/ROADMAP.md`'s "Sprints" section.

- **Sprint G** (queued) — Item 24 file-only ETL SDK lands engine-side. Heavy engine work.
- **Sprint H** — Items 7 + 15 + 30 (generation determinism + reference/distribution + post-mask resolution) ship together.
- **Sprint I+** — Op-by-op `ctx.logger.info` narrative emissions (filter, drop_column, sort, limit, dedupe, derive, source.file, target.file) for richer per-node logs.

---

## 5. Current Task

**Task:** _(none active — last shipped: per-node config-summary in `graph/runner.py`, commit e87aa30)_
**Context:** see `../forge-platform/ROADMAP.md` + recent commits (`git log --oneline -10`).
**Acceptance:** N/A.

---

## 6. Decision Log

Append-only. Most-recent first.

- 2026-05-12 — Runner-level config-summary emit instead of per-op `ctx.logger.info` everywhere — one-file change vs. mutating 15 op files; secrets redacted by key name in the summary line.
- 2026-05-12 — if_router engine already supported `pass`/`fail` ports; the canvas surface caught up. Engine signature unchanged.
- 2026-05-12 — Item 65 V1 ships with five preservation modes: upper / lower / title / mixed / digits-only casing + a digit-template format. Date strftime preservation included. SKIP_STRATEGIES set covers hash/redact/passthrough/date_shift.

---

## 7. Open Questions

- [ ] Per-op narrative emissions: should each engine op emit `ctx.logger.info` with its own status line ("filter: dropped 766 of 1000 rows"), or is the runner-level config-summary enough? Lean: add per-op gradually as users request more detail.
- [ ] Item 19 (join + aggregate ops) — bake-time customer signal hasn't arrived. Defer until it does.

---

## 8. Risks & Trade-offs

- **Risk:** Polars+DuckDB substrate is the default; pandas-only edge cases may surface. **Mitigation:** parity tests under `tests/parity/`; divergences documented in `SEMANTIC_DIFFERENCES.md`.
- **Trade-off:** `if_router` evaluates predicates via `pl.SQLContext` (Polars SQL) with a pandas `df.query()` fallback. The two dialects mostly agree for our usage (==, !=, <, >, <=, >=, and, or, not, parens, single-quoted strings). **Acceptable because:** parity tests catch any drift.

---

## 9. Backlog / Future

- Per-op `ctx.logger.info` narrative emissions in filter / drop_column / sort / limit / dedupe / derive / source.file / target.file.
- Item 47·9 (FK-aware generation hybrid — Polars orchestration + pandas per-cell). Defer until customer signal.
- Migration of `if_router` to expose `var.X` resolution as a structured config alongside the freeform predicate (deferred; today's `${var.X}` substitution in the SQL string covers the use case).

---

## Changelog

- 2026-05-12 — initial PLAN.md drafted alongside AGENTS.md.
