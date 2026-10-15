Status: plan (Phase D, D3b: token vault on the bounded out-of-core route. rev 1, pre-Codex-gate. Lets a RAM-fitting pure-mask FK job that writes a token vault run on the out-of-core route instead of full-frame-or-reject, with vault side effects byte-identical to the oracle. HOLD build until Codex plan-gate GO. Autonomous merge on the full green gate per Cam's standing order (byte-identical side effects, no app-behavior change).)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; DE-02 vault-keyed fail-closed). Base: engine `origin/main` @ `1b2628e6`.

# D3b: token vault on the bounded out-of-core route

## 0. Why + scope

Today a relationship-bearing (FK) pure-mask job that requests a token vault (`vault_writer`) is disqualified from the bounded routes (`_pipeline_routing.py` `_sequential_eligible` returns `(False, "vault_writer_requested")`), so it runs full-frame if the byte estimate fits and is rejected (`fk_full_frame_oom_risk_rejected`) when too big. That caps vault jobs at full-frame memory. This slice lets such a job run on the out-of-core (OOC) route, collecting the vault in bounded memory, with the vault's persisted entries byte-identical to the full-frame oracle.

Scope: the **out-of-core route only**. The sequential (SEQ) route has no vault collector; a vault job that is SEQ-eligible but NOT OOC-compatible stays full-frame (documented decline). Native-SEQ vault is an explicit follow-on, not this slice. Validators (D3a), `fidelity_report`, and `post_validation` remain disqualified from the bounded routes (unchanged); only the `vault_writer` disqualifier is loosened, and only for OOC.

Non-goal: no change to the vault format, the DE-02 keyed-vault requirement, orphan handling, or any masked output. Masked outputs are already byte-identical on the OOC route (D0 baseline); this slice adds only the vault side effect.

## 1. What exists to build on

- **Vault collector:** `src/decoy_engine/vault.py` -- `collect_vault_entries(config, sources, outputs)` (`:171`) pairs `source[i]` with `masked[i]` POSITIONALLY per vaulted column (a column with `vault: true` + a `namespace`), skipping rows where either side is null, returning `(namespace, masked, source)` string triples. `VaultWriter.add(entries)` accumulates DEDUPLICATED (in-memory footprint bounded by DISTINCT triples, not row count); `write()` Fernet-encrypts in bounded chunks. `iter_vault_columns(config)` enumerates `(table, column, namespace)` for every `vault: true` column.
- **The chunked precedent (the exact pattern to mirror):** `execution/_chunked_oracle.py:393` -- per chunk, after masking and AFTER a fail-closed `row_errors` check, `vault_writer.add(collect_vault_entries(config, {table: chunk}, {table: masked}))` where `chunk` is the SOURCE batch and `masked` is its masked output in the SAME row order. This is the aligned collection point.
- **DE-02 keyed-vault guard:** `execution/_pipeline_context.py:309` (`assert_vault_writer_keyed`) -- the vault holds reversible plaintext and must be fail-closed on a keyed vault. Unchanged; the OOC path must honor it exactly as full-frame does (the guard runs in `run_pipeline` before routing, so it already applies).
- **OOC runner stream path:** `execution/out_of_core/_runner.py` -- `run_fk_out_of_core:112`; `_stream_table:303` ("mask + join per batch, then emit"). CRITICAL: `skip_columns = frozenset(col for edge in incoming_edges for col in edge.child_columns)` -- a table's INCOMING-FK child-key columns are SKIPPED from the per-batch mask and REMAPPED at the join (`_batch_join.py`), not masked in source order. Non-key columns and OUTGOING parent-key columns ARE masked per batch via `out_of_core/_mask.py mask_batch`, element-wise, SOURCE ORDER preserved.
- **Routing:** `_pipeline_routing.py` `_sequential_eligible` (`:172-228`) the shared bounded gate (the `vault_writer_requested` disqualifier at `:224`); `decide_execution_route:275`; OOC compat gate (`out_of_core_ready` ~`:428`); `out_of_core/_compat.py` reject codes.
- **Parity baseline:** `tests/parity/test_out_of_core_fk_parity.py` (D0 strict per-executor baseline: exact schema/metadata/nullability/row+col order/null-vs-NaN/int64-vs-float64 + native witness). `tests/parity/SEMANTIC_DIFFERENCES.md` (OOC documented diffs). **The build MUST read SEMANTIC_DIFFERENCES.md before implementing.**

## 2. The alignment crux (why this is byte-identical, and the decline)

The vault is a DEDUPED SET of `(namespace, masked, source)` triples, so its persisted content is ORDER-INDEPENDENT. Correctness requires only that each masked value is paired with ITS OWN source value. Collecting at the MASK step -- where `mask_batch` produced the masked non-skip columns from the source batch, row-for-row aligned, BEFORE the join remaps skip columns or reorders for emit -- gives exactly that pairing for every non-skip vaulted column. The downstream join reorder does not matter: it changes the emitted OUTPUT row order, not the source<->masked value pairs captured at mask time.

**Child-FK-key vault columns are the one unsafe case and are DECLINED.** A vaulted column that is a `skip_column` (an incoming-FK child key) is not masked at the mask step; its final value is the join's remap of the parent's masked key. Reproducing the oracle's `(namespace, remapped_child_fk, source_child_fk)` triples byte-identically on the OOC path would require re-aligning post-reorder and reconciling the remap + orphan policy + per-column namespace -- out of scope and error-prone. So: **if any vaulted column is a child-FK-key (skip) column, the job stays full-frame** (the disqualifier still fires for that job). A job that vaults only non-key / parent-key columns is admitted to OOC. This is the engine's byte-identical-or-decline discipline.

Note on parent-key vault parity: a vaulted outgoing parent-key column is masked per batch in source order, so its triples match the oracle. A vaulted child FK key referencing that parent would (in full-frame) add triples that are a subset of the parent's under a shared namespace, but the namespace/orphan interaction is not guaranteed byte-identical, which is the second reason the child-FK-key case declines rather than attempting partial capture.

## 3. The change

- **New module `out_of_core/_vault_collect.py`** (keeps the hook out of the 673-LOC `_runner.py`): a thin `collect_batch_vault(config, table, source_batch, masked_batch, vault_writer)` that calls `collect_vault_entries(config, {table: source_batch}, {table: masked_batch})` and `vault_writer.add(...)`, mirroring `_chunked_oracle.py:393`. It collects only the non-skip vaulted columns present in the masked batch (skip columns are absent there; admission guarantees no vaulted column is a skip column, so there is nothing to miss). Fail-closed ordering preserved: the existing per-batch `row_errors` / orphan fail-closed checks run BEFORE any vault add (a batch that will raise must not persist vault entries).
- **Hook in `_stream_table`** (`_runner.py`): after `mask_batch` yields the masked batch and after the existing fail-closed checks, call `collect_batch_vault(...)` when `vault_writer is not None`. One call site; the logic lives in the new module. Thread `vault_writer` into `_stream_table` / `run_fk_out_of_core` (it is already resolved in the pipeline context).
- **Routing admission** (`_pipeline_routing.py` / `decide_execution_route`): a vault job is admitted to the bounded route for OOC ONLY. Concretely: add a signal `vault_ooc_safe = vault_writer is not None and <no vaulted column is an incoming-FK child-key column>` (a new helper keyed on `iter_vault_columns(config)` vs the FK child columns from the graph). `_sequential_eligible` keeps returning `(False, "vault_writer_requested")` for its own SEQ purpose, BUT `decide_execution_route` admits the job to OUT-OF-CORE when it is otherwise OOC-compatible + fitting + `vault_ooc_safe` (so SEQ-only vault jobs and child-FK-key-vault jobs are unaffected and stay full-frame). Keep the reason strings: an admitted OOC vault job routes `("out_of_core", "byte_estimate_bounded_out_of_core")` (or the probe/estimate reason it already would). The exact wiring (whether to thread `vault_ooc_safe` through `_sequential_eligible` or gate it in the OOC branch of `decide_execution_route`) is a build decision; it MUST NOT admit SEQ vault or child-FK-key vault. Keep the `use_byte_estimate_routing=False` rollback intact.
- No change to `mask_batch`, the join, the vault format, orphan handling, or masked output.

## 4. Behavior / parity contract

- **Masked outputs: unchanged** (D0 baseline already byte-identical on OOC). This slice only adds the vault side effect.
- **Vault side effects BYTE-IDENTICAL to the full-frame oracle** for an admitted job: the persisted vault, decrypted, contains exactly the same set of `(namespace, masked, source)` triples (deduped) as the full-frame run of the same config. Null-skip semantics identical. DE-02 keyed-vault fail-closed identical.
- **Fail-closed ordering:** if any batch raises (row error / total-orphan), NO vault entry for that run is persisted (the writer's `write()` is only reached on a fully successful run, same as full-frame). Verify the writer is not flushed on an aborted run.
- **Declines stay full-frame, byte-identical:** a job vaulting a child-FK-key column; a vault job that is SEQ-eligible but not OOC-compatible; a too-big job (rejected as today). Assert the route is unchanged for these.
- **No new SEMANTIC_DIFFERENCES.md entry:** the vault is byte-identical or the job declines. If the build finds an unavoidable vault divergence, STOP and escalate.

## 5. Acceptance matrix

- **T1 admission:** an OOC-compatible, fitting, pure-mask FK job vaulting only non-key/parent-key columns now routes `("out_of_core", ...)` (assert route + reason); the same job too-big still routes/rejects as before; `use_byte_estimate_routing=False` rolls it back to the prior behavior.
- **T2 vault byte-identical:** run the admitted job on OOC and on `execution_mode="full_frame"`; decrypt both vaults; assert identical triple SETS (and identical decrypted payload under a fixed key). Cover: a vaulted parent-key column, a vaulted reversible payload column (fpe), a mix, nulls (skipped), an all-null vaulted column (no entries), and multiple vaulted columns across parent+child payload.
- **T3 child-FK-key vault declines:** a job vaulting an incoming-FK child-key column stays full-frame (assert route + reason unchanged); its vault is the oracle's (unchanged path). Negative control: if admission wrongly admitted it, the vault would differ -- assert the guard holds (a mutant dropping the child-key check is killed).
- **T4 SEQ-only vault unaffected:** a vault job that is NOT OOC-compatible stays full-frame (assert route). A mutant admitting SEQ vault is killed.
- **T5 fail-closed:** a job whose a batch raises a row error / total-orphan persists NO vault (assert the vault file is absent/empty and the run raised); matches full-frame's abort.
- **T6 masked output unchanged:** the admitted job's masked outputs remain byte-identical to the OOC baseline (D0 strict comparator) -- the vault hook did not perturb masking.
- **T7 bounded memory (witness, narrowed):** the vault writer's in-memory footprint stays bounded by distinct triples across batches (assert the collector is called per batch and the writer dedupes; no full-table materialization). No 10M run.

## 6. Failure modes (reproduce, do not smooth)

- A reordered/remapped value paired with the wrong source (vault corruption) -> prevented by collecting at the mask step on non-skip columns only; T2 + T3.
- A child-FK-key vault silently admitted and mis-collected -> T3 decline guard + mutant.
- SEQ vault silently admitted without a collector -> T4.
- Vault persisted on an aborted run (leak of partial reversible data) -> T5 fail-closed.
- The vault hook perturbing masked output or order -> T6.
- DE-02 keyed-vault guard bypassed on the OOC path -> assert the guard still fires (it runs pre-routing; confirm OOC does not skip it).

## 7. Verification + gates

- abi-3 companion venv for OOC parity (native kernels on the OOC route); ci-mirror for routing-decision/pure-Python. One test process via `~/bin/pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`.
- T1-T7 green; full existing OOC-parity + routing suites green; no masking/parity test weakened. Coverage + mutation on changed lines (the child-FK-key decline mutant, the SEQ-vault-not-admitted mutant, a dropped-fail-closed-order mutant must be killed).
- Module sizes: new logic in `out_of_core/_vault_collect.py` (small); `_runner.py` (673) gains only a thin call site -- keep <=700 (27 LOC headroom), census at exact LOC if it crosses 600... it is already >600, so it may only grow to <=700 with a census update, or extract to keep it from growing. `_pipeline_routing.py` (617) gains the admission signal -- keep <=700, census update. Prefer the new module for all non-trivial logic.
- Established-methodology note in the new module docstring (mirrors `_chunked_oracle` vault collection; cite it).
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build freely; autonomous merge on the full green gate (byte-identical side effects, no app-behavior change), flag Slack before merge-to-main, open PR, wait PR CI green, merge, confirm main CI. **In the Codex FINAL prompt, state explicitly: do NOT merge/push/PR; Opus merges after confirming CI** (a review prompt must never authorize an R3 action).

## 8. Done when

- [ ] T1-T7 green for the OOC route; declines (child-FK-key, SEQ-only, too-big) stay full-frame byte-identical; vault byte-identical to the oracle.
- [ ] Changed-lines coverage + mutation-proven (child-key decline, SEQ-not-admitted, fail-closed-order mutants killed); module sizes within policy.
- [ ] diff + evidence to Slack; PR CI green; merge; main CI green.
- [ ] ROADMAP + RECENTLY-SHIPPED updated (D3b closed, D3a still open); engine graphify refresh (subject to the keyless/main-checkout blocker -- note if deferred).

cam
