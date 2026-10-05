Status: plan (revision 2, author = Opus). Codex plan-gate round 1 = REVISE (0 BLOCKER, 4 HIGH, 1 MEDIUM), all folded here; awaiting round 2.

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

**3c. A rebound provider declines at admission, before any provider code runs** (rev 2; replaces rev 1's post-build reroute). Admission proves only that the provider NAME is allowlisted and `pool_native`. A custom registry can rebind that name to an arbitrary adapter. Rev 1 proposed catching the coordinator's `FAKER_POOL_NON_STRING_OUTPUT` and rerouting, but that check runs after `PoolBuilder.build` has already called the adapter and filled the cache (`_shadow_coordinator.py:553-588`). A stateful adapter would then run twice, once on each route, so the reroute cannot guarantee single execution or side-effect parity.

Decision: add a binding-identity check to the Faker admission path. A Faker node admits only if the admitted provider's binding in `inputs.registry` is the proven default binding:
- `registry.get_adapter(p) is get_default_registry().get_adapter(p)`, or
- the same adapter type (exact `type`, not a subclass) with an equal `get_capabilities(p)`.

Put it in one helper, `_faker_provider_binding_is_default(registry, provider)`, called from `resident_contract_admission`'s Faker branch. Do not change `_faker_pool_bindable`, which the dormant shadow corpus also uses.

A registry that overrides only an unrelated provider still admits. A rebound admitted provider declines, so the whole table runs on the oracle exactly once, as today.

The coordinator's runtime non-string check stays as defense in depth. If it is ever reached on the unified lane, that is an admission gap, and D8 fails closed (`UnifiedSliceInvariantError`) as for every other coded difference. No reroute code is added.

**3d. Zero-row and all-null parity** (rev 2: assert the existing rule; no new code). Faker is already a tokenizing strategy in `_shadow_assembly.py`, so a zero-row Faker column reconciles to `float64` like the oracle's `df[column] = []`. The tests assert that on the production lane. An all-null column must match exactly in values, type and `b"pandas"` metadata. Change reconstruction only if a test shows a real divergence, and then fix it at `_shadow_assembly.py`.

**3e. D7 positive evidence.** Extend the hash-only D7 check (`_unified_slice.py` ~:276) to a set `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS = {HASH_OPERATOR_ID, FAKER_OPERATOR_ID}`. A completed Faker node must carry `compiled_kernel_executed=True`. `run_operator` sets it only after `sample_faker_array` returns from the compiled kernel, which `load_compiled_index_kernel` resolves (never the reference fallback; admission has already required the index kernel).

**3f. Pool cache scope.** The coordinator's run-scoped fresh `PoolCache` stays. Pool content is a pure function of the identity (provider, size, locale, config, namespace, job_seed), so a fresh cache gives byte-identical values to the oracle's caller-supplied cache. No warning parity is lost, because the full-frame oracle surfaces no pool-cache warnings (fact 4). The builder must confirm this with a test asserting equal `result.warnings` and quality evidence between lane-on and lane-off for an admitted Faker table.

**3g. Forward `native_threads` into the unified lane** (rev 2, fixes fact 7 at the root for every unified operator, not just Faker). Add `native_threads` to the unified-lane entry signatures and the local-forwarding census, and pass it to `ShadowContext.from_key_provider(native_threads=...)`. The value is the one `run_pipeline` already resolved for its other native routes; resolve no new default here. Output must not depend on the thread count (thread invariance).

**3h. Per-node route evidence to the program contract** (rev 2, fixes fact 8). Extend every unified node's evidence entry. Keep the existing keys and add:
- `planned_backend`: from the admitted binding, using the chunked vocabulary in `native/_chunked_evidence.py` (import those constants; do not invent new names). Faker is `rust_pool_select`; hash, categorical, bucket_perturb, date_shift and group_key are `rust_companion`; redact, truncate and passthrough are `arrow_python`.
- `executed_backend`: derived from the coordinator's completed `OperatorCallEvidence`. It is the planned backend only when `executed` holds and, for companion and pool operators, `compiled_kernel_executed` holds. Any mismatch raises `UnifiedSliceInvariantError` alongside the existing D7 check.
- `calls`: `OperatorCallEvidence.batches_run`.
- `elapsed_ms`: joined from that node's `TimingCollector` record. Define one join rule (by node id) and assert that every node has exactly one record.

All new values must be JSON-safe. A positive unified run has, by construction, zero oracle fallbacks for the table; the tests assert that by poisoning the oracle adapter (section 5). Existing tests that pin the exact evidence dict get the new keys. This is additive, and no consumer outside the engine reads these keys today (the builder must grep and record that).

## 4. Implementation

1. `_unified_slice_admission.py`: the 3a entries, the `FAKER_OPERATOR_ID` constant, and the 3c binding-identity helper and its call. Update the comments that enumerate companion-dependent operators.
2. `_unified_slice.py`: pass the registry (3b); add the D7 operator set (3e); forward `native_threads` (3g); add the 3h evidence fields. No reroute code.
3. `physical/_shadow_coordinator.py`: docstring only.
4. No Rust, no new kernel, no change to `sample_faker_array`, `_faker_pool_bindable`, the chunked route, or the oracle.
5. Docs: CHANGELOG entry; compatibility-contract line if the unified-slice admitted set is listed there; build record `docs/records/2026-10-05-c5a-unified-slice-faker-build.md`.

## 5. Acceptance tests (written first; red-before recorded)

New file `tests/physical/test_unified_slice_faker.py`, plus additions to `test_unified_slice_admission.py` and `test_unified_slice_parity.py`. Every lane-vs-oracle compare uses the strictest existing equality helper in `test_unified_slice_parity.py`: values, Arrow field types, column order and schema metadata, plus `result.warnings` and per-column evidence.

**Non-vacuity rule:** every positive case poisons `PandasExecutionAdapter.run` (raise if called) during the lane run, so a silent decline cannot pass. The oracle reference output comes from a separate lane-off run.

1. **Admits and runs native.** One in-scope Faker column plus a passthrough column. The lane admits. The Faker node's evidence equals exactly: `operator == "native_faker_select"`, `executed is True`, `compiled_kernel_executed is True`, `planned_backend == executed_backend == "rust_pool_select"`, `calls == 1`, and `elapsed_ms` a non-negative float. Output equals the oracle's.
2. **Mixed operators.** Faker plus hash, categorical and redact in one table. Admitted, identical, and every node's 3h evidence has the expected backend.
3. **Two Faker columns sharing one pool identity:** identical to the oracle, and `PoolBuilder.build` is called exactly once.
4. **Two Faker columns with different namespaces:** identical.
5. **Nulls:** interleaved nulls kept by position; an all-null column; a zero-row table, which yields `float64`/`double` exactly like the oracle (3d).
6. **Batch boundaries:** 50,001 rows (crosses the coordinator's 50,000-row batch, `calls == 2`). Also an uneven last batch at a smaller configured `batch_size_rows` (several batch sizes, e.g. 7, 1000, default). And a source whose Arrow column is a ragged multi-chunk `ChunkedArray`. All identical to the oracle.
7. **Thread invariance (3g):** `run_pipeline(native_threads=1)` and `native_threads=4` give byte-identical output. A spy on the compiled `derive_index_batch` proves that the value 4 actually arrives in the second run. The same spy check applies to one non-Faker index operator (categorical), proving 3g's lane-wide fix.
8. **Locale and provider config:** a supported non-default locale and a provider-config variation on the production lane, identical to the oracle.
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
11. **3c rebound provider:** a custom registry rebinding `person_first_name` to a poolable adapter yielding ints. The lane declines at admission. A spy proves zero coordinator pool builds and exactly one oracle build, and the job output equals the oracle's. Two sibling tests:
    - A registry that overrides only an UNRELATED provider still admits Faker natively.
    - A registry re-registering the same default adapter type with equal capabilities admits.
12. **3e D7 / 3h mismatch:** a stubbed coordinator result where the Faker node has `compiled_kernel_executed=False`, or `executed_backend != planned_backend`, raises `UnifiedSliceInvariantError`.
13. **Real runtime error parity:** an in-scope provider whose pool build raises (monkeypatch the default adapter to raise a coded `ProviderError` on build). The lane and the oracle raise the same exception type, code and message. No output is returned, and no sink or vault side effect occurs (assert the sink is empty or not written).
14. **Warnings/evidence parity (3f), non-vacuous:** instrument `PoolCache.warnings` with a spy that records calls. On an admitted Faker run, the full-frame lane and the oracle both never call it, and `result.warnings` are equal. Equality of two empty tuples alone does not count as evidence.
15. **Companion-absent clean env** (ci-mirror): Faker tables decline, and companion-only assertions are guarded with `@NEEDS_COMPANION`.

Mutation targets (hand-mutants, each must be killed): drop Faker from `ALLOWED_OPERATOR_IDS`; drop it from `_COMPANION_DEPENDENT_OPERATOR_IDS`; add `large_string` to the type matrix; omit `registry=`; make the 3c identity check always true; accept a subclass in 3c; drop Faker from the D7 set; drop the `native_threads` forwarding; report `calls` as a constant 1; swap `mask_key` for `job_seed` in selection.

## 6. Risk, rollback, docs, gates

- **Risk: R2.** A behavior change on the default full-frame path for admitted Faker tables. It is byte-identical by contract, and any doubt declines to the unchanged oracle. 3g changes the thread count for every compiled unified operator; that is safe because output is thread-invariant (test 7). 3h is additive evidence. Rollback is reverting the admission-table lines; the lane then declines Faker as today, and 3g/3h remain as independent fixes.
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
