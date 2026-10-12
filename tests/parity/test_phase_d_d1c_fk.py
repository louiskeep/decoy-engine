"""Phase D / D1c: preserve_values on a single-hop FK parent key keeps referential integrity.

The parent key owns the transform. A child row that matches a parent inherits the parent's
masked value, and an orphan under `orphan_policy: remap` is re-masked by the parent's own
handler, which preserves too. Unsupported shapes are rejected at compile time (see the config
suite); here the supported shape runs end to end through the real pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.plan._errors import PlanCompileError
from tests.parity._d1c_data import IN_RANGE, NORMAL, OPERATORS, OUT_OF_RANGE

_ORPHAN = "2011-11-11"


def _config(
    tmp_path: Path,
    strategy: str,
    pc: dict[str, Any],
    policy: str,
    sources: dict[str, pa.Table] | None = None,
) -> dict[str, Any]:
    # The declared source files must exist and be non-empty for the size probe; the run itself
    # reads the `sources` mapping.
    for name, table in (sources or _sources()).items():
        table.to_pandas().to_csv(tmp_path / f"{name}.csv", index=False)
    key_cfg = {**OPERATORS[strategy], "date_format": "%Y-%m-%d", **pc}
    raw = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            t: {"type": "file", "format": "csv", "path": str(tmp_path / f"{t}.csv")}
            for t in ("parent", "child")
        },
        "targets": {
            t: {"type": "file", "format": "csv", "path": str(tmp_path / f"{t}_out.csv")}
            for t in ("parent", "child")
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {
                        "name": "d",
                        "strategy": strategy,
                        "namespace": "fk_d",
                        "provider_config": key_cfg,
                    }
                ],
            },
            {"name": "child", "columns": [{"name": "fk", "strategy": "passthrough"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["d"]},
                "children": [{"table": "child", "columns": ["fk"]}],
                "orphan_policy": policy,
                "namespace": "fk_d",
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _sources() -> dict[str, pa.Table]:
    parent = [NORMAL[0], NORMAL[1], IN_RANGE, OUT_OF_RANGE, NORMAL[0]]
    child = [NORMAL[0], IN_RANGE, OUT_OF_RANGE, NORMAL[1], NORMAL[0], _ORPHAN]
    return {
        "parent": pa.table({"d": pa.array(parent, pa.string())}),
        "child": pa.table({"fk": pa.array(child, pa.string())}),
    }


def _run(tmp_path: Path, strategy: str, pc: dict[str, Any], policy: str) -> Any:
    return run_pipeline(
        _config(tmp_path, strategy, pc, policy),
        sources=_sources(),
        engine_version="d1c-fk",
        unified_slice_enabled=False,
    )


@pytest.mark.parametrize("strategy", sorted(OPERATORS))
@pytest.mark.parametrize("policy", ["preserve", "remap"])
def test_single_hop_parent_key_keeps_sentinels_and_referential_integrity(
    strategy: str, policy: str, tmp_path: Path
) -> None:
    keep = {"preserve_values": [IN_RANGE, OUT_OF_RANGE]}
    result = _run(tmp_path, strategy, keep, policy)
    parent = result.outputs["parent"].column("d").to_pylist()
    child = result.outputs["child"].column("fk").to_pylist()
    assert parent[2] == IN_RANGE and parent[3] == OUT_OF_RANGE
    assert child[1] == IN_RANGE and child[2] == OUT_OF_RANGE
    # Rows with a parent inherit exactly the parent's masked value.
    assert child[0] == parent[0] == parent[4] == child[4]
    assert child[3] == parent[1]
    assert set(child[:5]) <= set(parent)


@pytest.mark.parametrize("strategy", sorted(OPERATORS))
def test_orphan_sentinel_is_remapped_by_the_parent_handler_and_stays_a_sentinel(
    strategy: str, tmp_path: Path
) -> None:
    sources = _sources()
    sources["child"] = pa.table({"fk": pa.array([NORMAL[0], IN_RANGE, _ORPHAN], pa.string())})
    sources["parent"] = pa.table({"d": pa.array([NORMAL[0], NORMAL[1]], pa.string())})
    result = run_pipeline(
        _config(tmp_path, strategy, {"preserve_values": [IN_RANGE]}, "remap", sources),
        sources=sources,
        engine_version="d1c-fk",
        unified_slice_enabled=False,
    )
    child = result.outputs["child"].column("fk").to_pylist()
    assert child[1] == IN_RANGE
    assert child[2] != _ORPHAN


@pytest.mark.parametrize("strategy", sorted(OPERATORS))
def test_unset_fk_run_is_unchanged(strategy: str, tmp_path: Path) -> None:
    sources = {
        "parent": pa.table({"d": pa.array([NORMAL[0], NORMAL[1]], pa.string())}),
        "child": pa.table({"fk": pa.array([NORMAL[0], NORMAL[1]], pa.string())}),
    }

    def run(pc: dict[str, Any]) -> list[Any]:
        out = run_pipeline(
            _config(tmp_path, strategy, pc, "preserve", sources),
            sources=sources,
            engine_version="d1c-fk",
            unified_slice_enabled=False,
        )
        return [
            out.outputs["parent"].column("d").to_pylist(),
            out.outputs["child"].column("fk").to_pylist(),
        ]

    assert run({}) == run({"preserve_values": []}) == run({"preserve_values": ["1999-09-09"]})


@pytest.mark.parametrize("strategy", sorted(OPERATORS))
def test_preserve_values_on_the_child_key_is_a_compile_error(strategy: str, tmp_path: Path) -> None:
    config = _config(tmp_path, strategy, {}, "remap")
    config["tables"][1]["columns"] = [
        {
            "name": "fk",
            "strategy": strategy,
            "namespace": "c",
            "provider_config": {
                **OPERATORS[strategy],
                "date_format": "%Y-%m-%d",
                "preserve_values": [IN_RANGE],
            },
        }
    ]
    with pytest.raises(PlanCompileError) as caught:
        run_pipeline(config, sources=_sources(), engine_version="d1c-fk")
    assert caught.value.code == "date_preserve_values_unsupported_fk_shape"
