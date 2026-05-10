# ADR-0001 — Polars + DuckDB hybrid engine substrate

> **Status:** Accepted
> **Date:** 2026-05-09
> **Supersedes:** the prior pandas-only engine substrate.

## Context

The engine ran on pandas from day one. Pandas is the right substrate for per-cell mask/generate code (Faker integration, formula evaluation, arbitrary Python in `column.apply`), but it has a hard memory and CPU ceiling — typical workloads ran out of headroom around 1–5 million rows on a developer laptop, and customers were asking about tens of millions.

Customer-visible scale was the gating constraint. The engine architecture was otherwise sound — graph runner, op-type registry, mask/generate strategies — so the substrate was the only piece that needed to change. Sales positioning had been pinned to "millions, not billions" as a defensible claim; the team wanted to push that to "tens of millions out of the box, hundreds of millions on a good box" without rewriting every mask/generate strategy.

## Decision

Adopt a three-engine **hybrid substrate** with Apache Arrow as the boundary format:

- **DuckDB** owns sources and sinks (CSV / Parquet / database connectors). DuckDB's native scanners outperform pandas / Polars for I/O at scale and integrate trivially with Arrow.
- **Polars** owns relational ops (filter / sort / dedupe / derive / join / aggregate). Polars's lazy planner and parallel execution are roughly 10× pandas on these patterns, with no Python-level rewrite required.
- **Pandas** owns per-cell mask/generate ops over Arrow-backed frames. Arrow-backed pandas (`pd.ArrowDtype`) keeps the existing strategy surface intact while removing the worst of pandas's memory overhead.

A per-op `NATIVE_ENGINE` declaration in the op-type registry tells the runner which substrate executes each op; the runner materializes via Arrow at boundaries and eagerly evicts cached frames to keep peak RSS bounded.

## Consequences

**Negative:**
- Three substrates to know. Contributors need a Polars cheat sheet (shipped as `POLARS_FOR_PANDAS_USERS.md`) and a mental model of where Arrow boundaries sit.
- Arrow boundary materialization is non-free; every op transition allocates. Multi-Polars-op chains today materialize between each Polars op, which Item 47·10 (lazy-Polars chains) is queued to fix.
- Adds DuckDB as a hard dependency. Already a pip-installable pure-Python wheel, but it's another moving piece in the supply chain.
- Pandas + Arrow-backed dtypes hit a few sharp edges during migration (Bug 4 — Arrow conversion default; Bug 5 — memory-pressure calibration + `rechunk=False` Arrow→Polars boundary). All closed but they were real cost.

**Positive:**
- 5–10× ceiling lift, validated by the STORM Arrow-boundary benchmark on `main`. Customer-facing scale claim moved from "millions" to "tens of millions out of the box."
- Per-op substrate choice means we can adopt new engines (e.g. a future GPU substrate for joins) without changing the public API — just add a `NATIVE_ENGINE` declaration.
- Connector SDK contract is now substrate-agnostic (`pyarrow.Table` in / `pyarrow.Table` out), which makes third-party connectors easier to write and validate.
- Existing mask/generate strategies didn't change. The `transforms/` and `generators/` packages run unchanged on Arrow-backed pandas frames.

## Alternatives considered

- **Stay pandas + chunk harder.** Rejected: chunking helps I/O-bound ops but doesn't lift algorithmic ceilings (joins, sorts, aggregates still need the full input). Bandaid, not a fix.
- **Pure Polars.** Rejected: every existing mask/generate strategy is written against pandas APIs (Faker integration, formula `eval`, `null_probability` injection). Pure Polars would be roughly years of churn to rewrite, with breakage at every step.
- **Pure DuckDB.** Rejected: cell-level UDFs are awkward in DuckDB and Faker integration is messy. DuckDB is the right tool for I/O and SQL-shaped relational ops; not for arbitrary Python per cell.
- **Dask.** Rejected: distributed-execution operational burden isn't warranted at the target scale. The problem is "fit ten times more data on one machine," not "scale across a cluster."

## References

- `forge-platform/ROADMAP.md` Item 47 (all 8 phases shipped 2026-05-09).
- `decoy-engine/SHARED_ENGINE_ARCHITECTURE.md` — substrate split + the "three engines, one Arrow boundary" diagram.
- `decoy-engine/POLARS_FOR_PANDAS_USERS.md` — contributor cheat sheet.
- `decoy-engine/CONNECTOR_SDK_CONTRACT.md` — Arrow-Tables-everywhere contract.
- `decoy-engine/BENCHMARKING_GUIDE.md` — perf-regression discipline that prevents substrate regressions.
