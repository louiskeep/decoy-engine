"""Fault-injection unit tests for `quarantine.write_jsonl_staged`'s
leak-safety (Codex finding #4, cross-model review of the DE-08 reland).

Before this fix, `write_jsonl_staged`'s error path did:

    except BaseException:
        staged.unlink(missing_ok=True)
        raise

Two problems:

  1. If `staged.unlink()` itself raised (e.g. a permission error, not a
     "missing" file), that new exception replaced the original
     write/serialize error on the bare `raise` -- the caller never saw the
     real cause.
  2. `os.fdopen(fd, ...)` itself was inside the same `try`; if it raised
     before wrapping the raw `mkstemp` fd in a file object, that fd (a bare
     int with no destructor) was never closed -- a leaked file descriptor.

The fix separates fdopen from the write loop and makes every cleanup step
(`os.close`, `fh.close`, `staged.unlink`) best-effort: a cleanup failure is
swallowed so it can never mask the original exception.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from decoy_engine.quarantine import write_jsonl_staged


class _BoomOnStr:
    """A value whose `__str__` raises, forcing `_json_default` (which falls
    back to `str(obj)` for non-JSON-serialisable values) to raise partway
    through `json.dumps` -- a realistic write/serialize fault, not a
    monkeypatched one."""

    def __str__(self) -> str:
        raise RuntimeError("boom serializing")


class TestUnlinkFailureDoesNotMaskOriginalError:
    def test_original_write_error_propagates_not_the_unlink_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record that fails to serialize raises RuntimeError from deep
        inside json.dumps. If the cleanup unlink() ALSO fails (simulated),
        the caller must still see the ORIGINAL RuntimeError, never the
        unlink OSError."""
        final_path = str(tmp_path / "out" / "quarantine.jsonl")
        records = [{"bad": _BoomOnStr()}]

        def _boom_unlink(self: Path, missing_ok: bool = False) -> None:
            raise OSError("simulated unlink failure during cleanup")

        monkeypatch.setattr(Path, "unlink", _boom_unlink)

        with pytest.raises(RuntimeError, match="boom serializing"):
            write_jsonl_staged(final_path, records)

    def test_write_error_propagates_when_unlink_succeeds(self, tmp_path: Path) -> None:
        """Sanity companion (no fault injection on cleanup): the original
        error still propagates and the partial staging file is removed."""
        final_path = str(tmp_path / "out" / "quarantine.jsonl")
        records = [{"bad": _BoomOnStr()}]

        with pytest.raises(RuntimeError, match="boom serializing"):
            write_jsonl_staged(final_path, records)

        leftovers = list((tmp_path / "out").glob("_decoy_quarantine_stage_*"))
        assert leftovers == [], "partial staging file must be removed on a real cleanup success"


class TestFdopenFailureDoesNotLeakRawFd:
    def test_fdopen_failure_closes_the_raw_mkstemp_fd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If os.fdopen() itself raises before wrapping the fd in a file
        object, the bare int fd from tempfile.mkstemp has no destructor and
        would leak unless explicitly closed. Spies on os.close (still
        delegating to the real close) to prove it happened exactly once."""
        final_path = str(tmp_path / "out" / "quarantine.jsonl")
        records = [{"a": 1}]

        closed_fds: list[int] = []
        original_close = os.close

        def _tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            original_close(fd)

        def _boom_fdopen(fd: int, *args: object, **kwargs: object) -> None:
            raise OSError("simulated fdopen failure")

        monkeypatch.setattr(os, "close", _tracking_close)
        monkeypatch.setattr(os, "fdopen", _boom_fdopen)

        with pytest.raises(OSError, match="simulated fdopen failure"):
            write_jsonl_staged(final_path, records)

        assert len(closed_fds) == 1, "the raw mkstemp fd must be explicitly closed, not leaked"

        leftovers = list((tmp_path / "out").glob("_decoy_quarantine_stage_*"))
        assert leftovers == [], "no orphaned staging file left behind after an fdopen failure"
