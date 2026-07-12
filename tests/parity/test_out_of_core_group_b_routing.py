"""SC3 routing-level parity + SC2 carry-forward M1.

`test_out_of_core_group_b_parity.py` pins the adapter-boundary parity of the
ported Group (b) strategies. This file pins the LAYER ABOVE it:

1. `run_pipeline` actually DISPATCHES an eligible large FK job carrying a
   Group (b) payload (fpe) to the out-of-core route and its output is
   byte-identical to the full-frame path.
2. DE-09 (superseding SC2 carry-forward M1): the missing-source branch in
   `_pipeline_route_exec.run_out_of_core_route` (reached via the forced
   `execution_mode="out_of_core"` escape hatch when `sources` are not resident)
   resolves each Parquet-backed table to a streamed `LazySource` rather than
   eagerly loading it through `source_loader`, and still reaches byte-parity
   with the resident oracle. The direct mechanism proof (LazySource vs resident
   `pa.Table`, honest telemetry, non-Parquet resident fallback) lives in
   `tests/unit/execution/test_public_out_of_core_lazy.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._pipeline import run_pipeline

_N = 40


def _src(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _fpe_payload_fk_config(tmp_path: Path) -> dict[str, Any]:
    """Pure-mask parent->child FK job: hash FK keys (SC1-supported) plus an fpe
    payload column on each table (SC3 Group (b))."""
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "ssn": pa.array([f"{100000000 + i}" for i in range(_N)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "pid": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "acct": pa.array([f"{200000000 + i}" for i in range(_N)], type=pa.string()),
        }
    )
    tables = {"parent": parent, "child": child}
    return {
        "version": 1,
        "global_settings": {"job_name": "sc3-fpe-routing", "seed": 11},
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
                    {
                        "name": "ssn",
                        "strategy": "fpe",
                        "namespace": "pns",
                        "provider_config": {"charset": "digits"},
                    },
                ],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "cid", "strategy": "hash", "namespace": "cns"},
                    {"name": "pid", "strategy": "hash", "namespace": "pns"},
                    {
                        "name": "acct",
                        "strategy": "fpe",
                        "namespace": "cns",
                        "provider_config": {"charset": "digits"},
                    },
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


def test_auto_route_fpe_payload_matches_full_frame_oracle(tmp_path: Path) -> None:
    config = _fpe_payload_fk_config(tmp_path)
    sources = _sources(config)
    oracle = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    routed = run_pipeline(config, sources, engine_version="0.1.0", out_of_core_threshold_rows=10)
    assert routed.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert _pydict(routed.outputs) == _pydict(oracle.outputs)
    # The fpe payload really transformed (not passed through).
    assert (
        routed.outputs["parent"].column("ssn").to_pylist()
        != sources["parent"].column("ssn").to_pylist()
    )


def test_m1_forced_out_of_core_lazy_source_loader(tmp_path: Path) -> None:
    """DE-09 (superseding SC2 carry-forward M1): forced out-of-core with empty
    `sources` + a lazy `source_loader` no longer eagerly loads every table
    through the loader. Because every config source here is an on-disk Parquet
    file, the route resolves each missing table to a `LazySource` and STREAMS
    it -- the `source_loader` is bypassed entirely (its resident load was the
    input-residency defect DE-09 removes). Byte-parity with the resident
    full-frame oracle is unchanged: a `LazySource` reads the same on-disk
    Parquet the loader would have.

    Pre-DE-09 this test asserted the opposite -- `set(loaded) ==
    table_topo_order(plan, graph)` -- pinning the eager-loader behavior. See
    `tests/unit/execution/test_public_out_of_core_lazy.py` for the direct
    mechanism proof (LazySource, not resident `pa.Table`) and the resident
    `source_loader` fallback that still fires for a non-Parquet source."""
    config = _fpe_payload_fk_config(tmp_path)
    resident = _sources(config)

    oracle = run_pipeline(config, resident, engine_version="0.1.0", execution_mode="full_frame")

    loaded: list[str] = []

    def loader(table: str) -> pa.Table:
        loaded.append(table)
        return resident[table]

    forced = run_pipeline(
        config,
        sources={},
        engine_version="0.1.0",
        execution_mode="out_of_core",
        source_loader=loader,
    )

    assert forced.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    # DE-09: the Parquet-backed sources were lazified from their config paths,
    # so the resident loader was never called.
    assert loaded == [], f"source_loader should be bypassed for Parquet sources, got {loaded}"
    # Honest telemetry: nothing is held resident on this streamed run.
    assert forced.quality_metrics["execution"]["loaded_fully_in_memory"] is False

    assert _pydict(forced.outputs) == _pydict(oracle.outputs)
