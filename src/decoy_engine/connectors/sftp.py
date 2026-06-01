"""SFTP file source + sink built on the public connector SDK.

Uses paramiko for the SSH/SFTP protocol. paramiko is an optional extra:
`pip install decoy-engine[sftp]`. Customers who only use S3 / GCS do not
pay for paramiko + cryptography in their install footprint.

Auth modes:
* Password (`config.password`).
* Private key as a PEM string (`config.private_key`). Supports RSA,
  Ed25519, ECDSA, DSA.

`base_path` plays the role S3's `prefix` plays: it scopes the connector
to a sub-tree of the SFTP host. Call-level prefixes are joined under
the base path.
"""

from __future__ import annotations

import io
import stat as stat_lib
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import ClassVar

from pydantic import Field, SecretStr, model_validator

from decoy_engine.sdk import (
    CAP_INTROSPECTION,
    CAP_STREAMING,
    CheckResult,
    ConnectorConfig,
    FileMeta,
    FileSink,
    FileSource,
    PermanentError,
    TransientError,
    WriteResult,
)

__all__ = ["SFTPConfig", "SFTPFileSink", "SFTPFileSource"]


# Streaming read/write chunk size. Matches the S3 connector for
# consistency; SFTP throughput is rarely chunk-size bound below this
# value, so larger chunks would not buy much.
_DEFAULT_CHUNK_BYTES = 1 * 1024 * 1024


def _wrap_sftp_error(exc: Exception) -> Exception:
    """Translate paramiko / OSError exceptions into typed SDK errors.

    paramiko maps remote SFTP status codes to plain `IOError`/`OSError`
    instances. `errno.ENOENT` is the no-such-file code we treat as
    permanent (404-like); auth and protocol errors are also permanent.
    Network-layer failures during an established session are transient
    so the engine retries.
    """
    import paramiko

    # QA-7 F4 (2026-06-01): the more-specific permanent classes must
    # match BEFORE the generic SSHException check (BadHostKeyException
    # is a subclass of SSHException). Pre-fix BadHostKey (MITM
    # indicator) and BadAuthenticationType (permanent auth failure)
    # were mapped to TransientError because the SSHException base
    # caught them first. The engine then retried -- amplifying MITM
    # exposure time + burning the retry budget on guaranteed-to-fail
    # auth attempts.
    if isinstance(exc, paramiko.BadHostKeyException):
        return PermanentError(
            f"SFTP host key mismatch (possible MITM): {type(exc).__name__}"
        )
    if isinstance(exc, paramiko.BadAuthenticationType):
        return PermanentError(
            f"SFTP auth method rejected by server: {type(exc).__name__}"
        )
    if isinstance(exc, paramiko.AuthenticationException):
        return PermanentError(f"SFTP auth failed: {type(exc).__name__}")
    if isinstance(exc, paramiko.SSHException):
        # SSHException covers connection-level failures (kex,
        # disconnect, channel-level transient). Treat as transient so
        # the engine retries; the permanent-fail subclasses are
        # already handled above.
        return TransientError(f"SFTP protocol error: {type(exc).__name__}")
    if isinstance(exc, FileNotFoundError) or "No such file" in str(exc):
        return PermanentError(f"SFTP path not found: {exc}")
    if isinstance(exc, PermissionError):
        return PermanentError(f"SFTP permission denied: {exc}")
    if isinstance(exc, OSError):
        # Anything else that surfaced as an OSError: transient (network blip).
        return TransientError(f"SFTP transient error: {exc}")
    return PermanentError(f"Unexpected SFTP error: {exc}")


class SFTPConfig(ConnectorConfig):
    """Config for SFTP source/sink.

    Either `password` or `private_key` must be provided. Both can be
    set; paramiko prefers the key when both are present.
    """

    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=255)
    password: SecretStr | None = None
    # PEM-encoded private key text. RSA, Ed25519, ECDSA, DSA all OK;
    # paramiko sniffs the type. Use a service account, not a personal
    # key, for production deploys.
    private_key: SecretStr | None = None
    # Directory on the remote host that scopes the connector. Empty
    # means the SSH user's home directory.
    base_path: str = ""

    @model_validator(mode="after")
    def _require_at_least_one_auth(self):
        if self.password is None and self.private_key is None:
            raise ValueError("SFTPConfig requires either `password` or `private_key`")
        return self


def _open_sftp(config: SFTPConfig):
    """Build a paramiko SSH client + SFTP subchannel for `config`.

    Caller is responsible for closing the returned (client, sftp) pair.
    Wraps any paramiko-side exception as a typed SDK error.
    """
    import paramiko

    try:
        import os

        client = paramiko.SSHClient()
        # QA 2026-05-31 session2 F5 (MEDIUM) closure: strict host-key
        # verification by default. The prior AutoAddPolicy silently
        # accepted ANY host key on first connection, making SFTP
        # imports MITM-able. Same fix shape applied to the platform-
        # side _sftp_connect in api/cloud/reader.py (S16b F2).
        #
        # RejectPolicy + load known_hosts from $DECOY_SFTP_KNOWN_HOSTS
        # (default ~/.ssh/known_hosts). Customers configure the SFTP
        # server's host key on initial setup; standard SSH practice.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        known_hosts_path = os.environ.get(
            "DECOY_SFTP_KNOWN_HOSTS",
            os.path.expanduser("~/.ssh/known_hosts"),
        )
        if os.path.exists(known_hosts_path):
            client.load_host_keys(known_hosts_path)

        connect_kwargs: dict = {
            "hostname": config.host,
            "port": config.port,
            "username": config.username,
            "timeout": 15,
        }
        if config.private_key is not None:
            pkey_text = config.private_key.get_secret_value()
            # Try Ed25519, then ECDSA, then RSA, then DSS in turn. Avoids
            # forcing the customer to declare their key type alongside the
            # key itself.
            pkey = _parse_private_key(pkey_text)
            connect_kwargs["pkey"] = pkey
        elif config.password is not None:
            connect_kwargs["password"] = config.password.get_secret_value()

        client.connect(**connect_kwargs)
        sftp = client.open_sftp()
        return client, sftp
    except Exception as exc:
        raise _wrap_sftp_error(exc) from exc


def _parse_private_key(pem_text: str):
    """Parse a PEM-encoded private key, trying each algorithm in turn.

    Saves customers from having to know whether their key is RSA vs
    Ed25519 etc. Paramiko's key classes parse the same PEM format but
    reject the wrong type at parse time, so we try each.
    """
    import paramiko

    last_exc = None
    for key_cls in (
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.RSAKey,
        paramiko.DSSKey,
    ):
        try:
            return key_cls.from_private_key(io.StringIO(pem_text))
        except (paramiko.SSHException, ValueError) as exc:
            last_exc = exc
            continue
    raise PermanentError(f"Unable to parse private key as RSA/Ed25519/ECDSA/DSS: {last_exc}")


def _join_path(base_path: str, path: str) -> str:
    """Compose base_path + path with single-slash separator.

    Empty `base_path` means the SSH user's home; in that case the path
    flows through unchanged. Leading slashes on `path` are stripped so
    a configured base_path is never silently bypassed.
    """
    bp = (base_path or "").rstrip("/")
    p = (path or "").lstrip("/")
    return f"{bp}/{p}" if bp else p


class _SFTPMixin:
    """Shared connect/close lifecycle for source and sink."""

    def __init__(self, config: SFTPConfig) -> None:
        super().__init__(config)
        self._client = None
        self._sftp = None

    def _connect(self):
        """Open or reuse an SFTP session, probing for liveness on reuse.

        QA 2026-05-31 session2 F2 (HIGH) closure: previously this
        returned the cached ``self._sftp`` without checking liveness.
        After a mid-operation SSH disconnect the cached object is
        non-None but the underlying paramiko channel is dead; the next
        op raises SSHException, the retry loop calls _connect() again,
        gets the same stale object, and the same exception fires
        forever. The fix is to probe for a live session before
        trusting the cached object + reconnect on a dead probe.
        """
        if self._sftp is not None:
            try:
                # Zero-cost probe: stat the SFTP server root. A dead
                # session raises immediately + we fall through to
                # reconnect. A live session returns quickly.
                self._sftp.stat(".")
                return self._sftp
            except Exception:
                # Stale session; tear down + reconnect below.
                try:
                    self._sftp.close()
                except Exception:
                    pass
                try:
                    if self._client is not None:
                        self._client.close()
                except Exception:
                    pass
                self._sftp = None
                self._client = None
        self._client, self._sftp = _open_sftp(self.config)
        return self._sftp

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


class SFTPFileSource(_SFTPMixin, FileSource[SFTPConfig]):
    """Read files from an SFTP host."""

    name: ClassVar[str] = "sftp"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
        CAP_INTROSPECTION: True,
    }

    def check(self) -> CheckResult:
        try:
            self._connect()
        except (PermanentError, TransientError) as exc:
            return CheckResult(ok=False, detail=str(exc))
        return CheckResult(ok=True)

    def list(self, prefix: str | None = None) -> Iterator[FileMeta]:
        sftp = self._connect()
        target_dir = _join_path(self.config.base_path, prefix or "")
        try:
            for entry in sftp.listdir_attr(target_dir or "."):
                # Skip subdirectories: list() returns files, not folders.
                if entry.st_mode is not None and stat_lib.S_ISDIR(entry.st_mode):
                    continue
                full_path = (
                    f"{target_dir.rstrip('/')}/{entry.filename}" if target_dir else entry.filename
                )
                yield FileMeta(
                    path=full_path,
                    size=entry.st_size,
                    content_type=None,  # SFTP does not surface content type.
                    modified=(
                        datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc).isoformat()
                        if entry.st_mtime is not None
                        else None
                    ),
                )
        except Exception as exc:
            raise _wrap_sftp_error(exc) from exc

    def head(self, path: str) -> FileMeta:
        sftp = self._connect()
        full_path = _join_path(self.config.base_path, path)
        try:
            attrs = sftp.stat(full_path)
        except Exception as exc:
            raise _wrap_sftp_error(exc) from exc
        return FileMeta(
            path=full_path,
            size=attrs.st_size,
            content_type=None,
            modified=(
                datetime.fromtimestamp(attrs.st_mtime, tz=timezone.utc).isoformat()
                if attrs.st_mtime is not None
                else None
            ),
        )

    def open(self, path: str) -> Iterator[bytes]:
        sftp = self._connect()
        full_path = _join_path(self.config.base_path, path)
        try:
            remote_fp = sftp.open(full_path, "rb")
        except Exception as exc:
            raise _wrap_sftp_error(exc) from exc

        try:
            while True:
                chunk = remote_fp.read(_DEFAULT_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        finally:
            try:
                remote_fp.close()
            except Exception:
                pass


class SFTPFileSink(_SFTPMixin, FileSink[SFTPConfig]):
    """Write files to an SFTP host."""

    name: ClassVar[str] = "sftp"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
    }

    def check(self) -> CheckResult:
        try:
            self._connect()
        except (PermanentError, TransientError) as exc:
            return CheckResult(ok=False, detail=str(exc))
        return CheckResult(ok=True)

    def write(self, path: str, chunks: Iterator[bytes]) -> WriteResult:
        sftp = self._connect()
        full_path = _join_path(self.config.base_path, path)

        # paramiko SFTPFile supports streaming writes. Open in wb (truncate
        # + binary), pipe chunks through, count bytes for the WriteResult.
        bytes_written = 0
        try:
            remote_fp = sftp.open(full_path, "wb")
        except Exception as exc:
            raise _wrap_sftp_error(exc) from exc

        try:
            for chunk in chunks:
                if not chunk:
                    continue
                remote_fp.write(chunk)
                bytes_written += len(chunk)
        except Exception as exc:
            try:
                remote_fp.close()
            except Exception:
                pass
            raise _wrap_sftp_error(exc) from exc
        else:
            try:
                remote_fp.close()
            except Exception as exc:
                raise _wrap_sftp_error(exc) from exc

        return WriteResult(path=full_path, bytes_written=bytes_written)
