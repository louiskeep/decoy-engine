"""Local reference adapter for the protected quarantine sink (POSIX, Fernet).

Every bounded chunk of rows is one length-prefixed Fernet token under a quarantine-only key
derived (HKDF-SHA256, new purpose/version) from a real ``KeyProvider.mask_key()``; the key is
never serialized and no plaintext is staged on disk. Directories are 0700, files 0600, created
exclusively with ``O_NOFOLLOW``, and an artifact becomes readable only by an atomic rename after
a digest trailer is written, so a partial file is never a receipt. The clock is injected.

Honest scope: this is application-level ciphertext under the run's mask key. It does not claim
filesystem encryption, quotas or orphan recovery (a managed arena is a later seam), and the OS
owner who also holds the key can read the data. Identity policies are a platform adapter's job.
Pattern sources: Fernet / ``cryptography`` (as ``vault.py`` uses it) and RFC 5869 HKDF.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import struct
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from decoy_engine import reasons
from decoy_engine.declared_casting import _quarantine_protocol as P  # noqa: N812 -- short alias for the contract namespace
from decoy_engine.determinism._hkdf import hkdf_sha256

_MAGIC = b"DQA1\n"
_SALT = b"decoy-engine/protected-quarantine/v1"
_CHUNK_ROWS = 256
_CHUNK_BYTES = 1 << 20
_R = reasons.runtime
_SAFE_ID = "0123456789abcdef"


def _qerror(code: Any, message: str = "", cleanup_state: str = "not_needed") -> Exception:
    from decoy_engine.execution._errors import QuarantineError

    return QuarantineError(code=str(code), message=message, cleanup_state=cleanup_state)


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"$hex": bytes(value).hex()}
    if isinstance(value, (Decimal, date, datetime)):
        return {"$str": str(value)}
    return {"$str": str(value)}


def _revive(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        if "$hex" in value:
            return bytes.fromhex(value["$hex"])
        if "$str" in value:
            return value["$str"]
    return value


@dataclass
class _State:
    partial: Path
    final: Path
    fd: int
    ref: str
    owner_hash: str
    policy: Any
    created_at: float
    expires_at: float
    sha: Any
    rows: int = 0
    plain_bytes: int = 0
    buffer: list[bytes] = field(default_factory=list)
    buffer_bytes: int = 0


def _owner_hash(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


class LocalProtectedQuarantineSink:
    """Fernet-encrypted POSIX store. Construct with a real ``KeyProvider`` and a private root."""

    def __init__(
        self, root: str | os.PathLike[str], *, key_provider: Any, clock: Any = time.time
    ) -> None:
        self._root = Path(root)
        self._key_provider = key_provider
        self._clock = clock
        self._ready = False

    # -- capability (pinned before any source is read) -----------------------------------------

    @property
    def capability(self) -> P.QuarantineCapability:
        from decoy_engine.keyprovider import SeedKeyProvider

        version = str(getattr(self._key_provider, "key_version", ""))
        if isinstance(self._key_provider, SeedKeyProvider) or version == "seed":
            return P.QuarantineCapability(
                P.PROTECTION_ENCRYPTED, version, False, "seed_key_provider"
            )
        try:
            strong = len(self._key_provider.mask_key()) >= 32
        except Exception:
            strong = False
        if not strong:
            return P.QuarantineCapability(P.PROTECTION_ENCRYPTED, version, False, "weak_key")
        try:
            self._prepare_root()
        except OSError:
            return P.QuarantineCapability(
                P.PROTECTION_ENCRYPTED, version, False, "arena_unavailable"
            )
        except ValueError:
            return P.QuarantineCapability(P.PROTECTION_ENCRYPTED, version, False, "arena_unsafe")
        return P.QuarantineCapability(P.PROTECTION_ENCRYPTED, version, True, "")

    # -- keys ------------------------------------------------------------------------------------

    def _fernet(self) -> Any:
        from cryptography.fernet import Fernet

        version = str(self._key_provider.key_version)
        key = hkdf_sha256(
            ikm=self._key_provider.mask_key(),
            salt=_SALT,
            info=b"quarantine-fernet/" + version.encode("utf-8"),
            length=32,
        )
        return Fernet(base64.urlsafe_b64encode(key))

    def _token_key(self) -> bytes:
        return hkdf_sha256(
            ikm=self._key_provider.mask_key(),
            salt=_SALT,
            info=b"quarantine-token/" + str(self._key_provider.key_version).encode("utf-8"),
            length=32,
        )

    # -- filesystem -------------------------------------------------------------------------------

    def _private_dir(self, path: Path, *, create: bool) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)
            st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError("quarantine arena path is not a plain directory")
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise ValueError("quarantine arena is not private to this user")

    def _prepare_root(self) -> None:
        if not self._ready:
            self._private_dir(self._root, create=True)
            self._ready = True

    def _owner_dir(self, owner_hash: str, *, create: bool) -> Path:
        self._prepare_root()
        path = self._root / owner_hash
        self._private_dir(path, create=create)
        return path

    # -- writing ------------------------------------------------------------------------------------

    def begin(self, run: P.QuarantineRun, policy: P.QuarantinePolicy) -> P.QuarantineSession:
        # Failures are raised after the handler exits, so no OS error text (which can carry a
        # path) is ever chained onto the registered error.
        state: _State | None = None
        try:
            state = self._open_artifact(run, policy)
        except (OSError, ValueError):
            state = None
        if state is None:
            raise _qerror(_R.PROTECTED_QUARANTINE_WRITE_FAILED, "quarantine could not be opened.")
        return P.QuarantineSession(session_id=state.ref, state=state)

    def _open_artifact(self, run: P.QuarantineRun, policy: P.QuarantinePolicy) -> _State:
        owner_hash = _owner_hash(run.owner)
        directory = self._owner_dir(owner_hash, create=True)
        run_id = secrets.token_hex(16)
        partial, final = directory / f"{run_id}.partial", directory / f"{run_id}.dqa"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        now = float(self._clock())
        header = {
            "format": "decoy-quarantine/v1",
            "key_version": str(self._key_provider.key_version),
            "owner": owner_hash,
            "created_at": now,
            "expires_at": now + policy.retention_seconds,
            "token_salt": secrets.token_hex(16),
            "protection": P.PROTECTION_ENCRYPTED,
        }
        blob = json.dumps(header, sort_keys=True).encode("utf-8")
        head = _MAGIC + struct.pack(">I", len(blob)) + blob
        os.write(fd, head)
        return _State(
            partial,
            final,
            fd,
            f"{owner_hash}-{run_id}",
            owner_hash,
            policy,
            now,
            header["expires_at"],
            hashlib.sha256(head),
        )

    def write_row(self, session: P.QuarantineSession, envelope: P.QuarantineEnvelope) -> None:
        state: _State = session.state
        line = json.dumps(
            {
                "table": envelope.table,
                "row_position": envelope.row_position,
                "reasons": [str(r) for r in envelope.reasons],
                "row": dict(envelope.row),
            },
            default=_json_default,
            sort_keys=True,
        ).encode("utf-8")
        if (
            state.rows + 1 > state.policy.max_rows
            or state.plain_bytes + len(line) > state.policy.max_bytes
        ):
            self._fail_write(state, "budget")
        state.rows += 1
        state.plain_bytes += len(line)
        state.buffer.append(line)
        state.buffer_bytes += len(line)
        if len(state.buffer) >= _CHUNK_ROWS or state.buffer_bytes >= _CHUNK_BYTES:
            self._flush(state)

    def _fail_write(self, state: _State, why: str) -> None:
        cleanup = self._cleanup(state)
        raise _qerror(
            _R.PROTECTED_QUARANTINE_WRITE_FAILED,
            "quarantine budget exceeded." if why == "budget" else "quarantine write failed.",
            cleanup,
        )

    def _flush(self, state: _State) -> None:
        if not state.buffer:
            return
        payload = b"\n".join(state.buffer)
        state.buffer, state.buffer_bytes = [], 0
        failed = False
        try:
            token = self._fernet().encrypt(payload)
            self._append_chunk(state, struct.pack(">I", len(token)) + token)
        except Exception:
            failed = True
        if failed:
            self._fail_write(state, "io")

    def _append_chunk(self, state: _State, frame: bytes) -> None:
        os.write(state.fd, frame)
        state.sha.update(frame)

    def seal(self, session: P.QuarantineSession) -> P.QuarantineReceipt:
        state: _State = session.state
        self._flush(state)
        digest = state.sha.hexdigest()
        trailer = json.dumps(
            {"rows": state.rows, "bytes": state.plain_bytes, "sha256": digest}, sort_keys=True
        ).encode("utf-8")
        sealed = True
        try:
            os.write(state.fd, struct.pack(">I", 0) + struct.pack(">I", len(trailer)) + trailer)
            os.fsync(state.fd)
            os.close(state.fd)
            state.fd = -1
            self._publish_atomic(state)
        except Exception:
            sealed = False
        if not sealed:
            cleanup = self._cleanup(state)
            raise _qerror(
                _R.PROTECTED_QUARANTINE_SEAL_FAILED, "quarantine could not be sealed.", cleanup
            )
        return P.QuarantineReceipt(
            ref=state.ref,
            rows=state.rows,
            bytes=state.plain_bytes,
            digest=digest,
            created_at=state.created_at,
            expires_at=state.expires_at,
            protection=P.PROTECTION_ENCRYPTED,
            key_version=str(self._key_provider.key_version),
        )

    def _publish_atomic(self, state: _State) -> None:
        os.replace(state.partial, state.final)
        dir_fd = os.open(state.final.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def abort(self, session: P.QuarantineSession) -> str:
        return self._cleanup(session.state)

    def _cleanup(self, state: _State) -> str:
        try:
            if state.fd >= 0:
                os.close(state.fd)
                state.fd = -1
        except OSError:
            pass
        try:
            self._remove_partial(state)
        except OSError:
            return "failed"
        return "confirmed"

    def _remove_partial(self, state: _State) -> None:
        for path in (state.partial, state.final):
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue

    # -- reading ---------------------------------------------------------------------------------

    def _token_mac(self, ref: str, owner_hash: str, salt: str) -> str:
        msg = f"{ref}|{owner_hash}|{salt}".encode()
        return hmac.new(self._token_key(), msg, hashlib.sha256).hexdigest()

    def _locate(self, ref: str) -> tuple[Path, str]:
        owner_hash, _, run_id = ref.partition("-")
        if (
            len(owner_hash) != 32
            or len(run_id) != 32
            or not set(ref.replace("-", "")) <= set(_SAFE_ID)
        ):
            raise _qerror(_R.PROTECTED_QUARANTINE_ACCESS_DENIED, "access denied.")
        return self._root / owner_hash / f"{run_id}.dqa", owner_hash

    def _read_header(self, path: Path) -> tuple[dict[str, Any], bytes]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as fh:
                blob = fh.read()
            if not blob.startswith(_MAGIC):
                raise ValueError("magic")
            (size,) = struct.unpack(">I", blob[5:9])
            header = json.loads(blob[9 : 9 + size])
            return header, blob
        except (OSError, ValueError, struct.error):
            raise _qerror(_R.PROTECTED_QUARANTINE_ACCESS_DENIED, "access denied.") from None

    def get_retrieval_token(
        self, receipt: P.QuarantineReceipt | str, access_context: P.QuarantineAccessContext
    ) -> P.OpaqueRetrievalToken:
        ref = receipt if isinstance(receipt, str) else receipt.ref
        path, owner_hash = self._locate(ref)
        header, _ = self._read_header(path)
        if (
            header.get("owner") != _owner_hash(access_context.owner)
            or header.get("owner") != owner_hash
        ):
            raise _qerror(_R.PROTECTED_QUARANTINE_ACCESS_DENIED, "access denied.")
        if float(self._clock()) >= float(header["expires_at"]):
            raise _qerror(_R.PROTECTED_QUARANTINE_EXPIRED, "retention has elapsed.")
        return P.OpaqueRetrievalToken(
            f"dqt1.{ref}.{self._token_mac(ref, owner_hash, header['token_salt'])}"
        )

    def _check_token(
        self, token: P.OpaqueRetrievalToken, ctx: P.QuarantineAccessContext
    ) -> tuple[Path, dict[str, Any], bytes]:
        parts = token.to_wire().split(".") if isinstance(token, P.OpaqueRetrievalToken) else []
        if len(parts) != 3 or parts[0] != "dqt1":
            raise _qerror(_R.PROTECTED_QUARANTINE_ACCESS_DENIED, "access denied.")
        ref, mac = parts[1], parts[2]
        path, owner_hash = self._locate(ref)
        header, blob = self._read_header(path)
        expected = self._token_mac(ref, owner_hash, str(header.get("token_salt", "")))
        owner_ok = header.get("owner") == _owner_hash(ctx.owner) == owner_hash
        key_ok = header.get("key_version") == str(self._key_provider.key_version)
        if not (hmac.compare_digest(mac, expected) and owner_ok and key_ok):
            raise _qerror(_R.PROTECTED_QUARANTINE_ACCESS_DENIED, "access denied.")
        if float(self._clock()) >= float(header["expires_at"]):
            raise _qerror(_R.PROTECTED_QUARANTINE_EXPIRED, "retention has elapsed.")
        return path, header, blob

    @staticmethod
    def _frames(blob: bytes, header_end: int) -> tuple[list[bytes], dict[str, Any], int]:
        frames: list[bytes] = []
        pos = header_end
        while True:
            (size,) = struct.unpack(">I", blob[pos : pos + 4])
            pos += 4
            if size == 0:
                (tsize,) = struct.unpack(">I", blob[pos : pos + 4])
                trailer = json.loads(blob[pos + 4 : pos + 4 + tsize])
                return frames, trailer, pos - 4
            frames.append(blob[pos : pos + size])
            pos += size

    def _verify(self, blob: bytes) -> tuple[list[bytes], dict[str, Any]]:
        try:
            (size,) = struct.unpack(">I", blob[5:9])
            header_end = 9 + size
            frames, trailer, body_end = self._frames(blob, header_end)
            if hashlib.sha256(blob[:body_end]).hexdigest() != trailer["sha256"]:
                raise ValueError("digest")
            return frames, trailer
        except (ValueError, KeyError, struct.error):
            raise _qerror(_R.PROTECTED_QUARANTINE_ACCESS_DENIED, "access denied.") from None

    def open(
        self, token: P.OpaqueRetrievalToken, access_context: P.QuarantineAccessContext
    ) -> list[P.QuarantineRecord]:
        from cryptography.fernet import InvalidToken

        _, _, blob = self._check_token(token, access_context)
        frames, _ = self._verify(blob)
        fernet = self._fernet()
        out: list[P.QuarantineRecord] = []
        try:
            for frame in frames:
                for line in fernet.decrypt(frame).split(b"\n"):
                    raw = json.loads(line)
                    out.append(
                        P.QuarantineRecord(
                            table=raw["table"],
                            row_position=raw["row_position"],
                            reasons=tuple(raw["reasons"]),
                            row={k: _revive(v) for k, v in raw["row"].items()},
                        )
                    )
        except (InvalidToken, ValueError, KeyError):
            raise _qerror(_R.PROTECTED_QUARANTINE_ACCESS_DENIED, "access denied.") from None
        return out

    def verify_digest(
        self, token: P.OpaqueRetrievalToken, access_context: P.QuarantineAccessContext
    ) -> bool:
        _, _, blob = self._check_token(token, access_context)
        self._verify(blob)
        return True

    def purge_expired(self, now: float, authority: P.QuarantineAccessContext) -> P.PurgeResult:
        owner_hash = _owner_hash(authority.owner)
        try:
            directory = self._owner_dir(owner_hash, create=False)
        except (OSError, ValueError):
            return P.PurgeResult(0, 0, 0)
        purged = failed = retained = 0
        for path in sorted(directory.glob("*.dqa")):
            try:
                header, _ = self._read_header(path)
                if header.get("owner") != owner_hash or float(header["expires_at"]) > now:
                    retained += 1
                    continue
                os.unlink(path)
                purged += 1
            except Exception:
                failed += 1
        return P.PurgeResult(purged, failed, retained)
