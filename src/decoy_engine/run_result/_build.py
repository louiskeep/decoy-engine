"""The one builder of a ``RunResult``: facts in, a validated frozen document out.

Pure and I/O free. Status and exit code come from ``derive_outcome`` called once with the
caller's ``blocking_failed``; the builder never decides blocking itself. Checks, route and
warnings are read from the run's quality metrics as codes and counts only, so no message,
sampled value or exception text reaches the document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, get_args

from decoy_engine.progress import EVENT_SCHEMA_VERSION, safe_error_class
from decoy_engine.reasons import REGISTRY_VERSION, ReasonRecord, approve_name, approve_ref
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

_MODES = tuple(m for m in get_args(ExecutionMode) if m != "other")
_MAX_CODES = 64
_MAX_WARNINGS = 256
_TRUNCATED: Final = "truncated"


class RunFacts(ExecutionFacts, Protocol):
    """``ExecutionResult`` as the builder reads it: also its metrics and warnings."""

    @property
    def quality_metrics(self) -> Mapping[str, Any]: ...

    @property
    def warnings(self) -> Sequence[Any]: ...


@dataclass(frozen=True)
class FailureInput:
    """A failed or cancelled run. ``reason`` is the registry record, never the exception."""

    category: FailureCategory
    reason: ReasonRecord | None
    error_class: str | None


def _route(result: RunFacts | None, chunked: bool, native: NativeRoute | None) -> Route:
    telemetry = result.quality_metrics.get("execution") if result is not None else None
    mode = telemetry.get("execution_mode") if isinstance(telemetry, Mapping) else None
    kind: ExecutionMode | None
    if chunked:
        kind = "chunked"
    elif mode is None:
        kind = None
    else:
        # A route the model has no name for (mixed, a later one) reads "other", never raises.
        kind = next((m for m in _MODES if m == mode), "other")
    raw = telemetry.get("route_reason") if isinstance(telemetry, Mapping) else None
    route_reason = approve_ref(raw)
    return Route(
        execution_mode=kind,
        route_reason=route_reason,
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


def _checks(result: RunFacts | None, blocking: bool) -> tuple[Check, ...]:
    metrics = result.quality_metrics if result is not None else {}
    found: list[Check] = []
    if "quality_summary" in metrics:
        refs = (approve_ref(c.get("code")) for c in metrics.get("failed_checks") or ())
        codes = [c for c in refs if c is not None][:_MAX_CODES]
        found.append(
            _check(
                "post_validation",
                "failed" if codes else "passed",
                blocking,
                (0, 0, len(codes), 0),
                codes,
            )
        )
    scan = metrics.get("post_scan_integrity")
    if isinstance(scan, Mapping):
        n = tuple(_count(scan, f"{k}_count") for k in ("pass", "warning", "fail", "error"))
        status: CheckStatus = (
            "error" if n[3] else "failed" if n[2] else "warned" if n[1] else "passed"
        )
        found.append(_check("post_scan_integrity", status, blocking, n, ()))
    if metrics.get("fidelity_reports"):
        found.append(_check("fidelity", "passed", False, (0, 0, 0, 0), ()))
    return tuple(found)


def _warnings(result: RunFacts | None) -> tuple[WarningEntry, ...]:
    seen: dict[tuple[str, str | None, str | None], int] = {}
    for item in result.warnings if result is not None else ():
        code = approve_ref(getattr(item, "code", None))
        if code is None:
            continue
        detail = getattr(item, "detail", None)
        table = approve_name(detail.get("table") if isinstance(detail, Mapping) else None)
        key = (code, table, approve_name(getattr(item, "column", None)))
        seen[key] = seen.get(key, 0) + 1
    entries = [
        WarningEntry(code=c, table=t, column=col, count=n) for (c, t, col), n in seen.items()
    ]
    if len(entries) > _MAX_WARNINGS:
        rest = sum(e.count for e in entries[_MAX_WARNINGS - 1 :])
        entries = [
            *entries[: _MAX_WARNINGS - 1],
            WarningEntry(code=_TRUNCATED, table=None, column=None, count=rest),
        ]
    return tuple(entries)


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
    links: Links,
    started_at: str,
    finished_at: str,
    elapsed_s: float,
    phases: Sequence[tuple[str, float]],
) -> RunResult:
    """``producer`` is the CLI version; the engine and registry versions are the builder's own."""
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
        route=_route(execution_result, chunked, native),
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
