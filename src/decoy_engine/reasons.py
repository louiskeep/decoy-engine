"""`decoy-reasons/v1`: the engine-owned registry of versioned phase reason codes.

One registry, phase as the namespace, so the CLI and platform consume engine
descriptors instead of keeping a second taxonomy. F29 extends this same artifact
with the remaining error families; entries are only ever added, never repurposed.

Pattern: closed, immutable catalog keyed by (namespace, code), the same shape
protobuf/gRPC status details and RFC 9457 problem types use for stable machine
reasons with a fixed human summary. The summary is fixed text, never formatted
from runtime values, so a reason can cross any output boundary safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from decoy_engine.errors import DecoyError

REGISTRY_SCHEMA = "decoy-reasons/v1"

Namespace = Literal["pre_scan", "transform", "post_scan", "route"]
Kind = Literal["failure", "finding", "warning", "decision", "control"]


class UnknownReasonError(DecoyError, ValueError):
    """A reason identity (or registry version) this engine does not publish."""


@dataclass(frozen=True)
class ReasonSpec:
    namespace: Namespace
    code: str
    summary: str
    kind: Kind

    @property
    def ref(self) -> ReasonRef:
        return ReasonRef(self.namespace, self.code)


@dataclass(frozen=True)
class ReasonRef:
    """The wire form of a reason: identity only, resolved against the registry."""

    namespace: str
    code: str


def _specs(*specs: ReasonSpec) -> MappingProxyType[tuple[str, str], ReasonSpec]:
    table = {(s.namespace, s.code): s for s in specs}
    if len(table) != len(specs):
        raise RuntimeError("duplicate reason identity in decoy-reasons/v1")
    return MappingProxyType(table)


REASONS = _specs(
    ReasonSpec(
        "route",
        "post_scan_integrity_requested",
        "Post-scan integrity needs the resident full-frame output.",
        "decision",
    ),
    ReasonSpec(
        "route",
        "post_scan_integrity",
        "Resident output required by the requested Post-scan integrity checks.",
        "decision",
    ),
    ReasonSpec(
        "post_scan",
        "check_error",
        "A Post-scan check raised before producing a result.",
        "failure",
    ),
    ReasonSpec(
        "post_scan",
        "scan_crashed",
        "A post-validation scan crashed before producing a result.",
        "failure",
    ),
    ReasonSpec(
        "post_scan",
        "pass_failed",
        "The Post-scan pass could not start.",
        "failure",
    ),
    ReasonSpec(
        "pre_scan",
        "profile_failed",
        "The Pre-scan profile did not complete.",
        "failure",
    ),
)


def get_reason(namespace: str, code: str) -> ReasonSpec:
    try:
        return REASONS[(namespace, code)]
    except KeyError:
        raise UnknownReasonError(f"unknown reason {namespace}/{code}") from None


def require_known(ref: ReasonRef) -> ReasonSpec:
    return get_reason(ref.namespace, ref.code)
