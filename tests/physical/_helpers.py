"""Shared fixture-building helpers for the physical-seam characterization
tests (Task 4.2, D3).

`build_job` reproduces the exact preflight sequence `run_pipeline` runs before
it calls any driver (`_pipeline.py`: `profile_source` -> `compile_plan` ->
`build_namespace_registry` -> `build_relationship_graph`), so a test can call
a driver adapter and its production delegate SIDE BY SIDE with the identical
`Plan`/`RelationshipGraph`/`NamespaceRegistry` a real `run_pipeline` call would
build, rather than routing through `run_pipeline` itself (which is what the
disconnection tests exercise instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.plan import compile_plan
from decoy_engine.plan._types import Plan
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import ProviderRegistry, get_default_registry
from decoy_engine.relationships import (
    RelationshipGraph,
    build_namespace_registry,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)
from decoy_engine.relationships._namespace import NamespaceRegistry

ENGINE_VERSION = "physical-seam-characterization"


def normalize_timing_fields(value: Any) -> Any:
    """Recursively replace any wall-clock timing leaf (a key ending in
    `_ms`/`_s` whose value is numeric) with a structural placeholder, so two
    independent real runs compare equal on everything EXCEPT elapsed time.
    Asserts the timing value is a non-negative number first (present +
    well-formed by structure/units, per plan D3) rather than silently
    swallowing a malformed one.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            if (
                isinstance(key, str)
                and (key.endswith("_ms") or key.endswith("_s"))
                and isinstance(val, (int, float))
                and not isinstance(val, bool)
            ):
                assert val >= 0, f"timing field {key!r} is negative: {val!r}"
                out[key] = "<timing>"
            else:
                out[key] = normalize_timing_fields(val)
        return out
    if isinstance(value, list):
        return [normalize_timing_fields(v) for v in value]
    if isinstance(value, tuple):
        return tuple(normalize_timing_fields(v) for v in value)
    return value


@dataclass(frozen=True)
class Job:
    """Everything a driver call needs, built the same way `run_pipeline` does."""

    config: dict[str, Any]
    sources: dict[str, pa.Table]
    plan: Plan
    graph: RelationshipGraph
    namespace_registry: NamespaceRegistry
    registry: ProviderRegistry


def write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def build_config(
    tmp_path: Path,
    tables: dict[str, pa.Table],
    table_specs: list[dict[str, Any]],
    *,
    relationships: list[dict[str, Any]] | None = None,
    seed: int = 20260914,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {
            name: {"type": "file", "format": "parquet", "path": write_source(tmp_path, tbl, name)}
            for name, tbl in tables.items()
        },
        "targets": {
            name: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{name}.out.parquet"),
            }
            for name in tables
        },
        "tables": table_specs,
    }
    if relationships is not None:
        raw["relationships"] = relationships
    return PipelineConfig.model_validate(raw).model_dump()


def build_job(
    tmp_path: Path, tables: dict[str, pa.Table], table_specs: list[dict[str, Any]], **kw: Any
) -> Job:
    config = build_config(tmp_path, tables, table_specs, **kw)
    registry = get_default_registry()
    job_seed = (config.get("global_settings") or {}).get("seed") or 0
    profile = profile_source(config, seed=job_seed)
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION)
    ns_registry = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships, namespace_registry=ns_registry, orphan_policy_lookup=lookup
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    return Job(
        config=config,
        sources=dict(tables),
        plan=plan,
        graph=graph,
        namespace_registry=ns_registry,
        registry=registry,
    )
