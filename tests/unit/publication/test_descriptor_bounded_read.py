"""F22 5.2: descriptor reads and writes are capped at 1 MiB.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 row `test_descriptor_bounded_read`
(slice a2; class 6). The cap is checked on the bytes before any decode, and the writer refuses a
descriptor the reader would refuse, so no bound unit carries one it cannot read back.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.publication import _doc

PREFIX = "descriptor_refused:"


def _codec() -> Any:
    from decoy_engine.publication import _descriptor

    return _descriptor


def test_descriptor_bounded_read() -> None:
    codec = _codec()
    assert codec.MAX_DESCRIPTOR_BYTES == _doc.MIB
    raw = _doc.encode(_doc.doc())
    codec.decode_descriptor(raw)  # non-vacuity: the unpadded document is accepted
    over = raw + b" " * (_doc.MIB + 1 - len(raw))
    at = raw + b" " * (_doc.MIB - len(raw))
    assert len(over) == _doc.MIB + 1 and len(at) == _doc.MIB
    with pytest.raises(ValueError) as caught:
        codec.decode_descriptor(over)
    assert str(caught.value) == PREFIX + "oversize"
    # At the cap the bytes are read and refused for what they are, not for their size.
    with pytest.raises(ValueError) as caught:
        codec.decode_descriptor(at)
    assert str(caught.value) == PREFIX + "not_canonical"


def test_deep_nesting_under_the_cap_is_refused_not_raised() -> None:
    codec = _codec()
    raw = b"[" * 200_000 + b"]" * 200_000
    assert len(raw) < _doc.MIB  # non-vacuity: the cap is not what refuses it
    with pytest.raises(ValueError) as caught:
        codec.decode_descriptor(raw)
    assert str(caught.value) == PREFIX + "not_json"


def test_only_bytes_are_read() -> None:
    codec = _codec()
    text = _doc.encode(_doc.doc()).decode("ascii")
    with pytest.raises(ValueError) as caught:
        codec.decode_descriptor(text)
    assert str(caught.value) == PREFIX + "wrong_type"


def test_writer_refuses_over_the_cap() -> None:
    codec = _codec()
    small = [
        codec.Artifact(role="table", path="t0.parquet", size=1, sha256="0" * 64, rows=1),
    ]
    codec.encode_descriptor(
        codec.build_descriptor(operation_id="op-1", artifacts=small, vault=None)
    )
    many = [
        codec.Artifact(role="table", path=f"t{i:06d}.parquet", size=1, sha256="0" * 64, rows=1)
        for i in range(10_000)
    ]
    built = codec.build_descriptor(operation_id="op-1", artifacts=many, vault=None)
    assert len(many) * 120 > _doc.MIB  # non-vacuity: the entries alone pass the cap
    with pytest.raises(ValueError) as caught:
        codec.encode_descriptor(built)
    assert str(caught.value) == PREFIX + "oversize"
