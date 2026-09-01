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

Gate rounds 2 and 3 (NO-GO) then reshaped the byte model. Round 2 fixed the
double-spend plumbing; round 3 confirmed that plumbing closed but proved the
sizing still unsound. The model below is the round-3 remediation. It runs
*before* the loader materializes anything and prices a **hard upper bound on peak
resident bytes**, rejecting whenever any term is not hard-boundable.

**Priceable means a HARD bound, not a sampled estimate (the guarantee boundary).**
The existing router already treats a lazy variable-width column as *unpriceable*
and refuses to use its sampled profile width as a bound
(`_pipeline_routing_signals.py:288`); the profile's `max_length` is a sampled
character count (`_walk.py:172`) that a later, wider, unsampled row does not
contradict, and CSV row counts are explicit estimates (`_readers.py:263`). This
guard adopts the same stance rather than inventing profile-width trust: a term is
*priceable* only from a **hard** bound — an already-resident Arrow buffer, a
fixed-width dtype, an exact empty/all-null column (bound 0), or an independently
declared hard byte maximum — and every other variable-width term is
**unpriceable → reject fail-closed**. Row counts used in a bound must be exact;
a non-exact (estimated) count on a variable-width contribution is unpriceable.
The guard needs the `profile` to classify columns, so `run_out_of_core_route`
must thread it through (it is not passed today, `_pipeline.py:447`). The exported
`run_fk_out_of_core` primitive stays caller-managed (hazard 3).

**One peak equation, reserved from the budget without flooring (fixes
double-spend and the floor contradiction).** The OOC non-spillable build floor
and the resident Python/Arrow footprint are *co-live*, so they are bounded
jointly. The guard computes a single `resident_peak_bytes` and reserves it at the
one choke point that feeds *every* downstream consumer — the `reserved_bytes`
parameter of `resolve_ooc_memory_limit` (`_budget.py:471`), which round 3
confirmed reduces the returned `budget_bytes`, the flat `memory_limit`, the
capacity preflight, and the phase-local DuckDB limits (`resolve_phase_memory_limits`,
`_memory_estimate.py:176`) with no path re-deriving from the un-reduced budget.
Reserve via a **one-pass** subtraction that does NOT floor the remainder back up
with `max(..., _MIN_BUDGET_BYTES)` — that floor is a progress heuristic
(`_budget.py:138`), not a proven working set, and flooring would silently restore
budget the residency already spent. Instead: reject outright when
`resident_peak_bytes` plus the real phase-cap/build-floor plus an explicit
transient-runtime headroom term exceeds `resolved_budget_bytes`; otherwise pass
the reservation through so the preflight and DuckDB see the reduced budget. The
fail-open catch at `_pipeline_route_exec.py:313` is narrowed to
`out_of_core_memory_detection_failed` only, so a reservation/fan-in failure
raises rather than silently failing open.

`resident_peak_bytes` sums three terms, each hard-bounded or the job is rejected:

- **Pre-supplied resident `pa.Table` sources — de-duplicated full buffer
  accounting.** Sum the sizes of the *unique referenced Arrow buffers* across
  every column and chunk, including validity bitmaps, offset buffers, and
  structural buffers, recursing into dictionary/nested child buffers; dedup by a
  **stable native allocation identity** (`Buffer.address`), not ephemeral Python
  `id(buffer)`. This is a true upper bound on the pinned allocation and, unlike
  `.nbytes`, is correct for zero-copy slices that report a small logical range
  while pinning a large backing buffer (`_pipeline_route_exec.py:517`).
- **Loader-resolved missing sources (materialize resident) — hard-bounded per
  column, without invoking the loader.** Fixed-width dtype columns (int/float/
  bool/date/time) price at exact dtype byte width × exact row count. An exact
  empty/all-null variable-width column prices at 0. A variable-width column with
  an independently declared hard byte maximum prices at that × exact row count.
  **Any other variable-width column — a sampled `max_length`, no width stat, or a
  non-exact row count — is unpriceable → reject.** `predict_ooc_build_floor_bytes`
  (`24 MiB + 190 B/row`) is not reused for Arrow sizing; it models DuckDB
  relation-build control structures and stays the separate co-live floor term.
- **No sink (`sink is None`) — sum over EVERY emitted FIELD of EVERY emitted
  table.** The runner holds a job-wide `outputs` dict and assembles each emitted
  table resident regardless of source form (`_runner.py:177,405,411`), so a
  no-sink job retains the sum of all output tables, plus Arrow structural
  overhead and an `assemble_resident` reassembly transient (`_emit.py:212`). Each
  field prices at exact row count × its *output* UTF-8 byte width upper bound:
  hash → fixed hex-digest length (64, or the configured truncation); redact →
  constant replacement width; truncate → `length` normally, but the FULL input
  width when `mask_char` is set (it preserves input character count,
  `_scalar.py:104`), multibyte-adjusted; fpe → input width in **UTF-8 bytes**
  (it preserves character positions, not byte width, and accepts custom charsets,
  `_mask_group_b.py:84`); passthrough → input width; bucket_perturb →
  `max(reformatted width, input passthrough width)` because unparseable values
  pass through unchanged (`bucket_perturb.py:157`); categorical → widest
  configured label; code_set → widest corpus code. **FK-join-owned child columns**
  (skipped by scalar masking, `_runner.py:294`) price at the policy-aware maximum
  of their possible outputs — parent-masked value, preserved raw orphan, or
  parent-strategy REMAP value (`_batch_join.py:365`). **Pre-GA undeclared
  passthrough columns** (`_output_projection.py:106`) price as passthrough of
  their input. A field whose output width derives from an unpriceable input
  (a lazy/loader variable-width source), or `text_redact`/`text_mask` whose
  free-text output has no finite bound unless its input width is itself hard
  (resident/fixed → finite token-expansion bound), or **any strategy with no
  width rule**, is unpriceable → reject. Cardinality is 1:1 (masking preserves
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

1. **Byte model soundness (resolved in "The fix", round 3).** The bound must be
   HARD, never a sampled estimate: pre-supplied sources use de-duplicated full-
   buffer accounting (all buffers, native-address dedup, slice-correct);
   loader-resolved sources price only from a hard bound (fixed-width dtype, exact
   empty/all-null→0, or a declared byte max with an exact row count) and are
   otherwise unpriceable→reject, matching how routing already treats lazy varlen
   (`_pipeline_routing_signals.py:288`); no-sink output sums *every emitted field*
   of *every emitted table* at its output UTF-8 width, covering the width subtleties
   round 3 named (truncate+`mask_char` keeps input width; fpe is byte-not-char;
   bucket_perturb `max(reformatted, passthrough)`; FK-join-owned child policy max;
   pre-GA undeclared passthrough), with `text_*` priceable only when the input
   width is hard and unpriceable otherwise. The `24 MiB + 190 B/row` floor is NOT
   reused for Arrow sizing — it stays the separate co-live DuckDB-floor term. The
   full OOC payload surface the width rules must cover is `hash, redact, truncate,
   passthrough` (`_INITIAL_SUPPORTED_STRATEGIES`, also the FK parent-key set),
   `fpe, text_redact, categorical` (`_GROUP_B_SUPPORTED_STRATEGIES`), `text_mask`
   (`_GROUP_C_ALWAYS_SUPPORTED`), and `code_set, bucket_perturb`
   (`_GROUP_C_CONDITIONAL`) — `_compat.py:29-44` — plus join-owned and undeclared
   fields; unknown strategy/field → unpriceable. Over-counting a fitting job is a
   usability cost, not a safety bug.
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

- [ ] **Task 1: Residency footprint pricing (hard upper bound).** A pure free
  function taking the pre-supplied `sources`, the `profile`, the graph, and the
  `sink` presence, returning `resident_peak_bytes` plus a list of unpriceable
  offenders. Three terms: (1) pre-supplied resident sources via de-duplicated
  full-buffer accounting — sum unique Arrow buffers (data, validity, offsets,
  structural, dictionary/nested children), dedup by `Buffer.address` not
  `id(buffer)`, not `.nbytes` (slice-correct); (2) loader-resolved missing tables
  priced per column from the profile without invoking the loader — fixed-width
  dtype exact × exact row count, exact empty/all-null → 0, declared hard byte max ×
  exact row count, else (sampled `max_length`, no stat, or non-exact row count) →
  unpriceable; (3) no-sink output summed over EVERY emitted field of EVERY emitted
  table at its *output* UTF-8-byte width upper bound, plus Arrow structural
  overhead and an `assemble_resident` reassembly transient. The output-width table
  keyed on the full `_compat.py:29-44` surface with the exact rules from "The fix"
  (hash fixed; redact constant; truncate `length` but full-input-width when
  `mask_char` set; fpe input UTF-8 bytes; passthrough input; bucket_perturb
  `max(reformatted, passthrough)`; categorical widest label; code_set widest
  corpus code; FK-join-owned child = policy-aware max of parent-masked/raw-orphan/
  REMAP; undeclared pre-GA passthrough = input; text_* finite only when input width
  is hard, else unpriceable) and an explicit "unknown strategy/field → unpriceable"
  default. Pure, unit-testable, mutation-graded.
- [ ] **Task 2: The preflight guard, placed before loader resolution, reserved
  from the one budget without flooring.** In `run_out_of_core_route` (now threaded
  the `profile`), move budget resolution and the guard ahead of source resolution
  (`_pipeline_route_exec.py:281`). Reject when Task 1 reports any unpriceable
  offender, or when `resident_peak_bytes` + the real phase-cap/build-floor + an
  explicit transient-runtime headroom exceeds `resolved_budget_bytes`, with a typed
  `ExecutionError` (`out_of_core_resident_footprint_unbounded` /
  `out_of_core_unpriceable_resident_column`) naming the offending table(s)/
  column(s) and the two ways forward (pass `LazySource` + sink; or force
  `execution_mode='full_frame'`). Otherwise reserve `resident_peak_bytes` through
  the same `resolve_ooc_memory_limit` call via a one-pass subtraction that does NOT
  floor the remainder back to `_MIN_BUDGET_BYTES`. Narrow the fail-open catch at
  `_pipeline_route_exec.py:313` to `out_of_core_memory_detection_failed` only. Runs
  before any loader call.
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
  (`test_out_of_core_group_c_routing.py:138`), including the small resident
  **no-sink `text_mask`** case — its text output IS priceable because the input
  width is hard (resident), so the finite token-expansion bound applies (this is
  the case round 3 flagged as wrongly rejected); (d) the **all-loader** small
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
