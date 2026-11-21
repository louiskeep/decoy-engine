# Token vault: handling and threat model

The token vault (`decoy_engine.vault`, `decoy run --vault`,
`decoy unmask --vault`) makes one-way strategies reversible by
recording each vaulted column's source-to-masked map at mask time. It
is the most sensitive artifact the engine can produce, and it changes
what an operator must protect.

## What the vault is

An encrypted file containing `(namespace, masked_value) ->
source_value` triples for every column declared `vault: true`. Each
bounded segment of entries is serialized as a Parquet table. The segments
are one libsodium secretstream (`crypto_secretstream_xchacha20poly1305`,
XChaCha20-Poly1305) through PyNaCl, a base dependency. Decoy writes no
cipher or framing of its own. The stream detects any change, reorder, drop,
duplicate or cut, and the header is bound to every segment as additional
data. No segment holds the full plaintext source-value table, so it is never
one serialized blob before encryption (F13 privacy fix, 2026-06-26).

The key derives from the keyed-mask IKM (`mask_key`): `derive(mask_key,
"vault", b"vault-key/v2" + uint32be(32) + vault_id)` (`src/decoy_engine/vault/_format.py`).
`vault_id` is 32 fresh random bytes drawn on every write, so two vaults under
one secret never share a key. Under
a configured secret, `mask_key` is the 32-byte `SecretKeyProvider` HKDF
mask root. There is no no-secret fallback: every key that writes, opens or
is matched against a vault must be exactly that 32-byte mask root,
checked when a `VaultWriter` is built and again at every write, load and
key-match (`weak_mask_secret` otherwise, before any file is created, changed
or read), and `vault_writer_for_config` raises `keyed_strategy_requires_secret`
when no secret is admitted, for every config. An 8-byte public seed gives a
KDF nothing to amplify. A vault an older engine wrote under the 8-byte
`job_seed` no longer opens under any key; it is regenerated, together with
its masked output, by re-running the mask job under a secret (pre-GA hard
delete; no migration and no recovery path). This is the same HMAC-SHA256 envelope every
other engine derivation uses, with its own label -- and the same `mask_key`
that FPE, hash, and the other keyed strategies draw from (see
[key derivation](key-derivation.md)), so a vault is bound to the run's
masking key, not to the public seed. The derivation mixes
`SEED_PROTOCOL_VERSION`, so a vault written under one protocol version
is undecryptable under another. The key label moved from `vault-key/v1` to
`vault-key/v2` with the v3 format, because the key is now per vault.

## File format (decoy-vault/v3)

Magic bytes `DCYVAULT3\n`, a length-prefixed canonical JSON header (at most
4096 bytes), then one length-prefixed secretstream ciphertext per segment of
up to 65 536 sorted entries. The last segment carries the stream's final tag,
and a vault with no entries still has one (empty) final segment. The 4-byte
big-endian length prefix is the same idiom used in the determinism layer.

Header fields (a closed set; any other key is refused):

| Field | Type | Description |
|---|---|---|
| `format` | string | `"decoy-vault/v3"` |
| `seed_protocol_version` | int | `SEED_PROTOCOL_VERSION` at write time |
| `key_version` | string | The non-secret label of the mask secret that wrote the vault |
| `key_check` | string | 16 hex characters: a key check value bound to `vault_id` |
| `vault_id` | string | 64 hex characters: the vault's random id, input to its key |
| `stream_header` | string | 48 hex characters: the secretstream header |
| `ambiguous_dropped` | int | Entries dropped due to masked-value collisions |
| `entry_count` | int | Total rows across all segments |
| `segment_rows` | int | Maximum rows per segment |

The SHA-256 of the magic and the header frame is the additional data of every
segment. The reader checks, in order: the magic, the header and its field rules,
the key check, and then the stream. A `seed_protocol_version` mismatch raises
`vault_protocol_version_mismatch` before any decryption. A key check mismatch
raises `vault_key_mismatch` before any segment is decrypted, and names the
recorded `key_version`. Once the key is right, any damage raises `vault_tampered`
and returns no rows.

A `decoy-vault/v2` file (Fernet) is still read, read-only: the reader picks v2 or v3 from the
magic bytes, and a v2 vault opens under the same mask secret as before. Nothing writes v2, and a v2
file is never adopted as a current vault. A `decoy-vault/v1` file is refused with
`vault_format_unsupported`.

The key check value reveals nothing about a strong secret, but a weak
passphrase can be tested offline against it, as with the resume store's key
check. The key floor is on length, not entropy. Choose a strong secret.

## Threat model

- **Vault + mask secret = re-identification.** The run's `mask_key`
  derives the decryption key: the secret behind
  `global_settings.mask_secret_ref` (or a programmatic `key_provider`).
  Anyone holding the vault file AND that mask secret recovers every
  vaulted source value. A vault is never keyed on the config's seed. This extends the key-as-unlock property `decoy unmask`
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
3. `vault: true` requires the `cryptography` package, a `namespace`, and
   is rejected on `strategy: fpe` (already reversible from the config
   alone) and on `strategy: passthrough` (never masked, so the vault would
   map a value to itself and the entry build raises on far dates / the
   -2^63 marker). The plan compiler enforces all four at PLAN/COMPILE time:
   `vault_requires_cryptography` (missing package), `vault_requires_namespace`
   (missing namespace), `vault_strategy_reversible` (fpe strategy),
   `vault_strategy_unmasked` (passthrough strategy).
   `cryptography` is a base dependency (Task 5.2), so a normal install
   always has it; the check is defense in depth against a broken or
   partial install, caught here rather than hours into a run at
   vault-write time.
4. Pooled strategies can map two sources to one masked value; those
   keys are dropped at write time (`ambiguous_dropped` in the unencrypted
   header and the unmask report). Exact round trips need `fpe`, or an
   untruncated `hash` or `tokenize` under a namespace, where a collision is
   negligible and not impossible.
5. **Publication (B-6 D1).** `VaultWriter.write` never writes the vault in
   place. It opens the target's parent directory, creates a stage
   `_decoy_vault_stage_<hex>` beside the vault with creation mode `0600`
   (the umask can narrow the `open(2)` mode, for example under `0477`, so the
   stage is `fchmod`ed back to exactly `0600` before the first byte; the
   published vault is a hard link of the stage and has the same mode), writes and `fsync`s it,
   `link(2)`s it to the vault name, `fsync`s the directory (the commit
   point) and attempts to remove the stage name (a failed removal leaves only the
   encrypted `0600` stage; the vault is kept). A crash at any step leaves either no
   vault or a complete one, never a partial one. A directory `fsync` that
   fails with `EINVAL`, `EBADF` or `ENOTSUP` means the filesystem cannot sync a
   directory, and the publish goes on (as PostgreSQL's `fsync_fname_ext`
   does); any other directory `fsync` error is `vault_commit_failed` and the
   vault is kept. Limit: a filesystem without hard links (`link` fails with
   `EPERM`, `ENOTSUP` or `EXDEV`, as on FAT and exFAT) cannot hold a vault,
   because the exclusive publish is a `link(2)`.
6. **Exclusive.** An existing vault is never overwritten: `write` raises
   `vault_exists` and leaves it untouched (two concurrent writers to one
   path: one publishes, the other gets `vault_exists`). Choose a new path,
   or remove the old vault together with the outputs it reverses.
7. **Vault before output, and never deleted.** A caller publishes no output
   until `write` has returned. The engine refuses a vault writer where it
   would publish output itself (`vault_requires_resident_output`: a `sink`,
   or a chunked entry). Once published, no engine failure path removes,
   truncates or replaces a vault; only an unpublished stage is cleaned. A
   vault left by a failed job is kept: it is the recovery key for any
   output that may already exist, and a vault without output is harmless.
8. **Operator rule.** Delete a vault only together with its outputs. A
   `_decoy_vault_stage_*` file beside a vault with no job running may be
   deleted: it is never the vault (a crash after the link leaves it as a
   second name of the published vault, or before it as an unpublished
   stage).

## Error codes

`VaultError` is the typed exception raised by the `vault` package. The `code`
attribute is machine-readable.

| Code | When raised |
|---|---|
| `vault_crypto_not_installed` | The PyNaCl package is absent from a broken or partial install. PyNaCl is a base dependency; a normal `pip install decoy-engine` always has it. |
| `vault_unreadable` | The file is missing, has a bad magic header, or has a header that is cut short, too long, not JSON, or breaks a field rule. |
| `vault_tampered` | The header was understood and the key check passed, but the file was changed or cut short: a segment edited, reordered, dropped, duplicated or cut, no final segment, bytes after the final segment, or a row count that differs from `entry_count`. No rows are returned. |
| `vault_entry_too_large` | One entry is larger than a segment can hold (64 MiB of Parquet). Raised by the writer before any file exists. |
| `vault_exists` | `write` found an existing vault at the target. Nothing was written and the existing vault is unchanged. |
| `vault_commit_failed` | `write` could not write, `fsync` or link the stage, or `fsync` the directory after the link. Before the link no vault exists; after it the vault is kept (it may not survive a power loss). The text names no path and no errno. Publish no output it would reverse. |
| `vault_format_unsupported` | The file has another vault magic (for example `DCYVAULT2\n`) or a `format` field this engine does not consume. Re-run the mask with `--vault` under this version. |
| `vault_protocol_version_mismatch` | The header's `seed_protocol_version` differs from the running `SEED_PROTOCOL_VERSION`. Cross-version unmask is not supported; re-mask under the correct engine version. |
| `vault_key_mismatch` | The key check failed. Either the mask secret differs from the one that wrote the vault, or the `key_check` or `vault_id` header field was edited (restore the stored copy). Raised before any segment is decrypted. |
| `weak_mask_secret` | The key handed to `VaultWriter`, `load_vault` or the run-entry key-match guard is not 32 bytes, as bytes (for example the 8-byte `job_seed`, or a longer key the key derivation cannot take). Raised before any file is created, changed or read. |

Note: `vault_protocol_version_mismatch` is checked before any decryption
attempt. A cross-version vault also fails the key check (the protocol version
byte is mixed into the derived vault key), but the pre-decrypt check
returns a clear typed error rather than a generic `vault_key_mismatch`.

## What the vault is not

- Not part of the determinism contract: the file embeds a random IV
  and timestamp, so it is not byte-reproducible. Vault CONTENTS are a
  pure function of (config, sources).
- Not a cross-run consistency store: it maps one run's outputs. The
  managed seed/namespace store remains a separate follow-up.
