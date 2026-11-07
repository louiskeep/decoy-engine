"""ORC reads and writes (plan V1P-4.1, AT-C1 to AT-C4)."""

from __future__ import annotations

import datetime as dt
import decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.orc as orc
import pytest

from decoy_engine.io import (
    ChunkBudget,
    OrcMalformed,
    OrcTypeUnsupported,
    TargetColumnTypeUnsupported,
    iter_orc_chunks,
    orc_io,
    read_orc,
    write_orc,
)


def _budget(nbytes: int = 1 << 20, rows: int = 1 << 20, read_rows: int = 65_536) -> ChunkBudget:
    return ChunkBudget(
        chunk_budget_bytes=nbytes, max_chunk_rows=rows, max_row_bytes=1 << 16, read_rows=read_rows
    )


def _table(n: int = 300_000) -> pa.Table:
    return pa.table(
        {
            "id": pa.array(range(n), pa.int64()),
            "s": pa.array([f"v{i}" for i in range(n)]),
            "d": pa.array([dt.date(2020, 1, 1) + dt.timedelta(days=i % 900) for i in range(n)]),
            "dec": pa.array(
                [decimal.Decimal(i % 1000) / 10 for i in range(n)], pa.decimal128(9, 1)
            ),
        }
    )


# AT-C1 --------------------------------------------------------------------------------------


@pytest.mark.serial
def test_chunked_equals_whole_and_reads_below_a_stripe(tmp_path: Path) -> None:
    table = _table()
    path = tmp_path / "m.orc"
    orc.write_table(table, str(path), stripe_size=1 << 20)
    assert orc.ORCFile(str(path)).nstripes > 1
    whole = read_orc(str(path), table="t")
    assert whole.equals(table)
    budget = _budget(nbytes=256 << 10, read_rows=5_000)
    chunks = list(iter_orc_chunks(str(path), budget, table="t"))
    assert max(c.num_rows for c in chunks) <= 5_000
    assert max(c.nbytes for c in chunks) <= budget.chunk_budget_bytes
    assert pa.concat_tables(chunks).equals(table)


# AT-C2 --------------------------------------------------------------------------------------


def test_write_round_trip_and_casts(tmp_path: Path) -> None:
    n = 20
    table = pa.table(
        {
            "i8": pa.array(range(n), pa.int8()),
            "u8": pa.array(range(n), pa.uint8()),
            "u16": pa.array(range(n), pa.uint16()),
            "u32": pa.array(range(n), pa.uint32()),
            "u64": pa.array(range(n), pa.uint64()),
            "cat": pa.array([["a", "b"][i % 2] for i in range(n)]).dictionary_encode(),
            "ls": pa.array([str(i) for i in range(n)], pa.large_string()),
            "lst": pa.array([[i] for i in range(n)]),
            "ts": pa.array(
                [dt.datetime(2020, 1, 1) + dt.timedelta(seconds=i) for i in range(n)],
                pa.timestamp("us"),
            ),
        }
    )
    path = tmp_path / "o.orc"
    write_orc(table, path, table="t")
    back = orc.ORCFile(str(path)).read()
    expected = {
        "i8": pa.int8(),
        "u8": pa.int16(),
        "u16": pa.int32(),
        "u32": pa.int64(),
        "u64": pa.int64(),
        "cat": pa.string(),
        "ls": pa.string(),
        "lst": pa.list_(pa.int64()),
        "ts": pa.timestamp("ns"),
    }
    assert {f.name: f.type for f in back.schema} == expected
    for name in table.column_names:
        assert back.column(name).to_pylist() == table.column(name).cast(expected[name]).to_pylist()
    # Without the timestamp column, the engine's own reader reads the file back.
    write_orc(table.drop(["ts"]), path, table="t")
    assert read_orc(str(path), table="t").num_rows == n


@pytest.mark.parametrize(
    "column",
    [
        pa.array([dt.time(1)], pa.time64("us")),
        pa.array([dt.timedelta(1)], pa.duration("us")),
        pa.nulls(1),
        pa.array([decimal.Decimal(1)], pa.decimal256(40, 0)),
        pa.array([2**64 - 1], pa.uint64()),
        pa.array([[1]], pa.list_(pa.uint32())),
    ],
    ids=["time", "duration", "null", "decimal256", "uint64_overflow", "nested_cast"],
)
def test_unwritable_refused_and_nothing_published(tmp_path: Path, column: pa.Array) -> None:
    path = tmp_path / "o.orc"
    path.write_bytes(b"existing")
    with pytest.raises(TargetColumnTypeUnsupported) as info:
        write_orc(pa.table({"x": column}), path, table="t")
    assert info.value.__context__ is None
    assert path.read_bytes() == b"existing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.orc"]


# AT-C3 --------------------------------------------------------------------------------------


def test_malformed_files(tmp_path: Path) -> None:
    good = tmp_path / "g.orc"
    orc.write_table(_table(1000), str(good))
    raw = good.read_bytes()
    cases = {"magic": b"XXX" + raw[3:], "empty": b"", "short": raw[:2]}
    for cut in (len(raw) - 1, len(raw) - 30, len(raw) // 2, 10):
        cases[f"cut{cut}"] = raw[:cut]
    for name, data in cases.items():
        path = tmp_path / f"{name}.orc"
        path.write_bytes(data)
        for read in (
            lambda p=path: read_orc(str(p), table="t"),
            lambda p=path: list(iter_orc_chunks(str(p), _budget(), table="t")),
        ):
            with pytest.raises(OrcMalformed) as info:
                read()
            assert info.value.__context__ is None and info.value.__cause__ is None
            assert str(tmp_path) not in str(info.value)


# AT-C4 (P0): timestamps --------------------------------------------------------------------

_LO = dt.datetime(1677, 9, 21, 0, 12, 44)
_HI = dt.datetime(2262, 4, 11, 23, 47, 16)


@pytest.mark.parametrize(
    "value",
    [
        dt.datetime(9999, 12, 31),
        dt.datetime(1, 1, 1),
        dt.datetime(1677, 9, 21, 0, 12, 43, 145224) - dt.timedelta(microseconds=1),
        dt.datetime(2262, 4, 12),
    ],
    ids=["9999", "0001", "just_below", "just_above"],
)
def test_write_refuses_timestamps_outside_the_ns_range(tmp_path: Path, value: dt.datetime) -> None:
    path = tmp_path / "o.orc"
    with pytest.raises(TargetColumnTypeUnsupported) as info:
        write_orc(pa.table({"ts": pa.array([value], pa.timestamp("us"))}), path, table="t")
    assert info.value.detail["column"] == "ts"
    assert not path.exists()


def test_write_accepts_the_range_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "o.orc"
    table = pa.table({"ts": pa.array([_LO, _HI], pa.timestamp("us", "UTC"))})
    write_orc(table, path, table="t")
    assert orc.ORCFile(str(path)).read().column("ts").to_pylist()[0].year == 1677


def test_pyarrow_corrupts_out_of_range_orc_timestamps() -> None:
    """Pins the reason for the read refusal: if pyarrow stops wrapping, revisit it."""
    import io

    buf = io.BytesIO()
    orc.write_table(
        pa.table({"ts": pa.array([dt.datetime(9999, 12, 31)], pa.timestamp("us"))}), buf
    )
    back = orc.ORCFile(pa.BufferReader(buf.getvalue())).read().column("ts").to_pylist()[0]
    assert back.year == 1816


@pytest.mark.parametrize(
    "column",
    [
        pa.array([dt.datetime(2020, 1, 1)], pa.timestamp("us")),
        pa.array([dt.datetime(9999, 12, 31)], pa.timestamp("us")),
        pa.array([{"a": dt.datetime(2020, 1, 1)}]),
        pa.array([[dt.datetime(2020, 1, 1)]]),
    ],
    ids=["in_range", "out_of_range", "in_struct", "in_list"],
)
def test_read_refuses_timestamp_columns_before_any_stripe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: pa.Array
) -> None:
    path = tmp_path / "t.orc"
    orc.write_table(pa.table({"id": [1], "ts": column}), str(path))
    stripes: list[int] = []
    real = orc_io.arrow_adapter.orc_batches

    def spy(*a, **k):  # type: ignore[no-untyped-def]
        stripes.append(1)
        return real(*a, **k)

    monkeypatch.setattr(orc_io.arrow_adapter, "orc_batches", spy)
    from decoy_engine.profile import profile_source

    for read in (
        lambda: read_orc(str(path), table="t"),
        lambda: list(iter_orc_chunks(str(path), _budget(), table="t")),
        lambda: profile_source(
            {"sources": {"t": {"type": "file", "format": "orc", "path": str(path)}}}, seed=1
        ),
    ):
        with pytest.raises(OrcTypeUnsupported) as info:
            read()
        assert info.value.detail["column"] == "ts"
    assert not stripes


def test_streamed_write_equals_whole(tmp_path: Path) -> None:
    table = _table(10_000)
    whole, streamed = tmp_path / "w.orc", tmp_path / "s.orc"
    write_orc(table, whole, table="t")
    write_orc(
        [table.slice(i, 1000) for i in range(0, 10_000, 1000)],
        streamed,
        table="t",
        schema=table.schema,
    )
    assert read_orc(str(streamed), table="t").equals(read_orc(str(whole), table="t"))


def test_output_bytes_are_deterministic(tmp_path: Path) -> None:
    import time

    table = _table(1000)
    write_orc(table, tmp_path / "a.orc", table="t")
    time.sleep(1.1)  # a wall-clock field in the footer would differ across this gap
    write_orc(table, tmp_path / "b.orc", table="t")
    assert (tmp_path / "a.orc").read_bytes() == (tmp_path / "b.orc").read_bytes()
