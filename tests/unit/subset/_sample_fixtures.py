"""Config and table builders shared by the sample-request tests."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

KINDS = {"mask": "columns", "generate": "generate_columns"}


def config(
    tables: dict[str, str],
    edges: list[tuple[str, str, str, str]],
    seeds: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`tables` maps a name to "mask" or "generate"; an edge is (parent, pcol, child, ccol)."""
    cfg: dict[str, Any] = {
        "tables": [{"name": n, KINDS[k]: [{"name": "x"}]} for n, k in tables.items()],
        "relationships": [
            {
                "parent": {"table": p, "columns": [pc]},
                "children": [{"table": c, "columns": [cc]}],
                "orphan_policy": "preserve",
            }
            for p, pc, c, cc in edges
        ],
    }
    if seeds is not None:
        cfg["subset"] = {"seeds": seeds}
        if budget is not None:
            cfg["subset"]["budget"] = budget
    return cfg


def parent_child(parent: str, child: str, parents: int = 6, children: int = 12) -> dict[str, Any]:
    ids = [f"{parent}{i}" for i in range(parents)]
    kids = {
        "cid": [f"{child}{i}" for i in range(children)],
        "pid": [ids[i % parents] for i in range(children)],
    }
    return {parent: pa.table({"id": ids}), child: pa.table(kids)}


def orphans(tables: dict[str, pa.Table], kept: dict[str, frozenset[int]], pairs) -> int:
    """Child rows kept whose parent key is not kept; `pairs` is (parent, pcol, child, ccol)."""
    missing = 0
    for p, pc, c, cc in pairs:
        have = {tables[p][pc][i].as_py() for i in kept[p]}
        missing += sum(1 for i in kept[c] if tables[c][cc][i].as_py() not in have)
    return missing
