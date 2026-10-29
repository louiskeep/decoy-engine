# Reason codes and versioned progress events

Status: current

Operators and CI integrations identify a failure, and what to do next, without parsing prose. The engine owns one versioned registry of reason codes, one safe record for an occurrence, and one versioned event schema. The CLI and the platform read these; neither keeps a second taxonomy.

## The registry

`decoy_engine.reasons` is a leaf package (it imports no execution, planning, CLI or platform code). Each entry is identified by `(namespace, code)` and carries:

- a fixed safe summary and the owner that raises it;
- a kind: `failure`, `finding`, `warning`, `decision` or `control`;
- for each phase it may occur in: a severity, an outcome category and a retry class.

A producer references a symbol (`reasons.runtime.MISSING_MASK_SECRET`). The symbol's value is the legacy wire string, so every existing `.code` is byte-for-byte unchanged and `==`, `in`, f-strings and JSON behave as before. One wire string can exist in two namespaces (`truncate_keep_invalid` is both a compile-time plan rejection and a runtime check); the namespace tells a consumer which.

Namespaces: `config`, `plan`, `runtime`, `provider`, `quality`, `connector`, `subset`, `route`, `warning`, `verification`, `physical` and `decoy` (reasons the engine assigns itself: fallbacks, outcomes, control).

### Versioning

`registry_version` (`1.0.0`), `reason_schema_version` (`1`) and `event_schema_version` (`1`) are independent. Codes are append-only: a code never disappears or changes meaning. A retired code stays as a tombstone with its replacement link. Additions raise the registry minor version, an editorial summary correction the patch version, and an incompatible reading needs a new identity or a schema major. `tests/fixtures/f29/reason-manifest-v1.json` pins the catalog and `tests/unit/test_reason_registry.py` rejects a deleted identity, a changed retry or outcome, or a changed summary without a version bump.

An older consumer that reads a record whose identity it does not know (same registry major) degrades to `decoy.unknown_reason_from_future`, keeping the original identity in an uninterpreted context field. An unsupported major becomes `decoy.unsupported_schema_version`. Neither is ever read as success or as retry advice.

## Retry classes

Advice about the required action. It never authorizes a replay and never starts a retry loop.

| Class | Meaning |
|---|---|
| `never` | Repeating this cannot resolve it, or policy prohibits replay. Findings and control outcomes. |
| `configuration_repair` | Repair config, credentials or secret resolution first. |
| `environment_repair` | Repair capacity, access or another precondition first. |
| `retry_operation` | A known transient reason; the owner must establish idempotency and a bounded budget. |
| `restart_attempt` | Recovery needs a new attempt, possibly from a verified checkpoint. |
| `unknown` | Not enough evidence. No automatic retry recommendation. |

## Outcome categories

The engine reports a semantic category; the CLI owns process exit integers.

`execution` (processing failed), `findings` (a check completed and found something; report-only stays report-only), `verification` (an enforcing check rejected the output, or the verifier could not complete), `publication` (staging, write, commit or a sidecar failed), plus `configuration`, `cancelled` and `success`.

A publication failure after a successful verification stays a publication failure. A verifier outage takes precedence over its findings.

## Getting a reason

```python
from decoy_engine import reason_from_exception, reasons_from_report

try:
    run_pipeline(config, sources, engine_version="1.0")
except Exception as exc:
    record = reason_from_exception(exc)  # a ReasonRecord; never raises
    record.code, record.retry_class, record.outcome, record.phase, record.context
```

`reason_from_exception` trusts an exception's `.code` only for a registered engine exception class and a code registered for that class. Anything else is `decoy.unexpected_error` with retry advice `unknown`. It never reads the message, the class name, a cause or a traceback, and the exception keeps propagating unchanged. For an exception the engine observed at its boundary it returns that observation, including the phase and the publication and cleanup state captured there.

`reasons_from_report(report, enforcing=False)` adapts a verification report (the STORM post-mask report or its dict form) without serializing it.

`IsolatedRunResult.reason` carries the same record across the worker boundary. The parent validates it: identity, phase and context are read from the worker, severity, outcome and retry class are recomputed from the parent's registry, and an old, corrupt or newer envelope degrades to a fixed diagnostic.

## The record

`ReasonRecord` has explicit fields: schema version, registry version, namespace, code, kind, phase, severity, retry class, outcome and a closed `ReasonContext`. The context holds only approved identifiers (`object_ref`, `storage_ref`, never a path or a connection string), fixed enums (`operation`, `publication_state`, `cleanup_state`) and a bounded set of non-negative counts. There is no exception object and no free-text field. Input of unbounded size is truncated with a flag, not iterated.

## Versioned progress events

`event_to_dict(event)` is version 1 and is a strict additive superset of the version 0 keys that `event_to_legacy_dict(event)` keeps emitting. Version 1 adds `event_schema_version`, `registry_version`, a stream-local `sequence`, `phase`, `object_ref`, byte counters with their basis, and the optional `reason`, `retry`, `checkpoint` and `publication` groups. Existing positional constructors are unchanged: every new field is keyword-only.

| Field group | Contract |
|---|---|
| Bytes | `bytes_done`, `bytes_total_estimate`, `byte_basis`. `arrow_logical` counts Arrow `nbytes` of the batches a consumer pulled; `stored_output` counts serialized bytes after a writer closed. Unknown is `null`, never `0`, and the bases are never mixed. |
| Reason | Present on an `error` terminal event and its closing phase errors: the run's first causal failure. An `ok` event has none. |
| Retry | Reported only by the owner of a schedule. The governor reports route attempts; a job scheduler reports job attempts. Attempt numbers start at one and never go backwards. |
| Checkpoint | `unsupported`, `none`, `writing`, `committed`, `failed`. Only a durability owner can report `committed`, with an opaque reference and a sequence that never regresses. The engine has no checkpoint store. |
| Publication | Operation, an approved storage alias, `publication_state` and `cleanup_state`. |

`StructuredEventsSink` stays a documented lossy adapter for the old logger signatures. A consumer that needs the complete record binds a `CallbackSink` and reads `event_to_dict`.

## What this does not do

No new retry executor, checkpoint store, verification policy or atomic-publication algorithm. A classification that says `retry_operation` is advice, not a schedule. Live progress from an isolated worker stays unsupported.
