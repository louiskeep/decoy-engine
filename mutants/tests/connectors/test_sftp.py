"""SFTPFileSource + SFTPFileSink contract tests.

paramiko has server-side classes but standing up a full SSH server in a
unit test is heavy (key generation, socket binding, transport
negotiation). Instead this module installs an in-memory fake SFTP
client onto the paramiko surface for the duration of each test. The
fake operates on a single dict[str, bytes] that represents the remote
filesystem. Same dict is exposed to tests via the `remote_fs` fixture
so they can pre-seed reads and assert on writes.

What the fake covers: `listdir_attr`, `stat`, `open` (read + write
modes), plus a minimal SFTPFile that reads/writes against the dict.
What it does not cover: directory creation, permission errors beyond
"path not found". Tests that need those should use a real paramiko
server fixture (out of scope for Sprint G Week 3).
"""

from __future__ import annotations

import io
import os
import stat as stat_lib
import time

import pytest

# paramiko (the `sftp` extra) is out of the R1.0 cutline (file + cloud
# object storage only; SFTP ships in S18). Default-extras CI does not
# install it, so skip the whole module rather than error at collection.
pytest.importorskip("paramiko")

from sdk_contract_tests import FileSinkContract, FileSourceContract

from decoy_engine.connectors.sftp import (
    SFTPConfig,
    SFTPFileSink,
    SFTPFileSource,
)
from decoy_engine.sdk import PermanentError

# ----- Fake paramiko SFTP layer ------------------------------------------


class _FakeSFTPAttributes:
    """Stand-in for paramiko.SFTPAttributes carrying just what list/stat use."""

    def __init__(self, filename: str, size: int, mtime: float, is_dir: bool = False):
        self.filename = filename
        self.st_size = size
        self.st_mtime = int(mtime)
        self.st_mode = stat_lib.S_IFDIR if is_dir else stat_lib.S_IFREG


class _FakeSFTPFile:
    """File-like wrapper around bytes in an in-memory dict."""

    def __init__(self, fs: dict, path: str, mode: str):
        self.fs = fs
        self.path = path
        self.mode = mode
        if "r" in mode:
            if path not in fs:
                raise FileNotFoundError(f"No such file: {path}")
            self._buf = io.BytesIO(fs[path])
        elif "w" in mode:
            self._buf = io.BytesIO()
        else:
            raise ValueError(f"unsupported mode: {mode}")

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def write(self, data: bytes) -> int:
        return self._buf.write(data)

    def close(self):
        if "w" in self.mode:
            self.fs[self.path] = self._buf.getvalue()
        self._buf.close()


class _FakeSFTPClient:
    """Minimal SFTPClient quacking just enough for our connector."""

    def __init__(self, fs: dict):
        self.fs = fs

    def listdir_attr(self, path: str = "."):
        # `.` (SSH home) and `""` both mean "root" in our flat fs.
        path_norm = "" if path in ("", ".") else path.rstrip("/")
        results = []
        for full_path, body in self.fs.items():
            parent, _, name = full_path.rpartition("/")
            if parent == path_norm:
                results.append(_FakeSFTPAttributes(name, len(body), time.time()))
        if not results and path_norm:
            # Path itself does not exist as a directory anchor: raise so
            # callers map it to PermanentError.
            if not any(k.startswith(path_norm + "/") for k in self.fs):
                raise FileNotFoundError(f"No such directory: {path}")
        return results

    def stat(self, path: str):
        if path not in self.fs:
            raise FileNotFoundError(f"No such file: {path}")
        return _FakeSFTPAttributes(os.path.basename(path), len(self.fs[path]), time.time())

    def open(self, path: str, mode: str):
        return _FakeSFTPFile(self.fs, path, mode)

    def close(self):
        pass


class _FakeSSHClient:
    """Stand-in for paramiko.SSHClient. Records connect() calls; opens
    the fake SFTP client on demand."""

    def __init__(self, fs: dict):
        self.fs = fs
        self.connected = False
        self.connect_kwargs: dict | None = None

    def set_missing_host_key_policy(self, policy):
        return None

    def load_host_keys(self, path):
        # QA 2026-05-31 session2 F5: real SSHClient supports this; the
        # production code only calls it when the known_hosts file
        # exists, so for tests we'd never reach this unless the test
        # env has a real known_hosts in HOME -- still safe to stub.
        return None

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        self.connected = True

    def open_sftp(self):
        if not self.connected:
            raise RuntimeError("open_sftp() before connect()")
        return _FakeSFTPClient(self.fs)

    def close(self):
        self.connected = False


# ----- Fixtures -----------------------------------------------------------


@pytest.fixture
def remote_fs() -> dict:
    """In-memory remote-filesystem dict shared across the test.

    Tests pre-seed `remote_fs[path] = bytes` to set up reads; the
    connector's writes also land here, so post-write asserts can inspect
    the same dict.
    """
    return {}


@pytest.fixture
def patched_paramiko(monkeypatch, remote_fs):
    """Patch paramiko.SSHClient so connections go to our fake."""
    import paramiko

    fake_client = _FakeSSHClient(remote_fs)
    monkeypatch.setattr(paramiko, "SSHClient", lambda: fake_client)
    # AutoAddPolicy and connection-class types only need to exist; the
    # fake set_missing_host_key_policy accepts whatever the SDK passes.
    return fake_client


@pytest.fixture
def sftp_config() -> SFTPConfig:
    return SFTPConfig(
        host="sftp.test",
        port=22,
        username="alice",
        password="hunter2",
    )


# ----- Contract conformance (FileSource side) ----------------------------


class TestSFTPFileSourceContract(FileSourceContract):
    @pytest.fixture
    def source(self, sftp_config, patched_paramiko, remote_fs):
        # Seed at the root: SFTP listdir is non-recursive, so the
        # contract test's `source.list()` (no prefix) only sees files
        # in the base directory. For S3 the equivalent works under any
        # path because list_objects_v2 is flat.
        remote_fs["hello.txt"] = b"contract-fixture-body\n"
        return SFTPFileSource(sftp_config)

    @pytest.fixture
    def seeded_path(self):
        return "hello.txt"


# ----- Contract conformance (FileSink side) ------------------------------


class TestSFTPFileSinkContract(FileSinkContract):
    @pytest.fixture
    def sink(self, sftp_config, patched_paramiko):
        return SFTPFileSink(sftp_config)

    @pytest.fixture
    def reader_for(self, remote_fs):
        def _read(path: str) -> bytes:
            return remote_fs[path]

        return _read


# ----- SFTP-specific behavior --------------------------------------------


class TestSFTPSourceBehavior:
    def test_check_succeeds_when_connect_works(self, sftp_config, patched_paramiko):
        source = SFTPFileSource(sftp_config)
        result = source.check()
        assert result.ok is True
        # The fake SSHClient recorded the connect kwargs; verify we passed
        # the right host + password through.
        assert patched_paramiko.connect_kwargs["hostname"] == "sftp.test"
        assert patched_paramiko.connect_kwargs["password"] == "hunter2"

    def test_list_returns_size_and_skips_dirs(self, sftp_config, patched_paramiko, remote_fs):
        remote_fs["data/one.txt"] = b"first"
        remote_fs["data/two.txt"] = b"second-body"
        source = SFTPFileSource(sftp_config)
        listing = sorted(source.list(prefix="data"), key=lambda m: m.path)
        assert [m.path for m in listing] == ["data/one.txt", "data/two.txt"]
        assert listing[0].size == len(b"first")
        assert listing[1].size == len(b"second-body")
        # None of them have a content_type: SFTP doesn't track it.
        assert all(m.content_type is None for m in listing)

    def test_head_returns_size(self, sftp_config, patched_paramiko, remote_fs):
        remote_fs["sized.bin"] = b"x" * 17
        source = SFTPFileSource(sftp_config)
        meta = source.head("sized.bin")
        assert meta.path == "sized.bin"
        assert meta.size == 17

    def test_head_missing_raises_permanent(self, sftp_config, patched_paramiko):
        source = SFTPFileSource(sftp_config)
        with pytest.raises(PermanentError):
            source.head("does-not-exist.bin")

    def test_qa7_f4_bad_host_key_is_permanent(self):
        """QA-7 F4 (2026-06-01): BadHostKeyException (MITM signal)
        must classify as PermanentError. Pre-fix it fell through to
        the generic SSHException branch and was retried -- amplifying
        MITM exposure time + burning the retry budget."""
        import paramiko

        from decoy_engine.connectors.sftp import _wrap_sftp_error
        from decoy_engine.sdk import PermanentError as _Permanent

        # Construct a minimal BadHostKeyException.
        try:
            exc = paramiko.BadHostKeyException(
                "sftp.example.com",
                paramiko.RSAKey.generate(1024),
                paramiko.RSAKey.generate(1024),
            )
        except Exception:
            # Some paramiko versions need a different signature; use
            # a generic stand-in if generation fails.
            class _FakeBadHostKey(paramiko.BadHostKeyException):
                def __init__(self):
                    pass

            exc = _FakeBadHostKey()
        wrapped = _wrap_sftp_error(exc)
        assert isinstance(wrapped, _Permanent), (
            "BadHostKeyException must classify as PermanentError, not "
            "TransientError (MITM signal should not be retried)"
        )

    def test_qa7_f4_bad_auth_type_is_permanent(self):
        """QA-7 F4: BadAuthenticationType (server rejects the auth
        method) must classify as PermanentError. Retrying the same
        rejected auth method against the same server will fail the
        same way."""
        import paramiko

        from decoy_engine.connectors.sftp import _wrap_sftp_error
        from decoy_engine.sdk import PermanentError as _Permanent

        exc = paramiko.BadAuthenticationType("auth rejected", ["publickey"])
        wrapped = _wrap_sftp_error(exc)
        assert isinstance(wrapped, _Permanent)

    def test_open_streams_full_body(self, sftp_config, patched_paramiko, remote_fs):
        body = b"streaming-sftp-body " * 200
        remote_fs["streamed.bin"] = body
        source = SFTPFileSource(sftp_config)
        chunks = list(source.open("streamed.bin"))
        assert b"".join(chunks) == body

    def test_base_path_scopes_listing(self, patched_paramiko, remote_fs):
        remote_fs["scoped/inside.txt"] = b"yes"
        remote_fs["outside.txt"] = b"no"
        cfg = SFTPConfig(
            host="sftp.test",
            username="alice",
            password="hunter2",
            base_path="scoped",
        )
        listing = [m.path for m in SFTPFileSource(cfg).list()]
        # base_path joins with empty call-prefix to scope at "scoped".
        # outside.txt is at root and never listed.
        assert "scoped/inside.txt" in listing
        assert "outside.txt" not in listing

    def test_config_requires_password_or_private_key(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SFTPConfig(host="sftp.test", username="alice")


class TestSFTPSinkBehavior:
    def test_write_round_trip(self, sftp_config, patched_paramiko, remote_fs):
        body = b"sink-round-trip\n"
        sink = SFTPFileSink(sftp_config)
        result = sink.write("written.txt", iter([body]))
        assert result.path == "written.txt"
        assert result.bytes_written == len(body)
        assert remote_fs["written.txt"] == body

    def test_write_multi_chunk(self, sftp_config, patched_paramiko, remote_fs):
        chunks = [b"part-1\n", b"part-2\n", b"part-3\n"]
        sink = SFTPFileSink(sftp_config)
        sink.write("multi.txt", iter(chunks))
        assert remote_fs["multi.txt"] == b"".join(chunks)

    def test_base_path_joined_to_write(self, patched_paramiko, remote_fs):
        cfg = SFTPConfig(
            host="sftp.test",
            username="alice",
            password="hunter2",
            base_path="outbox",
        )
        result = SFTPFileSink(cfg).write("subdir/file.txt", iter([b"prefixed"]))
        assert result.path == "outbox/subdir/file.txt"
        assert remote_fs["outbox/subdir/file.txt"] == b"prefixed"
