Status: plan

# Part 1 Phase 3: C1 masking vertical slice (bounded Faker pools + native source-keyed selection)

Roadmap parent: `docs/plans/2026-08-26-engine-efficiency-plan.md`, Part D "Part 1 Phase 3".
Decision anchor: master-plan Decision 5 (limit Phase 3 to the C1 masking path; bounded pools only
for the poolable Faker providers C1 uses; native source-keyed selection; add only the cache and
state this path uses).

Revision note: this is revision 2, remediating the Codex plan-gate (round 1 NO-GO). The round-1
findings are addressed at root here; the material changes are the deterministic-variant scope
decision (JC-5, from BLOCKER 1), the frozen `pool_quality` metric definition (BLOCKER 2), the
exact-count route ledger (BLOCKER 3), shared pool-identity reuse (HIGH 1), the DE-02 seam test
(HIGH 2), the chunk warning/error aggregation contract (HIGH 3), route-local scope guards
(HIGH 4), and the reworded rationale (MEDIUM 1 / LOW 1).

## What Phase 3 changes, and the corrected rationale

C1's Faker masking is already pool-backed on the pandas oracle. `FakerStrategyHandler`
(`execution/_strategies/_faker.py`) builds a bounded value pool once through `PoolBuilder`, caches
it in a 256 MB LRU `PoolCache`, and calls `PoolSampler.sample(...)` once per column. There is no
per-row Faker provider call on the hot path: the provider is amortized through the pool.

Two corrections from the gate, carried into this plan so no task inherits an overclaim:
- The deterministic selection inside `PoolSampler` is a single batched sampler API that contains a
  Python per-row loop, one `derive_index(seed, namespace, canonical(source), pool.size)` per
  non-null row (`generation/pool/_sampler.py`). It is not SIMD-vectorized, and `derive_index` is
  not proven cheap until Task 3.1 Step 0 measures it.
- The working diagnosis of C1's ~12.7x peak is full-frame residency (the whole table held in
  pandas) PLUS the whole-column sampler temporaries the handler allocates per column
  (`source.tolist()`, a null mask, an output list, a sampled Series, the assignment list). Task 3.0
  stages the RSS measurement into buckets (input residency, pool build, selection, publication) so
  the claim rests on evidence, not assertion. Chunking addresses both whole-table residency and the
  O(n) per-column temporaries, so the slice still delivers the memory win.

The win Phase 3 delivers: route the C1 masking onto the bounded-memory chunked native route Phase
1/2 built, with the pool built once and selected per chunk. Because deterministic source-keyed
selection is partition-independent, per-chunk selection reproduces the whole-column result exactly.

## The determinism precondition (JC-5, the load-bearing scope decision)

Only the DETERMINISTIC source-keyed sampler path is partition-independent. The other sampler modes
are not, and per-chunk execution would diverge from the whole-frame oracle:
- Non-deterministic selection builds a fresh `default_rng(seed)` per `sample()` call, so it repeats
  the same RNG sequence at every chunk boundary.
- UNIQUE cardinality repeats a permutation prefix per chunk and can violate global uniqueness;
  deterministic UNIQUE is explicitly unsupported by the sampler.
- MATCH/SCALE recompute source cardinality and the chosen value mapping from each chunk's LOCAL
  source set, so per-chunk results do not agree with the whole-column mapping.

The frozen C1 recipe (`mask-fullframe-saturate`) writes its three faker columns with NO
`deterministic:`, NO `namespace:`, and NO `pool_size:`. Those resolve to `deterministic=False` and
`cardinality_mode="reuse"` (`plan/_seed_envelope.py:160,191`). The chunked route requires
`deterministic: true` + `namespace` + explicit `pool_size` (`execution/_chunked.py:39,152`).
Therefore the recipe AS WRITTEN is not chunk-compatible, which is exactly why Phase 1 rejects it.

JC-5 (flagged for Cam): Phase 3 targets the DETERMINISTIC source-keyed C1 masking variant. The C1
target recipe for Phase 3 is the same two-table workload with `deterministic: true` + `namespace` +
explicit `pool_size` on the three faker columns. The original non-deterministic default C1 stays on
the pandas oracle permanently (its selection is not partition-independent; supporting it would need
cross-chunk RNG state, which is Phase 4 global work and out of Decision 5's scope). RECOMMENDATION:
confirm the Phase 3 C1 target is the deterministic variant. This is the honest reading of Decision 5
("native source-keyed selection" is the deterministic path; "add only the cache and state this path
uses" excludes cross-chunk RNG state). If Cam instead wants the non-deterministic recipe supported,
that is a different, larger slice and this plan does not cover it.

Everything below assumes JC-5 resolved as recommended. Task 3.3 eligibility ENFORCES the
precondition: it admits only `deterministic: true`, non-UNIQUE (`reuse`) faker columns with a
namespace and explicit pool_size, and rejects every other combination coded, with negative tests.

## Scope lock (Decision 5)

IN scope, deterministic C1 only:
- The deterministic C1 variant: two independently-masked tables, no `relationships:`; `patients`
  with three deterministic faker columns (`FIRST`=`person_first_name`, `LAST`/`MAIDEN`=
  `person_last_name`, each `deterministic: true` + namespace + pool_size) plus six hash columns;
  `observations` with two hash columns. All three Faker providers are poolable (`pool_native`).
- Native chunked execution of deterministic faker masking for exactly those poolable provider IDs.
- The bounded pool cache and the `pool_quality` enforcement state this path uses, nothing more.

OUT of scope (stays on oracle / Part 2+), enforced by eligibility rejections with distinct codes:
- Non-deterministic faker (JC-5), UNIQUE/MATCH/SCALE cardinality, missing namespace/pool_size.
- Synthetic `generate_columns` and synthetic per-row Faker (Part 2).
- Other `pool_native` families (composites, address_full, DecoyNative identifiers beyond C1,
  Mimesis), vault, custom/reference pools, `joint_mask`.
- Nonpoolable Faker (`python_only`), arbitrary custom callables (`reject_large`).
- `mask.shuffle` and any global/relational strategy (Phase 4).

## Preconditions and dependencies

1. Phase 2 stacked and held at `feat/native-phase2-task2.7` (9528ad74). Phase 3 stacks on it.
2. Phase 0 primitives present on the stack and merged to engine main: `PoolBuilder`/`PoolCache`/
   `PoolSampler`, `classify_provider`, the three C1 draw-site protocol entries, the pinned-oracle
   parity harness.
3. Phase 2 native dispatch is the integration surface: `execution/native/_dispatch.py`,
   `_requirements.py` (`_STATE_TABLE_BY_STRATEGY` already maps `faker -> value_pool`), `_plan.py`
   (`native_route_eligibility`), `_capabilities.py`.
4. PLATFORM DEPENDENCY (blocks Task 3.5 and the platform leg of 3.6 only): platform
   `phase1_eligibility` lives only on the unmerged `streaming-flip` worktree. It is HELD for Cam's
   review of the A/B peak-RSS numbers. Engine Tasks 3.0 through 3.4 and the engine leg of 3.6 do
   not depend on it (JC-4).

## Judgment calls flagged for Cam / Codex

- JC-1 (selection engine). Selection is a Python per-row `derive_index` loop inside one sampler
  call. The memory win comes from streaming, not selection speed. RECOMMENDATION: reuse the Python
  sampler per chunk; build a Rust `derive_index` batch kernel ONLY if Task 3.1 Step 0 shows Python
  per-chunk selection misses the JC-3 wall bound. Do not prejudge the measurement.
- JC-2 (pool_quality metric). Defined concretely below in Task 3.2; the metric DEFINITION and
  threshold methodology are frozen in Task 3.0 BEFORE the number is observed, so the threshold is
  not tautological. Codex should confirm the frozen metric is non-vacuous and not stricter than the
  oracle.
- JC-3 (perf target). HARD: peak RSS bounded and flat across tiers (executable thresholds in Task
  3.0). Wall-clock is a hard non-regression criterion (not a warning), bound and method frozen in
  Task 3.0. Both are gate-failing, not advisory.
- JC-4 (platform sequencing). Engine Tasks 3.0-3.4 + engine leg of 3.6 build now; Task 3.5 + the
  platform leg of 3.6 wait for Cam to land streaming-flip. Two distinct completion statuses
  (Task 3.6).
- JC-5 (deterministic-variant scope). Above. The single decision the rest of the plan rests on.

Doc-accuracy: the master plan calls the registry "34-provider"; the live catalog is 24 to 26
bindings. This plan never quotes 34; Task 3.3 asserts totality against the live registry.

---

## Task 3.0: Freeze the deterministic C1 variant + staged pinned-oracle baseline (measure first)

Produces the frozen baseline the Task 3.6 gate compares against, the staged RSS evidence that
grounds the rationale (MEDIUM 1), and the frozen metric definitions that make JC-2 and JC-3
non-tautological (BLOCKER 2, MEDIUM 3).

Files:
- Create: `docs/plans/PHASE3-C1-BASELINE.md`, `scripts/native-baseline/bench_c1_oracle.py`.
- Create/record: the deterministic C1 recipe variant (frozen verbatim in the baseline doc).

Steps:
1. Freeze the deterministic C1 recipe variant verbatim: the two-table workload with the three faker
   columns carrying `deterministic: true`, an explicit `namespace` per column, and an explicit
   `pool_size`. Record the resolved sampler settings for every faker column (`deterministic`,
   `cardinality_mode`, `pool_size`, `scale`, `locale`, `namespace`) so Task 3.3 can enforce and
   Task 3.1 can reproduce them exactly. State explicitly that the non-deterministic default C1 stays
   on the oracle (JC-5).
2. Freeze the dataset tiers: a small parity tier and the representative large memory tier, with
   exact row counts and column shapes.
3. Build the C1 data out of band (separate, non-timed, non-sampled process, drop each batch after
   write), as Phase 2's `build_w2_parquet.py` does.
4. Run the deterministic C1 recipe on the PINNED oracle (`substrate="pandas"`,
   `execution_mode="full_frame"`, `auto_chunk=False`) in a fresh process under external `VmHWM`
   sampling, STAGED so peak RSS is attributed to buckets: input residency, pool build, selection,
   publication. Record end-to-end latency, hash throughput, Faker (selection) throughput, per-bucket
   and total peak RSS, and spill.
5. Freeze the `pool_quality` metric DEFINITION and threshold methodology BEFORE reading the number
   (BLOCKER 2). Specify exactly: the population measured (the pooled OUTPUT values over non-null
   source rows of an admitted deterministic faker column); the metric = distinct-source collision
   rate, defined as `(non-null source values that are distinct but map to an already-used pool
   value) / (distinct non-null source values)`, EXCLUDING intentional repeats where equal source
   values map to the same output (deterministic reuse is correct, not a collision); separately, the
   pool-duplicate rate = `(duplicate values in the built pool) / pool_size`; the UNIQUE-feasibility
   check applies only if an admitted column is UNIQUE-cardinality (for the reuse-only C1 scope it is
   vacuous and recorded as N/A, not silently passed); the threshold is derived from the oracle run
   with a stated margin, the sample tier, and deterministic reproducibility (fixed seed). Record the
   methodology now; the numeric threshold is filled from Step 4's measurement.
6. Freeze the JC-3 performance gate as executable thresholds (MEDIUM 3): tier sizes, chunk size,
   warmup count, repetition count, the reported statistic (median) and variance policy (IQR), the
   absolute peak-RSS ceiling, the permitted RSS slope/ratio across tiers (flatness bound), and the
   wall non-regression ratio (proposed <= 1.25x oracle median) as a HARD fail, not a warning.
7. Commit: `docs(native): freeze deterministic C1 variant + staged pinned-oracle baseline`.

Acceptance: deterministic C1 recipe + resolved sampler settings frozen; staged RSS buckets recorded;
`pool_quality` metric definition + methodology frozen before the number; JC-3 thresholds executable.

## Task 3.1: Native chunked deterministic-faker masking (pool once, select per chunk)

Files:
- Modify: `execution/native/_dispatch.py` (`_mask_chunk_native` gains a `faker` branch; per-table
  pool resolution threaded through `run_native_or_oracle_chunked`).
- Modify: `execution/native/_requirements.py` (a dedicated `NATIVE_POOL_STRATEGIES` set for `faker`,
  distinct from `NATIVE_KERNEL_STRATEGIES` which means "a compiled scalar kernel exists").
- Create: a shared pool-resolution function reused by BOTH the oracle handler and the native route
  (HIGH 1), extracted from `FakerStrategyHandler` so the two sides cannot drift.
- Test: `tests/parity/native/test_c1_faker_parity.py`, `tests/native/test_dispatch_faker.py`.

Steps:
0. MEASURE (JC-1 gate): micro-measure vectorized-Python per-chunk `PoolSampler` selection against
   Task 3.0's Faker throughput. If it holds the JC-3 wall bound, proceed with Python; if not, record
   the miss and escalate JC-1 before adding any compiled path.
1. Strategy-set seam: add `faker` via `NATIVE_POOL_STRATEGIES`; a node is native when it is a scalar
   node whose strategy is in `NATIVE_KERNEL_STRATEGIES` OR `NATIVE_POOL_STRATEGIES`. Keep the two
   admission reasons distinct.
2. Pool identity (HIGH 1): extract the oracle's exact identity computation (provider, resolved
   pool_size, job_seed, locale, filtered build_config, namespace) into one shared function and call
   it from BOTH sides. Define "build once" as once per unique `PoolIdentity`, not once per column.
   Build/resolve the pool before the chunk loop via `identity_for` then `PoolCache.get`/`build`.
3. Dispatch branch: in `_mask_chunk_native`, add the `faker` branch running
   `PoolSampler.sample(pool, n, mode=reuse, seed=select_seed, source=chunk_source,
   namespace=plan.namespace, deterministic=True, scale=scale)` with
   `select_seed = mask_key` (the DE-02 seam: build stays on job_seed, deterministic selection
   re-keys onto mask_key). Assign results POSITIONALLY with oracle-equivalent null normalization
   (MEDIUM 2): source-null positions become None and the output is assigned by position, never by a
   Series label-aligned to a non-zero chunk index.
4. Parity (BLOCKER 1 scope): assert exact logical parity (values, order, null placement, warnings)
   between the native chunked route and the pinned oracle over the deterministic C1 faker columns,
   across multiple batch sizes and table orders, including chunks with non-zero/non-contiguous
   indices and mixed None/pd.NA/NaN nulls. Because deterministic selection is partition-independent,
   per-chunk equals whole-column exactly.
5. DE-02 seam test (HIGH 2): assert that changing ONLY `mask_key` keeps the identical built pool but
   changes the deterministic selections, and changing ONLY `job_seed` changes the pool identity and
   build. Run across warm and cold cache and multiple batch sizes.
6. Pool-cache correctness (HIGH 1): test warm and cold cache, reversed table order, repeated
   provider IDs across columns, distinct namespaces, build_config key-ordering invariance, explicit
   null pool_size, and forced eviction, asserting the pool is built exactly once per unique identity
   and shared correctly across chunks.
7. Route evidence: record a per-column-chunk `pool_select` runtime counter (feeds Task 3.6's
   ledger).
8. Commit: `feat(native): chunked deterministic-faker masking on the native route (pool once)`.

Acceptance: exact parity across batch sizes/orders/null shapes; pool built once per unique identity;
DE-02 seam proven; positional null assignment; per-column-chunk route counters recorded.

## Task 3.2: `pool_quality` obligation enforcement (route-local)

Files:
- Create: `execution/native/_pool_quality.py`.
- Modify: `execution/native/_capabilities.py` (enforce `pool_quality` ONLY on the C1 native route;
  preserve existing behavior for every other obligation and every non-C1 path, HIGH 4).
- Test: `tests/native/test_pool_quality.py`, plus a non-interference test.

Steps:
1. Encode the Task 3.0 frozen metric (BLOCKER 2) as named constants with docstrings citing the
   baseline run: the distinct-source collision-rate bound, the pool-duplicate-rate bound, and the
   UNIQUE-feasibility check (N/A for reuse-only C1, recorded explicitly, not silently passed).
2. `enforce_pool_quality(evidence, *, tolerance)` reads the emitted `PoolCache.warnings()` /
   `QualityWarning` plus the measured pooled distribution and raises a coded `PoolQualityError`
   before publication when the frozen tolerance is exceeded.
3. Route-local guard (HIGH 4): enforcement runs ONLY when the C1 phase3 eligibility admitted the
   column. Do NOT change the general capability resolver's behavior for non-C1 obligations or
   non-C1 paths. Reject an unrecognized obligation on the C1 route coded; leave all other routes
   untouched.
4. Non-vacuity + non-interference tests: a pool whose collision rate exceeds the bound MUST raise; a
   compliant pool MUST pass; a non-C1 obligation on a non-C1 path MUST behave exactly as before this
   task (assert against the pre-task behavior).
5. Commit: `feat(native): route-local pool_quality enforcement with frozen C1 tolerance`.

Acceptance: tolerance concrete and sourced to Task 3.0; raises on breach, passes compliant; no
behavior change for non-C1 obligations or paths (non-interference test green).

## Task 3.3: Config-aware `phase3_c1_eligibility` engine predicate (enforces the JC-5 precondition)

Files:
- Create: `execution/native/_phase3_eligibility.py`.
- Test: `tests/native/test_phase3_eligibility.py`.

Steps:
1. Failing tests first: the deterministic C1 variant admits (both tables); a non-deterministic
   faker column rejects `faker_not_deterministic`; a UNIQUE/MATCH/SCALE cardinality rejects
   `faker_cardinality_not_partition_independent`; a missing namespace rejects
   `faker_namespace_required`; a missing/implicit pool_size rejects `faker_pool_size_required`; a
   nonpoolable faker rejects `provider_not_pool_native`; a non-C1 poolable provider rejects
   `provider_not_in_c1_allowlist`; a custom callable rejects `provider_reject_large`; an unsupported
   faker config shape rejects `faker_config_shape_unsupported`.
2. Implement as a pure function above `native_route_eligibility`. For every faker column: resolve
   the faker config, REQUIRE the partition-independent combination (`deterministic: true`,
   `cardinality_mode` in the partition-independent set (`reuse`), explicit `namespace`, explicit
   `pool_size`), call `classify_provider` and require `pool_native`, and admit only the exact C1
   poolable provider IDs and the config shape the C1 pool builder and native selector support.
   Non-faker columns defer to the Phase 1 predicate unchanged.
3. Reject every out-of-scope provider/config/mode with the distinct codes above, all before staging.
4. Totality against the live provider registry (no hardcoded count; the 24-to-26 correction).
5. Cross-check: Phase 1 and Phase 3 eligibility against the deterministic C1 variant. Phase 1 still
   rejects it for streaming (`strategy_not_allowlisted_for_streaming`, the real string); Phase 3
   admits it.
6. Commit: `feat(native): phase3_c1_eligibility enforcing the deterministic partition-safe combo`.

Acceptance: deterministic C1 admitted; every non-partition-independent or out-of-scope
column/config/mode rejected coded before staging; total over the live registry.

## Task 3.4: Bounded-state adversary + chunk diagnostic-aggregation contract

Covers bounded state AND the HIGH 3 warning/error aggregation contract (they share the
per-invocation state that must be proven bounded and correct).

Files:
- Create: `execution/native/_route_diagnostics.py` (invocation-scoped warning/error collectors,
  keyed by pool/table/column, deterministic ordering + dedup).
- Test: `tests/native/test_c1_bounded_state.py`, `tests/native/test_c1_diagnostics.py`.

Steps:
1. Diagnostic contract (HIGH 3): define invocation-scoped collectors that snapshot per-chunk
   `QualityWarning`s and row errors, attribute them by pool/table/column, deduplicate, and order
   deterministically, ISOLATED from any warnings a shared `PoolCache` accumulated on a prior
   invocation. Define exactly how chunk-local warnings and row errors become job evidence.
2. Diagnostic tests: a pre-populated cache (prior warnings), multiple chunks, multiple tables,
   repeated warnings (deduped), and a later-chunk row error (surfaced in job evidence), asserting
   parity of both the returned diagnostics and the persisted job evidence against the oracle.
3. Bounded-state adversary: drive a high-cardinality deterministic C1 input (many distinct sources,
   many chunks) and assert each state owner stays within budget: `PoolCache` evicts at its byte
   bound; the warning/error collectors do not grow unboundedly across chunks; DuckDB intermediates
   spill rather than balloon. Assert peak RSS stays flat with row count.
4. Commit: `test(native): bounded state + chunk diagnostic-aggregation contract for C1`.

Acceptance: diagnostic collectors bounded, deduped, deterministically ordered, isolated from prior
cache state, and parity-matched to the oracle; every state owner bounded under the adversary; peak
RSS flat with row count.

## Task 3.5: Platform `phase3_c1_eligibility` admission layer (gated on streaming-flip)

GATED on Cam landing streaming-flip (JC-4). Build on the streaming-flip base, not platform main.

Files:
- Create: `api/jobs/_phase3_eligibility.py` (on the streaming-flip base), consumed by `admission.py`.
- Test: platform eligibility + admission tests.

Steps:
1. Add a pure `phase3_c1_eligibility` layer above `phase1_eligibility`. Admit the deterministic C1
   variant at claim time; enforce `reject_large` at admission. SCOPE GUARD (HIGH 4): this task does
   NOT introduce a new generic small-job oracle-pricing policy. It preserves the existing admission
   behavior for every non-C1 job and only adds the C1 admit path. If an oracle small-job threshold
   is needed, it is a separate, explicitly-Cam-gated change, not part of this slice.
2. Phase 1 records its streaming-rejection reason; Phase 3 admits the same recipe before any source
   staging or native execution.
3. Record the selected AND executed Phase 3 route in the job evidence.
4. Commit on the streaming-flip base; do not fork a second `phase1_eligibility`.

Acceptance: deterministic C1 admitted at claim time; `reject_large` enforced; no change to non-C1
admission; selected and executed route both recorded.

## Task 3.6: C1 evidence gate + prod-sim (engine leg now; platform leg post-3.5)

The frozen gate. Four hard criteria; failing any one fails the gate.

Files:
- Create: `tests/parity/native/test_phase3_c1_gate.py`, `docs/plans/native-phase3-C1-gate.md`.
- Reference: `scripts/native-baseline/bench_c1_oracle.py`, the deterministic C1 prod-sim scenario.

Criteria:
1. EXACT parity: deterministic C1 native+streaming output equals the pinned oracle (values, order,
   nulls, warnings, row errors, logical schema), across batch sizes and table orders.
2. Seed stability + partition invariance: fresh processes with different batch boundaries reproduce
   identical output for the partition-independent deterministic faker selection; the DE-02 seam
   holds (Task 3.1 Step 5).
3. Bounded state + C1 fidelity: peak RSS within the Task 3.0 absolute ceiling and flat within the
   frozen slope bound; wall within the frozen non-regression ratio (hard); `pool_quality` within
   the frozen tolerance.
4. Intended-route proof via an INVOCATION-SCOPED ROUTE LEDGER (BLOCKER 3), not a single counter.
   Assert: `pool_select` count equals the exact number of admitted faker column-chunks; native hash
   count equals the exact hash column-chunk count; every expected table/column/chunk identity
   appears exactly once; oracle calls, oracle rows, fallback calls, fallback rows, and rejected
   chunks are all exactly zero; the selected route and the COMPLETED route both equal Phase 3
   native+streaming; counters are recorded only after successful native publication (attempted vs
   completed distinguished). Oracle completion, admission rejection, or any fallback fails the gate.

Steps:
1. Engine leg (now): run the frozen gate over the deterministic C1 recipe at the parity tier and the
   memory tier; record all four criteria in the certification doc with Task 3.0's staged baseline as
   the comparison. On pass, mark status "engine gate passed; platform certification pending"
   (MEDIUM 4).
2. Benchmark: run the Phase 2 bench harness on deterministic C1 (out-of-band build + lazy read +
   external VmHWM) at the frozen tiers per Task 3.0's method; record RSS/wall/throughput against the
   frozen baseline and JC-3 bounds.
3. Platform leg (after Task 3.5 / streaming-flip): run the deterministic C1 prod-sim end to end;
   confirm admission admits, native+streaming executes, and the route ledger records it. Only on
   this leg passing does the status become "Phase 3 C1 complete".
4. Commit: `test(native): C1 Phase 3 gate + certification (parity, bounded RSS, exact route ledger)`.

Acceptance: all four criteria PASS at both tiers (engine leg); two explicit completion statuses;
"Phase 3 C1 complete" only after the platform prod-sim leg passes on streaming-flip.

---

## Sequencing

1. Task 3.0 (baseline + frozen metrics) first.
2. Tasks 3.1, 3.2, 3.3 (engine slice); 3.1 gates JC-1 at Step 0; 3.3 enforces the JC-5 precondition.
3. Task 3.4 (bounded state + diagnostics) after 3.1/3.2.
4. Task 3.6 engine leg after 3.1 through 3.4.
5. Tasks 3.5 and the 3.6 platform leg gate on Cam landing streaming-flip (JC-4).

Every task stacked on the Phase 2 branches, each dennis + Codex gated, nothing merged or pushed
without Cam. Five calls held for Cam before the dependent tasks build: JC-5 (deterministic-variant
scope, load-bearing), JC-1 (selection engine, measured at 3.1 Step 0), JC-2/JC-3 (metric + perf
thresholds, frozen at 3.0), JC-4 (platform sequencing).

## Phase 3 acceptance (whole slice)

- Deterministic C1 masked on the native+streaming route with exact parity to the pinned oracle.
- Peak RSS within the frozen ceiling and flat across tiers; wall within the frozen non-regression
  ratio (the headline memory win, on evidence).
- `pool_quality` enforced within the frozen tolerance, route-local; no non-C1 behavior change.
- Bounded state + correct chunk diagnostic aggregation proven under a high-cardinality adversary.
- Phase 3 eligibility admits exactly the deterministic C1 poolable providers and rejects every
  non-partition-independent or out-of-scope column/config/mode coded.
- Intended-route proof via the exact-count route ledger; oracle completion, admission rejection, or
  fallback all fail the gate.
- 100M-row cap held; reviewed only after this slice's parity, bench, and prod-sim C1 complete.
