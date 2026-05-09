# Polars + DuckDB hybrid engine — architecture plan

> **Status:** planning — strategic engineering plan, not yet committed scope. Lands on `forge-engine` because that's where the runner + ops + connectors live; platform impact is limited to the preview path (Phase 5).
> **Branch:** `feature/polars-duckdb-hybrid-plan`
> **References:** [SHARED_ENGINE_ARCHITECTURE.md](../SHARED_ENGINE_ARCHITECTURE.md), [PIPELINE_GRAPH_GUIDE.md](../PIPELINE_GRAPH_GUIDE.md), [forge-platform/plans/2026-05-07-etl-direction-and-connector-sdk.md](../../forge-platform/plans/2026-05-07-etl-direction-and-connector-sdk.md), pandas-ETL-ceiling memo (currently on `forge-platform` branch `claude/api-cli-orchestration-Dpi2t`, candidate for landing on main).
> **Audience:** the implementer of this work — most likely the next Claude session that takes ownership of the engine architecture.
> **Supersedes:** the pandas cheap-wins prescription in the ceiling memo (chunked CSV reads, `chunksize` on `source.db` / `target.db`). The runner-cache eviction half of the cheap-wins is preserved here as Phase 1.

---

## Context

Today's engine runs every graph op on pandas with a runner cache that holds every node's output for the lifetime of the run. The pandas-ETL-ceiling memo identified the practical limits:

- **Comfort zone:** ≤5M rows / ≤2 GB working set on 32 GB hardware
- **Stretch zone:** 5–20M rows, but no full-table sort/dedupe/shuffle, no FK-aware generation
- **No-go:** 50M+ rows on a single masked table

Three weeks of pandas cheap-wins (chunked reads on source.db/target.db, wiring the dead CSV chunked iterator, evicting the runner cache) would push these tiers ~5–10× higher. That's tactical work — table stakes, not a moat.

**The strategic move is replacing pandas as the I/O + relational substrate** with a Polars + DuckDB hybrid, keeping pandas for the per-row Python work (mask transforms, generation, STORM) where the Faker / scipy / sklearn ecosystem actually pays. Apache Arrow is the substrate that makes the hybrid zero-copy across all three engines.

**Why this beats cheap-wins:** ~60% of the cheap-wins prescription is throwaway after this work lands (DuckDB streams natively; better than pandas chunking). The runner-cache half isn't throwaway — it ships as Phase 1 of this plan instead of as a standalone pandas patch. Going straight to the hybrid saves ~2 weeks of work that would get deleted and avoids shipping a transitional architecture that confuses future-us.

**Strategic framing:** this is a moat move. Competitors who built pandas-first masking tools (most of the comp set) will have to do this same migration to scale beyond mid-market. Doing it now buys a 12-month head start where Decoy is "scales cleanly" and they're "thrashing on chunking workarounds." The post-Phase-8 sales conversation moves from "millions, not billions" to "tens of millions out of the box, hundreds of millions on a good box."

---

## TL;DR — Scheme D

Three engines, one Arrow substrate, op-type boundaries:

```
SOURCE (DuckDB)  →  TRANSFORM (Polars)  →  MASK/GENERATE (Pandas)  →  TARGET (DuckDB)
                              ↕                      ↕
                         Arrow tables in runner cache (zero-copy)
```

**The op-type boundary is the key architectural choice.** Each op declares `native_engine = "pandas" | "polars" | "duckdb"`. The runner holds Arrow in cache and materializes to the op's preferred type at `execute()` time. This makes the system explicit, debuggable, and lets us move ops between engines later without rewriting everything.

**Why this split:**

| Engine | Job | Why |
|---|---|---|
| **DuckDB** | source / target / cross-DB I/O | Best spill-to-disk; native S3 / postgres_scanner / parquet glob support; query optimizer for filter-pushdown at read time |
| **Polars** | filter / sort / dedupe / derive / join / group_by | Lazy planner that pushes filters + selects to the scan; columnar + SIMD; parallel by default |
| **Pandas** | mask / generate / STORM profiling | Faker, scipy, the entire per-row Python ecosystem; mask is inherently per-row Python; moving it off pandas buys nothing and costs everything |

**Licenses are clean.** Polars (MIT) + DuckDB (MIT) + existing pandas (BSD-3-Clause) are all permissive and BUSL-1.1 compatible. No copyleft. The existing LGPL psycopg2 dep is precedent for mixed licensing.

---

## Phasing — 8 phases, ~12–15 weeks

The original sketch budgeted 10–14 weeks. This refined plan adds time for Phase 1 (runner cache eviction is graph-traversal work, not a one-line change) and adds an explicit dogfood phase that runs on real customer data before the default flip.

### Phase 1 — Arrow-canonical runner cache (~2 weeks)

Refactor `decoy_engine/graph/runner.py` to hold `pyarrow.Table` in cache instead of `pd.DataFrame`. Each op materializes to its preferred type at start of `execute()`. Add **eager eviction**: track downstream consumers per node; evict when zero remain.

This phase doesn't change any ops yet — pandas ops keep working, just with an Arrow boundary on either side. **It IS the foundation; if it doesn't land cleanly, nothing else does.**

**Specific deliverables:**

- [ ] `runner.py:cache` becomes `dict[str, pyarrow.Table]` instead of `dict[str, Any]`
- [ ] Per-node downstream-consumer count computed once at run start; cache entry deleted when count reaches zero
- [ ] Conversion shim at op boundaries: `arrow_to_engine(table, native_engine)` + `engine_to_arrow(result, native_engine)`
- [ ] Benchmark Arrow ↔ pandas conversion cost in isolation. **STORM/FORECAST ops are the canary** — they run on every scan, every `run_storm` graph op. If conversion blows up STORM's runtime, declare STORM's `native_engine = "arrow"` (no conversion) and let STORM consume Arrow tables directly. Decision pending benchmark.

### Phase 2 — Op-type registry + engine declaration + SDK contract (~1 week)

Each op declares `native_engine` in its module:

```python
# decoy_engine/graph/ops/sort.py
KIND = "sort"
NATIVE_ENGINE = "polars"   # NEW
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"
```

Runner reads the declaration to drive materialization. Pure plumbing.

**Lock the connector SDK contract here, not Phase 7.** External connector authors (current and future) need to know whether to return Arrow / Polars / Pandas. The contract: **connectors return Arrow tables.** The runner converts to the op's native engine. Document this in the SDK contract spec; full doc update lands in Phase 7 but the API is fixed here.

### Phase 3 — Polars relational ops (~3 weeks)

Port six ops to Polars LazyFrame:

- `filter` — `pl.LazyFrame.filter(pl.expr.eval(condition))`
- `sort` — `LazyFrame.sort(by, descending)`
- `dedupe` — `LazyFrame.unique(subset, keep)`
- `derive` — `LazyFrame.with_columns([pl.lit(...).alias(...)])`
- `join` — `LazyFrame.join(other_lazy, on, how)`
- `group_by` — `LazyFrame.group_by(by).agg([pl.col(...).method()])`

Mask / generate / STORM untouched.

**Critical: parity tests.** Each op gets a test matrix that runs the old pandas path and the new Polars path on the same input and asserts equivalent output. Document known semantic differences (NaN vs null, empty-string vs null on read, floating-point sort tie-break). **Budget 30% of phase time for parity tests, not 10%** — this is where you'll bleed.

**Forbidden footgun:** `.map_elements(callback)` for "I almost have a Polars expression but need a Python callback." Looks like Polars but isn't — slow, surprising, and breaks the planner. Code review checkpoint: every Polars op must justify any `.map_elements()` call or move the op back to pandas.

### Phase 4 — DuckDB source/sink connectors + dogfood opt-in (~2.5 weeks)

Port four ops to DuckDB:

- `source.file` (CSV / parquet / JSON; native streaming + glob)
- `target.file` (parquet via `COPY ... TO`)
- `source.db` (postgres_scanner / mysql_scanner / sqlite_scanner)
- `target.db` (DuckDB executes `INSERT ... SELECT` against attached target)

**This phase introduces the `engine: hybrid` opt-in.** Add a per-pipeline flag (top-level YAML key) that switches the runner from pandas-only to the hybrid path. Default is `engine: pandas` (current behavior). Customers who opt in run the new path on real data for 2–4 weeks before Phase 8 default-flip. **This is the difference between "we found an edge case in production" and "we found it on the dogfood pipeline."**

Old pandas readers stay as fallback for one release cycle past the default flip.

### Phase 5 — Preview path compatibility (~1 week)

The two preview serialization paths in `decoy_engine/graph/runner.py` (preview output) and `forge-platform/api/jobs/runner.py` (job runner preview) both convert to pandas at the boundary before JSON serialization. UI sees identical output regardless of internal engine.

**This is the critical "no UX regression" gate.** No ops change in this phase; only the boundary serializers. If the preview UI changes shape, this phase shipped wrong.

**Error message translation layer:** Polars / DuckDB raise different exception shapes than pandas. Add a thin error-translation module that maps engine-specific exceptions to user-friendly messages. **Don't skip this** — it's where "professional tool" gets won or lost. A Polars `SchemaError: column 'foo' not found` is fine for engineers but useless for the canvas user; translate to "Column 'foo' isn't in the input dataframe — did upstream drop it?"

### Phase 6 — Parity test suite + dogfood validation (~2 weeks)

Every op gets a test that runs the old pandas path and the new path on the same input and asserts equivalent output. Documented exceptions for known semantic differences (the list from Phase 3 + any new ones discovered in Phase 4).

**This is the gate for default-flip.** No phase 8 until this suite is green.

**Plus dogfood validation:** review the `engine: hybrid` opt-in pipelines from Phase 4. Document any edge cases. If the dogfood phase surfaced regressions that aren't fixed, hold default-flip.

### Phase 7 — Docs + connector SDK update (~1 week)

- Update `forge-platform/plans/2026-05-07-etl-direction-and-connector-sdk.md` to reflect "hybrid Polars + Pandas + DuckDB; no SQL dialect compiler." (Future-you will thank present-you.)
- Update `forge-engine/SHARED_ENGINE_ARCHITECTURE.md` with the three-engine boundary diagram.
- Write `forge-engine/POLARS_FOR_PANDAS_USERS.md` cheat sheet — half a day of `.df()` → `.collect()` mappings + the `.map_elements()` footgun callouts. Pays back forever for external contributors.
- Update connector SDK docs so external connector authors know the engine boundaries (the contract was locked in Phase 2; this is the formal write-up).

### Phase 8 — Default flip + old-path removal (~1 week)

`engine: hybrid` becomes the default. Pipelines without an explicit `engine:` key get hybrid. The old pandas-only path stays available via `engine: pandas` for one release cycle as a fallback, then gets deleted.

**Sales / marketing follow-ups (not blocking):**

- Update the pandas-ETL-ceiling memo with new tier numbers (the "millions, not billions" framing stays public-facing; the internal rules-of-thumb tier shifts up substantially).
- Release notes call out: "Memory ceiling messages disappear from the UI for source/transform ops."
- Train sales on the new conversation. The deal-size shift matters.

---

## Engine boundary by op (today vs target)

| Op | Today | Target engine | Notes |
|---|---|---|---|
| `source.file` (CSV/parquet/JSON) | pandas (full load) | DuckDB | Phase 4. Native streaming + glob. |
| `source.db` | pandas `read_sql` (full load) | DuckDB | Phase 4. `postgres_scanner` etc. |
| `target.file` | pandas `to_csv` / `to_parquet` | DuckDB | Phase 4. `COPY ... TO`. |
| `target.db` | pandas `to_sql` (full load) | DuckDB | Phase 4. `INSERT ... SELECT`. |
| `filter` | pandas `query()` | Polars | Phase 3. Lazy filter pushdown. |
| `sort` | pandas `sort_values` | Polars | Phase 3. |
| `dedupe` | pandas `drop_duplicates` | Polars | Phase 3. |
| `derive` | pandas `eval` | Polars | Phase 3. |
| `join` (queued op) | n/a | Polars | Phase 3 if Item 19 lands; otherwise deferred. |
| `group_by` (queued op) | n/a | Polars | Phase 3 if Item 19 lands; otherwise deferred. |
| `drop_column` / `select_column` / `limit` | "streaming" pandas | Polars | Phase 3. Pure column-projection / row-slicing. |
| `mask` | pandas | **Pandas** (kept) | Faker / Disguise / scipy. Per-row Python. |
| `generate` | pandas | **Pandas** (kept) | Faker / categorical / formula. Per-row Python. **FK-aware generators with reference tables need explicit thought** — the reference table in memory is a separate ceiling axis from the source/sink axis. Polars for orchestration (row count, column iteration) + pandas for per-cell value generation may be the right hybrid here; defer to Phase 9 follow-up. |
| `run_storm` | pandas | **Pandas** (kept; benchmark Arrow boundary in Phase 1) | STORM ops profile per-column with scipy stats. |
| `assert_*` (Roadmap Item 45) | n/a | Polars | When it lands; pure column ops. |

---

## Critical files

### Touched (this work)

- `decoy-engine/src/decoy_engine/graph/runner.py:72` — runner cache, becomes Arrow-canonical with eager eviction (Phase 1)
- `decoy-engine/src/decoy_engine/graph/ops/_base.py` — adds `NATIVE_ENGINE` declaration to op contract (Phase 2)
- `decoy-engine/src/decoy_engine/graph/ops/{filter,sort,dedupe,derive,drop_column,select_column,limit}.py` — Polars rewrites (Phase 3)
- `decoy-engine/src/decoy_engine/graph/ops/{source_file,source_db,target_file,target_db}.py` — DuckDB rewrites (Phase 4)
- `decoy-engine/src/decoy_engine/connectors/csv_connector.py:111-126` — dead chunked iterator becomes obsolete; remove after Phase 8
- `forge-platform/api/jobs/runner.py:247` — preview-path Arrow → pandas conversion (Phase 5)
- `forge-platform/plans/2026-05-07-etl-direction-and-connector-sdk.md` — doc update (Phase 7)
- `forge-engine/SHARED_ENGINE_ARCHITECTURE.md` — three-engine boundary diagram (Phase 7)

### New

- `decoy-engine/src/decoy_engine/graph/conversion.py` — `arrow_to_engine` + `engine_to_arrow` shims (Phase 1)
- `decoy-engine/src/decoy_engine/graph/errors.py` — engine-specific exception → user-friendly message translation (Phase 5)
- `decoy-engine/POLARS_FOR_PANDAS_USERS.md` — cheat sheet for contributors (Phase 7)
- `decoy-engine/tests/parity/` — dual-engine equivalence test suite (Phase 6)

### Untouched (deliberately)

- `decoy-engine/src/decoy_engine/transforms/*.py` — mask transforms stay on pandas
- `decoy-engine/src/decoy_engine/generators/*.py` — generation stays on pandas (Phase 9 follow-up may revisit FK-aware generators)
- `decoy-engine/src/decoy_engine/storm/*.py` — STORM stays on pandas; conversion cost benchmarked in Phase 1
- `decoy-engine/src/decoy_engine/forecast/*.py` — FORECAST is a pure function of `StormProfile`; engine-agnostic

---

## Verification

### Phase-level

- **Phase 1.** Existing 397 engine tests still pass; new tests assert `runner.cache` evicts entries with zero downstream consumers; STORM scan benchmark shows ≤10% slowdown vs. baseline (or STORM gets `NATIVE_ENGINE = "arrow"` declared).
- **Phase 3.** Each ported op has a parity test that runs old pandas + new Polars on a 100K-row fixture and asserts equivalent output. Documented exceptions list lives in `decoy-engine/tests/parity/SEMANTIC_DIFFERENCES.md`.
- **Phase 4.** Same parity tests for source/sink ops. `engine: hybrid` opt-in flag works on a sample pipeline; pandas default still works.
- **Phase 5.** Preview UI on `localhost:5173` shows identical output for the 4 representative pipelines (mask-only, transform-only, generate, hybrid) regardless of engine choice. Error messages on a deliberately-broken pipeline read user-friendly.
- **Phase 6.** Full parity test suite green. Dogfood pipelines from Phase 4 reviewed; no unfixed regressions.
- **Phase 8.** All non-`engine:` pipelines run on hybrid by default. `engine: pandas` still works as opt-out for one release cycle.

### Customer-impact validation

Run the calibration benchmark from the pandas-ETL-ceiling memo on a 32 GB box, before Phase 1 and after Phase 8. Expected results:

| Pipeline shape | Pre-D | Post-D |
|---|---|---|
| 1M rows, mask-only | <60s | <30s |
| 10M rows, mask-only | <120s | <60s |
| 50M rows, mask-only | OOM today | <300s on a good box |
| 100M rows, mask-only | OOM today | runs (single-table mask) |
| 10M rows, sort + dedupe + mask | OOM today | runs cleanly |

Numbers are estimates — calibrate against real customer data. If post-D is worse than 2× the estimate, hold the default flip and investigate.

---

## Risks I'd flag

1. **Parity test surface is the real cost.** ~16 ops × N edge cases × 2 engines (or 3 for ops in pandas + Polars + DuckDB). Budget 30% of Phase 3 + 4 time for tests. The known-difference list will surface bugs that look like regressions but aren't (NaN vs null is a famous one).

2. **STORM/FORECAST integration.** The Arrow boundary at every STORM scan is a recurring cost. Phase 1 benchmark gates this; if STORM slows down, declare `NATIVE_ENGINE = "arrow"` and let STORM consume Arrow tables directly without conversion. **Don't punt this to "we'll see in production."**

3. **`.map_elements()` footgun.** Polars' Python callback escape hatch looks like a savior when a transform doesn't quite fit the expression DSL. It's slow, surprising, and breaks the planner. Code review checkpoint: every `.map_elements()` call must be justified or the op moves to pandas. Don't ship a "Polars op" that's secretly a pandas op with overhead.

4. **DuckDB extension cold-start on Windows.** `postgres_scanner`, `httpfs`, `aws` extensions sometimes fetch at first call. **Test on a clean Windows VM before Phase 4 ships.** This is a "professional tool" failure mode if a customer's first run hits a download error. Pre-bundle extensions or surface a clear "fetching DuckDB postgres extension…" status message.

5. **Connector SDK contract drift.** External connector authors need to know "do I return Arrow / Polars / Pandas?" Lock in Phase 2 (Arrow), document in Phase 7. Don't let Phase 3–6 ship with ambiguity.

6. **Hard cutover risk.** Even with the dogfood phase, the default-flip in Phase 8 affects every customer pipeline. Keep `engine: pandas` available for one release cycle as a fallback so an unexpected regression doesn't strand customers. Decommission only when confidence is high.

---

## Open questions

1. **Polars version pin.** Polars releases break minor APIs more aggressively than pandas. Pin major.minor in `pyproject.toml` and bump deliberately. Recommend pinning to the latest stable at Phase 3 kickoff.

2. **DuckDB version pin.** Same story but tighter — DuckDB's storage format isn't backward-compatible across major versions. Pin major.minor.

3. **Memory limit knob.** DuckDB has a `memory_limit` PRAGMA. Should the engine read this from a config? Default `memory_limit = '50% of host RAM'` matches DuckDB's own default and gives sane behavior on customer boxes without manual tuning. Recommend yes.

4. **Streaming vs. batch.** DuckDB streams natively; Polars is lazy + collects in memory. For ops that must materialize (generate, mask), the source DataFrame must fit. Documenting the new "single masked table fits in available RAM" ceiling is the post-Phase-8 marketing line.

5. **Benchmark hardware target.** "32 GB box" was the pandas-ceiling baseline. Should the post-D rules-of-thumb assume the same hardware, or do we re-baseline against modern self-hosted assumptions (64 GB)? **Recommend keep 32 GB** — it's the conservative number and customers running on bigger boxes benefit beyond what we promise. Don't trade conservatism for headline numbers.

6. **Phase 9 follow-up: FK-aware generation hybrid.** Generation stays on pandas in this plan, but FK-aware generators (Items 16, 35) load full reference tables which is its own ceiling. Polars for the orchestration + pandas for per-cell generation may be the right hybrid. Out of scope for this plan; flag as Phase 9 when we get there.

7. **STORM `NATIVE_ENGINE`.** Phase 1 benchmark decides whether STORM consumes Arrow directly or pandas via conversion. Default to pandas (less migration risk); flip to Arrow only if benchmark shows >10% regression.

---

## Out of scope

- **Mask / generate engine swap.** Mask transforms stay on pandas; generation stays on pandas. This is by design — Faker / scipy / sklearn are the value, and per-row Python is what mask transforms inherently are.
- **SQL dialect compiler.** Per `forge-platform/plans/2026-05-07-etl-direction-and-connector-sdk.md`, we do not generate dialect-specific SQL. DuckDB executes the query plan; that's all.
- **Hard real-time / streaming / sub-second SLAs.** This plan addresses batch ceiling, not sub-second SLAs. Streaming/CDC (Item from Roadmap Deferred) remains deferred.
- **Cross-warehouse moves at extreme scale.** This plan helps mid-market scale (single 100M-row table on a 64 GB box). It does not help "1B-row Snowflake-to-Redshift mirror with sub-minute SLA" — that's a different product (Item 25 MIRROR Phase 3 covers warehouse-native fast paths separately).
- **Polars / DuckDB inside mask transforms or generation.** Tempting opt-in optimization but out of scope. Phase 9 if the pandas mask path becomes a real bottleneck on real customers.
- **A `decoy benchmark` CLI verb.** The calibration benchmark stays a manual harness in this plan. Could become a CLI verb in a follow-up if customers ask.

---

## Why this plan supersedes the cheap-wins prescription

The pandas-ETL-ceiling memo recommended three weeks of cheap wins:

1. **Wire the dead chunked CSV iterator** in `csv_connector.py:111-126`. → **Throwaway** post-D; DuckDB streams CSVs natively, better than the manual chunked iterator.
2. **Add `chunksize` to `source.db` / `target.db`.** → **Throwaway** post-D; DuckDB's `postgres_scanner` and `INSERT ... SELECT` handle this with proper query planning instead of arbitrary row chunks.
3. **Eager runner-cache eviction.** → **Preserved** as Phase 1, but ships as part of the Arrow refactor instead of as a standalone pandas patch.

Roughly 60% of the cheap-wins scope is throwaway. The cache-management half isn't throwaway, but it shouldn't ship as "patch pandas runner cache" — it should ship as "rebuild runner cache around Arrow," which is exactly Phase 1 of this plan.

Going straight to D saves ~2 weeks of work that would get deleted, and avoids shipping a transitional architecture that confuses future-us.

The pandas-ETL-ceiling memo's sales-facing math is still valid through the plan execution window — keep it in customer conversations until Phase 8 ships, then update with new tier numbers.
