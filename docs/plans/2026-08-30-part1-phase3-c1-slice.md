Status: plan

# Part 1 Phase 3: C1 masking vertical slice (bounded Faker pools + native source-keyed selection)

Roadmap parent: `docs/plans/2026-08-26-engine-efficiency-plan.md`, Part D "Part 1 Phase 3".
Decision anchor: master-plan Decision 5 (limit Phase 3 to the C1 masking path; bounded pools
only for the poolable Faker providers C1 uses; native source-keyed selection; add only the cache
and state this path uses).

This plan is task-by-task and build-ready for the engine tasks. It follows the same discipline as
Phase 2: measure before optimizing, freeze a gate before implementing, keep everything stacked on
the Phase 2 branches, gate every task with dennis + Codex, merge nothing without Cam.

## What Phase 3 actually changes, and why

The recon that preceded this plan established one fact that reshapes the slice: C1's Faker masking
is ALREADY pooled and vectorized on the pandas oracle. `FakerStrategyHandler`
(`execution/_strategies/_faker.py`) builds a bounded value pool once through `PoolBuilder`, caches
it in a 256 MB LRU `PoolCache`, and selects a value per row with a SINGLE vectorized
`PoolSampler.sample(...)` call whose per-row `derive_index(seed, namespace, canonical(source),
pool.size)` is already source-keyed and already `partitionable=True`. There is no per-row Faker
call on the hot path.

So the measured ~12.7x memory blowup on the C1 recipe is NOT the masking compute. It is the
full-frame substrate: the pandas oracle route holds the entire table (both C1 tables) in memory at
once. The win Phase 3 delivers is routing C1 onto the bounded-memory chunked native route that
Phase 1 and Phase 2 already built for hash/redact/truncate/passthrough, and extending that route to
admit and execute `faker` with the pool built once and selected per chunk. Because
`derive_index` selection is partition-independent by construction, per-chunk selection reproduces
the whole-column result exactly.

This framing matters for the gate (below): the headline Phase 3 target is bounded, flat peak RSS
across dataset tiers, with exact parity to the oracle. Wall-clock is a non-regression bound, not
the headline. We do not claim a speed win we have not measured, and Task 3.0 captures the real
oracle baseline before any target is frozen.

## Scope lock (Decision 5)

IN scope, C1 only:
- The exact C1 recipe: `mask-fullframe-saturate` (two independently-masked tables, no
  `relationships:`, which is what routes it to full_frame today). `patients` has three `faker`
  columns (`FIRST` = `person_first_name`, `LAST` and `MAIDEN` = `person_last_name`) plus six `hash`
  columns; `observations` has two `hash` columns. All three Faker providers are poolable and
  classify `pool_native`.
- Native chunked execution of `faker` masking for exactly those poolable provider IDs.
- The bounded pool cache and the `pool_quality` enforcement state this path uses, nothing more.

OUT of scope (stays on the oracle / defers to Part 2), enforced by eligibility rejections:
- Synthetic `generate_columns` and synthetic per-row Faker (separate draw site, Part 2).
- Other `pool_native` provider families (composites, address_full, DecoyNative identifiers beyond
  what C1 uses, Mimesis), vault, custom/reference pools, `joint_mask`.
- Nonpoolable Faker (`python_only`) and arbitrary custom callables (`reject_large`).
- `mask.shuffle` and any global/relational strategy (Phase 4).

## Preconditions and dependencies

1. Phase 2 is stacked and held at `feat/native-phase2-task2.7` (9528ad74). Phase 3 stacks on it.
2. Engine Phase 0 primitives Phase 3 consumes are already merged to engine main and present on the
   stack: `PoolBuilder` / `PoolCache` / `PoolSampler` (`generation/pool/`), `classify_provider`
   (`execution/native/_provider_class.py`), the `gen.pool_build_faker` / `gen.pool_deterministic` /
   `mask.faker` draw-site protocol entries (`execution/native/_determinism_protocol.py`), and the
   pinned-oracle parity harness (`tests/parity/native/`).
3. Phase 2 native dispatch is the integration surface: `execution/native/_dispatch.py`
   (`run_native_or_oracle_chunked`, `_mask_chunk_native`), `execution/native/_requirements.py`
   (`NATIVE_KERNEL_STRATEGIES`, `native_kernel_rejection`, `_STATE_TABLE_BY_STRATEGY` which already
   maps `faker -> value_pool`), and `execution/native/_plan.py` (`native_route_eligibility`).
4. PLATFORM DEPENDENCY (blocking for Task 3.5 only): the platform `phase1_eligibility`
   (`api/jobs/_phase1_eligibility.py`) lives only on the unmerged `streaming-flip` worktree, not on
   platform main. Phase 3's platform admission layer sits above it, so Task 3.5 and the full
   end-to-end prod-sim (Task 3.6 platform leg) are gated on the streaming-flip merge, which is
   itself HELD for Cam's review of the A/B peak-RSS numbers. Engine Tasks 3.0 through 3.4 and the
   engine leg of 3.6 do not depend on it and proceed independently.

## Judgment calls flagged for Cam / Codex

These are the decisions this plan cannot settle unilaterally. Each has a recommendation; none is
built until resolved.

- JC-1 (selection engine). The source-keyed selection is already vectorized Python
  (`PoolSampler._deterministic` over `determinism.derive_index`). The memory win comes from
  streaming, not selection speed, and `derive_index` is not the hot loop. RECOMMENDATION: run the
  existing vectorized `PoolSampler` per chunk on the native route; do NOT build a Rust
  `derive_index` batch kernel for Phase 3. Add a compiled selector only if Task 3.1 Step 0
  measurement shows Python per-chunk selection misses the frozen wall non-regression bound. This
  keeps Phase 3 additive over Phase 2's Rust surface rather than growing it.
- JC-2 (C1 fidelity tolerance). `pool_quality` is a declared capability tag today with no
  validator and no tolerance constant. Phase 3 must DEFINE the frozen C1 fidelity tolerance that
  `pool_quality` enforces before publication. RECOMMENDATION: define it as (a) UNIQUE-draw
  feasibility satisfied for any UNIQUE-cardinality column (the existing `unique_capacity_ok`
  floor), and (b) a bounded repeat/collision rate for the pooled output at the frozen C1 tier,
  derived from the oracle run in Task 3.0 and pinned as a constant. Exact numeric bound proposed in
  Task 3.2 after the baseline is measured. This is a correctness-policy call; Codex should confirm
  the definition is neither vacuous nor stricter than the oracle already is.
- JC-3 (perf target shape). Because streaming trades whole-frame residency for bounded per-chunk
  residency, wall-clock can be flat or slightly higher than full-frame while peak RSS drops by an
  order of magnitude. RECOMMENDATION: the C1 gate's HARD target is peak RSS bounded and flat across
  tiers (within the Phase 1 streaming ceiling), plus exact parity and intended-route proof;
  wall-clock is a NON-REGRESSION bound (proposed <= 1.25x the oracle wall at the frozen tier),
  flagged and measured, not a speed-up claim. Final numbers set in Task 3.0 / 3.6 from real runs.
- JC-4 (platform sequencing). Task 3.5 (platform admission) and the platform leg of Task 3.6 are
  gated on the streaming-flip merge. RECOMMENDATION: build and gate engine Tasks 3.0 through 3.4
  and the engine parity/bench leg of 3.6 now (they stand alone and are the bulk of the slice);
  hold Task 3.5 and the end-to-end prod-sim until Cam lands streaming-flip. Do not fork a second
  copy of `phase1_eligibility`.

Doc-accuracy correction carried into this plan: the master plan calls the registry "34-provider";
the live catalog is 24 to 26 bindings. This plan never quotes 34, and Task 3.3 asserts totality
against the live registry rather than a hardcoded count.

---

## Task 3.0: Freeze C1 and capture the pinned-oracle baseline (measure first)

No optimization is designed until the current cost is measured. This task produces the frozen
baseline the Task 3.6 gate compares against, and the numbers that turn JC-2 and JC-3 from
placeholders into constants.

Files:
- Create: `docs/plans/PHASE3-C1-BASELINE.md` (the frozen record, mirrors `PHASE2-BASELINE.md`).
- Create: `scripts/native-baseline/bench_c1_oracle.py` (out-of-band data build + fresh-process
  worker + external `VmHWM` sampling, reusing the honest harness pattern proven in Phase 2).
- Reference (read): `scripts/prod-sim/.../scenarios/mask-fullframe-saturate/recipe.yaml`,
  `docs/plans/PHASE2-BASELINE.md`.

Steps:
1. Freeze the exact C1 recipe verbatim into the baseline doc (the two-table
   `mask-fullframe-saturate` recipe, seed `20260821`), and freeze the dataset tier(s): a small
   parity tier and the representative large tier for the memory measurement. Record the exact row
   counts and column shapes.
2. Build the C1 data out of band (separate, non-timed, non-sampled process; drop each batch after
   write) so generation never inflates the measured RSS, exactly as Phase 2's `build_w2_parquet.py`
   does.
3. Run the recipe on the PINNED oracle (`substrate="pandas"`, `execution_mode="full_frame"`,
   `auto_chunk=False`) in a fresh process under external `VmHWM` sampling. Record: end-to-end
   latency, hash throughput (rows/s/col), Faker throughput (rows/s/col), peak RSS, and spill use.
4. From the oracle output, compute the pooled-output fidelity statistics that JC-2 will pin: the
   UNIQUE-feasibility status per Faker column and the observed repeat/collision rate of the pooled
   values at the frozen tier. Record them.
5. Set the gate targets in the baseline doc: the HARD peak-RSS bound (flat, within the Phase 1
   streaming ceiling), the wall non-regression bound (JC-3), and the C1 fidelity tolerance (JC-2),
   each a concrete number derived from this run, each labeled as proposed-for-Codex-confirmation.
6. Commit: `docs(native): freeze C1 recipe + pinned-oracle baseline for the Phase 3 gate`.

Acceptance: the baseline doc records real numbers (not placeholders) for latency, both throughputs,
peak RSS, spill, and the two fidelity statistics; the three gate targets are concrete and sourced
to this run.

## Task 3.1: Native chunked Faker masking (pool built once, selected per chunk)

Extend the Phase 2 native chunked route to execute `faker` masking with bounded per-chunk memory
and exact parity to the oracle `FakerStrategyHandler`.

Files:
- Modify: `execution/native/_dispatch.py` (`_mask_chunk_native` dispatch ladder gains a `faker`
  branch; per-table pool build/resolve threaded through `run_native_or_oracle_chunked`).
- Modify: `execution/native/_requirements.py` (`NATIVE_KERNEL_STRATEGIES` handling for `faker`, or
  a dedicated `NATIVE_POOL_STRATEGIES` set, see Step 1).
- Test: `tests/parity/native/test_c1_faker_parity.py`, `tests/native/test_dispatch_faker.py`.

Steps:
0. MEASURE (JC-1 gate). Before wiring, micro-measure vectorized `PoolSampler` selection throughput
   per chunk against Task 3.0's Faker throughput. If it holds the JC-3 wall bound, proceed with the
   Python selector; if not, record the miss and escalate JC-1 to Cam before adding any compiled
   path. Do not build a Rust selector speculatively.
1. Decide the strategy-set seam. `faker` differs from the Phase 2 four in that it is pool-backed,
   not a stateless scalar kernel. Add `faker` through a dedicated `NATIVE_POOL_STRATEGIES`
   membership rather than overloading `NATIVE_KERNEL_STRATEGIES` (which means "a compiled scalar
   kernel exists"), so the two admission reasons stay distinct and honest. The dispatch admits a
   node native when it is a scalar node whose strategy is in EITHER set.
2. Build the pool once per (table, Faker column identity), before the chunk loop, using the
   existing `PoolBuilder.identity_for` cheap-lookup then `PoolCache.get`/`build`. The pool is
   shared across all chunks of the table. This is the ONLY added state; the 256 MB `PoolCache`
   bound already exists.
3. In `_mask_chunk_native`, add the `faker` branch: for the chunk's source column, run the existing
   `PoolSampler.sample(pool, n, mode=..., seed=select_seed, source=chunk_source,
   namespace=plan.namespace, deterministic=plan.deterministic, scale=scale)` where
   `select_seed = mask_key` for deterministic selection (the DE-02 seam: build stays on job_seed,
   selection re-keys onto mask_key). Preserve source nulls exactly as the oracle handler does.
4. Parity: assert exact logical parity (values, row order, null placement, warnings) between the
   native chunked route and the pinned oracle over the C1 Faker columns, across multiple batch
   sizes and table orders, using the Phase 0 `assert_logical_parity` harness. Because
   `derive_index` is partition-independent, the per-chunk result must equal the whole-column oracle
   result byte-for-byte at the logical level.
5. Route evidence: record that the Faker columns executed on the native route (a `pool_select`
   runtime counter analogous to the Phase 2 kernel counters), so the gate can prove the intended
   route ran and the oracle did not.
6. Commit: `feat(native): chunked faker masking on the native route (pool once, select per chunk)`.

Acceptance: exact parity across batch sizes/orders; peak RSS bounded per chunk (no whole-table
residency); pool built exactly once per column identity; route evidence shows native execution.

## Task 3.2: `pool_quality` obligation enforcement

Turn the declared `pool_quality` capability tag into an enforced obligation with the frozen C1
fidelity tolerance, checked before publication.

Files:
- Create: `execution/native/_pool_quality.py` (`enforce_pool_quality(evidence, *, tolerance) ->
  None | raises PoolQualityError`).
- Modify: `execution/native/_capabilities.py` (the `pool_quality` obligation now resolves to a real
  enforcer, not just a tag).
- Test: `tests/native/test_pool_quality.py`.

Steps:
1. Define the frozen C1 tolerance from Task 3.0 (JC-2): UNIQUE-feasibility satisfied where the
   column is UNIQUE-cardinality, and pooled repeat/collision rate within the pinned bound. Encode
   it as a named constant with a docstring citing the baseline run.
2. Consume the existing runtime signal: `PoolCache.warnings()` / `QualityWarning` /
   `quality_summary`. `enforce_pool_quality` reads the emitted warnings plus the measured pooled
   distribution and raises `PoolQualityError` (coded) when the tolerance is exceeded, before the
   masked output is published.
3. Permit EXACTLY the `pool_quality` obligation for an admitted C1 Faker column. Reject every other
   quality obligation with a coded reason (no silent pass-through of an unrecognized obligation).
4. Non-vacuity test: a synthetic pool whose collision rate exceeds the tolerance MUST raise; a
   compliant pool MUST pass; assert the enforcer is not trivially always-true.
5. Commit: `feat(native): enforce pool_quality obligation with frozen C1 fidelity tolerance`.

Acceptance: the tolerance is a concrete constant sourced to Task 3.0; enforcement raises on a
tolerance breach and passes a compliant pool; every non-`pool_quality` obligation is rejected coded.

## Task 3.3: Config-aware `phase3_c1_eligibility` engine predicate

One pure engine-side layer above the Phase 1 predicate that admits `mask.faker` for exactly the C1
poolable providers and rejects everything else before staging.

Files:
- Create: `execution/native/_phase3_eligibility.py` (`phase3_c1_eligibility(config, *, table,
  profile=None) -> Phase3Eligibility`).
- Test: `tests/native/test_phase3_eligibility.py`.

Steps:
1. Write failing tests first: the frozen C1 recipe admits (both tables); a nonpoolable Faker
   provider rejects `provider_not_pool_native`; a non-C1 poolable provider rejects
   `provider_not_in_c1_allowlist`; a custom callable rejects `provider_reject_large`; an
   unsupported Faker config shape rejects `faker_config_shape_unsupported`.
2. Implement as a pure function above `native_route_eligibility`. For every Faker column: resolve
   the Faker config before admission, call `classify_provider` and require `pool_native`, and admit
   only the exact poolable provider IDs in the frozen C1 recipe and only the config shape the C1
   pool builder and native selector support. Non-Faker columns defer to the Phase 1 predicate
   unchanged.
3. Reject nonpoolable Faker, non-C1 providers, other provider families, `python_only`, and
   `reject_large` with distinct coded reasons, all before any source staging.
4. Totality: assert `phase3_c1_eligibility` is total over the live provider registry (no
   hardcoded count; enumerate the registry, per the 24-to-26 correction).
5. Cross-check: run Phase 1 and Phase 3 eligibility against the unchanged frozen C1 recipe. Phase 1
   records its streaming-rejection reason (`strategy_not_allowlisted_for_streaming`, the real
   string, not the plan's aspirational `faker_not_chunk_compatible`); Phase 3 admits the same
   recipe.
6. Commit: `feat(native): config-aware phase3_c1_eligibility above the phase1 predicate`.

Acceptance: C1 recipe admitted; every out-of-scope provider/config rejected coded before staging;
total over the live registry; Phase 1 still rejects, Phase 3 admits, same recipe.

## Task 3.4: Bounded-state adversary tests

Prove every state owner on the C1 native path has a fixed memory budget under a high-cardinality
adversary. This is the "add only the state this path uses, and bound it" obligation made testable.

Files:
- Test: `tests/native/test_c1_bounded_state.py`.

Steps:
1. Enumerate the state owners the C1 native path touches: the `PoolCache` (256 MB LRU), the warning
   state (`QualityWarning` accumulation), the row-error state, and each DuckDB-backed intermediate
   table on the chunked route.
2. For each, drive a high-cardinality C1 input (many distinct source values, many chunks) and
   assert the owner's footprint stays within its declared budget: the pool cache evicts and does
   not grow past its byte bound; warning/error state does not accumulate unboundedly across chunks;
   DuckDB intermediates spill rather than balloon.
3. Assert peak RSS across the adversarial run stays flat with row count (the Phase 3 headline
   invariant), not merely "completes".
4. Commit: `test(native): bounded-state adversary coverage for the C1 native path`.

Acceptance: each state owner provably bounded under the adversary; peak RSS flat with row count.

## Task 3.5: Platform `phase3_c1_eligibility` admission layer (gated on streaming-flip)

GATED: build only after Cam lands the streaming-flip merge (precondition 4 / JC-4). The platform
layer sits above the platform `phase1_eligibility` and admits the C1 recipe at claim time.

Files (on the streaming-flip base, not platform main):
- Modify: `api/jobs/_phase3_eligibility.py` (new), consumed by `admission.py`.
- Test: platform eligibility + admission tests.

Steps:
1. Add a pure `phase3_c1_eligibility` layer above `phase1_eligibility`. Enforce `reject_large` at
   claim-time admission. Permit the pandas oracle only below an explicit priced small-job limit.
2. Admit the frozen C1 recipe at claim time; Phase 1 records its streaming-rejection reason, Phase 3
   admits the same recipe before any source staging or native execution.
3. Record the selected AND executed Phase 3 route in the job evidence (not just the decision).
4. Commit on the streaming-flip base; do not fork a second `phase1_eligibility`.

Acceptance: C1 admitted at claim time; `reject_large` enforced; oracle permitted only under the
priced small-job limit; selected and executed route both recorded in evidence.

## Task 3.6: C1 evidence gate + prod-sim C1 run

The frozen Phase 3 gate. Engine leg proceeds now; platform end-to-end leg follows Task 3.5.

Files:
- Create: `tests/parity/native/test_phase3_c1_gate.py` (the frozen gate), `docs/plans/
  native-phase3-C1-gate.md` (the certification record).
- Reference: `scripts/native-baseline/bench_c1_oracle.py` (Task 3.0), the prod-sim
  `mask-fullframe-saturate` scenario.

The gate has four hard criteria; failing any one fails the gate:
1. EXACT parity: the C1 native+streaming output equals the pinned oracle (values, row order, null
   placement, warnings, row errors, logical schema), across batch sizes and table orders.
2. Seed stability + partition invariance: fresh processes with different batch boundaries reproduce
   identical output for the `partitionable=True` Faker selection.
3. Bounded state + C1 fidelity: peak RSS bounded and flat within the Phase 1 streaming ceiling
   (Task 3.4), and `pool_quality` within the frozen C1 tolerance (Task 3.2).
4. Intended-route proof: the job ran on the native+streaming route. Oracle completion, admission
   rejection, or fallback each FAIL the gate (the Phase 2 Decision-10 trap: job success alone does
   not prove the route; assert the pool-select runtime counters fired and the oracle did not).

Steps:
1. Engine leg: run the frozen gate over the C1 recipe at the parity tier and the memory tier;
   record all four criteria in the certification doc with real numbers from Task 3.0's baseline as
   the comparison.
2. Benchmark: run the Phase 2 bench harness on C1 (out-of-band build + lazy read + external VmHWM)
   at the frozen tiers; record peak RSS, wall, throughput against the Task 3.0 oracle baseline and
   the JC-3 bounds.
3. Platform leg (after Task 3.5 / streaming-flip): run the prod-sim `mask-fullframe-saturate`
   workload end to end; confirm admission admits, the native+streaming route executes, and the
   route evidence records it.
4. Commit: `test(native): C1 Phase 3 gate + certification (parity, bounded RSS, intended route)`.

Acceptance: all four criteria PASS at both tiers; bench numbers recorded against the frozen
baseline; prod-sim C1 runs on the intended route (platform leg, post-streaming-flip).

---

## Sequencing

1. Task 3.0 (baseline) first: it sets the constants the rest depends on.
2. Tasks 3.1, 3.2, 3.3 build the engine slice (dispatch, pool_quality, eligibility); 3.1 gates JC-1
   at its Step 0.
3. Task 3.4 (bounded-state) after 3.1/3.2.
4. Task 3.6 engine leg after 3.1 through 3.4.
5. Tasks 3.5 and the 3.6 platform leg gate on Cam landing streaming-flip (JC-4).

Every task is stacked on the Phase 2 branches, each dennis + Codex gated, nothing merged or pushed
without Cam. The three throughput/tolerance/wall numbers (JC-2, JC-3) are placeholders until Task
3.0 measures them; the selection engine (JC-1) and platform sequencing (JC-4) are the two calls
this plan holds for Cam before the corresponding tasks build.

## Phase 3 acceptance (whole slice)

- C1 masked on the native+streaming route with exact parity to the pinned oracle.
- Peak RSS bounded and flat across tiers, within the Phase 1 streaming ceiling (the headline win).
- `pool_quality` enforced within the frozen C1 tolerance; every other obligation rejected coded.
- Bounded state proven under a high-cardinality adversary.
- Phase 3 eligibility admits exactly the C1 poolable providers and rejects everything else coded.
- Intended-route proof: oracle completion, admission rejection, or fallback all fail the gate.
- 100M-row cap held; reviewed only after this slice's parity, bench, and prod-sim C1 complete.
