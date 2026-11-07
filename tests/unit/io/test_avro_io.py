"""Avro reads and writes (plan V1P-4.1, AT-A5, AT-A6, AT-B1, AT-B2, AT-B4 to AT-B9)."""

from __future__ import annotations

import datetime as dt
import decimal
import io
import json
import os
from pathlib import Path

import polars as pl
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine import fileaccess
from decoy_engine.fileaccess import PreopenedFileAccess
from decoy_engine.io import (
    AvroBlockTooLarge,
    AvroMalformed,
    BoundedReadError,
    ChunkBudget,
    TargetColumnTypeUnsupported,
    TargetSchemaMismatch,
    UnsupportedColumnType,
    avro_io,
    iter_avro_chunks,
    read_avro,
    write_avro,
)
from tests.unit.io._avro_build import astr, container, record, snappy, zz

SECRET = "SECRET-VALUE-7731"


def _budget(nbytes: int = 1 << 20, rows: int = 1 << 20) -> ChunkBudget:
    return ChunkBudget(chunk_budget_bytes=nbytes, max_chunk_rows=rows, max_row_bytes=1 << 16)


def _header_schema(path: Path) -> dict:  # type: ignore[type-arg]
    raw = path.read_bytes()
    start = raw.index(b'{"type"')
    depth = 0
    for i in range(start, len(raw)):
        depth += {ord("{"): 1, ord("}"): -1}.get(raw[i], 0)
        if depth == 0:
            return json.loads(raw[start : i + 1])
    raise AssertionError("no schema")


# AT-B1: every admitted type, chunked == whole -----------------------------------------------

_ALL = record(
    [
        ("b", "boolean"),
        ("i", "int"),
        ("l", ["null", "long"]),
        ("f", "float"),
        ("d", "double"),
        ("by", "bytes"),
        ("s", ["string", "null"]),
        ("dt", {"type": "int", "logicalType": "date"}),
        ("tms", {"type": "long", "logicalType": "timestamp-millis"}),
        ("tus", {"type": "long", "logicalType": "timestamp-micros"}),
        ("lms", {"type": "long", "logicalType": "local-timestamp-millis"}),
        ("lus", {"type": "long", "logicalType": "local-timestamp-micros"}),
        ("db", {"type": "bytes", "logicalType": "decimal", "precision": 10, "scale": 2}),
        (
            "df",
            {
                "type": "fixed",
                "name": "D8",
                "size": 8,
                "logicalType": "decimal",
                "precision": 12,
                "scale": 3,
            },
        ),
        ("u", {"type": "string", "logicalType": "uuid"}),
        ("tm", {"type": "int", "logicalType": "time-millis"}),
        ("e", {"type": "enum", "name": "E", "symbols": ["A", "B"]}),
        ("fx", {"type": "fixed", "name": "F3", "size": 3}),
    ]
)
_EXPECTED_TYPES = {
    "b": pa.bool_(),
    "i": pa.int32(),
    "l": pa.int64(),
    "f": pa.float32(),
    "d": pa.float64(),
    "by": pa.binary(),
    "s": pa.string(),
    "dt": pa.date32(),
    "tms": pa.timestamp("ms", "UTC"),
    "tus": pa.timestamp("us", "UTC"),
    "lms": pa.timestamp("ms"),
    "lus": pa.timestamp("us"),
    "db": pa.decimal128(10, 2),
    "df": pa.decimal128(12, 3),
    "u": pa.string(),
    "tm": pa.time64("ns"),
    "e": pa.string(),
    "fx": pa.binary(),
}


def _row(i: int) -> bytes:
    import struct

    return b"".join(
        [
            bytes([i % 2]),
            zz(i),
            zz(1) + zz(10 * i) if i % 3 else zz(0),
            struct.pack("<f", i / 2),
            struct.pack("<d", i / 4),
            astr(bytes([i % 256])),
            zz(0) + astr(f"s{i}".encode()),
            zz(18262 + i),
            zz(1577836800000 + i),
            zz(1577836800000000 + i),
            zz(1577836800000 + i),
            zz(1577836800000000 + i),
            astr((12345 + i).to_bytes(4, "big", signed=True)),
            (-5000 + i).to_bytes(8, "big", signed=True),
            astr(b"123e4567-e89b-12d3-a456-426614174000"),
            zz(1000 * i),
            zz(i % 2),
            b"abc",
        ]
    )


def _all_file(tmp_path: Path, blocks: int = 5, per: int = 7) -> str:
    raw = container(
        _ALL, [(per, b"".join(_row(b * per + k) for k in range(per))) for b in range(blocks)]
    )
    path = tmp_path / "all.avro"
    path.write_bytes(raw)
    return str(path)


def test_every_admitted_type(tmp_path: Path) -> None:
    out = read_avro(_all_file(tmp_path), table="t")
    assert {f.name: f.type for f in out.schema} == _EXPECTED_TYPES
    assert out.num_rows == 35
    row = out.slice(3, 1).to_pylist()[0]
    assert row["l"] is None and row["s"] == "s3" and row["e"] == "B"
    assert row["db"] == decimal.Decimal("123.48") and row["df"] == decimal.Decimal("-4.997")
    assert row["dt"] == dt.date(2020, 1, 4)
    assert row["tms"] == dt.datetime(2020, 1, 1, 0, 0, 0, 3000, tzinfo=dt.timezone.utc)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(nbytes=st.integers(2_000, 200_000), rows=st.integers(1, 40))
def test_chunked_equals_whole(tmp_path: Path, nbytes: int, rows: int) -> None:
    path = _all_file(tmp_path)
    whole = read_avro(path, table="t")
    chunks = list(iter_avro_chunks(path, _budget(nbytes, rows), table="t"))
    assert all(c.num_rows <= rows for c in chunks)
    assert pa.concat_tables(chunks).equals(whole)


# AT-A5: differential against Polars' own whole-file read ----------------------------------


def _normalised_polars(path: str) -> pa.Table:
    arrow = pl.read_avro(path).to_arrow(compat_level=pl.CompatLevel.oldest())
    return avro_io._normalize(arrow, name="t")


def test_differential_hand_built(tmp_path: Path) -> None:
    path = _all_file(tmp_path)
    chunks = pa.concat_tables(list(iter_avro_chunks(path, _budget(4_000, 9), table="t")))
    assert chunks.equals(_normalised_polars(path))


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(n=st.integers(0, 3000), nbytes=st.integers(300_000, 2_000_000))
def test_differential_engine_written(tmp_path: Path, n: int, nbytes: int) -> None:
    table = pa.table(
        {
            "id": pa.array(range(n), pa.int64()),
            "s": pa.array([None if i % 5 == 0 else "x" * (i % 40) for i in range(n)], pa.string()),
            "dec": pa.array([decimal.Decimal(i) / 100 for i in range(n)], pa.decimal128(12, 2)),
        }
    )
    path = str(tmp_path / "w.avro")
    write_avro(table, path, table="t")
    chunked = list(iter_avro_chunks(path, _budget(nbytes, 512), table="t"))
    got = pa.concat_tables(chunked) if chunked else read_avro(path, table="t").slice(0, 0)
    assert got.equals(_normalised_polars(path)) and got.equals(table)


# AT-A6: fuzzing ---------------------------------------------------------------------------

_ALLOWED = (BoundedReadError,)


@settings(
    max_examples=150,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_mutated_files_only_give_coded_errors(tmp_path: Path, data: st.DataObject) -> None:
    raw = bytearray(Path(_all_file(tmp_path, blocks=3, per=4)).read_bytes())
    for _ in range(data.draw(st.integers(1, 4))):
        op = data.draw(st.sampled_from(["flip", "cut", "insert", "varint"]))
        pos = data.draw(st.integers(0, max(0, len(raw) - 1)))
        if op == "flip" and raw:
            raw[pos] ^= data.draw(st.integers(1, 255))
        elif op == "cut":
            del raw[pos:]
        elif op == "insert":
            raw[pos:pos] = data.draw(st.binary(min_size=1, max_size=8))
        else:
            raw[pos:pos] = b"\xff" * data.draw(st.integers(1, 12))
    path = tmp_path / "fuzz.avro"
    path.write_bytes(bytes(raw))
    for read in (
        lambda: read_avro(str(path), table="t"),
        lambda: list(iter_avro_chunks(str(path), _budget(50_000, 10), table="t")),
    ):
        try:
            read()
        except _ALLOWED as exc:
            assert exc.__cause__ is None and exc.__context__ is None


# AT-B2: bounded by block --------------------------------------------------------------------


def _wide(n: int) -> pa.Table:
    return pa.table({"id": pa.array(range(n), pa.int64()), "s": pa.array(["v" * 60] * n)})


@pytest.mark.serial
def test_groups_and_chunks_stay_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "big.avro")
    write_avro(_wide(200_000), path, table="t")
    budget = _budget(4 << 20, 1 << 20)
    decoded: list[int] = []
    real = avro_io._decode_group

    def spy(payload: bytes, *, name: str) -> pa.Table:
        out = real(payload, name=name)
        decoded.append(out.nbytes)
        return out

    monkeypatch.setattr(avro_io, "_decode_group", spy)
    chunks = list(iter_avro_chunks(path, budget, table="t"))
    assert sum(c.num_rows for c in chunks) == 200_000
    assert decoded and max(decoded) <= budget.chunk_budget_bytes
    assert max(c.nbytes for c in chunks) <= budget.chunk_budget_bytes


def test_one_block_polars_file_refused_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "polars.avro"
    pl.from_arrow(_wide(200_000)).write_avro(path)  # Polars writes one block per call
    calls: list[int] = []
    monkeypatch.setattr(avro_io, "_decode_group", lambda *a, **k: calls.append(1))
    with pytest.raises(AvroBlockTooLarge):
        list(iter_avro_chunks(str(path), _budget(4 << 20), table="t"))
    assert not calls


# AT-B4: write round trip --------------------------------------------------------------------


def _typed(n: int = 50) -> pa.Table:
    base = dt.datetime(2020, 1, 1)
    return pa.table(
        {
            "b": pa.array([i % 2 == 0 for i in range(n)]),
            "i8": pa.array([i % 100 for i in range(n)], pa.int8()),
            "u32": pa.array(range(n), pa.uint32()),
            "u64": pa.array(range(n), pa.uint64()),
            "i64": pa.array([None if i % 7 == 0 else i for i in range(n)], pa.int64()),
            "f": pa.array([i / 3 for i in range(n)], pa.float64()),
            "s": pa.array([f"s{i}" for i in range(n)], pa.large_string()),
            "cat": pa.array([["x", "y"][i % 2] for i in range(n)]).dictionary_encode(),
            "d": pa.array([dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(n)]),
            "tms": pa.array(
                [base + dt.timedelta(seconds=i) for i in range(n)], pa.timestamp("ms", "UTC")
            ),
            "tus": pa.array(
                [base + dt.timedelta(microseconds=i) for i in range(n)], pa.timestamp("us")
            ),
            "tns": pa.array(
                [base + dt.timedelta(microseconds=i) for i in range(n)], pa.timestamp("ns", "UTC")
            ),
            "dec": pa.array([decimal.Decimal(i) / 100 for i in range(n)], pa.decimal128(12, 2)),
            "nn": pa.array(range(n), pa.int32()),
        },
        schema=pa.schema(
            [
                pa.field("b", pa.bool_()),
                pa.field("i8", pa.int8()),
                pa.field("u32", pa.uint32()),
                pa.field("u64", pa.uint64()),
                pa.field("i64", pa.int64()),
                pa.field("f", pa.float64()),
                pa.field("s", pa.large_string()),
                pa.field("cat", pa.dictionary(pa.int32(), pa.string())),
                pa.field("d", pa.date32()),
                pa.field("tms", pa.timestamp("ms", "UTC")),
                pa.field("tus", pa.timestamp("us")),
                pa.field("tns", pa.timestamp("ns", "UTC")),
                pa.field("dec", pa.decimal128(12, 2)),
                pa.field("nn", pa.int32(), nullable=False),
            ]
        ),
    )


_WRITTEN = {
    "b": pa.bool_(),
    "i8": pa.int32(),
    "u32": pa.int64(),
    "u64": pa.int64(),
    "i64": pa.int64(),
    "f": pa.float64(),
    "s": pa.string(),
    "cat": pa.string(),
    "d": pa.date32(),
    "tms": pa.timestamp("ms", "UTC"),
    "tus": pa.timestamp("us"),
    "tns": pa.timestamp("us", "UTC"),
    "dec": pa.decimal128(12, 2),
    "nn": pa.int32(),
}


def test_write_round_trip_keeps_logical_types(tmp_path: Path) -> None:
    table = _typed()
    path = tmp_path / "o.avro"
    write_avro(table, path, table="t")
    back = read_avro(str(path), table="t")
    assert {f.name: f.type for f in back.schema} == _WRITTEN
    for name in table.column_names:
        assert back.column(name).to_pylist() == table.column(name).cast(_WRITTEN[name]).to_pylist()
    schema = _header_schema(path)
    assert schema["name"] == "topLevelRecord"
    by_name = {f["name"]: f["type"] for f in schema["fields"]}
    assert by_name["tms"] == ["null", {"type": "long", "logicalType": "timestamp-millis"}]
    assert by_name["tns"] == ["null", {"type": "long", "logicalType": "timestamp-micros"}]
    assert by_name["tus"] == ["null", {"type": "long", "logicalType": "local-timestamp-micros"}]
    assert by_name["d"] == ["null", {"type": "int", "logicalType": "date"}]
    assert by_name["dec"] == [
        "null",
        {"type": "bytes", "logicalType": "decimal", "precision": 12, "scale": 2},
    ]


def test_header_differs_from_polars_only_in_name_and_relabel(tmp_path: Path) -> None:
    table = _typed()
    path = tmp_path / "o.avro"
    write_avro(table, path, table="t")
    ours = _header_schema(path)
    from decoy_engine.io._target_types import avro_plan, prepare_chunk

    prepared = prepare_chunk(table, avro_plan(table.schema, table="t"), table="t")
    buf = io.BytesIO()
    pl.from_arrow(prepared).write_avro(buf)  # type: ignore[union-attr]
    theirs = json.loads(buf.getvalue()[buf.getvalue().index(b'{"type"') :].split(b"\x00")[0])
    theirs["name"] = "topLevelRecord"
    for field in theirs["fields"]:
        if field["name"] in ("tms", "tns"):
            branch = field["type"][1]
            branch["logicalType"] = branch["logicalType"].replace("local-", "")
    assert ours == theirs


# AT-B5: write refusals ------------------------------------------------------------------------

_REFUSED_TYPES = {
    "time": pa.array([dt.time(1, 2)], pa.time64("us")),
    "dur": pa.array([dt.timedelta(1)], pa.duration("us")),
    "null": pa.nulls(1),
    "dec256": pa.array([decimal.Decimal("1.5")], pa.decimal256(40, 1)),
    "nested": pa.array([[1, 2]]),
    "tz_other": pa.array([dt.datetime(2020, 1, 1)], pa.timestamp("us", "Europe/Oslo")),
    "secs": pa.array([dt.datetime(2020, 1, 1)], pa.timestamp("s")),
}


def _assert_nothing_published(tmp_path: Path, path: Path, before: bytes | None) -> None:
    if before is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == before
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("_decoy_")]
    assert leftovers == []


@pytest.mark.parametrize("name", sorted(_REFUSED_TYPES))
def test_unwritable_types_refused(tmp_path: Path, name: str) -> None:
    path = tmp_path / "o.avro"
    path.write_bytes(b"existing")
    with pytest.raises(TargetColumnTypeUnsupported) as info:
        write_avro(pa.table({"x": _REFUSED_TYPES[name]}), path, table="t")
    assert info.value.detail["column"] == "x"
    _assert_nothing_published(tmp_path, path, b"existing")


@pytest.mark.parametrize(
    "table",
    [
        pa.table({"bad name": [1]}),
        pa.table([pa.array([1]), pa.array([2])], names=["a", "a"]),
        pa.table({"x": pa.array([2**64 - 1], pa.uint64())}),
        pa.table({"x": pa.array([1], pa.timestamp("ns"))}),
    ],
    ids=["invalid_name", "duplicate_name", "uint64_overflow", "sub_microsecond"],
)
def test_value_and_name_refusals(tmp_path: Path, table: pa.Table) -> None:
    path = tmp_path / "o.avro"
    with pytest.raises(TargetColumnTypeUnsupported) as info:
        write_avro(table, path, table="t")
    assert info.value.__context__ is None
    _assert_nothing_published(tmp_path, path, None)


def test_chunk_schema_mismatch_and_missing_schema(tmp_path: Path) -> None:
    path = tmp_path / "o.avro"
    schema = pa.schema([("x", pa.int64())])
    with pytest.raises(TargetSchemaMismatch):
        write_avro([pa.table({"x": [1]}), pa.table({"y": ["a"]})], path, table="t", schema=schema)
    _assert_nothing_published(tmp_path, path, None)
    with pytest.raises(TypeError):
        write_avro(iter([pa.table({"x": [1]})]), path, table="t")


def test_hold_stage_is_never_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.fileaccess import _output

    published: list[str] = []
    real = _output.RootedOutputTxn.publish

    def spy(self):  # type: ignore[no-untyped-def]
        published.append(self._state.stage_name)
        return real(self)

    monkeypatch.setattr(_output.RootedOutputTxn, "publish", spy)
    write_avro(pa.table({"x": [1, 2]}), tmp_path / "o.avro", table="t")
    assert not any(name.startswith("_decoy_hold_") for name in published)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.avro"]


# AT-B6: streamed == whole, deterministic bytes -----------------------------------------------


def test_streamed_equals_whole_and_bytes_are_deterministic(tmp_path: Path) -> None:
    table = _typed(500)
    whole, streamed, again = (tmp_path / n for n in ("w.avro", "s.avro", "a.avro"))
    write_avro(table, whole, table="t")
    chunks = [table.slice(i, 50) for i in range(0, 500, 50)]
    write_avro(chunks, streamed, table="t", schema=table.schema)
    write_avro(chunks, again, table="t", schema=table.schema)
    assert read_avro(str(streamed), table="t").equals(read_avro(str(whole), table="t"))
    assert streamed.read_bytes() == again.read_bytes()


def test_empty_write_has_a_header(tmp_path: Path) -> None:
    schema = pa.schema([("x", pa.int64())])
    write_avro([], tmp_path / "e.avro", table="t", schema=schema)
    out = read_avro(str(tmp_path / "e.avro"), table="t")
    assert out.num_rows == 0 and out.schema.names == ["x"]


# AT-B7: codecs end to end ---------------------------------------------------------------------


def test_snappy_crc_flip_is_coded(tmp_path: Path) -> None:
    schema = record([("s", "string")])
    data = bytearray(snappy(astr(SECRET.encode())))
    data[-2] ^= 0x01
    path = tmp_path / "c.avro"
    path.write_bytes(container(schema, [(1, bytes(data))], codec=b"snappy"))
    with pytest.raises(AvroMalformed) as info:
        read_avro(str(path), table="t")
    assert info.value.__context__ is None


# AT-B8: value-free ----------------------------------------------------------------------------


def test_refusals_carry_no_value_or_path(tmp_path: Path) -> None:
    schema = record([("s", "string"), ("e", {"type": "enum", "name": "E", "symbols": [SECRET]})])
    bad_utf8 = astr(SECRET.encode() + b"\xff\xfe") + zz(0)
    cases = [
        container(schema, [(1, bad_utf8)]),
        container(
            record([("m", {"type": "map", "values": "string", "default": {SECRET: SECRET}})]), []
        ),
        container(schema, [(3, astr(SECRET.encode()) + zz(0))]),
    ]
    from tests.fileaccess.support import RootRelative

    rooted = RootRelative(tmp_path)
    paths = []
    for i, raw in enumerate(cases):
        path = tmp_path / f"v{i}.avro"
        path.write_bytes(raw)
        paths.append(path)
    # Ambient first: once a strict policy is used, the process refuses ambient access.
    for access in (None, rooted):
        for path in paths:
            with pytest.raises(BoundedReadError) as info:
                read_avro(str(path), table="t", file_access=access)
            text = str(info.value) + repr(info.value.detail)
            assert SECRET not in text and str(tmp_path) not in text
            assert info.value.__context__ is None
    rooted.close()
    with pytest.raises(TargetColumnTypeUnsupported) as winfo:
        write_avro(
            pa.table({"x": pa.array([2**64 - 1], pa.uint64())}),
            tmp_path / "w.avro",
            table="t",
            file_access=RootRelative(tmp_path),
        )
    assert str(2**64 - 1) not in str(winfo.value)


# AT-B9: the in-memory decode opens nothing ------------------------------------------------------


def test_decode_group_opens_nothing(tmp_path: Path) -> None:
    payload = container(record([("x", "long")]), [(2, zz(1) + zz(2))])
    with fileaccess.scope(PreopenedFileAccess({})):
        out = avro_io._decode_group(payload, name="t")
    assert out.column("x").to_pylist() == [1, 2]


def test_nested_chunk_route_uses_existing_code_when_cut(tmp_path: Path) -> None:
    # Nested types never reach cut_batch on the Avro chunked route; the refusal is admission's.
    path = tmp_path / "n.avro"
    path.write_bytes(
        container(record([("a", {"type": "array", "items": "long"})]), [(1, zz(1) + zz(1) + zz(0))])
    )
    with pytest.raises(BoundedReadError) as info:
        list(iter_avro_chunks(str(path), _budget(), table="t"))
    assert not isinstance(info.value, UnsupportedColumnType)
    assert os.path.exists(path)


def test_a_polars_panic_becomes_a_coded_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission refuses `map`; with admission out of the way Polars panics (a BaseException),
    and the decode wrapper still turns that into `AvroMalformed` with no context."""
    from decoy_engine.io import avro_schema

    monkeypatch.setattr(
        avro_io.avro_schema, "admit", lambda *a, **k: avro_schema.Admission(0, True)
    )
    path = tmp_path / "m.avro"
    path.write_bytes(container(record([("m", {"type": "map", "values": "long"})]), [(1, zz(0))]))
    with pytest.raises(AvroMalformed) as info:
        read_avro(str(path), table="t")
    assert info.value.detail["reason"] == "decode"
    assert info.value.__context__ is None and info.value.__cause__ is None
