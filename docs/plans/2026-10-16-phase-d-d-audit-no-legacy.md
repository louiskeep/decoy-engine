Status: plan (Phase D terminal slice, D-audit: no-legacy COMPUTE-backend audit + witness coverage + dead-path hard-delete. rev 1 DRAFT. Cam 2026-10-16: scope = AUDIT THE COMPUTE BACKEND (per the program plan's own §5:63 "it is about the masking, not the route's bounded orchestration"); do NOT remove operators from the shared `_dispatch_mask_node` (it is shared with the retained full-frame oracle). HOLD build until Codex plan-gate GO. Flag Slack before merge-to-main; hard-deletes are the risk area.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `pre-release-audit.md`, `architecture.md`; engine `CLAUDE.md` (Pre-GA = hard delete; validation-never-mutates; module-size ratchet; `internal/` boundary; graphify-trace before deleting; established methodology). Base: engine `origin/main` @ `1a180f2e` (all Phase D merged; engine 0.9.0 released + platform pinned at D5). Program: `docs/plans/2026-10-10-phase-d-fk-routes-program.md` §5.

# D-audit: no-legacy compute-backend audit (terminal Phase D slice)

## 0. Why

Phase D moved every admitted FK-cell masking COMPUTE onto a kernel (compiled Rust, or a named-Arrow kernel, or a no-op) on the sequential, native-chunked, and out-of-core routes; the full-frame pandas path is retained ONLY as the oracle baseline (§5). D-audit is the mandatory terminal slice that PROVES this is true and hard-deletes the now-dead legacy payload-masking paths, so the pandas masking compute is unreachable for an admitted cell and a future change cannot silently reintroduce it. Scope is the COMPUTE backend, not the shared `_dispatch_mask_node` orchestration (Cam 2026-10-16; §5:63). No new compute path is built (the FRAME confirmed all operators already run on a kernel).

## 1. What exists

- **SEQ seam:** `_sequential.py:430` dispatches every scalar node through `PandasExecutionAdapter._dispatch_mask_node` (shared with full-frame `run`, `_pandas_adapter.py:303`); each handler consults `ctx.seq_native` (`_adapter.py:213`) and routes compute to its kernel when admitted, else an in-handler Python/named-Arrow branch. seq_native `_OPS` (`_sequential_native.py:181-190`): HASH, FPE, CATEGORICAL, BUCKET_PERTURB, DATE_SHIFT, FAKER, TEXT_REDACT, TEXT_MASK (compiled Rust / Rust-span+Python-splice for text). redact + truncate run the named-Arrow kernels `redact_array` / `truncate_array` (`kernel/_scalar.py:110,183`); passthrough is a no-op. ALL are kernel-backed today; NONE needs a new path.
- **Witness mechanism (already shipped):** `SeqNativeRun.metrics()` (`_sequential_native.py:667-683`) emits per-column `executed_backend` disposition on `quality_metrics` under `sequential_<strategy>`; witness harnesses `tests/parity/_seq_witness.py` (MarkingKernel + `assert_native_consumed`/`assert_python_consumed`/`spy_python_hash`) and `_kernel_witness.py` (OOC buffer-identity); per-operator SEQ native+witness tests exist (hash/fpe/categorical/bucket/date_shift/faker/text_redact/text_mask). GAP: redact/truncate/passthrough are NOT in `_OPS`, so they have NO `executed_backend` entry and no named-Arrow witness (their on-kernel compute is unproven vs the handler fallback).
- **Legacy/fallback paths (hard-delete candidates):** OOC bucket_perturb pandas bridge `out_of_core/_mask_group_c.py:462-479` + `_to_series` `:524-533` (reachable only when `native.bound is None`); redact handler pandas fallback `_strategies/_redact.py:39-42` (redundant with `redact_array`'s own `_redact_array_reference`). Per-handler Python branches (text splice loops, hash named-kernel fallback) remain LIVE for declined/NER/companion-absent cells (witness-fence, NOT delete).
- **Reorder liveness (reachability baseline):** `docs/records/2026-10-10-ooc-reorder-liveness.md` + `docs/records/2026-10-15-d4a-reorder-revalidation.md`; tests `tests/unit/execution/test_r3_ooc_reorder_reachability.py`, `tests/perf/test_d4a_reorder_reachability.py`. Confirm the live P4-A external-reorder lane; guard the reverted OOC-B O(rows) regression.
- **Defense-in-depth predicate:** `native/_dispatch.py:233` `fk_relationship_not_native_route` — audit-confirm it stays narrower-never-wider and no admitted cell depends on it (do NOT delete).

## 2. The change (audit + witness coverage + hard-delete; compute-backend scope)

- **(A) Consolidated no-legacy audit test (headline, none exists today).** A single cross-cutting test over the §4 admitted (shape x operator x route) matrix that, for EVERY admitted cell, asserts BOTH: (i) a compute witness fired on a kernel (native `executed_backend` disposition for seq_native/OOC/native-chunked ops, or a named-Arrow witness for redact/truncate/passthrough), AND (ii) the pandas-fallback compute did NOT fire (via the MarkingKernel/spy mechanism, extended as needed). Routes: sequential, native-chunked, out-of-core. Full-frame is the oracle (excluded from the no-legacy requirement by §5). The per-operator tests stay; this adds the consolidated sweep that fails if ANY admitted cell silently uses pandas.
- **(B) Witness coverage for redact / truncate / passthrough on SEQ (the one real harness gap).** Give these three a compute witness proving the named-Arrow kernel (`redact_array`/`truncate_array`) or the no-op ran for an admitted cell, not the handler's pandas fallback. Preferred: a named-Arrow witness (do NOT force them into the seq_native compiled `_OPS` if that is semantically wrong — redact/truncate are named-Arrow, not companion kernels). The builder picks the narrowest mechanism (a disposition entry or a spy) consistent with the existing witness design; a test asserts the named-Arrow path ran + the pandas fallback did not.
- **(C) Graphify-traced hard-deletes (Pre-GA = hard delete).** Before deleting, graphify-trace each path for callers (the platform graph is refreshed; engine graph via `graphify update` if needed). Delete ONLY paths with no reachable caller for an ADMITTED cell:
  - OOC bucket_perturb pandas bridge (`_mask_group_c.py:462-479` + `_to_series`): delete IFF OOC admission GUARANTEES companion-present (fail-closed) for bucket_perturb so `native.bound is None` is unreachable for an admitted cell. If companion-absent is a SUPPORTED degraded mode, do NOT delete - witness-fence it and record why.
  - redact handler pandas fallback (`_redact.py:39-42`): delete IFF `redact_array`'s own `_redact_array_reference` fully covers the `pa.ArrowException` case (redundant), proven by test.
  - KEEP (witness-fence, do NOT delete): the declined / NER / companion-absent Python branches in each handler - they are legitimately live for non-admitted cells.
- **(D) Reorder-lane reachability assertion.** Cite the R3 + D4a records as the baseline and assert (by the existing reachability tests, extended if needed) that the live route is the P4-A external-reorder lane and NO OOC-B O(rows) remnant is reachable.
- **Do NOT** touch `_dispatch_mask_node` (shared FF orchestration), the full-frame oracle, `_canonicalize_source`/`_encode_int`, or any kernel. No compute path is built. No accepted-job output changes (deletes are of unreachable-for-admitted fallbacks; any kept fallback is unchanged).

## 3. Risks (reproduce, do not smooth)

- **Deleting a reachable fallback.** A fallback deleted while still reachable (companion-absent degraded mode, a declined cell) breaks a supported path. Graphify-trace + a no-caller proof + the full test suite BEFORE each delete; when in doubt, witness-fence instead of delete. The OOC bucket bridge delete is conditional on a proven companion-present admission guarantee.
- **Vacuous audit.** The consolidated test must prove the pandas-fallback did NOT fire (not merely "a dispatch ran" / "output looks right"). Use the MarkingKernel/spy so a mutant that routes an admitted cell through pandas FAILS the audit. Mutation: force an admitted cell to the pandas branch -> audit red.
- **Witness-mechanism correctness for redact/truncate.** They are named-Arrow, not companion kernels; the witness must prove the NAMED-ARROW kernel ran, not conflate it with a compiled-companion disposition.
- **Module size + physical-seam sentry.** Hard-deletes shrink `execution/` modules (good); any changed `execution/` module stays in `permitted_non_physical` ([[physical-seam-sentry-allowlist]]); census only shrinks. New audit test file is fine.
- **Pre-GA gate.** `is_pre_ga()` governs hard-delete (engine CLAUDE.md). Confirm RELEASE_PHASE == pre-ga before deleting.
- **Determinism envelope untouched.**

## 4. Acceptance

- **Consolidated no-legacy audit (A):** for every admitted (shape x operator x route) cell across SEQ / native-chunked / OOC, the test asserts a kernel compute witness fired AND the pandas-fallback did not. Mutant: route one admitted cell through `_dispatch_mask_node`'s pandas compute -> audit fails. (Full-frame excluded as the oracle.)
- **Witness coverage (B):** redact, truncate, passthrough on SEQ each have a compute witness proving the named-Arrow/no-op path ran for an admitted cell; a mutant forcing their pandas fallback fails the new test.
- **Hard-deletes (C):** each deleted path proven caller-free (graphify-trace + grep) for admitted cells; full suite green after deletion; the OOC bucket bridge either deleted (with the companion-present admission guarantee proven) or explicitly retained+witness-fenced with a recorded reason; redact ArrowException fallback deleted only if `_redact_array_reference` covers it (test). Kept declined/NER/companion-absent branches unchanged + still covered.
- **Reorder reachability (D):** the P4-A lane is the live reorder path; no OOC-B O(rows) remnant reachable (assertion green, citing the records).
- **No regression:** full engine suite green (abi-3 companion venv for native/OOC/parity; ci-mirror for pure-python); no accepted-job output change; determinism envelope + `_dispatch_mask_node` + FF oracle untouched; module census only shrinks.

## 5. Verification + gates

- abi-3 companion venv `~/.cache/decoy-d1a-abi3-venv/bin/python` for native/OOC/parity/witness; ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python` for pure-python/sentry. ALWAYS `PYTHONPATH="$(pwd)/src:$(pwd)" ~/bin/pytest-one <python> <paths> -q`. One test run at a time. (AVOID the flaky `tests/test_cgroup_job_launcher.py` equivalent if any full-suite run is attempted; name suites.)
- Graphify-trace the delete candidates before deleting (platform graph refreshed; `graphify update` the engine if its graph is used).
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build on feat/d-audit (off engine main 1a180f2e). Autonomous merge on FULL green (dennis + Codex + CI), flag Slack before merge-to-main (hard-deletes). The Codex FINAL prompt MUST forbid the reviewer from merging; Opus merges after CI.
- DOCUMENT: program plan §5 D-audit boxes checked; roadmap + RECENTLY-SHIPPED (Phase D CLOSED); engine graph refreshed if used; Slack section-complete explainer. Phase D closes.

## 6. Done when

- [ ] Consolidated no-legacy audit test green (every admitted cell kernel-witnessed + pandas-fallback-not-fired; mutant kills it); redact/truncate/passthrough SEQ witnessed.
- [ ] Dead legacy payload paths graphify-traced caller-free + hard-deleted (OOC bucket bridge per the companion-present determination; redact ArrowException fallback if redundant); kept fallbacks (declined/NER/companion-absent) unchanged + covered; module census shrinks; physical-seam sentry green.
- [ ] Reorder P4-A liveness asserted; no OOC-B remnant reachable.
- [ ] Full engine suite green; no accepted-job output change; determinism + `_dispatch_mask_node` + FF oracle untouched. dennis + Codex final GO; CI green; merged. Phase D CLOSED; docs current.

cam
