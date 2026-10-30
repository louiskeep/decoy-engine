"""A quarantined parent row must never leave children pointing at nothing (F06 r5 blocker).

Parse-stage quarantine drops the row before the parent-key maps are built, so ``orphan_policy:
fail`` fires. Format-stage quarantine drops it after masking, once the maps already saw it; no
route re-checks the children, so the engine refuses that combination up front instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.acceptance._f06_support import (
    INT64,
    PROTECTED_QUARANTINE,
    column,
    run,
    zero_pad,
)
from tests.acceptance.test_f06_route_parity import _runtime

ROUTES = ("full_frame", "sequential", "chunked", "unified_slice")
ROUTE_KWARGS: dict[str, dict[str, Any]] = {
    "full_frame": {
        "execution_mode": "full_frame",
        "auto_chunk": False,
        "unified_slice_enabled": False,
    },
    "sequential": {"execution_mode": "sequential", "auto_chunk": False},
    "chunked": {"auto_chunk": True, "chunk_size_rows": 1, "auto_chunk_threshold_rows": 1},
    "unified_slice": {"auto_chunk": False},
}
# parse: "x" is not an integer (no output_format, so only the parse stage can fail). format: "123" parses but overflows the width-2 zero-pad.
BAD_CELL = {"parse": "x", "format": "123"}
EXPECTED_CODE = {"parse": "orphan_fk_violation", "format": "declared_cast_route_unsupported"}


def _write(tmp_path: Path, name: str, data: pa.Table) -> tuple[dict[str, Any], dict[str, Any]]:
    path = str(tmp_path / f"{name}.parquet")
    pq.write_table(data, path)
    return (
        {"type": "file", "format": "parquet", "path": path},
        {"type": "file", "format": "parquet", "path": path + ".out"},
    )


def _config(tmp_path: Path, stage: str) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table(
        {
            "id": pa.array(["1", "2"], pa.string()),
            "amt": pa.array([BAD_CELL[stage], "5"], pa.string()),
        }
    )
    child = pa.table(
        {"cid": pa.array(["a", "b"], pa.string()), "pid": pa.array(["1", "2"], pa.string())}
    )
    psrc, pdst = _write(tmp_path, "parent", parent)
    csrc, cdst = _write(tmp_path, "child", child)
    amt = column(
        "amt",
        logical_type="integer",
        parse_format=INT64,
        error_policy="quarantine",
        **({"output_format": zero_pad(2)} if stage == "format" else {}),
    )
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"parent": psrc, "child": csrc},
        "tables": [
            {"name": "parent", "columns": [column("id", "hash", namespace="ns"), amt]},
            {"name": "child", "columns": [column("cid"), column("pid", "hash", namespace="ns")]},
        ],
        "targets": {"parent": pdst, "child": cdst},
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "fail",
                "namespace": "ns",
            }
        ],
        "namespaces": {},
        "protected_quarantine": PROTECTED_QUARANTINE,
    }
    return cfg, {"parent": parent, "child": child}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("stage", ["parse", "format"])
def test_quarantined_parent_row_never_orphans_its_children(
    tmp_path: Path, route: str, stage: str
) -> None:
    from decoy_engine.execution._errors import ExecutionError

    cfg, sources = _config(tmp_path, stage)
    try:
        result = run(
            cfg, sources, quarantine_runtime=_runtime(tmp_path / "q"), **ROUTE_KWARGS[route]
        )
    except ExecutionError as exc:
        assert str(exc.code) == EXPECTED_CODE[stage]
        return
    parents = result.outputs["parent"].num_rows
    pytest.fail(f"{route}/{stage} published: parent rows={parents}, completeness unchecked")


def test_auto_mode_refuses_before_any_read(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import DeclaredCastRouteError

    cfg, sources = _config(tmp_path, "format")
    with pytest.raises(DeclaredCastRouteError) as info:
        run(cfg, sources, quarantine_runtime=_runtime(tmp_path / "q"))
    assert str(info.value.code) == "declared_cast_route_unsupported"
    assert "post_mask_parent_row_quarantine" in str(info.value)


def test_format_quarantine_on_a_child_only_table_stays_admitted(tmp_path: Path) -> None:
    cfg, sources = _config(tmp_path, "format")
    amt = cfg["tables"][0]["columns"].pop(1)
    cfg["tables"][1]["columns"].append(amt)
    cfg["sources"]["child"] = cfg["sources"]["parent"]
    sources["child"] = sources["parent"].rename_columns(["pid", "amt"]).select(["pid", "amt"])
    sources["child"] = sources["child"].append_column("cid", pa.array(["a", "b"], pa.string()))
    pq.write_table(sources["child"], cfg["sources"]["child"]["path"].replace("parent", "child"))
    cfg["sources"]["child"]["path"] = cfg["sources"]["child"]["path"].replace("parent", "child")
    cfg["targets"]["child"]["path"] = cfg["sources"]["child"]["path"] + ".out"
    result = run(
        cfg, sources, quarantine_runtime=_runtime(tmp_path / "q"), execution_mode="sequential"
    )
    assert result.outputs["parent"].num_rows == 2
