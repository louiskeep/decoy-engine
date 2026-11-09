"""``build``, ``encode`` and ``decode``: the only way a record is made or read.

Writers validate with the kind's strict write model. Readers verify the id by the one id rule,
check the envelope and payload majors, and validate with the tolerant read model; the verified
envelope is returned untouched next to that view so a copy never drops a field.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from decoy_engine import reasons
from decoy_engine.records import _kinds  # noqa: F401  (registers the V1 kinds)
from decoy_engine.records._canonical import canonical_bytes, payload_digest, record_id
from decoy_engine.records._envelope import (
    ENVELOPE_SCHEMA,
    DecodedRecord,
    Labels,
    Link,
    OpaqueRecord,
    Producer,
    RecordEnvelope,
    RecordEnvelopeRead,
    RecordNeedsNewer,
)
from decoy_engine.records._errors import RecordError
from decoy_engine.records._registry import (
    KindSpec,
    family_major,
    kind_spec,
    major_of,
    require_writable,
)

__all__ = ["build", "decode", "encode"]

_PLACEHOLDER_ID = "sha256:" + "0" * 64
_ENVELOPE_FAMILY = "decoy-record"
_M = TypeVar("_M", bound=BaseModel)
_ID = re.compile(r"sha256:[0-9a-f]{64}")
_KIND = re.compile(r"[a-z][a-z0-9-]{0,63}")


def _locations(
    exc: ValidationError, prefix: tuple[object, ...] = ()
) -> tuple[tuple[object, ...], ...]:
    # Locations only: the validator's message and input can hold the offending value.
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return tuple((*prefix, *e["loc"]) for e in errors)


def validated(value: Any, model: type[_M], prefix: tuple[object, ...] = ()) -> _M:
    """``value`` as ``model``, or a value-free ``RecordError`` naming only the failing locations.

    The one place the record code validates a model from a caller's data: the validator's error
    holds the offending input, so it is reduced to locations and never chained.
    """
    failed: tuple[tuple[object, ...], ...] | None = None
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except ValidationError as exc:
        failed = _locations(exc, prefix)
    # Raised after the except block closes: inside it the validator error would stay reachable as
    # __context__, and its input holds the caller's value.
    raise RecordError(reasons.runtime.RECORD_INVALID, failed or ())


def _dump(value: Any, model: type[BaseModel], field: str) -> dict[str, Any]:
    """The stored form of an envelope part: validated by its model, so the id covers what is kept.

    A caller's mapping can hold a value the model drops on write (an unset label); the id is
    computed over this result, never over the mapping the caller passed.
    """
    parsed = validated(value, model, (field,))
    return parsed.model_dump(mode="json", exclude_none=model is Labels)


def _payload_body(spec: KindSpec, payload: Any) -> dict[str, Any]:
    model = spec.write_model
    if model is None:
        raise RecordError(reasons.runtime.RECORD_KIND_RESERVED)
    parsed = validated(payload, model, ("payload",))
    to_payload = getattr(parsed, "to_payload", None)
    if to_payload is not None:
        return dict(to_payload())
    # Fields the producer left unset stay absent, so the stored payload is the one it handed over.
    return parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)


def utc_stamp(value: datetime, field: str) -> str:
    """``value`` as UTC text; a naive time is refused, since reading it as host time would make
    the stored bytes depend on the machine."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise RecordError(reasons.runtime.RECORD_INVALID, ((field,),))
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _digest(body: dict[str, Any]) -> str:
    bad = False
    try:
        digest = payload_digest(body)
    except ValueError:
        bad = True
    if bad:
        raise RecordError(reasons.runtime.RECORD_INVALID, (("payload",),))
    return digest


def build(
    kind: str,
    payload: Any,
    *,
    producer: Producer | Mapping[str, Any],
    invocation_id: str | UUID,
    created_at: datetime,
    labels: Labels | Mapping[str, Any] | None = None,
    links: Iterable[Link | Mapping[str, Any]] = (),
) -> RecordEnvelope:
    """A frozen, self-identifying record of ``kind``.

    ``payload`` is validated with the kind's write model, which refuses unknown fields. The
    result carries ``payload_digest`` and ``id``. Raises ``RecordError``: ``record_invalid`` for
    a payload or envelope field that fails (the error names the field, never its value) and
    ``record_kind_reserved`` for a kind with no model yet.
    """
    spec = require_writable(kind)
    body = _payload_body(spec, payload)
    parts: dict[str, Any] = {
        "record_schema": ENVELOPE_SCHEMA,
        "kind": spec.kind,
        "payload_schema": spec.payload_schema,
        "created_at": utc_stamp(created_at, "created_at"),
        "invocation_id": str(invocation_id),
        "producer": _dump(producer, Producer, "producer"),
        "labels": _dump(labels if labels is not None else {}, Labels, "labels"),
        "links": [_dump(link, Link, "links") for link in links],
        "payload_digest": _digest(body),
        "payload": body,
    }
    validated({**parts, "id": _PLACEHOLDER_ID}, RecordEnvelope)
    return validated({**parts, "id": record_id(parts)}, RecordEnvelope)


def encode(envelope: RecordEnvelope | Mapping[str, Any]) -> bytes:
    """The canonical bytes of a built envelope or of a verified ``DecodedRecord.raw``."""
    data = envelope.to_dict() if isinstance(envelope, RecordEnvelope) else dict(envelope)
    try:
        return canonical_bytes(data)
    except ValueError:
        pass
    raise RecordError(reasons.runtime.RECORD_INVALID)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _refuse_constant(_: str) -> Any:
    raise ValueError("non-finite number")


def json_object(data: bytes | str) -> dict[str, Any] | None:
    """The one strict JSON-object parse for records, export lines and job logs.

    Refuses duplicate keys, ``NaN``/``Infinity`` and anything that is not an object, returning
    ``None`` so no decode error (it keeps the input text in its ``doc``) can leave.
    """
    raw: Any = None
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        raw = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raw = None
    return raw if isinstance(raw, dict) else None


def _parse(data: bytes | str) -> dict[str, Any]:
    raw = json_object(data)
    if raw is None:
        raise RecordError(reasons.runtime.RECORD_CORRUPT)
    return raw


def _str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def decode(data: bytes | str) -> DecodedRecord | OpaqueRecord | RecordNeedsNewer:
    """Parse, verify and classify one stored record.

    The id is recomputed by the one id rule (parse, drop ``id``, canonicalize, hash), so a CRLF
    checkout keeps its id. Returns a ``DecodedRecord`` for a registered kind, an ``OpaqueRecord``
    for a kind this version has no model for, and ``RecordNeedsNewer`` for a later envelope or
    payload major. Raises ``RecordError`` with ``record_corrupt`` when the bytes are not a record
    or the id does not match, and ``record_invalid`` when a verified payload fails its read model.
    """
    raw = _parse(data)
    major = family_major(raw.get("record_schema"), _ENVELOPE_FAMILY)
    if major is None:
        raise RecordError(reasons.runtime.RECORD_CORRUPT)
    if major > 1:
        # A later envelope major cannot be verified by this version's rule. Its id and kind
        # are still held to their formats, and the result is marked unverified.
        if not (_ID.fullmatch(_str(raw, "id")) and _KIND.fullmatch(_str(raw, "kind"))):
            raise RecordError(reasons.runtime.RECORD_CORRUPT)
        return RecordNeedsNewer(
            raw["kind"], raw["id"], _str(raw, "payload_schema"), "envelope", raw, verified=False
        )
    envelope: Any = None
    try:
        if raw.get("id") == record_id(raw):
            envelope = RecordEnvelopeRead.model_validate(raw)
    except ValueError:  # includes pydantic's ValidationError
        envelope = None
    if envelope is None or payload_digest(raw["payload"]) != raw["payload_digest"]:
        raise RecordError(reasons.runtime.RECORD_CORRUPT)
    spec = kind_spec(raw["kind"])
    if spec is None or spec.reserved:
        return OpaqueRecord(
            kind=raw["kind"],
            id=raw["id"],
            created_at=raw["created_at"],
            invocation_id=raw["invocation_id"],
            labels=raw["labels"],
            links=tuple(raw["links"]),
            payload=raw["payload"],
            raw=raw,
        )
    return _typed(spec, raw, envelope)


def _typed(spec: KindSpec, raw: dict[str, Any], envelope: Any) -> DecodedRecord | RecordNeedsNewer:
    wanted = major_of(spec.payload_schema)
    found = major_of(raw["payload_schema"])
    model = spec.read_model
    if wanted is None or model is None:
        raise RecordError(reasons.runtime.RECORD_KIND_RESERVED)
    if found is None or found[0] != wanted[0] or found[1] < wanted[1]:
        raise RecordError(reasons.runtime.RECORD_CORRUPT)
    if found[1] > wanted[1]:
        return RecordNeedsNewer(spec.kind, raw["id"], raw["payload_schema"], "payload", raw)
    view = validated(raw["payload"], model, ("payload",))
    return DecodedRecord(raw=raw, envelope=envelope, view=view)
