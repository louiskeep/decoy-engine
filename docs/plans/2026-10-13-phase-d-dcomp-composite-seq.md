Status: plan (Phase D / Dcomp build-slice. DRAFT rev 1, pre Codex plan-gate. Scope = composite / multi-column FK masking parity-completeness on the SEQ route.)

# Phase D / Dcomp: composite FK parity-completeness on the sequential route

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md` (established-methodology, module size, pre-GA hard delete, explain-why comments). Grounding: the Phase D program plan `docs/plans/2026-10-10-phase-d-fk-routes-program.md` (§4 row "Composite / multi-col", §3 invariants, §5 no-legacy, §6 parity contract); a 2026-10-13 FRAME survey of the composite path (this session, verified against origin/main). Base: engine `origin/main` @ `5694331e` (post D2c-iv). Build does not start until the program plan lands on main (PR #248).

## 0. Scope correction (what Dcomp is NOT)
The program table marks composite "HIGHEST RISK (cross-chunk/cross-batch composite tuple keying)." The FRAME shows that framing applies to the OUT-OF-CORE route, not SEQ, and the compute is already native:
- Composite per-component masking already runs on Rust: hash (D2a), fpe/categorical/bucket_perturb/date_shift (D2c) via the `_sequential_native.py` seam. The composite FK **resolution** is by design a Python N-tuple remap through `parent_map` (`_pandas_adapter.py:480-596`); the `composite_fk_group` node (strategy `"<group>"`) matches no `NativeOp` descriptor and must never run a kernel (`_sequential_native.py:11-14`, "Only hash may be an FK key on this route").
- SEQ ingests each table whole, one bounded pass (`_sequential.py:383`, `frames[table] = df`); the parent map is built once from the full parent frame (`:478`). A composite tuple is never split across a chunk boundary on SEQ. The cross-chunk tuple-keying risk is an OOC property, handled by the length-prefixed `fk_join_key_tuple` string encoder (`_fk_keys.py:210`) and already OOC-parity-tested (`test_out_of_core_fk_parity.py:1223`).
- D2b already ships composite-remap / composite-preserve shapes, the composite witness (`test_witness_names_the_native_parents_and_the_inherited_children`), the `composite_key` golden under `compare_strict == _ALL_TRUE` + a `rust_companion` witness (PRESERVE policy), composite-child-inherits/remap, composite-chain, and composite-with-nullable-member-rejected (`test_phase_d_d2b_seq_shapes.py:47-48,119-126,337-399`; `test_phase_d_d2b_seq_real.py:196,232`).

**So Dcomp is parity-COMPLETENESS, not a kernel slice.** No new crate entry point. The engine behavior is already correct; Dcomp proves it across the composite acceptance cells D2b did not cover, pins the two composite declines as caller-visible decline-with-reason, and feeds D-audit. This is test-and-contract work plus any small routing/evidence fix a failing acceptance case surfaces.

## 1. Risk
R2 (privacy/RI-critical; composite FK key correctness is GATE-TQ0 100% crypto/RI). Although the slice is mostly tests, a composite orphan/partial-null parity gap would be a real divergence, so the acceptance cases are adversarial and compare SEQ-native to the pandas oracle on the strict 9-dimension contract, not a normalized one.

## 2. Net-new acceptance matrix (the spec)
Each case runs the composite shape three ways where applicable (full-frame oracle, SEQ-native, SEQ python-fallback with the companion hidden) and asserts `tests/parity/_strict_compare.py::compare_strict == _ALL_TRUE` (9 dimensions: schema_structure, field_metadata, type_width, column_order, row_count, row_set, row_order, cellwise_values, null_vs_nan) plus the native-execution witness. Only cells D2b does not already cover are added; no padding of existing coverage.

**C1 — orphan policy FAIL, genuine orphan.** Composite child tuple with a genuine orphan (no matching parent) under `OrphanPolicy.FAIL`. Acceptance: raises `orphan_fk_violation` (`_fk_resolve.py:122-130`); message carries schema identifiers only, no data; identical raise on native and python-fallback SEQ and on the full-frame oracle.

**C2 — orphan policy FAIL, errored-parent cascade (NOT an orphan raise).** Composite key where a parent key column row-errors. Acceptance: the errored parent cascades to quarantine under FAIL (`_fk_resolve.py:102-114`), a data-quality disposition, and does NOT raise `orphan_fk_violation`; the first-key-column-error-wins trigger is asserted for the composite key (`_pandas_adapter.py:648`). SEQ-native == oracle on the surviving rows + the RowError channel.

**C3 — orphan policy WARN.** Composite genuine orphan under `OrphanPolicy.WARN`. Acceptance: the orphan survives with the configured disposition and the warning is emitted byte-identically on both routes; strict parity on output.

**C4 — partial-null composite tuple under the strict comparator.** A composite child tuple with some-but-not-all components null. Acceptance: partial-null is treated as fully-null on both routes (`SEMANTIC_DIFFERENCES.md:62`), the row is preserved (`_pandas_adapter.py:551-556`), and `compare_strict == _ALL_TRUE` including `null_vs_nan` and `type_width` (the gap today: OOC proves this value-normalized, SEQ does not prove it strict).

**C5 — wide-int / null-bearing composite component, strict type-width.** A composite key with a component that is a nullable wide integer (Int64/UInt64) alongside a string component. Acceptance: per-component lossless int typing (`_pandas_adapter.py:566-592`, `_fk_keys.py:284-456`) pinned exact under `type_width` + `null_vs_nan` (int64 vs float64 not folded); and the provably-unrepresentable mix raises `out_of_core_fk_key_dtype_unsupported` fail-closed identically on both routes.

**C6 — composite-GROUP native witness.** Extend the witness to attribute, for a composite group, native per-parent-column compute AND inherited children, while asserting the group node itself ran no kernel (no descriptor match). Guards against a future change that routes the tuple remap through a kernel.

## 3. Declines to pin (caller-visible, decline-with-reason)
**D-1 composite-chain REMAP** — `sequential_composite_fk_chain_unsupported` (`_sequential_composite_chain.py`): a composite child that is itself the parent of a later REMAP edge has no scalar node to re-derive from, so a genuine orphan on that hop declines with a coded, schema-identifier-only message. Pin the decline + witness it; keep it intact (do not "fix").

**D-2 OOC composite-scalar-child** — `out_of_core_composite_fk_scalar_child_unsupported` (`out_of_core/_compat.py:253-285`, CF2 in `SEMANTIC_DIFFERENCES.md:57-66`): the double-masking shape (a child FK column carrying its own scalar strategy while also being a composite-FK child) stays DECLINED to full-frame (raw-value-leak divergence), NOT fixed. The canonical `composite_fk_group` stays OOC-admitted and oracle-parity (gated by `_composite_group_join_covered`, `_compat.py:234,399-407`). Assert both dispositions are unchanged.

## 4. Failure modes (must be reproduced, not smoothed)
- Genuine orphan vs errored-parent cascade are DIFFERENT dispositions under FAIL (raise vs quarantine); conflating them is the primary correctness risk (C1 vs C2).
- Partial-null composite must equal fully-null, not a partial lookup (C4).
- A composite key component whose dtype cannot be losslessly carried fails closed with the shared code, never silently widens (C5).
- The composite group must never acquire a kernel path (C6); the chain-REMAP and scalar-child shapes must never silently fall back (D-1, D-2).

## 5. Fixtures
Use `tests/fixtures/golden/composite_key/` (3-col composite PK `(member_id, plan_id, effective_date)`; `claims` FKs the tuple). Do NOT use `composite_coherence` (`manifest.yaml:11` `relationships: []`, a generator fixture). Stand-in shapes extend the existing `tests/parity/_d2b_shapes.py` composite builders (`composite_shape`, `mixed_composite_shape`) with policy + null + wide-int variants. Reuse `tests/parity/_seq_witness.py` (MARK/MarkingKernel/install) for the witness and companion-hidden fallback.

## 6. Verification + gates
- abi-3 companion venv `~/.cache/decoy-d1a-abi3-venv/bin/python`; ci-mirror no-companion venv `~/.cache/decoy-ci-mirror-venv/bin/python`; one test process at a time via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- The new acceptance cases pass in abi-3 (native, non-vacuous witness) and the companion-independent assertions pass in ci-mirror (skip where they need the companion).
- Coverage + mutation on any changed production line (expected: few or none; this is test-completeness). If an acceptance case forces a small routing/evidence fix, it carries its own kill-proven tests.
- Gate chain per program §3: Opus VERIFY (diff + both venvs + equivalence proofs) -> dennis (fresh-context adversarial) -> Codex final. Merge under the standing Rust rule only after the program plan (#248) is on main.
- Module size: no production module expected to cross 600; if a fix touches `_sequential_native.py` (already at the 600 ceiling) the split (ColumnDecision/RemapRecord/constants -> `_sequential_native_decisions.py`) is a hard prerequisite, same seam noted for D2c-v.

## 7. Done when
- [ ] C1-C6 pass with `compare_strict == _ALL_TRUE` + native witness on SEQ, against the oracle; companion-independent parts green in ci-mirror.
- [ ] D-1 and D-2 declines pinned as caller-visible decline-with-reason; both dispositions asserted unchanged.
- [ ] No composite cell routes through a pandas masking path for an admitted operator (D-audit input recorded); the composite group runs no kernel.
- [ ] Any production change is coverage+mutation-proven; `_sequential_native.py` stays within size budget (split first if touched).
- [ ] ROADMAP + RECENTLY-SHIPPED updated; graphify refreshed; Slack recap in plain language.

cam
