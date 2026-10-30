Status: plan only. No implementation or release approval is granted by this document.

Summary: Make ordinary operational output value-free by construction through one engine-owned typed event, one registry-backed serializer, and build-failing runtime and static sentries.

Baseline: `decoy-engine` commit `fdd08a894d5dd4440a1adea0ee62bf0b670808bb`, equal to `origin/main` when framed on 2026-10-30. Planning branch: `plan/f31-safe-output`.

# F31: Shared safe-output and redaction controls, FRAME + PLAN

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `/home/cam/dev-rules/risk-and-exceptions.md`, `/home/cam/dev-rules/README.md`, and the README-selected rulebooks `/home/cam/dev-rules/development-loop.md`, `/home/cam/dev-rules/workspace-and-vcs.md`, `/home/cam/dev-rules/reuse-first.md`, `/home/cam/dev-rules/exploration.md`, `/home/cam/dev-rules/scope-discipline.md`, `/home/cam/dev-rules/verification.md`, `/home/cam/dev-rules/feature-dev.md`, `/home/cam/dev-rules/testing.md`, `/home/cam/dev-rules/architecture.md`, `/home/cam/dev-rules/api-and-compatibility.md`, `/home/cam/dev-rules/release-and-rollback.md`, `/home/cam/dev-rules/observability-and-resilience.md`, `/home/cam/dev-rules/config-secrets-and-supply-chain.md`, `/home/cam/dev-rules/security.md`, `/home/cam/dev-rules/code-review.md`, and `/home/cam/dev-rules/documentation.md`. Repository guidance consulted: `AGENTS.md`, `README.md`, `CODEMAP.md`, `CONTRIBUTING.md`, `CLAUDE.md`, and `SECURITY.md`.

Risk: **R2**. This is a security and privacy boundary, a public API addition, a wire-contract change, and a coordinated three-repository migration. The owner is the approver. Every slice requires fail-before evidence, targeted green evidence, an adversarial review, and remediation before the next slice.

## FRAME

### Problem

The system has several useful redaction mechanisms, but no single type that controls all ordinary output. A key allowlist does not make an arbitrary string value safe. A regex replacement does not prove absence. An exception object, traceback, path, source identifier, engine warning, or report dictionary can still cross a CLI, Platform, evidence, telemetry, diagnostic, or log surface.

The repeated Phase-1 gate failures on B-9, F26, STORM, F43, and F13 are the same architectural defect. Each local patch closes one path while another unconstrained value remains representable. F31 changes the representation and the build gate so a producer cannot construct or emit such output accidentally.

### Guarantee, precise and testable

**Invariant:** No value derived from customer data, a source or output path, a credential or secret reference, an exception message or representation, a captured local, a library or driver message, or customer-controlled prose may reach an ordinary output surface except after conversion to a provenance-backed immutable `SafeEvent` by the engine-owned safe-output boundary. Conversion never preserves such a value. Application and renderer code accepts only a provenance-backed `SafeEvent` or an opaque sealed `SerializedSafePayload`. Only registered sink drivers can unwrap that payload directly into final bytes. No plain mapping is an authorized ordinary-output carrier.

For this invariant, a **value** is any bytes, text, object representation, identifier, label, collection member, mapping key, or formatted derivative whose content or length can depend on:

- source or generated cell contents, raw rows, buffers, samples, layouts, schemas, table names, or column names;
- local, cloud, archive, report, source, destination, or temporary paths;
- URLs, DSNs, hosts, query strings, credentials, tokens, keys, secret references, provider arguments, or subprocess output;
- `str`, `repr`, `args`, `__dict__`, `__cause__`, `__context__`, traceback frames, source lines, or locals of an exception;
- free-form extension, connector, validator, engine, CLI, Platform, warning, logging, driver, or tracker text.

Allowed ordinary output is limited to:

- registry identities and registry-authored prose;
- exact members of reviewed closed enums;
- nonnegative bounded counts, ordinals, finite bounded measurements, booleans, and timestamps created by the boundary;
- line, column, row, chunk, and component positions without a filename or source line;
- exception type labels returned by exact type identity lookup in a closed engine and builtin type-to-enum table, with every other exact type represented as `Exception`; no exception class name is read dynamically;
- provenance-backed opaque stable identifiers minted at an engine-owned or server-owned creation seam; UUID or ULID syntax is only a secondary check and never sufficient, and there is no generic token field;
- stable aliases such as `table-1` and `field-2`, allocated from first-seen order or a trusted ordinal without retaining the original name in the event.

A short or clean-looking source identifier is still a value and is aliased. A generic bounded string is not safe merely because it matches a grammar. No ordinary event has a free-form string, `Any`, arbitrary mapping, exception, path, bytes, dataframe, row, or user-extensible metadata field.

The guarantee is tested twice. Runtime acceptance tests plant unique markers in every input class and scan every ordinary surface in every mode. A construction sentry rejects production output operations that do not consume the boundary type. Each sentry has mutation tests that intentionally introduce a violation and prove the sentry fails.

### Definition of done

F31 is done only when all of the following are true:

1. `decoy_engine.safe_output` is the public owner of `decoy-safe-output/v1`, and `decoy_engine` 0.8.0 exports its supported public names.
2. Every `SafeEvent`, `OpaqueId`, and `SerializedSafePayload` is immutable, construction-validated, weak-referenceable despite being slotted, and registered through a closure-held provenance capability that is absent from class attributes and public constructors. Emission verifies both exact type and provenance and rejects `object.__new__`, `object.__setattr__`, subclass, replacement, copied-token, stale-identity, and token-shaped-value forgeries. Garbage collection evicts provenance registrations before an object address can be accepted for a later object.
3. One internal serializer seals a canonical payload. Text, JSON, log, audit, evidence, telemetry, diagnostic, support, and tracker renderers accept only a provenance-backed `SafeEvent` or opaque `SerializedSafePayload`; no renderer accepts or returns an application-mutable mapping. Only registered final sink drivers turn the sealed payload into bytes.
4. Invalid or unknown input produces the fixed registered `safe_output.invalid` event. Serializer or sink failure produces only the literal emergency `safe_output.failed` record. Neither path logs, formats, or falls back to the rejected object.
5. Exceptions become a registry reason, a closed type label, and a closed component plus numeric position only. Ordinary exports hold no raw exception, chain, traceback, frame, source line, or local.
6. The default engine file handler is removed. Permanent stream proxies and process hooks are installed only by explicit CLI, Platform server or worker, or isolated-worker entrypoints, never by import or a library call. Process-owned mode discards unscoped stream writes by default and admits underlying-stream writes only through the sink capability. Library calls use a reversible `SafeOutputScope` bound to the host's current streams, propagate context through every engine-owned thread submission with `copy_context().run`, and restore the exact host objects. Decoy-owned child workers redirect file descriptors 1 and 2 at operating-system level and use a separate non-inheritable structured channel.
7. CLI and Platform pin exactly `decoy-engine==0.8.0` and replace their duplicate boundaries with thin adapters to the engine contract. No second value policy remains.
8. The seeded corpus covers every listed surface and mode with a positive non-vacuity assertion. It includes high-entropy, grammar-clean, UUID-shaped, short, numeric, and shape-preserving canaries. One shared analyzer runs in engine, CLI, and Platform CI with repository-specific sink manifests, catches direct, aliased, assigned, reflective, framework-implicit, and exception-state sink access, and proves each rule RED before implementation.
9. Existing security assertions remain present and at least as strict. The new suite, each repository's full suite, static checks, and secret scanners pass on the exact reviewed commits.
10. Documentation describes the ordinary channel, the isolated unsafe channel, schema compatibility, and the residual risks. The exact-artifact gate receipt is recorded only after all required reviews and documentation sync.

### Boundaries and non-goals

Ordinary output includes stdout, stderr, `--json`, ordinary `--output` reports, application and engine logs, warnings, progress, API error bodies, job events, audit rows, evidence, telemetry, diagnostics, verbose mode, support bundles, and third-party error or observability trackers.

Primary data artifacts are not ordinary output. Masked datasets, explicitly requested raw scan profiles, encrypted vaults, private isolated-worker payloads, and quarantine rows may necessarily contain data. They must use separately typed data-plane sinks, restrictive permissions where applicable, and can never be passed to an ordinary sink. Their status and failures are reported only as `SafeEvent` values. The static sentry maintains a reviewed exact list of data-plane writer functions, not a directory or filename wildcard.

Native libraries or drivers that write directly to file descriptors 1 or 2 remain a residual only for in-process execution. Python stream containment also cannot prove control of a foreign C background thread or a library that retained a duplicate of an original descriptor. Every Decoy-owned isolated child redirects descriptors 1 and 2 before importing or invoking extensions, disables `faulthandler` output, and carries structured results over a dedicated non-inheritable descriptor or private 0600 file. Callers must use that isolated path where native output or foreign background threads are in the threat model.

Closure-held provenance prevents accidental construction and ordinary application misuse. It is not a sandbox against arbitrary Python or native code already executing in the process. Such code can inspect frames, closures, or process memory despite API hiding. The production sentry prohibits those introspection mechanisms, but code that is malicious rather than buggy must run through the isolated-worker boundary.

The explicit unsafe diagnostic channel is out of the ordinary contract. It is disabled by default, requires an unmistakable `--unsafe-debug-log PATH` opt-in, writes mode 0600, labels every file as sensitive, is excluded from automatic upload, evidence, support bundles, and telemetry, and has a retention warning. Customer or administrator privilege never makes ordinary evidence less strict. The unsafe channel is tested for isolation, not for value freedom.

Regex substitution, Sentry scrubbing, OpenTelemetry processors, `detect-secrets`, and Gitleaks are defense in depth only. They are not proof of the invariant. F31 does not invent encryption, hashing, or a new secret-scanning algorithm.

### Phase pillars

1. **Make unsafe states unrepresentable:** one immutable model, closed values, registry prose, aliases, and one serializer.
2. **Contain ambient producers:** exception conversion, logging, warnings, streams, subprocesses, drivers, and trackers fail closed.
3. **Migrate without parallel boundaries:** engine first, then independently releasable CLI and Platform adapters, preserving prior tests.
4. **Prove the negative:** seeded-marker matrices, AST and taint sentries, mutation self-tests, full verification, and independent review.

## Established solutions surveyed before design

The implementation must cite and adapt these patterns in module documentation. It must not substitute a new local convention without a recorded reason.

- The [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) recommends application-wide handlers, structured formats, validation across trust zones, bounded fields, and excluding passwords, connection strings, keys, paths, and sensitive data. F31 adopts one structured handler and a closed field catalog.
- Python's [`logging.Filter` documentation](https://docs.python.org/3/library/logging.html#filter-objects) shows that handler filters see each record before emission and can replace a record. It also shows why a filter attached only to a parent logger misses descendant records. F31 installs an allowlisted handler and filters at the handler seam, while treating filters as containment rather than the construction proof.
- Python's [`TracebackException` documentation](https://docs.python.org/3/library/traceback.html#tracebackexception-objects) states that `capture_locals=True` captures object representations and that chained exceptions are formatted by default. F31 exports no traceback object and bans locals capture at ordinary exporters.
- Python's [`contextvars` documentation](https://docs.python.org/3/library/contextvars.html) states that each thread has its own context stack and provides `copy_context().run(...)` for explicit propagation. F31 wraps every engine-owned thread and executor submission with a copied context and does not assume that a `ContextVar` crosses a thread boundary.
- Python's [`weakref` documentation](https://docs.python.org/3/library/weakref.html) states that slotted classes need an explicit `__weakref__` slot and that weak-reference callbacks run when the referent is reclaimed. F31 makes each provenance-bearing type weak-referenceable and pairs each `id(obj)` entry with a weak reference whose callback evicts that exact entry.
- Python's [`faulthandler` file-descriptor warning](https://docs.python.org/3/library/faulthandler.html#issue-with-file-descriptors) states that replacing a descriptor with `dup2` changes where retained handlers write. F31 disables fault-handler output before child descriptor replacement and never uses stdout or stderr as the structured transport.
- FastAPI's [direct response documentation](https://fastapi.tiangolo.com/advanced/response-directly/) states that a returned `Response` is passed through without Pydantic conversion. Platform safe-output routes therefore return registered `SafeJSONResponse` or `SafeSSEResponse` objects containing sink-produced bytes and never hand a sealed payload to a response model, `jsonable_encoder`, or `JSONResponse(content=...)`.
- OpenTelemetry's [sensitive data guidance](https://opentelemetry.io/docs/security/handling-sensitive-data/) says instrumentation cannot determine business sensitivity, recommends data minimization, and documents allowlist redaction processors. Its [URL convention](https://opentelemetry.io/docs/specs/semconv/url/#sensitive-information) prohibits user and password capture and warns about query values. F31 sends only already-safe attributes. Collector processing is a second control because a processor can be omitted, misordered, or bypassed.
- Sentry's [project scrubbing controls](https://docs.sentry.io/api/projects/update-a-project/) are field-name and rule based and can exempt safe fields. F31 uses a `before_send` rejection hook over canonical safe events and disables default locals, request bodies, breadcrumbs, and raw exceptions. Server-side scrub rules remain secondary because data has already left the process and rules cannot recognize every customer value.
- Alexis King's [Parse, don't validate](https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/) provides the typed-output pattern: parse untrusted input into a representation whose constructors establish the invariant. F31 does not pass a validated dictionary onward.
- Semgrep's [taint rule guidance](https://semgrep.dev/docs/writing-rules/rule-ideas#detect-tainted-data-flowing-into-a-dangerous-sink) tracks sources into sinks. F31 keeps a repository-local AST sentry for exact Python policy and adds Semgrep taint rules as a broader CI backstop.
- [`detect-secrets`](https://github.com/Yelp/detect-secrets) documents regex, entropy, and keyword detectors and warns that regex-only use reduces recall. [Gitleaks](https://github.com/gitleaks/gitleaks) supplies a second repository and diff scanner. Both run after the construction proof and seeded corpus.

## Grounded current-state inventory

This inventory describes the engine baseline above. Cross-repository rows use `decoy` `origin/main` at `574aa1e9ec2b95f9ba8af5eb8112c1a3c0fcf434` and `decoy-platform` `origin/main` at `051ff2b7ab0ce835a6bd4eca103448e04579fdba`. The STORM and F43 rows are explicitly reference branches, not prerequisites present on the engine baseline.

| Emitter or boundary | Grounded source | Current exposure and F31 disposition |
|---|---|---|
| F29 reason record | `src/decoy_engine/reasons/_record.py:31-96`, `:114-223`, `:241-366` | Frozen record, closed context, registry validation, and emit-time rebuilding already exist. `approve_name` still permits clean schema names. Reuse the record as the nested reason identity, but aliases replace names at the ordinary boundary. |
| F29 registry and manifest | `src/decoy_engine/reasons/_registry.py:24-44`, `:126-180`; `src/decoy_engine/reasons/__init__.py:1-10` | Registry identity and authored summaries are the prose authority. Extend it with safe-output reasons instead of creating a prose registry beside it. |
| F29 progress events and current serializer | `src/decoy_engine/_progress_events.py:38-105`, `:113-222`, `:230-415`, `:428-599` | Frozen groups and `event_to_dict` revalidate many fields, but progress is only one surface and clean object names may survive. Convert these types to `SafeEvent` factories and make compatibility functions projections of the one serializer. |
| Public exports and version | `src/decoy_engine/__init__.py:169-185`, `:227-228`, `:308`, `:380-464`; `pyproject.toml:10` | Reasons and progress are public at version 0.7.0. F31 adds the safe-output package and bumps the pre-GA minor to 0.8.0. |
| Caller logger and structured callbacks | `src/decoy_engine/context.py:22-83`, `:97-224` | `Logger` accepts arbitrary messages. `StructuredEvents` accepts arbitrary names, labels, reasons, and `error_msg`; helper failures are swallowed. Replace narrative and structured callback arguments with one `SafeEventSink`. |
| Telemetry and captured exports | `src/decoy_engine/context.py:227-275` | `TelemetryClient.emit` accepts an arbitrary event and dictionary. `captured_outputs` and node exports accept arbitrary dictionaries. Ordinary exports become safe events; data-plane captures receive a separate capability and never enter ordinary sinks. |
| Engine file and console logging | `src/decoy_engine/internal/logging.py:16-55`, `:63-90`, `:92-136`, `:139-247` | Import-time singleton defaults to `logs/decoy_engine.log`; failure logs the requested path and raw exception. `ProgressLogger` formats caller messages. Remove the default file handler and make the adapter consume safe events only. |
| Direct production log calls | Examples: `src/decoy_engine/transforms/base.py:93-112`, `transforms/date_shift.py:166-246`, `profiling/model_pack/loader.py:161`, `internal/memory.py:54`, `execution/out_of_core/_capacity_eval.py:517`, `generation/_fidelity_gate.py:183` | Current calls include column names, configuration, paths, free messages, and exceptions. Inventory all calls mechanically, replace them with registered factories, then let the sentry forbid regressions. |
| Connector hardening | `src/decoy_engine/connectors/postgres_snapshot.py:505-526` | The connector already maps exceptions to a closed code and type and logs count, code, and location. Preserve this pattern and route the event through the shared serializer. It is evidence that value-free conversion is practical. |
| Exception adapter | `src/decoy_engine/reason_adapter.py:1-16`, `:38-100`, `:146-189`, `:368-390` | It deliberately avoids exception text and untrusted class codes. Reuse it as the reason-selection layer under `safe_event_from_exception`; do not duplicate classification. |
| Isolated error scrub | `src/decoy_engine/execution/_isolated_common.py:178-211`, `:214-286`, `:320-388` | Type and trusted-code summaries are useful, but regex scrub is narrow and `IsolatedRunResult.error`, metrics, and extras remain free-form. Replace the error with a safe event and type projections for ordinary metrics. Keep regex only as defense in depth. |
| Isolated worker and subprocess | `src/decoy_engine/execution/_isolated_worker.py:47-70`, `:167-193`, `:222-265`; `execution/_isolated_run.py:385-405`, `:433-471`, `:503-586` | `default=str`, raw argv on stderr, row-error strings, and free dictionaries are unsafe. The private 0600 payload is a data-plane artifact. Captured child streams are never forwarded; envelope errors and evidence cross only as safe events. |
| Fixed-width exception frames | `src/decoy_engine/profile/_fixed_width_reader.py:21-38`, `:74-131`, `:205-265` | Errors can include path, column, and layout values while parsing frames hold raw buffers and rows. Convert at the smallest catch frame and export no traceback or locals, including verbose mode. |
| Validation messages | `src/decoy_engine/validation_result.py:47-71`, `:87-186` | Public validation records carry free message, path, node, and hint strings. Add safe reason and alias projections. Do not serialize `ValidationMessage` directly to an ordinary surface. |
| Quality warnings and post-validation | `src/decoy_engine/generation/pool/_events.py:14-39`; `validation/post/_runner.py:109-133` | `QualityWarning.detail` is arbitrary and the runner records `repr(exc)`. Replace exported details with registered reasons and typed measures. |
| Pipeline evidence and row errors | `src/decoy_engine/execution/_adapter.py:42-83`, `:145-220`; `execution/_row_errors.py:28-90`; `execution/_pipeline_finalize.py:286-323`, `:351-463` | `quality_metrics`, table kinds, row errors, table and column names, and summaries are value-bearing dictionaries. Keep raw data internally, project ordinary evidence into typed events, and alias all source identifiers. |
| Diagnostics and subset manifest | `src/decoy_engine/quality/diagnostic.py:127-143`, `:170-225`; `subset/_manifest.py:1-12`, `:44-90`; `subset/_api.py:42-75`, `:182-190` | Reports expose real schema and namespace identifiers and write JSON directly. Their ordinary views use typed projections and aliases. An explicitly requested subset data manifest remains a data-plane artifact with a safe completion event. |
| Text-mask raw-value isolation | `src/decoy_engine/transforms/text_mask.py:44-47`, `:253-257`, `:548-553` | Raw matched text is already confined to keying and strategy code. Preserve these tests and add the marker corpus around every output sink. |
| Current log sentry | `tests/sentry/test_log_interpolation.py:1-10`, `:23-83`, `:86-123`, `:244-355`, `:439-566` | It has useful alias and named-sink tracking, but scopes logger interpolation and intentionally ignores `print`. Generalize its analyzer rather than adding a separate weak scan. |
| Current security corpus | `tests/security/test_reason_boundary.py:44-73`, `:107-169`, `:226-277`; `tests/unit/execution/test_isolated_safe_error.py` | Existing hostile markers cover reason and isolated paths. Retain every assertion and lift the corpus into the cross-surface matrix. |
| Phase envelopes | No `src/decoy_engine/phase.py` exists at this baseline | Do not invent a prerequisite. If STORM merges first, adapt its `phase-result/v1` and `phase-event/v1` into safe-event payload families; its generic frozen strings and mappings are not sufficient for F31. |
| CLI stdout, stderr, JSON, verbose, and files | `decoy/src/decoy/cli/scan.py:773-798`, `subset.py:278`, `:352-522`, `fit.py:321`, `:495-522`, `templates.py:113-148` | Main still has direct Rich, `sys.stdout`, file writes, full report dictionaries, free errors, and verbose tracebacks. The Slice 6 topology gate supplies the STORM structure whether or not it has merged, keeps command behavior, and replaces local value policy with engine factories. |
| CLI progress and report surfaces | `decoy/src/decoy/cli/run.py:207-219`, `:455-487`; `evidence.py:118`, `:459-492` | Progress, errors, evidence, and mode-specific rendering are distinct sinks today. All render from a provenance-backed event or sealed payload. Data stdout for `run` remains a typed data-plane sink. |
| Platform audit | `decoy-platform/api/audit/logger.py:19-28`, `:83-171` | `detail` is explicitly stored verbatim and before, after, and meta are arbitrary scrubbed objects. Ordinary audit fields become safe-event projections; existing audit integrity and blocking semantics remain unchanged. |
| Platform request and server log context | `decoy-platform/api/logging/context.py:39-68`, `:71-146`, `:222-227` | Route templates and validated UUIDs are useful trusted fields, but IP, user agent, actor, and other values are not permitted in an F31 ordinary event. Keep security audit data only where policy explicitly classifies it outside the value-free event, and never mix it into safe evidence. |
| Platform evidence and telemetry | `decoy-platform/api/evidence/redaction.py:142-204`; `api/evidence/assembly.py:1887-1888`; `api/jobs/routes_runtime_facts.py:76-87` | Existing redaction and fingerprints are defense in depth. Evidence assembly and runtime facts must accept typed engine projections, not arbitrary engine dictionaries. |
| Platform child and driver output | `decoy-platform/api/generation/generation_parent.py:189-200`; `generation_child.py:277-291`, `:380`; `cloud/pg_probe.py:85-86`, `cloud/_pg_probe_child.py:6`, `:83-84` | Some subprocess paths already capture or discard streams and one probe emits a closed code. Standardize on the scoped silencer and safe-event conversion; never replay a captured line. |
| Third-party trackers and locals | `decoy-platform/api/cloud/io.py:134-137`; `api/jobs/v2_cloud_materialize.py:409-410`; cloud credential tests using `TracebackException(..., capture_locals=True)` | The existing tests prove local variables are a live exfiltration channel. Configure trackers with locals and raw breadcrumbs disabled, then use a pre-send safe-event type check. |
| STORM reference, not baseline | Branch `claude/eager-clarke-14deh1`: `decoy/src/decoy/cli/_value_free.py:303-417`, `_projections.py:1-24`, `tests/sentry/test_value_free_boundary.py`; `decoy-platform/api/jobs/fidelity_projection.py:1-16`; `decoy-engine/src/decoy_engine/phase.py:1-16` | Adopt the single boundary, fixed prose, typed projection, command guard, engine silencer, and canary sentry shape. Strengthen it by always aliasing field names and removing generic string or mapping payloads. The Slice 6 and 7 topology gate uses the exact structural commits even when the branch is unmerged. |
| F43 reference, not baseline | Branch `feat/f43-ci-integration`: `decoy/src/decoy/ci/contracts.py:1-11`, `:60-145`, `:227-277`; `ci/engine_logging.py:1-12`, `:24-61` | Adopt construction validation, renderer revalidation, and logger-state restoration. Do not retain its repository path field in the universal event. `SafeFinding` becomes an F31 event family, not another boundary. |

The direct engine logging census is exhaustive for this baseline. The framing command was `rg -n '\.(debug|info|warning|warn|error|exception|critical)\(' src/decoy_engine --glob '*.py'`. Calls occur at:

- connector and context: `connectors/postgres_snapshot.py:522`, `:526`; `context.py:153`; `disguises/loader.py:45`, `:53`, `:70`;
- execution: `execution/_governor_monitor.py:165`; `_isolated_memwatch.py:321`, `:341`; `_isolated_run.py:215`, `:223`; `_pipeline_auto_chunk.py:436`; `_pipeline_multi_table.py:305`; `_pipeline_routing_signals.py:194`; `_transforms.py:87`; `_unified_slice.py:256`, `:360`; `_when_gate.py:155`, `:173`; `execution/out_of_core/_capacity_eval.py:517`; `_spill_estimate.py:614`;
- generation: `generation/_fidelity_gate.py:109`, `:183`; `generators/_distribution.py:59`, `:84`, `:128-146`, `:251-261`, `:322-381`; `generators/_formula.py:104-110`, `:140`, `:187-193`, `:250`; `generators/columns.py:124`, `:185-191`, `:236-244`, `:272-307`, `:359-399`, `:447-530`, `:632-647`;
- internal, profile, and profiling: `internal/crypto.py:30`; `internal/faker_setup.py:221-281`, `:631`; `internal/logging.py:129-247`; `internal/memory.py:39-54`; `profile/_pii.py:80`; `profile/_source.py:112`, `:195`; `profiling/model_pack/classify.py:178`; `model_pack/loader.py:161-164`, `:295-331`; `model_pack/provenance.py:254-261`; `profiling/profiler.py:491`, `:566-625`;
- quality, reference, quarantine, and transforms: `quality/_freetext_advisory.py:245`; `quality/_retention_gate.py:140`; `quality/dp_policy.py:85`; `quarantine.py:498`; `reference_tables/_loader.py:127-138`; `transforms/_codeset_loader.py:442`; `_text_mask_splice.py:58`; `base.py:93-112`; `bucket_perturb.py:161`; `date_shift.py:92`, `:166-190`, `:246`; `formula.py:42`; `geo_generalize.py:281`, `:401`; `text_mask.py:385`.

`reasons/warning/_part1.py:27` also matches the mechanical expression because it declares a reason named `warning.error`; it is registry data, not a logging call. Slice 1 turns this census into a parsed AST manifest, classifies each call as safe, unsafe, or registry false positive, and requires the unsafe count to reach zero. File, stream, warning, traceback, subprocess, callback, evidence, audit, telemetry, and tracker emitters are inventoried separately in the table because a logger-only search cannot find them.

## Defect-class closure table

For class 1, this F31 plan is itself the closing structure: the engine-owned boundary, not a later local patch, defines which values can exist on ordinary output.

| # | Catalog class | Applies or N/A | Closing structure | Proving test | Required mutation |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Applies | This plan's exact-type `SafeEvent` boundary, provenance registry, aliases, sealed serializer, and process-entry adapters | Seed every enumerated field and surface, first prove a registered control event arrived, then assert marker absence | Remove one factory or entry boundary and require the surface scan to turn red |
| 2 | Cleanup missing on an exit path | Applies | One `SafeOutputScope` owner for reversible handlers, hooks, streams, and child descriptors, with fixed emergency reporting | Exit-path matrix covers success, invalid input, sink failure, timeout, cancellation, exception, nested scope, and double close | Omit one restore or child-descriptor close and require leaked state or bytes to fail |
| 3 | State machine and concurrency | Applies | Locked provenance and alias registries, identity-checked weak cleanup, distinct copied contexts, and a coordinator discovery barrier | Barrier-forced weakref reuse, late callback, task, thread, and alias-remap interleavings | Remove a lock, identity check, copied context, or remap barrier and require nondeterminism or cross-context output |
| 4 | Route parity and silent fallback | Applies | One route and surface manifest plus one serializer for full-frame, sequential, mixed, chunked, out-of-core, and isolated routes | Run the hostile corpus on every enumerated route and assert a registered route event before absence | Bypass the boundary on one route or suppress its reason and require the route matrix to fail |
| 5 | Referential integrity | N/A: F31 changes representation of FK reports, not masking, ordering, orphan, or publication semantics | Preserve `RelationshipGraph` and route behavior unchanged | Existing relational suites plus the FK-shape output matrix remain green | N/A: no FK behavior is mutated; a changed relational result is a regression |
| 6 | Untrusted producer | Applies | Exact types, bounded parsers, fixed reason reconstruction, smallest-catch conversion, and child FD containment | Field walk uses hostile subclasses, mappings, exceptions, frames, extension writes, and child streams | Re-enable `str`, `repr`, dynamic `__name__`, unbounded mapping iteration, or inherited FD output and require red |
| 7 | Check then use on a path | Applies to the unsafe diagnostic file and exact data-plane writer manifest | Open-once file or FD capability, no-follow exclusive creation, 0600 mode, and exact writer allowlist | Symlink-swap and path-substitution tests plus AST seeded evasions | Reopen a bare path or widen the writer exemption and require the sentry or isolation test to fail |
| 8 | Weak tests and overclaimed evidence | Applies | RED artifacts, non-vacuous recorders, same-environment baselines, mutation evidence, and exact-commit receipts | Reviewer reruns every recorder and sentry self-test with `uv.lock` and checks each expected control | Disconnect a recorder, empty a sink, or weaken a bound and require the positive control or mutation gate to fail |
| 9 | Behavior-preserving refactor drift | Applies | Characterization snapshots first, compatibility projections from one serializer, topology gates, and one integration branch | Preserve ordering, payload keys, OpenAPI, CLI modes, and route results while replacing only output authorization | Change one field, order, route result, or moved-file sentry scope per slice and require a characterization failure |
| 10 | Frozen contract drift | Applies | The additive/breaking list below, schema snapshots, exact engine pins, and explicit 0.8.0 break declaration | Consumer fixtures and schema snapshots compare exact keys, enum members, versions, and public exports | Add, remove, or rename a frozen field without its declared version change and require snapshot failure |

F25 note: its current plan has Slices 0 through 6 and no Slice 4b or 4c (`docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md:226-286`). F31 does not fabricate that amendment. When it is supplied, the contract must be applied to Slice 4c and to every Slice 4b behavior the amendment touches in the F25 document itself; this F31 table cannot stand in for it.

## Enumerations from the baseline code

These are implementation checklists. A slice updates the relevant row when it removes a current carrier; it cannot silently narrow the list.

### Exit paths

- Transactional output has no-write, write failure, commit success, commit failure, and abort cleanup paths at `src/decoy_engine/execution/_transactional_sink.py:58-97` and `src/decoy_engine/execution/_transactional_sink.py:324-380`; streamed output adds exception, commit failure, and normal-without-commit exits at `src/decoy_engine/execution/_chunked_output_sink.py:128-175`.
- Isolated execution has pre-spawn refusal, in-process failure and commit failure, owned work-root cleanup, timeout kill, callback failure, abnormal exit, self-reported failure, and commit failure at `src/decoy_engine/execution/_isolated_run.py:194-243`, `src/decoy_engine/execution/_isolated_run.py:258-319`, `src/decoy_engine/execution/_isolated_run.py:322-365`, and `src/decoy_engine/execution/_isolated_run.py:437-573`.
- Progress has no sink, successful delivery, replacement or sink exception, phase error and completion, and terminal success or error emission paths at `src/decoy_engine/progress.py:162-197`, `src/decoy_engine/progress.py:243-266`, and `src/decoy_engine/progress.py:428-484`; F31 adds invalid construction, serializer failure, driver failure, nested scope, cancellation, and process-hook re-entry to this matrix.

### Routes

- Relationship jobs select `out_of_core`, `sequential`, `mixed`, `full_frame`, or reject before read; explicit modes fail closed rather than silently falling back (`src/decoy_engine/execution/_pipeline_routing.py:13-85`, `src/decoy_engine/execution/_pipeline_routing.py:300-321`).
- Non-relationship work also has per-table full-frame or chunked dispatch, while independent multi-table jobs split tables only when no FK edge touches the job (`src/decoy_engine/execution/_pipeline_routing.py:68-85`, `src/decoy_engine/execution/_pipeline_multi_table.py:1-20`).
- Every route runs either in-process or through the isolated subprocess wrapper; re-entry intentionally degrades to in-process and must still remain inside the same boundary (`src/decoy_engine/execution/_isolated_run.py:205-243`).

### FK shapes

- Single and composite keys are the same edge shape, distinguished by tuple arity; parent and child arity must match (`src/decoy_engine/relationships/_graph.py:71-92`).
- Self-references remain routable, cross-table cycles cannot take a bounded ordered route, and multi-parent child tuples use declared-order first-hit-wins with one orphan policy (`src/decoy_engine/execution/_pipeline_routing.py:408-414`, `src/decoy_engine/relationships/_graph.py:163-180`).
- Children may arrive in later streamed batches: each raw batch is staged with global row numbers, then the driver aligns staged child keys and payload batches through the bounded join (`src/decoy_engine/execution/out_of_core/_stream_join.py:244-281`, `src/decoy_engine/execution/out_of_core/_stream_driver.py:317-432`). Multi-parent relations are merged before the unchanged child join (`src/decoy_engine/execution/out_of_core/_multi_parent.py:49-103`). F31 aliases these identities in output but does not alter their values or orphan decisions.

### Fields

- The public reason carriers are all `ReasonContext` fields at `src/decoy_engine/reasons/_record.py:114-126` and all `ReasonRecord` fields at `src/decoy_engine/reasons/_record.py:241-254`; F31 retains identity and closed numeric facts but replaces source refs with provenance-backed aliases.
- The complete progress field catalog is the seven dataclasses at `src/decoy_engine/_progress_events.py:273-425`, their exact legacy keys at `src/decoy_engine/_progress_events.py:430-457`, and their v1 additions at `src/decoy_engine/_progress_events.py:458-477`.
- The result carriers touched by Slice 4 and Slice 5 are `ExecutionResult` at `src/decoy_engine/execution/_adapter.py:42-71`, `IsolatedRunResult` at `src/decoy_engine/execution/_isolated_common.py:320-388`, `RowError` and `RowErrorRecord` at `src/decoy_engine/execution/_row_errors.py:28-59`, and `RowErrorSummary` at `src/decoy_engine/execution/_row_error_summary.py:37-86`.
- Validation output carries `severity`, `code`, `message`, `path`, `node_id`, `hint`, errors, warnings, and normalized config at `src/decoy_engine/validation_result.py:47-92`; no free-text or normalized-config field crosses an ordinary F31 projection.
- The other root-public results are connector `CheckResult` and `WriteResult` (`src/decoy_engine/sdk.py:111-131`), `DiscoveryResult` (`src/decoy_engine/data_discovery.py:77-88`), `PlanCheckError` and `PlanValidationResult` (`src/decoy_engine/plan/validate/_consolidator.py:36-65`), and `UnmaskColumnReport` and `UnmaskResult` (`src/decoy_engine/unmask.py:76-94`). Their free detail, messages, names, and paths require fixed reasons or aliases; discovery rows, Arrow tables, attached plans, and path references are data-plane carriers and may not cross an ordinary projection.
- The streamed-generation subpackage returns `GenerationResult(mode, reason, row_counts, tables, peak_reserved_bytes, declines)` at `src/decoy_engine/generation/_streamed.py:71-88`; resident `tables` is data-plane only, while ordinary status uses registered reason and decline identities plus closed numeric facts.
- The public subset package returns `SubsetResult(plan, manifest, output_paths)` at `src/decoy_engine/subset/_types.py:217-223`; the plan, manifest, and output path references stay on the data plane, with only an aliased typed completion event on ordinary output.
- The public walks package returns `WalkResult(snapshot_summary, edges, hazards)`, `DriftResult(added_tables, removed_tables, changed_columns, new_pii)`, and `CrossFileWalkResult(snapshot_summary, edges)` at `src/decoy_engine/walks/types.py:97-124` and `src/decoy_engine/walks/cross_file.py:39-49`. Their schema identities, descriptions, and open dictionaries are value-bearing; ordinary views use aliases, closed hazard/change codes, and bounded counts rather than serializing these result objects directly.

### Output surfaces

- Callback, versioned dictionary, legacy dictionary, and structured-logger bridges are separate current surfaces (`src/decoy_engine/progress.py:67-127`, `src/decoy_engine/_progress_events.py:581-599`).
- Stdlib console and rotating-file handlers, fallback warnings, and narrative progress are current text/file surfaces (`src/decoy_engine/internal/logging.py:55-136`, `src/decoy_engine/internal/logging.py:139-247`).
- Caller logger methods, structured callbacks, telemetry mappings, captured outputs, and per-node export mappings are current application surfaces (`src/decoy_engine/context.py:22-83`, `src/decoy_engine/context.py:97-231`, `src/decoy_engine/context.py:242-275`).
- In-process results, isolated envelopes, subprocess stdout/stderr, row-error summaries, and validation records are current object/bytes/error surfaces (`src/decoy_engine/execution/_adapter.py:42-71`, `src/decoy_engine/execution/_isolated_run.py:430-530`, `src/decoy_engine/execution/_row_error_summary.py:55-87`, `src/decoy_engine/validation_result.py:47-92`). CLI and Platform surface cells remain the explicit cross-repository matrix under Acceptance tests and are topology-gated before their slices.

## Reuse inventory

| Existing capability | Verified source | Required reuse and reason new machinery remains necessary |
|---|---|---|
| F29 reason registry and closed records | `src/decoy_engine/reasons/_registry.py:24-44`; `src/decoy_engine/reasons/_record.py:241-366` | Reuse identities, fixed prose, semantics, and compatibility fallback. New provenance is needed because a valid reason alone does not authorize an enclosing output object. |
| Exact exception classification | `src/decoy_engine/reason_adapter.py:38-100`, `src/decoy_engine/reason_adapter.py:146-189` | Reuse reason selection. Add exact closed type labels and traceback disposal because the adapter does not own ambient exception state. |
| F29 event variants and projections | `src/decoy_engine/_progress_events.py:273-477`, `src/decoy_engine/_progress_events.py:581-599` | Preserve public compatibility through projections. Replace mutable dictionaries as authorized carriers with one sealed serializer. |
| Progress binding and sink isolation | `src/decoy_engine/progress.py:162-197` | Reuse sequencing and failure counting. Add provenance checks and typed final drivers because the current sink accepts any event instance. |
| Log interpolation sentry | `tests/sentry/test_log_interpolation.py:20-83`, `tests/sentry/test_log_interpolation.py:330-443` | Generalize its AST and mutation-fixture pattern. New rules are needed for non-log, framework, persistence, reflection, and cross-helper sinks. |
| Module-size sentry | `tests/sentry/test_module_size.py:54-66` | Reuse the repository's exact-LOC ratchet and 600/700 limits unchanged. F31 adds the stricter plan-local rule that every new or expanded F31 Python module stays below 450 physical lines; neither threshold may be widened to pass. |
| Physical-seam disconnection sentry | `tests/sentry/test_physical_seam_disconnection.py:156-203` | N/A to physical-plan wiring because F31 adds no such connection, so preserve this sentry unchanged. Reuse its repository-wide scan plus stale-allowlist failure pattern for the exact safe-output analyzer manifest. |
| Existing hostile security corpus | `tests/security/test_reason_boundary.py:44-79`, `tests/security/test_reason_boundary.py:107-195`; `tests/security/test_f29_remediation.py:41-119` | Retain and expand it across the enumerated surfaces; never replace these assertions with a weaker aggregate scan. |
| Publication and cleanup tests | `tests/unit/execution/test_transactional_sink.py:105-151`; `tests/unit/execution/test_isolated_run.py:390-420`, `tests/unit/execution/test_isolated_run.py:620-655` | Reuse their exit-path and non-vacuity patterns when output scope and child descriptors are added. |

## Frozen contracts touched

| Contract | Change classification and proof |
|---|---|
| `decoy-engine` public package, currently 0.7.0 | Additive public `safe_output` names plus intentional pre-GA 0.8.0 behavior break for unsafe legacy forwarding; current version and exports are at `pyproject.toml:5-13` and `src/decoy_engine/__init__.py:308-329`. |
| Reason registry `1.0.0`, reason schema 1, event schema 1 | Add reason entries only, never remove or redefine; constants and duplicate rejection are at `src/decoy_engine/reasons/_registry.py:24-44`. |
| Progress v0/v1 keys and positional constructors | Frozen compatibility projections from the sealed canonical event; exact snapshots and constructor checks are at `tests/unit/progress/test_versioned_contract.py:37-78` and `tests/unit/progress/test_versioned_contract.py:98-117`, `tests/unit/progress/test_versioned_contract.py:164-180`. |
| `ReasonRecord` unknown-version and unknown-identity behavior | Additive only; future identities and unsupported majors retain their fixed degradation rules at `tests/security/test_reason_boundary.py:172-195`. |
| `ExecutionResult`, `IsolatedRunResult`, row-error, and validation Python fields | Breaking ordinary serialization, unchanged computation and in-process data-plane access; field locations are enumerated above and compatibility adapters must snapshot the prior public shape. |
| CLI and Platform JSON/OpenAPI/audit/evidence schemas | Frozen at each consumer topology gate. F31 changes the producer representation, not published keys; Slice 6 and Slice 7 must capture exact consumer fixtures before replacement and use an exact engine 0.8.0 pin. |

## Pre-mortem: five likely gate findings

| Likely gate finding | Catalog class | Prevention and RED-first witness |
|---|---|---|
| One output surface is missing, most likely pre-command CLI help/parse failure or Platform framework serialization | 1, 4 | The output-surface manifest is executable and each cell requires a positive registered event before marker absence; delete one surface adapter and its seeded marker must escape. |
| A legacy helper still returns or enriches a mutable dictionary after validation | 1, 6, 9 | Exact provenance and non-mapping payload tests cover every helper edge; mutate `event_to_dict`, telemetry, or an evidence helper to append a marker and require emit-time rejection. |
| Dynamic exception class text, traceback state, or a child stream escapes through a rare failure path | 1, 2, 6 | Exact type lookup, smallest-catch disposal, FD redirection, and the exit-path matrix use hostile `__str__`, subclass, active-frame, timeout, and abnormal-exit cases; re-enable one read or inherited stream and require red. |
| Scoped output corrupts host state or loses containment across a task, thread, cancellation, or nested scope | 2, 3 | Exact-object restoration and barrier-forced copied-context tests cover all pairs; bypass one wrapper or restore the wrong stream and require identity or marker failure. |
| The shared analyzer passes while a moved file, alias, reflection spelling, implicit response, or widened exemption bypasses it | 7, 8, 9 | Repository manifests contain exact functions, not directories, and self-tests seed every evasion in all three repositories; move a fixture or widen one exemption and require the analyzer to report the expected file and rule. |

## Design

### Ownership, layout, public API, and compatibility

The engine owns the contract because it is the lowest shared dependency and already owns F29 reasons. Add:

```text
src/decoy_engine/safe_output/
  __init__.py       public imports and schema constants
  _model.py         closed value types; no public or class-held minting token
  _provenance.py    closure-owned mint and verification functions
  _registry.py      safe-output reason and prose declarations via F29 registry
  _aliases.py       bounded keyed-digest ordinal alias allocator
  _exception.py     exception to SafeEvent conversion
  _scope.py         ambient logging, warning, and process-hook containment
  _streams.py       ContextVar proxy streams and child FD policy
  _serialize.py     canonical sealing and emergency literal
  _sink.py          SafeEventSink protocol and adapters
  _testing.py       shared analyzer entrypoint; no production sink capability
  families/
    progress.py     progress and F29 compatibility factories
    runtime.py      runtime, validation, and exception factories
    quality.py      quality, fidelity, and diagnostic factories
    evidence.py     evidence, audit, publication, and support factories
    ci.py           F43 finding factories
```

Every Python module added or materially expanded by F31 has a hard plan budget below 450 physical lines, including tests for the existing repository size sentry. Factories live in the domain modules under `families/`; F31 does not add factories to `_progress_events.py` or turn `__init__.py` into an implementation module. If a module approaches 400 lines during a slice, split it by event family before review rather than spending the repository's 600/700 line limit.

The supported imports are `decoy_engine.safe_output.SafeEvent`, `SafeLocation`, `SerializedSafePayload`, `SafeEventSink`, `SafeOutputScope`, and `safe_event_from_exception`, plus named family factories such as `connection_failed` whose signatures contain only typed fields. `serialize_safe_event` remains as a compatibility spelling but returns only `SerializedSafePayload`, never a `dict`; new code calls `SafeEventSink.emit(event)` and lets the sink seal internally. Re-export the supported public names from `decoy_engine` for the 0.8 line without moving implementation into the root module. There is no generic public factory that accepts arbitrary metadata. Internal constructors, provenance registration, opaque-ID minting, payload unsealing, and registry mutation are not public.

This is an additive public API with an explicitly coordinated pre-GA behavioral break for unsafe compatibility methods. Bump engine 0.7.0 to 0.8.0. The wire schema is `decoy-safe-output/v1`. `decoy-reasons/v1` stays readable and is nested canonically. Existing progress v1 and legacy wire shapes are projections produced only by registered sink drivers inside `_serialize.py`; they cannot maintain independent validation or prose and are never returned as authorization-bearing application dictionaries.

`Logger`, `StructuredEvents`, `TelemetryClient.emit`, `event_to_dict`, and `event_to_legacy_dict` are neutered in the first 0.8.0 release, not merely deprecated. Their import and call signatures may remain temporarily, but they discard every free-form string, mapping, `error_msg`, and extra argument without inspecting or forwarding it. Logger and callback methods emit only a fixed `ambient.output_withheld` event through `SafeEventSink`. The two `event_to_*` names accept only provenance-backed events and return a sealed payload for the requested legacy wire projection; raw dictionaries, legacy objects with free fields, and forged events yield the sealed `safe_output.invalid` payload. Removal before 1.0 remains planned, but there is no raw compatibility lane in 0.8.0.

F29 gains `ordinary_output: bool = False`. The default applies to every existing reason and every new declaration, so no pre-F31 reason becomes output-safe implicitly. Slice 0 creates an explicit reviewed compatibility manifest of existing reason identities that may be projected after every name-bearing context value has become a `SafeAlias`; all other existing reasons remain internal and map to `safe_output.invalid` at the ordinary boundary. Registry tests assert both the false default and the exact opted-in set.

CLI and Platform each pin exactly `decoy-engine==0.8.0`, including lockfiles and deploy images. No compatible range is acceptable for the first contract release. Their work can land separately after the engine release.

### The single model

`SafeEvent` is an exact, frozen, slotted type with these fields and no extension dictionary. `SafeEvent`, every `OpaqueId` variant, and `SerializedSafePayload` explicitly include `__weakref__` in their slots; the implementation does not rely on `dataclass(slots=True)` implicitly supplying weak-reference support on every supported Python version. Their apparent Python immutability is not the security proof. `_provenance.py` creates a closure-held capability object and a closure-held identity registry at module initialization. The capability is never placed on a class, instance, module export, default argument, or spellable `str` subclass. Named factories validate fields, construct exact instances, and register `id(obj)` with a canonical field digest and `weakref.ref(obj, cleanup)`. The cleanup captures only the integer ID and removes the entry only when the stored weak reference is the callback's reference, so a late callback cannot delete a registration for a reused address. The serializer verifies exact object identity through the live weak reference, the registry entry, and the current canonical digest. An instance made with a public constructor, `object.__new__`, copying, subclassing, dataclass replacement, mutation through `object.__setattr__`, or a reused stale ID has no matching registration and fails closed. The registry does not use `WeakKeyDictionary`, whose equality behavior is unnecessary for an identity capability.

- `schema_version`: literal `decoy-safe-output/v1`.
- `reason`: a validated F29 `ReasonRecord` whose registry entry is marked `ordinary_output=True`.
- `kind`, `severity`, `phase`, `operation`, and `outcome`: enums from reviewed closed sets.
- `location`: optional frozen `SafeLocation(component, line, column, row, chunk, ordinal)`. `component` is a registry enum such as `connector`, `reader`, `validator`, or `publisher`. There is no filename or path.
- `error_type`: optional `SafeErrorType`, constructed from an exact builtin or engine allowlist. Unknown and extension types become `Exception`.
- `counts`: tuple of frozen `SafeCount(name_enum, value)` entries, with unique names and `0 <= value <= 2**63 - 1`.
- `measurements`: tuple of frozen `SafeMeasurement(name_enum, finite_value, unit_enum)` entries with field-specific ranges. Scores and rates are `[0, 1]`; durations, byte counts, and throughput have documented nonnegative caps.
- `aliases`: tuple of `SafeAlias(kind_enum, ordinal)`, serialized as `table-N`, `field-N`, `node-N`, or `artifact-N`. The original string is not stored.
- `opaque_ids`: tuple of dedicated exact ID variants. The same closure-held provenance service mints and registers them only from an engine-owned or server-owned correlation-ID source at its creation seam. It never reconstructs one from serialized text. Exact UUID or ULID syntax and a cap are additional checks, not proof of provenance. A UUID-shaped customer primary key, secret reference, `str` subclass, or object built through `__new__` is unregistered and cannot become an opaque ID. Other identifiers receive an ordinal alias. There is no generic token constructor.
- `flags`: tuple of closed flag enums. Absence and false are distinct only where the schema requires it.

Every field is validated before provenance registration, input sequences are copied to tuples, booleans as integers are rejected, names are unique, enums are exact, collection lengths are capped, and arbitrary subclasses are refused where exact types matter. Named factories accept untrusted source objects, extract only permitted facts, catch fixed construction errors, and return either the intended registered event or the registered `safe_output.invalid` event. They never return the source dictionary after checking it. Direct construction is deliberately not a supported route for typed consumers. The fixed programming error never echoes input.

`SafeAliasAllocator` is per-run and receives a 32-byte ephemeral key from `os.urandom` when the root `SafeOutputScope` is established. It stores only keyed BLAKE2b digests mapped to ordinals, never the source identifier. Each alias kind is capped at 4096 distinct digests. Further inputs produce the fixed ordinal `kind-0` plus the closed `alias_overflow` flag without retaining another digest. A lock protects lookup and allocation. The coordinator pre-registers identifiers in canonical plan order before thread or process fan-out, and workers receive already assigned typed ordinals; first scheduler arrival never determines an alias.

Dynamic schema discovery uses a coordinator barrier. Workers do not allocate globally visible aliases and cannot emit an ordinary event that refers to an unresolved identifier. They return discoveries plus stable source-position metadata through the private data-plane envelope. The coordinator computes keyed digests, deduplicates, canonical-sorts by alias kind, stable source position, and digest, assigns ordinals once, remaps worker-local handles, and only then releases related `SafeEvent` values. A streaming path that cannot supply a complete deterministic discovery barrier uses the fixed overflow alias or fails closed; it never allocates from a worker-local counter. All sinks for one run share that allocator, so aliases are deterministic across surfaces and execution schedules within the run and deliberately unlinkable across runs.

F29 `ReasonRecord` remains the reason authority. Add fixed `safe_output.invalid`, `safe_output.failed`, `runtime.unexpected`, `connection.failed`, `diagnostic.withheld`, and `ambient.output_withheld` entries only after the registry compatibility test approves the exact catalog. All human prose is read from that registry. Consumer-specific fixed messages become registry entries owned by their namespace, not local dictionaries.

### One serializer and fail-closed behavior

`serialize_safe_event(event, projection=SAFE_OUTPUT_V1)` is the compatibility entry to the one internal sealing operation. It:

1. checks exact supported type, schema, and the closure-held provenance registration for the event and every provenance-bearing nested object;
2. recomputes the canonical field digest and rejects any post-construction mutation without reconstructing an `OpaqueId` from its textual value;
3. resolves prose from the registered reason, never from an event field;
4. builds the closed-key canonical representation inside the serializer closure;
5. applies length and control-character pattern handling only to already-approved bounded tokens as defense in depth;
6. registers and returns an exact opaque `SerializedSafePayload` whose internals expose no mapping protocol, iteration, mutation, `__dict__`, or general unseal method.

Only `_sink.py` receives a closure-bound driver function that can consume a registered payload and write canonical bytes directly to an already selected destination. Drivers cannot return the intermediate mapping to application code. Text, JSON, Rich, log, audit, evidence, telemetry, diagnostic, support, tracker, and framework response adapters accept `SafeEvent` or `SerializedSafePayload` and select a registered projection enum. They cannot accept a plain mapping, append extras, or combine the source event with arbitrary metadata. Registry prose is the only text renderer input.

This capability placement is an API confinement and review aid, not a claim that a secret can be hidden from arbitrary same-process Python. The shared analyzer rejects production uses of `gc`, `inspect`, `ctypes`, `sys._getframe`, frame attributes, and `__closure__` or `__code__` inspection outside the exact reviewed `_provenance.py` and `_exception.py` operations and the single input-free C stdio flush in `_child_fd_bootstrap.py`. Those rules prevent accidental or opportunistic reach-through in reviewed code. They do not protect against a debugger, hostile native extension, memory inspection, or deliberately malicious code with arbitrary execution in the process; that threat requires an isolated worker.

An unknown enum, schema, registry identity, projection, provenance identity, canonical digest, or invalid numeric value causes sealing to discard the entire event and seal a fresh registered `safe_output.invalid` event. If that construction or encoding fails, the boundary writes the constant ASCII emergency record for `safe_output.failed` through a literal-only emergency driver. It does not call `str`, `repr`, a logger, a warning, the rejected sink, or a general JSON `default` callback. Sink failure is counted in memory if possible and otherwise silent. It never invokes raw fallback logging.

F29 `event_to_dict` and `event_to_legacy_dict` are the neutered compatibility spellings described above, despite their historical names. `ReasonRecord.to_payload` remains an internal nested reason primitive and is not an ordinary sink authorization. STORM phase envelopes, if merged, become typed event families. Their current generic string and frozen mapping payloads are not permitted unchanged.

### Exception boundary

Catch untrusted extension, provider, connector, validator, reader, and driver exceptions in the smallest engine-owned frame. `safe_event_from_exception(exc, reason, location)` does only the following while the exception is live:

- select a reason through the existing `reason_adapter` without reading arbitrary message, `args`, `repr`, extension `code`, or attributes;
- perform only `_ALLOWED_EXCEPTION_TYPES.get(type(exc), SafeErrorType.EXCEPTION)`, where the dictionary keys are exact reviewed exception classes and the values are closed `SafeErrorType` enum members; never read, compare, log, or serialize `type(exc).__name__`;
- attach a caller-supplied closed component and numeric position, not a traceback-derived filename or source line;
- build the event and return it without storing `exc`.

The catch wrapper itself binds no row, raw buffer, credential, path, or extension arguments. Where a current parser catches inside a frame that owns such locals, refactor it into an inner worker and an outer boundary wrapper so the data-owning frame has unwound before conversion. While the outer `except` frame is executing, the implementation never calls `traceback.clear_frames(exc.__traceback__)`, because that traceback includes the executing catch frame. It may clear only `tb.tb_next` chains whose inner frames are known to have unwound, then assigns `exc.__traceback__ = None`. For a bounded cause and context chain it repeats that rule, severs each traceback and link without formatting, and stops cycles or overlong chains by severing the remaining link. If a traceback topology cannot prove a frame is inactive, it skips `clear_frames` and detaches the traceback instead. Frame clearing is memory hygiene, not an output-safety dependency.

Only exact engine-owned exception families may expose a registered code, and the existing adapter determines trust. An extension subclass, arbitrary provider error, or lookalike `code` attribute is untrusted and maps to `runtime.unexpected` or a fixed family reason.

If an engine error must propagate, create a new closed `SafeBoundaryError` outside the original `except` block. Set `__cause__ = None`, `__context__ = None`, `__suppress_context__ = True`, and use `with_traceback(None)` before it crosses an export seam. Do not attach the original exception or traceback. Ordinary exporters do not call `traceback.format_exc`, `traceback.print_exception`, `TracebackException`, Rich `print_exception`, `logger.exception`, or `exc_info=True`. Any diagnostic-only stack extraction uses static registered component codes; it does not keep frame objects.

No ordinary path enables `capture_locals`. Tests monkeypatch traceback helpers and third-party tracker hooks to assert `capture_locals` is absent or false and `chain` is false. Fixed-width tests place raw bytes and rows in the failing frame and prove neither locals nor representations survive. The existing cloud credential tests remain intact.

### Third-party and ambient containment

`SafeOutputScope` is a context manager around engine calls and each untrusted extension call. Importing `decoy_engine` and making an ordinary library call install no permanent stream proxy and no process exception hook. The explicit `install_process_safe_output()` entry adapter is reserved for the CLI, Platform server or worker, and isolated-worker startup. It selects process-owned mode for that process lifetime. Direct library callers instead get scoped embedding mode. Both modes avoid holding a process-wide lock across extension code or an `await`.

In scoped embedding mode, `SafeOutputScope` snapshots and restores, in `finally`, logger handlers, filters, levels, disabled state, propagation, `warnings.showwarning`, warning filters, the exact current `sys.stdout` and `sys.stderr` objects, and context tokens. On entry it temporarily binds proxies around the streams that are current at that moment; on exit it restores those exact objects if ownership has not been violated. This cooperates with `pytest` `capsys` and with hosts that replace streams between calls. The scope never installs or changes `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook`, or an async loop exception handler. Concurrent host mutation of a stream or hook during an active scope is outside the cooperative embedding contract and causes a fixed failure rather than a guessed restoration.

- Python logging receives an allowlisted handler whose `emit` accepts only a `SafeEventLogRecord` created by `_sink.py`. All other records are withheld and counted by logger name category only. It never formats `record.msg`, `args`, `exc_info`, pathname, function, or stack info.
- The default engine rotating file handler is removed. Library import installs `NullHandler` only. Applications may attach the safe handler explicitly. There is no import-time `logs/decoy_engine.log` creation.
- `warnings.catch_warnings(record=True)` captures extension warnings. It reports only a count and a closed category if allowlisted; it never formats warning text, filename, or source.
- In process-owned mode, the explicit entry adapter installs one permanent proxy object on each Python stream before parser, framework, extension, or worker code. The default for an unscoped thread is discard, not delegation. Only the closure-held final-driver capability can reach the bound underlying stream. Entry and every command boundary verify that both owned proxies are still installed; unexpected replacement fails closed. Nested scopes reset context tokens in LIFO order under success, exception, cancellation, and concurrent task switching.
- In scoped embedding mode, output from unrelated host threads continues to use the host stream. Every engine-owned `threading.Thread` target and `ThreadPoolExecutor.submit` callable is registered as engine-owned and invoked through a distinct `copy_context().run(...)`; a `Context` object is never entered concurrently by two workers. Tests fail if an internal pool or thread bypasses that wrapper. A foreign driver or C-created background thread cannot be classified reliably while unrelated host output is preserved, so a driver with such behavior must run in process-owned or isolated mode.
- Decoy-owned isolated children perform descriptor bootstrap before any extension or untrusted callback import. The parent provides a dedicated structured transport rather than duplicating stdout. `_child_fd_bootstrap.py` owns a fixed, input-free `flush_c_stdio()` boundary: on POSIX it may call only `ctypes.CDLL(None).fflush(None)`; on Windows it may call only the supported CRT `_flushall()` selected by a static platform adapter. The analyzer manifest admits those exact symbols and call shapes in that module and no other `ctypes` use. The child flushes inherited known-safe Python streams and calls that helper; an unavailable runtime symbol or platform-adapter-reported flush failure aborts with a fixed code and has no fallback. It then disables `faulthandler` and scheduled fault dumps, marks the transport descriptor non-inheritable with `os.set_inheritable(fd, False)`, and registers an `os.register_at_fork(after_in_child=...)` callback where available that closes the transport immediately in a Python `os.fork` child. It then redirects operating-system descriptors 1 and 2 with `os.dup2` to `/dev/null` or a private bounded pipe. C standard streams initialized after the boundary therefore target the redirected descriptors. Structured payloads travel only over the dedicated descriptor or private 0600 file. Parent reads are bounded and timed out. Exec-based grandchildren lose the transport through close-on-exec; Python `os.fork` grandchildren lose it through the at-fork close callback. Return code and closed child role may become fields; stream content never does.
- Root CLI and worker initialization install safe `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook`, and async loop exception handlers only through `install_process_safe_output()`. They do not format the exception, traceback, thread, task, or unraisable object and emit only a fixed `runtime.unexpected` or literal emergency event. They remain for the explicitly owned process lifetime. Library import and `SafeOutputScope` never install them.
- Driver and library callbacks are adapted inside the scope. Third-party Sentry or OpenTelemetry exporters receive only a `SafeEvent` or sealed payload. Tracker breadcrumbs, request bodies, locals, raw exceptions, and default integrations that capture them are disabled.

Containment failure emits only `safe_output.failed` through the emergency literal path. State restoration errors are never formatted. Scoped embedding cannot both preserve unrelated host-thread output and classify arbitrary threads created inside a third-party C driver. Native code may also open `/dev/tty`, a separate file or socket, retain a duplicated pre-bootstrap descriptor, or call `fork()` without Python's at-fork callbacks. Those cases are outside Python stream and FD 1 or 2 containment and require process isolation or a separately reviewed connector policy. Deterministic garbage-collection timing across Python implementations is also not claimed; acceptance requires eventual registry eviction after collection, not a particular collection schedule.

### CLI process-entry boundary

The CLI wraps the entire process entry before invoking Typer or Click, not individual commands. `main()` explicitly calls `install_process_safe_output()` to establish the permanent proxy streams, process hooks, alias scope, and `SafeOutputScope`, then invokes the root application with `standalone_mode=False` or an equivalent custom `Command.main`. No import or reusable engine API calls this installer. Root callbacks, eager options, `--help`, parameter conversion, usage construction, and command dispatch therefore all execute inside the boundary.

The process-entry adapter catches exact Click and Typer control-flow types including `ClickException`, `UsageError`, `BadParameter`, `Abort`, and their explicitly reviewed exact builtins. It does not call their `format_message`, `show`, `str`, `repr`, or framework default stderr path. It maps them to fixed registered usage events and a closed exit code. Help text is static application-authored text rendered through a registered help driver; option defaults, environment values, argument values, parameter hints derived from a value, and offending tokens are never echoed. The root callback and every early-exit path share this entry wrapper.

### Diagnostics, verbose mode, and unsafe opt-in

`--verbose`, debug logging, support bundles, administrator access, and customer diagnostic privileges change event selection or add bounded counts, but never change the field policy. A verbose connection failure can add `error_type=TimeoutError` and `component=connector`; it cannot add the URL. A fixed-width failure can add a row ordinal; it cannot add a path, layout, row buffer, local, or traceback.

The one unsafe channel is an explicit product feature, not a fallback. The CLI flag and any Platform equivalent must be independently approved, use a caller-selected new file with mode 0600, display a fixed sensitivity warning before creation, refuse stdout and stderr targets, refuse symlinks and preexisting files, and never auto-upload. Ordinary support bundles contain only safe events and a manifest stating whether an unsafe file exists, never its path or contents.

### Cross-repository adoption and STORM/F43 convergence

STORM reframe on `claude/eager-clarke-14deh1` and F43 on `feat/f43-ci-integration` are structural inputs, not timing assumptions. Before Slices 6 and 7, record one deterministic topology choice per consumer. If the relevant branch is merged, rebase the slice on that merge. If it is not merged, base the integration branch directly on the reviewed STORM or F43 commit, or cherry-pick the exact structural commits that introduce `_value_free.py`, `_boundary.py`, `_projections.py`, `fidelity_projection.py`, their tests, and `SafeFinding`, then replace the local policy before landing one unified migration. Do not implement a second main-based version and do not wait for a failed predecessor gate. If a cited branch or exact commit is unavailable remotely, stop the consumer slice and ask the owner for the source artifact rather than inventing its shape.

The fourth STORM repository, `decoy-web`, was not present in this planning workspace, and the remote implementation handoff exposes engine, CLI, and Platform only, so F31 does not invent a web prerequisite. Engine, CLI, and Platform contain the value policy and require code changes. The web client requires contract verification against the unchanged Platform response shapes; it needs a separate slice only if that verification finds a schema consumer change.

The migration targets are explicit:

- STORM CLI `_value_free.py` migrates its local model, registry, exception policy, engine silencer, and serialization onto provenance-backed `decoy_engine.safe_output.SafeEvent`, named `families` factories, `SafeOutputScope`, and `SafeEventSink`. It retains only consumer mode selection and presentation adapters that accept `SafeEvent` or `SerializedSafePayload`.
- STORM CLI `_boundary.py` migrates its per-command guards onto the engine `SafeOutputScope` plus the whole-process entry adapter described above. It owns wiring `main()` before Typer or Click parsing, static help handling, fixed usage conversion, safe process hooks, and the `ContextVar` stream proxy. It does not own another value policy.
- STORM CLI `_projections.py` migrates onto engine family factories and sealed projection enums. It always aliases source field names and never reads engine prose, message, detail, or a plain event dictionary.
- Platform `api/jobs/fidelity_projection.py` migrates onto `decoy_engine.safe_output.families.quality` fidelity factories plus a Platform response driver that consumes a sealed payload. It preserves the existing response schema and numeric facts but cannot accept an engine mapping or free detail.
- Engine `phase.py`, if present in the selected topology, migrates onto explicit phase event families and cannot serialize generic string or mapping evidence.
- F43 `SafeFinding` migrates onto `decoy_engine.safe_output.families.ci` named factories and the same sealed sink contract. Its JSON, Markdown, stderr, and SARIF renderers take `SafeEvent` or `SerializedSafePayload`; `engine_logging_silenced` delegates to `SafeOutputScope`. The local dataclass is removed after its callers migrate.

Existing STORM and F43 command behavior, mode behavior, `integrity-report/v1` and `fidelity-report/v1` schemas, renderer coverage, and tests remain. Their duplicate construction and output authorization policies do not.

F43 repository paths are not admitted to the universal value-free event. Ordinary JSON, Markdown, stderr, and SARIF use an artifact alias plus numeric region. F31 does not create a raw-path CI exception. If a future product requirement needs one, it requires a separately approved artifact threat model rather than a hidden bypass in this contract.

The F43 stable fingerprint must also stop deriving from a repository path. Its F31 form uses the closed rule identity plus an engine-minted finding ID when one exists, or the run-local artifact alias and numeric region for a run-local finding. The ordinary event never carries a path hash, because a hash is still derived from the forbidden path value.

## Acceptance tests, authored RED first

### Seeded marker corpus

Create a shared corpus specification in the engine and consumer fixtures with distinct high-entropy markers for:

1. source path and filename;
2. output and report path, including hostile control characters;
3. table, column, node, and layout names, including clean identifiers;
4. source and generated cell values, raw bytes, row buffers, and samples;
5. DSNs, URLs with user information and query secrets, cloud credentials, tokens, and secret references;
6. fixed-width offsets, widths, and layout prose;
7. exception message, `args`, `repr`, custom `code`, custom `__str__`, cause, context, traceback source, and locals;
8. engine log messages, warning text, extension prints, driver messages, subprocess stdout and stderr;
9. arbitrary evidence, validation, quality, audit, telemetry, breadcrumb, and tracker attributes;
10. forged event fields, unknown schemas, invalid numerics, serializer exceptions, and sink exceptions.

Alongside every high-entropy marker class, seed grammar-clean and shape-preserving canaries that exercise paths a noisy secret marker may not reach: short lowercase identifiers such as `canary_table_alpha` and `canary_col_beta`, clean eight-character tokens, lowercased and punctuation-stripped variants, a valid UUIDv4 placed only in a customer value and secret-reference position, and valid positive and negative integers and floats placed in fixed-width layout or configuration fields. Assertions scan both the original and every expected truncation, case-folding, punctuation removal, numeric formatting, and UUID normalization. The UUID must never acquire opaque-ID provenance; arbitrary numbers must never become counts or measurements.

For every surface, the harness records at least one expected registered safe event and asserts it is present before checking marker absence. This non-vacuity assertion prevents a disconnected or empty sink from passing. For a surface expected not to exist, such as the default engine log file, assert both that the file is absent and that another known surface observed the operation.

The matrix runs success and failure in default, quiet, verbose, JSON, and file modes where meaningful:

| Repository | Surfaces scanned byte-for-byte |
|---|---|
| engine | callback sink, progress v1 compatibility output, reason output, isolated envelope, in-process result, warnings capture, stdlib logs, attempted engine file, diagnostic and evidence projections, subset status, support payload fixture, subprocess captures |
| CLI | stdout, stderr, Rich capture, `--json`, `--quiet`, `--verbose`, ordinary `--output` files, report files, evidence, progress, warnings, root callback, eager option, `--help`, unknown option, malformed option value, `BadParameter`, `UsageError`, `Abort`, `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook`, async task handler, all uncaught-command guards |
| Platform | API success and error bodies, `HTTPException`, `JSONResponse`, base and text/HTML `Response` variants, SSE yields, server and worker logs, job progress and runtime facts, audit/evidence model constructors and persisted rows, evidence JSON and HTML, telemetry, diagnostics, support bundles, warnings, child logs, Sentry event fixture, OpenTelemetry attributes |

Each recorder scans text, bytes, JSON keys and values, filenames created under its temporary root, captured log records before formatting, and nested database JSON. It also asserts no `Traceback`, source line, absolute path, URL user information, or traceback-local representation appears. Property tests generate Unicode, control characters, long values, path variants, nested containers, exception subclasses, and alias collisions.

The unsafe channel has a separate test. It proves explicit opt-in, label, permissions, no symlink overwrite, no stdout or stderr destination, and exclusion from every ordinary recorder. It may contain its seeded marker by design.

### Construction and emit-time tests

Author tests before implementation for:

- direct construction, dataclass replacement, subclassing, object mutation attempts, weak-reference support, duplicate fields, unknown enums, booleans as integers, NaN and infinity, huge values, oversized collections, and arbitrary dictionaries;
- `object.__new__`, `object.__setattr__`, copied instances, guessed module tokens, class-attribute inspection, `str` subclasses, fresh UUID-shaped customer values, and forged `SerializedSafePayload` objects all failing provenance verification;
- serializer and every renderer returning no mapping protocol or mutable intermediate, plus a cross-function helper attempting to append `str(exc)` to a payload and failing before any sink write;
- clean source names being aliased just like hostile names;
- provenance churn across 100,000 collected events and payloads, proving a test-only isolated registry returns to zero and weak references never raise `TypeError`; on CPython the bounded test must observe and record at least one reused ID or fail, while every runtime runs a deterministic isolated-registry collision test proving a stale entry and late cleanup cannot validate or delete the newer identity;
- alias allocation storing only keyed digest bytes, capping at 4096 per kind, returning the fixed overflow alias, preserving canonical pre-fan-out and dynamic-discovery order across worker counts and schedules, rejecting worker-local ordinal collisions and pre-remap emission, and never retaining weak-referenceable source wrapper objects;
- invalid construction raising fixed programming errors that do not echo the rejected value;
- emit-time provenance and canonical-digest verification detecting forged or mutated nested state without reconstructing an opaque ID from text;
- unknown field and schema mapping to `safe_output.invalid`;
- registry or serializer failure producing the exact emergency literal with no logger call;
- sink failure producing no raw fallback and restoring ambient state;
- dynamically named exception subclasses mapping to the fixed `Exception` enum without accessing `__name__`, plus exact allowed types mapping to the expected enum;
- executing catch-frame tests proving `traceback.clear_frames` is never called on the active frame, inner unwound frames may be cleared, traceback links are detached, and chains and locals become unreachable after conversion;
- concurrent threads and async tasks proving process-owned unscoped writes fail closed, internal `threading.Thread` and `ThreadPoolExecutor` handoffs use distinct copied contexts, scoped embedding preserves unrelated host output, no lock crosses `await`, nested tokens restore, and the authorized sink remains usable;
- import and ordinary library-call tests proving streams and process hooks remain unchanged, plus `capsys` tests that replace streams between scopes and prove containment, capture usability, and exact object restoration;
- isolated-child native-style `os.write(1, marker)` and `os.write(2, marker)`, buffered controlled C standard-stream output, enabled `faulthandler`, an exec-based grandchild, and a Python `os.fork` grandchild where supported, proving FD-level redirection, fault-output containment, close-on-exec, at-fork closure, and a non-vacuous separate structured channel;
- legacy `Logger`, `StructuredEvents`, `TelemetryClient.emit`, `event_to_dict`, and `event_to_legacy_dict` calls proving free strings and mappings are discarded in 0.8.0 and only fixed or sealed events emerge;
- F29 `ordinary_output` defaulting false for every existing reason, with the exact reviewed opt-in manifest and aliased compatibility projections;
- legacy F29 progress and STORM schemas being sealed projections of the canonical event rather than independent validation paths.

### AST and taint sentry

Generalize `tests/sentry/test_log_interpolation.py` into one symbol-aware analyzer exported from `decoy_engine.safe_output._testing`. Engine, CLI, and Platform import and run that exact implementation in CI with separate versioned sink manifests; consumers may add sink descriptors but may not fork analyzer logic. Scan all production Python outside the one exception module and exact reviewed data-plane sink functions. Resolve:

- direct and aliased imports;
- module and function aliases assigned through names and attributes;
- `getattr` with constant sink names;
- bound logger and console variables;
- `open`, `io.open`, `Path.open`, `write_text`, and `write_bytes`;
- `print`, `sys.stdout.write`, `sys.stderr.write`, Click, Typer, Rich console and traceback calls;
- all stdlib logging levels, `log`, `exception`, handlers, warning functions, and traceback formatting;
- `json.dump` and serializer writes to streams;
- subprocess creation with inherited output or later replay of captured output;
- third-party tracker capture and telemetry attribute calls.
- FastAPI and Starlette `HTTPException`, `JSONResponse`, `Response`, `PlainTextResponse`, `HTMLResponse`, framework-returned dictionaries and models, SSE event yields, and audit/evidence ORM or Pydantic model constructors.
- `gc`, `inspect`, and `ctypes` APIs; `sys._getframe`; and access to `__closure__`, `__code__`, traceback, frame, or closure-cell attributes outside the exact reviewed `_provenance.py` and `_exception.py` operations and the manifest-pinned `flush_c_stdio()` calls in `_child_fd_bootstrap.py`. A RED sentry fixture proves every other `ctypes` import, symbol, argument, or call shape still fails.
- Platform safe-output routes that declare a Pydantic `response_model`, use an unapproved `response_class`, return a model or `SerializedSafePayload`, call `jsonable_encoder`, or pass a payload to `JSONResponse(content=...)` instead of returning an approved byte-oriented response adapter.

Outside the boundary, an ordinary sink call is legal only when its data argument is a provenance-backed `SafeEvent` or `SerializedSafePayload` handed to a registered adapter. No dictionary, local variable named `safe`, class with a spellable brand, or helper return annotation is trusted. The analyzer follows assignments, parameters, returns, and calls across local helper functions, rejects mutation or enrichment of sealed payload flow, rejects reflective or unresolved dispatch near a sink, and fails closed on newly discovered sink APIs.

For every `except ... as exc`, uses of the caught name are limited to `type(exc)`, `isinstance`, bare re-raise inside internal control flow, or direct handoff to `safe_event_from_exception`. Attribute access, formatting, string conversion, logging, returning, closure capture, container insertion, chaining, and passing to another function fail the sentry unless that exact function is the boundary.

Regardless of whether an exception is bound with `as`, the analyzer bans `sys.exc_info`, every `traceback.*` access, `logging.exception`, logger `.exception`, and every `exc_info=` keyword outside `safe_output/_exception.py`. There is no directory exclusion or bare-except loophole. The exception module manifest permits only the exact operations required by the reviewed conversion design.

An exact manifest names data-plane writer functions and their artifact class. A code review and sentry test fail if the manifest grows without a matching threat-model entry. Directory exclusions and generic `# noqa` are prohibited.

Add Semgrep taint rules from exception, path, config, report, row, connector, and extension sources to output sinks. Semgrep supplements the AST rule, particularly across helper calls, but the local analyzer remains the build gate.

### Sentry self-tests

Use in-memory source fixtures and temporary packages to seed one violation for every sink class and every evasion class: aliased `print`, assigned logger, `getattr(console, "print")`, `io.open`, `Path.write_text`, exception f-string, a bare `except` with `sys.exc_info`, every `traceback` spelling, `logger.exception`, `logging.exception`, `exc_info`, warning text, JSON dump, subprocess inheritance, captured-output replay, tracker extras, a fake branded variable, cross-helper dictionary enrichment, each prohibited reflection spelling, `HTTPException(detail=...)`, each Response variant, a response model declaration, framework model or payload return, `JSONResponse(content=payload)`, SSE yield, and audit/evidence model construction. These are defensive static fixtures, not executable capability-recovery or exploitation scripts. Run the same core fixtures against all three repository manifests and assert the analyzer reports the expected file and rule.

The runtime harness monkeypatches one renderer to append a marker after serialization and asserts the surface scan fails. It monkeypatches construction and serialization failure separately and asserts only the fixed fallback appears. CI records these as self-test passes, not by committing a broken production fixture.

### Argon finding closure matrix

Every row below is an acceptance commitment. The named test is authored and captured RED before the design change in that row is implemented.

| Finding | Concrete design closure | RED-first acceptance witness | Slice and sizing effect |
|---|---|---|---|
| BLOCKER-1 | Closure-held provenance backs `SafeEvent`, `OpaqueId`, and `SerializedSafePayload`; application code never receives an authorized plain mapping. | Forge with `__new__`, `__setattr__`, copy, subclass, guessed token, customer UUID, fresh dictionary, and cross-helper enrichment; each must fail closed while a named factory remains non-vacuously renderable. | Slice 0 is limited to model, sealing, aliases, and registry. Analyzer work moves to Slice 1. |
| BLOCKER-2 | The root CLI wrapper starts before Typer or Click parsing, uses `standalone_mode=False`, converts usage control flow without value echo, and installs process and async hooks. | Seed markers through root callback, help, unknown and malformed options, `BadParameter`, `UsageError`, `Abort`, main-thread, child-thread, unraisable, and task failures. | Generic hooks enter Slice 2; CLI process-entry integration is a dedicated part of Slice 6. |
| BLOCKER-3 | Exact `type(exc)` lookup returns a closed enum, and traceback hygiene never clears the executing catch frame. | Use a secret-named subclass and a real active traceback to prove no `__name__` read and no `RuntimeError`; prove inner frames detach and locals become unreachable. | Slice 2 owns exception conversion alone; stream and child containment move to Slice 3. |
| HIGH-1 | Process-owned `ContextVar` proxy streams and reversible scoped proxies replace lock-held global swapping; owned children redirect FD 1 and 2 with `os.dup2` and use a separate structured channel. | Interleave tasks and threads, hold work across `await`, exercise unscoped process and scoped host output, write safe output from another context, and call `os.write` in every child worker. | New Slice 3 isolates concurrency and FD mechanics for focused review. |
| HIGH-2 | Legacy `Logger`, `StructuredEvents`, `TelemetryClient.emit`, `event_to_dict`, and `event_to_legacy_dict` discard free-form input in 0.8.0 and emit or return only fixed events or sealed payloads. | Call every legacy signature with markers in each string and mapping position and show that current behavior is RED before all forwarding is removed. | Slice 4 explicitly owns legacy neutering before broader engine family migration. |
| HIGH-3 | One analyzer from `decoy_engine.safe_output._testing` runs with engine, CLI, and Platform manifests, covers implicit framework and persistence sinks, and bans all exception-state APIs outside `_exception.py`. | Shared fixtures exercise helper indirection, every Response and HTTP exception variant, SSE, model constructors, bare `except`, `sys.exc_info`, `traceback.*`, `logging.exception`, and `exc_info=`. | Shared analyzer becomes Slice 1; consumer manifest gates repeat in Slices 6 and 7. |
| MEDIUM-1 | Alias state contains only keyed digests, is locked and capped, and is preallocated in canonical order before fan-out. | Inspect state for absent raw identifiers, overflow past 4096, race concurrent allocation, and compare aliases across every surface and schedule. | Alias work stays in narrow Slice 0 and is complete before event-family consumers. |
| MEDIUM-2 | `families/` separates domains, every module stays below 450 lines, and F29 `ordinary_output` defaults false with an exact opt-in manifest. | Make module-size and registry-default tests RED before package creation, then prove existing reasons are rejected until explicitly projected with aliases. | Slice 0 owns the default and budgets; Slice 5 adds families one module at a time. |
| MEDIUM-3 | A topology gate chooses merged commits or exact STORM/F43 structural commits and produces one migration, never dual implementations. | On a fixture branch without the merge, prove the slice resolves the cited structures and fails closed if their source commit is unavailable. | Slices 6 and 7 start with recorded topology gates and exact input commits. |
| LOW-1 | The corpus includes clean, short, normalized, UUID-shaped, integer, float, and fixed-width canaries in addition to high-entropy markers. | Demonstrate at least one current regex, truncation, normalization, UUID, and numeric path that misses a noisy marker but leaks the shape-valid canary. | Corpus specification begins in Slice 0; analyzer and full recorder execution move to Slice 1 and repeat in consumer slices. |

### Argon round-2 mechanics closure matrix

Every row below is an additional acceptance commitment. The named witness is authored and captured RED before the corresponding design change.

| Finding | Concrete design closure | RED-first acceptance witness | Slice | Honest residual boundary |
|---|---|---|---|---|
| HIGH-1 | Process-owned proxies discard unscoped writes by default. Scoped embedding wraps current host streams, registers engine-owned threads, and invokes each internal thread or pool callable through a distinct `copy_context().run(...)`. | Raw `threading.Thread` and executor handoffs leak before the wrapper; controlled unscoped driver-thread output is discarded in process-owned mode; unrelated host-thread output remains usable in scoped mode. | Slice 3 | Scoped mode cannot classify an arbitrary foreign C-created thread while preserving unrelated host output. Such drivers require process-owned mode or isolation. |
| HIGH-2 | Every provenance-bearing slotted type includes `__weakref__`; the registry keys by `id(obj)` and stores a digest plus identity-checked weak-reference cleanup. | Weak-reference construction and 100,000-object GC churn fail before the slots and cleanup change; a test-only isolated registry returns to zero. CPython must observe a reused ID within the bounded loop or fail, and every runtime runs a deterministic stale-entry and late-callback collision test. | Slice 0 | Collection timing is implementation-dependent. The contract is eventual cleanup after collection, not a deterministic collection schedule. |
| HIGH-3 | Import and library calls install no permanent proxy or process hook. Explicit process entrypoints own permanent installation; library scopes bind and restore the exact streams current on entry. | Import and ordinary calls leave streams and hooks identical; `capsys` swaps streams between tests while containment, capture, nested restoration, and teardown remain correct. | Slices 2 and 3 | A host that mutates streams concurrently during an active scoped call violates the cooperative embedding contract and must serialize that mutation or isolate the call. |
| HIGH-4 | The sentry bans reflection and memory-introspection APIs in production, and Platform routes return only byte-oriented `SafeJSONResponse` or `SafeSSEResponse` adapters under a response-model gate. | Static reflection fixtures fail; current Pydantic and `JSONResponse(content=...)` paths fail; approved adapters emit exact canonical bytes; route declarations with a response model, unapproved response class, or model return fail the gate. | Slices 1 and 7 | The sentry constrains reviewed production source. Closure hiding does not protect against arbitrary malicious same-process Python, native code, debuggers, or memory inspection. |
| MEDIUM-1 | Child bootstrap completes before extension import, flushes known-safe inherited Python and C standard streams through a manifest-pinned helper, disables `faulthandler`, redirects FD 1 and 2, marks the parent-provided transport close-on-exec, and closes it in a Python at-fork child. | Buffered controlled C output, Python output, fault-handler output, an exec-based grandchild, and a Python `os.fork` grandchild where supported cannot reach parent stderr or corrupt the envelope; the structured transport remains non-vacuously usable. | Slice 3 | FD 1 and 2 containment cannot stop native code from opening `/dev/tty`, another file or socket, duplicating the transport deliberately, using a duplicate retained before bootstrap, or invoking native `fork()` without Python callbacks. |
| MEDIUM-2 | Dynamic discovery has a coordinator barrier that canonical-sorts and deduplicates discoveries, assigns aliases once, remaps worker handles, and releases events only after remapping. | Under one fixed test run key and input, random worker counts, completion orders, batching, repeated discoveries, and worker-local collisions produce identical aliases or a fixed fail-closed result; no pre-remap event emits. | Slice 0 | Aliases are deterministic only within a run and disclose bounded kind, count, and canonical ordering metadata. They remain deliberately unlinkable across runs. |

## Implementation slices

Every slice begins with its acceptance tests in RED, preserves the failure output, implements the smallest change, runs the self-check, then receives an adversarial review and remediation. No slice may weaken, delete, xfail, or broaden an assertion to turn green.

The Argon remediation increases the plan from seven to nine slices. The former combined model/sentry slice is split, and exception handling is separated from concurrent stream and child-FD containment. This keeps provenance, analyzer, exception, and process-global work independently reviewable and keeps every new or expanded Python module below 450 lines.

### Slice 0: provenance model, sealed payload, aliases, and registry

Scope:

- Author RED tests first for direct and forged construction, closure-held provenance, UUID-shaped opaque-ID laundering, mutation through `object.__setattr__`, weak-reference creation, 100,000-object GC churn, a mandatory observed CPython ID reuse, and a deterministic stale-entry plus late-callback collision, non-mapping sealed payload behavior, cross-helper enrichment, alias digest-only storage, cap and overflow, concurrent canonical allocation, schedule-invariant dynamic discovery, pre-remap emission rejection, F29's false default, exact opt-in manifest, and the 450-line module budgets.
- Add the `safe_output` package skeleton, schema constants, provenance service with explicit `__weakref__` slots and identity-checked cleanup, exact model, sealed payload, sink driver capability, alias allocator and dynamic-discovery coordinator barrier, registry entries, emergency literal, and minimal family factories.
- Add the grammar-clean, shape-preserving, UUID, and numeric canaries to the shared corpus specification and prove the current behavior RED where applicable.
- Bump engine to 0.8.0 only when provenance, sealing, aliases, registry compatibility, and module-size tests are green.

Self-check:

```bash
pytest -q tests/unit/safe_output tests/security/test_safe_output_boundary.py tests/sentry/test_value_free_boundary.py tests/sentry/test_log_interpolation.py
ruff check src/decoy_engine/safe_output tests/unit/safe_output tests/security tests/sentry
mypy src/decoy_engine/safe_output
```

Adversarial review: attempt direct dataclass construction, `object.__new__`, `object.__setattr__`, copied or subclassed objects, guessed tokens, class inspection, generic token secrets, valid customer UUIDs, hostile numeric objects, payload mapping access, cross-function mutation, alias flooding and reordering, and serializer monkeypatches. Remediate every BLOCKER and HIGH before Slice 1.

### Slice 1: shared analyzer, sink manifests, and RED surface corpus

Scope:

- Author analyzer self-tests RED first for every direct, aliased, assigned, reflective, cross-helper, framework-implicit, bare-except, exception-state, response-model, and fake-brand evasion listed above.
- Export the one analyzer from `decoy_engine.safe_output._testing` and add versioned engine, CLI, and Platform sink manifest fixtures. Platform fixtures include `HTTPException`, every Response variant, returned Pydantic and dataclass models, SSE yields, and audit/evidence constructors.
- Add runtime recorder primitives, Semgrep rules, shared marker corpus harness, and baseline RED evidence showing current direct sinks, free fields, default log file, verbose tracebacks, process-entry leaks, and seeded markers.
- Fail closed on unknown sink APIs and ban `sys.exc_info`, `traceback.*`, `logging.exception`, `exc_info=`, `gc.*`, `inspect.*`, `ctypes.*`, `sys._getframe`, and closure, code, or frame attribute inspection outside the exact reviewed boundary operations. The only `ctypes` exception is the manifest-pinned, input-free C stdio flush in `_child_fd_bootstrap.py`; self-tests reject any other module, symbol, argument, or call shape.

Self-check:

```bash
pytest -q tests/sentry/test_value_free_boundary.py tests/sentry/test_log_interpolation.py tests/security/test_safe_output_boundary.py
ruff check src/decoy_engine/safe_output/_testing.py tests/sentry tests/security
mypy src/decoy_engine/safe_output/_testing.py
```

Adversarial review: add local helper indirection, framework auto-serialization, dynamically assigned constructors, bare `except`, reflection, and one unlisted sink to temporary fixtures. Each must fail with the expected rule and location before Slice 2.

### Slice 2: exact exception conversion and explicit process hooks

Scope:

- Author RED tests first for dynamically named subclasses, exact type-to-enum lookup, active catch-frame behavior, inner-frame clearing, traceback detachment, bounded chains, safe hooks, unreachable locals, and import or library calls that must leave every process hook unchanged.
- Implement exception conversion over `reason_adapter`, the exact `_ALLOWED_EXCEPTION_TYPES` map, and closed `SafeBoundaryError` propagation.
- Implement `SafeOutputScope` logging handler and warnings capture. Implement `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook`, and the async exception handler only behind `install_process_safe_output()` for explicit process-owned entrypoints. Prove library scope does not alter those hooks. Stream containment and child descriptors remain Slice 3.
- Remove the default engine file handler and raw fallback warning.
- Migrate fixed-width and extension/provider catch seams first because they exercise raw buffers and arbitrary errors.

Self-check:

```bash
pytest -q tests/unit/execution/test_isolated_safe_error.py tests/unit/profile tests/security/test_reason_boundary.py tests/security/test_safe_output_boundary.py tests/sentry/test_value_free_boundary.py
pytest -q tests/unit/transforms/test_text_mask.py tests/connectors
```

Adversarial review: dynamic secret-named exception classes, exact allowed classes, nested contexts, handler failure, warning formatter failure, malicious `__str__`, active and unwound traceback shapes, chained exceptions, raw bytes in locals, hook re-entry, and state-restore exceptions. Confirm no class name is read and no original exception remains reachable from an exported object.

### Slice 3: concurrent stream proxy and isolated child descriptors

Scope:

- Author RED tests first with interleaved threads and async tasks, raw and wrapped `threading.Thread` and `ThreadPoolExecutor` handoffs, controlled foreign background-thread output, nested cancellation, `capsys` stream replacement, exact host-object restoration, safe-driver output, extension output, and native-style `os.write` calls in each owned child type.
- Implement process-owned mode only through the explicit entry adapter. Install permanent `ContextVar`-aware stdout and stderr proxies whose unscoped default is discard, verify ownership at command boundaries, and keep authorized safe driver writes distinct from ordinary writes.
- Implement scoped embedding around the streams current at entry, with exact restoration and no hook installation. Maintain the active-scope and engine-owned-thread registry. Wrap every internal thread and executor submission with its own captured context and `ctx.run`, and add a sentry or exact call-site manifest that rejects bypasses.
- In `_isolated_worker.py`, `generation_child.py`, `_pg_probe_child.py`, and every other owned worker, perform child bootstrap before extension import. Flush inherited known-safe Python streams and call `_child_fd_bootstrap.flush_c_stdio()`, whose exact POSIX `fflush(NULL)` and Windows `_flushall()` call shapes are the only manifest-approved `ctypes` operations. Disable `faulthandler` and delayed dumps, mark the parent-provided structured descriptor non-inheritable, register an at-fork close callback where supported, redirect descriptors 1 and 2 with `os.dup2`, and abort with a fixed code if a flush or startup ordering invariant fails. Do not derive the structured transport from stdout or stderr.
- Author RED tests with controlled buffered C standard-stream writes, `faulthandler`, an exec-based grandchild, and a Python `os.fork` grandchild where supported. Prove those writes do not reach parent stderr or the structured parser, exec cannot inherit the transport, the Python at-fork callback closes it immediately, and a known safe event still crosses the structured channel.
- Preserve the explicit residual for arbitrary foreign threads and native sinks in non-isolated in-process extensions and route high-risk drivers to isolation.

Self-check:

```bash
pytest -q tests/unit/safe_output/test_stream_proxy.py tests/unit/execution/test_isolated_safe_error.py tests/security/test_safe_output_boundary.py
pytest -q tests/unit/execution tests/connectors
```

Adversarial review: attempt cross-task suppression, missing context propagation, unscoped process writes, unrelated host-thread output, `capsys` replacement, concurrent host mutation, lock starvation, nested proxy misuse, direct original-stream access, output floods, native and buffered C writes before and after child initialization, fault-handler output, grandchild inheritance, communication-channel corruption, cancellation, and descriptor restoration. Remediate every BLOCKER and HIGH before Slice 4.

### Slice 4: engine progress, legacy API neutering, logging, and callbacks

Scope:

- Author RED tests first showing that each legacy API currently forwards a marker and that each `event_to_*` path returns an application-mutable dictionary.
- Convert F29 progress events and `context.py` callbacks to `SafeEventSink` factories.
- Neuter `Logger`, `StructuredEvents`, `TelemetryClient.emit`, `event_to_dict`, and `event_to_legacy_dict` in 0.8.0. Keep signatures only where transition requires them; discard free-form arguments and return or emit only fixed events or sealed payloads.
- Replace all direct production narrative logs and warning emissions with safe events.
- Convert isolated worker envelopes, `IsolatedRunResult.error`, row error summaries, child exit reporting, and serializer `default=str` paths without undoing Slice 3's descriptor boundary.
- Maintain progress v1 and legacy wire compatibility only through registered sealed projections.

Self-check:

```bash
pytest -q tests/unit/execution tests/unit/progress tests/security/test_reason_boundary.py tests/security/test_safe_output_boundary.py tests/sentry
rg -n 'default=str|logger\.exception|exc_info=True|print_exception|format_exc' src/decoy_engine
```

The final `rg` must produce only the exact `_exception.py` boundary definitions recorded in the sentry manifest. Adversarial review injects markers through every legacy method argument, logger callback, warning, isolated envelope key, child stream, and compatibility serializer and verifies that deprecation does not preserve a raw forwarding lane.

### Slice 5: engine event families, evidence, diagnostics, and manifests

Scope:

- Author the family-specific RED projection and module-size tests before each family module.
- Add explicit safe event families for validation, quality, row-error counts, fidelity, diagnostics, subset status, publication, and support facts.
- Replace exported `quality_metrics`, warning details, `repr(exc)`, real schema names, row-error prose, and arbitrary evidence dictionaries with typed projections and aliases.
- Preserve raw data only inside typed data-plane artifacts and private computation. Do not relabel a report dictionary as safe.
- If STORM phase envelopes have landed, adapt them here through explicit event families.
- Keep each family and support module below 450 physical lines; split by change reason before adding another concern.

Self-check:

```bash
pytest -q tests/unit/validation tests/unit/quality tests/unit/subset tests/unit/execution tests/security/test_safe_output_boundary.py tests/sentry
python scripts/test_flight.py
```

Adversarial review traces one marker from each internal raw report to every exported projection, checks alias stability and collisions, and verifies customer diagnostic privilege does not change the output class.

### Slice 6: CLI consumer and whole-process boundary, independently releasable

Prerequisite: published engine 0.8.0. Pin it exactly.

Scope:

- Run the topology gate first. Use the merged STORM structure or base/cherry-pick the exact `_value_free.py`, `_boundary.py`, `_projections.py`, tests, and F43 structural commits as one integration branch. Record exact input commits. Do not build a parallel main-only boundary.
- Author RED tests first for root callback, eager options, `--help`, unknown option, malformed value, usage error, `BadParameter`, `Abort`, each process hook, and background task failure. Every test seeds an offending value and proves the current pre-command boundary leaks it.
- Migrate `_value_free.py` onto engine provenance-backed models, named factories, sealed payloads, `SafeEventSink`, and `SafeOutputScope`, retaining only presentation responsibility.
- Migrate `_boundary.py` onto an explicit `install_process_safe_output()` wrapper established before `app()` or Click parsing, with `standalone_mode=False`, fixed usage events, process-owned stream proxies whose unscoped default is discard, safe hooks, and no default framework echo or exception formatting.
- Convert `_projections.py` into thin sealed schema adapters over engine event factories. Always alias table and field names. Never read engine message, detail, or a plain event mapping.
- Migrate every command's stdout, stderr, JSON, quiet, verbose, ordinary file, report, evidence, warning, and progress path.
- Migrate F43 `SafeFinding` onto `families.ci` and sealed JSON, Markdown, stderr, and SARIF adapters; delegate engine logger suppression to `SafeOutputScope` and remove the local model after callers move. Preserve raw data stdout only for commands explicitly documented as data-plane operations.
- Import and run the engine analyzer with the CLI sink manifest; do not fork it.

Self-check:

```bash
pytest -q tests/sentry/test_value_free_boundary.py tests/sentry tests/e2e/test_output_modes.py tests/e2e/test_scan_command.py tests/e2e/test_report_command.py tests/e2e/test_run_command.py
pytest -q
ruff check src tests
mypy src/decoy
```

Adversarial review runs every command mode and every pre-command path with hostile paths, reports, extension exceptions, usage errors that would echo option values, help, root callbacks, layout failures, process hooks, engine logs, and output-file failures. Existing STORM and F43 assertions must remain at least as strict.

### Slice 7: Platform consumer and framework sinks, independently releasable

Prerequisite: published engine 0.8.0. Pin it exactly. It does not depend on Slice 6.

Scope:

- Run the topology gate first. Use the merged STORM structure or base/cherry-pick the exact `fidelity_projection.py` structural commit and tests, then replace its policy in the same integration branch.
- Author RED tests first for raw and forged payloads reaching `HTTPException`, `JSONResponse`, `Response`, `PlainTextResponse`, `HTMLResponse`, Pydantic response models, dataclass and framework return serialization, SSE yields, audit/evidence constructors, and concurrent Platform output. Prove current model serialization rejects or introspects the opaque payload before the adapter exists.
- Replace `fidelity_projection.py` policy with a thin sealed adapter to `safe_output.families.quality` while preserving route schema and numeric facts.
- Migrate API errors, job and progress events, server and worker logs, audit event details, evidence, telemetry, runtime facts, diagnostics, support bundles, warnings, and third-party tracker events.
- Call `install_process_safe_output()` only from explicit Platform server and worker startup before framework or job execution. Platform modules imported by tests or embedding hosts must not install streams or hooks.
- Add one `SafeJSONResponse(Response)` and one byte-oriented `SafeSSEResponse`. `_sink.py` drives canonical UTF-8 bytes directly into these adapters. They never pass a payload through Pydantic, `jsonable_encoder`, `JSONResponse(content=...)`, model dumping, dataclass conversion, or framework-return serialization.
- Add the Platform response-model gate to the shared analyzer and route manifest. An ordinary safe-output route must return an approved adapter, set no Pydantic `response_model` or model return annotation, and use no unapproved `response_class`. Preserve the existing OpenAPI and client schema through separately reviewed static response metadata and contract fixtures rather than runtime model serialization.
- Install Platform sink adapters for database audit/evidence models, structured logs, Sentry, and OpenTelemetry. These accept only `SafeEvent` or `SerializedSafePayload`.
- Import and run the engine analyzer with the Platform manifest containing every implicit framework and persistence sink; do not fork it. Prove a route returning a payload or model, declaring a response model, or using an unapproved response class fails the gate.
- Preserve audit chain, transaction, authorization, and retention behavior. F31 changes payload representation, not audit durability.
- Standardize owned child processes on bounded captured streams and closed-code results.

Self-check:

```bash
pytest -q tests/test_fidelity_typed_projection.py tests/test_post_scan_fidelity_projection.py tests/test_quality_shape_fidelity_persistence.py tests/test_audit_api.py tests/test_evidence_assembly.py tests/test_evidence_bundle.py tests/test_evidence_redaction.py tests/test_obs3_logging.py tests/test_s4_cloud_creds.py tests/test_s7_fixed_width_layouts.py tests/test_safe_output_boundary.py
pytest -q
ruff check api tests
mypy api
```

Use the actual test module names present after rebase if a listed aggregate file is split, but do not reduce the named surface coverage. Adversarial review queries persisted database JSON and rendered HTML in addition to captured HTTP and logs. It also verifies byte-for-byte canonical response bodies, SSE framing, unchanged OpenAPI and `decoy-web` contracts, and rejection of every framework auto-serialization route.

### Slice 8: convergence, release evidence, and duplicate removal

Scope:

- Run the shared corpus against the exact released engine and exact pinned CLI and Platform commits.
- Run the Platform response-schema contract fixtures against the current `decoy-web` consumer when that repository is available. An unchanged schema requires no web code change; a failed consumer contract is surfaced to the owner as a separate web slice rather than being guessed by the three-repository workers.
- Remove obsolete local registries, sanitizers, exception renderers, direct sinks, and duplicated sentries. Keep consumer renderers and data-plane writers.
- Confirm a repository-wide search finds one value policy and one engine serializer.
- Run full verification, secret scanners, documentation sync, compatibility tests, and release rollback rehearsal.

Self-check:

```bash
pytest -q
ruff format --check .
ruff check .
mypy src/decoy_engine
detect-secrets scan --all-files
gitleaks dir . --redact
```

Run equivalent full commands from each consumer's current `CONTRIBUTING.md`. Do not normalize command differences by skipping a repository gate.

Adversarial review: search for second registries, free-form output models, untyped dictionaries, output-capable imports, stale engine pins, bypass comments, and ordinary artifacts not included in the corpus. Remediate every BLOCKER and HIGH and rerun on the new exact commits.

## Verification and evidence plan

### Evidence captured per slice

- exact repository commit and dirty-state check;
- RED command, failing test names, seeded marker, and relevant failure excerpt;
- GREEN targeted command and full output summary;
- sentry self-test results proving injected violations fail;
- source inventory delta showing each migrated sink and no unreviewed new sink;
- adversarial findings with severity, disposition, fix commit, and rerun evidence;
- compatibility manifests and exact engine pins;
- full-suite, format, lint, type, secret-scan, and large-block flight results;
- a hash or artifact reference for corpus outputs without storing seeded secret values in ordinary logs.

### Objective green conditions

The objective gate is green only if:

1. every surface matrix cell ran or has a reviewed not-applicable reason;
2. every recorder proved non-vacuity before absence;
3. all planted markers are absent from ordinary output;
4. each sentry mutation is detected;
5. no ordinary traceback or local capture exists;
6. no default engine log file is created;
7. invalid construction, serialization, redaction, and sink failures return only registered or literal fallbacks;
8. all prior security tests remain and pass;
9. full repository gates pass on the exact candidate commits;
10. CLI and Platform lock exactly to the released engine contract;
11. provenance forgery, mutable payload, helper enrichment, and UUID-laundering tests fail before implementation and pass only through registered factories and drivers;
12. CLI parsing, help, root callbacks, usage errors, and process hooks are inside the root boundary with seeded values absent;
13. concurrent stream tests prove task isolation, and every owned child proves FD 1 and 2 redirection with a live dedicated structured channel;
14. all three repositories run the same analyzer implementation with their reviewed manifest, and all legacy free-form APIs are neutered;
15. every new or expanded F31 Python module remains below 450 physical lines;
16. process-owned unscoped thread writes are discarded, while every engine-owned thread and executor handoff preserves containment through a distinct copied context;
17. import and library calls leave process hooks unchanged, and `capsys` plus nested scoped calls prove exact host-stream restoration;
18. weak-reference and 100,000-object GC-churn tests return a test-only isolated provenance registry to zero; CPython records at least one reused ID within the bound or fails, and every runtime passes the deterministic stale-entry and late-callback collision test;
19. Platform safe-output routes use only canonical-byte response adapters, the response-model gate rejects framework auto-serialization, and OpenAPI plus `decoy-web` contracts remain unchanged;
20. child tests contain Python, controlled buffered C, and `faulthandler` output, prove close-on-exec and Python at-fork transport closure separately where supported, and still carry one known structured event;
21. under one fixed test run key and input, dynamic schema discovery produces the same alias mapping across worker counts, batching, and completion schedules, and no event emits before coordinator remapping.

Coverage percentage alone is not acceptance evidence. Add branch and mutation attention specifically to constructors, emit-time revalidation, emergency fallback, exception conversion, state restoration, alias allocation, AST resolution, and tracker adapters. A surviving mutation in one of those functions blocks release.

### Rollback

Engine 0.8.0 is additive at import level and intentionally breaking for unsafe legacy output behavior. Rollback means pinning CLI or Platform back to their previous exact engine version and previous consumer commit together. Never roll back only the engine beneath an F31 consumer. Keep `decoy-safe-output/v1` readers forward-compatible with unknown registered reasons by mapping them to `safe_output.invalid`; do not silently re-enable raw output. Database migrations are not required for the contract. If a consumer adds typed columns, its slice supplies its own reversible migration and rollback test.

## Documentation plan

Engine documentation changes:

- add the safe-output public API, schema, allowed field catalog, alias semantics, exception behavior, and examples to the API reference and README entrypoints;
- update `CODEMAP.md` with the owner package and sink adapters;
- update the security error-reporting and data-exposure guide to prohibit locals, chains, free prose, and default file logging;
- document data-plane artifacts separately from ordinary output;
- document 0.8.0 compatibility, deprecations, registry additions, and rollback in the changelog and release notes;
- document how a new event family, registry reason, numeric field, data-plane writer, or sink is reviewed and added to both sentries.

CLI documentation changes:

- state that default, JSON, quiet, verbose, report, and evidence modes share one value-free policy;
- identify data-plane stdout commands explicitly;
- document the isolated `--unsafe-debug-log` workflow, permissions, sensitivity, no-upload rule, and retention responsibility;
- update schema examples to aliases and fixed codes.

Platform documentation changes:

- document canonical safe events for API errors, job logs, audits, evidence, telemetry, diagnostics, support bundles, Sentry, and OpenTelemetry;
- document the byte-oriented safe response adapters, the no-response-model rule for safe-output routes, and the static OpenAPI contract path;
- state that administrator and diagnostic privileges do not relax ordinary evidence;
- document tracker defaults and the prohibition on locals, request bodies, raw breadcrumbs, and raw exceptions;
- update operations and support runbooks for the unsafe artifact isolation rule.

Barry performs the repository documentation sync after code review as required by `AGENTS.md`. Documentation examples run through the same marker scan so prose fixtures cannot introduce a bypass pattern.

## Risks and mitigations

| Risk | Mitigation and acceptance evidence |
|---|---|
| A safe-looking token contains a secret | No generic token field. Only registry enums, provenance-backed opaque variants, and ordinal aliases. UUID or ULID syntax alone grants nothing. Seed clean identifiers and token-shaped secrets. |
| A forged event or customer UUID bypasses construction | Closure-held provenance registration for events, opaque IDs, and sealed payloads; canonical digest verification; no textual reconstruction; mutation, `__new__`, `__setattr__`, copy, subclass, and UUID-laundering tests. |
| Slotted objects cannot be weakly registered, registry entries leak, or an address is reused | Explicit `__weakref__` slots; `id(obj)` plus live weak-reference identity and digest; callback identity checks; 100,000-object GC churn; a mandatory observed CPython reuse; and a deterministic stale-entry collision on every runtime. Collection timing is not promised. |
| Failure handling leaks the rejected value | Fixed error messages, generic invalid event, literal emergency record, and monkeypatched serializer and logger failures. |
| Logging filters miss descendants or last-resort handlers | Boundary-owned handler, no default engine handler, propagation and last-resort tests, exact snapshot and restoration. |
| Stream containment disappears across threads or corrupts an embedding host | Process-owned unscoped writes fail closed; every internal thread handoff uses `copy_context().run`; library scopes bind and restore the host's current streams; import and ordinary calls install nothing; `capsys`, nested-scope, unrelated-host-thread, and exact-restoration tests. Arbitrary foreign C threads require process-owned mode or isolation. |
| Permanent hooks or proxies run during import or a library call | One explicit installer is called only by CLI, Platform server or worker, and isolated-worker entrypoints. Library tests snapshot every stream and hook before import and calls. Concurrent host mutation during an active scoped call is outside the cooperative contract. |
| Exception locals survive in chains or frames | Convert in an outer frame, never clear an executing frame, clear only proven unwound inner frames, detach tracebacks, sever cause and context, ban locals capture, and run weak-reference reachability tests. |
| Aliases retain values, grow without bound, or reorder concurrently | Store keyed BLAKE2b digests only, cap each kind at 4096 plus a fixed overflow alias, lock allocation, pre-register static names, and use a coordinator discovery barrier before dynamic-name events. Randomized schedule tests prove within-run determinism. Cross-run linking is deliberately unsupported, and bounded ordering metadata remains visible. |
| Numeric fields encode values | Closed field-specific semantics and ranges. No arbitrary metric name. Review each added numeric field as a schema change. |
| Existing detailed diagnostics become less useful | Add safe type, component, position, counts, and registry reason. Use the explicit isolated unsafe channel only when approved. Verbose never weakens safety. |
| Data-plane writers become a loophole | Exact function manifest, threat-model entry per writer, no directory exclusions, sentry review on growth, and status-only ordinary events. |
| STORM or F43 is unmerged or conflicts | Use the deterministic topology gate, base or cherry-pick exact structural commits, replace duplicated policy in one unified migration, and keep their tests. Do not build dual targets. |
| Consumer schema drift | Exact engine pin, compatibility fixtures from sealed projections, cross-repo corpus, and release engine first. |
| Sentry evasion through helpers, framework serialization, aliases, or reflection | One shared analyzer, repository sink manifests, interprocedural local flow, implicit framework and persistence sinks, reflection bans, conservative failure on unresolved dispatch, Semgrep backstop, and explicit defensive self-tests. This constrains reviewed source; it is not hostile same-process isolation. |
| FastAPI or Pydantic serializes an opaque payload or bypasses the sink | `SafeJSONResponse` and `SafeSSEResponse` accept sink-produced bytes; the route gate rejects response models, model returns, unapproved response classes, `jsonable_encoder`, and `JSONResponse(content=payload)`. Static OpenAPI metadata and consumer fixtures preserve the contract. |
| Regex or secret scanners create false confidence | The construction model and sentries are the proof. Pattern scans are recorded only as defense-in-depth evidence. |
| Native driver, buffered C output, or `faulthandler` writes around child redirection | Bootstrap before extension import; flush known-safe inherited Python and C standard streams through the exact `_child_fd_bootstrap.py` boundary; disable fault handlers; redirect FD 1 and 2; use close-on-exec plus a Python at-fork close callback; test buffered output and both supported grandchild forms. Native code that opens another sink, deliberately duplicates the transport, retains a pre-bootstrap descriptor, or calls native `fork()` without Python callbacks remains outside this control. |
| Unsafe debug artifact is uploaded accidentally | New exclusive file, 0600, fixed label, no standard support or evidence discovery, separate retention, and exclusion tests. |

## Gate chain and handoff

### This plan

The plan gate is sequential: argon (`google/gemini-4-argon`) reviews this exact file, then one Opus-4.8 Dennis performs the repo-grounded gate, then the owner decides. Review findings are remediated in the plan before owner approval. No implementation begins from an unapproved revision.

### Code

Each implementation candidate follows: **objective-green evidence -> Codex review -> argon review -> one Dennis adversarial gate**. Resolve every BLOCKER and HIGH, rerun objective evidence on the changed exact commits, then have Barry synchronize documentation. Record the exact HEAD receipt with `bash .claude/hooks/decoy-gate-pass.sh`, then hand the evidence packet to the owner. Any new commit re-arms the gate.

The remote claude.ai/code workers receive only the remote `decoy-engine`, `decoy`, and `decoy-platform` repositories. This document therefore makes no assumption about local sibling paths. They must rebase onto the approved remote baselines, verify whether STORM has merged, use the cited branch only as reference if it is still available, release engine 0.8.0 first, and implement the CLI and Platform slices as independent exact-pin consumers. No prerequisite is invented from a local checkout.
