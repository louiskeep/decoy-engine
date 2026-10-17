Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification

# Phase F slice F4: generate + mixed-route capacity validation (1M/10M/100M)

Per the Phase F program plan (F4) + Cam's scope (100M capped; 200M deferred; GCP
budget pre-authorized). Rev-2 folds the Codex plan-gate NO-GO (3 P1 + 2 P2): the
mixed route needs F2's OWN evidence (not the OOC witness), a mixed-specific 2-table
verifier (not the 3-table fk-scale parity), and the oracle byte-check must be
isolated from the capacity measurement.

## 1. Problem + goal

No capacity/bounded-memory validation exists for generation or generate+mask at
scale (fk-scale + fk_memory_probe + bench-* are masking-only or throughput-only).
Goal: validate that a mixed generate+mask job (the shipped F2 production route) runs
at 1M/10M (now) and 100M (deferred) TOTAL rows with memory UNDER A PROPOSED CEILING,
CORRECT output, and recorded throughput. "Bounded" here = "completed with external
peak RSS under a pre-declared ceiling"; it is a PROPOSED mixed-run ceiling, NOT a
proven size-independent bound (resident generation grows with parent cardinality -
§5). A killed/failed run is NEVER labeled bounded regardless of its recorded peak.

## 2. Scope + explicit deferrals

IN (build + light validation, no new spend):
- A new capacity bench worker for the MIXED route (scripts/fk-scale/
  bench_worker_mixed.py), driving the PRODUCTION run_pipeline through the F2 mixed
  route: a generate-kind PARENT table (resident) + a masked CHILD over one FK edge
  (child masked on the bounded chunked route), with a TRANSACTIONAL STREAMING SINK
  (ParquetTransactionalSink) so outputs do not accumulate in memory.
- Reuse (correctly): scripts/fk-scale/_supervisor.py VERBATIM (external ru_maxrss
  via os.wait4, kill-safe) - generation runs INSIDE the supervised worker so its
  lifetime RSS (incl. transient peaks) is captured. Reuse build_fk_chain.py's
  chunked-write helpers + _chain_layout.py helpers for the child parquet.
- Do NOT reuse ooc_route_witness.py (it instruments run_fk_out_of_core, not the
  mixed route) and do NOT reuse _parity.py verbatim (it needs 3 source tables + both
  edges masked + rejects unchanged parent keys - wrong shape). See §3.
- A FAST wiring/correctness test (tests/perf/, pytest.mark.perf, default gate): tiny
  size (2-5k rows) in a subprocess, asserts completed, bounded (external peak under
  a generous small ceiling), MIXED route actually taken, FK integrity, and
  byte-identity to the full-frame mixed oracle.
- An OPT-IN scale test (pytest.mark.benchmark, excluded from default suite) for 1M
  and 10M tiers (capacity + correctness per the oracle matrix in §3).
- RUN the light tiers LOCALLY now as EVIDENCE: 1M + 10M, memory-monitored; record.

DEFERRED (Cam-paced, scale/spend): the 100M run (heavy; local within the proposed
ceiling, or GCP within the pre-authorized budget); the GCP engine-bench.sh mixed
battery case (flagged possibly-not-live - confirm/add in the deferred step); 200M.
Completion of THIS slice does NOT claim 100M mixed capacity.

## 3. Design (reuse-correctly)

ROUTE EVIDENCE (fold P1-1): assert the mixed route via F2's own signals, not the OOC
witness: quality_metrics execution_mode == "mixed", and per mask table the
mixed_route.mask_tables[<t>].chunked_route evidence (executed backend != pandas
oracle for native-admitted columns, chunk count > 1 at scale). Require the
transactional streaming sink and assert streamed-child evidence (outputs_streamed /
sink committed, result.outputs empty) so the child does not accumulate resident.

FIXTURE + VERIFIER (fold P1-2): the job is TWO tables - a generated parent + a masked
child over one FK edge. Tiers are 2-table exact counts (parent + child), NOT the
3-table parent/child/grandchild allocations: 1M smoke = parent 0.2M / child 0.8M;
10M = parent 2M / child 8M (child is the chunked-masked table; counts are capacity
workload choices, not a reorder-threshold claim - the mixed route does not use the
OOC reorder driver). (100M deferred = parent 20M / child 80M.) A new
bench_worker_mixed verifier (NOT _parity.py): deterministic generated parent keys
(from the generate config + seed), child FK built to reference them with a known
orphan fraction, expected parent row-count from config, committed child row-count ==
input, and bounded (head/middle/tail windowed) FK parity checked ROW-SPECIFICALLY -
expected-vs-actual value at each sampled child row position (incl. deterministic
orphan rows per the orphan policy), not mere membership - labeled as SAMPLED coverage. Specify exact parent/child
counts + schemas per tier in the worker.

ORACLE CORRECTNESS, ISOLATED FROM CAPACITY (fold P1-3): the oracle byte-identity
check runs in its OWN step/process, separately measured, NEVER alongside the
capacity-measured production run (its whole-output materialization would dominate
RSS). Oracle tier matrix: byte-identity at TINY (fast test) + 1M (opt-in); 10M is
CAPACITY-ONLY (no oracle comparison alongside; an optional isolated 10M oracle check
may run as a separate process, reported separately). Comparison is canonical Arrow
schema + value-byte equality (reuse the F2 sweep's oracle-vs-mixed helper), not
Parquet file identity. Unchecked tiers reported explicitly.

## 4. Acceptance

- Fast perf test (default gate, BLOCKING): tiny mixed job -> completed, external peak
  under a generous ceiling, mixed route + native backend witnessed (F2 evidence),
  streamed sink (no resident accumulation), FK parity ok, byte-identical to oracle
  (isolated). Deterministic, seconds, subprocess-isolated.
- Opt-in benchmark test (NON-blocking capacity; see disposition): 1M + 10M complete,
  external peak under the PROPOSED ceiling, FK parity ok; byte-identity asserted at
  1M (10M capacity-only). Throughput (mask_s p50/p95) + generation-phase RSS +
  parent Arrow bytes recorded as DISTINCT diagnostics.
- DISPOSITION (fold P2-5): executing AND recording both light tiers is REQUIRED.
  A capacity/throughput MISS (completed but over the proposed ceiling, or slow) is a
  FINDING (Slacked, recorded), NOT a merge blocker. A HARNESS DEFECT or an observed
  CORRECTNESS mismatch (byte/parity/route) BLOCKS readiness. A killed/failed run is a
  failure, never "bounded".
- Light-tier EVIDENCE recorded in decoy-platform/docs/product/release-1-validation-
  runs/<date>-f4-mixed/summary.json + a decoy-engine docs/records/ note;
  ROADMAP/RECENTLY-SHIPPED updated; 100M + GCP listed as the deferred paced step.
- ci-mirror clean; markers correct (benchmark excluded from default; perf in gate).

## 5. Risk

The mask-half bounded behavior is proven (Phase E/D4); the NEW risk is the resident
generated PARENT's memory, which grows with parent cardinality - this is precisely
what the deferred 100M run measures, and why 8 GiB is a PROPOSED ceiling not a proven
bound. ru_maxrss is whole-process and cannot attribute bytes to the parent; so report
generation-phase RSS (observed) + parent Arrow table nbytes as separate diagnostics,
without claiming exact attribution. Light tiers validate the harness + bound SHAPE,
not 100M capacity. The oracle materialization is the other trap (fold P1-3):
isolated/separately-measured. NOTE (plan-gate r2 P2): the mixed route reads the WHOLE
child FK key column and retains the source + resolved key arrays in its carrier, so
child-side key memory is NOT fully chunk-bounded; report resident child-key/carrier
bytes as a DISTINCT diagnostic alongside resident-generation bytes. Whole-process
external RSS remains the capacity decision (it captures both).

## 6. Gate chain

Codex plan-gate (this doc) -> build (Sonnet, reuse _supervisor + chunked-write
helpers; mixed-specific route-evidence + verifier; isolated oracle) -> ci-mirror +
fast perf test + 1M/10M opt-in run (record) -> dennis -> Codex final -> merge on
green + Slack. DOCUMENT: ROADMAP + RECENTLY-SHIPPED + records note; 100M/GCP flagged
as the deferred paced step for Cam.
