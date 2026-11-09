"""The ``decoy-library-export/v1`` codec: JSON Lines, one wrapper object per line.

The engine owns the codec so the platform importer upserts by id with the same code. It touches
no filesystem: callers hand it verified raw envelopes in link order and write the lines it yields.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from decoy_engine import reasons
from decoy_engine._value_free import Count, InvocationId, UtcTimestamp
from decoy_engine.records import _kinds  # noqa: F401  (registers the V1 kinds)
from decoy_engine.records._canonical import canonical_bytes, payload_digest, record_id, sha256_tag
from decoy_engine.records._codec import decode, json_object, project, utc_stamp, validated
from decoy_engine.records._envelope import (
    ENVELOPE_SCHEMA,
    DecodedRecord,
    OpaqueRecord,
    Producer,
    RecordNeedsNewer,
)
from decoy_engine.records._errors import RecordError
from decoy_engine.records._export_rules import file_name
from decoy_engine.records._grammar import RecordId, family_major, is_record_id
from decoy_engine.records._read_model import read_model_of
from decoy_engine.records._registry import KindSpec, kind_spec

__all__ = [
    "EXPORT_SCHEMA",
    "BadLine",
    "ExportHeader",
    "ExportedLog",
    "ExportedRecord",
    "UnstrippedRef",
    "export_lines",
    "read_export",
]

EXPORT_SCHEMA = "decoy-library-export/v1"


class UnstrippedRef(BaseModel):
    """A record that was exported with a remote locator removed that no path rule reduces."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: RecordId
    source_id: RecordId | None


class ExportHeader(BaseModel):
    """First line of an export.

    A default export (no ``keep_paths``) carries no raw path or locator. ``unstripped`` lists the
    records that had a remote locator dropped, which is a loss the reader should know about.
    ``withheld_records`` lists the source ids of records this version cannot strip (a newer or
    unknown kind) and so left out. ``withheld_logs`` lists the invocations whose job log was left
    out because its lines can name paths this version cannot strip.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    type: Literal["header"] = "header"
    export_schema: Literal["decoy-library-export/v1"] = "decoy-library-export/v1"
    exported_at: UtcTimestamp
    producer: Producer
    record_count: Count
    refs: dict[StrictStr, RecordId]
    paths_stripped: StrictBool
    unstripped: tuple[UnstrippedRef, ...]
    withheld_logs: tuple[InvocationId, ...] = ()
    withheld_records: tuple[RecordId, ...] = ()
    external_links: Count


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


def _strip(spec: KindSpec, payload: dict[str, Any]) -> tuple[dict[str, Any], bool, bool] | None:
    """The payload as ``spec``'s export rule leaves it, as ``(payload, changed, locator_removed)``;
    ``None`` when the payload does not fit the kind's model or the rule cannot prove it safe."""
    model = spec.write_model
    if model is None or spec.export_rule is None:
        return None
    try:
        projected = project(validated(payload, model, ("payload",)))
    except RecordError:
        return None
    result = spec.export_rule(projected)
    if result is None:
        return None
    out, removed = result
    return out, out != payload, removed


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
    ``source_id: null``. Without ``keep_paths`` a record carries no raw path or
    locator: path fields are reduced to file names, locator fields (a bucket or endpoint) are removed
    and the record is listed under ``unstripped``, and a record this version cannot read well
    enough to strip is left out and its id listed under ``withheld_records``. A link to a record outside the export keeps its id and is counted.

    Every envelope in ``records`` must be a v1 envelope: this function refuses a later major with
    ``record_corrupt`` because it cannot verify or rewrite one, so callers filter out
    ``RecordNeedsNewer`` results before passing ``raw`` values in. Logs (``(invocation_id, bytes)``)
    are written only with ``keep_paths``; otherwise their owners go in the header's
    ``withheld_logs``.
    """
    id_map: dict[str, str] = {}
    lines: list[dict[str, Any]] = []
    unstripped: list[dict[str, Any]] = []
    withheld_records: list[str] = []
    stripped_any = False
    external = 0
    for raw in records:
        if raw.get("record_schema") != ENVELOPE_SCHEMA or raw.get("id") != record_id(raw):
            raise RecordError(reasons.runtime.RECORD_CORRUPT)
        reduced: tuple[dict[str, Any], bool, bool] | None = None
        if not keep_paths:
            spec = kind_spec(raw["kind"])
            if spec is not None and raw["payload_schema"] == spec.payload_schema:
                reduced = _strip(spec, raw["payload"])
            if reduced is None:
                # This version cannot tell which fields are paths, so the record stays out.
                withheld_records.append(raw["id"])
                continue
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
        if reduced is not None and reduced[1]:
            rec["payload"] = reduced[0]
            rec["payload_digest"] = payload_digest(reduced[0])
            changed = stripped_any = True
        pipeline = rec["labels"].get("pipeline")
        if not keep_paths and isinstance(pipeline, str) and file_name(pipeline) != pipeline:
            rec["labels"]["pipeline"] = file_name(pipeline)
            changed = stripped_any = True
        if changed:
            rec["id"] = record_id(rec)
        if reduced is not None and reduced[2]:
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
            "withheld_records": tuple(withheld_records),
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
        wrapper = json_object(line)
        if not seen_header:
            header: Any = None
            try:
                if wrapper is not None and wrapper.get("type") == "header":
                    header = ExportHeaderRead.model_validate(wrapper)
            except ValueError:  # includes pydantic's ValidationError
                header = None
            # The tag is read from the wire: a read model keeps the writer's default for a missing
            # one, which would let a header with no version pass as v1.
            if (
                header is None
                or wrapper is None
                or family_major(wrapper.get("export_schema"), "decoy-library-export") != 1
            ):
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
        if not isinstance(record, dict) or not (source is None or is_record_id(source)):
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
