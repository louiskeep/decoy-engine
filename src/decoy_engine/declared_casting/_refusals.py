"""Value-free record of the routes a declared contract kept a run off.

Admission layers decline in their own way (a token, a ``None``, a rejection list) and a declared
job then runs on a less bounded route. This ledger carries the registered refusal each layer got
from ``declared_route_guard`` to the run's ``quality_metrics["execution"]`` so the fallback is
machine-readable. Pattern source: the ``Warning``-registry / logging-context model: producers
note into a run-scoped collector and the run publishes one deduplicated list. Outside a run
(capacity estimates, plan reads) there is no collector and noting is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

KEY = "declared_route_refusals"

_ledger: ContextVar[dict[tuple[str, str], dict[str, Any]] | None] = ContextVar(
    "decoy_declared_route_refusals", default=None
)


@contextmanager
def collect_refusals() -> Iterator[dict[tuple[str, str], dict[str, Any]]]:
    """The run's collector; a nested run gets its own and never sees this one's entries."""
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    token = _ledger.set(entries)
    try:
        yield entries
    finally:
        _ledger.reset(token)


def note(route: str, code: Any, missing: Any = ()) -> None:
    """Record that a declared contract kept the run off ``route``, under registered ``code``."""
    entries = _ledger.get()
    if entries is None:
        return
    entries.setdefault(
        (route, str(code)),
        {
            "route": route,
            "namespace": code.namespace,
            "code": str(code),
            "missing": sorted(missing),
        },
    )


def note_declined(route: str, decision: Any) -> None:
    """Record ``decision`` (a declining ``CastRouteDecision``) for ``route`` in the open run."""
    if not decision.admitted and decision.rejection is not None:
        note(route, decision.rejection, decision.missing)


def refusal_for(route: str) -> dict[str, Any] | None:
    """The refusal noted so far for ``route`` in the open run, if any."""
    entries = _ledger.get()
    if not entries:
        return None
    return next((e for (r, _), e in entries.items() if r == route), None)


def refusal_text(entry: dict[str, Any]) -> str:
    """The fixed explanation line: the registered code plus the capabilities it names."""
    needs = ", ".join(entry["missing"])
    return f"{entry['code']}: the {entry['route']} route lacks {needs}" if needs else entry["code"]


def stamp(quality_metrics: dict[str, Any], entries: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Publish the run's refusals under ``execution``; no key when there are none."""
    if not entries:
        return
    block = quality_metrics.setdefault("execution", {})
    block[KEY] = [entries[k] for k in sorted(entries)]
    plan = quality_metrics.get("execution_plan")
    if isinstance(plan, dict):  # the explain block reads the same evidence
        plan[KEY] = block[KEY]
