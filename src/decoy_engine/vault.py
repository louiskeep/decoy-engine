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
holding the vault file AND the pipeline config (whose seed derives the
vault key) can recover every vaulted source value. Store the vault and
the config separately, with the handling the source data itself would
get; never ship a vault alongside its masked output. The encryption is
Fernet from the `cryptography` package (AES-128-CBC + HMAC-SHA256,
encrypt-then-MAC, per the published Fernet spec) -- an audited AEAD
construction rather than anything hand-rolled, per the engine's
established-methodology rule. `cryptography` ships in the optional
`vault` extra (`pip install 'decoy-engine[vault]'`); imports are
function-local so the default install never pays for it.

Key model. One key per job, domain-separated from every other engine
derivation by a fresh label: `derive(job_seed, "vault",
b"vault-key/v1")` (HKDF-style HMAC-SHA256 expansion, RFC 5869 model;
the same envelope FPE keys use with their own label). `derive` mixes
`SEED_PROTOCOL_VERSION` into the key, so a vault written under one
protocol version is undecryptable under another. The `decoy-vault/v2`
file format stamps that version in its unencrypted header, so a
cross-version load fails with a clear `vault_protocol_version_mismatch`
rather than an opaque key error.

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
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.determinism import SEED_PROTOCOL_VERSION, derive

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
        raise VaultError(code="vault_unreadable", message="vault file truncated (length prefix)")
    length = int.from_bytes(blob[offset : offset + 4], "big")
    start = offset + 4
    end = start + length
    if end > len(blob):
        raise VaultError(code="vault_unreadable", message="vault file truncated (block body)")
    return blob[start:end], end


class VaultError(Exception):
    """Vault read/write failure. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _fernet(mask_key: bytes) -> Any:
    # DE-02: the Fernet key derives from the keyed-mask IKM -- the 8-byte job_seed
    # when no secret is present (byte-identical to pre-DE-02) or a 32-byte
    # KeyProvider mask root under a secret. A vault written under job_seed cannot
    # be opened under a secret key (pre-GA = hard-delete/regenerate; no dual-read).
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise VaultError(
            code="vault_crypto_not_installed",
            message=(
                f"the vault needs the `cryptography` package; install the "
                f"vault extra: {_INSTALL_HINT}"
            ),
        ) from exc
    key = derive(mask_key, VAULT_NAMESPACE, VAULT_KEY_LABEL)
    return Fernet(base64.urlsafe_b64encode(key))


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

    def __init__(self, mask_key: bytes) -> None:
        # DE-02: the vault's keyed-mask IKM (job_seed when no secret is present).
        self._mask_key = mask_key
        self._entries: set[tuple[str, str, str]] = set()

    def assert_keyed_with(self, mask_key: bytes) -> None:
        """Fail closed if this writer's key does not match `mask_key`.

        DE-02 (Codex BLOCKER 5): the vault holds reversible plaintext PII, so it
        MUST be Fernet-encrypted under the SAME keyed-mask secret the masking run
        used -- never the public `job_seed` while the output masks under a real
        secret. `run_pipeline` calls this against the run's resolved `mask_key`
        before any masking. Constant-time compare; never echoes the key bytes.
        """
        if not hmac.compare_digest(self._mask_key, mask_key):
            raise VaultError(
                code="vault_key_mismatch",
                message=(
                    "the supplied vault writer is keyed with a different secret than "
                    "the masking run resolved. Build the vault writer from the same "
                    "key_provider / mask_secret_ref (vault_writer_for_config(config, "
                    "key_provider=...)) so the vault decrypts under the run secret."
                ),
            )

    def add(self, entries: Iterable[tuple[str, str, str]]) -> None:
        """Accumulate `(namespace, masked, source)` triples."""
        self._entries.update(entries)

    def write(self, path: str | Path) -> int:
        """Encrypt and write the vault file. Returns the entry count written.

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
                `cryptography` package is missing.
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
        Path(path).write_bytes(b"".join(parts))
        return len(rows)

    def _fernet(self) -> Any:
        return _fernet(self._mask_key)


def vault_writer_for_config(
    config: dict[str, Any], *, key_provider: KeyProvider | None = None
) -> VaultWriter:
    """Build a `VaultWriter` keyed by the run's mask key.

    Mirrors the mask path's key resolution (DE-02) so the vault always opens
    under the same key the masked output was produced with: a programmatic
    `key_provider` wins, else `global_settings.mask_secret_ref` (env:/file:), else
    the normalized `job_seed` (byte-identical to pre-DE-02). Normalization matches
    the plan compiler exactly (the same `global_settings.seed` rules).
    """
    from decoy_engine.keyprovider import key_provider_from_ref
    from decoy_engine.plan._seed import _normalize_job_seed

    job_seed = _normalize_job_seed(config)
    provider: KeyProvider | None = key_provider
    if provider is None:
        ref = (config.get("global_settings") or {}).get("mask_secret_ref")
        if ref:
            provider = key_provider_from_ref(ref)
    mask_key = provider.mask_key() if provider is not None else job_seed
    return VaultWriter(mask_key)


def load_vault(path: str | Path, mask_key: bytes) -> tuple[dict[tuple[str, str], str], int]:
    """Decrypt a vault file into `{(namespace, masked): source}`.

    DE-02: `mask_key` is the keyed-mask IKM the vault was written under (the
    8-byte `job_seed` when no secret, or a 32-byte KeyProvider root).

    Returns the map plus the recorded `ambiguous_dropped` count.

    Raises:
        VaultError: ``code='vault_crypto_not_installed'`` when the
            `cryptography` package is missing;
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
    # object only derives the key; it does not decrypt.
    fernet = _fernet(mask_key)
    try:
        blob = Path(path).read_bytes()
    except OSError as exc:
        raise VaultError(
            code="vault_unreadable",
            message=f"vault file {str(path)!r} could not be read: {exc}",
        ) from exc
    if not blob.startswith(_MAGIC):
        raise VaultError(
            code="vault_unreadable",
            message=f"{str(path)!r} is not a decoy vault file (bad magic header).",
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
            code="vault_unreadable", message=f"vault {str(path)!r} has an unreadable header"
        ) from exc
    fmt = header.get("format")
    if fmt != VAULT_FORMAT_VERSION:
        raise VaultError(
            code="vault_format_unsupported",
            message=(
                f"vault {str(path)!r} declares format {fmt!r}; this engine "
                f"consumes {VAULT_FORMAT_VERSION!r}."
            ),
        )
    vault_version = header.get("seed_protocol_version")
    if vault_version != SEED_PROTOCOL_VERSION:
        raise VaultError(
            code="vault_protocol_version_mismatch",
            message=(
                f"vault {str(path)!r} was written under seed protocol version "
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
                code="vault_key_mismatch",
                message=(
                    "the config's seed does not decrypt this vault; pass the SAME "
                    "pipeline config the mask run that wrote the vault used."
                ),
            ) from exc
        chunk = pq.read_table(pa.BufferReader(payload))
        namespaces = chunk.column("namespace").to_pylist()
        masked = chunk.column("masked").to_pylist()
        sources = chunk.column("source").to_pylist()
        mapping.update({(ns, m): s for ns, m, s in zip(namespaces, masked, sources, strict=True)})
    return mapping, ambiguous
