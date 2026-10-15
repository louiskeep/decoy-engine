Status: plan (Phase D, D3b: token vault on the bounded out-of-core route. rev 2 (Codex plan-gate rev1 REVISE folded: 2 HIGH + 2 MED -- string-only vaulted columns for byte-identity, BOTH OOC drivers covered, the real transactional vault contract, and the fixture/premise fixes). HOLD build until Codex plan-gate GO. Autonomous merge on the full green gate per Cam's standing order; the Codex FINAL prompt must forbid the reviewer from merging.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; DE-02 vault-keyed fail-closed). Base: engine `origin/main` @ `1b2628e6`.

# D3b: token vault on the bounded out-of-core route

## 0. Why + scope

A relationship-bearing (FK) pure-mask job that requests a token vault (`vault_writer`) is disqualified from the bounded routes (`_pipeline_routing.py` `_sequential_eligible` -> `(False, "vault_writer_requested")`), so it runs full-frame if the byte estimate fits and is rejected (`fk_full_frame_oom_risk_rejected`) when too big. This slice lets such a job run on the out-of-core (OOC) route, collecting the vault in bounded memory, with the persisted vault byte-identical to the full-frame oracle.

Scope (narrowed by the plan gate): the **out-of-core route only**, and only for jobs whose vaulted columns are all **string/binary-typed non-key columns** (details in §2). A vaulted column that is numeric/float/temporal/decimal, or an incoming-FK child-key column, keeps the job on full-frame. SEQ has no vault collector, so a vault job that is SEQ-eligible but not OOC-compatible stays full-frame (SEQ-native vault is a follow-on). Validators (D3a), `fidelity_report`, `post_validation` stay disqualified (unchanged). No change to the vault format, DE-02, orphan handling, or any masked output.

## 1. What exists to build on

- **Vault collector:** `vault.py` -- `collect_vault_entries(config, sources, outputs)` (`:171`) pairs `source[i]` with `masked[i]` positionally per vaulted column, SKIPS rows where either side is `None`, returns `(namespace, str(masked), str(source))` triples. It stringifies with `str()` and skips only `None` (NOT float NaN). `VaultWriter.add(entries)` dedups; it DROPS ambiguous keys (same masked -> different source) and tracks `ambiguous_dropped`. `iter_vault_columns(config)` enumerates `(table, column, namespace)` for `vault: true` columns. `write()` Fernet-encrypts in bounded chunks and is **caller-owned** (see §4).
- **Config check:** `plan/_checks.py:519` `check_vault_columns` -- `fpe` with `vault: true` is REJECTED (`vault_strategy_reversible`). So fpe is never a vaulted strategy (it is a negative compile test, not a runtime case).
- **Chunked precedent:** `_chunked_oracle.py:393` -- per chunk, `vault_writer.add(collect_vault_entries(config, {table: chunk}, {table: masked}))`, source batch + masked batch in the same order.
- **OOC runner -- TWO drivers (plan-gate HIGH-2):** `out_of_core/_runner.py run_fk_out_of_core:112` dispatches a table to EITHER `_runner._stream_table:303` (in-memory reassembly) OR `_stream_driver.stream_table` (`:254`, sink/reorder path, selected at RUNTIME by parent-key cardinality + budget). BOTH must collect the vault. Both mask per batch (element-wise, source order preserved) before any join reorder/staging; the reorder driver checks total orphans AFTER its mask/staging pass. `skip_columns = union of incoming edges' child_columns` are present with RAW values at the mask step and remapped at the join (`_batch_join.py`) -- they are NOT the masked value there.
- **DE-02 keyed-vault guard:** `_pipeline_context.py:309` `assert_vault_writer_keyed`, runs in context construction BEFORE routing (so it already applies to an OOC-routed job). Confirmed by the plan gate.
- **Routing:** `_pipeline_routing.py` `_sequential_eligible:172-228` (disqualifier `:224`); `decide_execution_route:275`; a fitting vault job currently returns at the full-frame-fit branch BEFORE the bounded block, so admitting OOC vault needs a new branch there (not just the final OOC branch). `fk_ooc_dtype_hazard`, cycles, compat still apply.
- **Parity:** `tests/parity/test_out_of_core_fk_parity.py` (D0 baseline) + `tests/parity/SEMANTIC_DIFFERENCES.md` -- D0 is NOT strict byte-identity with full-frame; it RECORDS OOC diffs (NaN-vs-null, int64-vs-float64, metadata). **The build MUST read SEMANTIC_DIFFERENCES.md.** These documented diffs are exactly why numeric/float vaulted columns are declined (§2).

## 2. Byte-identity: why string/binary-only, and the declines (plan-gate HIGH-1)

The vault stores `(namespace, str(masked), str(source))` as a deduped set. `collect_vault_entries` uses `str()` and skips only `None`. The documented OOC-vs-full-frame divergences therefore LEAK into the vault strings:
- **int64-vs-float64:** a nullable-int column is float-widened on full-frame (`to_pandas_fk_safe`) but stays int64 on OOC, so `str(source)`/`str(masked)` differ (`"7.0"` vs `"7"`). Verified counterexample: numeric redact replacement `7` with a null elsewhere -> full-frame `("payload","7.0","secret")` vs OOC `("payload","7","secret")`.
- **NaN-vs-null:** a float NaN is skipped-as-null on one route but stringified `"nan"` on the other -> an extra `("ns","nan","nan")` triple. Verified counterexample: a passthrough parent key column containing NaN.

Both survive dedup, so the persisted vault would NOT be byte-identical. Fix: **admit a job only when every vaulted column's SOURCE dtype is string/binary** (`utf8`/`large_utf8`/`binary`/`large_binary`) -- these do not float-widen or NaN-diverge, and the vault-eligible string strategies (hash, categorical, truncate, redact-token, passthrough-of-string; NOT fpe) produce string masked output whose `str()` is identical on both routes. **Decline** (keep full-frame) any job with a vaulted column that is numeric/float/temporal/decimal, or an incoming-FK child-key (skip) column. The admission check compares each vaulted `(table, column)` against EVERY component of EVERY graph edge (covers composite / overlapping / multi-parent / self-ref) for the child-key decline, and checks the source schema dtype for the string-only rule.

This narrows D3b to the common vault case (reversible recovery of masked string IDs/pseudonyms) while guaranteeing byte-identity. If a future slice wants numeric vaulted columns, it needs a route-stable representation policy (out of scope; do NOT add a SEMANTIC_DIFFERENCES entry here).

## 3. The change

- **New module `out_of_core/_vault_collect.py`:** `collect_batch_vault(config, table, source_batch, masked_batch, vault_writer)` -> `vault_writer.add(collect_vault_entries(config, {table: source_batch}, {table: masked_batch}))`, collecting the admitted (string, non-skip) vaulted columns present in the masked batch. Thin; mirrors `_chunked_oracle.py:393`.
- **Hook BOTH drivers (HIGH-2):** call `collect_batch_vault` at the mask step in `_runner._stream_table` AND `_stream_driver.stream_table`, after `mask_batch` and after that driver's existing fail-closed checks, when `vault_writer is not None`. Thread `vault_writer` + `config` through `run_fk_out_of_core` and `_pipeline_route_dispatch`/`_pipeline_route_exec` to both drivers. One shared helper, two call sites.
- **Routing admission:** add a `vault_ooc_safe` signal = `vault_writer is not None` AND every vaulted column is string/binary-typed AND no vaulted column is an incoming-FK child key. In `decide_execution_route`, add an OOC-vault admission branch BEFORE the full-frame-fit return so a fitting `vault_ooc_safe` job that is otherwise OOC-compatible (passes compat, not cyclic, not `fk_ooc_dtype_hazard`, all other disqualifiers clear) routes to OUT-OF-CORE with its normal reason; it must NOT enable SEQ vault (SEQ-only-eligible vault jobs stay full-frame) and must NOT admit a child-key / numeric vault job. Keep the `use_byte_estimate_routing=False` rollback. `_sequential_eligible` stays as-is for its SEQ purpose.
- No change to `mask_batch`, the join, the vault format/`write()`, orphan handling, or masked output.

## 4. Transactional contract (plan-gate MED-3) + parity

- **Persistence is caller-owned.** The engine never calls `VaultWriter.write()`; `_pipeline_generate_mask.py:296` (the caller) owns it. OOC masking failures RAISE directly (no `row_errors` result object like the chunked route). The build MUST confirm the caller calls `write()` ONLY after the OOC run returns successfully, so a raised run persists NOTHING even though earlier batches already called `vault_writer.add(...)` (the writer object is discarded with the failed run). If the caller's `write()` is not strictly success-gated, that is a BLOCKER to fix (no partial reversible-plaintext persist). Specify the contract explicitly: **a failed run persists no vault.**
- **Vault side effects byte-identical** to full-frame for an admitted job: decrypt both vaults; assert identical triple sets AND identical `ambiguous_dropped` accounting (dedup drops ambiguous keys, so repeated/colliding sources must match). Null-skip identical. DE-02 keyed-vault fail-closed identical (guard is pre-routing).
- **Masked outputs unchanged** from the existing OOC baseline (D0 strict comparator) -- NOT "byte-identical to full-frame" (D0 records OOC diffs). The vault hook must not perturb masking or order.
- **No new SEMANTIC_DIFFERENCES.md entry.** Byte-identical-or-decline.

## 5. Acceptance matrix

- **T1 admission + declines:** a fitting OOC-compatible pure-mask FK job vaulting only string non-key columns routes `("out_of_core", ...)`; jobs that (a) vault a numeric/float/temporal/decimal column, (b) vault an incoming-FK child-key column, (c) are SEQ-eligible but not OOC-compatible, (d) are too-big -- each stays full-frame/reject with the unchanged reason. `use_byte_estimate_routing=False` rolls back. Mutants: dropping the string-only check, dropping the child-key check, admitting SEQ vault -- each killed.
- **T2 vault byte-identical (incl. the HIGH-1 counterexamples as declines):** admitted job on OOC vs full-frame -> identical decrypted triple sets + `ambiguous_dropped`. Cover: a vaulted string parent-key column, a vaulted string payload column (hash/categorical/truncate/redact), multiple vaulted columns, nulls (skipped), all-null column (no entries), repeated + conflicting-source collisions (ambiguous drop). Add the two HIGH-1 cases (numeric redact `7`/`7.0`; passthrough NaN parent key) as ADMISSION DECLINES (assert full-frame, not an OOC vault), proving the string-only rule prevents the divergence.
- **T3 child-FK-key vault declines:** vaulting an incoming-FK child-key column (incl. composite / overlapping-parent-and-child / multi-parent) stays full-frame; assert route + reason; mutant dropping the all-edge-component check killed.
- **T4 both drivers collect:** force the reorder/sink driver (`_stream_driver.stream_table`, threshold forced) AND the in-memory driver; both produce the byte-identical vault. A mutant hooking only one driver is killed (the other driver's vault would be incomplete). Witness which driver ran.
- **T5 fail-closed persists nothing:** a late masking failure and a reorder-path total-orphan failure each abort the run and persist NO vault; assert via instrumenting `add` + the caller's `write()` (not file-absence alone) -- a mutant that persists after a late failure is killed.
- **T6 masked output unchanged:** admitted job's masked outputs == the existing OOC baseline (D0 strict comparator); the vault hook did not perturb masking.
- **T7 memory (narrowed, accurate):** the collector is called per batch and the writer dedups, so storage is O(DISTINCT triples) -- which can be O(rows), NOT a fixed bound; the RAM-fitting routing restriction is what bounds it. Assert per-batch collection + dedup; no full-table source/output materialization for the vault. No 10M run.
- **T-neg fpe+vault:** remains a config-reject (`vault_strategy_reversible`) -- a negative compile test, not an OOC case.

## 6. Failure modes (reproduce, do not smooth)

- Numeric/NaN vault divergence silently admitted -> T1/T2 string-only declines.
- A vaulted column collected via only one of the two drivers -> T4.
- Child-FK-key vault admitted + mis-collected -> T3.
- Vault persisted on an aborted run (reversible-plaintext leak) -> T5.
- The vault hook perturbing masked output/order -> T6.
- DE-02 keyed-vault guard bypassed on OOC -> assert it still fires.

## 7. Verification + gates

- abi-3 companion venv for OOC parity; ci-mirror for routing/pure-Python. `~/bin/pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`.
- T1-T7 + T-neg green; full OOC-parity + routing suites green; no masking/parity test weakened. Coverage + mutation on changed lines (string-only, child-key, SEQ-not-admitted, single-driver, persist-after-failure mutants killed).
- Module sizes: new logic in `out_of_core/_vault_collect.py`; `_runner.py` (673) + `_stream_driver.py` (595) + `_pipeline_routing.py` (617) gain thin call sites/signals -- keep <=700, census at exact LOC if crossed; extract rather than grow a >600 file where possible.
- Established-methodology note in the new module (mirrors `_chunked_oracle` vault collection; cite it).
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build freely; autonomous merge on the full green gate (byte-identical side effects), flag Slack before merge-to-main, open PR, wait PR CI green, merge, confirm main CI. **The Codex FINAL prompt MUST say: do NOT merge/push/PR; Opus merges after CI.**

## 8. Done when

- [ ] T1-T7 + T-neg green; declines (numeric/float, child-FK-key, SEQ-only, too-big) stay full-frame; vault byte-identical to the oracle on both drivers; no persist on a failed run.
- [ ] Changed-lines coverage + mutation-proven; module sizes within policy.
- [ ] diff + evidence to Slack; PR CI green; merge; main CI green.
- [ ] ROADMAP + RECENTLY-SHIPPED updated (D3b closed, D3a open); engine graphify refresh (note if deferred).

cam
