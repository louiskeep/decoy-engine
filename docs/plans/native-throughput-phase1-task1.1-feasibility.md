Status: record

# Phase 1 Task 1.1: Feasibility gate result (reference host)

The go/no-go feasibility gate BEFORE building the cached-key + Rayon kernel
(Tasks 1.2-1.5). Measured on the frozen reference host with the committed probe
`decoy-engine-native/examples/feasibility_probe.rs`.

## Host (confirmed topology)
GCP n2-standard-8: 1 socket x 4 cores/socket x 2 threads/core = **4 physical cores
+ hyperthreading = 8 vCPU**, Intel Xeon @ 2.80GHz, 32 GiB, Linux 6.8.0-1063-gcp.
This confirms the 4-physical-core reality that made the 600s target margin-thin.

## Result (3 consistent runs on the REFERENCE host, run-id p1feas)
All numbers below are measured on n2-standard-8 UNLESS marked (devbox). The 3 runs
gave identical projections (422 / 422 / 422 s; cached-HMAC 1365/1366/1367 ns), so
on-host run-to-run spread is negligible.

| Metric | Value | Host |
|---|---|---|
| cached-HMAC per row (post-Task-1.2 cost) | 1,366 ns | reference |
| current per row (HKDF recomputed) | ~3,950 ns | reference |
| cache gain | 2.89x | reference |
| effective 8-thread scaling (HMAC-only proxy) | **3.89x** | reference (devbox proxy 3.42x) |
| baseline full per-row hash cost | 4,267 ns (Task 0.2) | reference |
| probe derive_array full pipeline (cross-validation) | 4,306 ns (within 1%) | devbox |
| per-row HKDF savings caching removes | ~775s over 300M ops | reference |
| cached hash single-thread | 505s | reference |
| cached hash at 8 threads (proxy scaling) | 130s | reference |
| non-hash serial floor (Task 0.2) | 292s | reference |
| **PROJECTED total** | **422s** | reference |
| target | 600s | |
| **verdict** | **FEASIBLE; realistic margin ~50-110s** | |

## Margin caveat (dennis methodology review)
The headline 178s (30%) margin is OPTIMISTIC because the 3.89x scaling is an UPPER
BOUND: the probe's `wall_threaded` fans out only the HMAC, so it omits the two most
likely scaling killers the real Rayon kernel will hit, per-row `build_frame` heap
allocation (300M allocs under 8-way fan-out = global-allocator contention) and the
`StringBuilder`/Arrow output path. The real built-kernel scaling will be materially
lower than 3.89x. The GO is still SAFE because break-even 8-thread scaling is only
~1.8x against 4 physical cores: even if real scaling degrades to ~2.8x the projection
is ~493s (107s margin), and it would have to fall below ~1.8x for a false GO, which
would itself be a build failure caught at the Task 1.6 gate, not a projection error.
Honest framing: **feasible with a realistic ~50-110s margin**, not "comfortable 30%".
BINDING ON TASK 1.6: the 100M gate MUST measure scaling on the BUILT kernel (real
`derive_array` under Rayon with per-row allocation + Arrow output), never re-use this
3.89x HMAC-only proxy.

## Projection method (honest, baseline-grounded)
Start from the baseline's FULL per-row hash cost (1,280.11s / 300M ops, which
already includes canonicalization + hex + Arrow), subtract ONLY the MEASURED per-row
HKDF savings that caching removes (everything else stays per-row and parallelizes),
divide by the MEASURED effective 8-thread scaling, add the 292s non-hash serial
floor. Not an HMAC-only microbench extrapolation.

## Decision
**Task 1.1 GATE PASSES** (dennis methodology review: GO conclusion TRUSTWORTHY, 0
blocker/high). The 600s target is reachable on the reference host via the Phase 1
levers (cache the namespace key once + release the GIL + bounded Rayon), with a
realistic ~50-110s margin. The load-bearing claim (the current kernel recomputes
HKDF per row; Task 1.2 hoists it) was traced in source and holds. Proceed to Task
1.2. No host/scope escalation to Cam needed. The realistic margin means the
redact+truncate serial floor does NOT additionally need parallelizing to hit 600s,
though it remains a Phase-1 option if the built kernel underperforms at Task 1.6.

Raw: `decoy-platform/docs/product/release-1-validation-runs/2026-09-10-tb6-50m/engine-bench-feat-native-throughput-consolidation/` (feasibility.jsonl, host.txt).
