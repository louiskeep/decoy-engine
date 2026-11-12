"""`test_marker_corpus_user_surfaces`, isolated part (plan 4.5, 7.2 G5; class 1; slice c2).

The isolated route's user-visible surfaces (`IsolatedRunResult.error`, its F29 `reason` and the
`publication_receipt`) carry codes, errno names, digests and counts only. `PATHMARK` is in the
destination path, `TBLMARK` in the table name and `OSMARK` in an injected `OSError` text, across
published, occupied, conflict, failed-bind and refused-stage outcomes on the isolated parent and
the in-process fallback. Non-vacuity: `TBLMARK` is in the bound descriptor's artifact paths.

Red before c2 (start commit `f9054196`): the route takes no `request` and returns no receipt.
"""

from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution import run_pipeline_isolated
from decoy_engine.publication import PublicationRequest
from tests.unit.execution.publication._isolated_support import (
    ENGINE_VERSION,
    install_fake_worker,
    job,
    sources,
)
from tests.unit.publication._publish_support import OSMARK, descriptor
from tests.unit.publication.test_publish_unit import _check_receipt_fields

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX isolated route")

PATHMARK, TBLMARK = "PATHMARK", "TBLMARK"
MARKERS = (PATHMARK, TBLMARK, OSMARK)
CASES = ["published", "occupied", "conflict", "bind_fails", "planted"]


def _run(tmp: Path, leaf: Path, rows: int, isolate: bool) -> Any:
    cfg = job(tmp, rows, table=f"{TBLMARK}_t")
    return run_pipeline_isolated(
        cfg,
        sources(cfg),
        engine_version=ENGINE_VERSION,
        output_dir=leaf,
        isolate=isolate,
        request=PublicationRequest("op-marker"),
    )


def _fail_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.replace

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if PATHMARK in os.fsdecode(dst):
            raise OSError(errno.EIO, f"{OSMARK} {dst}")
        real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


@pytest.mark.parametrize("isolate", [True, False], ids=["isolated_parent", "isolated_fallback"])
@pytest.mark.parametrize("case", CASES)
def test_marker_corpus_user_surfaces(
    case: str, isolate: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case == "planted" and not isolate:
        pytest.skip("only a child stages for the parent")
    leaf = tmp_path / f"dest-{PATHMARK}" / PATHMARK
    if case == "occupied":
        leaf.mkdir(parents=True)
        (leaf / f"{TBLMARK}.txt").write_text("prior")
    if case == "conflict":
        assert _run(tmp_path, leaf, 3, isolate).outcome == "completed"
    if case == "bind_fails":
        _fail_rename(monkeypatch)
    if case == "planted":
        install_fake_worker(tmp_path, monkeypatch, plant="extra_file")
    result = _run(tmp_path, leaf, 4, isolate)
    receipt = result.publication_receipt
    assert receipt is not None
    _check_receipt_fields(receipt)
    expected = {"published": "published", "conflict": "operation_conflict"}
    assert receipt.outcome == expected.get(case, receipt.outcome)
    surfaces = [repr(receipt), str(result.error), repr(result.committed_output_dir)]
    if result.reason is not None:
        surfaces.append(json.dumps(result.reason.to_payload()))
    if case == "published":
        assert result.outcome == "completed"
        paths = [a["path"] for a in descriptor(leaf)["artifacts"]]
        assert any(TBLMARK in p for p in paths)  # non-vacuity: the table name is in the data
        surfaces.remove(repr(result.committed_output_dir))  # the caller's own path, by contract
    else:
        assert result.outcome == "crashed" and result.reason is not None
        assert result.reason.code == receipt.reason_code
    if case == "bind_fails":
        assert receipt.os_error == "EIO"  # non-vacuity: the injected OS error was hit
    for text in surfaces:
        for marker in MARKERS:
            assert marker not in text, (case, marker, text)
