"""Generated relational matrix for F06 parent-row quarantine (catalog classes 1, 4 and 5).

Every axis is read from the code, never typed out: the route lanes from
``_eligibility.ROUTES`` plus the multi-table split, the forced modes from ``run_pipeline``'s
``execution_mode`` annotation, the orphan policies from ``OrphanPolicyLiteral``. The FK shapes
are the ones the relationship schema can express: an endpoint of one column or several, a
parent equal to its child, a child that is itself a declared-casting table, and children that
land in a later batch or chunk than their parent row.

The raw parent key of the quarantined row is a seeded marker, so a cell that leaks it (the
``preserve`` defect) is visible on every surface the marker test scans.
"""

from __future__ import annotations

import ast
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from tests.acceptance._f06_support import INT64, PROTECTED_QUARANTINE, column, zero_pad

MARKER = "SEEDPKQ7"
BAD_CELL = {"parse": "SEEDBADX", "format": "123"}
STAGES = ("parse", "format")


def route_lanes() -> tuple[str, ...]:
    from decoy_engine.declared_casting._eligibility import ROUTES

    # The multi-table split is the one lane that is not a declared-casting route owner.
    return (*ROUTES, "multi_table")


def forced_modes() -> tuple[str, ...]:
    from decoy_engine.execution._pipeline import run_pipeline

    # Only this one annotation is evaluated: the others name TYPE_CHECKING-only imports.
    hint = inspect.signature(run_pipeline).parameters["execution_mode"].annotation
    if not isinstance(hint, str):
        return tuple(m for m in typing.get_args(hint) if m != "auto")
    node = ast.parse(hint, mode="eval").body
    assert isinstance(node, ast.Subscript) and ast.unparse(node.value) == "Literal", hint
    return tuple(m for m in ast.literal_eval(node.slice) if m != "auto")


def orphan_policies() -> tuple[str, ...]:
    from decoy_engine.config._relationships import OrphanPolicyLiteral

    return typing.get_args(OrphanPolicyLiteral)


# Flags that steer ``auto`` toward a lane. Only a lane's own switches are named; whether the lane
# is then taken is what the executed-route assertion records.
_AUTO_STEER: dict[str, dict[str, Any]] = {
    "full_frame": {"auto_chunk": False, "unified_slice_enabled": False},
    "sequential": {"auto_chunk": False, "unified_slice_enabled": False},
    "chunked": {"auto_chunk": True, "chunk_size_rows": 1, "auto_chunk_threshold_rows": 1},
    "unified_slice": {"auto_chunk": False, "unified_slice_enabled": True},
    "out_of_core": {"out_of_core_threshold_rows": 1},
    "mixed": {},
    "multi_table": {"multi_table_dispatch_enabled": True},
}


@dataclass(frozen=True)
class Request:
    lane: str
    mode: str  # "auto" or the forced execution_mode

    @property
    def id(self) -> str:
        return f"{self.lane}-{self.mode}"

    def kwargs(self) -> dict[str, Any]:
        if self.mode == "auto":
            return dict(_AUTO_STEER[self.lane])
        return {"execution_mode": self.mode}


def requests() -> tuple[Request, ...]:
    from decoy_engine.execution._pipeline import run_pipeline

    params = inspect.signature(run_pipeline).parameters
    for steer in _AUTO_STEER.values():
        assert set(steer) <= set(params), steer  # a renamed switch must not silently vanish
    assert set(_AUTO_STEER) == set(route_lanes())
    forced = forced_modes()
    out = [Request(lane, "auto") for lane in route_lanes()]
    out += [Request(lane, lane) for lane in route_lanes() if lane in forced]
    return tuple(out)


@dataclass(frozen=True)
class Job:
    config: dict[str, Any]
    sources: dict[str, pa.Table]
    parent: tuple[str, tuple[str, ...]]
    child: tuple[str, tuple[str, ...]]


def _write(root: Path, name: str, data: pa.Table) -> tuple[dict[str, Any], dict[str, Any]]:
    path = str(root / f"{name}.parquet")
    pq.write_table(data, path)
    return (
        {"type": "file", "format": "parquet", "path": path},
        {"type": "file", "format": "parquet", "path": path + ".out"},
    )


def _amt(stage: str, policy: str) -> dict[str, Any]:
    return column(
        "amt",
        logical_type="integer",
        parse_format=INT64,
        error_policy=policy,
        **({"output_format": zero_pad(2)} if stage == "format" else {}),
    )


def _strings(**cols: list[Any]) -> pa.Table:
    return pa.table({k: pa.array(v, pa.string()) for k, v in cols.items()})


def _assemble(
    root: Path,
    tables: dict[str, tuple[pa.Table, list[dict[str, Any]]]],
    parent: tuple[str, tuple[str, ...]],
    child: tuple[str, tuple[str, ...]],
    orphan_policy: str,
    quarantine: bool,
) -> Job:
    sources: dict[str, Any] = {}
    targets: dict[str, Any] = {}
    for name, (data, _) in tables.items():
        sources[name], targets[name] = _write(root, name, data)
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": sources,
        "tables": [{"name": n, "columns": cols} for n, (_, cols) in tables.items()],
        "targets": targets,
        "relationships": [
            {
                "parent": {"table": parent[0], "columns": list(parent[1])},
                "children": [{"table": child[0], "columns": list(child[1])}],
                "orphan_policy": orphan_policy,
                "namespace": "ns",
            }
        ],
        "namespaces": {},
        **({"protected_quarantine": PROTECTED_QUARANTINE} if quarantine else {}),
    }
    return Job(cfg, {n: t for n, (t, _) in tables.items()}, parent, child)


def _hash(name: str) -> dict[str, Any]:
    return column(name, "hash", namespace="ns")


def single(root: Path, stage: str, policy: str, bad: str, ep: str) -> Job:
    par = _strings(id=[MARKER, "2"], amt=[bad, "5"])
    chi = _strings(cid=["a", "b"], pid=[MARKER, "2"])
    tables = {
        "parent": (par, [_hash("id"), _amt(stage, ep)]),
        "child": (chi, [column("cid"), _hash("pid")]),
    }
    return _assemble(
        root, tables, ("parent", ("id",)), ("child", ("pid",)), policy, ep == "quarantine"
    )


def composite(root: Path, stage: str, policy: str, bad: str, ep: str) -> Job:
    par = _strings(a=[MARKER, "2"], b=["x", "y"], amt=[bad, "5"])
    chi = _strings(cid=["c1", "c2"], pa=[MARKER, "2"], pb=["x", "y"])
    tables = {
        "parent": (par, [_hash("a"), _hash("b"), _amt(stage, ep)]),
        "child": (chi, [column("cid"), _hash("pa"), _hash("pb")]),
    }
    return _assemble(
        root, tables, ("parent", ("a", "b")), ("child", ("pa", "pb")), policy, ep == "quarantine"
    )


def self_referencing(root: Path, stage: str, policy: str, bad: str, ep: str) -> Job:
    emp = _strings(id=[MARKER, "2", "3"], mgr=[None, MARKER, "2"], amt=[bad, "5", "6"])
    tables = {"emp": (emp, [_hash("id"), _hash("mgr"), _amt(stage, ep)])}
    return _assemble(root, tables, ("emp", ("id",)), ("emp", ("mgr",)), policy, ep == "quarantine")


def cross_declared(root: Path, stage: str, policy: str, bad: str, ep: str) -> Job:
    par = _strings(id=[MARKER, "2"], amt=[bad, "5"])
    chi = _strings(cid=["a", "b"], pid=[MARKER, "2"], qty=["1", "2"])
    qty = column("qty", logical_type="integer", parse_format=INT64, error_policy="null")
    tables = {
        "parent": (par, [_hash("id"), _amt(stage, ep)]),
        "child": (chi, [column("cid"), _hash("pid"), qty]),
    }
    return _assemble(
        root, tables, ("parent", ("id",)), ("child", ("pid",)), policy, ep == "quarantine"
    )


def later_chunk(root: Path, stage: str, policy: str, bad: str, ep: str) -> Job:
    keys = [MARKER, "2", "3", "4", "5", "6"]
    par = _strings(id=keys, amt=[bad, "5", "6", "7", "8", "9"])
    # The only child of the quarantined parent is the last row, so any batching puts it in a
    # later batch than the parent row.
    chi = _strings(cid=list("abcdef"), pid=["6", "5", "4", "3", "2", MARKER])
    tables = {
        "parent": (par, [_hash("id"), _amt(stage, ep)]),
        "child": (chi, [column("cid"), _hash("pid")]),
    }
    return _assemble(
        root, tables, ("parent", ("id",)), ("child", ("pid",)), policy, ep == "quarantine"
    )


Shape = Callable[[Path, str, str, str, str], Job]
SHAPES: dict[str, Shape] = {
    "single": single,
    "composite": composite,
    "self_referencing": self_referencing,
    "cross_declared": cross_declared,
    "later_chunk": later_chunk,
}


def shape_coverage(jobs: dict[str, Job]) -> set[str]:
    """The FK shapes the jobs actually express, derived from their relationship config."""
    from decoy_engine.declared_casting._eligibility import _columns_from_config

    seen: set[str] = set()
    for job in jobs.values():
        rel = job.config["relationships"][0]
        p, c = rel["parent"], rel["children"][0]
        seen.add("composite" if len(p["columns"]) > 1 else "single_column")
        if p["table"] == c["table"]:
            seen.add("self_referencing")
        declared = {col.table for col in _columns_from_config(_validated(job.config))}
        if c["table"] != p["table"] and c["table"] in declared:
            seen.add("cross_declared")
        child_rows = job.sources[c["table"]].column(c["columns"][0]).to_pylist()
        if child_rows.index(MARKER) > 0:
            seen.add("later_chunk")
    return seen


def _validated(raw: dict[str, Any]) -> dict[str, Any]:
    from tests.acceptance._f06_support import validate

    return validate(raw)


def executed_route(quality_metrics: dict[str, Any]) -> str:
    """The lane that actually ran, from the result's own metrics (never from the request)."""
    if "unified_slice_activation" in quality_metrics:
        return "unified_slice"
    if "mixed_route" in quality_metrics:
        return "mixed"
    if (quality_metrics.get("auto_chunk") or {}).get("mode") == "chunked":
        return "chunked"
    if "multi_table_dispatch" in quality_metrics:
        return "multi_table"
    return str(quality_metrics["execution"]["execution_mode"])
