"""email_mask on every execution route (AT-6) and the Python-only guard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.errors import ConfigError
from decoy_engine.execution._chunked_fk_vocab import CHUNK_SAFE_STRATEGIES
from decoy_engine.execution._operator_registry import OPERATORS
from decoy_engine.execution.native._requirements import (
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
)
from decoy_engine.execution.out_of_core import _compat
from tests.unit.email_mask import _support as s

VALUES = [v for v in s.MIXED if isinstance(v, str) or v is None] * 3


def test_email_mask_is_python_only() -> None:
    assert "email_mask" not in OPERATORS
    assert "email_mask" not in CHUNK_SAFE_STRATEGIES
    assert "email_mask" not in _compat._SUPPORTED_WORK_STRATEGIES
    assert "email_mask" not in NATIVE_KERNEL_STRATEGIES
    assert "email_mask" not in NATIVE_POOL_STRATEGIES


@pytest.mark.parametrize("keep", ["domain", "domain_class"])
def test_single_table_routes_agree(keep: str, tmp_path: Path) -> None:
    reference = s.run_single(VALUES, tmp_path, keep)
    assert s.run_single(VALUES, tmp_path, keep, unified_slice_enabled=False) == reference
    assert (
        s.run_single(VALUES, tmp_path, keep, auto_chunk=True, auto_chunk_threshold_rows=1)
        == reference
    )


def _fk_config(tmp_path: Path, keep: str) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    n = len(VALUES)
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)]),
            "email": pa.array(VALUES, type=pa.string()),
        }
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(n)]),
            "parent_id": pa.array([f"p{i}" for i in range(n)]),
            "email": pa.array(list(reversed(VALUES)), type=pa.string()),
        }
    )
    email = s.column(keep)
    raw = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": {
            "parent": {
                "type": "file",
                "format": "parquet",
                "path": s.write(parent, tmp_path / "p.parquet"),
            },
            "child": {
                "type": "file",
                "format": "parquet",
                "path": s.write(child, tmp_path / "c.parquet"),
            },
        },
        "targets": {
            name: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{name}.out.parquet"),
            }
            for name in ("parent", "child")
        },
        "tables": [
            {
                "name": "parent",
                "columns": [{"name": "id", "strategy": "hash", "namespace": "ns"}, email],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "id", "strategy": "redact"},
                    {"name": "parent_id", "strategy": "hash", "namespace": "ns"},
                    email,
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump(), {"parent": parent, "child": child}


def _emails(result: Any) -> dict[str, list[Any]]:
    return {t: result.outputs[t].column("email").to_pylist() for t in ("parent", "child")}


@pytest.mark.parametrize("keep", ["domain", "domain_class"])
def test_fk_routes_agree(keep: str, tmp_path: Path) -> None:
    cfg, sources = _fk_config(tmp_path, keep)

    def run(**kwargs: Any) -> Any:
        return run_pipeline(cfg, sources=sources, engine_version=s.ENGINE_VERSION, **kwargs)

    full = run(execution_mode="full_frame")
    seq = run(execution_mode="sequential")
    auto = run(out_of_core_threshold_rows=1)
    assert seq.quality_metrics["execution"]["execution_mode"] == "sequential"
    assert _emails(seq) == _emails(full) == _emails(auto)
    # The same address masks the same way in both tables (one namespace, one key).
    assert sorted(x for x in _emails(full)["parent"] if x) == sorted(
        x for x in _emails(full)["child"] if x
    )


def test_forced_out_of_core_refuses_with_an_existing_code(tmp_path: Path) -> None:
    cfg, sources = _fk_config(tmp_path, "domain")
    with pytest.raises(ConfigError, match="out_of_core_strategy_unsupported"):
        run_pipeline(
            cfg, sources=sources, engine_version=s.ENGINE_VERSION, execution_mode="out_of_core"
        )
