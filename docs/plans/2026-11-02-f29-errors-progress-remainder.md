Status: handoff

Summary: F29's engine half is built; what V1 still owes is two consumers. The CLI `decoy run --json` failure envelope and the Platform failed-job node error should carry the engine's registered reason, and the Platform error nodes (execution and vault) should stop carrying exception text onto an owner-visible surface.

Revision 5 (2026-11-02, from Dennis on `98496275`): CLI baseline moved to `b4f19ba` (CLI #78 added `_ChunkedVaultError` to the exit-1 tuple, now 16 entries), `_VaultWriteError` carries no reason, the classifier evaluation order is stated (section 5.1, C-T7 to C-T9). Change map in section 14.

Baselines (`git rev-parse`, 2026-11-02): engine `v0.17.0`, the version both consumers build against, registry **1.1.0** (`git show v0.17.0:src/decoy_engine/reasons/_registry.py:24`; `reason_adapter.py` read at this tag); Platform `origin/main` `67e8cbba`; CLI `louiskeep/decoy` `b4f19ba` (`origin/main` is `004afda`, which changes only `docs/diagrams/`; `src/` is identical); F31 plan `plan/f31-safe-output` revision 6 `c2b16de0`.

Risk: **R2**, plan only. The build slices change a public CLI JSON envelope, the Platform node-error shape and an evidence-manifest model, all pre-GA. This document grants no build approval. Review order: Dennis, then Codex.

Rules consulted: 00-universal, development-loop, risk-and-exceptions, scope-discipline, reuse-first; README-selected rulebooks: testing, api-and-compatibility, security, architecture, documentation. Read: engine `CLAUDE.md`; Platform `handoffs/queue` `docs/handoffs/V1-BAR.md`, `DEFECT-CATALOG.md`, `tools/plan-lint`; F31 revision 6 sections 3.8, 6, 11 and the P8 row.

# F29 remainder: consumer reasons for the CLI and Platform (FRAME + PLAN)

## 1. FRAME

### 1.1 What is already done (verified)

Done in the engine (MASTER §F29): the versioned registry (`src/decoy_engine/reasons/_registry.py:24`, `docs/reasons.md`), retry classes and outcomes, `reason_from_exception` (`reason_adapter.py:144-164` at `v0.17.0`: `decoy.unexpected_error` outside `registered_families()`, never reads message text, never raises), `ReasonRecord.to_payload` / `from_payload` (never raises, recomputes outcome and retry) / `.summary`, and the progress-event fields. Not done: the CLI (`run.py:857-1056`) and the Platform, which imports nothing from `decoy_engine.reasons` (it pins `decoy-engine==0.17.0`, `pyproject.toml:40`). Assertions read `registry_version` and identities as `<pinned>`, never a literal version.

### 1.2 What F29 still owes V1 (the problem, re-derived)

**P-CLI.** The `decoy run --json` failure envelope (`run.py:1036-1042`) is `{command, status, config, mode, error}`, `error = str(exc)[:500]`; a machine code exists only for capacity, native gate and missing extra (`:1043-1053`). CI cannot tell execution, publication and verification failures apart.

**P-PLAT-1. Error nodes carry exception text to the job owner.** (a) `_failure_details` copies `getattr(selected, "message", None)` (`api/jobs/_partial_publication.py:201`) into the node (`v2_node_runs.py:434`); engine `.message` embeds exception text (`generation/statistical/_spec.py:160` at `v0.17.0`). (b) The vault node stores `str(exc)` (`api/jobs/v2_vault.py:98`). (c) `_hydrate_nodes` copies a non-JSON row's raw text (`api/evidence/assembly.py:1984`). All reach the non-admin owner via `GET /api/jobs/{id}/node-runs` (`routes_runtime_facts.py:70-115`; `JobNodeRunOut.error: str | None`, `schemas.py:430`) and the manifest. **V1 bar rule 3**, class 1.

**P-PLAT-2.** `Job.log` renders only `APPROVED_ERROR_MESSAGES` codes (`error_projection.py:45-79`); an engine failure renders `internal error: <TypeName>`.

### 1.3 Definition of done

1. `decoy run --json` failure envelope carries `reason` (`ReasonRecord.to_payload()`) for engine-entered failures, computed from the exception as raised, before any CLI rewrite; exit integers unchanged. C-T1 to C-T9 pass.
2. Every Platform error node (execution, vault) is written and read through the section 1.6 contract on every reader that builds from `JobNodeRun` rows; no exception text is stored or served; a node without an error stays `null`. P-T1 to P-T7 and P-T10 to P-T12 pass; `node-error` sentry (section 1.6 item 7, seeded fixtures) at 0.
3. `Job.log`'s terminal failure line for an engine-reasoned failure shows the registry summary and `error_class=<namespace>.<code>` (P2, after F31 P2b). P-T8, P-T9 pass. `docs/reasons.md` gains a "Consumers" section naming the surfaces and limits (E7).

### 1.4 Reuse inventory

Engine (public in 0.17.0): `reason_from_exception`, `reason_adapter.registered_families` (`reason_adapter.py:115`), `ReasonRecord.to_payload` / `from_payload` / `.summary`. CLI: `emit_json` and the single failure emit site (`run.py:1056`), the exit-1 tuple (`run.py:944-993`). Platform: `_failure_details` chain walk (kept as is), `APPROVED_ERROR_MESSAGES`, `TrustedFailure` / `project_failure`, the persisted-exports re-projection pattern (`routes_runtime_facts.py:105-113`). One new module, `api/jobs/job_node_error.py` (section 1.6), shared with F31.

### 1.5 Boundaries

- **Engine:** no production change (registry additions are O3). The CLI pins 0.17.0 (`pyproject.toml:103`).
- **F31:** the node-error module is F29 P1's (owner ruling 2026-11-02); the interface and what F31 revision 7 drops are section 1.6. F31 owns every other except-bound copy, the CLI ones included (section 8). P2 waits for F31 P2b.
- **decoy-web:** `JobNodeRunOut.error` stays a JSON string with the same keys plus `reason`, or `null`; the frontend is not touched.

### 1.6 Interface contract F29 publishes (F31 rev7 references this)

Module `api/jobs/job_node_error.py`, created by F29 P1. Public names and signatures are frozen for F31:

1. `node_error(code: str, *, job_id: int, where: str | None = None, reason: ReasonRecord | None = None) -> str`. Returns a JSON object string with exactly the keys `code`, `message`, `where`, `reason`. `reason` is `reason.to_payload()` when `type(reason) is ReasonRecord`, else `null`. `message` comes only from `node_message`. No caller passes a message. Never raises on content.
2. `node_message(code: str, reason: ReasonRecord | None, *, job_id: int) -> str`, the one message rule, in this order: `APPROVED_ERROR_MESSAGES[code]` when approved; else `reason.summary` when `reason` is not `None`; else the fallback prose `"The job failed; see the server log (job_id=<id>)."`.
3. `project_node_error(raw: str | None, *, job_id: int) -> dict[str, Any] | None`. Returns `None` when `raw` is `None` or empty (a node without an error). Otherwise a dict with exactly `code` (closed token `^[a-z][a-z0-9_.]{0,63}$`, else `node.runtime_error`), `message` (re-rendered by `node_message`, never the stored text), `where` (string or `None`), `reason` (`ReasonRecord.from_payload(stored).to_payload()`, or `None` when absent). Every other stored key is dropped. A non-JSON row becomes `node.runtime_error` with the fallback prose.
4. `failure_reason(selected: BaseException | None, code: str | None) -> ReasonRecord | None`: the only place the Platform computes a reason for an error node.
5. `FailureDetails` (`api/jobs/_partial_publication.py`) has fields `selected`, `code`, `where`, `reason`; no `message`. The `.message` read at `_partial_publication.py:201` is deleted.
6. `NodeError` (`api/evidence/schema.py:328`) gains `reason: dict[str, Any] | None = None`.
7. Sentry `tests/sentry/test_node_error.py` (`node-error`): every `JobNodeRun(...)` with `error=` in `api/` passes a `node_error(...)` call; `_partial_publication.py` and `job_node_error.py` read no `.message`. Baseline 0 after P1.
8. Tests owned by F29 P1 in `tests/test_job_node_error.py`: `test_vault_node_has_no_marker`, `test_legacy_row_withheld_at_read[...]`, `test_node_error_contract_shape`. F31 extends these; it does not recreate them.
9. Extension point for F31 (RECOMMENDED (go with recommended applies)): F31 P8 may add one keyword-only parameter `error_type: SafeErrorType | None = None` to `node_error`, one `error_type` key to the output and to `project_node_error` (via `SafeErrorType.from_label`), one `error_type` field to `NodeError`, and may replace the fallback prose string (branch 3 of `node_message`) with the `platform.job_failed` prose. The `error_type` key extends item 1's key set and P-T10's key assertion; F31 edits P-T10 for it. Nothing else in items 1 to 8 changes.

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
11. T-P8's message positive control (F31 rev6 line 205). It becomes `message == node_message(code, reason, job_id=...)`, which is `reason.summary` for the registered causes `subset_error` and `plan_compile_error`. The `safe_error_type` label check moves under item 9 (the `error_type` key).
12. The failure-projection row rule at F31 rev6 line 253 ('approved code selects prose, else the type label'). Replace it with a reference to section 1.6 item 2.

Anchors in F31 revision 6 (`c2b16de0`, `docs/plans/2026-10-30-f31-shared-safe-output-boundary.md`): section 3.8 is line 124; the `node-error` baseline 3 is lines 26 and 228; the P8 row (7 files, 480 lines, no `schema.py`) is line 300; T-P8 is line 205; the failure-projection row is line 253.

## 2. Established solutions, searched first

1. **AIP-193 `ErrorInfo`** ([aip.dev/193](https://AIP.dev/193)): a stable `(reason, domain)` identity separate from the message; the engine `(namespace, code)` follows it. **Followed.** `RetryInfo` delays not adopted.
2. **RFC 9457** ([rfc9457](https://www.rfc-editor.org/rfc/rfc9457.pdf)): unknown extension members are ignored, so `reason` is additive. **Followed.**
3. **In-codebase:** the section 1.4 reuse inventory (stored-export re-projection, `TrustedFailure`). Reused; no new sanitizer.

## 3. Guarantee, pinned to the V1 bar

1. **Function.** For a `decoy run --json` failure the envelope carries `reason` equal to `reason_from_exception(engine_exc)` under the pinned engine, where `engine_exc` is the exception as raised; CLI-owned failures (refusals and `_VaultWriteError`) carry none. Exit integers are unchanged. On the Platform, **job status selection is unchanged**: `_failure_details` keeps today's walk (first non-empty string `.code`, `_partial_publication.py:186-200`), so `details.selected` and the cancelled-versus-failed decision (`v2_orchestrator.py:315-318`) are identical. Reason computation cannot stop the failure tail: `failure_reason` swallows its own errors (P-T12). A successful node's `error` stays `null` (P-T11).
2. **Data integrity.** No output, commit order or staging changes. The reason is computed after the failure is decided and stored in the existing node-error write.
3. **Nothing visible to a non-admin.** On `/node-runs`, manifest `nodes[].error` built from `JobNodeRun` rows, the CLI `reason` member and (P2) the `Job.log` terminal body, the only text is registry summaries, `APPROVED_ERROR_MESSAGES` text, the fixed fallback prose and closed tokens.

**Named limits:** CLI verbs other than `run`; the CLI `error` field (F31); failures before the engine is entered; Platform-coded failures (`job_cancelled`, `job_already_terminal`, `_QualityHardFail` codes, `post_validation_route_rejected`, `partial_publication_failure`, the `native_budget.py` codes) and Platform codes raised on an engine class (`out_of_core_budget_unresolved`, `v2_out_of_core.py:319`) keep their code and get no `reason` (O3); an uncoded engine cause (`CommitError`, `ValidatorFailedError`) under `PartialPublicationError` yields the wrapper's code and no reason, because the walk selects the wrapper; failure tails that do not reach `v2_orchestrator.py:289-396` carry no reason (C0 lists them; every node they write still goes through `node_error`). **Stored manifests:** `GET /jobs/{id}/evidence` returns `Job.evidence_manifest` as stored (`routes_evidence.py:164-166`); a manifest persisted before P1 keeps its copied `nodes[].error.message` and is not re-projected (re-projection would break `manifest_hash`). Pre-release hard delete applies: no customer data exists, no migration. **Non-goals:** progress-event wiring (O1; F30 and F23 own it); new exit integers (O2; `reason.outcome` carries the category); a human-mode reason line; Platform codes in the engine registry (O3); a retry executor or policy from `retry_class`; attempt-level reason history; F31's alias and safe-event work; frontend changes.

## 4. Observable behavior, failure modes and acceptance tests (before implementation)

Red-first on each repository's lock, at the real boundary: CLI tests through `CliRunner` over `decoy run --json` with the engine entry patched to raise; Platform tests through `run_v2_pipeline_job`, read back via the HTTP route and the manifest assembler. `MARKER` is a unique string; every absence check has a positive control on the same payload.

### CLI (repository `decoy`)

| Test (`tests/e2e/test_run_reason_envelope.py::`) | Behavior | Fail-before evidence |
|---|---|---|
| C-T1 `test_capacity_failure_carries_registry_reason` | `ExecutionError(code="out_of_core_insufficient_memory", message=MARKER)`: exit 5; `reason` identity `runtime.out_of_core_insufficient_memory`, outcome `execution`, retry `environment_repair`; `error_kind`/`code` still present (`tests/e2e/test_run_capacity.py:86`); MARKER absent from `reason` | no `reason` key at `run.py:1036-1042` |
| C-T2 `test_publication_failure_is_distinguishable` | engine `CommitError`: exit 3; `reason` `decoy.publication_failed`, outcome `publication` | as above |
| C-T3 `test_config_failure_reason_says_configuration_repair` | `ConfigError(MARKER)`: exit 1; outcome `configuration`, retry `configuration_repair` | as above |
| C-T4 `test_unknown_exception_gets_fallback_reason` | `ValueError(MARKER)` with `.code = MARKER`: exit 3; `reason` `decoy.unexpected_error`; MARKER absent from `reason` | as above |
| C-T5 `test_cli_owned_refusal_keeps_error_kind_without_reason` | ImportError translated by `translate_missing_extra`: `error_kind == "missing_extra"`, no `reason` | red if `unexpected_error` is attached |
| C-T6 `test_rewritten_ner_failure_keeps_engine_reason` | engine `NerUnavailableError` rewritten into `MissingExtraError` (`run.py:882-884`): `error_kind == "missing_extra"` **and** `reason` is the engine record of the original | mutation: compute from the rewritten `exc` |
| C-T7 `test_cli_owned_failure_has_no_reason[config_validation, chunked_vault, vault_write]` | `_ConfigValidationError` (raised `run.py:520`, `:618`) and `decoy run --chunked --vault` (`_ChunkedVaultError`, `:696`): exit 1; `_VaultWriteError` (`_commit_vault`, `:1627`): exit 3; no `reason` in any cell | mutations: hand-kept refusal set missing `_ChunkedVaultError` (`chunked_vault` red); `_VaultWriteError` left out of `_cli_refusal_types()` (`vault_write` red, gains `decoy.unexpected_error`) |
| C-T8 `test_refusal_set_is_derived_from_exit_mapping` | `_usage_types()` equals, member for member, the 16 entries of the `b4f19ba` literal at `run.py:944-993`: `PlanCompileError`, `PipelineValidationError`, `ConfigError`, `_ChunkedGenerateError`, `_ChunkedFixedWidthError`, `_ChunkedReportFlagError`, `_ChunkedVaultError`, `_VaultUsageError`, `_MaskSecretUsageError`, `DuckDBSourceError`, `MissingExtraError`, `UnsupportedCloudEndpointError`, `*_ner_unavailable_types`, `_native_gate.NativeGateError`, `_ConfigValidationError`, `*_mask_secret_error_types` (starred groups expanded as the CLI does); then every non-engine member is in `_cli_refusal_types()`, `_VaultWriteError` is in it, and every engine member (MRO hits `registered_families()`) is not | `_usage_types()` does not exist at `b4f19ba`; mutations: drop `DuckDBSourceError` or `_ChunkedVaultError` (red) |
| C-T9 `test_classifier_failure_keeps_exit_and_envelope[config_error, commit_error, capacity]` | `decoy_engine.reason_adapter.registered_families` raises `RuntimeError(MARKER)` and `decoy.cli.run` is reloaded under the patch: exits 1, 3 and 5 (`capacity` keeps `error_kind` and `code`, `test_run_capacity.py:86`); each envelope parses with the `b4f19ba` keys and no `reason`; MARKER absent from stdout and stderr; teardown reloads `decoy.cli.run` again after the patch is undone, so later tests get unpatched class objects | no guard in revision 3; mutations: remove the `try` in `_engine_family` (no envelope), compute `_cli_refusal_types()` at import (the reload raises), skip it when `exc` is not in `_usage_types()` (`commit_error` gains `decoy.unexpected_error`) |

### Platform (repository `decoy-platform`)

| Test | Behavior | Fail-before evidence |
|---|---|---|
| P-T1 `tests/test_f29_node_error_reason.py::test_engine_failure_node_error_has_reason_and_fixed_message[statistical_spec, subset_error_wrapped, plan_compile_error_wrapped]` | `[statistical_spec]` `StatisticalSpecError(code=plan.STATISTICAL_SNAPSHOT_UNREADABLE, message=MARKER)`; `[*_wrapped]` a marker `SubsetError` or `PlanCompileError` with a registered code (`subset_budget_exceeded`, `empty_domain_pool`; `reasons/subset/_part1.py:44`, `reasons/plan/_part1.py:129`) under a `PartialPublicationError`, as F31 T-P8; owner `GET /node-runs`: `error.reason` is the registry record, `error.message == reason.summary`, not the fallback prose; MARKER absent from body and manifest | `_partial_publication.py:201` copies `.message`; mutation: `node_message` returns the fallback prose for every non-approved code (`_wrapped` cells red) |
| P-T2 `...::test_wrapped_cancellation_still_lands_cancelled` | `JobCancelledError` inside a streamed read, wrapped in `PartialPublicationError` (`v2_stream_coordinator.py:237`): job `cancelled`, node `code == "job_cancelled"`, `reason` null, fallback prose | mutation: select by `registered_families()` in the walk |
| P-T3 `...::test_wrapped_engine_cause_reason_and_location` | `PartialPublicationError` over an engine coded cause: `reason` from the cause, `where == "table=<t>"` | locks the walk and `where` |
| P-T4 `...::test_platform_coded_failure_keeps_code_and_fixed_message[approved, not_approved]` | `[approved]` raised as at `v2_out_of_core.py:319` (the real class, engine `ExecutionError`; code outside registry 1.1.0): `message == APPROVED_ERROR_MESSAGES["out_of_core_budget_unresolved"]`, `reason is None`; `[not_approved]` `job_already_terminal`: fallback prose, `reason` null; code unchanged in both | `_partial_publication.py:201` copies `.message`; mutations: summary-first order (red on message); no unregistered filter (red on `reason`) |
| P-T5 `...::test_stored_reason_reread_through_from_payload` | stored `reason` with a forged `retry_class`, and one with an unknown same-major code: both readers show the registry retry class and `decoy.unknown_reason_from_future` | `NodeError` (`schema.py:328`) has no `reason` |
| P-T6 `tests/test_job_node_error.py::test_legacy_row_withheld_at_read[json, free_text, extra_key] x [node_runs_route, finish_manifest, legacy_synthesis]` | rows stored as at `d8908f42` with MARKER, read by `/node-runs`, `update_finished_manifest` (`assembly.py:2408`) and the legacy synthesis (`routes_evidence.py:190`): MARKER absent; `message` from the rule; unknown keys dropped; positive control: `code` and `where` kept | `assembly.py:1984` copies raw text; the route returns `row.error` as stored |
| P-T7 `tests/test_job_node_error.py::test_vault_node_has_no_marker` | vault write raises `OSError(MARKER)`: stored row, `/node-runs` and manifest carry `vault_write_failed` and the fallback prose, no MARKER | `v2_vault.py:98` stores `str(exc)` |
| P-T8 `tests/test_obs3_leak.py::test_job_log_engine_failure_shows_registry_summary` (P2) | the P-T1 failure: terminal line body is the registry summary, tail `error_class=plan.statistical_snapshot_unreadable`, no MARKER | `error_projection.py:145-159` renders `internal error: StatisticalSpecError` |
| P-T9 `...::test_tampered_reason_on_trusted_failure_cannot_inject_text` (P2) | `TrustedFailure.reason` not a `ReasonRecord`: generic rendering | mirrors the `.code` tamper test |
| P-T10 `tests/test_job_node_error.py::test_node_error_contract_shape` | `inspect.signature`: `job_id`, `where`, `reason` keyword-only, no positional `exc`; output keys exactly `{code, message, where, reason}`; a row written with a `ReasonRecord` survives `project_node_error` with `reason` intact | `job_node_error.py` absent at `67e8cbba`; mutation: a reader dropping keys outside `{code, message, where}` (the rev6 rule) |
| P-T11 `tests/test_job_node_error.py::test_successful_node_error_stays_null` | success `JobNodeRun` with `error=None`: `/node-runs` row `error is None`; manifest node has no error | characterization, green at `67e8cbba`, written first; mutation: unconditional `json.dumps(project_node_error(...))` |
| P-T12 `tests/test_f29_node_error_reason.py::test_reason_failure_still_fails_the_job` | `registered_families` patched to raise `RuntimeError(MARKER)`: job `failed`, node has its code and the rule's message, `reason` null, MARKER absent from node, manifest and `Job.log` | no guard exists; mutation: remove the `try` |

**Pinned assertions that encode the leak.** P1 rewrites exactly these eight, names them in the PR, and changes no other existing assertion:

| Assertion at `67e8cbba` | Today | New expectation |
|---|---|---|
| `tests/test_b3_error_mapping.py:114` | `node["message"].startswith("'t' chunk 1:")` | message from the rule; MARKER-style absence of the chunk text; `code` kept |
| `tests/test_b3_error_mapping.py:185` | `details.message == "engine said so"` | `FailureDetails` has no `message`; assert `details.code` and `details.reason` |
| `tests/test_b3_error_mapping.py:247` | `(details.code, details.message, details.where)` | `(details.code, details.where)` unchanged values |
| `tests/test_evidence_assembly.py:791` | `"frobnicate" in` raw-row message | `"frobnicate"` absent; `code == "node.runtime_error"`, fallback prose |
| `tests/test_evidence_assembly.py:837` | `"missing required field" in` JSON-row message | text absent; stored `code` kept; message from the rule |
| `tests/test_p6_failure_trace_baseline.py:161` | `message == "ValueError: see server log ..."` | `message ==` the fallback prose for `job.id` |
| `tests/test_v2_runner.py:2317` | `err["message"] == "boom"` | `"boom"` absent; `code` kept; message from the rule |
| `tests/test_job_node_runs_endpoint.py:184` | `"not_equal" in rows[0]["error"]` | `"not_equal"` absent from `message`; `code` kept |

Mutations to show red before push: each one named in the tables above and in section 12.

## 5. Slices

| Slice | Repo | Production files | Est. changed lines (incl. tests) | Depends on | Gate |
|---|---|---|---|---|---|
| **C0** grounding record | engine (docs) | 0 (`docs/records/2026-11-0x-f29-c0-consumer-grounding.md`) | ~120 | none | failure-tail census below, file:line on its merge base |
| **C1** CLI `run` reason member | `decoy` | 2: `src/decoy/cli/run.py`, `docs/cli-reference.md` (plus `CHANGELOG.md`) | ~360 | C0 | C-T1..C-T9 red then green on `uv.lock`; `test_run_capacity.py`, `test_run_native.py` green; every C-T mutation red |
| **P1** error-node contract, reason | `decoy-platform` | 8: `api/jobs/job_node_error.py` (new), `_partial_publication.py`, `v2_orchestrator.py`, `v2_node_runs.py`, `v2_vault.py`, `routes_runtime_facts.py`, `api/evidence/schema.py`, `api/evidence/assembly.py` | ~740 (~270 production, ~470 tests and sentry) | C0; F31 rev7 drop list | P-T1..P-T7, P-T10..P-T12 and the sentry red then green on the Platform lock; only the eight named assertions changed; golden fixtures regenerated only where `nodes[].error` appears, diff listed |
| **F31 P8** (owned by F31) | `decoy-platform` | per F31 revision 7, section 1.6 item 9 only | F31's | **P1**, F31 ER and repin | F31's gate |
| **P2** `Job.log` registry summary | `decoy-platform` | 2: `api/persistence/error_projection.py`, `api/jobs/v2_orchestrator.py` | ~250 | P1, **F31 P2b** | P-T8, P-T9 red then green; `test_obs3_leak.py` green |
| **E7** docs close-out | engine | 0 (`docs/reasons.md` "Consumers"; F29 original plan status; Platform `docs/ROADMAP.md` F29 row) | ~60 | C1, P2 | docs only |

Order: C0; then C1 and P1 in parallel (different repositories, one writer each); then F31 P8 on top of P1; P2 after F31 P2b; E7 last. **Neither P1 nor F31 P8 receives a build gate until F31 rev7 states items 1 to 12 of the section 1.6 drop list.** Every slice is within 10 production files and 800 lines.

**C0 scope.** List each Platform path that writes a failed V2 job and whether it reaches `v2_orchestrator.py:289` (reason carried) or not (named limit): `_run_v2_pipeline_streaming` (`v2_runner.py:269`), `v2_sequential.py`, `v2_full_frame.py`, host-safety helpers (`queue_worker.py:419`, `:434`; `scheduler_claim_loop.py:173`). Confirm the CLI has one failure `emit_json` site and `JobDetail.tsx` reads `error.message` as text.

### 5.1 Slice C1 design (CLI)

In `except Exception as exc` (`run.py:857`), bind `engine_exc = exc` before the `translate_missing_extra` and `translate_ner_unavailable` rewrites (`:872-884`). Hoist the inline exit-1 tuple (`:944-993`) into `_usage_types()` (same 16 members including `_ChunkedVaultError`, same defensive imports; C-T8) and use it in the exit mapping unchanged. One helper, `_engine_family(exc) -> bool | None`, is the only CLI caller of `registered_families()`; it wraps the call and the MRO walk in `try/except Exception` and returns `None` on failure (the hazard is in section 5.2). `_cli_refusal_types()` (the `_usage_types()` members where it is `False`, plus `_VaultWriteError`) and the attach rule use it, computed lazily in the failure path, never at import. Rule, stated once: **attach `payload["reason"] = reason_from_exception(engine_exc).to_payload()` unless `exc` is a CLI refusal and `engine_exc` is not an engine family.** All of it sits in one guard: on `None` or a raise, `reason` is omitted, exit and envelope keys stay as at `b4f19ba` (`run.py:1036-1056`), and nothing about the failure reaches stdout (C-T9). **Evaluation order:** inside the guard, compute `_cli_refusal_types()` and `_engine_family(engine_exc)` first and unconditionally, then test `exc`, then call `reason_from_exception`; short-circuiting past the classifier would let `reason_from_exception` (which never raises) attach `decoy.unexpected_error` in the C-T9 `commit_error` cell. **`_VaultWriteError`** (`run.py:134`, raised `:1627` with the constant `VAULT_WRITE_FAILED`, exit 3) is a CLI-owned runtime failure, not an engine one. RECOMMENDED (go with recommended applies): it joins `_cli_refusal_types()` and carries no `reason`, because `decoy.unexpected_error` would blame the engine; its exit stays 3 (C-T7). So a rewritten NER failure carries the engine reason and keeps `error_kind: missing_extra` (C-T6); a plain missing extra and every CLI-owned usage error carry none (C-T5, C-T7). `docs/cli-reference.md` documents `reason` as additive and that `reason.outcome` distinguishes `execution`, `publication`, `verification` and `configuration`. Human mode: unchanged.

### 5.2 Slices P1 and P2 design (Platform)

**P1** builds section 1.6 items 1 to 8. Decisions not stated there:
- `failure_reason(selected, code)`: `reason_from_exception(selected)` when `selected`'s MRO hits `registered_families()` or `code` is `None` (giving `decoy.unexpected_error`); `None` when the result is `decoy.unregistered_reason` (a Platform code on an engine class, `v2_out_of_core.py:319`, `reason_adapter.py:177-181` at `v0.17.0`); the whole body in `try/except Exception` returning `None` and logging only the type name. `registered_families()` imports every `_CODED` and `_FIXED` module unguarded and `lru_cache` does not cache a failure (`reason_adapter.py:101-118`).
- `_failure_details` keeps its walk byte for byte and sets `reason=failure_reason(selected, code)`; `report` returns the code; `_error_node_run` (`v2_node_runs.py:434`) and the vault node (`v2_vault.py:98`, `code="vault_write_failed"`) call `node_error`.
- Node-runs route: `projected = project_node_error(r.error, job_id=job.id)`; `item.error = None if projected is None else json.dumps(projected)`. `_hydrate_nodes` uses the same reader, so `update_finished_manifest` and the legacy synthesis both re-project.

**P2.** `TrustedFailure` gains `reason: ReasonRecord | None`; `project_failure` checks `type(failure.reason) is ReasonRecord` and uses `reason.summary` as the body and `error_class=<namespace>.<code>` as the tail; approved codes keep precedence (same order as `node_message`). The orchestrator passes `details.reason`. Built on F31 P2b's `Job.log` writer.

## 6. Design notes

One fact, one home: reasons live in the engine registry; consumers carry `to_payload()` and re-read with `from_payload()`; the message rule, reason computation and reader live in `job_node_error.py`.

## 7. Owner questions (each with a recommended default)

- **O1.** Progress events in V1 consumers? **No.** **O2.** New exit integers? **No** (section 5.1). **O3.** Platform-owned codes in the engine registry? **No** for V1 (section 3 limits). **O4.** Manifest schema bump for `reason`? **No** (section 10).

## 8. V2-LOG (seen, not V1, no work planned)

- `_failure_details` trusts any string `.code` (`_partial_publication.py:169-171`); checked values are fixed strings or ints, not data. Class 6.
- CLI `run.py:1002-1004` `error` carries `str(exc)[:500]`, `print_exception()` under `--verbose` (`:1082`), and engine `.message` f-strings embed `{exc}` (`generation/statistical/_spec.py:160`); all F31. Class 1.
- Registry summaries are terse humanised codes; a patch-version editorial pass. Class 10.
- Cancelled and vault nodes get the "job failed" fallback prose, which misdescribes a cancellation (`v2_orchestrator.py:315-318`, `v2_vault.py:98`); not folded because it adds a branch to the frozen section 1.6 rule. Class 1 (wording, no value).
- P2's integration with F31 P2b has two possible shapes; settled on top of P2b. Class 9.
- Item 9's prose replacement changes the string pinned for `test_p6_failure_trace_baseline.py:161`, P-T6 and P-T7; F31 owns that update. Class 10.
- The key-setup failure (`_key_setup_error`, `v2_orchestrator.py:253-254`) now reaches the failure tail; C0 lists it. Class 4.
- `APPROVED_ERROR_MESSAGES` gained `keyed_strategy_requires_secret` and `weak_mask_secret` (`error_projection.py:74`, `:77`), which render approved prose; E7 names them. Class 1.

## 9. What this plan did not verify

- No CLI, Platform or engine test was run and no lock venv was installed; every anchor is read from source at the baselines.
- F31 rev6's `exc-copy` census (`census-d8908f42.txt`) records the vault copy at `v2_vault.py:91` (statement start) and has no `_partial_publication.py:201` row; drop-list item 9 is stated as given. Drop-list item 10 is read from rev6 line 300, not checked against F31's intended P8 design.
- The Platform failure tails outside `v2_orchestrator.py:289-396` and how `JobDetail.tsx` renders `error` (C0).

## 10. Frozen contracts touched

Pre-GA (`RELEASE_PHASE`, compatibility contract §1); every change is additive except the node-error `message` value, which was never a contract (it was exception text).

| Frozen surface | Touched? | Answer |
|---|---|---|
| Engine reason registry identities | Read only | Nothing added or re-meant. |
| CLI exit-code contract (§3.4; `exit_codes.py`) | No | `_usage_types()` is a hoist with the same 16 members, `_ChunkedVaultError` included (C-T7, C-T8); `_VaultWriteError` keeps exit 3. |
| CLI `run --json` failure envelope (`run.py:1036-1042`) | Additive | Optional `reason`; no key renamed or removed; `error` stays (F31). |
| Platform node-run error (`JobNodeRun.error`, served as `JobNodeRunOut.error: str | None`, `schemas.py:430`) | Additive key; `message` source changes | Gains `reason`; `code` and `where` unchanged; `message` from the rule; unknown stored keys dropped at read. **`error` stays `null` for nodes without an error** (P-T11). |
| Evidence manifest `nodes[].error` (`NodeError`, `schema.py:328`; `SCHEMA_VERSION = "r3.1"`) | Additive optional field | `reason: dict | None`, re-validated on read (P-T5). |
| F29 to F31 node-error module | New, frozen for F31 | Section 1.6. |

**Schema version (O4): no bump.** Compatibility contract §5 ("Add an optional field to an existing artifact: no version bump"); the manifest keeps `r3.1` (`schema.py:20`). Manifest models are `_Strict` (`schema.py:57`), so a pre-change reader rejects `reason`; pre-GA, reader and writer ship together and no external reader exists. **Re-ask at the GA flip.**

## 11. Pre-mortem

| How it failed | Early signal | Guard |
|---|---|---|
| A reason change moved job status: cancelled jobs land `failed` | `cancelled` count drops after P1 | walk unchanged; P-T2; `test_b3_error_mapping.py:142-155` |
| F31 P8 built from rev6 strips `reason` or breaks P1's call sites | `TypeError` on `job_id=`; `reason` missing at read | section 1.6 contract; no build gate until F31 rev7 states the drop list; P-T10 |
| Successful nodes render as failed | `"error": "null"` on `/node-runs` | P-T11 |
| A reason computation error leaves a job `running` | failure tail raises from `registered_families()` | `failure_reason` guard; P-T12 |

## 12. Defect class table

| # | Class | Applies | Structure that closes it | Test that proves it | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: error and vault nodes, legacy rows | one writer `node_error`, one reader `project_node_error`; `message` only from `node_message`; `node-error` sentry | P-T1, P-T6, P-T7, P-T12, C-T1, C-T4, P-T8 markers absent | restore `getattr(selected, "message")` (P-T1); keep `str(exc)` in the vault node (P-T7) |
| 2 | Cleanup missing on an exit path | Yes: reason computation could skip the Platform node write and terminal transition, or the CLI exit mapping and JSON emit | `failure_reason` and `_engine_family` swallow their errors and return `None` | P-T12, C-T9 | remove either `try`; compute `_cli_refusal_types()` at import (C-T9) |
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

- CLI: `except Exception` at `run.py:857`, single emit at `:1056` (gains `reason`); refusal arms `:1043-1053` keep `error_kind`; a `_engine_family` failure keeps exit and envelope (C-T9).
- Platform: failure tail `v2_orchestrator.py:299-318`, node write `v2_node_runs.py:434`; vault failure `v2_vault.py:90-99`.

### 13.2 Routes

- Platform unified V2 through `v2_orchestrator.py:289-396`: reason carried. Streaming (`v2_runner.py:269`), sequential, full-frame and host-safety tails: C0, named limit.
- Readers: `/node-runs` (`routes_runtime_facts.py:70`), `update_finished_manifest` (`assembly.py:2408`), legacy synthesis (`routes_evidence.py:190`); stored manifest (`routes_evidence.py:164-166`) served as stored.

### 13.3 FK shapes

- N/A: failure reporting only; no table, key or row relation changes.

### 13.4 Fields

- N/A: fields are stated in section 1.6 and section 10.

### 13.5 Output surfaces

- N/A: surfaces are stated in section 3 and section 10.

## 14. Revision 5 change map

| Finding | Class | Change | Section | Test |
|---|---|---|---|---|
| V1 (HIGH) CLI #78 `_ChunkedVaultError` missing from the pinned 15-entry literal | 9, 10 | baseline `b4f19ba`; `_usage_types()` 16 members; anchors refreshed | header, 1, 4, 5.1, 10, 13.1 | C-T8 `test_refusal_set_is_derived_from_exit_mapping`; C-T7 `[chunked_vault]` |
| V2-LOG (MEDIUM) `_VaultWriteError` would gain `decoy.unexpected_error` | 4 | RECOMMENDED: in `_cli_refusal_types()`, no `reason`, exit 3 | 3, 5.1 | C-T7 `test_cli_owned_failure_has_no_reason[vault_write]` |
| V2-LOG (LOW) C-T9 reload leaves stale classes; evaluation order unstated | 8, 2 | teardown reload; order stated | 4, 5.1 | C-T9 `test_classifier_failure_keeps_exit_and_envelope[commit_error]` |
