"""Token vault: an encrypted source-to-masked map for one-way strategies.

`decoy_engine.unmask` reverses fpe columns algebraically (a keyed
bijection inverts); hash, faker, redact and the rest destroy or replace
information, so their unmask status is `irreversible`. The vault makes
a column reversible by RECORDING the mapping at mask time instead of
relying on mathematical invertibility -- the model commercial masking
tools call reversible tokenization (a token vault). Opt-in twice: the
column declares `vault: true` AND the operator passes a vault path; a
mask run never writes a vault otherwise.

Security model. The vault is a re-identification database: anyone
holding the vault file AND the run's mask key (the secret behind
`mask_secret_ref` / a programmatic `key_provider`) can recover every
vaulted source value. A vault is never keyed on the public job seed:
every key that writes, opens or is matched against a vault must be the
32-byte mask root, checked at construction and again at every write,
load and key-match, because an 8-byte public seed gives a KDF nothing to
amplify. Store the vault and the config separately, with the handling the
source data itself would get; never ship a vault alongside its masked output. The encryption is
libsodium's secretstream (XChaCha20-Poly1305) through PyNaCl, a base dependency: an audited
construction, with no cipher or framing of ours (`_format.py` cites the source).

Key model. One key per vault: `derive(mask_key, "vault", b"vault-key/v2" + len(vault_id) +
vault_id)`, where `vault_id` is 32 fresh random bytes drawn on every `serialize()`. Two vaults
under one secret never share a key. `mask_key` is the run's keyed-mask IKM, the 32-byte
`SecretKeyProvider` HKDF root (DE-02), so the vault is bound to the run's masking key, never the
public seed. `derive` mixes `SEED_PROTOCOL_VERSION` into the key, so a vault written under one
protocol version is undecryptable under another; the header also stamps it, so a cross-version
load fails with `vault_protocol_version_mismatch` rather than an opaque key error.

File format (`decoy-vault/v3`). The magic `DCYVAULT3\n`, a length-prefixed canonical-JSON header
(a closed set of nine fields: `format`, `seed_protocol_version`, `key_version`, `key_check`,
`vault_id`, `stream_header`, `ambiguous_dropped`, `entry_count`, `segment_rows`), then one
length-prefixed secretstream ciphertext per segment of sorted entries, the last with the final tag.
The SHA-256 of the magic and header frame is the additional data of every segment, so editing the
header, or reordering, dropping, duplicating or truncating segments, is refused as
`vault_tampered`. `key_check` is a key check value bound to `vault_id`: a wrong secret is
`vault_key_mismatch` before any segment is decrypted. Each segment is serialized and encrypted
independently (F13), so the full plaintext table is never held as one blob before encryption.
`decoy-vault/v2` (Fernet) stays readable through `_v2_reader.py`, chosen from the magic bytes in
`_reader._open_vault`; nothing writes it. An older format (v1) is refused with
`vault_format_unsupported` and a re-mask remedy.

Determinism boundary. Vault CONTENTS are a pure function of (config, sources): entries are sorted
before serialization. The vault FILE is not byte-reproducible because `vault_id` and the stream
header are random; reproducibility contracts apply to masked outputs, never to this artifact.

Ambiguity. Pooled strategies may map two source values to one masked
value (e.g. `cardinality_mode: reuse` faker with a small pool). A
masked value with conflicting sources cannot be inverted; `write()`
drops those keys and records the count in the authenticated header
(`ambiguous_dropped`), and the unmask report surfaces it. Exact round
trips hold only for keys no other source value masked to; no strategy,
hash included, is collision-free unless it is unique-mode.

Package layout. `_format.py` owns the file layout and the crypto, `_writer.py` the writer and its
commit token, `_reader.py` the read path. This module re-exports the public names so
`from decoy_engine.vault import ...` is unchanged.
"""

from __future__ import annotations

from decoy_engine import reasons
from decoy_engine.internal.vault_entries import collect_vault_entries as collect_vault_entries
from decoy_engine.internal.vault_entries import iter_vault_columns as iter_vault_columns

# isort: off
# The import order is the definition order of the old single module (`_writer` first, because it
# opens with the TYPE_CHECKING block); isort would reorder it.
from decoy_engine.vault._writer import VaultCommit as VaultCommit
from decoy_engine.vault._writer import VaultWriter as VaultWriter
from decoy_engine.vault._writer import assert_vault_writer_keyed as assert_vault_writer_keyed
from decoy_engine.vault._writer import issue_commit as issue_commit
from decoy_engine.vault._writer import vault_writer_for_config as vault_writer_for_config
from decoy_engine.vault._format import VAULT_FORMAT_VERSION as VAULT_FORMAT_VERSION
from decoy_engine.vault._format import VAULT_KEY_LABEL as VAULT_KEY_LABEL
from decoy_engine.vault._format import VAULT_NAMESPACE as VAULT_NAMESPACE
from decoy_engine.vault._format import VaultError as VaultError
from decoy_engine.vault._reader import OpenedVault as OpenedVault
from decoy_engine.vault._reader import VaultHeader as VaultHeader
from decoy_engine.vault._reader import load_vault as load_vault
from decoy_engine.vault._reader import open_vault as open_vault
# isort: on

# The codes a vault read or write can raise, from the registry constants, so a caller (the CLI)
# keeps no copy of the list. `tests/unit/vault/test_v3_format.py` checks it against the source.
VAULT_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        reasons.runtime.VAULT_COMMIT_FAILED,
        reasons.runtime.VAULT_CRYPTO_NOT_INSTALLED,
        reasons.runtime.VAULT_ENTRY_TOO_LARGE,
        reasons.runtime.VAULT_EXISTS,
        reasons.runtime.VAULT_FORMAT_UNSUPPORTED,
        reasons.runtime.VAULT_KEY_MISMATCH,
        reasons.runtime.VAULT_PROTOCOL_VERSION_MISMATCH,
        reasons.runtime.VAULT_TAMPERED,
        reasons.runtime.VAULT_UNREADABLE,
        reasons.runtime.VAULT_WRITER_UNSUPPORTED,
        reasons.runtime.WEAK_MASK_SECRET,
    }
)

# The vault became a package; a module never needed `__all__`, a public package does (census).
__all__ = [
    "VAULT_FORMAT_VERSION",
    "VAULT_KEY_LABEL",
    "VAULT_NAMESPACE",
    "VAULT_REFUSAL_CODES",
    "OpenedVault",
    "VaultCommit",
    "VaultError",
    "VaultHeader",
    "VaultWriter",
    "assert_vault_writer_keyed",
    "collect_vault_entries",
    "issue_commit",
    "iter_vault_columns",
    "load_vault",
    "open_vault",
    "vault_writer_for_config",
]
