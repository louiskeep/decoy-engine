"""Tests for the multi-part parquet export reader (warehouse sources S1a).

Plan: ``docs/plans/2026-10-23-warehouse-s1a-multipart-parquet-reader.md``.
Primitive: ``decoy_engine.connectors.parquet_dataset.stage_parquet_parts``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.connectors.parquet_dataset import ParquetDatasetError, stage_parquet_parts


def _write_part(tmp_path: Path, name: str, table: pa.Table) -> Path:
    path = tmp_path / name
    pq.write_table(table, path)
    return path


def _staged_files(dest_dir: Path) -> list[Path]:
    return sorted(dest_dir.glob("dataset-*.parquet"))


def _no_leftovers(dest_dir: Path) -> bool:
    return not _staged_files(dest_dir) and not list(dest_dir.glob(".stage-*"))


# ---------------------------------------------------------------------------
# Merge + order
# ---------------------------------------------------------------------------


def test_merges_parts_in_sorted_path_order(tmp_path: Path) -> None:
    p1 = _write_part(tmp_path, "part-0001.parquet", pa.table({"id": [3, 4], "v": ["c", "d"]}))
    p0 = _write_part(tmp_path, "part-0000.parquet", pa.table({"id": [1, 2], "v": ["a", "b"]}))
    dest = tmp_path / "out"
    # Pass in the "wrong" order on purpose; output must follow sorted path order.
    staged = stage_parquet_parts([p1, p0], dest)
    merged = pq.read_table(staged)
    assert merged.column("id").to_pylist() == [1, 2, 3, 4]
    assert merged.column("v").to_pylist() == ["a", "b", "c", "d"]


def test_input_list_order_does_not_change_output(tmp_path: Path) -> None:
    pa_ = _write_part(tmp_path, "part-a.parquet", pa.table({"id": [1], "v": ["a"]}))
    pb = _write_part(tmp_path, "part-b.parquet", pa.table({"id": [2], "v": ["b"]}))
    one = pq.read_table(stage_parquet_parts([pa_, pb], tmp_path / "o1")).to_pydict()
    two = pq.read_table(stage_parquet_parts([pb, pa_], tmp_path / "o2")).to_pydict()
    assert one == two


def test_deterministic_content_across_runs(tmp_path: Path) -> None:
    parts = [
        _write_part(tmp_path, f"part-{i}.parquet", pa.table({"id": [i], "v": [str(i)]}))
        for i in range(4)
    ]
    a = pq.read_table(stage_parquet_parts(parts, tmp_path / "a")).to_pydict()
    b = pq.read_table(stage_parquet_parts(parts, tmp_path / "b")).to_pydict()
    assert a == b


# ---------------------------------------------------------------------------
# Type fidelity
# ---------------------------------------------------------------------------


def test_types_are_preserved_exactly(tmp_path: Path) -> None:
    schema = pa.schema(
        [
            ("i", pa.int64()),
            ("d", pa.decimal128(18, 2)),
            ("ts", pa.timestamp("us", "UTC")),
            ("day", pa.date32()),
            ("s", pa.string()),
        ]
    )
    part = pa.table(
        {
            "i": pa.array([1, 2], pa.int64()),
            "d": pa.array([Decimal("1.50"), Decimal("2.25")], pa.decimal128(18, 2)),
            "ts": pa.array([1, 2], pa.timestamp("us", "UTC")),
            "day": pa.array([dt.date(2024, 1, 1), dt.date(2024, 1, 2)], pa.date32()),
            "s": pa.array(["a", "b"]),
        }
    )
    p = _write_part(tmp_path, "part-0.parquet", part)
    staged = pq.read_table(stage_parquet_parts([p], tmp_path / "out"))
    assert staged.schema.equals(schema, check_metadata=False)
    assert staged.column("d").to_pylist() == [Decimal("1.50"), Decimal("2.25")]


# ---------------------------------------------------------------------------
# Fail-closed edges
# ---------------------------------------------------------------------------


def test_empty_list_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ParquetDatasetError) as exc:
        stage_parquet_parts([], tmp_path / "out")
    assert exc.value.code == "no_parts"


def test_missing_part_fails_closed_and_publishes_nothing(tmp_path: Path) -> None:
    good = _write_part(tmp_path, "part-0.parquet", pa.table({"id": [1]}))
    dest = tmp_path / "out"
    with pytest.raises(ParquetDatasetError) as exc:
        stage_parquet_parts([good, tmp_path / "missing.parquet"], dest)
    assert exc.value.code == "part_unreadable"
    assert _no_leftovers(dest)


def test_schema_mismatch_fails_closed_and_publishes_nothing(tmp_path: Path) -> None:
    p0 = _write_part(tmp_path, "part-0.parquet", pa.table({"id": pa.array([1], pa.int64())}))
    # same column name, different type -> not one table
    p1 = _write_part(tmp_path, "part-1.parquet", pa.table({"id": pa.array([2], pa.int32())}))
    dest = tmp_path / "out"
    with pytest.raises(ParquetDatasetError) as exc:
        stage_parquet_parts([p0, p1], dest)
    assert exc.value.code == "schema_mismatch"
    assert exc.value.location == "part-1.parquet"
    assert _no_leftovers(dest)


def test_extra_column_is_a_schema_mismatch(tmp_path: Path) -> None:
    p0 = _write_part(tmp_path, "part-0.parquet", pa.table({"id": [1]}))
    p1 = _write_part(tmp_path, "part-1.parquet", pa.table({"id": [2], "extra": [9]}))
    with pytest.raises(ParquetDatasetError) as exc:
        stage_parquet_parts([p0, p1], tmp_path / "out")
    assert exc.value.code == "schema_mismatch"


# ---------------------------------------------------------------------------
# Zero-row part
# ---------------------------------------------------------------------------


def test_zero_row_part_contributes_no_rows(tmp_path: Path) -> None:
    empty = _write_part(
        tmp_path,
        "part-0.parquet",
        pa.table({"id": pa.array([], pa.int64()), "v": pa.array([], pa.string())}),
    )
    full = _write_part(
        tmp_path, "part-1.parquet", pa.table({"id": pa.array([1, 2], pa.int64()), "v": ["a", "b"]})
    )
    merged = pq.read_table(stage_parquet_parts([empty, full], tmp_path / "out"))
    assert merged.num_rows == 2
    assert merged.column("id").to_pylist() == [1, 2]


# ---------------------------------------------------------------------------
# Property: splitting a table into parts and merging round-trips the rows in
# split order (invariant per testing.md: round-trip claims get a property test).
# ---------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    rows=st.lists(
        st.tuples(st.integers(min_value=-(10**9), max_value=10**9), st.text(max_size=12)),
        min_size=0,
        max_size=40,
    ),
    n_parts=st.integers(min_value=1, max_value=6),
)
def test_split_then_merge_roundtrips(tmp_path_factory: Any, rows: list, n_parts: int) -> None:
    tmp = tmp_path_factory.mktemp("split")
    ids = [r[0] for r in rows]
    vs = [r[1] for r in rows]
    full = pa.table({"id": pa.array(ids, pa.int64()), "v": pa.array(vs, pa.string())})
    # Split into n_parts contiguous slices (preserving order).
    size = len(rows)
    bounds = [round(size * k / n_parts) for k in range(n_parts + 1)]
    parts = []
    for k in range(n_parts):
        chunk = full.slice(bounds[k], bounds[k + 1] - bounds[k])
        # zero-padded names so sorted order == split order
        parts.append(_write_part(tmp, f"part-{k:03d}.parquet", chunk))
    merged = pq.read_table(stage_parquet_parts(parts, tmp / "out"))
    assert merged.column("id").to_pylist() == ids
    assert merged.column("v").to_pylist() == vs


# ---------------------------------------------------------------------------
# The staged file is consumable by the unchanged downstream path.
# ---------------------------------------------------------------------------


def test_staged_file_is_maskable_by_run_pipeline(tmp_path: Path) -> None:
    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline
    from decoy_engine.keyprovider import SecretKeyProvider

    p0 = _write_part(
        tmp_path,
        "part-0.parquet",
        pa.table(
            {
                "s": ["a@x.com", "b@x.com"],
                "amt": pa.array([Decimal("1.50"), Decimal("2.50")], pa.decimal128(18, 2)),
            }
        ),
    )
    p1 = _write_part(
        tmp_path,
        "part-1.parquet",
        pa.table({"s": ["c@x.com"], "amt": pa.array([Decimal("3.50")], pa.decimal128(18, 2))}),
    )
    staged = stage_parquet_parts([p0, p1], tmp_path / "staged")

    table = pq.read_table(staged)
    raw = {
        "version": 1,
        "global_settings": {"seed": 5},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(staged)}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out")}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "s", "strategy": "hash", "namespace": "ns"},
                    {"name": "amt", "strategy": "passthrough"},
                ],
            }
        ],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    kp = SecretKeyProvider(secret=bytes(range(32)), key_version="v1")
    result = run_pipeline(config, {"t": table}, engine_version="s1a", key_provider=kp)
    out = result.outputs["t"]
    assert out.num_rows == 3
    # passthrough amount values preserved across the merged dataset
    assert out.column("amt").to_pylist() == [Decimal("1.50"), Decimal("2.50"), Decimal("3.50")]
