Status: plan only, revision 1 after the Opus hostile review (NO-GO). No implementation or release approval is granted by this document.

Summary: Every output surface is classified as operational, owner result, or data plane. Operational output is value-free by construction through one engine-owned typed event, one sealed serializer and one shared analyzer; owner results keep real schema names but never cell values; the data plane is untouched.

Baselines: engine `fdd08a8` (equal to `origin/main` on 2026-10-30); CLI `decoy` `origin/main` `730d3de` (contains F43, merged as #69 at `b2209e1`); Platform `origin/main` `ea68d8e`. Planning branch: `plan/f31-safe-output`. Platform citations below were re-read at `ea68d8e`.

Rules consulted: `DEFECT-CATALOG.md` (decoy-platform `handoffs/queue`), the engine, CLI and Platform `CLAUDE.md`.

# F31: Shared safe-output boundary, PLAN (revision 1)

Risk: R2 (security boundary, public API addition, wire-contract change, three-repository migration). The owner approves. Owner decisions B1 (three-way classification) and B2 (run identity) are binding inputs to this revision and are not reopened here.

## 1. Guarantee (invariants as properties)

**Output classes.** Every output surface is exactly one of three classes. The table in section 5.5 assigns every surface; a surface with no row is a build failure (test T0).

| Class | Shows | Never shows | Who reads it |
|---|---|---|---|
| (a) operational | registry prose, closed enums, bounded numbers, positions, opaque run ID, aliases `table-N` / `field-N` / `node-N` / `artifact-N` | any schema name, path, DSN, credential, exception text or class name, cell value, free prose | logs, progress, diagnostics, error text, API error bodies, metrics, trackers, support bundles |
| (b) owner result | everything (a) may show, plus real schema names (table, column, node, repository-relative config path) as a typed `SchemaName` | any source or masked CELL VALUE, sample, path outside the owner's project, credential, exception text or class name, free prose | the authenticated owner (Decoy is self-hosted single-org; the owner is the data owner) |
| (c) data plane | the data itself | n/a: written only by the exact writer manifest | the masked dataset, vault, quarantine rows, explicit raw profile file |

**Invariant P1 (operational).** For every operational surface S and every input value v of the classes below, the bytes S emits contain no derivative of v except through a provenance-backed `SafeEvent` built by a named engine factory. Value classes: cell contents, samples, raw rows and buffers; schema names; paths; URLs, DSNs, credentials, secret references; exception `str`, `repr`, `args`, attributes, class names, chains, tracebacks and locals; free text from extensions, drivers, warnings or loggers.

**Invariant P2 (owner result).** For every owner-result surface, no cell value (source or masked) and no exception text or dynamic class name is reachable; schema names appear only as `SchemaName` (exact type, NFC, length at most 256, no control characters), which only owner-result drivers accept and which no operational driver can render.

**Invariant P3 (data plane).** A data-plane writer is an exact function in the writer manifest; no other function writes data bytes, and no data-plane object is accepted by an (a) or (b) driver.

**Invariant P4 (re-ingest).** An event that crosses a process or a database is re-validated on read against the closed v1 registry and the run ID of the record that owns it; a line that fails validation is never replayed, only counted.

**Invariant P5 (exception types).** Exception type labels come only from `_ALLOWED_EXCEPTION_TYPES.get(type(exc), SafeErrorType.EXCEPTION)`. No production code reads `__name__` or `__qualname__` of an exception type or `exc.__class__`.

Residual for P2: a schema name is shown on (b) surfaces as the owner declared it; if an owner declares a header row on a file whose first row is data, those names are that owner's own values shown to that owner. Headerless inference keeps generated `col_N` names.

Non-goals: no new encryption or hashing algorithm; regex scrubbing, Sentry scrubbing, `detect-secrets` and Gitleaks stay defense in depth only; closure-held provenance stops accidental misuse, not malicious same-process code (that needs the isolated worker). The earlier `--unsafe-debug-log` channel is cut from F31: it fits none of the three classes and needs its own owner decision.

## 2. Run identity and crossing a process or database (B2)

- **Run.** One run is one job. The caller supplies the run ID. Platform mints `uuid.uuid4()` at job creation and stores it in a new non-null unique `jobs.run_uuid` column beside the integer `Job.id` (`api/models.py:329`). The engine never sees `Job.id`. The CLI mints `uuid4()` per process invocation.
- **Engine entry.** `SafeOutputScope.for_run(run_id: uuid.UUID)` accepts only an exact `uuid.UUID` of version 4 and registers it with provenance as `SafeRunId`. The Platform analyzer manifest admits only `Job.run_uuid` or a `uuid.uuid4()` call as its argument, so a customer UUID cannot be laundered into a run ID.
- **Aliases.** An alias identity is the triple (run ID, kind, ordinal). Ordinals are a pure function of canonical plan order and stable source position: the coordinator pre-registers every static name before fan-out, and dynamic discoveries are deduplicated and canonical-sorted at the coordinator barrier before any event that names them is released. The keyed BLAKE2b dedup table lives only in the coordinating process memory and dies with it. A retried attempt of the same job therefore reproduces the same ordinals; two jobs never share an alias identity because every event carries its run ID. Each kind is capped at 4096; overflow is `kind-0` plus the `alias_overflow` flag.
- **Isolated child.** The parent passes pre-assigned ordinals through the private data-plane envelope; a child never allocates.
- **Wire form `decoy-safe-output/v1`.** One event per line, UTF-8, `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`, at most 4096 bytes, nesting depth at most 3. Closed keys: `s` (schema literal), `rv` (registry version), `run` (canonical UUID), `seq` (int), `ts` (UTC millisecond ISO-8601), `sev`, `kind`, `phase`, `op`, `outcome` (closed enums), `reason` (`{"id","v"}`), `loc` (`component` enum plus integer `line`, `column`, `row`, `chunk`, `ordinal`), `err` (`SafeErrorType` label), `counts` (`[[enum,int]]`), `meas` (`[[enum,float,unit]]`), `aliases` (`["table-3"]`), `edges` (`[[alias,alias,relation_enum,arity]]`), `ver` (`[[dist_enum,[int,...]]]`), `flags` (enums).
- **Registry for cross-process validation.** The reader is the same exactly pinned engine version as the writer and validates with its compiled-in reason registry (`src/decoy_engine/reasons/_registry.py:24-44`). A line whose `rv` minor is newer than the reader, whose reason is unknown or not `ordinary_output=True`, or whose any field fails its closed check becomes `safe_output.invalid`. The re-ingest factory `reingest_line(line: bytes, *, expected_run: uuid.UUID) -> SafeEvent` takes `expected_run` from the owning database row, never from the line; a mismatch is invalid.
- **Stored `Job.log`.** `Job.log` (`api/models.py:424`) holds only v1 lines written by the Platform job-log sink driver. The SSE replay (`api/jobs/routes_streaming.py:86-101`), the job `log` field (`api/jobs/schemas.py:264`) and `JobNodeRunOut.logs` (`api/jobs/routes_runtime_facts.py:46-100`) all call one Platform reader that re-ingests each line and renders it with the existing text shape `ISO_TS  LEVEL  STEP  MSG`, where STEP is `node-N` or `job` and MSG is registry prose plus safe facts. The web client (`web/src/hooks/useLiveJob.ts:212-215`, `web/src/pages/JobDetail.tsx:229`) appends `ev.data` as text and needs no change.
- **Legacy free-text rows.** A line that does not parse as v1 is never replayed. Each maximal run of such lines renders as one fixed line from the registered reason `safe_output.legacy_withheld` with a count. No data migration rewrites old rows (pre-GA; retention purges them). For legacy jobs `JobNodeRunOut.logs` is empty.
- **Runtime-facts join.** `StructuredEvents.step` (`src/decoy_engine/context.py:64-74`) keeps its owner-result `name` and `node_id` (real node IDs, class b) and gains a `node_alias: SafeAlias` keyword. Platform stores the ordinal in a new `JobNodeRun.node_alias` column, and `_split_log_by_node` groups re-ingested events by alias and joins on it. The route shape and real `node_id` stay unchanged.
- **F30.** The OpenTelemetry correlation ID is the run UUID as a span attribute; aliases may be span attributes (operational). Prometheus labels never carry the run ID or aliases (cardinality and cross-run linkage); they carry closed enums only.
- **F32.** A support bundle built after the run reads stored v1 lines by `run_uuid`, re-ingests each with `expected_run`, and carries aliases verbatim. It never allocates aliases and never includes a name legend (matches MASTER F32 "Schema labels are not reversible mappings"). One bundle covers one run; a multi-run bundle must namespace aliases by run and is F32's decision.

## 3. Design

### 3.1 Single model and serializer

Package `src/decoy_engine/safe_output/`: `_model.py`, `_provenance.py`, `_registry.py`, `_aliases.py`, `_exception.py`, `_scope.py`, `_streams.py`, `child.py` (public child bootstrap), `_serialize.py`, `_reingest.py`, `_sink.py`, `_testing.py` (shared analyzer), `families/{progress,runtime,quality,evidence,phase}.py`. Survey citations (OWASP logging, Python `logging`/`traceback`/`contextvars`/`weakref`/`faulthandler`, FastAPI direct responses, OpenTelemetry sensitive data, Sentry `before_send`, Parse don't validate, Semgrep taint) go in each module docstring, per the engine rule.

`SafeEvent` is exact, frozen, slotted with explicit `__weakref__`. `_provenance.py` holds a closure capability and an `id(obj)` registry entry of (weak reference, canonical digest); a cleanup callback removes an entry only if its stored weak reference is the callback's own. Any instance not made by a named factory (public constructor, `object.__new__`, copy, subclass, `replace`, `object.__setattr__`, reused stale ID) fails emit-time verification. Fields are those of the wire form in section 2, typed: `ReasonRecord` with `ordinary_output=True` (F29 gains `ordinary_output: bool = False`; Slice E0 commits the exact opt-in manifest), closed enums, `SafeLocation`, `SafeErrorType`, `SafeCount`, `SafeMeasurement`, `SafeAlias(kind, ordinal, dtype: SafeDtype | None)`, `SafeEdge(parent, child, relation, arity)`, `SafeVersion(dist, release)` built only by the factory from `importlib.metadata` for a closed distribution enum, `SafeRunId`, flags. There is no string, `Any`, mapping or extension field. Owner-result projections additionally carry `SchemaName`, which has its own provenance kind and is rejected by every operational driver.

Serialization: the sink seals a `SerializedSafePayload` (opaque, no mapping protocol). Only closure-bound drivers in `_sink.py` turn a payload into bytes; each driver is registered with exactly one class (a) or (b). Unknown enum, schema, provenance or digest mismatch seals a fresh `safe_output.invalid`; a failure of that seals the constant ASCII `safe_output.failed` literal. No path calls `str`, `repr`, a logger or a JSON `default`.

Metrics projection: `SafeEvent.metric_labels(projection: MetricProjection) -> SealedLabels` yields closed-enum label values only; only a registered metrics driver (Prometheus `.labels(...)`, OpenTelemetry attributes) consumes it, inside `_sink.py`. Platform keeps its closed `_pick` pickers (`api/observability/metrics.py:192`).

Compatibility: `event_to_dict` and `event_to_legacy_dict` return sealed payloads of the legacy key shape; `Logger` narrative methods are no longer called by engine code (analyzer rule) and engine narrative goes through `SafeEventSink`; `StructuredEvents` keeps `step`, `lineage`, `fidelity`, `quarantine` as owner-result calls with `error_msg` removed, `error_class` typed `SafeErrorType`, and `quarantine.reason` a registered reason ID.

### 3.2 Exception boundary

`safe_event_from_exception(exc, reason, location)` selects a reason through `reason_adapter` (`src/decoy_engine/reason_adapter.py:38-100`), looks up `_ALLOWED_EXCEPTION_TYPES.get(type(exc), SafeErrorType.EXCEPTION)` by exact type identity, and returns without storing `exc`. Exported promise for STORM and others: `safe_error_type(exc: BaseException) -> SafeErrorType` and `SafeErrorType.label` (closed string). Two existing policies are replaced by this one table: `_progress_events.safe_error_class` (`src/decoy_engine/_progress_events.py:86-98`, an allowlist over name strings) and the `postgres_snapshot` pattern `cause_type=type(exc).__name__` (`src/decoy_engine/connectors/postgres_snapshot.py:242`, `:256`, `:327`, `:408`, `:491`, `:515`, joined into error text at `:130-131`), which is banned, not preserved. Platform `api/observability/metrics.py:391` logs `type(exc).__name__` and is migrated in P2.

Frame hygiene: convert in an outer frame after the data-owning frame unwound; never `clear_frames` the executing frame; detach `__traceback__`, sever bounded `__cause__`/`__context__` chains; propagate only a new `SafeBoundaryError` with no cause, context or traceback. No ordinary path calls `traceback.format_exc`, `print_exception`, `TracebackException`, Rich `print_exception`, `logger.exception` or passes `exc_info`.

### 3.3 Containment and process entry

States of process output ownership (closes class 3):

| From \ event | `install_process_safe_output()` | `SafeOutputScope` enter | scope exit | proxy replaced by host | `uninstall_for_tests()` lease exit |
|---|---|---|---|---|---|
| uninstalled | owned | scoped | forbidden (fixed error) | ignored | forbidden |
| owned | forbidden (idempotent check fails closed) | owned+scoped (nested) | n/a | violated | uninstalled, exact restore |
| scoped (library) | forbidden | nested (LIFO) | uninstalled, exact restore | violated | forbidden |
| owned+scoped | forbidden | nested (LIFO) | owned | violated | forbidden while scoped |
| violated | forbidden | forbidden | fixed `safe_output.failed`, then failed | failed | uninstalled |

Import and library calls install nothing. The analyzer bans importing `safe_output._testing` from production modules and reading `Job.log` outside the one Platform reader. Process-owned mode (CLI `main()`, Platform server and worker startup, isolated child) installs `ContextVar` stream proxies that discard unscoped writes and the safe `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook` and asyncio handler. Engine-owned threads and pool submissions run through a distinct `copy_context().run`. `uninstall_for_tests()` is a test-only lease (importable only from `decoy_engine.safe_output._testing`) that restores exact objects, so in-process CLI tests can enter through `main()` without poisoning pytest (closes M1).

Child bootstrap (public `decoy_engine.safe_output.child.bootstrap_child_streams(transport_fd)`): before any extension import, flush Python streams, call the manifest-pinned `fflush(NULL)` / `_flushall()`, disable `faulthandler`, set the transport non-inheritable, register an at-fork close, `os.dup2` descriptors 1 and 2 to `/dev/null`. Structured results travel only on the dedicated descriptor or a private 0600 file. Residual: native code that opens `/dev/tty`, a socket, a pre-bootstrap duplicate, or native `fork()` is outside containment.

CLI entry: the console script changes from `decoy.__main__:app` (`decoy/pyproject.toml:178`) to `decoy.__main__:main`, which installs process mode then calls `app(standalone_mode=False)`. Exact Click/Typer control-flow types (`UsageError`, `BadParameter`, `Abort`, `ClickException`) map to fixed usage events and exit codes; no `format_message`, `show` or echo of option values. Help text is static.

## 4. Defect-class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | Three-way surface classification (5.5), provenance-backed `SafeEvent`, sealed payload, class-bound drivers, exact exception-type table, `__name__` ban | T0 classification completeness plus the seeded corpus T1-T10 over every (a) and (b) surface with a positive control first | Remove one factory call, register an (a) driver as (b), or re-add `type(exc).__name__` in `postgres_snapshot`: a marker or the analyzer fails |
| 2 | Cleanup missing on an exit path | One owner per state: `SafeOutputScope` (handlers, streams, hooks), the test lease, the child transport | Exit-path matrix (6.3): success, invalid input, sink failure, cancel, exception, nested scope, double exit, lease exit | Drop one restore in `__exit__` or the at-fork close: identity or leaked-bytes check fails |
| 3 | State machine and concurrency | The ownership transition table (3.3); locked provenance and alias registries; coordinator barrier | Barrier-forced test per table cell including every forbidden pair; deterministic stale-ID collision | Allow one forbidden transition or remove the alias lock: the interleaving test fails |
| 4 | Route parity and silent fallback | One sink manifest covering every engine route and the re-ingest route (SSE, job `log`, node-runs, server-log tail, F32 reader) | Corpus over every route in 5.2 with a registered route event before absence; T3 re-ingest | Replay one stored line without `reingest_line`: T3 fails |
| 5 | Referential integrity | F31 changes no FK semantics; FK reports are projected: `FkValidityReport.relationship` is (b) `SchemaName`s or (a) `SafeEdge` aliases | FK-shape output matrix (6.4) over single, composite, self, multi-parent, later-batch children | Render `relationship` raw on an (a) surface: the matrix fails |
| 6 | Untrusted producer | Exact types, bounded `reingest_line` parser, rebuilt reasons, child FD redirection | Field walk derived from `decoy_engine.__all__` at test time with hostile values; hostile-line corpus for `reingest_line` | Accept a subclass or an unknown key in `reingest_line`: the corpus fails |
| 7 | TOCTOU / check-then-use on a path | Child transport is an inherited descriptor, never a reopened path; data-plane writers are exact functions | Analyzer fixture that reopens the transport by path; symlink swap on the private envelope file | Open the envelope by path after the check: the fixture fails |
| 8 | Weak tests and overclaimed evidence | Shrink-only analyzer baseline (6.5); positive control in every recorder; schema snapshots on values; reuse-observation test advisory only | Recorder self-test with a disconnected sink; baseline-growth test | Add a baseline entry or disconnect a recorder: the sentry fails |
| 9 | Behavior-preserving refactor drift | Characterization tests first for `routes_runtime_facts`, `assembly.py` node join, progress legacy keys, CLI `scan --json` | Snapshot of node-runs grouping and evidence `nodes` before P1 | Change the alias join to name join: characterization fails |
| 10 | Frozen contract drift | Section 7 list with value semantics; additive-only v1 | Schema snapshot tests that assert value domains (regex or enum per field), not only keys (T5) | Emit `field-3` where (b) requires a name, or a name where (a) requires an alias: T5 fails |

## 5. Enumerations from the code

### 5.1 Exit paths

- Transactional sink: no-write, write failure, commit success, commit failure, abort (`src/decoy_engine/execution/_transactional_sink.py:58-97`, `:324-380`); chunked sink exception, commit failure, normal without commit (`src/decoy_engine/execution/_chunked_output_sink.py:128-175`).
- Isolated execution: pre-spawn refusal, in-process failure, timeout kill, callback failure, abnormal exit, self-reported failure, commit failure (`src/decoy_engine/execution/_isolated_run.py:194-243`, `:437-573`).
- Progress: no sink, delivery, sink exception, terminal emission (`src/decoy_engine/progress.py:162-197`, `:428-484`).
- New in F31: every cell of the ownership table (3.3), invalid construction, serializer failure, driver failure, re-ingest rejection, lease exit, process-hook re-entry.

### 5.2 Routes

- Relationship jobs: `out_of_core`, `sequential`, `mixed`, `full_frame`, or reject (`src/decoy_engine/execution/_pipeline_routing.py:13-85`, `:300-321`).
- Non-relationship per-table full-frame or chunked; independent multi-table split (`src/decoy_engine/execution/_pipeline_multi_table.py:1-20`).
- Every route in-process or isolated; re-entry degrades to in-process inside the same scope (`src/decoy_engine/execution/_isolated_run.py:205-243`).
- Re-ingest route (new): stored `Job.log` to SSE, job `log` and node-runs (`api/jobs/routes_streaming.py:86-101`, `api/jobs/routes_runtime_facts.py:46-100`); server-log files to the tail SSE (`api/server_logs/tail_stream.py`); isolated envelope to parent (`src/decoy_engine/execution/_isolated_worker.py:70`); F32 reader.

### 5.3 FK shapes

- Single and composite edges by tuple arity (`src/decoy_engine/relationships/_graph.py:71-92`).
- Self-reference, cross-table cycle refusal, multi-parent first-hit (`src/decoy_engine/execution/_pipeline_routing.py:408-414`, `src/decoy_engine/relationships/_graph.py:163-180`).
- Children in later streamed batches (`src/decoy_engine/execution/out_of_core/_stream_join.py:244-281`); multi-parent merge (`src/decoy_engine/execution/out_of_core/_multi_parent.py:49-103`). F31 projects these as `SafeEdge` only; values and orphan decisions are unchanged.

### 5.4 Fields

The field walk is generated at test time from `decoy_engine.__all__` plus every public exception class's attributes; this list is the review baseline, and the generated walk fails if a public type is missing from it.

- `ReasonContext` and `ReasonRecord` (`src/decoy_engine/reasons/_record.py:114-126`, `:241-254`): (a), source refs become aliases.
- Seven progress variants and legacy keys (`src/decoy_engine/_progress_events.py:273-477`): (a); `table` and `column` values become aliases (see open question Q1).
- `ExecutionResult` (`src/decoy_engine/execution/_adapter.py:42-71`), `IsolatedRunResult` (`src/decoy_engine/execution/_isolated_common.py:320-388`), `RowError`, `RowErrorRecord`, `RowErrorSummary` (`src/decoy_engine/execution/_row_errors.py:28-59`, `src/decoy_engine/execution/_row_error_summary.py:37-86`): (b) projection; `error` and row-error prose become reasons.
- `QualitySummary` (`src/decoy_engine/validation/post/_types.py:61-74`): `distinct_counts`, `null_counts`, `duplicate_counts`, `fk_validity`, `composite_coherence`, `timing_per_phase`, `failed_checks`, `quality_warnings` are (b) with names as `SchemaName`; **`sampled_values: dict[str, list[Any]]` (`:70`) holds masked cell samples and is data plane (c) only**: dropped from every (a) and (b) projection, including the isolated envelope `quality_metrics` (`src/decoy_engine/execution/_isolated_worker.py:55-70`).
- `FkValidityReport.relationship` (`src/decoy_engine/validation/post/_types.py:39`) and `CompositeCoherenceReport.generator`, `.columns` (`:53-54`): (b) as `SchemaName`; (a) as `SafeEdge` and aliases.
- `QualityWarning.detail` (`src/decoy_engine/generation/pool/_events.py:14-39`): free text, replaced by a reason ID.
- `ValidationMessage` (`src/decoy_engine/validation_result.py:47-92`): (b); `message` and `hint` become registry prose; `path` and `node_id` are config pointers (`SchemaName`).
- `CheckResult`, `WriteResult` (`src/decoy_engine/sdk.py:111-131`), `DiscoveryResult` (`src/decoy_engine/data_discovery.py:77-88`), `PlanValidationResult` (`src/decoy_engine/plan/validate/_consolidator.py:36-65`), `UnmaskResult` (`src/decoy_engine/unmask.py:76-94`), `GenerationResult` (`src/decoy_engine/generation/_streamed.py:71-88`), `SubsetResult` (`src/decoy_engine/subset/_types.py:217-223`), `WalkResult`, `DriftResult` (`src/decoy_engine/walks/types.py:97-124`): free detail becomes reasons; tables and output paths are (c).
- Public exception attributes, for example `PostgresSnapshotError.cause_type` (`src/decoy_engine/connectors/postgres_snapshot.py:123-126`): retyped to `SafeErrorType`.

### 5.5 Output surfaces (three-way classification)

| # | Surface | Source | Class | Rule |
|---|---|---|---|---|
| S1 | Engine progress callback, v0 and v1 dicts | `src/decoy_engine/_progress_events.py:430-477` | a | keys frozen; `table`/`column` values are aliases (Q1) |
| S2 | Engine stdlib logs, default file `logs/decoy_engine.log` | `src/decoy_engine/internal/logging.py:16-60` | a | file handler removed; safe handler only |
| S3 | Warnings, error text, `IsolatedRunResult.error`, exception messages | `src/decoy_engine/execution/_isolated_common.py:178-286` | a | reason plus `SafeErrorType` |
| S4 | `Logger` narrative, `TelemetryClient.emit` | `src/decoy_engine/context.py:22-45`, `:227-275` | a | `SafeEventSink` only |
| S5 | `StructuredEvents.step/lineage/fidelity/quarantine` | `src/decoy_engine/context.py:64-83` | b | real node IDs and labels; `error_msg` removed; adds `node_alias` |
| S6 | In-process results and reports (5.4) | `src/decoy_engine/execution/_adapter.py:42-71` | b | names as `SchemaName`; no samples |
| S7 | Masked output, subset output, vault, quarantine rows, raw profile `--output` | `decoy/src/decoy/cli/scan.py:796` | c | exact writer manifest |
| S8 | Isolated envelope file and child transport | `src/decoy_engine/execution/_isolated_worker.py:70` | c transport, a/b content | content typed; `default=str` removed |
| S9 | Diagnostics, support payloads | `src/decoy_engine/quality/diagnostic.py:127-225` | a | aliases |
| S10 | STORM `phase-result/v1`, `phase-event/v1` | STORM `src/decoy_engine/phase.py:459-478` | b | `error_type` from `safe_error_type` |
| S11 | CLI stdout and stderr text, errors, help, usage and parse errors, root callback, hooks, `--verbose`, progress | `decoy/src/decoy/__main__.py:164` | a | entry wrapper |
| S12 | CLI `scan --json`, `scan fields --json` | `decoy/src/decoy/cli/scan.py:151` | b | real names for the `jq '.fields[].name'` use; no samples |
| S13 | CLI evidence and report files | `decoy/src/decoy/cli/evidence.py:118` | b | no cell values |
| S14 | CLI `decoy ci` JSON, SARIF, Markdown (`decoy-ci/v1`) | `decoy/src/decoy/ci/contracts.py:153-178` | b | repository-relative paths of owner files stay; stderr is a |
| S15 | Platform API error bodies, `HTTPException.detail` (33 f-string details at `ea68d8e`) | `api/admin/custom_faker_providers_router.py:216` | a | `SafeJSONResponse` |
| S16 | Platform API success bodies, fidelity view, evidence views | `api/evidence/assembly.py:1885-1890` | b | OpenAPI unchanged |
| S17 | `Job.log` column (stored) | `api/models.py:424` | a | v1 lines only |
| S18 | Job SSE, job `log` field, `JobNodeRunOut.logs` | `api/jobs/routes_streaming.py:86-101`, `api/jobs/schemas.py:264` | a inside a b response | re-ingest then render |
| S19 | `JobStep`, `JobNodeRun`, lineage, fidelity tables | `api/models.py:1449-1460` | b | real `node_id` join kept |
| S20 | Server log files and `/server-logs/tail` SSE | `api/server_logs/tail_stream.py` | a | v1 lines with no `run`; tail re-ingests |
| S21 | Audit rows (`detail`, before, after, meta) | `api/audit/logger.py:83-92` | b | `detail` becomes reason ID; no cell values |
| S22 | `/api/metrics` Prometheus exposition | `api/observability/metrics.py:208-266`, `:351` | a | closed-enum labels only |
| S23 | OpenTelemetry spans and attributes (F30, not yet built) | none at `ea68d8e` | a | `SealedLabels`; run UUID and aliases allowed, names not |
| S24 | Sentry and third-party trackers | `api/cloud/io.py:134-137` | a | `before_send` accepts only sealed payloads; locals off |
| S25 | Platform child stdout and stderr | `api/generation/generation_child.py:277-291`, `api/cloud/_pg_probe_child.py:80-84` | a (discarded) | dedicated transport |
| S26 | Support bundles (F32, not yet built) | none | a | stored v1 lines re-ingested; no legend |

## 6. Acceptance tests, red first

### 6.1 Seeded corpus

Distinct high-entropy markers plus grammar-clean canaries (`canary_table_alpha`, a valid UUIDv4 in a cell, small integers in layout fields) for every value class in P1. Every recorder first asserts a registered control event arrived, then asserts marker absence over bytes, JSON keys and values, created filenames, captured log records before formatting, and persisted database JSON. On (b) surfaces the recorder asserts the clean schema-name canary IS present (non-vacuity of P2) and every cell-value marker is absent.

### 6.2 The ten ways a gate reviewer breaks it (each RED first)

| # | Break | RED-first test | Slice |
|---|---|---|---|
| T1 | `--help` or a parse error echoes a seeded option value | `tests/e2e/test_entry_boundary.py`: subprocess of the console script with markers in option values, unknown options, `BadParameter`, root callback | C1 |
| T2 | A test calls `CliRunner(app)` and skips the entry wrapper | `tests/sentry/test_cli_entry_only.py`: AST scan fails any e2e test invoking `app` not via `main`; plus one subprocess cell per command through `decoy.__main__:main` | C1 |
| T3 | Job SSE replays a marker from `Job.log` | `tests/test_safe_output_sse_replay.py`: seed a legacy free-text row and a forged v1 line with a wrong `run`; SSE, job `log` and node-runs show only `safe_output.legacy_withheld` / invalid | P1 |
| T4 | A `/metrics` label carries a value | `tests/test_safe_output_metrics.py`: markers into every metrics helper argument; scrape `/api/metrics` bytes; `metrics.py:391` path raises a secret-named subclass | P2 |
| T5 | A schema snapshot passes while value meaning changes | Value-domain snapshots: (b) fidelity, `scan --json` and node-runs assert real canary names; (a) progress and SSE assert `^(table|field|node)-\d+$`; keys alone never pass | E4, C2, P1 |
| T6 | A secret-named exception subclass flows through `postgres_snapshot` `cause=` | Runtime test raising `type("SECRET_x", (OSError,), {})` in each of the six sites, plus analyzer rule `no-dynamic-exc-name` with fixtures for `type(e).__name__`, `__qualname__`, `e.__class__.__name__`, `getattr(type(e), "__name__")` | E2 |
| T7 | `QualitySummary.sampled_values` reaches the manifest or evidence | Field walk seeds a marker in `sampled_values`; scan the isolated envelope, Platform manifest JSON and evidence bundle | E5, P2 |
| T8 | The `default=str` gate is red on `_reproducibility.py` | Analyzer rule `json-default-str` scoped to functions reaching an (a)/(b) driver; the four digest functions are in the non-output manifest; a fixture that routes a `default=str` dump to a sink fails | E4 |
| T9 | Child and parent ordinals disagree, or a re-rendered row relabels | Isolated run with dynamic discovery: aliases in child-emitted and parent-emitted events and in a later re-render are identical; retry of the same `run_uuid` reproduces them | E3, P1 |
| T10 | Analyzer CI is red in E1-E4, or an allowlist creeps in | `tests/sentry/test_safe_output_baseline.py`: fails on a violation not in the baseline, on a stale entry, and on any entry or per-rule count added after E1 | E1 |

T0: a test enumerates the sink manifest and fails if any registered driver has no row in 5.5.

### 6.3 Exit-path matrix

One parametrized test over every row of 5.1 and every cell of the 3.3 table, asserting exact restoration of handlers, streams and hooks, no leaked bytes, and the expected registered event.

### 6.4 FK-shape output matrix

For each shape in 5.3 and each route in 5.2: (a) surfaces show `SafeEdge` aliases and no names; (b) `FkValidityReport` shows real names; relational results are byte-identical to the baseline.

### 6.5 Analyzer ratchet

Slice E1 lands the analyzer with a committed baseline `tests/sentry/safe_output_baseline.json` keyed by (file, enclosing qualname, rule), not by line. At `fdd08a8` there are 260 `except ... as` sites (`git grep -nE "except .* as [a-z_]+:" -- src/decoy_engine`) and 55 dynamic exception-name reads (`git grep -nE "type\((exc|e|err|error|ex)\)\.__(qual)?name__|__class__\.__name__"`); E1 records the analyzer's exact counts per rule. The rule mirrors the module-size census (`tests/sentry/test_module_size.py:54-66`): entries and per-rule counts may only shrink, stale entries fail, and the final engine slice requires the baseline empty. The reproducibility-observation GC test is advisory; the deterministic stale-entry collision test is mandatory.

## 7. Frozen contracts touched

| Contract | Change |
|---|---|
| `decoy-engine` public API (0.7.0, `pyproject.toml:5-13`) | Additive `safe_output`; pre-GA break: `StructuredEvents.step` drops `error_msg`, gains `node_alias`; legacy dict helpers return sealed payloads. Version: the next engine minor released after E5 (recorded at release; consumers pin exactly that, per F36's lineage rule) |
| Reason registry 1.0.0 (`src/decoy_engine/reasons/_registry.py:24-44`) | Additive entries and the `ordinary_output` flag |
| Progress v0/v1 (`tests/unit/progress/test_versioned_contract.py:37-78`) | Keys unchanged; `table`/`column` value domain becomes aliases (breaking value change, Q1); snapshot asserts values |
| `phase-result/v1`, `phase-event/v1` (STORM) | Keys unchanged; `error_type` value domain is the closed `SafeErrorType` labels (STORM swaps `phase.py:478` to `safe_error_type`) |
| `fidelity-report/v1`, `integrity-report/v1` | Unchanged (class b); value snapshot asserts real names |
| `decoy-ci/v1` (CLI main, `src/decoy/ci/contracts.py`) | Unchanged (class b) |
| CLI `scan --json` / `scan fields --json` (`decoy/src/decoy/cli/scan.py:151`, `docs/cli-reference.md:1120`) | Unchanged (class b) |
| Platform OpenAPI and web client | Unchanged shapes. Content changes: `log` text and SSE `data:` lines become rendered v1 events (class a). Additive DB columns `jobs.run_uuid`, `job_node_runs.node_alias` |
| `decoy-safe-output/v1` (new) | Additive only after release |

## 8. Reuse inventory

| Existing | Source | Use |
|---|---|---|
| F29 reason registry and records | `src/decoy_engine/reasons/_registry.py:24-44`, `src/decoy_engine/reasons/_record.py:241-366` | Prose authority and nested reason |
| Reason adapter | `src/decoy_engine/reason_adapter.py:38-100` | Reason selection under `safe_event_from_exception` |
| Progress sequencing | `src/decoy_engine/progress.py:162-197` | Kept; drivers added |
| Log interpolation sentry | `tests/sentry/test_log_interpolation.py:20-83` | Generalized into the shared analyzer |
| Module-size census | `tests/sentry/test_module_size.py:54-66` | 600/700 unchanged; ratchet pattern for 6.5 |
| Hostile corpora | `tests/security/test_reason_boundary.py:44-195` | Retained and lifted into 6.1 |
| Platform closed metric pickers | `api/observability/metrics.py:192` | Reused as the metrics label domain |
| F43 value-free CI boundary | `decoy/src/decoy/ci/contracts.py:1-11`, `ci/engine_logging.py:1-12` | Kept as the class (b) CI renderer; `engine_logging` silencer replaced by `SafeOutputScope` in C3 |

New machinery (provenance, sealed payload, re-ingest, class-bound drivers) exists because no current carrier authorizes a whole output object or validates a stored line.

## 9. Pre-mortem

| Likely gate finding | Class | Prevention |
|---|---|---|
| A surface is missing from 5.5 | 1, 4 | T0 fails on a driver with no row |
| A (b) driver accepts an operational event or renders a `SchemaName` on an (a) surface | 1, 10 | Drivers are class-bound at registration; T5 value snapshots |
| SSE or node-runs replays a stored line raw | 4, 6 | One reader; T3; analyzer rule bans `Job.log` reads outside it |
| CLI tests bypass `main()` | 8 | T2 plus the test lease |
| Analyzer passes while the baseline grows | 8 | T10 |

## 10. Slices

Each slice: RED tests first with output recorded, smallest change, self-check, review, at most two remediation rounds. Production modules stay below 450 physical lines (plan rule); test files follow the repository sentry (600/700).

| Slice | Repo | Scope | Size | Depends on |
|---|---|---|---|---|
| E0 | engine | Model, provenance, sealed payload, aliases and coordinator barrier, `SafeRunId`, wire form, `reingest_line`, registry flag and opt-in manifest | M | none |
| E1 | engine | Shared analyzer in `_testing.py`, engine sink manifest, writer and non-output manifests, ratchet baseline, corpus harness, T0, T10 | M | E0 |
| E2 | engine | Exception table, `safe_error_type` export, replace `safe_error_class` and the six `postgres_snapshot` sites, drain `no-dynamic-exc-name` baseline, T6 | M | E1 |
| E3 | engine | Ownership state machine, stream proxies, process hooks, test lease, public `child.bootstrap_child_streams`, isolated worker, T9 (engine half) | M | E2 |
| E4 | engine | Progress drivers, logging handler, `Logger`/`StructuredEvents` changes, envelope typing, `default=str` rule, T5 (progress), T8 | M | E3 |
| E5 | engine | Families: quality, evidence, diagnostics, phase; `QualitySummary` projections, T7 (engine half); baseline empty; release | M | E4 |
| C1 | CLI | Console script to `main`, entry wrapper, usage mapping, test lease use, T1, T2 | S | engine release |
| C2 | CLI | Command migration: (a) text/progress; (b) `scan --json`, evidence; T5 (CLI) | M | C1 |
| C3 | CLI | `decoy ci` silencer to `SafeOutputScope`, CLI analyzer manifest | S | C2 |
| P1 | Platform | Alembic: `jobs.run_uuid`, `job_node_runs.node_alias`; job-log sink driver; `JobLogger` free-text narrative methods (`api/persistence/logger.py:152-175`) removed so Platform code also writes `Job.log` only through the driver; one re-ingest reader for SSE, `log`, node-runs; characterization first; T3, T5, T9 | M | engine release |
| P2 | Platform | `SafeJSONResponse`, `SafeSSEResponse`, API errors, audit `detail`, evidence projections, metrics labels and `metrics.py:391`, server-log tail re-ingest, Sentry `before_send`, T4, T7 | M | P1 |
| P3 | Platform | Children: `generation_child.py`, `_pg_probe_child.py` moved off stdout to a dedicated descriptor plus `bootstrap_child_streams`; Platform analyzer manifest | S | P2 |
| X | all | Cross-repo corpus on the exact released engine and pinned consumer commits | S | C3, P3 |

All Platform edits (including children) are after the engine release; no engine slice edits another repository. STORM CLI and Platform rebase onto F31 after C1 and P2 respectively and are not F31 slices; F31's promise to them is the exported API in 3.1-3.3.

Self-check per slice: the slice's named tests, then `pytest -q`, `ruff check`, `mypy` on the touched package, run from `uv.lock` (`uv sync --locked`). `scripts/dev/gates.sh` does not exist in the engine at `fdd08a8`; until it lands, evidence states the exact commands.

## 11. Worker evidence rules

- Name the class of every finding and fix the class structure, not only the instance.
- Write the generated tests first (field walk, exit-path matrix, interleavings, marker corpus) and record them red.
- Attack your own diff: mutate each new mechanism once and show the test goes red.
- Run baselines with `uv.lock` in the same environment as the branch; state what you did not run.
- Never widen a threshold, skip a test, grow the analyzer baseline, or reduce a budget to pass.
- If a defect of a class already in the catalog recurs, stop and report it; do not patch a third time.

## 12. Review finding disposition (Opus review on `review/f31-opus`)

| Finding | Disposition | Where |
|---|---|---|
| B1 aliasing scope vs frozen contracts | Fixed (owner decision B1) | 1, 5.5, 7, T5 |
| B2 persisted and cross-process semantics | Fixed (owner decision B2) | 2, 5.2, T3, T9 |
| H1 F32 not expressible | Fixed: `SafeEdge`, `SafeVersion`, `SafeDtype` in v1 | 2, 3.1 |
| H2 F30 telemetry and `/metrics` | Fixed: `SealedLabels`, S22, S23 | 3.1, 5.5, T4 |
| H3 STORM type label and phase schemas | Fixed: exported `safe_error_type`; phase schemas frozen | 3.2, 7 |
| H4 phantom F43 branch | Fixed: F43 cited from CLI main `b2209e1`; F43 is class (b) and not migrated beyond its silencer | Baselines, 8, C3 |
| H5 `default=str` gate | Fixed: rule scoped to sink-reaching functions; four digest functions (`generation/_reproducibility.py:204`, `pool/_builder.py:42`, `plan/_generation.py:260`, `profiling/model_pack/trainer.py:593`) in a non-output manifest; no hash change | T8 |
| H6 two exception policies, no `__name__` ban | Fixed: one table, analyzer rule, `postgres_snapshot` banned | 1 P5, 3.2, T6 |
| H7 field walk misses raw samples | Fixed: walk generated from `__all__`; `sampled_values` is (c) | 5.4, T7 |
| H8 slice size and cross-repo Slice 3 | Fixed: 13 slices, no engine slice edits another repo, public child bootstrap | 3.3, 10 |
| M1 install has no test lifecycle | Fixed: test lease plus T2 | 3.3, 6.2 |
| M2 analyzer ratchet | Fixed: shrink-only baseline | 6.5, T10 |
| M3 no transition table | Fixed | 3.3 |
| M4 flaky ID-reuse test | Fixed: advisory | 6.5 |
| M5 version pin | Fixed: next minor after E5, exact pin | 7 |
| L1 Platform pin | Fixed: `ea68d8e` | Baselines |
| L2 undefined FK-shape matrix | Fixed | 6.4 |
| L3 450-line budget ambiguity | Fixed: production modules only | 10 |
| Grounding: `run.py:207-219`, missing Slice 0 test file, missing `gates.sh` | Fixed: citation and file removed; `gates.sh` absence stated | 10 |

## 13. Open questions for the owner

- Q1. Progress v0/v1 is listed as operational, so `table` and `column` values become aliases. Do the CLI progress bar and any Platform progress view need real names (which would make progress class b)?
- Q2. Should Platform persist a full owner-result alias legend so the owner UI can resolve `table-N` in job log lines? Default in this plan: node aliases only (via `job_node_runs.node_alias`).
- Q3. Legacy free-text `Job.log` rows are withheld at read, not purged. Purge them with a migration instead?
