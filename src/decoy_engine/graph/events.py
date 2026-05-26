"""graph.events: node lifecycle record creation and structured telemetry.

Provides helpers for building NodeRunRecord dicts and emitting structured
log events for node start, finish, and failure.  Callers (primarily
_execute_graph) use these instead of inline record dicts and direct
log/emit_step calls so the lifecycle contract is defined in one place.

Audit Sprint 1.3 - Event, Result, And Error Boundary.

Done when:
  - Platform job logging does not need to parse one-off runner strings
    for node lifecycle facts.
  - New op authors do not touch runner internals to get normal
    event/result behavior.
"""

from __future__ import annotations

import traceback as _traceback
from typing import Any

from decoy_engine.context import emit_step, emit_throughput_sample
from decoy_engine.graph.types import NodeRunRecord


def emit_node_start(
    log: Any,
    step_name: str,
    descriptor: str,
    engine: str,
    rows_in: int,
) -> None:
    """Log node start and emit the step-start boundary.

    ``descriptor`` is the human-readable node label from ``_node_descriptor``.
    ``rows_in`` is the sum of upstream row counts (0 for source nodes).
    """
    if log is not None:
        log.info("graph: running node %s (engine=%s)", descriptor, engine)
    emit_step(log, step_name, status="start", rows_in=rows_in or None)


def make_node_ok_record(
    nid: str,
    kind: str,
    row_count: int,
    elapsed_ms: int,
    exports: dict[str, Any] | None,
    *,
    memory_delta_kb: int = 0,
) -> NodeRunRecord:
    """Build a successful NodeRunRecord for a stream or split op.

    ``memory_delta_kb`` is the RSS delta captured around node execution
    (PERF.BASE.1): rss_after minus rss_before, clamped to >= 0. Defaults
    to 0 for callers that have not been updated to capture memory;
    non-zero when the executor passes the measured delta.
    """
    return {
        "node_id": nid,
        "kind": kind,
        "status": "ok",
        "row_count": row_count,
        "elapsed_ms": elapsed_ms,
        "error": None,
        "exports": exports,
        "memory_delta_kb": memory_delta_kb,
    }


def emit_node_ok(
    log: Any,
    step_name: str,
    descriptor: str,
    rows_in: int,
    row_count: int,
    elapsed_ms: int,
    *,
    is_split: bool = False,
) -> None:
    """Log node success and emit the step-finish boundary and throughput sample.

    ``is_split`` adds a " (split)" suffix to the log line for ops with
    OUTPUT_KIND="split" so the narrative log distinguishes them from
    single-output ops.  Throughput sample is skipped for zero-duration
    or zero-row nodes to keep the chart free of Infinity / NaN.
    """
    if log is not None:
        suffix = " (split)" if is_split else ""
        log.info(
            "graph: node %s ok rows=%d elapsed=%dms%s",
            descriptor,
            row_count,
            elapsed_ms,
            suffix,
        )
    emit_step(log, step_name, status="finish", rows_in=rows_in or None, rows_out=row_count)
    if elapsed_ms > 0 and row_count > 0:
        emit_throughput_sample(log, row_count * 1000 / elapsed_ms)


def make_node_error_record(
    nid: str,
    kind: str,
    elapsed_ms: int,
    error: str,
    *,
    exports: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_path: str | None = None,
    memory_delta_kb: int = 0,
) -> NodeRunRecord:
    """Build a failed NodeRunRecord.

    ``error_code`` and ``error_path`` carry R3.4 typed error metadata when
    the translated exception exposes them.  Both are omitted from the dict
    when None so downstream readers that iterate record keys don't see
    unexpected None entries for the common non-R3.4 case.

    ``memory_delta_kb`` is the RSS delta captured around node execution
    (PERF.BASE.1). Captured even on error paths since hot-spot analysis
    cares about memory pressure regardless of outcome; in practice we
    expect this to be tiny on errors since the failed work didn't run.
    """
    rec: NodeRunRecord = {
        "node_id": nid,
        "kind": kind,
        "status": "error",
        "row_count": None,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "exports": exports,
        "memory_delta_kb": memory_delta_kb,
    }
    if error_code is not None:
        rec["error_code"] = error_code  # type: ignore[typeddict-unknown-key]
    if error_path is not None:
        rec["error_path"] = error_path  # type: ignore[typeddict-unknown-key]
    return rec


def emit_node_error(
    log: Any,
    step_name: str,
    descriptor: str,
    rows_in: int,
    exc: BaseException,
    translated: BaseException,
    nid: str,
    elapsed_ms: int,
) -> None:
    """Log node failure and emit the step-error boundary.

    ``exc`` is the original exception (for ``type(exc).__name__`` in the
    step payload so the JobLogger can group errors by class);  ``translated``
    is the user-facing message from ``translate_engine_error``.
    """
    if log is not None:
        log.error("graph: node %s failed: %s", descriptor, translated)
        log.error(_traceback.format_exc())
    emit_step(
        log,
        step_name,
        status="error",
        rows_in=rows_in or None,
        error_class=type(exc).__name__,
        error_msg=str(translated),
        node_id=nid,
    )
