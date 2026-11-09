"""The ``decoy-library-export/v1`` codec: JSON Lines, one wrapper object per line.

The engine owns the codec so the platform importer upserts by id with the same code. It touches
no filesystem: callers hand it verified raw envelopes in link order and write the lines it yields.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from decoy_engine import reasons
from decoy_engine._value_free import InvocationId
from decoy_engine.records import _kinds  # noqa: F401  (registers the V1 kinds)
from decoy_engine.records._canonical import canonical_bytes, payload_digest, record_id, sha256_tag
from decoy_engine.records._codec import decode, utc_stamp, validated
from decoy_engine.records._envelope import (
    ENVELOPE_SCHEMA,
    DecodedRecord,
    OpaqueRecord,
    Producer,
    RecordNeedsNewer,
)
from decoy_engine.records._errors import RecordError
from decoy_engine.records._read_model import read_model_of
from decoy_engine.records._registry import KindSpec, kind_spec

__all__ = [
    "EXPORT_SCHEMA",
    "BadLine",
    "ExportHeader",
    "ExportedLog",
    "ExportedRecord",
    "export_lines",
    "read_export",
]

EXPORT_SCHEMA = "decoy-library-export/v1"
_SEPARATORS = re.compile(r"[\\/]+")


class ExportHeader(BaseModel):
    """First line of an export.

    ``unstripped`` lists records whose paths could not be reduced. ``withheld_logs`` lists the
    invocations whose job log was left out because its lines can name paths this version cannot
    strip.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    type: Literal["header"] = "header"
    export_schema: Literal["decoy-library-export/v1"] = "decoy-library-export/v1"
    exported_at: StrictStr
    producer: Producer
    record_count: StrictInt
    refs: dict[StrictStr, StrictStr]
    paths_stripped: StrictBool
    unstripped: tuple[dict[str, Any], ...]
    withheld_logs: tuple[InvocationId, ...] = ()
    external_links: StrictInt


ExportHeaderRead: Any = read_model_of(ExportHeader)
_OWNER: TypeAdapter[str] = TypeAdapter(InvocationId)


def _is_invocation_id(value: object) -> bool:
    try:
        _OWNER.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


@dataclass(frozen=True)
class ExportedRecord:
    source_id: str | None
    record: DecodedRecord | OpaqueRecord | RecordNeedsNewer
    lineno: int


@dataclass(frozen=True)
class ExportedLog:
    invocation_id: str
    sha256: str
    content: str
    verified: bool
    lineno: int


@dataclass(frozen=True)
class BadLine:
    """A line that failed on its own; the rest of the file is still read."""

    lineno: int
    code: str


def _file_name(value: str) -> str:
    parts = [p for p in _SEPARATORS.split(value) if p]
    return parts[-1] if parts else value


def _reduce(node: Any, segments: list[str]) -> bool:
    """Reduce the strings the pointer ``segments`` reach to file names. True when one changed."""
    head, rest = segments[0], segments[1:]
    if isinstance(node, list) and head == "*":
        children: list[Any] = list(node)
    elif isinstance(node, dict) and head == "*":
        children = list(node.values())
    elif isinstance(node, dict) and head in node:
        if not rest:
            value = node[head]
            if isinstance(value, str) and _file_name(value) != value:
                node[head] = _file_name(value)
                return True
            return False
        children = [node[head]]
    else:
        return False
    if not rest:
        return False
    return any([_reduce(child, rest) for child in children])


def _strip(spec: KindSpec, payload: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
    """The payload with its declared path fields reduced, or ``None`` when this version cannot
    tell which fields are paths (a payload with fields it does not know, or another shape)."""
    model = spec.write_model
    if model is None:
        return None
    try:
        model.model_validate(payload)
    except ValidationError:
        return None
    out = copy.deepcopy(payload)
    changed = any([_reduce(out, p.split("/")[2:]) for p in spec.path_fields])
    return out, changed


def export_lines(
    records: Iterable[Mapping[str, Any]],
    *,
    producer: Producer | Mapping[str, Any],
    exported_at: datetime,
    refs: Mapping[str, str] | None = None,
    keep_paths: bool = False,
    logs: Iterable[tuple[str, bytes]] = (),
) -> Iterator[bytes]:
    """The lines of an export, header first, each ending in a newline.

    ``records`` are verified raw envelopes (``DecodedRecord.raw``), oldest link target first.
    Every envelope's links are rewritten through the old-to-new id map built so far and its id is
    recomputed; a record whose links and payload are both unchanged keeps its id and gets
    ``source_id: null``. A payload this version cannot fully read is copied byte for byte and
    listed under ``unstripped``. A link to a record outside the export keeps its id and is counted.

    Every envelope in ``records`` must be a v1 envelope: this function refuses a later major with
    ``record_corrupt`` because it cannot verify or rewrite one, so callers filter out
    ``RecordNeedsNewer`` results before passing ``raw`` values in. Logs (``(invocation_id, bytes)``)
    are written only with ``keep_paths``; otherwise their owners go in the header's
    ``withheld_logs``.
    """
    id_map: dict[str, str] = {}
    lines: list[dict[str, Any]] = []
    unstripped: list[dict[str, Any]] = []
    stripped_any = False
    external = 0
    for raw in records:
        if raw.get("record_schema") != ENVELOPE_SCHEMA or raw.get("id") != record_id(raw):
            raise RecordError(reasons.runtime.RECORD_CORRUPT)
        rec = copy.deepcopy(dict(raw))
        links = []
        for link in rec["links"]:
            if link["id"] in id_map:
                links.append({**link, "id": id_map[link["id"]]})
            else:
                external += 1
                links.append(link)
        changed = links != rec["links"]
        rec["links"] = links
        reduced: tuple[dict[str, Any], bool] | None = None
        if not keep_paths:
            spec = kind_spec(rec["kind"])
            if spec is not None and rec["payload_schema"] == spec.payload_schema:
                reduced = _strip(spec, rec["payload"])
        if reduced is not None and reduced[1]:
            rec["payload"] = reduced[0]
            rec["payload_digest"] = payload_digest(reduced[0])
            changed = stripped_any = True
        pipeline = rec["labels"].get("pipeline")
        if not keep_paths and isinstance(pipeline, str) and _file_name(pipeline) != pipeline:
            rec["labels"]["pipeline"] = _file_name(pipeline)
            changed = stripped_any = True
        if changed:
            rec["id"] = record_id(rec)
        if not keep_paths and reduced is None:
            unstripped.append({"id": rec["id"], "source_id": raw["id"] if changed else None})
        id_map[raw["id"]] = rec["id"]
        lines.append({"type": "record", "source_id": raw["id"] if changed else None, "record": rec})
    log_list = list(logs)
    if not all(_is_invocation_id(owner) for owner, _ in log_list):
        raise RecordError(reasons.runtime.RECORD_INVALID, (("logs",),))
    withheld = () if keep_paths else tuple(owner for owner, _ in log_list)
    header = validated(
        {
            "exported_at": utc_stamp(exported_at, "exported_at"),
            "producer": validated(producer, Producer, ("producer",)),
            "record_count": len(lines),
            "refs": {name: id_map.get(target, target) for name, target in (refs or {}).items()},
            "paths_stripped": stripped_any,
            "unstripped": tuple(unstripped),
            "withheld_logs": withheld,
            "external_links": external,
        },
        ExportHeader,
    )
    for entry in [header.model_dump(mode="json"), *lines]:
        yield canonical_bytes(entry) + b"\n"
    for invocation_id, data in log_list:
        if not keep_paths:
            # Log lines can name paths and this version cannot strip them, so they stay out.
            continue
        content: str | None = None
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            content = None
        if content is None:
            raise RecordError(reasons.runtime.RECORD_INVALID)
        entry = {
            "type": "log",
            "invocation_id": invocation_id,
            "sha256": sha256_tag(data),
            "content": content,
        }
        yield canonical_bytes(entry) + b"\n"


def _wrapper(line: bytes | str) -> dict[str, Any] | None:
    try:
        text = line.decode("utf-8") if isinstance(line, bytes) else line
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def read_export(
    lines: Iterable[bytes | str],
) -> Iterator[Any]:
    """Read an export line by line.

    Yields the ``ExportHeader`` first, then an ``ExportedRecord`` or ``ExportedLog`` per good
    line and a ``BadLine`` per line that fails by itself (bad JSON, an id that does not match,
    an unknown line type). A file whose first line is not a current header raises
    ``RecordError`` (``record_invalid``), because nothing after it can be trusted; so does a
    file with no header at all.
    """
    seen_header = False
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        wrapper = _wrapper(line)
        if not seen_header:
            header: Any = None
            try:
                if wrapper is not None and wrapper.get("type") == "header":
                    header = ExportHeaderRead.model_validate(wrapper)
            except ValueError:  # includes pydantic's ValidationError
                header = None
            if header is None or header.export_schema != EXPORT_SCHEMA:
                raise RecordError(reasons.runtime.RECORD_INVALID)
            seen_header = True
            yield header
            continue
        yield _read_line(number, wrapper)
    if not seen_header:
        # An empty or blank file is a truncated export, not an empty library.
        raise RecordError(reasons.runtime.RECORD_INVALID)


def _read_line(number: int, wrapper: dict[str, Any] | None) -> Any:
    if wrapper is None:
        return BadLine(number, reasons.runtime.RECORD_CORRUPT)
    kind = wrapper.get("type")
    if kind == "record":
        source, record = wrapper.get("source_id"), wrapper.get("record")
        if not isinstance(record, dict) or not (source is None or isinstance(source, str)):
            return BadLine(number, reasons.runtime.RECORD_INVALID)
        try:
            decoded = decode(canonical_bytes(record))
            if isinstance(decoded, RecordNeedsNewer) and not decoded.verified:
                return BadLine(number, reasons.runtime.RECORD_INVALID)
            return ExportedRecord(source, decoded, number)
        except (RecordError, ValueError) as exc:
            return BadLine(number, getattr(exc, "code", reasons.runtime.RECORD_CORRUPT))
    if kind == "log":
        content, digest, owner = (wrapper.get(k) for k in ("content", "sha256", "invocation_id"))
        if isinstance(content, str) and isinstance(digest, str) and isinstance(owner, str):
            if not _is_invocation_id(owner):
                return BadLine(number, reasons.runtime.RECORD_INVALID)
            verified = sha256_tag(content.encode("utf-8")) == digest
            return ExportedLog(owner, digest, content, verified, number)
        return BadLine(number, reasons.runtime.RECORD_INVALID)
    return BadLine(number, reasons.runtime.RECORD_INVALID)
