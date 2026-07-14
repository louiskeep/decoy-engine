# Token vault: handling and threat model

The token vault (`decoy_engine.vault`, `decoy run --vault`,
`decoy unmask --vault`) makes one-way strategies reversible by
recording each vaulted column's source-to-masked map at mask time. It
is the most sensitive artifact the engine can produce, and it changes
what an operator must protect.

## What the vault is

An encrypted file containing `(namespace, masked_value) ->
source_value` triples for every column declared `vault: true`. Each
bounded chunk of entries is serialized as a Parquet table and encrypted
with Fernet (AES-128-CBC + HMAC-SHA256, encrypt-then-MAC, per the
Fernet spec) from the `cryptography` package, installed via the optional
`vault` extra. The file is a sequence of such encrypted chunks, so the
full plaintext source-value table is never materialized as a single
serialized blob before encryption (F13 privacy fix, 2026-06-26).

The key derives from the keyed-mask IKM (`mask_key`): `derive(mask_key,
"vault", b"vault-key/v1")` (`src/decoy_engine/vault.py`, `_fernet`). Under
a configured secret, `mask_key` is the 32-byte `SecretKeyProvider` HKDF
mask root; with no secret it falls back to the 8-byte `job_seed`
(byte-identical to pre-DE-02). This is the same HMAC-SHA256 envelope every
other engine derivation uses, with its own label -- and the same `mask_key`
that FPE, hash, and the other keyed strategies draw from (see
[key derivation](key-derivation.md)), so a vault is bound to the run's
masking key, not to the public seed. The derivation mixes
`SEED_PROTOCOL_VERSION`, so a vault written under one protocol version
is undecryptable under another. The key label itself stays at `vault-key/v1`
across the v1 to v2 format bump because only the file layout changed, not
the key derivation.

## File format (decoy-vault/v2)

Magic bytes `DCYVAULT2\n`, then a length-prefixed unencrypted JSON header,
then a sequence of length-prefixed Fernet tokens (one per bounded chunk of
up to 65 536 sorted entries). The 4-byte big-endian length prefix is the
same idiom used in the determinism layer.

Unencrypted header fields:

| Field | Type | Description |
|---|---|---|
| `format` | string | `"decoy-vault/v2"` |
| `seed_protocol_version` | int | `SEED_PROTOCOL_VERSION` at write time |
| `ambiguous_dropped` | int | Entries dropped due to masked-value collisions |
| `chunk_rows` | int | Maximum rows per encrypted chunk |
| `chunk_count` | int | Total number of chunks |

The header is read before any decryption. A `seed_protocol_version`
mismatch raises `VaultError(code="vault_protocol_version_mismatch")`
immediately, with a clear message naming both versions. This is a
diagnosability improvement: a cross-version vault is also undecryptable
(the version byte is mixed into the vault key), but without the guard
the failure would surface as an opaque `vault_key_mismatch`.

## Threat model

- **Vault + mask secret = re-identification.** The run's `mask_key`
  derives the decryption key: the secret behind
  `global_settings.mask_secret_ref` (or a programmatic `key_provider`)
  when one is configured, or the `job_seed` in the no-secret fallback.
  Anyone holding the vault file AND that mask secret (or, in the
  no-secret case, the config's seed) recovers every vaulted source
  value. This extends the key-as-unlock property `decoy unmask`
  introduced for fpe columns: with a vault in play, the mask key now
  unlocks one-way columns too.
- **The vault grows with the data.** Unlike the seed (a constant-size
  secret), the vault contains actual source values. Treat it with the
  handling the source data itself requires.
- **Never ship the vault with the masked output.** The masked output
  is the artifact that is safe to share; the vault is the thing that
  un-shares it.

## Operational rules

1. Vault creation is opt-in twice: a column must declare `vault: true`
   AND the operator must pass `--vault PATH`. A mask run never writes
   a vault otherwise.
2. Store the vault and the config separately, each access-controlled.
3. `vault: true` requires the `cryptography` package (the optional
   `vault` extra), a `namespace`, and is rejected on `strategy: fpe`
   (already reversible from the config alone). The plan compiler
   enforces all three at PLAN/COMPILE time: `vault_requires_cryptography`
   (missing package), `vault_requires_namespace` (missing namespace),
   `vault_strategy_reversible` (fpe strategy). A missing `cryptography`
   package is caught here rather than hours into a run at vault-write
   time; install with `pip install 'decoy-engine[vault]'`.
4. Pooled strategies can map two sources to one masked value; those
   keys are dropped at write time (`ambiguous_dropped` in the unencrypted
   header and the unmask report). Exact round trips are guaranteed only
   for collision-free maskings such as `hash` under a namespace.

## Error codes

`VaultError` is the typed exception raised by `vault.py`. The `code`
attribute is machine-readable.

| Code | When raised |
|---|---|
| `vault_crypto_not_installed` | The `cryptography` package is absent. Install with `pip install 'decoy-engine[vault]'`. |
| `vault_unreadable` | The file is missing, has a bad magic header, or is truncated. |
| `vault_format_unsupported` | The `format` field in the header names a version this engine does not consume. |
| `vault_protocol_version_mismatch` | The header's `seed_protocol_version` differs from the running `SEED_PROTOCOL_VERSION`. Cross-version unmask is not supported; re-mask under the correct engine version. |
| `vault_key_mismatch` | A Fernet chunk could not be decrypted. The vault was written under a different `mask_key` (a different mask secret, or the no-secret `job_seed` when the current run supplies a secret). |

Note: `vault_protocol_version_mismatch` is checked before any decryption
attempt. A cross-version vault also fails decryption (the protocol version
byte is mixed into the derived vault key), but the pre-decrypt check
returns a clear typed error rather than a generic `vault_key_mismatch`.

## What the vault is not

- Not part of the determinism contract: the file embeds a random IV
  and timestamp, so it is not byte-reproducible. Vault CONTENTS are a
  pure function of (config, sources).
- Not a cross-run consistency store: it maps one run's outputs. The
  managed seed/namespace store remains a separate follow-up.
