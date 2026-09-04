# Phase 4 (engine-efficiency) merge-readiness

Status: record

Point-in-time consolidation for the Phase-4 merge decision. Everything below is
HELD on `feat/native-phase3`; nothing has merged. Phase 4 merges once, at the
end, on Cam's explicit go.

## Slices done (5 of 6, each double-gated: dennis + cross-model Codex-final)

| Slice | What it does | Gate outcome |
|---|---|---|
| Task 4 -- split-dedup | Bounded-parent dedup in the out-of-core relation build | dennis GO + Codex GO |
| Task 6 -- reorder driver | Streams the unordered-join -> external-sort -> contiguity-guard route | dennis GO + Codex GO (r2, after one cleanup fix) |
| Polars-hash parity | Routes the Polars hash strategy through the shared kernel so it is byte-identical to pandas on the common dtype set | dennis GO + Codex GO |
| Cascade safety | Narrows chunked FK self-mask to hash-only + predicate 12 (exact cross-adapter-safe dtype set, two-stage declared+real guard) | dennis GO (x2) + Codex GO (r2, after two real fail-open holes the cross-model gate caught were fixed) |
| HIGH-1 -- OOC decomposition | Pure-move split of two oversized out-of-core modules to clear the size sentry | dennis GO + Codex GO |

Branch CI is now green: the module-size sentry passes (both formerly-oversized
out-of-core modules cleared; `_stream_join.py` ratcheted at 720 with a
decomposition-owed note, `_external_sort.py` back under the cap).

The two-checkpoint cross-model gate proved load-bearing, not ceremony: on the
cascade slice it caught two genuine fail-open holes (an undeclared-dtype hash key
bypassing the runtime safety stage, and a composite-component parent wrongly
treated as a root key) that the same-model review had cleared. Both were
reproduced, fixed at root cause, and re-gated.

## Remaining before merge

### Task 7 -- route seam (LIVE) -- needs a Cam decision

Task 6 built the reorder-route driver but it is not wired into live route
selection. Task 7 is that wiring. Two reasons it is not an autonomous slice:

1. It changes which execution route runs for real workloads -- a
   route-selection behavior change. Under the standing "don't lock decisions
   pre-release" rule, the go-live timing is Cam's call, not the loop's.
2. It carries two obligations from the Task 6 gate: a disk-reservation guard in
   the seam (phase-1 stages N spills with no disk guard today) and a
   multi-edge bounded-RSS assertion (N-edge co-residency is unproven by test).

Decision needed: build Task 7 now (wire the route live, with the two
obligations), or hold it and merge Phase 4 with the reorder route present but
not yet selected.

### Open questions carried through the phase

- **Q1 -- FK-4a timing.** A deferred FK dedup follow-up; when to pick it up.
- **Q2 -- sequence coordinator.** Whether the multi-slice route sequencing wants
  a coordinator, or stays as-is.
- **Q3 -- P4-C native ports.** Which of the remaining hot paths (if any) get
  native-kernel ports next.
- **Q4 -- merge trigger.** When to merge Phase 4 to main (the one merge, on
  explicit go).

## What merging now would ship

The four efficiency/correctness slices plus the branch-hygiene decomposition,
all byte-parity-preserving against the pinned pandas oracle for masking, with
the reorder route available but not selected (until Task 7). No behavior change
to the default route; the cascade slice tightens FK self-mask admission (some
previously-admitted non-hash / exotic-dtype FK self-masks now fail closed with a
clear error pointing at run_pipeline / run_sequential).
