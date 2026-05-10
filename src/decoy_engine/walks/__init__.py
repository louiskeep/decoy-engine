"""Walks engine package.

Pure-function PK/FK inference, hazard detection, ER graph construction,
and schema-drift comparison. No DB I/O — input is `SchemaSnapshot`,
output is `WalkResult` / `DriftResult`. The platform layer
(`forge-platform/api/aqueduct/walks/`) wires these into the runner;
the snapshotter that produces `SchemaSnapshot` from a live DB connection
also lives platform-side (or in a future engine `connectors/snapshotter.py`).

Public API — only these names are stable:

    from decoy_engine.walks import (
        SchemaSnapshot, Table, Column, Edge, Hazard, WalkResult, DriftResult,
        infer_edges, detect_hazards, build_er_graph, compare,
    )

Everything else under `walks/` is an implementation detail and may change.
"""
from decoy_engine.walks.types import (
    Column,
    DriftResult,
    Edge,
    Hazard,
    SchemaSnapshot,
    Table,
    WalkResult,
)
from decoy_engine.walks.diff import compare
from decoy_engine.walks.graph import ERGraph, build_er_graph
from decoy_engine.walks.hazards import detect_hazards
from decoy_engine.walks.inference import infer_edges

__all__ = [
    "Column",
    "DriftResult",
    "Edge",
    "ERGraph",
    "Hazard",
    "SchemaSnapshot",
    "Table",
    "WalkResult",
    "build_er_graph",
    "compare",
    "detect_hazards",
    "infer_edges",
]
