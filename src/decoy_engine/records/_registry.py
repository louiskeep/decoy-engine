"""The kind registry: one entry per record kind, the single source for everything per-kind.

The CLI's ``list --kind``, diff dispatch, export path stripping and the canary fixtures all read
a kind's spec from here instead of keeping their own table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

from decoy_engine import reasons
from decoy_engine.records._envelope import TAG_PATTERN
from decoy_engine.records._errors import RecordError

__all__ = ["KindSpec", "kind_spec", "major_of", "register_kind", "registered_kinds"]

_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}\Z")
_registry: dict[str, KindSpec] = {}


def major_of(payload_schema: str) -> tuple[str, int] | None:
    """The family and major of a payload tag (``sample-run/v1``, ``cli-local-1``), or ``None``."""
    match = TAG_PATTERN.match(payload_schema)
    return (match["family"], int(match["major"])) if match else None


@dataclass(frozen=True)
class KindSpec:
    """What this version knows about one kind.

    A reserved kind has no models: the name and its link types are stable now, and building a
    record of it is refused until its plan adds the model. ``path_fields`` are JSON-pointer
    patterns (``*`` matches every key or index) over the envelope, used by export.
    ``locator_fields`` use the same patterns for fields that locate a remote store but are not
    paths: export cannot reduce them, so a record that holds one is listed as unstripped.
    """

    kind: str
    payload_schema: str
    write_model: type[BaseModel] | None = None
    read_model: type[BaseModel] | None = None
    path_fields: tuple[str, ...] = ()
    locator_fields: tuple[str, ...] = ()
    diff_rule: str | None = None
    canary_fixture: str | None = None

    @property
    def reserved(self) -> bool:
        return self.write_model is None

    def __post_init__(self) -> None:
        if not _KIND.match(self.kind) or major_of(self.payload_schema) is None:
            raise ValueError("invalid kind spec")
        if (self.write_model is None) != (self.read_model is None):
            raise ValueError("a kind has both models or neither")
        if self.reserved and (
            self.path_fields or self.locator_fields or self.diff_rule or self.canary_fixture
        ):
            raise ValueError("a reserved kind declares no paths, diff rule or canary")
        if any(not p.startswith("/payload/") for p in (*self.path_fields, *self.locator_fields)):
            raise ValueError("path fields point into the payload")
        if not self.reserved and (self.diff_rule is None or self.canary_fixture is None):
            raise ValueError("a writable kind declares a diff rule and a canary fixture")


def register_kind(spec: KindSpec) -> KindSpec:
    """Add ``spec`` and return it. A kind is registered once."""
    if spec.kind in _registry:
        raise ValueError("kind already registered")
    _registry[spec.kind] = spec
    return spec


def registered_kinds() -> tuple[KindSpec, ...]:
    """Every registered kind, sorted by name."""
    return tuple(_registry[k] for k in sorted(_registry))


def kind_spec(kind: str) -> KindSpec | None:
    """The spec of ``kind``, or ``None`` when this version does not know the name."""
    return _registry.get(kind)


def require_writable(kind: str) -> KindSpec:
    """The spec to build a record of ``kind`` from. Unknown names are refused; ``build`` refuses a
    reserved kind where it needs the write model."""
    spec = _registry.get(kind)
    if spec is None:
        raise RecordError(reasons.runtime.RECORD_INVALID, (("kind",),))
    return spec
