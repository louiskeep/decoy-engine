Status: plan (engine R3 refactor slice; Codex plan-gate PENDING; Cam-gated checkpoint inside, see §9)

# R3 engine refactor: pipeline run context + per-route executors + out-of-core liveness reconciliation

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `feature-dev.md`/`architecture.md` (design-notes rule); platform engineering-best-practices §4.1 (module size), §2.1 (validation never mutates). Grounding survey: `docs/records/2026-10-10-r3-refactor-frame.md` (FRAME, branch `docs/fk-shape-matrix`). Source decision: decoy-platform `ROADMAP.md` refactor track, "Before Phase D" row; `docs/records/2026-10-06-codebase-health-and-refactor-report.md` (E5/P6/OOC-audit ids).

Base: engine `origin/main` @ `ac6a0e8e` (includes C6a #232, C6b-i #233, C6b-fakerfix #234). All line references below are against this commit and were re-verified when this plan was written.

## 1. Why now, and why this shape

Phase D (Rust FK routes) builds directly on the pipeline routing + dispatch surface. That surface currently carries two pieces of accidental complexity that would make Phase D more expensive and more error-prone the longer they stay:

1. `run_pipeline` (`execution/_pipeline.py:150`, 684 LOC) takes ~37-38 keyword parameters and, for the native-chunked lane, forwards its own `locals()` through `run_from_pipeline_locals` (`execution/_unified_slice.py:558`), guarded by an import-time signature check `_assert_forwarding_covers_signature` (`:587`, invoked `:605`). That `locals()` forwarding exists because the parameter block is too wide to restate at the call site, and the import-time assertion exists to catch drift between the two. Both are workarounds for a missing value object.
2. Per-route executors are asymmetric. `sequential` and `out_of_core` are extracted functions in `execution/_pipeline_route_exec.py` (`run_sequential_route:112`, `run_out_of_core_route:191`, plus `run_mask_chunked:542` and shared `execution_telemetry:63`), but `full_frame` is the inline tail of `run_pipeline` and `native-chunked` is reached only through the `locals()` hack. Phase D adds Rust FK handling on exactly these routes; asymmetric entry points mean every Phase D change has to be made in two different code shapes.

"Make the change easy, then make the easy change." R3 makes the routing surface uniform and deletes the `locals()`/import-assertion workaround, so Phase D reads and writes one executor-per-route shape with an explicit, typed run context.

R3 is **behavior-preserving**: no new features, no routing-decision changes, no output-byte changes. That is the whole acceptance bar (§6).

## 2. Risk classification

**R2.** Broad refactor of a core execution path that is compatibility-governed and privacy-sensitive (FK/RI preservation, crypto determinism). Per `risk-and-exceptions.md`: written reviewed plan, self-check, full relevant verification, adversarial independent review (dennis), exact-artifact gate. No author self-certification. Two-checkpoint cross-model Codex gate applies: (1) this plan before build, (2) finished product after dennis is green.

Not R3: this slice takes no destructive/irreversible production action. The one legacy-path deletion question (OOC reorder lane) is explicitly deferred out of this slice (§5.3, §9) and belongs to Phase D's hard-delete step, which is separately Cam-gated.

Role map: Opus authored this plan; Sonnet builds from the Codex-approved plan; dennis is the fresh-context adversarial reviewer; Codex is the plan reviewer and the post-dennis final reviewer. Merge only on a full green chain.

## 3. Scope

**In scope (engine R3 slice):**
- **E5a** `PipelineRunContext`: a frozen value object built once in `run_pipeline`, carrying the resolved run inputs the lanes consume. Thread it to the lanes in place of the wide kwarg block.
- **E5b** Phase split of `run_pipeline` into `resolve -> route -> dispatch/finalize` helpers that operate on the context.
- **E5c** Delete `run_from_pipeline_locals`'s `locals()` forwarding and the import-time `_assert_forwarding_covers_signature`, replacing the drift guard with a real unit test over the context (§6).
- **P6-engine (per-route executor normalization)** give `full_frame` and `native-chunked` named executors with the same `execute(ctx) -> ExecutionResult` shape as the existing `sequential`/`out_of_core` executors, so all four routes dispatch uniformly.
- **OOC liveness RECONCILIATION (report, not deletion)** a reachability trace from `run_out_of_core_route` confirming the external-sort/reorder lane (`out_of_core/_runner.py:257`) is the live P4-A lane and not a stranded OOC-B remnant, committed as a record with parity + perf evidence. This is the input Phase D needs before it hard-deletes any legacy FK path (§5.3).

**Out of scope (explicitly):**
- **Platform P6** (`run_v2_pipeline_job`, `create_job_core`) is a separate platform sibling slice (platform CI is billing-blocked; local CI). It is NOT in this engine slice. The A3 contract test (platform FK-cycle predicate mirrors the engine) is noted for that slice, not built here.
- **Any routing-logic change.** `_pipeline_routing.py` (`decide_execution_route:275`, `decide_chunk_route`, `_sequential_eligible:172`, `_has_cross_table_fk_cycle:143`) is frozen. R3 moves how inputs are carried, never how routes are decided.
- **Any legacy-path hard delete.** R3 confirms-and-documents reachability; Phase D deletes.
- **The public `run_pipeline` keyword signature.** It is public API (`decoy_engine/__init__.py`), compat-contract-governed, and called cross-repo (platform v2_runner, unmask/vault/keyprovider). It does NOT change. The context is engine-internal, built from the same arguments.

## 4. Behavior-preservation contract (the invariants the build may not move)

1. **Routing decisions are identical.** Layer-1 `decide_execution_route` -> {full_frame, sequential, out_of_core} and SC2 reject-before-read (`fk_full_frame_oom_risk_rejected`); layer-2 `decide_chunk_route` -> native-chunked admission via the unified-slice gate / `native/_dispatch.py`. Both layers preserved exactly; the four-route taxonomy does not map 1:1 to the three layer-1 branches and must not be flattened.
2. **Outputs are byte-identical vs the pandas oracle** on every golden and parity fixture, including GATE-TQ0's 100% crypto/RI preservation. The one already-recorded divergence (composite-scalar child, `tests/parity/SEMANTIC_DIFFERENCES.md`) stays exactly as recorded; R3 neither fixes nor worsens it.
3. **Stamp-nothing-on-default hot path is unchanged.** The all-default config path must not gain work; goldens and the compat corpus must not move.
4. **Public `run_pipeline` signature frozen** (keyword names, defaults, order). Verified by a signature-freeze test (§6).
5. **No new cross-module coupling into the cheap-admission path.** The context must not pull `execution.physical` into cheap admission (`test_physical_seam_disconnection`).

## 5. Design

### 5.1 PipelineRunContext (E5a) and the phase split (E5b)

Introduce a frozen dataclass `PipelineRunContext` (likely `execution/_pipeline_context.py`, its own module so `_pipeline.py` net-shrinks, see §7). It is built once near the top of `run_pipeline`, after knob validation and run-state resolution (today inline at `_pipeline.py` ~319-419), from the same arguments the public signature already accepts. It carries the resolved, immutable run inputs the lanes read: resolved config/run-state, transform plan, routing inputs, seed/namespace material, output projection, telemetry handles. It is a value object (no behavior, no I/O, validation-at-construction only per best-practices §2.1: construction must not mutate its inputs).

Fowler refactorings this is: *Introduce Parameter Object* (the kwarg block -> context) followed by *Preserve Whole Object* (lanes take the context, not re-extracted scalars), then *Extract Function* for the phases. `run_pipeline` becomes: `ctx = _resolve(...)` -> `route = _route(ctx)` -> `return _dispatch(route, ctx)`, where `_dispatch` selects the per-route executor (§5.2). The public function keeps its exact signature and does the resolve internally; callers see no change.

### 5.2 Per-route executor normalization (P6-engine)

Target shape: one executor per route behind a uniform `execute(ctx) -> ExecutionResult`.
- `sequential` and `out_of_core` already have extracted executors (`_pipeline_route_exec.py:112`, `:191`); adapt their entry to take the context.
- `full_frame` currently inline in `run_pipeline`'s tail (~564-684) becomes a named `run_full_frame_route(ctx)` executor.
- `native-chunked` currently reached via the `locals()` hack becomes a named executor the dispatcher calls directly; the hack and its import-time assertion (E5c) are deleted once nothing routes through `run_from_pipeline_locals`.

A small route -> executor mapping in `_dispatch` replaces the current branchy tail. This is *Replace Conditional Dispatch with Polymorphism*'s function-table variant (no class hierarchy needed; a dict/match on the route enum is enough and keeps the module small).

### 5.3 Out-of-core liveness reconciliation (report)

The FRAME established this is a RECONCILIATION, not a dead-blob prune. The external-reorder lane IS reachable and test-covered on current main: `out_of_core/_runner.py:257` selects `_stream_driver.stream_table` (the reorder driver) when `route.reorder_caps is not None`, auto-selected for a sink table whose parent-key count reaches `REORDER_PARENT_KEY_THRESHOLD = 2_000_000` (`out_of_core/_route_policy.py:49`) with a resolvable budget. This is the P4-A Task 7 lane, **distinct from the reverted OOC-B** external-reorder variant (#108, 200M regression; see memory `decoy-ooc-b-parked-codex-block`).

Deliverable: a committed record (`docs/records/2026-10-10-ooc-reorder-liveness.md`) that traces reachability from `run_out_of_core_route` to `stream_table` by hand (automated 0-importer scans gave false positives and are not trusted), lists the covering tests (`test_ooc_external_sort*`, `test_stream_driver_reorder_caps`, `test_route_policy*`, `test_ooc_reorder_budget`, `test_slim_sort_reorder_acceptance`, `tests/perf/test_out_of_core_reorder_*_memory`, `tests/parity/test_out_of_core_route_seam_parity`), and states plainly which reorder branches are live with parity+perf proof and that none is an OOC-B remnant. R3 deletes nothing here. The record is the artifact Phase D consumes before it hard-deletes legacy FK paths, and it is the Cam-visible checkpoint (§9).

Vestigial flags `PLANNER_ROUTING_ENABLED=False` and `DECOY_SUBSTRATE` are §PREGA-tagged, not R3; this slice does not touch them.

## 6. Acceptance tests (defined here; no later contributor may weaken these)

**Behavior-preservation (must pass unchanged):** `test_out_of_core_routing`, `test_auto_chunk_routing`, `test_byte_estimate_routing`, `test_composite_routing`, `test_c1b_i_route_regression`; parity `test_out_of_core_fk_parity`, `test_out_of_core_route_seam_parity`, `tests/physical/test_shadow_ooc_fk`, `test_shadow_mixed_fk`; property `tests/property/test_fk_keys_invariants`; the golden corpus and compat corpus (no movement).

**New tests this slice must add:**
1. `test_pipeline_run_context_covers_signature` replaces the deleted import-time `_assert_forwarding_covers_signature`. It asserts `PipelineRunContext` carries every input the public `run_pipeline` signature declares (parametrized over the signature so it fails if a parameter is added without a context field). This is the real test that the import-time hack stood in for.
2. `test_run_pipeline_signature_frozen` snapshots the public keyword signature (names, defaults, kind) and fails on any change, as a compat-contract guard.
3. Per-route executor parity: each named executor (`full_frame`, `sequential`, `out_of_core`, `native_chunked`) produces byte-identical output to the oracle on its golden fixtures, invoked through the new uniform `execute(ctx)` entry.
4. `test_ooc_reorder_lane_reachable`: an executable assertion (not just the §5.3 doc) that at/above `REORDER_PARENT_KEY_THRESHOLD` with a resolvable budget the route policy yields `reorder_caps is not None` and the runner selects `stream_table`, pinning the P4-A lane as live so a future accidental dead-code prune fails this test.
5. A Hypothesis property (required for fast-path parity from R1 onward): over generated all-default and simple-operator configs, the context-threaded path yields the same route decision and the same output bytes as the oracle, exercising the stamp-nothing hot path.

**Sentries (must stay green, with required updates):** `test_module_size` (see §7), `test_physical_seam_disconnection`, public-import-boundary, operator-registry, unified-backend-map, exception-hierarchy, seam-disconnection diff sentry.

## 7. Module-size census (load-bearing constraint)

Current recorded entries (`tests/sentry/test_module_size.py`): `_pipeline.py` = 684 (dense: 600 < x <= MAX 700, 16 LOC under the hard ceiling; must ratchet DOWN on shrink, may never be raised), `_unified_slice.py` = 605.

E5 moves resolution/context/phase logic out of `_pipeline.py` into `_pipeline_context.py` (and possibly a `_pipeline_dispatch` helper). Required outcomes:
- `_pipeline.py` net-shrinks; its census number is re-synced DOWN to the new exact LOC (never up).
- `_unified_slice.py` shrinks when the `locals()` forwarding + import assertion (~lines 558-605) are deleted; its census number re-synced down.
- Any new module over GOAL (600) gets its own census entry at its exact LOC; the aim is for `_pipeline_context.py` and helpers to each land under 600 and need no entry.
If E5 cannot net-reduce `_pipeline.py`, it cannot land. The decomposition is the point, not a side effect.

## 8. Build sequencing and verification

Build as ordered, individually-green commits within one gated slice (one dennis + one Codex-final gate over the whole diff):
1. Introduce `PipelineRunContext` and build it in `run_pipeline`; thread it to the already-extracted `sequential`/`out_of_core` executors. Suite green.
2. Add `full_frame` and `native-chunked` named executors; route `_dispatch` through the uniform mapping. Suite green.
3. Delete `run_from_pipeline_locals` `locals()` forwarding + `_assert_forwarding_covers_signature`; add tests #1/#2. Suite green.
4. Add executor-parity tests #3, reachability test #4, Hypothesis property #5; write the §5.3 reconciliation record.
5. Census re-sync (§7) + sentry updates.

Each commit is a recoverable checkpoint. Verification per commit and before the gate: `ci-mirror` clean-env (ruff check + ruff format --check + mypy over the changed set), `pytest-one` on the targeted suites in §6 (routing + parity + sentries + new tests), plus `test_module_size` and the seam-disconnection sentry. Lint own direct edits before committing (ruff check + format + mypy) per standing rule.

## 9. Open decision surfaced to Cam (does not block build; gates the Phase-D hard-delete, not the R3 merge)

The OOC reorder-lane reconciliation (§5.3) produces a Cam-visible record stating the P4-A external-reorder lane is live and is not an OOC-B remnant. R3 deletes nothing on the strength of it. The decision Cam owns is the subsequent one, in Phase D: whether the reconciled evidence is sufficient to hard-delete the legacy FK paths that Phase D replaces. This plan's build proceeds without that decision; the record is handed to Cam at the end of the slice so the Phase-D plan can cite a sanctioned reconciliation. If the trace surfaces any reorder branch that is NOT reachable (contradicting current evidence), that is a material finding and the slice stops for direction rather than deleting or "fixing" it.

## 10. Risks and mitigations

1. **Subtle default-path byte-change** from re-plumbing ~37 interdependent inputs. Mitigation: the Hypothesis parity property (#5) + the untouched golden/compat corpus; context is frozen and construction is validation-only (no mutation) per §2.1.
2. **Asymmetric executor extraction** (full_frame inline + native-chunked via the hack) is genuinely new structure, not a pure move. Mitigation: per-route executor-parity tests (#3) before deleting the old entry points; delete the `locals()` path only after nothing routes through it.
3. **Census ceiling** could block landing if `_pipeline.py` does not shrink. Mitigation: the decomposition targets net reduction by design (§7); if it does not, the slice is reframed, not forced.
4. **Two-layer routing flattening** risk. Mitigation: `_pipeline_routing.py` is frozen and in the behavior-preservation contract; routing tests pin both layers.
5. **Scope bleed into platform P6.** Mitigation: platform P6 is explicitly out of scope and scheduled as a sibling slice.

## 11. Done when

- [ ] All §6 behavior-preservation tests green, all new tests present and green.
- [ ] `locals()` forwarding + import-time assertion deleted; replaced by real tests.
- [ ] Four routes dispatch through uniform named executors.
- [ ] `_pipeline.py` + `_unified_slice.py` census ratcheted down to exact LOC; any new >600 module has its entry.
- [ ] OOC reorder liveness reconciliation record committed with by-hand reachability trace + parity/perf evidence.
- [ ] ci-mirror clean; targeted suites + sentries green.
- [ ] dennis gate green (remediated), Codex final gate green.
- [ ] Reconciliation record handed to Cam for the Phase-D hard-delete decision.

cam
