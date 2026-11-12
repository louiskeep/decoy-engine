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
amplify. Store the vault and
the config separately, with the handling the source data itself would
get; never ship a vault alongside its masked output. The encryption is
Fernet from the `cryptography` package (AES-128-CBC + HMAC-SHA256,
encrypt-then-MAC, per the published Fernet spec) -- an audited AEAD
construction rather than anything hand-rolled, per the engine's
established-methodology rule. `cryptography` was promoted to a base
dependency at Task 5.2 (the FF1 cipher needs AES unconditionally, not
just vault); the `[vault]` extra is now a no-op compatibility alias, and
`decoy_engine` itself fails to import without `cryptography` installed.
The function-local imports below predate that promotion and stay as
defense in depth against a future split, not a live degrade path.

Key model. One key per job, domain-separated from every other engine
derivation by a fresh label: `derive(mask_key, "vault", b"vault-key/v1")`
(HKDF-style HMAC-SHA256 expansion, RFC 5869 model; the same envelope FPE
keys use with their own label). `mask_key` is the run's keyed-mask IKM --
the 32-byte `SecretKeyProvider` HKDF root of the configured secret (DE-02)
-- so the vault is bound to the run's masking key, never the public seed. A
vault written by an older engine under the 8-byte `job_seed` no longer opens
and is regenerated, together with its masked output, under a secret (pre-GA
hard delete; no migration). `derive` mixes `SEED_PROTOCOL_VERSION` into the
key, so a vault written under one protocol version is undecryptable under
another. The `decoy-vault/v2` file format stamps that version in its
unencrypted header, so a cross-version load fails with a clear
`vault_protocol_version_mismatch` rather than an opaque key error.

File format (`decoy-vault/v2`). Magic, then a length-prefixed
unencrypted JSON header (`format`, `seed_protocol_version`,
`ambiguous_dropped`, chunk framing), then a sequence of length-prefixed
Fernet tokens, one per bounded chunk of sorted entries. Each chunk is
serialized + encrypted independently (F13), so the full plaintext table
is never held as one serialized blob before encryption.

Determinism boundary. Vault CONTENTS are a pure function of (config,
sources): entries are sorted before serialization. The vault FILE is
not byte-reproducible because Fernet embeds a random IV and a
timestamp; reproducibility contracts apply to masked outputs, never to
this artifact.

Ambiguity. Pooled strategies may map two source values to one masked
value (e.g. `cardinality_mode: reuse` faker with a small pool). A
masked value with conflicting sources cannot be inverted; `write()`
drops those keys and records the count in the unencrypted header
(`ambiguous_dropped`), and the unmask report surfaces it. Exact round
trips are guaranteed only for collision-free maskings (hash under a
namespace, unique-mode substitution).
"""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from decoy_engine import fileaccess, reasons
from decoy_engine._opaque import OpaqueBox
from decoy_engine.determinism import SEED_PROTOCOL_VERSION, derive
from decoy_engine.errors import DecoyError
from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessUnavailable,
    FileHandle,
    FileRequest,
    FileRole,
    OutputDir,
)
from decoy_engine.fileaccess.adapters import serial
from decoy_engine.keyprovider import (
    KEY_REQUIRED_GUIDANCE,
    KeyedStrategyRequiresSecret,
)

if TYPE_CHECKING:
    from decoy_engine.keyprovider import KeyProvider

VAULT_FORMAT_VERSION = "decoy-vault/v2"
VAULT_NAMESPACE = "vault"
# Key model is unchanged across the v1 -> v2 format bump, so the derivation
# label stays at v1: only the file LAYOUT and the protocol-version stamping
# changed, not how the key is derived. Bumping the label would re-key every
# vault for no security reason.
VAULT_KEY_LABEL: bytes = b"vault-key/v1"

# decoy-vault/v2 framing: magic, then length-prefixed unencrypted header JSON,
# then a sequence of length-prefixed Fernet tokens (one per chunk). The 4-byte
# big-endian length prefix is the same idiom the determinism layer uses
# (determinism/_derive.py, generators/derivation.py).
_MAGIC = b"DCYVAULT2\n"
_CHUNK_ROWS = 65536
_INSTALL_HINT = "pip install 'decoy-engine[vault]'"


def _frame(block: bytes) -> bytes:
    """Length-prefix a block: 4-byte big-endian length || block."""
    return len(block).to_bytes(4, "big") + block


def _read_frame(blob: bytes, offset: int) -> tuple[bytes, int]:
    """Read one length-prefixed block at `offset`; return (block, next_offset).

    Raises VaultError(vault_unreadable) on truncation so a corrupt/short file
    fails cleanly instead of slicing past the end.
    """
    if offset + 4 > len(blob):
        raise VaultError(
            code=reasons.runtime.VAULT_UNREADABLE, message="vault file truncated (length prefix)"
        )
    length = int.from_bytes(blob[offset : offset + 4], "big")
    start = offset + 4
    end = start + length
    if end > len(blob):
        raise VaultError(
            code=reasons.runtime.VAULT_UNREADABLE, message="vault file truncated (block body)"
        )
    return blob[start:end], end


class VaultError(DecoyError):
    """Vault read/write failure. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# Every provider's mask root is 32 bytes of HKDF output, and `derive` takes no other strong key
# length, so a longer key would pass a minimum and then fail at the first write or load.
_VAULT_KEY_BYTES = 32

_WEAK_KEY_MESSAGE = (
    f"the vault key must be the {_VAULT_KEY_BYTES}-byte mask root of a secret, as bytes; a "
    "vault is never keyed on the job seed. Build the writer with "
    "vault_writer_for_config(config, key_provider=...) or global_settings.mask_secret_ref."
)


def _key_size(key: object) -> int:
    # A `str` or any other non-bytes key counts as size 0, so it fails the floor like a short key.
    return len(key) if isinstance(key, (bytes, bytearray)) else 0


def _require_vault_key(size: int) -> None:
    """Refuse with `weak_mask_secret` unless the vault key's size (`_key_size`) is the mask root's.

    Callers pass only the size, so the key never becomes a local of this frame. The refusal text
    is fixed.
    """
    if size != _VAULT_KEY_BYTES:
        raise VaultError(code=reasons.runtime.WEAK_MASK_SECRET, message=_WEAK_KEY_MESSAGE)


def _fernet(mask_key: OpaqueBox, size: int) -> Any:
    # The Fernet key derives from the run's mask root. The floor is checked after the crypto
    # import and before the derive, so every write and load refuses a short key before any file
    # is touched. The IKM arrives boxed with its size beside it, so the key is opened once, in
    # the derive call, and the derived key is never a local (`_opaque`).
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise VaultError(
            code=reasons.runtime.VAULT_CRYPTO_NOT_INSTALLED,
            message=(
                f"the vault needs the `cryptography` package; install the "
                f"vault extra: {_INSTALL_HINT}"
            ),
        ) from exc
    _require_vault_key(size)
    return Fernet(
        base64.urlsafe_b64encode(derive(mask_key.open()[0], VAULT_NAMESPACE, VAULT_KEY_LABEL))
    )


def iter_vault_columns(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """List `(table, column, namespace)` for every `vault: true` mask column.

    Pure config walk; the compile check `check_vault_columns` has
    already guaranteed each vaulted column carries a namespace and a
    one-way strategy.
    """
    out: list[tuple[str, str, str]] = []
    for table_cfg in config.get("tables") or []:
        if not isinstance(table_cfg, dict):
            continue
        table = table_cfg.get("name")
        if not table:
            continue
        for col_cfg in table_cfg.get("columns") or []:
            if not isinstance(col_cfg, dict) or not col_cfg.get("vault"):
                continue
            name = col_cfg.get("name")
            namespace = col_cfg.get("namespace")
            if name and namespace:
                out.append((str(table), str(name), str(namespace)))
    return out


def collect_vault_entries(
    config: dict[str, Any],
    sources: Mapping[str, pa.Table],
    outputs: Mapping[str, pa.Table],
) -> list[tuple[str, str, str]]:
    """Pair source and masked values for every vaulted column.

    Returns `(namespace, masked, source)` string triples. Masking
    preserves row count and order, so pairing is positional: source row
    i maps to output row i. Rows where either side is null are skipped
    (null is preserved by every strategy; there is nothing to recover).
    """
    entries: list[tuple[str, str, str]] = []
    for table, column, namespace in iter_vault_columns(config):
        src_tbl = sources.get(table)
        out_tbl = outputs.get(table)
        if src_tbl is None or out_tbl is None:
            continue
        if column not in src_tbl.schema.names or column not in out_tbl.schema.names:
            continue
        src_values = src_tbl.column(column).to_pylist()
        out_values = out_tbl.column(column).to_pylist()
        for src, masked in zip(src_values, out_values, strict=True):
            if src is None or masked is None:
                continue
            entries.append((namespace, str(masked), str(src)))
    return entries


class VaultWriter:
    """Accumulate vault entries across chunks, encrypt per chunk at `write()`.

    Entries live in memory deduplicated until `write()`, so the in-memory
    footprint is bounded by the number of DISTINCT (namespace, masked,
    source) triples, not by row count. At `write()` the sorted entries are
    serialized + Fernet-encrypted one bounded chunk at a time (F13), so the
    full plaintext table is never materialized as a single serialized blob
    before encryption.
    """

    def __init__(self, mask_key: bytes, *, file_access: FileAccess | None = None) -> None:
        # Stored as a `bytes` copy so a caller-held buffer cannot change the key after
        # admission. The policy lookup below can raise; this frame must not hold the key then.
        try:
            _require_vault_key(_key_size(mask_key))
            self._mask_key = bytes(mask_key)
            self._entries: set[tuple[str, str, str]] = set()
            # The writer writes after the entry point that built it returned, so it keeps the
            # policy it was built under and never looks one up later.
            self._access = fileaccess.capture(file_access)
        except BaseException:
            del mask_key
            raise

    def assert_keyed_with(self, mask_key: bytes) -> None:
        """Fail closed if this writer's key does not match `mask_key`.

        DE-02 (Codex BLOCKER 5): the vault holds reversible plaintext PII, so it
        MUST be Fernet-encrypted under the SAME keyed-mask secret the masking run
        used -- never the public `job_seed` while the output masks under a real
        secret. `run_pipeline` calls this against the run's resolved `mask_key`
        before any masking. Constant-time compare; never echoes the key bytes.

        Both keys are held to the floor first: a non-keyed run's key is the 8-byte
        `job_seed`, so a writer whose key drifted to it would otherwise match, and a
        strong writer checked against it is refused as `weak_mask_secret`, the same
        code a weak writer gets, not as a key mismatch.
        """
        try:
            _require_vault_key(_key_size(self._mask_key))
            _require_vault_key(_key_size(mask_key))
            if not hmac.compare_digest(self._mask_key, mask_key):
                raise VaultError(
                    code=reasons.runtime.VAULT_KEY_MISMATCH,
                    message=(
                        "the supplied vault writer is keyed with a different secret than "
                        "the masking run resolved. Build the vault writer from the same "
                        "key_provider / mask_secret_ref (vault_writer_for_config(config, "
                        "key_provider=...)) so the vault decrypts under the run secret."
                    ),
                )
        except BaseException:
            del mask_key  # a traceback keeps this frame: the key must not be among its locals
            raise

    def add(self, entries: Iterable[tuple[str, str, str]]) -> None:
        """Accumulate `(namespace, masked, source)` triples."""
        self._entries.update(entries)

    def write(self, path: str | Path | OutputDir) -> int:
        """Encrypt and write the vault file. Returns the entry count written.

        `path` is an `OutputDir` the policy issued (the file is created exclusively,
        refusing an existing leaf), or, under the ambient CLI policy only, a path that is
        overwritten as it always was. A path under any other policy raises
        `FileAccessUnavailable`.

        Conflicting sources for one `(namespace, masked)` key are
        dropped (see the module docstring's ambiguity policy); the
        dropped-key count rides in the unencrypted header.

        F13 (2026-06-26): the plaintext source values are serialized and
        encrypted one bounded chunk at a time, never as a single full-table
        plaintext Parquet blob. The distinct-triple dedup set is unchanged
        (it is the correct memory bound); what this removes is the second
        full plaintext copy (the whole-table Parquet bytes) and the window
        where it sat unencrypted in heap before `fernet.encrypt`.

        Raises:
            VaultError: ``code='vault_crypto_not_installed'`` when the
                `cryptography` package is missing; ``code='weak_mask_secret'``
                when the writer's key is shorter than the mask root (no file is
                created or changed).
        """
        by_key: dict[tuple[str, str], str | None] = {}
        for namespace, masked, source in self._entries:
            key = (namespace, masked)
            if key in by_key and by_key[key] != source:
                by_key[key] = None  # conflicting sources: not invertible
            else:
                by_key.setdefault(key, source)
        ambiguous = sum(1 for v in by_key.values() if v is None)
        rows = sorted(
            (ns, masked, source) for (ns, masked), source in by_key.items() if source is not None
        )
        import pyarrow.parquet as pq

        # Build the Fernet (and surface the crypto-absent error) before any
        # chunk work, so the failure point matches the pre-streaming writer.
        fernet = self._fernet()
        # chunk_rows/chunk_count are forensic/debug metadata only. load_vault
        # does NOT trust them: it self-terminates on the framed token stream
        # (while offset < len(blob)), so a tampered count cannot make the
        # reader over- or under-read.
        chunk_count = (len(rows) + _CHUNK_ROWS - 1) // _CHUNK_ROWS
        header = {
            "format": VAULT_FORMAT_VERSION,
            "seed_protocol_version": SEED_PROTOCOL_VERSION,
            "ambiguous_dropped": ambiguous,
            "chunk_rows": _CHUNK_ROWS,
            "chunk_count": chunk_count,
        }
        parts: list[bytes] = [_MAGIC, _frame(json.dumps(header, sort_keys=True).encode("utf-8"))]
        for start in range(0, len(rows), _CHUNK_ROWS):
            window = rows[start : start + _CHUNK_ROWS]
            chunk_table = pa.table(
                {
                    "namespace": pa.array([r[0] for r in window], type=pa.string()),
                    "masked": pa.array([r[1] for r in window], type=pa.string()),
                    "source": pa.array([r[2] for r in window], type=pa.string()),
                }
            )
            buf = pa.BufferOutputStream()
            pq.write_table(chunk_table, buf)
            # Encrypt this chunk and drop its plaintext before the next one.
            parts.append(_frame(fernet.encrypt(buf.getvalue().to_pybytes())))
        out: OutputDir
        if isinstance(path, (str, Path)):
            if not fileaccess.is_ambient(self._access):
                raise FileAccessUnavailable(
                    "a vault path string is accepted only under the ambient policy; "
                    "pass an OutputDir issued by the policy"
                )
            out = self._access.output_dir(FileRequest(FileRole.VAULT, "write", str(path), "vault"))
            mode: Literal["exclusive", "replace"] = "replace"
            label: Literal["vault", "vault_direct"] = "vault_direct"
        else:
            out = path
            mode = "exclusive"
            label = "vault"
        with out.begin("file", mode, label=label) as txn:
            serial.write_bytes(txn, "vault", b"".join(parts))
            txn.publish()
        return len(rows)

    def _fernet(self) -> Any:
        return _fernet(OpaqueBox(self._mask_key), _key_size(self._mask_key))


def assert_vault_writer_keyed(vault_writer: object, mask_key: bytes) -> None:
    """Fail closed unless `vault_writer` is a VaultWriter keyed with `mask_key`.

    DE-02 (Codex item 6a + 6, dennis L3): the single shared guard every public
    entry point that accepts a vault writer (`run_pipeline`,
    `run_mask_pipeline_chunked`, ...) MUST call before any vault entry is
    collected, so the vault (reversible plaintext PII) can never be encrypted
    under a key that does not match the masking run's resolved secret. A
    duck-typed writer without the key-match contract is rejected outright.
    """
    key = OpaqueBox(mask_key)
    del mask_key  # a traceback keeps this frame: the key must not be among its locals
    if not isinstance(vault_writer, VaultWriter):
        raise VaultError(
            code=reasons.runtime.VAULT_WRITER_UNSUPPORTED,
            message=(
                "vault_writer must be a decoy_engine.vault.VaultWriter so its "
                "keyed-mask key can be verified against the run secret "
                "(DE-02 fail-closed vault-key guard)."
            ),
        )
    vault_writer.assert_keyed_with(key.open()[0])


@fileaccess.entry_point
def vault_writer_for_config(
    config: dict[str, Any],
    *,
    key_provider: KeyProvider | None = None,
    file_access: FileAccess | None = None,
) -> VaultWriter:
    """Build a `VaultWriter` keyed by the run's mask key.

    Mirrors the mask path's key resolution (DE-02) so the vault always opens
    under the same key the masked output was produced with: a programmatic
    `key_provider` wins, else `global_settings.mask_secret_ref` (any registered scheme).
    With neither, it raises `KeyedStrategyRequiresSecret` for every config, keyed or not:
    a vault is never keyed on `job_seed`. The seed is still validated with the plan
    compiler's rules, so a bad `global_settings.seed` fails the same way it does on a run.
    """
    from decoy_engine.plan._seed import _normalize_job_seed
    from decoy_engine.secret_providers import AdmissionScope, admit_mask_key

    _normalize_job_seed(config)
    # F13: the secret is admitted through the shared preflight. A-4: the writer carries the
    # caller's policy. No job_seed fallback exists for a vault in any release phase.
    with AdmissionScope() as scope:
        provider = scope.adopt(admit_mask_key(config, key_provider=key_provider)).key_provider
        if provider is None:
            raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
        key = OpaqueBox(provider.mask_key())
    return VaultWriter(key.open()[0], file_access=file_access)


@fileaccess.entry_point
def load_vault(
    path: str | Path | None,
    mask_key: bytes,
    *,
    vault: FileHandle | None = None,
    file_access: FileAccess | None = None,
) -> tuple[dict[tuple[str, str], str], int]:
    """Decrypt a vault file into `{(namespace, masked): source}`.

    DE-02: `mask_key` is the keyed-mask IKM the vault was written under (the
    32-byte KeyProvider root). A shorter key, such as the 8-byte `job_seed`
    an older engine keyed vaults on, is refused before the file is read.

    `vault` is a handle the policy issued for the vault file and takes the place of `path`;
    a path string is accepted only under the ambient CLI policy and raises
    `FileAccessUnavailable` under any other.

    Returns the map plus the recorded `ambiguous_dropped` count.

    Raises:
        VaultError: ``code='vault_crypto_not_installed'`` when the
            `cryptography` package is missing;
            ``code='weak_mask_secret'`` when `mask_key` is not the 32-byte
            mask root, as bytes (checked before the file is read);
            ``code='vault_unreadable'`` when the file is missing or not
            a vault; ``code='vault_format_unsupported'`` on a format
            version this engine does not consume;
            ``code='vault_protocol_version_mismatch'`` when the vault was
            written under a different `SEED_PROTOCOL_VERSION` (cross-version
            unmask is not supported);
            ``code='vault_key_mismatch'`` when `mask_key` does not
            decrypt the file (wrong config/secret for this vault).
    """
    # Build the Fernet first so a missing cryptography extra fails with
    # vault_crypto_not_installed even when the file is absent (preserves the
    # pre-streaming ordering the absent-dep contract test pins). Building the
    # object only derives the key; it does not decrypt. The key's size is read inside the
    # boxing statement, because the key must be boxed and dropped before anything else runs.
    key = OpaqueBox(mask_key, size := _key_size(mask_key))
    del mask_key  # a traceback keeps this frame: the key must not be among its locals
    fernet = _fernet(key, size)
    handle = vault
    if handle is None:
        if path is None or not fileaccess.is_ambient():
            raise FileAccessUnavailable(
                "load_vault needs a vault handle from the policy; a path string is accepted "
                "only under the ambient policy"
            )
        handle = fileaccess.current().open_read(
            FileRequest(FileRole.VAULT, "read", str(path), "vault")
        )
    shown = handle.display
    try:
        blob = handle.read_all()
    except OSError as exc:
        if fileaccess.keeps_class(exc):
            raise
        raise VaultError(
            code=reasons.runtime.VAULT_UNREADABLE,
            message=f"vault file {shown!r} could not be read: {exc}",
        ) from exc
    finally:
        if vault is None:
            handle.close()
    if not blob.startswith(_MAGIC):
        raise VaultError(
            code=reasons.runtime.VAULT_UNREADABLE,
            message=f"{shown!r} is not a decoy vault file (bad magic header).",
        )

    # Parse + validate the UNENCRYPTED header before any decrypt, so a
    # format/protocol-version mismatch yields a clear typed error instead of
    # an opaque InvalidToken (the version byte is mixed into the vault key, so
    # a cross-version vault is also undecryptable -- this makes the error
    # diagnosable, it does not restore cross-version compatibility).
    header_bytes, offset = _read_frame(blob, len(_MAGIC))
    try:
        header = json.loads(header_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VaultError(
            code=reasons.runtime.VAULT_UNREADABLE,
            message=f"vault {shown!r} has an unreadable header",
        ) from exc
    fmt = header.get("format")
    if fmt != VAULT_FORMAT_VERSION:
        raise VaultError(
            code=reasons.runtime.VAULT_FORMAT_UNSUPPORTED,
            message=(
                f"vault {shown!r} declares format {fmt!r}; this engine "
                f"consumes {VAULT_FORMAT_VERSION!r}."
            ),
        )
    vault_version = header.get("seed_protocol_version")
    if vault_version != SEED_PROTOCOL_VERSION:
        raise VaultError(
            code=reasons.runtime.VAULT_PROTOCOL_VERSION_MISMATCH,
            message=(
                f"vault {shown!r} was written under seed protocol version "
                f"{vault_version!r}; this engine runs version {SEED_PROTOCOL_VERSION!r}. "
                f"Cross-version unmask is not supported; re-mask under this engine "
                f"or use an engine at protocol version {vault_version!r}."
            ),
        )
    ambiguous = int(header.get("ambiguous_dropped", 0))

    import pyarrow.parquet as pq
    from cryptography.fernet import InvalidToken

    mapping: dict[tuple[str, str], str] = {}
    while offset < len(blob):
        token, offset = _read_frame(blob, offset)
        try:
            payload = fernet.decrypt(token)
        except InvalidToken as exc:
            raise VaultError(
                code=reasons.runtime.VAULT_KEY_MISMATCH,
                message=(
                    "the key does not decrypt this vault; open it with the mask secret "
                    "of the run that wrote it."
                ),
            ) from exc
        chunk = pq.read_table(pa.BufferReader(payload))
        namespaces = chunk.column("namespace").to_pylist()
        masked = chunk.column("masked").to_pylist()
        sources = chunk.column("source").to_pylist()
        mapping.update({(ns, m): s for ns, m, s in zip(namespaces, masked, sources, strict=True)})
    return mapping, ambiguous
