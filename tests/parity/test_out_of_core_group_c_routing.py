"""SC4 routing-level parity: `run_pipeline` dispatches an eligible large FK job
carrying a Group (c) payload to the out-of-core route, byte-identical to full-frame.

`test_out_of_core_group_c_parity.py` pins the adapter-boundary parity of the
ported Group (c) strategies. This file pins the LAYER ABOVE it: the live router
(`decide_execution_route`) actually selects the out-of-core substrate for a
pure-mask FK job whose payload columns use a ported Group (c) strategy
(`text_mask`, `code_set` mask mode, `bucket_perturb` with an explicit
date_format), and the streamed output matches the full-frame oracle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._pipeline import run_pipeline

_N = 40


def _src(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


# Per case: (payload column strategy, provider_config, per-row source values, namespace-required).
# Every 10th value is None (row index % 10 == 0) so the router-level parity
# proof also pins nulls, not just the direct run_fk_out_of_core path.
_CASES: dict[str, tuple[str, dict[str, Any], list[str | None], bool]] = {
    "text_mask": (
        "text_mask",
        {"token": "[X]"},
        [
            None if i % 10 == 0 else f"ssn {100000000 + i}-{i:02d} call 415-555-{1000 + i}"
            for i in range(_N)
        ],
        False,
    ),
    "code_set": (
        "code_set",
        {"code_set": "mcc"},
        [None if i % 10 == 0 else f"src-value-{i}" for i in range(_N)],
        True,
    ),
    "bucket_perturb": (
        "bucket_perturb",
        {"bucket": "month", "date_format": "%Y-%m-%d"},
        [
            None if i % 10 == 0 else f"20{20 + (i % 4)}-0{1 + (i % 9)}-{10 + (i % 18):02d}"
            for i in range(_N)
        ],
        True,
    ),
}


def _payload_fk_config(
    tmp_path: Path, strategy: str, provider_config: dict[str, Any], values: list[str | None]
) -> dict[str, Any]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "pay": pa.array(values, type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "pid": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "cpay": pa.array(list(reversed(values)), type=pa.string()),
        }
    )
    tables = {"parent": parent, "child": child}

    def _col(name: str) -> dict[str, Any]:
        col: dict[str, Any] = {"name": name, "strategy": strategy, "namespace": "pns"}
        if provider_config:
            col["provider_config"] = dict(provider_config)
        return col

    return {
        "version": 1,
        "global_settings": {"job_name": f"sc4-{strategy}-routing", "seed": 11},
        "sources": {
            name: {"type": "file", "path": _src(tmp_path, tbl, name), "format": "parquet"}
            for name, tbl in tables.items()
        },
        "targets": {
            name: {
                "type": "file",
                "path": str(tmp_path / f"{name}.out.parquet"),
                "format": "parquet",
            }
            for name in tables
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {"name": "id", "strategy": "hash", "namespace": "pns"},
                    _col("pay"),
                ],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "cid", "strategy": "hash", "namespace": "cns"},
                    {"name": "pid", "strategy": "hash", "namespace": "pns"},
                    _col("cpay"),
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "pns",
            },
        ],
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _pydict(outputs: dict[str, pa.Table]) -> dict[str, dict[str, list[Any]]]:
    return {t: tbl.to_pydict() for t, tbl in outputs.items()}


@pytest.mark.parametrize("case", list(_CASES))
def test_auto_route_group_c_payload_matches_full_frame_oracle(tmp_path: Path, case: str) -> None:
    strategy, provider_config, values, _ns = _CASES[case]
    config = _payload_fk_config(tmp_path, strategy, provider_config, values)
    sources = _sources(config)
    oracle = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    routed = run_pipeline(config, sources, engine_version="0.1.0", out_of_core_threshold_rows=10)
    assert routed.quality_metrics["execution"]["execution_mode"] == "out_of_core", (
        f"{case}: expected out_of_core route, got "
        f"{routed.quality_metrics['execution']['execution_mode']}"
    )
    assert _pydict(routed.outputs) == _pydict(oracle.outputs)
    # The Group (c) payload really transformed (not passed through unchanged).
    assert (
        routed.outputs["parent"].column("pay").to_pylist()
        != sources["parent"].column("pay").to_pylist()
    )
