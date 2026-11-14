"""Builders for the RUN-STREAM engine slices (docs/plans/2026-11-08-run-stream.md, section 8)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a

ENGINE_VERSION = "run-stream-test"
PARQUET_WRITER_CAP = 1_048_576
CANARY = "CANARY-CELL-VALUE-91357"


def two_column_table(rows: int, tag: str = "v") -> pa.Table:
    """`r` is redacted, `p` passes through: cheap to mask at millions of rows."""
    return pa.table(
        {
            "r": pa.array([f"{tag}{i}" for i in range(rows)], pa.string()),
            "p": pa.array([f"p{i}" for i in range(rows)], pa.string()),
        }
    )


def single_table_job(
    tmp_path: Path, table: pa.Table, columns: list[dict[str, Any]] | None = None
) -> tuple[dict[str, Any], LazySource]:
    path = support.write_source(table, tmp_path / "t.parquet")
    cols = columns or [support.redact_col("r"), support.pass_col("p")]
    return support.make_cfg(cols, path=path), LazySource(Path(path))


def run_stream(cfg: dict[str, Any], sources: Mapping[str, Any], sink: Any, **extra: Any) -> Any:
    """The strict call: default routing knobs, so the 100,000-row threshold is in force."""
    return run_pipeline(
        cfg,
        sources=sources,
        sink=sink,
        engine_version=ENGINE_VERSION,
        execution_mode="stream",
        **extra,
    )


def reference(cfg: dict[str, Any], table: pa.Table) -> pa.Table:
    """The resident full-frame result of the same job."""
    result = run_pipeline(
        cfg,
        sources={support.TABLE: table},
        engine_version=ENGINE_VERSION,
        auto_chunk=False,
    )
    return result.outputs[support.TABLE]


def row_groups(path: Path) -> list[int]:
    meta = pq.ParquetFile(path).metadata
    return [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]


def expected_groups(rows: int, chunk: int) -> list[int]:
    """Row groups of base `--chunked`: each chunk written on its own, split at the writer cap."""
    out: list[int] = []
    for start in range(0, rows, chunk):
        left = min(chunk, rows - start)
        while left > 0:
            out.append(min(left, PARQUET_WRITER_CAP))
            left -= out[-1]
    return out


def per_chunk_file(table: pa.Table, chunk: int, path: Path) -> bytes:
    """What base `--chunked` writes: one `ParquetWriter`, one `write_table` per chunk."""
    with pq.ParquetWriter(path, table.schema) as writer:
        for start in range(0, table.num_rows, chunk):
            writer.write_table(table.slice(start, chunk))
    return path.read_bytes()


def real_sink(tmp_path: Path, name: str = "out") -> tuple[b6a.RecordingSink, Path]:
    target = tmp_path / name
    return b6a.RecordingSink(ParquetTransactionalSink(target)), target


def engine_sink(tmp_path: Path, name: str = "out") -> tuple[ParquetTransactionalSink, Path]:
    """The engine's own sink class, unwrapped. A keyed run reads its staged output back only from
    this exact class; `real_sink` wraps it, which is a foreign producer and keeps the run
    resident."""
    target = tmp_path / name
    return ParquetTransactionalSink(target), target


def fk_job(
    tmp_path: Path,
    *,
    orphan_policy: str = "remap",
    child_strategy: str = "hash",
    rows: int = 30,
) -> tuple[dict[str, Any], dict[str, LazySource], dict[str, pa.Table]]:
    """customers(id) -> orders(customer_id): the chunked FK self-mask shape, parquet on disk."""
    customers = pa.table({"id": pa.array([f"C{i}" for i in range(rows)], pa.string())})
    orders = pa.table(
        {"customer_id": pa.array([f"C{i % 10}" for i in range(rows)] + ["C-orphan"], pa.string())}
    )
    tables = {"customers": customers, "orders": orders}
    paths = {n: support.write_source(t, tmp_path / f"{n}.parquet") for n, t in tables.items()}
    child: dict[str, Any] = {"name": "customer_id", "strategy": child_strategy, "dtype": "string"}
    if child_strategy == "hash":
        child["namespace"] = "id_ns"
    cfg = PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {
                n: {"type": "file", "format": "parquet", "path": p} for n, p in paths.items()
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "id", "strategy": "hash", "namespace": "id_ns", "dtype": "string"}
                    ],
                },
                {"name": "orders", "columns": [child]},
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": orphan_policy,
                    "namespace": "id_ns",
                }
            ],
            "targets": {
                n: {"type": "file", "format": "parquet", "path": "/dev/null"} for n in tables
            },
        }
    ).model_dump()
    lazy = {n: LazySource(Path(p)) for n, p in paths.items()}
    return cfg, lazy, tables
