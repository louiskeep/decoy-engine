Status: handoff

Summary: F29's engine half is built; what V1 still owes is two consumers. The CLI `decoy run --json` failure envelope and the Platform failed-job node error should carry the engine's registered reason (identity, outcome, retry class), and the Platform node error should stop copying exception-carried `.message` text onto an owner-visible surface. Progress-event consumer wiring is cut from V1.

Baselines (`git rev-parse`, 2026-11-02): engine `origin/main` `0173c05` (registry 1.2.0, 893 entries counted with `reasons.manifest()` in the `uv sync --locked` venv, Python 3.11.18); Platform `origin/main` `d8908f42`; CLI `louiskeep/decoy` `origin/main` `a5d36b46` (read-only clone, shallow). Engine `feat/b6-vault-core` carries registry 1.3.0 (`src/decoy_engine/reasons/_registry.py:24`, `runtime/_part1.py`, `runtime/_part3.py`); nothing in this plan depends on it.

Risk: **R2**, plan only. The build slices change a public CLI JSON envelope and a Platform evidence-manifest model, both pre-GA. This document grants no build approval. Review order: Dennis, then Codex.

Rules consulted: 00-universal, development-loop, risk-and-exceptions, scope-discipline, reuse-first, testing, api-and-compatibility, security, architecture, documentation. **Not read in this session:** the dev-rules tree (`/home/cam/dev-rules/`) and the README routing table are not in this container (`ls /home/cam` fails; a filesystem search found no `dev-rules` directory). The list names the rulebooks prior plans in this repository cite for the same task shape (`docs/plans/2026-10-29-f29-machine-readable-errors-progress.md:11`, `docs/plans/2026-10-29-f22-atomic-idempotent-publication.md:13`). The routing-table selection is unverified. Repository guidance read: engine and Platform `CLAUDE.md`, CLI `run.py` and `exit_codes.py` docstrings.

# F29 remainder: consumer reasons for the CLI and Platform (FRAME + PLAN)

## 1. FRAME

### 1.1 What is already done (verified on current main)

| F29 clause (MASTER §F29, Platform `docs/plans/Market Expansion Specs/MASTER - Decoy Feature Specification.md`) | State | Evidence |
|---|---|---|
| One versioned reason registry, engine-owned | **Done** (engine #336 `fdd08a8`, unified in #349, 1.2.0 on main) | `src/decoy_engine/reasons/_registry.py:24`; `docs/reasons.md`; append-only pin `tests/fixtures/f29/reason-registry-release.json`, `tests/unit/test_reason_release_pin.py` |
| Retry-classification taxonomy and outcome categories | **Done** | `RetryClass` / `Outcome` per phase rule; `docs/reasons.md` "Retry classes", "Outcome categories". 40 rules map to `publication`, 23 to `verification`, 79 to `findings` (counted from `reasons.manifest()`) |
| Unknown exceptions receive a safe fallback | **Done** | `reason_from_exception` (`src/decoy_engine/reason_adapter.py:145`) returns `decoy.unexpected_error` / `unknown` for any non-engine class. Probe below |
| Safe record and reader for untrusted payloads | **Done** | `ReasonRecord.to_payload` (`reasons/_record.py:347`), `ReasonRecord.from_payload` (`:369`, recomputes severity, outcome and retry, degrades unknown identities), `.summary` (`:295`) |
| Versioned events with bytes, retry and checkpoint fields | **Done in the engine** | `_progress_events.py:238` (`bytes_done`), `:114` `RetryInfo`, `:153` `CheckpointInfo`, `:389` `RetryEvent`, `:404` `CheckpointEvent`; A6, A7, A10 in the build record |
| Consumer contract fixture | **Done** | `tests/fixtures/f29/consumer-contract.json` (`degraded_readings`, `reason_records`, `events`) |
| CLI uses the registry | **Partly** | `decoy ci` derives its rule-id allowlist from the registry: CLI `src/decoy/ci/rule_codes.py:93-103`. `decoy run` does not use it (below) |
| CLI maps reasons to exits; categories distinguishable | **Not done** | CLI `src/decoy/cli/run.py:861-1086`, below |
| Platform shows the same reason, with job and attempt state | **Not done** | Platform imports nothing from `decoy_engine.reasons` (`grep -rn "decoy_engine.reasons\|reason_from_exception" api` returns nothing); Platform pins engine `0.16.0` (`pyproject.toml:36`), which has no `reasons` package (`git show v0.16.0:src/decoy_engine/reasons/__init__.py` fails) |

The engine build record (`docs/records/2026-10-29-f29-engine-build.md`, "Open items") lists consumers C0, C1 and C2 as not started. Branch check (`git ls-remote --heads origin` on the engine, roadmap row F29 on Platform `docs/ROADMAP.md:320-323`): no open branch holds F29 consumer work. **F31** (`plan/f31-safe-output`, revision 3 at `465ca9e`, not approved) is the sanitizer behind F29 and owns free-text removal on CLI and Platform surfaces in general. Section 1.5 says where this plan stops so that F31 is not duplicated.

Probe (engine venv, `uv sync --locked`, `python -I`): `reason_from_exception(ExecutionError(code="out_of_core_insufficient_memory", message="SSN 123-45-6789"))` gives `runtime.out_of_core_insufficient_memory`, outcome `execution`, retry `environment_repair`, registry `1.2.0`; the marker is absent from `to_payload()`. An unregistered code gives `decoy.unregistered_reason`; `ValueError` gives `decoy.unexpected_error`; `ConfigError("...")` gives `config.untagged` / `configuration_repair`.

### 1.2 What F29 still owes V1 (the problem, re-derived)

**P-CLI. `decoy run --json` has no stable machine-readable failure reason.** The failure envelope is built at CLI `run.py:1040-1047` as `{command, status, config, mode, error}`, where `error` is `str(exc)[:500]` (`:1005-1007`). A machine code is added only for three CLI-chosen cases: capacity (`:1048-1050`, `_CAPACITY_CODES` at `:54`), native gate (`:1051-1053`) and missing extra (`:1054-1058`). An engine publication failure (`CommitError`, registered as `decoy.publication_failed`, `reason_adapter.py:95`), a missing secret, or a verifier outage all land in the generic arm: exit `EXIT_RUNTIME` 3 or `EXIT_USAGE` 1 (`:944-1000`) with prose only. CI cannot tell execution, publication and verification failures apart, and cannot read retry advice, without parsing text. That is the MASTER §F29 expectation "Execution failure, findings, verification failure, and publication failure remain distinguishable".

**P-PLAT-1. The failed-job node error has no reason, and its `message` is copied from the exception.** On a failed V2 job, `api/jobs/v2_orchestrator.py:286-287` calls `_failure_details(exc).report(...)`. `_failure_details` (`api/jobs/_partial_publication.py:174`) takes the first `.code` in the cause chain (any class, not only engine classes) and **`message = getattr(selected, "message", None)`** (`:201`). That pair is stored as JSON on `JobNodeRun.error` (`api/jobs/v2_node_runs.py:434`). It is returned verbatim by `GET /api/jobs/{id}/node-runs` (`api/jobs/routes_runtime_facts.py:70`, `JobNodeRunOut.error: str | None`, `api/jobs/schemas.py:431`) to the job owner, who is a non-admin user. It is also hydrated into the evidence manifest `nodes[].error` (`api/evidence/assembly.py:1974-1990`, `NodeError` at `api/evidence/schema.py:326-333`). Engine `.message` attributes are f-strings that can embed exception text. Examples on engine main: `generation/statistical/_spec.py:166` (`f"snapshot_file {shown!r} could not be read: {exc}"`, the `OSError` text, in ambient file-access mode) and `unmask.py:126` (`f"column ... : {exc}"`). F31's census counts 189 f-string `PlanCompileError` raises (F31 plan §T18 table, `PlanCheckError.message` row). This is **V1 bar rule 3** (`str(exc)`-derived text on a non-admin surface), defect **class 1** (value-bearing output) and **class 6** (an untrusted producer's `.code`/`.message` trusted by attribute shape).

**P-PLAT-2. `Job.log` cannot show the engine reason.** `api/persistence/error_projection.py` renders only Platform-owned codes from `APPROVED_ERROR_MESSAGES` (`:42-68`). Its docstring says engine codes are "deliberately absent", so an engine failure renders `internal error: <TypeName>` (`:141-146`). The text is safe, but the Platform shows less than the CLI will. MASTER: "Platform shows the same reason".

**Not owed for V1 (cut, section 5.3):** consumer wiring of progress events (Platform passes no `progress=` to `run_pipeline`, `api/jobs/v2_runner.py:242-270`; the CLI shows a spinner). Also not owed: a retry executor, new CLI exit integers, and reasons on CLI verbs other than `run`.

### 1.3 Definition of done

1. `decoy run --json` failure envelope carries `reason` (the engine `ReasonRecord.to_payload()` shape) derived from the engine exception, with exit integers unchanged. Named acceptance tests C-T1 to C-T6 pass.
2. A failed Platform V2 job's node error carries `reason` (same shape). Its `message` comes from a fixed source (registry summary, Platform approved message, or the existing type-name fallback), never from an exception attribute. The manifest re-reads the stored reason through `ReasonRecord.from_payload`. P-T1 to P-T6 pass.
3. `Job.log`'s terminal failure line for an engine-reasoned failure shows the registry summary and `error_class=<code>`. P-T7 and P-T8 pass.
4. `docs/reasons.md` gains a "Consumers" section naming the two surfaces and their limits. The F29 original plan's status line points here.

### 1.4 Reuse inventory

`reason_from_exception`, `ReasonRecord.to_payload` / `from_payload` / `.summary`, `REGISTRY_VERSION` (engine, public, in 0.17.0); `consumer-contract.json` as test vectors; CLI `emit_json` (`src/decoy/ui/output.py:85`) and the existing single failure `emit_json` call site (`run.py:1033-1059`); Platform `FailureDetails` / `_failure_details`, `_error_node_run`, `NodeError`, `_hydrate_nodes`, `TrustedFailure` / `project_failure`. No new module is needed in any repository.

### 1.5 Boundaries

- **Engine:** no production change. The registry, records and events are frozen inputs. If a consumer needs a code the registry lacks, that code is an engine registry addition, and that addition is out of this plan (owner question O3).
- **F31 owns** the CLI envelope's `error: str(exc)[:500]` text (`run.py:1005`), the CLI `print_exception` sites, and Platform except-bound text reads in general. This plan touches exactly one Platform text read, `_partial_publication.py:201`, because it is the line that builds the field this plan changes. F31 then has one fewer site. This plan does not delete the CLI `error` field.
- **Dependency:** the Platform slices need the Platform repin to engine `0.17.0`. That repin is in flight (handoff `p0-platform-repin-engine-0-17-0`, Platform `docs/ROADMAP.md:121`) and is not planned here. The CLI is already on 0.17.0 (`pyproject.toml:103`).
- **decoy-web:** the frontend parses the `Job.log` tail (`ProjectedFailure.tail`, `error_projection.py:112`). This plan adds no tail key, so the frontend is not touched.

## 2. Established solutions, searched first

Searches actually run: web, "Google AIP-193 ErrorInfo reason domain machine-readable error RetryInfo"; web, "RFC 9457 problem details type extension members machine-readable error JSON CLI"; in-codebase, `rg` for `reason_from_exception|decoy_engine.reasons|ReasonRecord|retry_class` across the Platform `api/` and the CLI `src/`, and `rg` for `error_kind`, `emit_json` and the exit-code module in the CLI.

Findings, and which source the design follows:

1. **Google AIP-193 `ErrorInfo`** ([aip.dev/193](https://AIP.dev/193), [Cloud API design: errors](https://cloud.google.com/apis/design/errors)): a stable `(reason, domain)` pair is the machine identity, separate from the human message. Services must not reuse a pair for different errors, and observed metadata keys stay present. The engine `(namespace, code)` already follows this. Consumers carry the pair unchanged; they do not re-derive it from text. **Followed.**
2. **RFC 9457 problem details** ([RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.pdf)): the type identifier is separate from `detail` prose, and unknown extension members are ignored by clients. This plan adds `reason` as an extension member of the existing CLI and Platform envelopes, so existing readers that ignore unknown keys are unaffected. **Followed** for the additive shape. The engine registry already cites RFC 9457 for identity versus occurrence (`reasons/_record.py:1-17`).
3. **AIP-193 `RetryInfo`** is a server-computed delay for the same request. The engine taxonomy is advice by category, not a delay. **Not adopted:** no consumer computes or honours a retry delay (non-goal).
4. **In-codebase:** CLI `decoy ci` already derives rule ids from the registry (`rule_codes.py:93-103`), and the F43 `SafeFinding` pattern encodes against a known-good set. The Platform's `TrustedFailure` and type-name fallback (`error_projection.py:84-96`, `_partial_publication.py:156-166`) already withhold exception text. This plan reuses both patterns and builds no new sanitizer or parser.

## 3. Guarantee, pinned to the V1 bar

The change promises only the following, as a ratchet with named limits:

1. **Function.** For a `decoy run --json` failure raised from engine code, the envelope carries `reason` whose `namespace`, `code`, `outcome` and `retry_class` equal what `reason_from_exception` returns for that exception under the CLI's pinned engine. For a failed Platform V2 job (the `v2_orchestrator` unified-pipeline failure tail), the node error carries the same `reason` for an engine exception. Exit integers and job status transitions are unchanged.
2. **Data integrity.** No output, commit order, staging or job status changes. The reason is computed after the failure is decided and is stored in the same write that stores the node error today.
3. **Nothing visible to a non-admin.** On the surfaces this plan changes (the CLI `reason` member, the Platform node error `message` and `reason`, the `Job.log` terminal body), the only text is registry-fixed summaries, Platform approved messages and closed tokens. A canary in an exception's message, args or `.message` attribute does not reach them in the named tests.

**Named limits:** CLI verbs other than `run`; the CLI `error` text field (F31); the Platform streaming, sequential and out-of-core failure tails that do not pass through `v2_orchestrator.py:274-381` (C0 inventory below lists what is and is not covered); Platform-coded failures, which keep their Platform code and get no `reason` (O3); failures before the engine is entered (Platform admission, CLI YAML parsing), which keep their current output.

**Non-goals:** a retry executor or automatic re-queue; new exit integers; progress-event wiring into the CLI or Platform; persisting attempt-level reason history; F31's alias and safe-event work; frontend changes.

## 4. Observable behavior, failure modes and acceptance tests (before implementation)

Tests are red-first against current main and drive the real boundary. The CLI tests use Typer's `CliRunner` or a subprocess over `decoy run --json` with the engine entry patched to raise. The Platform tests run the real `run_v2_pipeline_job` failure tail and read back through the HTTP route and the manifest assembler. `CANARY` is a unique marker string.

### CLI (repository `decoy`)

| Test | Behavior | Fail-before evidence |
|---|---|---|
| C-T1 `tests/e2e/test_run_reason_envelope.py::test_capacity_failure_carries_registry_reason` | `ExecutionError(code="out_of_core_insufficient_memory", message=CANARY)`: exit 5; `reason == {namespace: runtime, code: out_of_core_insufficient_memory, outcome: execution, retry_class: environment_repair, registry_version: <pinned>, ...}`; existing `error_kind`/`code` keys still present (pins `tests/e2e/test_run_capacity.py:87` behavior); `CANARY` absent from `json.dumps(payload["reason"])` | `run.py:1040-1047` builds no `reason` key |
| C-T2 `...::test_publication_failure_is_distinguishable` | engine `CommitError`: exit 3 (unchanged); `reason.namespace == "decoy"`, `reason.code == "publication_failed"`, `reason.outcome == "publication"` | as above; today indistinguishable from any runtime crash |
| C-T3 `...::test_config_failure_reason_says_configuration_repair` | `ConfigError(CANARY)`: exit 1; `reason.outcome == "configuration"`, `retry_class == "configuration_repair"` | as above |
| C-T4 `...::test_unknown_exception_gets_fallback_reason` | `ValueError(CANARY)`: exit 3; `reason.code == "unexpected_error"`, `retry_class == "unknown"`; canary absent from `reason` | as above |
| C-T5 `...::test_cli_owned_refusal_keeps_error_kind_without_reason` | a missing optional extra (ImportError translated by `translate_missing_extra`): `error_kind == "missing_extra"`, no `reason` key (O2 default) | locks the O2 default; red if a fallback `unexpected_error` reason is attached |
| C-T6 `...::test_reason_computed_from_original_engine_exception` | an engine `NerUnavailableError` rewritten by `translate_ner_unavailable` (`run.py:886-888`): `reason` is the engine record of the original exception | mutation target: computing the reason from the rewritten `exc` |

Failure modes covered: the reason derived after the CLI rewrites `exc` (C-T6); a forged or foreign `.code` (C-T4 with `ValueError` carrying `.code = CANARY`, plus a sub-case); a CLI refusal mislabelled `unexpected_error` (C-T5).

### Platform (repository `decoy-platform`)

| Test | Behavior | Fail-before evidence |
|---|---|---|
| P-T1 `tests/test_f29_node_error_reason.py::test_engine_failure_node_error_has_reason_and_fixed_message` | `run_pipeline` raises engine `StatisticalSpecError(code=plan.STATISTICAL_SNAPSHOT_UNREADABLE, message=CANARY)`; `GET /api/jobs/{id}/node-runs` as the owner: parsed `error.reason` is the registry record; `error.message == reason summary`; `CANARY` absent from the response body and from the assembled manifest | `_partial_publication.py:201` copies `.message`; `v2_node_runs.py:434` stores it |
| P-T2 `...::test_foreign_code_attribute_is_not_trusted` | a non-engine exception with `.code = CANARY` and `.message = CANARY`: stored code `v2_runner_unexpected_error`, reason `decoy.unexpected_error`, canary absent | `_coded` (`_partial_publication.py:169-171`) accepts any string `.code` |
| P-T3 `...::test_wrapped_engine_cause_reason_and_location` | `PartialPublicationError` wrapping an engine coded cause: `reason` from the cause, `where == "table=<t>"` unchanged | locks `_failure_details` chain walk |
| P-T4 `...::test_platform_coded_failure_keeps_code_and_static_message` | a Platform-coded failure (`out_of_core_budget_unresolved`): code unchanged, `message == APPROVED_ERROR_MESSAGES[code]`, no `reason` (O3 default) | today `message` is the exception's `.message` |
| P-T5 `...::test_stored_reason_reread_through_from_payload` | a `JobNodeRun.error` row whose stored `reason` has a forged `retry_class` and another with an unknown same-major code: manifest `nodes[].error.reason` shows the registry retry class and `decoy.unknown_reason_from_future` | `NodeError` (`schema.py:326`) is `extra="forbid"` and has no `reason` field |
| P-T6 `...::test_legacy_row_without_reason_still_hydrates` | a pre-change `{code, message, where}` row: manifest builds, `reason` null | additive-field regression guard |
| P-T7 `tests/test_obs3_leak.py::test_job_log_engine_failure_shows_registry_summary` (new case in the existing leak suite) | the P-T1 failure: the `Job.log` terminal line body equals the registry summary, the tail has `error_class=statistical_snapshot_unreadable`, `CANARY` absent | `error_projection.py:136-146` renders `internal error: StatisticalSpecError` |
| P-T8 `...::test_tampered_reason_on_trusted_failure_cannot_inject_text` | a `TrustedFailure` whose `reason` field is a non-`ReasonRecord` or carries text: generic rendering | mirrors the existing tamper test for `.code` |

Mutations to show red before push (one per new mechanism): C: compute the reason from the rewritten `exc` (C-T6), attach a reason to CLI refusals (C-T5). P: restore `message = getattr(selected, "message")` (P-T1), accept a `.code` from a non-engine class (P-T2), skip `from_payload` on read (P-T5), drop the summary branch in `project_failure` (P-T7).

## 5. Slices

| Slice | Repo | Production files | Est. changed lines (incl. tests) | Depends on | Gate |
|---|---|---|---|---|---|
| **C0** grounding record | engine (docs) | 0 (one record `docs/records/2026-11-0x-f29-c0-consumer-grounding.md`) | ~120 | — | the Platform failure-tail census below is written with file:line on the slice's merge base; review accepts it |
| **C1** CLI `run` reason member | `decoy` | 2: `src/decoy/cli/run.py`, `docs/cli-reference.md` (plus `CHANGELOG.md`) | ~300 | C0 | C-T1..C-T6 red then green on `requirements-dev.lock`; existing `tests/e2e/test_run_capacity.py`, `test_run_native.py` green; two mutations red |
| **P1** node error reason and fixed message | `decoy-platform` | 5: `api/jobs/_partial_publication.py`, `api/jobs/v2_orchestrator.py`, `api/jobs/v2_node_runs.py`, `api/evidence/schema.py`, `api/evidence/assembly.py` | ~550 | C0; Platform repin to 0.17.0 merged | P-T1..P-T6 red then green on the Platform lock; `tests/test_obs3_*`, evidence-manifest tests green; manifest golden fixtures regenerated only where `nodes[].error` appears, with a diff listed in the PR |
| **P2** `Job.log` registry summary | `decoy-platform` | 2: `api/persistence/error_projection.py`, `api/jobs/v2_orchestrator.py` (passes the record) | ~250 | P1 | P-T7, P-T8 red then green; existing `test_obs3_leak.py` unchanged and green |
| **E7** docs close-out | engine | 0 (`docs/reasons.md` "Consumers" section; status line of `docs/plans/2026-10-29-f29-machine-readable-errors-progress.md`; Platform `docs/ROADMAP.md` F29 row) | ~60 | C1, P2 merged | docs only |

All slices are within 10 production files and 800 lines. Order: C0, then C1 and P1 in parallel (different repositories, one writer each), then P2, then E7.

**C0 scope (the remaining grounding).** List each Platform path that marks a V2 job failed and say whether it reaches `v2_orchestrator.py:274` (covered) or not (named limit). Candidates seen but not traced in this planning session: `_run_v2_pipeline_streaming` (`api/jobs/v2_runner.py:273`), `api/jobs/v2_sequential.py`, the host-safety failure helpers (`api/jobs/queue_worker.py:419`, `:434`; `scheduler_claim_loop.py:173`), and `logger.step(error_class=...)` callers (`api/persistence/logger.py:179-190`). C0 also confirms the CLI has no second failure `emit_json` site in `run.py` (the docstring at `:1036` says there is one) and records `tests/e2e/test_run_capacity.py` as the envelope pin.

### 5.1 Slice C1 design (CLI)

In the `except Exception as exc` arm (`run.py:861`), bind `engine_exc = exc` **before** the `translate_missing_extra` and `translate_ner_unavailable` rewrites (`:877-888`). After `_exit_code` is decided, when the rewritten exception is not a CLI-owned refusal (`MissingExtraError`, `NativeGateError`, `DuckDBSourceError`, `UnsupportedCloudEndpointError`, the `_Chunked*` and `_Vault*` usage errors), set `payload["reason"] = reason_from_exception(engine_exc).to_payload()` at the single failure emit site (`:1040`). Exit mapping (`:931-1000`) is untouched. Human mode: no change in this slice (cut, below). `docs/cli-reference.md` documents `reason` as an additive member and states that `reason.outcome` distinguishes `execution`, `publication`, `verification` and `configuration`. Enforced post-validation findings stay `status: ok` with exit 4 and `report.post_validation_enforce` (`:1134-1172`); that is the existing findings-versus-enforced distinction and is documented, not changed.

### 5.2 Slices P1 and P2 design (Platform)

P1: `FailureDetails` gains `reason: ReasonRecord | None`. `_failure_details` keeps its chain walk and adds `reason_from_exception(selected)` when `selected` is not Platform-coded (`code not in APPROVED_ERROR_MESSAGES`). The `message` it returns is `reason.summary` when a reason exists, else `APPROVED_ERROR_MESSAGES[code]` when the code is Platform-approved, else the existing `"<Type>: see server log (job_id=...)"`. The `getattr(selected, "message")` read is deleted. For the code: a non-engine class's `.code` no longer selects the stored code. It falls to `v2_runner_unexpected_error`, and the engine's own `.code` trust table decides for engine classes. `_error_node_run` stores `{"code", "message", "where", "reason"}`. `NodeError` gains `reason: dict[str, Any] | None = None`. `_hydrate_nodes` passes a stored `reason` through `ReasonRecord.from_payload(...).to_payload()`, so a stored row is re-validated on read and never trusted as written. Pre-GA: manifest `SCHEMA_VERSION` follows the Platform's additive-field convention (`schema.py:626` comment: an additive optional field takes no bump). O4 asks the owner to confirm.

P2: `TrustedFailure` gains `reason: ReasonRecord | None`. `project_failure` checks `type(failure.reason) is ReasonRecord`. If so, it uses `reason.summary` as the body and the registry code as the `error_class` tail token, and Platform-approved codes keep precedence. The orchestrator failure tail (`v2_orchestrator.py:371-381`) passes `details.reason`. The server diagnostic line stays closed-token only.

### 5.3 What was cut, and why

- **Progress-event consumer wiring** (Platform SSE or job steps, CLI `--json` progress): the engine fields exist and are tested (A6, A7, A10). No consumer surface is broken without them, and wiring them is new product surface owned by F30 (telemetry) and F23 (checkpoints), which "wait on F31/F29 events" (Platform `docs/ROADMAP.md:377`). O1.
- **New exit integers** for publication and verification: `reason.outcome` makes the categories distinguishable without moving the pinned integers (`exit_codes.py` docstring: "the integers may not" move), and F34 has claimed exit 6 (F31 plan §F34 note). O2.
- **Reasons on other CLI verbs** (`validate`, `subset`, `scan`, `report`): `run` is the job runner CI gates on. The others keep their current envelopes.
- **Human-mode reason line** in the CLI: presentation only; not needed for machine readability.
- **Registering Platform-owned codes in the engine registry:** it needs an engine release; Platform codes already have fixed approved text. O3.
- **Retry policy driven by `retry_class`:** advice only, per `docs/reasons.md`.

## 6. Design notes

No new structure. One fact, one home: the reason identity and its outcome and retry class live in the engine registry. The CLI and Platform carry `to_payload()` and re-read stored copies with `from_payload()`, so neither keeps a second table (architecture rule: single source of truth for a contract; the change pain is the Platform's hand-kept `APPROVED_ERROR_MESSAGES`, which cannot grow with engine codes). Established pattern: AIP-193 `(reason, domain)` identity carried as an RFC 9457-style additive extension member.

## 7. Owner questions (each with a recommended default)

- **O1. Progress events in V1 consumers?** Default: **no**. F29's event-field clause is satisfied by the engine; consumer wiring rides F30 and F23.
- **O2. New exit integers for publication and verification failures?** Default: **no**. Exits stay 0 to 5; `reason.outcome` carries the category. CLI-owned refusals (missing extra, native gate, DuckDB source, unsupported cloud endpoint, chunked and vault usage errors) keep `error_kind` and get **no** `reason` rather than a misleading `decoy.unexpected_error`.
- **O3. Platform-owned failure codes in the engine registry?** Default: **no** for V1. Platform-coded failures keep their code and approved message, with `reason` null; revisit with the A-5 rename wave, where a platform-local code was already a Codex finding.
- **O4. Manifest schema version for the additive `nodes[].error.reason`?** Default: **no bump**, per the Platform's additive-field convention; pre-GA, no migration and no backward-compatibility shim. Existing stored rows hydrate with `reason` null (P-T6).
- **O5. Does F29 P1 own the `_partial_publication.py:201` text read, or does F31?** Default: **F29 P1 owns it**, because P1 rewrites that function. F31's Platform census loses one site, and the F31 plan is told at review.

## 8. V2-LOG (seen, not V1, no work planned)

- CLI `run.py:1005-1007` and the `--json` `error` field carry `str(exc)` truncated to 500 characters on the operator's own terminal and CI log; owned by F31's CLI entry slice. Class 1.
- CLI `run.py:1085` `print_exception()` under `--verbose` prints traceback locals to the operator; F31. Class 1.
- Engine `.message` f-strings embedding `{exc}` (`generation/statistical/_spec.py:166`, `:182`; `unmask.py:126`): in-process exception text, a V2 concern once no V1 surface copies it; F31 R7. Class 1.
- Platform `FailureDetails.report` fallback names the exception class in `nodes[].error.message` (`_partial_publication.py:162-166`); a class name, not a value. Class 1.
- Engine `reasons` summaries are mostly humanised codes ("Out of core insufficient memory."), terse but fixed (build record item 3); a patch-version editorial pass. Class 10.
- Platform pins engine `0.16.0` while the CLI pins `0.17.0` (`pyproject.toml:36` vs CLI `pyproject.toml:103`); the repin is in flight. Class 4.

## 9. What this plan did not verify

- The dev-rules files and routing table (absent from this container); the rules line is carried, not re-read.
- `plan-lint`: the house tool is not installed here. A PyPI package with that name exists, but it is an unrelated linter for LLM-agent JSON plans (`cirbuk/plan-lint` 0.0.3), so it was not run.
- No CLI or Platform test was run. The CLI checkout is a shallow read-only clone and the Platform lock venv is not installed. Platform and CLI anchors are read from source at the baselines above.
- The Platform failure tails outside `v2_orchestrator.py:274-381` (C0 records them).
- Whether `decoy-web` reads `JobNodeRunOut.error` as JSON or as text. P1 changes only the `message` value and adds a key; C0 should open the frontend reader.
