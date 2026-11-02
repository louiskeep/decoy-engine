Status: handoff

Summary: F29's engine half is built; what V1 still owes is two consumers. The CLI `decoy run --json` failure envelope and the Platform failed-job node error should carry the engine's registered reason (identity, outcome, retry class), and the Platform error nodes (execution and vault) should stop carrying exception text onto an owner-visible surface. Progress-event consumer wiring is cut from V1.

Revision 3 (2026-11-02, from Codex `gates/v/f29c.txt` and the Dennis review of `fa79e45f`): seven V1 fixes, chiefly the node-error contract F31 revision 7 references (section 1.6). Change map in section 14.

Baselines (`git rev-parse`, 2026-11-02): engine `v0.17.0`, the version both consumers build against, registry **1.1.0** (`git show v0.17.0:src/decoy_engine/reasons/_registry.py:24`; `reason_adapter.py` read at this tag); Platform `origin/main` `d8908f42`; CLI `louiskeep/decoy` `a5d36b46`; F31 plan `plan/f31-safe-output` revision 6 `c2b16de0`.

Risk: **R2**, plan only. The build slices change a public CLI JSON envelope, the Platform node-error shape and an evidence-manifest model, all pre-GA. This document grants no build approval. Review order: Dennis, then Codex.

Rules consulted: 00-universal, development-loop, risk-and-exceptions, scope-discipline, reuse-first; README-selected rulebooks: testing, api-and-compatibility, security, architecture, documentation. Read: engine `CLAUDE.md`; Platform `handoffs/queue` `docs/handoffs/V1-BAR.md`, `DEFECT-CATALOG.md`, `tools/plan-lint`; F31 revision 6 sections 3.8, 6, 11 and the P8 row.

# F29 remainder: consumer reasons for the CLI and Platform (FRAME + PLAN)

## 1. FRAME

### 1.1 What is already done (verified)

Done in the engine (MASTER §F29): the versioned registry (`src/decoy_engine/reasons/_registry.py:24`, `docs/reasons.md`), retry classes and outcomes, `reason_from_exception` (`reason_adapter.py:144-164` at `v0.17.0`: `decoy.unexpected_error` outside `registered_families()`, never reads message text, never raises), `ReasonRecord.to_payload` / `from_payload` (never raises, recomputes outcome and retry) / `.summary`, and the progress-event fields. Not done: the CLI (`run.py:861-1059`) and the Platform, which imports nothing from `decoy_engine.reasons` and pins engine `0.16.0` (`pyproject.toml:36`). Assertions read `registry_version` and identities as `<pinned>`, never a literal version.

### 1.2 What F29 still owes V1 (the problem, re-derived)

**P-CLI.** The `decoy run --json` failure envelope (`run.py:1040-1047`) is `{command, status, config, mode, error}`, `error = str(exc)[:500]`; a machine code exists only for capacity, native gate and missing extra (`:1048-1058`). CI cannot tell execution, publication and verification failures apart (MASTER requires it).

**P-PLAT-1. Error nodes carry exception text to the job owner.** (a) `_failure_details` copies `getattr(selected, "message", None)` (`api/jobs/_partial_publication.py:201`) into the node (`v2_node_runs.py:434`); engine `.message` embeds exception text (`generation/statistical/_spec.py:160` at `v0.17.0`). (b) The vault node stores `str(exc)` (`api/jobs/v2_vault.py:97`). (c) `_hydrate_nodes` copies a non-JSON row's raw text (`api/evidence/assembly.py:1984`). All reach the non-admin owner via `GET /api/jobs/{id}/node-runs` (`routes_runtime_facts.py:70-115`; `JobNodeRunOut.error: str | None`, `schemas.py:430`) and the manifest. **V1 bar rule 3**, class 1.

**P-PLAT-2.** `Job.log` renders only `APPROVED_ERROR_MESSAGES` codes (`error_projection.py:42-68`); an engine failure renders `internal error: <TypeName>`.

### 1.3 Definition of done

1. `decoy run --json` failure envelope carries `reason` (`ReasonRecord.to_payload()`) for engine-entered failures, computed from the exception as raised, before any CLI rewrite; exit integers unchanged. C-T1 to C-T8 pass.
2. Every Platform error node (execution, vault) is written and read through the section 1.6 contract on every reader that builds from `JobNodeRun` rows; no exception text is stored or served; a node without an error stays `null`. P-T1 to P-T7 and P-T10 to P-T12 pass; `node-error` sentry (section 1.6 item 7, seeded fixtures) at 0.
3. `Job.log`'s terminal failure line for an engine-reasoned failure shows the registry summary and `error_class=<namespace>.<code>` (P2, after F31 P2b). P-T8, P-T9 pass. `docs/reasons.md` gains a "Consumers" section naming the surfaces and limits (E7).

### 1.4 Reuse inventory

Engine (public in 0.17.0): `reason_from_exception`, `reason_adapter.registered_families` (`reason_adapter.py:115`), `ReasonRecord.to_payload` / `from_payload` / `.summary`. CLI: `emit_json` and the single failure emit site (`run.py:1040`), the exit-1 tuple (`run.py:944-1000`). Platform: `_failure_details` chain walk (kept as is), `APPROVED_ERROR_MESSAGES`, `TrustedFailure` / `project_failure`, the persisted-exports re-projection pattern (`routes_runtime_facts.py:105-113`), the module-size census pattern for ratchets (`tests/sentry/test_module_size.py`). One new module, `api/jobs/job_node_error.py` (section 1.6), shared with F31.

### 1.5 Boundaries

- **Engine:** no production change. A code the registry lacks is an engine registry addition outside this plan (O3). **Dependency:** the Platform slices need the Platform repin to engine `0.17.0` or later (handoff `p0-platform-repin-engine-0-17-0`). The CLI pins 0.17.0 (`pyproject.toml:103`).
- **F31:** the node-error module is F29 P1's (owner ruling 2026-11-02); the interface and what F31 revision 7 drops are section 1.6. F31 also owns the CLI `error` text (`run.py:1005`), CLI `print_exception`, and every other Platform except-bound copy. P2 waits for F31 P2b.
- **decoy-web:** `JobNodeRunOut.error` stays a JSON string with the same keys plus `reason`, or `null`; the frontend is not touched. C0 confirms `web/src/pages/JobDetail.tsx` reads `message` as text only.

### 1.6 Interface contract F29 publishes (F31 rev7 references this)

Module `api/jobs/job_node_error.py`, created by F29 P1. Public names and signatures are frozen for F31:

1. `node_error(code: str, *, job_id: int, where: str | None = None, reason: ReasonRecord | None = None) -> str`. Returns a JSON object string with exactly the keys `code`, `message`, `where`, `reason`. `reason` is `reason.to_payload()` when `type(reason) is ReasonRecord`, else `null`. `message` comes only from `node_message`. No caller passes a message. Never raises on content.
2. `node_message(code: str, reason: ReasonRecord | None, *, job_id: int) -> str`, the one message rule, in this order: `APPROVED_ERROR_MESSAGES[code]` when approved; else `reason.summary` when `reason` is not `None`; else the fallback prose `"The job failed; see the server log (job_id=<id>)."` (see V1-3 for the order).
3. `project_node_error(raw: str | None, *, job_id: int) -> dict[str, Any] | None`. Returns `None` when `raw` is `None` or empty (a node without an error). Otherwise a dict with exactly `code` (closed token `^[a-z][a-z0-9_.]{0,63}$`, else `node.runtime_error`), `message` (re-rendered by `node_message`, never the stored text), `where` (string or `None`), `reason` (`ReasonRecord.from_payload(stored).to_payload()`, or `None` when absent). Every other stored key is dropped. A non-JSON row becomes `node.runtime_error` with the fallback prose.
4. `failure_reason(selected: BaseException | None, code: str | None) -> ReasonRecord | None` (V1-3, V1-5): the only place the Platform computes a reason for an error node.
5. `FailureDetails` (`api/jobs/_partial_publication.py`) has fields `selected`, `code`, `where`, `reason`; no `message`. The `.message` read at `_partial_publication.py:201` is deleted.
6. `NodeError` (`api/evidence/schema.py:328`) gains `reason: dict[str, Any] | None = None`.
7. Sentry `tests/sentry/test_node_error.py` (`node-error`): every `JobNodeRun(...)` with `error=` in `api/` passes a `node_error(...)` call; `_partial_publication.py` and `job_node_error.py` read no `.message`. Baseline 0 after P1.
8. Tests owned by F29 P1 in `tests/test_job_node_error.py`: `test_vault_node_has_no_marker`, `test_legacy_row_withheld_at_read[...]`, `test_node_error_contract_shape`. F31 extends these; it does not recreate them.
9. Extension point for F31 (RECOMMENDED (go with recommended applies)): F31 P8 may add one keyword-only parameter `error_type: SafeErrorType | None = None` to `node_error`, one `error_type` key to the output and to `project_node_error` (via `SafeErrorType.from_label`), one `error_type` field to `NodeError`, and may replace the fallback prose string (branch 3 of `node_message`) with the `platform.job_failed` prose. Nothing else in items 1 to 8 changes.

What F31 rev7 must drop or change:

1. Its `node_error(code, exc, *, where=None)` signature; it uses item 1 plus the item 9 keyword.
2. Creating `job_node_error.py`; P8 edits the existing module.
3. Its `project_node_error(raw)` signature and its unknown-key rule as applied to `reason`; it keeps F29's reader and `reason` round trip.
4. "`message` is always the `platform.job_failed` prose"; only the fallback branch changes. Approved text and `reason.summary` stay.
5. `node-error` baseline 3 (2 writes, 1 `.message` read) and shipping the sentry; the sentry is at 0 from F29 P1.
6. The `FailureDetails` `message` removal and the `:201` deletion (done by P1).
7. P8 ownership of `_partial_publication.py`, `v2_orchestrator.py`, `routes_runtime_facts.py`, `evidence/assembly.py` (P1 owns those edits); P8's file list and its 480-line estimate shrink to what `error_type` needs.
8. Recreating `test_vault_node_has_no_marker` and `test_legacy_row_withheld_at_read`; T-P8 extends them with `error_type` assertions.
9. Any `exc-copy` baseline row for `v2_vault.py:97` or `_partial_publication.py:201`; P1 removes both copies.
10. Note for the F31 worker (Not verified in this handoff): F31 rev6's P8 file list omits `api/evidence/schema.py`, but `NodeError` is `_Strict` (`schema.py:57`), so an `error_type` key in `nodes[].error` needs the field.

Anchors in F31 revision 6 (`c2b16de0`, `docs/plans/2026-10-30-f31-shared-safe-output-boundary.md`): section 3.8 is line 124; the `node-error` baseline 3 is lines 26 and 228; the P8 row (7 files, 480 lines, no `schema.py`) is line 300; T-P8 is line 205.

## 2. Established solutions, searched first

1. **AIP-193 `ErrorInfo`** ([aip.dev/193](https://AIP.dev/193)): a stable `(reason, domain)` identity separate from the message; the engine `(namespace, code)` follows it. **Followed.** `RetryInfo` delays not adopted.
2. **RFC 9457** ([rfc9457](https://www.rfc-editor.org/rfc/rfc9457.pdf)): unknown extension members are ignored, so `reason` is additive. **Followed.**
3. **In-codebase:** the Platform re-projects stored exports on read (`routes_runtime_facts.py:105-113`) and withholds exception text in `TrustedFailure`. Reused; no new sanitizer.

## 3. Guarantee, pinned to the V1 bar

1. **Function.** For a `decoy run --json` failure the envelope carries `reason` equal to `reason_from_exception(engine_exc)` under the pinned engine, where `engine_exc` is the exception as raised; CLI-owned refusals carry none. Exit integers are unchanged. On the Platform, **job status selection is unchanged**: `_failure_details` keeps today's walk (first non-empty string `.code`, `_partial_publication.py:186-200`), so `details.selected` and the cancelled-versus-failed decision (`v2_orchestrator.py:300-303`) are identical. Reason computation cannot stop the failure tail: `failure_reason` swallows its own errors (P-T12). A successful node's `error` stays `null` (P-T11).
2. **Data integrity.** No output, commit order or staging changes. The reason is computed after the failure is decided and stored in the existing node-error write.
3. **Nothing visible to a non-admin.** On `/node-runs`, manifest `nodes[].error` built from `JobNodeRun` rows, the CLI `reason` member and (P2) the `Job.log` terminal body, the only text is registry summaries, `APPROVED_ERROR_MESSAGES` text, the fixed fallback prose and closed tokens.

**Named limits:** CLI verbs other than `run`; the CLI `error` field (F31); failures before the engine is entered; Platform-coded failures (`job_cancelled`, `job_already_terminal`, `_QualityHardFail` codes, `post_validation_route_rejected`, `partial_publication_failure`, the `native_budget.py` codes) and Platform codes raised on an engine class (`out_of_core_budget_unresolved`, `v2_out_of_core.py:318`) keep their code and get no `reason` (O3); an uncoded engine cause (`CommitError`, `ValidatorFailedError`) under `PartialPublicationError` yields the wrapper's code and no reason, because the walk selects the wrapper; failure tails that do not reach `v2_orchestrator.py:274-381` carry no reason (C0 lists them; every node they write still goes through `node_error`). **Stored manifests:** `GET /jobs/{id}/evidence` returns `Job.evidence_manifest` as stored (`routes_evidence.py:164-166`); a manifest persisted before P1 keeps its copied `nodes[].error.message` and is not re-projected (re-projection would break `manifest_hash`). Pre-release hard delete applies: no customer data exists, no migration. **Non-goals:** a retry executor; new exit integers; progress-event wiring; attempt-level reason history; F31's alias and safe-event work; frontend changes.

## 4. Observable behavior, failure modes and acceptance tests (before implementation)

Red-first on each repository's lock, driving the real boundary: CLI tests through `CliRunner` over `decoy run --json` with the engine entry patched to raise; Platform tests through `run_v2_pipeline_job`, read back via the HTTP route and the manifest assembler. `MARKER` is a unique string; every absence check has a positive control on the same payload.

### CLI (repository `decoy`)

| Test (`tests/e2e/test_run_reason_envelope.py::`) | Behavior | Fail-before evidence |
|---|---|---|
| C-T1 `test_capacity_failure_carries_registry_reason` | `ExecutionError(code="out_of_core_insufficient_memory", message=MARKER)`: exit 5; `reason` identity `runtime.out_of_core_insufficient_memory`, outcome `execution`, retry `environment_repair`; `error_kind`/`code` still present (`tests/e2e/test_run_capacity.py:86`); MARKER absent from `reason` | no `reason` key at `run.py:1040-1047` |
| C-T2 `test_publication_failure_is_distinguishable` | engine `CommitError`: exit 3; `reason` `decoy.publication_failed`, outcome `publication` | as above |
| C-T3 `test_config_failure_reason_says_configuration_repair` | `ConfigError(MARKER)`: exit 1; outcome `configuration`, retry `configuration_repair` | as above |
| C-T4 `test_unknown_exception_gets_fallback_reason` | `ValueError(MARKER)` with `.code = MARKER`: exit 3; `reason` `decoy.unexpected_error`; MARKER absent from `reason` | as above |
| C-T5 `test_cli_owned_refusal_keeps_error_kind_without_reason` | ImportError translated by `translate_missing_extra`: `error_kind == "missing_extra"`, no `reason` | red if `unexpected_error` is attached |
| C-T6 `test_rewritten_ner_failure_keeps_engine_reason` | engine `NerUnavailableError` rewritten into `MissingExtraError` (`run.py:886-888`): `error_kind == "missing_extra"` **and** `reason` is the engine record of the original | mutation: compute from the rewritten `exc` |
| C-T7 `test_config_validation_error_is_a_refusal` | `_ConfigValidationError` (raised `run.py:501`, `:599`): exit 1, no `reason` | red if the refusal set is hand-kept and misses it |
| C-T8 `test_refusal_set_is_derived_from_exit_mapping` | `_usage_types()` equals, member for member, the 15 entries of the `a5d36b46` literal at `run.py:944-1000`: `PlanCompileError`, `PipelineValidationError`, `ConfigError`, `_ChunkedGenerateError`, `_ChunkedFixedWidthError`, `_ChunkedReportFlagError`, `_VaultUsageError`, `_MaskSecretUsageError`, `DuckDBSourceError`, `MissingExtraError`, `UnsupportedCloudEndpointError`, `*_ner_unavailable_types`, `_native_gate.NativeGateError`, `_ConfigValidationError`, `*_mask_secret_error_types` (starred groups expanded as the CLI does); then every non-engine member is in `_cli_refusal_types()` and every engine member (MRO hits `registered_families()`) is not | `_usage_types()` does not exist at `a5d36b46`; mutation: drop `DuckDBSourceError` (red) |

### Platform (repository `decoy-platform`)

| Test | Behavior | Fail-before evidence |
|---|---|---|
| P-T1 `tests/test_f29_node_error_reason.py::test_engine_failure_node_error_has_reason_and_fixed_message` | `StatisticalSpecError(code=plan.STATISTICAL_SNAPSHOT_UNREADABLE, message=MARKER)`; owner `GET /node-runs`: `error.reason` is the registry record, `error.message == reason.summary`; MARKER absent from body and manifest | `_partial_publication.py:201` copies `.message` |
| P-T2 `...::test_wrapped_cancellation_still_lands_cancelled` | `JobCancelledError` inside a streamed read, wrapped in `PartialPublicationError` (`v2_stream_coordinator.py:235`): job `cancelled`, node `code == "job_cancelled"`, `reason` null, fallback prose | mutation: select by `registered_families()` in the walk |
| P-T3 `...::test_wrapped_engine_cause_reason_and_location` | `PartialPublicationError` over an engine coded cause: `reason` from the cause, `where == "table=<t>"` | locks the walk and `where` |
| P-T4 `...::test_platform_coded_failure_keeps_code_and_fixed_message[approved, not_approved]` | `[approved]` raised as at `v2_out_of_core.py:318` (the real class, engine `ExecutionError`; code outside registry 1.1.0): `message == APPROVED_ERROR_MESSAGES["out_of_core_budget_unresolved"]`, `reason is None`; `[not_approved]` `job_already_terminal`: fallback prose, `reason` null; code unchanged in both | `_partial_publication.py:201` copies `.message`; mutations: summary-first order (red on message); no unregistered filter (red on `reason`) |
| P-T5 `...::test_stored_reason_reread_through_from_payload` | stored `reason` with a forged `retry_class`, and one with an unknown same-major code: both readers show the registry retry class and `decoy.unknown_reason_from_future` | `NodeError` (`schema.py:328`) has no `reason` |
| P-T6 `tests/test_job_node_error.py::test_legacy_row_withheld_at_read[json, free_text, extra_key] x [node_runs_route, finish_manifest, legacy_synthesis]` | rows stored as at `d8908f42` with MARKER, read by `/node-runs`, `update_finished_manifest` (`assembly.py:2408`) and the legacy synthesis (`routes_evidence.py:190`): MARKER absent; `message` from the rule; unknown keys dropped; positive control: `code` and `where` kept | `assembly.py:1984` copies raw text; the route returns `row.error` as stored |
| P-T7 `tests/test_job_node_error.py::test_vault_node_has_no_marker` | vault write raises `OSError(MARKER)`: stored row, `/node-runs` and manifest carry `vault_write_failed` and the fallback prose, no MARKER | `v2_vault.py:97` stores `str(exc)` |
| P-T8 `tests/test_obs3_leak.py::test_job_log_engine_failure_shows_registry_summary` (P2) | the P-T1 failure: terminal line body is the registry summary, tail `error_class=plan.statistical_snapshot_unreadable`, no MARKER | `error_projection.py:136-146` renders `internal error: StatisticalSpecError` |
| P-T9 `...::test_tampered_reason_on_trusted_failure_cannot_inject_text` (P2) | `TrustedFailure.reason` not a `ReasonRecord`: generic rendering | mirrors the `.code` tamper test |
| P-T10 `tests/test_job_node_error.py::test_node_error_contract_shape` | `inspect.signature`: `job_id`, `where`, `reason` keyword-only, no positional `exc`; output keys exactly `{code, message, where, reason}`; a row written with a `ReasonRecord` survives `project_node_error` with `reason` intact | `job_node_error.py` absent at `d8908f42`; mutation: a reader dropping keys outside `{code, message, where}` (the rev6 rule) |
| P-T11 `tests/test_job_node_error.py::test_successful_node_error_stays_null` | success `JobNodeRun` with `error=None`: `/node-runs` row `error is None`; manifest node has no error | characterization, green at `d8908f42`, written first; mutation: unconditional `json.dumps(project_node_error(...))` |
| P-T12 `tests/test_f29_node_error_reason.py::test_reason_failure_still_fails_the_job` | `registered_families` patched to raise `RuntimeError(MARKER)`: job `failed`, node has its code and the rule's message, `reason` null, MARKER absent from node, manifest and `Job.log` | no guard exists; mutation: remove the `try` |

**Pinned assertions that encode the leak.** P1 rewrites exactly these eight, names them in the PR, and changes no other existing assertion:

| Assertion at `d8908f42` | Today | New expectation |
|---|---|---|
| `tests/test_b3_error_mapping.py:114` | `node["message"].startswith("'t' chunk 1:")` | message from the rule; MARKER-style absence of the chunk text; `code` kept |
| `tests/test_b3_error_mapping.py:185` | `details.message == "engine said so"` | `FailureDetails` has no `message`; assert `details.code` and `details.reason` |
| `tests/test_b3_error_mapping.py:247` | `(details.code, details.message, details.where)` | `(details.code, details.where)` unchanged values |
| `tests/test_evidence_assembly.py:791` | `"frobnicate" in` raw-row message | `"frobnicate"` absent; `code == "node.runtime_error"`, fallback prose |
| `tests/test_evidence_assembly.py:837` | `"missing required field" in` JSON-row message | text absent; stored `code` kept; message from the rule |
| `tests/test_p6_failure_trace_baseline.py:161` | `message == "ValueError: see server log ..."` | `message ==` the fallback prose for `job.id` |
| `tests/test_v2_runner.py:2301` | `err["message"] == "boom"` | `"boom"` absent; `code` kept; message from the rule |
| `tests/test_job_node_runs_endpoint.py:184` | `"not_equal" in rows[0]["error"]` | `"not_equal"` absent from `message`; `code` kept |

Mutations to show red before push: each one named in the tables above and in section 12, plus restoring the `assembly.py:1984` raw copy (`:791` and `:184` red).

## 5. Slices

| Slice | Repo | Production files | Est. changed lines (incl. tests) | Depends on | Gate |
|---|---|---|---|---|---|
| **C0** grounding record | engine (docs) | 0 (`docs/records/2026-11-0x-f29-c0-consumer-grounding.md`) | ~120 | none | failure-tail census below, file:line on its merge base |
| **C1** CLI `run` reason member | `decoy` | 2: `src/decoy/cli/run.py`, `docs/cli-reference.md` (plus `CHANGELOG.md`) | ~360 | C0 | C-T1..C-T8 red then green on `uv.lock`; `test_run_capacity.py`, `test_run_native.py` green; four mutations red |
| **P1** error-node contract, reason | `decoy-platform` | 8: `api/jobs/job_node_error.py` (new), `_partial_publication.py`, `v2_orchestrator.py`, `v2_node_runs.py`, `v2_vault.py`, `routes_runtime_facts.py`, `api/evidence/schema.py`, `api/evidence/assembly.py` | ~740 (~270 production, ~470 tests and sentry) | C0; Platform repin to 0.17.0+; F31 rev7 drop list | P-T1..P-T7, P-T10..P-T12 and the sentry red then green on the Platform lock; only the eight named assertions changed; golden fixtures regenerated only where `nodes[].error` appears, diff listed |
| **F31 P8** (owned by F31) | `decoy-platform` | per F31 revision 7, section 1.6 item 9 only | F31's | **P1**, F31 ER and repin | F31's gate |
| **P2** `Job.log` registry summary | `decoy-platform` | 2: `api/persistence/error_projection.py`, `api/jobs/v2_orchestrator.py` | ~250 | P1, **F31 P2b** | P-T8, P-T9 red then green; `test_obs3_leak.py` green |
| **E7** docs close-out | engine | 0 (`docs/reasons.md` "Consumers"; F29 original plan status; Platform `docs/ROADMAP.md` F29 row) | ~60 | C1, P2 | docs only |

Order: C0; then C1 and P1 in parallel (different repositories, one writer each); then F31 P8 on top of P1; P2 after F31 P2b; E7 last. **Neither P1 nor F31 P8 receives a build gate until F31 rev7 states items 1 to 10 of the section 1.6 drop list.** Every slice is within 10 production files and 800 lines.

**C0 scope.** List each Platform path that writes a failed V2 job and whether it reaches `v2_orchestrator.py:274` (reason carried) or not (named limit): `_run_v2_pipeline_streaming` (`v2_runner.py:273`), `v2_sequential.py`, `v2_full_frame.py`, host-safety helpers (`queue_worker.py:419`, `:434`; `scheduler_claim_loop.py:173`). Confirm the CLI has one failure `emit_json` site and `JobDetail.tsx` reads `error.message` as text.

### 5.1 Slice C1 design (CLI)

In `except Exception as exc` (`run.py:861`), bind `engine_exc = exc` before the `translate_missing_extra` and `translate_ner_unavailable` rewrites (`:877-888`). Hoist the inline exit-1 tuple (`:944-1000`) into `_usage_types()` (same 15 members, same defensive imports; C-T8) and use it in the exit mapping unchanged. Derive `_cli_refusal_types()` from it: the members with no class in their MRO in `registered_families()`. Rule, stated once: **attach `payload["reason"] = reason_from_exception(engine_exc).to_payload()` unless `exc` is a CLI refusal and `engine_exc` is not an engine family.** So a rewritten NER failure carries the engine reason and keeps `error_kind: missing_extra` (C-T6); a plain missing extra and every CLI-owned usage error carry none (C-T5, C-T7). `docs/cli-reference.md` documents `reason` as additive and that `reason.outcome` distinguishes `execution`, `publication`, `verification` and `configuration`. Human mode: unchanged.

### 5.2 Slices P1 and P2 design (Platform)

**P1** builds section 1.6 items 1 to 8. Decisions not stated there:
- `failure_reason(selected, code)`: `reason_from_exception(selected)` when `selected`'s MRO hits `registered_families()` or `code` is `None` (giving `decoy.unexpected_error`); `None` when the result is `decoy.unregistered_reason` (a Platform code on an engine class, `v2_out_of_core.py:318`, `reason_adapter.py:177-181` at `v0.17.0`); the whole body in `try/except Exception` returning `None` and logging only the type name. `registered_families()` imports every `_CODED` and `_FIXED` module unguarded and `lru_cache` does not cache a failure (`reason_adapter.py:101-118`).
- `_failure_details` keeps its walk byte for byte and sets `reason=failure_reason(selected, code)`; `report` returns the code; `_error_node_run` (`v2_node_runs.py:434`) and the vault node (`v2_vault.py:97`, `code="vault_write_failed"`) call `node_error`.
- Node-runs route: `projected = project_node_error(r.error, job_id=job.id)`; `item.error = None if projected is None else json.dumps(projected)`. `_hydrate_nodes` uses the same reader, so `update_finished_manifest` and the legacy synthesis both re-project.

**P2.** `TrustedFailure` gains `reason: ReasonRecord | None`; `project_failure` checks `type(failure.reason) is ReasonRecord` and uses `reason.summary` as the body and `error_class=<namespace>.<code>` as the tail; approved codes keep precedence (same order as `node_message`). The orchestrator passes `details.reason`. Built on F31 P2b's `Job.log` writer.

### 5.3 What was cut, and why

Progress-event wiring (O1; F30 and F23 own it); new exit integers (O2; `reason.outcome` carries the category); reasons on other CLI verbs; a human-mode reason line; Platform codes in the engine registry (O3); retry policy from `retry_class`.

## 6. Design notes

One fact, one home: reasons live in the engine registry; consumers carry `to_payload()` and re-read with `from_payload()`. One message rule, one reason computation and one reader, in `job_node_error.py` (section 1.6).

## 7. Owner questions (each with a recommended default)

- **O1.** Progress events in V1 consumers? **No.** **O2.** New exit integers? **No**; CLI refusals keep `error_kind`, no `reason`, except a rewritten engine failure.
- **O3.** Platform-owned codes in the engine registry? **No** for V1: code, approved or fallback text, `reason` null. **O4.** Manifest schema bump for `reason`? **No** (section 10).
- **O5.** Error-node module owner: **decided (2026-11-02), F29 P1** (section 1.6).

## 8. V2-LOG (seen, not V1, no work planned)

- `_failure_details` trusts any string `.code` (`_partial_publication.py:169-171`); checked values are fixed strings or ints, not data. Class 6.
- CLI `run.py:1005-1007` `error` carries `str(exc)[:500]`, `print_exception()` under `--verbose`, and engine `.message` f-strings embed `{exc}` (`generation/statistical/_spec.py:160`); all F31. Class 1.
- Registry summaries are terse humanised codes; a patch-version editorial pass. Class 10.
- Cancelled and vault nodes get the "job failed" fallback prose, which misdescribes a cancellation (`v2_orchestrator.py:300-303`, `v2_vault.py:97`); not folded because it adds a branch to the frozen section 1.6 rule. Class 1 (wording, no value).
- Rollback across P1: the pre-change `_Strict` `NodeError` (`schema.py:57`, `:328`) rejects `reason`; re-ask at the GA flip (section 10). Class 10.
- P2's integration with F31 P2b has two possible shapes; settled on top of P2b. Class 9.

## 9. What this plan did not verify

- The dev-rules rulebooks were not re-read in this cloud container (`/home/cam/dev-rules` is on the owner's machine); the rulebook names in "Rules consulted" are carried from the earlier F29 plan.
- No CLI, Platform or engine test was run and no lock venv was installed (plan only); every anchor is read from source at the baselines. That `registered_families()` stays exported past 0.17.0 was not checked.
- F31 rev6's `exc-copy` census (`census-d8908f42.txt`) records the vault copy at `v2_vault.py:91` (statement start) and has no `_partial_publication.py:201` row; drop-list item 9 is stated as given. Drop-list item 10 is read from rev6 line 300, not checked against F31's intended P8 design.
- The Platform failure tails outside `v2_orchestrator.py:274-381` and how `JobDetail.tsx` renders `error` (C0).

## 10. Frozen contracts touched

Pre-GA (`RELEASE_PHASE`, compatibility contract §1); every change is additive except the node-error `message` value, which was never a contract (it was exception text).

| Frozen surface | Touched? | Answer |
|---|---|---|
| Engine reason registry identities | Read only | Nothing added or re-meant. |
| CLI exit-code contract (§3.4; `exit_codes.py`) | No | `_usage_types()` is a hoist with the same 15 members (C-T8). |
| CLI `run --json` failure envelope (`run.py:1040-1047`) | Additive | Optional `reason`; no key renamed or removed; `error` stays (F31). |
| Platform node-run error (`JobNodeRun.error`, served as `JobNodeRunOut.error: str | None`, `schemas.py:430`) | Additive key; `message` source changes | Gains `reason`; `code` and `where` unchanged; `message` from the rule; unknown stored keys dropped at read. **`error` stays `null` for nodes without an error** (P-T11). |
| Evidence manifest `nodes[].error` (`NodeError`, `schema.py:328`; `SCHEMA_VERSION = "r3.1"`) | Additive optional field | `reason: dict | None`, re-validated on read (P-T5). Manifests persisted before P1 are served as stored (section 3). |
| F29 to F31 node-error module | New, frozen for F31 | Section 1.6. |

**Schema version (O4): no bump.** Compatibility contract §5 ("Add an optional field to an existing artifact: no version bump"); the manifest keeps `r3.1` (`schema.py:626`). Manifest models are `_Strict` (`schema.py:57`), so a pre-change reader rejects `reason`; pre-GA, reader and writer ship together and no external reader exists. **Re-ask at the GA flip.**

## 11. Pre-mortem

| How it failed | Early signal | Guard |
|---|---|---|
| A reason change moved job status: cancelled jobs land `failed` | `cancelled` count drops after P1 | walk unchanged; P-T2; `test_b3_error_mapping.py:142-155` |
| F31 P8 built from rev6 strips `reason` or breaks P1's call sites | `TypeError` on `job_id=`; `reason` missing at read | section 1.6 contract; no build gate until F31 rev7 states the drop list; P-T10 |
| Successful nodes render as failed | `"error": "null"` on `/node-runs` | P-T11; section 10 row |
| A reason computation error leaves a job `running` | failure tail raises from `registered_families()` | `failure_reason` guard; P-T12 |
| Some failures carry `reason`, others not | engine failures with `reason` null | C0 census; section 3 limits |

## 12. Defect class table

| # | Class | Applies | Structure that closes it | Test that proves it | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: error and vault nodes, legacy rows | one writer `node_error`, one reader `project_node_error`; `message` only from `node_message`; `node-error` sentry | P-T1, P-T6, P-T7, P-T12, C-T1, C-T4, P-T8 markers absent | restore `getattr(selected, "message")` (P-T1); keep `str(exc)` in the vault node (P-T7) |
| 2 | Cleanup missing on an exit path | Yes: reason computation inside the failure tail could skip the node write and terminal transition | `failure_reason` swallows its errors and returns `None` | P-T12 | remove the `try` (P-T12 red) |
| 3 | State machine and concurrency | Yes: cancelled versus failed is chosen from `details.selected`; terminal transition must still run | walk not changed; reason computed beside it, never selects, never raises | P-T2, P-T12; `test_b3_error_mapping.py:142-155`, `:235-240` | select by `registered_families()` in the walk (P-T2 red) |
| 4 | Route parity and silent fallback | Yes: three row readers, stored manifests, message order, CLI refusals | one reader; stored manifests a named limit; one order shared with P2; refusal set derived | P-T4, P-T6, C-T5, C-T7, C-T8 | skip `project_node_error` in `assembly.py` (P-T6); summary-first order (P-T4) |
| 5 | Referential integrity | N/A: no rows, FKs or publication order change | none new | none | none |
| 6 | Untrusted producer | Yes: stored JSON rows and stored reasons | `project_node_error` re-projects on read; `from_payload` recomputes; exact `type(...) is ReasonRecord` at write and in `project_failure` | P-T5, P-T6, P-T9 | serve the stored `message` (P-T6); skip `from_payload` (P-T5) |
| 7 | Check then use on a path | N/A: no path is checked or used | none new | none | none |
| 8 | Weak tests and overclaimed evidence | Yes: eight assertions pin the leak; P-T4 could pass on a fake class | eight assertions listed; P-T4 on the real raise class; positive control per absence check | the eight assertions; P-T4; P-T6 | restore the `assembly.py:1984` raw copy (`:791`, `:184` red) |
| 9 | Behavior-preserving refactor drift | Yes: chain walk, CLI exit tuple hoist | walk unchanged; `_usage_types()` pinned member for member | P-T3, C-T8, `test_run_capacity.py`, `test_run_native.py` | drop `DuckDBSourceError` from `_usage_types()` (C-T8 red) |
| 10 | Frozen contract drift | Yes: CLI envelope, node error, `NodeError`, the F29 to F31 module | additive `reason`; section 1.6 frozen; `null` kept for success nodes | P-T10, P-T11, C-T1 existing keys | rev6 key-dropping reader (P-T10 red); unconditional `json.dumps` (P-T11 red) |

## 13. Enumerations (from the code at the baselines)

### 13.1 Exit paths

- CLI: `except Exception` at `run.py:861`, single emit at `:1040` (gains `reason`); refusal arms `:1048-1058` keep `error_kind`.
- Platform: failure tail `v2_orchestrator.py:283-303`, node write `v2_node_runs.py:434`; vault failure `v2_vault.py:89-98`.

### 13.2 Routes

- Platform unified V2 through `v2_orchestrator.py:274-381`: reason carried. Streaming (`v2_runner.py:273`), sequential, full-frame and host-safety tails: C0, named limit; their error nodes still pass `node_error`.
- Readers: `/node-runs` (`routes_runtime_facts.py:70`), `update_finished_manifest` (`assembly.py:2408`), legacy synthesis (`routes_evidence.py:190`); stored manifest (`routes_evidence.py:164-166`) served as stored.

### 13.3 FK shapes

- N/A: failure reporting only; no table, key or row relation changes.

### 13.4 Fields

- N/A: every field is stated once in section 1.6 (items 1, 3, 5, 6) and section 10.

### 13.5 Output surfaces

- N/A: the surfaces are stated once in section 3 item 3 and section 10.

## 14. Revision 3 change map

| Finding | Class | Change | Section | Test |
|---|---|---|---|---|
| V1-1 F31 rev6 conflicts with P1 | 10, 4 | interface contract and F31 drop list published once; 1.5, O5, 5.2 prose removed; no build gate until F31 rev7 | 1.6, 5 | P-T10 `test_node_error_contract_shape` |
| V1-2 `json.dumps(None)` | 10 | `None` stays `None` on the route | 5.2, 10 | P-T11 `test_successful_node_error_stays_null` |
| V1-3 P-T4 and `decoy.unregistered_reason` | 4, 8 | approved first; `failure_reason` drops `unregistered_reason` | 1.6, 5.2 | P-T4 `[approved, not_approved]` through `v2_out_of_core.py:318` |
| V1-4 stored manifests bypass the reader | 4, 1 | named limit; P-T6 over three row readers | 3, 10, 4 | P-T6 `test_legacy_row_withheld_at_read[...] x [...]` |
| V1-5 unguarded reason computation | 2, 3 | `failure_reason` guard | 5.2 | P-T12 `test_reason_failure_still_fails_the_job` |
| V1-6 five more leak-pinning assertions | 8 | eight listed with new expectations | 4 | the eight rewritten assertions |
| V1-7 C-T8 does not pin members | 9, 8 | 15 members pinned | 4 | C-T8 `test_refusal_set_is_derived_from_exit_mapping` |
