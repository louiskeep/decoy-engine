---
Status: planning
Branch: `chore/hybrid-engine-bug-followup`
References:
  - [`plans/2026-05-10-hybrid-engine-dogfood-review.md`](2026-05-10-hybrid-engine-dogfood-review.md) — the engine dev's wrap-up notes this plan responds to.
  - [`SHARED_ENGINE_ARCHITECTURE.md`](../SHARED_ENGINE_ARCHITECTURE.md) — three-engine substrate spec; Bug 3 falls out of the gap between this guide and current code.
  - `decoy-platform/plans/2026-05-09-test-fixtures.md` (on `claude/test-database-setup-KJ6rY`) — fixture base for the calibration benchmark (Bug 5).
---

# Hybrid engine — code review follow-up plan

## Context

Engine dev shipped the 8-phase Polars+DuckDB hybrid (Item 47) on `claude/sprint-c-auth-engine-plan-9pWL0`. Code review on **2026-05-09** confirmed the substrate is sound — runner cache + eviction match the plan; per-op `NATIVE_ENGINE` declaration is clean; SEMANTIC_DIFFERENCES.md is honest; Polars relational ports are tight; `source.file` is genuinely DuckDB-native.

But review surfaced **5 issues** that need follow-up before the work is "done." Severity, in order:

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | Optional deps + flipped default — `pip install decoy-engine` would crash on first pipeline run | BLOCKER | **Fixed** — `fix/promote-hybrid-deps` 4887ef3, ready to merge |
| 2 | STORM benchmark not reproducible — dev's 2.4% vs reviewer's 56.2% | BLOCKER | This plan, §Bug 2 |
| 3 | DB sources/sinks don't actually use DuckDB streaming despite declaring `NATIVE_ENGINE='duckdb'` | DEGRADATION | This plan, §Bug 3 |
| 4 | `types_mapper=pd.ArrowDtype` skipped; default `.to_pandas()` always copies | IMPROVEMENT | This plan, §Bug 4 |
| 5 | Phase 8 50M-row calibration benchmark never actually run | CONFIDENCE GAP | This plan, §Bug 5 |

Bug 1 is already resolved on its own branch and just needs the merge. Bugs 2 / 4 are scoped (~0.5–1 day each). Bugs 3 / 5 are bigger pieces of work and split into phases. The plan tracks all five so we don't lose them when we return to Sprint C.

> **No production users today.** This product is in development; no users are affected by these gaps. We have time to do the cleanup right rather than ship under pressure.

---

## Bug 1 — Optional deps + flipped default *(fixed)*

### What we found

Phase 8 flipped the runner default from `engine: pandas` to `engine: hybrid`, but `pyproject.toml` still listed `polars` and `duckdb` under `[hybrid]` extras. So `pip install decoy-engine` (no extras) would crash on the first pipeline run with `ModuleNotFoundError: polars`.

### What we did

`fix/promote-hybrid-deps` (head `4887ef3`):

- Promoted `polars>=1.0` and `duckdb>=0.10.0` to required deps.
- Kept the `hybrid = []` extras alias so existing `pip install decoy-engine[hybrid]` invocations don't error.
- Comment in `pyproject.toml` explains the why so the next reviewer doesn't re-orphan them.

### Verify

```bash
pip install -e . --no-deps
pip install -e .              # no extras
python -c "import polars, duckdb; print('ok')"
pytest tests/                 # 494 passing including hybrid paths
```

### Action

Merge `fix/promote-hybrid-deps` to `main` whenever convenient. No blockers.

---

## Bug 2 — STORM benchmark not reproducible

### What we found

`tests/benchmark/test_storm_arrow_boundary.py` measures Arrow→pandas conversion overhead on STORM scans. The architecture decision rule: <10% overhead → keep `NATIVE_ENGINE='pandas'`; ≥10% → declare `NATIVE_ENGINE='arrow'`.

- **Dev's measurement (Phase 2 commit notes):** 2.4% overhead on Linux / Python 3.11 → kept pandas.
- **Reviewer's measurement (2026-05-09, Windows 11, Python 3.10, 16 GB, AMD Ryzen):** 56.2% overhead → would have flipped to arrow.

That's a 23× discrepancy. The test asserts only the catastrophic-regression threshold (50%), so CI doesn't catch the divergence.

### Why it matters

Both numbers are real. The overhead is genuinely **machine-dependent** because Arrow→pandas conversion cost is sensitive to:

- **pyarrow version** — zero-copy paths matured significantly across pyarrow 14 → 24.
- **Python version** — string / bytes handling path differs between 3.10 and 3.11+.
- **CPU cache size** — the workload is memory-bandwidth-bound, so cache topology matters.

So the Phase 2 architectural decision ("STORM stays on pandas because conversion is cheap") holds on modern Linux + recent pyarrow + 3.11 — and breaks on older Windows + 3.10. We can't build customer-facing perf claims on top of pandas-via-conversion when the variance is that wide.

### Approach

Bug 2's resolution depends on Bug 4's data. If `types_mapper=pd.ArrowDtype` works (Bug 4) the conversion cost largely vanishes and pandas-mode STORM is fine; if it doesn't, we have a real perf problem and should port STORM to `NATIVE_ENGINE='arrow'`. So: **do Bug 4 first, then decide Bug 2.**

**Three options on the table:**

- **A. Re-benchmark on a standardized environment** (GitHub Actions). Cheap, removes machine noise, gives a stable regression baseline.
- **B. Flip STORM to `NATIVE_ENGINE='arrow'`** unconditionally. Eliminates the conversion cost entirely; trades against STORM's pandas / scipy assumptions in column-level processing.
- **C. Make it runtime-configurable** per pipeline. Most flexible; most surface area to test.

**Recommendation: do A always (CI baseline), and let Bug 4's outcome decide whether B is also needed.** Variance this wide means we shouldn't ship customer-facing perf on top of pandas-via-conversion when a better path exists — so if Bug 4 shows Arrow-backed pandas doesn't help, we port STORM (B). C stays out of scope.

### Cloud / single-laptop options *(direct answer to your question)*

You don't need to own multiple machines.

- **GitHub Actions — recommended default.** Free for public repos, ~$0.008/min for private. Standard runners are 4-core / 16 GB Linux — enough for both the STORM benchmark and the 8M-row calibration tier (Bug 5). Add a workflow that runs on a `[run-bench]` PR label and posts overhead as a PR comment. Removes "I don't have hardware" friction permanently.
- **EC2 spot — for the rare big runs.** `r5.xlarge` is 32 GB / ~$0.04/hour spot. Spin up, run, kill. ~$1 for a thorough run. Good for the 50M-row calibration in Bug 5; overkill for routine PR benchmarking.
- **Claude Code cloud sandbox:** standardized ephemeral Linux VM. Useful for portable measurements, not differentiated enough to span hardware shapes.

GitHub Actions is the right default for everything except the 50M-row calibration tier.

### Phasing

| Phase | Scope | Effort |
|---|---|---|
| 2.1 | After Bug 4 lands, re-run STORM benchmark with whichever conversion path Bug 4 settles on | 0.25 day |
| 2.2 | GitHub Actions workflow `.github/workflows/benchmark.yml` — runs on `[run-bench]` PR label, posts overhead to PR comment | 0.5 day |
| 2.3 | **Decision gate:** if median overhead across CI runs is <10%, leave STORM on pandas; if 10–25%, document the caveat + tighten the catastrophic threshold from 50% → 25%; if >25%, flip STORM to `NATIVE_ENGINE='arrow'` (engine dev's lean: just port it — variance is too wide to keep on pandas-via-conversion) | 0.5 day |

**Total: ~1.25 days** if STORM-to-arrow port isn't needed; **+3–5 days** if it is.

### Out of scope

- Option C (runtime-configurable per-pipeline) — premature; revisit only if (B) regresses something.
- Benchmarking other ops at the Arrow boundary — same methodology will apply when needed.

---

## Bug 3 — DB sources/sinks don't actually use DuckDB

### What we found

`source.db` declares `NATIVE_ENGINE = "duckdb"` and the engine plan claims "DuckDB streams large tables to disk so they don't OOM." But `_apply_duckdb` in [`source_db.py`](../src/decoy_engine/graph/ops/source_db.py) does:

1. `pd.read_sql(query, sqlalchemy_engine)` — pandas materializes the entire result.
2. Convert to Arrow.

So the streaming claim is false for DB sources. `target.db` has the same pattern reversed: Arrow → pandas → `df.to_sql()`. The architectural promise of "DuckDB-native I/O" only holds for `source.file` (which legitimately uses `read_csv_auto` / `read_parquet`).

### Why it matters

This is the difference between "memory-bound at the DB row count" and "streams to disk." For a customer scanning a 50M-row Postgres table on a 16 GB box, the current implementation OOMs; the architecturally-claimed implementation doesn't.

### Snowflake / Redshift staging — not actually a hack

Engine dev's correction: the parquet-via-S3 staging pattern is **industry-standard**, not a hack. dbt does it, Fivetran does it, Airbyte does it, Snowflake's own docs push you toward `COPY INTO S3` for any cross-system bulk move. The pattern is the right shape; the UX of "customer maintains the S3 stage" is what would feel hacky.

Two paths handle this:

1. **Honest two-mode UI.** "Direct connection: ~10M-row ceiling. Staged via S3: no ceiling." Customer picks based on their data shape. Ships in 1–2 days once we have a customer.
2. **Hidden orchestration.** Customer gives Decoy S3 credentials once; Decoy itself runs the `UNLOAD` / `COPY INTO` behind the scenes. One button. ~2–3 weeks beyond the per-DB scanners.

**Pre-customer it's not worth building either.** Post-customer-signal (when you have a real Snowflake user) (2) is the obvious build. For now: ship per-DB scanners for the easy-DB majority + document the warehouse path as "staged via parquet, hidden orchestration coming when we have a customer who needs it." That's honest "we know what good looks like, building toward it."

### Approach: per-database, leverage DuckDB scanner extensions

| DB | DuckDB scanner | Effort | Initial scope | Notes |
|---|---|---|---|---|
| **Postgres** | `postgres_scanner` (extension) | ~1 day | **Yes** | The most-asked-for ICP DB |
| **SQLite** | `sqlite_scanner` (extension) | ~0.5 day | **Yes** | Already what the fixtures plan exercises; trivial port |
| **MySQL** | `mysql_scanner` (extension) | ~1 day | **No — wait for customer signal** | Engine dev's call: skip until someone asks |
| **Redshift** | `postgres_scanner` (Redshift speaks PG wire) | ~1.5 days incl. compatibility testing | No — wait for customer signal | Some PG features work, some don't; document compatibility limits when we engage |
| **Snowflake** | None native; staged via parquet/S3 | ~3 days for honest two-mode UI; +2 weeks for hidden orchestration | No — wait for customer signal | Industry-standard pattern; build (2) when first customer signs |
| **SQL Server** | None native | — | No — pandas fallback | Document; revisit if customer demand |
| **Oracle** | None native | — | No — pandas fallback | Same |

Initial scope = **Postgres + SQLite only.** Covers the OSS majority + the fixtures plan; defers MySQL and the warehouses until a real customer surfaces demand. We avoid building three connectors for hypothetical users.

### Connector maintenance — your CI/CD question, honest answer

Not one-and-done. Each DuckDB scanner extension has its own release cadence (~quarterly), and upstream DBs change behavior at major versions. Realistic ongoing cost:

- **Pin DuckDB version** in `pyproject.toml` (we already pin `>=0.10.0`; we'd tighten that).
- **Pin extension versions** via `INSTALL postgres_scanner; LOAD postgres_scanner;` against a known DuckDB release.
- **Integration tests against docker-compose Postgres + SQLite.** The test matrix runs in CI on every PR. If a scanner regresses, the matrix turns red.
- **Smoke-test against each supported DB on every engine release.** ~1–2 hours/quarter once the harness is set up. Less than maintaining the SQLAlchemy paths today.

So: **set-up cost ~1 day; ongoing ~1–2 hours/quarter.** Less than the current pandas/SQLAlchemy maintenance because there's less custom code in the path.

### Phasing

| Phase | Scope | Effort |
|---|---|---|
| 3.1 | docker-compose harness for Postgres + SQLite + integration test scaffolding | 1 day |
| 3.2 | Port `source.db` to use `postgres_scanner` for Postgres + parity tests | 1 day |
| 3.3 | Port `source.db` to use `sqlite_scanner` for SQLite + parity tests | 0.5 day |
| 3.4 | Mirror to `target.db` for both (writes go via `COPY` to a staging table, then DB-side INSERT) | 1 day |
| 3.5 | Update `SHARED_ENGINE_ARCHITECTURE.md` with the actual streaming guarantee per DB; mark MySQL / warehouses / MSSQL / Oracle as pandas-fallback with a "engages on customer signal" note | 0.25 day |

**Total: ~3.75 days** for Postgres + SQLite. MySQL / Redshift / Snowflake / SQL Server / Oracle deferred.

### Out of scope

- MySQL scanner — defer until a customer signal.
- Snowflake / Redshift staged-parquet flow (either UI mode). Engages when a customer signs.
- SQL Server / Oracle native scanners. Same.
- Streaming on the *write* side beyond what `COPY ... FROM` gives us natively.

---

## Bug 4 — `types_mapper=pd.ArrowDtype` skipped *(do this first)*

### What we found

In [`graph/conversion.py`](../src/decoy_engine/graph/conversion.py) (line 39–43), the Arrow→pandas conversion uses the default `.to_pandas()`, which copies every column from Arrow into numpy-backed pandas. Engine dev's comment notes that `types_mapper=pd.ArrowDtype` (pandas 2.0+) would give zero-copy Arrow-backed columns, but masker / faker code "assumes legacy numpy-backed dtypes," so it was skipped.

### Why it matters

For wide tables and large frames, the copy is the dominant cost of `pandas` → `polars` → `pandas` round-trips. Eliminating it is potentially a meaningful perf win for any pipeline that touches both engines. **The masker / faker compatibility concern is a measurement question, not a settled fact.**

### Why this is first

**Bug 4 → Bug 2 → Bug 5** is the right dependency order:

- If Arrow-backed pandas works, the conversion cost drops significantly. Bug 2's 56% overhead might evaporate without any STORM port.
- If Arrow-backed pandas works, the masker step in Bug 5's hybrid pipeline gets cheaper too — and the OOM threshold on a 16 GB laptop pushes upward, making the calibration-on-laptop story more credible.

So a half-day spike unblocks the data we need for the next two bugs.

### Approach: half-day investigation, then decide

**Phase 4.1 — Compatibility spike (~0.5 day):**

1. Build a fixture: 100k rows × 20 columns, pandas DataFrame, both numpy-dtype and Arrow-dtype variants.
2. Run each transform from `decoy_engine.transforms` against both:
   - faker (string output)
   - hash (int / string)
   - redact (any → null)
   - map (string lookup)
   - shuffle (any column)
   - passthrough (no-op)
   - date_shift (datetime)
   - formula (mixed)
3. For each: does the transform run? Does output match? Is it faster / slower / same?

**Phase 4.2 — Decision gate:**

| Outcome | Action |
|---|---|
| All 8 transforms work + are faster on Arrow-dtype | Flip default to `types_mapper=pd.ArrowDtype` for the whole engine; document in `SHARED_ENGINE_ARCHITECTURE.md` |
| Mixed (some break, some don't) | Add an opt-in flag at the conversion site; default stays numpy; document which transforms benefit |
| All 8 break or are slower | Leave the comment expanded with the measured numbers; no change |

**Total: ~0.5 day spike + 0.5 day to act on whichever outcome we get = 1 day**.

---

## Bug 5 — Phase 8 50M-row calibration benchmark never run

### What we found

The hybrid plan said "calibrate engine ceilings on 50M-row HIPAA-shaped data" as part of Phase 8. The dogfood notes admit this wasn't done. Predicted ceilings (where pandas falls over, where polars OOMs, where DuckDB streams) are educated guesses, not measurements.

### Why it matters

The whole architectural argument for the hybrid engine is "we hit ceilings with pandas-only at scale, and the hybrid pushes them out." Without measurements, that argument is a hypothesis. Customers asking "what's the largest dataset Decoy can mask?" deserve a measured answer.

### Hardware reality check (your "small machine" question)

- Your laptop: 16 GB RAM, single user.
- 50M rows of HIPAA-shaped data: ~200–300 bytes/row in pandas → ~10–15 GB materialized.
- Pandas-native pipeline on 50M rows: **OOMs** on 16 GB. Confirmed.

But not every pipeline materializes everything. Three regimes:

| Pipeline shape | Materialization | 50M on 16 GB? |
|---|---|---|
| **DuckDB-only** (source.db → filter → sort → target.file) | Streams; pandas never holds the rows | **Feasible** |
| **Polars-only relational** (source.file → filter → derive → target.file) | Polars streaming engine; works for most ops | **Often feasible** |
| **Hybrid with pandas mask** (source → mask.faker → target) | Pandas materializes at the mask step | **OOMs** |

So we can measure SOMETHING at 50M on the laptop, but the most interesting case (the hybrid one) won't fit.

### Approach: split engineering-correctness from marketing-correctness

The architectural property we want to prove is **asymptotic** — "does the engine handle data bigger than RAM?" That qualitative answer transfers across scale even if the absolute numbers don't. So split it into two cheap, decisive checks:

1. **Engineering-correctness check (laptop, ~$0).** Generate an 8M-row HIPAA fixture (~3 GB on disk). On `engine: pandas`: pipeline OOMs around the 5M-row mark. On `engine: hybrid`: pipeline completes. Architecture validated.
2. **Marketing-correctness check (cloud, ~$1).** Same shape, run on a 32 GB EC2 spot box at 50M rows. Capture real throughput numbers. Sales-line validated.

You can do (1) today on your laptop. (2) is for when you want to publish numbers. Don't conflate them.

The senior dev's fixture plan caps committed fixtures at 70k rows by design — "realism per signal, not per row." That's the right call for unit / integration tests. The calibration benchmark sits *above* that — generated locally on demand, never committed.

**Three tiers:**

| Tier | Size | Hardware | Runs on laptop? | Output |
|---|---|---|---|---|
| **Smoke** | 10k–70k rows | senior dev's fixtures (committed) | Yes | Per-PR CI |
| **Engineering-correctness** | 8M rows (~3 GB) | generated locally, not committed | Yes (pandas OOMs at ~5M, hybrid completes) | Manual benchmark, captured in plan; this is the architectural check |
| **Marketing-correctness** | 50M rows | generated locally, run on EC2 spot | No (hybrid OOMs at ~10M+ on 16 GB) | One-shot cloud run; ~$1 in spot pricing; numbers go in `SHARED_ENGINE_ARCHITECTURE.md` |

**Generating 50M rows on a 16 GB laptop is fine** — Decoy generators support `chunk_size` config and write streaming parquet. ~30 minutes wall-clock, ~10–15 GB on disk, never holds all rows in memory at once. The OOM problem is **running the hybrid pipeline against it**, not generating it.

### Phasing

| Phase | Scope | Effort | Hardware |
|---|---|---|---|
| 5.1 | Reuse senior dev's fixture configs as the seed; add `fixtures/configs/calibration/` with HIPAA-shaped 8M-row variant (same YAMLs, scaled `row_count`) | 0.25 day | Laptop |
| 5.2 | Build `tests/benchmark/test_calibration.py` — parameterized on tier, runs hybrid + pandas-only versions, prints throughput + peak memory + records OOM/completion outcome | 0.5 day | Laptop |
| 5.3 | Run engineering-correctness tier (8M) on laptop; capture results to `tests/benchmark/results/2026-05-XX-calibration.md`. Expected: pandas OOMs ~5M, hybrid completes ~8M | 0.25 day | Laptop |
| 5.4 *(optional, defer until publishable numbers needed)* | Marketing-correctness tier (50M) on `r5.xlarge` spot (~$0.04/hour, ~$1 total); capture throughput + memory profile | 0.5 day | EC2 spot |
| 5.5 | Update `SHARED_ENGINE_ARCHITECTURE.md` "ceilings" section with measured numbers; replace predicted-ceiling claims | 0.25 day |

**Total: ~1.25 days** for engineering-correctness only. **+0.5 day** if/when we run marketing-correctness.

### Out of scope

- 250M-row stress data. Same reasoning — extrapolate or rent a bigger box, but neither is critical for the substrate decision.
- Multi-machine / cluster scale. Not in the roadmap.
- Streaming / CDC benchmarks. Whole streaming module is queued.

---

## Recommended order of operations *(final, after engine dev's response)*

Ordered by ROI and dependency:

| # | Task | Effort | Why this order |
|---|---|---|---|
| 1 | **Merge Bug 1** | 0 days | Already done; just needs the merge. |
| 2 | **Bug 4 investigation** — does masker / faker code break under `types_mapper=pd.ArrowDtype`? | 0.5 day | Feeds Bug 2 + Bug 5. If Arrow-backed pandas works, the conversion cost drops and we don't need the STORM-to-arrow port. |
| 3 | **Bug 5 engineering-correctness** — 8M-row HIPAA fixture, run pandas (expect OOM), run hybrid (expect success). Architectural claim validated cheaply. | 0.5 day | Cheap qualitative validation. Bug 4's outcome affects the OOM threshold, so do this after Bug 4. |
| 4 | **Bug 2 decision** — based on Bug 4's data, either re-benchmark STORM with Arrow-backed pandas (if Bug 4 helped) or scope STORM-to-Arrow port (if it didn't). | 0.5 day decision + 0–5 days execution | Decision drops out of Bug 4's data. |
| 5 | **Bug 3 — Postgres + SQLite scanners** (defer MySQL until customer signal). | 3.75 days | Delivers the actual architectural promise for the most common ICP databases. |
| 6 | **GitHub Actions benchmark workflow** | 0.5 day | Removes "but I don't have hardware" friction permanently. |

**Sequence total: ~6 dev-days** before STORM port (if needed); **+3–5 days** if Bug 4 doesn't help and we port STORM. Then back to Sprint C.

## Lessons learned (worth absorbing for next plan)

Engine dev's meta-point on the hybrid plan's gating discipline:

> "When a plan has 4 gate criteria and only 2 are verifiable in your environment, ship 2 and explicitly hold the gate open for the other 2 — don't declare 4/4 done."

The Phase 6 gate the engine dev declared green wasn't actually green. The plan's gate was "calibration benchmark + customer-shape fixtures" — the easy two checks (`tests pass` + manual smoke) got marked done, the hard two (50M-row calibration + reproducibility) stayed undone but the gate flipped anyway. That's how Bugs 2 + 5 escaped detection until code review.

**Rule for future plans:** if a gate has criteria you can't verify in the current environment, the gate stays yellow until they're verified — you don't fold them into "later" or rely on "this is in the dogfood notes." Either:

- Run them in a different environment before declaring the phase done, OR
- Explicitly defer the gate criterion in the plan with an "owed before merge" note.

Plan-rot also bit Bug 1: the Phase-1 decision to keep `polars` / `duckdb` optional was sound at the time, but Phase 8's flip of the default never circled back to reconcile. **Rule:** when a later phase changes a contract, every earlier phase's decisions get re-checked against the new contract before merging.

---

## Sprint C continuation

Once this plan is committed, the engine work pauses and we return to **Sprint C** (`forge-platform/plans/2026-05-09-sprint-c-auth-foundation.md`).

**Status as of branch start:**

- ✅ Item 2.A — Audit log infrastructure (X-Total-Count + /actions endpoint) — shipped `0a4d172`.
- ✅ Item 2.B — Password reset flow — shipped `2cb3856`.
- ⏳ **Item 2.C — Session management** *(next, ~1.5 days)*.
- Item 39 — SSO / OIDC (~1 week).
- Item 26 — Per-table RBAC (~1.5–2 weeks).

Returning to Item 2.C immediately after this plan commits.

---

## Out of scope (own plans)

- **The roadmap entry for Item 47 ("Polars+DuckDB hybrid engine")** — Bug 1 closes one BLOCKER on it; the roadmap entry's "shipped" status flips only after Bugs 2–5 are resolved. Roadmap edit is a 5-minute follow-up.
- **`SHARED_ENGINE_ARCHITECTURE.md` ceiling claims** — Bug 5 phase 5.5 owns the doc edit. Out of scope here.
- **Test fixtures plan** — not this plan's concern; it ships separately on `claude/test-database-setup-KJ6rY`. We just consume its YAMLs as the seed for Bug 5 calibration.

