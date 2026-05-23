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
from decoy_engine.walks.cross_file import (
    CrossFileWalkResult,
    infer_cross_file_edges,
    run_cross_file_walk,
    storm_profiles_to_snapshot,
)
from decoy_engine.walks.diff import compare
from decoy_engine.walks.graph import ERGraph, build_er_graph
from decoy_engine.walks.hazards import detect_hazards
from decoy_engine.walks.inference import infer_edges
from decoy_engine.walks.types import (
    Column,
    DriftResult,
    Edge,
    Hazard,
    SchemaSnapshot,
    Table,
    WalkResult,
)

__all__ = [
    "Column",
    "CrossFileWalkResult",
    "DriftResult",
    "ERGraph",
    "Edge",
    "Hazard",
    "SchemaSnapshot",
    "Table",
    "WalkResult",
    "build_er_graph",
    "compare",
    "detect_hazards",
    "infer_cross_file_edges",
    "infer_edges",
    "run_cross_file_walk",
    "storm_profiles_to_snapshot",
]
