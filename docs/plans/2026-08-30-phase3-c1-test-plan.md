Status: plan

# Phase 3 C1 native surface: thorough testing plan

Phase 3 routed the deterministic C1 recipe onto the bounded-memory native chunked route (Tasks
3.0-3.6, built and double-gated on `feat/native-phase3`). This plan hardens the Phase 3 delta to
the same durable bar the Phase 2 surface reached, measuring where coverage and mutation actually
stand on the CHANGED units rather than adding tests by count. It runs as gated batches, each
ending in a dennis + Codex gate.

It is the Phase 3 continuation of `docs/plans/2026-08-29-native-efficiency-test-plan.md` (the
Phase 2 T0-T6 program). **The method in that plan's section 3 is binding here by reference** (the
five-field denominator, the measure-before-adding rule, the parity-oracle discipline, the
property-over-example rule, the mutation-substrate caveats). This plan does not restate it; it
states the Phase 3 SCOPE, the Phase 3-specific substrate wrinkles, and the CANDIDATE gaps per
batch. A test is added when, and only when, a measured coverage hole or a surviving mutant
demonstrates the gap.

## 0. What this plan does and does NOT certify (read first)

Phase 3 built the leak-gate primitives but did NOT wire them onto the native route's critical
path. `enforce_pool_quality` is STANDALONE by construction (`_pool_quality.py` docstring: it is a
separate function a future coordinator calls; the dispatch/route generators never invoke it), and
`RouteDiagnostics` is a collector that nothing on the route yet consults to BLOCK publication. So:

- This plan certifies the **primitive correctness** of `_pool_quality`, `_route_diagnostics`,
  `_phase3_eligibility`, `_identity`, and the `_dispatch` faker branch: given their inputs, do they
  compute the right verdict/measurement/isolation and fail closed.
- This plan does NOT and CANNOT certify the **operational outcome** that a pool-quality breach
  PREVENTS masked output from being published. The master plan requires publication to wait on the
  `pool_quality` obligation, but Task 3.5's current detailed steps cover admission and route evidence,
  NOT a call to `enforce_pool_quality` before publication -- so that wiring is not yet a written,
  owned step. This plan records the owed work explicitly: Task 3.5 + its prod-sim leg must add (a) the
  coordinator wiring that calls `enforce_pool_quality` before releasing any masked output, and (b) an
  injected-breach test proving a breach blocks publication. Until that lands, the operational leak-gate
  is unproven, and this plan does not stand in for it.

Every "leak-gate" claim below is therefore scoped to primitive correctness. The operational
certification is explicitly deferred, and this plan says so at its acceptance bar rather than
implying a wired gate it has not proven.

## 1. Scope

The surface under test is the Phase 3 delta measured against `origin/main` (the merged Phase 2
HEAD, `186141ba`), Python-only. There is NO Rust delta: the faker route selects values with the
Python `PoolSampler`, and the C1 hash columns reuse the Phase 2 Rust keyed kernel UNCHANGED (that
crate is already graded by the Phase 2 T1-T3 batches and is not re-graded here).

New units (graded in full):

- `execution/native/_phase3_eligibility.py` (229 LOC) -- the C1 admission layer above
  `native_route_eligibility`: the JC-5 deterministic/partition-independence gate and the eight
  coded faker-column rejection reasons.
- `execution/native/_pool_quality.py` (397 LOC) -- the distinct-source collision measurement
  (DuckDB spill-backed) and `enforce_pool_quality` per-column threshold check. This is the
  leak-gate PRIMITIVE (see section 0 for what "leak-gate" does and does not mean here).
- `execution/native/_route_diagnostics.py` (206 LOC) -- per-invocation warning isolation over the
  append-only `PoolCache` warning log (length-prefix baseline) and caller-attributed pool owners.
- `generation/pool/_identity.py` (58 LOC) -- the shared faker-pool identity resolver used by the
  oracle handler, the native route, and `_chunked._warm_faker_pools`.

Changed units (grade the CHANGED lines):

- `execution/native/_dispatch.py` (+251) -- the native faker branch (`_mask_chunk_native` faker
  path: pool lookup/build, deterministic selection seed, namespace/scale, the `native_pool` route
  tag, the pool_select counters, positional null restore, `pool_cache` reuse) and the preflight
  string-type scope-lock guard.
- `execution/native/_requirements.py` (+81) -- the `NATIVE_POOL_STRATEGIES` seam and the JC-5
  admission guard.
- `execution/_chunked.py` (+19) -- `_warm_faker_pools` converged onto the shared identity resolver.
- `execution/_strategies/_faker.py` (+40) -- the oracle faker handler converged onto the shared
  identity resolver (graded for parity-anchor equivalence, since it is the native route's oracle).

Unchanged dependency, graded transitively (NOT a mutation unit): `generation/pool/_cache.py`. Its
`origin/main..HEAD` diff is COMMENT-ONLY (an expanded rationale on the `pool_dominates_cache`
append-per-emission; the `if pool_bytes > threshold` / `.append` logic is byte-identical to Phase
2). Grading it as a changed semantic unit would distort the denominator. Its append-per-emission
behavior is exercised transitively by `_route_diagnostics`'s isolation tests; its known monotonic-
log growth under evict-then-rebuild is a tracked pre-existing Phase 0 follow-up, out of scope here.

Out of scope: the pandas oracle and the Phase 2 native masking/route surface (unchanged, the
parity reference); the Rust crate (Phase 2 batches); the non-deterministic default C1 recipe (it
stays on the oracle by design, JC-5); the JC-3 perf/RSS certification (Task 3.6 bench, a separate
Cam-gated decision, NOT a correctness item); the OPERATIONAL leak-gate wiring and its
breach-blocks-publication test (Task 3.5 + prod-sim, held on `streaming-flip`; see section 0).

Independence, stated precisely: this plan is independent of the pending JC-3 perf-threshold
decision (it grades correctness/safety, not perf). It is independent of the platform leg ONLY for
primitive correctness; the operational safety OUTCOME is NOT independent of it and is deferred, not
claimed here.

## 2. What already exists (do not duplicate)

Phase 3's build landed real tests; this plan extends them. Faker-only (no compiled companion):
`test_phase3_eligibility.py` (the eight coded reasons + JC-5 coercion), `test_pool_quality.py`
(measurement + enforcement branches), `test_c1_bounded_state.py` (the `PoolCache` byte-bound +
`RouteDiagnostics` isolation adversary), `test_c1_diagnostics.py`. Companion-required (gated by
`@_NEEDS_COMPANION`): `test_dispatch_faker.py` (the faker route + preflight reroute),
`tests/parity/native/test_c1_faker_parity.py` (exact parity vs the frozen recipe),
`tests/parity/native/test_phase3_c1_gate.py` (the frozen four-criterion C1 gate). Because so much
exists, every batch below states CANDIDATE gaps only.

## 3. Phase 3-specific substrate wrinkles (binding for the harness batch)

Two Phase 3 units break assumptions the Phase 2 mutation harness was tuned for. Both are
harness-correctness issues settled in P3-T0 before any unit is graded, or every later number is
suspect:

1. **`_pool_quality.py` runs a DuckDB filesystem spill.** DuckDB is an IN-process extension (not a
   separate database process) that spills aggregation to a temp `temp_directory` under a
   `memory_limit` + threads clamp. Whether that path false-timeouts under mutmut (as the pandas/Arrow
   substrates do in the Phase 2 record) is an EMPIRICAL question, not an assumption: P3-T0 measures
   it by running a known-semantic `_pool_quality` threshold mutant and recording whether it is
   KILLED or TIMED-OUT, then classifying any timeout via a standalone rerun. The must-grade
   comparisons live in free functions (not decorated-class bodies) so mutmut reaches them; restate as
   a free helper any that it does not.
2. **`_route_diagnostics.py` isolation is order-dependent WITHIN a test.** The collector isolates one
   invocation's warnings via a length-prefix baseline over the append-only `PoolCache.warnings()`
   log; the baseline must be captured BEFORE the invocation it isolates. This is a TEST-CONSTRUCTION
   precondition (construct the collector before dispatch; do not interleave two invocations sharing
   one cache within a single test), NOT a claim about mutation-worker parallelism -- mutmut workers
   normally run in separate processes with their own caches and so do not interleave shared-cache
   invocations. P3-T0's job here is only to (a) confirm the test-construction precondition is honored
   in the existing `_route_diagnostics` tests, and (b) determine whether the actual mutation runner
   shares a process/cache across the `_route_diagnostics`/`_cache` module; require serial mutation for
   those modules ONLY if it demonstrably does. Do not assert a sequential-worker requirement the
   runner's process model does not create.

## 4. Batches

Each batch: measure (branch coverage + mutation on the changed lines) -> fill demonstrated gaps ->
re-measure to the bar -> dennis + Codex gate -> hold.

- **P3-T0 Harness delta (precondition, before any measurement).** Phase 3 is Python-only, so reuse
  the Phase 2 standalone-pytest-per-mutant runner and the `only_mutate` + focused-selection
  convention; do NOT stand up a new harness. What P3-T0 must newly PROVE:
  - The two lanes are correctly split: a faker-only slice (`_phase3_eligibility` / `_identity` /
    `_pool_quality`, no compiled companion) grades under the plain `.[dev]` venv, and the
    companion-required slice (`_dispatch` faker+hash ledger, the C1 gate) grades only under a build
    WITH the compiled companion, so a `@_NEEDS_COMPANION`-skipped test is never miscounted as
    covering a line it skipped.
  - Substrate wrinkle 1 (empirical): produce one KILLED and one SURVIVING `_pool_quality` mutant, and
    reproduce + classify (standalone rerun) any DuckDB-path false-timeout, recording the per-mutant
    time limit that distinguishes a true survivor from a timeout.
  - Substrate wrinkle 2 (process-model check): confirm the `_route_diagnostics` test-construction
    precondition holds, and determine empirically whether the mutation runner shares a
    process/cache for that module; pin serial mutation only if it does.
  - Record exact tool versions + bootstrap commands (reuse the Phase 2 T0 record's).
  Gate P3-T0 on: both lanes demonstrably distinguish killed from surviving; the DuckDB timeout (if
  any) is reproduced and classified; the `_route_diagnostics` process model is established (serial
  requirement asserted only if real); all with recorded commands.

- **P3-T1 Pool identity + C1 eligibility (route-integrity + identity-semantics bar).**
  `_identity.py` + `_phase3_eligibility.py` + the `_requirements.py` JC-5 seam. Candidate gaps:
  - a property sweep over the JC-5 config shape (`deterministic` x `cardinality_mode` x `namespace`
    presence x `pool_size` presence x `allow_collisions`) proving the admitted set is EXACTLY the
    deterministic, source-keyed, partition-independent configs and nothing else;
  - each of the eight coded rejection reasons (`faker_not_deterministic`,
    `faker_cardinality_not_partition_independent`, `faker_namespace_required`,
    `faker_pool_size_required`, `provider_not_pool_native`, `provider_not_in_c1_allowlist`,
    `provider_reject_large`, `faker_config_shape_unsupported:{vault,when}`) fires on its OWN trigger
    and only its own, and the reason list is stable-deduped and ORDERED (a duplicate faker
    declaration is not collapsed into hiding an unsafe column);
  - the `allow_collisions` -> deterministic/reuse coercion AND the `allow_collisions_mode_conflict`
    rejection when `allow_collisions:true` meets an explicit non-reuse mode;
  - IDENTITY SEMANTICS, graded against an INDEPENDENT reconstruction, not caller convergence: the
    three-caller equality (oracle handler / native route / `_warm_faker_pools`) proves only that
    they agree, so a mutant INSIDE `_identity` that changes all three identically would survive it.
    Grade the resolver against a hand-written reconstruction of the frozen identity contract and kill
    every mutant that changes the resolved provider, pool_size, locale, build-config hash, namespace,
    or job_seed (each is a distinct-pool determinant -- `resolve_faker_pool_identity` feeds all of
    them, provider included, into `builder.identity_for`; a wrong one silently reuses or rebuilds the
    wrong pool). Vary provider independently in the reconstruction so a wrong-provider identity cannot
    survive on caller convergence.
  Bar: kill every mutant that changes an admission verdict, a coded rejection reason, or an identity
  determinant. Adjudicate message-text-only survivors.

- **P3-T2 Pool-quality measurement + enforcement (zero-unadjudicated-survivor bar).**
  `_pool_quality.py`, the leak-gate primitive. It carries the crypto/RI-tier bar. Candidate gaps:
  - the distinct-source collision-rate and pool-duplicate arithmetic as a DIFFERENTIAL property vs a
    pure-Python reference. The reference MUST compute from the RAW `(source, masked)` pairs and RAW
    pool values only; it must NOT consume `PoolQualityMeasurement`, the production threshold
    constants, or `ValuePool.distinct_count` (that would grade the code against itself). It must
    follow the FROZEN contract, not mirror production: the frozen baseline population is "non-null
    SOURCE rows" and its frozen SQL filters `source IS NOT NULL` ONLY (PHASE3-C1-BASELINE.md), but
    production `_COLLISION_SQL` DRIFTED to `source IS NOT NULL AND masked IS NOT NULL`. That drift is
    fail-open in direction (dropping a non-null-source/null-masked row shrinks the `distinct_sources`
    denominator and can turn a real collision population into a smaller-or-empty pass). The oracle
    follows the frozen `source IS NOT NULL` semantics, and P3-T2 must ADJUDICATE the drift, not bake
    it in: prove the extra `masked IS NOT NULL` filter is behaviorally INERT on the deterministic
    route (i.e. a null masked output occurs only where the source is null too, because null-restore is
    positional from the source) OR that it is a real divergence. If inert, record the proof as an
    equivalent-mutant justification. If divergent, it is a fail-open production bug: restore the
    frozen `source IS NOT NULL`-only SQL, or escalate a formal re-freeze of the baseline population to
    Cam (a frozen Task-0-gated contract change is Cam's call, the same class as the JC-3 decision), and
    do NOT let P3-T2 pass against the drifted semantics until one of those happens. The reference must
    also model duplicate sources, nondeterministic mappings (source -> multiple outputs), empty inputs,
    and raw pool-value counting;
  - every fail-closed branch proven to RAISE `PoolQualityError`, not admit: a non-finite rate, a rate
    outside [0,1], `measurement.column != requested column`, an unrecognized obligation, a column not
    in the frozen set, and the `non_deterministic_sources != 0` integrity path;
  - TUPLE-AWARE threshold fail-closed (BLOCKER from the plan gate): enforcement selects the threshold
    by COLUMN only, but the frozen thresholds were measured at the C1 recipe's fixed `(population,
    pool_size)` tier. A pool with a DIFFERENT pool_size or distinct-source population than the frozen
    tier has a different collision floor, so applying the frozen per-column threshold is fail-open
    (a larger pool_size lowers the floor; the stale threshold is too lenient). P3-T2 must test the
    mismatched-population, mismatched-pool_size, and unsupported-tier cases and require them to be
    FAIL-CLOSED (reject as a coded integrity failure, or select a tuple-correct threshold); silently
    applying a column threshold to a mismatched tier is a non-adjudicable leak-gate survivor. If the
    current code does not fail closed here, this batch's finding forces the code fix before the bar
    is met;
  - the per-column threshold comparison is `>` (strict) not `>=`: a pool exactly at the threshold is
    admitted, one a hair above rejected; both boundary mutants must die;
  - EXCEPTIONAL-CLEANUP, at the right seam (HIGH from the plan gate): the spool is unlinked in
    `measure_pool_quality`'s own `finally` BEFORE it returns, so "deleted even when enforce raises"
    is vacuous (enforce runs afterward). Inject faults at the real measurement seams -- `pq.write_table`,
    `conn.execute`/`fetchone`, and `conn.close` -- and assert the spool is unlinked AND the connection
    closed on each. Also address the fixed `pairs_{column}.parquet` name: two concurrent measurements
    of the same column would collide, so require either a unique per-measurement path or an explicit
    single-measurement exclusivity contract, and test whichever is chosen;
  - `connect_duckdb` is called directly (not a forked copy): a mutant dropping the
    threads-vs-`memory_limit` clamp or the `0o700` temp-dir mode is killed or adjudicated
    tool-excluded with a reason.
  Bar: zero unadjudicated semantic survivors on the measurement arithmetic, the tuple-aware
  threshold selection, and every enforcement branch.

- **P3-T3 Dispatch faker branch + route diagnostics (route-integrity + masking-output + fail-closed
  bar).** The `_dispatch.py` faker branch + preflight string-type scope-lock guard + the counters;
  `_route_diagnostics.py`. Candidate gaps:
  - MASKING OUTPUT (broadened per the plan gate): the faker branch selects the actual masked values,
    so grade the SELECTION, not just the route metadata. Kill every mutant that changes a selected
    value, the deterministic selection seed or its separation from other seeds, the deterministic
    mode / namespace / scale fed to the sampler, the resolved pool identity, the pool cache-hit vs
    rebuild decision, the raised exception type/code, or the output Arrow type. The selected values
    are graded against the pandas oracle over a source with repeated and null keys (never against a
    native-produced golden);
  - the preflight string-type guard reroutes the WHOLE table to the oracle when ANY faker source
    column is not `utf8`/`large_utf8`, coded `faker_source_type_not_string:{col}:{type}`: test the
    utf8 and large_utf8 accept AND the nullable-Int64 reject that motivated the guard, asserting
    whole-table reroute, not per-column;
  - the route ledger must be INDEPENDENTLY OBSERVED, not synthesized (BLOCKER from the plan gate):
    the existing gate reconstructs a Cartesian `node_routes x emitted-chunks` ledger and the runtime
    evidence carries only aggregate counters, so a per-`(table, column, chunk)` mis-route could
    survive. P3-T3 must upgrade the evidence to emit independently-observed per-identity events
    (each `(table, column, chunk)` recorded once, with attempted-vs-completed separation) and grade
    the ledger against THOSE events, not against the routing intent it was derived from. If the
    evidence does not emit per-identity events, this batch adds that instrumentation to `_dispatch`
    (small, graded by its own test). Counters advancing before a chunk is yielded must be
    distinguishable from a chunk that completed;
  - the positional null-restore after pool selection preserves null positions byte-for-byte vs the
    oracle over a null-dense source;
  - `RouteDiagnostics` per-invocation isolation via the length-prefix baseline holds under the
    rebuild-churn adversary (already in `test_c1_bounded_state.py`) AND is bounded by the count of
    DISTINCT dominating pools, not the re-put count; row-error handling is fail-closed;
  - LATER-CHUNK DRIFT must fail CODED, not silently corrupt (MEDIUM from the plan gate): the route
    returns a LAZY per-chunk iterator, so "no partial output" holds ONLY for preflight-detectable
    faults. Dispatch iterates the columns PRESENT in each chunk, so a later chunk missing a configured
    column is silently skipped -- and this is NOT faker-specific: a missing, extra, or type-changed
    column of ANY admitted strategy (hash, passthrough, redact, truncate) in a chunk AFTER the first
    can be silently yielded. Require a before-yield schema sentry over the COMPLETE admitted table
    schema and every admitted strategy that raises a coded failure BEFORE the drifting chunk is
    yielded; do NOT promise rollback of already-consumed earlier chunks, and do NOT let a drift pass
    as silent corruption.
  Bar: kill every mutant that changes a selected value, a selection determinant, a route tag, a
  per-identity ledger event, the preflight-reroute verdict, the null-restore, the isolation, or the
  drift failure code.

- **P3-T4 End-to-end C1 gate + parity consolidation (right-sized).** Confirm
  `test_phase3_c1_gate.py` (the four frozen criteria) and `test_c1_faker_parity.py` are the frozen
  e2e for this surface; measure their branch-coverage contribution to the changed units and fill any
  changed line neither a unit batch nor the gate reaches. Assert no changed unit is graded ONLY by a
  golden it produced (the logical grader is the pandas oracle / the frozen recipe). Re-run the NAMED
  Phase 0 determinism sentries (`tests/native/test_determinism_goldens.py` + the draw-site inventory
  tests) and assert no fingerprint moves -- a bounded, named claim, not "anywhere in the program."
  This batch certifies CORRECTNESS only: it does NOT wait on the JC-3 perf decision, and it does NOT
  substitute for the operational breach-blocks-publication test that section 0 defers to Task 3.5.

## 5. Acceptance

- Per unit: the five denominator fields recorded (branch coverage %, killed semantic mutants,
  equivalent mutants with a one-line justification each, unreachable-by-contract mutants,
  tool-excluded code with its grading method), with the `_pool_quality` zero-unadjudicated-survivor
  bar met (including the tuple-aware threshold selection) and every other survivor adjudicated in
  writing.
- Every property test demonstrably able to fail (a seeded mutation makes it red).
- The pandas oracle / frozen recipe is the only grader of logical output; no self-grading path
  exists -- including the DuckDB collision measurement (graded vs a raw-input pure-Python reference)
  and `_identity` (graded vs an independent contract reconstruction, not caller convergence).
- The route ledger is graded against independently-observed per-identity events, not a Cartesian
  reconstruction from routing intent.
- The two Phase 3 substrate wrinkles are honored in every graded number: the companion lane split,
  and the empirically-established `_route_diagnostics`/`_cache` process model.
- Scope honesty (section 0): this plan claims PRIMITIVE correctness only; the operational
  breach-blocks-publication certification is recorded as deferred to Task 3.5 + prod-sim, not
  implied as met.
- dennis + Codex GO per batch; nothing weakens an existing gate or a frozen Phase 2/Phase 3 target
  (including the frozen four-criterion C1 gate).

## 6. Sequencing note

This runs on `feat/native-phase3` (held, stacked on the merged Phase 2 main). It does not itself
merge. It is independent of the pending JC-3 perf-threshold decision. It is independent of the
platform leg ONLY for primitive correctness; the operational leak-gate outcome is deferred to Task
3.5/prod-sim (section 0), not claimed here. P3-T0 gates the rest: no measurement batch starts until
the two-lane split and the two substrate wrinkles are established, with recorded commands.
