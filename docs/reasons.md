# Reason codes and versioned progress events

Status: current

Operators and CI integrations identify a failure, and what to do next, without parsing prose. The engine owns one versioned registry of reason codes, one safe record for an occurrence, and one versioned event schema. The CLI and the platform read these; neither keeps a second taxonomy.

## The registry

`decoy_engine.reasons` is a leaf package (it imports no execution, planning, CLI or platform code). Each entry is identified by `(namespace, code)` and carries:

- a fixed safe summary and the owner that raises it;
- a kind: `failure`, `finding`, `warning`, `decision` or `control`;
- for each phase it may occur in: a severity, an outcome category and a retry class.

A producer references a symbol (`reasons.runtime.MISSING_MASK_SECRET`). The symbol's value is the legacy wire string, so every existing `.code` is byte-for-byte unchanged and `==`, `in`, f-strings and JSON behave as before. One wire string can exist in two namespaces (`truncate_keep_invalid` is both a compile-time plan rejection and a runtime check); the namespace tells a consumer which.

Namespaces: `config`, `plan`, `runtime`, `provider`, `quality`, `connector`, `subset`, `route`, `warning`, `verification`, `physical`, `decoy` (reasons the engine assigns itself: fallbacks, outcomes, control), `pre_scan` / `post_scan` (the failures a phase envelope, `decoy_engine.phase`, can carry), `platform` (Platform job lifecycle, response, notification and validation prose; F31) and `safe_output` (the `decoy-safe-output/v1` placeholder lines). A phase envelope carries a reason as identity only: `ReasonRef(namespace, code)`, resolved fail-closed by `require_known` / `get_reason` (`UnknownReasonError`); `REASONS` is the read-only `(namespace, code) -> ReasonSpec` view and `REGISTRY_SCHEMA` names that identity form (`decoy-reasons/v1`).

### Versioning

`registry_version` (`1.24.0`), `reason_schema_version` (`1`) and `event_schema_version` (`1`) are independent.

Codes are append-only: a code never disappears or changes meaning. A retired code stays as a tombstone with its replacement link.

New reasons are declared `since=UNRELEASED` (`decoy_engine.reasons.UNRELEASED`; the manifest reports the text `"unreleased"`), and the registry stays at the allowed version: the greater of the version the last release shipped and `1.24.0`, the last minor the old per-PR process created. A development build can therefore report an identity with `"since": "unreleased"`; an older consumer still reads an identity it does not know as `decoy.unknown_reason_from_future`, and no released manifest carries the text. A pull request that adds a reason adds one `changelog.d/` fragment naming each new identity as the code span `` `namespace/code` `` and edits neither `REGISTRY_VERSION`, a minor fixture, `consumer-contract.json` nor the version sentence above. A released reason may change only by two declared pending edits: a first deprecation (`deprecated_in=UNRELEASED` together with `replaced_by`, when both were null) and an editorial summary correction listed in `PENDING_SUMMARY_EDITS` (read through `decoy_engine.reasons.pending_summary_edits()`).

Release prep (`scripts/release/stamp_release.py --version X.Y.Z`) stamps every pending fact in the same transaction that compiles the changelog fragments: new identities take the next registry minor and its fixture `reason-manifest-v<major>.<minor>-release-<package-version-slug>.json`; a deprecation or summary correction alone takes the next patch; with nothing pending the registry keeps its version. It rewrites the catalog arguments, `REGISTRY_VERSION` and the sentence above, empties `PENDING_SUMMARY_EDITS`, rebuilds `consumer-contract.json` and recaptures the release pin. `scripts/release/reason_release_pin.py --release-ready`, run by `scripts/release/release_checks.sh`, refuses a tag while anything is still pending. Two tombstones exist today, from the Pre-scan / Transform / Post-scan reframe (`reasons/route/_retired.py`): the v0.16.0 route reason and resident reason replaced by `route.post_scan_integrity_requested` and `route.post_scan_integrity`; both stay resolvable so persisted routing evidence from that release can be read. Additions raise the registry minor version, an editorial summary correction the patch version, and an incompatible reading needs a new identity or a schema major. `tests/fixtures/f29/reason-manifest-v1.json` pins the catalog, `reason-manifest-v1.4-f06.json` the F06 additions (declared parsing, casting and protected quarantine: `declared_*`, `protected_quarantine_*`, `fixed_width_record_overlong`; `since` 1.4.0, after F34's 1.3.0), `reason-manifest-v1.6-sub-stop.json` the one SUB-STOP addition (`config.subset_run_unsupported`, `since` 1.6.0: a run refuses a config that sets `subset:`; it retires to a tombstone when SUB-RUN lands and a run applies the block), `reason-manifest-v1.7-lib-l0-1.json` the two protected-directory additions (`runtime.scratch_unsafe` and `runtime.scratch_acl_failed`, `since` 1.7.0: an unsafe existing scratch directory is refused and an owner-only protection that cannot be applied fails closed), `reason-manifest-v1.8-scanml-m1.json` the seven SCAN-ML M1 additions (`config.detectors_invalid`, `config.post_scan_inactive`, `config.pre_scan_ner_inactive`, `config.ner_model_invalid`, `config.dismissal_invalid`, `provider.classifier_unavailable`, `provider.ner_model_incompatible`; `since` 1.8.0), `reason-manifest-v1.9-derived-visibility.json` the two derived-visibility additions (`runtime.derived_evaluation_failed` and `runtime.derived_ref_missing`; `since` 1.9.0), `reason-manifest-v1.10-scan-multi-s1a.json` the four scan-set additions (`warning.scan_header_suspect`, `warning.scan_key_type_mismatch`, `runtime.scan_source_failed` and `config.endpoint_url_invalid`; `since` 1.10.0), and `tests/unit/test_reason_registry.py` rejects a deleted identity, a changed retry or outcome, or a changed summary without a version bump. `tests/fixtures/f29/reason-registry-release.json` (format 2) pins the catalog the last engine release shipped: the registry version it shipped, the greatest captured `since`, per-`since` counts and, for every numeric identity, one SHA-256 per manifest field over canonical JSON. `scripts/release/reason_release_pin.py --check` (and `tests/unit/test_reason_release_pin.py`) rejects drift in any field of a released reason outside the two declared pending edits, a numeric identity missing from the pin, and a numeric `since` or registry version above the allowed version; `scripts/release/release_checks.sh`, run by `scripts/release/cut-release.sh`, refuses to tag until it and `--release-ready` pass.

`reason-manifest-v1.11-fit-redact.json` pins the one fit-redaction addition (`plan.statistical_snapshot_values_redacted`; `since` 1.11.0: generation refuses to sample categories from a snapshot that withheld the values of a sensitive column).

`reason-manifest-v1.12-project-layout-e1.json` pins the one pipeline-name addition (`config.pipeline_name_invalid`; `since` 1.12.0: a declared `name:` that is not a string or fails `is_valid_pipeline_name`).

`reason-manifest-v1.13-gen-strict.json` pins the nineteen strict generate-column additions (seven `config.generate_*` option, weight, formula and duplicate-name refusals, and twelve `plan` refusals for Faker types, locales and arguments, mixed categories, forward references, formula names, references and brace templates, `grouped_series_group_by_equals_order_by`, `reference_weights_length_mismatch` and the translated `generate_option_invalid`; `since` 1.13.0).

`reason-manifest-v1.14-fk-fanout.json` pins the six fitted fan-out additions (`quality.fanout_column_unknown`, `plan.reference_fanout_missing`, `plan.reference_fanout_invalid`, `plan.reference_fanout_conflict`, `plan.reference_parent_pool_empty` and `plan.dp_generate_fitted_fanout_unsupported`; `since` 1.14.0).

`reason-manifest-v1.15-config-explicit-e1.json` holds the three CONFIG-EXPLICIT E1 additions (`config.source_file.has_header_required`, `config.source_file.column_names_source_conflict` and `config.source_file.column_names_file_invalid`, `since` 1.15.0: a csv source must say whether row one is a header, may name its columns inline or in a header file but not both, and a header file that is not one UTF-8 record is refused).

`reason-manifest-v1.16-gen-fit-shape.json` pins the one fitted-cell-format addition (`plan.statistical_cell_format_invalid`; `since` 1.16.0: a statistical column's snapshot `cell_format` block is malformed).

`reason-manifest-v1.18-run-stream-e1a.json` pins the one RUN-STREAM addition (`runtime.streaming_unavailable`; `since` 1.18.0: a run that requires streaming, `execution_mode="stream"`, is refused before any source passed in `sources` is read because no streaming route applies).

`reason-manifest-v1.17-gen-unique.json` pins the four GEN-UNIQUE additions (`config.generate_unique_conflict`, `config.generate_unique_domain_too_small`, `config.generate_unique_unsupported` and `plan.pinned_unique_pair_unsupported`; `since` 1.17.0: a generate column's `unique` option is combined with a conflicting option, asks for more distinct values than its domain holds or sits on a type that cannot honor it, and a pinned plan carries `unique_together`).
`reason-manifest-v1.19-config-explicit-e2a.json` holds the two CONFIG-EXPLICIT E2a additions (`config.source_file.undeclared_column` and `config.source_file.undeclared_table`, `since` 1.19.0: a source column the config does not list, and a source with no `tables` entry). The message names a position and the table, never a column. Nothing raises them yet; later parts wire the gate into the run routes.

`reason-manifest-v1.20-lib-l1a.json` pins the seven local-record-library additions (`runtime.record_invalid`, `runtime.record_corrupt`, `runtime.record_kind_reserved` and the CLI-side events `warning.library_write_failed`, `warning.job_log_write_failed`, `warning.library_degraded`, `warning.library_delete_skipped`; `since` 1.20.0).

`reason-manifest-v1.21-run-stream-e1b.json` pins the one host-CPU addition (`runtime.cpu_detection_failed`; `since` 1.21.0: a CPU limit file that is present but unreadable).

`reason-manifest-v1.22-gen-cond-cap.json` pins the three joint-cap additions (`quality.contingency_top_k_invalid`, `quality.contingency_top_k_kind_unsupported` and `warning.fit_joint_cells_truncated`; `since` 1.22.0: a `contingency_top_k` outside 1 to 1000, a raised cap on a joint with a non-categorical column in a plain snapshot, and a joint whose cell cap dropped combinations).

The same fixture also pins the two GEN-CLI additions (`warning.generation_fidelity_below_threshold`: a generated table's overall score, or one comparable column's score, is below `fidelity_warn_threshold`; and `warning.seed_ignored_with_master_key`: a job seed is set next to a master key, so generated tables do not use the seed).

`reason-manifest-v1.23-gen-types.json` pins the three GEN-TYPES E2 additions (`config.generate_output_type_unsupported`, `runtime.output_type_cast_failed` and `runtime.declared_boolean_invalid`; `since` 1.23.0: a generate column or fixed-width target cannot carry the requested `output_type`, a generated value could not be cast to its `output_type`, and a value is not a boolean under `boolean/text/v1`).

`reason-manifest-v1.24-fpe-fallbacks.json` pins the two FPE handler fallback codes (`runtime.fpe_unencryptable_value` and `runtime.fpe_checksum_unsupported`; `since` 1.24.0). The `fpe` strategy had emitted both since Task 5.2 when a low-level FPE error carried no more specific code, but neither was registered, so they surfaced as `decoy.unregistered_reason`. The checksum one's symbol is `FPE_CHECKSUM_UNSUPPORTED_STRATEGY`, because `FPE_CHECKSUM_UNSUPPORTED` already names the low-level `fpe.checksum_unsupported`.

An older consumer that reads a record whose identity it does not know (same registry major) degrades to `decoy.unknown_reason_from_future`, keeping the original identity in an uninterpreted context field. An unsupported major becomes `decoy.unsupported_schema_version`. Neither is ever read as success or as retry advice.

`scripts/dev/registry_renumber.py --check --main origin/main` holds the merge gate: a branch keeps main's registry version and adds no minor fixture and no numeric identity. A branch that still claims a numeric minor from the old per-PR process merges main and runs `--to-unreleased`, which rewrites its new identities and their carriers to the pending form, resets the version anchors and deletes its fixture; a branch that edits an existing identity is reapplied by hand from current main (see `CONTRIBUTING.md`). `tests/sentry/test_reason_minor_fixtures.py` requires one fixture per minor above 1.5 and a fragment naming every pending identity, and `scripts/release/reason_manifest_policy.py` is the one place these rules live.

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

`reasons_from_report(report, enforcing=False)` adapts a verification report (the Post-scan integrity report or its dict form) without serializing it.

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
