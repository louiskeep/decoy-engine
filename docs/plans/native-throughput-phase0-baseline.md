Status: record

# Phase 0 Task 0.2: Authoritative baseline (8-core reference host)

The starting-line measurement for the native-throughput program, captured on the
approved reference host BEFORE any Phase 1 code. All numbers are current-main
engine behavior (engine source == main @152fbc8d; the run bundled branch
`feat/native-throughput-consolidation` @c7c8214d, which differs from main only in
harness scripts and docs, not engine source).

## Host (frozen)
- GCP `n2-standard-8`: 8 vCPU Intel(R) Xeon(R) @ 2.80GHz, 32,863,272 kB RAM,
  Linux 6.8.0-1063-gcp, Ubuntu 22.04 base (`decoy-bench-base`).
- Companion built on the node: pinned Rust 1.98.0 + maturin, import-verified
  before any native tier.
- Method: fresh process per rep, external `VmHWM` sampling, wall as median of reps
  (small tiers reps 3, 100M rep 1 cold-read), per `PHASE2-BASELINE.md`.

## W2 workload (10 cols: 3 keyed-hash + 3 passthrough + 2 redact + 2 truncate)

| Rows | Route | Wall (s) | Peak RSS (MB) | Hash tput (rows/s/col) | Whole-job (rows/s) |
|---|---|---:|---:|---:|---:|
| 1M | native streaming | 16.45 | 344.3 | 235,685 | 60,804 |
| 4M | native streaming | 64.39 | 357.6 | 235,210 | 62,122 |
| 100M | native streaming | **1,571.77** | **463.3** | 234,355 | 63,623 |
| 1M | oracle full-frame | 44.53 | 1,417.3 | 80,610 | 22,457 |
| 4M | oracle full-frame | 177.63 | 4,951.1 | 80,757 | 22,519 |

Oracle 100M not measured (kept extrapolated per PHASE2-BASELINE; ~18 GB, slow).

## C1 deterministic-faker workload

| Rows | Route | Wall (s) | Peak RSS (MB) | Hash tput (rows/s/col) | Faker tput (rows/s) |
|---|---|---:|---:|---:|---:|
| 1M | native | 61.57 | 360.4 | 267,141 | 106,627 |
| 3M | native | 178.15 | 381.4 | 266,971 | 107,191 |
| 1M | oracle | 118.91 | - | 90,492 | 103,066 |
| 3M | oracle | 354.02 | - | 90,635 | 105,823 |

## Starting-line findings (what Phase 1/2 must move)

1. **The number to beat: 100M native = 1,571.77s (~26.2 min).** Target is <= 600 s.
   Very close to the devbox cert (1,563 s), confirming the n2 single-core speed is
   near the devbox and that single-thread wall is the honest starting point.
2. **Hash derivation dominates.** Native 100M hash is ~234k rows/s/col over 3 cols
   (~1,281 s of the 1,572 s wall, ~81%); the rest (~290 s) is redact/truncate/
   passthrough/IO. To reach 600 s, the hash kernel must drop ~1,281 s -> ~310 s,
   about **4.1x** (or ~2.6x on total wall). This is the Phase 1 target, achievable
   with the cached namespace key + GIL release + 8-thread Rayon on this 8-core host.
3. **Native peak RSS is flat and tiny**: 344 MB (1M) -> 463 MB (100M), far under the
   6.5 GiB ceiling. The streaming memory property holds on the reference host.
4. **Native already beats the oracle ~2.9x on hash** (235k vs 80.7k rows/s/col) and
   ~2.7x on wall, but single-threaded; the parallelism win is still on the table.
5. **Faker is pool-select-bound, NOT crypto-accelerated yet.** Native faker
   throughput (~107k rows/s) is barely above the oracle (~103-105k), because the
   native faker route still calls Python `derive_index` per row. This is exactly
   the Phase 2 gap (`derive_index_batch`): the native hash columns in the same job
   run at ~267k rows/s/col, so the pool-select path is the bottleneck, not the crypto.

## Provenance
Raw: `decoy-platform/docs/product/release-1-validation-runs/2026-09-09-tb6-50m/engine-bench-feat-native-throughput-consolidation/`
(w2_native_small, w2_native_100m, w2_oracle_small, c1_native, c1_oracle .json).
Harness: `decoy-platform/scripts/gcp-bench/{native-baseline-run,remote-native-baseline}.sh`.
Run id p0base3 (two prior attempts failed fast on fresh-node setup: missing C
toolchain, and a hardcoded devbox venv path in bench_driver.py; both fixed and
committed before this run).
