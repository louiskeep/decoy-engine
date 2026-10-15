Status: plan (Phase D, D3a: validators on the bounded out-of-core route. rev 1 DRAFT. HOLD build until Codex plan-gate GO. Autonomous merge on the full green gate per Cam's standing order; the Codex FINAL prompt must forbid the reviewer from merging.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; validation-never-mutates). Base: engine `origin/main` @ `4422e030`.

# D3a: validators on the bounded out-of-core route

## 0. Why + scope

A relationship-bearing (FK) pure-mask job that declares a `validators:` block is disqualified from the bounded routes today (`_pipeline_routing.py` `_sequential_eligible` -> `(False, "validators_present")` at :219), so it runs full-frame if the byte estimate fits and is rejected (`fk_full_frame_oom_risk_rejected`) when too big. This slice lets such a job run on the out-of-core (OOC) route, evaluating validators in bounded memory, with the `ValidationReport`, fail-closed behavior, and `quality_metrics["validation"]` telemetry matching the full-frame oracle. It extends the same GAP6 win D3b extended (the bounded route is measured faster and lower-memory than the full-frame oracle for RAM-fitting FK jobs) to validator-bearing jobs.

Scope: the **out-of-core route only**, for jobs whose validators are ALL **per-cell** checks (allowlist, §2) evaluated in the **fail-closed (raise) mode only**, on a **RAM-fitting** job (§3 fit-gate). Any cross-row / cross-table validator, any job coupling a validator to **quarantine** row-removal, a SEQ-eligible-but-not-OOC-compatible job, or a too-big / unpriceable job keeps the job on full-frame (or its existing reject). No change to validator semantics, the `ValidationReport` contract, quarantine, the vault, orphan handling, or any masked output. D3b (vault) shipped; this is its direct sibling and the D3b plan was written to leave D3a next.

## 1. What exists to build on

- **Validators run full-frame ONLY today:** `execution/_pipeline_finalize.py::finalize_validators_and_quarantine` (`:374`) runs on the branch that did NOT take a routed early return; its module docstring (`:1-21`) states these are POST-mask, full-frame-only concerns, and that `run_sequential_route` / `run_out_of_core_route` own their own telemetry and never reach it. So the OOC route runs NO validators today. It calls `validators._registry.validate(outputs, config, sources=caller_sources)` on the whole `outputs: dict[str, pa.Table]` (all tables resident), then (on failure + a quarantine trigger) `apply_quarantine` rebinds `outputs` to filtered tables.
- **The registry + the unbounded step:** `validators/_registry.py::validate` (`:75`) dispatches all 11 built-ins against the whole `outputs` dict. The memory-unbounded step the bounded route must avoid is per-validator full-column materialization: `validators/_catalog.py::_check_digit_validator` (luhn/npi/iban/vin) does `col = table.column(col_name); values = col.to_pylist()` -- a full-column Python list per validated column. On a large OOC job that `to_pylist()` defeats the bounded-memory contract.
- **Per-cell vs cross-row classification (the allowlist basis):**
  - **Per-cell, streamable** (each cell's verdict is row-local, independent of other rows/tables): `luhn`, `npi`, `iban`, `vin` (`_catalog.py` check-digit family), `regex_match`, `column_in_set` (`_catalog.py`). These evaluate correctly batch-by-batch with bounded, additive findings.
  - **Cross-row / cross-table, NOT bounded per batch:** `fk_intact`, `no_orphan_children` (`validators/_fk_validators.py`, materialize parent PK set, O(distinct parent keys)); `leak_check` (`validators/_leak_check.py`, positional output-vs-source compare, `failing_row_indices` unbounded under heavy leak); `reconciliation_holds`, `parent_window_respected` (need parent aggregates / parent windows resident). These DECLINE to full-frame.
- **OOC drivers -- TWO (mirror D3b HIGH-2):** `out_of_core/_runner.py run_fk_out_of_core:112` dispatches a table to EITHER `_runner._stream_table:303` (in-memory reassembly) OR `_stream_driver.stream_table` (sink/reorder path), selected at RUNTIME by parent-key cardinality + budget. BOTH mask per batch (element-wise, source order preserved). BOTH must evaluate the admitted validators. `skip_columns` (incoming-edge child keys) carry RAW values at the mask step and are remapped at the join -- validators that target such a column must be treated with care (see §2/§4).
- **Validation contract (what to match):** `ValidationReport` is frozen; validation NEVER mutates (CLAUDE.md best-practices §2.1). Fail-closed raises `ValidatorFailedError`; nulls are skipped as today; `quality_metrics["validation"]` is stamped. The oracle behavior in `finalize_validators_and_quarantine` is the exact telemetry/fail-closed contract to reproduce.
- **Routing:** `_pipeline_routing.py` `_sequential_eligible:172` (disqualifier `:218-219`); `decide_execution_route:275`; a fitting validator job currently returns at the full-frame-fit branch BEFORE the bounded block, so admitting OOC validators needs a NEW admission branch there (exactly where D3b added its `vault_ooc_safe` branch, next to the `vault_writer_requested` disqualifier at :224). `resolve_full_frame_fits_estimate` (:117) is the fit signal D3b reused.
- **Parity:** `tests/parity/test_out_of_core_fk_parity.py` (D0 baseline) + `tests/parity/SEMANTIC_DIFFERENCES.md`. D0 is NOT strict byte-identity with full-frame; it RECORDS OOC diffs (NaN-vs-null, int64-vs-float64, metadata). **The build MUST read SEMANTIC_DIFFERENCES.md** -- any validator that stringifies / numerically compares a value could inherit those divergences, which is a reason the allowlist is scoped to value-robust per-cell checks (§2).

## 2. The allowlist + the declines (central correctness argument)

Admit a job for the OOC validator path only when EVERY declared validator satisfies ALL of:
(a) it is in the **per-cell reviewed-safe allowlist**; (b) the job is **fail-closed** (NOT coupled to quarantine row-removal -- see §4); (c) the job **FITS** the full-frame estimate (§3 fit-gate).

**The per-cell reviewed-safe validator allowlist is EXACTLY:** `luhn`, `npi`, `iban`, `vin`, `regex_match`, `column_in_set`. Each evaluates a single cell independently of any other row or table, so batch-by-batch evaluation yields findings identical to the full-frame run, and findings accumulate additively (a count + the finding records, not a full-column copy).

**Every OTHER validator DECLINES the job to full-frame** (unchanged reason `validators_present`):
- `fk_intact`, `no_orphan_children` -- cross-table parent-PK-set materialization (O(distinct parent keys)); not bounded per batch.
- `leak_check` -- positional output-vs-source compare with unbounded `failing_row_indices`.
- `reconciliation_holds`, `parent_window_respected` -- need parent aggregates / windows resident.

**SEMANTIC_DIFFERENCES safety:** the allowlisted checks operate on the value as the OOC route produces it (check-digit on the masked string, regex/in-set membership). Because the OOC masked output already diverges from full-frame on int64-vs-float64 and NaN-vs-null, the plan MUST confirm each allowlisted validator's verdict is identical on the OOC-typed value vs the full-frame-typed value for the covered fixtures, OR decline the specific typed case. Target: **no new SEMANTIC_DIFFERENCES entry** -- the validator path adds no divergence of its own.

## 3. The change

- **New module `out_of_core/_validate_batch.py`:** a thin per-batch evaluator `collect_batch_validations(config, table, masked_batch, accumulator)` that runs ONLY the admitted (allowlisted, per-cell) validators for `table`'s columns present in the masked batch, appending findings to a bounded accumulator (counts + finding records, never a full-column `to_pylist`). At run end, assemble the `ValidationReport` from the accumulator, apply the fail-closed raise (`ValidatorFailedError`) identically to the oracle, and stamp `quality_metrics["validation"]`. Mirror the shape of D3b's `_vault_collect.py` + `_chunked_oracle` per-chunk precedent; cite it (established methodology).
- **Hook BOTH drivers (mirror D3b HIGH-2):** call `collect_batch_validations` at the mask step in `_runner._stream_table` AND `_stream_driver.stream_table`, after `mask_batch` and after that driver's existing fail-closed checks, when admitted validators are present. Thread the validator accumulator + config through `run_fk_out_of_core` and the route dispatch to both drivers. One shared helper, two call sites; final report assembly + raise at run end (after the last batch / after the reorder driver's orphan pass), so the fail-closed semantics match the oracle (raise before any success return; nothing persisted).
- **Routing admission:** add a `validators_ooc_safe` signal = validators present AND every validator is in the per-cell allowlist AND the job is not quarantine-coupled. In `decide_execution_route`, add an OOC-validator admission branch BEFORE the full-frame-fit return so a `validators_ooc_safe` job that is otherwise OOC-compatible (passes compat, not cyclic, not `fk_ooc_dtype_hazard`, all other disqualifiers clear) AND **FITS the full_frame estimate (`full_frame_fits_estimate is True`)** routes to OUT-OF-CORE with its normal reason. Fit-gate is load-bearing (inherit D3b's Codex-final HIGH): findings accumulate in PYTHON memory OUTSIDE the OOC spill budget, so a NON-fitting or UNPRICEABLE (`None`) validator job is NOT admitted and falls through to the reject (`fk_full_frame_oom_risk_rejected`), exactly as a too-big validator job rejects today. D3a thus extends GAP6's bounded route to FITTING validator jobs; too-big validator jobs still reject (a spillable-findings follow-on could lift this). The branch must NOT enable SEQ validators (SEQ-only-eligible jobs stay full-frame; SEQ-native validators are a follow-on) and must NOT admit a cross-row/quarantine job. Keep the `use_byte_estimate_routing=False` rollback. `_sequential_eligible` stays as-is for its SEQ purpose (its `validators_present` return is unchanged for the SEQ/chunked paths).
- No change to `mask_batch`, the join, validator semantics, the `ValidationReport` contract, quarantine, or masked output.

## 4. Quarantine boundary + transactional contract + parity

- **Quarantine on OOC is OUT OF SCOPE by construction.** `finalize_validators_and_quarantine` couples a failed validator to quarantine row-removal across the whole output (`apply_quarantine` rebinds `outputs` to filtered tables). The OOC route returns before that pass and has NO row-error / quarantine channel (`out_of_core/_compat.py` declines row-error-quarantine strategies). A validator-bearing job with `quarantine.enabled` + a `validation_fail` trigger therefore CANNOT be honored on OOC -> it declines to full-frame with the unchanged reason. Only the **fail-closed (raise, no quarantine)** case is admissible on OOC. Surface this as an explicit decline + test; never retro-filter a streamed sink. (A spillable-quarantine OOC channel is a separate V2 item, the same shape as D3b's spillable-vault follow-on.)
- **Fail-closed persists nothing.** OOC masking failures RAISE directly. The validator fail-closed raise must fire at run end BEFORE any success return / sink promotion, so a failed validation persists NO output (matching the oracle, which raises before returning). The build MUST confirm no partial sink is promoted on a validator raise.
- **Validator parity:** for an admitted job, the OOC `ValidationReport` (findings, `passed` flag, `quality_metrics["validation"]`) is IDENTICAL to full-frame for passing and failing columns; fail-closed raises `ValidatorFailedError` identically; nulls skipped identically; findings order deterministic.
- **Masked outputs unchanged** from the existing OOC baseline (D0 strict comparator) -- the validator hook must not perturb masking or order. Validators are read-only by contract.
- **No new SEMANTIC_DIFFERENCES.md entry.**

## 5. Acceptance matrix

- **T1 admission + declines:** a fitting OOC-compatible pure-mask FK job whose validators are ALL per-cell-allowlist checks routes `("out_of_core", ...)`; jobs that (a) declare a cross-row/cross-table validator (`fk_intact`/`no_orphan_children`/`leak_check`/`reconciliation_holds`/`parent_window_respected`), (b) enable quarantine with a `validation_fail` trigger, (c) are SEQ-eligible but not OOC-compatible, (d) are too-big / unpriceable -- each stays full-frame/reject with the unchanged reason. `use_byte_estimate_routing=False` rolls back. Mutants: dropping the allowlist check, dropping the quarantine decline, admitting SEQ, dropping the fit-gate -- each killed.
- **T2 validator parity:** admitted job on OOC vs full-frame produces IDENTICAL `ValidationReport` (findings, `passed`, `quality_metrics["validation"]`) for passing and failing columns -- cover each allowlisted validator (luhn/npi/iban/vin/regex_match/column_in_set), multiple validators, multiple columns/tables, nulls (skipped), all-null (no findings), a parent-key column and a payload column. Fail-closed raises `ValidatorFailedError` identically. Findings order identical.
- **T3 both drivers evaluate:** force the reorder/sink driver (`_stream_driver.stream_table`, threshold forced) AND the in-memory driver (`_runner._stream_table`); both produce the identical report. A mutant hooking only one driver is killed. Witness which driver ran.
- **T4 fail-closed persists nothing:** a validator failure on OOC (and, separately, a late masking failure) raises and persists NO output -- assert via instrumenting the accumulator + the success path (not file-absence alone); a mutant that returns/promotes a sink after a failed validation is killed.
- **T5 masked output unchanged:** admitted job's masked outputs == the existing OOC D0 baseline (strict comparator); the validator hook did not perturb masking/order.
- **T6 bounded memory (narrowed, accurate):** assert per-batch evaluation + bounded additive findings; NO full-column `to_pylist` for the OOC validator path; findings storage is O(findings), and the RAM-fitting routing restriction is what bounds the job. No 10M run (D3b T7 pattern).
- **T7 SEMANTIC_DIFFERENCES:** each allowlisted validator's verdict is identical on the OOC-typed value vs the full-frame-typed value for the covered fixtures (or the specific typed case is declined); no new SEMANTIC_DIFFERENCES entry.

## 6. Failure modes (reproduce, do not smooth)

- A cross-row/cross-table validator silently admitted to OOC -> T1 (would under-count or crash on a partial batch).
- A quarantine-coupled validator admitted without a channel -> T1 (would raise-or-mis-filter).
- The OOC report diverging from the oracle (missing/extra/ordered findings, wrong `passed`, missing `quality_metrics`) -> T2.
- Only one of the two drivers evaluating -> T3 (the other driver's rows unchecked).
- A sink promoted after a failed validation (fail-open) -> T4.
- The validator hook mutating masked output/order (validation must never mutate) -> T5.
- A validator verdict differing by OOC type divergence -> T7.

## 7. Verification + gates

- abi-3 companion venv (`~/.cache/decoy-d1a-abi3-venv`) for OOC parity; ci-mirror for routing / pure-Python. `~/bin/pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`.
- T1-T7 green; full OOC-parity + routing + validator suites green; no masking/parity/validator test weakened. Coverage + mutation on changed lines (allowlist, quarantine-decline, SEQ-not-admitted, fit-gate, single-driver, persist-after-failure mutants killed).
- Module sizes: new logic in `out_of_core/_validate_batch.py`; `_pipeline_routing.py` (651, over the 600 goal / under the 700 census ceiling), `out_of_core/_runner.py` (691, near 700), and `_stream_driver.py` gain only a thin signal + call sites -- keep <=700, census at EXACT LOC if crossed (`tests/sentry/test_module_size.py`), extract rather than grow a >600 file. If a thin call site would push `_runner.py` over 700, move the plumbing into `_validate_batch.py` and keep the driver edit to a single call.
- Established-methodology note in the new module (mirror `_chunked_oracle` validator/per-chunk collection and D3b `_vault_collect`; cite them). Log hygiene: findings/telemetry carry counts + finding records only, no masked/source value text in logs.
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build freely; autonomous merge on the full green gate, flag Slack before merge-to-main, open PR, wait PR CI green, merge, confirm main CI. **The Codex FINAL prompt MUST say: do NOT merge/push/PR; Opus merges after CI.**

## 8. Done when

- [ ] T1-T7 green; declines (cross-row validator, quarantine-coupled, SEQ-only, too-big) stay full-frame; `ValidationReport` identical to the oracle on both drivers; no persist on a failed validation.
- [ ] Changed-lines coverage + mutation-proven; module sizes within policy (census at exact LOC if crossed).
- [ ] diff + evidence to Slack; PR CI green; merge; main CI green.
- [ ] ROADMAP + RECENTLY-SHIPPED updated (D3a closed); engine graphify refresh (note if deferred / none cached).

## 9. Scope decisions taken (precedent-backed; surface, do not silently assume)

- **Per-cell allowlist only.** Cross-row/cross-table validators (`fk_intact`/`no_orphan_children`/`leak_check`/`reconciliation_holds`/`parent_window_respected`) are OUT of D3a and decline to full-frame. A bounded streaming form of any of them (e.g. a lockstep `leak_check` with a cap on `failing_row_indices`) is a deliberate V2 follow-on, not assumed here.
- **Quarantine-on-OOC out of scope** (no channel; §4) -- validator jobs with quarantine stay full-frame.
- **Fit-gate inherited from D3b** (`full_frame_fits_estimate is True`): D3a does not lift the reject ceiling; too-big validator jobs still reject. Stated plainly so the slice is not mis-sold as validators at unbounded scale.

cam
