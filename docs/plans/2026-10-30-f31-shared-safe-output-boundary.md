Status: plan only, revision 8: fixes for the gates of revision 7 (`f1c12362`; Dennis GO-WITH-FIXES 1 V1, Codex NO-GO 1 V1). Revision 7 applied the cut variant and the F29-first amendment. No implementation or release approval is granted by this document.

Summary: F31 stops exception text and cell values from reaching surfaces a non-admin can see. The V1 core is one design (section 0): the `Job.log` choke point, API responses that copy exception text directly or through a callee, the pipeline validation response (closed at its one sink), the `error_type` label on F29's job error node, node warnings (closed at one projection), and the notification fields, each with a shrink-only ratchet and named limits. The alias, legend and run-UUID stack is cut (section 14). F31 adds no run identity: the scheduler's `jobs.claim_generation` is the one per-run token (section 3.7).

Repo and branch: decoy-engine `plan/f31-safe-output` (this file); the build slices land in decoy-engine and decoy-platform (section 11); no CLI slice.

Baselines (`git rev-parse`, 2026-11-02): Platform `origin/main` `0638a97d` (descends from `27304856`; includes #149 repin to engine 0.18.0, #150 F21 slice 1, #152 vault before output, #153 generation go-live, #154 B-10 S0); engine `origin/main` `492f35ac` (0.18.0 plus F34 E0, registry `1.3.0`; revision 8 checked `9eec6123`, which adds only a subset fix, #366, and moves no cited anchor); CLI `origin/main` `2577062` (read only for the one CLI citation in section 15); F29 plan `plan/f29-errors-progress-remainder` `a26994bf` (revision 5, Codex GO). Every revision 7 and 8 anchor below was read at these heads.

Measurement: stdlib `ast` and `grep` with the container Python 3.11.2 (revision 6 said 3.13.18; this box has 3.11.2), NOT the `uv.lock` interpreter; nothing imported from the targets. Evidence in `docs/plans/evidence/2026-11-02-f31-r7-fresh/`: `logassign-0638a97d.txt`, `httpexc_kinds-0638a97d.txt`, `census-0638a97d.txt` (revision 6 probe), `census-allow-0638a97d.txt` (allowlist probe `excbound_allow.py`), `excattrs-0638a97d.txt`, `httpexc_in_except-0638a97d.txt`, `selftest_allow-r6-probe.txt` (red), `selftest_allow-r7-probe.txt` (green), `README.md`. The revision 6 probes under `2026-11-01-f31-r3-census/`, `2026-11-01-f31-r4-anchors/` and `2026-11-02-f31-r6-callee/` were re-run unchanged; the revision 6 `selftest.py` still passes. Revision 8 adds `2026-11-03-f31-r8/` (`excbound_r8.py`, the allowlist probe plus `getattr`; its census, byte-identical under two `PYTHONHASHSEED` values; `selftest_r8.py`; `README.md`). No test suite was run.

Rules consulted: dev-rules `README.md`, `00-universal.md`, `development-loop.md`, `risk-and-exceptions.md`, `scope-discipline.md`, `reuse-first.md`, `security.md`, `api-and-compatibility.md`, `testing.md`, `refactoring.md`, `release-and-rollback.md`, `observability-and-resilience.md`, all read in full for revision 7 (`data-and-migrations.md` dropped: the cut removes both migrations). Also read: the plan contract and defect catalog (`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md` and the remote-worker template), `10_Projects/decoy/v1-bar.md`, `handoffs/OWNER-DECISIONS-2026-10-30.md` (F31 lines and the 2026-11-02 night sections), the archived revision 4 to 6 handoffs, `handoffs/phase1/PHASE1-TRACKS.md` (2.1 to 2.4, 3), `handoffs/phase1/storm/STORM-FILE-MAP.md`, the F29 plan sections 1.6, 3, 4, 5, 8, 10 and the F29 P1 handoff, the engine repin checklist, both repositories' `CLAUDE.md`.

# F31: Shared safe-output boundary, PLAN (revision 8)

Risk: R2 (wire contract, two-repository migration, a shared Platform module with F29). Owner decisions applied as written: three-class surface table with real schema names on operational output (owner names) and progress views as owner results; legacy `Job.log` rows withheld at read, never purged; O1 CUT (2026-11-02 night); one run token (`jobs.claim_generation`); F25 4c, F34, F36 rulings (section 9). The V1 bar (2026-11-02) decides what blocks the merge.

Naming: V1 rules are Rule 1 to Rule 3 (section 1). Slices are E (engine), P (Platform), X (cross-repository); ER is the engine release. Ratchets are R5 (Platform `Job.log` writes), `detail-exc` (Platform API details), `exc-copy` (other Platform except-bound copies, two probe passes, attribute allowlist), `validation-sink` (the validation response), `warning-shape` (node warnings) and `notify-closed` (notification text). The `node-error` sentry is F29's (F29 P1).

## 0. V1 core: one design

One idea: exception text and cell values never become user-visible bytes. Where copied, exception text becomes a closed label (`safe_error_type(exc)`) or a registered reason; free narrative becomes registered prose. One choke point and one ratchet per part (part (d), the isolated child, is V2-LOG; letters kept):

| Part | Choke point | What changes | Ratchet | Slices |
|---|---|---|---|---|
| (a) `Job.log` | `api/jobs/job_log.py` (`append`, `clear`, `read`), new | 28 `.log` assignments in 17 files write only `decoy-safe-output/v1` lines; four readers render through `read`; an unparsable line renders as a counted `legacy_withheld`; `JobLogger` keeps the engine protocol methods, text-free (3.3) | R5: no `.log` assignment outside `job_log.py`; 28, empty after P2b | P1, P2a, P2b (one release) |
| (b) API responses | the `except` handler, or the callee or HTTP mapper that builds the response | 50 non-admin `HTTPException` details (10 more are `require_admin`, V2); 6 except-bound copies (P4d); 2 lane and 2 HTTP refusal mappers (P4e) (3.4) | `detail-exc` (60, 10 admin rows left); `exc-copy` (128 classified rows under the allowlist with `getattr`, unclassified fails) | P4, P4a to P4e |
| (c) Validation response | `result_to_response` (`api/pipelines/validation_messages.py:92-124`), the one sink for both callers | the 422 and validate bodies render `message` and `detail` from the code (registry prose), never `ValidationMessage.message`; `hint` passes `approve_ref`; covers all 10 `add_error` sites (5.7) | `validation-sink` (no `.message` read of a validation entry in `api/`; callers enumerated) | P7 |
| (e) Job error node | F29's `api/jobs/job_node_error.py` (F29 P1) | F31 adds only F29 section 1.6 item 9: the `error_type` keyword and key, the `NodeError.error_type` field, and the `platform.job_failed` fallback prose (3.8) | F29's `node-error` (at 0 from F29 P1) | P8 |
| (f) Node warnings | `api/jobs/node_warning.py:project_warning` (new), at both writers and all three readers | each warning keeps `code`, `severity`, a re-rendered `message` and a `hint` holding only allowlisted detail keys of closed type; every other key is dropped and counted (3.11) | `warning-shape`: no `warnings=` write or read in `api/` bypasses `project_warning` | P9 |
| Notifications | `make_job_failed_event`, `make_node_flagged_event`, `make_flag_gate_event` (`api/notifications/dispatcher.py:187-213`, `:369-405`, `:330-365`) | `error_summary`, `log_message` and `reason` stop carrying free text into `detail` and the webhook payload (3.6) | `notify-closed`: a `str` is rejected | P5n |

Shared engine pieces: `SafeEvent`, serializer and parser (E0a, E0b, 3.1), `safe_error_type` (3.2), the registry `platform` and `safe_output` namespaces (E0c, added through F29's registry rules, 3.9; Platform has no registry, owner decision).

**Blocks V1** (the merge gate for F31's V1 claim): E0a, E0b, E0c, ER, P1, P2a, P2b, P4, P4a, P4b, P4c, P4d, P4e, P5n, P7, P8, P9, X. F29 P1 is a dependency of P8, not an F31 slice.

**Cut** (owner, 2026-11-02 night; section 14): S-E3a, S-E3b, S-E4, S-P0, S-P3a, S-P3b, `SafeEvent.run` and `aliases`, the run-stamping rule, `from_line`'s run argument, tests T9, T11, T12, T15, T16, T22, the `job_alias_legend` table, the `run_uuid` column and both migrations.

**Is V2** (section 15, no work planned): the R7 ratchet (E1), the 10 `require_admin` details (O4), the R1 and R2 drains, entry-frame scrubbing (`closed_entry`, F1, F2), audit details, census machinery (T0, surface JSON, slice manifest), metrics labels (P5), child stdio (P6), CLI stderr (C1, C2), the free-text fields with no non-admin reader (section 15), the isolated child's error text (E6), the engine `ValidationMessage.message` field itself (in-process only after P7), census rows that need no V1 work, and in-process `raise` messages.

## 1. Guarantee (V1 rules 1 to 3, ratchet plus named limits)

**Threat model (V1).** Self-hosted, single-org; the engine process is trusted; the leak is accidental (a value or path in exception text copied into output, or a free-text log line). The audience that matters is a viewer who is not the server admin. Hostile in-process code is V2.

**Rule 1 (product function).** F31 changes no masked, subset or generated output and no route selection. `Job.log` stays readable through its four readers: a v1 line renders as `ISO_TS  LEVEL  STEP  MSG`; each run of lines that do not parse renders as one counted `legacy_withheld` line, never raw and never an error. Named limit: legacy rows and unmigrated writers show as `legacy_withheld` until P2b ships, so P1, P2a and P2b ship in one Platform release (3.3). `JobLogger` keeps satisfying the engine `Logger` protocol (3.3). The validation response keeps its status codes, keys, `code`, `path`, `node_id` and an approved `hint`; only the `message` text changes (named limit: schema errors lose pydantic's free `msg`, the `hint` keeps pydantic's closed error type).

**Rule 2 (data).** No F31 write can abort a job's terminal record. `job_log.platform_event` and `job_log.append` are total: any validation failure while building the event (an invalid count, a step `approve_name` refuses, an unknown reason) yields the registered `safe_output.invalid` event for that `seq`, and `append` writes it; neither raises (3.3). `append` writes in the caller's transaction and adds no failure point beyond today's string append. Legacy `Job.log` and error-node rows are never rewritten or purged. F31 adds no table, column or migration.

**Rule 3 (values a non-admin can see).** No exception text and no cell value appear on: stored `Job.log` and its four readers (SSE, job `log` field, node-run logs, STORM failure summary); the 50 non-admin `HTTPException` details, the 6 P4d responses (validate, workflow summary, iteration preview, both database health routes) and the P4e refusals (job and node preview, retrieval page, the latter before authentication) of part (b); the validation response of part (c); the `error_type` key F31 adds to F29's error node; node warnings (`nodes[].warnings`, `/node-runs` `warnings`, the report's known limitations); the `job_failed`, `node_flagged` and `flag_gate` notification `detail` and payload. Ratchets: R5, `detail-exc`, `exc-copy`, `validation-sink`, `warning-shape`, `notify-closed`, each shrink-only with a seeded red fixture (section 6). Named limits:

- `exc-copy` ratchets exception text reaching a response outside `HTTPException` (3.4): its sentry runs both probe passes over `api/` and FAILS on any row not in the classified baseline, which only shrinks. At `0638a97d`: 128 rows (56 pass 1, 72 pass 2) under the attribute allowlist with `getattr`, classified (`2026-11-02-f31-r7-fresh/README.md`, `2026-11-03-f31-r8/README.md`). Named limits: (1) callees are followed to module-level `def`s in `api/` (same module or `from api.x import`), depth 3; a method or dynamic call receiving the exception bare is an `unres` row, not followed; (2) a store into a container (the lane registry) is not traced to its read, so the READ is the row; (3) **attributes are an allowlist** (revision 7): a read of any attribute of a caught exception in a copy position is a text read unless the attribute is one of `code`, `errno`, `status_code`, `transient`, `cap`, each value-free by its producers' types at `0638a97d` (`excattrs-0638a97d.txt`); a new attribute needs a reviewed allowlist entry with its producer cited; `getattr(exc, "x"[, default])` is the same read, and a non-literal name is always a text read (revision 8); (4) a re-wrapping `raise` is not a row, its text is one where it is caught and copied; (5) the classification rule below.
- **Classification rule (revision 7, rule 5).** A row whose sink is a non-admin response is V1 unless every producer of its text is cited and builds it only from: registered codes and closed tokens; integers (counts, caps, ids, HTTP statuses, errnos); exception type names (a V2-LOG shape, not V1); fixed prose; and **owner names**. Owner names are exactly: a table, column, node, relationship or variable (scope and key) name, or a config field path joined from them and from list indices (pydantic `loc`, `tables.<t>.columns.<c>...`), taken from the pipeline config or schema the owner wrote; the NAME (never the value) of a resource binding, environment variable or secret the owner declared; plus the integer id of the owner's own stored object (a layout id, an environment id). Owner names are not leaks: the owner decision puts real schema names on operational output and progress views. NOT owner names: file and directory paths, hostnames, user names, bucket and object keys, secret refs, connection strings, URLs, config literal values (an iteration `mode`, loop bounds, a `provider_config` value, a `when` literal, a provider `locale`, a nested `target` JSONPath, an FPE `join_group`), per-row positions, cell values and exception text. "Owner-only" and "the response already carries it" are not reasons.
- The 10 `HTTPException` details on `require_admin` routes (`admin/audit_redaction_router.py:88`, `:92`; `admin/router.py:896`, `:996`; `seeds/admin_router.py:277`, `:299`, `:307`, `:471`, `:493`, `:498`) stay in the `detail-exc` baseline as V2 (O4).
- Engine exception-text projections outside part (c) are not ratcheted (R7 is V2-LOG); they reach engine results with no Platform non-admin reader at `0638a97d` (O5), the isolated parent, or admin logs. `QualityWarning` is not among them: it has owner-visible readers and is closed by P9 (3.11).
- Exceptions raised to an in-process SDK caller keep their message (O2). F31 stops that text where Platform copies it into `Job.log` (a), a response (b), the validation response (c) or the error node (F29 P1 plus P8); `exc-copy` catches a new copy.
- Admin-only surfaces (server logs, `require_admin` audit details at `api/audit/router.py:159`, traceback locals, `__cause__` chains, metrics, child stdio, CLI stderr) are V2.
- Traceback serialization: `grep` at both mains finds no traceback formatter (`format_exc`, `print_exc`, `format_exception`, `TracebackException`, `extract_tb`, `format_tb`) that writes to a non-admin surface in `api/` or `src/`. The grep is the check (re-run at P1's and X's base).

**In-process exception boundary.** An exception raised to an SDK caller is not a serialized surface (the caller wrote the input); Rule 3 starts where its text is copied into a result field, stored line, response body or notification.

Non-goals: no new encryption or hashing; regex scrubbing (`api/logging/redaction.py`) stays a backstop; no `--unsafe-debug-log`; no run identity, alias or legend (cut).

## 2. Established solutions and design notes

Established solutions (cited in module docstrings): RFC 9457 identity versus occurrence (F29, `src/decoy_engine/reasons/_record.py:1-17`); AIP-193 `ErrorInfo` (F29 section 2); OpenTelemetry `error.type` (`src/decoy_engine/reason_adapter.py:1-7`); OWASP Logging Cheat Sheet (log the event, never the data); python-json-logger's single exception field (`api/logging/exc_safe.py:1-18`); "Parse, don't validate" for stored lines and rows; the Platform precedent for a pydantic error summary that keeps `loc` and `type` and drops `msg` (`api/environments/bindings.py:276-290`). Custom code: only the `decoy-safe-output/v1` record.

Design notes: engine package `decoy_engine.safe_output` (`_event.py`, `_exception.py`, `_wire.py`); Platform module `api/jobs/job_log.py`; F29's `api/jobs/job_node_error.py` is extended, not created. One place per fact: prose in the registry, the exception table in `_exception.py`, parsing in `_wire.from_line`, `Job.log` writes in `job_log.py`, validation text in `result_to_response`, approval of names in F29's `approve_ref` and `approve_name` (unchanged). No flag arguments.

## 3. Design

### 3.1 `SafeEvent` and its wire form (E0a, E0b)

Kept unchanged: `ReasonRecord`/`ReasonContext` (`src/decoy_engine/reasons/_record.py:115-260`), the progress projections `event_to_dict`, `event_to_legacy_dict` (`src/decoy_engine/_progress_events.py:581-599`), the `decoy-reasons/v1` identity form (`src/decoy_engine/reasons/_identity.py:27-57`).

`SafeEvent` (`safe_output/_event.py`): frozen, validated in `__post_init__` and `to_dict`, exact-type checks on nested groups as `_V1Fields` does (`_progress_events.py:231`). Fields: `seq`, `ts_ms`, `sev: reasons.Severity`, `kind: EventKind` (`NARRATIVE`, `STEP`, `TERMINAL`, `WITHHELD`, `INVALID`), `reason: ReasonRef` (must pass `require_known`, `_identity.py:57`), `err: SafeErrorType | None`, `step: StepRef | None` (a node id; `StepRef` is built only from a value that passes F29's `approve_name`, `_record.py:83-97`, else `None`), `counts` under `COUNT_KEYS` (`_record.py:34-36`, additive), `flags: frozenset[Flag]`. No free `str`, `Any` or mapping field. No run field and no alias field (cut).

Serializers (`safe_output/_wire.py`):

- `to_line(event) -> bytes`: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)` plus `\n`, at most 4096 bytes and depth 3. Over a limit it returns the registered `safe_output.invalid` event for the same `seq` with a closed `cause` count, never a truncation; if that raises, the import-time constant `_FAILED_LINE = b'{"kind":"failed","s":"decoy-safe-output/v1"}\n'`. Never raises.
- `from_line(line) -> SafeEvent | None`: rejects over 4097 bytes before parsing; bounded parse rejecting depth over 3 and duplicate keys; closed keys; fields re-checked; reason known via `require_known`. Anything else, `_FAILED_LINE` included, is `None`.
- `render_text(event) -> str`: `ISO_TS  LEVEL  STEP  MSG` with two-space separators, as `JobLogger` writes today (`api/persistence/logger.py:64-69`, `:442-444`). LEVEL is exactly `DEBUG`, `INFO ` (padded), `WARN ` (`Severity.WARNING` renders `WARN`, padded) or `ERROR`; STEP is the step or the sentinel `job` when `step is None` (the value `_split_log_by_node` drops, `api/jobs/routes_runtime_facts.py:45-66`). MSG is the registry summary plus closed facts; an `ERROR` line ends with `(error_class=<namespace>.<code>, node_id=<step>)`, the tail `parseLogLine` reads (`web/src/pipelines/hifi/reporting/logParsing.ts:21-46`). Named loss: the `request_id=` and `contact support` tail of today's error lines (`logger.py:172`, `:200`) is not carried; the server log keeps the request id, correlated by `Job.id`.

### 3.2 Exception label (E0a)

`SafeErrorType` is a closed `str` enum built once at import from exact classes: builtin exception classes (as `_safe_error_names` enumerates them, `_progress_events.py:69-81`), the engine families in `reason_adapter._CODED` (`reason_adapter.py:39-85`), the classes of `decoy_engine.errors`, plus `EXCEPTION`. `safe_error_type(exc)` is `_ALLOWED_EXCEPTION_TYPES.get(type(exc), SafeErrorType.EXCEPTION)`; `SafeErrorType.from_label(s)` maps an unknown string to `EXCEPTION`. `phase.error_result` (`phase.py:574-594`, table `_ERROR_KINDS` at `:555`) delegates; its value domain widens additively (section 7). Wider migration (`postgres_snapshot`, `progress.run_scope`) is admin-log only: V2-LOG.

### 3.3 `Job.log` choke point (P1, P2a, P2b)

`Job.log` (`api/models.py:424`) keeps its `Text` column and, from P1, holds only v1 lines written by `job_log.append(db, job, event)`; resets become `job_log.clear(db, job)`. Platform builds events with `job_log.platform_event(reason, *, sev, err=None, step=None, counts=())`, `reason` any registered `ReasonRef` (the engine's `platform` namespace from E0c, or an engine reason F29 P2 passes for an engine-reasoned failure). `platform_event` is total: it wraps the `SafeEvent` constructor (which validates in `__post_init__` and can raise) in `try/except Exception` and on any failure returns the `safe_output.invalid` event with a closed `cause` count; `append` does the same around `to_line`. The terminal writers (`api/jobs/job_terminal_cas.py:251-257`, `api/jobs/runner.py:669`, `api/jobs/reaper.py:233`, `api/main.py:1071`, `:1079`) therefore cannot fail on event content. `job_log.read(job)` parses with `from_line` and renders with `render_text`; each run of failing lines is one counted `safe_output.legacy_withheld` line. The four readers call it: SSE (`api/jobs/routes_streaming.py:92-101`, `data: <text>` kept for `web/src/hooks/useLiveJob.ts:215`), the job `log` field (`api/jobs/_shared.py:203-211`, 256 KiB tail cap), node runs (`api/jobs/routes_runtime_facts.py:96`, grouped by `step`), the STORM failure summary (`api/storm/router.py:1707`).

Writers (5.6) move to `append` in P2a and P2b (registered prose plus closed facts); a writer appending a caller's `reason: str` drops it (`_fail_host_safety`, `api/jobs/queue_worker.py:378-386`, fed by six census rows, e.g. `api/jobs/worker_budget.py:402`). In P2a `JobLogger._append_narrative` (`api/persistence/logger.py:426-460`) writes a `NARRATIVE` event (`platform.job_narrative`, level, step) and drops the message; after P2a `_evaluate_flags` (`logger.py:475-512`) receives the rendered MSG of that event, never the caller's text (P5n then removes the text parameter, 3.6); signatures are unchanged, so the constructors (`api/jobs/route_narrative.py:223`, `api/jobs/v2_orchestrator.py:392`, `api/storm/router.py:1031`) and the engine handoff are untouched.

`JobLogger` is also an engine logger: the `Logger` protocol (`src/decoy_engine/context.py:25-46`, `runtime_checkable`) requires `debug`, `info`, `warning`, `error`; STORM passes `JobLogger` as `ExecutionContext(logger=...)` (`api/storm/router.py:1169`) and the profiler calls `logger.info(...)` (`src/decoy_engine/profiling/profiler.py:494`). P2b KEEPS the four methods, text-free as in P2a, and migrates every Platform CALL of them to `job_log.append`; an AST sentry (T-P2r) then forbids a Platform call. V2 jobs pass no logger to `run_pipeline` at `0638a97d` (`api/jobs/v2_runner.py:243`); the V2 gate cell uses `JobLogger` as `ctx.logger` anyway.

Release unit: from P1 an unmigrated writer's line renders `legacy_withheld`, so no Platform release between P1 and P2b; P2b's gate runs T-P1 and T-P2 on the merged result. Rule 2: `append` is a string append in the caller's transaction (as at `api/jobs/runner.py:66-68`, `api/jobs/job_terminal_cas.py:256`); `to_line` always returns bytes, so `append` cannot raise on content. Under the scheduler's fenced writer (when S9a-2 lands), `append` runs inside the caller's fenced transaction; F31 adds no fence of its own (3.7).

### 3.4 API responses (P4, P4a to P4e)

At `0638a97d`, 66 `HTTPException` calls inside `except ... as NAME` read `NAME` in their detail (`httpexc_kinds-0638a97d.txt`): 60 copy its text in 27 files, 5 read only `NAME.code`, 1 only `type(NAME).__name__` (V2-LOG). P4 lands `detail-exc` (tests only, baseline 60, seeded fixture); P4a to P4c rewrite the 50 non-admin sites to constant prose plus `safe_error_type(NAME)`, keeping status codes and shapes; the 10 `require_admin` sites stay (O4). Four of the 50 are new since revision 6 (#143, the 0.17.0 repin): `jobs/_service.py:237`, `jobs/routes_preview.py:163`, `pipelines/router.py:1384`, `unmask/router.py:133`. Each copies `MaskSecretConfigError.message`, which is a constant per code (`api/keys/mask_secret_errors.py:15-45`); the rewrite reads `MESSAGES[exc.code]` so the body bytes stay identical and the ratchet still drains.

`exc-copy`: pass 1 is revision 5's probe (56 rows); pass 2 adds an exception passed to a callee, read through an attribute, or stored (72 rows under the allowlist with `getattr`; 54 in revision 7, 31 under revision 6's list). P4d lands the sentry on P4's matcher, keyed by file, enclosing function and kind with a count, with the allowlist of section 1.

P4d rewrites to registered prose plus the label: the validate route's preflight catch (`api/pipelines/router.py:721-733`, census row `:725`); `api/workflows/runner.py:225`, `:238` (`WorkflowRun.summary`; a PyYAML marked error quotes the config line); the iteration preview `api/pipelines/router.py:479`, `:484` (`IterationPreviewOut.error`; the error branches return `values=[]` and the text carries the stored `mode` and loop bounds, `api/jobs/iteration.py:116`, `:162`); and `check_database` (`api/health_check.py:48-72`): `error` becomes label plus prose for unauthenticated `/healthz/db` (`api/main.py:223`) and `release_manager` `/server-logs/services` (`api/server_logs/router.py:50-104`); the driver text goes to the server log.

P4e closes the refusals at the HTTP mappers every producer passes: `unavailable_http_status_and_detail` (`api/jobs/preview_http.py:47-52`; job preview `api/jobs/routes_preview.py:164`, node preview `api/pipelines/router.py:1385`) and `unavailable_status_and_detail` and `status_and_detail` (`api/retrieval/retrieval_http.py:96-103`, `:106-118`, `:137`; `api/retrieval/router.py:268` runs before authentication) render `message` from the closed code, never the error's text. `PreviewUnavailableError` (`api/jobs/preview_slot.py:54`) and `RetrievalUnavailableError` (`api/retrieval/retrieval_slot.py:72`) gain a `code` attribute holding the same closed value as `reason`, and the mappers read only `exc.code` (allowlisted), so the six `exc-copy` rows at the four P4e sites leave the baseline. That stops the startup disable reason (`api/supervision/startup.py:370`, `:390`, `:406`, filesystem paths), the source path of `api/jobs/preview_sample.py:403-405` and the pyarrow text of `api/retrieval/decode_child.py:1940`, `:2137`, `:2176`. Both `_map_disabled_error` (`api/jobs/preview_slot.py:112-123`, `api/retrieval/retrieval_slot.py:126-135`) stop interpolating `exc.reason`, as `api/generation/generation_slot.py:77-78` already does.

**Sink re-frame for part (b) (Dennis, revision 6 review).** Adopted where one sink exists: the validation response (3.5, replacing E5-4), the two refusal mappers (P4e), F29's error-node reader, node warnings (3.11), `check_database`. Rejected for `HTTPException` details: a global handler would also have to wrap the 57 constant details raised inside `except` blocks (123 in-`except` calls, 66 read the bound name, `httpexc_in_except-0638a97d.txt`), and the P4d and P4e rows are not `HTTPException` details; that redesign is owner question O6, still unanswered. Split rule adopted: a gate finding of a part (b) flow neither probe pass sees (a new row class) goes to a follow-on plan F31b in the V1 set; a new row of an existing class fails the sentry at build time.

### 3.5 Validation response (P7)

`ValidationMessage.message` and `.hint` (`src/decoy_engine/validation_result.py:68`, `:71`) are filled by Platform at 10 `add_error` sites in `api/pipelines/v2_validation.py` (5.7; revision 6 named 1 of the 8 that existed at `d8908f42`) and copied into the response by `result_to_response` (`api/pipelines/validation_messages.py:92-124`, `"message": m.message` at `:113`, `"detail": primary.message`). It has exactly two callers: job create (`api/jobs/_create_phases.py:346-350`) and validate (`api/pipelines/router.py:664`, `:694`). The engine never calls `add_error` itself (`grep` at `492f35ac`: only the definitions at `validation_result.py:99`, `:119`).

P7 closes the part at that sink: `result_to_response` builds each entry from `code` and never reads `m.message`. `message` (and the top-level `detail`) is `validation_prose(code)`: the `platform` namespace summary when `code` is registered there (E0c registers the 12 Platform validation codes, 3.9, keeping today's constant wording where today's text is constant); else the summary of the single engine spec `reasons.lookup(code)` returns (the `PlanCompileError` codes of `:155` are `plan` reasons); else `platform.validation_failed`. `hint` is `approve_ref(m.hint)`: the strategy names (`:159`, `:179`) and pydantic error types (`:316`) pass, anything else becomes `None`. `code`, `path` (owner names, rule 5) and `node_id` are unchanged. P7 also deletes `adapt_validation_message` (`v2_validation.py:338-347`), which copies `message.message` and has no caller in `api/` or `web/`.

Why the sink and not the engine type change (revision 6's E5-4): it covers the 10 sites and any future one in one place; it needs no pre-GA break of `ValidationResult.add_error`, so the platform repin to the F31 release is additive and P7 is not coupled to it; and the CLI's own `_ValidationMessage` (`decoy/src/decoy/cli/validate.py:116`) is untouched either way. E5-4 moves to V2-LOG (O6).

V2-LOG since revision 5 (no non-admin reader at `0638a97d`, O5): E5-1 (`CheckResult.detail`), the `CapacityEstimate`/`PlanCheckError` half of E5-4, E6 (the child's text is a class name, `_isolated_worker.py:47-49`; where `_governor.py:550` raises it, parts (a), (b) and the error node stop the copy).

### 3.6 Notifications (P5n)

`make_job_failed_event` (`api/notifications/dispatcher.py:187-213`) appends `error_summary` to the notification `detail` that reaches Slack, email and webhooks. No job path passes it (`api/jobs/runner.py:428-432`); the test-fire passes a constant (`api/notifications/router.py:498`). Nothing leaks now; nothing stops a future leak (class 4). Fix: `error_summary: FailureLabel | None`, a closed enum (`test_fire`, plus the labels a future caller needs, added through review); `detail` renders the label's fixed prose from the `platform` namespace.

Same file, same slice: `make_node_flagged_event` (`dispatcher.py:369-405`) appends `log_message`, the narrative line text from `JobLogger._evaluate_flags` (`api/persistence/logger.py:493-502`), to `detail` and the webhook payload (`:391`, `:405`); `make_flag_gate_event` (`:330-365`) does the same with `reason: str | None` (`:354-355`, `:365`; no caller at `0638a97d`). P5n removes `log_message` (detail renders fixed prose naming the severity and the node id, an owner name) and types `reason` as a closed `GateReason | None`. The payload keys stay; their values become closed. P5n edits the one `_evaluate_flags` call site, so it follows P2b.

### 3.7 Run identity: none (one run token)

Revision 6 section 3.7 defined a Platform-minted run UUID, a per-job scope, aliases and a legend. All of it is cut. F31 defines no run identity, no ordinal and no second per-run token. The single per-run token is the scheduler's `jobs.claim_generation` (owner, 2026-11-02 night; scheduler plan `docs/plans/2026-11-02-scheduler-direction.md:119` on `plan/scheduler-state-and-direction`, not on Platform main at `0638a97d`). F36 and F35 consume it through the scheduler's fenced writer helper (`Fence`, `fenced_side_write`, PHASE1-TRACKS section 3). A `Job.log` line is bound to its job by the row it is stored in (`Job.id`); forging a line needs database write access, outside the V1 threat model. `SafeEvent` carries no run field, and F30 and F32 key by `Job.id` (section 9).

### 3.8 Job error node (P8, after F29 P1)

F29 P1 owns the error-node fix (owner ruling 2026-11-02; F29 plan section 1.6): it creates `api/jobs/job_node_error.py` (`node_error`, `node_message`, `project_node_error`, `failure_reason`), drops `FailureDetails.message` and the `.message` read at `api/jobs/_partial_publication.py:201`, routes `_error_node_run` (`api/jobs/v2_node_runs.py:434`) and both readers (`/node-runs`, `api/jobs/routes_runtime_facts.py:71-115`; manifest `_hydrate_nodes`, `api/evidence/assembly.py:1888`, raw copy at `:1984`) through it, and lands the `node-error` sentry at 0. F31 builds none of that.

P8 is F29 section 1.6 item 9 and nothing else: `node_error` gains one keyword-only `error_type: SafeErrorType | None = None` and one `error_type` key in its output; `project_node_error` re-projects that key via `SafeErrorType.from_label`; `NodeError` (`api/evidence/schema.py:328`, `_Strict` at `:57`) gains `error_type: str | None = None`; the caller passes `error_type=safe_error_type(details.selected)`; `node_message` branch 3 (the fallback) becomes the `platform.job_failed` prose. Approved text and `reason.summary` keep precedence. P8 updates the strings pinned for the fallback (`tests/test_p6_failure_trace_baseline.py:161` and F29's P-T6 and P-T7 cells) and F29's P-T10 key assertion, as F29 V2-LOG assigns to F31.

Changed since F29's plan was written: #152 (`d058f361`) removed the vault node error write. `api/jobs/v2_vault.py:269-279` now raises `VaultPersistError()` with a fixed message (`:74-91`) and writes no error row, so the `v2_vault.py:98` anchor in the F29 P1 handoff is gone and the vault failure reaches the node through the orchestrator walk (code `vault_write_failed`). F29 P1 must re-anchor; P8 extends whichever vault cell F29 P1 lands.

### 3.9 Registry additions through F29's rules (E0c)

The registry is F29's (MASTER section F29; `docs/reasons.md:21`). At `492f35ac` the live `REGISTRY_VERSION` is `1.3.0` (`src/decoy_engine/reasons/_registry.py:24`, set by F34 E0, #365) and the release pin `tests/fixtures/f29/reason-registry-release.json` records `1.2.0` (engine 0.18.0, 893 identities). E0c is step R4 of the strictly serial registry chain (PHASE1-TRACKS 2.1: R0 F34 E0 merged; R1 STORM E1 and R2 F35 E1a reuse the unreleased minor; R3 F32 E1b and R4 F31 E0c and R5 F36 4 take the next minor). E0c opens once R1 (STORM E1) has merged and no registry slice is open; it does not wait for the parked R2 and R3. Procedure, in this order:

1. Base check: merge `origin/main`; read the live `REGISTRY_VERSION` (`L`) and the release pin's `registry_version` (`P`). Stop if a registry slice is open on another branch.
2. Version: `V` is the next minor after `L` (expected `1.4.0` while R2 and R3 stay parked; `1.5.0` if F32 E1b merged first). Set `REGISTRY_VERSION = V`. If the owner settles PHASE1-TRACKS 6.3 for reuse instead, and `L > P`, `V = L` and no bump (O8).
3. Declare: new packages `reasons/platform/` and `reasons/safe_output/` (`__init__.py`, `_part1.py` each), a profile per namespace in `reasons/_catalog.py`, the two imports in `reasons/__init__.py:15-30`. Every new identity declares the literal `since="V"`.
4. Fixtures: regenerate `tests/fixtures/f29/consumer-contract.json` in its `registry_version` strings only (as F34 E0 did). Never edit `tests/fixtures/f29/reason-registry-release.json` or `tests/fixtures/f29/reason-manifest-v1.json`: the release pin is re-captured only by the release (`scripts/release/reason_release_pin.py --write`, run at ER by the release owner).
5. Docs: `docs/reasons.md` names the two namespaces and the new `registry_version`.
6. Gates: `tests/unit/test_reason_registry.py` (a new identity needs the minor bump, `:151`), `tests/unit/test_reason_release_pin.py` (no new identity may claim a released `since`), `tests/sentry/test_reason_registry.py`, `tests/unit/progress/test_consumer_contract.py`.
7. The chain is serial, so no other registry slice can merge while E0c is open. If `origin/main`'s `REGISTRY_VERSION` differs from `L` at merge time, the chain was broken: stop and report, do not re-bump.

Contents: `safe_output.invalid`, `safe_output.legacy_withheld`; `platform.job_narrative`, `platform.job_failed`, one reason per `Job.log` writer group (5.6), the P4d and P4e prose, the notification labels, `platform.validation_failed` and the 12 Platform validation codes of 5.7 (`yaml_empty`, `yaml_unparseable`, `yaml_not_a_mapping`, `legacy_graph_mode_retired`, `not_a_v2_config`, `mask_secret_ref_not_allowed`, `fixed_width_layout_ref_missing`, `fixed_width_layout_ref_invalid`, `fixed_width_layout_not_found`, `fixed_width_layout_corrupt`, `geo_lat_lng_requires_h3`, `v2_config_invalid`). This adds no Platform failure code to the error-node rule: F29 O3 (no Platform failure codes in the registry for V1) is about `node_message`, which keeps `APPROVED_ERROR_MESSAGES` first.

The engine owns the contract; Platform consumes it through the pinned release. F31 adds no approval function: `approve_ref` and `approve_name` (`_record.py:72-97`) stay F29's, unchanged in V1, and F31 is the sink behind them (`StepRef`, the validation `hint`). Their docstring's "F31 plugs in" seam is met by F31 calling them; provenance checking stays V2 (F22 Q2).

### 3.10 F29 section 1.6 drop list, item by item

| F29 item | Revision 7 and 8 |
|---|---|
| 1 | Revision 6's exception-taking writer signature is gone; P8 calls F29 item 1 plus the item 9 keyword (3.8) |
| 2 | F31 does not create the node-error module; P8 edits F29's |
| 3 | F31 keeps F29's `project_node_error(raw, *, job_id)` and its `reason` round trip; revision 6's unknown-key rule is gone |
| 4 | Only `node_message` branch 3 changes to `platform.job_failed` prose; approved text and `reason.summary` stay |
| 5 | F31 ships no `node-error` sentry and states no baseline for it; F29 P1 lands it at 0 |
| 6 | The `FailureDetails.message` removal and the `:201` deletion are F29 P1's |
| 7 | P8 touches only what `error_type` needs (11.1); F29 P1 owns `_partial_publication.py`, `routes_runtime_facts.py`, `evidence/assembly.py` |
| 8 | P8 extends F29's tests in `tests/test_job_node_error.py`; it does not recreate them |
| 9 | No `exc-copy` row for the vault copy or `:201` is F31's (the vault row disappeared at `0638a97d`, #152; `:201` goes in F29 P1) |
| 10 | P8's file list includes `api/evidence/schema.py` (`NodeError` is `_Strict`) |
| 11 | T-P8's message control is `message == node_message(code, reason, job_id=...)`; the label check is on the `error_type` key |
| 12 | The failure-projection reuse row points to F29 section 1.6 item 2 (section 8) |

### 3.11 Node warnings (P9)

Revision 7 called `QualityWarning` value-free by structure; that was false (Codex, revision 7 gate). `MimesisAdapter._emit_locale_fallback` (`src/decoy_engine/providers_v2/mimesis/_adapter.py:193-207`) copies the configured `ProviderSpec.locale` into `detail["requested_locale"]`; `_warning_dict` (`api/jobs/v2_node_runs.py:59-75`) serializes the whole `detail` into `hint`, and three readers serve it to the job owner: `/node-runs` `warnings` (`api/jobs/routes_runtime_facts.py:99`, served as stored), the manifest (`api/evidence/assembly.py:1938-1956`) and the report's known limitations (`api/reporting/parsing.py:172-215`, `message` only).

Decision: project at the Platform sink, not by editing the 22 engine producers. Reasons: rows already stored carry raw `detail`, so a read-side projection is needed anyway; the projection fails closed for any future producer key; it needs no engine release; the engine's `detail` stays for in-process SDK callers (section 1 boundary).

P9 adds `api/jobs/node_warning.py` with `project_warning(entry) -> dict`, called by both writers (`_warning_dict`, `_failed_check_warning_dict` at `v2_node_runs.py:79-91`; `v2_runner.py:436` goes through `_warning_dict`) and all three readers. Output keys `severity`, `code`, `message`, `hint`: `code` must match `^[a-z][a-z0-9_.]{0,63}$` (else `untagged`); `message` is re-rendered as `<code>[; provider=<approve_ref>][; column=<approve_name>]`, or the engine's failed-check template `post-validation scan '<scan>' hard-failed` (`src/decoy_engine/execution/_pipeline_finalize.py:354`, `scan` a closed scan name), and a stored message that matches neither renders as `code`; `hint` is the JSON of the allowlisted `detail` keys below with values of the stated type, plus `withheld: <n>` when keys were dropped; a non-JSON or non-object hint becomes `null`.

| Allowed key | Type check | Producers (`src/decoy_engine/...`) |
|---|---|---|
| `orphan_count`, `orphan_rows`, `source_distinct`, `output_distinct`, `fixed_point_count`, `leaked_count`, `violations`, `pool_bytes`, `cache_bytes_capacity`, `affected_values`, `total_values`, `total`, `over_count`, `under_count` | non-negative `int`, not `bool` | `validation/post/_checks/_fk_validity.py:70`, `_cardinality.py:56`, `_leakage.py:63`, `:80`, `_format_rules.py:57`; `generation/pool/_cache.py:143`; `execution/_fk_resolve.py:161`, `out_of_core/_join.py:47`; `execution/_strategies/_fpe.py:311`, `_text_mask.py:237`, `_top_code.py:245`; `out_of_core/_mask_group_c.py:131`; `native/_text_mask_route.py:60` |
| `dominate_threshold` | finite `float` | `generation/pool/_cache.py:143` |
| `regulated` | `bool` | `validation/post/_checks/_format_rules.py:57` |
| `table`, `parent_table`, `child_table`, `relationship` | `str` passing `approve_name` (owner names) | `_cardinality.py`, `_leakage.py`, `_format_rules.py`, `_output_projection.py:140`, `_fk_resolve.py`, `_join.py`, `_fk_validity.py` |
| `parent_columns`, `child_columns`, `undeclared_columns` | list of `str`, each passing `approve_name` | `_fk_resolve.py`, `_join.py`, `execution/_output_projection.py:140` |
| `error_type`, `policy` | `str` passing `approve_ref` (a type name; an engine-validated policy enum) | `validation/post/_runner.py:117`; `_text_mask.py`, `_mask_group_c.py`, `_text_mask_route.py` |
| `by_detector` | object of `approve_ref` key to non-negative `int` | the three text-mask sites |

Dropped (counted in `withheld`): `requested_locale` and `mimesis_locales` (`providers_v2/mimesis/_adapter.py:201`, a config literal), `target` and `row_pos` and the string `match_count` (`execution/_strategies/_nested.py:341`, `:369`, a JSONPath literal and row positions), `join_group` and `security_note` (`_fpe.py:263`, `native/_crypto_reference.py:227`, a config literal), `cascade_decisions` (`_geo_generalize.py:86`) and `generalized` (`_top_code.py:245`), both per-row position maps, `shapes` and `message` (`execution/_residency_warning.py:90`), and every `note`. Named loss: those facts no longer reach the owner surface; the code and the counts still do. The sentry `warning-shape` (AST) fails on a `warnings=` write or a `.warnings` read of a `JobNodeRun` in `api/` outside `node_warning.py` and the three enumerated readers.

## 4. Defect-class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | The V1 choke points (section 0) with closed labels and registered prose; ratchets R5, `detail-exc`, `exc-copy` (two passes, allowlist, `getattr`), `validation-sink`, `warning-shape`, `notify-closed` | T-P1 marker corpus over the four readers, T-P2, T-P4, T-P4d, T-P4e, T-P7 (10 site cells), T-P8, T-P9, T-P5n, X | let `_append_narrative` keep the message; add `requested_locale` to the warning allowlist; restore `detail=str(exc)` at `api/pipelines/router.py:101`; read `m.message` in `result_to_response`; serve `exc.message` in `unavailable_http_status_and_detail` |
| 2 | Cleanup missing on an exit path | `platform_event` and `append` are total (any construction or serialization failure becomes the `safe_output.invalid` line), so no terminal writer can fail on content; P8 adds one keyword and cannot raise on content (F29's `node_error` never raises) | T-P2 terminal cells; 6.3 `test_invalid_event_on_terminal_path` | let `platform_event` raise (the terminal cell loses its status commit); make `to_line` raise on an over-limit event |
| 3 | State machine and concurrency | N/A: the cut removes the per-job scope and coordinator; F31 adds no lock, state or token; `append` runs in the caller's (later fenced) transaction | the 6.3 terminal cells only assert the last parsed line is the terminal line | none: no F31 state machine remains |
| 4 | Route parity and silent fallback | One `Job.log` reader for the four stored-line routes; one validation sink for both callers; F29's one error-node reader for both routes; a withheld line is counted, never dropped; the notification summary is a closed type | T3 over four readers; T-P7 `[create, validate]`; T-P8 over both error-node readers; T-P5n | skip `from_line` in one reader (T3); bypass `result_to_response` in job create (T-P7 create cell); accept a `str` `error_summary` (T-P5n) |
| 5 | Referential integrity | N/A: F31 changes no FK semantics; FK reports keep their fields and are not on a V1 surface | FK-shape output matrix (6.4) stays byte-identical | change an FK report field: the matrix fails |
| 6 | Untrusted producer | `from_line` bounded and closed; a stored error-node row is re-projected on read by F29's reader, and its `error_type` via `SafeErrorType.from_label`; a stored warning is re-projected on read by `project_warning` | hostile-line corpus (T14); T-P8 tampered `error_type` cell; T-P9 legacy-row cells | accept an unknown key in `from_line`; serve a stored `error_type` string unmapped; skip `project_warning` in one reader |
| 7 | TOCTOU / check-then-use on a path | N/A: F31 opens no path after a check and adds no file writer | none needed | none |
| 8 | Weak tests and overclaimed evidence | Each count cites a committed probe at a named commit; positive control per recorder; ratchets shrink only; `exc-copy` attributes are an allowlist (`getattr` included) and an unclassified row fails; the probe output is sorted and seed-independent; rule 5 names owner names exactly; named limits (section 1), no completeness claim | recorder self-test with a disconnected sink; baseline-growth test per ratchet; `selftest.py`, `selftest_allow.py` (red on the revision 6 probe), `selftest_r8.py` (red on the revision 7 probe) | add a baseline entry: the growth test fails; drop callee resolution: the seeded chain passes; turn the allowlist back into a list: `attr_unlisted` passes; drop the `getattr` rule: `attr_getattr` passes |
| 9 | Behavior-preserving refactor drift | Characterization first for the four `Job.log` readers and the two downstream line parsers (`parseLogLine`, the STORM error-line pick at `api/storm/router.py:1714`), node-run grouping, the validation response keys and `phase.error_type`; `JobLogger` keeps the engine `Logger` protocol; P1, P2a, P2b ship in one release; the 10 `add_error` sites are enumerated with file:line (5.7) | snapshots taken before P1, P7 and E0a; T-P2r STORM-scan and V2-job cells; T-P7 key snapshot | group node runs by a different key; delete `JobLogger.info` (STORM cell red); drop `hint` from the sink (key snapshot red) |
| 10 | Frozen contract drift | Section 7 with value semantics; `decoy-safe-output/v1` additive only; registry additions follow F29's rules at the live version (3.9); no pre-GA break left in F31 | T5 value-domain snapshots; the F29 registry and release-pin tests | change a progress value domain: T5 fails; declare `since` equal to a released version: the release-pin test fails |

## 5. Enumerations from the code

### 5.1 Exit paths

- `Job.log` append on the terminal path: success, failure, cancel and reaper (`api/jobs/job_terminal_cas.py:256-266`, `api/jobs/reaper.py:233`, `api/jobs/runner.py:669`).
- Serializer: normal, substitute on a limit, double fault (`_FAILED_LINE`); parser rejection (3.1).
- Failed V2 job: error node from a coded chain, an uncoded exception, a cancel; vault persist failure now raises before publication (`api/jobs/v2_orchestrator.py:289-318`, `api/jobs/v2_vault.py:269-279`).
- Validation: each of the 10 `add_error` early returns (5.7) through the one sink.

### 5.2 Routes

- Stored-line routes: SSE (`api/jobs/routes_streaming.py:92`), job `log` field (`api/jobs/_shared.py:203`), node runs (`api/jobs/routes_runtime_facts.py:96`), STORM summary (`api/storm/router.py:1707`).
- Job lifecycle roots that write `Job.log`: flag-off `run_job` (`api/jobs/runner.py:451`), flag-on worker `run_v2_lifecycle_in_worker` (`api/jobs/dispatch_worker_main.py:350`), STORM `run_storm_job` (`api/storm/router.py:984`), iteration parent and children (5.6).
- Validation routes: job create (`api/jobs/_create_phases.py:341-350`), validate (`api/pipelines/router.py:689-694`).
- Error-node routes (F29's reader): `GET /jobs/{id}/node-runs` (`api/jobs/routes_runtime_facts.py:71`), evidence manifest `nodes[].error` (`api/evidence/assembly.py:2408`).
- P4d routes: pipeline validate (`api/pipelines/router.py:725`), workflow runs (`api/workflows/runner.py:225`), iteration preview (`api/pipelines/router.py:446`), `/healthz/db` (`api/main.py:223`), `/server-logs/services` (`api/server_logs/router.py:50`).
- P4e routes: job preview (`api/jobs/routes_preview.py:164`), node preview (`api/pipelines/router.py:1385`), retrieval page (`api/retrieval/router.py:268`, no authentication before admission).

### 5.3 FK shapes

- Single and composite edges, `RelationshipEdge` (`src/decoy_engine/relationships/_graph.py:72-92`).
- Multi-parent children and children in later streamed batches (`src/decoy_engine/execution/out_of_core/_multi_parent.py:49-106`). F31's V1 slices touch no execution path; 6.4 checks the relational output stays byte-identical.

### 5.4 Fields

- V1: Platform `Job.log` (`api/models.py:424`); validation response `messages[].message`, `messages[].hint`, `detail` (`api/pipelines/validation_messages.py:108-124`); the `error_type` key of `JobNodeRun.error` (F29's writer at `api/jobs/v2_node_runs.py:434`) and `NodeError.error_type` (`api/evidence/schema.py:328`); `WorkflowRun.summary` (`api/workflows/runner.py:225`, `:238`); validate `messages[].message` from the preflight catch (`api/pipelines/router.py:733`); `IterationPreviewOut.error` (`api/pipelines/router.py:206`); `HealthReport.error` (`api/health_check.py:72`); refusal `detail.message` (`api/jobs/preview_http.py:52`, `api/retrieval/retrieval_http.py:103`, `:118`, `:137`); `error_summary` (`api/notifications/dispatcher.py:192`).
- V1 (P9): `JobNodeRun.warnings` entries built from `QualityWarning` (`src/decoy_engine/generation/pool/_events.py:15-38`) and failed checks (`api/jobs/v2_node_runs.py:59-91`), served by `/node-runs`, the manifest and the report (3.11).
- V2-LOG (no non-admin reader): `CheckResult.detail` (`src/decoy_engine/sdk.py:123`), `IsolatedRunResult.error` (`src/decoy_engine/execution/_isolated_common.py:369`), engine `ValidationMessage.message` after P7 (`src/decoy_engine/validation_result.py:68`), and section 15.

### 5.5 Output surfaces

| # | Surface | Source | Who sees it | V1 | Rule |
|---|---|---|---|---|---|
| S1 | Progress dicts v0, v1 | `src/decoy_engine/_progress_events.py:581-599` | job viewers | unchanged | T5 |
| S14 | Validation response (422 on create, validate body) | `api/pipelines/validation_messages.py:92-124` | pipeline editors | yes | `validation-sink`, T-P7 |
| S13 | API error details | 60 sites in 27 files (`httpexc_kinds-0638a97d.txt`); 10 `require_admin` | API callers | 50 yes | `detail-exc` |
| S15, S16 | Stored `Job.log` and its readers | `api/models.py:424`, 5.6, 5.2 | job viewers | yes | R5, T3 |
| S25 | `job_failed`, `node_flagged`, `flag_gate` notification `detail` and payload | `api/notifications/dispatcher.py:187-213`, `:330-405` | Slack, email, webhook | yes | `notify-closed` |
| S26 | Job error node | F29 P1; `error_type` key from P8 | job owner | yes | F29 `node-error`, T-P8 |
| S27 | Other except-bound response copies | P4d sites (3.4) | pipeline and workflow owners; anyone for `/healthz/db` | yes | `exc-copy`, T-P4d |
| S28 | Preview and retrieval refusals | `api/jobs/preview_http.py:52`, `api/retrieval/retrieval_http.py:103`, `:137` | preview callers; anyone for retrieval admission | yes | `exc-copy`, T-P4e |
| S29 | Node warnings | `api/jobs/v2_node_runs.py:59-91`; readers in 3.11 | job owner | yes | `warning-shape`, T-P9 |
| S2-S4, S8, S10, S18-S21, S24 | Isolated child text, engine and server logs, audit, metrics, child stdio, CLI stderr, traceback locals | revision 3 section 5.5 | admin or operator | V2-LOG | section 15 |

### 5.6 Platform `Job.log` writers (assignment sites at `0638a97d`)

28 direct assignments in 17 files (`logassign-0638a97d.txt`; count unchanged since `d8908f42`, five lines moved): `api/jobs/binding_resolve.py:429`; `api/jobs/iteration.py:504`, `:537`, `:551`, `:654`, `:674`; `api/jobs/iteration_dispatch.py:160`, `:426`; `api/jobs/job_principal.py:94`; `api/jobs/job_terminal_cas.py:256`; `api/jobs/queue_worker.py:385`, `:607` (was `:604`); `api/jobs/reaper.py:233`; `api/jobs/runner.py:68` (was `:67`, `_append_log`), `:669` (was `:661`); `api/jobs/scheduler_claim_loop.py:98`; `api/main.py:1071` (was `:1043`), `:1079` (was `:1051`); `api/persistence/logger.py:448` (`JobLogger._append_narrative`); `api/quality/runner_hook.py:707`; `api/storm/post_mask_hook.py:372`, `:373`; `api/storm/router.py:588`, `:699`, `:1023`; `api/synth/runner_hook.py:333`; `api/workflows/restart_recovery.py:48`; `api/workflows/runner.py:90`. R5 lands in P1 with these as its baseline. #152, #153 and #154 added no writer.

### 5.7 Validation `add_error` sites (all in `api/pipelines/v2_validation.py` at `0638a97d`)

| Site | Code | Today's message source | Value risk | P7 |
|---|---|---|---|---|
| `:155` | `PlanCompileError.code` | `exc.message` (a `when` literal can reach it); `hint=strategy` | V1 | `plan` reason summary; hint approved |
| `:174` | `geo_lat_lng_requires_h3` | constant; `hint=strategy` | none | `platform` prose, same wording |
| `:224` | `yaml_empty` | constant | none | `platform` prose |
| `:237` | `yaml_unparseable` | `type(exc).__name__` | class name only | `platform` prose |
| `:244` | `yaml_not_a_mapping` | `type(parsed).__name__` | class name only | `platform` prose |
| `:255` | `legacy_graph_mode_retired` | constant | none | `platform` prose |
| `:272` | `not_a_v2_config` | constant | none | `platform` prose |
| `:290` | `mask_secret_ref_not_allowed` | `exc.message`, constant per code (#143) | none | `platform` prose |
| `:298` | `fixed_width_layout_*` (4 codes) | `str(ref_error)`: table name and an int layout id (`api/jobs/v2_config.py:315-348`, F21 slice 1) | owner names only | `platform` prose, path kept |
| `:316` | `v2_config_invalid` | pydantic `msg` (a custom validator message can echo config text); `hint=` pydantic error type | V1 | `platform` prose; hint approved |

At `d8908f42` there were 8 sites; `:290` (#143) and `:298` (#150) are new. The engine has no `add_error` caller (`validation_result.py:99`, `:119` are the definitions). Preflight messages (`api/pipelines/preflight.py:170`, `str(UnresolvedVariable)`) do not pass through `ValidationResult`. Every producer is cited and value-free under rule 5, so the row needs no V1 work: `api/jobs/variables.py:37-41` builds `unresolved variable: ${scope.key}` plus a hint from `:157` (the closed list of known scopes), `:426` (`binding_resolves`, `loc` and pydantic `type` only, `api/environments/bindings.py:276-290`), `:440`, `:446` (binding kind and field names) and `:462` (`BindingResolutionError` text from `bindings.py:177`, `:222`, `:349`, `:360`, `:379`: binding, variable and secret names, kinds, ids and the same `loc`/`type` summary). Scope and key are owner names (rule 5).

## 6. Acceptance tests, red first (hostile corpus)

Each test is written before its slice's production change and recorded red at the named fail-before location, run from `uv.lock` (`uv sync --locked`); for a new mechanism the named mutation is the semantic proof, recorded red.

### 6.1 Seeded corpus

Markers in exception messages (foreign library, `PlanCompileError` `when` literal, `SubsetError`, `OSError` path, a lane disable reason, a pydantic custom-validator message) and a schema-name canary (an owner name that must still render, as the positive control for rule 5). Each recorder asserts a control event arrived, then marker absence over response bytes, stored `Job.log`, error-node rows, node warnings, reader output and notifications.

### 6.2 Tests (each RED first)

| # | Break | RED-first test (file::function) | Fail-before | Mutation that must turn it red | Slice |
|---|---|---|---|---|---|
| T3 | A reader replays raw text from `Job.log` | Platform `tests/test_job_log_reader.py::test_each_reader_withholds_invalid_lines[sse, log_field, node_runs, storm_summary]`: legacy free-text row with a marker, a 4097-byte line, an unknown key; positive control: a valid line renders; characterization cells committed first and kept green: `::test_render_matches_downstream_parsers[parseLogLine, split_log_by_node, storm_error_pick]` (a rendered `WARN`, `INFO`, `ERROR` and `step=None` line split by the regex of `logParsing.ts:22` and its error tail, by `routes_runtime_facts.py:45-66` (a `job` line is dropped) and by the ` ERROR ` pick at `api/storm/router.py:1714`) | `api/jobs/_shared.py:203-211` (raw text returned) | skip `from_line` in one reader; render `WARNING` unpadded (the parser cells go red) | P1 |
| T-P1 | A writer stores exception text | Platform `tests/sentry/test_job_log_writes.py::test_no_log_assignment_outside_job_log` (R5, baseline 28) and `::test_seeded_assignment_is_red` over `tests/sentry/fixtures/job_log/leaky_writer.py` | `api/jobs/runner.py:68` (`_append_log` assigns `job.log`) | add a `.log` assignment in a fixture-free module | P1 |
| T-P2 | A writer keeps message text | Platform `tests/test_job_log_writers.py::test_narrative_drops_message` (`JobLogger(...).error(f"failed on {MARKER}")`: the line parses as `platform.job_narrative`, no marker), one cell per migrated writer site driving its path with a marker exception, and `::test_host_safety_drops_reason` (a `yaml_snapshot` whose parse error quotes a marker, rejected via `worker_budget.py:402` and `_fail_host_safety`) | `api/persistence/logger.py:448`; `api/jobs/queue_worker.py:385` | keep `msg` in `_append_narrative`; keep `reason` in `_fail_host_safety` | P2a, P2b |
| T-P2r | Release split; engine protocol kept | Platform `tests/test_job_log_release_unit.py`: `test_no_writer_left_unmigrated` (R5 empty); `test_no_platform_call_to_narrative_methods` (AST: no `.debug`/`.info`/`.warning`/`.error` call on a `JobLogger(...)` or a name bound from one in `api/`; seeded); `test_job_logger_satisfies_engine_protocol`; `test_storm_scan_with_job_logger` (`run_storm_job` reaches `profile_columns`; job `success`, `Job.log` parses, one `platform.job_narrative` line from the profiler, no marker from a marker column name); `test_v2_job_with_job_logger_as_ctx_logger` (success and failure). In P2b's gate on the merged P1+P2a+P2b result | `api/persistence/logger.py:152-205` (Platform callers present) | delete `JobLogger.info` (STORM cell `AttributeError`); add a Platform `.info(...)` call (AST red) | P2b |
| T-P4 | An API detail copies exception text | Platform `tests/sentry/test_detail_exc.py::test_detail_reads_no_exception_text` (baseline 60; 10 `require_admin` rows remain), `::test_seeded_detail_is_red` (`detail=f"bad: {exc}"`, `detail=str(exc)`, `detail=exc.args[0]`); per drain slice `tests/test_detail_exc_routes.py::test_route_detail_has_no_marker[<file>:<line>]`, one cell per site, dependency raising `RuntimeError(MARKER)`; the four `MaskSecretConfigError` cells assert the body bytes are unchanged | `api/pipelines/router.py:101` and the other 49 non-admin sites | restore `str(exc)` at one site | P4, P4a to P4c |
| T-P4d | An except-bound copy reaches a response, directly or through a callee | Platform `tests/sentry/test_exc_copy.py`: `test_no_unclassified_copy` (both passes, allowlist; unclassified or stale key fails), `test_seeded_copy_is_red[return_str, assign_fstring, append_message, attr_reason, attr_unlisted, attr_getattr, callee_depth2, from_import_alias, store_attr, unres_method]` (`attr_unlisted` reads `exc.filename`, `exc.orig`, `exc.strerror`; `attr_getattr` reads `getattr(exc, "orig")` and `getattr(exc, name)`), `test_census_is_seed_independent` (two `PYTHONHASHSEED` values, same rows), `test_negative_controls_pass[closed_code, errno, status_code, rewrap_raise]`, `test_allowlist_is_exact` (the five names, nothing else); `tests/test_exc_copy_routes.py::test_response_has_no_marker[validate_preflight_725, workflow_225, workflow_238, iteration_mode_484, iteration_loop_bounds_484, iteration_size_479, healthz_db, services_db]` (MARKER in the preflight error, the stored `mode`, the loop bounds, an `OperationalError` host; `/healthz/db` unauthenticated, `/services` as `release_manager`; control: label and prose present, `/healthz/db` still 503) | `api/pipelines/router.py:479`, `:484`, `:733`; `api/workflows/runner.py:225`, `:238`; `api/health_check.py:72` | restore `{exc}` at one site; drop callee resolution (`callee_depth2` passes); replace the allowlist with revision 6's list (`attr_unlisted` passes) | P4d |
| T-P4e | A refusal serves stored exception text | Platform `tests/test_refusal_text.py`: `test_disabled_lane_reason_not_served[job_preview, node_preview, retrieval_unauthenticated]` (`validate_lane_root` raises `SupervisionPathError(MARKER_PATH)`; control: `lane_disabled_reason` holds it; 503 body: code and prose, no MARKER); `test_retrieval_child_detail_not_served[unsupported_parquet, cap_exceeded]`; `test_preview_source_path_not_served`; `test_map_disabled_error_is_constant[preview, retrieval]`; `test_mappers_read_only_code` (the six `exc-copy` rows at the P4e sites are gone from the census) | `api/jobs/preview_http.py:52`, `api/retrieval/retrieval_http.py:103`, `:137`, `api/jobs/preview_slot.py:122`, `api/retrieval/retrieval_slot.py:134` | serve `exc.message` in one HTTP mapper; restore `{exc.reason}` in one `_map_disabled_error` | P4e |
| T-P7 | The validation response carries exception or config text | Platform `tests/test_validation_sink.py::test_site_message_from_registry[s155_plan_compile, s174_geo_h3, s224_empty, s237_unparseable, s244_not_mapping, s255_graph_mode, s272_not_v2, s290_mask_secret_ref, s298_layout_ref, s316_pydantic]` x `[create, validate]` (`s224_empty` x `validate` is N/A: the validate route answers `ok=True` for empty YAML before the sink, `api/pipelines/router.py:667-668`): each site driven with a marker where it can carry one (a `when` literal, a custom validator message, a layout table name as the owner-name control); `message` and `detail` equal the registry prose; `hint` is a closed token or `None`; positive controls: `code`, `path`, `node_id` unchanged, owner names present in `path`; `::test_response_keys_unchanged` (characterization, green on base, committed first); sentry `tests/sentry/test_validation_sink.py` (`validation-sink`): no `.message` attribute read on an element of `.errors`/`.warnings` in `api/` (seeded fixture), and the callers of `validate_v2_or_graph` and `result_to_response` are exactly the two in 5.2 (a third fails) | `api/pipelines/validation_messages.py:113` (`"message": m.message`) | read `m.message` again in `result_to_response`; pass `m.hint` unapproved | P7 |
| T-P8 | The error node's label carries exception text, or F29's contract breaks | Platform, extending F29's files: `tests/test_job_node_error.py::test_error_type_label[subset_error, plan_compile_error, fixed_width]` (marker `SubsetError` or `PlanCompileError` under a `PartialPublicationError`: `error_type == safe_error_type(selected)`, no marker in the stored row, `/node-runs` or the manifest; `message == node_message(code, reason, job_id=...)`); `::test_error_type_tampered_row` (stored `error_type` of `"<MARKER>"` re-projects to `EXCEPTION`); F29's P-T10 key set gains `error_type`; F29's P-T6, P-T7 and `test_p6_failure_trace_baseline.py:161` take the `platform.job_failed` prose | F29's `node_error` has no `error_type` (after F29 P1) | serve the stored `error_type` string; drop the keyword | P8 |
| T-P5n | A notification carries free text | Platform `tests/test_notifications_failed_event.py::test_error_summary_is_closed_label`: passing a `str` raises `TypeError`; the test-fire renders fixed prose; a `job_failed` event from a job that failed with a marker exception has no marker in `detail`; `::test_node_flagged_has_no_line_text` (a flagged node's narrative line carries MARKER: no MARKER in `detail` or payload; control: severity and node id present); `::test_flag_gate_reason_is_closed` (a `str` reason raises `TypeError`) | `api/notifications/dispatcher.py:192`, `:391`, `:354-355` | widen a type back to `str`; restore `log_message` | P5n |
| T5 | A key snapshot passes while value meaning changes | engine `tests/unit/safe_output/test_value_domains.py`: progress keys and values unchanged; `phase.error_type` domain is a superset of the 15 labels | `src/decoy_engine/phase.py:594` | change a label value | E0a |
| T14 | Serializer limits or the double fault leak | engine `tests/unit/safe_output/test_to_line_limits.py`: `test_exact_4096_bytes_passes`, `test_4097_bytes_becomes_invalid`, `test_depth_3_passes_depth_4_becomes_invalid`, `test_double_fault_emits_failed_literal`, `test_from_line_rejects_failed_literal`, hostile-line corpus (duplicate keys, unknown key, subclass values, a `step` shaped like a path that `approve_name` refuses) | new module (`ModuleNotFoundError: decoy_engine.safe_output` at `492f35ac`) | truncate instead of substitute; let the double fault raise | E0b |
| T-E0c | A registry addition breaks F29's rules | engine: the existing `tests/unit/test_reason_registry.py`, `tests/unit/test_reason_release_pin.py`, `tests/unit/progress/test_consumer_contract.py` stay green with no edit to the release-pin fixture; new `tests/unit/safe_output/test_platform_reasons.py::test_every_platform_code_declared_since_current` (every `platform` and `safe_output` identity has `since == REGISTRY_VERSION`) | the namespaces are absent at `492f35ac` | declare one identity with `since="1.2.0"` (release-pin test red) | E0c |
| T-P9 | A node warning carries a config literal or a value | Platform `tests/test_node_warning.py::test_warning_has_no_marker[mimesis_locale_fallback, nested_target, fpe_join_group, top_code_rows] x [node_runs, manifest, report]` (`mimesis_locale_fallback`: a Mimesis provider with `locale="xx_MARKER"` falls back to Faker; no MARKER in any reader; control: `code` and `withheld >= 1` present, the table name present in `hint`); `::test_allowlisted_keys_kept_with_types` (counts, owner names, `by_detector`); `::test_legacy_row_reprojected[raw_detail, bad_message, non_json_hint]` (rows stored as at `0638a97d`); `::test_failed_check_message_template`; sentry `tests/sentry/test_warning_shape.py` (seeded) | `api/jobs/v2_node_runs.py:74` (`json.dumps(detail)`) | restore `requested_locale` to the allowlist; skip `project_warning` in the `/node-runs` reader | P9 |
| X-QW | End to end, a warning carries a value | cross-repo `tests/test_x_marker_corpus.py::test_node_warnings_have_no_marker` on the released engine (a marker in a source cell and in a Mimesis locale; a marker table name as the owner-name control) | red on the pre-P9 Platform commit (the locale cell) | revert P9's writer call | X |

### 6.3 Exit-path matrix

`tests/unit/safe_output/test_exit_matrix.py::test_exit_paths` (engine) over 5.1's serializer rows; Platform `tests/test_job_log_exit_paths.py` over 5.1's terminal rows (success, failure, cancel, reaper): the terminal line is the last parsed line and the status commits; `::test_invalid_event_on_terminal_path[cas, runner, reaper, startup_recovery]` passes a negative count and a step `approve_name` refuses to each terminal writer: the status commits and the last parsed line is `safe_output.invalid` (mutation: let `platform_event` raise); T-P7 covers the validation early returns; F29 P1's tests cover the error-node rows and T-P8 extends them.

### 6.4 FK-shape output matrix

For each shape in 5.3 on the relational routes: relational output is byte-identical on the released engine (ER) and its predecessor, run in X.

### 6.5 Ratchets

Each ratchet mirrors the module-size census (`tests/sentry/test_module_size.py:66`): counts only shrink, stale entries fail, a seeded fixture proves the rule fires. R5 (Platform, P1, baseline 28, empty after P2b); `detail-exc` (Platform, P4, baseline 60, the 10 admin rows after P4c); `exc-copy` (Platform, P4d, both passes with the allowlist and `getattr`, baseline the 128 classified rows at P4d's base; after P4d and P4e the 12 rewritten rows are gone (P4d 6, P4e 6), so 116 remain, 115 if F29 P1 stops passing the exception to `report`; an unclassified row fails; its limits are section 1's); `validation-sink` (Platform, P7, two enumerated callers, no baseline); `warning-shape` (Platform, P9, two writers and three enumerated readers, no baseline); `notify-closed` (Platform, P5n, a type test, no baseline). F29's `node-error` is F29's.

## 7. Frozen contracts touched

| Contract | Change |
|---|---|
| `decoy-engine` public API | Additive `decoy_engine.safe_output` (`SafeEvent`, `SafeErrorType`, `safe_error_type`, `to_line`, `from_line`, `render_text`). No break: revision 6's `ValidationMessage` and `add_error` change is dropped (3.5). Version: the next engine release in the owner's release order (PHASE1-TRACKS 2.1 "owner" row: F34 first); consumers pin exactly that |
| Reason registry (live `1.3.0` at `492f35ac`, release pin `1.2.0`) | Additive `platform` and `safe_output` namespaces at the next minor after the live version at E0c's base, `since=` that literal, release pin untouched (3.9) |
| Progress v0/v1 | Unchanged |
| `phase-result/v1`, `phase-event/v1` | Keys unchanged; `error_type` domain widens additively |
| Platform OpenAPI and web client | Shapes unchanged; `log` and SSE lines are rendered v1 records; rewritten details, copies and refusals lose exception text; validation `message`/`detail` are registry prose and `hint` is a closed token or absent; notification text is registered prose (webhook payload keys `log_message` and `reason` keep their names, values closed); node warning `hint` holds only allowlisted keys plus `withheld`; error nodes gain `error_type` (after F29's `reason`), no key removed |
| Evidence manifest `NodeError` (`r3.1`) | Additive optional `error_type` (F29 O4 rule: no version bump) |
| `decoy-safe-output/v1` (new) | Additive only after release |

## 8. Reuse inventory

| Existing | Source | Use |
|---|---|---|
| F29 registry, records, identity form, approval seam | `src/decoy_engine/reasons/_registry.py:24`, `_record.py:72-97`, `:115-260`, `_identity.py:27-57` | Prose authority, `ReasonRef`, `COUNT_KEYS`, `approve_name` (`StepRef`), `approve_ref` (validation `hint`) |
| F29 registry rules and fixtures | `docs/reasons.md:21`, `tests/unit/test_reason_registry.py:151`, `tests/unit/test_reason_release_pin.py`, `tests/fixtures/f29/` | E0c procedure (3.9) |
| F29 node-error module | F29 plan section 1.6 (F29 P1) | P8 extends it; the message rule is item 2 (`node_message`) |
| Reason adapter | `src/decoy_engine/reason_adapter.py:145` | `plan` reasons for `PlanCompileError` codes in the validation sink |
| STORM exact-type table | `src/decoy_engine/phase.py:555-594` | Lifted into `safe_output._exception`; `phase` delegates |
| Module-size census ratchet | `tests/sentry/test_module_size.py:66` | Baseline pattern for the ratchets |
| Platform `exc_safe` | `api/logging/exc_safe.py:76` | Same label shape for API details |
| Closed lane refusal | `api/generation/generation_slot.py:77-78` | P4e: a disabled lane maps to a code, never its reason |
| Pydantic error summary | `api/environments/bindings.py:276-290` | P7 keeps `loc` and `type`, drops `msg` |
| Constant mask-secret messages | `api/keys/mask_secret_errors.py:15-45` | The four new `detail-exc` sites read `MESSAGES[exc.code]` |
| Failure projection (approved code selects prose, else a type label) | `api/persistence/error_projection.py:1-28`, `APPROVED_ERROR_MESSAGES` at `:45` | F29's `node_message` precedence (F29 section 1.6 item 2); the terminal `Job.log` failure line F29 P2 builds on P2b |
| Persisted-exports re-projection on read | `api/jobs/route_evidence.py:131` | Pattern for `project_warning` on read (P9) |

## 9. Consumer promises

- F25 4c (owner): `scratch._os_error.classify_os_error` is a reviewed exception boundary (it reads `.errno` only; `errno` is on the allowlist); F31 rebases onto 4c-5a.
- F34 (owner): promised `safe_error_type`, `SafeErrorType`, additive `COUNT_KEYS`. Not promised: `SafeOutputScope` (cut), sealed payloads, child fd bootstrap, an atomic file sink.
- F36 (owner): both reason codes engine-owned in `decoy-reasons/v1`; F31 keeps `families/invariants.py` and closed count names out of its own slices. F36's plan tables seven F31 extension points from revision 3 (`plan/f36-cross-column-invariants` `f94fb8eb`, lines 78-83). Revision 7 provides: X1 no `safe_output/families/` framework (F36 slice 4 may add its own factories over `SafeEvent` in its own module); X2 no `SafeCount` (F36 adds its four count names to `COUNT_KEYS`, additive, through the registry rules); X3 no `ordinary_output` flag; X4 no data-plane writer manifest or analyzer (census machinery, V2 since revision 4); X5 is F36-P's; X6 no `SafeEventSink`, `SafeOutputScope` or process-entry install (cut and V2); X7 F31 changes no `ExecutionResult` or `IsolatedRunResult` serialization (E5-1 and E6 are V2-LOG), so F36's snapshots need not wait for F31. F36 slices 4, 8c and 9 gate on these; their reconcile check will stop for the owner (O7).
- F35 and F36 run token: the scheduler's `jobs.claim_generation` through the fenced writer helper; F31 offers none (3.7).
- F30: span id keys by `Job.id`. F32: bundle key is `Job.id`; F32 P2 reads stored lines with `from_line(line)` after F31 P1, P2a and P2b (PHASE1-TRACKS 2.2 P-6).
- F29: P8 stacks on F29 P1; F29 P2 (the `Job.log` registry summary) builds on F31 P2b's writer by passing the engine reason to `job_log.platform_event`.
- A-4 / B-5: entry-frame scrubbing is V2-LOG; the operator control (`docs/security/error-reporting-and-data-exposure.md:23-24`) stays the stated coverage.
- STORM: `safe_error_type` exported; `phase.error_result` delegates; phase schemas frozen.

## 10. Pre-mortem

| Likely gate finding | Class | Prevention |
|---|---|---|
| A reader replays a stored line raw | 4, 6 | one reader, T3 over four readers |
| A Platform release ships between P1 and P2b and `Job.log` shows mostly `legacy_withheld` | 9 | one merge window, T-P2r in P2b's gate |
| Text reaches a response through a helper, a callee, an unlisted attribute or a carrier attribute | 1, 8 | `detail-exc`, `exc-copy` both passes with the allowlist and seeded fixtures; P4e at the HTTP mapper |
| A row with an owner name is called a leak, or a leak is waved through as a "name" | 8 | rule 5 defines owner names exactly; the owner-name control in 6.1 |
| A new `add_error` site bypasses the validation fix | 9 | the sink renders from `code` for every entry; `validation-sink` enumerates the callers |
| P8 is built against revision 6's node-error design and breaks F29 P1 | 9, 10 | section 3.10; P8 depends on F29 P1; T-P8 extends F29's tests |
| E0c bumps to a version another registry slice already took, or edits the release pin | 10 | 3.9 steps 1, 2, 4, 7; release-pin test |
| F36's reconcile check stops on missing X1 to X7 | 10 | section 9 states each; O7 |
| A notification caller passes exception text later | 4 | closed `FailureLabel` and `GateReason`, no `log_message`, T-P5n |
| A terminal writer raises on an invalid event and leaves the job non-terminal | 2 | total `platform_event` and `append`, 6.3 `test_invalid_event_on_terminal_path` |
| A warning producer adds a config literal | 1 | `project_warning` drops unknown keys; T-P9 |
| The plan claims a drain it did not finish | 8 | ratchet baselines are the check; named limits in section 1 |

## 11. Slices

Size bound: at most 10 production files and 800 changed lines per slice, tests included (`git diff --stat`, `uv.lock` excluded); a slice over it is split before merge. Sizes are ESTIMATES (sites times rewrite size plus named test cells). Each slice: RED tests first with output recorded, smallest change, self-check, review, at most two remediation rounds.

STORM column: Phase 1 platform work waits for STORM D.2 (PHASE1-TRACKS 2.2; STORM-FILE-MAP: a branch editing one of the 469 scanned files merges before S0a or after D.2, and no other platform branch merges while a STORM platform slice is open). "scanned" means a production file of the slice contains the STORM token (`grep -ic storm` at `0638a97d`, the STORM inventory's ASCII case-insensitive regex); "free" means none does, so the slice could merge before S0a if its dependencies had landed, which in practice (ER and the repin come first) means after D.2 as well.

| Slice | Repo | Scope | Prod files | Est. prod lines | Est. test lines | Est. total | Depends on | Runs |
|---|---|---|---|---|---|---|---|---|
| E0a | engine | `safe_output/__init__.py`, `_event.py`, `_exception.py` (table lifted from `phase`), `phase.py` delegation; T5, field walk | 4 | 290 | 290 | 580 | none | now, beside STORM (engine only, no registry file) |
| E0b | engine | `_wire.py` (`to_line`, `from_line`, `render_text`), export; T14 | 2 | 250 | 360 | 610 | E0a | now, beside STORM |
| E0c | engine | `reasons/platform/` and `reasons/safe_output/` (4 new files), `_catalog.py`, `reasons/__init__.py`, `_registry.py` version, `docs/reasons.md`, consumer-contract fixture; T-E0c | 7 | 300 | 150 | 450 | E0a; its turn in the registry chain (R4) | engine only, beside STORM, but serial in the registry chain |
| ER | engine | release from the lineage (version, CHANGELOG, release pin recapture by the release script) | 3 | 30 | 0 | 30 | E0b, E0c; the release order (one release at a time) | owner's release |
| P1 | Platform | `api/jobs/job_log.py` (new), the four readers; R5; T3, T-P1, characterization first | 5 | 280 | 400 | 680 | ER and the Platform repin to it | scanned (`_shared.py`, `storm/router.py`): after D.2 |
| P2a | Platform | writers in 10 files (11.1), `_append_narrative` through `append`, `_fail_host_safety` drops `reason`; T-P2 cells, 6.3 invalid-event cells | 10 | 220 | 370 | 590 | P1 | scanned: after D.2 |
| P2b | Platform | writers in 10 files (11.1), Platform `JobLogger` calls migrated, protocol methods kept; T-P2, T-P2r | 10 | 220 | 440 | 660 | P2a | scanned: after D.2 |
| P4 | Platform | `detail-exc` rule, baseline 60, fixtures | 0 | 0 | 250 | 250 | none | free (tests only): before S0a or after D.2 |
| P4a | Platform | 8 files, 17 sites (11.1); T-P4 cells | 8 | 60 | 285 | 345 | P4 | scanned (`analytics`, `data_tables`, `files/router`): after D.2 |
| P4b | Platform | 10 files, 22 sites (11.1); T-P4 cells | 10 | 80 | 360 | 440 | P4 | scanned (`header_layouts`, `routes_evidence`, `pipelines/router`): after D.2 |
| P4c | Platform | 6 files, 11 sites (11.1); T-P4 cells; 10 admin rows left | 6 | 42 | 193 | 235 | P4 | scanned (`storm/detectors_router`): after D.2 |
| P4d | Platform | 6 sites (3.4); `exc-copy` both passes with the allowlist and `getattr` on P4's matcher; T-P4d | 3 | 40 | 505 | 545 | P4, ER and repin | scanned (`pipelines/router`): after D.2 |
| P4e | Platform | `jobs/preview_http.py`, `jobs/preview_slot.py`, `retrieval/retrieval_http.py`, `retrieval/retrieval_slot.py` (3.4, `code` attribute); T-P4e | 4 | 70 | 230 | 300 | P4d | free |
| P5n | Platform | `api/notifications/dispatcher.py`, `api/notifications/router.py`, `api/persistence/logger.py` (the `_evaluate_flags` call); T-P5n | 3 | 60 | 140 | 200 | ER and repin; P2b | scanned (`dispatcher.py`): after D.2 |
| P7 | Platform | `api/pipelines/validation_messages.py` (the sink), `api/pipelines/v2_validation.py` (delete `adapt_validation_message`); `validation-sink`; T-P7 | 2 | 50 | 200 | 250 | ER and repin | scanned (`v2_validation.py`): after D.2 |
| P8 | Platform | F29 section 1.6 item 9: `jobs/job_node_error.py`, `jobs/v2_node_runs.py` or the orchestrator call site, `evidence/schema.py`; T-P8 | 3 | 60 | 200 | 260 | F29 P1, ER and repin | scanned (`evidence/schema.py`): after D.2 |
| P9 | Platform | `jobs/node_warning.py` (new), `jobs/v2_node_runs.py` (both writers), `jobs/routes_runtime_facts.py`, `evidence/assembly.py`, `reporting/parsing.py` (the three readers); `warning-shape`; T-P9 | 5 | 120 | 300 | 420 | F29 P1 (shares two reader files); needs no F31 engine piece (`approve_name`, `approve_ref` ship in 0.18.0) | scanned (`assembly.py`): after D.2 |
| X | all | cross-repo marker corpus on the released engine and pinned Platform commit, including X-QW; 6.4 matrix | 0 | 0 | 320 | 320 | P2b, P4c, P4d, P4e, P5n, P7, P8, P9 | after D.2 |

Totals (estimates): 18 V1 slices, about 7,165 lines with tests (revision 7: 17, about 6,590). Revision 8 adds P9 (+420), P5n +85 (two more notification fields, the call site), P4d +20 (`attr_getattr`, seed cell), P1 +20 (parser characterization cells), P2a +20 (terminal invalid-event cells), P4e +20 (`code` attribute); P7 -10.

Engine-only, can run now beside STORM: E0a, E0b, then E0c in its registry turn, then ER. Platform: P1 to P9 and X, all after STORM D.2; all but P9 also after ER and the Platform repin.

### 11.1 File lists

Platform paths are under `api/`. P4 groups are alphabetical by file, so a reviewer can check them against `httpexc_kinds-0638a97d.txt` in one pass.

- P2a: `jobs/runner.py`, `jobs/iteration.py`, `jobs/iteration_dispatch.py`, `jobs/job_terminal_cas.py`, `jobs/reaper.py`, `jobs/queue_worker.py`, `jobs/job_principal.py`, `jobs/scheduler_claim_loop.py`, `jobs/binding_resolve.py`, `persistence/logger.py` (`_append_narrative` only). Shares `jobs/runner.py`, `queue_worker.py`, `job_terminal_cas.py`, `scheduler_claim_loop.py` with scheduler S9a-2a and `jobs/runner.py` with F35 P0: one writer at a time; the second to merge rebases.
- P2b: `quality/runner_hook.py`, `storm/router.py`, `storm/post_mask_hook.py`, `synth/runner_hook.py`, `main.py`, `workflows/restart_recovery.py`, `workflows/runner.py` (the `.log` write at `:90`; the `summary` sites are P4d), `persistence/logger.py` (protocol methods kept, text-free), `jobs/route_narrative.py`, `jobs/v2_orchestrator.py`.
- P4a (8 files, 17 sites): `analytics/router.py` (2), `composites/preview.py` (2), `data_tables/router.py` (1), `disguises/router.py` (2), `environments/binding_router.py` (2), `files/router.py` (1), `files/s3_pull_router.py` (4), `fixed_width_layouts/router.py` (3).
- P4b (10 files, 22 sites): `header_layouts/router.py` (1), `jobs/_create_phases.py` (3), `jobs/_service.py` (2: `:117`, `:237`), `jobs/binding_resolve.py` (2), `jobs/routes_evidence.py` (1), `jobs/routes_preview.py` (4: `:88`, `:104`, `:158`, `:163`), `notifications/router.py` (1), `org_disguises/router.py` (2), `pipelines/router.py` (5: `:101`, `:383`, `:1314`, `:1379`, `:1384`), `reporting/parsing.py` (1).
- P4c (6 files, 11 sites): `schema_contract/router.py` (2), `server_logs/router.py` (2), `storm/detectors_router.py` (3), `subset/router.py` (1), `text_redact/preview.py` (1), `unmask/router.py` (2: `:133`, `:173`).
- Left as V2 (O4, `require_admin`): `admin/audit_redaction_router.py` (2), `admin/router.py` (2), `seeds/admin_router.py` (6).
- P4d: `pipelines/router.py` (`:479`, `:484`, `:725`), `workflows/runner.py` (`:225`, `:238`), `health_check.py`. P4e: 3.4.
- P9: 3.11; it follows F29 P1, which also edits `routes_runtime_facts.py` and `evidence/assembly.py`.
- P8: `jobs/job_node_error.py` (F29's), the one call site that passes `error_type` (`jobs/v2_node_runs.py` `_error_node_run` or `jobs/v2_orchestrator.py`, whichever F29 P1 leaves holding `details.selected`), `evidence/schema.py`.

### 11.2 Order, registry chain and repins

1. E0a, then E0b (now, engine track, no registry file).
2. E0c at its registry-chain turn (R4: after R1 merged and with no registry slice open; 3.9).
3. ER: one engine release at a time, in the owner's order (F34 first). The release script re-captures the release pin.
4. Platform repin to the F31 release (orchestrator; all seven pin locations of the engine repin checklist in one change; one repin open per repository). Additive: no F31 change breaks Platform at the repin. The CLI needs no repin for F31 (no CLI slice; the release is additive for it); a later CLI repin to this or a newer release carries no F31 change.
5. After STORM D.2: P4 (may land earlier, tests only), P1, P2a, P2b in one release window; P4a, P4b, P4c; P4d then P4e; P5n after P2b; P7; P8 and P9 after F29 P1; X last.

## 12. Worker evidence rules

- Name each finding's class (1 to 10) and fix the class; if a cataloged class recurs, stop and report it.
- Tests first, recorded red at the named fail-before location; baselines from `uv.lock` (`uv sync --locked`) in the branch's environment, said so.
- Apply each 6.2 mutation once and show its test red (attack your own diff); never commit with a mutation applied.
- Never widen a threshold, baseline or the `exc-copy` allowlist, skip a test, weaken an assertion or edit a pinned test beyond the ones this plan names; state what you did not run.
- V1 bar: a finding reaching only an admin surface goes to V2-LOG with `file:line`; it does not block.
- P1, P2a and P2b merge in one window with no Platform release between; P2b leaves `JobLogger` satisfying the engine `Logger` protocol (T-P2r in its gate).
- E0c never edits `tests/fixtures/f29/reason-registry-release.json`; P8 never edits F29's module beyond item 9.

## 13. Owner questions (each with a recommended default)

- **O1 (alias, legend and run-UUID stack). ANSWERED: CUT** (2026-11-02 night). Applied (section 14).
- **O2 (in-process `raise` messages).** Default: V2-LOG; parts (a), (b), (c) and the error node stop that text where Platform copies it into a named surface. Not yet answered.
- **O3 (Platform exception text outside details). DONE** in revision 5 and 6; revision 7 turns the attribute list into an allowlist (+23 rows) and revision 8 adds `getattr` (+18 rows), all classified, no new V1 site.
- **O4 (admin-only routes among the details). ANSWERED by the V1 bar** (standing rule, 2026-11-02): V2-LOG. The 10 sites are in `require_admin` handlers at `0638a97d`.
- **O5 (engine result fields with no Platform reader).** Default: V2-LOG. `QualityWarning` is excluded from this question: it has readers and P9 closes it (3.11). Not yet answered.
- **O6 (part (c) at the sink, part (b) not at a global handler). NOT YET ANSWERED.** Default: P7 closes the validation response at `result_to_response` and E5-4 moves to V2-LOG; `HTTPException` details stay per-site rewrites with the F31b split rule (3.4). Alternative "keep E5-4": +160 lines engine, a pre-GA break, and P7 must land in the same change as the Platform repin.
- **O7 (F36 extension points X1 to X7).** Default: F36 re-plans slices 4, 8c and 9 against section 9 (no framework, no sink, no scope, no writer manifest from F31). F31 does not grow to supply them.
- **O8 (E0c registry version rule).** Default: "next minor" as PHASE1-TRACKS 2.1 lists for R4. If the owner picks the reuse rule (PHASE1-TRACKS 6.3), E0c reuses an unreleased minor when the live version is above the release pin.

## 14. Cut variant (applied, owner 2026-11-02 night)

Removed: the six S slices (S-E3a, S-E3b, S-E4, S-P0, S-P3a, S-P3b; 25 production file touches, 2 migrations, 1 route, about 3,570 lines); `SafeEvent.run` and `aliases`; the stamping rule of revision 6 section 3.7; `from_line`'s run argument; tests T9, T11, T12, T15, T16, T22; the `job_alias_legend` table, `jobs.run_uuid`, `job_node_runs.node_alias` and the `GET /api/jobs/{id}/alias-legend` route; the engine modules `_scope.py`, `_alias.py`, `_sink.py`, `_view.py`; the Platform modules `api/jobs/run_identity.py` and `api/jobs/job_scope.py`.

Kept: `step` (an approved node id), so `Job.log` shows real schema names, which are owner names; progress views stay owner results; legacy `Job.log` rows withheld at read.

Lost (accepted by the owner): aliasing on operational output; the owner-only legend; binding a stored line to a run (forging one needs database write access, outside the V1 threat model); a run UUID as F30 span id and F32 bundle key (both use `Job.id`; the run token is `jobs.claim_generation`, 3.7).

## 15. V2-LOG (logged, not planned; class tag in brackets)

- R1 class-name drain (56 sites), `src/decoy_engine/connectors/postgres_snapshot.py:241` [1]
- R2 logger-argument drain (116 calls), `src/decoy_engine/internal/logging.py:129` [1]
- Entry-frame scrubbing (`closed_entry`, F1, F2), `src/decoy_engine/fileaccess/_entry.py:50` [1]
- 109 non-constant audit details behind `require_admin`, `api/audit/router.py:159` [1]
- Census machinery (T0, surface JSON, T21), `platform-d8908f42.txt` [8]
- Metrics label from a class name, `api/observability/metrics.py:391` [1]
- Child stdio (8 spawn sites), `api/files/pg_pull.py:242` [1]
- CLI stderr and `print_exception`, `decoy/src/decoy/cli/scan.py:794` (verified at CLI `2577062`) [1]
- `HTTPException` detail with a class name only, `api/cloud/accounts.py:572` [1]
- Free text with no non-admin reader (`JobEstimate`, `UnmaskColumnReport`, `ExecutionEvent`), `src/decoy_engine/unmask.py:393` and three more [1]
- Engine `ValidationMessage.message`/`hint` free text for in-process callers after P7 (revision 6's E5-4), `src/decoy_engine/validation_result.py:68` [1]
- Engine narrative names table and column in the stdlib log, `src/decoy_engine/progress.py:119` [1]
- Capacity recheck logs `result.detail`, `api/scheduler/admission_pass.py:457` [1]
- Engine `raise` messages (O2), `src/decoy_engine/execution/capacity.py:353`, `:504` [1]
- Revision 5 cuts: `CheckResult.detail`, `src/decoy_engine/connectors/gcs.py:157` [1]; `CapacityEstimate.message`, `_capacity_eval.py:255` [1]; `PlanCheckError.message`, `_consolidator.py:92` [1]; child text, `src/decoy_engine/execution/_isolated_run.py:578` [6]
- `exc-copy` rows with no V1 work (r6, r7 and r8 READMEs), e.g. `api/launcher/docker_runtime.py:102` [1]
- R7 engine ratchet (E1, cut in revision 6), `src/decoy_engine/plan/_generation.py` [1]
- 10 `require_admin` details (O4), `api/seeds/admin_router.py:277` [1]
- Lane disable reasons keep `str(exc)` (reaper CAS, server log), `api/supervision/startup.py:370` [1]
- R5 misses the bulk-update form `"log": ""`, `api/jobs/job_terminal_cas.py:290` [8]
- P1, P2a, P2b one window is process only; land as one stack, `api/jobs/runner.py:68` [9]
- Cancelled jobs get the "job failed" fallback prose (F29 V2-LOG), `api/jobs/v2_orchestrator.py:315` [1]

## 16. Revision 8 change log (gates of `f1c12362`)

Revision 7's log (cut variant, F29-first, Dennis revision 6 findings): git `f1c12362`.

| Finding | Class | Resolving mechanism (sections) | Tests |
|---|---|---|---|
| V1-A (Dennis): `SafeEvent` construction can raise on terminal `Job.log` paths | 2 | `platform_event` and `append` are total; any failure writes `safe_output.invalid`; Rule 2 restated (1, 3.3, class 2 row) | 6.3 `test_invalid_event_on_terminal_path[cas, runner, reaper, startup_recovery]`; mutation "let `platform_event` raise" |
| V1-B (Codex): `QualityWarning` not value-free (Mimesis `requested_locale`) | 1 | New part (f), P9: `project_warning` at both writers and all three readers, a typed key allowlist re-derived from the 22 constructor sites, everything else dropped and counted; chosen over editing producers because stored rows need read-side projection anyway (3.11) | T-P9 with `mimesis_locale_fallback`; mutation restoring `requested_locale`; X-QW now seeds a locale marker |
| L6 (Dennis), rule 5 | 8 | `locale`, nested `target`, `join_group` and per-row positions listed as not owner names; binding, variable and secret NAMES listed as owner names (1) | T-P9 cells |
| M1: `getattr` escapes the allowlist | 8 | Probe treats `getattr` with a non-allowlisted or non-literal name as a text read; +18 rows, all value-free (botocore status and error codes, SQLSTATEs, predicates, an owner name); output sorted, byte-identical across hash seeds (L5) (1, 3.4, 6.5, `2026-11-03-f31-r8/README.md`) | `attr_getattr`, `test_census_is_seed_independent`, `selftest_r8.py` |
| M2: log rendering | 9 | `render_text` LEVEL vocabulary, `job` sentinel and `(error_class, node_id)` tail pinned; `request_id` tail named as a loss (3.1); parser characterization cells (T3); `error_projection.py` and the re-projection pattern in the reuse inventory (8) | T3 `test_render_matches_downstream_parsers` |
| M3: E0c opening and step 7 | 10 | E0c opens after R1 merged with no registry slice open, not waiting for parked R2/R3; step 7 now stops instead of re-bumping (3.9, 11.2) | none |
| M4: other notification text | 1 | P5n also removes `make_node_flagged_event` `log_message` and closes `make_flag_gate_event` `reason`; after P2a `_evaluate_flags` receives only the rendered MSG (3.3, 3.6) | T-P5n two new cells |
| L1: preflight row vs rule 5 | 8 | Every `UnresolvedVariable` producer cited; value-free under rule 5 (5.7) | none |
| L2, L3, L4 | 9, 8 | P7 deletes the dead `adapt_validation_message`; `s224_empty` x `validate` marked N/A; P4e gives both unavailable errors a `code` attribute so its 6 census rows leave the baseline (3.4, 3.5, 6.2) | T-P4e `test_mappers_read_only_code` |
| L8, L9 | 8 | Engine main `9eec6123` checked (subset only); the failed-check message is the engine template with a closed scan name, line removed from V2-LOG (header, 3.11, 15) | `test_failed_check_message_template` |
| O6 | none | Still an owner question with its recommended default; unanswered (13) | none |

Numbers that changed from revision 7: `exc-copy` rows 110 to 128 (pass 2 54 to 72, `getattr`); rows left after P4d and P4e 99 to 116 (P4e rewrites 6, not 5); V1 slices 17 to 18 and about 6,590 to 7,165 lines; P5n 2 to 3 production files; P7 1 to 2 production files and now scanned (after D.2).
