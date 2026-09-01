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

Gate round 2 (NO-GO) then proved the first byte model unsound on three counts
(budget double-spend, no-sink output mis-sized, and a per-row size model that is
a category error). The remediated model below is designed around all three. It
runs *before* the loader materializes anything, and it prices a **sound upper
bound on peak resident bytes against the memory budget**, rejecting only when the
bound does not fit.

**Trust boundary (states the guarantee honestly).** The route already trusts the
profile for the routing decision itself (row counts, byte estimates). The guard
prices against that same profile, so its guarantee is "never-OOM *relative to the
trusted profile*" — the identical trust boundary the rest of the route uses. A
`source_loader` that returns data contradicting its declared profile is a source
contract violation, out of scope here exactly as it is for routing (the existing
resolver already warns on a resident-vs-profile row-count disagreement); the
exported `run_fk_out_of_core` primitive stays caller-managed (see hazard 3).

**One peak equation, reserved from the budget (fixes double-spend).** The OOC
non-spillable build floor and the resident Python/Arrow footprint are *co-live*,
so they must be bounded jointly, not each against the full budget. The guard
computes a single `resident_peak_bytes` and reserves it from the budget at the
one choke point that feeds *both* downstream consumers — the `reserved_bytes`
parameter of `resolve_ooc_memory_limit` (`_budget.py:471`, applied before the
returned `budget_bytes` and the DuckDB `memory_limit` are derived, currently
defaulted to 0 and unset at the `_pipeline_route_exec.py:301` call site). The
existing preflight (`enforce_ooc_memory_preflight`) then checks the DuckDB build
floor against the *reduced* budget, so `resident_peak + build_floor + overhead`
is bounded by the original budget with no second denomination to keep in sync.
Before reserving, if `resident_peak_bytes` alone exceeds
`resolved_budget_bytes` (leaving less than the minimum OOC working set), reject
outright rather than floor the remainder.

`resident_peak_bytes` sums three sound terms:

- **Pre-supplied resident `pa.Table` sources — de-duplicated buffer accounting.**
  Sum the sizes of the *unique referenced Arrow buffers* across every column and
  chunk (dedup by buffer id, across tables). This is a true upper bound on the
  pinned allocation and, unlike `.nbytes`, is correct for zero-copy slices that
  report a small logical range while pinning a large backing buffer
  (slice-sharing is relied on elsewhere, `_pipeline_route_exec.py:517`).
- **Loader-resolved missing sources (materialize resident) — priced per column
  from the profile, without invoking the loader.** Fixed-width dtype columns
  (int/float/bool/date/time) price at their exact dtype byte width × profile row
  count (sound). Variable-length columns price at the profile's UTF-8-adjusted
  `max_length` × row count. When a variable-length source column carries **no
  profile width statistic at all**, it is unpriceable → reject fail-closed (do
  not guess). `predict_ooc_build_floor_bytes` (`24 MiB + 190 B/row`) is NOT
  reused here — it models DuckDB relation-build control structures, a different
  quantity, and remains a separate co-live term via the existing preflight.
- **No sink (`sink is None`) — sum over EVERY emitted table, not the largest.**
  The runner holds a job-wide `outputs` dict and assembles each emitted table
  resident regardless of source form (`_runner.py:177,405,411`), so a no-sink
  job retains the sum of all output tables. Each table prices at output row
  count × Σ per-column *output* width, where output width is the masked width,
  not the input width: hash → its fixed hex-digest length (64, or the configured
  truncation), redact → the constant replacement width, truncate → the configured
  length, fpe/passthrough → the input width (known exactly for a resident source,
  dtype-exact for a fixed-width column, profile `max_length` for a profiled
  varlen column), bucket_perturb → the fixed reformatted-date/​numeric width,
  categorical → the widest configured category label, code_set → the widest
  corpus code. `text_redact` and `text_mask` produce genuinely unbounded
  free-text output; a no-sink job that emits one of those columns is unpriceable
  → reject fail-closed (require a sink). Cardinality is 1:1 (masking preserves
  rows); byte width is not.

Reject with a typed `ExecutionError` (`out_of_core_resident_footprint_unbounded`,
or `out_of_core_unpriceable_resident_column` when a term cannot be bounded at
all) mirroring the sibling fail-closed idiom (`fk_full_frame_oom_risk_rejected`,
`_pipeline_routing.py:469,534`), naming the offending table(s)/column(s) and the
two honest ways forward: pass bounded inputs (a `LazySource` per table + a sink),
or force `execution_mode='full_frame'` to run resident at the caller's own memory
risk. **Fail-open is narrowed:** only an abnormal budget-resolution failure
(non-POSIX host, `/proc/meminfo` unreadable) leaves the budget unresolved, and
the production governor normally forwards an explicit slot budget
(`_governor.py:285`); the guard fails open *only* on that abnormal
detection failure, never silently on the normal single-org worker path.

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

1. **Byte model soundness (resolved in "The fix").** The peak-bytes bound must
   not under-count (admit a real OOM). Three terms, each sound: pre-supplied
   sources use de-duplicated referenced-buffer accounting (correct for zero-copy
   slices, unlike `.nbytes`); loader-resolved sources price per column from the
   profile (fixed-width dtype exact, varlen via UTF-8-adjusted `max_length`,
   unpriceable→reject when a varlen column has no width stat); no-sink output
   sums *every* emitted table at its per-strategy *output* width, with
   `text_redact`/`text_mask` treated as unbounded→reject. The `24 MiB + 190 B/row`
   floor constant is NOT reused for Arrow sizing — it stays a separate co-live
   term (the DuckDB relation-build floor) via the existing preflight. The full
   OOC payload surface the width rules must cover is `hash, redact, truncate,
   passthrough` (`_INITIAL_SUPPORTED_STRATEGIES`, also the FK parent-key set),
   `fpe, text_redact, categorical` (`_GROUP_B_SUPPORTED_STRATEGIES`), `text_mask`
   (`_GROUP_C_ALWAYS_SUPPORTED`), and `code_set, bucket_perturb`
   (`_GROUP_C_CONDITIONAL`) — `_compat.py:29-44`. Over-counting a fitting job is a
   usability cost, not a safety bug, and only fires for genuinely large
   resident/no-sink jobs that should pass a sink.
2. **Guard placement before loader materialization, and one budget number.** The
   check and the budget resolution must both move before `_pipeline_route_exec.py:281`
   (loader source resolution; today the budget resolves at line ~301, after the
   loader). Budget resolution depends only on the supplied budget and the
   graph-derived concurrency/sink state (`_budget.py:397`), not on the sources, so
   the reorder preserves its value and its fail-open behavior; the only intended
   change is that a rejection now precedes the loader's side effects. The priced
   `resident_peak_bytes` is reserved through the *same* `resolve_ooc_memory_limit`
   call via `reserved_bytes` (`_budget.py:471`), so the preflight floor check and
   the DuckDB `memory_limit` both see the reduced budget — no second budget
   denomination.
3. **Guard scope / direct `run_fk_out_of_core` callers.** `run_fk_out_of_core`
   is exported (`out_of_core/__init__.py:24`), so a direct caller can bypass the
   route guard. No production or `run_pipeline` path does. The never-OOM
   GUARANTEE is therefore scoped to `run_pipeline`'s managed OOC route; the
   exported primitive is caller-managed, with its residency precondition
   documented at its export and docstring (and the misleading "re-iterates for
   free" line, `_runner.py:28-29`, corrected). The plan-gate confirms this
   boundary and that no `run_pipeline` path reaches the runner without the seam.
4. **No behavior change for fitting callers.** Every job whose priced footprint
   fits the budget stays byte-for-byte unaffected: the worker path (all
   `LazySource` + sink, zero resident source footprint), small forced/auto/byte-
   routed resident OOC with and without a sink, the small **loader-resolved** OOC
   jobs (`test_out_of_core_group_b_routing.py:137`,
   `test_lazy_path_route_admission.py:390` — priced, not rejected on loader
   presence), and sub-threshold jobs routed to full-frame/sequential (never OOC).
   Only a genuinely large resident footprint, or an unpriceable unbounded-width
   term, is rejected. The full no-regression list is Task 5.

## Tasks

- [ ] **Task 1: Residency footprint pricing (sound upper bound).** A pure free
  function returning `resident_peak_bytes` plus a list of unpriceable offenders,
  from three terms: (1) pre-supplied resident sources via de-duplicated
  referenced-buffer accounting (sum unique Arrow buffer sizes across columns/
  chunks, dedup by buffer id across tables — correct for zero-copy slices, not
  `.nbytes`); (2) loader-resolved missing tables priced per column from the
  profile without invoking the loader (fixed-width dtype exact × row count; varlen
  via UTF-8-adjusted `max_length` × row count; varlen with no width stat →
  unpriceable); (3) no-sink output summed over EVERY emitted table at per-strategy
  *output* width (hash→digest/truncation length, redact→constant width,
  truncate→configured length, fpe/passthrough→input width, bucket_perturb→fixed
  reformatted width, categorical→widest label, code_set→widest corpus code;
  `text_redact`/`text_mask`→unpriceable). A per-strategy output-width table keyed
  on the full `_compat.py:29-44` surface, with an explicit "unknown strategy →
  unpriceable" default so a future OOC strategy cannot silently under-price. Pure,
  unit-testable, mutation-graded.
- [ ] **Task 2: The preflight guard, placed before loader resolution, reserved
  from the one budget.** In `run_out_of_core_route`, move budget resolution and
  the guard ahead of source resolution (`_pipeline_route_exec.py:281`). Reject
  when Task 1 reports any unpriceable offender, or when `resident_peak_bytes`
  leaves less than the minimum OOC working set of `resolved_budget_bytes`, with a
  typed `ExecutionError` (`out_of_core_resident_footprint_unbounded` /
  `out_of_core_unpriceable_resident_column`) naming the offending table(s)/
  column(s) and the two ways forward (pass `LazySource` + sink; or force
  `execution_mode='full_frame'`). Otherwise pass `resident_peak_bytes` as
  `reserved_bytes` into the same `resolve_ooc_memory_limit` call so the preflight
  floor check and the DuckDB `memory_limit` both see the reduced budget. Fail open
  only on abnormal budget-resolution failure. Runs before any loader call.
- [ ] **Task 3: Docstring + scope.** Correct `_runner.py:28-29` (resident source
  is a small-N convenience with unbounded RAM, not free), document the
  never-OOM guarantee as scoped to `run_pipeline`'s managed OOC route, and
  document the residency precondition on the exported `run_fk_out_of_core`
  primitive (`out_of_core/__init__.py`).
- [ ] **Task 4: Rejection tests.** Over-budget cases raise
  `out_of_core_resident_footprint_unbounded`: (a) a large pre-supplied resident
  source; (b) a large loader-resolved fixed-width source (priced from the
  profile, loader NOT invoked); (c) a large no-sink output. Unpriceable cases
  raise `out_of_core_unpriceable_resident_column`: (d) a loader-resolved varlen
  source column with no profile width stat; (e) a no-sink job emitting a
  `text_redact`/`text_mask` column. Each message names the offending table/column
  and the two ways forward. A buffer-accounting test proves a small logical
  **slice** of a large `pa.Table` is priced at the pinned backing-buffer size (not
  the slice's `.nbytes`), so a sliced huge source is rejected. A
  loader-materialization test asserts the loader is NOT called on any rejected
  path (the guard fires first).
- [ ] **Task 5: No-regression tests.** These shapes stay byte-for-byte unaffected
  because their priced footprint is tiny and fits the budget (all fixtures are
  small): (a) forced small resident OOC + no-sink reading back `outputs`
  (`test_out_of_core_routing_parity.py:159`); (b) auto-row-threshold small
  resident OOC (`test_out_of_core_routing.py:348`) and forced small resident OOC
  streaming to a sink (`:397`); (c) byte-routed small resident OOC, Group B
  (`test_out_of_core_group_b_routing.py:114`) and Group C
  (`test_out_of_core_group_c_routing.py:138`); (d) the **all-loader** small
  no-sink OOC job (`test_out_of_core_group_b_routing.py:137`) and the mixed
  tiny-resident-parent + **loader-resolved child** OOC reroute (H1 supported case,
  `test_lazy_path_route_admission.py:390`) — both resolve sources through the
  loader on the OOC route, so the guard must price them (fixed-width/fpe columns,
  tiny) rather than reject on loader presence; (e) worker-shaped all-`LazySource`
  + sink (zero resident source footprint, bounded sink output); (f) sub-threshold
  jobs routed to full-frame/sequential (guard never consulted). Note the
  round-1 H1 citation (`:362`) is the *rejected-before-read* case (loader asserted
  never called); the supported OOC reroute is `:390`.
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

- The peak-bytes bound is sound: pre-supplied sources buffer-accounted (slices
  included), loader-resolved sources profile-priced per column, no-sink output
  summed over all emitted tables at per-strategy output width, unbounded-width
  terms rejected as unpriceable. The priced peak is reserved from the one budget
  (`reserved_bytes`), so the resident peak and the DuckDB build floor are bounded
  jointly, not double-spent (Tasks 1, 2).
- Over-budget resident-footprint / no-sink OOC jobs, and jobs with an unpriceable
  unbounded-width term, are rejected with a clear typed error and guidance, before
  any source materialization (Tasks 2, 4).
- Every job whose priced footprint fits the budget is byte-for-byte unaffected,
  including the small loader-resolved OOC jobs (priced, not rejected on loader
  presence): forced/auto/byte-routed small resident OOC with and without a sink,
  the all-loader and mixed loader-child OOC jobs, the worker path, and
  sub-threshold jobs (Task 5).
- The rejected combination provably cannot materialize a whole table or invoke
  the loader (Task 6).
- 0 unresolved-logic mutation survivors on the pricing + guard; ruff + mypy
  clean; sentry green (Task 7).
- Guarantee scoped to `run_pipeline`'s managed OOC route; the exported runner
  primitive's residency precondition documented (Task 3).
- Held on `feat/native-phase3`; no merge.
