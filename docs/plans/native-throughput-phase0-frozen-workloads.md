Status: draft for Cam approval

# Phase 0 Task 0.1: Frozen reference workloads, host, and targets

This freezes the benchmarks, host, and acceptance targets for the Execution
Consolidation and Native Throughput program (`docs/plans/2026-09-09-execution-consolidation-and-native-throughput.md`)
before any code is written. It reuses the workload and harness already proven in
the Phase 2 native certification rather than inventing new ones. The exit gate is
Cam's approval of the eight items in §7 (which are the plan's §15 decisions).

## 1. Reference workloads

### 1.1 W2 keyed-hash workload (the throughput target workload)
Reused verbatim from `PHASE2-BASELINE.md` and the committed harness. One mask
table, 10 columns, fixed 32-byte `mask_key`, fixed seed `20260828`,
`post_validation=False`:

- 3 keyed-`hash` (dominant cost): `h_email` (utf8), `h_token` (utf8), `h_uid`
  (int64), each with its own namespace.
- 3 `passthrough`: `pt_amount` (int64), `pt_flag` (bool), `pt_ts` (timestamp-tz, us, UTC).
- 2 `redact`: `rd_ssn`, `rd_notes` (utf8).
- 2 `truncate`: `tr_phone` (len 3, head), `tr_card` (len 4, tail) (utf8).

Harness (committed, reused unchanged): `scripts/native-baseline/build_w2_parquet.py`
(out-of-band generation, 50k rows/group), `bench_worker_native.py` (native route,
lazy `ParquetFile.iter_batches`), `bench_worker.py` (pandas oracle), `bench_driver.py`
(fresh process per rep, external `VmHWM` sampling).

### 1.2 Deterministic Faker workload (the Phase 2 target workload)
One mask table with deterministic-`faker` columns in the C1 reuse-only scope (a
registry-backed value pool, bounded `pool_size`, deterministic per-row selection
via `derive_index`). To be pinned exactly in Task 0.2 from the existing Phase 3
C1 faker fixture (`tests/parity/native/` C1 harness): column set, `pool_size`,
seed, mask-key shape, null fraction, and the admitted cardinality mode. Recorded
here as a required Task 0.2 artifact; its numeric target is set at the Task 2.4 gate.

### 1.3 Row tiers, batch size, sink
- Tiers: 1M, 4M, 16M, 100M (the 100M tier is the product's conservative cap and
  the 600s-target tier). Phase 1 Task 1.6 additionally sweeps native thread counts
  1/2/4/8 at the 100M tier.
- Batch size: 50,000 rows per group (matches the certified harness).
- Sink: transactional Parquet, output batches dropped after row-count (never
  accumulating the full output), per the certified worker.

## 2. Reference host  (DECISION NEEDED, see §7.1)

The plan requires an eight-core reference host and forbids treating a different
host as equivalent evidence (§4.1). This devbox is **4-core / 12 GiB**, so it
cannot be the reference host. The existing 100M native cert (1,563.39s) is a
**4-core, single-threaded** number and is NOT a valid baseline for an 8-thread
600s target.

Recommendation: **GCP `n2-standard-8` (8 vCPU / 32 GiB / local or pd-ssd scratch)**,
the node the bench harness (`decoy-platform/scripts/gcp-bench`) already provisions.
Rationale: it is 8-core (matches the plan's Rayon thread sweep), has ample RAM and
disk for spill, is reproducible from a clean image, and is already wired into the
authorized bench budget. The authoritative Task 0.2 baseline runs on this host.

## 3. Targets  (DECISION NEEDED, see §7)

- Throughput: frozen 100M W2 workload in **<= 600 s** on the approved 8-core host
  at the approved batch size and 8 native threads. (Implies the hash kernel must
  drop from ~1,219s to ~256s, ~4.8x, holding other measured work ~344s constant,
  to be re-measured on the reference host in Task 0.2, not carried from the devbox.)
- Peak RSS: **<= 6.5 GiB** and flat (native peak RSS at 4x/16x/100x <= 1.5x its 1x
  value). The certified native route used only 398MB at 100M, so 6.5 GiB is a loose
  ceiling with wide headroom; kept as-is unless Cam wants it tightened.
- Non-regression floor: native wall <= oracle wall at every tier.
- Per-batch Rust transient scratch <= 2x the input batch's Arrow byte size.

## 4. Method (frozen, reused)
1 discarded warmup + timed reps in fresh processes per tier; wall reported as
median / IQR / p95-of-reps; peak RSS from an external `VmHWM` sampler polling
`/proc/<pid>/status` (never read inside the measured process); keyed-hash
throughput as per-hash-column rows/sec from in-process strategy timing. For the
100M tier, 1 rep with 0 warmup is the honest cold-read figure (a real deployment
reads the file once); lower tiers carry >=3 reps for variance.

## 5. Correctness reference (frozen)
The pinned pandas full-frame path is the oracle. Byte/logical parity is asserted
against it at every tier via the existing gate fixtures
(`tests/parity/native/test_phase2_gate.py`, `test_e2e_certification.py`). No frozen
target may be weakened to make a result pass (plan §6.7).

## 6. Reproducibility
Another worker can reproduce every baseline from a clean checkout: the workload
definition (§1), the committed harness (§1.1), the host image (§2), the method
(§4), the seed/mask-key (§1.1), and the dependency lock (`uv.lock`). Task 0.2
records the exact image, CPU model, kernel, and lock hash alongside the numbers.

## 7. Approval decisions (Phase 0 exit gate; the plan's §15)
Status as of Cam review 2026-09-09:

1. Reference host: **GCP n2-standard-8** (8 vCPU / 32 GiB). **APPROVED (Cam).** More
   cores would be faster (the crypto is embarrassingly parallel); 8-core is chosen
   as a conservative floor so the "10 min" claim over-delivers on larger hardware.
2. Throughput target: **600 s at 100M** on that host. **APPROVED (Cam).**
3. Peak RSS + spill limits: **<= 6.5 GiB, flat.** **APPROVED as recommended (Cam).**
4. Deterministic Faker workload (§1.2 C1 reuse-only scope) + Task 2.4 target.
   **APPROVED as recommended (Cam).**
5. OS/arch matrix. **APPROVED (Cam).** Resolution: (a) PLATFORM Docker image is one
   controlled Linux container (x86-64 primary; ARM64 later if ARM-server support is
   wanted) with the native companion BUNDLED and REQUIRED (worker refuses to start
   if absent, no silent slow mode). (b) CLI runs on ANY Python platform via the
   fail-closed pure-Python fallback; prebuilt native wheels ship for a four-target
   starter pack for the compiled speedup: **Linux x86-64, Windows x86-64, macOS
   arm64, Linux ARM64** (Windows included: CLI users run Windows laptops). Intel Mac
   and others fall back to Python or source-build. Exact wheel list locked at Task 3.1.
6. Large-job policy for arbitrary Python providers. **APPROVED (Cam): Codex's
   hybrid.** Classify a job as large when work reaching UNPROVEN Python operators
   exceeds 100,000 rows OR 64 MiB decoded input (summed across such nodes; unknown
   metadata = large). Bounded/streaming request: reject every unproven Python
   operator regardless of size. Below both caps: permit the pandas oracle only if
   the non-callable working set is <= 50% of the job memory budget, behind an
   explicit `allow_unbounded_python_small_jobs` deployment setting, reported
   `memory_assurance: not_guaranteed`. Above either cap: reject before staging with
   code `unprovable_python_large_job`. NO measure-then-admit, NO `bounded:true`
   trust flag. Future escape hatch = a real `StreamingProviderAdapter` contract
   (fixed schemas, max output bytes/batch, declared state, batch-invariance tests),
   labeled `trusted_bounded` until an OS memory-limited worker exists. Matches the
   engine's existing `pool_native|python_only|reject_large` model. Consult:
   scratchpad/largejob-policy-consult.out.
7. Freeze new Polars work during consolidation, with a keep/deprecate/REMOVE
   decision made on merit at Phase 6 (Task 6.1). **APPROVED (Cam):** freeze now,
   likely remove later. Rationale: Polars has no primitive for our keyed crypto (the
   bottleneck), so it cannot vectorize it, only relocate the per-row Python loop; its
   one strength (a parallel relational engine) is already filled by DuckDB; and it is
   a standing pandas-parity risk. Not a low-usage shortcut: it is the wrong tool for
   this bottleneck even with zero users.
8. Defer FPE until a fresh profile identifies it as the next dominant cost; preserve
   the current HMAC-Feistel construction (no silent FF1 switch). **APPROVED as
   recommended (Cam).**

All 8 §15 decisions APPROVED by Cam 2026-09-09. Remaining before Phase 0 exit:
Task 0.2 authoritative baseline on n2-standard-8; then dennis + Codex plan-review
GO on the full Phase 0 package (0.1 + 0.2 + 0.3 + 0.4).
