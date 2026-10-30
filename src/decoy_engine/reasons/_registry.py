"""The reason registry: declaration, lookup, manifest and the safe fallback.

Every ``(namespace, code)`` is declared once, at import time, by a catalog module. Declaration
rejects a duplicate identity, so two producers cannot silently share or re-define a code.
Entries are never removed: a retired code is kept as a tombstone with its replacement link.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from decoy_engine.reasons._types import (
    Kind,
    Outcome,
    Phase,
    PhaseRule,
    ReasonCode,
    ReasonSpec,
    RetryClass,
    Severity,
)

REGISTRY_VERSION = "1.0.0"
REASON_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1

_SPECS: dict[tuple[str, str], ReasonSpec] = {}
_BY_CODE: dict[str, list[ReasonSpec]] = {}


def symbol_for(code: str) -> str:
    """The default module attribute for a wire string: upper case, non-identifier characters to ``_``."""
    sym = re.sub(r"[^0-9A-Za-z]", "_", code).upper()
    return f"_{sym}" if sym[:1].isdigit() else sym


def declare(spec: ReasonSpec) -> ReasonCode:
    """Register ``spec`` and return its wire string. A repeated identity is a programming error."""
    if spec.identity in _SPECS:
        raise ValueError(f"reason {spec.namespace}.{spec.code} is declared twice")
    _SPECS[spec.identity] = spec
    _BY_CODE.setdefault(spec.code, []).append(spec)
    return ReasonCode(spec.code, spec.namespace)


class Declarer:
    """Builds the specs of one catalog section that shares a namespace, owner and profile."""

    def __init__(
        self,
        namespace: str,
        owner: str,
        *,
        kind: Kind,
        phases: tuple[Phase, ...],
        severity: Severity,
        outcome: Outcome,
        retry: RetryClass,
    ) -> None:
        self._namespace = namespace
        self._owner = owner
        self._kind = kind
        self._phases = phases
        self._severity = severity
        self._outcome = outcome
        self._retry = retry

    def __call__(
        self,
        code: str,
        summary: str,
        *,
        symbol: str | None = None,
        kind: Kind | None = None,
        phases: tuple[Phase, ...] | None = None,
        severity: Severity | None = None,
        outcome: Outcome | None = None,
        retry: RetryClass | None = None,
        context: tuple[str, ...] = (),
        since: str = "1.0.0",
    ) -> ReasonCode:
        rules = tuple(
            PhaseRule(
                phase,
                severity or self._severity,
                outcome or self._outcome,
                retry or self._retry,
            )
            for phase in (phases or self._phases)
        )
        return declare(
            ReasonSpec(
                namespace=self._namespace,
                code=code,
                symbol=symbol or symbol_for(code),
                summary=summary,
                owner=self._owner,
                kind=kind or self._kind,
                rules=rules,
                context=context,
                since=since,
            )
        )


def get(namespace: str, code: str) -> ReasonSpec:
    """The entry for ``(namespace, code)``; ``KeyError`` when it was never declared."""
    return _SPECS[(namespace, code)]


def find(namespace: str, code: object) -> ReasonSpec | None:
    """Like :func:`get` but total: ``None`` for an unregistered or non-string code."""
    if not isinstance(code, str):
        return None
    return _SPECS.get((namespace, code))


def lookup(code: object) -> tuple[ReasonSpec, ...]:
    """Every entry whose wire string is ``code``, across namespaces."""
    if not isinstance(code, str):
        return ()
    return tuple(_BY_CODE.get(code, ()))


def is_registered(code: object, namespaces: Iterable[str] | None = None) -> bool:
    """Whether ``code`` is an engine-registered wire string (optionally within ``namespaces``).

    Character shape is not trust: an exception from a parser or a user callback can carry a
    data-derived ``code`` that is still lowercase and underscores. Only registry membership is.
    """
    specs = lookup(code)
    if namespaces is None:
        return bool(specs)
    allowed = set(namespaces)
    return any(s.namespace in allowed for s in specs)


def specs() -> tuple[ReasonSpec, ...]:
    """Every entry, tombstones included, ordered by identity."""
    return tuple(_SPECS[k] for k in sorted(_SPECS))


def namespaces() -> tuple[str, ...]:
    return tuple(sorted({ns for ns, _ in _SPECS}))


def manifest() -> dict[str, object]:
    """The deterministic, JSON-safe catalog export that the compatibility fixture pins."""
    entries = []
    for spec in specs():
        entries.append(
            {
                "namespace": spec.namespace,
                "code": spec.code,
                "symbol": spec.symbol,
                "summary": spec.summary,
                "owner": spec.owner,
                "kind": spec.kind.value,
                "rules": [
                    {
                        "phase": r.phase.value,
                        "severity": r.severity.value,
                        "outcome": r.outcome.value,
                        "retry": r.retry.value,
                    }
                    for r in spec.rules
                ],
                "context": list(spec.context),
                "since": spec.since,
                "deprecated_in": spec.deprecated_in,
                "replaced_by": list(spec.replaced_by) if spec.replaced_by else None,
            }
        )
    return {
        "registry_version": REGISTRY_VERSION,
        "reason_schema_version": REASON_SCHEMA_VERSION,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "entries": entries,
    }


def check_integrity() -> list[str]:
    """Structural problems in the catalog; an empty list means it is internally consistent."""
    problems: list[str] = []
    for spec in specs():
        name = f"{spec.namespace}.{spec.code}"
        if not spec.rules:
            problems.append(f"{name}: no phase rules")
        if spec.replaced_by is not None and spec.deprecated_in is None:
            problems.append(f"{name}: a replacement is only meaningful on a deprecated entry")
        if spec.replaced_by is not None and spec.replaced_by not in _SPECS:
            problems.append(f"{name}: replacement {spec.replaced_by} is not a declared identity")
    return problems
