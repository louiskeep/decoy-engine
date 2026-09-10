Status: record

# Phase 1 Task 1.1: Feasibility gate result (reference host)

The go/no-go feasibility gate BEFORE building the cached-key + Rayon kernel
(Tasks 1.2-1.5). Measured on the frozen reference host with the committed probe
`decoy-engine-native/examples/feasibility_probe.rs`.

## Host (confirmed topology)
GCP n2-standard-8: 1 socket x 4 cores/socket x 2 threads/core = **4 physical cores
+ hyperthreading = 8 vCPU**, Intel Xeon @ 2.80GHz, 32 GiB, Linux 6.8.0-1063-gcp.
This confirms the 4-physical-core reality that made the 600s target margin-thin.

## Result (3 consistent runs, run-id p1feas)
| Metric | Value |
|---|---|
| cached-HMAC per row (post-Task-1.2 cost) | 1,366 ns |
| current per row (HKDF recomputed) | ~3,950 ns |
| cache gain | 2.89x |
| effective 8-thread scaling | **3.89x** (HT beats the devbox's 3.42x) |
| baseline full per-row hash cost | 4,267 ns (Task 0.2), probe derive_array cross-validates |
| per-row HKDF savings caching removes | ~775s over 300M ops |
| cached hash single-thread | 505s |
| cached hash at 8 threads | 130s |
| non-hash serial floor (Task 0.2) | 292s |
| **PROJECTED total** | **422s** |
| target | 600s |
| **verdict** | **FEASIBLE, 178s margin (30% headroom)** |

## Projection method (honest, baseline-grounded)
Start from the baseline's FULL per-row hash cost (1,280.11s / 300M ops, which
already includes canonicalization + hex + Arrow), subtract ONLY the MEASURED per-row
HKDF savings that caching removes (everything else stays per-row and parallelizes),
divide by the MEASURED effective 8-thread scaling, add the 292s non-hash serial
floor. Not an HMAC-only microbench extrapolation.

## Decision
**Task 1.1 GATE PASSES.** The 600s target is reachable on the reference host via the
Phase 1 levers (cache the namespace key once + release the GIL + bounded Rayon) with
comfortable margin. Proceed to Task 1.2 (compute the namespace key once). No
host/scope escalation to Cam needed. The 178s margin also means the redact+truncate
serial floor does NOT additionally need parallelizing to hit 600s (it would only add
margin), though it remains a Phase-1 option if the built kernel underperforms the
projection at the Task 1.6 gate.

Raw: `decoy-platform/docs/product/release-1-validation-runs/2026-09-10-tb6-50m/engine-bench-feat-native-throughput-consolidation/` (feasibility.jsonl, host.txt).
