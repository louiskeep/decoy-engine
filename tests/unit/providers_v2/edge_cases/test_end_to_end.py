"""Edge-case providers end to end: a `strategy: faker` column, full-frame and chunked."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import DecoyError
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._chunked import concat_masked_chunks, run_mask_pipeline_chunked
from decoy_engine.providers_v2.edge_cases import EDGE_CASE_SPECS

SPECS = {spec.provider: spec for spec in EDGE_CASE_SPECS}
_ARROW = {
    "edge_case_string": pa.string(),
    "edge_case_unicode": pa.string(),
    "edge_case_integer": pa.int64(),
    "edge_case_float": pa.float64(),
    "edge_case_date": pa.date32(),
    "edge_case_datetime": pa.timestamp("ns", tz="UTC"),
}
_CHUNK = 50


def _config(provider: str, path: Path, **column: Any) -> dict[str, Any]:
    col = {
        "name": "c",
        "strategy": "faker",
        "provider": provider,
        "deterministic": True,
        "namespace": "ns_edge_case",
        "pool_size": len(SPECS[provider].cases),
        **column,
    }
    return PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "tables": [{"name": "t", "columns": [col]}],
            "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


def _source(tmp_path: Path, values: list[Any]) -> tuple[pa.Table, Path]:
    table = pa.table({"c": pa.array(values, type=pa.string())})
    path = tmp_path / "t.parquet"
    pq.write_table(table, path)
    return table, path


def _run(config: dict[str, Any], source: pa.Table, **kw: Any) -> Any:
    return run_pipeline(config, sources={"t": source}, engine_version="edge-test", **kw)


def _chunked(config: dict[str, Any], source: pa.Table) -> list[pa.Table]:
    chunks = [source.slice(i, _CHUNK) for i in range(0, source.num_rows, _CHUNK)]
    return list(run_mask_pipeline_chunked(config, chunks, table="t", engine_version="edge-test"))


def _reprs(values: Any) -> set[str]:
    return {repr(v) for v in values}


def _catalogue(name: str) -> set[str]:
    # Read back through Arrow so values compare in the form the output column returns.
    return _reprs(pa.array(SPECS[name].values, type=_ARROW[name]).to_pylist())


@pytest.mark.parametrize("name", sorted(SPECS))
def test_deterministic_column_full_frame_and_chunked(name: str, tmp_path: Path) -> None:
    source, path = _source(tmp_path, [f"real-{i % 200}" for i in range(2000)])
    config = _config(name, path)
    first = _run(config, source).outputs["t"].column("c")
    again = _run(config, source).outputs["t"].column("c")
    values = first.to_pylist()
    assert first.type == _ARROW[name]
    assert values == again.to_pylist()
    assert _reprs(values) <= _catalogue(name)
    by_source: dict[str, set[str]] = {}
    for src, out in zip(source.column("c").to_pylist(), values, strict=True):
        by_source.setdefault(src, set()).add(repr(out))
    assert all(len(outs) == 1 for outs in by_source.values())

    chunks = _chunked(config, source)
    assert {chunk.schema.field("c").type for chunk in chunks} == {_ARROW[name]}
    assert concat_masked_chunks(chunks, table="t").column("c").to_pylist() == values


@pytest.mark.parametrize("name", sorted(SPECS))
def test_non_deterministic_reuse_column(name: str, tmp_path: Path) -> None:
    source, path = _source(tmp_path, [f"real-{i}" for i in range(300)])
    config = _config(name, path, deterministic=False, cardinality_mode="reuse")
    out = _run(config, source).outputs["t"].column("c")
    assert out.type == _ARROW[name]
    assert _reprs(out.to_pylist()) <= _catalogue(name)


def test_nullable_integer_column_is_a_known_failure(tmp_path: Path) -> None:
    # Nulls only in the first chunk: that chunk becomes float64, the others stay int64.
    values = [None if i < _CHUNK and i % 5 == 0 else f"real-{i % 200}" for i in range(2000)]
    source, path = _source(tmp_path, values)
    config = _config("edge_case_integer", path)
    assert _run(config, source).outputs["t"].column("c").type == pa.float64()
    chunks = _chunked(config, source)
    assert {str(chunk.schema.field("c").type) for chunk in chunks} == {"double", "int64"}
    with pytest.raises(DecoyError) as excinfo:
        concat_masked_chunks(chunks, table="t")
    assert excinfo.value.code == "chunked_schema_mismatch"  # type: ignore[attr-defined]


def test_match_source_cardinality_under_delivers_without_error(tmp_path: Path) -> None:
    source, path = _source(tmp_path, [f"real-{i % 100}" for i in range(1000)])
    config = _config(
        "edge_case_string",
        path,
        deterministic=False,
        cardinality_mode="match_source_cardinality",
        pool_size=10_000,
    )
    out = _run(config, source).outputs["t"].column("c").to_pylist()
    # 100 distinct sources, no error, and 46 distinct outputs (fixed by the job seed): the mode
    # checks the target against the raw pool size, not the distinct pool values.
    assert len(set(out)) == 46
    assert len(set(out)) <= len(SPECS["edge_case_string"].values) < 100


def test_source_value_equal_to_an_edge_case_fails_the_leakage_scan(tmp_path: Path) -> None:
    source, path = _source(tmp_path, [f"real-{i % 50}" for i in range(200)])
    # Every catalogue integer appears in the source, so any output value is a source value.
    catalogue = list(SPECS["edge_case_integer"].values)
    integer_source = pa.table({"c": pa.array(catalogue * 4, type=pa.int64())})
    config = _config("edge_case_integer", path)
    result = _run(config, integer_source, post_validation=True)
    assert "leakage" in {c["code"] for c in result.quality_metrics["failed_checks"]}
    clean = _run(_config("edge_case_integer", path), source, post_validation=True)
    assert "leakage" not in {c["code"] for c in clean.quality_metrics.get("failed_checks", [])}
