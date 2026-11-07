"""Avro and ORC through the engine's routes (plan V1P-4.1, AT-P1, AT-R1 to AT-R4)."""

from __future__ import annotations

import datetime as dt
import decimal
import warnings
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from decoy_engine import __version__, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_mask_pipeline_chunked
from decoy_engine.io import (
    AvroMalformed,
    BoundedReadError,
    ChunkBudget,
    TargetSchemaMismatch,
    iter_avro_chunks,
    iter_orc_chunks,
    read_avro,
    read_orc,
    write_avro,
    write_orc,
)
from decoy_engine.profile import build_profile_source, profile_source
from tests.unit.io._avro_build import container, record, zz

N = 60


def _data(with_ts: bool = True) -> pa.Table:
    cols: dict[str, Any] = {
        "id": pa.array(range(N), pa.int64()),
        "name": pa.array([f"name{i}" for i in range(N)]),
        "d": pa.array([dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(N)]),
        "dec": pa.array([decimal.Decimal(i * 37) / 100 for i in range(N)], pa.decimal128(12, 2)),
    }
    if with_ts:
        cols["tms"] = pa.array(
            [dt.datetime(2020, 1, 1) + dt.timedelta(seconds=i) for i in range(N)],
            pa.timestamp("ms", "UTC"),
        )
    return pa.table(cols)


def _cfg(fmt: str, src: Path, out: Path) -> dict[str, Any]:
    raw = {
        "version": 2,
        "global_settings": {"seed": 3},
        "sources": {"people": {"type": "file", "format": fmt, "path": str(src)}},
        "tables": [{"name": "people", "columns": [{"name": "name", "strategy": "redact"}]}],
        "targets": {"people": {"type": "file", "format": fmt, "path": str(out)}},
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _run(cfg: dict[str, Any], table: pa.Table, **kw: Any) -> pa.Table:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return run_pipeline(
            cfg, sources={"people": table}, engine_version=__version__, **kw
        ).outputs["people"]


# AT-P1 --------------------------------------------------------------------------------------


def test_profile_sources_exact_counts_schema_and_bounded_sample(tmp_path: Path) -> None:
    table = _data()
    avro_path, orc_path = tmp_path / "p.avro", tmp_path / "p.orc"
    write_avro(table, avro_path, table="t")
    write_orc(_data(with_ts=False), orc_path, table="t")
    for fmt, path, full in (("avro", avro_path, read_avro), ("orc", orc_path, read_orc)):
        source = build_profile_source({"type": "file", "format": fmt, "path": str(path)})
        assert source.row_count().value == N and source.row_count().exact
        whole = full(str(path), table="t")
        assert source.schema() == whole.schema
        assert len(source.sample_frame(7)) == 7
        assert len(source.to_frame()) == N


def test_avro_row_count_does_not_decode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.io import avro_io

    path = tmp_path / "c.avro"
    write_avro(_data(), path, table="t")
    monkeypatch.setattr(avro_io, "_decode_group", lambda *a, **k: pytest.fail("decoded"))
    source = build_profile_source({"type": "file", "format": "avro", "path": str(path)})
    assert source.row_count().value == N


def test_nested_and_one_block_avro_still_profile_and_run(tmp_path: Path) -> None:
    nested = tmp_path / "n.avro"
    nested.write_bytes(
        container(
            record([("id", "long"), ("tags", {"type": "array", "items": "long"})]),
            [(2, zz(1) + zz(1) + zz(5) + zz(0) + zz(2) + zz(0))],
        )
    )
    one_block = tmp_path / "one.avro"
    big = 50_000  # one Polars block of several MiB: more than a typical chunk budget
    pl.DataFrame({"id": list(range(big)), "name": ["n" * 60] * big}).write_avro(one_block)
    # The reader admits both on the profile route; the nested file then meets the profiler's
    # existing limit on list columns, exactly as the same data from Parquet does.
    nested_source = build_profile_source({"type": "file", "format": "avro", "path": str(nested)})
    assert nested_source.row_count().value == 2
    assert nested_source.sample_frame(5)["tags"].map(list).tolist() == [[5], []]
    pq.write_table(read_avro(str(nested), table="t"), tmp_path / "n.parquet")
    outcomes = []
    for fmt, path in (("avro", nested), ("parquet", tmp_path / "n.parquet")):
        try:
            profile_source(
                {"sources": {"p": {"type": "file", "format": fmt, "path": str(path)}}}, seed=1
            )
            outcomes.append("ok")
        except TypeError:
            outcomes.append("TypeError")
    assert outcomes[0] == outcomes[1]
    assert one_block.stat().st_size > 2 << 20
    one_cfg = {"sources": {"people": {"type": "file", "format": "avro", "path": str(one_block)}}}
    assert profile_source(one_cfg, seed=1).tables[0].row_count == big
    out = _run(_cfg("avro", one_block, tmp_path / "o.avro"), read_avro(str(one_block), table="t"))
    assert out.num_rows == big


# AT-R1: engine round trip, oracle = the Parquet route -------------------------------------------


@pytest.mark.parametrize("fmt", ["avro", "orc"])
def test_round_trip_matches_the_parquet_route(tmp_path: Path, fmt: str) -> None:
    table = _data(with_ts=fmt == "avro")
    write, read = (write_avro, read_avro) if fmt == "avro" else (write_orc, read_orc)
    src, out = tmp_path / f"in.{fmt}", tmp_path / f"out.{fmt}"
    write(table, src, table="t")
    masked = _run(_cfg(fmt, src, out), read(str(src), table="t"))
    write(masked, out, table="t")
    back = read(str(out), table="t")

    pq.write_table(table, tmp_path / "in.parquet")
    oracle = _run(
        _cfg("parquet", tmp_path / "in.parquet", tmp_path / "o.parquet"),
        pq.read_table(tmp_path / "in.parquet"),
    )
    assert back.schema == oracle.schema
    assert back.to_pylist() == oracle.to_pylist()
    assert set(back.column("name").to_pylist()) != set(table.column("name").to_pylist())


# AT-R2: the chunked route with streamed writes -------------------------------------------------


@pytest.mark.serial
@pytest.mark.parametrize("fmt", ["avro", "orc"])
def test_chunked_route_equals_full_frame_with_varying_decimal_chunks(
    tmp_path: Path, fmt: str
) -> None:
    n = 3000
    # Magnitudes grow per chunk, so the masked chunks' decimal precision differs on main.
    dec = [decimal.Decimal(10 ** (i // 1000 * 3)) * (i % 7) / 100 for i in range(n)]
    table = pa.table(
        {
            "id": pa.array(range(n), pa.int64()),
            "name": pa.array([f"n{i}" for i in range(n)]),
            "dec": pa.array(dec, pa.decimal128(12, 2)),
        }
    )
    write, read = (write_avro, read_avro) if fmt == "avro" else (write_orc, read_orc)
    chunks_of = iter_avro_chunks if fmt == "avro" else iter_orc_chunks
    src = tmp_path / f"in.{fmt}"
    write(table, src, table="t")
    cfg = _cfg(fmt, src, tmp_path / f"o.{fmt}")
    budget = ChunkBudget(
        chunk_budget_bytes=1 << 20, max_chunk_rows=1000, max_row_bytes=1 << 16, read_rows=1000
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        masked_chunks = list(
            run_mask_pipeline_chunked(
                cfg,
                chunks_of(str(src), budget, table="people"),
                table="people",
                engine_version=__version__,
            )
        )
    full = _run(cfg, read(str(src), table="t"))
    assert pa.concat_tables([c.cast(table.schema) for c in masked_chunks]).equals(
        full.cast(table.schema)
    )
    out = tmp_path / f"out.{fmt}"
    write(masked_chunks, out, table="t", schema=table.schema)
    back = read(str(out), table="t")
    assert back.schema.field("dec").type == pa.decimal128(12, 2)
    assert (
        back.to_pylist()
        == pa.concat_tables([c.cast(table.schema) for c in masked_chunks]).to_pylist()
    )
    precisions = {c.schema.field("dec").type.precision for c in masked_chunks}
    if len(precisions) > 1:
        with pytest.raises(TargetSchemaMismatch):
            write(masked_chunks, tmp_path / f"bad.{fmt}", table="t", schema=masked_chunks[0].schema)


# AT-R3: refusals on the routes that do not take these formats -----------------------------------


def test_subset_refuses_avro_source(tmp_path: Path) -> None:
    raw = {
        "version": 2,
        "global_settings": {"seed": 3},
        "sources": {"people": {"type": "file", "format": "avro", "path": "p.avro"}},
        "tables": [{"name": "people", "columns": [{"name": "name", "strategy": "redact"}]}],
        "targets": {"people": {"type": "file", "format": "avro", "path": "o.avro"}},
        "subset": {
            "seeds": [{"table": "people", "mode": "sample", "key_columns": ["id"], "count": 5}]
        },
    }
    with pytest.raises(ValidationError, match="Parquet"):
        PipelineConfig.model_validate(raw)


@pytest.mark.parametrize("kind", ["s3", "gcs"])
def test_cloud_sources_and_targets_refuse_avro_and_orc(kind: str) -> None:
    loc = {"bucket": "b", "key": "k"} if kind == "s3" else {"bucket": "b", "object": "o"}
    for fmt in ("avro", "orc"):
        raw = {
            "version": 2,
            "sources": {"people": {"type": kind, "format": fmt, **loc}},
            "tables": [{"name": "people", "columns": [{"name": "name", "strategy": "redact"}]}],
            "targets": {"people": {"type": "file", "format": "csv", "path": "o.csv"}},
        }
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(raw)


def _comp3_cfg(source_fmt: str, src: Path, target_fmt: str) -> dict[str, Any]:
    comp3 = {"id": "cobol/comp-3/v1", "precision": 5, "scale": 2, "signed": True}
    return {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": {"t": {"type": "file", "format": source_fmt, "path": str(src)}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "id", "strategy": "passthrough"},
                    {
                        "name": "amt",
                        "strategy": "passthrough",
                        "logical_type": "decimal",
                        "parse_format": comp3,
                        "error_policy": "null",
                        "output_format": comp3,
                    },
                ],
            }
        ],
        "targets": {
            "t": {"type": "file", "format": target_fmt, "path": str(src.parent / f"o.{target_fmt}")}
        },
        "relationships": [],
        "namespaces": {},
    }


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or "")


def test_binary_contract_refused_for_avro_and_orc_sources_and_targets(tmp_path: Path) -> None:
    table = pa.table({"id": ["1"], "amt": pa.array([b"\x12\x3c"], pa.binary())})
    for fmt, write in (("avro", write_avro), ("orc", write_orc)):
        src = tmp_path / f"a.{fmt}"
        write(table, src, table="t")
        with pytest.raises(Exception) as info:
            run_pipeline(
                _comp3_cfg(fmt, src, "parquet"), sources={"t": table}, engine_version=__version__
            )
        assert "declared_cast_contract_invalid" in _code(info.value) + str(info.value)
    pq.write_table(table, tmp_path / "a.parquet")
    for fmt in ("avro", "orc"):
        cfg = _comp3_cfg("parquet", tmp_path / "a.parquet", fmt)
        with pytest.raises(Exception) as info:
            run_pipeline(
                cfg, sources={"t": table}, engine_version=__version__, execution_mode="full_frame"
            )
        assert "declared_cast_route_unsupported" in _code(info.value) + str(info.value)


# AT-R4: corrupt sources surface the coded error everywhere --------------------------------------


def test_corrupt_sources_are_coded_everywhere(tmp_path: Path) -> None:
    from decoy_engine.execution.capacity import estimate_job_capacity

    bad_avro = tmp_path / "bad.avro"
    bad_avro.write_bytes(b"Obj\x01\x02garbage")
    bad_orc = tmp_path / "bad.orc"
    bad_orc.write_bytes(b"ORC" + b"\x00" * 40)
    for fmt, path in (("avro", bad_avro), ("orc", bad_orc)):
        cfg = _cfg(fmt, path, tmp_path / f"o.{fmt}")
        for call in (
            lambda c=cfg: profile_source(c, seed=1),
            lambda c=cfg: run_pipeline(c, sources={"people": _data()}, engine_version=__version__),
            lambda c=cfg: estimate_job_capacity(c, base_dir=str(tmp_path)),
        ):
            with pytest.raises(BoundedReadError) as info:
                call()
            assert info.value.__context__ is None
            assert type(info.value).__name__ in {"AvroMalformed", "OrcMalformed"}
    assert AvroMalformed.code == "avro_container_malformed"
