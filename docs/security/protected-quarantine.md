# Protected quarantine

Status: current

`error_policy: quarantine` isolates a row that failed a declared parse or format. The row is raw data, so it is only ever written through a **protected sink** that proves encryption, finite retention and bounded size before the source is read.

## Sink protocol

`ProtectedQuarantineSink` (`decoy_engine.declared_casting`): `begin`, `write_row`, `seal`, `abort`, `get_retrieval_token`, `open`, `purge_expired`, plus a `capability` the engine checks before reading any source. A run is given `ProtectedQuarantineRuntime(sink, owner, run_id=None)` through `run_pipeline(quarantine_runtime=...)`. Missing, unprotected, wrongly protected or protocol-incomplete sinks are refused with `protected_quarantine_unavailable`; the legacy `quarantine.output_path` does not qualify.

## Portable access token

After sealing, the run returns `ExecutionResult.quarantine_access`, an `OpaqueRetrievalToken`: a serializable, value-free bearer capability (`to_wire()` / `from_wire()`), hidden from `repr`, absent when no row was quarantined and never written to evidence. A remote owner presents it later to `sink.open(token, QuarantineAccessContext(owner=..., run_id=...))`. The token MAC binds the artifact's owner, run identity and salt, so a context for another run is refused even for the same owner and key; `run_id` is the caller's run/job id, or a fresh random one reported in the receipt evidence when none is given. `get_retrieval_token(receipt, context)` issues one again from the opaque receipt ref. `quality_metrics["declared_casting"]["quarantine"]` holds only the receipt: opaque ref, row and byte counts, digest, expiry, protection, key version and run id.

## Records

Each protected record holds the table, the absolute source row position, the raw framed row and a full F29 `ReasonRecord` per failing field: registry and schema version, the phase (`mask` for a parse failure, `finalize` for a format failure), outcome and retry class resolved from the registry, and the column as `table.column`. The raw row reopens with its types: Decimal, date, datetime, time, timedelta, bytes and nested values round-trip exactly, and a value of any other type fails the write rather than being stringified.

## Failure ordering

The seal is the last failure-capable step before the publish. A failure after the seal discards the sealed artifact (`discard`), and a failure before it aborts the open session, so a failed run leaves no sealed artifact behind and no token is returned.

When the discard itself fails (the adapter reports `failed`, or raises), the run's original failure still propagates, the receipt is kept so a later `DeclaredCastRun.abort()` retries, and the cleanup is reported rather than hidden: the failure's F29 record gains `cleanup_state: failed` with the run identity as `object_ref`, the registered runtime reason `protected_quarantine_cleanup_failed` is captured as a secondary reason, and the exception carries `declared_cast_cleanup` (a value-free `CleanupOutcome`: the opaque artifact `ref`, `run_id`, `key_version`, `expires_at`). The artifact is encrypted and bounded by its retention period, so `purge_expired` is the backstop; an operator reconciles the named artifact by its ref. No path, key material or row value appears in the report.

## Local reference adapter

`LocalProtectedQuarantineSink(root, key_provider=..., clock=...)`:

- Each bounded chunk of rows is a length-prefixed Fernet token under a quarantine-only key derived by HKDF-SHA256 (distinct purpose and version) from a real `KeyProvider.mask_key()`. A `SeedKeyProvider` (no secret) is rejected. The key is never serialized; no plaintext is staged.
- Directories are `0700`, files `0600`, created exclusively with `O_NOFOLLOW`; a symlinked or group/other-accessible root is refused.
- A SHA-256 digest trailer is written before an atomic rename, so a partial file is never a receipt. Row and byte budgets fail closed; a failed write or seal removes the partial and reports how cleanup ended (`cleanup_state`) apart from the original code.
- Retention is finite and measured with the injected clock; `open` after expiry raises `protected_quarantine_expired`, and `purge_expired(now, authority)` removes only the authority's own expired artifacts.

## What this does and does not guarantee

The local adapter's guarantee is scoped to token possession plus owner/run and key-version match. It is an in-process bearer-token store: identity policies such as `tenant-job-owner` are enforced only by an identity-bearing platform adapter implementing the same protocol. The OS owner who also holds the key can read the data. The ciphertext is application-level; the adapter does not claim filesystem encryption, quotas, orphan recovery, a background sweeper, or atomic publication together with the normal output (a future publisher binds the receipt; until then a quarantine failure aborts the run before output commit).
