"""A parent's relation file shared by two outgoing edges survives a multi-parent merge.

Two edges from the same parent columns write one relation file (`_relation._edge_stem` keys the
stem on the parent table and columns). A multi-parent group's merge must not remove that file
while another edge of the run still reads it: a sibling child processed later, a single-parent
column of the same child, or a second group over the same parents. The out-of-core table order
is lexicographic among ready tables (`_route_policy._table_order`), so `b_single` runs before the
group child `a_multi` and `z_single` after it. Each case runs on the resident and the sink
route and is checked row for row against the full-frame oracle. All values are made up.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.csv as pcsv
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _runner
from decoy_engine.fileaccess import ScratchArena

P_KEYS = [f"p{i}" for i in range(1, 7)]
Q_KEYS = [f"q{i}" for i in range(1, 4)]
MULTI_REFS = ["p1", "q1", "p2", "q2", "p3", "q3"]
SINGLE_REFS = ["p1", "p4", "p5", "p6", "p2"]

# shape -> {table: {column: values}}, the FK edges as (parent, child table, child column).
_SHAPES: dict[str, tuple[dict[str, dict[str, list[str]]], list[tuple[str, str, str]]]] = {
    "sibling_before": (
        {"a_multi": {"ref": MULTI_REFS}, "b_single": {"pref": SINGLE_REFS}},
        [("p", "a_multi", "ref"), ("p", "b_single", "pref"), ("q", "a_multi", "ref")],
    ),
    "sibling_after": (
        {"a_multi": {"ref": MULTI_REFS}, "z_single": {"pref": SINGLE_REFS}},
        [("p", "a_multi", "ref"), ("p", "z_single", "pref"), ("q", "a_multi", "ref")],
    ),
    "same_table": (
        {"a_multi": {"ref": MULTI_REFS, "pref": [*SINGLE_REFS, "p3"]}},
        [("p", "a_multi", "ref"), ("p", "a_multi", "pref"), ("q", "a_multi", "ref")],
    ),
    "second_group": (
        {"a_multi": {"ref": MULTI_REFS}, "z_multi": {"ref": list(reversed(MULTI_REFS))}},
        [
            ("p", "a_multi", "ref"),
            ("p", "z_multi", "ref"),
            ("q", "a_multi", "ref"),
            ("q", "z_multi", "ref"),
        ],
    ),
}


def _key(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": "ns", "deterministic": True}


def _tables(shape: str) -> dict[str, pa.Table]:
    children, _ = _SHAPES[shape]
    s = pa.string()
    tables = {"p": pa.table({"k": pa.array(P_KEYS, s)}), "q": pa.table({"k": pa.array(Q_KEYS, s)})}
    for name, columns in children.items():
        rows = len(next(iter(columns.values())))
        tables[name] = pa.table(
            {"id": pa.array([str(i) for i in range(rows)], s)}
            | {col: pa.array(values, s) for col, values in columns.items()}
        )
    return tables


def _config(tmp_path: Path, shape: str) -> dict[str, Any]:
    children, edges = _SHAPES[shape]
    names = ["p", "q", *children]
    tables = [{"name": "p", "columns": [_key("k")]}, {"name": "q", "columns": [_key("k")]}]
    tables += [
        {"name": t, "columns": [{"name": "id", "strategy": "passthrough"}, *map(_key, cols)]}
        for t, cols in children.items()
    ]
    relationships = [
        {
            "parent": {"table": parent, "columns": ["k"]},
            "children": [{"table": t, "columns": [c]} for p, t, c in edges if p == parent],
            "orphan_policy": "preserve",
            "namespace": "ns",
        }
        for parent in ("p", "q")
    ]
    return {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": {
            t: {
                "type": "file",
                "format": "csv",
                "has_header": True,
                "path": str(tmp_path / f"{t}.csv"),
            }
            for t in names
        },
        "tables": tables,
        "targets": {
            t: {"type": "file", "format": "csv", "path": str(tmp_path / f"{t}.out.csv")}
            for t in names
        },
        "relationships": relationships,
        "namespaces": {},
    }


def _run(
    tmp_path: Path, shape: str, mode: Literal["out_of_core", "full_frame"], *, sink: bool = False
) -> dict[str, pa.Table]:
    tables = _tables(shape)
    for name, table in tables.items():
        pcsv.write_csv(table, tmp_path / f"{name}.csv")
    cfg = PipelineConfig.model_validate(_config(tmp_path, shape)).model_dump()
    published = ParquetTransactionalSink(tmp_path / f"published-{mode}") if sink else None
    result = run_pipeline(
        cfg,
        tables,
        engine_version="test",
        execution_mode=mode,
        sink=published,
    )
    assert result.quality_metrics["execution"]["execution_mode"] == mode
    if published is None:
        return dict(result.outputs)
    return {t: pq.read_table(published.committed_table_path(t)) for t in tables}


@pytest.mark.parametrize("sink", [False, True], ids=["resident", "sink"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_every_edge_of_a_shared_parent_relation_joins_after_a_multi_parent_merge(
    tmp_path: Path, shape: str, sink: bool
) -> None:
    out = _run(tmp_path, shape, "out_of_core", sink=sink)
    oracle = _run(tmp_path, shape, "full_frame")
    source = _tables(shape)
    masked = {
        key: value
        for parent in ("p", "q")
        for key, value in zip(
            source[parent].column("k").to_pylist(), out[parent].column("k").to_pylist(), strict=True
        )
    }
    _, edges = _SHAPES[shape]
    for _, child, column in edges:
        refs = source[child].column(column).to_pylist()
        assert out[child].num_rows == len(refs)
        assert out[child].column(column).to_pylist() == [masked[r] for r in refs]
    for name in source:
        assert out[name].to_pylist() == oracle[name].to_pylist(), name


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_no_relation_file_outlives_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool
) -> None:
    # The run's arena is held past the run (the test reclaims it; collection does if an assert
    # fails first), so what the runner leaves inside its root is visible.
    kept: list[ScratchArena] = []

    class _KeptArena(ScratchArena):
        @classmethod
        def open(cls, *, under: str | os.PathLike[str] | None = None) -> ScratchArena:
            arena = ScratchArena.open(under=under)
            monkeypatch.setattr(arena, "reclaim", lambda: None)
            kept.append(arena)
            return arena

    monkeypatch.setattr(_runner, "ScratchArena", _KeptArena)
    if fail:
        real_decide = _runner.decide_route

        def decide_then_fail_on_the_last_child(edges: Any, *args: Any, **kwargs: Any) -> Any:
            # Fails after the merge kept the shared file for the sibling that never runs.
            if any(edge.child_table == "z_single" for edge in edges):
                raise RuntimeError("route failed")
            return real_decide(edges, *args, **kwargs)

        monkeypatch.setattr(_runner, "decide_route", decide_then_fail_on_the_last_child)
        with pytest.raises(RuntimeError, match="route failed"):
            _run(tmp_path, "sibling_after", "out_of_core")
    else:
        _run(tmp_path, "sibling_after", "out_of_core")
    assert len(kept) == 1
    work = Path(kept[0].local_path())
    assert work.is_dir()
    assert sorted(p.relative_to(work).as_posix() for p in work.rglob("*.parquet")) == []
    assert not (work / "relations").exists()
    ScratchArena.reclaim(kept[0])
