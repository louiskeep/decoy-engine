Status: plan (Phase D / faker SEQ-native slice. DRAFT rev 1, pre Codex plan-gate. faker as the 6th SEQ-native payload tenant.)

# Phase D / faker native on the sequential route

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md` (established-methodology, module size, pre-GA hard delete, explain-why comments). Grounding: the Phase D program plan `docs/plans/2026-10-10-phase-d-fk-routes-program.md` (§4/§5/§6); a 2026-10-13 FRAME survey (this session); Cam directive 2026-10-13 "maximize Rust fast-path coverage; no per-cell-Python PAYLOAD column should bottleneck the Rust columns in a mixed job" ([[decoy-rust-fast-path-everything]]). Base: engine `origin/main` @ `c61fb658` (post fpe-key).

## 0. Why + scope
faker masks a column by generating pool-backed values. On the sequential route its index derivation is ALREADY native (`PoolSampler._deterministic` calls `derive_index_batch`), but the assembly (`values = list(sampled)`, then a per-row `df[column] = [...]` null-merge loop, `_strategies/_faker.py:111,139`) is per-cell Python -> it bottlenecks the Rust columns in a mixed job. This slice moves that assembly onto the native Arrow path, making faker the 6th SEQ-native payload tenant.

The Arrow apply and admission already exist and are shared with the chunked route: `native/_operator_step.py:72` `sample_faker_array` (deterministic: `derive_index_batch` over the source array, null-safe gather into `pool.values`), `:134` `sample_faker_array_positional` (non-det REUSE by global row position), `_faker_deterministic_admission.py`, `_faker_positional_admission.py`, `_faker_null_mask.py`, and the shared `resolve_faker_pool_identity`. The index kernel is the SAME `load_compiled_index_kernel` / `BoundIndexKernel` the categorical/bucket/date_shift tenants already load on SEQ. The OOC deferral (`_compat.py` `_DEFERRED_GROUP_B["faker"]`, cross-batch pool cache) does NOT apply: SEQ loads each table whole (`_sequential.py:212`), and the handler holds `ctx.registry` + `ctx.pool_cache`.

**In scope:** deterministic (source-keyed) faker, and non-det REUSE (positional, job-seed-keyed) as a same-slice follow (same kernel), on a STRING-output source, for the native provider allowlist only (`{person_first_name, person_last_name}`, `_operator_registry.py:214`).
**Out of scope / explicit DECLINE to Python (caller-visible reason):** the non-det UNIQUE / MATCH / SCALE cardinality modes (need whole-column RNG state), non-allowlisted providers, non-string sources, the `when:` subset where the positional admission rejects it, and no-companion. faker as an FK KEY is non-injective (pool-select) and auto-declines through the existing `{hash,fpe}` key-eligibility gate (the fpe-key slice's `sequential_fk_key_injectivity_not_guaranteed` path) -- this slice adds no key support, only an acceptance test that it declines.

## 1. Risk
R2 (privacy-relevant masking; determinism/joinability matter). faker output must be byte-identical to the Python handler (same pool identity, same index draw, same null placement). Acceptance compares SEQ-native to the pandas oracle on the strict 9-dimension contract + a native witness; the chunked route already ships this kernel (C5a/b/c), so the pool/identity/kernel are proven, this is the SEQ wiring.

## 2. The code change
- **FakerCall + faker_op** (`_sequential_native_ops.py`): a `FakerCall` payload carrying the BUILT pool (+ identity, mode, namespace/seed), and a `faker_op(load_index)` `NativeOp` (disposition `SEQ_FAKER_DISPOSITION_KEY = "sequential_faker"`). Admission reuses `_faker_deterministic_admission` / `_faker_positional_admission` (allowlist, string-output, pool_size, `when:` rules). Keep `ctx.registry` OUT of the descriptor.
- **bespoke `faker_column` method** (`_sequential_native.py`, mirroring `categorical_column:312` / `bucket_column:335`): builds/fetches the pool in the handler (`PoolBuilder(ctx.registry)`, `ctx.pool_cache`, `resolve_faker_pool_identity`), picks the leg from the plan's determinism/cardinality mode, calls `sample_faker_array` (deterministic) or `sample_faker_array_positional` (REUSE), applies the null mask, and reports the disposition. Decline (return None -> Python) for the out-of-scope modes/providers/types with a coded reason.
- **faker handler seam** (`_strategies/_faker.py`): where it calls the native seam (like fpe/categorical handlers), route the deterministic + positional-REUSE legs through `seq.faker_column(...)`; the UNIQUE/MATCH/SCALE legs stay Python.
- **register faker in `_OPS`.**
- faker is NOT added to `_KEY_STRATEGIES`; a faker FK-key column hits the existing decline path unchanged.

## 3. Acceptance matrix (the spec)
Each masking cell runs full-frame oracle vs SEQ-native vs SEQ-python-fallback (companion hidden), asserts `tests/parity/_strict_compare.compare_strict == _ALL_TRUE` (output tables), with the native witness proving the index kernel ran (non-vacuous) and warnings on their own channel.
- **F1 deterministic, allowlisted provider, string source** (person_first_name, person_last_name): byte-identical to the oracle; native witness; across a range of pool_size and row counts incl. all-null and empty.
- **F2 non-det REUSE (positional)**: byte-identical to the oracle on the whole-table positional draw (row_offset 0); native witness.
- **F3 null handling**: a source column with nulls -> nulls preserved positionally (null-in/null-out), strict parity incl. `null_vs_nan`.
- **F4 determinism**: same job run twice yields identical output; the deterministic draw is f(mask_key, namespace, canonicalized source), the positional is f(job_seed, selection_namespace, row position) -- pin both.
- **F5 declines (stay Python, coded reason, output still oracle-parity)**: UNIQUE / MATCH / SCALE cardinality modes; a non-allowlisted provider; a non-string source; no companion (ci-mirror); and `when:` where the positional admission rejects it.
- **F6 faker as an FK key declines**: a faker FK-key column records the existing `sequential_fk_key_injectivity_not_guaranteed` decline (payload faker on the same job still native), proving faker did not leak into `_KEY_STRATEGIES`.
- **F7 mixed job**: faker payload native beside a hash payload and a categorical payload -> each reports under its own disposition key, no cross-contamination.

## 4. Failure modes (reproduce, do not smooth)
- Picking the wrong leg (deterministic vs positional) silently changes the keying -> F1/F2/F4 pin each.
- A pool-identity mismatch between SEQ and the oracle -> different values; reuse `resolve_faker_pool_identity`, pin byte-parity.
- Null placement drift (the non-string null mask uses an oracle `to_pandas` path) -> F3.
- A non-allowlisted provider or cardinality mode silently going native -> F5 (must decline).

## 5. Verification + gates
- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python`; ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python`; one test process at a time via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- F1-F7 pass in abi-3 (native witness non-vacuous); companion-independent parts + the declines in ci-mirror. Full existing SEQ suite stays green (the 5 tenants + fpe-key + Dcomp).
- Coverage + mutation on every changed production line; prove any surviving mutant equivalent by injection.
- Module size: `_sequential_native.py` is 556; `faker_column` (~40-60 LOC) may push it to ~600-615. 600 is the census GOAL, 700 the hard MAX: if it lands at/over 600, add the census entry at the exact LOC (`tests/sentry/test_module_size.py`) rather than split (well under 700); also update the d2b hard-cap sentry (`test_phase_d_d2b_seq_shapes.py` `loc(_sequential_native) <= 600`) if it crosses -- prefer trimming to stay <=600 if cheap. Measure post-build and decide.
- Gate chain: Opus VERIFY (diff + both venvs + equivalence proofs) -> dennis -> Codex final. Merge under the standing Rust rule (dennis GO + Codex GO + CI green + faker parity contract + faster on the faker step + within memory ceiling).

## 6. Done when
- [ ] F1-F7 green (deterministic + positional native byte-parity + witness; declines; FK-key decline; mixed-job isolation); full SEQ suite green both venvs.
- [ ] Changed production lines coverage+mutation-proven; `_sequential_native.py` within the size policy (census entry if >=600, or trimmed <=600); sentries green.
- [ ] ROADMAP + RECENTLY-SHIPPED updated; graphify refreshed to the merge SHA; Slack recap in plain language.

cam
