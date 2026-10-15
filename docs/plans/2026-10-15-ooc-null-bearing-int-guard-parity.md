Status: plan (Phase D fix, OOC null-bearing-int ingest-guard parity. rev 2 (folds Codex plan-gate rev-1 NO-GO, 2 HIGH + 2 MED: (1) OOC sources are lazy/loader-resolved -- guard must read RESOLVED sources via footer null-counts, never materialize; (2) unknown-stats handling -- streamed null-count when footer stats are absent, never assume-no-nulls, never blanket-reject; (3) use the RUNNER seam with the planned extraction, NOT a route-exec wrapper, to retain the direct-runner test invariant + cover all callers; (4) predicate precision -- FK-CHILD exemption, integer+positive-null-count, plain `categorical`). Conformance direction affirmed correct. HOLD build until Codex plan-gate GO AND GAP9 (#266) is merged -- build rebases onto post-GAP9 main (shared _runner.py, at the 700 cap -> extraction required). Autonomous merge on the full green gate; the Codex FINAL prompt must forbid the reviewer from merging.)

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

## 2. The change (RUNNER seam + extraction + lazy-source-aware guard)

Run the null-bearing-int guard inside `run_fk_out_of_core` so OOC rejects identically for EVERY caller (Codex MED-3: route-exec is the sole LIVE run_pipeline OOC entry, but the existing parity harness `_run_ooc` calls `run_fk_out_of_core` DIRECTLY, and the other routes guard at the ingest level; the runner seam retains the direct-runner invariant and matches them. Do NOT put it in a route-exec wrapper and then redirect the harness to hide the direct-runner fail-open).

- **Seam:** `run_fk_out_of_core` preflight (`_runner.py`, by the existing `check_out_of_core_compatibility` / `preflight_group_c` region ~:195-201), on the RESOLVED sources the runner already holds. `_runner.py` is at the 700-LOC hard cap post-GAP9, so the guard call + import REQUIRE an EXTRACTION: move an existing self-contained block (e.g. the preflight sequence, or the OocHashRun/table-kwargs assembly) into a sibling module so the file stays <= 700. Builder picks the block; census reconciled exact.
- **Lazy/loader-resolved sources (Codex HIGH-1):** OOC sources are lazy / loader-resolved, not resident Arrow tables, so the resident-table path (`.column(...).null_count`) is not enough and lazy tables must NOT be materialized. The guard must read null-bearing-int status from the RESOLVED sources via footer counts -- `LazySource.column_null_counts()` (`profile/_readers.py:261`) -- for lazy sources, and the resident null_count for resident ones. This likely needs an OOC-aware guard entry (extend `reject_null_bearing_int` / add a sibling that accepts the OOC source set) rather than the literal `run_chunk_ingest_guards(plan, sources, ...)` call, which assumes resident tables.
- **Unknown-stats handling (Codex HIGH-2):** `column_null_counts()` returns `None` when the Parquet footer has no statistics. In that case the guard must do a CORRECT streamed null-count over the column (pyarrow compute over scanned batches, NO full-column Python materialization) -- it must NEVER assume "no nulls" (would preserve the fail-open bug) NOR blanket-reject on unknown stats (would over-reject a clean job). A bounded prefix sample is INSUFFICIENT (a late null in a later row group would be missed).

No change to the reject POLICY, the rejected-strategy set, masked output of any ACCEPTED job, or any other route. Reject-only, fail-closed.

## 3. Risks

- Seam coverage: the guard must run on EVERY live OOC entry (gate confirms route-exec is the sole live one; else runner-with-extraction).
- Guard cost on OOC-scale inputs (bounded null-check, §2).
- Over-rejection: the fix only ADDS the reject the engine already applies elsewhere; it must reject EXACTLY `_INT_NULL_REJECTED_STRATEGIES` on int+null, and NOT touch redact/passthrough, non-null int, float, or string payloads (the investigation matrix: redact/passthrough/non-null-int/float do NOT diverge). T2 pins this.
- Module size: route-exec seam keeps `_runner.py` untouched (at 700); `_pipeline_route_exec.py` 590 < 600, no census. Both modules are already on the physical-seam sentry allowlist (from prior slices) -- confirm, add none.
- Byte-parity: unaffected for accepted jobs; the change is reject-only (fail-closed), raising the same code as the other routes.

## 3a. Exact runtime predicate (Codex MED-4)

The guard rejects a column iff: its dtype is INTEGER (signed or unsigned) AND it has a POSITIVE null count AND its strategy is in `_INT_NULL_REJECTED_STRATEGIES` (`{truncate, hash, categorical}` -- the guard uses PLAIN `categorical`; it does NOT inspect determinism, which is a separate OOC-compat restriction) AND the column is NOT an FK-CHILD (FK children are guard-EXEMPT -- the exemption is children specifically, not "all keys"; they resolve via the edge). The predicate is integer-dtype + positive-null-count, NOT profile-presence (no_profile is just the path on which the runtime guard is the only backstop).

## 4. Acceptance

- **T1 (the confirmed repro) rejects on OOC via the DIRECT runner:** a two-table FK job, non-FK `int64 [12,null,34]` payload under `truncate` -> `run_fk_out_of_core` (the existing `_run_ooc` direct path) RAISES `ExecutionError(code="null_bearing_int_unsupported")` (exact code), matching full-frame; assert the transactional sink PUBLISHES nothing on the late reject. Mutant: removing the guard -> OOC emits output -> T1 fails.
- **T2 strategy/dtype matrix (no over/under-reject):** on OOC -- integer+null (signed AND unsigned; include a typed ALL-null integer) under truncate / hash(with namespace) / categorical REJECT; redact + passthrough + NON-null int + string payload do NOT get a NEW rejection from this guard; for FLOAT payloads assert "no NEW rejection FROM THIS GUARD" (some float/strategy combos already fail for other reasons -- do not attribute those to the guard). FK-CHILD integer+null column stays EXEMPT (not rejected by this guard).
- **T3 close the parity-harness hole (keep the direct-runner invariant):** extend `_build_single_edge` / `test_out_of_core_fk_parity.py` to add a NON-FK integer-with-null payload column AND its masking seed under a rejected strategy, so `_assert_parity_or_faithful_rejection` exercises the reject-parity invariant. Do NOT redirect `_run_ooc` away from `run_fk_out_of_core` -- the runner seam makes the direct call reject correctly.
- **T4 no regression:** a legitimate OOC FK job (string payload, or non-null int) still runs and matches its baseline; existing OOC parity + routing suites green.
- **T5 lazy-source + unknown-stats (executable, not documented-only):** cover (a) a lazy Parquet source WITH footer stats and a null-bearing int -> reject; (b) a stats-DISABLED Parquet source with a null BEYOND the first row group / batch -> still reject (proves the streamed count, not a prefix sample); (c) a null-free integer control -> NOT rejected; and assert NO full-column materialization occurs (bounded). Unknown stats must neither imply "no nulls" nor cause a blanket reject.

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
