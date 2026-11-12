"""The one builder of a ``RunResult``: facts in, a validated frozen document out.

Pure and I/O free. Status and exit code come from ``derive_outcome`` called once with the
caller's ``blocking_failed``; the builder never decides blocking itself. Checks and warnings are
read from the run's quality metrics as codes and counts only; the route arrives as a caller fact
(``route_summary``), so the metrics' route block has one reader. No message, sampled value or
exception text reaches the document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, get_args

from decoy_engine.progress import EVENT_SCHEMA_VERSION, safe_error_class, safe_registered_code
from decoy_engine.reason_adapter import report_unavailable
from decoy_engine.reasons import REGISTRY_VERSION, ReasonRecord, approve_name, approve_ref, decoy
from decoy_engine.release import __version__
from decoy_engine.run_result._model import (
    Check,
    CheckCounts,
    CheckStatus,
    ConfigRef,
    ExecutionMode,
    Failure,
    FailureCategory,
    Links,
    NativeRoute,
    PhaseTiming,
    ProducerInfo,
    Route,
    RunResult,
    Timings,
    WarningEntry,
)
from decoy_engine.run_result._outcome import derive_outcome
from decoy_engine.run_result._project import ExecutionFacts, ProjectedTables

_MAX_CODES = 64
_MAX_WARNINGS = 256
_Key = tuple[str, str | None, str | None]
# The route vocabulary of ``route_summary`` against the model's ``ExecutionMode``. A route the
# table lacks (a later one) reads "other", never raises; the exhaustiveness test keeps every
# route the engine writes listed here on purpose.
_ROUTE_MODE: dict[str, ExecutionMode | None] = {
    **{m: m for m in get_args(ExecutionMode) if m != "other"},
    "auto_chunk": "chunked",
    "mixed": "other",
    "unknown": None,
}


class RunFacts(ExecutionFacts, Protocol):
    """``ExecutionResult`` as the builder reads it: also its metrics and warnings."""

    @property
    def quality_metrics(self) -> Mapping[str, Any]: ...

    @property
    def warnings(self) -> Sequence[Any]: ...


class RouteView(Protocol):
    """The route as a caller fact: ``decoy_engine.route_summary(quality_metrics)`` fits."""

    @property
    def route(self) -> str: ...

    @property
    def reason_code(self) -> str | None: ...


@dataclass(frozen=True)
class FailureInput:
    """A failed or cancelled run. ``reason`` is the registry record, never the exception."""

    category: FailureCategory
    reason: ReasonRecord | None
    error_class: str | None


def _registered(raw: Any) -> str:
    """A code the registry knows, else the fixed ``unregistered_reason``; a count is never lost."""
    return safe_registered_code(raw if isinstance(raw, str) else None) or str(
        decoy.UNREGISTERED_REASON
    )


def _route(view: RouteView | None, chunked: bool, native: NativeRoute | None) -> Route:
    kind = None if view is None else _ROUTE_MODE.get(view.route, "other")
    chunked = chunked or kind == "chunked"
    reason = None if view is None or view.reason_code is None else _registered(view.reason_code)
    return Route(
        execution_mode="chunked" if chunked else kind,
        route_reason=reason,
        chunked=chunked,
        native=native,
    )


def _count(report: Mapping[str, Any], key: str) -> int:
    value = report.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _check(
    id_: Any, status: CheckStatus, blocking: bool, counts: tuple[int, ...], codes: Any
) -> Check:
    passed, warning, fail, error = counts
    return Check(
        id=id_,
        status=status,
        blocking=blocking and status in ("failed", "error"),
        counts=CheckCounts(**{"pass": passed, "warning": warning, "fail": fail, "error": error}),
        codes=tuple(codes),
    )


def _fidelity_warned(report: Any) -> bool:
    """A table whose report carries warnings, grades F or is unavailable. Codes only."""
    if not isinstance(report, Mapping):
        return True
    return bool(report.get("warnings")) or report.get("grade") in ("F", "unavailable")


def _checks(result: RunFacts | None, blocking: bool) -> tuple[Check, ...]:
    metrics = result.quality_metrics if result is not None else {}
    found: list[Check] = []
    if "quality_summary" in metrics:
        rows = list(metrics.get("failed_checks") or ())
        refs = (approve_ref(r.get("code")) for r in rows if isinstance(r, Mapping))
        codes = [c for c in refs if c is not None][:_MAX_CODES]
        # Every row is one failed check. A row whose code cannot be shown, or that falls
        # past the code cap, is still counted.
        failed = len(rows)
        status: CheckStatus = "failed" if failed else "passed"
        found.append(_check("post_validation", status, blocking, (0, 0, failed, 0), codes))
    scan = metrics.get("post_scan_integrity")
    if isinstance(scan, Mapping):
        n = tuple(_count(scan, f"{k}_count") for k in ("pass", "warning", "fail", "error"))
        scan_status: CheckStatus = (
            "error"
            if report_unavailable(scan)
            else "failed"
            if n[2]
            else "warned"
            if n[1]
            else "passed"
        )
        # Post-scan only reports in this slice, so it never blocks the run.
        found.append(_check("post_scan_integrity", scan_status, False, n, ()))
    reports = metrics.get("fidelity_reports")
    if isinstance(reports, Mapping) and reports:
        warned = sum(_fidelity_warned(r) for r in reports.values())
        status = "warned" if warned else "passed"
        found.append(_check("fidelity", status, False, (len(reports) - warned, warned, 0, 0), ()))
    return tuple(found)


def _refold(seen: dict[_Key, int], keep: int) -> dict[_Key, int]:
    """Sum counts over the first ``keep`` key parts, in first-seen order; the rest become null."""
    out: dict[_Key, int] = {}
    for (code, table, _column), count in seen.items():
        short: _Key = (code, table if keep > 1 else None, None)
        out[short] = out.get(short, 0) + count
    return out


def _warnings(result: RunFacts | None) -> tuple[WarningEntry, ...]:
    """Warnings by (code, table, column). Past the model's cap the key coarsens to (code, table),
    then to code alone, summing counts, so no count is lost. Order is first-seen at each level."""
    seen: dict[_Key, int] = {}
    for item in result.warnings if result is not None else ():
        code = _registered(getattr(item, "code", None))
        detail = getattr(item, "detail", None)
        table = approve_name(detail.get("table") if isinstance(detail, Mapping) else None)
        key = (code, table, approve_name(getattr(item, "column", None)))
        seen[key] = seen.get(key, 0) + 1
    for keep in (2, 1):
        if len(seen) > _MAX_WARNINGS:
            seen = _refold(seen, keep)
    # Unreachable while the registry holds fewer warning codes than the cap.
    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0][0]))[:_MAX_WARNINGS]
    kept = {k for k, _ in ranked}
    return tuple(
        WarningEntry(code=c, table=t, column=col, count=n)
        for (c, t, col), n in seen.items()
        if (c, t, col) in kept
    )


def build_run_result(
    *,
    tables: ProjectedTables,
    execution_result: RunFacts | None,
    failure: FailureInput | None,
    config_ref: ConfigRef | None,
    producer: str,
    invocation_id: str,
    exit_map: Mapping[tuple[str, str | None], int],
    blocking_failed: bool,
    native: NativeRoute | None,
    chunked: bool,
    route: RouteView | None,
    links: Links,
    started_at: str,
    finished_at: str,
    elapsed_s: float,
    phases: Sequence[tuple[str, float]],
) -> RunResult:
    """``producer`` is the CLI version; the engine and registry versions are the builder's own.

    ``route`` is ``route_summary(quality_metrics)``, read once by the caller; ``chunked`` is the
    caller's own fact that the ``--chunked`` entry ran, which leaves no route in the metrics."""
    if tables.run_failed != (failure is not None):
        raise ValueError("tables.run_failed disagrees with failure")
    outcome = derive_outcome(
        category=None if failure is None else failure.category,
        delivery_mode=tables.delivery.mode,
        delivery_outcome=tables.delivery.outcome,
        blocking_failed=blocking_failed,
        exit_map=exit_map,
    )
    kinds = {t.kind for t in tables.tables}
    return RunResult(
        invocation_id=invocation_id,
        status=outcome.status,
        exit_code=outcome.exit_code,
        pipeline_kind=None if not kinds else "mixed" if len(kinds) > 1 else kinds.pop(),
        producer=ProducerInfo(
            cli_version=producer,
            engine_version=__version__,
            registry_version=REGISTRY_VERSION,
            event_schema=EVENT_SCHEMA_VERSION,
        ),
        config=config_ref,
        route=_route(route, chunked, native),
        delivery=tables.delivery,
        tables=tables.tables,
        checks=_checks(execution_result, blocking_failed),
        warnings=_warnings(execution_result),
        failure=None
        if failure is None
        else Failure(
            category=failure.category,
            error_class=safe_error_class(failure.error_class),
            reason=None if failure.reason is None else failure.reason.to_payload(),
        ),
        timings=Timings(
            started_at=started_at,
            finished_at=finished_at,
            elapsed_s=elapsed_s,
            phases=tuple(PhaseTiming(name=n, elapsed_s=s) for n, s in phases),
        ),
        links=links,
    )
