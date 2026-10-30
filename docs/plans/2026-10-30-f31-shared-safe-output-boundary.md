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

**Invariant:** No value derived from customer data, a source or output path, a credential or secret reference, an exception message or representation, a captured local, a library or driver message, or customer-controlled prose may reach an ordinary output surface except after conversion to an immutable `SafeEvent` by the engine-owned safe-output boundary. Conversion never preserves such a value. Every ordinary sink accepts only the serialized form of a `SafeEvent`.

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
- exception type names accepted by a closed engine and builtin allowlist, with every other type represented as `Exception`;
- exact UUID, ULID, or engine-minted opaque stable identifiers in a dedicated field, never a generic token field;
- stable aliases such as `table-1` and `field-2`, allocated from first-seen order or a trusted ordinal without retaining the original name in the event.

A short or clean-looking source identifier is still a value and is aliased. A generic bounded string is not safe merely because it matches a grammar. No ordinary event has a free-form string, `Any`, arbitrary mapping, exception, path, bytes, dataframe, row, or user-extensible metadata field.

The guarantee is tested twice. Runtime acceptance tests plant unique markers in every input class and scan every ordinary surface in every mode. A construction sentry rejects production output operations that do not consume the boundary type. Each sentry has mutation tests that intentionally introduce a violation and prove the sentry fails.

### Definition of done

F31 is done only when all of the following are true:

1. `decoy_engine.safe_output` is the public owner of `decoy-safe-output/v1`, and `decoy_engine` 0.8.0 exports its supported public names.
2. Every `SafeEvent` field is immutable and construction-validated against a closed set or bounded non-string type. Emit-time validation reconstructs the canonical event and rejects forged or mutated instances.
3. One serializer produces the canonical JSON-ready mapping. Text, JSON, log, audit, evidence, telemetry, diagnostic, support, and tracker renderers consume only that mapping or a registry lookup keyed by it.
4. Invalid or unknown input produces the fixed registered `safe_output.invalid` event. Serializer or sink failure produces only the literal emergency `safe_output.failed` record. Neither path logs, formats, or falls back to the rejected object.
5. Exceptions become a registry reason, a closed type label, and a closed component plus numeric position only. Ordinary exports hold no raw exception, chain, traceback, frame, source line, or local.
6. The default engine file handler is removed. Ambient logging, warnings, Python stream output from extension calls, and subprocess output are contained and restored correctly.
7. CLI and Platform pin exactly `decoy-engine==0.8.0` and replace their duplicate boundaries with thin adapters to the engine contract. No second value policy remains.
8. The seeded corpus covers every listed surface and mode with a positive non-vacuity assertion. The AST sentry catches direct, aliased, assigned, and reflective sink access. Its self-tests prove each rule goes red.
9. Existing security assertions remain present and at least as strict. The new suite, each repository's full suite, static checks, and secret scanners pass on the exact reviewed commits.
10. Documentation describes the ordinary channel, the isolated unsafe channel, schema compatibility, and the residual risks. The exact-artifact gate receipt is recorded only after all required reviews and documentation sync.

### Boundaries and non-goals

Ordinary output includes stdout, stderr, `--json`, ordinary `--output` reports, application and engine logs, warnings, progress, API error bodies, job events, audit rows, evidence, telemetry, diagnostics, verbose mode, support bundles, and third-party error or observability trackers.

Primary data artifacts are not ordinary output. Masked datasets, explicitly requested raw scan profiles, encrypted vaults, private isolated-worker payloads, and quarantine rows may necessarily contain data. They must use separately typed data-plane sinks, restrictive permissions where applicable, and can never be passed to an ordinary sink. Their status and failures are reported only as `SafeEvent` values. The static sentry maintains a reviewed exact list of data-plane writer functions, not a directory or filename wildcard.

Native libraries or drivers that write directly to an inherited operating-system file descriptor are residual risk. F31 captures Python logging, warnings, Python streams, and child processes owned by Decoy, but does not claim portable interception of arbitrary native writes. Callers must use process isolation where this threat matters.

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
| CLI stdout, stderr, JSON, verbose, and files | `decoy/src/decoy/cli/scan.py:773-798`, `subset.py:278`, `:352-522`, `fit.py:321`, `:495-522`, `templates.py:113-148` | Main still has direct Rich, `sys.stdout`, file writes, full report dictionaries, free errors, and verbose tracebacks. After STORM lands, keep command behavior but replace local value policy with engine factories. |
| CLI progress and report surfaces | `decoy/src/decoy/cli/run.py:207-219`, `:455-487`; `evidence.py:118`, `:459-492` | Progress, errors, evidence, and mode-specific rendering are distinct sinks today. All render from the canonical safe mapping. Data stdout for `run` remains a typed data-plane sink. |
| Platform audit | `decoy-platform/api/audit/logger.py:19-28`, `:83-171` | `detail` is explicitly stored verbatim and before, after, and meta are arbitrary scrubbed objects. Ordinary audit fields become safe-event projections; existing audit integrity and blocking semantics remain unchanged. |
| Platform request and server log context | `decoy-platform/api/logging/context.py:39-68`, `:71-146`, `:222-227` | Route templates and validated UUIDs are useful trusted fields, but IP, user agent, actor, and other values are not permitted in an F31 ordinary event. Keep security audit data only where policy explicitly classifies it outside the value-free event, and never mix it into safe evidence. |
| Platform evidence and telemetry | `decoy-platform/api/evidence/redaction.py:142-204`; `api/evidence/assembly.py:1887-1888`; `api/jobs/routes_runtime_facts.py:76-87` | Existing redaction and fingerprints are defense in depth. Evidence assembly and runtime facts must accept typed engine projections, not arbitrary engine dictionaries. |
| Platform child and driver output | `decoy-platform/api/generation/generation_parent.py:189-200`; `generation_child.py:277-291`, `:380`; `cloud/pg_probe.py:85-86`, `cloud/_pg_probe_child.py:6`, `:83-84` | Some subprocess paths already capture or discard streams and one probe emits a closed code. Standardize on the scoped silencer and safe-event conversion; never replay a captured line. |
| Third-party trackers and locals | `decoy-platform/api/cloud/io.py:134-137`; `api/jobs/v2_cloud_materialize.py:409-410`; cloud credential tests using `TracebackException(..., capture_locals=True)` | The existing tests prove local variables are a live exfiltration channel. Configure trackers with locals and raw breadcrumbs disabled, then use a pre-send safe-event type check. |
| STORM reference, not baseline | Branch `claude/eager-clarke-14deh1`: `decoy/src/decoy/cli/_value_free.py:303-417`, `_projections.py:1-24`, `tests/sentry/test_value_free_boundary.py`; `decoy-platform/api/jobs/fidelity_projection.py:1-16`; `decoy-engine/src/decoy_engine/phase.py:1-16` | Adopt the single boundary, fixed prose, typed projection, command guard, engine silencer, and canary sentry shape. Strengthen it by always aliasing field names and removing generic string or mapping payloads. Do not depend on the branch until merged. |
| F43 reference, not baseline | Branch `feat/f43-ci-integration`: `decoy/src/decoy/ci/contracts.py:1-11`, `:60-145`, `:227-277`; `ci/engine_logging.py:1-12`, `:24-61` | Adopt construction validation, renderer revalidation, and logger-state restoration. Do not retain its repository path field in the universal event. `SafeFinding` becomes an F31 event family, not another boundary. |

The direct engine logging census is exhaustive for this baseline. The framing command was `rg -n '\.(debug|info|warning|warn|error|exception|critical)\(' src/decoy_engine --glob '*.py'`. Calls occur at:

- connector and context: `connectors/postgres_snapshot.py:522`, `:526`; `context.py:153`; `disguises/loader.py:45`, `:53`, `:70`;
- execution: `execution/_governor_monitor.py:165`; `_isolated_memwatch.py:321`, `:341`; `_isolated_run.py:215`, `:223`; `_pipeline_auto_chunk.py:436`; `_pipeline_multi_table.py:305`; `_pipeline_routing_signals.py:194`; `_transforms.py:87`; `_unified_slice.py:256`, `:360`; `_when_gate.py:155`, `:173`; `execution/out_of_core/_capacity_eval.py:517`; `_spill_estimate.py:614`;
- generation: `generation/_fidelity_gate.py:109`, `:183`; `generators/_distribution.py:59`, `:84`, `:128-146`, `:251-261`, `:322-381`; `generators/_formula.py:104-110`, `:140`, `:187-193`, `:250`; `generators/columns.py:124`, `:185-191`, `:236-244`, `:272-307`, `:359-399`, `:447-530`, `:632-647`;
- internal, profile, and profiling: `internal/crypto.py:30`; `internal/faker_setup.py:221-281`, `:631`; `internal/logging.py:129-247`; `internal/memory.py:39-54`; `profile/_pii.py:80`; `profile/_source.py:112`, `:195`; `profiling/model_pack/classify.py:178`; `model_pack/loader.py:161-164`, `:295-331`; `model_pack/provenance.py:254-261`; `profiling/profiler.py:491`, `:566-625`;
- quality, reference, quarantine, and transforms: `quality/_freetext_advisory.py:245`; `quality/_retention_gate.py:140`; `quality/dp_policy.py:85`; `quarantine.py:498`; `reference_tables/_loader.py:127-138`; `transforms/_codeset_loader.py:442`; `_text_mask_splice.py:58`; `base.py:93-112`; `bucket_perturb.py:161`; `date_shift.py:92`, `:166-190`, `:246`; `formula.py:42`; `geo_generalize.py:281`, `:401`; `text_mask.py:385`.

`reasons/warning/_part1.py:27` also matches the mechanical expression because it declares a reason named `warning.error`; it is registry data, not a logging call. Slice 0 turns this census into a parsed AST manifest, classifies each call as safe, unsafe, or registry false positive, and requires the unsafe count to reach zero. File, stream, warning, traceback, subprocess, callback, evidence, audit, telemetry, and tracker emitters are inventoried separately in the table because a logger-only search cannot find them.

## Design

### Ownership, layout, public API, and compatibility

The engine owns the contract because it is the lowest shared dependency and already owns F29 reasons. Add:

```text
src/decoy_engine/safe_output/
  __init__.py       public imports and schema constants
  _model.py         frozen SafeEvent and closed component types
  _registry.py      safe-output reason and prose declarations via F29 registry
  _aliases.py       per-operation ordinal alias allocator
  _exception.py     exception to SafeEvent conversion
  _scope.py         ambient logging, warning, stream, and child containment
  _serialize.py     the only canonical serializer and emergency literal
  _sink.py          SafeEventSink protocol and adapters
```

The supported imports are `decoy_engine.safe_output.SafeEvent`, `SafeLocation`, `SafeEventSink`, `SafeOutputScope`, `safe_event_from_exception`, and `serialize_safe_event`, plus named family factories such as `connection_failed` whose signatures contain only typed fields. Re-export these from `decoy_engine` for the 0.8 line. There is no generic public factory that accepts arbitrary metadata. Internal constructors, opaque-ID minting, and registry mutation are not public.

This is an additive public API with a coordinated behavioral tightening of unsafe output. Bump engine 0.7.0 to 0.8.0. The wire schema is `decoy-safe-output/v1`. `decoy-reasons/v1` stays readable and is nested canonically. Existing progress v1 and legacy dictionaries are compatibility projections produced inside `_serialize.py`; they cannot maintain independent validation or prose. Deprecate direct narrative `Logger`, `StructuredEvents`, and arbitrary telemetry callbacks in 0.8.0 and remove them before 1.0 after CLI and Platform migrate.

CLI and Platform each pin exactly `decoy-engine==0.8.0`, including lockfiles and deploy images. No compatible range is acceptable for the first contract release. Their work can land separately after the engine release.

### The single model

`SafeEvent` is a frozen, slotted dataclass with these fields and no extension dictionary:

- `schema_version`: literal `decoy-safe-output/v1`.
- `reason`: a validated F29 `ReasonRecord` whose registry entry is marked `ordinary_output=True`.
- `kind`, `severity`, `phase`, `operation`, and `outcome`: enums from reviewed closed sets.
- `location`: optional frozen `SafeLocation(component, line, column, row, chunk, ordinal)`. `component` is a registry enum such as `connector`, `reader`, `validator`, or `publisher`. There is no filename or path.
- `error_type`: optional `SafeErrorType`, constructed from an exact builtin or engine allowlist. Unknown and extension types become `Exception`.
- `counts`: tuple of frozen `SafeCount(name_enum, value)` entries, with unique names and `0 <= value <= 2**63 - 1`.
- `measurements`: tuple of frozen `SafeMeasurement(name_enum, finite_value, unit_enum)` entries with field-specific ranges. Scores and rates are `[0, 1]`; durations, byte counts, and throughput have documented nonnegative caps.
- `aliases`: tuple of `SafeAlias(kind_enum, ordinal)`, serialized as `table-N`, `field-N`, `node-N`, or `artifact-N`. The original string is not stored.
- `opaque_ids`: tuple of dedicated ID variants. A private boundary capability mints them only from an engine-owned or server-owned correlation-ID source that has already been typed at its creation seam. Exact UUID or ULID syntax and a cap are additional checks, not proof of provenance. A caller cannot turn an arbitrary source string into an opaque ID through the public API. Other identifiers receive an ordinal alias. There is no generic token constructor.
- `flags`: tuple of closed flag enums. Absence and false are distinct only where the schema requires it.

Every field validates in `__post_init__`, copies input sequences to tuples, rejects booleans as integers, rejects duplicate names, rejects unknown enums, caps collection length, and refuses arbitrary subclasses where exact types matter. Named factories accept untrusted source objects, extract only permitted facts, catch fixed construction errors, and return either the intended event or `safe_output.invalid`. They never return the source dictionary after checking it. Direct construction remains available for typed consumers but can raise only a fixed programming error that never echoes input; ordinary emitters must use a named factory or handle that fixed error by producing the generic invalid event.

F29 `ReasonRecord` remains the reason authority. Add fixed `safe_output.invalid`, `safe_output.failed`, `runtime.unexpected`, `connection.failed`, `diagnostic.withheld`, and `ambient.output_withheld` entries only after the registry compatibility test approves the exact catalog. All human prose is read from that registry. Consumer-specific fixed messages become registry entries owned by their namespace, not local dictionaries.

### One serializer and fail-closed behavior

`serialize_safe_event(event, projection=SAFE_OUTPUT_V1)` is the only function allowed to produce an ordinary output mapping. It:

1. checks the exact supported type and schema;
2. reconstructs every nested value through its constructor to defeat forged instances or post-construction mutation;
3. resolves prose from the registered reason, never from an event field;
4. builds a new closed-key mapping in canonical key order;
5. applies length and control-character pattern handling only to already-approved bounded tokens as defense in depth;
6. returns only JSON-native primitives from the closed mapping.

An unknown field cannot exist on the slotted event. An unknown enum, schema, registry identity, projection, or invalid numeric value causes serialization to discard the entire event and serialize a fresh registered `safe_output.invalid` event. If that construction or JSON encoding fails, the boundary writes the constant ASCII emergency record for `safe_output.failed`. It does not call `str`, `repr`, a logger, a warning, the rejected sink, or a general JSON `default` callback. Sink failure is counted in memory if possible and otherwise silent. It never invokes raw fallback logging.

Text renderers, JSON envelopes, log handlers, audit adapters, evidence writers, telemetry exporters, and trackers accept the serialized mapping. They may select or format its known fields, but cannot receive the source event plus arbitrary extras. Registry prose is the only text renderer input.

F29 `event_to_dict` and `event_to_legacy_dict` become compatibility projections within the same serializer. `ReasonRecord.to_payload` remains the nested reason serialization primitive, not an ordinary sink. STORM phase envelopes, if merged, become typed event families. Their current generic string and frozen mapping payloads are not permitted unchanged.

### Exception boundary

Catch untrusted extension, provider, connector, validator, reader, and driver exceptions in the smallest engine-owned frame. `safe_event_from_exception(exc, reason, location)` does only the following while the exception is live:

- select a reason through the existing `reason_adapter` without reading arbitrary message, `args`, `repr`, extension `code`, or attributes;
- map `type(exc).__name__` through the closed safe-type allowlist, falling back to `Exception`;
- attach a caller-supplied closed component and numeric position, not a traceback-derived filename or source line;
- build the event and return it without storing `exc`.

The catch wrapper itself binds no row, raw buffer, credential, path, or extension arguments. Where a current parser catches inside a frame that owns such locals, refactor to let that frame unwind into the wrapper. Before detaching the original error, walk its bounded cause and context chain without formatting it, call `traceback.clear_frames` on each available traceback after those frames have unwound, set each traceback to `None`, and break the cause and context links. This removes raw byte and row references from retained frame locals as well as preventing their export. A cycle or overlong chain stops at the fixed bound and the remaining link is severed, never formatted.

Only exact engine-owned exception families may expose a registered code, and the existing adapter determines trust. An extension subclass, arbitrary provider error, or lookalike `code` attribute is untrusted and maps to `runtime.unexpected` or a fixed family reason.

If an engine error must propagate, create a new closed `SafeBoundaryError` outside the original `except` block. Set `__cause__ = None`, `__context__ = None`, `__suppress_context__ = True`, and use `with_traceback(None)` before it crosses an export seam. Do not attach the original exception or traceback. Ordinary exporters do not call `traceback.format_exc`, `traceback.print_exception`, `TracebackException`, Rich `print_exception`, `logger.exception`, or `exc_info=True`. Any diagnostic-only stack extraction uses static registered component codes; it does not keep frame objects.

No ordinary path enables `capture_locals`. Tests monkeypatch traceback helpers and third-party tracker hooks to assert `capture_locals` is absent or false and `chain` is false. Fixed-width tests place raw bytes and rows in the failing frame and prove neither locals nor representations survive. The existing cloud credential tests remain intact.

### Third-party and ambient containment

`SafeOutputScope` is a context manager around engine calls and each untrusted extension call. It snapshots and restores, in `finally`, logger handlers, filters, levels, disabled state, propagation, `warnings.showwarning`, warning filters, and any Python stream replacement it owns.

- Python logging receives an allowlisted handler whose `emit` accepts only a `SafeEventLogRecord` created by `_sink.py`. All other records are withheld and counted by logger name category only. It never formats `record.msg`, `args`, `exc_info`, pathname, function, or stack info.
- The default engine rotating file handler is removed. Library import installs `NullHandler` only. Applications may attach the safe handler explicitly. There is no import-time `logs/decoy_engine.log` creation.
- `warnings.catch_warnings(record=True)` captures extension warnings. It reports only a count and a closed category if allowlisted; it never formats warning text, filename, or source.
- Python stdout and stderr around in-process extension calls are redirected to bounded discard buffers under a process-wide reentrant guard because those globals are not task-local. The state restores under success, exception, cancellation, and nested use. Concurrency tests prove no state leak. For code that cannot tolerate serialization, run the extension in a child process instead.
- Decoy-owned subprocesses use `PIPE` or `DEVNULL`, bounded reads, timeouts, and no inherited ordinary output descriptor. Return code and closed child role may become fields; stdout and stderr content never does.
- Driver and library callbacks are adapted inside the scope. Third-party Sentry or OpenTelemetry exporters receive canonical safe mappings only. Tracker breadcrumbs, request bodies, locals, raw exceptions, and default integrations that capture them are disabled.

Containment failure emits only `safe_output.failed` through the emergency literal path. State restoration errors are never formatted. Native direct file-descriptor writes remain the declared residual risk.

### Diagnostics, verbose mode, and unsafe opt-in

`--verbose`, debug logging, support bundles, administrator access, and customer diagnostic privileges change event selection or add bounded counts, but never change the field policy. A verbose connection failure can add `error_type=TimeoutError` and `component=connector`; it cannot add the URL. A fixed-width failure can add a row ordinal; it cannot add a path, layout, row buffer, local, or traceback.

The one unsafe channel is an explicit product feature, not a fallback. The CLI flag and any Platform equivalent must be independently approved, use a caller-selected new file with mode 0600, display a fixed sensitivity warning before creation, refuse stdout and stderr targets, refuse symlinks and preexisting files, and never auto-upload. Ordinary support bundles contain only safe events and a manifest stating whether an unsafe file exists, never its path or contents.

### Cross-repository adoption and STORM/F43 convergence

STORM reframe on `claude/eager-clarke-14deh1` is expected to land first across its four repositories, including `decoy-web`. The fourth repository was not present in this planning workspace, and the remote implementation handoff exposes engine, CLI, and Platform only, so F31 does not invent a web prerequisite. Engine, CLI, and Platform contain the value policy and require code changes. The web client requires contract verification against the unchanged Platform response shapes; it needs a separate slice only if that verification finds a schema consumer change. F31 does not require any unmerged branch to exist. After STORM lands:

- keep CLI command guards, renderers, mode behavior, and `integrity-report/v1` and `fidelity-report/v1` schemas;
- replace `_value_free.py` model, registry, exception policy, engine silencer, and raw emit functions with thin imports and adapters over `decoy_engine.safe_output`;
- replace `_projections.py` value policy with engine event factories, always aliasing source field names and dropping engine prose;
- replace Platform `api/jobs/fidelity_projection.py` with a schema adapter over the same factories;
- adapt engine `phase.py` results to explicit safe event families and prohibit its generic string and mapping evidence from ordinary serialization.

This avoids a conflict-prone command rewrite. Existing STORM files shrink but retain their consumer rendering responsibilities and tests.

F43 `SafeFinding.create`, dataclass revalidation, fixed prose, renderer revalidation, and engine logger silencing are reference patterns. `SafeFinding` becomes a safe-event family and `engine_logging_silenced` delegates to `SafeOutputScope`. F43 repository paths are not admitted to the universal value-free event. Ordinary JSON, Markdown, stderr, and SARIF use an artifact alias plus numeric region. F31 does not create a raw-path CI exception. If a future product requirement needs one, it requires a separately approved artifact threat model rather than a hidden bypass in this contract.

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

For every surface, the harness records at least one expected registered safe event and asserts it is present before checking marker absence. This non-vacuity assertion prevents a disconnected or empty sink from passing. For a surface expected not to exist, such as the default engine log file, assert both that the file is absent and that another known surface observed the operation.

The matrix runs success and failure in default, quiet, verbose, JSON, and file modes where meaningful:

| Repository | Surfaces scanned byte-for-byte |
|---|---|
| engine | callback sink, progress v1 compatibility output, reason output, isolated envelope, in-process result, warnings capture, stdlib logs, attempted engine file, diagnostic and evidence projections, subset status, support payload fixture, subprocess captures |
| CLI | stdout, stderr, Rich capture, `--json`, `--quiet`, `--verbose`, ordinary `--output` files, report files, evidence, progress, warnings, command usage errors, all uncaught-command guards |
| Platform | API success and error bodies, server and worker logs, job progress and runtime facts, audit rows, evidence JSON and HTML, telemetry, diagnostics, support bundles, warnings, child logs, Sentry event fixture, OpenTelemetry attributes |

Each recorder scans text, bytes, JSON keys and values, filenames created under its temporary root, captured log records before formatting, and nested database JSON. It also asserts no `Traceback`, source line, absolute path, URL user information, or traceback-local representation appears. Property tests generate Unicode, control characters, long values, path variants, nested containers, exception subclasses, and alias collisions.

The unsafe channel has a separate test. It proves explicit opt-in, label, permissions, no symlink overwrite, no stdout or stderr destination, and exclusion from every ordinary recorder. It may contain its seeded marker by design.

### Construction and emit-time tests

Author tests before implementation for:

- direct construction, dataclass replacement, subclassing, object mutation attempts, duplicate fields, unknown enums, booleans as integers, NaN and infinity, huge values, oversized collections, and arbitrary dictionaries;
- clean source names being aliased just like hostile names;
- invalid construction raising fixed programming errors that do not echo the rejected value;
- emit-time revalidation detecting forged nested state;
- unknown field and schema mapping to `safe_output.invalid`;
- registry or serializer failure producing the exact emergency literal with no logger call;
- sink failure producing no raw fallback and restoring ambient state;
- exception chains and locals becoming unreachable after conversion;
- legacy F29 progress and STORM schemas being projections of the canonical event rather than independent validation paths.

### AST and taint sentry

Generalize `tests/sentry/test_log_interpolation.py` into one symbol-aware analyzer shared by engine tests. Scan all production Python outside `safe_output` and exact reviewed data-plane sinks. Resolve:

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

Outside the boundary, an ordinary sink call is legal only when its data argument is the direct result of `serialize_safe_event` or a branded safe mapping carried by `SafeEventSink`. No local variable named `safe` is trusted. The analyzer follows simple assignments and returns within a function, rejects reflective or unresolved dispatch near a sink, and fails closed on newly discovered sink APIs.

For every `except ... as exc`, uses of the caught name are limited to `type(exc)`, `isinstance`, bare re-raise inside internal control flow, or direct handoff to `safe_event_from_exception`. Attribute access, formatting, string conversion, logging, returning, closure capture, container insertion, chaining, and passing to another function fail the sentry unless that exact function is the boundary.

An exact manifest names data-plane writer functions and their artifact class. A code review and sentry test fail if the manifest grows without a matching threat-model entry. Directory exclusions and generic `# noqa` are prohibited.

Add Semgrep taint rules from exception, path, config, report, row, connector, and extension sources to output sinks. Semgrep supplements the AST rule, particularly across helper calls, but the local analyzer remains the build gate.

### Sentry self-tests

Use in-memory source fixtures and temporary packages to seed one violation for every sink class and every evasion class: aliased `print`, assigned logger, `getattr(console, "print")`, `io.open`, `Path.write_text`, exception f-string, `logger.exception`, `exc_info`, warning text, JSON dump, subprocess inheritance, captured-output replay, tracker extras, and a fake branded variable. Assert the analyzer reports the expected file and rule.

The runtime harness monkeypatches one renderer to append a marker after serialization and asserts the surface scan fails. It monkeypatches construction and serialization failure separately and asserts only the fixed fallback appears. CI records these as self-test passes, not by committing a broken production fixture.

## Implementation slices

Every slice begins with its acceptance tests in RED, preserves the failure output, implements the smallest change, runs the self-check, then receives an adversarial review and remediation. No slice may weaken, delete, xfail, or broaden an assertion to turn green.

### Slice 0: typed model, registry, RED corpus, and sentries

Scope:

- Add the `safe_output` package, schema constants, frozen model, aliases, registry entries, serializer, emergency literal, and public exports.
- Add the shared marker corpus, construction tests, runtime surface recorder primitives, generalized AST analyzer, Semgrep rules, and sentry mutation fixtures.
- Record baseline RED evidence showing current direct sinks, free fields, default log file, verbose tracebacks, and seeded markers.
- Bump engine to 0.8.0 only when the model and compatibility manifest are green.

Self-check:

```bash
pytest -q tests/unit/safe_output tests/security/test_safe_output_boundary.py tests/sentry/test_value_free_boundary.py tests/sentry/test_log_interpolation.py
ruff check src/decoy_engine/safe_output tests/unit/safe_output tests/security tests/sentry
mypy src/decoy_engine/safe_output
```

Adversarial review: attempt direct dataclass construction, forged internals, generic token secrets, hostile numeric objects, serializer monkeypatches, aliases, reflective sink access, and sentry bypass. Remediate every BLOCKER and HIGH before Slice 1.

### Slice 1: exceptions and ambient containment

Scope:

- Implement exception conversion over `reason_adapter` and closed `SafeBoundaryError` propagation.
- Implement `SafeOutputScope`, logging handler, warnings capture, Python stream containment, subprocess policy, and exact state restoration.
- Remove the default engine file handler and raw fallback warning.
- Migrate fixed-width and extension/provider catch seams first because they exercise raw buffers and arbitrary errors.

Self-check:

```bash
pytest -q tests/unit/execution/test_isolated_safe_error.py tests/unit/profile tests/security/test_reason_boundary.py tests/security/test_safe_output_boundary.py tests/sentry/test_value_free_boundary.py
pytest -q tests/unit/transforms/test_text_mask.py tests/connectors
```

Adversarial review: nested contexts, threads, cancellation, handler failure, warning formatter failure, malicious `__str__`, chained exceptions, raw bytes in locals, child output floods, and state-restore exceptions. Confirm no original exception remains reachable from an exported object.

### Slice 2: engine progress, logging, callbacks, and isolation

Scope:

- Convert F29 progress events and `context.py` callbacks to `SafeEventSink` factories.
- Replace all direct production narrative logs and warning emissions with safe events.
- Convert isolated worker envelopes, `IsolatedRunResult.error`, row error summaries, child exit reporting, and serializer `default=str` paths.
- Maintain progress v1 and legacy compatibility only through serializer projections.

Self-check:

```bash
pytest -q tests/unit/execution tests/unit/progress tests/security/test_reason_boundary.py tests/security/test_safe_output_boundary.py tests/sentry
rg -n 'default=str|logger\.exception|exc_info=True|print_exception|format_exc' src/decoy_engine
```

The final `rg` must produce only reviewed internal data-plane or boundary definitions recorded in the sentry manifest. Adversarial review injects markers through every logger callback, warning, isolated envelope key, child stream, and compatibility serializer.

### Slice 3: engine evidence, diagnostics, validation, and manifests

Scope:

- Add explicit safe event families for validation, quality, row-error counts, fidelity, diagnostics, subset status, publication, and support facts.
- Replace exported `quality_metrics`, warning details, `repr(exc)`, real schema names, row-error prose, and arbitrary evidence dictionaries with typed projections and aliases.
- Preserve raw data only inside typed data-plane artifacts and private computation. Do not relabel a report dictionary as safe.
- If STORM phase envelopes have landed, adapt them here through explicit event families.

Self-check:

```bash
pytest -q tests/unit/validation tests/unit/quality tests/unit/subset tests/unit/execution tests/security/test_safe_output_boundary.py tests/sentry
python scripts/test_flight.py
```

Adversarial review traces one marker from each internal raw report to every exported projection, checks alias stability and collisions, and verifies customer diagnostic privilege does not change the output class.

### Slice 4: CLI consumer, independently releasable

Prerequisite: published engine 0.8.0. Pin it exactly.

Scope:

- After STORM merges, retain `_value_free.py` command guards and renderers but delegate construction, prose, exception conversion, silencing, and serialization to engine 0.8.0.
- Convert `_projections.py` into thin schema adapters over engine event factories. Always alias table and field names. Never read engine message or detail fields.
- Migrate every command's stdout, stderr, JSON, quiet, verbose, ordinary file, report, evidence, warning, and progress path.
- Integrate F43 `SafeFinding` and engine logger suppression as event families and scope delegation. Preserve raw data stdout only for commands explicitly documented as data-plane operations.

Self-check:

```bash
pytest -q tests/sentry/test_value_free_boundary.py tests/sentry tests/e2e/test_output_modes.py tests/e2e/test_scan_command.py tests/e2e/test_report_command.py tests/e2e/test_run_command.py
pytest -q
ruff check src tests
mypy src/decoy
```

Adversarial review runs every command mode with hostile paths, reports, extension exceptions, usage errors, layout failures, engine logs, and output-file failures. Existing STORM and F43 assertions must remain at least as strict.

### Slice 5: Platform consumer, independently releasable

Prerequisite: published engine 0.8.0. Pin it exactly. It does not depend on Slice 4.

Scope:

- Replace `fidelity_projection.py` policy with a thin adapter to engine event factories while preserving route schema and numeric facts.
- Migrate API errors, job and progress events, server and worker logs, audit event details, evidence, telemetry, runtime facts, diagnostics, support bundles, warnings, and third-party tracker events.
- Install Platform sink adapters for database audit, structured logs, Sentry, and OpenTelemetry. These accept canonical mappings only.
- Preserve audit chain, transaction, authorization, and retention behavior. F31 changes payload representation, not audit durability.
- Standardize owned child processes on bounded captured streams and closed-code results.

Self-check:

```bash
pytest -q tests/test_fidelity_typed_projection.py tests/test_post_scan_fidelity_projection.py tests/test_quality_shape_fidelity_persistence.py tests/test_audit_api.py tests/test_evidence_assembly.py tests/test_evidence_bundle.py tests/test_evidence_redaction.py tests/test_obs3_logging.py tests/test_s4_cloud_creds.py tests/test_s7_fixed_width_layouts.py tests/test_safe_output_boundary.py
pytest -q
ruff check api tests
mypy api
```

Use the actual test module names present after rebase if a listed aggregate file is split, but do not reduce the named surface coverage. Adversarial review queries persisted database JSON and rendered HTML in addition to captured HTTP and logs.

### Slice 6: convergence, release evidence, and duplicate removal

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
10. CLI and Platform lock exactly to the released engine contract.

Coverage percentage alone is not acceptance evidence. Add branch and mutation attention specifically to constructors, emit-time revalidation, emergency fallback, exception conversion, state restoration, alias allocation, AST resolution, and tracker adapters. A surviving mutation in one of those functions blocks release.

### Rollback

Engine 0.8.0 is additive at import level but coordinated at output behavior. Rollback means pinning CLI or Platform back to their previous exact engine version and previous consumer commit together. Never roll back only the engine beneath an F31 consumer. Keep `decoy-safe-output/v1` readers forward-compatible with unknown registered reasons by mapping them to `safe_output.invalid`; do not silently re-enable raw output. Database migrations are not required for the contract. If a consumer adds typed columns, its slice supplies its own reversible migration and rollback test.

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
- state that administrator and diagnostic privileges do not relax ordinary evidence;
- document tracker defaults and the prohibition on locals, request bodies, raw breadcrumbs, and raw exceptions;
- update operations and support runbooks for the unsafe artifact isolation rule.

Barry performs the repository documentation sync after code review as required by `AGENTS.md`. Documentation examples run through the same marker scan so prose fixtures cannot introduce a bypass pattern.

## Risks and mitigations

| Risk | Mitigation and acceptance evidence |
|---|---|
| A safe-looking token contains a secret | No generic token field. Only registry enums, exact UUID or ULID variants, and ordinal aliases. Seed clean identifiers and token-shaped secrets. |
| A forged event bypasses construction | Frozen slotted types, exact nested types, canonical reconstruction at emit time, and mutation tests. |
| Failure handling leaks the rejected value | Fixed error messages, generic invalid event, literal emergency record, and monkeypatched serializer and logger failures. |
| Logging filters miss descendants or last-resort handlers | Boundary-owned handler, no default engine handler, propagation and last-resort tests, exact snapshot and restoration. |
| Global stream capture races | Reentrant process guard, nested and concurrent tests, process isolation option, declared native FD residual risk. |
| Exception locals survive in chains or frames | Convert in the smallest frame, do not store the exception, clear cause and context, drop traceback, ban locals capture, weak-reference reachability tests. |
| Aliases reveal ordering or cardinality | Ordering and bounded counts are explicitly allowed. Alias spaces are per operation and contain no hash of the source value. Document this residual metadata. |
| Numeric fields encode values | Closed field-specific semantics and ranges. No arbitrary metric name. Review each added numeric field as a schema change. |
| Existing detailed diagnostics become less useful | Add safe type, component, position, counts, and registry reason. Use the explicit isolated unsafe channel only when approved. Verbose never weakens safety. |
| Data-plane writers become a loophole | Exact function manifest, threat-model entry per writer, no directory exclusions, sentry review on growth, and status-only ordinary events. |
| STORM or F43 merge conflicts | Preserve their command and renderer structure, replace only duplicated policy with adapters after rebase, and keep their tests. No dependency on unmerged code. |
| Consumer schema drift | Exact engine pin, compatibility fixtures from canonical mappings, cross-repo corpus, and release engine first. |
| Sentry evasion through aliases or reflection | Symbol and assignment resolution, conservative failure on unresolved sink dispatch, Semgrep backstop, and explicit evasion self-tests. |
| Regex or secret scanners create false confidence | The construction model and sentries are the proof. Pattern scans are recorded only as defense-in-depth evidence. |
| Native driver writes directly to an FD | Use subprocess isolation for high-risk drivers, close or redirect inherited descriptors where supported, document remaining platform-specific risk. |
| Unsafe debug artifact is uploaded accidentally | New exclusive file, 0600, fixed label, no standard support or evidence discovery, separate retention, and exclusion tests. |

## Gate chain and handoff

### This plan

The plan gate is sequential: argon (`google/gemini-4-argon`) reviews this exact file, then one Opus-4.8 Dennis performs the repo-grounded gate, then the owner decides. Review findings are remediated in the plan before owner approval. No implementation begins from an unapproved revision.

### Code

Each implementation candidate follows: **objective-green evidence -> Codex review -> argon review -> one Dennis adversarial gate**. Resolve every BLOCKER and HIGH, rerun objective evidence on the changed exact commits, then have Barry synchronize documentation. Record the exact HEAD receipt with `bash .claude/hooks/decoy-gate-pass.sh`, then hand the evidence packet to the owner. Any new commit re-arms the gate.

The remote claude.ai/code workers receive only the remote `decoy-engine`, `decoy`, and `decoy-platform` repositories. This document therefore makes no assumption about local sibling paths. They must rebase onto the approved remote baselines, verify whether STORM has merged, use the cited branch only as reference if it is still available, release engine 0.8.0 first, and implement the CLI and Platform slices as independent exact-pin consumers. No prerequisite is invented from a local checkout.
