"""A quarantine artifact that fails to open leaves no fd and no ``.partial``, and says how
cleanup ended instead of reporting ``not_needed``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.contracts.declared_casting import ProtectedQuarantineConfig
from tests.acceptance._f06_support import PROTECTED_QUARANTINE


def _sink(tmp_path: Path) -> Any:
    from decoy_engine.declared_casting import LocalProtectedQuarantineSink
    from decoy_engine.keyprovider import SecretKeyProvider

    return LocalProtectedQuarantineSink(
        tmp_path / "q", key_provider=SecretKeyProvider(secret=b"q" * 32)
    )


def _begin(sink: Any) -> Any:
    from decoy_engine.declared_casting import _quarantine_protocol as P  # noqa: N812

    policy = ProtectedQuarantineConfig(**PROTECTED_QUARANTINE)
    return sink.begin(P.QuarantineRun(owner="tenant-1", run_id="run-A"), policy)


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _partials(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "q").rglob("*.partial"))


@pytest.mark.parametrize("failing", ["fchmod", "write"])
def test_failed_open_closes_the_fd_and_removes_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    from decoy_engine.declared_casting import _quarantine_local as local
    from decoy_engine.execution._errors import QuarantineError

    sink = _sink(tmp_path)
    before = _open_fds()

    def boom(*a: Any, **k: Any) -> None:
        raise OSError(28, "injected")

    monkeypatch.setattr(local.os, failing, boom)
    with pytest.raises(QuarantineError) as info:
        _begin(sink)
    monkeypatch.undo()
    assert info.value.cleanup_state == "confirmed"
    assert _partials(tmp_path) == []
    assert _open_fds() == before


def test_failed_open_reports_a_cleanup_that_could_not_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.declared_casting import _quarantine_local as local
    from decoy_engine.execution._errors import QuarantineError

    sink = _sink(tmp_path)

    def boom(*a: Any, **k: Any) -> None:
        raise OSError(28, "injected")

    def no_unlink(path: Any, *a: Any, **k: Any) -> None:
        raise PermissionError(13, "injected")

    monkeypatch.setattr(local.os, "fchmod", boom)
    monkeypatch.setattr(local.os, "unlink", no_unlink)
    with pytest.raises(QuarantineError) as info:
        _begin(sink)
    monkeypatch.undo()
    assert info.value.cleanup_state == "failed"
