Status: plan (revision 1, author = Opus). Awaiting Codex plan gate.

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
2. **The unified slice declines Faker in exactly two places:**
   - (a) `"native_faker_select"` is absent from `ALLOWED_OPERATOR_IDS` (`_unified_slice_admission.py:84`), so `resident_contract_admission` returns None.
   - (b) `_unified_slice.py:242` constructs `ShadowCoordinator(ctx=ctx)` with no `registry`, and `_resolve_pool` asserts on a bound Faker node when `registry is None`. The coordinator docstring (~:216) calls this deliberate.
3. **The oracle selection is source-keyed and already compiled.** `PoolSampler._deterministic` calls `derive_index_batch(values, mask_key, namespace, pool.size)` then gathers `pool.values[idx]`, with nulls kept by position. `sample_faker_array` reproduces this exactly: `select_seed = mask_key`, pool build on `job_seed`, `pool_size = pool.size` as built. Cell-for-cell parity is already pinned by `tests/physical/test_shadow_corpus.py`, `test_shadow_faker_lifecycle.py` and `tests/native/test_sample_faker_array.py`. C5a's win is removing the pandas wrapping and the frame round-trip, not compiling selection.
4. **Faker carries no diagnostic obligation that admission would reject.** Its capabilities (`native/_capabilities.py:228`) declare no `warning_codes` and no `row_error_modes`, so `diagnostic_reducers` is empty. It needs no prepass (row-local, not global). Its only tag is the class-A `pool_quality` obligation, which has no full-frame consumer (it is enforced only by the standalone chunked `enforce_pool_quality`). The full-frame oracle does not read `PoolCache.warnings()` into the job result either. Only `_route_diagnostics` (chunked) and `_pipeline_auto_chunk` read it.
5. **Output reconstruction already matches the Faker oracle's assignment.** The oracle writes `df[column] = <python list, None for nulls>` (`_strategies/_faker.py:97-99`). The unified slice overlays `masked.to_pylist()` positionally on `candidate.source_frame`, then runs the legacy adapter's own `pa.Table.from_pandas`. The zero-row special case at `_unified_slice.py` (~:316, the `to_pandas()` empty-dtype path) needs a Faker-specific check (see 3d).

## 3. Decisions

**3a. Admission table entries** (`_unified_slice_admission.py`). Add `FAKER_OPERATOR_ID = "native_faker_select"` to:
- `ALLOWED_OPERATOR_IDS`
- `_COMPANION_DEPENDENT_OPERATOR_IDS`
- `_OPERATOR_REQUIRED_KERNEL` → `"index"`

Also add `_ADMITTED_RESIDENT_TYPES["faker"] = {pa.string()}`. Do NOT add `large_string`, even though the binder accepts it. The matrix is "widened only by a separately proven slice" (module comment ~:148), and every sibling index operator admits `string` only. A `large_string` Faker column therefore declines the whole table to the oracle, as it does today.

The UTF-8 namespace check and the per-operator kernel gate in `resident_contract_admission` apply through the existing companion-dependent branch with no new code. Faker reaches that branch only after `_faker_pool_bindable` has bound the node; an unbound node already returns None at the `binding is None` check.

**3b. Thread the registry into the coordinator** (`_unified_slice.py:242`): `ShadowCoordinator(ctx=ctx, registry=inputs.registry)`. Use `inputs.registry`, the one resolved `ProviderRegistry` the physical plan compiled against, so the binder's `classify_provider` and the coordinator's `PoolBuilder` see the same registry. Update the coordinator docstring (~:216-219) that says the unified slice never admits Faker. `ShadowContext.from_key_provider` already supplies a real `job_seed`, so the job_seed guard holds.

**3c. Non-string pool from a custom registry: reroute, do not fail closed.** Admission proves only that the provider NAME is allowlisted and `pool_native`. A custom registry can rebind that name to an adapter yielding non-strings, which the coordinator detects only after building the pool (`FAKER_POOL_NON_STRING_OUTPUT`). Today any coded `ShadowDifference` on an admitted table raises `UnifiedSliceInvariantError` (D8). A legitimate, oracle-runnable job would crash, which is a regression from today's behavior (it runs on the oracle).

Decision: in the `except ShadowDifference` block, if `exc.code == FAKER_POOL_NON_STRING_OUTPUT`, return None. The table falls through to the oracle, which is the date_shift routed-obligation precedent of rerouting before anything is published. Every other code keeps the D8 fail-closed. This is safe because the coordinator raises it inside `_resolve_pool`, before any output exists, and the unified slice publishes nothing until `run` returns.

The reroute must be an explicit, named single-code allowlist (a module constant), not a broad catch. Alternative considered and rejected: building pools during admission. That duplicates the coordinator's build-once map, and it costs a pool build for tables that then decline for other reasons.

**3d. Zero-row and all-null parity.**
- Zero rows: the oracle's `df[column] = []` on an empty frame gives pandas float64, which Arrow writes as `double`. The unified slice's empty-table branch uses the coordinator's `to_pandas()` empty dtype, which for Faker is `pa.string()`. The builder must run the oracle and the lane on a zero-row Faker table and make them agree. If they differ, extend the existing empty-table branch so a Faker column follows the tokenizing (`to_pylist`) rule, matching how the branch already special-cases bucket_perturb. Fix it at that branch, not by post-hoc casting. Record the observed oracle type in the build record.
- All-null column: assert identical values, type and `b"pandas"` metadata.

**3e. D7 positive evidence.** Extend the hash-only D7 check (`_unified_slice.py` ~:276) to a set `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS = {HASH_OPERATOR_ID, FAKER_OPERATOR_ID}`. A completed Faker node must carry `compiled_kernel_executed=True`. `run_operator` sets it only after `sample_faker_array` returns from the compiled kernel, which `load_compiled_index_kernel` resolves (never the reference fallback; admission has already required the index kernel).

**3f. Pool cache scope.** The coordinator's run-scoped fresh `PoolCache` stays. Pool content is a pure function of the identity (provider, size, locale, config, namespace, job_seed), so a fresh cache gives byte-identical values to the oracle's caller-supplied cache. No warning parity is lost, because the full-frame oracle surfaces no pool-cache warnings (fact 4). The builder must confirm this with a test asserting equal `result.warnings` and quality evidence between lane-on and lane-off for an admitted Faker table.

## 4. Implementation

1. `_unified_slice_admission.py`: the 3a entries and the `FAKER_OPERATOR_ID` constant. Update the comments that enumerate companion-dependent operators.
2. `_unified_slice.py`: pass the registry (3b); add the single-code reroute (3c); add the D7 operator set (3e); fix the zero-row branch if needed (3d).
3. `physical/_shadow_coordinator.py`: docstring only.
4. No Rust, no new kernel, no change to `sample_faker_array`, `_faker_pool_bindable`, the chunked route, or the oracle.
5. Docs: CHANGELOG entry; compatibility-contract line if the unified-slice admitted set is listed there; build record `docs/records/2026-10-05-c5a-unified-slice-faker-build.md`.

## 5. Acceptance tests (written first; red-before recorded)

New file `tests/physical/test_unified_slice_faker.py`, plus additions to `test_unified_slice_admission.py` and `test_unified_slice_parity.py`. Every lane-vs-oracle compare uses the strictest existing equality helper in `test_unified_slice_parity.py`: values, Arrow field types, column order and schema metadata, plus `result.warnings` and per-column evidence.

1. **Admits and runs native.** A single-table job with one in-scope Faker column plus a passthrough column: the lane admits, the node evidence shows `operator == "native_faker_select"` and `compiled_kernel_executed is True`, and output equals the oracle exactly.
2. **Mixed operators.** Faker plus hash, categorical and redact in one table: admitted, and identical to the oracle.
3. **Two Faker columns sharing one pool identity** (same provider, size, namespace): admitted, identical, and the pool is built once (spy on `PoolBuilder.build` call count == 1).
4. **Two Faker columns with different namespaces:** identical to the oracle.
5. **Nulls:** a column with interleaved nulls keeps them by position; an all-null column; a zero-row table (3d). All identical to the oracle, including Arrow type.
6. **Determinism:** two runs give identical bytes; a different `mask_key` changes the output (wrong-key mutant guard); a different `job_seed` changes pool content.
7. **Declines to the oracle** (lane returns None, job output equals the lane-off run), one parametrized case each:
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
   - companion index kernel unavailable (monkeypatched `native_kernel_availability`)
8. **3c reroute:** a custom registry rebinding `person_first_name` to a poolable adapter yielding ints. The lane returns None (no `UnifiedSliceInvariantError`) and the job output equals the oracle's. A sibling test: any OTHER coded ShadowDifference raised from the coordinator still raises `UnifiedSliceInvariantError` (the reroute is single-code).
9. **3b registry:** with a custom registry whose `person_first_name` pool differs from the default, the lane output equals the oracle run with that same registry. This proves the coordinator uses `inputs.registry`, not a default.
10. **3e D7:** a stubbed coordinator result where the Faker node has `compiled_kernel_executed=False` raises `UnifiedSliceInvariantError`.
11. **Warnings/evidence parity (3f):** `result.warnings` and quality evidence are equal lane-on vs lane-off.
12. **Companion-absent clean env** (ci-mirror): Faker tables decline, and no companion-only assertion runs unguarded (`@NEEDS_COMPANION` where needed).

Mutation targets (hand-mutants, each must be killed): drop Faker from `ALLOWED_OPERATOR_IDS`; drop it from `_COMPANION_DEPENDENT_OPERATOR_IDS`; add `large_string` to the type matrix; omit `registry=`; widen the reroute to all codes; drop Faker from the D7 set; swap `mask_key` for `job_seed` in selection (should already be killed by the existing corpus).

## 6. Risk, rollback, docs, gates

- **Risk: R2.** A behavior change on the default full-frame path for admitted Faker tables. It is byte-identical by contract, and any doubt declines to the unchanged oracle. Rollback is reverting the two admission-table lines; the lane then declines Faker as today.
- **Gates:** Codex plan gate → Sonnet build (tests first) → dennis → Codex final → ci-mirror companion-absent → Cam merge authorization.
- **Docs:** CHANGELOG, compatibility contract (if listed), build record; platform RECENTLY-SHIPPED on merge.

## 7. Open questions for the plan gate

1. Is 3c's single-code reroute the right call, or should a custom-registry provider simply fail admission? (For example: decline whenever `registry` is not the default registry. That is simpler, but it would decline legitimate custom registries.)
2. Does any full-frame result surface carry pool-cache warnings that fact 4 missed? (The test 11 parity check will catch it empirically, but the plan should be right up front.)
3. Is `inputs.registry` guaranteed identical to the registry the oracle path would use for the same call (for example when `run_pipeline` gets `registry=None` and resolves a default)?
