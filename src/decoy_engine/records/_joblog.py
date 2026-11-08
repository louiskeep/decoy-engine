"""The ``decoy-job-log/v1`` header and a reader for the file it opens.

Line 1 is the header; every other line is a value-free engine progress event the CLI wrote as it
arrived. The file is appended while the job runs and never renamed into place, so a crash leaves
a truncated last line, which the reader reports and skips.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from decoy_engine.records._canonical import canonical_bytes
from decoy_engine.records._envelope import InvocationId, Producer, RelPath, Timestamp
from decoy_engine.records._errors import RecordError
from decoy_engine.records._read_model import read_model_of

__all__ = ["JOB_LOG_SCHEMA", "JobLog", "JobLogHeader", "header_line", "read_job_log"]

JOB_LOG_SCHEMA = "decoy-job-log/v1"


class JobLogHeader(BaseModel):
    """``command`` is a closed enum, never ``argv``: a command line can carry secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    log_schema: Literal["decoy-job-log/v1"] = "decoy-job-log/v1"
    event_schema: StrictInt
    invocation_id: InvocationId
    command: Literal["run", "scan"]
    pipeline: RelPath | None
    started_at: Timestamp
    producer: Producer


JobLogHeaderRead: Any = read_model_of(JobLogHeader)


@dataclass(frozen=True)
class JobLog:
    """A log as read: the header, the events, and what was skipped."""

    header: Any
    events: tuple[dict[str, Any], ...]
    truncated_tail: bool
    bad_lines: int


def header_line(header: JobLogHeader) -> bytes:
    """The header as the first line of a log file."""
    return canonical_bytes(header.model_dump(mode="json")) + b"\n"


def _object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def read_job_log(data: bytes | str) -> JobLog:
    """Read a log. A header that is missing or invalid raises ``RecordError`` (``record_corrupt``);
    a truncated last line is skipped and flagged, and any other bad line is skipped and counted."""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        raise RecordError("record_corrupt") from None
    lines = text.split("\n")
    complete = text.endswith("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise RecordError("record_corrupt")
    first = _object(lines[0])
    try:
        header = JobLogHeaderRead.model_validate(first)
        if header.log_schema != JOB_LOG_SCHEMA:
            raise ValueError
    except (ValidationError, ValueError):
        raise RecordError("record_corrupt") from None
    events: list[dict[str, Any]] = []
    bad = 0
    truncated = False
    for index, line in enumerate(lines[1:], 1):
        if not line.strip():
            continue
        event = _object(line)
        if event is not None:
            events.append(event)
        elif index == len(lines) - 1 and not complete:
            truncated = True
        else:
            bad += 1
    return JobLog(header, tuple(events), truncated, bad)
