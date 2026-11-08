"""A1: opaque, reserved and newer records are results, never errors."""

from __future__ import annotations

from decoy_engine import records
from decoy_engine.records import canonical_bytes
from tests.unit.records._helpers import SHA_B, make, raw_of, seal


def test_a_reserved_kind_written_by_a_later_version_is_opaque() -> None:
    raw = raw_of(make())
    raw.update(kind="publication", payload_schema="publication/v1", payload={"anything": 1})
    got = records.decode(canonical_bytes(seal(raw)))
    assert isinstance(got, records.OpaqueRecord) and got.kind == "publication"


def test_opaque_record_keeps_every_field_for_a_byte_exact_copy() -> None:
    raw = raw_of(make())
    raw.update(kind="future-thing", payload_schema="future-thing/v3", payload={"z": 1, "a": [2]})
    raw["links"] = [{"rel": "supersedes", "id": SHA_B}]
    sealed = seal(raw)
    got = records.decode(canonical_bytes(sealed))
    assert isinstance(got, records.OpaqueRecord)
    assert records.encode(got.raw) == canonical_bytes(sealed)
    assert got.links[0]["id"] == SHA_B
