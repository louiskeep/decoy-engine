Status: plan (engine slices E0-E6 built; see [the build record](../records/2026-10-29-f29-engine-build.md). Consumer slices C0-C2 are not started.)

Summary: Give engine errors and progress one versioned reason registry, safe structured records, and enforceable producer boundaries.

Baseline commit: `2a44723ff26ffd16c69f026582ec93998d67ca5d` (`git rev-parse HEAD`, this checkout, 2026-10-29).

# F29: FRAME + PLAN

First draft for the Codex -> Gemini -> Dennis plan gate. This document grants no build or release approval.

Rules consulted: `/home/cam/dev-rules/` files `00-universal.md`, `development-loop.md`, `risk-and-exceptions.md`, `workspace-and-vcs.md`, `reuse-first.md`, `exploration.md`, `scope-discipline.md`, `verification.md`, `feature-dev.md`, `refactoring.md`, `testing.md`, `architecture.md`, `api-and-compatibility.md`, `data-and-migrations.md`, `observability-and-resilience.md`, `config-secrets-and-supply-chain.md`, `security.md`, and `documentation.md`. Repository guidance: `AGENTS.md`, `README.md`, `CODEMAP.md`, `CONTRIBUTING.md`, `CLAUDE.md`, and `SECURITY.md`.

Risk: **R2**, because this feature changes public schemas, compatibility, and sensitive error output. Gemini and Dennis review this plan before implementation. Implementation requires independent review and exact-artifact evidence under the house loop. The user-supplied MASTER contract overrides the general pre-GA deletion default for reason codes.

## FRAME

### Registry ownership and schema version

**The engine owns the registry.** CLI and platform consume its published descriptors and schemas. Neither consumer maintains a second taxonomy. Consumer-specific reasons enter the engine-owned catalog through reviewed additions, without importing consumer code.

Proposed public package: `decoy_engine.reasons` (new). Start with `registry_version="1.0.0"`, `reason_schema_version=1`, and `event_schema_version=1`. Registry content and wire schemas have independent versions. The existing unversioned events are legacy version 0.

Each immutable `ReasonSpec` owns these facts:

- A stable `(namespace, code)` identity, a fixed safe summary, and a producer owner.
- A kind: `failure`, `finding`, `warning`, `decision`, or `control`.
- Allowed phases and their severity, outcome category, and retry class. A phase-dependent mapping lives here once.
- A closed context schema, introduction version, deprecation version, and optional replacement identity.

Namespaces separate config, runtime, and route identities where existing spellings overlap. Existing `.code` strings remain byte-for-byte unchanged. Existing `CODES` attributes and enum imports become compatibility views of catalog entries. A namespace disambiguates a collision without renaming either wire string.

Additions increment the registry minor version. Editorial corrections increment its patch version. Incompatible interpretation requires a new identity or schema major version. Codes never disappear or acquire a different meaning. Deprecated entries remain readable as tombstones, including their original metadata and replacement link. A release manifest test rejects deletion, identity reuse, and silent changes to retry or outcome semantics.

### Problem and confirmed baseline

Operators need a stable reason and next action without parsing prose. The current engine has useful contracts, but their code ownership and failure metadata differ.

All engine anchors below were opened in this checkout. Proposed files are explicitly marked **new**. Indexed recall failed with `SQLITE_READONLY`. No `graphify-out/` exists here. Source reads are the evidence. No CLI or platform checkout supplied evidence.

| Confirmed source | Consequence for F29 |
|---|---|
| `src/decoy_engine/errors.py:25`, `:47`, `:196`, `:236` | `DecoyError` has no common code contract. Some subclasses accept optional codes. Others declare literal class codes. Preserve catch relationships and constructors. |
| `src/decoy_engine/validation_result.py:25`, `:45`, `:145` | `CODES` promises stable strings. `ValidationMessage` also carries prose. The new safe record must not serialize that object wholesale. |
| `src/decoy_engine/execution/_errors.py:21`, `src/decoy_engine/plan/_errors.py:24`, `src/decoy_engine/generation/pool/_errors.py:14`, `src/decoy_engine/providers_v2/_errors.py:33` | Runtime, compilation, generation, and provider families accept unrestricted strings. They need registry-backed producers and a common normalization boundary. |
| `src/decoy_engine/keyprovider.py:60`, `:88` | `missing_mask_secret` already identifies an unresolved mask secret. Preserve it. Do not invent a replacement spelling. |
| `src/decoy_engine/plan/_checks_grouped_series.py:156` | A code uses `f"grouped_series_missing_{ref_key}_ref"` (raised at `:155`). Literal-only searches cannot prove registry coverage. |
| `src/decoy_engine/progress.py:32`, `:99`, `:102` | Five event dataclasses cover phases, rates, routes, warnings, and terminal state. Serialization currently uses unrestricted `dataclasses.asdict`. No version, bytes, retry, or checkpoint fields exist on these types. |
| `src/decoy_engine/progress.py:129`, `src/decoy_engine/context.py:64`, `:97` | The legacy logger bridge loses event detail. Its optional protocol and old signatures require compatibility coverage. |
| `src/decoy_engine/execution/_isolated_common.py:243`, `:266`, `:393` | A separate trusted-code set feeds safe text. `IsolatedRunResult` carries text plus coarse outcomes, not a structured reason. |
| `src/decoy_engine/execution/_governor.py:163` | The governor reads route markers from safe text. Removing those markers now changes control flow. |
| `src/decoy_engine/execution/native/_route_decline_codes.py:23`, `src/decoy_engine/execution/physical/_reasons.py:7` | Routing decisions are distinct from runtime failures. F29 must preserve that distinction and the physical-seam import boundary. |
| `src/decoy_engine/execution/_pipeline_finalize.py:443`, `:475`, `src/decoy_engine/verification/postmask/types.py:128` | Validator enforcement raises. STORM post-mask reporting does not enforce findings. Reports distinguish `fail_count`, `error_count`, and `pass_failed_with`. |
| `src/decoy_engine/execution/_transactional_sink.py:52`, `:93`, `:299` | Transactional abort is best effort. The callable adapter can leave partial output. A returned abort call does not prove successful cleanup. |
| `src/decoy_engine/execution/_sequential.py:585`, `:610`, `:621` | Quarantine publication follows the table commit. A later failure must preserve committed tables and report partial publication. |

A source-wide AST probe found 839 literal `code=` call arguments, with 509 distinct strings, plus 189 nonliteral arguments. This is a search census, not a count of error identities. It excludes positional arguments and class constants, and includes unrelated `code` parameters. E0 completes the semantic inventory.

Relevant local precedents: `docs/plans/2026-10-24-parity-e1-progress-contract.md:42` defines the structured-event approach. `docs/plans/2026-10-19-obs-1b-engine-log-leak-scrub.md:1` records safe-summary hardening. `docs/plans/2026-10-19-obs-2-engine-evidence.md:15` requires producer attribution and complete propagation. `docs/records/2026-10-02-b6a-incremental-sink-build.md:18` records meaningful fail-before evidence. Their historical status text is not proof of current implementation.

### Established solutions, surveyed before design

External searches used `RFC 9457 problem details stable type extensions security`, `OpenTelemetry error.type exception message sensitive`, and `gRPC status codes failed precondition aborted unavailable retry`. The author opened the RFC, gRPC guidance, and dbt event documentation.

- [RFC 9457, sections 3 through 5](https://www.rfc-editor.org/rfc/rfc9457.html): separate stable problem identity from occurrence detail and transport status. Adopt that separation and its disclosure cautions. The engine has no HTTP surface, so its record is not an HTTP Problem Details response.
- [gRPC status-code guidance](https://grpc.io/docs/guides/status-codes/): distinguish retrying an operation, restarting a larger operation, and repairing state first. Adopt those distinctions without adding an RPC dependency.
- [dbt events and logs](https://docs.getdbt.com/reference/events-logging): typed events support different consumer renderers. Extend the engine's existing pattern at `src/decoy_engine/progress.py:1`.

Design notes: one catalog removes competing facts. A small normalizer separates exception implementation from consumer policy. Typed occurrence context prevents arbitrary metadata dictionaries. Standard-library enums, frozen dataclasses, mappings, and AST inspection are sufficient. No message bus, retry framework, protocol generator, or new serialization dependency is justified. Implementing module docstrings must cite the relevant source pattern and explain the local adaptation.

### Retry taxonomy and outcome categories

Retry classification is advice about the required action. It never authorizes replay or starts a retry loop.

| Retry class | Meaning |
|---|---|
| `never` | Repeating this operation cannot resolve the reason, or policy prohibits replay. Includes findings and control outcomes. |
| `configuration_repair` | Repair config, credentials, or secret resolution before a new attempt. |
| `environment_repair` | Repair capacity, access, or another environmental precondition before a new attempt. |
| `retry_operation` | A known transient reason permits retry consideration at the operation boundary. The owner must establish idempotency and a bounded budget. |
| `restart_attempt` | Recovery requires a new attempt, possibly from a verified checkpoint. Cleanup and replay safety remain separate conditions. |
| `unknown` | Insufficient evidence. No automatic retry recommendation. |

`missing_mask_secret` maps to `configuration_repair`. Typed `ENOSPC` or `EDQUOT` evidence at a sink maps to a proposed `storage_capacity_exhausted` reason and `environment_repair`. Generic `OSError` does not imply capacity. A failed commit with unknown publication state cannot recommend immediate replay. Unknown exceptions map to `unexpected_error` with `unknown`. These new spellings are proposals for the catalog, not claims about existing codes.

The engine exports semantic outcome categories. The CLI owns process exit integers.

| Category | Contract |
|---|---|
| `execution` | Processing or strategy execution failed. Uncovered `RowErrorsFailedError` belongs here. |
| `findings` | A check completed and reported findings. Report-only mode remains report-only. Findings do not imply a broken verifier. |
| `verification` | An enforcing check rejected output, or the verifier could not complete. Separate reasons distinguish `verification_failed` from `verification_unavailable`. `ValidatorFailedError` is an enforced negative verdict. |
| `publication` | Staging, writing, commit, or a required sidecar publication failed. Context distinguishes these operations and partial publication. |

Additional categories are `configuration`, `cancelled`, and `success`. Existing CLI usage, authentication, and other coarse categories remain consumer policy. A pause signal remains a control outcome, not an unexpected execution failure (`src/decoy_engine/errors.py:120`). Direct calls continue to propagate interrupts and existing exceptions.

The first causal failure remains primary. Cleanup failure is secondary structured state. A publication failure after successful verification cannot become a verification failure. A verifier infrastructure error takes precedence over its findings. Report-only findings cannot change `run_pipeline` from success to failure. The CLI must preserve these distinctions in structured output immediately. Distinct new exit integers require an explicit, grounded compatibility decision in C1.

### Safe fields and behavior contract

**F31 is a hard output constraint.** Error records, events, and consumer presentation must pass through its safe boundary. F29 supplies typed reasons and fields. It does not implement a parallel redactor or treat a regex-shaped code as trusted.

The confirmed local foundation is `exception_error_text` at `src/decoy_engine/execution/_isolated_common.py:266` and the policy at `docs/security/error-reporting-and-data-exposure.md:25`. A F31-named plan or public boundary was not found in this checkout. Its exact interface is **asserted, verify before build**. E0 must identify the sibling artifact and its owner before E2 integrates output. Catalog and schema tests can proceed independently after the plan gate. No invented F31 filename is a prerequisite.

Proposed `ReasonRecord` has explicit fields: schema version, registry version, namespace, code, phase, severity, retry class, outcome category, and typed context. The record contains no exception object or arbitrary extension dictionary. Context contains only approved object references, bounded counts, fixed operation identifiers, and publication evidence. Registry metadata supplies safe summaries. Callers cannot override the summary or retry class with free text.

Version-1 phases are `configuration`, `resolve`, `compile`, `mask`, `generate`, `finalize`, `verify`, `publish`, `cleanup`, and `unknown`. Existing event names remain compatibility labels. Reason phase comes from the operation boundary, never from message text. Severity uses the existing `info`, `warning`, and `error` vocabulary.

| Event field group | Version 1 contract |
|---|---|
| Identity | `event_schema_version`, `registry_version`, existing `kind` and `ts`. A stream-local monotonic `sequence` supports ordering. Transport owners attach job and attempt IDs. |
| Phase and object | Explicit `phase` and an optional typed `object_ref`. Existing `name`, `table`, and `column` remain compatibility fields. Identifiers pass F31 policy and stay only in designated fields. |
| Rows | Preserve existing row fields and meanings. Counters describe a specific phase, object, and attempt. A final sample means observation ended, not successful publication. |
| Bytes | `bytes_done`, optional `bytes_total_estimate`, and `byte_basis`. `arrow_logical` counts Arrow batch `nbytes`. `stored_output` counts serialized bytes after writer close. Unknown is null, not zero. Bases never mix in a counter. |
| Time | `elapsed_s` uses a monotonic clock. `ts` is wall time. Durations must be finite and nonnegative. |
| Reason | Optional `reason: ReasonRecord`. A success event has no fabricated error. Route and warning reasons retain their own kind and severity. |
| Retry | Optional typed `retry` with scope, attempt number, retries completed, state, and optional delay. States: `not_scheduled`, `scheduled`, `running`, `exhausted`. Attempt numbers start at one. Only the retry owner reports its schedule. |
| Checkpoint | Optional typed `checkpoint` with state, opaque reference, sequence, rows, and bytes. States: `unsupported`, `none`, `writing`, `committed`, `failed`. Only the durability owner can report `committed`. |
| Publication context | Operation, approved `storage_ref`, `publication_state` (`not_published`, `published`, `partial`, `unknown`), and `cleanup_state` (`not_needed`, `attempted`, `confirmed`, `failed`, `unknown`). No filesystem path. |

The safe boundary excludes values, expressions, connection strings, secret references, raw exception text, causes, tracebacks, child streams, and arbitrary `.code` attributes. Exception type names require the same boundary policy. A hostile class name must not become a disclosure route. Unknown identities become fixed fallback reasons without echoing the rejected identity. Invalid optional telemetry is omitted or replaced with a fixed diagnostic. It never masks the original result.

The serializer accepts only explicit event variants and field allowlists. Numeric fields reject booleans, negative counts, nonfinite floats, and inconsistent retry or checkpoint combinations. Unknown optional fields are ignored on read within a supported schema major. An unknown major produces a fixed unsupported-version diagnostic, without claiming success or retry safety. Known but newer registry content falls back safely in older consumers. Concretely, a consumer that reads a `ReasonRecord` whose `(namespace, code)` is unknown but whose `registry_version` shares the consumer's supported major replaces the identity with a fixed well-known reason (`decoy`, `unknown_reason_from_future`), keeps the original namespace and code only in a non-interpreted safe context field, and never crashes, drops the record, or treats it as success. The engine serializes the full identity; the older consumer degrades to the fixed reason without losing the audit trail.

New dataclass fields use defaults and keyword-only parameters to preserve positional constructors. `event_to_dict` emits version 1. A separately named legacy projection preserves version-0 keys for consumers that require them. Both projections use the same safe boundary. The existing `StructuredEventsSink` remains a documented lossy adapter. A versioned sink receives the complete record without changing old logger signatures or duplicating delivery.

Progress remains observational. Outputs, route choices, exception catch behavior, and iterator laziness remain unchanged. Sink `Exception` failures remain counted and suppressed (`src/decoy_engine/progress.py:203`). Delivery remains synchronous, so a blocking sink can delay execution. F29 introduces no queue or new delivery guarantee. Terminal delivery is attempted once per owned run scope. A terminal sink failure cannot appear retrospectively in that same event.

Direct error callers receive reasons through a public `reason_from_exception` helper. `IsolatedRunResult` gains an optional reason field. Worker envelopes carry that record explicitly, with parent-side validation. Successful `ExecutionResult` remains the data result (`src/decoy_engine/execution/_adapter.py:42`). A report adapter exposes findings and verification reasons without serializing reports wholesale.

The helper accepts typed occurrence context for generic exceptions. Without that context, it reports unknown phase and publication state. Each owned run keeps its first failure record independently of the progress sink. A shared no-op channel cannot retain this state. Operation observers capture the record before their scope closes. Outer scopes reuse it instead of relabeling the failure. Worker transport and terminal events therefore share the same record, even without a sink. Compatibility marker extraction must tolerate hostile exception methods without masking the primary failure.

Scope limits: no new retry executor, checkpoint store, verification policy, or atomic-publication algorithm. Existing F23/F30/F31/F33 mechanisms supply their own evidence. Engine routes without checkpoint support report `unsupported`. A commit is not a resume checkpoint. Isolated live progress remains unsupported unless separately grounded and approved: `tests/unit/progress/test_run_pipeline_progress.py:259` pins its rejection. F29 must not advertise live isolated progress merely because terminal reasons cross the process boundary.

### Failure modes and acceptance tests, defined before implementation

New test paths below are deliverables, not existing files. A1 through A10 are the acceptance contract. Builders must preserve their assertions.

| ID and proposed test | Required observations |
|---|---|
| **A1** `tests/unit/test_reason_registry.py::test_v1_manifest_is_append_only` | Snapshot every baseline public code and alias. Reject deleted identities, reused meanings, conflicting namespace entries, changed retry semantics, and invalid deprecation links. The snapshot includes each code's fixed safe summary text, so changing an existing code's summary fails the test: a summary edit requires a new code or a registry major bump. Import old `CODES` and exception classes unchanged. |
| **A2** `tests/unit/test_reason_registry.py::test_every_producer_resolves` | Derive coverage from the complete E0 inventory. Exercise positional, class-level, dynamic, wrapped, and forwarded codes. Every built-in producer resolves without fallback. Generated code families become finite tables, conditional on E0's finite-table determination: an E0-approved redesign for an intractably large code space is not an A2 failure. |
| **A3** `tests/security/test_reason_boundary.py::test_hostile_exception_never_escapes` | Inject different markers into message, code, class name, cause, context, path, secret reference, and child streams. Inspect actual serialized events, isolated results, and log calls. Reject markers and unexpected fields. Include raising `__str__`, `code` properties, and an untrusted exception with a known code. Include a value-safe but pathologically large context (for example a mapping or list with millions of entries): the record must bound-truncate with a diagnostic or reject it with a fixed low-resource error, never consume unbounded memory. Preserve the original exception. |
| **A4** `tests/integration/test_f29_failure_contract.py::test_secret_capacity_and_unknown` | Real missing-secret resolution retains `missing_mask_secret` and requires config repair. Inject `ENOSPC` during write and footer close, `EDQUOT`, unrelated `OSError`, and an unknown callback exception. Assert distinct reason, retry advice, phase, storage reference, publication state, and truthful cleanup state. No raw paths. |
| **A5** `tests/integration/test_f29_outcomes.py::test_findings_verification_and_publication_are_distinct` | Cover report-only findings, enforced negative verdict, verifier exception/error report, strategy error, commit error, and pause/cancel. Include mixed findings plus verifier failure. Preserve report-only success and existing catch types. |
| **A6** `tests/unit/progress/test_versioned_contract.py::test_schema_and_legacy_projection` | Exact key sets, version dispatch, null versus zero, numeric bounds, opaque references, and constructor compatibility. Exercise all event variants through callback, dictionary, legacy logger, and complete versioned sink. Unknown code/version cannot leak or become success. |
| **A7** `tests/unit/progress/test_counters.py::test_counts_follow_consumption` | Use controlled clocks and generated Arrow batches. Assert exact logical bytes and rows per scope, no mixed byte bases, bounded sample count, and one final sample. Empty input, early iterator close, failure after a batch, nested runs, and multiple tables cannot double-count or imply completion. Compare stored bytes with real closed files. |
| **A8** `tests/integration/test_f29_isolation.py::test_direct_worker_parent_reason_parity` | Compare direct, `isolate=False`, and worker-envelope failures. Cover both parent/in-process commit branches, timeout, abnormal death, missing/corrupt reason payload, old envelope, and unknown schema. Preserve coarse isolated outcomes and governor reroute decisions. Inject an exception whose string form embeds a valid governor route marker and assert the governor does not reroute on it: only trusted structured route decisions, never arbitrary exception text, change control flow. Cleanup failure cannot replace the primary reason. Nested and successive no-sink runs cannot share failure state. |
| **A9** `tests/integration/test_f29_publication_state.py::test_post_commit_failure_preserves_output` | Fail a quarantine sidecar after table commit. Assert `partial` publication, no abort of committed tables, and no automatic replay advice. For best-effort abort and custom sinks, assert `attempted` or `unknown` unless evidence proves cleanup. |
| **A10** `tests/unit/progress/test_retry_checkpoint.py::test_owner_reports_truthful_state` | A retry classification alone emits no scheduled retry. Assert supported/unsupported checkpoint states, committed-reference requirements, retry budget exhaustion, per-attempt resets, and no regression of checkpoint sequence. Consumer fixtures prove one failed attempt plus one successful attempt without double-counting. |

Extend existing progress regressions at `tests/unit/progress/test_channel.py:57`, `:68`, `:103`, `:182` and safe-summary coverage at `tests/unit/execution/test_isolated_safe_error.py:366`. Compare pinned output fixtures and schemas across full-frame, chunked, sequential, out-of-core, and generation routes. Use companion-present and companion-absent runs where native paths change. A recording sink, absent sink, and raising sink must produce the same data and original exceptions.

For each new guarantee, run a meaningful fail-before case against the baseline. Keep positive controls beside leak assertions. Use Hypothesis for batch partitions, null/empty cases, and counter invariants. Record changed-unit line/branch coverage and mutation results for normalization, publication classification, and the sentries. A surviving security or classification mutant requires a fix.

## Registry choke-point and SENTRY

`decoy_engine.reasons` owns code creation and descriptor lookup. Producers reference registry symbols. `reason_from_exception` recognizes trusted engine families and registered code identities. It never trusts an arbitrary object's `.code`. Public constructors can retain their legacy string inputs, but unknown inputs cannot cross the structured output boundary unchanged.

The complete public path is: **producer -> registry descriptor + typed occurrence context -> F31 safe boundary -> serializer/consumer**. The registry has no imports from execution, physical planning, CLI, or platform. Existing catalogs import its leaf descriptors. This removes `_TRUSTED_CODES` duplication without pulling the physical seam into production execution.

Concrete deliverable: **`tests/sentry/test_reason_registry.py::test_no_ad_hoc_reason_codes`**.

- Scan every Python source file, including optional modules, without importing those modules. Discover engine exception families and code-bearing record constructors through imports and inheritance.
- Inspect positional and keyword code arguments, class assignments, `.code` writes, `reason_code`, warning/quarantine emissions, and serialized reason fields. Resolve local aliases and finite lookup tables.
- Reject literal definitions outside catalog data, computed strings, unchecked forwarding, and new unclassified code-bearing sinks. Dynamic public input must enter the normalizer. Permit only symbol-based built-in forwarding.
- Keep an exact inventory of adapter functions, not a directory exemption. Every exception requires a rationale and a test. Temporary migration entries can only decrease. No migrated built-in producer retains an ad-hoc exemption at F29 completion.
- Seed synthetic source mutants: aliased constructor, positional code, f-string code, helper return, class constant, dict payload, and a newly added module. Each mutant must fail the sentry.

Companion deliverable: `tests/sentry/test_reason_registry.py::test_reason_exports_use_safe_boundary`. It guards production event delivery and reason serialization, including direct dataclass construction and dictionary assembly. Runtime field validation backs the static guard. The guard is not a claim of whole-program taint analysis.

Existing precedents are `tests/sentry/test_exception_hierarchy.py:121` and `tests/sentry/test_log_interpolation.py:8`. The CI regression job already collects `tests/sentry` (`.github/workflows/ci.yml:124`, `:143`). Add a collection assertion for both new guards and their mutation fixtures. Preserve the existing leak and physical-seam guards.

The invariant covers public machine-readable reasons, including config, runtime, warnings, and exposed route decisions. Free-text `RowError.reason` is not a code. It stays excluded from safe records, consistent with `src/decoy_engine/execution/_row_error_summary.py:3`. Private diagnostic prose can remain private. It cannot populate a public reason field.

## PLAN

### Engine slices

These slices belong in this checkout. E0 and E1 are buildable after the plan gate. Later integration slices have the stated dependency gates.

| Slice | Scope and files | Acceptance and gate |
|---|---|---|
| **E0: inventory and freeze** | Enumerate producers, wrappers, consumers, class constants, positional codes, warnings, decision catalogs, and transport fields. Start from the confirmed baseline table. Add **new** `tests/fixtures/f29/reason-baseline.json` and a producer inventory beside it. Record source symbols and namespaces, including every exclusion. Ground F31's actual safe boundary and the F23/F30/F33 interfaces that supply retry/checkpoint evidence. | A1/A2 baseline fixtures first. Gate: review the complete inventory, collisions, safe fields, and semantic outcome mapping. No blanket classification of all `DecoyError` instances. E0 must assess whether every dynamic or interpolated code (for example `_checks_grouped_series.py`) can migrate to a finite set of registry entries; if a producer's code space is intractably large, a revised design for that producer is approved before E1 begins. Missing sibling interfaces block only dependent integration. |
| **E1: registry and enforcement** | **Move Field / Replace Magic Literal**: add **new** `src/decoy_engine/reasons/` with descriptors, immutable catalog data, normalizer contracts, and manifest export. Move definitions from `validation_result.py:145`, `errors.py:196`, `_errors.py:21`, and the catalog sites above. Migrate source families in inventory-sized commits. Replace `_checks_grouped_series.py:155` interpolation with explicit finite entries. Add both sentries. | A1/A2 plus sentry mutants. Gate: old strings and imports survive, every inventory row resolves, no fallback for known built-ins, no physical-seam import violation. Keep catalog modules under the house size limit. No parallel handwritten trust list. |
| **E2: safe reasons and transport** | **Introduce Parameter Object / Move Function**: implement the typed occurrence context and public helper through the grounded F31 boundary. Update `errors.py:25`, `progress.py:331`, `_isolated_common.py:266` and `:393`, `_isolated_worker.py:190`, `_isolated_run.py:254` and `:521`. Add the report adapter at the public reasons surface, using `verification/postmask/types.py:128`. Export through `src/decoy_engine/__init__.py:169`. | A3/A5/A8. Gate: F31 interface and safe identifier policy grounded first. Parent validates worker reasons. Old envelopes remain readable. Preserve governor markers at `_governor.py:163` until a separately proven typed migration replaces them. No message parsing for new reasons. |
| **E3: publication evidence** | Observe write, close, commit, and abort boundaries at `_transactional_sink.py:157`, `:216`, `:295`, `:306`, `_isolated_commit.py:73`, `_chunked_output_sink.py:127`, `_sequential.py:585` and `:610`, and `out_of_core/_runner.py:309`. Add a shared typed publication-observation helper (**new**, under `execution/`). Keep exception types and existing publication order. Record evidence without replacing sink algorithms. | A4/A5/A9. Gate: writer faults, close faults, both isolated commit paths, failed cleanup, and post-commit sidecar failure all pass. Add `test_publication_sites_use_observer` to the sentry for engine-owned sink operations. Custom sinks without evidence report unknown state. |
| **E4: versioned progress** | Extend `progress.py:32`, `:102`, `:185`, `:315`, and `context.py:97` through the same safe boundary. Add typed fields and explicit serialization. Count logical bytes alongside rows without extra iteration. Observe stored bytes at the closed writer. Preserve the legacy bridge at `progress.py:129`. State whether v1 `event_to_dict` output is a strict additive superset of the v0 keys so existing callers are unaffected, and pin it with a key-set compatibility assertion (the same explicit-decision rigor C1 applies to CLI exit integers). The existing driver binding is `_pipeline.py:268`. | A6/A7 and existing route parity tests. Gate: exact schemas, lazy consumption, finite counters, synchronous sink behavior, no duplicate phase/terminal events, and unchanged output. No live isolated progress claim. |
| **E5: owner-driven retry/checkpoint events** | Add validated owner reporting methods to the progress channel. Use `_governor.py:341` (the route-ladder loop; trips list at `:335`) as the grounded attempt-loop integration point for route attempts. Keep job attempts distinct from governor route attempts. Consumer-owned schedules and durable checkpoints remain C2 work. | A10 plus governor equivalence from A8. Gate: existing reroute policy unchanged, no invented checkpoint completion, and no new retry executor. Missing checkpoint support emits `unsupported`. Ground each owner before integrating it. |
| **E6: compatibility package and documentation** | Export a deterministic catalog/schema fixture under **new** `tests/fixtures/f29/`. Publish consumer contract cases for C1/C2. Barry updates the existing security reference at `docs/security/error-reporting-and-data-exposure.md:25`, public API documentation located during E0, and `CHANGELOG.md`. | All A1-A10 plus sentries and project checks. Dennis reviews the exact implementation artifact. Gate evidence records tested routes, optional dependencies, skips, coverage, mutations, and consumer work still pending. Engine completion alone does not complete cross-repo F29. |

Use the repository lint, format, type, and regression commands. The confirmed CI command is `pytest tests -m "not benchmark and not packaging and not codspeed"` (`.github/workflows/ci.yml:143`). Native changes also require their companion matrix. Resolve every BLOCKER and HIGH before the implementation gate. Follow the repository's Dennis/Barry gate sequence before any push or PR creation. This draft does not run those later gates.

Compatibility rollout: ship the additive engine package and fixtures first. Upgrade consumers only after their contract tests pass. Preserve legacy projections and tombstones throughout the version-1 window. Rollback restores consumer pins without deleting stored version-1 events. Readers must retain version dispatch. Contract changes cannot silently rewrite historical records.

### CLI and platform slices: GROUND BEFORE BUILDING

Every consumer source anchor in this section is **asserted, verify before build**. The supplied MASTER audit is authoritative task input, not a source-code inspection. Neither repository is present here. Numeric exits, commands, logger signatures, persistence models, and transport paths require fresh grounding.

**C0: consumer grounding gate.** Record both baseline commits and open the real callers, exit mapper, error renderer, progress serializer, persistence model, and retry/checkpoint owners. Locate the F31 consumer boundary and F23/F30/F33 contracts. Replace asserted anchors with confirmed file:line evidence in the consumer plans. Locate the referenced F31 plan and mirror its split gate. No consumer implementation starts before this record passes review.

**C1: CLI mapping and output.** Consumer: `decoy` error/exit and progress entrypoints, **asserted, verify before build**, exact files unknown. Create an exhaustive table of existing reason/command -> exit integer behavior. Preserve the current coarse taxonomy by default. Map configuration and execution reasons to their current categories. Preserve existing findings behavior. Reserve distinct verification and publication categories in the structured result.

Before changing numeric exits, record an explicit compatibility decision: exact old/new integers, affected commands, default behavior, version boundary, and migration examples. Until that decision, no numeric remapping ships and this part of F29 remains partial. Acceptance: subprocess tests for execution, findings, enforced verification failure, verifier outage, and publication failure. Also cover unknown reasons, old engine payloads, structured-output framing, and F31 leak sentinels. Gate: C0, E6 fixtures, approved exit decision, and CLI review.

**C2: platform presentation and persistence.** Candidate `decoy-platform/api/persistence/logger.py::JobLogger`, **asserted, verify before build**, appears only in the local historical E1 plan. The actual job/attempt models, SSE adapters, and checkpoint owners have no confirmed anchors. Store schema version, registry version, safe reason, category, and attempt association. Show the same reason as the CLI, with approved storage references and actual cleanup state. Store unknown safe-version payloads only under the grounded reader policy, never as trusted success.

Acceptance: current and old persisted events, additive schema rollout, duplicate/out-of-order delivery, attempt transitions, unsupported checkpoints, committed checkpoints, and exhausted retries. The UI must distinguish findings, failed verification, and failed publication. A failed presentation sink must not change job outcome. Gate: C0, engine fixtures, persistence migration/rollback evidence, F31 boundary tests, and platform review.

## Cross-repo contract and completion

| Owner | Publishes or consumes |
|---|---|
| Engine | One versioned descriptor catalog, immutable reason records, exception/report adapters, explicit event serializers, schemas, legacy projections, and compatibility fixtures. No CLI exit numbers or platform imports. |
| CLI | Maps engine categories to grounded coarse exits. Shows safe reasons and next actions without parsing messages. Numeric compatibility decisions belong to C1. Consumer anchors: **asserted, verify before build**. |
| Platform | Shows and persists the same reason with job/attempt state. Its retry and checkpoint owners provide truthful progress. Consumer anchors: **asserted, verify before build**. |

The roadmap source `decoy-platform/docs/ROADMAP.md`, MASTER F29 section, and F31 consumer plan are **asserted, verify before build**. Only the supplied excerpts ground their requirements here. F29 remains partial until C1/C2 prove shared reasons, distinct categories, and the required event fields end to end. The substrate-hardening work rides E1-E4: central ownership, safe projection, complete transport, and bypass sentries are required deliverables.

## Provenance

### Gemini review

Gemini 2.5 Pro (plan gate, 2026-10-29): **GO-WITH-FIXES**. Judged exceptionally strong; all eight review axes PASS (registry, retry/outcome taxonomy, safe-field contract, choke-point sentry, acceptance tests, scope discipline, cross-repo correctness, risk). Five fixes folded into this revision: (1) the unknown-future-code consumer fallback is now an explicit contract in the safe-fields section; (2) A8 adds the governor control-flow-hijack case; (3) A1 pins each code's safe summary text against silent change; (4) A3 adds the value-safe resource-exhaustion case; (5) E0's gate now requires a dynamic/interpolated-code migration-feasibility assessment before E1. The one standing risk Gemini named is the unverified F31 boundary, which the E0/C0 grounding gates manage.

### Dennis review

Dennis (repo-grounded plan gate, 2026-10-29): **GO**, 0 blocker / 0 high / 0 medium / 4 LOW. Verified 35+ file:line anchors first-hand against engine baseline `2a44723f` and independently reproduced the AST census (839 literal `code=` / 509 distinct / 189 nonliteral, exact match). Confirmed no phantom prerequisites: F31/F23/F30/F33 are correctly marked "asserted, verify before build", and the real F31 foundation is `exception_error_text` at `_isolated_common.py:266` plus `docs/security/error-reporting-and-data-exposure.md`. The registry sentry is a genuine construction-level guard modeled on the proven log-interpolation ratchet, scope discipline is real (no hidden retry executor / checkpoint store / verification policy), and A1-A10 are non-vacuous. The 4 LOW builder-facing tightenings are folded: (LOW-1) `_checks_grouped_series` code anchor corrected to `:156`; (LOW-2) governor attempt-loop anchor corrected to `:341`; (LOW-3) E4 now pins v1 `event_to_dict` as a strict additive superset of v0 keys; (LOW-4) A2 wording made conditional on E0's finite-table determination. Plan is ready for a builder.
