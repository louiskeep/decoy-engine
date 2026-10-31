Status: built
Shipped: merge PR #250 (06348c84)

# Phase D prereq: split the SEQ-native decision types into `_sequential_native_decisions.py`

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md` (refactor safety: separate a refactor from any behavior change), `development-loop.md`, `architecture.md`; engine `CLAUDE.md` (module size 600 GOAL / 700 MAX, explain-why comments, `internal/` boundary). Grounding: a 2026-10-13 FK-key-native FRAME survey (this session) that confirmed `_sequential_native.py` is exactly at the 600 GOAL and must be decomposed before the FK-key-native slice can add fpe-on-key wiring. Base: engine `origin/main` @ `e9e0435a`.

## 0. Why (and why now, as its own slice)
`src/decoy_engine/execution/_sequential_native.py` is exactly 600 LOC, the no-census GOAL (`tests/sentry/test_module_size.py:56-57`; it is not allowlisted). The next slice (fpe-on-key native + explicit declines for the lossy key operators) adds enough to blow past the 700 MAX. Per refactor-safety, that behavior change must not ride in on top of a module move. This slice is the move ALONE: zero behavior change, every existing SEQ test byte-identical green, so the FK-key slice starts from a module with headroom and a clean decision-types seam.

## 1. Risk
R1-R2 (behavior-preserving refactor on a privacy/RI-critical module, but no logic change). The guarantee covers EXECUTION RESULTS, quality/disposition evidence, existing import paths, and the supported test seams: every existing test passes UNCHANGED (no test edited to accommodate the move, except the physical-seam sentry list), byte-identical, in both venvs. Moving a dataclass does change its `__module__` and method-global namespace; no serialization/pickle or `__module__`-reading consumer exists (Codex AST inventory found none), so that change is inert here and is not claimed away. If one ever appears, retain the old `__module__` explicitly.

## 2. The move
Create `src/decoy_engine/execution/_sequential_native_decisions.py` holding the two pure decision/evidence dataclasses and ONLY the constants they own:
- `ColumnDecision` (currently `_sequential_native.py:128-169`) and `RemapRecord` (`:172-197`).
- The constants these two dataclasses reference: `PATH_SCALAR`, `PATH_REMAP`, `MIXED`, `_NO_KERNEL_RAN`, `_REASON_NO_ROWS`, `_NO_CALLS_REASON`.
- Its imports: `HASH`, `BUCKET_PERTURB`, `DATE_SHIFT` from `_sequential_native_ops`; `RUST_COMPANION`, `PYTHON_SCALAR` from `_operator_registry`; `pyarrow`, `dataclasses`, `typing.Any`.

Stays in `_sequential_native.py` (NOT moved):
- `SeqNativeRun` (the orchestration class), the loaders `_load_hash`/`_load_fpe`/`_load_index` (the docstring at `:207-208` is explicit that they live here so a test patching the companion-load name resolves through this module's globals -- moving them would break that seam), `_OPS`, `_table_columns`, and the constants only `SeqNativeRun` uses: `PATH_INHERITED`, `INHERITED`, `REASON_REMAP_PARENT_PYTHON`, `REASON_REMAP_INPUT_NOT_ARROW`, `REASON_BUCKET_OUT_OF_RANGE`, `_CONVERSION_ERRORS`.
- `_sequential_native.py` imports the moved names back and RE-EXPORTS them. `SeqNativeRun` does NOT reference the six moved constants directly (they are owned by the two dataclasses), but `PATH_SCALAR`, `PATH_REMAP`, and `MIXED` are in the module's existing `__all__`, so they must stay importable from `_sequential_native`. Import the three back with explicit re-export (alias or `__all__` membership) to avoid an unused-import lint failure.

## 3. Back-compat (hard requirement)
`ColumnDecision` is imported directly from `_sequential_native` by tests (`test_phase_d_d2c_fpe_seq.py:340,357`) and `RemapRecord` is constructed as `_sequential_native.RemapRecord` (`test_phase_d_d2a_seq_native.py:392`); `test_phase_d_d2c_gap_closure.py:16` imports `INHERITED`, `MIXED`, `BoundHashKernel`, `_table_columns` from it; other code imports `SeqNativeRun`, `SEQ_HASH_DISPOSITION_KEY`, `SEQ_FPE_DISPOSITION_KEY`. **Preserve the entire existing `__all__` (`_sequential_native.py:589-600`) unchanged**, and keep every one of those names importable from `_sequential_native`: `ColumnDecision`, `RemapRecord`, `PATH_SCALAR`, `PATH_REMAP`, `MIXED` are re-exported from the new module; `INHERITED`, `BoundHashKernel`, `_table_columns`, `SeqNativeRun`, the disposition keys stay defined/imported here. No test import path changes.

## 4. Sentry updates (the only non-refactor edits)
- `tests/sentry/test_physical_seam_disconnection.py:639-640`: add `src/decoy_engine/execution/_sequential_native_decisions.py` to the `permitted_non_physical` list beside the existing `_sequential_native.py` / `_sequential_native_ops.py` entries, with a one-line why comment (it is the decision/evidence value types for the SEQ-native seam, no physical I/O).
- `tests/sentry/test_module_size.py`: the new module is ~100 LOC (well under GOAL), needs no allowlist entry; `_sequential_native.py` drops to ~510 (under GOAL), so its (absent) allowlist status is still correct. No edit expected; confirm both hold.
- `tests/parity/test_phase_d_d2b_seq_shapes.py:592` (`assert loc(_sequential_native) <= 600`): still true after the drop. Leave the assertion; update its inline comment only if it names a specific count.

## 5. Acceptance (the spec)
- `_sequential_native_decisions.py` created; `ColumnDecision`/`RemapRecord` + their owned constants moved; `_sequential_native.py` imports + re-exports them; SeqNativeRun/loaders/_OPS unchanged in place.
- `ruff check` + `ruff format` + `mypy` clean on both modules.
- EVERY existing SEQ parity/gap test passes UNCHANGED and byte-identical in abi-3 AND ci-mirror: the `test_phase_d_d2a/d2b/d2c_*`, `test_phase_d_dcomp_composite_*` suites. No test edited except the physical-seam sentry list.
- `tests/sentry` passes in abi-3 (module-size + physical-seam green with the new module listed).
- `_sequential_native.py` LOC < 600; `_sequential_native_decisions.py` LOC < 600.
- Diff is: one new module, the import/re-export edit + deletions in `_sequential_native.py`, one sentry-list line. No logic change anywhere.

## 6. Verification + gates
- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python`; ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python`; one test process at a time via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- Behavior-preservation proof: the full SEQ suite + sentries green unchanged in both venvs. Because no logic changed, coverage/mutation on changed lines is N/A (the moved lines keep their existing test coverage); the proof is the unchanged-tests-still-green invariant.
- Gate chain: Opus VERIFY (diff is move-only, both venvs green unchanged) -> dennis (confirm byte-identical move, seam preserved, no logic drift) -> Codex final. Merge under the standing Rust rule.

## 7. Done when
- [ ] New module created; dataclasses + owned constants moved; back-compat re-exports in place; SeqNativeRun/loaders/_OPS untouched.
- [ ] Full SEQ suite + `tests/sentry` green UNCHANGED in both venvs; no test edited but the physical-seam list.
- [ ] Both modules under 600 LOC; `ruff`+`mypy` clean.
- [ ] ROADMAP + RECENTLY-SHIPPED note the prereq split; graphify refreshed; Slack recap (brief, it is plumbing).

cam
