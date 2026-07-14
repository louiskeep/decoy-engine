# Track B Completion Program — Autoroute to Avoid OOM at Any Size

- **Status:** ACTIVE (Cam selected Track B, 2026-07-12). Executed via autonomous `/loop`.
- **Goal:** The engine autoroutes any job to a method that will not OOM, at any row count, based on available hardware (cgroup/host memory + scratch disk). No manual mode selection, no wedge, no rc137-only failures.
- **Builds on:** `2026-07-10-oom-avoidance-routing-redesign.md` (the Track B design, Fable/Council-reviewed) and the 50M benchmark findings (`memory: decoy-50m-baseline-wedge`, findings #54/#56).

## Where we actually are (not a greenfield)

Already built + merged to engine main, flag-gated **default-OFF**, **never validated**:
- Estimator (`_mem_estimate*`), two-point probe (`_probe*`), runtime governor (`_governor*`), telemetry (`_mem_telemetry`), subprocess isolation (`_isolated_*`, `run_pipeline_isolated`). These are the doc's Sprint 1a/1b + B1–B5 machinery.

Why it was never turned on: the 50M benchmark's governor phase (B6) came back **DEGRADED** — it proved *containment* (clean SIGKILL + diagnostic, no wedge) but never proved *reroute-to-completion*. Root-cause (#54, code-verified + measured #56): two independent defects.

### The two defects this program must fix first

1. **Out-of-core is not memory-bounded in production (#56, foundational).** The "50M at 4.2 GB" proof used a test-only path (`run_fk_out_of_core` + `LazySource`, streaming). The production path (`run_pipeline` → governor → `_isolated_worker`) does `pq.read_table` on every input table (full input resident) and calls `run_pipeline` with **no sink** (full output resident, then staged). Measured: +496 MB over direct at 1M (≈ input size), ~2.1× at 50M. So even perfect routing to out-of-core does not bound memory at scale — 100M would wedge on input residency alone. Out-of-core today bounds only the join/mask working set, not input or output.
2. **Governor budget never reaches DuckDB (#54).** `run_job_with_governor(budget_bytes=X)` uses `X` only for the RSS-kill threshold; it is never forwarded to `run_pipeline`'s `out_of_core_budget_bytes`, so `resolve_budget(None)` falls back to 25% of *host* RAM (`_HOST_RAM_FRACTION`), not the job's slot budget. Wrong on any non-32 GB host and with concurrent slots.

## Sprint sequence

Each sprint: Sonnet builds from this spec → dennis adversarial gate → remediate → barry docs → CI-gate green → I verify high-stakes. Flags stay OFF until TB-5. No GCP spend until TB-6 (Cam-gated).

### TB-1 — Foundation: make out-of-core truly memory-bounded (fixes #56) — FIRST, unblocked
The streaming runner already exists and is proven by B3; this is glue, ~4 files.
- **Input:** wire `LazySource` through `_isolated_worker._load_sources` (the manifest already carries file paths via `_isolated_run._write_payload`) and through `run_pipeline`'s `sources` handling so out-of-core input is not materialized via `pq.read_table`. Audit every `run_pipeline` site that assumes `pa.Table` (route decision, eligibility, merge-with-generate-outputs) so a `LazySource` is not accidentally forced resident.
- **Output:** pass a `ParquetTransactionalSink` through `run_out_of_core_route` in the governed/isolated path so outputs stream to disk instead of returning fully in memory then staging.
- **Budget → DuckDB:** forward the governor's `budget_bytes` to `run_pipeline`'s `out_of_core_budget_bytes` so DuckDB's `memory_limit` is sized from the job slot budget, not host RAM.
- **Ordering:** route decision must happen before eager materialization, or the lazy wrapper must defer materialization until a route that needs residency (full_frame/sequential) actually asks for it.
- **Acceptance:** (a) governed out-of-core peak ≈ direct out-of-core peak for the same job (no ~2× input/output residency; re-measure at 1M locally, delta ≈ machinery not input); (b) a cgroup-capped run where input bytes exceed the cap **completes** via out-of-core (input never fully resident); (c) DuckDB `memory_limit` observably equals the passed slot budget, not 25% host. Land the failing test first (proves the residency pre-fix, passes post-fix).

### TB-2 — Governor calibration + reroute-to-completion
- Fix the budget window so a trip reroutes to a route that **completes**: budget sits above out-of-core's now-bounded peak and below full_frame's real peak. With TB-1, out-of-core fits a realistic slot budget.
- **Acceptance:** the vacuity-guard condition the 50M B6 failed — an auto run that trips full_frame reroutes and shows `tripped=true, route!=full_frame, completed=true, fk_internal_consistency=ok`. Verified in a unit/perf test under a memory cap.

### TB-3 — Local cgroup-capped validation (NO GCP spend)
- Per the design's §7 local fallback: run the estimator+probe+governor under a systemd/cgroup `memory.max` cap on the devbox at 4–6M rows to prove, end to end and with zero VM spend: correct route selection by *bytes vs budget* (width-change test, not row-count), reroute-to-completion, and out_of_core-vs-full_frame **path parity** (byte-identity).
- This is the confidence gate before enabling flags (TB-5) and before paying for GCP (TB-6).

### TB-4 — Calibration + telemetry (doc B5)
- Recompute `k_path` multipliers from TB-3's clean per-job isolated peaks (Sprint 1a isolation makes them attributable). Pin the initial `k_full_frame` / `k_out_of_core` / `k_sequential` and the `error_band`; define the recalibration trigger. Retire the unmeasured placeholder constants (§13).

### TB-5 — Enablement + contract migration (doc B3)
- Flip `use_byte_estimate_routing`, `use_probe_routing`, governor, and `isolated_execution_enabled` to **default-ON**, each behind its existing rollback flag.
- Contract migration: deprecate the row-count reject (`fk_full_frame_oom_risk_rejected`), rewrite the 18 reject tests to byte-based, keep the irreducible ineligible+too-big reject class, update CHANGELOG + SC5 docstring, coordinate platform admission. Preserve the compatibility-contract process (engine is pre-GA; `is_pre_ga()` gates).
- **Acceptance (doc §9):** router keys off computed bytes vs cgroup budget (width-change test); a schema unlike the calibration shape routes without OOM; full_frame only when `estimate × (1 + error_band) < budget`; no cgroup-limited run OOMs across the parity/perf suite.

### TB-6 — Validation at scale (COST-GATED, Cam-approved per run)
- Corrected GCP 50M re-run (flags ON, calibrated budgets, TB-1 streaming) → prove bounded memory + reroute-to-completion at 50M via the **production** path (not the test path). Recalibrate the bench battery so B1 does not self-wedge and B4's no-admission-control test runs last.
- Then **100M** — the goal. HARD-GATED on Cam's explicit spend approval AND TB-1..TB-5 green AND the 50M production-path run proving bounded memory.
- Document 50M + 100M on the /proof page (#16).

## Guardrails
- Flags OFF through TB-4; ON only at TB-5 behind rollback flags.
- No GCP spend before TB-6; each paid run Slack-flagged for Cam.
- 100M stays hard-held until TB-1..TB-5 are green and the 50M production-path run proves bounded memory.
- Established-methodology rule (engine CLAUDE.md): cite source patterns; do not roll our own.
- Every sprint dennis-gated before merge; barry docs after.
