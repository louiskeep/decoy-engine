# Retention contract (`decoy_engine.retention`)

The engine owns the vocabulary that the platform retention sweeper (`decoy-admin retention-sweep`) and
the CLI (`decoy local-cleanup`) both speak. It is pure: no locator, no backend SDK, no deletion. Each
consumer builds typed inputs and serializes only through this package, so their reason codes, exit
codes and report shapes cannot drift. Plan: `decoy-platform/docs/plans/2026-10-29-f26-retention-sweeper.md`.

## `retention-policy/v1`

A strict map from policy key to `{mode, window_hours}`. Keys: `raw_upload_transient`,
`raw_upload_saved`, `snapshot`, `published_output`, `staging`, `checkpoint`. Modes: `keep_all`
(no window) or `expire_after_quiescence` (1 to 87600 hours). Defaults are conservative: everything is
`keep_all` except transient uploads and staging (24 h after quiescence). A policy states when an
artifact MAY become eligible. It never deletes, and a policy change never purges.

`RetentionPolicy.from_dict` is strict: an unknown schema, key, field or mode, a missing class, a
boolean or out-of-range window raises `ValueError`, and callers treat that as `retain_policy_invalid`.
`policy_id` is content-addressed (16 hex characters), so a run's policy snapshot cannot be swapped.

## `retention-report/v1`

Opaque ids (32 lowercase hex), closed reason/result codes, counts and the confirmation digest. No
filename, path, bucket/key, value, SQL, YAML, content hash, receipt or exception message. Fields
are allow-listed (`ALLOWED_*_KEYS`); `tests/sentry/test_retention_contract.py` fails the build when
a field is added without review. An unrecognized future code, class or decision parses to
unknown-and-retained and can never become eligible.

`inventory_digest` is the confirmation digest an operator echoes to authorize exactly one reviewed
set. It binds scope, policy id, limits, expiry and each eligible target's id AND identity digest
(an input only; it is never serialized). It is domain-separated and pinned by a known-answer test.

## Reason codes

`eligible`; `retain_*` (why an artifact is kept: ledger and identity, policy, holds, producer
quiescence, dependencies, recovery, and `retain_state_changed` for drift after a preview);
`result_*` (an execution item outcome); `refused_*` (a whole run refused, nothing deleted). The
registry is append-only; every code has a fixed safe summary (`REASON_SUMMARY`).

## Exit codes

Outcomes only, clear of every consumer's own usage and runtime codes: `0` success, `10` nothing to do,
`11` everything retained, `12` partial, `13` refused, `14` indeterminate (an outcome this build cannot read;
never `0`).

## Environment

`DECOY_ENGINE_LOG_FILE` overrides the engine logger's default `./logs/decoy_engine.log`; an empty
value disables the file handler. Importing the engine otherwise creates that file in the working
directory, which a stateless caller must be able to avoid.
