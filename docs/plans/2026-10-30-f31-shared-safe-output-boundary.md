Status: plan only, revision 2 after the Codex hostile review of revision 1 (NO-GO). No implementation or release approval is granted by this document.

Summary: F31 is the sanitizer behind F29. The F29 reason registry, reason records and typed progress events already carry no free text; F31 decides what may be NAMED on each output surface (real schema names on owner results, aliases on operational output, nothing on the data plane), gives the engine one plain typed operational event with one serializer and one parser, one exact-type exception policy, a run identity that survives a process and a database, and sentries that keep every emitter behind those choke points. Under the V1 threat model the boundary is validated typed data plus static enforcement, not provenance or sealing.

Baselines: engine `ab29ef0c` (`claude/eager-clarke-14deh1`, the STORM close-out tip; it contains `origin/main` `b509c7e8` and F29 `fdd08a89`, merged as #336). `origin/main` has since taken #347 (`scripts/dev/`) and #348 (an execution module split); F31 cites nothing that #348 moved, and the E1 census is re-recorded on the merged tip. CLI `decoy` `origin/main` `d28fae2` (contains F43, merged as #69 at `b2209e1`). Platform `origin/main` `00e55c27`. Planning branch: `plan/f31-safe-output`.

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md` (PLAN, "Established solutions", "Design notes"), `security.md`, `api-and-compatibility.md`, `testing.md`, `data-and-migrations.md`, `observability-and-resilience.md` (log content rule), `architecture.md`, `reuse-first.md`, `refactoring.md`; `DEFECT-CATALOG.md` and `OWNER-DECISIONS-2026-10-30.md` (decoy-platform `handoffs/queue`); the engine, CLI and Platform `CLAUDE.md`.

# F31: Shared safe-output boundary, PLAN (revision 2)

Risk: R2 (public API addition, wire contract, three-repository migration, two schema migrations). The owner decisions of 2026-10-30 are applied as written and are not reopened: three-class surface table; progress views are owner results with real names; a per-job owner-only alias legend; legacy `Job.log` rows withheld at read, never purged; run identity is a Platform-minted UUID beside the integer `Job.id`; the F25 4c, F34 and F36 rulings in section 9; the V1 threat model below.

## 1. Guarantee (invariants as properties)

**Threat model (V1, binding).** Self-hosted, single-org; the engine process is trusted. The leak is ACCIDENTAL: a source value or path in exception text, `args`, `__notes__`, a normal `__cause__`/`__context__` chain, a log line, a `repr`, a progress message, a result field or a serialized report. Canaries are ordinary shapes (str, int, list, dict, a dataclass with a marker in its repr, an ordinary exception raised by a callee). Hostile in-process objects (forged instances, hostile dunders, `BaseException` from dunders, frame and `gc` games, descriptor aliasing) are V2 (section 1, last paragraph).

| Class | Shows | Never shows | Surfaces |
|---|---|---|---|
| (a) operational | registry prose, closed enums, bounded integers, positions, the run UUID, aliases `table-N` / `field-N` / `node-N` / `artifact-N` | any schema name, path, DSN, credential, exception text or dynamic class name, cell value, free prose | engine and server logs, `Job.log`, diagnostics, metrics, support bundles, CLI stderr |
| (b) owner result | everything (a) may show plus real schema names (table, column, node id, repository-relative config pointer) | any source or masked CELL VALUE, sample, statistic computed from values, path outside the owner's project, credential, exception text or dynamic class name | progress views, results and reports, evidence, `scan --json`, API success and error bodies, audit, node runs, the alias legend |
| (c) data plane | the data itself | n/a: written only by the exact writer manifest; read only by data-plane routes | masked and subset output, vault, quarantine rows, raw profile artifact, discovery rows |

**P1 (operational).** For every (a) surface S and every value v of the classes in the threat model, the bytes S emits contain no derivative of v. The only producers of (a) bytes are `safe_output.to_line`, `safe_output.render_text` and the Platform OBS-3 formatters (3.5).

**P2 (owner result).** No (b) surface carries a cell value, a value-derived statistic, exception text or a dynamic class name. Names appear as the owner declared them. The fields in 5.4 marked (c) are absent from every (b) projection.

**P3 (data plane).** A data-plane writer is an exact function in the generated writer manifest; no (a) or (b) renderer accepts a data-plane object.

**P4 (re-ingest).** A `Job.log` line is replayed only after `from_line(line, expected_run=job.run_uuid)` accepts it; a line that fails is counted and withheld. Server logs are not replayed as events (3.5).

**P5 (exception types).** Exception type labels come only from `safe_error_type(exc)`, an exact-type lookup `_ALLOWED_EXCEPTION_TYPES.get(type(exc), SafeErrorType.EXCEPTION)`. No production code reads `__name__`, `__qualname__` or `__class__` of an exception.

Non-goals: no new encryption or hashing; regex scrubbing (`api/logging/redaction.py`) stays a backstop; no `--unsafe-debug-log`. V2 note (hostile in-process hardening, recorded, not planned): provenance tokens, sealed payloads, forged-instance detection past `__post_init__`, hostile dunder defenses, process-wide stream ownership, child fd bootstrap, sentries over aliases and descriptors.

## 2. Established solutions and design notes

Established solutions (what the design follows; cited again in module docstrings per the engine rule): RFC 9457 identity-versus-occurrence split, already the F29 pattern (`src/decoy_engine/reasons/_record.py:1-17`); OpenTelemetry semantic conventions, `error.type` as a low-cardinality identifier, already the `reason_adapter` pattern (`src/decoy_engine/reason_adapter.py:1-7`); the OWASP Logging Cheat Sheet (log the event and its location, never the data); python-json-logger's single exception field, already the Platform `exc_safe` shape (`api/logging/exc_safe.py:1-18`); SARIF fingerprints and fixed message registries, already F43 `SafeFinding` (`decoy/src/decoy/ci/contracts.py:1-11`, `:228-275`); "Parse, don't validate" for the stored-line parser; RFC 9562 UUIDv4 version and variant bits for the run id; SQLAlchemy `@validates` plus a CHECK constraint for a database-boundary invariant; the Alembic add-nullable, backfill, then constrain pattern the repo already uses (`api/migrations/versions/obs3_request_id.py:1-16`). Remaining custom code: the alias table and the `decoy-safe-output/v1` record, because no library knows the engine's plan order or reason registry.

Design notes: new structure is one engine package `decoy_engine.safe_output` (`_event.py`, `_exception.py`, `_wire.py`, `_scope.py`, `_alias.py`, `_sink.py`, `families/`) and one Platform module `api/jobs/job_log.py`. Principles applied: one place per fact (prose lives only in the registry; the alias legend lives only in `job_alias_legend`; the exception table lives only in `_exception.py`); typed context objects (`SafeEvent`, `SafeOutputScope`) over keyword soup; single-purpose functions (`safe_error_type`, `to_line`, `from_line`); no flag arguments. House style: comments say why.

## 3. Design

### 3.1 F29 is the model, F31 is the sanitizer behind it

Kept unchanged: `ReasonRecord`/`ReasonContext` (`src/decoy_engine/reasons/_record.py:115-212`, `:242-260`), the progress variants and their allowlisted projections `event_to_dict` and `event_to_legacy_dict` (`src/decoy_engine/_progress_events.py:513-599`), which stay JSON-ready dicts with the documented key sets (`tests/unit/progress/test_versioned_contract.py:37-78`, `:101-112`), and the `decoy-reasons/v1` identity form (`src/decoy_engine/reasons/_identity.py:19-57`). Progress is an owner result: `table` and `column` keep real names through `approve_name` (`_record.py:83`, `_progress_events.py:530-531`). STORM symbols relied on: `phase._ERROR_KINDS` and `error_result` (`src/decoy_engine/phase.py:555-595`), `_contained` and `_fixed_text` (`:160-181`), `REGISTRY_SCHEMA`, `ReasonRef`, `require_known`, `UnknownReasonError` (`_identity.py:19-57`), and the sentry `tests/sentry/test_phase_containment.py:17`.

Added, `SafeEvent` (`safe_output/_event.py`): an ordinary frozen dataclass validated in `__post_init__` and again in `to_dict`, with exact-type checks on nested groups exactly as `_V1Fields` does (`_progress_events.py:262-270`). Fields: `run: uuid.UUID | None`, `seq: int`, `ts_ms: int`, `sev: reasons.Severity`, `kind: EventKind` (closed enum), `phase: reasons.Phase | None`, `op: reasons.Operation | None`, `outcome: reasons.Outcome | None`, `reason: ReasonRef`, `err: SafeErrorType | None`, `loc: SafeLocation | None` (component enum plus integer `line`, `column`, `row`, `chunk`, `ordinal`), `counts: tuple[tuple[str, int], ...]` under `COUNT_KEYS` (`_record.py:34-36`, extended additively), `aliases: tuple[SafeAlias, ...]`, `edges: tuple[SafeEdge, ...]`, `ver: tuple[SafeVersion, ...]`, `flags: frozenset[Flag]`. There is no `str`, `Any`, mapping or extension field. `operational_view(progress_event, scope)` projects an F29 progress event onto a `SafeEvent` with names replaced by aliases; that projection, not the progress dict, is what logs receive.

Serializers (`safe_output/_wire.py`), the only producers of (a) bytes: `to_dict(event) -> dict` (the `decoy-safe-output/v1` record, closed keys `s`, `rv`, `run`, `seq`, `ts`, `sev`, `kind`, `phase`, `op`, `outcome`, `reason`, `err`, `loc`, `counts`, `aliases`, `edges`, `ver`, `flags`); `to_line(event) -> bytes` (`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`, at most 4096 bytes, depth at most 3; a record that fails becomes the registered `safe_output.invalid`, and a failure of that is the constant ASCII literal `safe_output.failed`); `render_text(record) -> str` (`ISO_TS  LEVEL  STEP  MSG`, STEP is `node-N` or `job`, MSG is the registry summary plus closed facts); `from_line(line: bytes, *, expected_run: uuid.UUID) -> SafeEvent | None` (bounded parse, closed keys, every field re-checked, `rv` minor not newer than the reader, reason known via `require_known`, `run` equal to `expected_run`; anything else is `None`). `metric_labels(event) -> dict[str, str]` yields closed-enum values only.

### 3.2 Exception boundary

`SafeErrorType` is a closed `str` enum built once at import from exact classes: every builtin exception class (enumerated from `builtins` as `_safe_error_names` does today, `_progress_events.py:69-81`), the engine families in `reason_adapter._CODED` (`reason_adapter.py:39-85`) and the exception classes of `decoy_engine.errors`, plus `EXCEPTION`; `_ALLOWED_EXCEPTION_TYPES` maps each exact class to its member and the member's label is that class's name as engine code recorded it. The label set therefore equals today's `_SAFE_NAMES` set, and `phase._ERROR_KINDS` (`phase.py:555-570`, 15 builtins) is a subset of it. `safe_error_type(exc)` is the one lookup (P5). Consumers migrated: `phase.error_result` (`phase.py:594`; its `error_type` domain widens from 15 labels to the closed table, an additive change recorded in section 7), `_progress_events.safe_error_class` (`:86-98`, deleted; the `error_class` sanitizer becomes `SafeErrorType.from_label`, so the progress value domain is unchanged), `progress.run_scope` (`progress.py:445`), `exception_error_text` and `commit_error_text` (`src/decoy_engine/execution/_isolated_common.py:246`, `:253`), the six `postgres_snapshot` `cause_type` sites (`src/decoy_engine/connectors/postgres_snapshot.py:242`, `:256`, `:327`, `:408`, `:491`, `:515`; `cause_type` becomes the label), Platform `api/observability/metrics.py:391`.

`safe_event_from_exception(exc, *, reason=None, loc=None) -> SafeEvent` takes the reason from `reason_from_exception` (`reason_adapter.py:146`) and the label from `safe_error_type`; it stores nothing of `exc`. `contain(fn)` lifts `phase._contained` (`phase.py:168-181`) into `safe_output._exception`; `phase` keeps the `__phase_contained__` mark so its sentry is unchanged. Frame contract, "no original or data-owning frames": a `SafeBoundaryError` is raised outside the handler after the boundary deletes its own arguments, with `__cause__` and `__context__` `None`, `__suppress_context__` set, no `__notes__`; its `__traceback__` starts at the boundary frame. No ordinary path calls `traceback.format_exc`, `print_exception`, `TracebackException`, `logger.exception` or passes `exc_info` (the engine has zero such sites today; `tests/sentry/test_log_interpolation.py:23-71` already lists the risky names).

### 3.3 Run identity, scope, aliases, legend

- Run: one run is one job. Platform mints `uuid.uuid4()` in one function `api/jobs/run_identity.py:mint_run_uuid` at job creation and stores it in `jobs.run_uuid` (`String(36)`, unique, not null after backfill) beside `Job.id` (`api/models.py:329`). DB-boundary enforcement: the column default is `mint_run_uuid`, so each of the 8 `Job(...)` constructions at `00e55c27` gets one without naming it; a SQLAlchemy `@validates("run_uuid")` accepts only the canonical form of a version-4 UUID; a CHECK constraint enforces length 36, version nibble `4` and variant `[89ab]`; an AST sentry forbids any explicit `run_uuid=` argument or `.run_uuid` assignment outside `mint_run_uuid`; every request schema excludes the field (a body that carries `run_uuid` is rejected, test T11). The CLI mints one UUID per invocation in `main`. The engine never sees `Job.id`.
- Scope: `SafeOutputScope.for_run(run)` accepts only `type(run) is uuid.UUID and run.version == 4`, binds a `ContextVar`, owns the alias table and the event sequence. `SafeOutputScope.current()` returns the bound scope or the process scope (`run=None`). A record with `run=None` is process-scoped (allowed in engine and server logs, never storable in `Job.log`).
- Aliases: identity is `(run, kind, ordinal)`, kinds `table`, `field`, `node`, `artifact`. The coordinator pre-registers every static name in canonical plan order before execution; later names are appended in first-seen order; allocation is allowed only in the coordinator thread (a worker thread or an isolated child gets `kind-0` plus flag `alias_unknown`); each kind caps at 4096 (`kind-0` plus `alias_overflow`). The isolated child receives the frozen table in the existing payload (`src/decoy_engine/execution/_isolated_run.py:368`) and never allocates. A retry of the same job on the same input reproduces the ordinals (T9).
- Legend: `scope.legend()` is `tuple[(kind, ordinal, name)]`, an owner result. Platform stores it in a new `job_alias_legend` table (`job_id`, `kind`, `ordinal`, `name`), written in the same transaction as the terminal `Job.log` record and read only by `GET /api/jobs/{id}/alias-legend` behind `require_job_owner_or_404`. A crash before terminal leaves no legend; the UI shows raw aliases with flag `legend_unavailable`. F32 bundles never include it.
- Runtime-facts join: `StructuredEvents.step` (`src/decoy_engine/context.py:46-72`) keeps real `name` and `node_id` (owner results), drops `error_msg` (pre-GA hard delete; `JobLogger.step` never wrote it, `api/persistence/logger.py:179-205`), and gains `node_alias: SafeAlias | None`. Platform stores the ordinal in `job_node_runs.node_alias` and `_split_log_by_node` (`api/jobs/routes_runtime_facts.py:45-66`) groups re-ingested records by alias; the route shape is unchanged.

### 3.4 Stored `Job.log`

`Job.log` (`api/models.py:424`) keeps its `Text` column and holds only v1 lines written by `api/jobs/job_log.py:append(db, job, event)`; the two resets (`api/jobs/iteration.py:499`, `api/jobs/runner.py:499`) become `job_log.clear(db, job)`, the only other writer. One reader `job_log.read(job)` parses each line with `from_line(expected_run=job.run_uuid)` and renders it with `render_text`; each maximal run of lines that fail is one fixed `safe_output.legacy_withheld` line with a count. A job whose `run_uuid` fails validation at read (a non-v4 or malformed stored value) renders its whole log as withheld with `safe_output.run_identity_invalid`. The four readers call it: the SSE stream (`api/jobs/routes_streaming.py:92-101`, which keeps `data: <text>` so `web/src/hooks/useLiveJob.ts:215` is unchanged), the job `log` field (`api/jobs/_shared.py:203-211`, `:229`), node runs (`routes_runtime_facts.py:96`) and the storm failure summary (`api/storm/router.py:1671-1680`). Every Platform writer in 5.6 moves to `append`, each free-text message becoming a registered reason in a new `platform` namespace that the engine declares (owner decision: Platform has no registry). Legacy rows are never rewritten or purged.

### 3.5 Surfaces that keep their own boundary

- Platform server logs are the OBS-3 JSON record (`api/logging/formatters.py:78-97`): `request_id`, `user_id`, `route`, scrubbed `msg`, contract-checked extras (`api/logging/extras.py:25-138`), exception summary without messages (`api/logging/exc_safe.py:76`). A server-log record is a request-scoped or process-scoped operational record, class (a), correlated by `request_id`; it carries no `run` unless job code is executing, in which case F31 adds `run` (canonical UUIDv4) as an approved extra. Server logs are parsed by one parser (`api/server_logs/line_parser.py:90-117`) for the tail, aggregation, request lookup, grouping and download readers and are never replayed as events; P4 does not apply to them. The primary guarantee is the existing sentry `tests/sentry/test_obs3_log_interpolation.py`.
- Child processes are already contained at the parent: the engine reads child stderr only into a classifier and a marker scan and stores none of its text (`_isolated_run.py:434`, `:504-519`; `_isolated_common.py:277`); the Platform generation parent writes child stdio to files it never surfaces (`api/generation/generation_parent.py:193-199`); the probe child returns a closed token (`api/cloud/_pg_probe_child.py:79-84`). F31 promises only "child stdio never reaches an (a) or (b) surface" and proves it with a sentry (P5). No child bootstrap is built.
- CLI entry: the console script changes from `decoy.__main__:app` (`decoy/pyproject.toml:178`) to `decoy.__main__:main`, which mints the run UUID, enters the scope, calls `app(standalone_mode=False)`, maps exact Click control-flow types (`UsageError`, `BadParameter`, `NoSuchOption`, `MissingParameter`, `Abort`, `Exit`, `ClickException`) to fixed usage prose plus the option name and `EXIT_USAGE` (`decoy/src/decoy/cli/exit_codes.py:44`), and any other exception to registry prose plus the `SafeErrorType` label and `EXIT_RUNTIME` (`:59`). Option values are never echoed. The five `print_exception()` sites (`decoy/src/decoy/cli/scan.py:794`, `run.py:1127`, `subset.py:278`, `report.py:2183`, `demo.py:555`) become `safe_frames(exc)` (type-name chain plus `file:line:function`, the `exc_safe` shape) under `--verbose`. F43's silencer (`decoy/src/decoy/ci/engine_logging.py:25`) stays.

## 4. Defect-class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | Three-class table (5.5), `SafeEvent` with no string field, two serializers, exact-type exception table, generated export classification (5.4), sentries R1-R5 | T0 census coverage, T1-T8 marker corpus with a positive control first, T6 killing test | Re-add `type(exc).__name__` in `postgres_snapshot`, add a `str` field to `SafeEvent`, or route a (c) field into a (b) projection: a sentry or a marker check fails |
| 2 | Cleanup missing on an exit path | `SafeOutputScope` is a context manager that unbinds the ContextVar and freezes the alias table on every exit; the legend write shares the terminal transaction | Exit-path matrix (6.3) | Drop the `finally` unbind: the nested-run test sees the previous run's aliases |
| 3 | State machine and concurrency | Alias table states: open (coordinator only), frozen (after the first child handoff or scope exit); allocation outside the coordinator thread is `kind-0` | Barrier-forced test: a worker thread and the coordinator allocate concurrently; a child allocates after handoff | Remove the coordinator-thread check or the freeze: the thread gets a fresh ordinal and T9 fails |
| 4 | Route parity and silent fallback | One operational sink per route (5.2) and one `Job.log` reader for every stored-line route; a withheld line is counted, never dropped silently | Corpus over every route in 5.2 with a registered event before absence; T3 over all four readers | Replay a stored line in one reader without `from_line`: T3 fails on that reader |
| 5 | Referential integrity | F31 changes no FK semantics; FK reports are projected: (b) real names, (a) `SafeEdge` aliases | FK-shape output matrix (6.4) | Render `FkValidityReport.relationship` on an (a) surface: the matrix fails |
| 6 | Untrusted producer | Exact types at construction and serialization; bounded `from_line`; reasons rebuilt from identities; child stdio never read into output | Field walk with ordinary canaries over every exported type; hostile-line corpus for `from_line` | Accept an unknown key or a subclass in `from_line`: the corpus fails |
| 7 | TOCTOU / check-then-use on a path | N/A: F31 opens no path after a check; the child payload is the existing payload file and data-plane writers are exact functions in the manifest (P3) | Manifest test that each writer is a real function at the recorded `file:line` | Rename a writer without updating the manifest: the test fails |
| 8 | Weak tests and overclaimed evidence | Shrink-only baselines (6.5); positive control in every recorder; value-domain snapshots | Recorder self-test with a disconnected sink; baseline-growth test | Add a baseline entry or disconnect a recorder: T10 fails |
| 9 | Behavior-preserving refactor drift | Characterization tests first for node-runs grouping, evidence `nodes`, progress keys and values, `scan fields --json`, phase `error_type` | Snapshots taken before P1, E2 and E5 | Change the alias join to a name join or a label value: the snapshot fails |
| 10 | Frozen contract drift | Section 7 with value semantics; `decoy-safe-output/v1` additive only | Snapshot tests that assert value domains per field (T5) | Emit `field-3` where (b) requires a name, or a name where (a) requires an alias: T5 fails |

## 5. Enumerations from the code

### 5.1 Exit paths

- Transactional sink: commit success, commit failure, abort, staging cleanup (`src/decoy_engine/execution/_transactional_sink.py:88-92`, `:324-380`); chunked publish `__exit__` on exception and on normal exit (`src/decoy_engine/execution/_chunked_output_sink.py:166-178`).
- Isolated execution: in-process fallback, spawn and classify (timeout kill, abnormal exit, self-reported failure), commit (`src/decoy_engine/execution/_isolated_run.py:258`, `:409-519`, `:590`).
- Progress: no sink, sink exception counted, terminal `ok` and `error` with open phases closed (`src/decoy_engine/progress.py:186-197`, `:429-485`).
- New in F31: scope exit (normal, exception, nested), alias freeze, serializer failure, parser rejection, legend write failure (terminal line still written; flag `legend_unavailable`).

### 5.2 Routes

- Relationship jobs: `out_of_core`, `sequential`, `mixed`, `full_frame`, or reject (`src/decoy_engine/execution/_pipeline_routing.py:22-61`).
- Independent multi-table split, one table at a time (`src/decoy_engine/execution/_pipeline_multi_table.py:1-7`); in-process or isolated for every route (`_isolated_run.py:141`, `:258`).
- Stored-line routes (re-ingest): SSE, job `log`, node runs, storm summary (3.4); F32 reader (section 9).
- Server-log readers: tail SSE (`api/server_logs/tail_stream.py:260`, `:429`), aggregation (`aggregate.py:263`), request lookup (`request_lookup.py:109`), grouping, download (`router.py:399`); all through `line_parser.parse_line` (`line_parser.py:117`).

### 5.3 FK shapes

- Single and composite edges by column-tuple arity, `RelationshipEdge` (`src/decoy_engine/relationships/_graph.py:72-92`); graph build and orphan-policy completeness (`:134`, `:316`).
- Multi-parent children (`src/decoy_engine/execution/out_of_core/_multi_parent.py:49-106`); children in later streamed batches (`src/decoy_engine/execution/out_of_core/_stream_join.py:84`). F31 projects these as `SafeEdge` (aliases, relation enum, arity) on (a) and as real names on (b); values and orphan decisions are unchanged.

### 5.4 Fields

The classification is generated, not hand-listed: `tools/safe_output_inventory.py` walks `src/decoy_engine/__init__.py` (`__all__`, 169 names at `:310-480`) and resolves every name to its definition. At `ab29ef0c`: 46 dataclasses, 29 classes, 27 exception classes, 54 functions, 3 enums, 10 constants, 0 unresolved. The committed `tests/sentry/safe_output_exports.json` records, for every dataclass field typed `str`, `Any`, `object` or a container of them, one class: `id` (fixed engine identifier), `b` (owner name or registry text) or `c` (data). Test R3 regenerates the file and fails on any difference, so a new export or field must be classified in the same change. Decisions the file must carry:

- (c), absent from every (a) and (b) projection: `QualitySummary.sampled_values` (`src/decoy_engine/validation/post/_types.py:70`); `FieldStats.min_value`, `max_value`, `mean_value`, `mode_value`, `date_min`, `date_max`, `sample_invalid` (`src/decoy_engine/profiling/types.py:64-92`); `SentinelFlag.value` (`:54`); `DetectorMatch.sample_misses` (`src/decoy_engine/detection/types.py:50`); `DiscoveryResult.rows` (`src/decoy_engine/data_discovery.py:88`); `ExecutionResult.outputs` (`src/decoy_engine/execution/_adapter.py:43`); `UnmaskResult.outputs` (`src/decoy_engine/unmask.py:90`); `ValuePool.values` (`src/decoy_engine/generation/pool/_value_pool.py:35`). `scan fields --json` already excludes the profile values (`decoy/src/decoy/cli/scan.py:518-522`); the raw profile `--output` (`:798`) is (c).
- (b) free-text fields that become registry text or a closed label: `CheckResult.detail` (`src/decoy_engine/sdk.py:123`), `UnmaskColumnReport.detail` (`unmask.py:86`), `QualityWarning.detail` (`src/decoy_engine/generation/pool/_events.py:39`, closed keys with numeric or name values), `ExecutionEvent.detail` (`src/decoy_engine/execution/_events.py:19`), `CapacityEstimate.message` (`src/decoy_engine/execution/out_of_core/_capacity_eval.py:153`), `PlanCheckError.message` and `ValidationMessage.message`/`hint` (`src/decoy_engine/plan/validate/_consolidator.py:47`, `src/decoy_engine/validation_result.py:48`), `JobEstimate.notes` (`src/decoy_engine/execution/_job_estimate.py:59`), `IsolatedRunResult.error` (`_isolated_common.py:369`, label plus registered code).
- (b) names kept: `FkValidityReport.relationship` (`post/_types.py:39`), `RelationshipEdge` tables and columns, `FileMeta.path` and `WriteResult.path` (owner paths), progress `table`/`column`.
- Public exception classes (27): their text is class (b), returned to the caller who wrote the input; it reaches no (a) surface because R1, R2 and the log sentry ban every path that would copy `str(exc)` into a log, event or record.

### 5.5 Output surfaces (three-class classification)

Emitters are enumerated by the E1 AST census (`tests/sentry/safe_output_surfaces.json`): at `ab29ef0c` the engine has 67 logger calls in 32 files (54 with arguments), 4 `warnings.warn`, 1 `sys.stderr.write`, 0 `print`, 5 `json.dumps(default=...)` and 1080 `raise` sites with f-strings (class (b) by 5.4, policed by R1 and the log sentry). Test T0 fails if a census category has no row here.

| # | Surface | Source | Class | Rule |
|---|---|---|---|---|
| S1 | Engine progress dicts v0 and v1 | `src/decoy_engine/_progress_events.py:581-599` | b | keys and value domains unchanged; `error_class` from `SafeErrorType` |
| S2 | Engine stdlib logs, file `logs/decoy_engine.log` | `src/decoy_engine/internal/logging.py:109`, `:136`, `:139` (`ProgressLogger` f-strings) | a | records are v1 lines via `LoggingSink`; R2 bans value arguments |
| S3 | `IsolatedRunResult.error`, `PostgresSnapshotError` text, engine exception text | `_isolated_common.py:238-290`, `postgres_snapshot.py:115-131` | b | label plus registered code; never copied to (a) |
| S4 | `StructuredEventsSink` narrative, `Logger` methods | `src/decoy_engine/progress.py:84-128` (`:119` names a table and column) | a | replaced by `operational_view` events |
| S5 | `StructuredEvents.step/lineage/fidelity/quarantine` | `src/decoy_engine/context.py:46-83` | b | real names; `error_msg` deleted; `node_alias` added |
| S6 | In-process results and reports (5.4) | `src/decoy_engine/execution/_adapter.py:43` | b | (c) fields never projected |
| S7 | Masked, subset, vault, quarantine, raw profile output | `decoy/src/decoy/cli/scan.py:798`, writer manifest | c | exact writer manifest |
| S8 | Isolated envelope file | `src/decoy_engine/execution/_isolated_worker.py:57-70` | b content | typed envelope; `default=str` removed |
| S9 | STORM `phase-result/v1`, `phase-event/v1` | `src/decoy_engine/phase.py:487`, `:574` | b | `error_type` from `safe_error_type`; payload is caller evidence |
| S10 | CLI stderr, usage and parse errors, `--verbose` | `decoy/src/decoy/__main__.py:164`, five `print_exception` sites (3.5) | a | entry wrapper; `safe_frames` |
| S11 | CLI `scan --json`, `scan fields --json`, evidence files | `decoy/src/decoy/cli/scan.py:150`, `:936`, `evidence.py:118` | b | unchanged; no values |
| S12 | CLI `decoy ci` JSON, SARIF, Markdown | `decoy/src/decoy/ci/contracts.py:374` | b | unchanged (F43) |
| S13 | Platform API error bodies (`HTTPException.detail`) | 89 f-string details in 31 files at `00e55c27` (AST count) | b | rule `detail-closed` (P4) |
| S14 | Platform API success bodies, fidelity, evidence views | `api/evidence/assembly.py:1884` | b | (c) fields never projected |
| S15 | `Job.log` (stored) | `api/models.py:424`; writers in 5.6 | a | v1 lines via `job_log.append` only |
| S16 | Job SSE, job `log` field, node-run `logs`, storm summary | `api/jobs/routes_streaming.py:92-101`, `_shared.py:203-211`, `routes_runtime_facts.py:96`, `storm/router.py:1671` | a text inside a b response | one reader; re-ingest then render |
| S17 | `JobStep`, `JobNodeRun`, lineage, fidelity tables | `api/models.py:1308-1323`, `:1454` | b | real `node_id`; `node_alias` added |
| S18 | Server log files and the five server-log readers | `api/logging/formatters.py:78-97`, 5.2 | a | OBS-3 record; `run` extra when job-scoped; no re-ingest |
| S19 | Audit rows (`detail`) | `api/audit/logger.py:83-92` | b | `detail` is registry text or an identifier (P5 rule) |
| S20 | `/api/metrics` Prometheus exposition | `api/observability/metrics.py:192`, `:351`, `:391`, `:433` | a | closed labels from `metric_labels` and `_pick` |
| S21 | Platform child stdio | `generation_parent.py:193-199`, `pg_probe.py:85`, `_pg_probe_child.py:83` | a (never surfaced) | child-stdio sentry |
| S22 | Alias legend route | new `GET /api/jobs/{id}/alias-legend` | b | owner only |
| S23 | OpenTelemetry (F30) and support bundles (F32), not yet built | none at the baselines | a | section 9 promises |

Sentry and third-party trackers: no `sentry_sdk` exists in Platform at `00e55c27`; the revision 1 row is deleted, not kept.

### 5.6 Platform `Job.log` writers (every assignment site at `00e55c27`)

30 direct assignments in 16 files: `api/quality/runner_hook.py:628`; `api/jobs/iteration.py:492`, `:499`, `:516`, `:523`, `:617`, `:638`; `api/jobs/job_terminal_cas.py:150`; `api/jobs/reaper.py:233`; `api/jobs/queue_worker.py:385`, `:595`; `api/jobs/runner.py:62` (the `_append_log` helper, called at `:123`, `:139`, `:171`, `:508`, `:545`, `:598`), `:499`, `:646`, `:674`; `api/jobs/job_principal.py:88`; `api/jobs/scheduler_claim_loop.py:94`; `api/jobs/iteration_dispatch.py:153`, `:417`; `api/jobs/binding_resolve.py:429`; `api/worker_main.py:125`; `api/storm/router.py:580`, `:680`, `:981`; `api/storm/post_mask_hook.py:372`, `:373`; `api/synth/runner_hook.py:333`; `api/main.py:1189`, `:1196`; `api/persistence/logger.py:448` (`JobLogger._append_narrative`, reached by `debug`/`info`/`warning`/`error`/`step` at `:152-205`, constructed at `api/jobs/route_narrative.py:223`, `api/jobs/v2_orchestrator.py:351`, `api/storm/router.py:989` and handed to the engine at `:1131`). P2 moves all of them to `job_log.append`; sentry R5 fails on any `.log` assignment outside `api/jobs/job_log.py`, and the census is regenerated by AST, so the list above is the review baseline, not the mechanism.

## 6. Acceptance tests, red first

### 6.1 Seeded corpus

Distinct high-entropy markers plus grammar-clean canaries (`canary_table_alpha`, a UUIDv4 in a cell, small integers in layout fields) for every value class in the threat model, in ordinary shapes. Every recorder first asserts a registered control event arrived, then asserts marker absence over bytes, JSON keys and values, created filenames, log records before formatting, and persisted database text. On (b) surfaces the recorder asserts the clean schema-name canary IS present and every cell-value marker is absent.

### 6.2 The ten ways a gate reviewer breaks it (each RED first)

| # | Break | RED-first test | Slice |
|---|---|---|---|
| T1 | `--help` or a parse error echoes a seeded option value | `tests/e2e/test_entry_boundary.py`: console-script subprocess with markers in option values, unknown options, `BadParameter` | C1 |
| T2 | A command's crash shows exception text or a dynamic class name | one subprocess cell per registered command (generated from `app` at test time) raising a marker-bearing engine exception; stderr shows registry prose and a `SafeErrorType` label; `--verbose` shows safe frames only | C1 |
| T3 | A reader replays a marker from `Job.log` | `tests/test_job_log_reader.py`: seed a legacy free-text row, a v1 line with a wrong `run`, a line over 4096 bytes and a line with an unknown key; each of the four readers shows only `legacy_withheld`; positive control: a valid line renders | P1 |
| T4 | A `/metrics` label carries a value | markers into every metrics helper argument; scrape `/api/metrics`; `metrics.py:391` path raises a marker-named subclass | P5 |
| T5 | A key snapshot passes while value meaning changes | value-domain snapshots: (b) progress, fidelity, `scan fields --json`, node runs assert real canary names; (a) `Job.log` and engine log assert `^(table|field|node|artifact)-\d+$` | E4, C2, P1 |
| T6 | Exact-type lookup is weakened to `isinstance` or an MRO walk | `safe_error_type(type("Sub", (ValueError,), {})())` is `SafeErrorType.EXCEPTION` and not `VALUE_ERROR`; `safe_error_type(ValueError())` is `VALUE_ERROR` (positive control); the six `postgres_snapshot` sites raise `type("SECRET_x", (OSError,), {})` and the text shows `Exception`; analyzer rule R1 fixtures for `type(e).__name__`, `__qualname__`, `e.__class__.__name__`, `getattr(type(e), "__name__")` | E0, E2 |
| T7 | A (c) field reaches a (b) projection | field walk seeds markers in every (c) field of 5.4; scan the isolated envelope, Platform manifest JSON, evidence bundle and API bodies | E5, P5 |
| T8 | `json.dumps(default=...)` reaches an output surface | rule R4: only the four digest functions (`generation/_reproducibility.py:204`, `pool/_builder.py:42`, `plan/_generation.py:260`, `profiling/model_pack/trainer.py:593`) are allowed; `_isolated_worker.py:70` is red until E5 | E1, E5 |
| T9 | Child and parent ordinals disagree, or a re-rendered row relabels | isolated run with dynamic discovery: aliases in child and parent events and in a later re-render are identical; a retry of the same `run_uuid` reproduces them; a worker-thread allocation is `kind-0` | E3, P1 |
| T10 | A baseline grows or a stale entry stays | `tests/sentry/test_safe_output_baseline.py`: fails on a violation not in the baseline, on a stale entry, and on any count that grows | E1 |

T0: the census categories of 5.5 and the writer manifest are enumerated at test time; a category or writer with no row fails. T11 (run identity): a migration test backfills three pre-existing rows with distinct v4 UUIDs; a raw-SQL non-v4 value, a 35-character value and a duplicate are rejected by the constraint or, when read, render the log as `run_identity_invalid`; a request body carrying `run_uuid` is rejected; no explicit `run_uuid=` argument or assignment exists outside `mint_run_uuid` (AST). T12 (legend): the legend is readable only by the owner (non-owner 404), matches the aliases in the stored lines exactly, and is absent after a simulated crash before terminal. T13 (frames): the `SafeBoundaryError` test in 3.2; a plain `raise ... from None` inside the handler is the positive control that fails.

### 6.3 Exit-path matrix

One parametrized test over every row of 5.1: the scope is unbound and the alias table frozen after normal exit, exception, nested scope and double exit; the legend write failure leaves the terminal line; no sink receives an event after exit.

### 6.4 FK-shape output matrix

For each shape in 5.3 and each route in 5.2: (a) surfaces show `SafeEdge` aliases and no names; (b) `FkValidityReport` shows real names; relational results are byte-identical to the baseline.

### 6.5 Sentry ratchet

E1 lands `tests/sentry/test_safe_output_boundary.py` with a committed baseline `tests/sentry/safe_output_baseline.json` keyed by (file, enclosing qualname, rule). Rules: R1 dynamic exception-name reads (56 sites in 30 files at `ab29ef0c`, `git grep -nE "type\((exc|e|err|error|ex|cause)\)\.__(qual)?name__|__class__\.__name__"`; the only allowed reader is `safe_output/_exception.py`, plus `scratch/_os_error.py:classify_os_error` for `.errno` once 4c lands); R2 logger arguments must be ints, floats, enums or aliases (54 sites with arguments); R3 export classification (5.4); R4 `json.dumps(default=...)` (5 sites, 4 allowed); R5 (Platform) `.log` assignment and read sites. The baseline mirrors the module-size census (`tests/sentry/test_module_size.py:66`): counts only shrink, stale entries fail, and E5 requires the engine baseline empty.

## 7. Frozen contracts touched

| Contract | Change |
|---|---|
| `decoy-engine` public API (0.7.0, `pyproject.toml:10`) | Additive `decoy_engine.safe_output` (`SafeEvent`, `SafeErrorType`, `safe_error_type`, `safe_event_from_exception`, `contain`, `SafeOutputScope`, `SafeAlias`, `SafeEdge`, `to_dict`, `to_line`, `from_line`, `render_text`, `metric_labels`, `families`). Pre-GA breaks (`src/decoy_engine/release.py:45-48`): `StructuredEvents.step` drops `error_msg` and gains `node_alias`; `_progress_events.safe_error_class` deleted. Version: the next engine minor released after E5, taken from the release lineage; consumers pin exactly that |
| Reason registry 1.0.0 (`src/decoy_engine/reasons/_registry.py:24`) | Additive `platform` and `safe_output` namespaces; minor bump to 1.1.0 per `tests/unit/test_reason_registry.py:151` |
| Progress v0/v1 (`tests/unit/progress/test_versioned_contract.py:37-78`) | Keys and value domains unchanged; `error_class` labels are the same names, now by exact type; snapshot asserts values |
| `phase-result/v1`, `phase-event/v1` | Keys unchanged; the `error_type` value domain widens additively from the 15 builtins of `_ERROR_KINDS` to the closed `SafeErrorType` table (STORM's snapshot of the 15 is re-recorded as a subset check) |
| `fidelity-report/v1`, `integrity-report/v1`, `decoy-ci/v1`, `scan --json` | Unchanged (class b); value snapshots assert real names |
| Platform OpenAPI and web client | Unchanged shapes. Content: `log` text and SSE `data:` lines become rendered v1 records. Additive: `jobs.run_uuid`, `job_node_runs.node_alias`, table `job_alias_legend`, route `/jobs/{id}/alias-legend`, server-log extra `run` |
| `decoy-safe-output/v1` (new) | Additive only after release |

## 8. Reuse inventory

| Existing | Source | Use |
|---|---|---|
| F29 registry, records, identity form | `src/decoy_engine/reasons/_registry.py:24-38`, `_record.py:115-260`, `_identity.py:19-57` | Prose authority, `ReasonRef`, `COUNT_KEYS`, `approve_name` on (b) |
| Reason adapter | `src/decoy_engine/reason_adapter.py:146`, `:352-368` | Reason selection in `safe_event_from_exception` |
| STORM exact-type table and containment | `src/decoy_engine/phase.py:160-181`, `:555-595` | Lifted into `safe_output._exception`; phase delegates |
| Progress projections and channel | `src/decoy_engine/_progress_events.py:513-599`, `progress.py:162-197` | Kept; `operational_view` added beside them |
| Log interpolation sentry | `tests/sentry/test_log_interpolation.py:23-71` | Risky-name detector reused by R2 |
| Module-size census ratchet | `tests/sentry/test_module_size.py:66` | Baseline pattern for 6.5 |
| Hostile reason corpus | `tests/security/test_reason_boundary.py:22` | Lifted into 6.1 |
| F43 `SafeFinding` choke point | `decoy/src/decoy/ci/contracts.py:64`, `:128-135`, `:228-275` | Pattern for `SafeEvent` construction and fixed registries; `decoy ci` unchanged |
| Platform OBS-3 formatter, extras, exc summary, sentry | `api/logging/formatters.py:78`, `extras.py:117-138`, `exc_safe.py:76`, `tests/sentry/test_obs3_log_interpolation.py` | Server-log boundary kept; `run` extra and `safe_frames` shape |
| Platform closed metric pickers | `api/observability/metrics.py:192` | Metrics label domain |
| Alembic backfill pattern | `api/migrations/versions/obs3_request_id.py` | `run_uuid` migration |

New machinery (the alias table, the v1 record and parser, the sentries) exists because no current carrier aliases names per run or validates a stored line.

## 9. Consumer promises

- F25 4c (owner): `scratch._os_error.classify_os_error` is admitted as a reviewed exception boundary (R1 allows `.errno` there only). The public `QuotaReport.errno` field is deleted by 4c (pre-GA); F31's export classification lists `QuotaReport` without it. 4c-5a may merge before engine slice E3; F31 rebases onto it and E1 re-records the baseline. At `ab29ef0c` no `scratch` package exists; the symbols live on `plan/f25-4c-consumer-capabilities`.
- F34 (owner): the CLI reports COMPLETE from a verified local manifest labeled `binding_source=cli-local-1`; Platform ships first; exit code 6; the engine pin is taken from the release lineage. F31 promises `SafeOutputScope`, `safe_error_type`, a `families/canary.py` module built in the engine by F34, and additive `COUNT_KEYS`. Not promised, and F34's assumptions table must be reconciled: `SerializedSafePayload`, sealed drivers, an `ordinary_output` opt-in (every registered reason is ordinary-safe by construction), child fd bootstrap (F34 owns its `DEVNULL` stdio), an atomic ordinary-file sink (F34 `:88` does not need one).
- F36 (owner): Linux-only relationship and reconciliation checks with `not_run` elsewhere; no persisted report without a job attempt; exact checksums above 10M rows; both reason codes engine-owned in `decoy-reasons/v1`. F31 keeps X1 (`families/invariants.py`), X2 (closed count names), X4 as the generated data-plane writer manifest, X5, X6 as `SafeOutputScope` (no process install), X7; X3 is dropped.
- STORM: `safe_error_type` and `SafeErrorType` exported; `phase.error_result` delegates; phase schemas frozen; the containment sentry unchanged.
- F30: the run UUID is the span correlation id; aliases may be span attributes; names may not; Prometheus labels carry closed enums only.
- F32: reads stored v1 lines by `run_uuid` through `from_line(expected_run)`, carries aliases verbatim, never includes the legend; `SafeEdge` and `ver` give topology and versions.

## 10. Pre-mortem

| Likely gate finding | Class | Prevention |
|---|---|---|
| An emitter category or writer has no row | 1, 4 | T0 over the generated census and writer manifest |
| A (c) field reaches evidence or an API body | 1, 10 | R3 classification plus T7 |
| A reader replays a stored line raw | 4, 6 | one reader, R5, T3 |
| A customer UUID becomes a run id | 6 | schema exclusion, `mint_run_uuid` sentry, constraint, T11 |
| The alias order differs between child and parent, or between retries | 3 | coordinator-only allocation, frozen handoff, T9 |
| Progress names are aliased by mistake | 10 | T5 asserts real names on (b) |
| A slice exceeds the size bound | 8 | the bound is checked in the slice's own evidence; split before starting |

## 11. Slices

Size bound (objective): a slice touches at most 10 production files and changes at most 800 production lines (insertions plus deletions from `git diff --stat`, tests and generated JSON excluded); a slice that would exceed it is split before it starts. Each slice: RED tests first with output recorded, smallest change, self-check, review, at most two remediation rounds. New F31 production modules stay below 450 physical lines; the repository sentry stays 600/700.

| Slice | Repo | Scope | Bound | Depends on |
|---|---|---|---|---|
| E0 | engine | `_event.py`, `_exception.py` (table lifted from `phase`), `_wire.py`; field walk, T6, parser corpus, record snapshot | 5 files | none |
| E1 | engine | `tools/safe_output_inventory.py`, `tests/sentry/test_safe_output_boundary.py` with R1-R4, generated `safe_output_exports.json` and `safe_output_surfaces.json`, baseline, T0, T10 | tests and tools only | E0 |
| E2 | engine | `safe_error_type` consumers: `phase.error_result`, `_progress_events` (`safe_error_class` deleted), `progress.run_scope`, `_isolated_common`, the six `postgres_snapshot` sites; characterization of `error_class` and `error_type` first | 6 files | E1 |
| E2b | engine | Drain the remaining R1 sites (about 40 in 25 files) to zero, mechanically, in parts of at most 10 files, each part gated alone | 10 files per part | E2 |
| E3 | engine | `_scope.py`, `_alias.py`, child handoff in `_isolated_run._write_payload`, T9, exit matrix | 5 files | E2 |
| E4 | engine | `_sink.py` (`SafeEventSink`, `LoggingSink`), `operational_view`, `StructuredEventsSink` narrative removed, `internal/logging.py` free text to events, `StructuredEvents.step` change, `platform` and `safe_output` reason namespaces (1.1.0), R2 drained | 9 files | E3 |
| E5 | engine | Typed isolated envelope (`sampled_values` dropped, `default=str` removed), R3 (c) projections, R4 to four, T7 engine half, baseline empty, release | 8 files | E4 |
| C1 | CLI | Console script to `main`, usage mapping, run UUID, T1, T2 | 3 files | engine release |
| C2 | CLI | Five `print_exception` sites to `safe_frames`, CLI marker corpus, T5 (CLI) | 6 files | C1 |
| P0 | Platform | Migration `f31_run_uuid` (add nullable, backfill per row, constrain), `mint_run_uuid`, `@validates`, schema exclusion, T11, engine pin | 6 files | engine release |
| P1 | Platform | `api/jobs/job_log.py` reader, the four readers, `job_node_runs.node_alias` migration and join, characterization first, T3, T5 (Platform) | 8 files | P0 |
| P2 | Platform | Every writer in 5.6 to `job_log.append`, `JobLogger` narrative methods removed, R5 | 16 files, split in two halves by the bound | P1 |
| P3 | Platform | `job_alias_legend` migration, route, write at terminal, T12 | 5 files | P2 |
| P4 | Platform | Rule `detail-closed` over `HTTPException.detail` and audit `detail` with a committed baseline (89 sites) that only shrinks; no response wrapper | tests plus baseline | P1 |
| P5 | Platform | `metrics.py:391`, `metric_labels`, evidence (c) projections, child-stdio sentry, server-log `run` extra, T4, T7 Platform half | 7 files | P2 |
| X | all | Cross-repo corpus on the exact released engine and pinned consumer commits | tests only | C2, P5 |

No engine slice edits another repository. STORM CLI and Platform rebase onto F31 after C1 and P2; F25 4c-5a may land before E3. Self-check per slice: the slice's named tests, then `scripts/dev/gates.sh --baseline` where it exists (engine `origin/main` after #347, CLI `d28fae2`, Platform `00e55c27`), run from `uv.lock` (`uv sync --locked`); the STORM tip `ab29ef0c` predates #347, so engine slices state the exact commands until the merge.

## 12. Worker evidence rules

- Name the class of every finding and fix the class structure, not only the instance.
- Write the generated tests first (field walk, exit-path matrix, interleavings, marker corpus) and record them red.
- Attack your own diff: mutate each new mechanism once and show the test goes red.
- Run baselines with `uv.lock` in the same environment as the branch; state what you did not run.
- Never widen a threshold, skip a test, grow a baseline, or reduce a budget to pass.
- If a defect of a class already in the catalog recurs, stop and report it; do not patch a third time.
