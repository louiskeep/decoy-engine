Status: plan (Phase D fix, OOC null-bearing-int ingest-guard parity. rev 1 DRAFT. HOLD build until Codex plan-gate GO AND GAP9 (#266) is merged -- build rebases onto post-GAP9 main (shared _runner.py). Autonomous merge on the full green gate; the Codex FINAL prompt must forbid the reviewer from merging.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md` (fail-closed; validation-never-mutates; module-size ratchet). Base for planning: engine `origin/main` @ `451060b4`; build base = post-GAP9 main.

# OOC null-bearing-int ingest-guard parity (fail-closed fix)

## 0. Why

The out-of-core (OOC) FK route is the ONLY route that does not run the `reject_null_bearing_int` ingest guard, so it FAILS OPEN on a config every other route fail-closed REJECTS. A non-FK integer payload column that contains at least one null, masked by `truncate` / `hash` / deterministic `categorical`, is a cross-substrate-ambiguous shape the engine decided (PO-settled 2026-05-28, S13) must be REJECTED on every route with code `null_bearing_int_unsupported`. Full-frame (`_pandas_adapter.py:202`), sequential (`_sequential.py:377`), native-chunked (`native/_chunked_entry.py:286`), and the unified slice (`_unified_slice_admission.py:555`) all run the guard. `out_of_core/_runner.py::run_fk_out_of_core` does NOT, so OOC silently masks the unsupported config (confirmed end-to-end: a two-table FK job with a non-FK `int64 [12, null, 34]` payload under `truncate` -> full-frame RAISES `null_bearing_int_unsupported`, OOC emits `['12', None, '34']`).

Correct behavior = REJECT (OOC is the non-conforming route; this fix makes OOC reject identically). Narrow scope: OOC-only, needs an FK job with the offending column a NON-FK (non-key) payload, reachable only under `no_profile=True` (the plan-compile check `check_null_bearing_int_unsupported`, `plan/_checks.py:197`, rejects it at compile when a profile is present; the runtime guard is the backstop OOC lacks). This is a pure FAIL-CLOSED parity fix with NO product decision (the reject policy is already settled); it gates the D5 Phase-D ship (we must not activate the OOC route in platform while it fails open here). Not an accepted diff (absent from `tests/parity/SEMANTIC_DIFFERENCES.md`).

## 1. What exists

- The guard: `execution/_guards.py` `reject_null_bearing_int` (`:34`, raises `null_bearing_int_unsupported` at `:63`) and `run_chunk_ingest_guards` (`:73`, calls it at `:86`). The rejected strategy set is `_INT_NULL_REJECTED_STRATEGIES = {truncate, hash, categorical(deterministic)}` (`plan/_checks.py`); the guard reads the sources to detect a null-bearing integer column.
- Called on every route EXCEPT OOC: full-frame `_pandas_adapter.py:202`, sequential `_sequential.py:377`, native-chunked `native/_chunked_entry.py:286`, unified slice `_unified_slice_admission.py:555`. NOT called in `out_of_core/_runner.py`.
- OOC entry (live path): `run_pipeline` -> `_pipeline_route_dispatch.execute_out_of_core_route` -> `_pipeline_route_exec.run_out_of_core_route:191` -> `run_fk_out_of_core` (preflight region `_runner.py:195` `check_out_of_core_compatibility` + `:201` `preflight_group_c`; table loop ingests at `:440` `_iter_source_batches`). The config-less shadow/physical OOC path (`physical/drivers/_out_of_core.py`) is not the live masking path (see [[physical-seam-sentry-allowlist]] / the D3a finding -- it is gated off by `cheap_admission`).
- OOC compat gate `out_of_core/_compat.py` admits `truncate`/`hash`/`categorical` with NO payload-dtype/null exclusion, so it does not catch this either.
- Coverage hole: `tests/parity/test_out_of_core_fk_parity.py::_assert_parity_or_faithful_rejection` ALREADY asserts "oracle raised => OOC must raise", but its `_build_single_edge` only varies the FK-KEY dtype and ALWAYS uses a STRING payload -- it never builds a non-FK integer payload, so the class-covering harness misses this.

## 2. The change (seam decision)

Run the null-bearing-int guard on the OOC route so it rejects identically. TWO candidate seams; the plan RECOMMENDS the route-exec seam, the gate confirms coverage:

- **PREFERRED -- `_pipeline_route_exec.run_out_of_core_route` (590 LOC, headroom):** call `run_chunk_ingest_guards(plan, sources, registry, relationship_graph)` at the top of `run_out_of_core_route`, BEFORE dispatching to `run_fk_out_of_core`, mirroring `_pandas_adapter.py:202`. This covers the LIVE OOC path (run_pipeline -> route dispatch), raises the SAME `null_bearing_int_unsupported`, and avoids `_runner.py` (which is at the 700-LOC hard cap after GAP9, so a line cannot be added there without an extraction). VERIFY at the gate: `run_out_of_core_route` has `plan`/`sources`/`registry`/`relationship_graph` in scope, and it is the sole LIVE entry to `run_fk_out_of_core` for a real masking job (the physical/shadow path is config-less/not-live).
- **FALLBACK -- `run_fk_out_of_core` preflight (`_runner.py` ~:195-201):** the most thorough (every caller of the runner, matching the other routes' ingest-level placement), but `_runner.py` is at 700 LOC post-GAP9, so this REQUIRES an extraction (move an existing block to a sibling to free room) -- only take this if the route-exec seam does not cover a live entry.

Guard-cost constraint (OOC-scale): `reject_null_bearing_int` must detect null-bearing integer columns CHEAPLY on OOC-scale sources (a pyarrow null-count / metadata check, not a full-column materialization) -- confirm it does not defeat the bounded-memory contract. If it would materialize, scope the check to a bounded probe. (The other routes run it, but OOC inputs are larger; verify.)

No change to the reject policy, the rejected-strategy set, the guard logic, masked output of any ACCEPTED job, or any other route.

## 3. Risks

- Seam coverage: the guard must run on EVERY live OOC entry (gate confirms route-exec is the sole live one; else runner-with-extraction).
- Guard cost on OOC-scale inputs (bounded null-check, §2).
- Over-rejection: the fix only ADDS the reject the engine already applies elsewhere; it must reject EXACTLY `_INT_NULL_REJECTED_STRATEGIES` on int+null, and NOT touch redact/passthrough, non-null int, float, or string payloads (the investigation matrix: redact/passthrough/non-null-int/float do NOT diverge). T2 pins this.
- Module size: route-exec seam keeps `_runner.py` untouched (at 700); `_pipeline_route_exec.py` 590 < 600, no census. Both modules are already on the physical-seam sentry allowlist (from prior slices) -- confirm, add none.
- Byte-parity: unaffected for accepted jobs; the change is reject-only (fail-closed), raising the same code as the other routes.

## 4. Acceptance

- **T1 (the confirmed repro) rejects on OOC:** a two-table FK job, non-FK `int64 [12,null,34]` payload under `truncate`, `no_profile`, routed out_of_core -> `run_pipeline` RAISES `ExecutionError(code="null_bearing_int_unsupported")`, matching full-frame. Mutant: removing the guard call -> OOC emits output -> T1 fails.
- **T2 strategy/dtype matrix (no over/under-reject):** on the OOC route -- int+null under truncate/hash(with namespace)/deterministic-categorical REJECT; redact + passthrough + non-null int + nullable float + string payload do NOT reject (match prior OOC behavior / the oracle). (Per the investigation's verified matrix.)
- **T3 close the parity-harness hole:** extend `test_out_of_core_fk_parity.py` (or `_build_single_edge`) to include a NON-FK integer-with-null payload case under a rejected strategy, so `_assert_parity_or_faithful_rejection` exercises the reject-parity invariant for this class.
- **T4 no regression:** a legitimate OOC FK job (string payload, or non-null int) still runs and matches its baseline; existing OOC parity + routing suites green.
- **T5 guard cost:** assert the OOC guard check is bounded (no full-column materialization) on a larger fixture, OR document the bounded-probe approach.

## 5. Verification + gates

- abi-3 companion venv for OOC parity; ci-mirror for routing/pure-Python. `~/bin/pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`.
- T1-T5 green; OOC parity + routing + the SEQ/native null_bearing_int reject tests stay green; no accepted-job output changed. Coverage + mutation on the guard call (remove-guard mutant killed).
- Module sizes per §2 (route-exec seam: no census; if the fallback runner seam is used, extract to stay <=700 + census exact). Physical-seam sentry: the touched execution module(s) already allowlisted -- confirm.
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build on post-GAP9 main. Autonomous merge on full green, flag Slack before merge-to-main. **The Codex FINAL prompt MUST say: do NOT merge/push/PR; Opus merges after CI.**

## 6. Done when

- [ ] T1-T5 green; OOC rejects int+null-under-{truncate,hash,categorical} identically to the other routes; no accepted job changed; the parity-harness class-hole closed.
- [ ] Module sizes within policy; sentry green. Changed-lines coverage + mutation-proven.
- [ ] diff + evidence to Slack; PR CI green; merge; main CI green. Record in RECENTLY-SHIPPED (fail-closed parity fix) + note it UNBLOCKS the D5 ship.

cam
