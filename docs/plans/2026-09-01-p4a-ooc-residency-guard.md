# P4-A (residency): scope the out-of-core never-OOM guarantee honestly

Status: plan

> Part 2 Phase 4, slice P4-A (FK streaming / never-OOM), residency sub-slice.
> Cam chose this over the FK-4a join-free path (2026-09-01): "residency first".
> After four plan-gate rounds (below), Cam chose **Option A: scope the guarantee
> to the bounded route and document the rest as caller-managed** (2026-09-01), not
> a precise byte-accounting guard. Design doc: `docs/plans/2026-08-31-part2-phase4-plan.md`.
> Ledger: auto-memory `decoy-engine-efficiency-plan.md`. Phase 4 merges once at
> the end after all testing; this slice is built and held, not merged.

## Why Option A (READ FIRST): the precise guard is not achievable

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

The first four plan drafts tried to *price* the resident footprint in bytes and
fail-closed reject when it exceeds the memory budget. Four cross-model plan-gate
rounds established that this cannot deliver an absolute never-OOM guarantee for
arbitrary in-process callers (recorded in the ledger):

- **A `source_loader` can return anything.** The route accepts a caller-supplied
  `source_loader: Callable[[str], pa.Table]` that resolves missing tables. A
  pre-run size check can only read the table's *profile*; the loader is arbitrary
  code and can return a table with a different schema, row count, or width than
  the profile records. A profile estimate cannot hard-bound an arbitrary result.
- **The caller's other memory is invisible.** The guard can only price the bytes
  *this job* would hold. A caller already holding 30 GB of unrelated data, then
  starting a 20 GB job under a 40 GB budget, OOMs at 50 GB — outside the guard's
  view.
- **Sampled column widths are not hard ceilings.** Profile `max_length` is a
  sampled character count (`_walk.py:172`); a later, unsampled, wider row does
  not contradict it, and CSV row counts are explicit estimates
  (`_readers.py:263`). The router already treats lazy variable-width columns as
  *unpriceable* for exactly this reason (`_pipeline_routing_signals.py:288`). A
  never-OOM guarantee needs a hard bound; a sample does not give one.

So the precise guard chases a guarantee it cannot reach. Option A states the
guarantee honestly instead.

## What this slice does (Option A)

- **Guarantee scope, stated true.** The OOC route's never-OOM guarantee holds for
  the **structural bounded shape only**: every source a `LazySource` (never fully
  materialized) *and* a `TransactionalSink` present. This is exactly what the
  production isolated worker always passes (`LazySource` sources for every
  relationship-bearing job, `_isolated_worker.py:215-216`; an unconditional
  `ParquetTransactionalSink`, `:225-226`), so **production keeps its guarantee
  unchanged**.
- **Caller-managed shapes, documented not policed.** A resident `pa.Table`
  source, a missing sink, and a `source_loader`-resolved source are
  caller-managed: the route runs them exactly as today, but their peak memory is
  the caller's responsibility. A resident source is RAM the caller already spent
  before the route saw it; a no-sink output is RAM the caller asked to receive.
  Neither can be retroactively bounded by the route.
- **A cheap best-effort warning.** When the managed OOC route
  (`run_out_of_core_route`) runs with any caller-managed input shape (a resident
  source, `sink is None`, or a `source_loader`), it emits **one** structured
  warning naming the shape and the bounded alternative (pass a `LazySource` per
  table + a sink, or force `execution_mode='full_frame'` to run resident at your
  own memory risk). No byte sizing, no budget reservation, no rejection — a
  best-effort heads-up, not a false guarantee.
- **Docstring correction.** The route docstring advertises the hole as a feature
  ("a resident `pa.Table` source re-iterates for free", `_runner.py:28-29`) —
  true for CPU, false for RAM. It is corrected to state the residency cost and
  the guarantee scope.

### Explicitly NOT built (chase an unachievable guarantee)

- The precise resident-footprint byte model (buffer accounting, per-strategy
  output-width table, loader/profile pricing).
- The fail-closed size rejection and the budget reservation
  (`resolve_ooc_memory_limit` `reserved_bytes`).
- Auto-spill of a resident source / auto-allocation of an internal sink.

These are recorded, with their gate history, in the ledger; if a real
customer-attributable OOM ever appears on this route, revisit with a
loader-contract change (a hard-bounded source descriptor) rather than a profile
estimate.

## Reachability (scopes the change)

- **Production is already safe and already on the guaranteed shape.** The
  isolated worker hard-wires `LazySource` + `ParquetTransactionalSink`
  (`_isolated_worker.py:215-226`); OOC eligibility is a strict subset of
  relationship-bearing, so neither hole fires there, and the new warning never
  fires on the production path.
- **The holes are reachable only via the in-process public API.** A direct
  `run_pipeline` caller passing a resident `pa.Table`, `sink=None`, or a
  `source_loader` reaches the managed OOC route (`_pipeline.py:446-455` ->
  `run_out_of_core_route`). No non-test `src/` caller does this today. The
  exported `run_fk_out_of_core` primitive (`out_of_core/__init__.py:24`) is a
  direct-caller surface that bypasses the route seam; it stays caller-managed
  with its residency precondition documented at its export and docstring.

## Tasks

- [ ] **Task 1: Guarantee-scope statement + docstring corrections.** State the
  never-OOM guarantee as holding for `LazySource` sources + a `TransactionalSink`
  only, in the `run_out_of_core_route` / route docstring and on the exported
  `run_fk_out_of_core` primitive (`out_of_core/__init__.py`). Correct the
  misleading "re-iterates for free" line (`_runner.py:28-29`) to state that a
  resident source holds the whole input in RAM (a small-N convenience, not free)
  and is caller-managed for memory.
- [ ] **Task 2: Best-effort caller-managed warning.** In `run_out_of_core_route`,
  when the job runs with any caller-managed input shape — at least one resident
  `pa.Table` source, `sink is None`, or a `source_loader` supplied — emit exactly
  one structured warning that names the shape(s) and the bounded alternative. Pure
  structural check on the inputs already in hand (no sizing, no loader invocation,
  no rejection); it must not change any masked output or any routing decision.
  Surface it through the channel the route already uses — a stdlib
  `warnings.warn` with a distinct engine warning category (there is no
  `filterwarnings = error` in the test config, so this does not break existing
  tests), and/or the route's existing `warnings` result field
  (`_pipeline_route_exec.py:398`). The check runs regardless of source resolution,
  so it fires before any loader call. Dedup so exactly one warning is emitted per
  invocation even when multiple caller-managed shapes are present.
- [ ] **Task 3: Tests.** (a) A `LazySource` + sink OOC job (the guaranteed shape,
  worker-shaped) emits **no** caller-managed warning. (b) A resident-source job, a
  `sink=None` job, and a `source_loader` job each emit the caller-managed warning
  naming the shape and the alternative. (c) Warning-does-not-change-results: an
  existing resident/no-sink parity test still matches the full-frame oracle
  byte-for-byte with the warning present (`test_out_of_core_routing_parity.py:159`).
  (d) The exported primitive's docstring documents the residency precondition.
- [ ] **Task 4: No-regression + lint + sentry.** Every existing OOC routing/parity
  test passes byte-for-byte (the warning is additive; nothing is rejected):
  `test_out_of_core_routing.py`, `test_out_of_core_routing_parity.py`,
  `test_out_of_core_group_b_routing.py`, `test_out_of_core_group_c_routing.py`,
  `test_lazy_path_route_admission.py`. ruff + mypy on the diff; module-size sentry
  green. Light mutation on the warning predicate (the input-shape classification)
  to 0 unresolved-logic survivors; message prose adjudicated per the ledger
  policy.

## Acceptance

- The never-OOM guarantee is documented as scoped to `LazySource` + sink;
  production (always that shape) keeps it, and the exported primitive's residency
  precondition is documented (Task 1).
- Resident-source / no-sink / `source_loader` OOC jobs run exactly as today but
  emit one best-effort caller-managed warning naming the shape and the bounded
  alternative; no masked output or routing decision changes (Tasks 2, 3).
- The misleading "re-iterates for free" docstring is corrected (Task 1).
- Every existing OOC routing/parity test passes byte-for-byte; ruff + mypy clean;
  sentry green; 0 unresolved-logic mutation survivors on the warning predicate
  (Task 4).
- The precise byte-model guard, fail-closed rejection, and budget reservation are
  NOT built, and the reason (unachievable guarantee, gate rounds 1-4) is recorded.
- Held on `feat/native-phase3`; no merge.
