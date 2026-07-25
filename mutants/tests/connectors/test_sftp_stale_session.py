"""Dennis M2 closure regression (QA gate review 2026-05-31).

Pin the behavior that `_SFTPMixin._connect()` detects a stale cached
SFTP session + tears down + reconnects, instead of returning the
broken session on every retry.

Before the fix: after a mid-operation SSH disconnect, `self._sftp`
was non-None but the underlying paramiko channel was dead. Every
retry call returned the same stale object + raised SSHException;
the retry loop never reconnected.

After the fix: each `_connect()` call probes the cached session with
a `stat('.')` call. A dead probe triggers a teardown + reconnect.
"""

from __future__ import annotations

from unittest.mock import MagicMock


class _FakeSFTP:
    """Stand-in for paramiko's SFTP client. Tracks open/closed state +
    raises on stat when ``alive`` is False (simulating a stale session)."""

    def __init__(self, alive: bool = True):
        self.alive = alive
        self.closed = False
        self.stat_calls = 0

    def stat(self, path: str):
        self.stat_calls += 1
        if not self.alive:
            raise OSError("simulated stale SFTP session")
        return MagicMock(st_mode=0o100644, st_size=100)

    def close(self):
        self.closed = True


class _FakeSSHClient:
    """Stand-in for paramiko.SSHClient -- just records close()."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestSFTPStaleSessionDetection:
    """The connect-cache must probe + reconnect when the cached session
    is stale, not return the dead object."""

    def test_live_cached_session_is_reused(self):
        """A live cached session passes the stat probe + is returned as-is."""
        from decoy_engine.connectors.sftp import _SFTPMixin

        mixin = _SFTPMixin.__new__(_SFTPMixin)
        mixin._client = _FakeSSHClient()
        live = _FakeSFTP(alive=True)
        mixin._sftp = live

        # Should not call _open_sftp; should return the cached session.
        result = mixin._connect()
        assert result is live
        assert live.stat_calls == 1
        assert not live.closed

    def test_stale_cached_session_triggers_reconnect(self, monkeypatch):
        """A dead probe tears down the cached session + opens a new one."""
        from decoy_engine.connectors import sftp as sftp_mod
        from decoy_engine.connectors.sftp import _SFTPMixin

        mixin = _SFTPMixin.__new__(_SFTPMixin)
        mixin.config = MagicMock()
        old_client = _FakeSSHClient()
        stale = _FakeSFTP(alive=False)
        mixin._client = old_client
        mixin._sftp = stale

        # Monkeypatch _open_sftp to return a fresh pair instead of doing
        # a real connect attempt.
        new_client = _FakeSSHClient()
        new_sftp = _FakeSFTP(alive=True)
        monkeypatch.setattr(sftp_mod, "_open_sftp", lambda config: (new_client, new_sftp))

        result = mixin._connect()
        # Stale session torn down.
        assert stale.closed
        assert old_client.closed
        # Fresh session returned + cached.
        assert result is new_sftp
        assert mixin._sftp is new_sftp
        assert mixin._client is new_client

    def test_no_cached_session_just_opens(self, monkeypatch):
        """When _sftp is None, _connect opens a fresh session (no probe)."""
        from decoy_engine.connectors import sftp as sftp_mod
        from decoy_engine.connectors.sftp import _SFTPMixin

        mixin = _SFTPMixin.__new__(_SFTPMixin)
        mixin.config = MagicMock()
        mixin._client = None
        mixin._sftp = None

        new_client = _FakeSSHClient()
        new_sftp = _FakeSFTP(alive=True)
        monkeypatch.setattr(sftp_mod, "_open_sftp", lambda config: (new_client, new_sftp))

        result = mixin._connect()
        assert result is new_sftp
        # The probe should NOT have run (nothing to probe).
        assert new_sftp.stat_calls == 0
