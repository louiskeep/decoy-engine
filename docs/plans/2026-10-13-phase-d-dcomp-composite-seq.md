Status: plan (Phase D / Dcomp build-slice. rev 2, folds Codex plan-gate round 1 = 2 HIGH + 2 MED. Scope narrowed + made honest: hash-composite-key parity-completeness on SEQ. Pre re-gate.)

# Phase D / Dcomp: composite FK parity-completeness on the sequential route (hash-key subset)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md` (established-methodology, module size, pre-GA hard delete, explain-why comments). Grounding: the Phase D program plan `docs/plans/2026-10-10-phase-d-fk-routes-program.md` (§3 invariants, §4 shape x route x operator x tier, §5 no-legacy, §6 parity contract); a 2026-10-13 FRAME survey + the Codex plan-gate round 1 (both verified against origin/main). Base: engine `origin/main` @ `5694331e` (post D2c-iv). Build does not start until the program plan lands on main (PR #248).

## 0. Scope: what Dcomp is, and the honest boundary (Codex HIGH-1)
**Dcomp is composite-FK parity-completeness for the HASH-key subset on the sequential route, plus the composite orphan-policy, wide-int, and boundary cases D2b did not cover.** It is not a new-kernel slice and it does not claim composite no-legacy completeness.

Composite FK masking has two distinct compute surfaces, and they are NOT both native today:
- **Key components.** `_sequential_native.py:288` skips native decisions for every non-hash strategy when a column is a parent or child FK key. So on a composite KEY: `hash` is native (D2a); `fpe`, `categorical`, `bucket_perturb`, `date_shift` run the PYTHON handler (pinned by `test_phase_d_d2c_fpe_seq.py:307`, "no native decision, matches the oracle"); REMAP re-runs the parent handler at `_strategies/_orphan.py:119`. The canonical `composite_key` golden masks all three components with `hash`, so the common composite case is native; a non-hash key component is correct but Python.
- **Payload columns** on the FK tables use the normal SEQ-native seam (hash/fpe/categorical/bucket_perturb/date_shift), already native where the Arrow type is admitted.

**Operator-x-composite-KEY inventory (disposition this slice assigns):**
| Key operator | Compute today | Dcomp disposition |
|---|---|---|
| hash | native (D2a) | IN SCOPE: prove native + REMAP + evidence on composite keys |
| redact / truncate / passthrough | named-Arrow (payload) / not a typical key strategy | note; not exercised as composite keys here |
| fpe / categorical / bucket_perturb / date_shift | PYTHON (native skipped on keys) | OUT OF SCOPE here. Correct-but-Python = a cross-cutting §5 no-legacy item for ALL FK shapes, not composite-specific. Recorded as an open program cell (see §8), owned by a dedicated FK-key-native slice / D-audit. Dcomp pins the current Python behavior as oracle-parity regression, and explicitly does NOT claim these are native. |

This boundary is stated so the slice cannot be mistaken for closing composite no-legacy. The non-hash-key-native decision is surfaced to Cam (program reshaping; recommend a dedicated key-native slice).

## 1. Risk
R2 (privacy/RI-critical; composite FK key correctness is GATE-TQ0 100% crypto/RI). Acceptance cases are adversarial: SEQ-native vs the pandas oracle on the strict 9-dimension output contract, with warnings / row-errors / quarantine / native-provenance asserted on their OWN channels (the strict comparator covers output tables only, Codex confirmation).

## 2. Coverage ledger (Codex HIGH-2, MED-3): required composite cell -> existing test OR new case
Retained regression (ALREADY covered by D2b; this slice keeps them green, adds nothing):
- Composite matched + partial-null strict parity, REMAP and PRESERVE: `test_phase_d_d2b_seq_real.py:70` (`[composite-remap]`, `[composite-preserve]`; partial-null row `_d2b_shapes.py:269`; companion-hidden byte identity `:83`).
- Composite native-parents + inherited-children witness + exact call counts: `test_phase_d_d2b_seq_shapes.py:126`, `:340`.
- D-1 composite-chain-REMAP decline (clean code, identifiers-only, abort): `test_phase_d_d2b_seq_shapes.py:427`, `:440`.
- D-2 OOC composite-scalar-child decline + canonical group admission/parity: `test_out_of_core_fk_parity.py:1253`, `:1223`.
- Composite-with-nullable-hashed-root rejection (`null_bearing_int_unsupported`, `_guards.py:48`): pinned by `test_phase_d_d2b_seq_shapes.py:377`.

Net-new acceptance cells (genuinely absent; the slice's spec). For output parity each composite shape runs the full-frame oracle, SEQ-native, and SEQ python-fallback (companion hidden) through the `run_full`/`run_seq` adapter helpers and asserts `_strict_compare.compare_strict == _ALL_TRUE` (9 dims); warnings/row-errors/quarantine/provenance are asserted separately. C-route is the cell that exercises PRODUCTION ROUTING (`decide_execution_route` + `out_of_core_admission` + `run_pipeline`), not a direct adapter call.

- **C1 orphan policy FAIL, genuine orphan.** Composite child tuple with no matching parent under FAIL. Assert: raises `orphan_fk_violation` (`_fk_resolve.py:122-130`), identifiers-only message, no data; identical raise on native SEQ, python-fallback SEQ, and the oracle. (FF vs SEQ both named explicitly.)
- **C2 orphan policy FAIL, errored-parent cascade (NOT an orphan raise).** Requires quarantine enabled with a covered trigger (else SEQ raises `RowErrorsFailedError` first, `_sequential.py:462`). Fixture: quarantine config on; a parent row with TWO distinct covered errors across its key columns; declared key-column order; assert the FIRST-declared key column's error is the winning trigger via `setdefault` (`_pandas_adapter.py:652`); errored parent cascades to quarantine under FAIL (`_fk_resolve.py:102-114`) and does NOT raise `orphan_fk_violation`; child attribution + surviving rows asserted; include an uncovered-trigger control that DOES raise. Functional error/quarantine evidence compared on its own channel, not via backend metrics.
- **C3 orphan policy WARN, exact channel.** Composite genuine orphan under WARN. SEQ composite WARN is net-new at the warning-object level: the D2b composite matrix has no orphan_fk WARN warning-channel case (its composite-chain WARN coverage at `test_phase_d_d2b_seq_shapes.py:514` checks output, not the warning object), and the only existing composite WARN-output coverage is the normalized OOC harness (`test_out_of_core_fk_parity.py:1223`), which §6 forbids as a substitute. Assert: the orphan_fk warning is emitted on its exact channel byte-identically on SEQ-native and the oracle, and output `compare_strict == _ALL_TRUE`.
- **C5 wide-int / mixed composite component typing (three separated cases, FF vs SEQ explicit).**
  - (a) Successful Int64/UInt64 preservation: an INTEGER passthrough key component beside a native-hash sibling component; assert exact values + exact `type_width` (int64 not folded to float64), null-vs-NaN pinned, on SEQ-native and oracle.
  - (b) Nullable hashed-root rejection: `null_bearing_int_unsupported` (`_guards.py:48`) (retained-regression reference; asserts the rejection is unchanged, not a new success path).
  - (c) Provably-unrepresentable mixed component (matched float + preserved non-exactly-representable integer orphan): raises the shared `out_of_core_fk_key_dtype_unsupported` fail-closed identically on FF and SEQ (confirmed applies to both despite the name).
- **C-bound boundary inputs (program §3).** Composite table that is (i) empty, and (ii) has an all-null RESOLVED component column (separate write-back branch `_pandas_adapter.py:581`); strict parity + honest no-compute evidence (no kernel call). Thread-count invariance asserted for one composite shape.
- **C-route routing dispositions (program §4).** For the composite shape, name (strategy, size tier) -> (route, backend, visible reason) through `decide_execution_route`: estimate-FIT -> full_frame; non-fit + eligible -> the bounded route; unpriceable estimate -> bounded-by-eligibility; flag-off below/at-threshold; explicit `execution_mode` override. These assert production routing, not direct adapter calls.

## 3. OOC strict composite coverage (Codex HIGH-2)
This build is SEQ-only. The program's strict OOC composite acceptance (§6 forbids substituting the normalized `test_out_of_core_fk_parity.py` harness) is NOT discharged here. It is recorded as an explicit open program cell owned by the OOC strict-contract work (D0's contract extended to composite on OOC), listed in §8. Dcomp does not mark composite OOC complete.

## 4. Declines to keep intact (pin as decline-with-reason, do not "fix")
- **D-1** `sequential_composite_fk_chain_unsupported` (`_sequential_composite_chain.py`): a composite child that is itself a later REMAP parent has no scalar node to re-derive; genuine orphan on that hop declines with a coded, identifiers-only message. Matched-only composite chains STAY admitted.
- **D-2** `out_of_core_composite_fk_scalar_child_unsupported` (`out_of_core/_compat.py:253-285`, CF2 `SEMANTIC_DIFFERENCES.md:57-66`): the double-masking shape stays DECLINED to full-frame (raw-value-leak divergence), not fixed. Canonical `composite_fk_group` stays OOC-admitted + oracle-parity (`_compat.py:234,399-407`).
Both are retained regression (§2); Dcomp re-asserts the dispositions are unchanged.

## 5. Failure modes (reproduce, do not smooth)
- Genuine-orphan FAIL (raise) vs errored-parent cascade under FAIL (quarantine, no raise) are DIFFERENT dispositions (C1 vs C2); first-declared-key-column error wins.
- Partial-null composite = every component resolves to null and the ROW SURVIVES; it is NOT preservation of the original non-null components (Codex correction).
- A key component whose dtype cannot be carried losslessly fails closed with the shared code; never silent widening (C5c).
- The composite group runs no INDEPENDENT masking kernel; but a REMAP group legitimately invokes the parent components' own kernels, so the witness must permit those calls while proving the group node itself did not mask (C6 retained + this clarification).
- Non-hash key operators stay correct-but-Python here and are NOT claimed native (§0 boundary).

## 6. Fixtures
`tests/fixtures/golden/composite_key/` (3-col hash composite PK). Do NOT use `composite_coherence` (`manifest.yaml:11` `relationships: []`, generator fixture). Stand-in shapes extend `tests/parity/_d2b_shapes.py` composite builders with policy + null-placement + integer-component variants; reuse `tests/parity/_seq_witness.py` for witness + companion-hidden fallback. Routing-disposition cases go through the production router (reuse the pipeline-routing harness, not a direct adapter call).

## 7. Verification + gates
- abi-3 companion venv `~/.cache/decoy-d1a-abi3-venv/bin/python`; ci-mirror no-companion venv `~/.cache/decoy-ci-mirror-venv/bin/python`; one test process at a time via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- Net-new cases pass in abi-3 (native, non-vacuous witness); companion-independent assertions pass in ci-mirror.
- Coverage + mutation on any changed production line (expected few/none; test-completeness slice). Any routing/evidence fix an acceptance case forces carries its own kill-proven tests.
- Gate chain per program §3: Opus VERIFY (diff + both venvs + equivalence proofs) -> dennis -> Codex final. Merge under the standing Rust rule only after #248 is on main.
- Module size: no production module expected to cross 600; if a fix touches `_sequential_native.py` (at the 600 ceiling) the split (ColumnDecision/RemapRecord/constants -> `_sequential_native_decisions.py`, add to `test_physical_seam_disconnection.py` permitted_non_physical, hard cap `test_phase_d_d2b_seq_shapes.py`) is a hard prerequisite.

## 8. Open program cells this slice explicitly does NOT close (honest hand-off)
- **Non-hash operators on an FK KEY run Python** (fpe/categorical/bucket_perturb/date_shift) on SEQ: a §5 no-legacy gap that is CROSS-CUTTING across all FK shapes. Owner: a dedicated FK-key-native slice (or folded into D-audit). Surfaced to Cam as program reshaping.
- **Strict OOC composite acceptance** (non-normalized): owned by the OOC strict-contract extension (D0 contract -> composite on OOC).

## 9. Done when
- [ ] C1, C2, C3, C5(a-c), C-bound, C-route pass; strict output parity + the separate warning/row-error/quarantine/provenance assertions hold on SEQ vs oracle; companion-independent parts green in ci-mirror.
- [ ] Retained-regression ledger (§2) stays green; D-1/D-2 dispositions re-asserted unchanged.
- [ ] The §0 boundary and §8 open cells are recorded; Dcomp does not claim composite no-legacy or composite OOC completeness.
- [ ] Any production change coverage+mutation-proven; `_sequential_native.py` within size budget (split first if touched).
- [ ] ROADMAP + RECENTLY-SHIPPED updated; graphify refreshed; Slack recap in plain language.

cam
