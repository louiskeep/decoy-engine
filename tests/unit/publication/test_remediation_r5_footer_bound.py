"""F22 r5 HIGH 2: the parent parses a child's Parquet footer under a fixed budget.

The child is untrusted. Before any Thrift parse the parent reads the 8-byte trailer and
refuses a declared footer length over the cap; the parse itself runs with string and
container limits; memory exhaustion and parse failures become `child_envelope_invalid`.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.publication import _child as child


def _req(dest: Path) -> pub.PublicationRequest:
    return pub.PublicationRequest(destination=dest, operation_id="op-1")


def _staged(
    root: Path, rows: int = 2, **write: object
) -> tuple[Path, pub.ChildPreparationEnvelope]:
    d = root / "assigned"
    d.mkdir()
    pq.write_table(pa.table({"id": list(range(rows))}), d / "t.parquet", **write)  # type: ignore[arg-type]
    return d, pub.ChildPreparationEnvelope(d, ("t",), (("t", rows),))


def _refusal(env: pub.ChildPreparationEnvelope, dest: Path) -> pub.PublicationRefusedError:
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.verify_child_envelope(env, _req(dest))
    return exc.value


def _forbid_parse(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def spy(*_a: object, **_k: object) -> None:
        seen.append("parse")
        raise AssertionError("the footer was parsed")

    monkeypatch.setattr(child.pq, "ParquetFile", spy)
    monkeypatch.setattr(child.pq, "read_metadata", spy)
    return seen


def test_a_valid_small_footer_passes(tmp_path: Path) -> None:
    _, env = _staged(tmp_path)
    pub.verify_child_envelope(env, _req(tmp_path / "out"))


def test_a_declared_oversized_footer_is_refused_before_any_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, env = _staged(tmp_path)
    raw = bytearray((d / "t.parquet").read_bytes())
    raw[-8:-4] = struct.pack("<I", child.MAX_FOOTER_BYTES + 1)  # lie about the footer length
    (d / "t.parquet").write_bytes(bytes(raw))
    seen = _forbid_parse(monkeypatch)
    assert _refusal(env, tmp_path / "out").receipt.reason_code == "child_envelope_invalid"
    assert seen == []


def test_a_footer_length_beyond_the_file_is_refused_before_any_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, env = _staged(tmp_path)
    raw = bytearray((d / "t.parquet").read_bytes())
    raw[-8:-4] = struct.pack("<I", len(raw))  # under the cap, but longer than the file
    (d / "t.parquet").write_bytes(bytes(raw))
    seen = _forbid_parse(monkeypatch)
    assert _refusal(env, tmp_path / "out").receipt.reason_code == "child_envelope_invalid"
    assert seen == []


@pytest.mark.parametrize("blob", [b"", b"PAR1", b"PAR1PAR1", b"x" * 7])
def test_a_file_too_short_for_a_trailer_is_refused(tmp_path: Path, blob: bytes) -> None:
    d, env = _staged(tmp_path)
    (d / "t.parquet").write_bytes(blob)
    assert _refusal(env, tmp_path / "out").receipt.reason_code == "child_envelope_invalid"


def test_a_wrong_trailing_magic_is_refused_before_any_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, env = _staged(tmp_path)
    raw = bytearray((d / "t.parquet").read_bytes())
    raw[-4:] = b"NOPE"
    (d / "t.parquet").write_bytes(bytes(raw))
    seen = _forbid_parse(monkeypatch)
    assert _refusal(env, tmp_path / "out").receipt.reason_code == "child_envelope_invalid"
    assert seen == []


def test_memory_exhaustion_during_the_parse_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, env = _staged(tmp_path)

    def oom(*_a: object, **_k: object) -> None:
        raise MemoryError

    monkeypatch.setattr(child.pq, "ParquetFile", oom)
    refusal = _refusal(env, tmp_path / "out")
    assert refusal.receipt.reason_code == "child_envelope_invalid"
    assert isinstance(refusal.__cause__, MemoryError)


@pytest.mark.parametrize(
    "exc", [pa.ArrowInvalid("x"), OSError(5, "x"), ValueError("x"), OverflowError()]
)
def test_parse_failures_are_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _, env = _staged(tmp_path)

    def bad(*_a: object, **_k: object) -> None:
        raise exc

    monkeypatch.setattr(child.pq, "ParquetFile", bad)
    assert _refusal(env, tmp_path / "out").receipt.reason_code == "child_envelope_invalid"


def test_the_parse_runs_with_thrift_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, env = _staged(tmp_path)
    real = pq.ParquetFile
    kwargs: list[dict[str, object]] = []

    def spy(*a: object, **k: object) -> pq.ParquetFile:
        kwargs.append(dict(k))
        return real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(child.pq, "ParquetFile", spy)
    pub.verify_child_envelope(env, _req(tmp_path / "out"))
    assert kwargs[0]["thrift_string_size_limit"] == child.MAX_THRIFT_STRING_BYTES
    assert kwargs[0]["thrift_container_size_limit"] == child.MAX_THRIFT_CONTAINER_ITEMS


def test_an_oversized_thrift_string_in_the_footer_is_refused_by_the_real_parser(
    tmp_path: Path,
) -> None:
    # A footer well under the byte cap whose key-value string exceeds the string limit.
    big = "A" * (child.MAX_THRIFT_STRING_BYTES + 1024)
    d = tmp_path / "assigned"
    d.mkdir()
    table = pa.table({"id": [1, 2]}).replace_schema_metadata({"k": big})
    pq.write_table(table, d / "t.parquet")
    size = (d / "t.parquet").stat().st_size
    assert size < child.MAX_FOOTER_BYTES * 2
    env = pub.ChildPreparationEnvelope(d, ("t",), (("t", 2),))
    assert _refusal(env, tmp_path / "out").receipt.reason_code == "child_envelope_invalid"


def test_a_wide_but_legitimate_footer_still_passes(tmp_path: Path) -> None:
    d = tmp_path / "assigned"
    d.mkdir()
    table = pa.table({f"c{i}": [i] for i in range(2000)})
    pq.write_table(table, d / "t.parquet")
    env = pub.ChildPreparationEnvelope(d, ("t",), (("t", 1),))
    pub.verify_child_envelope(env, _req(tmp_path / "out"))
