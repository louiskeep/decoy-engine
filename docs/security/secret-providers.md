# Secret providers (`secret-provider/v1`)

The engine admits the keyed-mask secret through one versioned contract **before any source
row is read**. This page describes shipped behavior; the design record is
`docs/plans/2026-10-29-f13-secret-provider-contract-preflight.md`.

## What changed for keyed work

- A plan with a keyed strategy (`hash`, `fpe`, `date_shift`, `shuffle`, `tokenize`, `vault: true`
  columns, deterministic columns, composite-FK groups, ...) **requires** an admitted secret in every
  release phase. The pre-GA fallback that keyed masking off the 8-byte `job_seed` is removed.
  `job_seed` remains valid for generation and genuinely unkeyed behavior only.
- `SeedKeyProvider` stays importable but is never a secret: supplying it for keyed work fails with
  `keyed_strategy_requires_secret`.
- Keyed-ness is decided from configuration alone (`secret_requirements_for_config`), so the
  secret is admitted before `profile_source`, before the chunk iterator is advanced, and before
  unmask or vault-writer construction reads anything. A strategy the classifier does not recognise
  is treated as keyed.

## Reference grammar

| Scheme | Form | Notes |
|---|---|---|
| `env:` | `env:NAME[?encoding=<base64\|hex\|raw>&key_version=<v>]` | Legacy `env:NAME` still works for the mask root. |
| `file:` | `file:/PATH[?encoding=...&key_version=...]` | The file is read as bytes. |
| `vault:` | `vault://<mount>/<path>?version=<N>&field=<f>&encoding=<e>` | Version, field and encoding are all required; `latest`, `0`, aliases, userinfo, fragments, duplicate keys and traversal segments are rejected. |

The default logical `key_version` of `env:`/`file:` is `v1`; change it to rotate output identity
intentionally. Vault derives `key_version` from the pinned KV version (`v3`).
Every component of a `vault:` reference (mount, path, field) is percent-decoded exactly once and
strictly: `%25ff` names the literal field `%ff`, an undecodable escape (`%ff`, an overlong or
truncated UTF-8 sequence), a malformed escape (a `%` not followed by two hex digits) and a control
character in any component are rejected. The query is form-decoded as `parse_qsl` defines it
(`+` is a space, `%2B` a plus); the mount and path are not (`+` is literal).
Errors never repeat the reference, path or field: every malformed reference (including a malformed
query or an undecodable percent-escape) fails `bad_secret_ref` with no chained parser exception.

## Per-algorithm validation

There is **no universal 32-byte rule**.

| Requirement | Accepts | Rejects |
|---|---|---|
| `decoy-mask-root-v1` | decoded raw bytes, at least 32 (legacy hex-then-base64 text decoding is the default) | fewer than 32 bytes (`weak_mask_secret`), malformed encoding (`bad_secret_ref`) |
| `fernet-v1` | URL-safe base64 of exactly 32 bytes; validated by `cryptography.fernet.Fernet` | anything the constructor rejects (`secret_material_invalid`) |
| `hmac-sha256-v1` | raw key of at least 32 bytes (RFC 2104) | shorter |
| `aes-gcm-v1` | exactly 16, 24 or 32 bytes; validated by `AESGCM` | any other length |

Legacy decoding applies **only** to the mask root. Every other requirement needs an explicit
`encoding=` on the reference (for a Fernet key stored as text: `env:KEY?encoding=raw`), or it is
rejected with `bad_secret_ref`. Pattern and entropy scans never certify key strength.

## Failure reasons

All provider failures are registered F29 reasons in the `runtime` namespace (phase `resolve`),
mapped by exception *type* with fixed text: `secret_provider_unknown`, `secret_provider_unavailable`,
`secret_access_denied`, `secret_version_unavailable`, `secret_expired`, `secret_lease_unsupported`,
`secret_material_invalid`, plus the existing `bad_secret_ref`, `missing_mask_secret`,
`weak_mask_secret`, `keyed_strategy_requires_secret`, and `secret_file_permissions_unsafe` (below).
The provider exception is not retained as `__cause__`/`__context__`, and neither is an exception the
caller was already handling when it called admission (the error is raised with its chain cleared), so
`logging.exception` and excepthooks cannot print provider internals.

`file:` secrets are read through the installed file-access policy (`FileRole.SECRET` at
`global_settings.mask_secret_ref`, see `file-access-policy.md`) and must be owner-only: group or other
permission bits refuse the file with `secret_file_permissions_unsafe`, checked with `fstat` on the
descriptor the bytes are read from (OpenSSH's rule for private keys; Windows ACLs are not inspected).
Kubernetes secret volumes need `defaultMode: 0400`, Docker secrets `mode: 0400`. There is no bypass.
Under a strict file-access policy, exactly `FileAccessDenied`, `FileAccessUnavailable`, `MemoryError`
and `ENOMEM`/`ENOSPC`/`EDQUOT` from that read keep their class across the boundary, rebuilt fresh
from constants; only the built-in file provider can pass them.

No caught exception is ever re-raised. The admission boundary (`prepare_secret`, `prepare_legacy`,
`prepare_ref` behind `keyprovider.key_provider_from_ref`, and a provider's own `preflight` called
from `admit_mask_key`) catches every `BaseException` except `KeyboardInterrupt` and `SystemExit`
(those pass through untouched), records only the registered code inside the handler, and raises a
fresh engine error outside it without the failing frames' traceback. A provider that
raises a `MaskSecretError` with its own message, or a subclass of a closed error, gets the
engine's constant text for that code. An unregistered code, a code that is not an exact `str`, or a
`code`/`message` attribute that raises becomes `secret_provider_unavailable`. Rendering with `traceback.TracebackException(capture_locals=True)`
therefore shows no frame that held secret material. Between the read and the derivation, material
lives only in `SecretBuffer` objects, which render as `<redacted secret>`.

## Cache and lifetime

- One admitted secret is reused for the whole run; it is closed (best-effort overwritten) when the
  run or chunk stream ends, however it ends.
- `env:` and `file:` secrets are **never** cached, whatever the policy says: the resolver enforces
  it, so a secret admitted under one file-access policy is never served to a run under another.
- Cross-run caching is **off by default**. `SecretCachePolicy(ttl_seconds=N)` allows `1..60`.
  Entries live at most `min(ttl, provider_expiry - 5s)` on a monotonic clock, are never extended
  by a hit, and are evicted on expiry or error. Concurrent preflights of one uncached secret read
  the provider once.
- Entries are scoped. The key is a per-process salted hash of the canonical reference, the
  requirement set and every field of the provider's `ResolveContext` (Vault: scheme, address,
  namespace, token and TLS settings). The engine takes that immutable context once per load and
  hands the same object to `resolve(ref, context)`, which reads with it, so the key and the read
  cannot name different tenants. A context must be a frozen dataclass subclass of
  `ResolveContext` with `repr=False`, the provider's scheme and only immutable scalar fields;
  anything else fails `secret_provider_unavailable` before any read. Each entry is also bound to the identity of the provider instance and
  the admitting registry, and is served only to those same objects. Two providers that share a
  scheme and a reference (two tenants) each read their own secret.
- The cache is bounded: at most 128 entries (the oldest insert is evicted first), every expired
  entry is pruned on each access, and the single-flight lock of a key is dropped when its last
  caller leaves. Evicted entries are closed.
- With a positive TTL, a revocation or deletion after admission can remain visible for up to the
  TTL (maximum 60 seconds). The default observes the next read immediately.
- Clearing Python-managed memory is **best effort**: the interpreter, base64 decoders and
  cryptographic libraries make immutable copies. The engine minimises copies and never claims
  zeroization.

## HashiCorp Vault KV v2

Optional extra: `pip install "decoy-engine[hashicorp-vault]"` (`hvac==2.4.0`, Apache-2.0). The
base install and `env:`/`file:` work without it.

```python
from decoy_engine.secret_providers import register_secret_provider
from decoy_engine.secret_providers.vault import VaultKV2Provider, client_from_env

register_secret_provider(VaultKV2Provider(client_from_env()))
```

- The caller authenticates and authorizes (F45) first and passes an already-authenticated client.
  The provider is immutable: construction copies the client's address, namespace, token and TLS
  settings (`verify`, client cert) into a frozen `VaultContext`, keeps no reference to the client,
  and each read goes through a client built from that context. Re-pointing or re-authenticating
  the caller's client afterwards changes nothing; to change tenant or token, build and register a
  new provider (a new registry, or `admit_mask_key(..., registry=...)`). The provider and its
  context never render their settings and cannot be pickled. `client_from_env` honors `VAULT_ADDR`, `VAULT_TOKEN`,
  `VAULT_NAMESPACE`, `VAULT_CACERT`, `VAULT_CLIENT_CERT`, `VAULT_CLIENT_KEY`, and **rejects**
  `VAULT_SKIP_VERIFY` (disabling TLS verification needs `insecure_skip_tls_verify=True` in code).
- One provider round trip: the KV v2 read returns data and metadata together and the returned
  version must equal the pinned one.
- Static KV v2 values only. A response carrying a lease, renewable flag, wrap info or auth data
  fails `secret_lease_unsupported`; there is no background renewal.
- Minimum policy: `read` on exactly `<mount>/data/<path>` for the secrets a workload uses. No
  `list`, `create`, `update`, `delete`, `destroy`, metadata write, token creation or policy
  administration.
- Rotation: write a new KV version and change the reference's `version=`; the output identity
  (`key_version`) changes with it. Incident invalidation: destroy the version in Vault and
  clear any positive-TTL cache by restarting the process or calling
  `default_cache().clear()` (all keys) or `default_cache().invalidate(key, owners=...)` (one).
  Both fence loads already in flight: a read that began before the call is returned to its own
  caller but is never inserted, so no revoked material is served from the cache after the call
  returns. Cached entries are also never served past the calling policy's TTL.
- Cache isolation is structural, not a before/after comparison: a shared client switched to
  another tenant and back during a load cannot change the context the read used, because the read
  never goes through that client.
- **Not live-verified.** The in-process fakes prove the adapter's mapping and ordering, not the
  real `hvac` wire format, TLS verification, namespace header routing or the KV v2 metadata
  envelope. The five live tests in `tests/secret_providers/test_vault_live.py` are skipped
  without a Vault service and have not been run.

## Not shipped

AWS Secrets Manager, Google Secret Manager, Azure Key Vault, 1Password, systemd credentials and
SOPS/age are demand-gated and have no stub here.

## Dependency decision (`hvac`)

`hvac==2.4.0` (Apache-2.0, pure Python; its only runtime requirement, `requests`, is already in the
resolved tree, so the lock delta is the single `hvac` entry) is the sole optional external
dependency, exact-pinned in `pyproject.toml` (`hashicorp-vault` extra) and hash-locked in
`uv.lock`. It is imported lazily and is never a base dependency; `[vault]` remains the unrelated
token-vault alias. CI installs and tests both the base (no `hvac`) and the extra environments.
The `pip-audit --strict` job covers it. Any change from this pin returns to owner review.
Review artifacts to attach to the PR: PyPI provenance/attestation check, release/maintenance
history, and the vulnerability scan at the reviewed SHA.

## Structural sentry

`.semgrep/f13-secret-providers.yml` (CI job `secret-provider-semgrep`, also
`tests/sentry/test_secret_provider_semgrep.py` when Semgrep is installed) fails the build if a
secret-provider module logs, prints, warns, chains an exception cause, renders caught exception
text or serializes, or if an execution/consumer module imports a concrete provider or calls
`resolve()` directly. Runtime coverage is the seeded-marker suite in `tests/secret_providers`.
