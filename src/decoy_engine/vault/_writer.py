"""The vault writer, its commit token and the writer factory (F22 adopt-or-refuse lives upstream).

`VaultWriter.reads_as_own` reads through `load_vault`, which lives in `_reader`. That import sits at
the bottom of the module so the move out of `vault.py` kept the original definition order.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from decoy_engine import fileaccess, reasons
from decoy_engine._canonical import canonical_json_bytes
from decoy_engine._opaque import OpaqueBox
from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessUnavailable,
    FileHandle,
    FileRequest,
    FileRole,
    OutputDir,
)
from decoy_engine.fileaccess.adapters import serial
from decoy_engine.internal.crypto import VAULT_KEY_CHECK_LABEL, key_check_value
from decoy_engine.keyprovider import (
    KEY_REQUIRED_GUIDANCE,
    KeyedStrategyRequiresSecret,
    is_key_version_label,
)
from decoy_engine.secret_providers import (
    MASK_ROOT_V1,
    PreparedSecret,
    SecretBuffer,
    SecretClosedError,
)

if TYPE_CHECKING:
    from decoy_engine.keyprovider import KeyProvider

from decoy_engine.vault import _format
from decoy_engine.vault._format import (
    _HEADER,
    _MAGIC,
    _VAULT_ID_BYTES,
    VAULT_FORMAT_VERSION,
    VaultError,
    _check_key,
    _frame,
    _HeaderRecord,
    _key_size,
    _open_stream,
    _require_vault_key,
    _segment_frames,
)

_ISSUER = object()
# The key version a bare `bytes` key records: the version every pre-v3 writer stamped.
_LEGACY_KEY_VERSION = "v1"


@dataclass(frozen=True)
class VaultCommit:
    """A vault whose bytes and directory chain are durable (F22 5.5); only `issue_commit` makes one."""

    sha256: str
    size: int
    entry_count: int
    adopted: bool
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise FileAccessUnavailable("a vault commit is issued by the vault module")


def issue_commit(blob: bytes, entry_count: int, *, adopted: bool) -> VaultCommit:
    """The one issuer: for bytes just published, or a leaf adopted after its fsyncs."""
    digest = hashlib.sha256(blob).hexdigest()
    return VaultCommit(digest, len(blob), entry_count, adopted, _ISSUER)


def _prepared_from_key(mask_key: bytes | bytearray) -> PreparedSecret:
    """A `PreparedSecret` over a raw mask root, as the key-provider bridge builds one.

    No length check here: the writer's floor refuses a short key as `weak_mask_secret`, the same
    code and text as before v3.
    """
    try:
        return PreparedSecret(
            SecretBuffer.adopt(bytearray(mask_key)),
            key_version=_LEGACY_KEY_VERSION,
            requirement_ids=(MASK_ROOT_V1,),
        )
    except BaseException:
        del mask_key  # a traceback keeps this frame: the key must not be among its locals
        raise


class VaultWriter:
    """Accumulate vault entries across chunks, encrypt per chunk at `write()`.

    Entries live in memory deduplicated until `write()`, so the in-memory
    footprint is bounded by the number of DISTINCT (namespace, masked,
    source) triples, not by row count. At `write()` the sorted entries are
    serialized and encrypted one bounded segment at a time (F13), so the
    full plaintext table is never materialized as a single serialized blob
    before encryption.
    """

    def __init__(
        self, prepared: PreparedSecret | bytes, *, file_access: FileAccess | None = None
    ) -> None:
        # `bytes` is the frozen pre-v3 form, `VaultWriter(mask_key)`. It is prepared the way a
        # `PreparedSecret` is built, then follows the one path below, so both give the same vault.
        lent = None
        if isinstance(prepared, (bytes, bytearray)):
            # Rebinding the name drops this frame's reference to the key bytes.
            prepared = lent = _prepared_from_key(prepared)
        try:
            self._admit(prepared, file_access)
        finally:
            if lent is not None:
                lent.close()

    def _admit(self, prepared: PreparedSecret, file_access: FileAccess | None) -> None:
        # The writer owns a clone, so closing the caller's secret (the admission scope does)
        # does not stop a write that happens later. Every check below runs before the clone.
        if not isinstance(prepared, PreparedSecret):
            raise TypeError("a VaultWriter is built from a PreparedSecret or the mask key bytes")
        if prepared.closed:
            raise SecretClosedError
        if not is_key_version_label(prepared.key_version):
            raise ValueError("the prepared secret's key version is not a valid label")
        _require_vault_key(_key_size(prepared.mask_key()))
        self._prepared = prepared.clone()
        try:
            self._entries: set[tuple[str, str, str]] = set()
            # The writer writes after the entry point that built it returned, so it keeps the
            # policy it was built under and never looks one up later.
            self._access = fileaccess.capture(file_access)
        except BaseException:
            self._prepared.close()
            raise

    def close(self) -> None:
        """Wipe the writer's copy of the secret. Later writes raise `SecretClosedError`."""
        self._prepared.close()

    def __enter__(self) -> VaultWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def assert_keyed_with(self, mask_key: bytes) -> None:
        """Fail closed if this writer's key does not match `mask_key`.

        DE-02 (Codex BLOCKER 5): the vault holds reversible plaintext PII, so it
        MUST be encrypted under the SAME keyed-mask secret the masking run
        used -- never the public `job_seed` while the output masks under a real
        secret. `run_pipeline` calls this against the run's resolved `mask_key`
        before any masking. Constant-time compare; never echoes the key bytes.

        Both keys are held to the floor first: a non-keyed run's key is the 8-byte
        `job_seed`, so a writer whose key drifted to it would otherwise match, and a
        strong writer checked against it is refused as `weak_mask_secret`, the same
        code a weak writer gets, not as a key mismatch.
        """
        try:
            _require_vault_key(_key_size(self._prepared.mask_key()))
            _require_vault_key(_key_size(mask_key))
            if not hmac.compare_digest(self._prepared.mask_key(), mask_key):
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

        `path` is an `OutputDir` the policy issued, or, under the ambient CLI policy only, a
        path string. Publication is private, durable and exclusive (`_publish`): an existing
        vault is never overwritten, and a published one never removed.

        Conflicting sources for one `(namespace, masked)` key are
        dropped (see the module docstring's ambiguity policy); the
        dropped-key count rides in the authenticated header.

        F13 (2026-06-26): the plaintext source values are serialized and encrypted one bounded
        segment at a time, never as a single full-table plaintext Parquet blob.

        Raises:
            VaultError: ``code='vault_crypto_not_installed'`` when PyNaCl is
                missing; ``weak_mask_secret`` when the key is not the mask root's size;
                ``vault_exists`` when the target exists; ``vault_commit_failed`` when the
                write, fsync or link failed, or a chain fsync failed after the link (the leaf is
                kept, so a retry through `write` gets ``vault_exists``; only `publish_unit` adopts).
        """
        blob, entries = self.serialize()
        out: OutputDir
        if isinstance(path, (str, Path)):
            if not fileaccess.is_ambient(self._access):
                raise FileAccessUnavailable(
                    "a vault path string is accepted only under the ambient policy; "
                    "pass an OutputDir issued by the policy"
                )
            out = self._access.output_dir(FileRequest(FileRole.VAULT, "write", str(path), "vault"))
        else:
            out = path
        _publish(out, blob)
        return entries

    def _fold(self) -> tuple[list[tuple[str, str, str]], int]:
        """The sorted invertible rows and the count of keys dropped for conflicting sources."""
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
        return rows, ambiguous

    def serialize(self) -> tuple[bytes, int]:
        """The vault file's bytes and its entry count; nothing is written.

        Every call draws a fresh `vault_id`, so two calls give different keys, stream headers and
        ciphertexts for the same rows; adoption compares rows through `reads_as_own`, not bytes.
        """
        rows, ambiguous = self._fold()
        # The crypto import and the key floor come first: a missing PyNaCl fails before any work.
        _check_key(self._prepared.mask_key)
        vault_id = os.urandom(_VAULT_ID_BYTES)
        state, stream_header = _open_stream(self._prepared.mask_key, vault_id)
        record = _HeaderRecord(
            format=VAULT_FORMAT_VERSION,
            seed_protocol_version=SEED_PROTOCOL_VERSION,
            key_version=self._prepared.key_version,
            key_check=key_check_value(
                self._prepared.mask_key, VAULT_KEY_CHECK_LABEL, vault_id.hex()
            ),
            vault_id=vault_id.hex(),
            stream_header=stream_header.hex(),
            ambiguous_dropped=ambiguous,
            entry_count=len(rows),
            segment_rows=_format._SEGMENT_ROWS,
        )
        header_frame = _frame(canonical_json_bytes(_HEADER.dump(record)))
        # The header is bound to every segment: the digest covers the magic and the frame bytes
        # as written, which are the bytes the reader hashes.
        ad = hashlib.sha256(_MAGIC + header_frame).digest()
        parts = [_MAGIC, header_frame, *_segment_frames(state, rows, ad, record.segment_rows)]
        return b"".join(parts), len(rows)

    def publish(self, out: OutputDir) -> VaultCommit:
        """`write` to a capability; the `VaultCommit` exists only once the leaf and its whole chain
        were fsynced."""
        blob, entries = self.serialize()
        _publish(out, blob)
        return issue_commit(blob, entries, adopted=False)

    def reads_as_own(self, handle: FileHandle) -> bool:
        """Whether `handle` opens under this key to exactly this writer's rows and dropped count."""
        try:
            opened = open_vault(None, self._prepared, vault=handle)
        except VaultError:
            return False
        if opened.header.format != VAULT_FORMAT_VERSION:  # a v2 file reads, but is never adopted
            return False
        rows, own_ambiguous = self._fold()
        return opened.ambiguous_dropped == own_ambiguous and opened.mapping == {
            (n, m): v for n, m, v in rows
        }


def _publish(out: OutputDir, data: bytes) -> None:
    """Publish via a 0600 stage: fsync, exclusive `link(2)` (compare-and-set), directory fsync
    (commit point). Fixed failure text, raised after the handlers; only the stage is removed (attempted; a failed removal leaves the encrypted 0600 stage)."""
    failure = ""
    with out.begin("file", "exclusive", label="vault") as txn:
        fd = txn.create_file("vault")  # step 1: its errors keep the policy's shape
        try:
            serial.write_durable(fd, data)
            txn.publish()
            out.sync_chain()  # the vault leaf's whole chain, before anything depends on it
        except FileExistsError:
            failure = "exists"
        except OSError:
            failure = "commit"
    if failure == "exists":
        raise VaultError(
            code=reasons.runtime.VAULT_EXISTS,
            message=reasons.get("runtime", reasons.runtime.VAULT_EXISTS).summary,
        )
    if failure:
        raise VaultError(
            code=reasons.runtime.VAULT_COMMIT_FAILED,
            message=reasons.get("runtime", reasons.runtime.VAULT_COMMIT_FAILED).summary,
        )


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
    from decoy_engine.secret_providers import (
        MASK_ROOT,
        AdmissionScope,
        admit_mask_key,
        prepare_legacy,
    )

    _normalize_job_seed(config)
    # F13: the secret is admitted through the shared preflight. A-4: the writer carries the
    # caller's policy. No job_seed fallback exists for a vault in any release phase. The writer
    # clones the admitted secret, so the scope can close the original on exit.
    with AdmissionScope() as scope:
        admitted = scope.adopt(admit_mask_key(config, key_provider=key_provider))
        if admitted.key_provider is None:
            raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
        if admitted.prepared is not None:
            return VaultWriter(admitted.prepared, file_access=file_access)
        legacy = prepare_legacy(admitted.key_provider, (MASK_ROOT,))
        try:
            return VaultWriter(legacy, file_access=file_access)
        finally:
            legacy.close()


from decoy_engine.vault._reader import open_vault  # noqa: E402
