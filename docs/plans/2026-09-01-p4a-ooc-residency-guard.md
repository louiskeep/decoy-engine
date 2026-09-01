# P4-A (residency): close the out-of-core never-OOM guarantee holes

Status: plan

> Part 2 Phase 4, slice P4-A (FK streaming / never-OOM), residency sub-slice.
> Cam chose this over the FK-4a join-free path (2026-09-01): "residency first".
> Design doc: `docs/plans/2026-08-31-part2-phase4-plan.md`. Ledger: auto-memory
> `decoy-engine-efficiency-plan.md`. Phase 4 merges once at the end after all
> testing; this slice is built and held, not merged. The FK-4a plan
> (`docs/plans/2026-09-01-p4a1-fk4a-join-free-out-of-core.md`) is parked.

## What this closes, and the honest framing (READ FIRST)

The out-of-core (OOC) FK route processes one batch at a time and is
memory-bounded, but its never-OOM guarantee **silently depends on two caller
inputs** that nothing enforces:

- **HOLE 1, resident source.** A source may be a resident `pa.Table` (whole
  input table in RAM) instead of a disk-backed `LazySource`. The runner iterates
  it in place (`_runner.py:491-496`, `src.to_batches(...)`), so the whole input
  stays resident regardless of the bounded join.
- **HOLE 2, no sink.** With no `TransactionalSink`, the streamed output batches
  are reassembled column-wise into one resident `pa.Table` to recover
  value-derived column types (`_runner.py:405-411` -> `assemble_resident`,
  `_emit.py:197-225`). The whole output table stays resident.

A recon pass established the exact reachability (it scopes the fix):

- **Production is already safe.** The isolated worker, the only large-N
  production entrypoint, hard-wires both inputs: `LazySource` sources for every
  relationship-bearing job (`_isolated_worker.py:215-216`) and an unconditional
  `ParquetTransactionalSink` (`_isolated_worker.py:225-226`). OOC eligibility is
  a strict subset of relationship-bearing, so neither hole can fire there.
- **The holes are latent and ungated via the public in-process API.** The route
  decision gates on row count only and never inspects source form or sink
  presence (`decide_execution_route`, `_pipeline_routing.py:357-364`); the memory
  preflight prices only the relation-build floor and models neither hole
  (`enforce_ooc_memory_preflight` <- `predict_ooc_build_floor_bytes`,
  `_memory_estimate.py:327-358`). So a direct `run_pipeline` caller passing a
  resident 6M-row table and/or `sink=None` is routed to OOC and **silently
  materializes whole tables**, violating the never-OOM guarantee the route
  advertises. No non-test caller in `src/` does this today.

So this is **guarantee-honesty hardening**, not a production-OOM fix. The value:
make the OOC route's never-OOM contract true for every public-API caller, by
refusing the unbounded input combination with a clear, actionable error instead
of running it and quietly blowing the memory envelope. The route docstring even
advertises the hole as a feature ("a resident `pa.Table` source re-iterates for
free", `_runner.py:28-29`), true for CPU, false for RAM; that gets corrected.

## The fix: a boundedness-aware fail-closed residency preflight

The first draft rejected any resident source or missing sink whenever OOC was
selected. Gate round 1 (NO-GO) proved that wrong on three counts, all now
designed around:

- **"OOC selected" does not imply "large."** Forced `execution_mode='out_of_core'`
  ignores the row threshold (`_pipeline_routing.py:371`), and byte-estimate
  routing selects OOC independent of it (`_pipeline_routing.py:454`). Existing
  parity tests force *small* resident, no-sink OOC and read back
  `outputs[table]` (`test_out_of_core_routing_parity.py:159`). A resident, no-sink
  OOC job on tiny data is legitimate and must keep working.
- **Mixed residency is a supported shape.** A small resident parent plus a large
  lazy child is explicitly supported and tested
  (`test_lazy_path_route_admission.py:362`, H1). Rejecting any resident source
  breaks it.
- **The `source_loader` materializes before the old guard.** For a forced-OOC
  job with `sources={}` + a `source_loader`, `run_out_of_core_route` resolves
  every missing table to a resident `pa.Table` at `_pipeline_route_exec.py:281-287`
  (the docstring concedes "full per-table residency"), before the line-360
  preflight. A large table OOMs before any check downstream of it can fire.

So the guard must price the *actual resident footprint against the memory
budget*, and it must run *before* the loader materializes anything. When the OOC
route is selected, before source resolution (`_pipeline_route_exec.py:281`),
compute the bytes the OOC path would hold resident and reject only when they do
not fit `resolved_budget_bytes`:

- **Pre-supplied resident `pa.Table` sources:** exact `.nbytes` (O(1), already in
  RAM, free to read).
- **Missing sources a `source_loader` will materialize:** estimated bytes from
  the table's profile row count (the same per-table count the routing size gate
  already resolves, `_resolve_largest_mask_table_rows`) times the preflight's
  established per-row estimate, priced *without invoking the loader*. A
  `source_loader` returns `pa.Table`, so every table it resolves is resident.
- **No sink (`sink is None`):** the output table is held resident
  (`assemble_resident`); estimate it at the largest mask table's bytes (FK
  masking is 1:1, so output size tracks input size).

Reject with a typed `ExecutionError` when the summed resident footprint exceeds
`resolved_budget_bytes`, mirroring the sibling fail-closed idiom
(`fk_full_frame_oom_risk_rejected`, `_pipeline_routing.py:469,534`) and pointing
at the two honest ways forward: pass bounded inputs (a `LazySource` per table +
a sink), or force `execution_mode='full_frame'` to run resident at the caller's
own memory risk. Fail OPEN when the budget is unresolved (host-RAM detection
failed and no explicit budget), matching `enforce_ooc_memory_preflight`'s own
None behavior, so a job the in-memory path would have run is not newly blocked.
This reuses the existing budget denomination (`resolved_budget_bytes`) and the
existing row/byte estimation approach (`predict_ooc_build_floor_bytes` is already
`24 MiB + 190 B/row`), extended to also count resident input and no-sink output.

### Why fail-closed, not auto-remediate

Gate round 1 (Q1) confirmed both:

- **Resident source (HOLE 1): the RAM is already spent.** By the time a resident
  `pa.Table` reaches the route, the caller (or the loader) has already
  materialized the whole input. Spilling it cannot retroactively cap a peak that
  already happened, cannot force the caller to release other references, and so
  cannot establish an end-to-end guarantee. The only truthful bounded execution
  is one where the input is never fully materialized, i.e. a `LazySource`.
- **No sink (HOLE 2): in-memory outputs of a large table are unbounded by
  construction.** An internal sink would bound execution only by returning files
  or empty outputs; preserving the documented `outputs[table] -> pa.Table`
  contract requires reading the output back, which recreates the hole. Auto-sink
  is an API redesign, not transparent remediation.

Auto-remediation is deferred; primitives exist if a future need arises
(`MaskedKeyStager` + `pq.write_table`/`LazySource`, `ParquetTransactionalSink`).

## Design hazards to clear in the plan-gate

1. **Byte model correctness.** The resident-footprint estimate must not
   under-count (admit a real OOM) or wildly over-count (reject a fitting job).
   Pre-supplied `.nbytes` is exact. The loader/profile estimate and the no-sink
   output estimate reuse the preflight's own per-row model; the plan-gate
   confirms the per-row constant and the 1:1 output assumption are sound for the
   supported strategy surface (`hash/redact/truncate/passthrough`, all near-1:1
   in width), and that a wide variable-length column is handled (nbytes is exact
   for pre-supplied; the profile estimate should use a width-aware count where
   available rather than a fixed row constant that a wide table would defeat).
2. **Guard placement before loader materialization.** The check must run before
   `_pipeline_route_exec.py:281` resolves missing sources through the loader, so
   the budget must be resolved before that point too (today it is resolved at
   line 301, after the loader). The plan reorders budget resolution ahead of
   source resolution, or computes the residency check with an independently
   resolved budget. The plan-gate confirms the reorder does not change budget
   values or the fail-open-on-unresolved behavior.
3. **Guard scope / direct `run_fk_out_of_core` callers.** `run_fk_out_of_core`
   is exported (`out_of_core/__init__.py:24`), so a direct caller can bypass the
   route guard. No production or `run_pipeline` path does. The never-OOM
   GUARANTEE is therefore scoped to `run_pipeline`'s managed OOC route; the
   exported primitive is caller-managed, with its residency precondition
   documented at its export and docstring (and the misleading "re-iterates for
   free" line, `_runner.py:28-29`, corrected). The plan-gate confirms this
   boundary and that no `run_pipeline` path reaches the runner without the seam.
4. **No behavior change for fitting callers.** Every job whose resident footprint
   fits the budget stays byte-for-byte unaffected: the worker path (all
   `LazySource` + sink, zero resident footprint), small forced-OOC + no-sink
   (`test_out_of_core_routing_parity.py:159`), mixed tiny-resident-parent (H1,
   `test_lazy_path_route_admission.py:362`), and sub-threshold jobs routed to
   full-frame/sequential (never OOC). Only a genuinely large resident footprint
   is rejected. The plan-gate confirms the existing forced/byte-routed small-OOC
   tests still pass.

## Tasks

- [ ] **Task 1: Residency footprint pricing.** A free function that, given the
  pre-supplied `sources`, the loader-resolved missing tables (by profile count,
  not materialized), the `sink` presence, and the graph, returns the estimated
  resident bytes the OOC path would hold: pre-supplied resident `.nbytes` +
  loader/profile-estimated bytes + (no-sink) output estimate. Width-aware where a
  profile carries it. Pure, unit-testable, mutation-graded.
- [ ] **Task 2: The preflight guard, placed before loader resolution.** In
  `run_out_of_core_route`, resolve the budget ahead of source resolution
  (`_pipeline_route_exec.py:281`) and reject when the Task 1 footprint exceeds
  `resolved_budget_bytes`, with a typed `ExecutionError`
  (`out_of_core_resident_footprint_unbounded`) naming the offending table(s) /
  the no-sink output and the two ways forward (pass `LazySource` + sink; or force
  `execution_mode='full_frame'`). Fail open when the budget is unresolved. Runs
  before any loader call, so no large table is materialized before the check.
- [ ] **Task 3: Docstring + scope.** Correct `_runner.py:28-29` (resident source
  is a small-N convenience with unbounded RAM, not free), document the
  never-OOM guarantee as scoped to `run_pipeline`'s managed OOC route, and
  document the residency precondition on the exported `run_fk_out_of_core`
  primitive (`out_of_core/__init__.py`).
- [ ] **Task 4: Rejection tests.** A resident source, a loader-resolved source,
  and a no-sink output, each sized past the budget on an OOC-selected job, raise
  `out_of_core_resident_footprint_unbounded`; the message names the cause and the
  two ways forward. A loader-materialization test asserts the loader is NOT
  called on the rejected path (the guard fires first).
- [ ] **Task 5: No-regression tests.** These existing/added shapes stay
  byte-for-byte unaffected because their footprint fits the budget: (a) forced
  small OOC + no-sink reading back `outputs` (`test_out_of_core_routing_parity.py:159`);
  (b) mixed tiny-resident-parent + lazy child (H1,
  `test_lazy_path_route_admission.py:362`); (c) byte-routed small OOC; (d)
  worker-shaped all-`LazySource` + sink; (e) sub-threshold jobs routed to
  full-frame/sequential (guard never consulted).
- [ ] **Task 6: Guarantee test.** Assert an over-budget resident/no-sink OOC job
  never reaches `run_fk_out_of_core` / `assemble_resident` / the loader (the
  guard raises first), so no whole-table materialization occurs on the rejected
  path. The always-on proof the guarantee is enforced, not merely documented.
- [ ] **Task 7: Lint + mutation + sentry.** ruff + mypy on the diff; mutation-
  grade the pricing + guard predicate to 0 unresolved-logic survivors (prose
  adjudicated per the ledger policy); module-size sentry green.

## Non-goals (explicitly deferred)

- Auto-spill of a resident source / auto-allocation of an internal sink (a
  future ergonomics follow-up; fail-closed is the honest minimal fix, and for a
  resident source cannot make the guarantee true anyway).
- Pricing the resident-source / no-sink residency into
  `enforce_ooc_memory_preflight` (the guard rejects the combination outright, so
  there is nothing to price; if auto-remediation ever lands, the preflight would
  then model the bounded cost).
- The FK-4a join-free path (parked), the bounded external sorter (P4-A.2), the
  4b order-restore reland (P4-A.3).
- Any change to the row-count route thresholds. The guard is orthogonal: it
  refines what "OOC-selected" requires of its inputs, not when OOC is selected.

## Acceptance

- Over-budget resident-footprint / no-sink OOC jobs are rejected with a clear
  typed error and guidance, before any source materialization (Tasks 2, 4).
- Every job whose footprint fits the budget is byte-for-byte unaffected: forced
  and byte-routed small OOC, mixed tiny-resident-parent, worker path, and
  sub-threshold jobs (Task 5).
- The rejected combination provably cannot materialize a whole table or invoke
  the loader (Task 6).
- 0 unresolved-logic mutation survivors on the pricing + guard; ruff + mypy
  clean; sentry green (Task 7).
- Guarantee scoped to `run_pipeline`'s managed OOC route; the exported runner
  primitive's residency precondition documented (Task 3).
- Held on `feat/native-phase3`; no merge.
