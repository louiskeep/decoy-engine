# OOM-Avoidance Execution-Routing Redesign — Implementation Spec

- **Date:** 2026-07-10
- **Status:** DRAFT — implementation gated on Sprint 0 speed determination (see §7). Do not start Track A/B until the out_of_core-vs-full_frame speed ratio is measured from the 50M/100M benchmark. **Revised 2026-07-10 after Fable adversarial review — headline estimates below are corrected inline; full keyed corrections in §11 (read before implementing).**
- **Owner:** Cam (decoy backend lane)
- **Scope:** decoy-engine `execution/` routing + decoy-platform `api/jobs/admission.py`
- **Related:** `docs/plans/2026-07-07-next-up-roadmap.md`, `docs/plans/2026-07-09-consultant-f1-f2-bounded-profiling.md`, SC5 cross-repo query surface (`src/decoy_engine/execution/__init__.py:25-42`)

---

## 1. Problem

The engine automatically routes FK+mask jobs across three execution paths — `full_frame` (all in RAM, fast, OOMs at scale), `sequential` (bounded but O(cardinality)), `out_of_core` (DuckDB streaming, RAM-capped + disk spill, genuinely bounded). The routing decision is driven by a **fixed row-count proxy against two hand-calibrated constants**:

- `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` (5M) — largest mask table ≥ this ⇒ route out_of_core.
- `FULL_FRAME_REJECT_ROWS_DEFAULT` (7.5M) — full_frame-bound job ≥ this ⇒ **hard-reject** (`fk_full_frame_oom_risk_rejected`) rather than risk OOM.

**The flaw:** those constants were calibrated on **one schema shape** (3 tables, width 16, hash-chain FK). Peak RSS depends on width × table-count × dtype mix, none of which the row count captures. Widen the tables or add FK tables and the true OOM cliff moves to a different row count, but the constant does not — so the router mis-routes on any schema unlike the calibration one: it OOMs jobs it thought were safe, or needlessly rejects/downgrades jobs that would have fit.

### Empirical grounding (2026-07-10 50M benchmark, B1 full-frame sweep, 32 GB node)

Peak RSS is **dead-linear** in rows for the calibration schema: ~4.1 GB per 1M rows/table (3 tables, width 16).

| rows/table | peak RSS | result |
|---|---|---|
| 1M | 4.4 GB | OK |
| 2M | 8.5 GB | OK |
| 4M | 16.6 GB | OK |
| 6M | 24.8 GB | OK |
| 8M | — | OOM (rc137) |

The linearity is the key enabling fact: it means a constant per-path *multiplier* over computed raw-byte size is the correct model, and a 1%-rows probe extrapolates exactly.

---

## 2. Findings that shape the design (measured 2026-07-10)

1. **A byte-level estimator already exists on the platform side.** `decoy-platform/api/jobs/admission.py` (~17.7 KB) computes `estimate_mb = input_bytes × multiplier` with per-path multipliers (`CSV_MEMORY_MULTIPLIER=6.0`, `PARQUET_MEMORY_MULTIPLIER=1.5`) and soft/hard thresholds → admit/queue/reject. Caveats: it is **measure-only** (`admission_control_enabled=False`), **not wired to the engine** (no shared constants; two disconnected brains that can disagree), and **host-RAM-based** (no cgroup awareness). This is "consolidate + calibrate", not "build from scratch."

2. **The reject path is a documented cross-repo contract.** `decide_execution_route`, the reject constants, `check_out_of_core_compatibility`, and the `fk_full_frame_oom_risk_rejected` (+`_estimated`) error codes are the "SC5 cross-repo query surface" (`execution/__init__.py:25-42`, exported in `__all__`, in CHANGELOG as behavioral contract, referenced across 3 roadmap docs). Deleting the reject path is a **contract migration** with a deprecation window, not a free deletion.

3. **No cgroup awareness anywhere.** Both repos key off *physical host RAM* (`sysconf SC_PHYS_PAGES` / `/proc/meminfo` / `psutil.virtual_memory`). The "32 GB" finding is really a 32 GB *limit*; the router should read the cgroup limit (`memory.max`), not host RAM.

---

## 3. Design (recommended by Fable, 2026-07-10)

**One-line:** schema-derived byte estimate × per-path multiplier, micro-probe near boundaries (linearity makes it cheap and exact), out_of_core as the default answer to all uncertainty so hard-reject disappears, asymmetric margins favoring downgrade over OOM, and a dumb RSS watchdog whose trips feed a telemetry loop that keeps the estimator self-calibrated.

### 3.1 Route on predicted bytes vs. a real budget

Retire the row-count constants. Routing input is `est_peak_bytes(job, path)` compared to `budget = effective_limit × 0.75`, where `effective_limit` is the **cgroup limit** (`memory.max`) or container limit, falling back to host available RAM only if no cgroup is present.

### 3.2 Two-factor estimate (the core fix)

Keep the two factors separate:

- **Raw data bytes** — schema-derived, *computed, never calibrated*: per table `rows × Σ per-column dtype cost` (fixed-width dtypes are known constants; strings use declared/sampled avg length + per-object overhead). This term absorbs width, table count, and dtype mix — exactly what broke the row-count design.
- **Path multiplier `k_path`** — calibrated once *per execution path*, not per schema: `k = peak_RSS / raw_bytes`, capturing copies, join intermediates, generator temporaries. The dead-linear 4.1 GB/1M measurement is the evidence a constant multiplier is right; the old bug hid both factors inside one row-count constant so schema changes silently invalidated it.

### 3.3 Probe near boundaries, don't over-model

Decision ladder:

1. Static estimate says full_frame fits with wide margin (< ~40% budget) → run full_frame, skip probe.
2. Static estimate says even sequential can't fit → straight to out_of_core, skip probe.
3. Anywhere near a boundary → **micro-probe**: run the actual job at ~1–2% of target rows (e.g. 100k), measure real peak RSS, extrapolate linearly, route on the measured slope.

The probe measures rather than models, with the real schema/dtypes/FK fan-out, at ~1% of runtime cost — it dissolves the "coefficients per schema" problem.

### 3.4 Telemetry feedback loop (self-calibration)

Every completed job logs `(schema fingerprint, predicted peak, actual peak, path)`. Recompute `k_path` from that telemetry. After a few dozen production runs the system is self-calibrated across real schema diversity; nobody hand-tunes a constant again. Platform already persists half the columns (`admission_estimate_mb`, `admission_decision`, `admission_reason` — `api/models.py:288-296`).

### 3.5 Uncertainty → bounded path

Any column the estimator can't price (unbounded free text, plugin generator, exotic dtype) → route to the micro-probe; if the probe result is also suspect (nonlinear generators, data-dependent width) → route to **out_of_core**. The bounded path is the answer to uncertainty; guessing is not.

### 3.6 Bias asymmetrically; delete hard-reject

With out_of_core as a universal fallback, a safe-direction miss is a **downgrade** (same correct output, slower); an unsafe miss is an **OOM kill** (whole job lost, rc137, possible collateral to co-tenants). Wall-clock vs. total loss is not a close call. Run full_frame only when `estimate × (1 + error_band) < budget`, `error_band` starting fat (~30%) and narrowing as telemetry tightens.

**Delete the hard-reject path.** It existed only because there was no genuinely bounded route. Reserve rejection for the one thing out_of_core cannot absorb — **insufficient spill disk** (checkable up front: predicted spill bytes vs. free disk).

### 3.7 Runtime governor (backstop, deliberately dumb)

Keep a watchdog as a safety net, not the router, and do **not** build mid-job checkpointing:

- Sample cgroup `memory.current` every few seconds during full_frame/sequential runs.
- Soft threshold (~85% budget): abort cleanly and **re-run from scratch on out_of_core**. The engine is seeded + deterministic, so a restart with the same seed loses only time.
- Hard threshold (~93%): abort with a clear diagnostic ("peak exceeded estimate by X, rerouted") instead of an opaque kernel rc137.
- Every governor trip is an estimator miss → log into the telemetry loop so the miss class is absorbed into `k_path`.

---

## 4. The "why not always run the bounded path?" question (resolved)

Attractive for the *default*, but "always, only bounded" fails on two hard reasons + one soft:

1. **Not every job is out_of_core-eligible** (hard). The bounded path requires acyclic single-parent FK graph, approved strategy set, no `when` predicate, pure-mask FK shape (`out_of_core/_compat.py`). Cyclic FKs, conditional generation, unsupported strategies physically cannot stream — so a router is still required; "always safe" only changes the default, it does not remove routing.
2. **Speed + disk tax on the common case** (hard-ish). full_frame masked 1M×3 tables in 97s in B1; streaming pays DuckDB setup + spill I/O on every job, and *requires scratch disk always provisioned*. Making the 95% small-job case slower to protect the 5% is a bad trade — unless the ratio turns out small (see §7).
3. **Possible semantic drift** (soft). If streaming is not byte-identical to full-frame, forcing everything through it changes output for jobs that work today. Needs parity verification.

Conclusion: the design already adopts the *right half* of the instinct — **out_of_core is the default answer to uncertainty; full_frame is the opt-in fast path taken only when confidently within budget.** Whether we build the full estimator or ship the minimal "safe-default + opt-in fast flag" depends entirely on the speed ratio (§7).

---

## 5. Blast radius (measured 2026-07-10)

### In-scope engine files (LOC)

| File | LOC | Role in change |
|---|---|---|
| `execution/_pipeline_routing.py` | 507 | routing decision — heavily modified |
| `execution/_planner.py` | 578 | remove row-count constants / memory model |
| `execution/_pipeline_routing_signals.py` | 227 | size signal → byte signal |
| `execution/_pipeline.py` | 592 | wiring |
| `execution/_pipeline_route_exec.py` | 324 | probe + governor dispatch |
| `execution/__init__.py` | 132 | SC5 contract surface |
| `execution/out_of_core/_budget.py` | 165 | cgroup-aware budget |
| `execution/out_of_core/_runner.py` | 491 | (regression surface) |
| `execution/out_of_core/_compat.py` | 472 | eligibility (regression surface) |
| `execution/_sequential.py` | 466 | (regression surface) |
| `scripts/fk_memory_probe.py` | 1113 | bench harness, reused for calibration |

Production surface *modified* (not all rewritten): ~7 files, ~2,600 LOC. **New modules:** byte-estimator + runtime governor (~300–500 LOC).

### Symbol reference counts (src / tests)

| Symbol | src | tests |
|---|---|---|
| `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` | 9 | 4 |
| `FULL_FRAME_REJECT_ROWS_DEFAULT` | 9 | 4 |
| `full_frame_reject_rows` | 13 | 15 |
| `fk_full_frame_oom_risk_rejected` (+`_estimated`) | 5 | 7 |
| `decide_execution_route` | 17 | 8 |
| `check_out_of_core_compatibility` | 16 | 27 |
| `resolve_budget` / `_HOST_RAM_FRACTION` | 9 / 2 | 10 / 0 |

### Test surface

~26 test files exercise routing/admission/reject/out-of-core/budget (~13 K LOC total; regression surface, mostly re-run not rewritten). **18 test functions directly assert the hard-reject behavior and must be rewritten** when it is deleted — concentrated in `test_out_of_core_routing.py` (9), `test_lazy_path_route_admission.py` (6), `test_out_of_core_public_api.py` (2), `test_out_of_core_routing_parity.py` (1).

### Platform side

`api/jobs/admission.py` consolidated to shared multipliers; possibly promoted out of measure-only; telemetry columns ~half-present (`api/models.py:288-296`, migration `n1o2p3q4r5s6`).

### Contract / docs

CHANGELOG, `execution/__init__.py` SC5 docstring, 3 roadmap/plan docs.

---

## 6. Sprint plan

### Sprint 1 (unconditional, ships regardless of track) — ~4–6 d
Pulled out of the gate after Fable review; none of it depends on the speed ratio and it is the cheapest OOM-risk reduction available.
- **1a — Process-model decision (load-bearing prerequisite).** Adopt subprocess-per-job / H7 `worker_mode="external"` as the isolation primitive. The probe (B2), governor (B4), and telemetry (B5) all silently require per-job process isolation the platform default (in-process asyncio, `worker_concurrency` slots) does not provide — `memory_ceiling.py` already documents the gap. Decide + wire before any Track-B sprint.
- **1b — cgroup budget + disk-spill preflight** (was A2). Read cgroup `memory.max` (handle literal `max` / v1≠v2 / nested-min / `swap.max`); budget is the **slot** budget (cgroup minus co-running `WorkerBudget` charges), not the whole cgroup. Replace the row-count reject with a disk preflight. Files: `out_of_core/_budget.py`, `internal/memory.py`, platform `worker_budget.py`.

### Sprint 0 — Decision gate (≈0.5 day, no code)
A **decision function, not a single ratio** (Fable §4/§7 correction): (a) speed ratio at **100k / 1M / 50M** — the small-job ratio is worst and is exactly what Track A's default changes; (b) fraction of jobs that are out_of_core-*ineligible*; (c) **path parity** (byte-identity of out_of_core vs full_frame output) — a blocking input, not a §10 risk; plus the ops call on always-provisioned scratch disk. Small ratio **and** low ineligibility **and** parity holds → Track A; else Track B. The tracks are **not** a hard binary (A ⊂ B) — Track-A safety can ship ahead of a B estimator. Local fallback if GCP stays blocked: 4–6M rows under a cgroup cap on the devbox reproduces the ratio/parity shape.

### Track A — Safe-default + opt-in fast (if speed ratio small, e.g. <1.5×)

| Sprint | Scope | Primary files | Est. |
|---|---|---|---|
| **A1** | Default to bounded path; `full_frame` becomes explicit opt-in "fast" flag; widen out_of_core eligibility where cheap | `_pipeline_routing.py`, `_planner.py`, `_pipeline_routing_signals.py`, `__init__.py` | 2–3 d |
| **A2** | cgroup-aware budget; replace row-count reject with disk-spill preflight | `out_of_core/_budget.py`, `internal/memory.py` | 2–3 d |

**Track A total (draft): ~4–6 engineer-days.** ⚠ **Revised after Fable review to ~8–12 eng-days (+ Sprint 1).** A1 carries the *same* contract migration as Track B — 18 engine reject-test rewrites, platform-admission coordination, SC5 docstring/CHANGELOG, plus a parity gate and the irreducible ineligible+too-big reject class — all omitted in this first cut. Its apparent cheapness is the plan's biggest trap. See §11.

### Track B — Full self-calibrating estimator (if speed ratio large)

Includes A1/A2 groundwork, plus:

| Sprint | Scope | Est. |
|---|---|---|
| **B1** | Consolidate estimator into a shared engine module (single source of truth for multipliers, ported from platform); route on predicted-bytes-vs-cgroup-budget; retire row-count constants | 3–4 d |
| **B2** | Micro-probe: run at ~1% rows, measure peak RSS, extrapolate, route on measured slope; nonlinear-generator guard → bounded | 4–5 d |
| **B3** | Contract migration: deprecate `fk_full_frame_oom_risk_rejected`, rewrite 18 reject tests, update CHANGELOG + SC5 docstring, coordinate platform admission | 2–3 d |
| **B4** | Runtime governor: RSS watchdog, soft→clean abort + deterministic seeded restart, hard→diagnostic-not-rc137 | 3–4 d |
| **B5** | Telemetry loop: log `(schema fingerprint, predicted, actual, path)`, recompute `k_path` (platform columns ~half-present) | 2–3 d |
| **B6** | Calibrate against real 50M/100M data + full parity/perf gate + dennis | ~2 d |

**Track B total (draft): ~18–25 engineer-days.** ⚠ **Revised after Fable review to ~24–34 eng-days.** B2 grows subprocess-probe plumbing + the two-point method; B4 grows a supervisor-kill redesign (partially infeasible as first drafted); B5 gains a per-job peak-attribution prerequisite (Sprint 1a). **Riskiest sprint: B4** (feasibility); **biggest correctness risk: B2** (probe method). Contract migration (B3) is *triggered by B1*, not sequenced after it — run concurrently or keep a legacy reject shim. See §11.

---

## 7. Gate condition (explicit)

**Do not begin implementation until Sprint 0 outputs the speed ratio.**
- ratio small → Track A, stop after A2.
- ratio large → Track B.

The number comes free from the in-flight 50M run (B1 full_frame vs B2/B5 out_of_core) plus the 100M follow-up. This spec is written to be actionable the moment that number lands.

---

## 8. Open architecture question (decide before Track B, B1)

Where does the estimator live?
- **(A) Engine-owned** — self-contained estimator in `execution/`, correct even when called directly (CLI/tests); platform becomes a thin caller. Matches Fable's implicit model.
- **(B) Platform-owned** — platform admission stays the brain (it is the more mature implementation today) and is wired to tell the engine which path; engine keeps a minimal internal guard.

Recommendation: **engine-owned core estimator + shared multiplier constants**, so direct-invocation paths are safe, with platform admission as the richer outer gate consuming the same constants. Confirm before B1.

---

## 9. Acceptance criteria

- Router decision keys off computed bytes vs. cgroup budget, not fixed row counts. Verified by a test that changes table *width* (not row count) and shows the route changes correctly.
- A schema unlike the calibration shape (different width / table count) routes without OOM on a real run.
- full_frame chosen only when `estimate × (1 + error_band) < budget`; asymmetric margin verified.
- (Track B) Micro-probe extrapolation within X% of measured peak on the bench schema.
- (Track B) Governor trip → deterministic seeded restart on out_of_core producing byte-identical output.
- Hard-reject path removed or deprecated with a migration note; 18 reject tests rewritten; SC5 contract surface updated; platform admission consistent.
- No cgroup-limited run OOMs across the parity/perf suite.

---

## 10. Risks

- **Contract break** (reject-path deletion) ripples to platform + docs — mitigated by deprecation window in B3.
- **Estimator wrong on unseen schema** — mitigated by probe + governor net; unknown → bounded.
- **Governor threading/flush races** (B4 is the fiddliest) — clean-abort + seeded-restart must be deterministic; heavy test focus.
- **Two-brain divergence** if engine and platform estimators are not unified on shared constants — B1 must make one source of truth.
- **Semantic drift** if out_of_core is not byte-identical to full_frame — parity suite is the guard.
- **Telemetry garbage-in** — recomputing `k_path` from process-wide contaminated peaks (no per-job attribution at concurrency>1) silently inflates `k` upward. Mitigated only by Sprint 1a process isolation.
- **Gate starvation** — Sprint 0's measurement is blocked on GCP auth/spend + an unmerged out-of-core branch; without the devbox local fallback the whole program has no start date.

---

## 11. Revision 1 — Fable adversarial review corrections (2026-07-10)

Fable reviewed the draft against both repos. Headline: the spec undercounted the memory-management surface — there are **four "brains," not two** — and the probe/governor/telemetry sprints silently assume per-job process isolation the platform default does not provide. Corrections keyed to section (headline estimates already fixed inline in §6):

**§2 — more than two brains.** Beyond engine routing + `admission.py`, the platform already ships: `api/jobs/worker_budget.py` (`WorkerBudget` charges each job its `admission_estimate_mb` at claim time; unsizeable ⇒ full budget ⇒ runs alone — already the §3.5 "uncertainty→safe" pattern); `api/jobs/memory_ceiling.py` (`peak_rss_mb()` via `ru_maxrss`, documented as a process-wide HWM contaminated across jobs/slots; true per-job delta is open work); `queue_worker.py` runs jobs as in-process asyncio tasks at `worker_concurrency` slots (H7 added `worker_mode="external"` per-job-process). Consolidation (§8/B1) must **absorb** these, not invent a parallel budget — else two brains becomes three.

**§3.1 (budget)** — `memory.max` can be literal `max`; cgroup v1≠v2; nested cgroups take the min up the hierarchy; `memory.swap.max` moves the real kill point. Budget must be the **slot** budget (cgroup minus co-running `WorkerBudget` charges) when concurrency>1.

**§3.2 (estimate)** — (a) sequential is O(cardinality); `k_seq × raw_bytes` mis-models it structurally — it needs a cardinality term or its own estimator. `k_seq` also shifts ~25% when PR #22 (`run_sequential` rework) merges — calibrate after. (b) Generation jobs have **no input to sample**: string widths must come from per-provider strategy metadata (hidden B1 work); only masking jobs can sample, so "skip probe on wide margin" can't be trusted for string-heavy generated schemas until that table exists. (c) object-dtype overhead (~50–60 B/string) must be explicit in the raw-bytes term.

**§3.3 (probe) — two technical errors as first written.** (a) Single-point extrapolation conflates slope and intercept: with B1's ~0.3 GB intercept, a 100k probe reads ~7.1 GB/M vs the true 4.08 — a ~73% overestimate that evicts most mid-size jobs from the fast path. Use a **two-point probe** (100k + 200k, route on the slope) or explicit baseline subtraction. (b) `ru_maxrss`/VmHWM is process-lifetime monotonic — a probe inside a warm worker reads the *historical* high-water mark, not the probe. Must run in a **fresh subprocess** (interacts with the Sprint 1a worker-mode decision). Also: the "~1% runtime" claim ignores fixed setup (interpreter/import/Faker/DuckDB init) which dominates at 100k; and name the scaling rule (all tables by the same fraction to preserve FK fan-out) + the uniqueness-saturation blind spot (unique columns saturate nonlinearly near cardinality at full scale — invisible to a small probe).

**§3.4 / B5 (telemetry)** — per-job actual peak **does not exist** at `worker_concurrency>1` or after any prior job in the same process; recomputing `k` from contaminated HWMs drifts it upward monotonically. B5 hard-depends on Sprint 1a (process isolation). Cold-start: pin initial `k_full_frame` (~10–11× raw for the calibration schema) and the initial `error_band` **inside B1**, and define the recalibration trigger — otherwise B1 ships routing on an unstated constant.

**§3.6 (delete reject) — one class is irreducible.** A job that is out_of_core-*ineligible* (cyclic FK, unsupported strategy, `when` predicate) **and** too big for sequential/full_frame has **no bounded route** → it must remain a reject (now byte-based, not row-based). As first written, Track A deletes the reject and routes those jobs into a guaranteed OOM. Also flag the **double margin**: budget = 0.75×limit *and* estimate×1.3 < budget ⇒ full_frame only under ~58% of the cgroup limit — on the calibration schema that shrinks the fast path from <7.5M to ~4.5M rows, a day-one perf regression on 5–6M jobs that measurably fit (24.8/32 GB). State it as intended + name the telemetry-narrowing recovery.

**§3.7 (governor) — infeasible as first specified in the default process model.** A watchdog thread cannot cleanly abort a native pandas/pyarrow op mid-allocation (Python signals/flags aren't checked until the native call returns; multi-GB step allocations blow through the 85%→93% window between samples). It only works when the job runs in a **child process a supervisor can SIGKILL** (H7 external mode) — redesign B4 as supervisor-kills-child or honestly descope to best-effort. Define the reroute ladder (out_of_core → sequential → fail-with-diagnostic; the target may itself be ineligible). Own **abort cleanup** (partial outputs, DuckDB spill/temp dirs, job-status/target-lock state, idempotent seeded re-run). Use anon memory (`memory.stat`, or `current − inactive_file`), not `memory.current` (page cache false-trips during spill/Parquet I/O).

**§5 (blast radius)** — add to the platform column: `worker_budget.py`, `memory_ceiling.py`, `queue_worker.py` (slot charging), the H5 streaming route (`v2_runner.py`/`streams.py`), `target_locks.py` (abort/rerun interaction). The 18 reject tests are **engine-only** — count the platform SC5/admission tests before sizing B3/A1.

**§6 (sequencing)** — contract migration fires at **B1/A1** (retiring the constants breaks the 18 tests + SC5 consumers immediately), not B3 → run B3 concurrent with B1 or keep a legacy reject shim behind a deprecation flag. Missing sprints: (1) abort-path cleanup + idempotent seeded restart; (2) an adversarial schema matrix (width × table-count × dtype grid under a cgroup cap) — §9 asserts it as acceptance but no sprint builds it.

**§9 (acceptance)** — define X ("within X%" → suggest ±15% with the two-point probe, else untestable); add a regression test that the probe measures its *own* peak in a warm worker (HWM-contamination guard); disambiguate "byte-identical **to what**" (from-scratch out_of_core = trivially true/weak; vs the aborted full_frame's would-be output = requires parity, an open question per §4.3); add checks for the ineligible-job fallback ladder and zero partial outputs/temp files after abort.

### Top 3 to fix before implementation-ready
1. **Decide the process model first (Sprint 1a).** The governor's clean abort (B4), the probe's peak measurement (B2), and telemetry's per-job actual peak (B5) all silently assume per-job process isolation the platform default (in-process asyncio) does not provide and `memory_ceiling.py` already documents as an open gap. Make subprocess-per-job / H7 external worker an explicit prerequisite, or redesign those three sprints around its absence.
2. **Fix the probe method (§3.3):** two-point / baseline-subtracted extrapolation (single-point misreads slope ~70% at 100k against our own B1 data), run in a fresh subprocess so it doesn't read a warm worker's historical peak.
3. **Re-scope the reject deletion + sequencing:** the contract migration fires at B1/A1 (not B3); Track A needs the same test/contract/parity work as Track B and is ~2× under-budgeted; ineligible+too-big remains an irreducible reject class §3.6 currently routes into a guaranteed OOM; and ship Sprint 1 (cgroup budget + disk preflight, process model) unconditionally *first*.

---

## 12. Sprint 1a decision — RATIFIED (2026-07-10)

**Decision (per the Sprint 1a research memo):** Option **(b)** — a subprocess-per-job execution wrapper in the engine — running inside Option **(a)** as its deployment shell. Port `scripts/fk_memory_probe.py`'s already-proven isolation primitive (fresh `execve` per job; peak read from `/proc/self/status` **VmHWM**, not `ru_maxrss`; `resource.setrlimit` cap applied in-child *before* workload allocation; self-classifying exit-code/stderr-marker OOM detection) from benchmark harness into a production execution primitive `run_pipeline_isolated(...)`. The platform worker (inline or external) invokes it instead of calling `run_pipeline` in-process, behind a rollback flag `isolated_execution_enabled` (mirroring `admission_control_enabled`).

**Why:** it is the only option that delivers all three things B2/B4/B5 require — a clean per-job peak, an externally SIGKILL-able unit, and fresh-`execve` HWM semantics — and it is largely extraction of ~1,100 LOC of already-working probe code, not new design. H7 `worker_mode="external"` alone isolates per-*worker*, not per-*job* (contamination reappears at `worker_concurrency>1` and across a worker's sequential jobs), so it is the shell, not the mechanism. Option (c) in-process concedes §9's acceptance criteria are unreachable and lets B5 telemetry corrupt its own `k_path`.

**Orchestrator rulings on the memo's open questions:**
1. **Concurrency:** pin `worker_concurrency=1` per external worker for Sprint 1a; subprocess-per-slot at `>1` is explicit future work, not this sprint.
2. **SQLite/inline:** the wrapper **degrades gracefully** — SQLite/inline deployments fall back to in-process execution, explicitly labeled "no governor/telemetry (option c)". Isolation is NOT a hard Postgres requirement; dev/local keep working.
3. **Artifact-write on kill:** child writes to a **staging** location; the parent commits to the final target only on clean child exit; a SIGKILL discards staging → no partial output lands at the target. Confirm `TransactionalSink` survives SIGKILL specifically; full abort-cleanup ownership is B4.
4. **WorkerBudget:** stays a pre-spawn gate for Sprint 1a; a child-reported "actual RSS, still running" update path is deferred to B5.
5. **Gate exemption:** confirmed — Sprint 1 (1a+1b) is exempt from Sprint 0's speed-ratio gate (§6 frames it unconditional). It does not wait on the benchmark.

**Sprint 1a acceptance additions:** the isolation path actually **runs** with `isolated_execution_enabled=True` in at least one lane (CI or a deployment target), producing a clean per-job VmHWM — not merely compiles. `_CAPPED_ENV` (`MALLOC_ARENA_MAX` / `ARROW_DEFAULT_MEMORY_POOL`) must be set **driver-side** via the subprocess `env=`, before the child's memory-pool init.

**Scope split:** Sprint 1a-part-1 (first build) = the engine `run_pipeline_isolated` primitive + worker entrypoint + result contract + unit tests. Sprint 1a-part-2 = platform wiring (`queue_worker` → wrapper behind the flag).

**Program sequencing (per Cam, 2026-07-10):** OOM redesign (this spec) → **Test-Flight Golden-Gate Hardening TH-1..TH-4** (`docs/plans/2026-07-10-testflight-golden-gate-hardening.md`, already authored) → revisit a trimmed 50M/100M run (validates auto-routing + yields the Sprint 0 speed ratio). The 50M revisit is also TH's out-of-scope item #2.

---

## 13. B1a calibration finding — the static estimator is a CONSERVATIVE FILTER, not a precise predictor (2026-07-11)

dennis + Codex (empirical RSS measurement) disproved the §3.2 assumption that `k_path` is schema-invariant, and showed the naive calibration errs in the OOM direction:

- The B1 calibration fixture draws most columns from a **shared string pool**, so `raw_data_bytes` (pricing each string cell at width+57) over-prices pooled cells ~8.5×. The fixture's raw_bytes is a ~5× inflated proxy → the measured `k=1.156` is an artifact. Applying 1.156 to a lean schema (all-numeric, or unique-string) **under-predicts → routes full_frame → OOM.**
- Measured true peak/raw: pooled-string ~0.12, unique-string ~1.4, numeric ~2–3× (reasoned).

**Ruling (refines §3.2 / §3.3):** the static estimator is a **coarse, conservative first-pass filter** — it must only ever OVER-predict full_frame peak, never under. Near-boundary accuracy comes from the probe (B2) + telemetry (B5). Concretely:
- Operational `K_FULL_FRAME_COLD_START = 3.0` (covers the numeric worst case; over-prices pooled-string schemas, which is the safe side — the probe recovers their fast path). The measured 1.156 is kept as evidence only.
- Sequential working-set = **sum of the two largest tables** (tighter single-table bound gated on PR #22).
- `K_OUT_OF_CORE` / `K_SEQUENTIAL` cold-starts are **unmeasured placeholders** — B1b must NOT route on them before Sprint 0 / B5.
- **B1b routing rule:** full_frame only when the conservative estimate clears the budget with margin; otherwise route bounded. The probe (B2) is a fast-path RECOVERY, not a safety requirement — so **B1b is safe to ship before B2** (it over-downgrades near-boundary jobs to bounded until B2 recovers them).

**Process note:** the CI `mypy` gate was not runnable in the venv during the 1a/1b/B1a reviews (mypy uninstalled), so a `Literal`-type error slipped into `_isolated_run.py` (Sprint 1a remediation) and was only caught at B1a re-verify. Fixed. Sprint self-checks now run mypy via `uv run --with mypy==2.1.0 -- mypy src/decoy_engine testflight`.
