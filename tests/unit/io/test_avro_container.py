"""Avro container framing, codecs and schema admission (plan V1P-4.1, AT-A1 to AT-A4)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.io import (
    AvroBlockTooLarge,
    AvroMalformed,
    AvroSchemaUnsupported,
    ChunkBudget,
    avro_io,
    iter_avro_chunks,
    read_avro,
)
from decoy_engine.io import avro_container as c
from tests.unit.io._avro_build import (
    LONG_SCHEMA,
    SYNC,
    astr,
    block,
    container,
    deflate,
    header,
    long_file,
    long_rows,
    record,
    snappy,
    zz,
)

BIG = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=1 << 20, max_row_bytes=1 << 16)


def _write(tmp_path: Path, raw: bytes, name: str = "f.avro") -> str:
    path = tmp_path / name
    path.write_bytes(raw)
    return str(path)


def _clean(exc: BaseException) -> None:
    assert exc.__cause__ is None and exc.__context__ is None


def _refused(tmp_path: Path, raw: bytes, kind: type[Exception] = AvroMalformed) -> Exception:
    path = _write(tmp_path, raw)
    with pytest.raises(kind) as info:
        read_avro(path, table="t")
    _clean(info.value)
    return info.value


# AT-A1 ------------------------------------------------------------------------------------


def test_header_and_blocks_are_framed() -> None:
    raw = long_file(blocks=3, per_block=4)
    stream = io.BytesIO(raw)
    head = c.read_header(stream, len(raw), table="t")
    assert json.loads(head.schema_json) == LONG_SCHEMA
    assert head.codec == "null" and head.sync == SYNC
    blocks = list(c.iter_blocks(stream, head, len(raw), table="t"))
    assert [b.count for b in blocks] == [4, 4, 4]
    assert blocks[1].data == long_rows(4, 4)


def test_missing_codec_means_null(tmp_path: Path) -> None:
    raw = header(LONG_SCHEMA, codec=None) + block(2, long_rows(0, 2))
    assert read_avro(_write(tmp_path, raw), table="t").column("x").to_pylist() == [0, 1]


def test_varint_round_trip() -> None:
    for value in (0, 1, -1, 63, -64, 2**31, -(2**63), 2**63 - 1):
        src = c._Bounded(io.BytesIO(c.write_long(value)), 10)
        assert c.read_long(src) == value


# AT-A2: framing refusals ------------------------------------------------------------------


def test_bad_magic_and_empty(tmp_path: Path) -> None:
    raw = long_file()
    assert _refused(tmp_path, b"XXXX" + raw[4:]).detail["reason"] == "magic"
    _refused(tmp_path, b"")


def test_header_cut_at_every_offset(tmp_path: Path) -> None:
    raw = long_file(blocks=1)
    head_len = len(header(LONG_SCHEMA))
    for cut in range(head_len):
        _refused(tmp_path, raw[:cut])


def test_last_block_cut_at_every_offset_but_the_boundary(tmp_path: Path) -> None:
    raw = long_file(blocks=2)
    first_end = len(header(LONG_SCHEMA)) + len(block(4, long_rows(0, 4)))
    for cut in range(first_end + 1, len(raw)):
        _refused(tmp_path, raw[:cut])
    # Cut exactly at a block boundary: a valid shorter file (the format has no end marker).
    assert read_avro(_write(tmp_path, raw[:first_end]), table="t").num_rows == 4


def test_sync_mismatch(tmp_path: Path) -> None:
    raw = bytearray(long_file(blocks=2))
    raw[-1] ^= 0xFF
    assert _refused(tmp_path, bytes(raw)).detail["reason"] == "sync"


@pytest.mark.parametrize(
    ("count", "size"),
    [(-1, 4), (2, -1), (0, 4), (5, 4)],
    ids=["neg_count", "neg_size", "zero", "count_gt_size"],
)
def test_bad_block_header(tmp_path: Path, count: int, size: int) -> None:
    raw = header(LONG_SCHEMA) + zz(count) + zz(size) + b"\x00" * 4 + SYNC
    assert _refused(tmp_path, raw).detail["reason"] == "block_header"


def test_size_past_eof_is_refused_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = header(LONG_SCHEMA) + zz(1) + zz(10_000_000) + b"\x02"
    reads: list[int] = []
    real = c._Bounded.read

    def spy(self: c._Bounded, n: int) -> bytes:
        reads.append(n)
        return real(self, n)

    monkeypatch.setattr(c._Bounded, "read", spy)
    _refused(tmp_path, raw)
    assert max(reads) < 10_000_000


def test_size_over_absolute_cap(tmp_path: Path) -> None:
    path = tmp_path / "sparse.avro"
    head = header(LONG_SCHEMA) + zz(1) + zz(c.MAX_BLOCK_BYTES + 1)
    with open(path, "wb") as fh:
        fh.write(head)
        fh.truncate(len(head) + c.MAX_BLOCK_BYTES + 64)
    with pytest.raises(AvroBlockTooLarge) as info:
        read_avro(str(path), table="t")
    _clean(info.value)


def test_varint_limits(tmp_path: Path) -> None:
    eleven = b"\xff" * 10 + b"\x01"
    _refused(tmp_path, header(LONG_SCHEMA) + eleven)
    # Ten bytes whose value exceeds int64 after zigzag decoding.
    too_big = b"\xff" * 9 + b"\x7f"
    _refused(tmp_path, header(LONG_SCHEMA) + too_big)


def test_header_over_cap_through_negative_form_map(tmp_path: Path) -> None:
    value = b"x" * (c.MAX_HEADER_BYTES + 10)
    meta = zz(-1) + zz(len(value) + 20) + astr(b"pad") + astr(value) + zz(0)
    _refused(tmp_path, b"Obj\x01" + meta + SYNC)


def test_negative_form_map_whose_size_lies(tmp_path: Path) -> None:
    schema = json.dumps(LONG_SCHEMA).encode()
    meta = zz(-1) + zz(10_000_000) + astr(b"avro.schema") + astr(schema) + zz(0)
    _refused(tmp_path, b"Obj\x01" + meta + SYNC)


def test_schema_json_too_deep_and_invalid(tmp_path: Path) -> None:
    deep = b"[" * 100_000 + b"]" * 100_000
    assert _refused(tmp_path, header(deep)).detail["reason"] == "schema_json"
    assert _refused(tmp_path, header(b"\xff\xfe")).detail["reason"] == "schema_json"


def test_walk_depth_cap(tmp_path: Path) -> None:
    node: object = "long"
    for _ in range(70):
        node = {"type": "array", "items": node}
    raw = container(record([("x", node)]), [])
    assert _refused(tmp_path, raw, AvroSchemaUnsupported).detail["reason"] == "depth"


# AT-A2b: codecs ---------------------------------------------------------------------------


def test_deflate_and_snappy_read(tmp_path: Path) -> None:
    for codec in (b"deflate", b"snappy"):
        out = read_avro(_write(tmp_path, long_file(codec=codec)), table="t")
        assert out.column("x").to_pylist() == list(range(12))


def test_deflate_bomb_refused_without_inflating_past_cap(tmp_path: Path) -> None:
    bomb = deflate(b"\x00" * (8 << 20))
    raw = container(LONG_SCHEMA, [(1, bomb)], codec=b"deflate")
    budget = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=10, max_row_bytes=1 << 16)
    with pytest.raises(AvroBlockTooLarge) as info:
        list(iter_avro_chunks(_write(tmp_path, raw), budget, table="t"))
    _clean(info.value)
    assert "remedy" in info.value.detail


def test_deflate_trailing_garbage_and_truncated_stream(tmp_path: Path) -> None:
    good = deflate(long_rows(0, 2))
    for data in (good + b"GARBAGE", good[:-2], b"\xff\xff\xff\xff"):
        raw = container(LONG_SCHEMA, [(2, data)], codec=b"deflate")
        assert _refused(tmp_path, raw).detail["reason"] == "deflate"


def test_snappy_preamble_over_cap_and_crc_mismatch(tmp_path: Path) -> None:
    budget = ChunkBudget(chunk_budget_bytes=1024, max_chunk_rows=10, max_row_bytes=1024)
    lying = zz(0)[:0] + b"\xff\xff\xff\x7f" + b"\x00" * 8  # preamble claims ~256 MiB
    raw = container(LONG_SCHEMA, [(1, lying + b"\x00\x00\x00\x00")], codec=b"snappy")
    with pytest.raises(AvroBlockTooLarge):
        list(iter_avro_chunks(_write(tmp_path, raw), budget, table="t"))
    good = bytearray(snappy(long_rows(0, 2)))
    good[-1] ^= 0xFF
    raw = container(LONG_SCHEMA, [(2, bytes(good))], codec=b"snappy")
    assert _refused(tmp_path, raw).detail["reason"] == "snappy"


def test_block_over_budget_is_refused_on_chunked_route_and_read_whole(tmp_path: Path) -> None:
    raw = container(LONG_SCHEMA, [(500, long_rows(0, 500))])
    path = _write(tmp_path, raw)
    small = ChunkBudget(chunk_budget_bytes=1000, max_chunk_rows=1000, max_row_bytes=1000)
    with pytest.raises(AvroBlockTooLarge) as info:
        list(iter_avro_chunks(path, small, table="t"))
    assert info.value.detail["remedy"] == "raise the chunk budget or read without chunking"
    assert read_avro(path, table="t").num_rows == 500


def test_polars_only_sees_null_codec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bytes] = []
    real = avro_io._decode_group

    def spy(payload: bytes, *, name: str):  # type: ignore[no-untyped-def]
        seen.append(payload)
        return real(payload, name=name)

    monkeypatch.setattr(avro_io, "_decode_group", spy)
    for codec in (b"deflate", b"snappy"):
        read_avro(_write(tmp_path, long_file(codec=codec)), table="t")
    assert seen and all(b"\x14avro.codec\x08null" in p for p in seen)


@pytest.mark.parametrize("codec", [b"zstandard", b"bzip2", b"xz"])
def test_unsupported_codec(tmp_path: Path, codec: bytes) -> None:
    raw = container(LONG_SCHEMA, [(2, long_rows(0, 2))], codec=codec)
    assert _refused(tmp_path, raw, AvroSchemaUnsupported).detail["reason"] == "codec"


# AT-A3: schema admission ------------------------------------------------------------------

_FX = {"type": "fixed", "name": "F", "size": 4}
REFUSED = {
    "map": {"type": "map", "values": "int"},
    "union3": ["null", "int", "string"],
    "union_null_only": ["null"],
    "union_single": ["int"],
    "duration": {"type": "fixed", "name": "D", "size": 12, "logicalType": "duration"},
    "ts_nanos": {"type": "long", "logicalType": "timestamp-nanos"},
    "local_ts_nanos": {"type": "long", "logicalType": "local-timestamp-nanos"},
    "decimal_p39": {"type": "bytes", "logicalType": "decimal", "precision": 39, "scale": 0},
    "decimal_fixed_too_small": {
        "type": "fixed",
        "name": "S",
        "size": 1,
        "logicalType": "decimal",
        "precision": 3,
    },
    "uuid_fixed": {"type": "fixed", "name": "U", "size": 16, "logicalType": "uuid"},
    "date_on_string": {"type": "string", "logicalType": "date"},
    "empty_record": {"type": "record", "name": "E", "fields": []},
    "fixed_zero": {"type": "fixed", "name": "Z", "size": 0},
    "bare_null": "null",
    "recursive": {
        "type": "record",
        "name": "Node",
        "fields": [{"name": "next", "type": ["null", "Node"]}],
    },
    "undefined_name": "Nowhere",
    "wrapped_schema": {"type": {"type": {"type": "long"}}},
    "wrapped_union": {"type": ["null", "long"]},
    "logical_type_not_a_string": {"type": "long", "logicalType": {}},
    "fixed_logical_type_list": {"type": "fixed", "name": "G", "size": 4, "logicalType": ["x"]},
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_refused_schema_elements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    called: list[int] = []
    monkeypatch.setattr(avro_io, "_decode_group", lambda *a, **k: called.append(1))
    raw = container(record([("x", REFUSED[name])]), [])
    exc = _refused(tmp_path, raw, AvroSchemaUnsupported)
    assert exc.detail["field"].startswith("x") and not called


def test_top_level_must_be_a_record(tmp_path: Path) -> None:
    raw = container({"type": "array", "items": "int"}, [])
    assert _refused(tmp_path, raw, AvroSchemaUnsupported).detail["reason"] == "top_level"


def test_nested_refused_on_chunked_route_only(tmp_path: Path) -> None:
    schema = record([("x", {"type": "array", "items": "int"})])
    raw = container(schema, [(1, zz(1) + zz(3) + zz(0))])
    path = _write(tmp_path, raw)
    with pytest.raises(AvroSchemaUnsupported):
        list(iter_avro_chunks(path, BIG, table="t"))
    assert read_avro(path, table="t").column("x").to_pylist() == [[3]]


def test_unknown_logical_type_is_ignored(tmp_path: Path) -> None:
    raw = container(record([("x", {"type": "int", "logicalType": "foo"})]), [(1, zz(7))])
    assert read_avro(_write(tmp_path, raw), table="t").column("x").to_pylist() == [7]


def test_named_fixed_defined_inline_reads_and_a_reference_is_refused(tmp_path: Path) -> None:
    ok = {"type": "record", "name": "r", "namespace": "a.b", "fields": [{"name": "x", "type": _FX}]}
    out = read_avro(_write(tmp_path, container(ok, [(1, b"abcd")])), table="t")
    assert out.to_pylist() == [{"x": b"abcd"}]
    for ref in ("F", "a.b.F"):
        schema = {**ok, "fields": [{"name": "x", "type": _FX}, {"name": "y", "type": ref}]}
        exc = _refused(tmp_path, container(schema, [(1, b"abcd" * 2)]), AvroSchemaUnsupported)
        assert exc.detail == {"table": "t", "field": "y", "reason": "named_reference"}


# AT-A4: grouping ----------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(
    sizes=st.lists(st.integers(1, 30), min_size=1, max_size=12),
    budget_rows=st.integers(1, 40),
)
def test_grouping_respects_budget_and_is_pure(sizes: list[int], budget_rows: int) -> None:
    raw = container(LONG_SCHEMA, [(n, long_rows(0, n)) for n in sizes])
    groups: list[list[int]] = []
    for _ in range(2):
        source = avro_io._Source.__new__(avro_io._Source)
        stream = io.BytesIO(raw)
        source.name = "t"
        source.length = len(raw)
        source.stream = stream
        source.header = c.read_header(stream, len(raw), table="t")
        from decoy_engine.io import avro_schema

        source.admission = avro_schema.admit(source.header.schema_json, table="t")
        source.null_header = c.encode_header(source.header.schema_json, source.header.sync)
        limits = avro_io._Limits(1 << 20, 1 << 20, budget_rows, False)
        groups.append([t.num_rows for t in source.groups(limits)])
    assert groups[0] == groups[1]
    assert sum(groups[0]) == sum(sizes)
    for rows in groups[0]:
        assert rows <= budget_rows or rows in sizes  # a single block over the row cap stands alone


# Code review round 1 -------------------------------------------------------------------------

_BOOLS = record([("b", "boolean")])


@pytest.mark.parametrize("codec", [b"deflate", b"snappy"])
def test_highly_compressible_blocks_are_read_on_every_path(tmp_path: Path, codec: bytes) -> None:
    from decoy_engine.profile import build_profile_source

    raw_rows = b"\x01" * 10_000  # 10,000 booleans compress to far fewer than 10,000 bytes
    data = deflate(raw_rows) if codec == b"deflate" else snappy(raw_rows)
    assert len(data) < 10_000
    path = _write(tmp_path, container(_BOOLS, [(10_000, data)], codec=codec))
    chunks = list(iter_avro_chunks(path, BIG, table="t"))
    assert sum(c.num_rows for c in chunks) == 10_000
    source = build_profile_source({"type": "file", "format": "avro", "path": path})
    assert source.row_count().value == 10_000
    assert len(source.sample_frame(10)) == 10


def test_a_count_the_bytes_cannot_hold_is_refused_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[int] = []
    monkeypatch.setattr(avro_io, "_decode_group", lambda *a, **k: called.append(1))
    # Each record of LONG_SCHEMA is at least 2 bytes (a long and a string length).
    lying_null = container(LONG_SCHEMA, [(1_000_000, b"\x00" * 10)])
    assert _refused(tmp_path, lying_null).detail["reason"] == "block_header"
    lying_deflate = container(LONG_SCHEMA, [(1_000, deflate(b"\x00" * 10))], codec=b"deflate")
    assert _refused(tmp_path, lying_deflate).detail["reason"] == "block_header"
    with pytest.raises(AvroMalformed):
        list(iter_avro_chunks(_write(tmp_path, lying_null), BIG, table="t"))
    assert not called


def test_a_block_whose_estimate_exceeds_the_budget_is_refused_on_the_chunked_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = 1 << 20  # one byte each (long zero), decoding to 8 bytes plus validity per row
    path = _write(tmp_path, container(record([("x", "long")]), [(rows, b"\x00" * rows)]))
    budget = ChunkBudget(chunk_budget_bytes=2 << 20, max_chunk_rows=1 << 22, max_row_bytes=1 << 16)
    called: list[int] = []
    real = avro_io._decode_group
    monkeypatch.setattr(avro_io, "_decode_group", lambda *a, **k: called.append(1) or real(*a, **k))
    with pytest.raises(AvroBlockTooLarge) as info:
        list(iter_avro_chunks(path, budget, table="t"))
    assert info.value.detail["remedy"] and not called
    assert read_avro(path, table="t").num_rows == rows  # the whole-file read takes it


def test_unknown_codec_is_not_echoed(tmp_path: Path) -> None:
    raw = container(LONG_SCHEMA, [(2, long_rows(0, 2))], codec=b"SECRET-CODEC-NAME")
    exc = _refused(tmp_path, raw, AvroSchemaUnsupported)
    assert exc.detail["codec"] == "unknown" and "SECRET" not in str(exc)


def test_metadata_entry_cap(tmp_path: Path) -> None:
    schema = json.dumps(LONG_SCHEMA).encode()
    many = zz(c.MAX_META_ENTRIES) + (astr(b"") + astr(b"")) * c.MAX_META_ENTRIES
    meta = zz(1) + astr(b"avro.schema") + astr(schema) + many + zz(0)
    assert _refused(tmp_path, b"Obj\x01" + meta + SYNC).detail["reason"] == "header"


_SLOT_KEYS = [
    "type",
    "name",
    "fields",
    "items",
    "values",
    "symbols",
    "size",
    "logicalType",
    "precision",
    "scale",
    "namespace",
]
_JSON_LEAF = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-3, 40),
    st.sampled_from(
        [
            "null",
            "long",
            "string",
            "record",
            "enum",
            "array",
            "map",
            "fixed",
            "bytes",
            "decimal",
            "date",
            "uuid",
            "duration",
            "timestamp-millis",
            "r",
            "x",
            "",
        ]
    ),
)
_JSON = st.recursive(
    _JSON_LEAF,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.sampled_from(_SLOT_KEYS), inner, max_size=5),
    ),
    max_leaves=25,
)


@settings(max_examples=400, deadline=None, derandomize=True)
@given(field_type=_JSON, top=st.dictionaries(st.sampled_from(_SLOT_KEYS), _JSON, max_size=3))
def test_any_schema_tree_is_admitted_or_refused_with_a_code(field_type: object, top: dict) -> None:  # type: ignore[type-arg]
    from decoy_engine.io import avro_schema

    for schema in (
        {"type": "record", "name": "r", "fields": [{"name": "f", "type": field_type}]},
        {"type": "record", "fields": [{"name": "f", "type": "long"}], **top},
        field_type,
    ):
        raw = json.dumps(schema).encode()
        try:
            avro_schema.admit(raw, table="t")
        except (AvroSchemaUnsupported, AvroMalformed) as exc:
            assert exc.__context__ is None
