"""Wire readers fail closed: schema tags, header field types and JSON parsing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from decoy_engine import records
from decoy_engine.records import RecordError
from tests.unit.records._helpers import PRODUCER, SHA_B, T0, make, raw_of, seal
from tests.unit.records.test_joblog import HEADER

BAD_TAGS = ["decoy-record/v0", "decoy-record/vbanana", "decoy-record/v99999", "decoy-record/v2\n"]


def _with_schema(tag: object) -> bytes:
    raw = raw_of(make())
    if tag is None:
        del raw["record_schema"]
    else:
        raw["record_schema"] = tag
    return records.encode(seal(raw))


@pytest.mark.parametrize("tag", [*BAD_TAGS, None, 1, "decoy-record/v01", "decoy-record-2"])
def test_a_record_with_a_missing_or_malformed_schema_is_refused(tag: object) -> None:
    with pytest.raises(RecordError) as caught:
        records.decode(_with_schema(tag))
    assert caught.value.code == "record_corrupt"


def test_only_a_valid_higher_major_needs_a_newer_reader() -> None:
    got = records.decode(_with_schema("decoy-record/v2"))
    assert isinstance(got, records.RecordNeedsNewer) and not got.verified


def _header_line(**over: Any) -> bytes:
    lines = list(records.export_lines([], producer=PRODUCER, exported_at=T0))
    header = json.loads(lines[0])
    for key, value in over.items():
        if value is _MISSING:
            header.pop(key)
        else:
            header[key] = value
    return json.dumps(header).encode() + b"\n"


_MISSING = object()


@pytest.mark.parametrize(
    "over",
    [
        {"export_schema": _MISSING},
        {"export_schema": "decoy-library-export/v0"},
        {"export_schema": "decoy-library-export/vbanana"},
        {"export_schema": "decoy-library-export/v2"},
        {"export_schema": "decoy-library-export/v1\n"},
        {"exported_at": "not-a-time"},
        {"record_count": -7},
        {"external_links": -2},
        {"refs": {"latest": "not-an-id"}},
        {"unstripped": [{"anything": True}]},
        {"withheld_records": ["not-an-id"]},
    ],
)
def test_an_export_header_that_is_not_exactly_v1_is_refused(over: dict[str, Any]) -> None:
    with pytest.raises(RecordError) as caught:
        list(records.read_export([_header_line(**over)]))
    assert caught.value.code == "record_invalid"


def test_a_good_header_still_reads() -> None:
    header = next(records.read_export([_header_line(refs={"latest": SHA_B})]))
    assert header.refs == {"latest": SHA_B}


def test_an_export_header_with_a_duplicate_key_or_nan_is_refused() -> None:
    good = _header_line().decode().strip()
    for text in (good[:-1] + ',"record_count":0}', good[:-1] + ',"x":NaN}'):
        with pytest.raises(RecordError):
            list(records.read_export([text]))


def _log(header: dict[str, Any], *events: str) -> bytes:
    head = records.header_line(records.JobLogHeader(**HEADER)).decode().strip()
    obj = json.loads(head)
    for key, value in header.items():
        if value is _MISSING:
            obj.pop(key)
        else:
            obj[key] = value
    return ("\n".join([json.dumps(obj), *events]) + "\n").encode()


@pytest.mark.parametrize(
    "over",
    [
        {"log_schema": _MISSING},
        {"log_schema": "decoy-job-log/v0"},
        {"log_schema": "decoy-job-log/v2"},
        {"log_schema": "decoy-job-log/vbanana"},
        {"event_schema": -1},
        {"event_schema": 999},
        {"event_schema": 0},
        {"event_schema": True},
        {"event_schema": _MISSING},
    ],
)
def test_a_job_log_header_that_is_not_exactly_v1_is_refused(over: dict[str, Any]) -> None:
    with pytest.raises(RecordError) as caught:
        records.read_job_log(_log(over))
    assert caught.value.code == "record_corrupt"


def test_a_job_log_with_a_duplicate_key_in_the_header_is_refused() -> None:
    head = records.header_line(records.JobLogHeader(**HEADER)).decode().strip()
    with pytest.raises(RecordError):
        records.read_job_log((head[:-1] + ',"command":"scan"}\n').encode())


def test_job_log_event_lines_use_the_strict_parse() -> None:
    log = records.read_job_log(
        _log({}, '{"kind":"a","kind":"b"}', '{"kind":"nan","v":NaN}', '{"kind":"ok"}')
    )
    assert [e["kind"] for e in log.events] == ["ok"] and log.bad_lines == 2


def test_the_header_unstripped_entry_is_a_closed_model() -> None:
    ref = records.UnstrippedRef(id=SHA_B, source_id=None)
    assert ref.id == SHA_B
    with pytest.raises(ValueError):
        records.UnstrippedRef(id=SHA_B, source_id=None, other=1)  # type: ignore[call-arg]
