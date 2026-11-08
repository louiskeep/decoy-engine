"""A1: the job-log header and reader."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from decoy_engine import records
from decoy_engine.records import RecordError
from tests.unit.records._helpers import INVOCATION, PRODUCER

HEADER = {
    "event_schema": 1,
    "invocation_id": INVOCATION,
    "command": "run",
    "pipeline": "pipelines/claims.yaml",
    "started_at": "2026-11-08T12:00:00.123456Z",
    "producer": PRODUCER,
}


def _log(*events: str, header: dict | None = None, tail: str = "\n") -> bytes:
    head = records.header_line(records.JobLogHeader(**(header or HEADER))).decode()
    return (head + "\n".join(events) + (tail if events else "")).encode()


def test_command_is_a_closed_enum() -> None:
    for command in ("argv", "run --key hunter2", ""):
        with pytest.raises(ValidationError):
            records.JobLogHeader(**{**HEADER, "command": command})
    with pytest.raises(ValidationError):
        records.JobLogHeader(**{**HEADER, "argv": ["decoy"]})
    assert records.JobLogHeader(**{**HEADER, "command": "scan"}).command == "scan"


def test_reader_returns_header_and_events() -> None:
    log = records.read_job_log(_log('{"kind":"phase","phase":"mask"}', '{"kind":"terminal"}'))
    assert log.header.command == "run" and log.header.log_schema == "decoy-job-log/v1"
    assert [e["kind"] for e in log.events] == ["phase", "terminal"]
    assert (log.truncated_tail, log.bad_lines) == (False, 0)


def test_truncated_last_line_is_skipped_and_flagged() -> None:
    log = records.read_job_log(_log('{"kind":"phase"}', '{"kind":"term', tail=""))
    assert [e["kind"] for e in log.events] == ["phase"] and log.truncated_tail is True


def test_a_bad_middle_line_is_counted_not_fatal() -> None:
    log = records.read_job_log(_log('{"kind":"a"}', "oops", '{"kind":"b"}'))
    assert [e["kind"] for e in log.events] == ["a", "b"] and log.bad_lines == 1


def test_header_only_log_is_valid() -> None:
    assert records.read_job_log(_log()).events == ()


@pytest.mark.parametrize("data", [b"", b"\n", b"{}\n", b"garbage\n", b"\xff\xfe\n"])
def test_missing_or_invalid_header_is_corrupt(data: bytes) -> None:
    with pytest.raises(RecordError) as caught:
        records.read_job_log(data)
    assert caught.value.code == "record_corrupt"


def test_reader_ignores_header_fields_a_later_version_added() -> None:
    head = json.dumps({**HEADER, "log_schema": "decoy-job-log/v1", "later": 1})
    assert records.read_job_log(head + "\n").header.invocation_id == INVOCATION
