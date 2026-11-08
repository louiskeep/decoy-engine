"""A plain generated sequence that is a relationship parent of a masked/source child stays text.

A source or masked child reads its key as text, so a typed parent would stop matching it. One
real orphan ("9") is planted; every policy must report exactly that one orphan, as before typing.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from tests.parity._f2_mixed_support import CHILD, FK, PARENT, child_table, mixed_config, run

KEYS = pa.array([str(i % 5) for i in range(12)] + ["9"], pa.string())


@pytest.fixture(autouse=True)
def _cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("mode", ["full_frame", "auto"])
def test_preserve_keeps_parent_and_child_the_same_text_type(tmp_path: Path, mode: str) -> None:
    child = child_table(KEYS)
    got = run(mixed_config(tmp_path, child, policy="preserve"), {CHILD: child}, mode=mode)
    parent_ids, child_keys = got.outputs[PARENT].column("id"), got.outputs[CHILD].column(FK)
    assert parent_ids.type == child_keys.type == pa.string()
    assert child_keys.to_pylist()[-1] == "9"


@pytest.mark.parametrize("mode", ["full_frame", "auto"])
def test_warn_reports_exactly_the_one_orphan(tmp_path: Path, mode: str) -> None:
    child = child_table(KEYS)
    got = run(mixed_config(tmp_path, child, policy="warn"), {CHILD: child}, mode=mode)
    orphan = [w for w in got.warnings if w.code == "orphan_fk"]
    assert len(orphan) == 1 and orphan[0].detail["orphan_rows"] == 1
    assert got.outputs[PARENT].column("id").type == pa.string()


@pytest.mark.parametrize("mode", ["full_frame", "auto"])
def test_fail_stops_on_exactly_the_one_orphan(tmp_path: Path, mode: str) -> None:
    child = child_table(KEYS)
    with pytest.raises(ExecutionError, match=r"orphan_fk_violation.*\b1 orphan row"):
        run(mixed_config(tmp_path, child, policy="fail"), {CHILD: child}, mode=mode)
