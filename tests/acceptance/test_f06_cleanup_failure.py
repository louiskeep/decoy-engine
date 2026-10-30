"""F06 r3: a failure AFTER the quarantine is sealed must never orphan protected data silently.

The artifact holds encrypted raw rows. When the run fails after the seal and the discard itself
fails (the adapter reports ``failed`` on an unlink error), either no artifact remains or the
failure is reported with a retained recovery identity and a registered, value-free F29 reason.
The run's own failure is what propagates in every case.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance import test_f06_route_parity as parity
from tests.acceptance._f06_support import run

Injector = Callable[[pytest.MonkeyPatch, Path], dict[str, Any]]


def _fail_unlink_of_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.declared_casting import _quarantine_local as local

    real = local.os.unlink

    def unlink(path: Any, *a: Any, **k: Any) -> None:
        if str(path).endswith(".dqa"):
            raise PermissionError(13, "injected: artifact cannot be removed")
        real(path, *a, **k)

    monkeypatch.setattr(local.os, "unlink", unlink)


def _fail_after_seal_in_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution._chunked_output_sink import OutputPublish

    def boom(self: Any) -> None:
        raise RuntimeError("injected failure after the seal")

    monkeypatch.setattr(OutputPublish, "commit", boom)


def _fail_token_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.declared_casting import LocalProtectedQuarantineSink

    def boom(self: Any, *a: Any, **k: Any) -> None:
        raise RuntimeError("injected failure after the seal")

    monkeypatch.setattr(LocalProtectedQuarantineSink, "get_retrieval_token", boom)


def _full_frame(tmp_path: Path, tag: str) -> dict[str, Any]:
    return {
        "raw": parity._cfg(tmp_path, "quarantine"),
        "sources": {"people": parity._source()},
        "kwargs": {"execution_mode": "full_frame", "auto_chunk": False},
    }


def _chunked(tmp_path: Path, tag: str) -> dict[str, Any]:
    return {
        "raw": parity._cfg(tmp_path, "quarantine"),
        "sources": {"people": parity._source()},
        "kwargs": {"auto_chunk": True, "chunk_size_rows": 2, "auto_chunk_threshold_rows": 1},
    }


def _unified(tmp_path: Path, tag: str) -> dict[str, Any]:
    return {
        "raw": parity._int_only(tmp_path, "quarantine"),
        "sources": {"people": parity._source().drop_columns(["start"])},
        "kwargs": {"auto_chunk": False},
    }


def _sequential(tmp_path: Path, tag: str) -> dict[str, Any]:
    from tests.parity._f2_mixed_support import RecordingSink

    return {
        "raw": parity._fk_cfg(tmp_path, endpoints=True, policy="quarantine"),
        "sources": parity._fk_sources(),
        "kwargs": {"execution_mode": "sequential", "sink": RecordingSink(fail_on="commit")},
    }


ROUTES: dict[
    str, tuple[Callable[..., dict[str, Any]], Callable[[pytest.MonkeyPatch], None], str]
] = {
    "full_frame": (_full_frame, _fail_after_seal_in_publish, "injected failure after the seal"),
    "chunked": (_chunked, _fail_after_seal_in_publish, "injected failure after the seal"),
    "unified_slice": (_unified, _fail_token_issue, "injected failure after the seal"),
    "sequential": (_sequential, lambda mp: None, "injected sink failure"),
}


def _artifacts(root: Path) -> list[Path]:
    return sorted(root.rglob("*.dqa")) if root.exists() else []


def _identity(path: Path) -> str:
    return f"{path.parent.name}-{path.stem}"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_failed_discard_after_seal_is_reported_with_the_recovery_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from decoy_engine import reason_from_exception, reasons
    from decoy_engine.reasons import CleanupState

    build, inject, message = ROUTES[route]
    spec = build(tmp_path, route)
    inject(monkeypatch)
    _fail_unlink_of_artifacts(monkeypatch)
    kwargs = {**spec["kwargs"], **parity._kwargs("quarantine", tmp_path, route)}

    with pytest.raises(Exception, match=message) as raised:
        run(spec["raw"], spec["sources"], **kwargs)

    assert type(raised.value).__name__ in {"RuntimeError"}, "the run's own failure propagates"
    left = _artifacts(tmp_path / f"q-{route}")
    assert len(left) == 1, f"{route}: the sealed artifact the injection stranded"
    # Reported: the failure's own F29 record says cleanup failed ...
    record = reason_from_exception(raised.value)
    assert record.context.cleanup_state is CleanupState.FAILED
    # ... a registered, value-free reason names the unfinished cleanup ...
    outcome = raised.value.declared_cast_cleanup
    assert (outcome.reason.namespace, outcome.reason.code) == (
        "runtime",
        str(reasons.runtime.PROTECTED_QUARANTINE_CLEANUP_FAILED),
    )
    assert reasons.is_registered(outcome.reason.code, ["runtime"])
    outcome.reason.validate()
    # ... and the recovery identity that lets an operator reconcile exactly this artifact.
    assert outcome.cleanup_state == "failed"
    assert outcome.ref == _identity(left[0])
    assert outcome.run_id == "run-A"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_successful_discard_after_seal_leaves_no_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    build, inject, message = ROUTES[route]
    spec = build(tmp_path, route)
    inject(monkeypatch)
    kwargs = {**spec["kwargs"], **parity._kwargs("quarantine", tmp_path, route)}
    with pytest.raises(Exception, match=message):
        run(spec["raw"], spec["sources"], **kwargs)
    assert _artifacts(tmp_path / f"q-{route}") == []


def test_a_failed_discard_keeps_the_receipt_so_a_later_abort_can_finish_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never forget the receipt on failure: the second ``abort`` retries and succeeds."""
    from decoy_engine.declared_casting import _quarantine_local as local

    spec = _full_frame(tmp_path, "retry")
    captured: dict[str, Any] = {}
    from decoy_engine.declared_casting._boundary import DeclaredCastRun

    original_abort = DeclaredCastRun.abort

    def spy(self: Any, failing: Any = None) -> Any:
        captured["run"] = self
        return original_abort(self, failing)

    monkeypatch.setattr(DeclaredCastRun, "abort", spy)
    _fail_after_seal_in_publish(monkeypatch)
    real = local.os.unlink
    _fail_unlink_of_artifacts(monkeypatch)
    kwargs = {**spec["kwargs"], **parity._kwargs("quarantine", tmp_path, "retry")}
    with pytest.raises(RuntimeError, match="injected failure after the seal"):
        run(spec["raw"], spec["sources"], **kwargs)
    assert len(_artifacts(tmp_path / "q-retry")) == 1

    monkeypatch.setattr(local.os, "unlink", real)
    outcome = captured["run"].abort()
    assert outcome.cleanup_state == "confirmed"
    assert _artifacts(tmp_path / "q-retry") == []


def test_a_discard_that_raises_still_lets_the_original_failure_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.declared_casting import LocalProtectedQuarantineSink

    def explode(self: Any, receipt: Any) -> str:
        raise OSError("store unreachable")

    monkeypatch.setattr(LocalProtectedQuarantineSink, "discard", explode)
    _fail_after_seal_in_publish(monkeypatch)
    spec = _full_frame(tmp_path, "raises")
    kwargs = {**spec["kwargs"], **parity._kwargs("quarantine", tmp_path, "raises")}
    with pytest.raises(RuntimeError, match="injected failure after the seal") as raised:
        run(spec["raw"], spec["sources"], **kwargs)
    assert raised.value.declared_cast_cleanup.cleanup_state == "failed"
    assert len(_artifacts(tmp_path / "q-raises")) == 1
