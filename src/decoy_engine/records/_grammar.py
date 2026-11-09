"""The wire grammars of the record package, each spelled once.

A record id, a kind name and a schema tag are read by the envelope model, the decoder's
newer-envelope check, the kind registry and the export reader. They all take their pattern, type
and parser from here, so changing a grammar is one edit.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import StrictStr, StringConstraints, TypeAdapter, ValidationError

__all__ = [
    "KIND_RE",
    "RECORD_ID_RE",
    "TAG_PATTERN",
    "Kind",
    "PayloadTag",
    "RecordId",
    "family_major",
    "is_kind",
    "is_record_id",
    "major_of",
]

_ID = r"sha256:[0-9a-f]{64}"
_KIND = r"[a-z][a-z0-9-]{0,63}"
# A payload tag is `family/vN` or the inherited `family-N` (`cli-local-1`); a major has no
# leading zero and at most four digits.
_TAG = r"[a-z][a-z0-9-]{0,63}(?:/v|-)[1-9][0-9]{0,3}"

RECORD_ID_RE = re.compile(_ID)
KIND_RE = re.compile(_KIND)
TAG_PATTERN = re.compile(r"(?P<family>[a-z][a-z0-9-]*?)(?:/v|-)(?P<major>[1-9][0-9]{0,3})")

RecordId = Annotated[StrictStr, StringConstraints(pattern=f"^{_ID}$")]
Kind = Annotated[StrictStr, StringConstraints(pattern=f"^{_KIND}$")]
PayloadTag = Annotated[StrictStr, StringConstraints(pattern=f"^{_TAG}$")]
_RECORD_ID: TypeAdapter[str] = TypeAdapter(RecordId)


def is_record_id(value: object) -> bool:
    """Whether ``value`` is a record id: a strict string, lower-case hex, no trailing newline."""
    try:
        _RECORD_ID.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def is_kind(value: object) -> bool:
    return isinstance(value, str) and KIND_RE.fullmatch(value) is not None


def major_of(payload_schema: object) -> tuple[str, int] | None:
    """The family and major of a payload tag (``sample-run/v1``, ``cli-local-1``), or ``None``."""
    match = TAG_PATTERN.fullmatch(payload_schema) if isinstance(payload_schema, str) else None
    return (match["family"], int(match["major"])) if match else None


def family_major(tag: object, family: str) -> int | None:
    """The major of a ``family/vN`` schema tag, or ``None`` for anything else.

    The one reader of a wire schema tag (envelope, export header, job-log header), built on
    ``major_of`` so a missing, malformed, zero or non-numeric version never reads as a version.
    """
    if not isinstance(tag, str) or not tag.startswith(f"{family}/v"):
        return None
    parsed = major_of(tag)
    return parsed[1] if parsed is not None and parsed[0] == family else None
