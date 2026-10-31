# Key derivation and the KeyProvider model

This document describes how `decoy-engine` sources the secret behind every
re-identification-protecting derivation, how that secret is turned into
purpose-scoped keys, and what happens when no secret (or a weak one) is
supplied. It supersedes the earlier `DECOY_MASTER_KEY` / `make_key_resolver`
design, which was never wired into the live masking path -- see the
[DE-02 KeyProvider design brief](https://github.com/louiskeep/decoy-engine/blob/main/docs/security/de-02-keyprovider-design.md)
for the finding and the migration (excluded from the built docs site; see
`docs/conf.py`).

## The mask_key model (DE-02)

Every keyed strategy handler -- `fpe`, `hash`, `date_shift`, `code_set`,
`joint_mask`, deterministic `faker`/`categorical`/composites, and the token
vault's Fernet key -- consumes a single per-job value: `StrategyContext.mask_key`
(bytes). Handlers draw from `mask_key`, **never** from `job_seed` (the
8-byte generation-reproducibility seed). This is a one-slot substitution: the
same `derive(...)` domain separation by namespace and per-purpose label that
existed before DE-02 applies unchanged; only the root IKM fed into it moved.

```
mask_key = HKDF-SHA256(secret, purpose/version)     (a >=32-byte secret is admitted)
mask_key = job_seed                                (only for plans with NO keyed strategy; inert)
```

## Where the secret comes from

The engine only ever accepts raw bytes. It never fetches, generates, or
stores a secret itself:

- **`SecretKeyProvider`** wraps a caller-supplied `bytes` secret (>=32 bytes,
  enforced at construction) and derives `mask_key()` as
  `HKDF-SHA256(secret, salt=<keyprovider salt>, info="decoy/mask/{key_version}")`.
- **`SeedKeyProvider`** stays importable but is **never a secret**: keyed work given one fails
  with `keyed_strategy_requires_secret`. `job_seed` is for generation only.
- **`global_settings.mask_secret_ref`** is a *reference*, never the secret itself, resolved by the
  versioned `secret-provider/v1` contract (see [secret providers](secret-providers.md)): `env:NAME`,
  `file:/PATH` (hex- or base64-decoded for the mask root; any other requirement needs an explicit
  `?encoding=`) or a registered provider such as `vault://`. Admission runs before any source is
  read (`decoy_engine.secret_providers.admit_mask_key`).
- A programmatic `run(key_provider=...)` (any object implementing the
  `KeyProvider` protocol: `key_version` + `mask_key()`) takes precedence over
  `mask_secret_ref`.

The raw secret is **never serialized** by the engine into a plan or a log.
The non-secret `key_version` label (e.g. `"v1"`, or `"seed"` for the
no-secret fallback) is a safe-to-record identifier for which key era
produced an artifact. Recording it into an evidence manifest is a
platform-layer concern, not an engine guarantee: the engine emits no
evidence manifest itself; the commercial platform's evidence assembler is
what stamps `provider.key_version` onto a job so an auditor can confirm two
jobs shared a key era without recovering any key material. Per-tenant secret
management (KMS, rotation policy, tenancy) is likewise a platform-layer
concern; the engine ships no KMS client.

## Fail-closed enforcement

Presence and strength (`>= 32` bytes) of the resolved secret are enforced at
every masking choke point -- `run_pipeline`'s fail-fast pre-flight and each
adapter's `StrategyContext` construction (including the out-of-core runner)
-- via `decoy_engine.keyprovider.require_mask_key`, so no entry point can
run keyed masking off a weak or absent secret at GA. This also covers nested
strategy children and any column declared `vault: true` (a vault-bound
column is always treated as keyed re-identification surface, even under an
otherwise-anonymizing strategy, because it persists a reversible mapping).

Error types (`decoy_engine.keyprovider`, all subclasses of `MaskSecretError`):

| Error | Code | Raised when |
|---|---|---|
| `KeyedStrategyRequiresSecret` | `keyed_strategy_requires_secret` | A plan with a keyed strategy runs with no admitted secret (any release phase), is given a `SeedKeyProvider`, or its resolved key is < 32 bytes. |
| `MissingMaskSecret` | `missing_mask_secret` | A `mask_secret_ref` points at an unset env var or an unreadable file. |
| `WeakMaskSecret` | `weak_mask_secret` | A resolved secret is shorter than 32 bytes (enforced at `SecretKeyProvider` construction, pre-GA or GA). |
| `MaskSecretError` (`bad_secret_ref`) | `bad_secret_ref` | The reference is malformed, or the material is not decodable (an unrecognized scheme is `secret_provider_unknown`). |

A keyed plan with no admitted secret, a `SeedKeyProvider`, or a mask key under 32 bytes
hard-errors with `KeyedStrategyRequiresSecret` in **every** release phase (the pre-GA `job_seed`
fallback was removed by F13). Material rules are per algorithm, not a universal 32-byte rule; see
[secret providers](secret-providers.md).

For local development, a throwaway secret is enough:

```bash
export DECOY_MASK_SECRET=$(openssl rand -hex 32)
```

```yaml
global_settings:
  seed: 42
  mask_secret_ref: "env:DECOY_MASK_SECRET"
```

## Implementation

- `src/decoy_engine/keyprovider.py`: `KeyProvider` protocol, `SecretKeyProvider`,
  `SeedKeyProvider`, ref resolution, and the fail-closed gate
  (`require_mask_key` / `resolve_key_provider`).
- `src/decoy_engine/execution/_adapter.py`: `StrategyContext.mask_key`, the
  field every strategy handler reads.
- `src/decoy_engine/execution/_pipeline.py`: `run_pipeline` resolves the
  provider once (fail-fast) and threads it into every route.
- `src/decoy_engine/config/_global_settings.py`: the `mask_secret_ref` field.
- `src/decoy_engine/vault.py`: the token vault's Fernet key derives from
  `mask_key`, not `job_seed` -- see [token vault](token-vault.md).

## The legacy `derive_key` resolver (superseded)

`src/decoy_engine/context.py` still exposes `make_key_resolver` and an
`ExecutionContext.derive_key` / `pipeline_derive_key` pair. This predates
DE-02. Do not confuse it with the `mask_key` model above:

- `ExecutionContext.derive_key` (`context.py`) was the legacy **mask**
  resolver. The V1 strategy classes that consumed it -- `transforms/fpe.py`
  and `transforms/date_shift.py`, both calling `self.derive_key("mask")` --
  are dead on the live masking path: V2 handlers key off
  `StrategyContext.mask_key` instead. `ExecutionContext.pipeline_derive_key`
  was the legacy generate resolver.
- Synthetic *generation* determinism on the live path takes its key material
  from the `derive_key` **keyword argument to `run_pipeline`**, threaded into
  `GenDeriveContext` (`generators/derivation.py`) -- a separate input from
  `ExecutionContext.derive_key`, despite the shared name.

`DECOY_MASTER_KEY` and `make_key_resolver` are superseded for masking by the
`KeyProvider` model; do not use them, or the env var, to reason about the
strength of masked output. Masked output keys off `mask_key`, never the
legacy resolver.

## Relationship to the evidence manifest

The evidence manifest is a **platform** artifact, not an engine one. The
engine exposes a job's non-secret `key_version` (e.g. `"v1"` for a
`SecretKeyProvider`, `"seed"` for the no-secret fallback), never any key
material; the commercial platform's evidence assembler is what records
`provider.key_version` into the manifest. Auditors can then confirm that two
jobs used the same key era by comparing `key_version` values; they cannot
reconstruct any key from the label alone.

See [SQL surfaces](sql-surfaces.md) for the companion security doc covering
SQL injection surfaces, and [token vault](token-vault.md) for the vault's
own threat model.
