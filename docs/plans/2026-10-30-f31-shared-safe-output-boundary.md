Status: plan only, revision 5: a targeted amendment after the Dennis review of revision 4 (`d1a837b1`, GO-WITH-FIXES: 2 BLOCKER and 2 HIGH V1, 6 V2-LOG). No implementation or release approval is granted by this document.

Summary: F31 stops exception text and cell values from reaching surfaces a non-admin can see. The V1 core is one design (section 0): the `Job.log` choke point, API responses that copy exception text, the `ValidationMessage` text Platform copies from `PlanCompileError.message`, the error node of a failed V2 job, and one notifications field, each with a shrink-only ratchet and named limits. The alias, legend and run-UUID stack is an owner decision of 2026-10-30; it does not block V1 and O1 recommends cutting it (section 14).

Baselines: Platform `origin/main` `d8908f42` (revision 5 re-read every new Platform anchor there; the handoff's earlier `b290593f` is an ancestor of revision 3's base); engine plan branch `07da6ee` (engine `origin/main` `0173c05` merged into revision 3), and the engine `Logger` protocol at `d1a837b1`. CLI: not attached; CLI citations are unverified and no CLI slice is in V1.

Measurement: stdlib `ast` scans and `git grep` with the container Python 3.13.18, NOT the `uv.lock` interpreter; they import nothing. Probes and output: `docs/plans/evidence/2026-11-01-f31-r4-anchors/` (`httpexc.py`, `httpexc_kinds.py`, revision 3's `logassign.py`, `r7b.py`) and `docs/plans/evidence/2026-11-02-f31-r5-o3/` (`excbound.py`, `census-d8908f42.txt`, the O3 classification `README.md`). No test suite was run for this plan.

Rules consulted: dev-rules `00-universal.md`, `development-loop.md`, `risk-and-exceptions.md`, `scope-discipline.md`, `reuse-first.md`, and the README-selected rulebooks `security.md`, `api-and-compatibility.md`, `testing.md`, `data-and-migrations.md`, `observability-and-resilience.md`, `refactoring.md`; the dev-rules tree is not in this checkout, so they are cited from revisions 2 and 3, not re-read. Read for revision 5 from Platform `handoffs/queue`: `V1-BAR.md`, `tools/plan-lint`; `DEFECT-CATALOG.md`, `OWNER-DECISIONS-2026-10-30.md`, `V2-CORRECTIVE-BACKLOG.md` as read for revision 4; both `CLAUDE.md` files.

# F31: Shared safe-output boundary, PLAN (revision 5)

Risk: R2 (wire contract, three-repository migration, schema migrations). The owner decisions of 2026-10-30 are applied as written: three-class surface table; progress views are owner results with real names; a per-job owner-only alias legend; legacy `Job.log` rows withheld at read, never purged; run identity is a Platform-minted UUID beside `Job.id`; the F25 4c, F34 and F36 rulings in section 9. The V1 bar (owner, 2026-11-02) decides what blocks the merge.

Naming: V1 rules are Rule 1 to Rule 3 (section 1). Slices are E (engine), P (Platform), S (the owner-decided stack, both repositories), X (cross-repository). Ratchets are R5 (Platform `Job.log` writes), R7 (engine exception-text projection), `detail-exc` (Platform API details), `exc-copy` (other Platform except-bound copies), `node-error` (job error nodes) and `notify-closed` (notification text).

## 0. V1 core: one design

One idea: exception text and cell values never become user-visible bytes. Where it would be copied, exception text becomes a closed label (`safe_error_type(exc)`) or a registered reason, and free narrative becomes registered prose. One choke point and one ratchet per part (revision 4's part (d), the isolated child, is V2-LOG now; letters kept for review references):

| Part | Choke point | What changes | Ratchet | Slices |
|---|---|---|---|---|
| (a) `Job.log` | `api/jobs/job_log.py` (`append`, `clear`, `read`), new | 28 `.log` assignments in 17 files write only `decoy-safe-output/v1` lines; four readers render through `read`; an unparsable line renders as a counted `legacy_withheld`; `JobLogger` keeps the engine protocol methods, text-free (3.3) | R5: no `.log` assignment outside `job_log.py`; 28, empty after P2b | P1, P2a, P2b (one release) |
| (b) API responses | the `except` handler that builds the response | 56 `HTTPException` details in 27 files, plus the 3 except-bound copies into other non-admin responses that the O3 classification found (3.4) | `detail-exc` (baseline 56, empty after P4c); `exc-copy` (baseline 56 classified rows, shrinks by the 5 rows P4d, P7 and P8 rewrite) | P4, P4a to P4d |
| (c) Validation text | `api/pipelines/v2_validation.py:153-157` and the engine `ValidationMessage` | Platform stops copying `PlanCompileError.message` into `ValidationMessage.message`/`hint`; the engine type carries `reason: ReasonRef` | R7 (engine, test-only, baseline 45 at `07da6ee`) | E1, E5-4, P7 |
| (e) Job error node | `api/jobs/job_node_error.py` (new: `node_error`, `project_node_error`) | the error and vault nodes carry `code`, the label and registered prose; `FailureDetails` stops reading `.message`; node-runs and the manifest render through the reader (3.8) | `node-error`: `JobNodeRun(error=...)` only via `node_error`, no `.message` read; 3, empty after P8 | P8 |
| Notifications | `make_job_failed_event` (`api/notifications/dispatcher.py:187-213`) | `error_summary: str` (`:190`, into `detail` at `:202-203`) becomes a closed `FailureLabel`; no caller passes free text today (test-fire: `api/notifications/router.py:494-498`) | `notify-closed`: a `str` is rejected | P5n |

Shared engine pieces: `SafeEvent`, serializer and parser (E0a, E0b, 3.1), `safe_error_type` (3.2), the registry `platform` namespace for Platform's prose (E0c; Platform has no registry, owner decision).

**Blocks V1** (the merge gate for F31's V1 claim): E0a, E0b, E0c, E1, E5-4, ER (engine release), P1, P2a, P2b, P4, P4a, P4b, P4c, P4d, P5n, P7, P8, X.

**Owner-decided stack, kept, does not block V1** (O1 recommends CUT; section 14): S-E3a, S-E3b, S-E4, S-P0, S-P3a, S-P3b. Kept, its slices ship after their V1 dependencies under the same bar.

**Is V2** (logged in section 15, no work planned here): the R1 dynamic class-name drain, the R2 engine logger-argument drain, entry-frame scrubbing (`closed_entry`, F1, F2), the audit-detail half of revision 3's P4, the census machinery (T0, generated surface JSON, slice manifest), metrics labels (P5), child stdio (P6), CLI stderr (C1, C2), the R7 sites outside part (c), the free-text fields with no non-admin reader (`CheckResult.detail`, `CapacityEstimate.message`, `PlanCheckError.message`, `QualityWarning.detail`, `JobEstimate.notes`, `UnmaskColumnReport.detail`, `ExecutionEvent`), the isolated child's error text (revision 4's E6; value-free today), the 44 O3 rows that need no V1 work, and in-process `raise` messages.

## 1. Guarantee (V1 rules 1 to 3, ratchet plus named limits)

**Threat model (V1).** Self-hosted, single-org; the engine process is trusted; the leak is accidental (a value or path in exception text copied into output, or a free-text log line). The audience that matters is a viewer who is not the server admin. Hostile in-process code is V2.

**Rule 1 (product function).** F31 changes no masked, subset or generated output and no route selection. `Job.log` stays readable through its four readers: a v1 line renders as `ISO_TS  LEVEL  STEP  MSG`; each run of lines that do not parse renders as one counted `legacy_withheld` line, never raw and never an error. Named limit: legacy rows and unmigrated writers show as `legacy_withheld` until P2b ships, so P1, P2a and P2b ship in one Platform release (3.3). `JobLogger` keeps satisfying the engine `Logger` protocol (3.3). With the stack: one alias table per job (3.7).

**Rule 2 (data).** No F31 write can abort a job's terminal record. `job_log.append` writes in the caller's transaction and adds no failure point beyond today's string append; the stack's legend write runs in its own savepoint after the terminal commit (3.7). Legacy `Job.log` and error-node rows are never rewritten or purged.

**Rule 3 (values a non-admin can see).** No exception text and no cell value appear on: stored `Job.log` and its four readers (SSE, job `log` field, node-run logs, STORM failure summary); the 56 `HTTPException` details and the 3 P4d responses of part (b); the validation `ValidationMessage` of part (c); the stored `JobNodeRun.error` of a failed V2 job and of the vault node, `GET /jobs/{id}/node-runs` and the manifest `nodes[].error` of part (e); the `job_failed` notification `detail`. Ratchets: R5, `detail-exc`, `exc-copy`, R7, `node-error`, `notify-closed`, each shrink-only with a seeded red fixture (section 6). Named limits:

- Platform except-bound copies outside `HTTPException`: 56 rows in 29 files at `d8908f42`, all classified (`2026-11-02-f31-r5-o3/README.md`). 5 are rewritten (P4d 3, P7 1, P8 1); 7 keep their copy but their only sink is closed (`Job.log` by P2a for 6, the error node by P8 for 1); 44 reach only admin or internal surfaces, are value-free by construction, or return to the owner data the same response already carries, and stay in the `exc-copy` baseline. A callee's copy of an exception passed to it is found by reading the choke points, not by this probe (part (e) is such a callee).
- Engine R7 sites outside part (c) stay in the R7 baseline; they reach engine results with no Platform reader at `d8908f42` (O5), the isolated parent, or admin logs.
- Exceptions raised to an in-process SDK caller keep their message; 75 engine `raise` sites interpolate an except-bound name at `07da6ee`. F31 stops that text where Platform copies it into a named surface: `Job.log` (a), an API response (b), `ValidationMessage` (c), the job error node (e). Revision 4 claimed part (b) alone did; the error node disproved it. A copy into an unnamed surface is outside the claim, and `exc-copy` keeps that set from growing (O2).
- Admin-only surfaces (server logs, audit details behind `require_admin` at `api/audit/router.py:159`, traceback locals, `__cause__` chains, metrics, child stdio, CLI stderr) are V2 by the bar.
- Traceback serialization: `git grep` at both baselines finds no code that formats a traceback (`format_exc`, `print_exc`, `format_exception`, `TracebackException`, `print_exception`, `extract_tb`, `format_tb`) in Platform `api/` or engine `src/`; the hits are comments (`platform-d8908f42.txt`, `engine-07da6ee.txt`). This grep is the check; no census is built.

**In-process exception boundary.** An exception raised to an SDK caller is not a serialized surface (the caller wrote the input). Rule 3 starts where exception text would be copied into a result field, a stored line, a response body or a notification.

Non-goals: no new encryption or hashing; regex scrubbing (`api/logging/redaction.py`) stays a backstop; no `--unsafe-debug-log`.

## 2. Established solutions and design notes

Established solutions (cited again in module docstrings): RFC 9457 identity-versus-occurrence split, the F29 pattern (`src/decoy_engine/reasons/_record.py:1-17`); OpenTelemetry `error.type` as a low-cardinality identifier, the `reason_adapter` pattern (`src/decoy_engine/reason_adapter.py:1-7`); the OWASP Logging Cheat Sheet (log the event, never the data); python-json-logger's single exception field, the Platform `exc_safe` shape (`api/logging/exc_safe.py:1-18`); "Parse, don't validate" for stored-line and stored-row parsing; SQLAlchemy `Session.begin_nested()` for a best-effort write (`api/jobs/job_terminal_cas.py:256-266`); for the stack, RFC 9562 UUIDv4 bits and Alembic add-nullable, backfill, constrain (`api/migrations/versions/obs3_request_id.py:1-16`). Custom code remains only for the `decoy-safe-output/v1` record (no library knows the reason registry).

Design notes: engine package `decoy_engine.safe_output` (`_event.py`, `_exception.py`, `_wire.py`; the stack adds `_scope.py`, `_alias.py`, `_sink.py`, `_view.py`); Platform modules `api/jobs/job_log.py` and `api/jobs/job_node_error.py` (the stack adds `api/jobs/job_scope.py`). One place per fact: prose in the registry, the exception table in `_exception.py`, parsing in `_wire.from_line` and `project_node_error`, `Job.log` writes in `job_log.py`. No flag arguments.

## 3. Design

### 3.1 `SafeEvent` and its wire form (E0a, E0b)

Kept unchanged: `ReasonRecord`/`ReasonContext` (`src/decoy_engine/reasons/_record.py:115-212`), the progress projections `event_to_dict`, `event_to_legacy_dict` (`src/decoy_engine/_progress_events.py:513-599`), the `decoy-reasons/v1` identity form (`src/decoy_engine/reasons/_identity.py:19-57`).

`SafeEvent` (`safe_output/_event.py`): frozen, validated in `__post_init__` and `to_dict`, exact-type checks on nested groups as `_V1Fields` does (`_progress_events.py:262-270`). Fields: `run: uuid.UUID | None` (`None` without the stack), `seq`, `ts_ms`, `sev: reasons.Severity`, `kind: EventKind` (`NARRATIVE`, `STEP`, `TERMINAL`, `WITHHELD`, `INVALID`), `reason: ReasonRef`, `err: SafeErrorType | None`, `step: StepRef | None` (a node id approved by `approve_name`), `counts` under `COUNT_KEYS` (`_record.py:34-36`, additive), `flags: frozenset[Flag]`; the stack adds `aliases`. No free `str`, `Any` or mapping field.

Serializers (`safe_output/_wire.py`):

- `to_line(event) -> bytes`: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)` plus `\n`, at most 4096 bytes and depth 3. Over a limit it returns the registered `safe_output.invalid` event for the same `seq` with a closed `cause` count, never a truncation; if that raises, the import-time constant `_FAILED_LINE = b'{"kind":"failed","s":"decoy-safe-output/v1"}\n'`. Never raises.
- `from_line(line, *, expected_run=None) -> SafeEvent | None`: rejects over 4097 bytes before parsing; bounded parse rejecting depth over 3 and duplicate keys; closed keys; fields re-checked; reason known via `require_known`; with the stack, `run == expected_run`. Anything else, `_FAILED_LINE` included, is `None`.
- `render_text(event) -> str`: `ISO_TS  LEVEL  STEP  MSG`, MSG the registry summary plus closed facts.

### 3.2 Exception label (E0a)

`SafeErrorType` is a closed `str` enum built once at import from exact classes: builtin exception classes (as `_safe_error_names` enumerates them, `_progress_events.py:69-81`), the engine families in `reason_adapter._CODED` (`reason_adapter.py:39-85`), the classes of `decoy_engine.errors`, plus `EXCEPTION`. `safe_error_type(exc)` is `_ALLOWED_EXCEPTION_TYPES.get(type(exc), SafeErrorType.EXCEPTION)`; `SafeErrorType.from_label(s)` maps an unknown string to `EXCEPTION`. `phase.error_result` (`phase.py:594`) delegates; its value domain widens additively (section 7). Wider consumer migration (`postgres_snapshot`, `progress.run_scope`) feeds admin logs: V2-LOG.

### 3.3 `Job.log` choke point (P1, P2a, P2b)

`Job.log` (`api/models.py:424`) keeps its `Text` column and, from P1, holds only v1 lines written by `job_log.append(db, job, event)`; resets become `job_log.clear(db, job)`. Platform builds events with `job_log.platform_event(reason, *, sev, err=None, step=None, counts=())`, `reason` from the engine's `platform` namespace (E0c). `job_log.read(job)` parses with `from_line` and renders with `render_text`; each run of failing lines is one counted `safe_output.legacy_withheld` line. The four readers call it: SSE (`api/jobs/routes_streaming.py:92-101`, `data: <text>` kept for `web/src/hooks/useLiveJob.ts:215`), the job `log` field (`api/jobs/_shared.py:203-211`, 256 KiB tail cap on rendered text), node runs (`api/jobs/routes_runtime_facts.py:96`, grouped by `step`), the STORM failure summary (`api/storm/router.py:1707`).

Writers (5.6) move to `append` in P2a and P2b, emitting registered prose plus closed facts; a writer that appends a caller's `reason: str` drops it (`_fail_host_safety`, `api/jobs/queue_worker.py:378-386`, receives exception text from six O3 rows, e.g. the PyYAML error of `api/jobs/worker_budget.py:397`). In P2a `JobLogger._append_narrative` (`api/persistence/logger.py:426-460`) writes a `NARRATIVE` event (`platform.job_narrative`, level, step) and drops the message; signatures are unchanged, so the constructors (`api/jobs/route_narrative.py:223`, `api/jobs/v2_orchestrator.py:377`, `api/storm/router.py:1031`) and the engine handoff are untouched.

`JobLogger` is also an engine logger: the engine `Logger` protocol (`src/decoy_engine/context.py:24-46`, `runtime_checkable`) requires `debug`, `info`, `warning`, `error`; STORM passes `JobLogger` as `ExecutionContext(logger=...)` (`api/storm/router.py:1169`) and the profiler calls `logger.info(...)` (`src/decoy_engine/profiling/profiler.py:493`). P2b KEEPS the four methods, text-free as in P2a, and migrates every Platform CALL of them to `job_log.append`; an AST sentry (T-P2r) then forbids a Platform call. V2 jobs pass no logger to `run_pipeline` at `d8908f42` (`api/jobs/v2_runner.py:242-262`); the V2 gate cell uses `JobLogger` as `ctx.logger` anyway.

Release unit ([9]): from P1 an unmigrated writer's line renders `legacy_withheld`, so no Platform release is cut between P1 and P2b; P2b's gate runs T-P1 and T-P2 on the merged result. Each intermediate commit passes because withholding is the safe direction. Rule 2: `append` is a string append in the caller's transaction (as at `api/jobs/runner.py:65-70`, `api/jobs/job_terminal_cas.py:256`); `to_line` always returns bytes, so `append` cannot raise on content.

### 3.4 API responses (P4, P4a to P4d)

At `d8908f42`, 60 `HTTPException` calls inside `except ... as NAME` read `NAME` in their detail (`httpexc_kinds.py`): 56 copy its text in 27 files, 3 read only `NAME.code`, 1 only `type(NAME).__name__` (V2-LOG). P4 lands `detail-exc` (tests only, baseline 56, seeded fixture); P4a to P4c rewrite each site to constant prose plus `safe_error_type(NAME)`, keeping status codes and shapes, admin-only routes included (O4).

O3, done in revision 5: `excbound.py` finds 56 except-bound copies outside `HTTPException`; the README beside it classifies every row. P4d rewrites the three that reach a non-admin response outside the other choke points to registered prose plus the label: `api/pipelines/router.py:717` (validate `message`, `f"preflight failed unexpectedly: {exc}"`) and `api/workflows/runner.py:225`, `:238` (`WorkflowRun.summary` on `StoredConfigTooLargeError`, `ValueError`, `yaml.YAMLError`; a PyYAML marked error quotes the config line). P4d also lands `exc-copy`, the probe as a sentry reusing P4's matcher, its baseline the rows present at P4d's base; a slice that rewrites a row (P4d 3, P7 1, P8 1) removes it, and the 7 rows whose sink P2a or P8 closes stay, annotated. The iteration preview (`api/pipelines/router.py:478`, `:483`) stays V2-LOG: an owner-only route (`get_owned_pipeline_or_404`, `:464`) about the owner's own `iteration_config` that persists nothing and whose success form returns the expanded `values` themselves.

### 3.5 Validation text (E5-4, P7)

`ValidationMessage.message` and `.hint` (`src/decoy_engine/validation_result.py:68`, `:71`) are filled by Platform from `PlanCompileError.message` (`api/pipelines/v2_validation.py:153-157`) and returned in the pipeline validation response. E5-4 replaces them with `reason: ReasonRef`; `ValidationResult.add_error(code, *, path, node_id)` and `add_warning` lose `message` and `hint` (pre-GA). P7 moves Platform to it. R7 lands in E1 as a test-only ratchet (baseline 45 at `07da6ee`) and matches `str(f(NAME))`.

Moved to V2-LOG in revision 5 (to keep the total flat, section 11): E5-1 (`CheckResult.detail`), the `CapacityEstimate` and `PlanCheckError` half of E5-4, and E6 (the isolated child's text). None has a non-admin reader at `d8908f42`: an import scan finds no Platform reader of the three types (O5), and the child's text is a class name plus trusted code today (`_isolated_worker.py:47-49`); where the governor raises it (`_governor.py:550`), parts (a), (b) and (e) stop the copy.

### 3.6 Notifications (P5n)

`make_job_failed_event` (`api/notifications/dispatcher.py:187-213`) appends `error_summary` to the notification `detail` that reaches Slack, email and webhooks. No job path passes it (`api/jobs/runner.py:420-424`); the test-fire passes a constant (`api/notifications/router.py:494-498`). Nothing leaks now and nothing stops a future leak (class 4, silent route). Fix: `error_summary: FailureLabel | None`, a closed enum (`test_fire`, plus the labels a future caller needs, added through review); `detail` renders the label's fixed prose.

### 3.7 Owner-decided stack: run identity, one scope per job, aliases, legend (S slices)

- Run (S-P0): Platform mints `uuid.uuid4()` (`api/jobs/run_identity.py:mint_run_uuid`) at job creation into `jobs.run_uuid` (`String(36)`, unique, not null after backfill) beside `Job.id` (`api/models.py:329`); `@validates` and a CHECK accept only canonical v4; request schemas exclude it; an AST sentry forbids other writers.
- One scope per job (V1-2). The job's post-success hooks make their own engine calls (`api/jobs/runner.py:287-326`), so the only Platform owner of a scope is `api/jobs/job_scope.py:job_output_scope(db, job_id)` (sole caller of `SafeOutputScope.for_run`, AST sentry), entered once, outermost, at the three lifecycle roots: `api/jobs/runner.py:run_job` (`:443`; the worker thread at `:556-565` inherits it through `contextvars.copy_context()`, `api/jobs/_dispatch_supervisor.py:219-220`, and the hooks at `:576` share it), `api/jobs/dispatch_worker_main.py:run_v2_lifecycle_in_worker` (`:350`, hooks at `:505`) and `api/storm/router.py:run_storm_job` (`:984`). Iteration children get their own scope through `run_job`. Same-run re-entry reuses the bound scope; another run while bound raises `ScopeConflict`.
- Coordinator and aliases (S-E3a, S-E3b): engine entries hold `scope.coordinate()`; only the holder allocates; a concurrent second holder gets `kind-0` and `alias_unknown`. Identity `(run, kind, ordinal)`, kinds `table`, `field`, `node`, `artifact`, cap 4096 per kind; lifecycle `OPEN`, `LENT(k)`, `FROZEN` as in revision 3 (snapshot lend to the isolated child, contiguous `merge`, `merge_lost`, `AliasTableBusy`), per job scope.
- Legend (S-P3b): `job_alias_legend(job_id, kind, ordinal, name)`, UNIQUE `(job_id, kind, ordinal)`, CHECK `ordinal >= 1`, written once at scope exit after the root commits the terminal record, inside `db.begin_nested()` (delete, insert), then `db.commit()`; on a savepoint failure only the savepoint rolls back and `append` writes `safe_output.legend_unavailable`. `job_log.clear` deletes the job's legend rows. Read only by `GET /api/jobs/{id}/alias-legend` behind `require_job_owner_or_404`.
- Run binding (S-P3a): `job_log.append` always stamps `SafeEvent.run` from the row it writes (`job.run_uuid`), never from the scope; the scope supplies aliases only, and a writer outside any scope writes no aliases. The writers outside the three roots (`api/jobs/reaper.py:233`, `api/main.py:1043`, `:1051`, `api/jobs/queue_worker.py:385`, `:604`, `api/jobs/scheduler_claim_loop.py:98`, `api/jobs/iteration.py:504-674`, `api/jobs/iteration_dispatch.py:426`, `api/workflows/restart_recovery.py:48`) therefore parse under run binding. `job_log.read` passes `expected_run=job.run_uuid`; an invalid stored `run_uuid` renders withheld as `safe_output.run_identity_invalid`.

### 3.8 Job error node (P8)

A failed V2 job stores one error node: `api/jobs/v2_orchestrator.py:286-319` calls `_failure_details(exc).report(...)`; `_failure_details` takes `getattr(selected, "message", None)` from the first coded exception in the `__cause__` chain (`api/jobs/_partial_publication.py:201`), `report` returns it (`:161`) and `_error_node_run` stores it in `JobNodeRun.error` (`api/jobs/v2_node_runs.py:434`); the vault node stores `str(exc)` (`api/jobs/v2_vault.py:97`). The owner reads the row through `GET /jobs/{id}/node-runs` (`api/jobs/routes_runtime_facts.py:70-115`) and the manifest copies it into `nodes[].error.message` (`api/evidence/assembly.py:1968-1986`, raw for a non-JSON row). A coded error whose message carries a value (a `SubsetError`, which `api/subset/router.py:85` and `:109` say can embed a submitted value or an absolute path; a `PlanCompileError` with a config literal; the `FixedWidthWriteError` text of `api/jobs/fixed_width_output.py:225`) lands in all three.

P8 adds `api/jobs/job_node_error.py`. `node_error(code, exc, *, where=None) -> str` builds `{"code", "error_type", "message", "where"}`: `error_type` is `safe_error_type(exc)`, `message` the registered prose of `platform.job_failed` (E0c), never exception text. `FailureDetails` loses `message`; `report` returns the code; `_error_node_run` and the vault node call `node_error`. `project_node_error(raw)` re-projects a stored row on read, as `reproject_persisted_exports` does (`routes_runtime_facts.py:105-115`): unknown keys dropped, `message` re-rendered from the registry, `error_type` through `SafeErrorType.from_label`, a non-JSON row becomes `node.runtime_error` with no text. The node-runs route and `assembly.py` call it, so legacy rows are withheld at read, never rewritten. `where` keeps `table=<name>` (an owner name). Ratchet `node-error` (AST): every `JobNodeRun(...)` with `error=` in `api/` passes `node_error(...)` (baseline 2: `v2_node_runs.py:434`, `v2_vault.py:97`), and `_partial_publication.py` and `job_node_error.py` read no `.message` (baseline 1: `:201`); empty after P8.

## 4. Defect-class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | The V1 choke points (section 0) with closed labels and registered prose; ratchets R5, `detail-exc`, `exc-copy`, R7, `node-error`, `notify-closed` | T-P1 marker corpus over the four readers, T-P2, T-P4, T-P4d, T-P7, T-P8, T-P5n | let `_append_narrative` keep the message; restore `detail=str(exc)` at `api/pipelines/router.py:100`; restore `getattr(selected, "message")` in `_failure_details` |
| 2 | Cleanup missing on an exit path | `append` adds no failure point to the terminal path; `node_error` cannot raise on content; with the stack, the legend write runs in its own savepoint after the terminal commit, and `job_output_scope` unbinds on all exits | T-P2 terminal cells; T16 legend failure; S exit matrix | drop `begin_nested` around the legend insert (T16 loses the `legend_unavailable` line on Postgres); drop the `finally` unbind (nested-run cell sees the previous scope) |
| 3 | State machine and concurrency | Stack: one scope per job at three roots; one coordinator at a time; `OPEN`/`LENT`/`FROZEN` with contiguous merge | T22 multi-call job; T15 multi-child cells | open a scope per engine call (T22 sees two ordinals for one table); let a second coordinator allocate (T15 ordinals diverge) |
| 4 | Route parity and silent fallback | One `Job.log` reader for the four stored-line routes; one error-node reader for the node-runs route and the manifest; a withheld line is counted, never dropped; the notification summary is a closed type | T3 over four readers; T-P8 over both error-node readers; T-P5n | skip `from_line` in one reader (T3 red on that reader); skip `project_node_error` in `assembly.py` (T-P8 legacy cell red); accept a `str` `error_summary` (T-P5n red) |
| 5 | Referential integrity | N/A: F31 changes no FK semantics; FK reports keep their fields and are not on a V1 surface | FK-shape output matrix (6.4) stays byte-identical | change an FK report field: the matrix fails |
| 6 | Untrusted producer | `from_line` bounded and closed; a stored error-node row is re-projected on read, not trusted | hostile-line corpus (T14); T-P8 legacy and tampered rows | accept an unknown key in `from_line`; serve the stored `message` in `project_node_error` |
| 7 | TOCTOU / check-then-use on a path | N/A: F31 opens no path after a check and adds no file writer | none needed | none |
| 8 | Weak tests and overclaimed evidence | Each count cites a committed probe at a named commit; positive control in each recorder; ratchets shrink only; the named limits are written in section 1 instead of a completeness claim | recorder self-test with a disconnected sink; baseline-growth test per ratchet | add a baseline entry: the growth test fails |
| 9 | Behavior-preserving refactor drift | Characterization first for the four `Job.log` readers, node-run grouping and `phase.error_type`; `JobLogger` keeps the engine `Logger` protocol; P1, P2a, P2b ship in one release | snapshots taken before P1 and E0a; T-P2r STORM-scan and V2-job cells | group node runs by a different key; delete `JobLogger.info` (STORM cell red); cut a release between P1 and P2b |
| 10 | Frozen contract drift | Section 7 with value semantics; `decoy-safe-output/v1` additive only; result-field changes recorded as pre-GA breaks | T5 value-domain snapshots | change a progress value domain: T5 fails |

## 5. Enumerations from the code

### 5.1 Exit paths

- `Job.log` append on the terminal path: success, failure, cancel and reaper (`api/jobs/job_terminal_cas.py:256-266`, `api/jobs/reaper.py:233`, `api/jobs/runner.py:661`).
- Serializer: normal, substitute on a limit, double fault (`_FAILED_LINE`); parser rejection (3.1).
- Failed V2 job: error node from a coded chain, an uncoded exception, a cancel; vault write failure (`api/jobs/v2_orchestrator.py:286-319`, `api/jobs/v2_vault.py:91-98`).
- Stack: scope exit (normal, exception, nested, cancel while the worker thread still runs), alias lend, merge, merge-lost, freeze, legend savepoint failure.

### 5.2 Routes

- Stored-line routes: SSE (`api/jobs/routes_streaming.py:92`), job `log` field (`api/jobs/_shared.py:203`), node runs (`api/jobs/routes_runtime_facts.py:96`), STORM summary (`api/storm/router.py:1707`).
- Job lifecycle roots: flag-off `run_job` (`api/jobs/runner.py:443`), flag-on worker `run_v2_lifecycle_in_worker` (`api/jobs/dispatch_worker_main.py:350`), STORM `run_storm_job` (`api/storm/router.py:984`), iteration parent and children (`api/jobs/iteration.py:421`, `api/jobs/iteration_dispatch.py:402`).
- Error-node routes: `GET /jobs/{id}/node-runs` (`api/jobs/routes_runtime_facts.py:70`), evidence manifest `nodes[].error` (`api/evidence/assembly.py:1968`).
- P4d routes: pipeline validate (`api/pipelines/router.py:717`), workflow runs (`api/workflows/router.py:465`).

### 5.3 FK shapes

- Single and composite edges, `RelationshipEdge` (`src/decoy_engine/relationships/_graph.py:72-92`).
- Multi-parent children and children in later streamed batches (`src/decoy_engine/execution/out_of_core/_multi_parent.py:49-106`). F31's V1 slices touch no execution path; 6.4 checks the relational output stays byte-identical.

### 5.4 Fields

- V1: `ValidationMessage.message`/`hint` (`src/decoy_engine/validation_result.py:68`, `:71`); Platform `Job.log` (`api/models.py:424`), `JobNodeRun.error` (`api/jobs/v2_node_runs.py:434`, `api/jobs/v2_vault.py:97`), `WorkflowRun.summary` (`api/workflows/runner.py:225`, `:238`), validate `messages[].message` (`api/pipelines/router.py:717`), `error_summary` (`api/notifications/dispatcher.py:190`).
- V2-LOG (no non-admin reader at `d8908f42`): `CheckResult.detail` (`src/decoy_engine/sdk.py:123`), `CapacityEstimate.message` (`src/decoy_engine/execution/out_of_core/_capacity_eval.py:153`), `PlanCheckError.message` (`src/decoy_engine/plan/validate/_consolidator.py:47`), `IsolatedRunResult.error` (`src/decoy_engine/execution/_isolated_common.py:369`), and the free-text fields of section 15.

### 5.5 Output surfaces

| # | Surface | Source | Who sees it | V1 | Rule |
|---|---|---|---|---|---|
| S1 | Progress dicts v0, v1 | `src/decoy_engine/_progress_events.py:581-599` | job viewers | unchanged | T5 |
| S6, S14 | `ValidationMessage`, in the validation response | `src/decoy_engine/validation_result.py:68`, `api/pipelines/v2_validation.py:153-157` | SDK and API callers | yes | R7, T-E5, T-P7 |
| S13 | API error details | 56 sites in 27 files (`platform-d8908f42.txt`) | API callers | yes | `detail-exc` |
| S15, S16 | Stored `Job.log` and its readers | `api/models.py:424`, 5.6, 5.2 | job viewers | yes | R5, T3 |
| S25 | `job_failed` notification `detail` | `api/notifications/dispatcher.py:187-213` | Slack, email, webhook | yes | `notify-closed` |
| S26 | Job error node | `api/jobs/v2_node_runs.py:434`, `api/jobs/v2_vault.py:97` | job owner | yes | `node-error`, T-P8 |
| S27 | Other except-bound response copies | `api/pipelines/router.py:717`, `api/workflows/runner.py:225`, `:238` | pipeline, workflow owners | yes | `exc-copy`, T-P4d |
| S22 | Alias legend route | new | job owner | stack | T12 |
| S2-S4, S8, S10, S18-S21, S24 | Isolated child text, engine and server logs, audit, metrics, child stdio, CLI stderr, traceback locals | revision 3 section 5.5 | admin or operator | V2-LOG | section 15 |

### 5.6 Platform `Job.log` writers (assignment sites at `d8908f42`)

28 direct assignments in 17 files (`logassign.py`): `api/jobs/binding_resolve.py:429`; `api/jobs/iteration.py:504`, `:537`, `:551`, `:654`, `:674`; `api/jobs/iteration_dispatch.py:160`, `:426`; `api/jobs/job_principal.py:94`; `api/jobs/job_terminal_cas.py:256`; `api/jobs/queue_worker.py:385`, `:604`; `api/jobs/reaper.py:233`; `api/jobs/runner.py:67` (`_append_log`, called at `:128`, `:141`, `:168`, `:502`, `:536`, `:585`), `:661`; `api/jobs/scheduler_claim_loop.py:98`; `api/main.py:1043`, `:1051`; `api/persistence/logger.py:448` (`JobLogger._append_narrative`); `api/quality/runner_hook.py:707`; `api/storm/post_mask_hook.py:372`, `:373`; `api/storm/router.py:588`, `:699`, `:1023`; `api/synth/runner_hook.py:333`; `api/workflows/restart_recovery.py:48`; `api/workflows/runner.py:90`. R5 lands in P1 with these as its baseline.

## 6. Acceptance tests, red first

Each test is written before its slice's production change and recorded red at the named fail-before location, run from `uv.lock` (`uv sync --locked`); for a new mechanism the named mutation is the semantic proof, recorded red.

### 6.1 Seeded corpus

Markers in exception messages (foreign library, `PlanCompileError` literal, `SubsetError`, `OSError` path) and a schema-name canary. Each recorder first asserts a registered control event arrived, then marker absence over response bytes, stored `Job.log` text, stored error-node rows, rendered reader output and notification payloads.

### 6.2 Tests (each RED first)

| # | Break | RED-first test (file::function) | Fail-before | Mutation that must turn it red | Slice |
|---|---|---|---|---|---|
| T3 | A reader replays raw text from `Job.log` | Platform `tests/test_job_log_reader.py::test_each_reader_withholds_invalid_lines[sse, log_field, node_runs, storm_summary]`: legacy free-text row with a marker, a 4097-byte line, an unknown key; positive control: a valid line renders | `api/jobs/_shared.py:203-211` (raw text returned) | skip `from_line` in one reader | P1 |
| T-P1 | A writer stores exception text | Platform `tests/sentry/test_job_log_writes.py::test_no_log_assignment_outside_job_log` (R5, baseline 28) and `::test_seeded_assignment_is_red` over `tests/sentry/fixtures/job_log/leaky_writer.py` | `api/jobs/runner.py:67` (`_append_log` assigns `job.log`) | add a `.log` assignment in a fixture-free module | P1 |
| T-P2 | A writer keeps message text | Platform `tests/test_job_log_writers.py::test_narrative_drops_message` (`JobLogger(...).error(f"failed on {MARKER}")`: the line parses as `platform.job_narrative`, no marker), one cell per migrated writer site driving its path with a marker exception, and `::test_host_safety_drops_reason` (a `yaml_snapshot` whose parse error quotes a marker, rejected via `worker_budget.py:397` and `_fail_host_safety`) | `api/persistence/logger.py:448`; `api/jobs/queue_worker.py:385` | keep `msg` in `_append_narrative`; keep `reason` in `_fail_host_safety` | P2a, P2b |
| T-P2r | Release split ([9]); engine protocol kept (V1-C) | Platform `tests/test_job_log_release_unit.py`: `test_no_writer_left_unmigrated` (R5 empty); `test_no_platform_call_to_narrative_methods` (AST: no `.debug`/`.info`/`.warning`/`.error` call on a `JobLogger(...)` expression or a name bound from one, in `api/`; seeded fixture); `test_job_logger_satisfies_engine_protocol`; `test_storm_scan_with_job_logger` (`run_storm_job` reaches `profile_columns`; the job ends `success`, its `Job.log` parses, one `platform.job_narrative` line from the profiler, no marker from a marker column name); `test_v2_job_with_job_logger_as_ctx_logger` (success and failure). In P2b's gate on the merged P1+P2a+P2b result | `api/persistence/logger.py:152-205` (Platform callers present) | delete `JobLogger.info` (STORM cell `AttributeError`); add a Platform `.info(...)` call (AST red) | P2b |
| T-P4d | An except-bound copy reaches another response (O3) | Platform `tests/sentry/test_exc_copy.py`: `test_no_unclassified_copy` (`exc-copy`, baseline the `census-d8908f42.txt` rows present at P4d's base, stale rows fail), `test_seeded_copy_is_red` (`return Out(error=str(exc))`, `obj.summary = f"x: {exc}"`, `items.append(exc.message)`); `tests/test_exc_copy_routes.py::test_response_has_no_marker[validate_yaml_717, workflow_225, workflow_238]` (preflight raises `RuntimeError(MARKER)`; `_build_step_config_yaml` raises `StoredConfigTooLargeError(MARKER)` or a `yaml.YAMLError` quoting a MARKER line; positive control: label and registered prose present) | `api/pipelines/router.py:717`, `api/workflows/runner.py:225`, `:238` | restore `{exc}` at one site | P4d |
| T-P8 | The job error node copies exception text (V1-A) | Platform `tests/test_job_node_error.py`: `test_failed_v2_job_error_node_has_no_marker[subset_error, plan_compile_error, fixed_width]` (a marker `SubsetError` or `PlanCompileError` under a `PartialPublicationError`; no marker in the stored `JobNodeRun.error`, the `/node-runs` body or the manifest `nodes[].error`; positive control: engine `code`, `safe_error_type` label and `platform.job_failed` prose present); `test_vault_node_has_no_marker` (`OSError(MARKER)`); `test_legacy_row_withheld_at_read[json, free_text, extra_key]` (rows stored as at `d8908f42` with MARKER, both readers); `tests/sentry/test_node_error.py` (`node-error`, seeded fixtures) | `api/jobs/_partial_publication.py:201`, `api/jobs/v2_vault.py:97` | restore `getattr(selected, "message")`; serve the stored `message` in `project_node_error` | P8 |
| T-P4 | An API detail copies exception text | Platform `tests/sentry/test_detail_exc.py::test_detail_reads_no_exception_text` (baseline 56), `::test_seeded_detail_is_red` (`detail=f"bad: {exc}"`, `detail=str(exc)`, `detail=exc.args[0]`); per drain slice `tests/test_detail_exc_routes.py::test_route_detail_has_no_marker[<file>:<line>]`, one cell per site, dependency raising `RuntimeError(MARKER)` | `api/pipelines/router.py:100` and the other 55 sites | restore `str(exc)` at one site | P4, P4a to P4c |
| T-E5 | `ValidationMessage` carries free text | engine `tests/unit/safe_output/test_free_text_fields.py::test_validation_message_text_comes_from_registry` (`add_error(code, message=MARKER)` is a `TypeError`; positive control: a registered code renders its prose) | `validation_result.py:102` | restore the `message` parameter | E5-4 |
| T-E1 | R7 misses a projection shape | engine `tests/sentry/test_exception_text_projection.py::test_seeded_shapes_are_red` over fixtures `str(e)`, `str(wrap(e))`, `e.args`, `e.message`, f-string, `%`, `.format`, each inside and outside `raise` (inside `raise` is allowed); `::test_baseline_only_shrinks` | new rule (baseline 45 at `07da6ee`) | drop the `str(f(NAME))` matcher: the `str(wrap(e))` fixture passes | E1 |
| T-P5n | A notification carries free text | Platform `tests/test_notifications_failed_event.py::test_error_summary_is_closed_label`: passing a `str` raises `TypeError`; the test-fire renders fixed prose; a `job_failed` event from a job that failed with a marker exception has no marker in `detail` | `api/notifications/dispatcher.py:190` (`error_summary: str`) | widen the type back to `str` | P5n |
| T-P7 | Platform copies `PlanCompileError.message` into a validation response | Platform `tests/test_v2_validation_text.py::test_validation_response_has_no_compile_message`: a pipeline whose `when` literal is a marker; the validation response lists the registry prose and the code | `api/pipelines/v2_validation.py:153-157` | pass `exc.message` through again | P7 |
| T5 | A key snapshot passes while value meaning changes | engine `tests/unit/safe_output/test_value_domains.py`: progress keys and values unchanged; `phase.error_type` domain is a superset of the 15 labels | `src/decoy_engine/phase.py:594` | change a label value | E0a |
| T14 | Serializer limits or the double fault leak | engine `tests/unit/safe_output/test_to_line_limits.py`: `test_exact_4096_bytes_passes`, `test_4097_bytes_becomes_invalid`, `test_depth_3_passes_depth_4_becomes_invalid`, `test_double_fault_emits_failed_literal`, `test_from_line_rejects_failed_literal`, hostile-line corpus (duplicate keys, unknown key, subclass values) | new module (`ModuleNotFoundError: decoy_engine.safe_output` at `07da6ee`) | truncate instead of substitute; let the double fault raise | E0b |
| T11 | Run identity forged or malformed (stack) | Platform `tests/test_run_identity.py` (backfill, CHECK, `@validates`, request rejection, AST sentry) | `api/models.py:329` (no `run_uuid`) | accept a v1 UUID in `@validates` | S-P0 |
| T22 | One job, several engine calls (V1-2); unscoped writers (V1-D) | Platform `tests/test_job_output_scope.py`: `test_unscoped_writers_parse_under_run_binding[reaper, startup_recovery, queue_worker]` (`reaper.py:233`, `main.py:1043`, `queue_worker.py:385` with no scope bound render, not withheld); `test_one_scope_spans_run_and_hooks[flag_off, flag_on]` (`customers`, `orders` in `run_pipeline`, `customers` again in the hooks: one alias per table, one legend row per `(kind, ordinal)`, names match the lines); `test_iteration_children_get_own_scopes`, `test_storm_job_single_scope`, `test_second_run_while_bound_raises`, `test_only_job_scope_constructs_scopes` (AST) | new mechanism (per-call scopes at `api/jobs/runner.py:576`, `api/jobs/dispatch_worker_main.py:505`) | stamp `run` from the scope; enter the scope inside `_run_post_success_report_hooks` | S-P3a |
| T9, T15 | Child and parent ordinals disagree (stack) | engine `tests/unit/safe_output/test_alias_handoff.py` (revision 3 cells: sequential children, reroute with `merge_lost`, multi-table split, allocation while `LENT`, second lend, non-contiguous merge) | new mechanism | freeze at the first `lend()`; skip the contiguity check | S-E3a, S-E3b |
| T12 | Legend leaks or mismatches (stack) | Platform `tests/test_job_alias_legend.py::test_owner_only_and_matches_lines` | new route | drop `require_job_owner_or_404` | S-P3b |
| T16 | A failed legend write disturbs the terminal record (stack) | Platform `tests/test_job_alias_legend.py::test_terminal_survives_legend_write_failure` (`pg_semantic` and SQLite): ordinal 0 violates the CHECK; the job stays terminal, `Job.log` keeps its terminal line plus `legend_unavailable`, no legend rows; positive control: a valid legend is stored | `api/jobs/job_terminal_cas.py:256-266` | remove `begin_nested` | S-P3b |

### 6.3 Exit-path matrix

`tests/unit/safe_output/test_exit_matrix.py::test_exit_paths` (engine) over 5.1's serializer rows; Platform `tests/test_job_log_exit_paths.py` over 5.1's terminal rows (success, failure, cancel, reaper): the terminal line is the last parsed line and the status commits; T-P8 covers 5.1's error-node rows. With the stack, `tests/test_job_output_scope.py::test_exit_paths` adds scope exits (normal, exception, nested, cancel).

### 6.4 FK-shape output matrix

For each shape in 5.3 on the relational routes: relational output is byte-identical on the released engine (ER) and its predecessor, run in X.

### 6.5 Ratchets

Each ratchet mirrors the module-size census (`tests/sentry/test_module_size.py:66`): counts only shrink, stale entries fail, a seeded fixture proves the rule fires. R5 (Platform, P1, baseline 28, empty after P2b); `detail-exc` (Platform, P4, baseline 56, empty after P4c); `exc-copy` (Platform, P4d, baseline the 56 rows less any already rewritten at P4d's base, 51 after P4d, P7 and P8; the 51 are the named limit); `node-error` (Platform, P8, baseline 3, empty after P8); R7 (engine, E1, baseline 45 at `07da6ee`; the rest is a named limit); `notify-closed` (Platform, P5n, a type test, no baseline).

## 7. Frozen contracts touched

| Contract | Change |
|---|---|
| `decoy-engine` public API | Additive `decoy_engine.safe_output` (`SafeEvent`, `SafeErrorType`, `safe_error_type`, `to_line`, `from_line`, `render_text`). Pre-GA breaks (`src/decoy_engine/release.py`): `ValidationMessage.message`/`hint` replaced by `reason`; `ValidationResult.add_error`/`add_warning` lose `message` and `hint` (3.5). Version: the next engine minor from the release lineage; consumers pin exactly that |
| Reason registry 1.0.0 (`src/decoy_engine/reasons/_registry.py:24`) | Additive `platform` and `safe_output` namespaces; minor bump to 1.1.0 per `tests/unit/test_reason_registry.py:151` |
| Progress v0/v1 | Unchanged |
| `phase-result/v1`, `phase-event/v1` | Keys unchanged; `error_type` domain widens additively |
| Platform OpenAPI and web client | Shapes unchanged; content: `log` and SSE lines are rendered v1 records; the 59 rewritten strings lose exception text; `ValidationMessage` and notification text are registered prose; `JobNodeRunOut.error` and manifest `nodes[].error` carry `code`, `error_type`, registered `message`, `where` (no key removed). Stack adds `jobs.run_uuid`, `job_alias_legend`, `job_node_runs.node_alias`, `GET /api/jobs/{id}/alias-legend` |
| `decoy-safe-output/v1` (new) | Additive only after release |

## 8. Reuse inventory

| Existing | Source | Use |
|---|---|---|
| F29 registry, records, identity form | `src/decoy_engine/reasons/_registry.py:24-38`, `_record.py:115-260`, `_identity.py:19-57` | Prose authority, `ReasonRef`, `COUNT_KEYS`, `approve_name` |
| Reason adapter | `src/decoy_engine/reason_adapter.py:146` | `ValidationMessage.reason` from a `PlanCompileError` code (P7) |
| STORM exact-type table | `src/decoy_engine/phase.py:555-595` | Lifted into `safe_output._exception`; `phase` delegates |
| Module-size census ratchet | `tests/sentry/test_module_size.py:66` | Baseline pattern for the ratchets |
| Terminal savepoint | `api/jobs/job_terminal_cas.py:256-266` | Pattern for the stack's legend write |
| Platform `exc_safe` | `api/logging/exc_safe.py:76` | Same label shape for API details |
| Persisted-exports re-projection | `api/jobs/routes_runtime_facts.py:105-115` | Pattern for `project_node_error` on read (P8) |
| Failure projection | `api/persistence/error_projection.py:1-28` | Its rule (approved code selects static prose, else the type label) is the one `node_error` applies |
| Alembic backfill pattern | `api/migrations/versions/obs3_request_id.py` | `run_uuid` migration (stack) |
| `pg_semantic` marker | Platform `pyproject.toml` | T16 Postgres leg must run, not skip |

## 9. Consumer promises

- F25 4c (owner): `scratch._os_error.classify_os_error` is a reviewed exception boundary; R7 allows `.errno` there only; F31 rebases onto 4c-5a.
- F34 (owner): F31 promises `safe_error_type`, `SafeErrorType` and additive `COUNT_KEYS`; with the stack, `SafeOutputScope`. Not promised: sealed payloads, child fd bootstrap, an atomic ordinary-file sink.
- F36 (owner): both reason codes engine-owned in `decoy-reasons/v1`; F31 keeps `families/invariants.py` and closed count names.
- A-4 / B-5: entry-frame scrubbing is V2-LOG; the operator control (`docs/security/error-reporting-and-data-exposure.md:23-24`) stays the stated coverage.
- STORM: `safe_error_type` exported; `phase.error_result` delegates; phase schemas frozen.
- F30 and F32: with the stack, the run UUID is the correlation id and F32 reads stored lines through `from_line(expected_run)`; under the cut variant they key by `Job.id`.

## 10. Pre-mortem

| Likely gate finding | Class | Prevention |
|---|---|---|
| A reader replays a stored line raw | 4, 6 | one reader, T3 over four readers |
| A Platform release ships between P1 and P2b and `Job.log` shows mostly `legacy_withheld` | 9 | one merge window, T-P2r in P2b's gate |
| A rewritten detail still carries text through a helper (`str(wrap(exc))`) | 1 | `detail-exc` and R7 match `str(f(NAME))`; seeded fixture |
| The plan claims a drain it did not finish | 8 | ratchet baselines are the check; named limits written in section 1 |
| A job's engine calls open separate scopes (stack) | 3 | `job_output_scope` at three roots, AST sentry, T22 |
| A legend failure disturbs the terminal record (stack) | 2 | written after the terminal commit, in a savepoint; T16 on Postgres |
| A notification caller passes exception text later | 4 | closed `FailureLabel`, T-P5n |
| A copy into a surface no part names (revision 4's error-node miss) | 1 | `exc-copy`; `project_node_error` on read; T-P8 end to end |
| Removing `JobLogger` methods breaks the engine handoff | 9 | protocol methods kept; T-P2r STORM-scan cell |

## 11. Slices

Size bound: at most 10 production files and 800 changed lines per slice, tests included (`git diff --stat`, `uv.lock` excluded); a slice over it is split before merge and this table updated. Sizes are planning ESTIMATES (no code written): sites times rewrite size, plus the named test cells. Each slice: RED tests first with output recorded, smallest change, self-check, review, at most two remediation rounds.

| Slice | Repo | Scope | Prod files | Est. prod lines | Est. test lines | Est. total | Blocks V1 | Depends on |
|---|---|---|---|---|---|---|---|---|
| E0a | engine | `safe_output/__init__.py`, `_event.py`, `_exception.py` (table lifted from `phase`), `phase.py` delegation; T5, field walk | 4 | 300 | 300 | 600 | yes | none |
| E0b | engine | `_wire.py` (`to_line`, `from_line`, `render_text`), export; T14 | 2 | 260 | 380 | 640 | yes | E0a |
| E0c | engine | `reasons/platform/`, `reasons/safe_output/`, `reasons/_registry.py` 1.1.0 (prose for 5.6, the narrative, notification labels, `platform.job_failed`, P4d) | 4 | 230 | 120 | 350 | yes | E0a |
| E1 | engine | R7 ratchet, baseline, fixtures; T-E1 | 0 | 0 | 350 | 350 | yes | E0a |
| E5-4 | engine | `ValidationMessage` (`validation_result.py`) only; T-E5 | 1 | 60 | 100 | 160 | yes | E1 |
| ER | engine | release from the lineage (version, changelog) | 2 | 20 | 0 | 20 | yes | E0b, E0c, E1, E5-4 |
| P1 | Platform | `api/jobs/job_log.py` (new), the four readers, engine pin `pyproject.toml`; R5; T3, T-P1, characterization first | 6 | 300 | 380 | 680 | yes | ER |
| P2a | Platform | writers in 10 files (11.1), `_append_narrative` through `append`, `_fail_host_safety` drops `reason`; T-P2 cells | 10 | 220 | 350 | 570 | yes | P1 |
| P2b | Platform | writers in 10 files (11.1), Platform `JobLogger` calls migrated, protocol methods kept; T-P2, T-P2r | 10 | 220 | 440 | 660 | yes | P2a |
| P4 | Platform | `detail-exc` rule, baseline 56, fixtures | 0 | 0 | 250 | 250 | yes | none |
| P4a | Platform | 10 files, 21 sites (11.1); T-P4 cells | 10 | 75 | 350 | 425 | yes | P4 |
| P4b | Platform | 10 files, 19 sites (11.1); T-P4 cells | 10 | 70 | 320 | 390 | yes | P4 |
| P4c | Platform | 7 files, 16 sites (11.1); T-P4 cells; baseline empty | 7 | 60 | 280 | 340 | yes | P4 |
| P4d | Platform | 3 sites (3.4); `exc-copy` on P4's matcher; T-P4d | 2 | 20 | 220 | 240 | yes | P4, ER |
| P8 | Platform | `jobs/job_node_error.py` (new), `_partial_publication.py`, `v2_node_runs.py`, `v2_vault.py`, `v2_orchestrator.py`, `routes_runtime_facts.py`, `evidence/assembly.py`; `node-error`; T-P8 | 7 | 150 | 330 | 480 | yes | ER |
| P5n | Platform | `api/notifications/dispatcher.py`, `api/notifications/router.py`; T-P5n | 2 | 35 | 80 | 115 | yes | none |
| P7 | Platform | `api/pipelines/v2_validation.py` to the new `ValidationMessage`; T-P7 | 1 | 50 | 90 | 140 | yes | ER |
| X | all | cross-repo marker corpus on the released engine and pinned Platform commit; 6.4 matrix | 0 | 0 | 300 | 300 | yes | P2b, P4c, P4d, P5n, P7, P8 |
| S-E3a | engine | `_scope.py`, `_alias.py`, `__init__` (coordinator, per-job table); T9 | 3 | 320 | 380 | 700 | no (stack) | E0b |
| S-E3b | engine | lend and merge across isolated children: `_isolated_run.py`, `_isolated_worker.py`, `_governor.py`, `_scope.py`; T15 | 4 | 200 | 400 | 600 | no (stack) | S-E3a |
| S-E4 | engine | `_sink.py` (`on_legend`), `_view.py` (aliased operational view), `context.py` `node_alias` | 3 | 260 | 250 | 510 | no (stack) | S-E3b |
| S-P0 | Platform | migration `f31_run_uuid`, `api/jobs/run_identity.py`, `api/models.py`, `api/jobs/schemas.py`; T11 | 4 | 220 | 300 | 520 | no (stack) | none |
| S-P3a | Platform | `job_scope.py` (new), `job_log.py` run binding and stamping, three roots; T22 | 5 | 250 | 340 | 590 | no (stack) | S-P0, P2b, stack engine release |
| S-P3b | Platform | migration, `api/models.py`, legend write, route, registration, alias join; T12, T16 | 6 | 300 | 350 | 650 | no (stack) | S-P3a |

Totals (estimates): V1 slices 18, about 6,710 lines including tests (revision 4: 18, about 6,780); stack slices 6, about 3,570 lines (revision 4: 3,530); all 24 slices about 10,280 (revision 4: 10,310). Revision 5 adds P8 (480), P4d (240, already reduced by 60 because its `exc-copy` sentry reuses P4's except-handler matcher instead of a second one), and grows P2a (+30), P2b (+80), E0c (+10), S-P3a (+40): +880. To stay flat it cuts E5-1 (290), E6 (340) and the `CapacityEstimate`/`PlanCheckError` half of E5-4 (280), none of which has a non-admin reader at `d8908f42` (3.5, O5): -910. Net -70 on V1, -30 overall.

No engine slice edits another repository. Self-check per slice: the slice's named tests, then `scripts/dev/gates.sh --baseline` where it exists, run from `uv.lock` (`uv sync --locked`).

### 11.1 File lists

Platform paths are under `api/`. P4 groups are alphabetical by file, so a reviewer can check them against `platform-d8908f42.txt` in one pass.

- P2a: `jobs/runner.py`, `jobs/iteration.py`, `jobs/iteration_dispatch.py`, `jobs/job_terminal_cas.py`, `jobs/reaper.py`, `jobs/queue_worker.py`, `jobs/job_principal.py`, `jobs/scheduler_claim_loop.py`, `jobs/binding_resolve.py`, `persistence/logger.py` (`_append_narrative` only).
- P2b: `quality/runner_hook.py`, `storm/router.py`, `storm/post_mask_hook.py`, `synth/runner_hook.py`, `main.py`, `workflows/restart_recovery.py`, `workflows/runner.py` (the `.log` write at `:90`; the `summary` sites are P4d), `persistence/logger.py` (protocol methods kept, text-free), `jobs/route_narrative.py`, `jobs/v2_orchestrator.py`.
- P4a (10 files, 21 sites): `admin/audit_redaction_router.py` (2), `admin/router.py` (2), `analytics/router.py` (2), `composites/preview.py` (2), `data_tables/router.py` (1), `disguises/router.py` (2), `environments/binding_router.py` (2), `files/router.py` (1), `files/s3_pull_router.py` (4), `fixed_width_layouts/router.py` (3).
- P4b (10 files, 19 sites): `header_layouts/router.py` (1), `jobs/_create_phases.py` (3), `jobs/_service.py` (1), `jobs/binding_resolve.py` (2), `jobs/routes_evidence.py` (1), `jobs/routes_preview.py` (3), `notifications/router.py` (1), `org_disguises/router.py` (2), `pipelines/router.py` (4), `reporting/parsing.py` (1).
- P4c (7 files, 16 sites): `schema_contract/router.py` (2), `seeds/admin_router.py` (6), `server_logs/router.py` (2), `storm/detectors_router.py` (3), `subset/router.py` (1), `text_redact/preview.py` (1), `unmask/router.py` (1).

## 12. Worker evidence rules

- Name each finding's class (1 to 10) and fix the class structure; if a cataloged class recurs, stop and report it.
- Tests first, recorded red at the named fail-before location, baselines run from `uv.lock` (`uv sync --locked`) in the branch's environment, said so.
- Apply each 6.2 mutation once and show its test red; never commit with a mutation applied.
- Never widen a threshold or baseline, skip a test, weaken an assertion or edit a pinned test; state what you did not run.
- V1 bar: a finding that reaches only an admin surface goes to V2-LOG with `file:line` and does not block.
- P1, P2a and P2b merge in one window with no Platform release between; P2b leaves `JobLogger` satisfying the engine `Logger` protocol (T-P2r in its gate).

## 13. Owner questions (each with a recommended default)

- **O1 (alias, legend and run-UUID stack). Default: CUT.** Decided 2026-10-30 and kept with the V1-2 and V1-D fixes (3.7); it does not block V1 (schema names are not Rule 3 values; the other surfaces it would protect are admin-only). CUT removes 6 slices, about 3,570 lines and the V1-2 failure mode; section 14 lists the loss. One word: CUT or KEEP.
- **O2 (in-process `raise` messages).** 75 engine `raise` sites interpolate an except-bound name at `07da6ee`. Default: V2-LOG; parts (a), (b), (c) and (e) stop that text where Platform copies it into a named surface.
- **O3 (Platform exception text outside details). Done in revision 5** (3.4): 56 rows classified; 3 P4d, 2 rewritten by P7 and P8, 7 closed at their sink by P2a and P8, 44 V2-LOG or no copy.
- **O4 (admin-only routes among the 56 details).** Default: rewrite them with the others (cheaper than proving each route admin-only).
- **O5 (engine result fields with no Platform reader). Default now: V2-LOG.** No Platform reader of `CheckResult`, `CapacityEstimate` or `PlanCheckError` at `d8908f42`; the CLI (not attached) may print `CheckResult.detail` to the operator who supplied the credentials. Moved to pay for P8 and P4d; "keep" restores them (+570 lines, V1 about 7,280).

## 14. Cut variant (costed; pick with one word: CUT)

If O1 is CUT, only this changes: S-E3a (3 files, 700), S-E3b (4, 600), S-E4 (3, 510), S-P0 (4, 520), S-P3a (5, 590), S-P3b (6, 650) go: 25 production file touches, 2 migrations, 1 route, about 3,570 lines. Also gone: `SafeEvent.run` and `aliases`, the stamping rule of 3.7, `from_line`'s `expected_run`, T9, T11, T12, T15, T16, T22. The event keeps `step` (an approved node id), so `Job.log` shows real schema names, which are owner names. V1 slices and the V1 gate are unchanged.

Lost: aliasing of names on operational output; the owner-only legend; binding a stored line to its run (forging one needs database write access, outside the V1 threat model); the run UUID as the F30 span id and F32 bundle key (both key by `Job.id`). The 2026-10-30 items "progress views are owner results with real names" and "legacy rows withheld at read" are unaffected.

## 15. V2-LOG (logged, not planned; class tag in brackets)

- R1 dynamic class-name drain (56 sites in 30 files at `ab29ef0c`), `src/decoy_engine/connectors/postgres_snapshot.py:241` [1]
- R2 engine logger-argument drain (116 calls in 37 files), `src/decoy_engine/internal/logging.py:129` [1]
- Entry-frame scrubbing (`closed_entry`, F1, F2); no traceback serializer (section 1 grep), `src/decoy_engine/fileaccess/_entry.py:50` [1]
- 109 non-constant audit details behind `require_admin`, `api/audit/router.py:159` [1]
- Census machinery (T0, surface JSON, T21), `docs/plans/evidence/2026-11-01-f31-r4-anchors/platform-d8908f42.txt` [8]
- Metrics label from a class name, `api/observability/metrics.py:391` [1]
- Child stdio (8 spawn sites), `api/files/pg_pull.py:242` [1]
- CLI stderr and `print_exception`, `decoy/src/decoy/cli/scan.py:794` (unverified) [1]
- Engine R7 sites outside part (c), `src/decoy_engine/plan/_generation.py` (3 sites) [1]
- `HTTPException` detail with a class name only, `api/cloud/accounts.py:572` [1]
- Free text with no non-admin reader: `src/decoy_engine/execution/_job_estimate.py:98`, `src/decoy_engine/unmask.py:393`, `src/decoy_engine/generation/pool/_events.py:39`, `src/decoy_engine/execution/_events.py:19`, `src/decoy_engine/context.py:74` [1]
- Engine narrative names a table and column in the stdlib log, `src/decoy_engine/progress.py:119` [1]
- Capacity recheck logs `result.detail`, `api/scheduler/admission_pass.py:457` [1]
- Engine `raise` messages with an estimate or exception message, `src/decoy_engine/execution/capacity.py:353`, `:504` (O2) [1]
- Revision 5 cuts (no non-admin reader): `CheckResult.detail`, `src/decoy_engine/connectors/gcs.py:157` and five more connector sites [1]; `CapacityEstimate.message`, `src/decoy_engine/execution/out_of_core/_capacity_eval.py:255` [1]; `PlanCheckError.message`, `src/decoy_engine/plan/validate/_consolidator.py:92` [1]; isolated parent copies child text, `src/decoy_engine/execution/_isolated_run.py:578` [6]
- 44 O3 rows needing no V1 work, reasons in `docs/plans/evidence/2026-11-02-f31-r5-o3/README.md`; e.g. the iteration preview, `api/pipelines/router.py:478`, `:483`: owner-only, ephemeral, already returns the expanded values [1]
- Unauthenticated `/healthz/db` returns 200 characters of DB error text, `api/health_check.py:72` [1]
- R5 misses the bulk-update form `"log": ""`, `api/jobs/job_terminal_cas.py:290`; with the stack a re-run keeps the previous run's legend rows [8]
- One-window rule for P1, P2a, P2b is process only: land them as one stacked PR or integration branch, `api/jobs/runner.py:67` [9]
- Cancel while the worker thread runs keeps the copied scope bound; the scope should mark itself exited, `api/jobs/_dispatch_supervisor.py:219` [3]
- dev-rules and CLI citations not re-read in revisions 4 and 5, `decoy/src/decoy/cli/scan.py:794` [8]

## 16. Revision 5 change log (Dennis review of `d1a837b1`, V1 bar)

Revision 4's log is in git history at `d1a837b1`.

| Finding | Class | Resolution (sections) | Slice | New tests |
|---|---|---|---|---|
| V1-A BLOCKER: error node and vault node store exception text, served by `/node-runs` and the manifest | 1 | Part (e): `node_error`, `FailureDetails` loses `message`, `project_node_error` on both readers, `node-error` ratchet; third named limit corrected (0, 1, 3.8, 6.2) | P8 | T-P8 `test_failed_v2_job_error_node_has_no_marker`, `test_vault_node_has_no_marker`, `test_legacy_row_withheld_at_read`; `tests/sentry/test_node_error.py` |
| V1-B HIGH: O3 deferred copies into non-admin responses | 1 | O3 done: 56 rows classified; 3 response rows in P4d; `exc-copy`; iteration preview V2-LOG with reason (1, 3.4, 13, 15) | P4d | T-P4d `test_no_unclassified_copy`, `test_seeded_copy_is_red`, `test_response_has_no_marker[validate_yaml_717, workflow_225, workflow_238]` |
| V1-C BLOCKER: P2b removed methods of the engine `Logger` protocol | 9 | Methods kept text-free; Platform calls migrated; T-P2r AST call check plus STORM, V2 and protocol cells in P2b's gate (3.3, 6.2, 12) | P2b | T-P2r `test_no_platform_call_to_narrative_methods`, `test_job_logger_satisfies_engine_protocol`, `test_storm_scan_with_job_logger`, `test_v2_job_with_job_logger_as_ctx_logger` |
| V1-D HIGH: `SafeEvent.run` source unstated | 3 | `append` stamps `run` from `job.run_uuid`; the scope supplies aliases only (3.7) | S-P3a | T22 `test_unscoped_writers_parse_under_run_binding[reaper, startup_recovery, queue_worker]` |
| Found: six O3 rows reach `Job.log` via `_fail_host_safety` | 1 | P2a drops `reason` (3.3) | P2a | T-P2 `test_host_safety_drops_reason` |
| Size: +880 estimated lines | 8 | Cut E5-1, E6, half of E5-4 (no non-admin reader); net -70 on V1 (3.5, 11, O5, 15) | E5-4 narrowed | T-E5 narrowed; T-E6 removed |
