Status: plan only, revision 4 after the Dennis plan review of revision 3 under the V1 bar (GO-WITH-FIXES: 2 V1, 8 V2-LOG). No implementation or release approval is granted by this document.

Summary: F31 stops exception text and cell values from reaching the surfaces a non-admin can see. The V1 core is one design with four parts (section 0): the Platform `Job.log` choke point, the 56 Platform `HTTPException` details that copy exception text, the engine result fields that copy `str(exc)` (E5-1, E5-4), and the isolated-child error text the parent copies (E6), plus one notifications field. Each part lands with a ratchet (a sentry whose baseline only shrinks) and its named limits. The alias, legend and run-UUID stack stays in the plan because it is an owner decision of 2026-10-30; it does not block V1 under the bar, and owner question O1 recommends cutting it (section 14 prices the cut).

Baselines (revision 4): engine plan branch at `07da6ee`, which is engine `origin/main` `0173c05` merged into revision 3's `465ca9e`; engine `file:line` citations below were re-read there. Platform `origin/main` `d8908f42` (2026-11-01). The revision handoff named Platform `b290593f`, but that commit (2026-10-27) is an ancestor of revision 3's `00e55c27` (2026-10-30), so re-reading there would move the anchors backwards; revision 4 re-read the Platform anchors at `d8908f42` instead. CLI: not attached to this session; CLI citations are carried unverified and no CLI slice remains in V1.

Measurement environment (revision 4): counts are stdlib `ast` scans and `git grep` run with the container Python 3.13.18, NOT the `uv.lock` interpreter; they read source and import nothing. Probes and raw output: `docs/plans/evidence/2026-11-01-f31-r4-anchors/` (new: `httpexc.py`, `httpexc_kinds.py`; reused from revision 3: `logassign.py`, `r7b.py`). No test suite was run for this plan revision.

Rules consulted: dev-rules `00-universal.md`, `development-loop.md` (PLAN, "Established solutions", "Design notes"), `risk-and-exceptions.md`, `scope-discipline.md`, `reuse-first.md`, and the task rulebooks the README routing table selects for a security-relevant cross-repository plan: `security.md`, `api-and-compatibility.md`, `testing.md`, `data-and-migrations.md`, `observability-and-resilience.md`, `refactoring.md`. The dev-rules tree is not present in this session's checkout, so these were not re-read for revision 4; they are cited from revisions 2 and 3. Read for revision 4 from decoy-platform `handoffs/queue`: `V1-BAR.md`, `DEFECT-CATALOG.md`, `OWNER-DECISIONS-2026-10-30.md`, `V2-CORRECTIVE-BACKLOG.md`, and the engine and Platform `CLAUDE.md`.

# F31: Shared safe-output boundary, PLAN (revision 4)

Risk: R2 (wire contract, three-repository migration, schema migrations). The owner decisions of 2026-10-30 are applied as written: three-class surface table; progress views are owner results with real names; a per-job owner-only alias legend; legacy `Job.log` rows withheld at read, never purged; run identity is a Platform-minted UUID beside `Job.id`; the F25 4c, F34 and F36 rulings in section 9. The V1 bar (owner, 2026-11-02) decides what blocks the merge.

Naming: V1 rules are Rule 1 to Rule 3 (section 1). Slices are E (engine), P (Platform), S (the owner-decided stack, both repositories), X (cross-repository). Ratchets are R5 (Platform `Job.log` writes), R7 (engine exception-text projection), `detail-exc` (Platform API details) and `notify-closed` (notification text).

## 0. V1 core: one design

One idea: exception text and cell values never become user-visible bytes. Exception text is replaced, at the point it would be copied, by a closed label (`safe_error_type(exc)`) or a registered reason; free narrative is replaced by registered prose. Four parts, each with one choke point and one ratchet:

| Part | Choke point | What changes | Ratchet | Slices |
|---|---|---|---|---|
| (a) `Job.log` | `api/jobs/job_log.py` (`append`, `clear`, `read`), new | 28 direct `.log` assignments in 17 files (one of them is `JobLogger._append_narrative`, `api/persistence/logger.py:426-460`) write only `decoy-safe-output/v1` lines; the four readers render through `read`; a line that does not parse renders as a counted `legacy_withheld` line | R5: no `.log` assignment outside `job_log.py`; baseline 28 sites, empty after P2b | P1, P2a, P2b (one release) |
| (b) API error details | the `except` handler that builds `HTTPException(detail=...)` | 56 details in 27 files that copy the except-bound exception's text become constant prose plus the `safe_error_type` label | `detail-exc`: inside `except ... as NAME`, an `HTTPException` detail may read `NAME` only through `safe_error_type(NAME)` or an allowlisted typed attribute (`code`); baseline 56, empty after P4c | P4, P4a, P4b, P4c |
| (c) Engine results | the constructor of the result field | `CheckResult.detail` (`str(exc)` at `connectors/gcs.py:157`, `:235`, `s3.py:209`, `:311`, `sftp.py:276`, `:355`); `CapacityEstimate.message` (`exc.message` at `execution/out_of_core/_capacity_eval.py:255`, `:307`); `PlanCheckError.message` (`exc.message` at `plan/validate/_consolidator.py:92`, `:96`); `ValidationMessage.message`/`hint`, which Platform fills from `PlanCompileError.message` (`api/pipelines/v2_validation.py:153-157`) | R7: inside `except ... as NAME`, no `str(NAME)`, `repr(NAME)`, `str(f(NAME))`, `NAME.args`, `NAME.message` or interpolation of `NAME` outside a `raise`; baseline 45 sites in 27 files at `07da6ee`, shrinking by the V1 sites | E1, E5-1, E5-4, P7 |
| (d) Isolated child text | `_result_from_envelope` (`execution/_isolated_run.py:566-580`) | the parent copies `envelope.get("error")` (`:578`), text the child wrote, into `IsolatedRunResult.error`; it becomes `error_type: SafeErrorType` parsed with `SafeErrorType.from_label` (an unknown label becomes `EXCEPTION`) | the R7 baseline plus T-E6 | E6 |
| Notifications row | `make_job_failed_event` (`api/notifications/dispatcher.py:187-213`) | `error_summary: str` (`:190`, appended to `detail` at `:202-203`) becomes `error_summary: FailureLabel` (closed enum); no caller passes free text today, the test-fire passes a constant (`api/notifications/router.py:494-498`) | `notify-closed`: the acceptance test rejects a `str` | P5n |

Shared engine pieces the core needs: `SafeEvent` and its serializer and parser (E0a, E0b, section 3.1), `safe_error_type` (section 3.2), and the registry `platform` namespace that holds Platform's prose (E0c; owner decision: Platform has no registry).

**Blocks V1** (the merge gate for F31's V1 claim): E0a, E0b, E0c, E1, E5-1, E5-4, E6, ER (engine release), P1, P2a, P2b, P4, P4a, P4b, P4c, P5n, P7, X.

**Owner-decided stack, kept, does not block V1** (O1 recommends CUT; section 14): S-E3a, S-E3b, S-E4, S-P0, S-P3a, S-P3b. With the stack kept, its slices ship after the V1 slices they depend on and are reviewed under the same bar; a stack finding that is not a Rule 1 to Rule 3 failure is V2-LOG.

**Is V2** (logged in section 15, no work planned here): the R1 dynamic class-name drain, the R2 engine logger-argument drain, entry-frame scrubbing (`closed_entry`, F1, F2), the audit-detail half of revision 3's P4, the census machinery (T0, generated surface JSON, slice manifest), metrics labels (P5), child stdio (P6), CLI stderr (C1, C2), the R7 sites outside the V1 parts, the free-text fields with no non-admin reader (`QualityWarning.detail`, `JobEstimate.notes`, `UnmaskColumnReport.detail`, `ExecutionEvent`), and in-process `raise` messages.

## 1. Guarantee (V1 rules 1 to 3, ratchet plus named limits)

**Threat model (V1).** Self-hosted, single-org; the engine process is trusted; the leak is accidental (a value or path in exception text copied into output, or a free-text log line). A viewer who is not the server admin is the audience that matters. Hostile in-process code is V2.

**Rule 1 (product function).** F31 changes no masked, subset or generated output and no route selection. `Job.log` stays readable through its four readers: a v1 line renders as `ISO_TS  LEVEL  STEP  MSG`; a line that does not parse renders as one counted `legacy_withheld` line per run of such lines, never as raw text and never as an error. Named limit: legacy rows and unmigrated writers show as `legacy_withheld` until P2b ships; P1, P2a and P2b therefore ship in one Platform release (section 3.3). With the stack kept: one job has one alias table, so an alias names one table for the whole job and the legend agrees with the lines (section 3.7, the V1-2 fix).

**Rule 2 (data).** No F31 write can abort a job's terminal record. `job_log.append` writes inside the caller's transaction and adds no new failure point to the terminal path beyond today's string append; the stack's legend write runs inside its own savepoint after the terminal record is committed (section 3.7). Legacy `Job.log` rows are never rewritten or purged.

**Rule 3 (values a non-admin can see).** On the named V1 surfaces, no exception text and no cell value appear: stored `Job.log` and its four readers (SSE, job `log` field, node-run logs, STORM failure summary); the 56 Platform `HTTPException` details of part (b); the engine result fields of part (c) and the Platform `ValidationMessage` that copies `PlanCompileError.message`; `IsolatedRunResult.error_type`; the `job_failed` notification `detail`. Ratchets: R5, `detail-exc`, R7, `notify-closed`, each a shrink-only baseline with a seeded red fixture (section 6). Named limits (stated, not hidden):

- Platform except-bound text reads outside `HTTPException`, audit, `raise` and logger calls (92 in 39 files at revision 3's `00e55c27`; not re-measured) are not drained by F31; owner question O3 decides whether to classify them in P4.
- Engine R7 sites outside the V1 parts stay in the R7 baseline (they cannot grow); they reach engine results that no Platform reader consumes at `d8908f42`, or admin logs (V2-LOG).
- Exceptions raised to an in-process SDK caller keep their message (the in-process contract of the public exception classes); 75 engine `raise` sites interpolate an except-bound name at `07da6ee`. Part (b) stops that text where Platform would copy it into a response; owner question O2.
- Admin-only surfaces (server logs, audit details behind `require_admin` at `api/audit/router.py:159`, traceback locals, `__cause__` chains, metrics, child stdio, CLI stderr) are V2 by the bar.
- Traceback serialization: a `git grep` at both baselines finds no code that formats a traceback (`format_exc`, `print_exc`, `format_exception`, `TracebackException`, `print_exception`, `extract_tb`, `format_tb`) in Platform `api/` or engine `src/`; the hits are comments (`platform-d8908f42.txt`, `engine-07da6ee.txt`). Frame locals therefore reach no user-visible surface today; this grep is the check, and no census is built.

**In-process exception boundary.** An exception raised to an SDK caller is not a serialized surface: the caller wrote the input, and the message may name schema objects and config pointers. Rule 3 starts where exception text would be copied into a result field, a stored line, a response body or a notification. R7 (engine) and `detail-exc` (Platform) ban that copy at the named sites.

Non-goals: no new encryption or hashing; regex scrubbing (`api/logging/redaction.py`) stays a backstop; no `--unsafe-debug-log`.

## 2. Established solutions and design notes

Established solutions (cited again in module docstrings per the engine rule): RFC 9457 identity-versus-occurrence split, already the F29 pattern (`src/decoy_engine/reasons/_record.py:1-17`); OpenTelemetry `error.type` as a low-cardinality identifier, already the `reason_adapter` pattern (`src/decoy_engine/reason_adapter.py:1-7`); the OWASP Logging Cheat Sheet (log the event and its location, never the data); python-json-logger's single exception field, already the Platform `exc_safe` shape (`api/logging/exc_safe.py:1-18`); "Parse, don't validate" for the stored-line parser; SQLAlchemy `Session.begin_nested()` (SAVEPOINT) to isolate a best-effort write, already used for the terminal manifest (`api/jobs/job_terminal_cas.py:256-266` at `d8908f42`); for the stack, RFC 9562 UUIDv4 version and variant bits and the Alembic add-nullable, backfill, then constrain pattern (`api/migrations/versions/obs3_request_id.py:1-16`). Remaining custom code: the `decoy-safe-output/v1` record, because no library knows the engine's reason registry.

Design notes: one engine package `decoy_engine.safe_output` (`_event.py`, `_exception.py`, `_wire.py`; the stack adds `_scope.py`, `_alias.py`, `_sink.py`, `_view.py`) and one Platform module `api/jobs/job_log.py` (the stack adds `api/jobs/job_scope.py`). One place per fact: prose lives in the registry; the exception table lives in `_exception.py`; stored-line parsing lives in `_wire.from_line`; `Job.log` writes live in `job_log.py`. No flag arguments. Comments say why.

## 3. Design

### 3.1 `SafeEvent` and its wire form (E0a, E0b)

Kept unchanged: `ReasonRecord`/`ReasonContext` (`src/decoy_engine/reasons/_record.py:115-212`), the progress variants and their projections `event_to_dict` and `event_to_legacy_dict` (`src/decoy_engine/_progress_events.py:513-599`), and the `decoy-reasons/v1` identity form (`src/decoy_engine/reasons/_identity.py:19-57`).

`SafeEvent` (`safe_output/_event.py`): a frozen dataclass validated in `__post_init__` and again in `to_dict`, with exact-type checks on nested groups as `_V1Fields` does (`_progress_events.py:262-270`). Fields: `run: uuid.UUID | None` (always `None` without the stack), `seq: int`, `ts_ms: int`, `sev: reasons.Severity`, `kind: EventKind` (closed: `NARRATIVE`, `STEP`, `TERMINAL`, `WITHHELD`, `INVALID`), `reason: ReasonRef`, `err: SafeErrorType | None`, `step: StepRef | None` (a node id string approved by `approve_name`, an owner name, not a value), `counts: tuple[tuple[str, int], ...]` under `COUNT_KEYS` (`_record.py:34-36`, extended additively), `flags: frozenset[Flag]`. The stack adds `aliases: tuple[SafeAlias, ...]`. There is no free `str`, `Any` or mapping field.

Serializers (`safe_output/_wire.py`):

- `to_line(event) -> bytes`: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)` plus `\n`; at most 4096 bytes excluding the newline and JSON depth at most 3. Over a limit, it returns the registered `safe_output.invalid` event for the same `seq` with a closed `cause` count (`oversize`, `too_deep`, `invalid_field`), never a truncation. If building that substitute raises, it returns `_FAILED_LINE = b'{"kind":"failed","s":"decoy-safe-output/v1"}\n'`, an import-time ASCII constant. `to_line` never raises.
- `from_line(line: bytes, *, expected_run: uuid.UUID | None = None) -> SafeEvent | None`: rejects a line over 4097 bytes before parsing; bounded parse that rejects depth over 3 and duplicate keys; closed keys; fields re-checked; reason known via `require_known`; with the stack, `run` must equal `expected_run`. Anything else is `None`. `_FAILED_LINE` is `None`.
- `render_text(event) -> str`: `ISO_TS  LEVEL  STEP  MSG`, MSG is the registry summary plus closed facts.

### 3.2 Exception label (E0a)

`SafeErrorType` is a closed `str` enum built once at import from exact classes: builtin exception classes (as `_safe_error_names` enumerates them, `_progress_events.py:69-81`), the engine families in `reason_adapter._CODED` (`reason_adapter.py:39-85`), the classes of `decoy_engine.errors`, plus `EXCEPTION`. `safe_error_type(exc)` is `_ALLOWED_EXCEPTION_TYPES.get(type(exc), SafeErrorType.EXCEPTION)`; `SafeErrorType.from_label(s)` maps an unknown string to `EXCEPTION`. `phase.error_result` (`phase.py:594`) delegates; its value domain widens additively (section 7). Revision 3's wider consumer migration (`postgres_snapshot` `cause_type`, `progress.run_scope`) reads class names into admin logs and is V2-LOG.

### 3.3 `Job.log` choke point (P1, P2a, P2b)

`Job.log` (`api/models.py:424`) keeps its `Text` column and, from P1, holds only v1 lines written by `job_log.append(db, job, event)`; resets become `job_log.clear(db, job)`. Platform builds its events with `job_log.platform_event(reason, *, sev, err=None, step=None, counts=())`, whose `reason` is a member of the engine's `platform` namespace (E0c). `job_log.read(job)` parses each line with `from_line` and renders it with `render_text`; each maximal run of lines that fail is one `safe_output.legacy_withheld` line with a count. The four readers call it: the SSE stream (`api/jobs/routes_streaming.py:92-101`, keeping `data: <text>` so `web/src/hooks/useLiveJob.ts:215` is unchanged), the job `log` field (`api/jobs/_shared.py:203-211`, the 256 KiB tail cap kept on rendered text), node runs (`api/jobs/routes_runtime_facts.py:96`, grouped by the event's `step`) and the STORM failure summary (`api/storm/router.py:1707`).

Writers (5.6) move to `append` in P2a and P2b. In P2a `JobLogger._append_narrative` (`api/persistence/logger.py:426-460`) writes a `NARRATIVE` event with reason `platform.job_narrative`, the level and the step, and drops the message text; `JobLogger`'s public methods keep their signatures, so its constructors (`api/jobs/route_narrative.py:223`, `api/jobs/v2_orchestrator.py:377`, `api/storm/router.py:1031`) and the engine handoff are unchanged in P2a. P2b removes the narrative methods and migrates their callers.

Release unit (finding [9]): from P1, a line written by a writer that has not migrated renders as `legacy_withheld`, and P2a removes `JobLogger`'s narrative text. A Platform release between P1 and P2b would therefore ship a `Job.log` that withholds most lines. P1, P2a and P2b merge in one window with no Platform release cut between them; P2b's gate runs T-P1 and T-P2 together on the merged result. Each intermediate commit passes the gates because withholding is the safe direction.

Rule 2 note: `append` is a string append in the caller's transaction (today's behavior at `api/jobs/runner.py:65-70` and `api/jobs/job_terminal_cas.py:256`); if `to_line` substitutes or fails, it still returns bytes, so `append` cannot raise on content.

### 3.4 API error details (P4, P4a to P4c)

At `d8908f42`, 60 `HTTPException` calls inside `except ... as NAME` read `NAME` in their detail (`httpexc_kinds.py`): 56 copy its text in 27 files, 3 read only `NAME.code`, 1 reads only `type(NAME).__name__` (V2-LOG). Revision 3 counted 57 by a reads-in-context method; revision 4 counts calls. P4 lands the `detail-exc` rule (tests only, baseline 56, seeded fixture). P4a to P4c rewrite each site to constant prose plus `safe_error_type(NAME)` where the label helps the caller; status codes and response shapes stay. A few sites sit on admin-only routes (`api/seeds/admin_router.py`, `api/admin/router.py`, `api/admin/audit_redaction_router.py`); they are rewritten too, because one expression each costs less than proving each route admin-only (owner question O4).

### 3.5 Engine result fields and the isolated child (E1, E5-1, E5-4, E6, P7)

| Type.field (line at `07da6ee`) | Free text enters at | Becomes | Platform reader at `d8908f42` | Slice |
|---|---|---|---|---|
| `CheckResult.detail` (`sdk.py:123`) | `connectors/gcs.py:157`, `:235`; `s3.py:209`, `:311`; `sftp.py:276`, `:355` | `reason: ReasonRef \| None`, `error_type: SafeErrorType \| None`; `detail` deleted (pre-GA) | none found by import scan (O5) | E5-1 |
| `CapacityEstimate.message` (`execution/out_of_core/_capacity_eval.py:153`) | `exc.message` at `:255`, `:307` | `reason: ReasonRef`, `counts`, `tables: tuple[str, ...]` | none found by import scan (O5) | E5-4 |
| `PlanCheckError.message` (`plan/validate/_consolidator.py:47`) | `exc.message` at `:92`, `:96` | `reason: ReasonRecord` from `reason_from_exception`; `path` kept | none found by import scan (O5) | E5-4 |
| `ValidationMessage.message`, `.hint` (`validation_result.py:68`, `:71`) | Platform `api/pipelines/v2_validation.py:153-157` passes `exc.message` of `PlanCompileError` | `reason: ReasonRef`; `add_error(code, *, path, node_id)` loses `message` and `hint` | `api/pipelines/v2_validation.py` (pipeline validation response) | E5-4, P7 |
| `IsolatedRunResult.error` (`execution/_isolated_common.py:369`) | `_isolated_run.py:578` copies child-written `envelope.get("error")`; the child writes `_error_text` (`_isolated_worker.py:47-49`, `:227`, `:263`), today a class name plus trusted code | `error_type: SafeErrorType \| None` beside the typed `reason`; the envelope carries `error_type`, parsed by `from_label`; `_governor.py:225`, `:227`, `:550` format the label | none directly; the governor puts it into a raised message (`_governor.py:550`) that a Platform `except` may copy (closed by part (b)) | E6 |

E6 is a class 6 fix (untrusted producer): at `07da6ee` the child's text is already value-free, so nothing leaks today; the parent accepting arbitrary child text is what nothing stops. R7 lands in E1 as a test-only ratchet with the census of 45 sites taken at E1's base and must also match `str(f(NAME))` (the `gcs.py`/`s3.py` shape, which revision 3's probe missed).

### 3.6 Notifications (P5n)

`make_job_failed_event` (`api/notifications/dispatcher.py:187-213`) appends `error_summary` to the notification `detail` that reaches Slack, email and webhooks. No job path passes it (`api/jobs/runner.py:420-424`); the test-fire passes a constant (`api/notifications/router.py:494-498`). Nothing leaks now and nothing stops a future leak (class 4, silent route). Fix: `error_summary: FailureLabel | None`, a closed enum (`test_fire`, plus the labels a future caller needs, added through review); `detail` renders the label's fixed prose.

### 3.7 Owner-decided stack: run identity, one scope per job, aliases, legend (S slices)

- Run (S-P0): Platform mints `uuid.uuid4()` in `api/jobs/run_identity.py:mint_run_uuid` at job creation and stores it in `jobs.run_uuid` (`String(36)`, unique, not null after backfill) beside `Job.id` (`api/models.py:329`). The column default is `mint_run_uuid`; `@validates("run_uuid")` and a CHECK constraint accept only canonical v4; request schemas exclude the field; an AST sentry forbids other writers.
- One scope per job (the V1-2 fix). A job is not one engine call: after `run_pipeline` the job runs the quality, synth and STORM post-success hooks (`api/jobs/runner.py:287-326`), each making its own engine calls. The single Platform function that owns a job's scope is `api/jobs/job_scope.py:job_output_scope(db, job_id)`, a context manager and the only Platform caller of `SafeOutputScope.for_run` (AST sentry in S-P3a). It is entered once, outermost, at the three job lifecycle roots: `api/jobs/runner.py:run_job` (`:443`, wrapping its whole `try`/`except`/`finally`, so the worker-thread engine call at `:556-565` and the hooks at `:576` share it; the worker thread inherits it through `run_owned_worker_thread`'s `contextvars.copy_context()`, `api/jobs/_dispatch_supervisor.py:219-220`), `api/jobs/dispatch_worker_main.py:run_v2_lifecycle_in_worker` (`:350`, spanning `_run_v2_pipeline_in_worker` and the hooks at `:505`), and `api/storm/router.py:run_storm_job` (`:984`). Iteration children run through `run_job` with their own job id, so each child job gets its own scope. Re-entry for the same run reuses the bound scope and does not exit it; entry for a different run while one is bound raises `ScopeConflict` (a programming error caught by the sentry test, not a job outcome).
- Coordinator (S-E3a): engine public entries hold `scope.coordinate()` while they run; only the holder allocates aliases; a concurrent second holder gets `kind-0` plus flag `alias_unknown`. Sequential calls on different threads (worker thread, then hooks on the event-loop thread) therefore allocate in call order.
- Aliases and isolated children (S-E3a, S-E3b): identity `(run, kind, ordinal)`, kinds `table`, `field`, `node`, `artifact`, cap 4096 per kind. Lifecycle `OPEN`, `LENT(k)`, `FROZEN` as revision 3 specified (lend a snapshot to the isolated child, contiguous `merge`, `merge_lost` on a child without an envelope, `AliasTableBusy` on a second lend), now per job scope rather than per engine call.
- Legend (S-P3b): table `job_alias_legend(job_id, kind, ordinal, name)` with UNIQUE `(job_id, kind, ordinal)` and CHECK `ordinal >= 1`. `job_output_scope` writes it once, at scope exit, after the lifecycle root has committed the terminal record: `with db.begin_nested():` delete the job's legend rows, insert the scope's legend; then `db.commit()`. If the savepoint raises (including a unique violation or a session already in a failed state), only the savepoint rolls back, `append` writes a `safe_output.legend_unavailable` line, and that commit is best effort (a failure is counted in the server log). `job_log.clear` also deletes the job's legend rows, so a re-run starts empty. The legend is read only by `GET /api/jobs/{id}/alias-legend` behind `require_job_owner_or_404`.
- Run binding (S-P3a): `job_log.read` passes `expected_run=job.run_uuid`; a job whose stored `run_uuid` fails validation renders as withheld with `safe_output.run_identity_invalid`.

## 4. Defect-class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | Four V1 choke points (section 0) with closed labels and registered prose; ratchets R5, `detail-exc`, R7, `notify-closed` | T-P1 marker corpus over the four readers, T-P2, T-P4 cells, T-E5, T-E6, T-P5n | restore `detail=str(exc)` at `gcs.py:157`; let `_append_narrative` keep the message; restore `detail=str(exc)` at `api/pipelines/router.py:100` |
| 2 | Cleanup missing on an exit path | `append` adds no failure point to the terminal path; with the stack, the legend write runs in its own savepoint after the terminal commit, and `job_output_scope` unbinds on all exits | T-P2 terminal cells; T16 legend failure; S exit matrix | drop `begin_nested` around the legend insert (T16 loses the `legend_unavailable` line on Postgres); drop the `finally` unbind (nested-run cell sees the previous scope) |
| 3 | State machine and concurrency | Stack: one scope per job at three roots; one coordinator at a time; `OPEN`/`LENT`/`FROZEN` with contiguous merge | T22 multi-call job; T15 multi-child cells | open a scope per engine call (T22 sees two ordinals for one table); let a second coordinator allocate (T15 ordinals diverge) |
| 4 | Route parity and silent fallback | One `Job.log` reader for the four stored-line routes; a withheld line is counted, never dropped; the notification summary is a closed type | T3 over four readers; T-P5n | skip `from_line` in one reader (T3 red on that reader); accept a `str` `error_summary` (T-P5n red) |
| 5 | Referential integrity | N/A: F31 changes no FK semantics; FK reports keep their fields and are not on a V1 surface | FK-shape output matrix (6.4) stays byte-identical | change an FK report field: the matrix fails |
| 6 | Untrusted producer | `from_line` bounded and closed; the isolated parent parses the child's `error_type` with `from_label` and copies no child text | hostile-line corpus (T14); T-E6 child writes `error=MARKER` | accept an unknown key in `from_line`; copy `envelope.get("error")` again |
| 7 | TOCTOU / check-then-use on a path | N/A: F31 opens no path after a check and adds no file writer | none needed | none |
| 8 | Weak tests and overclaimed evidence | Each count cites a committed probe at a named commit; positive control in each recorder; ratchets shrink only; the named limits are written in section 1 instead of a completeness claim | recorder self-test with a disconnected sink; baseline-growth test per ratchet | add a baseline entry: the growth test fails |
| 9 | Behavior-preserving refactor drift | Characterization first for the four `Job.log` readers, node-run grouping and `phase.error_type`; P1, P2a, P2b ship in one release | snapshots taken before P1 and E0a | group node runs by a different key, or cut a release between P1 and P2b: the P2b gate run fails |
| 10 | Frozen contract drift | Section 7 with value semantics; `decoy-safe-output/v1` additive only; result-field changes recorded as pre-GA breaks | T5 value-domain snapshots | change a progress value domain: T5 fails |

## 5. Enumerations from the code

### 5.1 Exit paths

- `Job.log` append on the terminal path: success, failure, cancel and reaper (`api/jobs/job_terminal_cas.py:256-266`, `api/jobs/reaper.py:233`, `api/jobs/runner.py:661`).
- Serializer: normal, substitute on a limit, double fault (`_FAILED_LINE`); parser rejection (3.1).
- Isolated execution: in-process fallback, timeout kill, abnormal exit, self-reported failure, commit (`src/decoy_engine/execution/_isolated_run.py:305`, `:330`, `:495`, `:547`, `:578`).
- Stack: scope exit (normal, exception, nested, cancel while the worker thread still runs), alias lend, merge, merge-lost, freeze, legend savepoint failure.

### 5.2 Routes

- Stored-line routes: SSE (`api/jobs/routes_streaming.py:92`), job `log` field (`api/jobs/_shared.py:203`), node runs (`api/jobs/routes_runtime_facts.py:96`), STORM summary (`api/storm/router.py:1707`).
- Job lifecycle roots: flag-off `run_job` (`api/jobs/runner.py:443`), flag-on worker `run_v2_lifecycle_in_worker` (`api/jobs/dispatch_worker_main.py:350`), STORM `run_storm_job` (`api/storm/router.py:984`), iteration parent and children (`api/jobs/iteration.py:421`, `api/jobs/iteration_dispatch.py:402`).
- Engine routes for (d): in-process or isolated for a relationship route (`src/decoy_engine/execution/_isolated_run.py:566`), governor reroute after a failed child (`src/decoy_engine/execution/_governor.py:550`).

### 5.3 FK shapes

- Single and composite edges, `RelationshipEdge` (`src/decoy_engine/relationships/_graph.py:72-92`).
- Multi-parent children and children in later streamed batches (`src/decoy_engine/execution/out_of_core/_multi_parent.py:49-106`). F31's V1 parts do not touch FK output; 6.4 checks the relational output stays byte-identical.

### 5.4 Fields

- V1 (section 3.5): `CheckResult.detail` (`src/decoy_engine/sdk.py:123`), `CapacityEstimate.message` (`src/decoy_engine/execution/out_of_core/_capacity_eval.py:153`), `PlanCheckError.message` (`src/decoy_engine/plan/validate/_consolidator.py:47`), `ValidationMessage.message`/`hint` (`src/decoy_engine/validation_result.py:68`, `:71`), `IsolatedRunResult.error` (`src/decoy_engine/execution/_isolated_common.py:369`); Platform `Job.log` (`api/models.py:424`), notification `error_summary` (`api/notifications/dispatcher.py:190`).
- V2-LOG (no non-admin reader at `d8908f42`): `QualityWarning.detail` (`src/decoy_engine/generation/pool/_events.py:39`), `JobEstimate.notes` (`src/decoy_engine/execution/_job_estimate.py:98`), `UnmaskColumnReport.detail` (`src/decoy_engine/unmask.py:393`), `ExecutionEvent.detail` (`src/decoy_engine/execution/_events.py:19`).

### 5.5 Output surfaces

| # | Surface | Source | Who sees it | V1? | Rule |
|---|---|---|---|---|---|
| S1 | Engine progress dicts v0, v1 | `src/decoy_engine/_progress_events.py:581-599` | job viewers | yes, unchanged | keys and value domains unchanged (T5) |
| S6 | Engine result fields of 3.5 | `src/decoy_engine/sdk.py:123` and the 3.5 table | SDK callers; Platform for `ValidationMessage` | yes | R7, T-E5 |
| S8 | Isolated envelope and `IsolatedRunResult` | `src/decoy_engine/execution/_isolated_run.py:578` | job viewers through raised messages | yes | T-E6 |
| S13 | Platform API error details | 56 sites in 27 files (`platform-d8908f42.txt`) | API callers | yes | `detail-exc` |
| S14 | `ValidationMessage` in the pipeline validation response | `api/pipelines/v2_validation.py:153-157` | API callers | yes | P7 |
| S15 | Stored `Job.log` | `api/models.py:424`, writers in 5.6 | job viewers | yes | R5 |
| S16 | `Job.log` readers | 5.2 stored-line routes | job viewers | yes | one reader, T3 |
| S25 | `job_failed` notification `detail` | `api/notifications/dispatcher.py:187-213` | Slack, email, webhook recipients | yes | `notify-closed`, T-P5n |
| S22 | Alias legend route (stack) | new | job owner | stack | owner only, T12 |
| S2, S3, S4, S18, S19, S20, S21, S10, S24 | Engine and server logs, audit details, metrics, child stdio, CLI stderr, traceback locals | revision 3 section 5.5 | server admin or operator | no, V2-LOG | section 15 |

### 5.6 Platform `Job.log` writers (assignment sites at `d8908f42`)

28 direct assignments in 17 files (`logassign.py`; revision 3 counted 30 in 16 at `00e55c27`; `api/worker_main.py` no longer writes, `api/workflows/` added two): `api/jobs/binding_resolve.py:429`; `api/jobs/iteration.py:504`, `:537`, `:551`, `:654`, `:674`; `api/jobs/iteration_dispatch.py:160`, `:426`; `api/jobs/job_principal.py:94`; `api/jobs/job_terminal_cas.py:256`; `api/jobs/queue_worker.py:385`, `:604`; `api/jobs/reaper.py:233`; `api/jobs/runner.py:67` (`_append_log`, called at `:128`, `:141`, `:168`, `:502`, `:536`, `:585`), `:661`; `api/jobs/scheduler_claim_loop.py:98`; `api/main.py:1043`, `:1051`; `api/persistence/logger.py:448` (`JobLogger._append_narrative`); `api/quality/runner_hook.py:707`; `api/storm/post_mask_hook.py:372`, `:373`; `api/storm/router.py:588`, `:699`, `:1023`; `api/synth/runner_hook.py:333`; `api/workflows/restart_recovery.py:48`; `api/workflows/runner.py:90`. R5 lands in P1 with these as its baseline.

## 6. Acceptance tests, red first

Each test is written before its slice's production change and recorded red at the named fail-before location, run from `uv.lock` (`uv sync --locked`) in the slice's own environment. Where the mechanism is new, the fail-before names the absent mechanism and the named mutation is the semantic proof, recorded red in the slice evidence.

### 6.1 Seeded corpus

Distinct high-entropy markers in exception messages (a foreign-library exception raised by a callee, a `PlanCompileError` whose config literal is a marker, an `OSError` carrying a marker path) and a schema-name canary. Each recorder first asserts a registered control event arrived (positive control), then asserts marker absence over response bytes, stored `Job.log` text, rendered reader output and notification payloads.

### 6.2 Tests (each RED first)

| # | Break | RED-first test (file::function) | Fail-before | Mutation that must turn it red | Slice |
|---|---|---|---|---|---|
| T3 | A reader replays raw text from `Job.log` | Platform `tests/test_job_log_reader.py::test_each_reader_withholds_invalid_lines[sse, log_field, node_runs, storm_summary]`: legacy free-text row with a marker, a 4097-byte line, an unknown key; positive control: a valid line renders | `api/jobs/_shared.py:203-211` (raw text returned) | skip `from_line` in one reader | P1 |
| T-P1 | A writer stores exception text | Platform `tests/sentry/test_job_log_writes.py::test_no_log_assignment_outside_job_log` (R5, baseline 28) and `::test_seeded_assignment_is_red` over `tests/sentry/fixtures/job_log/leaky_writer.py` | `api/jobs/runner.py:67` (`_append_log` assigns `job.log`) | add a `.log` assignment in a fixture-free module | P1 |
| T-P2 | `JobLogger` keeps message text | Platform `tests/test_job_log_writers.py::test_narrative_drops_message`: `JobLogger(...).error(f"failed on {MARKER}")`; stored line parses, carries `platform.job_narrative`, no marker; plus one cell per migrated writer site in the slice's files driving its path with a marker exception | `api/persistence/logger.py:448` | keep `msg` in `_append_narrative` | P2a, P2b |
| T-P2r | Release split (finding [9]) | Platform `tests/test_job_log_release_unit.py::test_no_writer_left_unmigrated`: R5 baseline empty and `JobLogger` has no narrative methods; run in P2b's gate on the merged P1+P2a+P2b result | `api/persistence/logger.py:152-205` (narrative methods present) | restore one narrative method | P2b |
| T-P4 | An API detail copies exception text | Platform `tests/sentry/test_detail_exc.py::test_detail_reads_no_exception_text` (baseline 56) and `::test_seeded_detail_is_red` over `tests/sentry/fixtures/detail_exc/leaky_route.py` (`detail=f"bad: {exc}"`, `detail=str(exc)`, `detail=exc.args[0]`); per drain slice, `tests/test_detail_exc_routes.py::test_route_detail_has_no_marker[<file>:<line>]`, one cell per rewritten site, the dependency raising `RuntimeError(MARKER)` | `api/pipelines/router.py:100` (first site in P4b) and the other 55 sites | restore `str(exc)` at one rewritten site | P4, P4a, P4b, P4c |
| T-E5 | An engine result field carries exception text | engine `tests/unit/safe_output/test_free_text_fields.py`: `test_check_result_has_no_exception_text[gcs-157, gcs-235, s3-209, s3-311, sftp-276, sftp-355]` (client factory raises `RuntimeError(MARKER)`; marker absent from `repr`, `dataclasses.asdict`, `json.dumps`; label `EXCEPTION`), `test_capacity_estimate_has_no_exception_text` (a marker `PlanCompileError` under the estimator), `test_plan_check_error_carries_reason_not_message` (positive control: the raised `PlanCompileError` from `compile_plan` does carry the marker), `test_validation_message_text_comes_from_registry` (`add_error(code, message=MARKER)` is a `TypeError`) | `connectors/gcs.py:157`, `out_of_core/_capacity_eval.py:255`, `plan/validate/_consolidator.py:92`, `validation_result.py:102` | restore the baseline line of a row | E5-1, E5-4 |
| T-E1 | R7 misses a projection shape | engine `tests/sentry/test_exception_text_projection.py::test_seeded_shapes_are_red` over fixtures `str(e)`, `str(wrap(e))`, `e.args`, `e.message`, f-string, `%`, `.format`, each inside and outside `raise` (inside `raise` is allowed); `::test_baseline_only_shrinks` | new rule (baseline 45 at `07da6ee`) | drop the `str(f(NAME))` matcher: the `str(wrap(e))` fixture passes | E1 |
| T-E6 | The parent copies child-written text | engine `tests/unit/safe_output/test_isolated_child_text.py::test_parent_copies_no_child_text`: a child that writes `error=MARKER` and `error_type="Not-A-Label"` into its envelope; the result has `error_type == EXCEPTION`, no marker anywhere, and the governor's reroute message carries the label only | `execution/_isolated_run.py:578` | copy `envelope.get("error")` again | E6 |
| T-P5n | A notification carries free text | Platform `tests/test_notifications_failed_event.py::test_error_summary_is_closed_label`: passing a `str` raises `TypeError`; the test-fire renders fixed prose; a `job_failed` event from a job that failed with a marker exception has no marker in `detail` | `api/notifications/dispatcher.py:190` (`error_summary: str`) | widen the type back to `str` | P5n |
| T-P7 | Platform copies `PlanCompileError.message` into a validation response | Platform `tests/test_v2_validation_text.py::test_validation_response_has_no_compile_message`: a pipeline whose `when` literal is a marker; the validation response lists the registry prose and the code | `api/pipelines/v2_validation.py:153-157` | pass `exc.message` through again | P7 |
| T5 | A key snapshot passes while value meaning changes | engine `tests/unit/safe_output/test_value_domains.py`: progress keys and values unchanged; `phase.error_type` domain is a superset of the 15 labels | `src/decoy_engine/phase.py:594` | change a label value | E0a |
| T14 | Serializer limits or the double fault leak | engine `tests/unit/safe_output/test_to_line_limits.py`: `test_exact_4096_bytes_passes`, `test_4097_bytes_becomes_invalid`, `test_depth_3_passes_depth_4_becomes_invalid`, `test_double_fault_emits_failed_literal`, `test_from_line_rejects_failed_literal`, hostile-line corpus (duplicate keys, unknown key, subclass values) | new module (`ModuleNotFoundError: decoy_engine.safe_output` at `07da6ee`) | truncate instead of substitute; let the double fault raise | E0b |
| T11 | Run identity forged or malformed (stack) | Platform `tests/test_run_identity.py` (backfill, CHECK, `@validates`, request rejection, AST sentry) | `api/models.py:329` (no `run_uuid`) | accept a v1 UUID in `@validates` | S-P0 |
| T22 | One job, several engine calls (V1-2) | Platform `tests/test_job_output_scope.py`: `test_one_scope_spans_run_and_hooks[flag_off, flag_on]` (a job whose `run_pipeline` names `customers` and `orders` and whose quality, synth and STORM hooks name `customers` again: one alias per table across stored lines, one legend row per `(kind, ordinal)`, no IntegrityError, legend names match the lines), `test_iteration_children_get_own_scopes`, `test_storm_job_single_scope`, `test_second_run_while_bound_raises`, `test_only_job_scope_constructs_scopes` (AST) | new mechanism; without it each engine call would open its own scope at `api/jobs/runner.py:576` and `api/jobs/dispatch_worker_main.py:505` | enter `job_output_scope` inside `_run_post_success_report_hooks` instead of at the root | S-P3a |
| T9, T15 | Child and parent ordinals disagree; several children in one job (stack) | engine `tests/unit/safe_output/test_alias_handoff.py` (revision 3 cells: two sequential children, governor reroute with `merge_lost`, multi-table split, coordinator allocation while `LENT`, second lend, non-contiguous merge) | new mechanism | freeze at the first `lend()`; skip the contiguity check | S-E3a, S-E3b |
| T12 | Legend leaks or mismatches (stack) | Platform `tests/test_job_alias_legend.py::test_owner_only_and_matches_lines` | new route | drop `require_job_owner_or_404` | S-P3b |
| T16 | A failed legend write disturbs the terminal record (stack) | Platform `tests/test_job_alias_legend.py::test_terminal_survives_legend_write_failure` (`pg_semantic` and SQLite legs): the legend builder emits ordinal 0 (CHECK violation); the job stays terminal, `Job.log` keeps its terminal line and gains `legend_unavailable`, no legend rows; positive control: a valid legend is stored | `api/jobs/job_terminal_cas.py:256-266` (no legend at baseline) | remove `begin_nested` around the legend insert | S-P3b |

### 6.3 Exit-path matrix

`tests/unit/safe_output/test_exit_matrix.py::test_exit_paths` (engine) over 5.1's serializer and isolated rows; Platform `tests/test_job_log_exit_paths.py` over 5.1's terminal rows (success, failure, cancel, reaper): the terminal line is the last parsed line and the status commits. With the stack, `tests/test_job_output_scope.py::test_exit_paths` adds scope exit on normal, exception, nested and cancel-while-thread-runs (late allocations get `kind-0`).

### 6.4 FK-shape output matrix

For each shape in 5.3 on the relational routes: relational output is byte-identical to the baseline before and after E6 (the only V1 slice on an execution path).

### 6.5 Ratchets

Each ratchet mirrors the module-size census (`tests/sentry/test_module_size.py:66`): counts only shrink, stale entries fail, a seeded fixture proves the rule fires. R5 (Platform, P1, baseline 28, empty after P2b); `detail-exc` (Platform, P4, baseline 56, empty after P4c); R7 (engine, E1, baseline 45 at `07da6ee`, shrinking by the E5-1, E5-4 and E6 sites; the rest is a named limit); `notify-closed` (Platform, P5n, a type test, no baseline).

## 7. Frozen contracts touched

| Contract | Change |
|---|---|
| `decoy-engine` public API | Additive `decoy_engine.safe_output` (`SafeEvent`, `SafeErrorType`, `safe_error_type`, `to_line`, `from_line`, `render_text`). Pre-GA breaks (`src/decoy_engine/release.py`): `CheckResult.detail`, `CapacityEstimate.message`, `PlanCheckError.message`, `ValidationMessage.message`/`hint`, `IsolatedRunResult.error` replaced as in 3.5; `ValidationResult.add_error`/`add_warning` lose `message` and `hint`. Version: the next engine minor from the release lineage; consumers pin exactly that |
| Reason registry 1.0.0 (`src/decoy_engine/reasons/_registry.py:24`) | Additive `platform` and `safe_output` namespaces; minor bump to 1.1.0 per `tests/unit/test_reason_registry.py:151` |
| Progress v0/v1 | Unchanged |
| `phase-result/v1`, `phase-event/v1` | Keys unchanged; `error_type` domain widens additively |
| Platform OpenAPI and web client | Shapes unchanged. Content: `log` text and SSE `data:` lines become rendered v1 records; the 56 rewritten `detail` strings lose exception text; `ValidationMessage` text comes from the registry; notification `detail` uses fixed prose. Stack adds `jobs.run_uuid`, `job_alias_legend`, `job_node_runs.node_alias`, `GET /api/jobs/{id}/alias-legend` |
| `decoy-safe-output/v1` (new) | Additive only after release |

## 8. Reuse inventory

| Existing | Source | Use |
|---|---|---|
| F29 registry, records, identity form | `src/decoy_engine/reasons/_registry.py:24-38`, `_record.py:115-260`, `_identity.py:19-57` | Prose authority, `ReasonRef`, `COUNT_KEYS`, `approve_name` |
| Reason adapter | `src/decoy_engine/reason_adapter.py:146` | `PlanCheckError.reason`, isolated `reason` |
| STORM exact-type table | `src/decoy_engine/phase.py:555-595` | Lifted into `safe_output._exception`; `phase` delegates |
| Isolated safe summary | `src/decoy_engine/execution/_isolated_common.py:238-259` | Its class-name and trusted-code logic becomes `safe_error_type` plus `reason`; no new summary |
| Module-size census ratchet | `tests/sentry/test_module_size.py:66` | Baseline pattern for the ratchets |
| Terminal savepoint | `api/jobs/job_terminal_cas.py:256-266` | Pattern for the stack's legend write |
| Owned worker thread context copy | `api/jobs/_dispatch_supervisor.py:219-220` | Carries the job scope into the engine thread (stack) |
| Platform `exc_safe` | `api/logging/exc_safe.py:76` | Same label shape for API details |
| Alembic backfill pattern | `api/migrations/versions/obs3_request_id.py` | `run_uuid` migration (stack) |
| `pg_semantic` marker | Platform `pyproject.toml` | T16 Postgres leg must run, not skip |

## 9. Consumer promises

- F25 4c (owner): `scratch._os_error.classify_os_error` is admitted as a reviewed exception boundary; R7 allows `.errno` there only. 4c-5a may merge before the stack's S-E3a; F31 rebases onto it.
- F34 (owner): F31 promises `safe_error_type`, `SafeErrorType` and additive `COUNT_KEYS`; with the stack, `SafeOutputScope`. Not promised: sealed payloads, child fd bootstrap, an atomic ordinary-file sink.
- F36 (owner): both reason codes engine-owned in `decoy-reasons/v1`; F31 keeps `families/invariants.py` and closed count names.
- A-4 / B-5: entry-frame scrubbing for paths and configs is V2-LOG; A-4 round 4 keeps frames that hold secret material. The documented operator control (`docs/security/error-reporting-and-data-exposure.md:23-24`) stays the stated coverage.
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

## 11. Slices

Size bound: at most 10 production files and at most 800 changed lines per slice, tests included (insertions plus deletions from `git diff --stat`; `uv.lock` excluded). A slice that measures over the bound is split before it merges and this table is updated in the same change. Sizes below are ESTIMATES made while planning (no code written): production lines from the number of sites times the expected rewrite size, test lines from the named cells. Each slice: RED tests first with output recorded, smallest change, self-check, review, at most two remediation rounds.

| Slice | Repo | Scope | Prod files | Est. prod lines | Est. test lines | Est. total | Blocks V1 | Depends on |
|---|---|---|---|---|---|---|---|---|
| E0a | engine | `safe_output/__init__.py`, `_event.py`, `_exception.py` (table lifted from `phase`), `phase.py` delegation; T5, field walk | 4 | 300 | 300 | 600 | yes | none |
| E0b | engine | `_wire.py` (`to_line`, `from_line`, `render_text`), export; T14 | 2 | 260 | 380 | 640 | yes | E0a |
| E0c | engine | `reasons/platform/`, `reasons/safe_output/`, `reasons/_registry.py` 1.1.0 (prose for the 28 writer sites, the narrative, the notification labels) | 4 | 220 | 120 | 340 | yes | E0a |
| E1 | engine | R7 ratchet, baseline, fixtures; T-E1 | 0 | 0 | 350 | 350 | yes | E0a |
| E5-1 | engine | `CheckResult` (`sdk.py`, `connectors/gcs.py`, `s3.py`, `sftp.py`); T-E5 connector cells | 4 | 70 | 220 | 290 | yes | E1 |
| E5-4 | engine | `CapacityEstimate` (`execution/capacity.py`, `out_of_core/_capacity_eval.py`), `PlanCheckError` (`plan/validate/_consolidator.py`), `ValidationMessage` (`validation_result.py`); T-E5 cells | 4 | 180 | 260 | 440 | yes | E1 |
| E6 | engine | `execution/_isolated_common.py`, `_isolated_run.py`, `_isolated_worker.py`, `_governor.py`; T-E6, 6.4 | 4 | 120 | 220 | 340 | yes | E1 |
| ER | engine | release from the lineage (version, changelog) | 2 | 20 | 0 | 20 | yes | E0b, E0c, E5-1, E5-4, E6 |
| P1 | Platform | `api/jobs/job_log.py` (new), readers `api/jobs/_shared.py`, `routes_streaming.py`, `routes_runtime_facts.py`, `api/storm/router.py` (reader only), engine pin `pyproject.toml`; R5 (baseline 28); T3, T-P1, characterization first | 6 | 300 | 380 | 680 | yes | ER |
| P2a | Platform | writers in 10 files (11.1), `_append_narrative` through `append`; T-P2 cells | 10 | 220 | 320 | 540 | yes | P1 |
| P2b | Platform | writers in 10 files (11.1), `JobLogger` narrative methods removed, R5 empty; T-P2 cells, T-P2r | 10 | 260 | 320 | 580 | yes | P2a |
| P4 | Platform | `detail-exc` rule, baseline 56, fixtures | 0 | 0 | 250 | 250 | yes | none |
| P4a | Platform | 10 files, 21 sites (11.1); T-P4 cells | 10 | 75 | 350 | 425 | yes | P4 |
| P4b | Platform | 10 files, 19 sites (11.1); T-P4 cells | 10 | 70 | 320 | 390 | yes | P4 |
| P4c | Platform | 7 files, 16 sites (11.1); T-P4 cells; baseline empty | 7 | 60 | 280 | 340 | yes | P4 |
| P5n | Platform | `api/notifications/dispatcher.py`, `api/notifications/router.py`; T-P5n | 2 | 35 | 80 | 115 | yes | none |
| P7 | Platform | `api/pipelines/v2_validation.py` to the new `ValidationMessage`; T-P7 | 1 | 50 | 90 | 140 | yes | ER |
| X | all | cross-repo marker corpus on the released engine and pinned Platform commit | 0 | 0 | 300 | 300 | yes | P2b, P4c, P5n, P7 |
| S-E3a | engine | `_scope.py`, `_alias.py`, `__init__` (coordinator, per-job table); T9 | 3 | 320 | 380 | 700 | no (stack) | E0b |
| S-E3b | engine | lend and merge across isolated children: `_isolated_run.py`, `_isolated_worker.py`, `_governor.py`, `_scope.py`; T15 | 4 | 200 | 400 | 600 | no (stack) | S-E3a, E6 |
| S-E4 | engine | `_sink.py` (`on_legend`), `_view.py` (aliased operational view), `context.py` `node_alias` | 3 | 260 | 250 | 510 | no (stack) | S-E3b |
| S-P0 | Platform | migration `f31_run_uuid`, `api/jobs/run_identity.py`, `api/models.py`, `api/jobs/schemas.py`; T11 | 4 | 220 | 300 | 520 | no (stack) | none |
| S-P3a | Platform | `api/jobs/job_scope.py` (new), `job_log.py` run binding, roots `runner.py`, `dispatch_worker_main.py`, `storm/router.py`; T22 | 5 | 250 | 300 | 550 | no (stack) | S-P0, P2b, stack engine release |
| S-P3b | Platform | migration (`job_alias_legend` with UNIQUE and CHECK, `job_node_runs.node_alias`), `api/models.py`, `job_scope.py` legend write, route module, router registration, `routes_runtime_facts.py` alias join; T12, T16 | 6 | 300 | 350 | 650 | no (stack) | S-P3a |

Totals (estimates): V1 slices 18, about 6,780 lines including tests; stack slices 6, about 3,530 lines. Revision 3 had 43 slices.

No engine slice edits another repository. Self-check per slice: the slice's named tests, then `scripts/dev/gates.sh --baseline` where it exists, run from `uv.lock` (`uv sync --locked`).

### 11.1 File lists

Platform paths are under `api/`. P4 groups are alphabetical by file, so a reviewer can check them against `platform-d8908f42.txt` in one pass.

- P2a: `jobs/runner.py`, `jobs/iteration.py`, `jobs/iteration_dispatch.py`, `jobs/job_terminal_cas.py`, `jobs/reaper.py`, `jobs/queue_worker.py`, `jobs/job_principal.py`, `jobs/scheduler_claim_loop.py`, `jobs/binding_resolve.py`, `persistence/logger.py` (`_append_narrative` only).
- P2b: `quality/runner_hook.py`, `storm/router.py`, `storm/post_mask_hook.py`, `synth/runner_hook.py`, `main.py`, `workflows/restart_recovery.py`, `workflows/runner.py`, `persistence/logger.py` (narrative methods removed), `jobs/route_narrative.py`, `jobs/v2_orchestrator.py`.
- P4a (10 files, 21 sites): `admin/audit_redaction_router.py` (2), `admin/router.py` (2), `analytics/router.py` (2), `composites/preview.py` (2), `data_tables/router.py` (1), `disguises/router.py` (2), `environments/binding_router.py` (2), `files/router.py` (1), `files/s3_pull_router.py` (4), `fixed_width_layouts/router.py` (3).
- P4b (10 files, 19 sites): `header_layouts/router.py` (1), `jobs/_create_phases.py` (3), `jobs/_service.py` (1), `jobs/binding_resolve.py` (2), `jobs/routes_evidence.py` (1), `jobs/routes_preview.py` (3), `notifications/router.py` (1), `org_disguises/router.py` (2), `pipelines/router.py` (4), `reporting/parsing.py` (1).
- P4c (7 files, 16 sites): `schema_contract/router.py` (2), `seeds/admin_router.py` (6), `server_logs/router.py` (2), `storm/detectors_router.py` (3), `subset/router.py` (1), `text_redact/preview.py` (1), `unmask/router.py` (1).

## 12. Worker evidence rules

- Name the class of each finding (1 to 10) and fix the class structure, not only the instance.
- Write the tests first and record them red at the named fail-before location; run baselines from `uv.lock` (`uv sync --locked`) in the same environment as the branch and say so.
- Attack your own diff: apply each mutation named in 6.2 once and show the test goes red; never commit while a mutation is applied.
- Never widen a threshold or baseline, skip a test, weaken an assertion or edit a pinned test; state what you did not run.
- If a defect of a class already in the catalog recurs, stop and report it; do not patch a third time.
- Apply the V1 bar: a finding that only reaches an admin-only surface goes to the report's V2-LOG with `file:line`, one line, and does not block the slice.
- P1, P2a and P2b merge in one window; no Platform release is cut between them.

## 13. Owner questions (each with a recommended default)

- **O1 (alias, legend and run-UUID stack). Recommended default: CUT.** The stack (S-E3a, S-E3b, S-E4, S-P0, S-P3a, S-P3b; G4's run binding; the alias parts of revision 3's E4a and P1) was decided on 2026-10-30, so revision 4 keeps it, with the V1-2 fix (section 3.7). Under the V1 bar it does not block: schema names are not Rule 3 values, and the surfaces the aliases would protect beyond `Job.log` are admin-only. Cutting it removes 6 slices and about 3,530 estimated lines, removes the V1-2 failure mode entirely, and loses what section 14 lists. Answer with one word: CUT or KEEP.
- **O2 (in-process `raise` messages).** 75 engine `raise` sites interpolate an except-bound name at `07da6ee`. Default: leave them (V2-LOG); part (b) stops that text where Platform copies it into a response, and P1 stops it in `Job.log`.
- **O3 (Platform exception text outside details).** Revision 3 counted 92 except-bound text reads in 39 Platform files outside `raise`, `HTTPException`, `write_audit*` and logger calls (response fields, model columns, helper returns); not re-measured at `d8908f42`. Some may reach a non-admin response, which would be Rule 3. Default: P4 classifies them (tests-only, one census row each: `response`, `admin`, `internal`), and any `response` row becomes a new bounded slice P4d planned in a revision 5 amendment; the rest go to V2-LOG.
- **O4 (admin-only routes among the 56 details).** Default: rewrite them with the others (one expression each, cheaper than proving each route admin-only).
- **O5 (E5-1 and E5-4 readers).** An import scan of Platform at `d8908f42` finds no reader of `CheckResult`, `CapacityEstimate` or `PlanCheckError`; the review placed them in the V1 core, and the CLI (not attached) may print `CheckResult.detail`. Default: keep them in V1 (two slices, about 730 estimated lines); answer "move" to put them in V2-LOG.

## 14. Cut variant (costed; pick with one word: CUT)

If O1 is answered CUT, these change and nothing else:

| Removed | Production files | Est. lines (prod plus tests) |
|---|---|---|
| S-E3a (scope, alias table, coordinator) | 3 | 700 |
| S-E3b (lend and merge across isolated children) | 4 | 600 |
| S-E4 (aliased operational view, `on_legend`, `node_alias`) | 3 | 510 |
| S-P0 (`run_uuid` migration, minting, validation, schema exclusion) | 4 | 520 |
| S-P3a (`job_output_scope`, run binding, three roots) | 5 | 550 |
| S-P3b (legend table and route, `node_alias` join) | 6 | 650 |
| Total | 25 production file touches, 2 migrations, 1 route | about 3,530 |

Also removed: `SafeEvent.run` and `aliases` (the event keeps `step`, an approved node id, so `Job.log` shows real schema names, which are owner names and not Rule 3 values); `from_line`'s `expected_run`; T9, T11, T12, T15, T16, T22. V1 slices are unchanged; the V1 merge gate is unchanged.

What is lost: aliasing of names on operational output (`Job.log` shows real table and node names to job viewers; engine and server logs are admin-only and V2 anyway); the owner-only legend; binding of a stored line to its run (a line written into another job's `Job.log` by direct database access would render; that needs database write access, outside the V1 threat model); the run UUID as the F30 span id and F32 bundle key (both key by `Job.id` instead). Owner decision of 2026-10-30 items "progress views are owner results with real names" and "legacy rows withheld at read" are unaffected.

## 15. V2-LOG (logged, not planned; class tag in brackets)

- R1 dynamic class-name drain, revision 3's E2b-1 to E2b-3 (56 sites in 30 files at `ab29ef0c`), e.g. `src/decoy_engine/connectors/postgres_snapshot.py:241` [1]
- R2 engine logger-argument drain, revision 3's E4b-1 to E4b-4 (116 calls in 37 files), e.g. `src/decoy_engine/internal/logging.py:129` [1]
- Entry-frame scrubbing (`closed_entry`, F1, F2), revision 3's E8a, E8b-1, E8b-2: traceback locals are admin-only; the grep in section 1 finds no traceback serializer, `src/decoy_engine/fileaccess/_entry.py:50` [1]
- Audit-detail half of revision 3's P4: 109 non-constant audit details; the audit router is `require_admin`, `api/audit/router.py:159` [1]
- Census machinery (T0, `tools/safe_output_inventory.py`, generated surface, export and entry JSON, T21 slice manifest): replaced by the traceback grep and per-ratchet baselines, `docs/plans/evidence/2026-11-01-f31-r4-anchors/platform-d8908f42.txt` [8]
- Metrics label from a class name (revision 3's P5), `api/observability/metrics.py:391` [1]
- Child stdio classification (revision 3's P6, R6), 8 Platform spawn sites, e.g. `api/files/pg_pull.py:242` [1]
- CLI stderr and `print_exception` (revision 3's C1, C2), `decoy/src/decoy/cli/scan.py:794` (unverified) [1]
- Engine R7 sites outside the V1 parts (named limit in the R7 baseline), e.g. `src/decoy_engine/plan/_generation.py` (3 sites) [1]
- `HTTPException` detail that reads only a class name, `api/cloud/accounts.py:572` [1]
- `JobEstimate.notes` class name, `src/decoy_engine/execution/_job_estimate.py:98` [1]
- `UnmaskColumnReport.detail` strategy text, `src/decoy_engine/unmask.py:393` [1]
- `QualityWarning.detail` open dict, `src/decoy_engine/generation/pool/_events.py:39` [1]
- `ExecutionEvent.detail` free-text export with no producer, `src/decoy_engine/execution/_events.py:19` [1]
- `StructuredEvents.step` `error_msg` parameter, `src/decoy_engine/context.py:74` [1]
- Engine narrative names a table and column in the stdlib log, `src/decoy_engine/progress.py:119` [1]
- Capacity recheck logs `result.detail` to the server log, `api/scheduler/admission_pass.py:457` [1]
- Engine `raise` messages that interpolate an estimate or exception message, `src/decoy_engine/execution/capacity.py:353`, `:504` (O2) [1]

## 16. Revision 4 change log (findings mapped to sections and slices)

| Finding (Dennis review of revision 3, V1 bar) | Class | Resolution | Sections | Slices |
|---|---|---|---|---|
| V1-1: G1 and G6 promise completeness over admin-only surfaces; ER gated on R1, R2, F1 | 8 | Guarantee restated as Rules 1 to 3 with ratchets and named limits; G1 to G6 deleted; ER depends only on V1 engine slices; R1, R2, F1 dropped from the gate | 0, 1, 11 | ER |
| V1-2: one run is not one job; per-call scopes give conflicting legend rows | 3 | `job_output_scope` is the single owner, entered at three lifecycle roots; legend UNIQUE `(job_id, kind, ordinal)`, written once at scope exit; T22 spans `run_pipeline` and the three hooks on both dispatch paths | 3.7, 6.2 T22 | S-P3a, S-P3b |
| Owner decision on the stack | n/a | Kept; O1 recommends CUT; cut variant costed | 13 O1, 14 | S slices |
| V1 core never stated together | 8 | Section 0: one design, four parts plus notifications; "blocks V1" and "is V2" lists | 0 | all |
| E2b R1 drain | 1 | V2-LOG | 15 | removed |
| E4b R2 drain | 1 | V2-LOG | 15 | removed |
| E8a, E8b entry frames, F1, F2 | 1 | V2-LOG; one traceback-serialization grep recorded | 1, 15 | removed |
| P4 audit-detail half (109 sites) | 1 | V2-LOG; P4 keeps the `HTTPException` half (56 sites) | 3.4, 15 | P4, P4a to P4c |
| Census machinery T0 and generated JSON | 8 | Replaced by a single grep plus per-ratchet baselines | 1, 15 | removed (E1 keeps only R7) |
| P5 metrics, P6 child stdio, C1, C2 | 1 | V2-LOG | 15 | removed |
| No notifications surface row | 4 | Row S25; closed `FailureLabel`; T-P5n | 0, 3.6, 5.5, 6.2 | P5n |
| P1, P2a, P2b split across releases | 9 | One merge window, T-P2r in P2b's gate | 1, 3.3, 10, 12 | P1, P2a, P2b |
| Platform anchors moved | 8 | Re-read at `d8908f42` (the handoff's `b290593f` predates revision 3's base): `Job.log` writers 28 in 17 files (was 30 in 16); terminal savepoint `job_terminal_cas.py:256-266`; `JobLogger` constructors `v2_orchestrator.py:377`, `storm/router.py:1031`; API details recounted as 56 text sites in 27 files | header, 3.3, 3.4, 5.6 | P1, P2a, P2b, P4 |
| Found in revision 4: Platform copies `PlanCompileError.message` into a validation response | 1 | P7 now V1 with T-P7 | 0, 3.5 | P7 |
| Found in revision 4: revision 3's R7 probe misses `str(wrap(exc))` | 8 | R7 matcher and fixture | 3.5, 6.2 T-E1 | E1 |
| Found in revision 4: the isolated child's text is value-free today | 6 | E6 kept as a producer-closing fix; stated as such | 3.5 | E6 |
| Size bound now counts tests | 8 | Slices re-estimated with tests; E0 split into E0a, E0b, E0c; stack E3 and P3 split | 11 | E0a to E0c, S-E3a, S-E3b, S-P3a, S-P3b |
