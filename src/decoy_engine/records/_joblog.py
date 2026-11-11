"""The ``decoy-job-log/v1`` header and a reader for the file it opens.

Line 1 is the header; every other line is a value-free engine progress event the CLI wrote as it
arrived. The file is appended while the job runs and never renamed into place, so a crash leaves
a truncated last line, which the reader reports and skips.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from decoy_engine import reasons
from decoy_engine.reasons import EVENT_SCHEMA_VERSION
from decoy_engine.records._canonical import canonical_bytes
from decoy_engine.records._codec import json_object
from decoy_engine.records._envelope import Producer
from decoy_engine.records._errors import RecordError
from decoy_engine.records._read_model import read_model_of
from decoy_engine.records._registry import family_major
from decoy_engine.run_result import InvocationId, RelPath, UtcTimestamp

__all__ = ["JOB_LOG_SCHEMA", "JobLog", "JobLogHeader", "header_line", "read_job_log"]

JOB_LOG_SCHEMA = "decoy-job-log/v1"
# The progress-event schema this version reads: a header naming another one is refused.
EventSchema = Annotated[StrictInt, Field(ge=EVENT_SCHEMA_VERSION, le=EVENT_SCHEMA_VERSION)]


class JobLogHeader(BaseModel):
    """``command`` is a closed enum, never ``argv``: a command line can carry secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    log_schema: Literal["decoy-job-log/v1"] = "decoy-job-log/v1"
    event_schema: EventSchema
    invocation_id: InvocationId
    command: Literal["run", "scan"]
    pipeline: RelPath | None
    started_at: UtcTimestamp
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


def read_job_log(data: bytes | str) -> JobLog:
    """Read a log. A header that is missing or invalid raises ``RecordError`` (``record_corrupt``);
    a truncated last line is skipped and flagged, and any other bad line is skipped and counted."""
    text: str | None = None
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        text = None
    if text is None:
        raise RecordError(reasons.runtime.RECORD_CORRUPT)
    lines = text.split("\n")
    complete = text.endswith("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise RecordError(reasons.runtime.RECORD_CORRUPT)
    first = json_object(lines[0])
    header: Any = None
    try:
        header = JobLogHeaderRead.model_validate(first)
    except ValueError:  # includes pydantic's ValidationError
        header = None
    # The tag is read from the wire, not from the model: a read model keeps the writer's default
    # for a missing tag, which would let a header with no version pass as v1.
    if (
        header is None
        or first is None
        or family_major(first.get("log_schema"), "decoy-job-log") != 1
    ):
        raise RecordError(reasons.runtime.RECORD_CORRUPT)
    events: list[dict[str, Any]] = []
    bad = 0
    truncated = False
    for index, line in enumerate(lines[1:], 1):
        if not line.strip():
            continue
        event = json_object(line)
        if event is not None:
            events.append(event)
        elif index == len(lines) - 1 and not complete:
            truncated = True
        else:
            bad += 1
    return JobLog(header, tuple(events), truncated, bad)
