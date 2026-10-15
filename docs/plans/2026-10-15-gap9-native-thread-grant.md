Status: plan (Phase D, GAP9: honor the native_threads grant on the sequential + out-of-core routes. rev 1 DRAFT. HOLD build until Codex plan-gate GO. Autonomous merge on the full green gate per Cam's standing order; the Codex FINAL prompt must forbid the reviewer from merging. Cam 2026-10-15: GAP9 is in Phase D now that D4 is done.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; byte-parity). Base: engine `origin/main` @ `fecdd794` (D3a merged; the reorder guard #265 touches only `_route_policy.py`/`_reorder_budget.py`, which GAP9 does not, so no overlap). FRAME line numbers that predate D3a are re-located on current main below.

# GAP9: honor the native_threads grant on the sequential + out-of-core routes

## 0. Why + scope

Native (Rust companion) masking is real and multi-threaded (rayon; ~2.7-3.2x on the hash path at 100M), and the per-job `native_threads` grant is ALREADY honored end-to-end on the chunked / full-frame / unified-slice / multi-table routes. The gap is NARROW: the two BOUNDED routes build their native-run objects with no thread argument, so the grant is silently dropped to 1:
- Sequential: `run_sequential` builds `SeqNativeRun()` (`_sequential.py:312`) with no arg.
- Out-of-core: `run_fk_out_of_core` does not accept `native_threads` at all and builds `OocHashRun()` (`out_of_core/_runner.py:231`, and `(hash_run or OocHashRun())` at `:376`) with no arg.
Both run objects, and every native kernel beneath them, ALREADY accept and honor `native_threads`; only the value never reaches them on these two routes.

GAP9 = thread the existing `ctx.native_threads` grant from the route dispatch down to `SeqNativeRun` / `OocHashRun`, so a passed value is honored on the sequential and out-of-core routes exactly as it already is on the chunked route. **Honor-only, byte-safe.**

Scope guardrails (do NOT overbuild):
- The engine default stays `native_threads=1` (`run_pipeline`, `_pipeline.py:139`). GAP9 changes NO default and NO runtime behavior until a caller passes >1; golden/compat fixtures run at 1 and stay byte-identical.
- NO scheduler machinery, NO reading box pressure, NO touching `adaptive_scheduler_lease_authority_enabled`. The DYNAMIC per-job grant (how many threads from live pressure) remains the paused scheduler's job (S9). GAP9 only guarantees a value, once passed, is honored.
- The thread-count POLICY knobs already exist in the Rust crate (`threads.rs`: default resolve `None->1`, knee `DEFAULT_NATIVE_MASK_THREAD_KNEE=4` + env `DECOY_NATIVE_MASK_THREAD_KNEE`, clamp `min(requested, host_available, knee)`, max 1024). GAP9 adds NO new knob. Raising the engine default, a new default-raising env, and cgroup-quota-aware clamping are DEFERRED product decisions (surfaced to Cam, §8), not this slice.

## 1. What exists to build on

- The grant source + validation: `run_pipeline(native_threads: int = 1)` (`_pipeline.py:139`) -> `ctx.native_threads` (`_pipeline_context.py`), validated 1..=1024 (`require_lane_knobs`, `_pipeline_auto_chunk.py`).
- The two dispatch sites that DROP it (current main): `execute_sequential_route` (`_pipeline_route_dispatch.py:27`, calls `run_sequential_route` at `:32`) and `execute_out_of_core_route` (`:54`, calls `run_out_of_core_route` at `:57`) - neither forwards `ctx.native_threads`. (The module docstring at `:5` even notes the route-exec functions "already take" the knob - i.e. the dispatch is the drop.)
- The forward chains: `run_sequential_route` (`_pipeline_route_exec.py:112`) -> `run_sequential` (`_sequential.py:209`) -> `SeqNativeRun()` (`:312`). And `run_out_of_core_route` (`_pipeline_route_exec.py:191`) -> `run_fk_out_of_core` (`out_of_core/_runner.py`) -> `OocHashRun()` (`:231`, `:376`).
- The honoring code (already present, unchanged by GAP9): `SeqNativeRun.__init__(native_threads=None)` and `OocHashRun.__init__(native_threads=None)` flow the value into the bound kernels (`BoundHashKernel`/`BoundIndexKernel`/`BoundFpeKernel`), and the OOC natives (`out_of_core/_bucket_native.py`, `_categorical_native.py`, `_fpe_native.py`, `_hash_native.py`) read `bound.native_threads`. The PyO3 boundary resolves the budget per call (`decoy-engine-native/src/arrow_ffi.rs`).
- Byte-parity across thread counts is PROVEN in-crate (do not re-litigate, do extend at the Python route level): `decoy-engine-native/src/batch.rs` tests `thread_count_never_changes_output`, `derive_index_thread_count_never_changes_output`, `thread_invariance_holds_for_non_string_admitted_types`, the TSan concurrent-fill test, and first-error-wins arbitration (`first_error_by_row_index`). The Python reference ignores `native_threads` and documents thread-invariant output (`native/_index_ext.py:284-288`).

## 2. The change (mechanical; honor-only)

Thread `native_threads` (default `1`) through both chains. Exact edits:
1. `execute_sequential_route` (`_pipeline_route_dispatch.py:32`): pass `native_threads=ctx.native_threads` to `run_sequential_route(...)`.
2. `run_sequential_route` (`_pipeline_route_exec.py:112`): add `native_threads: int = 1`; forward to `run_sequential(...)`.
3. `run_sequential` (`_sequential.py:209`): add `native_threads: int = 1`; `SeqNativeRun(native_threads=native_threads)` at `:312`.
4. `execute_out_of_core_route` (`_pipeline_route_dispatch.py:57`): pass `native_threads=ctx.native_threads` to `run_out_of_core_route(...)`.
5. `run_out_of_core_route` (`_pipeline_route_exec.py:191`): add `native_threads: int = 1`; forward to `run_fk_out_of_core(...)`.
6. `run_fk_out_of_core` (`out_of_core/_runner.py`): add `native_threads: int = 1`; `OocHashRun(native_threads=native_threads)` at both `:231` and `:376`.
No change to defaults, kernels, the resolve/clamp, error arbitration, or any other route.

## 3. Module-size constraints (HARD - read before editing)

- `out_of_core/_runner.py` is **699 LOC, ONE under the 700 hard max** (`tests/sentry/test_module_size.py`). Over-max files may ONLY shrink, so the edit (6) must be **net <= 700**: make it net-zero (e.g. import the already-imported `OocHashRun` and pass the kwarg on the existing construction lines without adding net lines; the signature kwarg is +1). If the kwarg + two call-site edits cannot stay <= 700, **extract** a small helper into an existing/!new sibling rather than grow `_runner.py` past 700 - crossing 700 is NOT allowed even with a census bump. Update the census to the EXACT new LOC if it changes (and stays <= 700).
- `_sequential.py` 677 and `_pipeline_route_exec.py` 586: both over the 600 goal / under 700; update the census to the EXACT new LOC for any that cross a current entry. `_pipeline_route_dispatch.py` 75: ample headroom.

## 4. Physical-seam sentry allowlist (do NOT forget - this bit D3a AND the reorder guard)

All four changed modules live under `src/decoy_engine/execution/`, so `tests/sentry/test_physical_seam_disconnection.py`'s exact-diff gate (`test_production_execution_modules_are_byte_identical_to_origin_main`) will FAIL unless each is in `permitted_non_physical` with a rationale: `_pipeline_route_dispatch.py`, `_pipeline_route_exec.py`, `_sequential.py`, `out_of_core/_runner.py`. (`_pipeline_route_exec.py`, `_sequential.py`, `_runner.py` are likely already listed from prior slices - ADD only the missing ones, e.g. `_pipeline_route_dispatch.py`, each importing NOTHING from `execution.physical`.) Do NOT add to `DELIBERATELY_CONNECTED_MODULES`. See [[physical-seam-sentry-allowlist]]. Run the sentry locally to confirm.

## 5. Acceptance matrix

- **T1 wiring (both routes):** spy on the constructed `SeqNativeRun` / `OocHashRun` (or the bound kernel's resolved `native_threads`) and assert the value passed to `run_pipeline(native_threads=N)` reaches them on the sequential route and the OOC route. Mutants: dropping the forward at any of the 6 edits -> the spy sees 1 -> killed. (Mirror B3's `run_mask_chunked` thread-spy.)
- **T2 byte-parity across thread counts (both routes):** a sequential FK/mask job and an OOC FK job each produce byte-identical output at `native_threads=1` vs `N>1` (digest match), covering the native-admitted operator surface (hash / index-faker / fpe / bucket / categorical as applicable per route). Mirror the chunked parity gate `tests/parity/native/test_phase2_gate.py`. This is the slice's GATE.
- **T3 default unchanged:** with no `native_threads` passed, the sequential + OOC routes behave byte-identically to pre-GAP9 (run at 1); existing golden/compat/parity suites stay green unmodified.
- **T4 scaling (directional, not a hard bar):** a hash-heavy sequential and OOC job show a wall-time improvement at `N>1` vs `1` on a large-enough table (directional; small shapes are overhead-dominated, so pick a sufficient size or mark informational).
- **T5 memory bounded (OOC + reorder-guard active):** peak RSS at `N` threads on a large OOC job (reorder guard in effect) stays within the per-job budget and is flat-ish vs the 1-thread run (threads do not duplicate the output buffer). Devbox-feasible scale; NO new GCP.

## 6. Risks (reproduce, do not smooth)

- **OOC threads stack on the reorder-guard buffer** within one per-job budget; a raised OOC grant could pressure memory. GAP9 keeps the default 1 so this is latent until a caller opts in; document it (and the separate unexplained native-runtime memory item recorded in platform RECENTLY-SHIPPED) as a consideration for whoever raises the OOC grant. Do NOT add a separate OOC cap here (scheduler/caller's job).
- **The engine clamp reads host cores, not the cgroup CPU quota** (`threads.rs` `available_parallelism`); bounding to the container quota is platform/scheduler's responsibility, not GAP9. Note, do not change.
- **Byte-parity is the gate** (T2/T3). The only real failure mode is a mis-wire passing the wrong value, not kernel arithmetic (proven thread-invariant in-crate).

## 7. Verification + gates

- abi-3 companion venv (`~/.cache/decoy-d1a-abi3-venv`) for the native parity/scaling/memory tests; ci-mirror for routing/pure-Python + the sentry. `~/bin/pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`.
- T1-T5 green; sequential + OOC parity + routing + the native parity suites green; golden/compat byte-identical at default. Coverage + mutation on the 6 forward edits (each drop -> T1 fail).
- Module sizes per §3 (hard: `_runner.py` <= 700); physical-seam sentry allowlist per §4; census exact if crossed.
- Established-methodology note is unnecessary (reuses the existing thread budget); keep comments to WHY (the grant was dropped on these two routes).
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build freely; autonomous merge on full green, flag Slack before merge-to-main, PR, PR CI green, merge, confirm main CI. **The Codex FINAL prompt MUST say: do NOT merge/push/PR; Opus merges after CI.**

## 8. Deferred product decisions (surface to Cam; NOT in this slice)

1. Raise the engine default `native_threads` above 1 (today raising requires the caller to pass a value). Recommend KEEP 1; let platform/scheduler pass the value.
2. A new engine-level env to raise the default (so a native deployment gets threads without every caller passing one).
3. Make the engine clamp cgroup-quota-aware (vs leaving quota-bounding to platform/scheduler).
4. How aggressive the OOC grant may get given the reorder-guard memory stacking + the unexplained native-runtime memory item.
5. The dynamic per-job grant from live box pressure = the paused scheduler (S9), explicitly OUT of GAP9.

## 9. Done when

- [ ] T1-T5 green; the grant is honored on sequential + OOC; default-1 behavior byte-identical; parity across thread counts holds on both routes.
- [ ] `_runner.py` <= 700 (extract if needed, never census-exempt past 700); other census entries exact; physical-seam sentry allowlist updated.
- [ ] Changed-lines coverage + mutation-proven; diff + evidence to Slack; PR CI green; merge; main CI green.
- [ ] ROADMAP + RECENTLY-SHIPPED updated (GAP9 shipped); the deferred items (§8) recorded.

cam
