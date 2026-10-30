Status: reference (engine slices built; cross-surface adoption pending their own gates)

Summary: How the engine publishes output atomically and idempotently: the descriptor, the two local publication units, outcomes, overwrite and backup, retention, and what is deliberately not claimed.

# Atomic and idempotent output publication (F22)

Plan: [plans/2026-10-29-f22-atomic-idempotent-publication.md](plans/2026-10-29-f22-atomic-idempotent-publication.md). Dependency report: [plans/2026-10-29-f22-slice0-dependency-report.md](plans/2026-10-29-f22-slice0-dependency-report.md). Code: `src/decoy_engine/publication/`.

## The commit truth

A result is **committed** only when the destination binding resolves to a `decoy-publication/v1` descriptor whose bound artifacts all verify (size and SHA-256, re-hashed). Receipts, job status, directory listings and evidence reports never overrule it. `result_id` is a SHA-256 over the sorted artifact inventory (role, path, size, digest, rows, schema digest). It is **physical** identity: a change in Parquet encoding or row order that changes bytes changes the id. Timestamps, attempt ids, staging paths and the destination are excluded.

The evidence core binds `result_id` and the validation decisions *before* the descriptor is written; the descriptor references the core by digest; any later envelope references the descriptor. There is no hash cycle and no signature claim.

## Publication units

| Unit | Class | Layout | Replacement |
|---|---|---|---|
| `immutable_directory/v1` (default) | `ParquetTransactionalSink` | `<target>/<table>.parquet` + `_decoy_publication.json` + `_decoy_evidence_core.json`, one rename | No. A different result is `conflict`; a descriptorless non-empty directory is `target_occupied_legacy` and is never adopted. |
| `managed_dataset/v1` | `ManagedDatasetSink` | `<root>/generations/<descriptor-digest>/…`, `<root>/CURRENT` | `Replace(expected_descriptor_digest, backup)`; compare-and-set under the lock. |

Readers of a managed dataset call `resolve_current` (or `pin_reader`) **once** and read only from the returned generation path. A bare listing of the root is not the reader protocol.

## Outcomes

`published`, `already_committed`, `conflict`, `unsupported`, `rejected`, `cancelled_before_commit`, `commit_uncertain`. Success outcomes return a `CommitReceipt`; the rest raise a `CommitError` subclass carrying the receipt. Visibility and durability are reported separately. `commit_uncertain` (rename done, directory sync or reread failed) never deletes the possible commit; call `reconcile()`.

Idempotence: keep `operation_id` stable across retries. The same operation with identical content, or any job with identical content and acceptance context, gets `already_committed` and the original descriptor. The same operation with different content or checks is `operation_conflict`. In a managed dataset a retry of a superseded operation reports the historical result with `is_current=False` and is never replayed; if its payload was expired it is `committed_artifacts_expired`. Byte-identical restaging relies on pyarrow writing deterministic bytes within the pinned version (tested); the expected-identity path (`expected_result_id`) recovers a lost receipt without restaging.

Recovery by `expected_result_id` (or operation id) also requires the **same acceptance context**: the committed evidence core's policy fingerprint and check dispositions must equal the request's (`acceptance_context_differs` otherwise), and a required failed/inconclusive check is rejected before any lookup.

## Overwrite and backup

`ErrorIfDifferent` (default) or `Replace(expected_descriptor_digest, NoBackup | ProtectedBackup(policy_ref))`. A protected backup is a private (0700/0600) verified copy of the prior generation plus a hold marker, created *before* the pointer switch; a backup failure prevents publication. Restore is `restore_backup(...)`, a new authorized replacement with an expected predecessor, never an implicit rollback. Local protection is not WORM and not encryption.

## Cancellation and the isolated child

The commit guard (the destination lock plus `PublicationRequest.cancelled`) arbitrates cancellation against publication: before the binding nothing new is released; after it the result is complete and committed. A completed rename is never reversed. In isolated runs the child only prepares a directory (`ParquetPreparationSink`, returning a non-committable `ChildPreparationEnvelope`); the parent re-measures and publishes it, and `cancel_check` is polled under the lock immediately before the rename.

The parent owns the staged dataset through one `StagingLease` (`execution/_isolated_commit.py`): the directory is disposed on every exit that did not commit (gate refusal, child crash, timeout, cancel, exception), and the lease only ever names the path the parent assigned, never one the child reports. The child gate also reads the Parquet trailer first and refuses a declared footer over 64 MiB, then parses the footer under Thrift string and container limits; memory exhaustion or a parse failure is `child_envelope_invalid`.

## Concurrency on one sink

The sink's public methods may be called from several threads. Every state read and transition holds one re-entrant sink lock; slow work runs in a named in-flight state (WRITING, COMMITTING) rather than under the lock. The transition table is in the `_transactional_sink` module docstring. A second `commit()` or a recovery read during a commit, and a `commit()` during a write, are refused (`commit_in_progress`, `write_in_progress`) without latching the attempt; an `abort()` during in-flight work is deferred and cannot undo a binding that landed first.

## Value-free surfaces

Receipts and refusals carry stable reason tokens, errno names (`rename_failed:ENOSPC`), exception type names, digests and counts, never paths, OS error text or table names. `CommitReceipt` has no destination field and reports `ArtifactSummary` (role, size, SHA-256, rows, schema digest) instead of artifact paths; the descriptor inside the committed unit still binds the real relative paths. Child-envelope refusals name a defect class, never a child-supplied value. The exception chain (`__cause__`) keeps the original in-process exception for debugging; it is not part of any receipt or result. F31's boundary will own redaction once it lands.

## Complete-unit quarantine

Default: the quarantine sidecar is a post-commit diagnostic outside the descriptor (unchanged behavior). With `PublicationRequest.sidecars_in_unit=True` the JSONL is staged inside the unit and bound by the descriptor; a quarantine path outside the destination is refused (`sidecar_outside_unit`) before any masking.

## Retention

Default is best-effort discard with a typed `AbortReport`. `RetentionPolicy(store, owner, expiry_s, byte_budget)` retains a rejected attempt only in an authorized private store within budget and finite expiry; otherwise it discards and says so. A retained descriptor is neutralized. `cleanup_retained` requires ownership, no holds and expiry, and acts on one exact contained record; it never claims secure erasure. `expire_historical_payload` never touches the current generation, anything a live reader pins, or protected backups.

## Sink admission

Qualification is by explicit `CommitProtocolDeclaration`, not method shape. The generated table in [capability-matrix.md](capability-matrix.md) lists every protocol. `FileSink` transfers (S3, GCS, SFTP) are transfer primitives and do not qualify. `decoy_engine.publication.conformance.run_conformance` is the reusable suite for external adapters; it does not replace vendor commit-disconnect and conditional-write race tests for DB or object-store adapters.

## Not claimed

No distributed transactions; no engine DB output sink (none exists); no object-store atomic dataset; no checkpoint engine; no encryption; no retention daemon; no F33 signing; no power-loss certification (fault tests cover process death and injected sync errors). The local profile is a POSIX filesystem with `flock`; NFS/SMB/FUSE are not presumed equivalent and lock failure is a refusal, never a lock-free fallback.
