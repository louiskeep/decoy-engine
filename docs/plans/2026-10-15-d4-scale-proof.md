Status: plan (Phase D, D4: external-reorder revalidate + 100M/200M scale proof. rev 2 (Codex plan-gate rev1 REVISE folded: TOTAL-rows-not-per-table + corrected ETA/matrix, corrected GAP premise, cap-semantics, D4a spill evidence, worker supervisor). GCP spend APPROVED by Cam 2026-10-15 for Phase D; still FLAG the run matrix + cost to Cam on Slack BEFORE each GCP run batch, and post results after. The bench-worker CODE goes through the normal gate chain; the RUNS are evidence, not a code merge.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md`; benchmark memory (`benchmark-pandas-path-minimal`, `watch-memory-long-agents`, `decoy-gcp-bench-budget`). Base: engine `origin/main` @ `1b2628e6`.

# D4: external-reorder revalidate + 100M/200M FK scale proof

## 0. Why + the bar

The gap-triage converges on D4 as the gating measurement: it settles the GAP6 routing cutoff, the GAP1 recoverable fraction, and the GAP9 thread/memory policy, and feeds Phase E. D4 = **D4a** (revalidate the external-reorder lane holds at 100M+ vs the R3 liveness record), **D4b** (implement/fix ONLY if D4a finds a gap), **D4c** (100M PLUS 200M parent+child-growth proof, spill + peak memory). Program plan `docs/plans/2026-10-10-phase-d-fk-routes-program.md:90,99,112`.

Bars (D4 must DECLARE the 100M/200M ones; none are written):
- **10M x ~15 cols: sub-600s (north-star) + peak-RSS ceiling** -- re-confirm on the native run_pipeline route (the audit had it ~4-7% under 600s single-threaded; the Rust D1/D2 kernels should widen the margin).
- **100M: COMPLETES within a declared peak-RSS ceiling (reference 6.5GiB = 6656MiB on n2-standard-8) + FK parity on every edge; bounded spill.** NOT sub-600s -- the OOC route is ~20k rows/s single-threaded, so 100M is ~78 min; D4c is a MEMORY/spill proof, not a time proof.
- **200M (parent + child growth): COMPLETES within a declared ceiling + FK parity + bounded spill.** Historical batch-route floor was 8-16GB (200M@8GB fails safe, @16GB completes, RSS ~6.95GB, 22GB spill, ~2.7h). D4 declares the ceiling; the reorder lane's 100M+ behavior is UNPROVEN end-to-end (the real risk).

Perf = isolated-process p50/p95 + a declared absolute memory ceiling (the D9 ratio gate is retired). This is a measurement + bench-infra slice; the engine reorder lane is already built (P4-A #126, live on main) -- D4a revalidates it rather than building it, unless D4a finds a gap (then D4b).

## 1. What exists + the gap

- **Reorder lane (built, live on main):** `run_pipeline` -> `run_out_of_core_route` (`_pipeline_route_exec.py:408`) -> `run_fk_out_of_core` (`_runner.py:114`) -> `decide_route` (`_route_policy.py`). Reorder (`_stream_driver.stream_table` + `BoundedExternalSorter`) fires only when: a sink is present, >=1 incoming edge, a memory + disk budget resolve, largest dedup-null-filtered parent-key count >= `REORDER_PARENT_KEY_THRESHOLD = 2_000_000` (`_route_policy.py:49`), fan-in <= 2x merge_fan_in, payload width under the per-head cap. Else the `ChildFkBatchJoiner` (DuckDB) batch route. R3 liveness record: `docs/records/2026-10-10-ooc-reorder-liveness.md` ("the lane is live", proven bounded only at small/moderate fixtures @ 1GiB, NOT end-to-end 100M, and not against a real object-store sink).
- **Existing bench infra:** `scripts/fk_memory_probe.py` (3-table chain, `--mode full|sequential|out_of_core`, `--rows/--sweep`, `--width`, `--strategy hash|fpe|faker|categorical` on payload, `--orphan-*`, `--mem-cap-mb`, `--json`; emits `peak_rss_mb`(VmHWM)/`mask_s`/`temp_disk_peak_mb`/`parity`/`completed`). Per-slice kernel benches `tests/perf/_d1a_hash_bench.py` etc. Platform `scripts/gcp-bench/engine-bench.sh` (batteries `fk`/`throughput`/`unified-cert`; git-bundle ship, detached, 8h cap, teardown; `--confirm-cost` mandatory; results -> `docs/product/release-1-validation-runs/`).
- **THE GAP (corrected, plan-gate MED):** `fk_memory_probe.py`'s direct `run_fk_out_of_core` call ALREADY constructs `OocHashRun` and passes it to both drivers (`_runner.py:216`), so it does NOT inherently bypass the Phase-D native payload kernels. The real gaps are: (a) it bypasses `run_pipeline`, so production ROUTING (GAP6) + the pipeline budget plumbing are untested; and (b) `fk_memory_probe.py:648` passes NEITHER `budget_bytes` NOR `temp_disk_budget_bytes`, so the reorder lane is DISABLED BY ADMISSION (not merely unwitnessed). So D4 adds a `run_pipeline` worker to exercise production routing + budgets, and the evidence contract must require: NATIVE per-column DISPOSITION evidence (a successful companion IMPORT alone does not prove native execution), a driver/reorder-TAKEN witness, AND -- for any GAP6 routing conclusion -- an AUTO-route observation (forced `execution_mode=out_of_core` proves only the forced path).

## 2. D4a -- revalidate the reorder lane (do first, cheap, no 100M spend)

- Read `decide_route` + the R3 liveness record; confirm the reorder admission conditions (2M-key threshold, temp-disk budget 0.9x free, fan-in/width caps) ADMIT the 100M/200M shapes (large parent-key count -> reorder, not batch). Record the expected driver per shape.
- Add a moderate-scale witness (local or a cheap VM smoke, NOT the 100M run): drive `run_pipeline` on an FK chain large enough to cross the 2M parent-key threshold (e.g. 3M parent rows) with a resolvable mem+disk budget, and assert via a route/driver spy (model on `tests/unit/.../test_r3_ooc_reorder_reachability.py`) that `stream_table` (reorder) is taken, that the sorter actually SPILLS + MERGES (not merely that `BoundedExternalSorter` was constructed -- construction alone is not evidence), and that peak memory stays MEASURED-bounded within its budget. This confirms the 100M run will exercise a working, bounded reorder lane, not silently fall to the batch joiner.
- If D4a shows the reorder lane is NOT reached or NOT bounded at that scale -> STOP; that is D4b (implement/fix), a separate plan + gate. Do NOT run the 100M GCP spend until D4a is green.

## 3. Bench workers to build (the code deliverable; gated normally)

1. **FK `run_pipeline` OOC bench worker** (`scripts/fk-scale/bench_worker_fk.py`, new): runs `run_pipeline` (auto route, or `execution_mode="out_of_core"` to force) on the native companion over a disk-backed FK chain; emits a `BENCH_JSON` line with: wall `mask_s` (p50/p95 over repeats where feasible), peak RSS measured EXTERNALLY (`/usr/bin/time -v` or `wait4`, NOT in-process VmHWM alone), temp-disk/spill peak, the chosen `execution_mode` (AND whether AUTO or forced) + `route_reason` + a REORDER-TAKEN witness (which driver ran), native per-column DISPOSITION (which columns ran the compiled kernel vs Python), FK parity on every edge, and `completed`. A SUPERVISING process (not the worker itself) emits the record so a killed / OOM / timed-out worker still produces one: `completed:true` requires the run RETURNED, the committed output row-counts MATCH the inputs, and bounded FK-parity validation PASSED; a signal kill, a timeout, or missing/malformed worker output each emit `completed:false` (rc=0 is NOT success). Run each size in a fresh subprocess.
2. **100M/200M fixture builder** (`scripts/fk-scale/build_fk_chain.py`, new): writes a 3-table FK chain to disk-backed Parquet CHUNK BY CHUNK at the declared per-table counts (§4; TOTAL across the chain, not per-table; never materialize a tier in memory), model on the probe's `write_large_fk_chain`; supports parent/child row ASYMMETRY (the plan's "parent + child growth"), `--width` (~15 cols), orphan fraction, and the hash key strategy. Deterministic seed for reproducibility.
3. **engine-bench battery `fk-scale`** (platform `scripts/gcp-bench/engine-bench.sh` + `remote-engine-bench.sh`): builds the native companion + records `host.json` provenance (copy the `unified-cert` block), builds the fixture on the node, runs the FK worker at the requested tiers + memory caps, syncs `remote-results/summary.json`. VERIFY the local `var/gcp-bench/config.env` zone/tag BEFORE a run (tag-label gotcha: `DECOY_BENCH_TAG`; -b zone exhausted -> use -c). No AR grant needed (git bundle + uv, no docker image).

## 4. D4c run matrix + cost (Slack to Cam BEFORE spending)

**Row counts are TOTAL across the 3-table chain, NOT per-table (plan-gate HIGH).** The historical ~80-min 100M result was 100M TOTAL (~33M/table); 100M/table would be 300M total (~4h20m) and 200M/table 600M total (~8.3h) -- over the 8h VM cap. Declare exact per-table counts summing to the tier total, with parent+child growth:
- **10M tier:** parent 2M (clears the 2M reorder threshold), child 3.5M, grand 4.5M (~10M total).
- **100M tier:** parent 20M, child 35M, grand 45M (~100M total); ~80 min mask at ~20k rows/s.
- **200M tier:** parent 40M, child 70M, grand 90M (~200M total); ~2.7 h mask.

**Cap semantics (plan-gate MED):** "16GB/8GB cap" = the GCE instance RAM. Three DISTINCT things, all recorded + IDENTICAL across an A/B pair: (1) the engine OOC memory BUDGET (resolved bytes, passed to `run_pipeline`), (2) peak RSS measured EXTERNALLY by the supervisor, (3) the declared RSS acceptance CEILING (the pass bar, §5). An `RLIMIT_AS` address-space limit is NOT RSS; do not conflate. Also record batch size + thread count.
**Reorder A/B (plan-gate MED):** ON = the default 2M parent-key threshold (reorder fires); OFF = a threshold ABOVE the max parent cardinality (forces the batch/DuckDB joiner). The A/B is TWO runs.

Budget (~5 runs; `--confirm-cost` mandatory; est. $3-15/run by wall):
- **Run 0 -- 10M smoke (cheap VM):** worker + fixture + companion build + the native-disposition + AUTO-route + reorder-taken + spill witnesses; captures the 10M sub-600s north-star re-confirm. (1)
- **Runs 1-2 -- 100M, reorder A/B, 16GB:** highest value (does the reorder lane hold + beat/bound the batch joiner at 100M, AUTO-routed). (2)
- **Run 3 -- 200M, 16GB:** the scale-ceiling proof. (1)
- **Run 4 (optional) -- 200M @ 8GB:** the historical fail-safe floor; does reorder lower it? (1)
That is the full ~5-run budget; strategy-mix variants + repeats are a follow-on. One 100M A/B informs the GAP6 cutoff + GAP9 thread policy but does not alone establish a crossover curve.
Each run: Slack the exact per-table counts + cap + enforcement (budget/RSS-ceiling/threads) + est. cost + ETA BEFORE launch; Slack `summary.json` (completed, peak RSS, spill, wall, parity, reorder-taken, disposition) after.

## 5. Declared bars (confirm in the pre-run Slack)

- 10M: `mask_s` < 600s AND peak RSS < the reference ceiling, FK parity OK.
- 100M: `completed:true`, FK parity OK on every edge, peak RSS <= 6656 MiB (reference-host declaration), spill within the resolved disk budget. (No sub-600s requirement; record the wall for the record.)
- 200M: `completed:true`, FK parity OK, peak RSS <= a DECLARED ceiling (propose <= 7 GiB on the 16GB cap, mirroring the batch-route ~6.95GB floor; confirm with Cam), bounded spill. A fail-safe (clean abort within the cap, no OOM-kill) at a lower cap is an acceptable "floor" datum, not a pass.
- The reorder-on-vs-off A/B at 100M records the cutoff that GAP6/GAP9 need.

## 6. Acceptance (bench-worker CODE; the runs are evidence)

- **D4a:** the reorder-reachability witness passes at moderate scale (reorder taken + bounded); recorded.
- **Worker/fixture correctness (small-scale, local):** the FK worker emits valid `BENCH_JSON` with the route + reorder-taken witness + external RSS + parity + `completed`; a forced OOM sets `completed:false` (not a silent rc=0 pass); the fixture builder produces a correct FK chain (parity holds) at a small N with parent/child asymmetry; the battery wiring dry-runs (no spend) with `--help`/a tiny local tier. Tests under `tests/perf/` or `scripts/fk-scale/` smoke tests.
- **The 100M/200M runs are EVIDENCE**, not a code gate: they are recorded in `docs/product/release-1-validation-runs/` + the roadmap + Slack. A run that fails the declared bar is a finding (feeds D4b / GAP policy), not a test failure.

## 7. Verification + gates

- The bench-worker + fixture + battery CODE goes through the normal chain: Opus VERIFY -> dennis -> Codex final (code review of the workers + D4a witness; no masking/parity regression; log hygiene; module sizes). This gate is LOCAL + cheap (no GCP).
- The GCP RUNS are R3 spend: Cam pre-approved for Phase D, but STILL Slack the matrix + cost BEFORE each batch and results after (standing costly-action rule). Flag before merge-to-main of the bench code too.
- `~/bin/pytest-one`; abi-3 companion for any companion-touching smoke; ci-mirror otherwise. `PYTHONPATH=<worktree>/src:<worktree>`.

## 8. Done when

- [ ] D4a reorder-reachability revalidated green (reorder taken + bounded at moderate scale); if a gap -> D4b filed, STOP before 100M spend.
- [ ] Bench worker + fixture builder + `fk-scale` battery built, gated (dennis + Codex), merged (Cam-notified; CI green).
- [ ] D4c runs executed within budget (Slack before each + results after): 10M north-star re-confirm; 100M (+ reorder A/B); 200M; recorded in `release-1-validation-runs/` + roadmap with declared bars pass/fail.
- [ ] ROADMAP + RECENTLY-SHIPPED updated (D4 closed; GAP6 cutoff / GAP1 / GAP9 numbers recorded); engine graphify refresh (note if deferred). Results fed to Phase E.

cam
