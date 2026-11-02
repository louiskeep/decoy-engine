Status: handoff

Summary: F29's engine half is built; what V1 still owes is two consumers. The CLI `decoy run --json` failure envelope and the Platform failed-job node error should carry the engine's registered reason (identity, outcome, retry class), and the Platform error nodes (execution and vault) should stop carrying exception text onto an owner-visible surface. Progress-event consumer wiring is cut from V1.

Revision 2 (2026-11-02, from the Dennis review of `72579da8`): P1 keeps today's `.code` chain walk, so job status selection is unchanged (V1-1); P1 now creates the shared error-node builder and reader `api/jobs/job_node_error.py` that F31 P8 had claimed, and goes first (V1-2, owner ruling); P1 also closes the vault node and legacy raw rows (V1-3); C1's refusal set is derived from the exit mapping, and a rewritten NER failure keeps the engine reason (M-4, M-5). Change map in section 14.

Baselines (`git rev-parse`, 2026-11-02): engine `origin/main` `0173c05` (registry 1.2.0); engine `v0.17.0`, the version both consumers build against, carries registry **1.1.0** (`git show v0.17.0:src/decoy_engine/reasons/_registry.py:24`); Platform `origin/main` `d8908f42`; CLI `louiskeep/decoy` `origin/main` `a5d36b46` (read-only shallow clone); F31 plan `plan/f31-safe-output` revision 5 at `ac8e1cf3`.

Risk: **R2**, plan only. The build slices change a public CLI JSON envelope, the Platform node-error shape and an evidence-manifest model, all pre-GA. This document grants no build approval. Review order: Dennis, then Codex.

Rules consulted: 00-universal, development-loop, risk-and-exceptions, scope-discipline, reuse-first; README-selected rulebooks named as testing, api-and-compatibility, security, architecture, documentation. **Not read in this session:** the dev-rules tree and its README routing table are not in this container (`ls /home/cam` fails), so the rulebook selection is carried from prior plans for the same task shape (`docs/plans/2026-10-29-f29-machine-readable-errors-progress.md:11`) and is unverified. Read: engine and Platform `CLAUDE.md`; Platform `handoffs/queue` `docs/handoffs/V1-BAR.md` and `DEFECT-CATALOG.md`; F31 revision 5 sections 3.8, 6 and 11.

# F29 remainder: consumer reasons for the CLI and Platform (FRAME + PLAN)

## 1. FRAME

### 1.1 What is already done (verified)

| F29 clause (MASTER §F29, Platform `docs/plans/Market Expansion Specs/MASTER - Decoy Feature Specification.md`) | State | Evidence |
|---|---|---|
| One versioned reason registry, engine-owned | **Done** (engine #336 `fdd08a8`, unified in #349) | `src/decoy_engine/reasons/_registry.py:24`; `docs/reasons.md`; pin `tests/fixtures/f29/reason-registry-release.json` |
| Retry classes and outcome categories | **Done** | `RetryClass` / `Outcome` per phase rule; `docs/reasons.md` "Retry classes", "Outcome categories" |
| Unknown exceptions receive a safe fallback | **Done** | `reason_from_exception` (`src/decoy_engine/reason_adapter.py`) returns `decoy.unexpected_error` for any class outside `registered_families()`; never reads message text |
| Safe record and reader for untrusted payloads | **Done** | `ReasonRecord.to_payload` / `from_payload` (never raises; recomputes severity, outcome and retry; malformed input becomes `decoy.reason_record_invalid`) / `.summary` |
| Versioned events with bytes, retry and checkpoint fields | **Done in the engine** | `_progress_events.py` `RetryInfo`, `CheckpointInfo`, `RetryEvent`, `CheckpointEvent`; A6, A7, A10 in the build record |
| CLI maps reasons to exits; categories distinguishable | **Not done** | CLI `src/decoy/cli/run.py:861-1059`, below |
| Platform shows the same reason, with job and attempt state | **Not done** | Platform imports nothing from `decoy_engine.reasons`; pins engine `0.16.0` (`pyproject.toml:36`), which has no `reasons` package |

Pinned-registry limit: the consumers build against 0.17.0 (registry 1.1.0). `FileAccessDenied` maps to a reason only on engine main (`reason_adapter.py:98` at `0173c05`; absent at `v0.17.0`), so a file-access refusal gets no specific reason on 0.17.0. Every acceptance assertion therefore reads `registry_version` and identities as `<pinned>` (what the installed engine returns), never a literal version.

### 1.2 What F29 still owes V1 (the problem, re-derived)

**P-CLI. `decoy run --json` has no machine-readable failure reason.** The envelope (`run.py:1040-1047`) is `{command, status, config, mode, error}`, with `error = str(exc)[:500]`. A machine code exists only for capacity, native gate and missing extra (`:1048-1058`). An engine publication failure (`CommitError`) or a verifier failure lands in exit 3 or 1 with prose only; CI cannot tell execution, publication and verification apart. MASTER: "Execution failure, findings, verification failure, and publication failure remain distinguishable".

**P-PLAT-1. The Platform error nodes carry exception text to the job owner.** (a) Execution node: `_failure_details` (`api/jobs/_partial_publication.py:174`) sets `message = getattr(selected, "message", None)` (`:201`); `_error_node_run` stores it (`api/jobs/v2_node_runs.py:434`). Engine `.message` attributes embed exception text (`generation/statistical/_spec.py:160` at `v0.17.0`, `f"snapshot_file {path!r} could not be read: {exc}"`). (b) Vault node: `api/jobs/v2_vault.py:97` stores `{"code": "vault_write_failed", "message": str(exc)}`. (c) Legacy rows: `_hydrate_nodes` copies a non-JSON row's raw text into `nodes[].error.message` (`api/evidence/assembly.py:1980-1987`, the copy at `:1984`). All three are served to the non-admin owner by `GET /api/jobs/{id}/node-runs` (`routes_runtime_facts.py:70-98`, `JobNodeRunOut.error: str | None`, `schemas.py:431`) and by the manifest. **V1 bar rule 3**, class 1.

**P-PLAT-2. `Job.log` cannot show the engine reason.** `error_projection.py` renders only codes in `APPROVED_ERROR_MESSAGES` (`:42-68`); an engine failure renders `internal error: <TypeName>`. Safe, but the Platform shows less than the CLI. MASTER: "Platform shows the same reason".

### 1.3 Definition of done

1. `decoy run --json` failure envelope carries `reason` (`ReasonRecord.to_payload()`) for engine-entered failures, computed from the exception as raised, before any CLI rewrite; exit integers unchanged. C-T1 to C-T8 pass.
2. Every Platform error node (execution, vault) is written by `job_node_error.node_error` and read by `job_node_error.project_node_error` on both readers (node-runs route, manifest). The stored and served `message` follows one rule (section 5.2); no exception text is stored or served; legacy rows are withheld at read. The execution node carries `reason` for engine failures. P-T1 to P-T7 pass; `node-error` ratchet empty.
3. `Job.log`'s terminal failure line for an engine-reasoned failure shows the registry summary and `error_class=<namespace>.<code>` (P2, after F31 P2b). P-T8, P-T9 pass.
4. `docs/reasons.md` gains a "Consumers" section naming the surfaces and limits.

### 1.4 Reuse inventory

Engine (public in 0.17.0): `reason_from_exception`, `reason_adapter.registered_families`, `ReasonRecord.to_payload` / `from_payload` / `.summary`. CLI: `emit_json` and the single failure emit site (`run.py:1040`), the exit-mapping tuple (`run.py:944-1000`). Platform: `_failure_details` chain walk (kept as is), `APPROVED_ERROR_MESSAGES`, `TrustedFailure` / `project_failure`, the persisted-exports re-projection pattern (`routes_runtime_facts.py:105-115`), the module-size census pattern for ratchets (`tests/sentry/test_module_size.py`). One new module, `api/jobs/job_node_error.py`, which F31 revision 5 already designs (section 3.8 there); P1 builds it once for both plans.

### 1.5 Boundaries

- **Engine:** no production change. A code the registry lacks is an engine registry addition outside this plan (O3).
- **F31 (revision 5, `ac8e1cf3`).** Owner ruling 2026-11-02: **F29 P1 lands first** (it needs only the Platform 0.17.0 repin; F31 P8 needs F31's engine release ER and another repin). P1 creates `job_node_error.py` (`node_error(..., reason=)`, `project_node_error` keeping `reason` through `ReasonRecord.from_payload`), routes both error-node writers through it, deletes the `.message` read, and ships the `node-error` ratchet at an empty baseline. F31 P8 then shrinks to: the `error_type` key (`safe_error_type`), the `platform.job_failed` prose replacing P1's fallback prose for rows with no reason, and any site added after P1. **F31 revision 6 must be amended to match** (P8 scope, T-P8, the `node-error` baseline 3 to 0, the two `exc-copy` rows P1 rewrites); the owner routes that to the F31 worker; this branch does not edit F31.
- F31 also owns the CLI `error` text (`run.py:1005`), CLI `print_exception`, and every other Platform except-bound copy. P2 waits for F31 P2b, which rewrites `Job.log` into `decoy-safe-output/v1` lines (section 5).
- **Dependency:** the Platform slices need the Platform repin to engine `0.17.0` or later (handoff `p0-platform-repin-engine-0-17-0`). The CLI pins 0.17.0 (`pyproject.toml:103`).
- **decoy-web:** `JobNodeRunOut.error` stays a JSON string with the same keys plus `reason`; the frontend is not touched. C0 opens `web/src/pages/JobDetail.tsx` to confirm it reads `message` as text only.

## 2. Established solutions, searched first

Searches run: web, "Google AIP-193 ErrorInfo reason domain machine-readable error RetryInfo"; web, "RFC 9457 problem details type extension members"; `rg` for `reason_from_exception|decoy_engine.reasons|ReasonRecord` across Platform `api/` and CLI `src/`.

1. **AIP-193 `ErrorInfo`** ([aip.dev/193](https://AIP.dev/193)): a stable `(reason, domain)` pair is the machine identity, separate from the message. The engine `(namespace, code)` follows it; consumers carry the pair unchanged. **Followed.** AIP-193 `RetryInfo` (a delay) is **not adopted**: the engine retry class is advice by category.
2. **RFC 9457** ([rfc9457](https://www.rfc-editor.org/rfc/rfc9457.pdf)): type identity separate from `detail`; unknown extension members are ignored. `reason` is an additive member. **Followed.**
3. **In-codebase:** `decoy ci` derives rule ids from the registry (CLI `rule_codes.py:93-103`); the Platform re-projects stored exports on read (`routes_runtime_facts.py:105-115`) and withholds exception text in `TrustedFailure`. Reused; no new sanitizer.

## 3. Guarantee, pinned to the V1 bar

A ratchet with named limits:

1. **Function.** For a `decoy run --json` failure the envelope carries `reason` equal to `reason_from_exception(engine_exc)` under the pinned engine, where `engine_exc` is the exception as raised; CLI-owned refusals carry none. Exit integers are unchanged. On the Platform, **job status selection is unchanged**: `_failure_details` keeps today's chain walk (first non-empty string `.code`, any class), so `details.selected` and the cancelled-versus-failed decision (`v2_orchestrator.py:300-303`) are identical; P-T2 and the existing `tests/test_b3_error_mapping.py:142-155`, `:235-240` pin it.
2. **Data integrity.** No output, commit order or staging changes. The reason is computed after the failure is decided and stored in the existing node-error write.
3. **Nothing visible to a non-admin.** On `/node-runs`, manifest `nodes[].error`, the CLI `reason` member and (P2) the `Job.log` terminal body, the only text is registry summaries, `APPROVED_ERROR_MESSAGES` text, P1's fixed fallback prose and closed tokens. Seeded markers in `.message`, `str(exc)`, args and legacy rows are absent in the named tests.

**Named limits:** CLI verbs other than `run`; the CLI `error` field (F31); failures before the engine is entered; Platform-coded failures (`job_cancelled`, `job_already_terminal`, `_QualityHardFail` codes, `post_validation_route_rejected`, `partial_publication_failure`, the `native_budget.py` codes) keep their code and get no `reason` (O3); **an uncoded engine cause (`CommitError`, `ValidatorFailedError`) under `PartialPublicationError` yields the wrapper's code `partial_publication_failure` and no reason** on the streaming route, because the walk selects the wrapper (selecting by `registered_families()` in the walk would change `selected`, which V1-1 forbids); failure tails that do not reach `v2_orchestrator.py:274-381` carry no reason (C0 lists them; every node they write still goes through `node_error`).

**Non-goals:** a retry executor; new exit integers; progress-event wiring; attempt-level reason history; F31's alias and safe-event work; frontend changes.

## 4. Observable behavior, failure modes and acceptance tests (before implementation)

Red-first against current main, driving the real boundary: CLI tests through `CliRunner` over `decoy run --json` with the engine entry patched to raise; Platform tests through `run_v2_pipeline_job`, read back via the HTTP route and the manifest assembler. `MARKER` is a unique string; every absence check has a positive control on the same payload.

### CLI (repository `decoy`)

| Test (`tests/e2e/test_run_reason_envelope.py::`) | Behavior | Fail-before evidence |
|---|---|---|
| C-T1 `test_capacity_failure_carries_registry_reason` | `ExecutionError(code="out_of_core_insufficient_memory", message=MARKER)`: exit 5; `reason` identity `runtime.out_of_core_insufficient_memory`, outcome `execution`, retry `environment_repair`, `registry_version` `<pinned>`; `error_kind`/`code` still present (`test_run_capacity.py:87`); MARKER absent from `reason` | no `reason` key at `run.py:1040-1047` |
| C-T2 `test_publication_failure_is_distinguishable` | engine `CommitError`: exit 3; `reason` `decoy.publication_failed`, outcome `publication` | as above |
| C-T3 `test_config_failure_reason_says_configuration_repair` | `ConfigError(MARKER)`: exit 1; outcome `configuration`, retry `configuration_repair` | as above |
| C-T4 `test_unknown_exception_gets_fallback_reason` | `ValueError(MARKER)` with `.code = MARKER`: exit 3; `reason` `decoy.unexpected_error`, retry `unknown`; MARKER absent from `reason` | as above |
| C-T5 `test_cli_owned_refusal_keeps_error_kind_without_reason` | ImportError translated by `translate_missing_extra`: `error_kind == "missing_extra"`, no `reason` | red if `unexpected_error` is attached |
| C-T6 `test_rewritten_ner_failure_keeps_engine_reason` | engine `NerUnavailableError` rewritten by `translate_ner_unavailable` into `MissingExtraError` (`run.py:886-888`, CLI `extras.py:171`): `error_kind == "missing_extra"` **and** `reason` is the engine record of the original `NerUnavailableError` (provider namespace, `<pinned>` code) | mutation: compute from the rewritten `exc` (no reason, red) |
| C-T7 `test_config_validation_error_is_a_refusal` | `_ConfigValidationError` (raised `run.py:501`, `:599`): exit 1, no `reason` | red if the refusal set is hand-kept and misses it |
| C-T8 `test_refusal_set_is_derived_from_exit_mapping` | every non-engine class in `_usage_types()` is in `_cli_refusal_types()`, every engine class (MRO hits `registered_families()`) is not; `_MaskSecretUsageError`, `_ConfigValidationError`, `MissingExtraError`, `NativeGateError` named as positive controls | new mechanism |

### Platform (repository `decoy-platform`)

| Test | Behavior | Fail-before evidence |
|---|---|---|
| P-T1 `tests/test_f29_node_error_reason.py::test_engine_failure_node_error_has_reason_and_fixed_message` | `run_pipeline` raises `StatisticalSpecError(code=plan.STATISTICAL_SNAPSHOT_UNREADABLE, message=MARKER)`; owner `GET /node-runs`: `error.reason` is the registry record, `error.message == reason.summary`; MARKER absent from the body and the manifest | `_partial_publication.py:201` copies `.message` |
| P-T2 `...::test_wrapped_cancellation_still_lands_cancelled` | `JobCancelledError` raised inside a streamed read, wrapped in `PartialPublicationError` (`v2_stream_coordinator.py:235`): job `cancelled`, node `code == "job_cancelled"`, `reason` null, `message` the fallback prose | mutation: select by `registered_families()` in the walk (job lands `failed`, red) |
| P-T3 `...::test_wrapped_engine_cause_reason_and_location` | `PartialPublicationError` over an engine coded cause: `reason` from the cause, `where == "table=<t>"` | locks the walk and `where` |
| P-T4 `...::test_platform_coded_failure_keeps_code_and_fixed_message[approved, not_approved]` | `out_of_core_budget_unresolved`: `message == APPROVED_ERROR_MESSAGES[code]`; `job_already_terminal`: fallback prose; both `reason` null, code unchanged | today `message` is the exception's `.message` |
| P-T5 `...::test_stored_reason_reread_through_from_payload` | stored `reason` with a forged `retry_class`, and one with an unknown same-major code: both readers show the registry retry class and `decoy.unknown_reason_from_future` | `NodeError` (`schema.py:328`) has no `reason`; no reader re-validates |
| P-T6 `tests/test_job_node_error.py::test_legacy_row_withheld_at_read[json, free_text, extra_key]` | rows stored as at `d8908f42` with MARKER (a `{code, message, where}` JSON row, a non-JSON raw row, a row with an extra key), both readers: MARKER absent; `message` is the fixed rule's text; unknown keys dropped; positive control: `code` and `where` kept | `assembly.py:1984` copies raw text; the route returns `row.error` as stored |
| P-T7 `tests/test_job_node_error.py::test_vault_node_has_no_marker` | vault write raises `OSError(MARKER)`: stored `JobNodeRun.error`, `/node-runs` and manifest carry `code == "vault_write_failed"` and the fallback prose, no MARKER | `v2_vault.py:97` stores `str(exc)` |
| P-T8 `tests/test_obs3_leak.py::test_job_log_engine_failure_shows_registry_summary` (P2) | the P-T1 failure: terminal line body is the registry summary, tail `error_class=plan.statistical_snapshot_unreadable`, no MARKER | `error_projection.py:136-146` renders `internal error: StatisticalSpecError` |
| P-T9 `...::test_tampered_reason_on_trusted_failure_cannot_inject_text` (P2) | `TrustedFailure.reason` not a `ReasonRecord`: generic rendering | mirrors the `.code` tamper test |

Sentry: `tests/sentry/test_node_error.py` (`node-error`, AST, seeded fixtures): every `JobNodeRun(...)` with `error=` in `api/` passes a `node_error(...)` call; `_partial_publication.py` and `job_node_error.py` read no `.message`. Baseline empty after P1.

**Pinned assertions that encode the leak.** `tests/test_b3_error_mapping.py:114` (`node["message"].startswith("'t' chunk 1:")`), `:185` and `:247` (`details.message` equals the exception's `.message`) assert the `.message` copy rule 3 removes. P1 rewrites exactly these three assertions to the fixed-rule expectation, names them in the PR, and changes no other existing assertion; the status and `where` assertions in the same file stay untouched.

Mutations to show red before push: C: compute the reason from the rewritten `exc` (C-T6); hand-list the refusals without `_ConfigValidationError` (C-T7); attach a reason to refusals (C-T5). P: restore `getattr(selected, "message")` (P-T1); select by `registered_families()` in the walk (P-T2); serve the stored `message` in `project_node_error` (P-T6); keep `str(exc)` in the vault node (P-T7, sentry); skip `from_payload` on read (P-T5); drop the summary branch in `project_failure` (P-T8).

## 5. Slices

| Slice | Repo | Production files | Est. changed lines (incl. tests) | Depends on | Gate |
|---|---|---|---|---|---|
| **C0** grounding record | engine (docs) | 0 (`docs/records/2026-11-0x-f29-c0-consumer-grounding.md`) | ~120 | none | failure-tail census below, file:line on its merge base |
| **C1** CLI `run` reason member | `decoy` | 2: `src/decoy/cli/run.py`, `docs/cli-reference.md` (plus `CHANGELOG.md`) | ~360 | C0 | C-T1..C-T8 red then green on `uv.lock`; `test_run_capacity.py`, `test_run_native.py` green; three mutations red |
| **P1** error-node builder, reader, reason | `decoy-platform` | 8: `api/jobs/job_node_error.py` (new), `_partial_publication.py`, `v2_orchestrator.py`, `v2_node_runs.py`, `v2_vault.py`, `routes_runtime_facts.py`, `api/evidence/schema.py`, `api/evidence/assembly.py` | ~700 (~260 production, ~440 tests and sentry) | C0; Platform repin to 0.17.0+ | P-T1..P-T7 and `test_node_error.py` red then green on the Platform lock; `tests/test_b3_error_mapping.py` green with only the three named assertions changed; evidence tests green; golden fixtures regenerated only where `nodes[].error` appears, diff listed |
| **F31 P8** (owned by F31, shrunk) | `decoy-platform` | per F31 revision 6 | F31's estimate less what P1 took | **P1**, F31 ER and repin | F31's gate |
| **P2** `Job.log` registry summary | `decoy-platform` | 2: `api/persistence/error_projection.py`, `api/jobs/v2_orchestrator.py` | ~250 | P1, **F31 P2b** | P-T8, P-T9 red then green; `test_obs3_leak.py` green |
| **E7** docs close-out | engine | 0 (`docs/reasons.md` "Consumers"; F29 original plan status line; Platform `docs/ROADMAP.md` F29 row) | ~60 | C1, P2 | docs only |

Order: C0; then C1 and P1 in parallel (different repositories, one writer each); then F31 P8 on top of P1; P2 after F31 P2b lands (P2 either amends F31's terminal event to carry the engine reason in place of `platform.job_failed`, or, if F31 P2b already renders the terminal line from `TrustedFailure`, adds the `reason` field there); E7 last. Every slice is within 10 production files and 800 lines.

**C0 scope.** List each Platform path that writes a failed V2 job and whether it reaches `v2_orchestrator.py:274` (reason carried) or not (named limit): `_run_v2_pipeline_streaming` (`v2_runner.py:273`), `v2_sequential.py`, `v2_full_frame.py`, host-safety helpers (`queue_worker.py:419`, `:434`; `scheduler_claim_loop.py:173`). Confirm the CLI has one failure `emit_json` site in `run.py` and `JobDetail.tsx` reads `error.message` as text.

### 5.1 Slice C1 design (CLI)

In `except Exception as exc` (`run.py:861`), bind `engine_exc = exc` before the `translate_missing_extra` and `translate_ner_unavailable` rewrites (`:877-888`). Hoist the inline exit-1 tuple (`:944-1000`) into `_usage_types()` (same members, same defensive imports) and use it in the exit mapping unchanged. Derive `_cli_refusal_types()` once from it: the members with no class in their MRO in `registered_families()`; nothing is hand-listed, so `_ConfigValidationError` and `_MaskSecretUsageError` are covered by construction. Rule, stated once: **attach `payload["reason"] = reason_from_exception(engine_exc).to_payload()` unless `exc` is a CLI refusal and `engine_exc` is not an engine family.** So a rewritten NER failure (engine `engine_exc`, `MissingExtraError` `exc`) carries the engine reason and keeps `error_kind: missing_extra` (C-T6); a missing extra from a plain `ImportError` and every CLI-owned usage error carry none (C-T5, C-T7). `docs/cli-reference.md` documents `reason` as additive and states that `reason.outcome` distinguishes `execution`, `publication`, `verification` and `configuration`. Human mode: unchanged.

### 5.2 Slices P1 and P2 design (Platform)

**P1, `api/jobs/job_node_error.py`.**
- **The message rule, stated once** (`node_message(code, reason, job_id)`): `reason.summary` when an engine reason exists; else `APPROVED_ERROR_MESSAGES[code]` when approved; else the fallback prose `"The job failed; see the server log (job_id=<id>)."` (no class name; F31 P8 swaps it for the `platform.job_failed` prose). No caller passes a message.
- `node_error(code, *, job_id, where=None, reason=None) -> str`: JSON `{"code", "message", "where", "reason"}`; `reason` serialized only when `type(reason) is ReasonRecord`.
- `project_node_error(raw, *, job_id) -> dict | None`: re-projects a stored row on read. Keeps `code` if a closed token (`^[a-z][a-z0-9_.]{0,63}$`, else `node.runtime_error`), `where` if a string, `reason` through `ReasonRecord.from_payload(...).to_payload()` (null when absent); drops every other key; re-renders `message` with the rule above, never the stored text. A non-JSON row becomes `node.runtime_error` with the fallback prose. Legacy rows are withheld at read, never rewritten.

**P1, callers.** `_failure_details` keeps its walk byte for byte; `FailureDetails` loses `message` and gains `reason`, set to `reason_from_exception(selected)` when `selected`'s MRO hits `registered_families()`, or when `code` is `None` (nothing coded, the `v2_runner_unexpected_error` case, giving `decoy.unexpected_error`); else `None` (a Platform-coded failure, including the wrapper's own `partial_publication_failure`). `report` returns the code; the `.message` read at `:201` is deleted. `_error_node_run` and the vault node (`v2_vault.py:97`, `code="vault_write_failed"`) call `node_error`. The node-runs route sets `item.error = json.dumps(project_node_error(r.error, job_id=...))`; `_hydrate_nodes` uses the same reader. `NodeError` gains `reason: dict[str, Any] | None = None`. The `node-error` ratchet ships here.

**P2.** `TrustedFailure` gains `reason: ReasonRecord | None`; `project_failure` checks `type(failure.reason) is ReasonRecord` and uses `reason.summary` as the body and `error_class=<namespace>.<code>` as the tail token; approved codes keep precedence. The orchestrator passes `details.reason`. Built on F31 P2b's `Job.log` writer, not on today's `JobLogger.error` path.

### 5.3 What was cut, and why

Revision 2 cuts, to keep the plan flat: the foreign-`.code` trust test (old P-T2) and its mechanism (V2-LOG, section 8: third-party `.code` values are fixed strings or ints, not values); the separate "fields" and "output surfaces" lists (folded into sections 4 and 10); the type-name fallback V2-LOG row (the fallback prose no longer names a class).

- **Progress-event consumer wiring:** engine fields exist and are tested; no consumer is broken without them; F30 and F23 own the surface (Platform `docs/ROADMAP.md:377`). O1.
- **New exit integers:** `reason.outcome` carries the category; the integers are frozen (`exit_codes.py`). O2.
- **Reasons on other CLI verbs; a human-mode reason line; registering Platform codes in the engine (O3); retry policy from `retry_class`.**

## 6. Design notes

One fact, one home: identity, outcome and retry class live in the engine registry; the CLI and Platform carry `to_payload()` and re-read stored copies with `from_payload()`. One message rule and one error-node reader, in `job_node_error.py`, shared with F31. Established pattern: AIP-193 identity carried as an RFC 9457-style additive member; stored rows re-projected on read as the Platform already does for exports.

## 7. Owner questions (each with a recommended default)

- **O1. Progress events in V1 consumers?** Default **no**.
- **O2. New exit integers?** Default **no**. CLI refusals (the non-engine members of the exit-1 tuple) keep `error_kind` and get no `reason`, except a rewritten engine failure, which keeps its engine reason.
- **O3. Platform-owned codes in the engine registry?** Default **no** for V1: they keep their code, the approved or fallback text, and `reason` null.
- **O4. Manifest schema version for `nodes[].error.reason`?** Default **no bump** (section 10).
- **O5. Who owns the error-node builder and the `:201` read?** **Decided by the owner (2026-11-02): F29 P1**, first; F31 revision 6 shrinks P8 to match (section 1.5).

## 8. V2-LOG (seen, not V1, no work planned)

- `_failure_details` trusts any string `.code` (`_partial_publication.py:169-171`); a third-party `.code` reaches the stored `code`. Checked values are fixed strings or ints, not data. Class 6.
- CLI `run.py:1005-1007` `error` carries `str(exc)[:500]` to the operator's own terminal and CI; F31. Class 1.
- CLI `run.py:1085` `print_exception()` under `--verbose`; F31. Class 1.
- Engine `.message` f-strings embedding `{exc}` (`generation/statistical/_spec.py:160`, `unmask.py:126`): in-process text once no V1 surface copies it; F31 R7. Class 1.
- Registry summaries are terse humanised codes; a patch-version editorial pass. Class 10.
- Platform pins `0.16.0`, CLI `0.17.0`; repin in flight. Class 4.

## 9. What this plan did not verify

- The dev-rules files and routing table (absent from this container).
- `plan-lint` run from the Platform `handoffs/queue` copy (`docs/handoffs/tools/plan-lint`); it checks shape, not anchors.
- No CLI, Platform or engine test was run; no lock venv was installed for this revision. Anchors are read from source at the baselines. That `registered_families()` stays exported past 0.17.0 was not checked on later tags.
- The Platform failure tails outside `v2_orchestrator.py:274-381` (C0), and how `JobDetail.tsx` renders `error` (C0).
- Whether F31 P2b's terminal line already reads `TrustedFailure`; P2's exact shape is settled on top of P2b.

## 10. Frozen contracts touched

Pre-GA (`RELEASE_PHASE`, compatibility contract §1); every change is additive except the node-error `message` value, which was never a contract (it was exception text).

| Frozen surface | Touched? | Answer |
|---|---|---|
| Engine reason registry identities (`docs/reasons.md`, `tests/unit/test_reason_release_pin.py`) | Read only | Nothing added or re-meant; consumers carry `to_payload()` and re-read with `from_payload()`. |
| CLI exit-code contract (compatibility contract §3.4; `exit_codes.py`) | No | Exit mapping unchanged; `_usage_types()` is a hoist with the same members (C-T8; `test_run_capacity.py`, `test_run_native.py` pin it). |
| CLI `run --json` failure envelope (`run.py:1040-1047`) | Additive | Optional `reason`; no key renamed or removed; `error` stays (F31). |
| Platform node-run error (`JobNodeRun.error` JSON, served as `JobNodeRunOut.error: str`) | Additive key; `message` source changes | Gains `reason`; `code` and `where` unchanged; `message` from the one rule; unknown stored keys dropped at read (P-T6). |
| Evidence manifest `nodes[].error` (`NodeError`, `schema.py:328`; `SCHEMA_VERSION = "r3.1"`) | Additive optional field | `reason: dict | None`, re-validated on read (P-T5). |
| Engine progress-event schema | No | Wiring cut (O1). |

**Schema version (O4): no bump.** Compatibility contract §5 ("Add an optional field to an existing artifact: no version bump") and §4.2; the manifest keeps `r3.1` per `schema.py:626`. Caveat: manifest models are `_Strict` (`schema.py:57`), so a reader built from the pre-change `NodeError` rejects a manifest carrying `reason`, including a Platform rolled back across P1 reading a newer manifest. Pre-GA, reader and writer ship together and no external reader exists. **Re-ask at the GA flip:** a rollback reader or external reader of a `_Strict` model then needs a tolerant reader or a version bump for each later field.

## 11. Pre-mortem

| How it failed | Early signal | Guard |
|---|---|---|
| A reason change moved job status: cancelled jobs land `failed` | `cancelled` count drops after P1 | walk unchanged (section 3 item 1); P-T2 and `test_b3_error_mapping.py:142-155` |
| F29 and F31 both build an error-node writer, or F31's reader drops `reason` | two builders; `reason` missing at read | P1 owns `job_node_error.py`; `project_node_error` keeps `reason` (P-T5); F31 rev6 amended (section 1.5) |
| Some failures carry `reason`, others not (other tails, uncoded cause under the wrapper) | engine-entered failures with `reason` null | C0 census; named limits in section 3; E7 documents them |
| Someone restores the `.message` copy to debug | PR touching `_failure_details` | P-T1, the `node-error` sentry, fallback prose points at the server log |
| CLI scripts keep parsing `error` prose | integrations quoting `error` | `reason` documented as the machine field (C1) |
| The no-bump decision breaks a `_Strict` reader after GA | older reader raises on newer manifest | section 10 caveat, re-asked at the GA flip |

## 12. Defect class table

| # | Class | Applies | Structure that closes it | Test that proves it | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: error and vault nodes, legacy rows | one writer `node_error` and one reader `project_node_error`; `message` only from the one rule; `node-error` sentry | P-T1, P-T6, P-T7, C-T1, C-T4, P-T8 markers absent | restore `getattr(selected, "message")` (P-T1); keep `str(exc)` in the vault node (P-T7) |
| 2 | Cleanup missing on an exit path | N/A: no file, lease or scratch; the reason rides the existing node-error write | none new | existing failure tests | none |
| 3 | State machine and concurrency | Yes: cancelled versus failed is chosen from `details.selected` | the chain walk is not changed; the reason is computed beside it, never selects | P-T2; `test_b3_error_mapping.py:142-155`, `:235-240` | select by `registered_families()` in the walk (P-T2 red) |
| 4 | Route parity and silent fallback | Yes: two node-error readers; tails outside the orchestrator; CLI refusals | one reader for both surfaces; C0 census; refusal set derived from the exit tuple | P-T6 on both readers; C-T5, C-T7, C-T8 | skip `project_node_error` in `assembly.py` (P-T6 red); hand-list refusals (C-T7 red) |
| 5 | Referential integrity | N/A: no rows, FKs or publication order change | none new | none | none |
| 6 | Untrusted producer | Yes: stored JSON rows and stored reasons | `project_node_error` re-projects on read; `from_payload` recomputes outcome and retry; exact `type(...) is ReasonRecord` at write and in `project_failure`. Engine class trust is by MRO (`_first_in_mro` accepts subclasses of registered classes); a hostile in-process subclass is V2 | P-T5, P-T6, P-T9 | serve the stored `message` (P-T6); skip `from_payload` (P-T5) |
| 7 | Check then use on a path | N/A: no path is checked or used | none new | none | none |
| 8 | Weak tests and overclaimed evidence | Yes | positive control beside every absence check; the three leak-pinning assertions named, not silently edited; `<pinned>` instead of literal versions; section 9 lists what was not run | C-T1..C-T8, P-T1..P-T9 red on each repo's lock first | each section 4 mutation once before push |
| 9 | Behavior-preserving refactor drift | Yes: chain walk, CLI exit tuple hoist | walk unchanged; `_usage_types()` same members | P-T3, C-T8, `test_run_capacity.py`, `test_run_native.py` | drop a member from `_usage_types()` (C-T8 red) |
| 10 | Frozen contract drift | Yes: CLI envelope, node error, `NodeError` | additive `reason`; exits and keys unchanged (section 10) | C-T1 existing keys, P-T6 kept keys | drop `code` from the capacity envelope (C-T1 red) |

## 13. Enumerations (from the code at the baselines)

### 13.1 Exit paths

- CLI: `except Exception` at `run.py:861`, single emit at `:1040` (gains `reason`); refusal arms `:1048-1058` keep `error_kind`; enforced post-validation `status: ok`, exit 4 (`:1134-1172`), unchanged.
- Platform: orchestrator failure tail `v2_orchestrator.py:286-303`, node write `v2_node_runs.py:434`; vault failure `v2_vault.py:90-98`.

### 13.2 Routes

- Platform unified V2 through `v2_orchestrator.py:274-381`: reason carried.
- Streaming (`v2_runner.py:273`), sequential, full-frame and host-safety tails: C0, named limit for `reason`; any error node they write passes `node_error` (the sentry covers every `JobNodeRun(error=...)`).
- CLI `decoy run` only; other verbs out of scope.

### 13.3 FK shapes

- N/A: failure reporting only.

### 13.4 Fields

- `ReasonRecord.to_payload()` (`docs/reasons.md`).
- `FailureDetails`: `selected`, `code`, `where`, `reason` (`message` removed).
- Node error and `NodeError`: `code`, `message`, `where`, `reason`.
- `TrustedFailure` plus `reason` (P2).
- CLI envelope plus `reason`.

### 13.5 Output surfaces

- CLI `--json` failure envelope (additive).
- CLI exit code (unchanged); CLI human stderr (unchanged, F31).
- `/node-runs` and manifest `nodes[].error` (one reader).
- `Job.log` terminal line (P2).
- server diagnostic `_log.exception` (`v2_orchestrator.py:285`, admin-only, unchanged).
- Progress events: cut (O1).

## 14. Revision 2 change map

| Finding | Class | Change | Slice | New or changed test |
|---|---|---|---|---|
| V1-1 BLOCKER: `.code` rule moves cancelled jobs to `failed` | 3 | `.code` selection change dropped; walk kept; reason computed beside it; section 3 item 1 corrected; foreign `.code` logged V2 | P1 | P-T2 `test_wrapped_cancellation_still_lands_cancelled`; P-T4 `[not_approved]` cell |
| V1-2 HIGH: P1 duplicates F31 P8 | 9, 4 | P1 builds `job_node_error.py` (`node_error(..., reason=)`, `project_node_error` keeping `reason`), the one message rule, the `node-error` sentry; F31 P8 shrinks; F31 rev6 to be amended; O5 decided | P1 | P-T6 `test_legacy_row_withheld_at_read[...]` (replaces old P-T6); `tests/sentry/test_node_error.py` |
| V1-3 HIGH: vault `str(exc)`, legacy raw rows | 1 | vault node through `node_error`; both readers through `project_node_error` | P1 | P-T7 `test_vault_node_has_no_marker`; P-T6 `free_text` cell |
| M-4: C-T6 contradicts C-T5 | 8 | rewritten NER failure keeps the engine reason via `engine_exc`; one rule in 5.1 | C1 | C-T6 `test_rewritten_ner_failure_keeps_engine_reason` |
| M-5: hand-kept refusal list | 4 | `_cli_refusal_types()` derived from `_usage_types()` | C1 | C-T7 `test_config_validation_error_is_a_refusal`; C-T8 `test_refusal_set_is_derived_from_exit_mapping` |
| P2 races F31 P2b | 9 | P2 ordered after F31 P2b | P2 | P-T8, P-T9 renumbered |
| Pinned 0.17.0 registry 1.1.0 | 8 | baselines and 1.1 corrected; `<pinned>` assertions | all | C-T1..C-T6 |
| Uncoded engine cause under the wrapper | 4 | named limit, section 3 | P1 | none (limit) |
| `error_class=` token | 10 | `<namespace>.<code>` | P2 | P-T8 |
| Class 6 wording; `_Strict` rollback reader | 6, 10 | section 12 row 6; section 10 re-ask at GA | n/a | none |
