"""`decoy-reasons/v1`: the identity form of a registry reason and its fail-closed resolver.

The phase envelopes (`decoy_engine.phase`) carry a reason as identity only, `(namespace,
code)`, and resolve it against the one F29 catalog at export; an identity the catalog does not
publish fails closed. The same shape protobuf/gRPC status details and RFC 9457 problem types
use for stable machine reasons with a fixed human summary: the summary is registry text, never
formatted from runtime values, so a reason can cross any output boundary safely.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from decoy_engine._error_base import DecoyError
from decoy_engine.reasons import _registry
from decoy_engine.reasons._types import ReasonSpec

REGISTRY_SCHEMA = "decoy-reasons/v1"


class UnknownReasonError(DecoyError, ValueError):
    """A reason identity (or registry version) this engine does not publish."""


@dataclass(frozen=True)
class ReasonRef:
    """The wire form of a reason: identity only, resolved against the registry."""

    namespace: str
    code: str


class _Catalog(Mapping[tuple[str, str], ReasonSpec]):
    """Read-only `(namespace, code) -> ReasonSpec` view over the live registry."""

    def __getitem__(self, key: tuple[str, str]) -> ReasonSpec:
        return _registry.get(*key)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(spec.identity for spec in _registry.specs())

    def __len__(self) -> int:
        return len(_registry.specs())


REASONS: Mapping[tuple[str, str], ReasonSpec] = _Catalog()


def get_reason(namespace: str, code: str) -> ReasonSpec:
    try:
        return _registry.get(namespace, code)
    except KeyError:
        raise UnknownReasonError(f"unknown reason {namespace}/{code}") from None


def require_known(ref: ReasonRef) -> ReasonSpec:
    return get_reason(ref.namespace, ref.code)
