Status: plan (engine R3 refactor slice; rev 3 folds Codex plan-gate round 2's 4 MEDIUM; Cam-gated checkpoint inside, see §9)

# R3 engine refactor: pipeline run context + layer-1 route executors + out-of-core liveness reconciliation

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `feature-dev.md`/`architecture.md` (design-notes rule); platform engineering-best-practices §4.1 (module size), §2.1 (validation never mutates). Grounding survey: `docs/records/2026-10-10-r3-refactor-frame.md` (FRAME). Source decision: decoy-platform `ROADMAP.md` refactor track, "Before Phase D" row; `docs/records/2026-10-06-codebase-health-and-refactor-report.md`.

Base: engine `origin/main` @ `ac6a0e8e` (includes C6a #232, C6b-i #233, C6b-fakerfix #234). All line references re-verified against this commit.

**FRAME correction carried into this plan (Codex plan-gate round 1, finding 1).** The FRAME said "native-chunked is reached via the `locals()` hack." That is wrong. The `locals()` forwarding (`run_from_pipeline_locals`) feeds the **unified-slice full-frame fast lane** (`maybe_run_unified_slice`), whose cheap admission explicitly rejects `route_chunked=True` (`_unified_slice_admission.py:212`) and returns a result or `None` to fall back. The native-chunked dispatch lives inside `_pipeline_generate_mask.run_generate_and_mask_steps` (it receives `route_chunked`). This plan is scoped to the real call graph (§5), not the FRAME's mental model.

## 1. Why now, and the real dispatch shape

Phase D (Rust FK routes) builds on the pipeline routing and dispatch surface. Two pieces of accidental complexity make Phase D more expensive the longer they stay:

1. `run_pipeline` (`execution/_pipeline.py:150`, 684 LOC) takes 37 keyword parameters (35 keyword-only; verified by AST) and, for the unified-slice fast lane, forwards its own `locals()` through `run_from_pipeline_locals` (`execution/_unified_slice.py:558`) into `maybe_run_unified_slice`, guarded by an import-time check `_assert_forwarding_covers_signature` (`:587`, invoked `:605`) that compares `inspect.signature(maybe_run_unified_slice).parameters` against the forwarded name set. The `locals()` forwarding and the import-time assertion are a stand-in for a typed carrier; they exist because the call site could not restate the ~30-keyword block under the module-size budget.
2. Layer-1 route executors are asymmetric. `sequential` and `out_of_core` are early-return executors in `execution/_pipeline_route_exec.py` (`run_sequential_route:112`, `run_out_of_core_route:191`); `full_frame` is the inline `with publish:` tail of `run_pipeline` (~557-684), which itself contains the unified-slice short-circuit, `run_generate_and_mask_steps`, the stitch, finalize/quarantine, telemetry, post-validation, and `publish.commit()`.

Actual dispatch (verified `_pipeline.py:446-684`):

```
resolve knobs + run-state (319-419) -> prepare transforms (431-445)
route, route_reason            = resolve_execution_route(...)            # layer 1: full_frame | sequential | out_of_core
execution_plan_decision, route_chunked, keep_lazy = decide_chunk_route(...)  # layer 2 classification
ooc_declined                   = out_of_core_declined(...)
if has_mask_table and route == "sequential":  return run_sequential_route(...)       # executor (extracted)
if has_mask_table and route == "out_of_core":  return run_out_of_core_route(...)      # executor (extracted)
# full_frame path only, from here:
resident_sources = resolve_resident_sources(...)   # ~531-547, lazy-preserving
pool_cache       = PoolCache()                      # ~559, shared by the unified lane and the oracle
result = run_from_pipeline_locals(locals())         # unified-slice FAST LANE; returns ExecutionResult or None
if result is not None: return result
with publish:
    step = run_generate_and_mask_steps(..., route_chunked=route_chunked, ...)   # full-frame oracle + native-chunked + multi-table live HERE
    outputs = stitch_generate_mask_outputs(...); finalize_validators_and_quarantine(...)
    quality_metrics["execution"] = execution_telemetry(route="full_frame", ...)
    compute_post_validation(...); publish.commit(); return ExecutionResult(...)
```

"Make the change easy, then make the easy change." R3 gives the routing surface a typed, phased run context and a named `full_frame` executor so all three layer-1 routes dispatch uniformly, and deletes the `locals()`/import-assertion workaround by threading the context into the unified lane. R3 is **behavior-preserving**: no feature, no routing-decision change, no change to what any route produces, fails with, warns, or when it commits.

## 2. Risk classification

**R2.** Broad refactor of a core execution path that is compatibility-governed and privacy-sensitive (FK/RI preservation, crypto determinism). Gates: written reviewed plan, self-check, full relevant verification, adversarial independent review (dennis), exact-artifact gate, no author self-certification, two-checkpoint Codex gate (plan + post-dennis final). Not R3: no destructive/irreversible production action. The one legacy-deletion question (OOC reorder lane) is explicitly deferred to Phase D (§5.3, §9).

Role map: Opus authored this plan; Sonnet builds from the Codex-approved plan; dennis is the fresh-context adversarial reviewer; Codex is plan reviewer and post-dennis final reviewer. Merge only on a full green chain.

## 3. Scope

**In scope (engine R3 slice):**
- **E5a** `PipelineRunContext`: a frozen value object carrying the **resolve-phase** inputs (§5.1). Field bindings are frozen; the objects they reference (source mappings, sinks, registry, caches) keep whatever mutability the current code relies on. Freezing is shallow and does not deep-freeze sources/sinks/caches.
- **E5b** Phase split of `run_pipeline` into `resolve -> route -> dispatch` over the context + a frozen `RouteDecision` (§5.1), preserving lazy materialization and the single shared `PoolCache`.
- **E5c** Delete the `run_from_pipeline_locals` `locals()` forwarding and the import-time `_assert_forwarding_covers_signature` by threading the context (and the two resolved names) into `maybe_run_unified_slice` as explicit arguments. Replace the import-time drift check with real tests of the context-to-`maybe_run_unified_slice` boundary (§6).
- **P6-engine (full_frame executor)** extract the `with publish:` tail into a named `run_full_frame_route(ctx, route)` executor, peer to `run_sequential_route`/`run_out_of_core_route`, so the three layer-1 routes dispatch uniformly. The unified-slice short-circuit stays inside this executor as a full_frame-internal fast lane, with its own admission and `None`-fallback unchanged. `run_generate_and_mask_steps` is called as-is.
- **OOC liveness RECONCILIATION (record, not deletion)** a by-hand reachability trace + execution-witness evidence that the external-reorder lane is the live P4-A lane (§5.3).

**Out of scope (explicitly):**
- **Native-chunked is NOT elevated to a top-level executor.** It lives inside `run_generate_and_mask_steps`; restructuring that internal chunked dispatch is not R3. R3 passes `route_chunked` through unchanged.
- **Platform P6** (`run_v2_pipeline_job`, `create_job_core`): separate platform sibling slice (platform CI billing-blocked; local CI). A3 contract test is noted for that slice, not built here.
- **Routing logic.** `_pipeline_routing.py` (`resolve_execution_route`, `decide_chunk_route`, `_sequential_eligible:172`, `_has_cross_table_fk_cycle:143`) and its helpers are FROZEN. R3 changes how inputs are carried, never how routes are decided.
- **Any legacy-path hard delete.** R3 confirms-and-documents reachability; Phase D deletes.
- **The public `run_pipeline` keyword signature** (names, defaults, positional/keyword kind, order). Public API (`decoy_engine/__init__.py`), compat-contract-governed, cross-repo callers. Unchanged; the context is built internally from the same arguments.

## 4. Behavior-preservation contract (invariants the build may not move)

Output equality alone does NOT define behavior preservation (Codex finding 5). The full contract:

1. **Routing decisions identical.** Layer-1 `resolve_execution_route` -> {full_frame, sequential, out_of_core} + SC2 reject-before-read (`fk_full_frame_oom_risk_rejected`); layer-2 `decide_chunk_route` -> `route_chunked`/`execution_plan_decision`/`keep_lazy`. Both layers preserved exactly; the taxonomy is not flattened.
2. **Per-route output preserved exactly, proven two ways (Codex finding 4):**
   - **Within-route pre/post-refactor identity**, via a pinned baseline captured BEFORE extraction (§6, §8): the refactor must not change any route's output at all.
   - **Existing cross-route pandas parity is retained as-is**, including its documented normalized-value comparison (`test_out_of_core_fk_parity.py` folds Arrow string/list WIDTH drift and IEEE-NaN-vs-null; it does NOT assert artifact-byte identity) and its rejection contracts. The composite-scalar child shape stays **REJECTED** with `out_of_core_composite_fk_scalar_child_unsupported` (`SEMANTIC_DIFFERENCES.md` records a raw-value leak; it is not an admitted divergence). "Bytes" is not claimed for resident results or written artifacts beyond what these harnesses actually check.
3. **Failure and publication behavior preserved (Codex finding 5):** exception type/code/message and validation-before-read/write ordering; sink open/write/commit/abort including post-validation failure; warning order and content, row errors, table kinds, deterministic telemetry; the unified lane's distinction between a fallback miss (`None`), an invariant failure, and a provider failure; orphan policies and FK typing (nullable large ints, unsigned keys, composite/null keys, precision-loss rejection).
4. **Stamp-nothing-on-default hot path unchanged.** The all-default config path gains no work; goldens and compat corpus do not move. Proven by an explicit stamp-ABSENCE assertion, not by output equality (Codex finding 6).
5. **Public `run_pipeline` signature frozen** (§6 signature-freeze test).
6. **No new coupling into cheap admission.** `test_physical_seam_disconnection` stays green; the context must not pull `execution.physical` into cheap admission.

## 5. Design

### 5.1 Phased run context (E5a/E5b)

Resolution happens in phases, so a single frozen-once object cannot hold everything (Codex finding 2: `resident_sources` resolves ~547 and `pool_cache` ~559, both full_frame-only; routing results come between). The model is layered, each layer frozen at its own phase:

- **`PipelineRunContext`** (frozen dataclass, likely `execution/_pipeline_context.py`): the resolve-phase inputs available by ~419 plus the prepared transforms (~431-445). Fields: resolved config/run-state, resolved `registry`, resolved `key_provider`, `graph`, `table_kinds`, `profile`, `plan`, prepared transforms, namespace registry, routing knobs (thresholds/budgets/flags), projection policy, `engine_version`, telemetry inputs. Built once after resolution; validation-only construction that does not mutate its inputs (best-practices §2.1). Frozen bindings only; referenced sources/sink/registry keep current mutability.
- **`RouteDecision`** (frozen): `route`, `route_reason`, `execution_plan_decision`, `route_chunked`, `keep_lazy`, `ooc_declined`. Produced by the (frozen) routing helpers.
- **Route-local execution state**: `resident_sources`, `pool_cache`, and the resolved `caller_sources` are built INSIDE `run_full_frame_route` (they are full_frame-only and must not be materialized before a bounded route can early-return or reject, Codex finding 2). The single `PoolCache` created there is shared by the unified-slice fast lane and the oracle exactly as today.

`run_pipeline` becomes: `ctx = _resolve(...)` -> `decision = _route(ctx)` -> early-return `run_sequential_route(ctx, decision)` / `run_out_of_core_route(ctx, decision)` -> else `run_full_frame_route(ctx, decision)`. Fowler: *Introduce Parameter Object* + *Preserve Whole Object* + *Extract Function*. The public function keeps its exact signature and resolves internally.

Where derived values already have resolved names at the call sites (the `_PIPELINE_RESOLVED_NAMES` pair in the current forwarding, e.g. resolved registry/key provider vs their raw inputs), the context carries the RESOLVED values, and the boundary test (§6) asserts resolved-not-raw forwarding with distinguishable test values (Codex finding 3).

### 5.2 full_frame executor (P6-engine)

Extract `run_full_frame_route(ctx, decision) -> ExecutionResult` wrapping the current `with publish:` tail: build route-local state (resident sources, pool cache), run the unified-slice fast lane (`maybe_run_unified_slice` via the context; return its result when not `None`), else run `run_generate_and_mask_steps` (unchanged, still receiving `route_chunked` from `decision`), stitch, `finalize_validators_and_quarantine`, `execution_telemetry(route="full_frame")`, `compute_post_validation`, `publish.commit()`. `sequential` and `out_of_core` executors are adapted to take `(ctx, decision)` in place of their current wide kwarg lists.

Executor placement has a size budget (Codex finding 8): `_pipeline_route_exec.py` is 582 LOC (18 under GOAL 600). `run_full_frame_route` cannot land there without crossing 600. It goes in its own module (e.g. `execution/_pipeline_full_frame.py`) or the existing file is split so every destination stays <= GOAL; the chosen placement and each destination's resulting LOC are named in the build.

### 5.3 Out-of-core liveness reconciliation (record)

The external-reorder lane IS reachable and test-covered on current main: `out_of_core/_runner.py:257` selects `_stream_driver.stream_table` when `route.reorder_caps is not None`. `reorder_caps` is set by `_route_policy.decide_route`, which requires (verified) a sink, incoming edges, both a memory and a disk budget, acceptable merge fan-in, and acceptable per-edge payload width, with the parent-key test using the largest incoming relation's DEDUPLICATED, NULL-FILTERED key count (not source row count) against `REORDER_PARENT_KEY_THRESHOLD = 2_000_000` (`out_of_core/_route_policy.py:49`) (Codex finding 7). This is the P4-A Task 7 lane, distinct from the reverted OOC-B (#108; memory `decoy-ooc-b-parked-codex-block`).

Deliverable: a committed record (`docs/records/2026-10-10-ooc-reorder-liveness.md`) that (a) traces reachability by hand from `run_out_of_core_route` to `stream_table` (automated 0-importer scans gave false positives and are not trusted), (b) cites the ACTUAL parity and perf runs that cover the lane with their revision and limitations (not just test filenames): `tests/parity/test_out_of_core_route_seam_parity.py`, `tests/perf/test_out_of_core_reorder_*_memory.py`, and the reorder unit/budget tests, and (c) establishes the P4-A-vs-OOC-B distinction from durable provenance (the P4-A Task 7 commit/PR and the #108 revert), not from the record's own assertion. R3 deletes nothing. The record is the evidence Phase D consumes before any hard-delete, and is the Cam-visible checkpoint (§9). If any reorder branch is NOT reachable (contradicting current evidence), that is a material finding and the slice stops for direction rather than deleting or "fixing" it.

## 6. Acceptance tests (defined here; no later contributor may weaken these)

**Captured BEFORE extraction, on `ac6a0e8e` (Codex findings 9, 2, 3):**
1. **Within-route output baseline, with an explicit snapshot projection** (Codex finding 3): for a fixed fixture set spanning full_frame (resident single-table, multi-table, generate+mask, unified-admitted, unified-declined-fallback), sequential, and out_of_core, snapshot each route's `ExecutionResult` and assert the refactored code reproduces the projection exactly. The projection compares EXACTLY, as deterministic fields: per-table outputs (`to_pydict()` + Arrow schema, applying only the two `SEMANTIC_DIFFERENCES.md` normalizations where a cross-route comparison applies), `warnings` (order + content), `row_errors`, `table_kinds`, quarantine, and the DETERMINISTIC members of `quality_metrics["execution"]` telemetry (`route`, `route_reason`, sink-active flags, `inputs_streamed`) plus `execution_plan`/`fidelity_reports` presence+content. It EXCLUDES, as enumerated volatile fields whose VALUES vary between identical runs: `timings`, `boundary_conversion_ms`, and any wall-time/RSS measurement inside the telemetry. For the volatile fields the test asserts STRUCTURE only (keys present, types, and that the same lane populates vs omits them), never their measured values. No volatile field is dropped silently; each is listed in the test. This is the within-route identity proof of §4.2.
2. **Public-signature freeze**: snapshot `run_pipeline`'s parameters (names, defaults, kind, order) and fail on any change.

Also captured on `ac6a0e8e` before extraction, feeding test #5 (Codex finding 2): the expected exception type/code/message, the validation-before-read/write ordering, and the sink open/write/commit/abort event trace (including a post-validation failure) for #5's characterization cases, so those failure/publication invariants have a pre-extraction reference rather than being authored later against the extracted implementation.

**Added WITH the extraction of each executor (Codex finding 9):**
3. Each layer-1 executor (`run_full_frame_route`, `run_sequential_route`, `run_out_of_core_route`) has a parity test against its baseline AND a dispatch-witness test proving `run_pipeline` routes to it for the routing inputs that select it.
4. **Context boundary** (`test_pipeline_run_context_boundary`, replaces `_assert_forwarding_covers_signature`, Codex finding 3): asserts the context + threaded args cover every parameter `maybe_run_unified_slice` requires, INCLUDING derived inputs (`plan`, `profile`, `graph`, `route_reason`, `adapter`, `pool_cache`), and that RESOLVED values (not raw registry/key provider) are forwarded, using distinguishable test values so a raw-vs-resolved swap fails. Kept separate from the public-signature freeze (#2).
5. **Failure/publication characterization** (Codex findings 5, 2): pipeline-level tests pinning exception type/code/message + validation-before-read/write ordering; sink open/write/commit/abort including a post-validation failure; warning order/content, row errors, table kinds, telemetry; the unified lane's fallback-miss vs invariant-failure vs provider-failure distinction; orphan policies and FK typing (nullable large ints, unsigned, composite/null keys, precision-loss rejection). These assert against the pre-extraction trace baseline (captured in step 1 on `ac6a0e8e`) and run with EACH executor change, so a changed exception precedence or sink open/commit/abort order is caught at the commit that causes it, never blessed by a test written against the already-extracted code. The existing `test_unified_slice_exception_boundary.py`, the unified-slice inertness/parity/Faker tests, `test_out_of_core_fk_parity.py`, and the route-seam parity test are in the MANDATORY verification set; lower-level FK tests alone do not verify context plumbing.
6. **Reorder reachability** (`test_ooc_reorder_lane_reachable`, Codex finding 7): a public-pipeline integration case with an eligible FK shape, a sink, deterministic memory+disk budgets, acceptable fan-in, and a key count over the (overridable) threshold, with a spy that observes the real `_stream_driver.stream_table` executing for the child AND asserts the child does NOT use the batch joiner. Paired with default-threshold boundary tests and negative admission cases (missing sink, over-wide payload, unresolvable budget), reusing the anti-vacuity witnesses in `test_route_policy.py`.
7. **Hypothesis parity + stamp-absence** (Codex finding 6), specified concretely:
   - **Output oracle:** `PandasExecutionAdapter.run` (the full-frame pandas path), the same oracle the parity harnesses use. Each generated case asserts `run_pipeline`'s output equals the oracle's under the §4.2 normalizations.
   - **Independent routing baseline:** a test-owned expected-route function that maps the generated `(table shapes, knobs)` to the expected `(route, route_chunked)` by a literal decision table written IN the test. It must NOT import or call `_pipeline_routing` (or any routing helper under refactor), so a routing drift makes expected != observed. The observed route comes from the execution witness below.
   - **Generated matrix (bounded):** operators `{hash, redact, truncate, passthrough}` on a string column, one `faker` column (name), one `fpe` column, one `date_shift` on a date column; Arrow types `{string, large_string, int64, int64-nullable, bool}`; data cases `{all-present, with-nulls, empty table, single row}`; seeds `{0, 1, 42}`; row counts at `{threshold-1, threshold, threshold+1}` for BOTH the auto-chunk and the full-frame-reject/out-of-core thresholds, set via the overridable `*_threshold_rows` knobs so fixtures stay small; knob combinations `auto_chunk ∈ {on, off}` x `unified_slice_enabled ∈ {on, off}`.
   - **Pinned environment:** fix `use_byte_estimate_routing` / `use_probe_routing` and the byte-estimate probe inputs to deterministic values so routing is a pure function of the generated inputs.
   - **Reproducible Hypothesis settings:** `@settings(derandomize=True, deadline=None, max_examples=<pinned>)` with an explicit `@example` per threshold boundary.
   - **Execution witness:** assert the lane that actually ran (from `quality_metrics["execution"]` telemetry / a spy) equals the independent baseline's expected route.
   - **Stamp-absence:** a separate, non-Hypothesis assertion that the all-default config path stamps nothing new in `quality_metrics` (explicitly check the keys that default runs must NOT add), since output equality cannot prove "gains no work."

**Must pass unchanged:** `test_out_of_core_routing`, `test_auto_chunk_routing`, `test_byte_estimate_routing`, `test_composite_routing`, `test_c1b_i_route_regression`; parity `test_out_of_core_fk_parity`, `test_out_of_core_route_seam_parity`, `tests/physical/test_shadow_ooc_fk`, `test_shadow_mixed_fk`; property `tests/property/test_fk_keys_invariants`; goldens + compat corpus (no movement).

**Sentries (green, with required updates):** `test_module_size` (§7), `test_physical_seam_disconnection`, `test_production_execution_modules_are_byte_identical_to_origin_main` (its allowlist of permitted execution-dir diffs must be updated for the new context/executor modules, as an explicit reviewed change), public-import-boundary, operator-registry, unified-backend-map, exception-hierarchy.

## 7. Module-size census (corrected per Codex finding 8)

Policy (from `test_module_size.py`): GOAL 600, MAX 700. DENSE (600<x<=700) entries MAY grow to MAX with a visible same-PR bump and MUST ratchet down on shrink; a file dropping to <=600 DELETES its entry (not reset to a smaller number); LEGACY (>700) may only shrink; nothing crosses MAX fresh; the recorded value must EQUAL current LOC. Current: `_pipeline.py` = 684 (dense), `_unified_slice.py` = 605 (dense), `_pipeline_route_exec.py` = 582 (no entry, 18 under GOAL).

Obligations:
- Update the census in EVERY size-changing commit (deferring to a final commit breaks the exact-match on earlier commits).
- `_pipeline.py` net-shrinks as resolution/context/executor logic moves out; re-sync its entry down each step, and DELETE the entry if it reaches <=600.
- `_unified_slice.py` shrinks when the `locals()` forwarding + import assertion are removed; re-sync or delete per the same rule.
- Any new module (context, full_frame executor) over GOAL gets an entry at its exact LOC with the named dense justification; the aim is each lands <=600 and needs none. `_pipeline_route_exec.py` has only 18 LOC of headroom, so a new executor does not go there without a split.
- R3 does NOT impose a stricter-than-policy shrink-only rule; the goal is net reduction, which the decomposition achieves by construction. If `_pipeline.py` does not net-shrink, the slice is reframed, not forced.

## 8. Build sequencing (Codex finding 9) and verification

Ordered, individually-green commits within one gated slice (one dennis + one Codex-final over the whole diff). Every commit must leave the full suite AND all sentries green, so census and the byte-identical-modules allowlist are updated IN the commit that changes an execution module, never deferred (Codex finding 4):
1. Capture the three pre-extraction baselines on `ac6a0e8e`: within-route output baseline (#1), public-signature freeze (#2), and the failure/publication trace baseline feeding #5. No production code changes. Suite green.
2. Introduce `PipelineRunContext` + `RouteDecision`; thread them into the already-extracted `sequential`/`out_of_core` executors; add their parity + dispatch-witness tests (#3) and run the #5 cases applicable to those routes against the step-1 trace baseline. Re-sync census AND update the byte-identical-modules allowlist for the new context module, with narrow justification, in this commit. Suite + sentries green.
3. Extract `run_full_frame_route` (unified-slice short-circuit preserved inside it); add its parity + dispatch-witness test (#3), the context-boundary test (#4), and the full_frame #5 cases against the step-1 baseline. Re-sync census + allowlist for the new executor module in this commit. Suite + sentries green.
4. Thread the context into `maybe_run_unified_slice`; delete the `locals()` forwarding + `_assert_forwarding_covers_signature` ONLY after #4 and the unified-slice connection/fallback tests pass. Re-sync census + allowlist in this commit. Suite + sentries green.
5. Add the reorder reachability + boundary/negative tests (#6) and the Hypothesis parity + stamp-absence (#7); write the §5.3 reconciliation record. Suite + sentries green.

Each commit is a recoverable checkpoint. Per-commit and pre-gate verification: `ci-mirror` clean-env (ruff check + ruff format --check + mypy over the changed set); `pytest-one` on the §6 suites (routing + parity + sentries + new tests); `test_module_size`, `test_physical_seam_disconnection`, and `test_production_execution_modules_are_byte_identical_to_origin_main` every size- or execution-module-changing commit. Lint own direct edits before committing.

## 9. Open decision surfaced to Cam (does not block build; gates the Phase-D hard-delete, not the R3 merge)

The §5.3 reconciliation record states the P4-A external-reorder lane is live and is not an OOC-B remnant, backed by execution witnesses and durable provenance. R3 deletes nothing on its strength. The decision Cam owns is the subsequent Phase-D one: whether that evidence is sufficient to hard-delete the legacy FK paths Phase D replaces. R3's build proceeds without it; the record is handed to Cam at the end of the slice so the Phase-D plan can cite a sanctioned reconciliation.

## 10. Risks and mitigations

1. **Subtle default-path byte-change** from re-plumbing 37 interdependent inputs. Mitigation: within-route baseline (#1), stamp-absence assertion (#7), untouched goldens/compat corpus; frozen context, validation-only construction.
2. **Lifecycle error** materializing sources before a bounded route can reject (Codex finding 2). Mitigation: route-local state (resident sources, pool cache) stays inside `run_full_frame_route`; lazy materialization and the shared PoolCache preserved and tested (#5).
3. **Unified-lane semantics** (fallback-miss vs invariant vs provider failure) silently changing. Mitigation: the existing exception-boundary test is mandatory; the context-boundary test pins resolved-value forwarding.
4. **Census per-commit exact-match** breakage. Mitigation: census updated in every size-changing commit (§7).
5. **full_frame extraction placement** crossing GOAL. Mitigation: explicit size budget + named destinations (§5.2, §7).
6. **Routing flattening / scope bleed** (platform P6, native-chunked elevation). Mitigation: routing frozen and in the contract; native-chunked explicitly out of scope; platform P6 a sibling slice.

## 11. Done when

- [ ] Within-route baseline + public-signature freeze captured before extraction and green after.
- [ ] `PipelineRunContext` + `RouteDecision` threaded; three layer-1 routes dispatch through named executors; unified-slice fast lane preserved inside full_frame.
- [ ] `locals()` forwarding + import-time assertion deleted; replaced by the context-boundary test; unified-slice connection/fallback tests green.
- [ ] Failure/publication characterization, reorder reachability (+boundary/negative), Hypothesis parity + stamp-absence all present and green; mandatory existing tests in the set.
- [ ] Census updated per size-changing commit; entries deleted at <=600; new >600 modules justified; no >700.
- [ ] OOC reorder liveness reconciliation record committed with by-hand trace + cited parity/perf runs (revision+limitations) + durable P4-A/OOC-B provenance.
- [ ] byte-identical-modules sentry allowlist updated as a reviewed change; all §6 suites + sentries green; ci-mirror clean.
- [ ] dennis gate green (remediated), Codex final gate green.
- [ ] Reconciliation record handed to Cam for the Phase-D hard-delete decision.

cam
