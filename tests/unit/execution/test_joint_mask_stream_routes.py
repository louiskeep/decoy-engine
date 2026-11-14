"""joint_mask draws one stream per group on the only route that runs it.

`joint_mask` is a cross-row strategy: the chunked and out-of-core routes decline it, so a
request for either falls back to the whole-frame route and the output is the same. The stream
seed carries the table name from the strategy context; an unstamped table is refused rather
than defaulted, since an empty name would merge every group of every table onto one stream.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._chunked import _CHUNK_ADMITTED_STRATEGIES
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution._strategies._joint_mask import JointMaskHandler
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.unit.execution import _auto_chunk_support as support

_REG = get_default_registry()
_N = 40
_REF = "us_zip5_city_state"


def _group(column: str) -> dict[str, Any]:
    return {
        "name": column,
        "strategy": "joint_mask",
        "provider_config": {"columns": [column], "reference": _REF, "key_by": "id", "mode": "gen"},
    }


def _job(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    table = pa.table(
        {
            "id": pa.array([f"P{i:04d}" for i in range(_N)]),
            "zip": pa.array(["00000"] * _N),
            "city": pa.array(["placeholder"] * _N),
        }
    )
    path = support.write_source(table, tmp_path / f"{support.TABLE}.parquet")
    columns = [{"name": "id", "strategy": "passthrough"}, _group("zip"), _group("city")]
    return support.make_cfg(columns, path=path), {support.TABLE: table}


def _run(tmp_path: Path, **kwargs: Any) -> Any:
    cfg, sources = _job(tmp_path)
    return run_pipeline(cfg, sources=sources, engine_version="joint-mask-streams", **kwargs)


def test_joint_mask_is_not_chunk_admitted() -> None:
    assert "joint_mask" not in _CHUNK_ADMITTED_STRATEGIES


def _seed(strategy: str, *, namespace: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=(),
        coherent_with=(),
    )


def test_the_out_of_core_gate_declines_joint_mask_with_its_coded_reason() -> None:
    key = _seed("hash", namespace="kns")
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x11" * 8,
            per_table=(
                (
                    "parent",
                    TableSeed(per_column=(("pk", key), ("pay", _seed("joint_mask"))), per_group=()),
                ),
                ("child", TableSeed(per_column=(("fk", key),), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    work = order_work(build_work_list(plan, _REG), graph)
    codes = [r.code for r in check_out_of_core_compatibility(plan, work, graph).rejections]
    assert "out_of_core_cross_row_strategy_unsupported" in codes


def test_gen_mode_groups_on_the_whole_frame_route_pick_different_rows(tmp_path: Path) -> None:
    """AT-E2-2: two gen groups of one table must not share one reference row index."""
    out = _run(tmp_path, execution_mode="full_frame").outputs[support.TABLE].to_pydict()
    from decoy_engine.reference_tables import load_table

    tbl = load_table(_REF)
    real = {(tbl.row(i)["zip"], tbl.row(i)["city"]) for i in range(tbl.row_count)}
    coupled = sum((z, c) in real for z, c in zip(out["zip"], out["city"], strict=True))
    assert coupled < _N // 2, f"{coupled} of {_N} rows picked one shared reference row"


def test_a_run_requested_out_of_core_falls_back_to_the_whole_frame_output(tmp_path: Path) -> None:
    whole = _run(tmp_path, execution_mode="full_frame")
    asked = _run(tmp_path, out_of_core_threshold_rows=1, auto_chunk_threshold_rows=1)
    assert asked.quality_metrics["execution"]["execution_mode"] != "out_of_core"
    assert asked.outputs[support.TABLE].equals(whole.outputs[support.TABLE])


def _ctx(table: str, frame: pd.DataFrame) -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=b"\x01" * 8,
        mask_key=b"\xca\xfe" * 16,
        group_anchor_snapshots={(table, "anchor"): frame["anchor"].copy()},
        current_table=table,
    )


def _handler_zips(table: str) -> list[str]:
    frame = pd.DataFrame({"anchor": [None] * 30, "zip": ["x"] * 30})
    seed = ColumnSeed(
        namespace="ns",
        strategy="joint_mask",
        provider="joint_mask",
        backend_type="transform",
        backend_version="v1",
        cardinality_mode="preserve",
        deterministic=False,
        provider_config=(("reference", _REF), ("columns", ["zip"]), ("key_by", "anchor")),
    )
    out, _ = JointMaskHandler().run(frame.copy(), "zip", seed, _ctx(table, frame))
    return out["zip"].tolist()


def test_the_handler_keys_the_stream_on_the_context_table() -> None:
    assert _handler_zips("patients") != _handler_zips("visits")
    assert _handler_zips("patients") == _handler_zips("patients")


def test_the_handler_refuses_an_unstamped_table() -> None:
    with pytest.raises(ValueError):
        _handler_zips("")
