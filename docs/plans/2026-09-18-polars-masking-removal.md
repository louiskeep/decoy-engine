# PLAN — Remove the dormant Polars MASKING adapter (keep Polars for subsetting)

Status: plan (rev3) — UNPARKED 2026-09-20. The one product decision that parked it is RESOLVED:
**Cam decided RETIRE the V2 baseline benchmark harness** (2026-09-20). Rationale (Cam's framing): the
V2 harness's "correctness" is cross-substrate parity (pandas==polars); once the polars masking adapter
is deleted there is no second substrate to compare against, so the check is moot, and the shipped
(pandas/native) path's correctness is already covered by the golden-gate test-flight + property/
mutation + acceptance suites. So D7b = RETIRE (delete the harness + its CI + fixtures + report),
preserving the historical baseline JSON + report as a dated `record` for evidence. This resolves the
V2-harness entanglement behind the two prior Codex NO-GOs. Ready to re-gate + build. Roadmap: Stage C
item 13 (later than Stage A/B in the master order — sequence with Cam). Author: Opus. Cam-decided
2026-09-18: keep Polars for subsetting, delete the dormant Polars masking adapter. Pre-GA hard-delete
is in force (`release.py` pre-ga; engine CLAUDE.md "Pre-GA = hard delete"). `substrate` /
`DECOY_SUBSTRATE` / `VALID_SUBSTRATES` is NOT a frozen surface (not in `docs/compatibility-
contract.md`), so no §9 checklist is required.

## FRAME
The Polars masking substrate shipped (S13), was measured no faster than pandas for keyed-crypto
masking, and was reverted to dormant/default-OFF (memory: [[decoy-polars-masking-off]]). It is opt-in
only (`substrate="polars"` / `DECOY_SUBSTRATE=polars`), value-parity with pandas, and carries a whole
adapter module + a CI substrate matrix. `src/decoy_engine/subset/` uses the polars LIBRARY directly
for FK-closure joins and imports nothing from `execution/` — it is unaffected and STAYS, as does the
`polars>=1.0,<2.0` runtime dependency. This removal deletes only the masking adapter and its routing/
reasons/test/CI scaffolding.

## Design decision: keep the generic non-pandas guards as fail-closed defence
Several files guard `resolved_substrate != "pandas"` (native route, unified-slice admission, pipeline
routing, shadow full-frame). After removal, `"pandas"` is the ONLY valid substrate, so these guards
become unreachable. KEEP them as cheap fail-closed defence (a future substrate would need them, and
they document "this lane is pandas-only"), with a one-line comment noting they're now defensive. This
deliberately keeps the diff OFF the sensitive routing/admission files (esp. `_unified_slice_admission`
which activation touches). Only polars-adapter-SPECIFIC code is deleted.

## DEVELOP

### D1 — Delete the masking adapter module (masking-only, verified no `subset/` edge)
`rm -r src/decoy_engine/execution/polars/` (all 13 modules: `_polars_adapter.py`,
`_conversion_boundary.py`, `_source_reader.py`, `_target_writer.py`, `_strategies/*`, `__init__.py`).

### D2 — Fix the 3 UNGUARDED hard imports (these break at import/selection if D1 lands alone)
- `execution/__init__.py`: delete the `PolarsExecutionAdapter` import (:127) + its `__all__` entry
  (:153). Keep the `VALID_SUBSTRATES` re-export.
- `execution/_substrate.py`: `VALID_SUBSTRATES = ("pandas",)` (:29); delete the `if substrate ==
  "polars": ... PolarsExecutionAdapter` selection branch (:105-112), keep the pandas construction.
  Trim the module + `select_execution_adapter` + `resolve_substrate` docstrings of the dormant-polars
  narrative. Decision on `max_workers`/`fallback_to_pandas` params (polars-only, :85-86): keep them
  as accepted-but-ignored no-ops with a "reserved" comment (avoids churning every caller's signature)
  — do NOT remove from the signature. `fpe_chunk_count` stays (pandas uses it).
- `execution/_planner.py`: delete the `polars_native` mode from `EXECUTION_MODES` (:85), the
  `_polars_native_rejection` function (:281-322, which hard-imports `POLARS_SCALAR_HANDLERS`), and its
  call + the `polars_native` branch in `classify_job` (:225-238). Keep all `substrate` param threading
  and the `_chunked_rejection` structural guard (now always-pandas). Trim the docstring mode list.

### D3 — Delete the other polars-adapter-specific code
- `execution/_when_gate.py`: delete `run_with_when_gate_polars` (:228-312, EOF) + the `import polars`
  lines (:48 TYPE_CHECKING, :258 local). Keep `run_with_when_gate` (pandas) + shared helpers. Trim
  docstring to pandas-only.
- `execution/_chunked_adapter_gate.py`: the polars branch (:46-61) collapses —
  `chunked_adapter_touches_pandas_ingestion` becomes unconditional `return True`. Simplify the
  function + docstring (leave the module in place; do not re-fold into `_chunked_fk.py` this slice —
  keep the diff bounded).
- `execution/physical/_reasons.py`: delete `translate_polars_rejection` (:326-342) + its regex/const
  block (:284-323) + the `__all__` entry (:489). Keep `translate_chunked_rejection` /
  `translate_relationship_mode_reason`. Trim docstring mentions.
- Top-level `src/decoy_engine/__init__.py`: trim the `PolarsExecutionAdapter` docstring mention (:16)
  — it is NOT imported/exported here, docstring-only.

### D4 — Docstring/comment-only wording (no logic change)
`_native_route.py` (:20-25 docstring), `_unified_slice_admission.py` (comment naming the polars
opt-in), `_pipeline_routing.py` (:200 docstring naming `polars_substrate_strategy_unmigrated`),
`physical/drivers/_full_frame.py` (:3-4), `_chunked.py`, `_adapter.py`, `_guards.py`, `_pipeline.py`
docstrings: strip polars-opt-in wording; add the "non-pandas guard is now fail-closed defence" note
where a guard remains. LOGIC in these files is unchanged (D-decision above).

### D5 — Module-size sentry (MANDATORY, or the sentry fails)
`tests/sentry/test_module_size.py` allowlists `_planner.py: 652`. D2 drops it ~56 LOC under the 600
LIMIT, which trips `test_allowlist_entries_are_still_oversized`. Remove the `_planner.py` allowlist
entry in the SAME change. Re-check any other allowlisted file this touches.

### D6 — Tests
- DELETE (polars-masking-only): `tests/unit/execution/test_polars_adapter.py`,
  `test_when_gate_polars_writeback.py`, `tests/parity/test_strategy_substrate_parity.py`,
  `test_composite_substrate_parity.py`.
- EDIT, preserving pandas coverage (RISK — do NOT delete these wholesale):
  - `tests/parity/test_chunked_substrate_parity.py`: it also asserts pandas-chunked vs
    pandas-full-frame BYTE parity (:5-7). Before deleting the polars cases, confirm that pandas
    assertion is duplicated in the pandas chunked suite; if not, MOVE it there. Then delete the file
    (or keep it pandas-only).
  - `tests/unit/execution/test_code_set_cross_substrate_evidence.py`: hole #1 (:10-25) is a PANDAS/
    shared-path evidence bug. Keep the `PandasExecutionAdapter` cases; drop only the polars cases.
- EDIT (drop polars parametrization / convert decline cases):
  - `test_run_pipeline_substrate.py`: keep "default==pandas byte-identical"; convert the
    `substrate="polars"` selection cases to assert `"polars"` now raises the invalid-substrate error
    (proves removal); the invalid-substrate message changes with `VALID_SUBSTRATES`.
  - `test_execution_planner.py`, `test_planner_mutation_kills.py`: drop `polars_native` /
    `_polars_native_rejection` cases.
  - `tests/physical/test_compiler_reasons.py`: delete the 6 `test_translate_polars_rejection_*`
    (:45-78).
  - Decline-path tests that set `substrate="polars"` to hit the non-pandas guard
    (`tests/parity/native/*`, `test_native_route_units.py`, `test_unified_slice_*.py`,
    `test_shadow_full_frame.py`, `test_compiler_units.py`, `test_job_performance_gates.py`): since the
    guards STAY (defensive) but `"polars"` is now invalid, convert these to assert
    `resolve_substrate("polars")` raises invalid-substrate, OR drop the polars parametrization where a
    pandas case already covers the shape. Do NOT leave a test that passes `substrate="polars"`
    expecting success/decline.
  - Chunked/nested/e2e polars parametrizations: drop; verify (via a quick coverage check on the
    changed files) none is the SOLE exerciser of a shared pandas branch before removing.

### D7 — CI + docs
- `.github/workflows/engine-v2-substrate-matrix.yml`: remove the `polars` matrix leg (keep pandas), or
  delete the workflow if pandas-only is redundant with the main suite — prefer removing the leg.
- `.github/workflows/engine-v2-parity.yml`: remove the pandas-vs-polars parity step (:39).
- `README.md` (:79), `CODEMAP.md` (:190), `pyproject.toml` description (:11) + mypy overrides
  (:848-849), `tests/parity/SEMANTIC_DIFFERENCES.md` (:27,:65-67), and the plan docs naming
  cross-substrate polars parity: update wording to pandas-only / mark historical. KEEP the `polars`
  dependency (:51,:88) and the numexpr/polars methodology comment — `subset/` needs it.

## VERIFY (acceptance)
1. `.venv/bin/python -c "import decoy_engine.execution"` succeeds (the 3 unguarded imports fixed).
2. Full engine suite GREEN: `.venv/bin/python -m pytest` — esp. `tests/unit/execution/`,
   `tests/parity/`, `tests/physical/`, `tests/sentry/test_module_size.py` (the allowlist edit).
3. `substrate="polars"` / `DECOY_SUBSTRATE=polars` now raises the invalid-substrate `ExecutionError`
   (a test asserts it); `DECOY_SUBSTRATE=pandas` and default are byte-identical to before.
4. The preserved pandas coverage (chunked-vs-full-frame byte parity; code_set nested evidence hole #1)
   still runs and passes.
5. `subset/` untouched + its tests green (polars subsetting unaffected).
6. ruff + mypy clean on the diff; module-size sentry green; test_flight fingerprints UNCHANGED
   (masking output is pandas — byte-identical).
7. Diff-coverage on the edited shared files (`_substrate`, `_planner`, `_when_gate`, `_reasons`,
   `_chunked_adapter_gate`).

## Out of scope
- The generic `substrate != "pandas"` structural guards (kept as fail-closed defence).
- Re-folding `_chunked_adapter_gate` back into `_chunked_fk.py` (keep the diff bounded).
- `subset/` (unrelated; keep).

## Rev2 remediations (Codex plan-gate round 1)

- **D2b — additional UNGUARDED hard imports [BLOCKER].** Beyond the 3 named, these also import the
  adapter at module/script load and MUST be handled or pytest collection / the script breaks:
  - `tests/physical/test_characterization_full_frame.py:15` (module-load import) → delete or rewrite
    pandas-only (check for buried pandas coverage first, like the other RISK files).
  - `scripts/run_engine_v2_baseline.py:53` (script-load) → see D7b.
  - Local imports in `tests/integration/test_row_errors_e2e.py`, `test_when_gate_row_error_leak.py`,
    `tests/unit/execution/test_de10_chunked_fk_passthrough.py`, and the chunked-strategy test files →
    drop the polars parametrization / rewrite pandas-only, preserving any pandas assertion.
  Do a fresh repo-wide sweep for `from decoy_engine.execution.polars` and `PolarsExecutionAdapter`
  and `polars_native` before building, and treat every hit as delete/edit-or-break.
- **D7b — benchmark tool [HIGH]. DECIDED: RETIRE (Cam 2026-09-20).** `scripts/run_engine_v2_baseline.py`
  + `scripts/compare_baselines.py` are a two-substrate parity/performance harness whose correctness
  dimension IS pandas==polars parity — moot once polars masking is gone. RETIRE: delete both scripts,
  the `.github/workflows/benchmark.yml` V2/polars job, the two perf tests, the committed baseline JSON
  fixture, and `docs/v2/perf/engine-v2-baseline-report.md`. PRESERVE the historical baseline JSON +
  report by moving them to a dated `record`-status location (evidence), not deleting the evidence.
  Sweep for any remaining consumer of the harness output before deleting. (Shipped-path correctness is
  covered by test-flight + property/mutation + acceptance — no coverage lost.)
- **D7c — generated capability matrix [HIGH].** `scripts/gen_capability_matrix.py:40` silently
  catches the missing polars registry and would emit "no" for every acceleration row; the checked-in
  `docs/capability-matrix.md` goes stale. Remove the polars-registry/acceleration-column logic if it
  is adapter-specific, REGENERATE `docs/capability-matrix.md`, and run its sentry (the matrix has a
  drift sentry — it must match the generator).
- **D6b — guard defensive coverage [MEDIUM].** Do NOT convert ALL non-pandas-guard tests to
  invalid-substrate tests (that loses the guards' future-substrate defence, since the lower-level
  functions accept a resolved substrate string and can be called independently of resolve_substrate).
  Instead: (a) retain ONE direct unit test per guard (`_native_route`, `_unified_slice_admission`,
  `_pipeline_routing`) using a synthetic value like `"future_substrate"`, asserting the existing
  decline reason; (b) SEPARATELY assert public `run_pipeline(..., substrate="polars")` and
  `DECOY_SUBSTRATE=polars` raise `invalid_substrate`. Preserves both public validation and defence.
- **D7d — active platform + engine docs [MEDIUM].** Coordinated-docs task: update the active platform
  architecture docs that still present `PolarsExecutionAdapter` as an available/default surface —
  `decoy-platform/docs/architecture/{engine.md, shared-engine-architecture.md,
  engine-product-flow.md}` — and the engine `CHANGELOG.md` (:1703 area) + planner mutation ledger.
  Label archived/historical records as historical rather than editing them. (Platform docs commit via
  the clean off-main worktree, separate PR from the engine change.)
- **Confirmed (no action beyond the plan):** `_planner.py` is the only affected allowlisted module
  (D5 stands); the `test_chunked_substrate_parity.py` pandas byte-parity is duplicated in
  `test_chunked.py`/auto-chunk tests (relocate its default-vs-full-frame case to be safe); rewrite
  `_run_both` in `test_code_set_cross_substrate_evidence.py` to pandas-only keeping hole-#1 evidence;
  pandas masking output is unchanged (golden/test-flight is the proof); keep `polars>=1.0,<2.0`.

## Rev3 remediations (Codex plan-gate round 3, 2026-09-20)

The RETIRE decision cleared the product blocker; round 3 surfaced that the adapter is woven wider
than rev2 enumerated. FULL consumer map (from a repo-wide sweep of `from decoy_engine.execution.polars`
/ `PolarsExecutionAdapter` / `polars_native` / `POLARS_SCALAR_HANDLERS` / `translate_polars_rejection`):

- **BLOCKER — 22 test files import the adapter (test collection breaks if unhandled).** Full list:
  `tests/unit/execution/`: `test_polars_adapter.py`, `test_bucket_perturb_chunked.py`,
  `test_text_mask_chunked.py`, `test_group_key_chunked.py`, `test_code_set_chunked.py`,
  `test_dgrn_windowed_date.py`, `test_nested_strategy.py`, `test_auto_chunk_routing.py`,
  `test_chunked_mutation_kills.py`, `test_de10_chunked_fk_passthrough.py`,
  `test_code_set_cross_substrate_evidence.py`, `test_run_pipeline_substrate.py`,
  `test_execution_planner.py`, `test_planner_mutation_kills.py`; `tests/unit/test_de02_keyprovider.py`;
  `tests/integration/`: `test_row_errors_e2e.py`, `test_when_gate_row_error_leak.py`;
  `tests/parity/`: `test_polars`-only `test_strategy_substrate_parity.py`,
  `test_composite_substrate_parity.py`, `test_chunked_substrate_parity.py`;
  `tests/physical/`: `test_characterization_full_frame.py`, `test_compiler_reasons.py`.
  Per file: DELETE the adapter-only ones (`test_polars_adapter.py`, `test_strategy_substrate_parity.py`,
  `test_composite_substrate_parity.py`); for the rest, drop the polars parametrization/import and
  PRESERVE any pandas assertion (relocate a sole-exerciser pandas case first — the chunked-substrate
  byte-parity is duplicated in `test_chunked.py`, verify before deleting). ACCEPTANCE: a zero-live-hit
  sweep for those symbols across `src/`+`tests/`+`scripts/` (historical `docs/` records excluded).

- **HIGH — D6b guard coverage must include ALL retained fail-closed guards.** Beyond native-route /
  unified-slice-admission / pipeline-routing, add synthetic-`"future_substrate"` unit coverage for
  `_planner._chunked_rejection`'s non-pandas guard AND both `_shadow_full_frame._require_admitted_substrate`
  checks (:259 compiled `table.substrate != "pandas"`, and the injected `adapter_name` check), each
  asserting the existing decline; SEPARATELY assert public `substrate="polars"` + `DECOY_SUBSTRATE=polars`
  raise `invalid_substrate`.

- **Source deleted-path references (data/docstrings, not imports):** update
  `execution/native/_determinism_protocol.py` (`mirror_call_sites` tuples naming `execution/polars/
  _strategies/*` at :135,:153,:179,:197 — drop or re-point the polars mirror sites),
  `plan/_checks_categorical.py:9` + `plan/_checks_truncate.py:10` (docstrings naming the deleted
  polars strategy files — trim to pandas).

- **Active CI doc:** `docs/ci-regression-gate.md:17` still lists `tests/parity/` pandas-vs-polars
  parity + the substrate/parity workflows as required — update to pandas-only.

- **D7b RETIRE consumer sweep:** account for `docs/ci-regression-gate.md` and the historical report
  reference in `scripts/fk_memory_probe.py:1` (re-point it to the final dated-record location).

- **Dated-record destination (define + link before moving):** move the historical V2 baseline JSON +
  `docs/v2/perf/engine-v2-baseline-report.md` to `docs/product/release-1-validation-runs/2026-05-28-engine-v2-baseline/`
  (Status: record), and update every link (fk_memory_probe.py, ci-regression-gate.md, any plan doc).

- **Scope reality (for Cam):** this is a ~30+-file sprawling cleanup (22 test files + src data refs +
  guards + V2 harness retire + capability-matrix regen + platform-doc PR), and Codex found more
  consumers on each of 3 rounds. Bounded + mechanical, but LARGE. Recommend executing it as a fresh
  focused pass (own branch `feat/polars-masking-removal`, already started) rather than tail-of-marathon.

## Rev3 remediations (Codex plan-gate round 4, 2026-09-20) + APPROACH REFRAME

Round 4 found STILL more consumers (4th round running, each finds new references) — the guard
coverage (D6b) is now confirmed complete, but the reference map is not closeable by upfront
enumeration. Newly found:
- **BLOCKER:** `tests/unit/execution/test_when_gate_mutation_kills.py:27` imports the deleted
  `run_with_when_gate_polars` (D3) — add to the D6 handle list (drop the polars case).
- `src/decoy_engine/execution/_strategies/_top_code.py:355` docstring names `run_with_when_gate_polars`.
- `tests/sentry/test_physical_seam_disconnection.py:86` names the deleted adapter path in its
  allowed-import set (like the 4.7 fix — update the allowlist).
- `docs/acceptance-test-flight.md:283` directs readers to cross-substrate parity + the substrate
  matrix workflow (update to pandas-only).
- `docs/native/draw-site-inventory.md:116,207,212` name deleted polars strategy paths.
- Mutation ledgers `execution_when_gate.md:5`, `execution_chunked.md:94`, `execution_planner.md:5`
  need an explicit archive/update decision (mark the polars-adapter mutants historical).

**APPROACH REFRAME (why to stop upfront-gating and build iteratively):** four adversarial plan-gate
rounds each surfaced more scattered references (docstrings, ledgers, doc pages, sentry lists, test
imports across the whole suite). The polars masking adapter is broad-but-shallow: not architecturally
risky, just referenced in many places. A perfect upfront map is a treadmill. The correct completeness
gate is the BUILD's iterative loop, not another plan-gate round:
  1. `rm -r execution/polars/` + fix the 3 unguarded src imports (D2) so the package imports.
  2. Repeatedly: run the full suite (chunked, since local OOMs) + the removal sweep + all sentries;
     fix every import error / stale reference / allowlist / doc they surface; repeat until the
     zero-live-hit sweep is clean AND suite+sentries+ruff+mypy are green.
  3. Then dennis + Codex FINAL review the actual diff (where a scattered-reference miss shows up as
     a real failure, not a plan-review guess) + full CI (both substrates).
The plan's DELETE/EDIT/KEEP map (D1-D7) + this rev3 consumer list are the STARTING point; the sweep
closes the tail. This is the method that worked for Task 4.7 (sweep + import-probe + suite caught the
consumers the plan review could not fully pre-enumerate).

**RECOMMENDATION (Cam):** this is a ~35-file sprawling cleanup best executed as a dedicated fresh pass
using the iterative method above, NOT at the tail of a long multi-merge session. The plan + full known
map are ready on branch `feat/polars-masking-removal`.

## Gates
FRAME (done) → PLAN rev3 (this) → build via the ITERATIVE sweep-driven loop above (Opus; the full
suite + removal sweep + sentries are the completeness gate, not further plan-gating) → SELF-CHECK →
VERIFY → dennis → Codex FINAL → CI (both substrates) → merge (Cam-authorized as the next unit; Slack
milestone). Platform-doc update is a separate off-main worktree PR.
