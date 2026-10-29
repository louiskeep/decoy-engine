# F13 — Secret-provider contract and preflight

**Date:** 2026-10-29  
**Plan class:** R2, crypto-adjacent, public API and cross-repository change  
**Engine baseline:** `decoy-engine` `origin/main` at `fdd08a894d5dd4440a1adea0ee62bf0b670808bb`  
**Planning branch:** `plan/f13-secret-provider`  
**Source:** ROADMAP Phase 2, Track B, V1; F7, G15, P-A1  
**Owners:** Engine provider interface and optional adapter; CLI and Platform consumers  
**Related work:** F29 is merged and is the current safe error boundary. F31 is a later safe-output seam to adopt when available, not a prerequisite. F45 authorization remains a consumer responsibility and is not an F13 blocker.

This document is the implementation authority for F13 after the plan gate passes. The remote implementation worker must re-ground every cited consumer anchor against the then-current remote repository before editing it, but must not weaken the decisions or acceptance criteria here without returning to the owner.

## 1. FRAME

### Problem

The engine has a useful but narrow keyed-masking abstraction: `KeyProvider` exposes `key_version` and `mask_key()`, and `env:`/`file:` references resolve into a redacted `SecretKeyProvider`. It does not expose value-free discovery, a provider-neutral preflight, an explicit cache/lifetime contract, or an external secret-manager adapter. Its validation is also coupled to the Decoy masking root's current 32-byte minimum rather than to the algorithm that will consume a secret.

More importantly, the main execution path currently profiles the source before it resolves the masking secret. In [`build_run_context`](../../src/decoy_engine/execution/_pipeline_context.py#L166), `profile_source()` is called at line 288, while `resolve_key_provider()` and `require_mask_key()` are not called until lines 292–305. The chunked oracle has the same ordering defect: [`_oracle_preflight`](../../src/decoy_engine/execution/_chunked_oracle.py#L79) calls `next()` on the input at lines 116–117, but compiles the plan and checks the key only at lines 161–174. A missing, inaccessible, expired, or unusable secret can therefore be discovered after protected source data has already been read.

F13 must make secret admission a real precondition. Every execution surface must resolve an approved, version-pinned reference and validate the material for its selected algorithm before protected work or its source rows begin. Failure must be closed, value-free, stable through the F29 registry, and identical across direct, worker, CLI, and Platform boundaries.

### Guarantee

For every operation whose configuration selects a keyed/protected algorithm:

1. The engine determines the secret requirements from configuration without reading source rows.
2. It selects a registered `secret-provider/v1` provider, obtains value-free metadata, reads the explicitly pinned version at most once, validates that value against the selected algorithm, and prepares the derived key before profiling, iteration, unmasking, vault-file reads, or masking begins.
3. Unknown providers, missing or inaccessible versions, expired values, unsupported dynamic leases, and invalid key material terminate through a registered F29 reason code. Provider exception text, secret bytes, credentials, reference paths, lease identifiers, and seeded markers never cross the safe boundary.
4. A keyed operation never silently substitutes `job_seed` or an unkeyed transform, in any release phase.
5. A resolved value exists only in a non-serializable, redacted buffer with an explicit owner and lifetime. Cross-run caching is disabled by default, is never longer than 60 seconds when enabled, and never outlives provider expiry. Clearing Python-managed memory is documented as best effort.
6. The selected algorithm owns the material rules. There is no universal 32-byte rule and no entropy or pattern heuristic that certifies key strength.

The guarantee is testable by using a source/iterator that fails if touched: a secret-admission failure must win and the source witness must remain untouched.

### Definition of done

F13 is done only when all of the following are true at the exact reviewed SHAs:

- The engine exports a versioned provider contract with `describe`, `resolve`, and `preflight`, while preserving compatibility for the existing public `KeyProvider` and its duck-typed implementations.
- `env:` and `file:` are implementations of the new contract, and HashiCorp Vault KV v2 is the sole external V1 adapter.
- All engine entry points that consume masking material preflight before their first protected read, including normal, chunked, unmask, and vault-writer paths.
- The requirement registry enforces the algorithm matrix in this plan and rejects keyed work without a valid provider in every release phase.
- All provider failures map to fixed F29 reasons, and the build-failing leak sentry proves that a seeded secret marker cannot enter errors, logs, evidence, result objects, progress events, YAML, JSON, CLI output, or Platform output.
- Cache, expiry, and unsupported-renewal behavior are deterministic and covered by controlled-clock plus live-Vault tests.
- The optional Vault dependency is isolated, exactly locked, reviewed for provenance/license/vulnerabilities, and absent from the base install.
- CLI and Platform consume the engine contract after re-grounding their current main branches. Platform reuses its existing Fernet-at-rest and 60-second resolver cache rather than duplicating them.
- Targeted, property, security, integration, live-adapter, cross-process, compatibility, static, mutation, and full-suite checks pass; documentation describes permissions, rotation, expiry, revocation visibility, renewal limits, and best-effort memory clearing.
- Codex and Opus-4.8 Dennis accept the code in that order, Barry synchronizes documentation, the exact final HEAD receives the repository gate receipt, and the owner authorizes merge.

### Boundaries and non-goals

**V1 ships only:** the versioned interface, value-free descriptor, preflight, algorithm requirement registry, bounded lifetime/cache machinery, `env:` and `file:` providers, compatibility wrapping for existing `KeyProvider` objects, and one external adapter: HashiCorp Vault KV v2.

**Demand-gated after V1:** AWS Secrets Manager, Google Cloud Secret Manager, Azure Key Vault, 1Password, systemd credentials, and SOPS/age. Their official auth and version models are surveyed below so the interface does not preclude them, but no package, configuration knob, code path, or nominal stub for them ships in F13. A later adapter needs its own demand evidence, SDK/supply-chain review, live tests, minimum-permission documentation, and owner approval.

**Not in scope:** a generic KMS/envelope-encryption system; secret creation or rotation; Vault policy provisioning; transport security configuration; a background renewal daemon; authentication-token storage; certifying key quality from entropy; replacing Platform's existing encrypted-at-rest inline secrets; weakening F29 while waiting for F31; or moving F45 authorization into the engine.

The engine accepts an already-authorized provider/client. CLI and Platform remain responsible for authenticating users and workloads, applying F45 authorization before provider invocation, and granting only the minimum read permission.

### Phase pillars

- **Fail closed before data:** admission precedes source construction/iteration and protected reads; no keyed fallback exists.
- **Value-free F29 failures:** provider errors become registered fixed reasons at one trusted boundary. Raw SDK text is never user output.
- **No secret persistence:** values never enter pipeline YAML, plan/evidence objects, logs, exception strings, reprs, JSON, metrics labels, or durable caches.
- **Bounded lifetime:** ownership, TTL, expiry, eviction, close, and best-effort zeroing are explicit; the default cross-run cache TTL is zero.
- **Validation is pure:** `describe` and validation do not rotate, renew, rewrite, or otherwise mutate provider state. Preflight reads one pinned value and prepares it without a second fetch.
- **One requirements authority:** pre-read classification and compiled-plan enforcement consume the same registry so they cannot silently drift.

### FRAME decisions

| Decision | V1 choice | Rationale and consequence |
|---|---|---|
| Provider set | `env:`, `file:`, and HashiCorp Vault KV v2 only | This proves the narrow external-adapter seam without importing four cloud SDK families. AWS/GCP/Azure/1Password/systemd/SOPS-age remain demand-gated. |
| Version pinning | Vault references require a positive integer KV v2 version and a field. `latest`, omitted versions, `0`, and aliases are rejected. `env:`/`file:` retain logical `key_version="v1"` by default and gain an additive explicit logical version for rotation. | A job must identify the material it intends to use. Future AWS uses exact `VersionId`, GCP uses a numeric version ID, and Azure uses an exact version identifier; moving aliases such as `AWSCURRENT` or `latest` are not an F13 pin. |
| Dynamic credentials and renewal | V1 supports static Vault KV v2 values only. KV v2 does not issue secret leases. A response/descriptor indicating a dynamic `lease_id` or renewable lease fails with `secret_lease_unsupported`. There is no background renewal thread and no renewal after source access begins. | Auth token renewal belongs to the caller's Vault Agent/SDK/session. A future dynamic-secret adapter must add an explicit synchronous renewal/reauthentication contract and expiry tests before it can ship. |
| Cache lifetime | Per-run prepared material may be reused within that run. Cross-run process caching defaults to disabled (`0` seconds), can be opted into only in the range `0..60`, and expires at `min(configured TTL, provider expiry minus skew)`. Evict and close on expiry, error, cancellation, and shutdown. | This matches Platform's existing 60-second plaintext cache ceiling without creating a second Platform cache. Revocation visibility is bounded by the configured TTL; the secure default observes the next read immediately. |
| Algorithm validation | Requirements are selected from the matrix below. The current Decoy mask root keeps its `>=32` raw-byte rule, while Fernet, HMAC-SHA256, and AES-GCM use their own contracts. | **A universal 32-byte rule is explicitly rejected.** Encoding, exact sizes, minimum sizes, and library validation differ by algorithm and secret purpose. |

### Per-algorithm validation matrix

| Requirement ID | Accepted material | Validation and preparation | Rejection |
|---|---|---|---|
| `decoy-mask-root-v1` | Explicitly decoded raw bytes, at least 32 bytes | Preserve the current minimum, then use the existing HKDF-SHA256 derivation in `SecretKeyProvider.mask_key()` to obtain the 32-byte masking key. | Fewer than 32 decoded bytes or malformed encoding. |
| `fernet-v1` | A URL-safe base64-encoded Fernet key representing exactly 32 bytes | Construct `cryptography.fernet.Fernet(material)` and let the maintained library validate the encoding/length. Do not reimplement its parser. | Any constructor validation failure. |
| `hmac-sha256-v1` | Raw key of at least 32 bytes; longer values are allowed | Enforce the SHA-256 output-length minimum recommended by RFC 2104, then use maintained HMAC/HKDF primitives at the consuming layer. | Fewer than 32 bytes or malformed explicit encoding. |
| `aes-gcm-v1` | Raw key of exactly 16, 24, or 32 bytes | Construct `cryptography.hazmat.primitives.ciphers.aead.AESGCM(material)` so library validation remains authoritative. | Any other decoded length or constructor failure. |

Pattern scans and entropy estimates may identify malformed encodings but must never be used to certify strength. Legacy `env:`/`file:` references retain the existing hex-then-base64 decoding only for `decoy-mask-root-v1`; new typed references carry an explicit encoding so one secret cannot be reinterpreted differently by different consumers.

## 2. Established solutions surveyed before design

F13 follows provider and cryptographic-library contracts rather than inventing them.

- **Vault KV v2:** the official API reads an exact version with `GET /:mount/data/:path?version=:version` and returns version metadata including created, deletion, destroyed, and version fields. The Python `hvac` client exposes that model as `read_secret_version(path, version, mount_point)`. [Vault KV v2 API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2), [hvac KV v2 usage](https://python-hvac.org/en/stable/usage/secrets_engines/kv_v2.html)
- **Vault leases and auth:** dynamic secrets return a lease ID, duration, and renewable flag; expired or revoked leases are unusable. KV v2 values do not issue leases. Tokens/AppRole may have their own TTL and renewal, which is why V1 delegates auth-session renewal to the caller and rejects dynamic leased secret values. [Vault lease model](https://developer.hashicorp.com/vault/docs/concepts/lease), [Vault auth methods](https://developer.hashicorp.com/vault/docs/auth), [AppRole recommended pattern](https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern)
- **AWS Secrets Manager:** `GetSecretValue` supports exact `VersionId` and stage-based `VersionStage`; omitting both selects `AWSCURRENT`, which is unsuitable as an immutable job pin. AWS SDKs use the standardized credential-provider chain and can refresh credentials. A future adapter must require exact `VersionId` and the minimum `secretsmanager:GetSecretValue` permission. [GetSecretValue](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html), [AWS standardized credential providers](https://docs.aws.amazon.com/sdkref/latest/guide/standardized-credentials.html)
- **Google Cloud Secret Manager:** access uses a version resource name and supports numeric IDs or aliases such as `latest`; production guidance recommends an explicit version ID. Application Default Credentials provide the standard auth path, and `roles/secretmanager.secretAccessor` is the narrow access role. A future adapter must require a numeric version. [Access a secret version](https://docs.cloud.google.com/secret-manager/docs/access-secret-version), [Secret Manager authentication](https://docs.cloud.google.com/secret-manager/docs/authentication)
- **Azure Key Vault:** `SecretClient.get_secret(name, version)` reads a particular version and requires secret `get`; `DefaultAzureCredential` and managed identity are the standard auth path. A future adapter must require the exact version and must not enable SDK body logging. [SecretClient API](https://learn.microsoft.com/python/api/azure-keyvault-secrets/azure.keyvault.secrets.secretclient), [Azure Key Vault Secrets Python guidance](https://learn.microsoft.com/en-us/python/api/overview/azure/keyvault-secrets-readme)
- **Algorithm requirements:** Fernet requires a URL-safe base64-encoded 32-byte key; `AESGCM` accepts 128-, 192-, or 256-bit keys; RFC 2104 accepts variable HMAC keys but recommends at least the hash output length, which is 32 bytes for SHA-256. These are distinct requirements. [cryptography Fernet](https://cryptography.io/en/stable/fernet/), [cryptography AESGCM](https://cryptography.io/en/stable/hazmat/primitives/aead/), [RFC 2104](https://www.rfc-editor.org/rfc/rfc2104)

Use `cryptography`, which is already a base dependency at [`pyproject.toml`](../../pyproject.toml#L157), for Fernet/AES-GCM/HMAC/HKDF operations and validation. Do not hand-roll cryptography or duplicate checks a constructor already performs.

## 3. Current-state anchors and constraints

### Engine

- [`src/decoy_engine/keyprovider.py`](../../src/decoy_engine/keyprovider.py#L103) defines the public runtime-checkable `KeyProvider` protocol as only `key_version` plus `mask_key()`. `SecretKeyProvider` stores bytes in a frozen field excluded from dataclass repr, emits a custom redacted repr, enforces `MIN_SECRET_BYTES = 32`, and derives a 32-byte key with HKDF-SHA256 at lines 124–160. This is repr hygiene, not a zeroizing buffer.
- [`resolve_mask_secret_ref`](../../src/decoy_engine/keyprovider.py#L219) accepts only `env:` and `file:` and returns decoded bytes; [`key_provider_from_ref`](../../src/decoy_engine/keyprovider.py#L274) wraps them. Reference errors redact the reference rather than including its full value.
- [`require_mask_key`](../../src/decoy_engine/keyprovider.py#L369) owns the no-key gate. Today, GA fails closed, but the pre-GA branch at lines 396–397 silently constructs a `SeedKeyProvider`. F13 removes that fallback for keyed/protected work in every release phase. `job_seed` remains valid only for generation and genuinely unprotected behavior.
- `_ALWAYS_KEYED_STRATEGIES` and nested-strategy inspection at lines 279–356 currently decide which plan needs a key. They must move behind the single requirement registry used both before profiling and after plan compilation.
- The normal path's ordering defect is in [`build_run_context`](../../src/decoy_engine/execution/_pipeline_context.py#L166), and the chunked defect is in [`_oracle_preflight`](../../src/decoy_engine/execution/_chunked_oracle.py#L79). Both public chunked APIs share that oracle.
- [`unmask.py`](../../src/decoy_engine/unmask.py#L245) and [`vault.py`](../../src/decoy_engine/vault.py#L334) resolve key references separately. They must consume the shared preflight rather than remain alternate escape hatches.
- Existing public exports are in [`src/decoy_engine/__init__.py`](../../src/decoy_engine/__init__.py#L151). The compatibility contract allows a pre-GA hard correction but otherwise requires additive, keyword-only API evolution; existing duck-typed `KeyProvider` tests must continue to pass. See [`docs/compatibility-contract.md`](../compatibility-contract.md#L1).

### F29 safe boundary

F29 is already the canonical safe reason registry under [`src/decoy_engine/reasons`](../../src/decoy_engine/reasons). `ReasonRecord` accepts fixed registry identity/context rather than provider-controlled text, `Operation.RESOLVE_SECRET` already exists, and [`reason_adapter.py`](../../src/decoy_engine/reason_adapter.py#L146) maps exception classes to fixed records without copying raw exception messages. [`tests/security/test_reason_boundary.py`](../../tests/security/test_reason_boundary.py#L1) already uses a seeded-marker end-to-end leak test.

F13 must use `Phase.RESOLVE` and `Operation.RESOLVE_SECRET`; it must not add an unnecessary preflight phase. Existing env/file conditions keep their current codes: `bad_secret_ref`, `missing_mask_secret`, `weak_mask_secret`, and `keyed_strategy_requires_secret`. Add the following registered fixed codes and summaries, with no free-form context:

| Code | Fixed safe meaning | Recovery class |
|---|---|---|
| `secret_provider_unknown` | The referenced provider is not registered. | Repair configuration. |
| `secret_provider_unavailable` | The provider could not complete a safe read. | Retry operation or repair environment. |
| `secret_access_denied` | The workload is not permitted to read the pinned secret. | Repair authorization/environment. |
| `secret_version_unavailable` | The pinned version is absent, deleted, destroyed, or inaccessible. | Repair configuration/version. |
| `secret_expired` | The selected secret is outside its usable lifetime. | Rotate or select a usable version. |
| `secret_lease_unsupported` | The selected value requires a dynamic lease that V1 cannot safely maintain. | Select a static value or add an approved adapter. |
| `secret_material_invalid` | Material does not meet the selected algorithm contract. | Replace the secret/configuration. |

A closed `SecretProviderError` hierarchy may retain the SDK exception as `__cause__` for local debugging, but its own message must be fixed and the cause must never be logged, serialized, or returned. The reason adapter trusts only the exception type and registered code. When F31 lands, adopt its approved-reference policy at this same choke point; do not defer F13 or open a parallel output path.

### Platform reuse, not duplication

At the planning observation point, Platform `origin/main` was `051ff2b7ab0ce835a6bd4eca103448e04579fdba`. `api/connectors/crypto.py` loads the configured Fernet key, validates it by constructing `Fernet(raw)`, preflights it at startup, and owns encryption/decryption for inline secrets. `api/seeds/secrets.py` performs create-time `cloud_ref` test-fetch, uses typed fail-closed errors, and owns a 60-second in-process plaintext cache. Its current `cloud_ref` resolves an environment-variable name and logs the reference name, never the value.

The Platform slice must wrap/delegate those facilities:

- Keep Fernet-at-rest and inline-secret encryption in `api/connectors/crypto.py`.
- Normalize the existing cloud-ref environment-name form to the engine's `env:NAME` form without breaking stored records.
- Route new provider references through an engine-backed adapter but do not add a second plaintext cache. The engine policy supplied by Platform must not exceed Platform's existing 60-second ceiling.
- Run Platform authorization/F45 checks before invoking the provider.
- Never store an auth token or resolved value in connector configuration, pipeline YAML, evidence, API responses, or logs.

Before editing Platform, re-open these files on current `origin/main` and record changed anchors in the implementation evidence. If their ownership changed, preserve the same single-owner behavior rather than copying it.

### CLI reuse

At the planning observation point, CLI `origin/main` was `574aa1e9ec2b95f9ba8af5eb8112c1a3c0fcf434`. `src/decoy/cli/run.py` locally validates only `env:`/`file:` prefixes and some error paths emit `str(exc)` into text/JSON. The CLI slice must re-ground current main, delete duplicate scheme knowledge in favor of the engine parser/registry, and render provider failures only through F29's fixed record. F31 can later replace that renderer at the same seam.

## 4. Design

### 4.1 Public contract and compatibility bridge

Create a focused `src/decoy_engine/secret_providers/` package rather than extending the already broad `keyprovider.py`:

```text
secret_providers/
  __init__.py        public contract exports
  _types.py          refs, descriptors, resolved/prepared objects, policies
  _buffer.py         SecretBuffer and lifetime ownership
  _requirements.py   algorithm validators and plan/config classification
  _cache.py          bounded optional process cache
  _registry.py       provider registration and scheme selection
  _local.py          env/file providers
  _preflight.py      trusted choke point and F29-safe translation
  vault.py           optional Vault KV v2 adapter
```

Add and export a runtime-checkable `SecretProvider` protocol with the immutable API identifier `secret-provider/v1`:

```python
class SecretProvider(Protocol):
    api_version: Literal["secret-provider/v1"]
    scheme: str

    def describe(self, ref: SecretRef) -> SecretDescriptor: ...
    def resolve(self, ref: SecretRef) -> ResolvedSecret: ...
    def preflight(
        self,
        ref: SecretRef,
        requirements: tuple[SecretRequirement, ...],
        cache_policy: SecretCachePolicy,
    ) -> PreparedSecret: ...
```

`describe` returns metadata only and never fetches or returns the value. `resolve` performs the one provider read and returns a context-managed `ResolvedSecret`. `preflight` is the public admission operation: every shipped provider implements it as a thin delegate to the common `prepare_secret(provider=self, ...)` choke point, which calls `describe`, reads once, validates all selected requirements without mutation, derives the runtime material, and returns a context-managed `PreparedSecret`. Adapter-specific code owns only metadata/read translation; it cannot substitute its own validator or output boundary. The prepared object reuses the fetched value; it must never call `resolve` a second time and risk version drift.

Preserve the current public `KeyProvider` protocol exactly. Do not add abstract methods to it: that would break runtime protocol checks and existing third-party duck types. Add `LegacyKeyProviderAdapter` so old objects enter the new preflight pipeline. Existing `key_provider=` keyword behavior and precedence remain; new registry/policy inputs are additive and keyword-only. A supplied legacy provider has no external ref or cross-run cache, receives a value-free logical descriptor, and is allowed only for the `decoy-mask-root-v1` requirement unless it explicitly implements the versioned protocol.

Keep `SeedKeyProvider` import-compatible, but make it ineligible to satisfy a protected/keyed requirement. Document that pre-GA correction under the compatibility contract. Lower-level `require_mask_key()` remains a defense-in-depth assertion and consumes only a prepared provider for keyed work.

Do not confuse this provider registry with `providers_v2.ProviderRegistry`; use the explicit name `SecretProviderRegistry`.

### 4.2 References and version identity

Use typed `SecretRef` parsing owned by the engine. A Vault reference is:

```text
vault://<mount>/<path>?version=<positive-integer>&field=<field>&encoding=<base64|hex|raw>
```

The parser rejects userinfo, embedded credentials, fragments, omitted versions/fields, duplicate query keys, moving aliases, traversal segments, invalid UTF-8, and unsupported encodings. Error output names only the scheme and fixed reason; it never repeats the path, field, or full reference. Auth address, token, namespace, role, and TLS material are out-of-band client configuration and must not appear in job YAML.

For `env:` and `file:`, preserve accepted syntax and the default non-secret `key_version="v1"`. Add an optional logical version setting, defaulting to `v1`, so rotation can intentionally change output identity without exposing a value fingerprint. Vault obtains its safe `key_version` from the requested/returned exact KV version and rejects a mismatch. Never derive `key_version` from secret bytes, a digest, a lease ID, or a file path.

`SecretDescriptor` contains only allowlisted value-free fields: contract version, provider/scheme, non-secret key version, creation/expiry timestamps when the provider supplies them, renewable flag, and effective cache deadline/status. It excludes path, field, canonical reference, value length, value digest, lease ID, token, accessor, namespace, and provider exception text. Its JSON representation is allowlist-based and is covered by the leak sentry.

### 4.3 Secret buffer and choke point

`SecretBuffer` owns mutable byte storage, has constant redacted `repr` and `str`, refuses pickle/copy/JSON/dataclass serialization, exposes material only as a scoped read-only view to validator/deriver functions, and overwrites its owned buffer on `close()`/context exit. `ResolvedSecret` and `PreparedSecret` are also context-managed, redacted, and non-serializable. A prepared masking key is closed on normal return, exception, cancellation, generator/iterator close, and process-cache eviction.

Python, SDKs, base64 decoders, and cryptographic libraries can create immutable copies, so the documentation must state that memory clearing is best effort, not a hard zeroization guarantee. The implementation must minimize copies, release references promptly, and never claim more.

All values cross exactly one engine choke point: `prepare_secret()` in `_preflight.py`. Provider adapters may return owned buffers to that function but may not log, serialize, inspect for entropy, or hand bytes directly to execution code. The choke point validates requirements, derives the masking key, translates exceptions to F29-safe types, and closes raw material immediately unless the explicit cache owns it.

Add a build-failing structural sentry that rejects adapter logging of buffers/raw SDK exceptions, provider-specific direct resolution from execution modules, serializers/pickle hooks on secret-bearing types, and raw-material access outside the buffer/preflight/validator allowlist. This is in addition to, not a substitute for, runtime leak scans.

### 4.4 Requirements and validation

Introduce immutable `SecretRequirement` records keyed by the four IDs in the FRAME matrix. Each owns encoding policy, library-backed validation, preparation, and the safe invalid-material reason. Validators are pure and must not mutate remote state or a source buffer.

Move the current keyed-strategy rules from `keyprovider.py` into this registry. Add `secret_requirements_for_config(config)` for pre-read classification and have compiled-plan inspection consume the same registry. After compilation, assert that the compiled requirements equal the pre-read result. A mismatch is an internal invariant failure before masking, not permission to fetch again or continue. Property tests generate supported strategies/nesting to prove both classifiers remain equivalent.

The config-only classifier must not inspect source schema or rows. If a future algorithm truly cannot be selected without profiling, it must redesign its configuration or receive a separately approved admission phase; it may not weaken the before-read guarantee.

### 4.5 Cache and lifetime contract

Separate two lifetimes:

1. **Per-run ownership:** a successful preflight returns one `PreparedSecret` used throughout that run. This is mandatory reuse, not a cross-run cache.
2. **Optional process cache:** disabled by default; explicit TTL is an integer from 0 through 60 seconds. Its internal key is provider identity, canonical ref, pinned version, and requirement set. The key itself is secret-sensitive metadata and never appears in logs/metrics/errors.

Use an injected monotonic clock. Effective expiry is the earliest of configured TTL, provider `expires_at` minus a documented safety skew, auth/lease validity if applicable, run close, or explicit invalidation. Do not extend TTL on read. Evict and best-effort wipe on expiry, resolve/validation error, cancellation, provider close, and process shutdown. Cache failures are closed; stale material is never returned as a fallback.

With V1's static exact-version Vault value, a revocation/deletion after admission can remain visible until the effective TTL. Default TTL zero eliminates cross-run persistence; operators choosing a positive TTL accept a bounded maximum of 60 seconds and must see that contract in docs. Provider metadata that is already expired fails `secret_expired`. Dynamic leased material fails `secret_lease_unsupported` before cache insertion.

Platform continues to own its existing 60-second cache. Its engine bridge passes the already-resolved value into the per-run preparation path or disables engine cross-run caching; it must never stack two 60-second caches.

### 4.6 Vault KV v2 adapter and dependency decision

`VaultKV2Provider` accepts a caller-injected, authenticated `hvac.Client`. A small CLI construction helper may use standard `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_NAMESPACE`, and the SDK's existing auth behavior, but never copies those values into an engine ref or serialized config. Platform injects a client after its authorization checks.

For preflight, call `client.secrets.kv.v2.read_secret_version(path=..., version=..., mount_point=...)` exactly once, verify returned metadata matches the requested version and is neither deleted nor destroyed, select the named field, decode according to the explicit encoding, and transfer ownership to `SecretBuffer`. Translate permission, transport, missing-version, deleted/destroyed, expired, and malformed-response failures to the fixed F29 taxonomy. Do not surface Vault response bodies.

Minimum Vault policy grants read only to the exact `/<mount>/data/<path>` values a workload needs. It does not grant list, create, update, delete, destroy, metadata write, token creation, or policy administration. TLS verification remains enabled. Static KV v2 values have no renewable lease; unexpected leased/dynamic data is rejected.

Add a new optional extra:

```toml
hashicorp-vault = ["hvac==2.4.0"]
```

Keep the existing `[vault] = []` compatibility alias untouched because it refers to Decoy's token-vault feature and must not be silently repurposed. Lazy-import `hvac`; base engine import and env/file operation must work without the extra. Commit the exact hash-locked `uv.lock` update. Record the `hvac` Apache-2.0 license, PyPI provenance/attestations, transitive dependency delta, vulnerability scan, and maintenance/release evidence in the review artifact. CI installs and tests both base/no-extra and `hashicorp-vault` environments. Any dependency change from this exact pin returns to owner review.

### 4.7 Execution ordering and surface parity

Change normal admission in this order:

1. Validate configuration-only knobs and construct non-secret run metadata/job seed.
2. Classify secret requirements from configuration.
3. Parse/select provider and preflight the pinned secret.
4. Only after success, call `profile_source()` and compile the plan.
5. Cross-check compiled requirements against the pre-read classification without refetching.
6. Execute with the prepared key and close it on every exit.

In `_oracle_preflight`, perform steps 1–3 before constructing/advancing the chunks iterator. Only then request the first chunk, compile, and cross-check. This single change covers both `run_mask_chunked` and `run_mask_pipeline_chunked`; tests must name both public entry points.

Route unmasking and vault-writer construction through the same preflight before reading masked sources, token-vault files, or protected columns. Audit every call to `resolve_mask_secret_ref`, `key_provider_from_ref`, `resolve_key_provider`, `require_mask_key`, and `mask_key()` and either route it through preparation or document it as a private already-prepared call. No secondary parser or raw resolution path remains.

## 5. RED-first acceptance specification

Slice 0 adds these acceptance tests before production behavior. They must fail on baseline for the intended reason, not because of a broken fixture.

1. **Before-read missing secret:** configure a keyed strategy with a missing `env:` ref and a source path that would itself fail if opened. Assert the F29 missing-secret reason is returned and the source-open witness is zero.
2. **Before-iteration provider failures:** use a chunk iterable whose `__iter__` and `__next__` raise and count calls. Unknown scheme, inaccessible Vault version, deleted/destroyed version, expired metadata, and denied access must each return their registered safe F29 reason with both counters zero for both public chunked APIs.
3. **Fail-closed unknown provider:** an unregistered scheme never invokes masking, job-seed fallback, source profiling, or adapter fallback.
4. **Algorithm matrix:** a valid Fernet key whose decoded payload is 32 bytes passes via `Fernet`; a 31-byte HMAC-SHA256 key fails while a 32-byte key passes; AES-GCM accepts exactly 16/24/32 and rejects 15/20/31/33; the Decoy mask root accepts 32+ and rejects 31. Include malformed encodings. This test must make it impossible to replace the registry with a universal `len >= 32` check.
5. **No silent fallback:** in pre-GA and GA configurations, keyed work without a valid prepared provider returns `keyed_strategy_requires_secret`; poison spies prove `SeedKeyProvider` and the masking adapter never run.
6. **Value-free describe:** every provider's descriptor serializes only the allowlist; assert it lacks ref/path/field/value/length/hash/lease/token/exception data and that `describe` does not read the value.
7. **One read, one version:** successful preflight calls provider resolution once, verifies the pinned version, and execution reuses that result. A provider whose second read changes data must still have a call count of one.
8. **Cache lifetime:** with an injected monotonic clock, TTL zero never reuses across runs; opt-in reuse stops at the exact deadline; TTL over 60 is rejected; provider expiry shortens TTL; close/error/cancel evicts and wipes; an expired entry causes a new pinned read rather than stale fallback.
9. **Lease/renewal path:** static KV v2 succeeds without lease machinery. A dynamic/renewable response fails safely with `secret_lease_unsupported`, performs no renewal mutation, is not cached, and reads no source. This is the V1 renewal-path acceptance test.
10. **Seeded-marker leak sentry:** place a unique marker in a secret value, Vault error payload, reference path/field, and SDK exception. Exercise success and every failure through direct engine, in-process worker, process worker, normal/chunked, unmask/vault writer, CLI text/JSON, Platform API/log, progress events, `ExecutionResult`, evidence, YAML, JSON, repr/str, and exception chaining. Recursively scan captured bytes/text and serialized artifacts; the marker must be absent. Include positive controls proving the expected fixed reason and ordinary non-secret output were captured.
11. **Surface parity:** the same authorized Vault reference yields the same non-secret `key_version` and deterministic masked output through engine, CLI, and Platform, while the value is absent from pipeline YAML, evidence, errors, and stored connector configuration.
12. **Compatibility:** existing custom/duck-typed `KeyProvider` objects, existing env/file references, signatures, and non-keyed behavior remain compatible; protected legacy providers pass through the compatibility adapter. Existing F29 isolation tests continue to produce the same records across direct/in-process/worker boundaries.

Do not mock past the boundary being tested. Use actual environment variables and filesystem permissions for local providers, and a real Vault dev service pinned by immutable container digest for permission, exact-version, deleted/destroyed, and transport behavior. The CI change must record the selected supported Vault image tag and digest in the test configuration and supply-chain evidence. SDK fakes are permitted only for isolated protocol unit tests; they cannot satisfy adapter, ordering, leak, or cross-surface acceptance. A controlled monotonic clock is permitted because time itself is the cache seam.

## 6. Implementation plan

Every slice closes with the same mandatory loop: self-review against this document and the diff; run the slice's targeted checks; obtain adversarial review; remediate every BLOCKER/HIGH and all accepted lower findings; rerun affected checks. Do not defer known security findings to a later slice.

### Slice 0 — Versioned contract, reasons, and RED acceptance

**Engine changes**

- Add the `secret_providers` package skeleton, immutable public types, protocol, typed refs, value-free descriptor schema, cache-policy schema, error hierarchy, and compatibility adapter interfaces.
- Add the requirement IDs and a single strategy-to-requirement registry while keeping production resolution behavior unchanged until Slice 1.
- Register the seven F29 reasons under `Phase.RESOLVE`/`Operation.RESOLVE_SECRET`; extend `reason_adapter.py` to map only the closed provider error hierarchy.
- Export additive public types from `decoy_engine.__init__` without changing `KeyProvider`.
- Add all RED-first acceptance tests and leak-sentry structure. Capture the baseline failure list as evidence; distinguish expected RED failures from unrelated failures.

**Targeted verification:** reason-registry validation, reason boundary/isolation tests, public API/compatibility sentries, protocol/type tests, requirement-classifier properties, and the acceptance suite demonstrating intended RED.

### Slice 1 — Local providers, buffer/lifetime, and before-read engine admission

**Engine changes**

- Implement `SecretBuffer`, `ResolvedSecret`, `PreparedSecret`, scoped validation/derivation, and close/zeroing behavior.
- Implement `env:` and `file:` providers through the new contract and preserve legacy decode/output behavior for `decoy-mask-root-v1`.
- Implement algorithm validators with `cryptography`, the bounded cache, injected monotonic clock, fixed maximum TTL, and per-run ownership.
- Change `build_run_context` and `_oracle_preflight` ordering exactly as specified; add the compiled-plan requirements cross-check.
- Route unmask and vault-writer paths through shared preflight. Audit all old resolver/key calls and remove alternate raw-value flows.
- Remove keyed pre-GA `job_seed` fallback. Preserve generation-only uses and compatibility imports.
- Make the local, matrix, cache, before-read, no-fallback, compatibility, and engine leak tests GREEN.

**Targeted verification:** `tests/unit/test_de02_keyprovider.py`, `tests/property/test_keyprovider_invariants.py`, normal/chunked admission tests, unmask/vault tests, F29 integration/security tests, secret-provider unit/property suite, and the leak/AST sentry.

### Slice 2 — Optional Vault KV v2 adapter

**Engine changes**

- Add the exact `hashicorp-vault` optional extra, lazy import, locked dependency update, supply-chain record, and base/extra install matrix.
- Implement strict Vault ref parsing and `VaultKV2Provider` with injected client, exact one-read version verification, static-KV-only behavior, minimum information in descriptors, and F29 translation.
- Add a live Vault service pinned by immutable digest. Install policies/tokens that prove allowed exact read, denied path, missing/deleted/destroyed version, and no list/write privileges. Do not persist test tokens in artifacts.
- Make live adapter, lifecycle, version, cache, before-read, and seeded-leak acceptance GREEN.

**Targeted verification:** base import without `hvac`; env/file base tests; optional-extra unit tests; live Vault integration; dependency/license/vulnerability/provenance checks; lockfile consistency.

### Slice 3 — CLI consumer, after remote re-grounding

**Precondition:** fetch current CLI main, record its SHA and current parsing/output anchors, and stop for owner guidance if its architecture no longer has an equivalent safe seam.

**CLI changes**

- Replace local scheme/prefix checks with the engine's typed parser/registry.
- Add only the optional Vault dependency wiring needed to construct/inject the provider; credentials remain standard environment/agent inputs and never command arguments saved in history or YAML.
- Ensure normal, chunked, tokenize, and unmask commands preflight through the engine before input reads.
- Replace provider-related `str(exc)` output with F29 fixed summaries and codes for text/JSON. Adopt F31 at this same renderer only if it has landed.
- Add CLI seeded-marker, no-input-read, unknown-provider, exact-version, and live-Vault tests.

**Targeted verification:** CLI unit/e2e suite, text/JSON golden output, engine-version compatibility matrix, live Vault tests, and recursive artifact/output leak scan.

### Slice 4 — Platform consumer, after remote re-grounding

**Precondition:** fetch current Platform main, record its SHA, and re-open `api/connectors/crypto.py`, cloud-ref resolution, seed-secret caching, authorization, API error, evidence, and worker seams before editing.

**Platform changes**

- Preserve the existing Fernet-at-rest owner and startup validation. Do not re-encrypt, migrate, or expose existing inline secrets as part of F13.
- Wrap/delegate the existing cloud-ref resolver and its cache into the engine contract; normalize legacy environment-name refs additively. Disable the engine cross-run cache when Platform's cache owns lifetime.
- Add Vault reference support only after F45/current authorization succeeds, using an injected authenticated client and exact version. Store only the reference and value-free metadata.
- Translate engine F29 records to existing typed Platform errors without raw causes. Ensure job admission completes before source rows or connector data are read.
- Add create-time and run-time tests, same-reference engine/CLI/Platform parity, ≤60-second expiry tests, authorization-before-provider tests, and API/log/evidence/YAML seeded-marker scans.

**Targeted verification:** connector crypto/secrets tests, auth tests, API/worker/evidence tests, live Vault integration, cache controlled-clock tests, migration/backward-compatibility tests, and full Platform leak scan.

### Slice 5 — Cross-repository closeout and documentation

- Run cross-repository compatibility tests at exact engine/CLI/Platform SHAs, including direct/in-process/process-worker execution and the same Vault reference.
- Complete the documentation set below, capability/compatibility records, dependency evidence, and operational examples with synthetic names only.
- Run mutation and full-suite gates; remediate survivors and review findings.
- Record exact commands, versions, SHAs, Vault image digest, test summaries, coverage/mutation results, dependency review, and any environment-limited checks. No ambiguous “tests passed” evidence.
- Complete the review and repository gate chain in Section 10.

## 7. Verification and evidence plan

### Required checks

- Formatting/lint/type: project-standard `ruff format --check`, `ruff check`, and `mypy` scopes for every changed package.
- Targeted engine: key-provider unit/property tests; normal and chunked execution admission; unmask/vault writer; F29 registry/adapter/isolation/security; provider protocol/local/cache/algorithm/Vault suites; public API and compatibility sentries.
- Security: seeded-marker runtime scan, structural import/logging/serialization sentry, provider exception fuzz/property tests, strict ref parser fuzz/property tests, and authorization-before-provider tests in consumers.
- Integration: actual env/file reads; direct/in-process/process worker parity; real Vault with minimum policies and immutable image digest; CLI text/JSON; Platform API/worker/evidence.
- Dependency matrix: base install with no `hvac`; `hashicorp-vault` extra; locked/reproducible install; vulnerability, license, provenance, and transitive-delta review.
- Mutation: target `secret_providers`, `keyprovider`, F29 adapter, and pre-read admission. Required killed mutants include removing preflight, moving it after first read, allowing moving/latest versions, weakening any algorithm size, returning expired cache entries, extending TTL on hit, emitting provider text, skipping close, or restoring `job_seed` fallback.
- Full regression: `pytest tests/ -n auto` or the repository's documented equivalent, plus the repository test-flight thresholds when the changed engine block qualifies. Run the corresponding documented full suites in CLI and Platform.
- Documentation/link/build checks and lockfile consistency.

### Evidence rules

Evidence is attached per repository at the exact reviewed commit and includes:

- command, exit status, duration, and concise result;
- exact repo SHA and dependency lock hash;
- Vault server tag plus immutable digest and policy fixture identities without tokens;
- RED baseline results and the matching GREEN results;
- seeded leak marker and scanned surface classes, without the actual secret value;
- branch/condition coverage for validation, errors, expiry, eviction, and close;
- mutation score plus every surviving mutant and disposition;
- dependency provenance/license/vulnerability decision;
- reviewer findings and remediation commits;
- any unrun check with reason, risk, and owner decision. Missing infrastructure is not represented as a pass.

## 8. Documentation plan

Update documentation only to describe shipped behavior:

- Engine `README.md` and public API reference: versioned provider contract, env/file/Vault examples, exact-version syntax, compatibility bridge, and no-fallback behavior.
- `CODEMAP.md`: new provider package, trusted preflight choke point, requirement registry, and execution admission seams.
- `docs/compatibility-contract.md`: additive protocol/export changes, legacy `KeyProvider` behavior, and the pre-GA removal of keyed seed fallback.
- `docs/security/key-derivation.md`: replace obsolete universal/fallback language with the requirement matrix and library-backed validation.
- Add `docs/security/secret-providers.md`: auth separation, minimum Vault policy, reference grammar, cache/expiry/revocation bounds, KV-vs-dynamic lease behavior, rotation/version changes, best-effort memory clearing, logging prohibitions, optional dependency, and incident invalidation procedure.
- Changelog/release notes and docs index; update the capability matrix if it records public engine capabilities.
- CLI help/operator docs: supported schemes, optional install, standard Vault auth inputs, safe reason output, and exact version requirement.
- Platform operator/API docs: inline Fernet ownership, legacy cloud-ref compatibility, Vault authorization order, single-cache ownership, rotation, and safe failure codes.
- Update the single authoritative roadmap/shipped log only after merge, in the repository that owns it. Do not duplicate roadmap state across repos.

Examples must use synthetic values and paths. Never paste a real token, secret, lease ID, full reference, or captured provider response. Run documentation synchronization after logic is stable so prose reflects the reviewed implementation.

## 9. Risks, rollback, and operational limits

| Risk | Control |
|---|---|
| Source read precedes admission on an overlooked surface | Central requirement/preflight APIs, audit of all legacy resolution calls, poison source/iterator witnesses, structural sentry, direct/worker/CLI/Platform parity tests. |
| Provider/SDK text leaks secret material | Closed exception hierarchy, F29 fixed registry, no raw cause output, seeded-marker scans over every output class, structural adapter logging ban. |
| Algorithm rules collapse to one size | Named requirement registry, constructor-backed validation, boundary matrices, and mutation tests that must kill universal-length implementations. |
| Cache serves revoked/expired material | TTL zero default, hard 60-second cap, provider-expiry minimum, no sliding extension, deterministic eviction/close tests, documented bounded revocation visibility. |
| Version drift between check and use | Required exact version, single provider read, returned-version verification, prepared-object reuse. |
| Optional SDK expands supply-chain risk | Exact pin and hash lock, lazy optional extra, no-extra CI, license/provenance/vulnerability/transitive review, one external adapter only. |
| Interface breaks existing callers | Preserve `KeyProvider`, additive keyword-only inputs, compatibility adapter, public signature/import/duck-type tests. |
| Memory-clearing promise is overstated | Mutable owned buffer and prompt close, but documentation explicitly says Python/library copies make clearing best effort. |
| Platform duplicates encryption/cache/auth | Re-ground and delegate to existing owners; F45 before provider; single cache owner; cross-repo architecture review. |

Rollback may unregister the external Vault adapter, remove its optional install path, and return consumers to env/file while preserving stored references for a later retry. It may not re-enable the silent `job_seed` fallback, emit raw provider errors, or bypass F29. If operational rollback appears to require any of those, stop and obtain owner approval for a new R2 plan. No implementation step in this plan authorizes production secret rotation, deletion, policy mutation, or live credential changes.

## 10. Review gates and remote-worker handoff

### Plan gate

This plan is reviewed in the required order:

1. Gated Gemini reviews framing, established-solution evidence, cross-repository completeness, and acceptance strength.
2. Opus-4.8 Dennis cold-reads adversarially and reports severity-tiered findings without writing fixes.
3. The owner resolves/accepts findings and approves the plan for implementation.

The remote `claude.ai/code` worker receives only the approved document and remote repositories. It must begin by verifying the engine baseline and re-grounding current CLI/Platform main; it must not rely on local paths or unstated conversation context. If baseline code, public contracts, consumer ownership, or F29 semantics have materially changed, it pauses and returns concrete evidence rather than guessing.

### Code gate

For each repository and again for the integrated final SHAs:

1. The implementer completes self-review, targeted verification, evidence, and remediation.
2. **Codex reviews first.** Resolve every BLOCKER and HIGH, record lower-severity dispositions, and rerun affected checks.
3. **Opus-4.8 Dennis reviews second.** He runs/cold-reads the quality, security, and QA gates. Resolve every BLOCKER and HIGH and repeat Codex then Dennis if fixes materially change the risk surface.
4. Barry synchronizes the documentation to the code that actually shipped and does not change logic.
5. Run full verification at the final HEAD. In `decoy-engine`, record the pass for that exact commit with `bash .claude/hooks/decoy-gate-pass.sh`; any new commit invalidates the receipt and repeats the applicable gate.
6. The owner reviews evidence and authorizes merge. Push/PR/merge are not implied by plan completion.

No gate may be satisfied by mocked-past-boundary tests, an earlier commit's receipt, or a statement without exact evidence.
