"""The frozen route view of a run's `quality_metrics`.

The engine already records `execution`, `auto_chunk`, `chunked_route` and
`resources.out_of_core` blocks. A caller that renders a run summary reads this one record
instead of those dict paths. A resource is reported as applied only from evidence a route wrote:
a configured cap alone never counts, so a route that never opened DuckDB or a native kernel
reports `None` / an empty tuple. Unknown keys are ignored; missing blocks give `route="unknown"`.

Nothing here carries a data value, an expression or a path: every field is a code, a mode name
or a number. Cap provenance (`auto`, a flag, an env var) belongs to whoever chose the cap, so
the caller passes the two source labels in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from decoy_engine import reasons

ROUTE_UNKNOWN = "unknown"
ROUTE_AUTO_CHUNK = "auto_chunk"
NATIVE_ADMITTED = "admitted"
NATIVE_NOT_INSTALLED = "not_installed"
NATIVE_DECLINED_PREFIX = "declined: "
# A native kernel that is missing or ABI-incompatible is reported under one code family:
# `<kernel>_extension_unavailable` / `<kernel>_ext_unavailable` (route namespace) and the
# physical companion's own code. One rule, so a new kernel needs no second list.
_NOT_INSTALLED_SUFFIXES = ("_extension_unavailable", "_ext_unavailable")


@dataclass(frozen=True)
class RouteSummary:
    """What route ran, why, and which resource settings the route actually applied."""

    route: str
    reason_code: str | None
    reason_text: str | None
    input_mode: str | None
    output_mode: str | None
    thread_cap: int | None
    thread_cap_source: str | None
    native_threads_applied: int | None
    duckdb_threads_applied: tuple[int, ...]
    out_of_core_memory_cap_bytes: int | None
    out_of_core_memory_cap_source: str | None
    out_of_core_memory_limits_applied_bytes: tuple[int, ...]
    native: str | None


def _block(metrics: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = metrics.get(key)
    return value if isinstance(value, Mapping) else {}


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _mode(block: Mapping[str, Any]) -> str | None:
    return _text_or_none(block.get("mode"))


def _distinct(values: list[Any]) -> tuple[int, ...]:
    return tuple(sorted({v for v in map(_int_or_none, values) if v is not None}))


def is_native_not_installed(code: str) -> bool:
    """Whether a native decline code means the compiled companion or a kernel is absent."""
    return code == reasons.physical.NATIVE_COMPANION_UNAVAILABLE or code.endswith(
        _NOT_INSTALLED_SUFFIXES
    )


@dataclass(frozen=True)
class _Unit:
    """One routed table's lane stamp and native evidence."""

    lane: Mapping[str, Any]
    chunked: Mapping[str, Any]


def _units(metrics: Mapping[str, Any]) -> list[_Unit]:
    """Every routed table's evidence: the single lane, the multi-table split or the mixed route."""
    lane = _block(metrics, "auto_chunk")
    by_table = _block(metrics, "chunked_route_by_table")
    if by_table:
        entries = [t for t in lane.get("tables", ()) if isinstance(t, Mapping)]
        return [
            _Unit(t, _block(by_table, str(t.get("table")))) for t in entries if t.get("dispatched")
        ]
    mixed = _block(_block(metrics, "mixed_route"), "mask_tables")
    if mixed:
        return [
            _Unit(_block(m, "auto_chunk"), _block(m, "chunked_route"))
            for m in mixed.values()
            if isinstance(m, Mapping)
        ]
    if lane or "chunked_route" in metrics:
        return [_Unit(lane, _block(metrics, "chunked_route"))]
    return []


def _unit_status(chunked: Mapping[str, Any]) -> str | None:
    if not chunked:
        return None
    if chunked.get("native_admitted") is True:
        return NATIVE_ADMITTED
    code = _text_or_none(chunked.get("reroute_reason"))
    if code is None:
        return None
    if is_native_not_installed(code):
        return NATIVE_NOT_INSTALLED
    return f"{NATIVE_DECLINED_PREFIX}{code}"


def _native_status(units: list[_Unit]) -> str | None:
    """`admitted` when native ran for any table; else the first decline (not_installed first)."""
    found = {s for s in (_unit_status(u.chunked) for u in units) if s is not None}
    if NATIVE_ADMITTED in found:
        return NATIVE_ADMITTED
    return min(found, key=lambda s: (s != NATIVE_NOT_INSTALLED, s)) if found else None


def _native_applied(units: list[_Unit]) -> int | None:
    """The clamp native actually ran with; a clamp recorded for a declined table is not evidence."""
    ran = [
        _int_or_none(_block(u.lane, "native_thread_clamp").get("applied"))
        for u in units
        if u.chunked.get("native_admitted") is True
    ]
    return max((v for v in ran if v is not None), default=None)


def _agreed_mode(units: list[_Unit], key: str) -> str | None:
    modes = {_mode(_block(u.lane, key)) for u in units}
    return modes.pop() if len(modes) == 1 else None


def route_summary(
    quality_metrics: Mapping[str, Any] | None,
    *,
    thread_cap_source: str | None = None,
    out_of_core_memory_cap_source: str | None = None,
) -> RouteSummary:
    """Read the route, its reason and the applied resources out of `quality_metrics`."""
    metrics = quality_metrics if isinstance(quality_metrics, Mapping) else {}
    execution = _block(metrics, "execution")
    lane = _block(metrics, "auto_chunk")
    ooc = _block(_block(metrics, "resources"), "out_of_core")
    units = _units(metrics)

    if lane.get("lane") is not None:
        route = ROUTE_AUTO_CHUNK
    else:
        route = _text_or_none(execution.get("execution_mode")) or ROUTE_UNKNOWN
    reason_code = _text_or_none(execution.get("route_reason")) or _text_or_none(
        lane.get("lane_reason")
    )
    spec = reasons.find("route", reason_code)

    applied = [p for p in ooc.get("applied", ()) if isinstance(p, Mapping)]
    thread_cap = _int_or_none(ooc.get("thread_cap"))
    if thread_cap is None:
        requested = [_int_or_none(u.lane.get("native_threads")) for u in units]
        thread_cap = next((v for v in requested if v is not None), None)
    memory_cap = _int_or_none(ooc.get("memory_cap_bytes"))
    mixed_output = _mode(_block(_block(metrics, "mixed_route"), "output"))

    return RouteSummary(
        route=route,
        reason_code=reason_code,
        reason_text=None if spec is None else spec.summary,
        input_mode=_agreed_mode(units, "input"),
        output_mode=mixed_output or _agreed_mode(units, "output"),
        thread_cap=thread_cap,
        thread_cap_source=thread_cap_source if thread_cap is not None else None,
        native_threads_applied=_native_applied(units),
        duckdb_threads_applied=_distinct([p.get("threads") for p in applied]),
        out_of_core_memory_cap_bytes=memory_cap,
        out_of_core_memory_cap_source=out_of_core_memory_cap_source if memory_cap else None,
        out_of_core_memory_limits_applied_bytes=_distinct(
            [p.get("memory_limit_bytes") for p in applied]
        ),
        native=_native_status(units),
    )
