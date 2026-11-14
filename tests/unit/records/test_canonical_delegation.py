"""The records codec rides the one canonical JSON owner without moving a byte or a refusal."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from decoy_engine import reasons, records
from decoy_engine._canonical import canonical_json_bytes
from decoy_engine.records import payload_digest, record_id
from decoy_engine.records._canonical import canonical_bytes

_BODY = {"name": "café", "n": 1, "t": "日本", "z": [1, 2]}


def test_canonical_bytes_for_non_ascii_text_equals_its_pre_delegation_bytes() -> None:
    assert canonical_bytes(_BODY) == (
        b'{"n":1,"name":"caf\xc3\xa9","t":"\xe6\x97\xa5\xe6\x9c\xac","z":[1,2]}'
    )
    assert canonical_bytes(_BODY) == canonical_json_bytes(_BODY)


def test_payload_digest_and_record_id_do_not_move() -> None:
    assert payload_digest(_BODY) == (
        "sha256:dae60dbfc3af8f612e98fa5c920eecd74778751c992ccd844234ff2ecec0b96f"
    )
    envelope = {"kind": "x", "payload": _BODY, "id": "ignored"}
    assert record_id(envelope) == (
        "sha256:54be08f78f85b04208601c1f1db04e75ed55663ce94c308c5d987dcd165ab5f2"
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"x": float("nan")},
        {"x": {"member"}},
        {"x": dt.datetime(2024, 1, 2, 3, 4, 5)},
        {"x": "before\ud800after"},
    ],
)
def test_encode_turns_every_refusal_into_the_coded_record_error(bad: dict[str, Any]) -> None:
    with pytest.raises(records.RecordError) as caught:
        records.encode(bad)
    assert caught.value.code == reasons.runtime.RECORD_INVALID


@pytest.mark.parametrize("bad", [float("nan"), {"member"}, dt.datetime(2024, 1, 2)])
def test_canonical_bytes_refusals_stay_value_errors(bad: object) -> None:
    with pytest.raises(ValueError):
        canonical_bytes({"x": bad})
