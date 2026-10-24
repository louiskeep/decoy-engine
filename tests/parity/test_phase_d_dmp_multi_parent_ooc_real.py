"""Phase D / Dmp: the compiled companion on multi-parent FK out-of-core (companion present).

The stand-in suite proves routing and consumption with a Python double behind the harness's
native tag; this file is the compiled-vs-Python proof for the multi-parent shapes: the loaded
kernel is the compiled wrapper at the engine's ABI, the route's evidence names
`rust_companion` for every parent, and the kernel's tokens on every parent key column equal
the oracle's and the companion-hidden Python route's byte for byte, on every run mode, at 1
and 4 native threads and at every batch size the resident suite pins, with the lane each mode
must build asserted. First-hit-wins in declared order is asserted on the compiled tokens
directly. Skipped only where NO companion is installed; an installed companion that does not
load FAILS every case (`_companion_gate`).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dmp_shapes as m
from tests.parity._companion_gate import CompanionState, require_companion
from tests.parity._kernel_witness import force_companion_unavailable

P, R, W = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN
BATCH_SIZES = [1, 2, 3, None]
BATCH_IDS = ["rows1", "rows2", "rows3", "default"]
THREADS = [1, 4]


@pytest.fixture(autouse=True, scope="module")
def _companion() -> CompanionState:
    """Absent companion: skip. Installed but not loadable: fail, never skip."""
    return require_companion()


_SHAPES = {
    "two-parent-preserve": lambda: s.multi_parent_shape(P),
    "two-parent-remap": lambda: s.multi_parent_shape(R),
    "three-parent-warn": lambda: m.three_parent_shape(W),
    "three-parent-reversed-remap": lambda: m.reversed_shape(m.three_parent_shape(R)),
    "composite-multi-parent-preserve": lambda: m.composite_multi_parent_shape(P),
    "wide-overlap-preserve": lambda: m.wide_shape(P, rows=6),
}


@functools.cache
def _oracle(name: str) -> Any:
    """The oracle does not see the run mode, thread count or batch size: one run per shape."""
    return s.run_full(_SHAPES[name]())


def _tokens(run: m.OocRun, table: str, column: str = "k") -> list[object]:
    return run.outputs[table].column(column).to_pylist()


@pytest.mark.parametrize("batch_rows", BATCH_SIZES, ids=BATCH_IDS)
@pytest.mark.parametrize("native_threads", THREADS, ids=["threads1", "threads4"])
@pytest.mark.parametrize("mode", m.MODES)
@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_real_kernel_equals_the_oracle_and_the_python_route(
    name: str,
    mode: str,
    native_threads: int,
    batch_rows: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _SHAPES[name]()
    kw: dict[str, Any] = {"batch_rows": batch_rows, "native_threads": native_threads}
    native = m.run_ooc(shape, mode, tmp_path=tmp_path / "native", **kw)
    m.assert_lane(native, mode)
    masked = [e for e in m.hash_evidence(native) if e["path"] == "mask_batch"]
    parents = [t for t in shape.sources if t.startswith("P")]
    # Every parent key column was masked on the compiled kernel; the child never is.
    assert {(e["table"], e["column"]) for e in masked} == {
        (t, c) for t in parents for c in shape.sources[t].column_names
    }
    assert {e["executed_backend"] for e in masked} == {"rust_companion"}
    oracle = _oracle(name)
    m.assert_agrees(oracle, native)
    assert m.sorted_warnings(native.warnings) == m.sorted_warnings(oracle.warnings)
    force_companion_unavailable(monkeypatch)
    python = m.run_ooc(shape, mode, tmp_path=tmp_path / "python", **kw)
    m.assert_lane(python, mode)
    for table in shape.sources:
        assert native.outputs[table].equals(python.outputs[table], check_metadata=True), table
    assert m.sorted_warnings(python.warnings) == m.sorted_warnings(native.warnings)


@pytest.mark.parametrize("mode", m.MODES)
@pytest.mark.parametrize("native_threads", THREADS, ids=["threads1", "threads4"])
def test_real_kernel_first_declared_parent_wins_on_the_compiled_tokens(
    native_threads: int, mode: str, tmp_path: Path
) -> None:
    """`all3` is in every parent and `p23` in the last two: the child carries the FIRST
    declared parent's compiled token for each, and reversing the declaration moves both."""
    forward = m.run_ooc(
        m.three_parent_shape(R), mode, tmp_path=tmp_path / "f", native_threads=native_threads
    )
    backward = m.run_ooc(
        m.reversed_shape(m.three_parent_shape(R)),
        mode,
        tmp_path=tmp_path / "b",
        native_threads=native_threads,
    )
    for run in (forward, backward):
        assert {
            e["executed_backend"] for e in m.hash_evidence(run) if e["path"] == "mask_batch"
        } == {"rust_companion"}
        assert len({_tokens(run, "P1")[1], _tokens(run, "P2")[1], _tokens(run, "P3")[1]}) == 3
    assert _tokens(forward, "X", "ref")[3] == _tokens(forward, "P1")[1]
    assert _tokens(forward, "X", "ref")[4] == _tokens(forward, "P2")[2]
    assert _tokens(backward, "X", "ref")[3] == _tokens(backward, "P3")[1]
    assert _tokens(backward, "X", "ref")[4] == _tokens(backward, "P3")[2]
    assert _tokens(forward, "X", "ref")[3] != _tokens(backward, "X", "ref")[3]
