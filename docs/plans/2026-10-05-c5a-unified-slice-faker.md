Status: plan (revision 4, author = Opus). Codex plan gate rounds 1-3 all REVISE with 0 BLOCKER; at the 3-round cap Cam chose (2026-10-05) "narrow scope + one confirming review": 3h (lane-wide route evidence) moved to a follow-up slice, and the shared pool cache keeps the default 256 MiB budget. Awaiting the confirming review.

# C5a: pooled Faker on the unified slice

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, slice C5a ("Pooled Faker on the unified slice").
Branch: `feat/c5a-unified-faker` off engine main `e24200ed` (C3 merged).

## 1. Goal and scope

Admit the deterministic-REUSE pooled Faker column to the full-frame **unified slice** (`_unified_slice.py`, the single-table resident Arrow lane that runs the physical plan through `ShadowCoordinator`). The output must be byte-and-type identical to the pandas full-frame oracle (`FakerStrategyHandler.run`). Today a table with any Faker column declines the whole lane and runs on pandas.

In scope, the same domain the chunked route admitted in C1, with no widening:

- `strategy: faker`, `deterministic: true`, `cardinality_mode: reuse`, explicit `namespace`, explicit `pool_size` (the JC-5 gate `faker_pool_precondition_met`).
- Provider in `C1_PROVIDER_ALLOWLIST` (`person_first_name`, `person_last_name`) and classified `pool_native`.
- Resident source type `pa.string()`.
- No `when`, no `vault`, table not in an FK relationship.
- Companion present with the index kernel loadable.

Out of scope, each a later slice: non-deterministic Faker (C5b, the config default), non-string sources (C5c), `large_string` sources, widening the provider allowlist, other cardinality modes. Each of these keeps declining to the oracle, which is today's behavior.

## 2. Established facts (engine `e24200ed`; from the C5a research probe, re-verified by the author)

1. **Most of the machinery exists and is dormant by design** (Task 4.6 slice 1, PR #143).
   - The shadow binder binds Faker to `native_faker_select` with a `KeyBinding` + `PoolBinding` (`physical/_shadow_bindings.py`, `_faker_pool_bindable` at ~:134 mirrors the C1 predicate).
   - `run_operator` runs it through the shared `sample_faker_array` (`physical/_shadow_operators.py` ~:191). This is the SAME helper the native chunked route calls (`native/_chunk_masking.py:67`).
   - `ShadowCoordinator._resolve_pool` builds each pool once per identity through `resolve_faker_pool_identity`, the same identity function the oracle and the chunked route use, and raises the coded `faker-pool-non-string-output` ShadowDifference if the built pool is not all strings.
2. **The unified slice declines Faker at three points** (corrected in rev 2):
   - (a) `"native_faker_select"` is absent from `ALLOWED_OPERATOR_IDS` (`_unified_slice_admission.py:84`), so `resident_contract_admission` returns None.
   - (b) `_ADMITTED_RESIDENT_TYPES` has no `"faker"` entry (map at ~:155), and an absent strategy declines at ~:592.
   - (c) `_unified_slice.py:242` constructs `ShadowCoordinator(ctx=ctx)` with no `registry`. That is not an admission check: a bound Faker node would hit the runtime `AssertionError` in `_resolve_pool` (`_shadow_coordinator.py:538`), which the lane's broad exception boundary (~:390) turns into a fall-through to the oracle. The coordinator docstring (~:216) calls the omission deliberate.
3. **The oracle selection is source-keyed and already compiled.** `PoolSampler._deterministic` calls `derive_index_batch(values, mask_key, namespace, pool.size)` then gathers `pool.values[idx]`, with nulls kept by position. `sample_faker_array` reproduces this exactly: `select_seed = mask_key`, pool build on `job_seed`, `pool_size = pool.size` as built. Cell-for-cell parity is already pinned by `tests/physical/test_shadow_corpus.py`, `test_shadow_faker_lifecycle.py` and `tests/native/test_sample_faker_array.py`. C5a's win is removing the pandas wrapping and the frame round-trip, not compiling selection.
4. **Faker carries no diagnostic obligation that admission would reject.** Its capabilities (`native/_capabilities.py:228`) declare no `warning_codes` and no `row_error_modes`, so `diagnostic_reducers` is empty. It needs no prepass (row-local, not global). Its only tag is the class-A `pool_quality` obligation, which has no full-frame consumer (it is enforced only by the standalone chunked `enforce_pool_quality`). The full-frame oracle does not read `PoolCache.warnings()` into the job result either. Only `_route_diagnostics` (chunked) and `_pipeline_auto_chunk` read it.
5. **Output reconstruction already matches the Faker oracle's assignment.** The oracle writes `df[column] = <python list, None for nulls>` (`_strategies/_faker.py:97-99`). The unified slice overlays `masked.to_pylist()` positionally on `candidate.source_frame`, then runs the legacy adapter's own `pa.Table.from_pandas`. Faker is already classified as a tokenizing strategy in `physical/_shadow_assembly.py:23`, and zero rows are already reconciled to `float64` there (~:47). The coordinator does not return an empty `pa.string()` for Faker (corrected in rev 2).
6. **Registry identity.** `run_pipeline` resolves the registry once (`_pipeline.py:354`; `registry=None` resolves the singleton default) and passes the same object to the physical inputs (`_unified_slice.py:183`) and to the oracle adapter (`_pipeline_generate_mask.py:270`). So `inputs.registry` is the oracle's registry. The masking `run_pipeline` has no `instance_default_locale` knob (only generation does), so the shadow context's `None` default matches.
7. **Thread budget gap (pre-existing, all unified operators).** `run_pipeline` accepts `native_threads` (`_pipeline.py:175`), but the unified lane's signature (~:397) and forwarding census (~:529) omit it, and `ShadowContext.from_key_provider` is called without it (~:236). Every compiled unified operator therefore runs with `native_threads=None`, which Rust resolves to one thread (`threads.rs:123`). This violates the program's thread-budget contract (`rust-engine-program.md:22-24`).
8. **Route evidence gap (pre-existing).** Unified node evidence carries only `operator`, `executed` and `compiled_kernel_executed` (`_unified_slice.py:285`). The program requires planned backend, executed backend, call count and elapsed time per column (`rust-engine-program.md:22`). `OperatorCallEvidence.batches_run` exists (`_shadow_operators.py:112`) but is not surfaced. Per-node elapsed time is collected separately in the `TimingCollector` records. The chunked route already has a backend vocabulary (`native/_chunked_evidence.py:32-35`: `rust_companion`, `rust_pool_select`, `arrow_python`, `pandas_oracle`).

## 3. Decisions

**3a. Admission table entries** (`_unified_slice_admission.py`). Add `FAKER_OPERATOR_ID = "native_faker_select"` to:
- `ALLOWED_OPERATOR_IDS`
- `_COMPANION_DEPENDENT_OPERATOR_IDS`
- `_OPERATOR_REQUIRED_KERNEL` → `"index"`

Also add `_ADMITTED_RESIDENT_TYPES["faker"] = {pa.string()}`. Do NOT add `large_string`, even though the binder accepts it. The matrix is "widened only by a separately proven slice" (module comment ~:148), and every sibling index operator admits `string` only. A `large_string` Faker column therefore declines the whole table to the oracle, as it does today.

The UTF-8 namespace check and the per-operator kernel gate in `resident_contract_admission` apply through the existing companion-dependent branch with no new code. Faker reaches that branch only after `_faker_pool_bindable` has bound the node; an unbound node already returns None at the `binding is None` check.

**3b. Thread the registry into the coordinator** (`_unified_slice.py:242`): `ShadowCoordinator(ctx=ctx, registry=inputs.registry)`. Use `inputs.registry`, the one resolved `ProviderRegistry` the physical plan compiled against, so the binder's `classify_provider` and the coordinator's `PoolBuilder` see the same registry. Update the coordinator docstring (~:216-219) that says the unified slice never admits Faker. `ShadowContext.from_key_provider` already supplies a real `job_seed`, so the job_seed guard holds.

**3c. Provider code runs at most once per job: one job-scoped pool cache shared by both routes** (rev 3; replaces rev 1's post-build reroute and rev 2's binding-identity check, both rejected by Codex).

The root problem: the pool build calls user-pluggable provider code (`PoolBuilder.build` → registry adapter, `_shadow_coordinator.py:553-588`), and the lane can fall through to the oracle after that build, which then builds again. No static check on the registry can prove a binding's behavior. The V2 custom-function override keeps the singleton adapter identity (`_faker_adapter.py:130,245`; Codex probe: `person_first_name -> 7` passed the identity check). So the fix is structural: the pool is built at most once per job, whichever route finishes the work.

- **Shared cache.** `run_pipeline` creates ONE `PoolCache` for the job after the sequential and out-of-core early returns and BEFORE the unified lane call (`_pipeline.py:556`). It forwards that cache to the lane and, through `run_generate_and_mask_steps` (`_pipeline.py:563`), to the full-frame `adapter.run(...)` in `_pipeline_generate_mask.py` (~:270). It is NOT passed to the chunked or split routes. (Rev 3 placed the creation in `_pipeline_generate_mask.py`, which runs after the lane; Codex round 3 HIGH 1.) It passes the same object to the unified lane (new `pool_cache` parameter, threaded through `maybe_run_unified_slice` and the forwarding census, then into the coordinator) and to `adapter.run(pool_cache=...)` (the parameter already exists, `_pandas_adapter.py:142,177`). `ShadowCoordinator` takes an optional injected `pool_cache` and uses it instead of its fresh one (`_shadow_coordinator.py:283`). With no injection it behaves exactly as today, so the dormant shadow corpus is unchanged.
  - The shared cache uses the DEFAULT `PoolCache` budget (`_DEFAULT_MAX_BYTES`, 256 MiB, `_cache.py:31`), the same capacity the oracle's own fresh cache has today; construct it as `PoolCache()` with no arguments so it tracks whatever default the oracle uses. It is not unbounded (Codex round 3 HIGH 2; Cam decision). Consequences:
    - A single pool larger than the budget: the oracle's `put` raises `PoolCapacityError(pool_exceeds_cache_budget)` today. On the lane, the coordinator's `put` into the shared cache raises the identical error. The `_PoolBuildFailed` wrapper below covers both the build AND the put, so the original `PoolCapacityError` escapes unchanged, after one build and with no reroute. That matches today's oracle failure.
    - Several pools over budget in aggregate: LRU eviction behaves exactly as on the oracle's cache. The coordinator's `pools_by_identity` map already pins every identity for the run, so selection is unaffected.
    - Documented residual: the single-build guarantee for the non-string reroute (next bullet) holds only while the lane's pool stays cached. If that pool was evicted, the oracle rebuilds it. This needs a rebound provider yielding non-strings AND aggregate pools over 256 MiB in one table. Pure providers give identical output either way; only an impure provider in that combination is invoked twice. Accepted by the narrowed scope.
  - Pool content depends only on identity, and the full-frame route never reads `PoolCache.warnings()` (fact 4), so sharing the cache changes no output, warning or evidence.
- **Non-string pool (rebound provider).** The coordinator builds once, puts the pool in the shared cache, and only then detects `FAKER_POOL_NON_STRING_OUTPUT`. The lane treats exactly that one coded difference as a reroute and returns None, using a named single-code constant, not a broad catch. Every other coded difference keeps D8 fail-closed. The oracle then takes the cached pool (no second provider call) and produces its normal output for that pool. No output or side effect exists before the reroute, because the coordinator raises inside `_resolve_pool` before any batch runs and the lane publishes nothing until `run` returns.
- **Pool build or cache insert raises (provider failure, capacity).** The coordinator wraps the `builder.build(...)` call and the following `pool_cache.put(...)`, and re-raises any exception from either as a lane-private `_PoolBuildFailed` carrying the original. The lane catches `_PoolBuildFailed` ahead of its broad reroute boundary (`_unified_slice.py:390`) and re-raises the ORIGINAL exception object, so there is no reroute and no second build.
  - This matches the oracle only if the oracle surfaces the same exception unwrapped. Test 13 proves that against a lane-off run. If the oracle wraps it (for example in `ExecutionError`), the builder STOPS and reports. Adding a wrapper needs a plan amendment, never an improvised one.
- **Rebound provider yielding strings.** It runs natively, built once. Both routes use the same `inputs.registry` (fact 6), so the output equals the oracle's.
- No binding-identity admission check is added (rev 2's 3c is withdrawn).

**3d. Zero-row and all-null parity** (rev 2: assert the existing rule; no new code). Faker is already a tokenizing strategy in `_shadow_assembly.py`, so a zero-row Faker column reconciles to `float64` like the oracle's `df[column] = []`. The tests assert that on the production lane. An all-null column must match exactly in values, type and `b"pandas"` metadata. Change reconstruction only if a test shows a real divergence, and then fix it at `_shadow_assembly.py`.

**3e. D7 positive evidence.** Extend the hash-only D7 check (`_unified_slice.py` ~:276) to a set `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS = {HASH_OPERATOR_ID, FAKER_OPERATOR_ID}`. A completed Faker node must carry `compiled_kernel_executed=True`. `run_operator` sets it only after `sample_faker_array` returns from the compiled kernel, which `load_compiled_index_kernel` resolves (never the reference fallback; admission has already required the index kernel).

**3f. Pool cache scope.** The coordinator's run-scoped fresh `PoolCache` stays. Pool content is a pure function of the identity (provider, size, locale, config, namespace, job_seed), so a fresh cache gives byte-identical values to the oracle's caller-supplied cache. No warning parity is lost, because the full-frame oracle surfaces no pool-cache warnings (fact 4). The builder must confirm this with a test asserting equal `result.warnings` and quality evidence between lane-on and lane-off for an admitted Faker table.

**3g. Forward `native_threads` into the unified lane** (rev 2, fixes fact 7 at the root for every unified operator, not just Faker). Add `native_threads` to the unified-lane entry signatures and the local-forwarding census, and pass it to `ShadowContext.from_key_provider(native_threads=...)`. The value is the one `run_pipeline` already resolved for its other native routes; resolve no new default here. Output must not depend on the thread count (thread invariance).

**3h. (Moved out of C5a; Cam, 2026-10-05.)** Bringing unified node evidence up to the program contract (planned/executed backend, call count, elapsed time per column) is a lane-wide gap that affects every operator. Round 3 showed it needs per-operator idle rules (bucket_perturb and date_shift `derive_calls`; all-null `group_key` is NOT idle) and a pure evidence-assembly helper to be testable. It becomes its own follow-up slice. C5a keeps the existing evidence keys and adds Faker only to the D7 positive-kernel check (3e).

## 4. Implementation

1. `_unified_slice_admission.py`: the 3a entries, the `FAKER_OPERATOR_ID` constant. Update the comments that enumerate companion-dependent operators.
2. `_unified_slice.py`: pass the registry (3b); thread `pool_cache` in and catch `_PoolBuildFailed` before the broad boundary (3c); add the single-code `FAKER_POOL_NON_STRING_OUTPUT` reroute (3c); add the D7 operator set (3e); forward `native_threads` (3g). No evidence-dict changes (3h moved out).
2b. `_pipeline.py` creates the job-scoped shared `PoolCache` (3c) and forwards it to the lane and through `run_generate_and_mask_steps` to `adapter.run` in `_pipeline_generate_mask.py`.
3. `physical/_shadow_coordinator.py`: an optional injected `pool_cache`; wrap only `builder.build` in `_PoolBuildFailed`; update the docstring that says the unified slice never admits Faker.
4. No Rust, no new kernel, no change to `sample_faker_array`, `_faker_pool_bindable`, the chunked route, or the oracle handler logic. The oracle only receives a caller-supplied cache, through its existing parameter.
5. Docs: CHANGELOG entry; compatibility-contract line if the unified-slice admitted set is listed there; build record `docs/records/2026-10-05-c5a-unified-slice-faker-build.md`.

## 5. Acceptance tests (written first; red-before recorded)

New file `tests/physical/test_unified_slice_faker.py`, plus additions to `test_unified_slice_admission.py` and `test_unified_slice_parity.py`. Every lane-vs-oracle compare uses the strictest existing equality helper in `test_unified_slice_parity.py`: values, Arrow field types, column order and schema metadata, plus `result.warnings` and per-column evidence.

**Non-vacuity rule:** every positive case poisons `PandasExecutionAdapter.run` (raise if called) during the lane run, so a silent decline cannot pass. The oracle reference output comes from a separate lane-off run.

1. **Admits and runs native.** One in-scope Faker column plus a passthrough column. The lane admits, and the Faker node's evidence equals exactly `{"operator": "native_faker_select", "executed": True, "compiled_kernel_executed": True}`. Output equals the oracle's.
2. **Mixed operators.** Faker plus hash, categorical and redact in one table. Admitted, identical, and every node's evidence shows its expected operator id.
3. **Two Faker columns sharing one pool identity:** identical to the oracle, and `PoolBuilder.build` is called exactly once.
4. **Two Faker columns with different namespaces:** identical.
5. **Nulls:** interleaved nulls kept by position; an all-null column; a zero-row table, which yields `float64`/`double` exactly like the oracle (3d).
6. **Batch boundaries:**
   - Production lane: 50,001 rows crosses the coordinator's 50,000-row batch (uneven last batch; spy shows two `derive_index_batch` calls), identical to the oracle. A source whose Arrow column is a ragged multi-chunk `ChunkedArray` is also identical.
   - Coordinator seam (production `run_pipeline` exposes no batch size; `_shadow_context.py:134`): `ShadowContext.from_key_provider(batch_size_rows=7 / 1000 / default)` over the same Faker table gives identical output, compared against the oracle output.
7. **Thread invariance (3g):** `run_pipeline(native_threads=1)` and `native_threads=4` give byte-identical output. A spy on the compiled `derive_index_batch` proves that the value 4 actually arrives in the second run. The same spy check applies to one non-Faker index operator (categorical), proving 3g's lane-wide fix.
8. **Locale and pool size:** a supported non-default locale and two different `pool_size` values on the production lane, identical to the oracle. The allowlisted providers take no provider kwargs, so there is no other config dimension (Codex round 2).
9. **Determinism:** two runs give identical bytes; a different `mask_key` changes the output; a different `job_seed` changes the pool content.
10. **Declines to the oracle** (lane returns None, job output equals the lane-off run). One parametrized case each:
    - non-deterministic
    - `cardinality_mode` != reuse
    - missing namespace
    - missing pool_size
    - provider outside the allowlist
    - `when` set
    - `vault` set
    - FK-participating table
    - `large_string` source
    - int64 source
    - companion index kernel unavailable
11. **3c single build, rebound non-string provider:** a custom registry (and, separately, the V2 custom-function override of `person_first_name` on the default adapter) yields ints through a STATEFUL adapter that counts invocations. The lane reroutes (returns None, no `UnifiedSliceInvariantError`). The provider is invoked exactly once across the whole job, and the job output equals the lane-off oracle run. Sibling tests:
    - A rebound provider yielding strings runs natively (poisoned oracle), is built once, and is identical to the oracle with the same registry.
    - Any OTHER coded ShadowDifference raised from the coordinator still raises `UnifiedSliceInvariantError` (the reroute is single-code).
    - With no injected cache, the coordinator still uses a fresh run-scoped cache (the shadow corpus is unchanged).
12. **3e D7:** a stubbed coordinator result with the Faker node at `compiled_kernel_executed=False` raises `UnifiedSliceInvariantError`.
13. **Oversized pool, oracle-identical:** with a lowered cache budget (through `decoy_engine.settings` or the injected cache's `max_bytes`, applied identically to both runs), the lane raises the same `PoolCapacityError` type, code and message as the lane-off run. The provider is built once, with no reroute.
13b. **Provider failure, single invocation, oracle-identical:** a stateful in-scope adapter whose build raises a coded `ProviderError`. The lane run invokes it exactly once and raises the original exception, and a lane-off run raises the same type, code and message. No output is returned, and the sink is unwritten.
14. **Warnings/evidence parity (3f), non-vacuous:** instrument `PoolCache.warnings` with a spy that records calls. On an admitted Faker run, the full-frame lane and the oracle both never call it, and `result.warnings` are equal. Equality of two empty tuples alone does not count as evidence.
15. **Companion-absent clean env** (ci-mirror): Faker tables decline, and companion-only assertions are guarded with `@NEEDS_COMPANION`.

Mutation targets (hand-mutants, each must be killed): drop Faker from `ALLOWED_OPERATOR_IDS`; drop it from `_COMPANION_DEPENDENT_OPERATOR_IDS`; add `large_string` to the type matrix; omit `registry=`; pass a fresh cache instead of the shared one (kills test 11 single-build); catch `_PoolBuildFailed` with the broad reroute (kills tests 13/13b); leave `put` outside the wrapper (kills test 13); widen the reroute to all coded differences; drop Faker from the D7 set; drop the `native_threads` forwarding; swap `mask_key` for `job_seed` in selection.

## 6. Risk, rollback, docs, gates

- **Risk: R2.** A behavior change on the default full-frame path for admitted Faker tables. It is byte-identical by contract, and any doubt declines to the unchanged oracle. 3g changes the thread count for every compiled unified operator; that is safe because output is thread-invariant (test 7). Rollback is reverting the admission-table lines; the lane then declines Faker as today, and 3g remains as an independent fix.
- **Gates:** Codex plan gate → Sonnet build (tests first) → dennis → Codex final → ci-mirror companion-absent → Cam merge authorization.
- **Docs:** CHANGELOG, compatibility contract (if listed), build record; platform RECENTLY-SHIPPED on merge.

## 7. Plan-gate history

- Round 1 (Codex): REVISE, 0 BLOCKER / 4 HIGH / 1 MEDIUM, all folded in rev 2.
  - HIGH 1: the post-build reroute ran provider code before declining → 3c now declines at admission on binding identity.
  - HIGH 2: `native_threads` was not forwarded → 3g.
  - HIGH 3: route evidence was below the program contract → 3h.
  - HIGH 4: the acceptance matrix was below the parity contract → section 5 (batch boundaries, ragged chunks, threads, locale/config, real error parity, poisoned oracle, non-vacuous warnings).
  - MEDIUM 5: two facts were wrong (three decline points; zero-row is already `float64`) → facts 2 and 5, and 3d.
- Codex's answers to rev 1's open questions: decline only a rebound admitted provider (3c); full-frame never surfaces `PoolCache.warnings()` (fact 4, test 14); `inputs.registry` is the oracle's registry (fact 6).
- Round 2 (Codex): REVISE, 0 BLOCKER / 3 HIGH / 2 MEDIUM, all folded in rev 3.
  - HIGH 1: the identity check is unsound (the V2 custom override keeps adapter identity) → 3c rewritten as a job-scoped shared `PoolCache` (single build) plus a single-code reroute.
  - HIGH 2: a provider failure executed twice via the broad reroute → `_PoolBuildFailed` re-raises the original, with no reroute.
  - HIGH 3: 3h crashed an empty `group_key` → the chunked kernel-idle rule (`arrow_python`).
  - MEDIUM 1: the timing join is on `(strategy, column)` with a bijection.
  - MEDIUM 2: test seams corrected (batch sizes at the coordinator seam, derived backend, no provider kwargs).
  - Round-1 HIGH 2 (`native_threads`) and MEDIUM 5 were confirmed CLOSED.
- Round 3 (Codex): REVISE, 0 BLOCKER / 3 HIGH / 1 MEDIUM / 1 LOW. The 3-round cap was reached, so this escalates to Cam.
  - Folded mechanically: HIGH 1 (cache creation moved to `run_pipeline` before the lane) and LOW 5 (removed the withdrawn identity-helper line).
  - Awaiting Cam's direction: HIGH 2 (the unbounded shared cache is not resource-safe), HIGH 3 (3h's kernel-idle rule is wrong per operator; fixing it widens into bucket/date `derive_calls`), MEDIUM 4 (the timing-bijection seam needs a pure evidence-assembly helper).
- Cam decision at the cap (2026-10-05): "Narrow + 1 confirm". Rev 4 folds round-3 HIGH 2 (default 256 MiB shared budget; capacity errors escape through `_PoolBuildFailed` with one build; eviction residual documented). It moves 3h out of C5a, which removes round-3 HIGH 3 and MEDIUM 4 from this slice; they carry into the follow-up evidence slice as its acceptance inputs. Next: one confirming review, then build.
