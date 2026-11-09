"""The ``decoy-record/v1`` envelope and the three things a decode can return.

The envelope models validate; the id and the digests are computed over plain dicts in
``_canonical``, so a field this version does not know still counts toward the id of the record
that carries it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, StringConstraints

from decoy_engine._value_free import (
    InvocationId,
    RelPath,
    SafeName,
    SafeRef,
    UtcTimestamp,
    Version,
)
from decoy_engine.records._read_model import read_model_of

__all__ = [
    "ENVELOPE_SCHEMA",
    "DecodedRecord",
    "Labels",
    "Link",
    "OpaqueRecord",
    "Producer",
    "RecordEnvelope",
    "RecordEnvelopeRead",
    "RecordNeedsNewer",
]

ENVELOPE_SCHEMA = "decoy-record/v1"

RecordId = Annotated[StrictStr, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
# A payload tag is `family/vN` or the inherited `family-N` (`cli-local-1`).
PayloadTag = Annotated[
    StrictStr, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}(?:/v|-)[1-9][0-9]{0,3}$")
]
TAG_PATTERN = re.compile(r"^(?P<family>[a-z][a-z0-9-]*?)(?:/v|-)(?P<major>[1-9][0-9]{0,3})\Z")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Producer(_Closed):
    name: SafeRef
    version: Version
    engine_version: Version


class Labels(_Closed):
    """The indexed fields: a closed set, strict types, so no free text and no float gets in."""

    pipeline: RelPath | None = None
    tables: tuple[SafeName, ...] | None = None
    status: SafeRef | None = None
    route: SafeRef | None = None
    exit_code: StrictInt | None = None


class Link(_Closed):
    rel: Literal[
        "baseline",
        "scan",
        "evidence",
        "result",
        "sample",
        "publication",
        "checkpoint",
        "supersedes",
    ]
    id: RecordId


class RecordEnvelope(_Closed):
    record_schema: Literal["decoy-record/v1"] = "decoy-record/v1"
    kind: Annotated[StrictStr, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    payload_schema: PayloadTag
    id: RecordId
    created_at: UtcTimestamp
    invocation_id: InvocationId
    producer: Producer
    labels: Labels
    links: tuple[Link, ...]
    payload_digest: RecordId
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """The envelope as the plain dict that is stored: unset labels are left out."""
        out = self.model_dump(mode="json")
        out["labels"] = self.labels.model_dump(mode="json", exclude_none=True)
        return out


RecordEnvelopeRead: Any = read_model_of(RecordEnvelope)


@dataclass(frozen=True)
class DecodedRecord:
    """A verified record of a kind this version reads.

    ``raw`` is the parsed envelope exactly as stored, every field known or not; anything that
    writes a record out again (export, a copy) uses it. ``view`` is the payload validated with
    the kind's read model, for this version's own commands.
    """

    raw: dict[str, Any]
    envelope: Any
    view: BaseModel

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def kind(self) -> str:
        return str(self.raw["kind"])


@dataclass(frozen=True)
class OpaqueRecord:
    """A verified record of a kind this version has no payload model for.

    It is indexed and listed; a default export withholds it (this version cannot strip its paths) and
    ``keep_paths`` copies it byte for byte. Its links still protect their targets.
    """

    kind: str
    id: str
    created_at: str
    invocation_id: str
    labels: dict[str, Any]
    links: tuple[dict[str, Any], ...]
    payload: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class RecordNeedsNewer:
    """A record written under a major this version does not read.

    ``scope`` says which major: the envelope's or the payload's. It is a result, not an error,
    so a listing keeps going.
    """

    kind: str
    id: str
    payload_schema: str
    scope: Literal["envelope", "payload"]
    raw: dict[str, Any] = field(repr=False)
    # A later envelope major cannot be checked by this version's id rule, so its id is only a
    # claim. Nothing may upsert, overwrite or delete by an unverified id.
    verified: bool = True
