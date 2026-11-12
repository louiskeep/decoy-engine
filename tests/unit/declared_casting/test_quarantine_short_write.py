"""A short ``os.write`` never seals a receipt for rows the artifact does not hold.

POSIX ``write(2)`` may return fewer bytes than asked (signals, quotas, pipes, some network file
systems). Each part of the artifact (header, encrypted body frame, trailer) gets one injected
short return here; the sealed artifact must still reopen with every row its receipt counts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.contracts.declared_casting import ProtectedQuarantineConfig
from tests.acceptance._f06_support import PROTECTED_QUARANTINE

_ROW = {"id": "1", "amount": "12.50", "note": "short-write"}


def _kind(data: bytes) -> str:
    from decoy_engine.declared_casting import _quarantine_local as local

    if data.startswith(local._MAGIC):
        return "header"
    if data[:4] == b"\x00\x00\x00\x00":
        return "trailer"
    return "body"


def _short_once(target: str) -> tuple[Any, dict[str, int]]:
    """An ``os.write`` that lands only half of the first write of ``target`` and reports it."""
    real = os.write
    hits = {"short": 0}

    def write(fd: int, data: Any) -> int:
        data = bytes(data)
        if not hits["short"] and len(data) > 1 and _kind(data) == target:
            hits["short"] += 1
            return real(fd, data[: len(data) // 2])
        return real(fd, data)

    return write, hits


@pytest.mark.parametrize("target", ["header", "body", "trailer"])
def test_a_short_write_never_loses_a_receipted_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    from decoy_engine.declared_casting import (
        LocalProtectedQuarantineSink,
        QuarantineAccessContext,
        QuarantineEnvelope,
        QuarantinePolicy,
        QuarantineRun,
    )
    from decoy_engine.declared_casting import _quarantine_local as local
    from decoy_engine.keyprovider import SecretKeyProvider

    sink = LocalProtectedQuarantineSink(
        tmp_path / "q", key_provider=SecretKeyProvider(secret=b"q" * 32)
    )
    write, hits = _short_once(target)
    monkeypatch.setattr(local.os, "write", write)
    session = sink.begin(
        QuarantineRun(owner="tenant-1", run_id="run-A"),
        QuarantinePolicy(**ProtectedQuarantineConfig(**PROTECTED_QUARANTINE).model_dump()),
    )
    sink.write_row(session, QuarantineEnvelope(table="t", row_position=0, reasons=(), row=_ROW))
    receipt = sink.seal(session)
    monkeypatch.undo()
    assert hits["short"] == 1, "the short return was injected"
    ctx = QuarantineAccessContext(owner="tenant-1", run_id="run-A")
    records = sink.open(sink.get_retrieval_token(receipt, ctx), ctx)
    assert receipt.rows == len(records) == 1
    assert records[0].row == _ROW
