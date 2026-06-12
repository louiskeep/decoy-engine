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
the same envelope FPE keys use with their own label). A new label in a
new namespace adds a derivation domain without touching existing ones,
so `SEED_PROTOCOL_VERSION` is unchanged.

Determinism boundary. Vault CONTENTS are a pure function of (config,
sources): entries are sorted before serialization. The vault FILE is
not byte-reproducible because Fernet embeds a random IV and a
timestamp; reproducibility contracts apply to masked outputs, never to
this artifact.

Ambiguity. Pooled strategies may map two source values to one masked
value (e.g. `cardinality_mode: reuse` faker with a small pool). A
masked value with conflicting sources cannot be inverted; `write()`
drops those keys and records the count in the payload metadata
(`ambiguous_dropped`), and the unmask report surfaces it. Exact round
trips are guaranteed only for collision-free maskings (hash under a
namespace, unique-mode substitution).
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.determinism import derive

VAULT_FORMAT_VERSION = "decoy-vault/v1"
VAULT_NAMESPACE = "vault"
VAULT_KEY_LABEL: bytes = b"vault-key/v1"

_MAGIC = b"DCYVAULT1\n"
_INSTALL_HINT = "pip install 'decoy-engine[vault]'"


class VaultError(Exception):
    """Vault read/write failure. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _fernet(job_seed: bytes) -> Any:
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
    key = derive(job_seed, VAULT_NAMESPACE, VAULT_KEY_LABEL)
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
    """Accumulate vault entries across chunks, encrypt once at `write()`.

    Build-then-encrypt: entries live in memory (deduplicated) until
    `write()`, so the vault's memory footprint is bounded by the number
    of DISTINCT (namespace, masked, source) triples, not by row count.
    A streaming-encrypted vault for cardinalities that exceed memory is
    a recorded follow-up.
    """

    def __init__(self, job_seed: bytes) -> None:
        self._job_seed = job_seed
        self._entries: set[tuple[str, str, str]] = set()

    def add(self, entries: Iterable[tuple[str, str, str]]) -> None:
        """Accumulate `(namespace, masked, source)` triples."""
        self._entries.update(entries)

    def write(self, path: str | Path) -> int:
        """Encrypt and write the vault file. Returns the entry count written.

        Conflicting sources for one `(namespace, masked)` key are
        dropped (see the module docstring's ambiguity policy); the
        dropped-key count rides in the payload metadata.

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
        payload_table = pa.table(
            {
                "namespace": pa.array([r[0] for r in rows], type=pa.string()),
                "masked": pa.array([r[1] for r in rows], type=pa.string()),
                "source": pa.array([r[2] for r in rows], type=pa.string()),
            }
        ).replace_schema_metadata(
            {
                "format": VAULT_FORMAT_VERSION,
                "ambiguous_dropped": str(ambiguous),
            }
        )
        import pyarrow.parquet as pq

        fernet = self._fernet()
        buf = pa.BufferOutputStream()
        pq.write_table(payload_table, buf)
        token = fernet.encrypt(buf.getvalue().to_pybytes())
        Path(path).write_bytes(_MAGIC + token)
        return len(rows)

    def _fernet(self) -> Any:
        return _fernet(self._job_seed)


def vault_writer_for_config(config: dict[str, Any]) -> VaultWriter:
    """Build a `VaultWriter` keyed by the config's normalized job seed.

    Normalization matches the plan compiler exactly (the same
    `global_settings.seed` rules), so the vault key always matches the
    seed envelope the mask run used.
    """
    from decoy_engine.plan._compile import _normalize_job_seed

    return VaultWriter(_normalize_job_seed(config))


def load_vault(path: str | Path, job_seed: bytes) -> tuple[dict[tuple[str, str], str], int]:
    """Decrypt a vault file into `{(namespace, masked): source}`.

    Returns the map plus the recorded `ambiguous_dropped` count.

    Raises:
        VaultError: ``code='vault_crypto_not_installed'`` when the
            `cryptography` package is missing;
            ``code='vault_unreadable'`` when the file is missing or not
            a vault; ``code='vault_format_unsupported'`` on a format
            version this engine does not consume;
            ``code='vault_key_mismatch'`` when `job_seed` does not
            decrypt the file (wrong config for this vault).
    """
    fernet = _fernet(job_seed)
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
    from cryptography.fernet import InvalidToken

    try:
        payload = fernet.decrypt(blob[len(_MAGIC) :])
    except InvalidToken as exc:
        raise VaultError(
            code="vault_key_mismatch",
            message=(
                "the config's seed does not decrypt this vault; pass the SAME "
                "pipeline config the mask run that wrote the vault used."
            ),
        ) from exc
    import pyarrow.parquet as pq

    table = pq.read_table(pa.BufferReader(payload))
    metadata = table.schema.metadata or {}
    fmt = (metadata.get(b"format") or b"").decode("utf-8")
    if fmt != VAULT_FORMAT_VERSION:
        raise VaultError(
            code="vault_format_unsupported",
            message=(
                f"vault {str(path)!r} declares format {fmt!r}; this engine "
                f"consumes {VAULT_FORMAT_VERSION!r}."
            ),
        )
    ambiguous = int((metadata.get(b"ambiguous_dropped") or b"0").decode("utf-8"))
    namespaces = table.column("namespace").to_pylist()
    masked = table.column("masked").to_pylist()
    sources = table.column("source").to_pylist()
    mapping = {(ns, m): s for ns, m, s in zip(namespaces, masked, sources, strict=True)}
    return mapping, ambiguous
