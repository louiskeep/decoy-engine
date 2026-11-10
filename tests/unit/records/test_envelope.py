"""A1: the envelope, the one id rule, strict writes and tolerant reads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import records
from decoy_engine.records import RecordError, canonical_bytes, record_id
from tests.unit.records._helpers import (
    CORPUS,
    PRODUCER,
    SHA_B,
    corpus,
    make,
    raw_of,
    sample_payload,
    seal,
)

ROOT = Path(__file__).resolve().parents[3]
REFERENCE_ID = json.loads((CORPUS / "id-stable.json").read_text(encoding="utf-8"))


def test_id_stable_across_processes_and_machines() -> None:
    """A1-id-stable: the same inputs give the id recorded in the corpus, in a fresh process."""
    assert make().id == REFERENCE_ID["id"]
    code = "from tests.unit.records._helpers import make; print(make().id)"  # fresh hash seed
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONHASHSEED": "random", "PYTHONPATH": str(ROOT)},
    ).stdout.strip()
    assert out == REFERENCE_ID["id"]


def _edits(raw: dict[str, Any]) -> list[dict[str, Any]]:
    def with_(**kw: Any) -> dict[str, Any]:
        return {**raw, **kw}

    return [
        with_(kind="other"),
        with_(payload_schema="sample-run/v2"),
        with_(created_at="2026-11-08T12:00:00.123457Z"),
        with_(invocation_id="11111111-2222-4333-8444-555555555556"),
        with_(producer={**raw["producer"], "version": "0.9.1"}),
        with_(labels={**raw["labels"], "status": "failed"}),
        with_(links=[{"rel": "baseline", "id": SHA_B}]),
        with_(payload_digest=SHA_B),
        with_(payload={**raw["payload"], "ttl_seconds": 1}),
    ]


def test_id_covers_every_envelope_field_and_payload_byte() -> None:
    """A1-id-covers-all."""
    raw = raw_of(make())
    assert all(record_id(edit) != raw["id"] for edit in _edits(raw))


def test_id_ignores_file_formatting() -> None:
    """A1-id-parsed: CRLF and two-space indent decode to the same id."""
    env = make()
    reformatted = json.dumps(raw_of(env), indent=2, ensure_ascii=False).replace("\n", "\r\n")
    decoded = records.decode(reformatted.encode("utf-8"))
    assert isinstance(decoded, records.DecodedRecord)
    assert decoded.id == env.id


def test_build_id_matches_the_id_rule_over_the_stored_dict() -> None:
    env = make()
    assert record_id(env.to_dict()) == env.id
    assert records.decode(records.encode(env)).id == env.id  # type: ignore[union-attr]


def test_canonical_bytes_are_utf8_sorted_and_compact() -> None:
    assert canonical_bytes({"b": "é", "a": [1, 2]}) == '{"a":[1,2],"b":"é"}'.encode()
    with pytest.raises(ValueError):
        canonical_bytes({"a": float("nan")})


def test_strict_payload_names_the_field_not_the_value() -> None:
    """A1-strict-payload."""
    secret = "jane.doe@example.com"
    with pytest.raises(RecordError) as caught:
        make(payload=sample_payload(free_text=secret))
    assert caught.value.code == "record_invalid"
    assert caught.value.fields == ("payload.free_text",)
    assert secret not in str(caught.value) and secret not in repr(caught.value)


def test_error_locations_never_echo_caller_keys() -> None:
    bad = sample_payload(table_rows={"jane.doe@example.com": -1})
    with pytest.raises(RecordError) as caught:
        make(payload=bad)
    assert "jane" not in str(caught.value)


def _newer_payload_record() -> dict[str, Any]:
    raw = raw_of(make())
    raw["payload"]["new_optional"] = {"later": True}
    raw["payload"]["outputs"][0]["later_field"] = 7
    return seal(raw)


def test_read_is_tolerant_but_keeps_raw() -> None:
    """A1-read-tolerant and A1-raw-preserved."""
    newer = _newer_payload_record()
    original = canonical_bytes(newer)
    decoded = records.decode(original)
    assert isinstance(decoded, records.DecodedRecord)
    assert not hasattr(decoded.view, "new_optional")
    assert not hasattr(decoded.view.outputs[0], "later_field")  # type: ignore[attr-defined]
    assert decoded.raw["payload"]["new_optional"] == {"later": True}
    assert records.encode(decoded.raw) == original
    assert decoded.id == newer["id"]


def test_read_still_checks_types_of_known_fields() -> None:
    bad = raw_of(make())
    bad["payload"]["sample_size"] = "50"
    with pytest.raises(RecordError) as caught:
        records.decode(canonical_bytes(seal(bad)))
    assert caught.value.code == "record_invalid"


def test_opaque_kind_decodes_without_a_model() -> None:
    """A1-opaque-kind."""
    raw = raw_of(make())
    raw.update(kind="future-thing", payload_schema="future-thing/v1", payload={"anything": [1]})
    raw["links"] = [{"rel": "scan", "id": SHA_B}]
    sealed = seal(raw)
    got = records.decode(canonical_bytes(sealed))
    assert isinstance(got, records.OpaqueRecord)
    assert (got.kind, got.id, got.created_at) == ("future-thing", sealed["id"], raw["created_at"])
    assert got.labels == raw["labels"] and got.links == tuple(raw["links"])


@pytest.mark.parametrize(
    "labels",
    [{"exit_code": 1.5}, {"exit_code": True}, {"status": 1.0}, {"tables": [1.5]}, {"unknown": "x"}],
)
def test_labels_are_closed_and_strict(labels: dict[str, Any]) -> None:
    """A1-no-float-envelope."""
    with pytest.raises(RecordError) as caught:
        make(labels=labels)
    assert caught.value.code == "record_invalid"


def test_non_finite_payload_float_is_refused() -> None:
    with pytest.raises(RecordError) as caught:
        make(
            "evidence",
            {
                **corpus("evidence-cli-local-1.json"),
                "timings": [
                    {
                        "strategy_type": "hash",
                        "column": "c",
                        "elapsed_ms": float("nan"),
                        "peak_memory_delta_kb": None,
                    }
                ],
            },
        )
    assert caught.value.code == "record_invalid"


@pytest.mark.parametrize("kind", ["publication", "checkpoint"])
def test_reserved_kind_cannot_be_built(kind: str) -> None:
    """A1-reserved."""
    with pytest.raises(RecordError) as caught:
        make(kind, {})
    assert caught.value.code == "record_kind_reserved"


def test_unknown_kind_cannot_be_built() -> None:
    with pytest.raises(RecordError) as caught:
        make("future-thing", {})
    assert caught.value.code == "record_invalid"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"ttl_seconds":86400', '"ttl_seconds":1'),  # payload and digest disagree
        ('"status":"ok"', '"status":"bad"'),  # only the id disagrees
    ],
)
def test_a_changed_value_fails_decode(old: str, new: str) -> None:
    """A1-decode-verify: each defense is exercised alone."""
    text = records.encode(make()).decode("utf-8")
    assert old in text
    with pytest.raises(RecordError) as caught:
        records.decode(text.replace(old, new))
    assert caught.value.code == "record_corrupt"


@pytest.mark.parametrize("data", [b"", b"[]", b"not json", b'{"a":1,"a":2}', b'{"x":NaN}'])
def test_garbage_is_corrupt(data: bytes) -> None:
    with pytest.raises(RecordError) as caught:
        records.decode(data)
    assert caught.value.code == "record_corrupt"


def test_newer_payload_major_is_a_result_not_an_error() -> None:
    """A1-newer."""
    raw = raw_of(make())
    raw["payload_schema"] = "sample-run/v9"
    got = records.decode(canonical_bytes(seal(raw)))
    assert isinstance(got, records.RecordNeedsNewer)
    assert got.scope == "payload" and got.payload_schema == "sample-run/v9"


def test_older_or_foreign_payload_tag_is_corrupt() -> None:
    for tag in ("sample-run/v0", "other/v1"):
        raw = raw_of(make())
        raw["payload_schema"] = tag
        with pytest.raises(RecordError):
            records.decode(canonical_bytes(seal(raw)))


def test_newer_envelope_major_is_a_result() -> None:
    raw = raw_of(make())
    raw["record_schema"] = "decoy-record/v2"
    got = records.decode(canonical_bytes(raw))
    assert isinstance(got, records.RecordNeedsNewer) and got.scope == "envelope"


def test_naive_created_at_is_refused() -> None:
    from datetime import datetime

    with pytest.raises(RecordError):
        make(created_at=datetime(2026, 11, 8, 12, 0, 0))


def test_producer_and_link_shapes_are_checked() -> None:
    with pytest.raises(RecordError):
        make(producer={**PRODUCER, "extra": "x"})
    with pytest.raises(RecordError):
        make(links=[{"rel": "nonsense", "id": SHA_B}])
