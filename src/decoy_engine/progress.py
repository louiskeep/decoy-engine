"""Engine progress contract: value-free, versioned run events for callers' monitors.

Pattern: dbt's structured event stream (one typed, named event per
occurrence; the renderer is the consumer's) delivered through the same
thread-local binding `instrumentation.timing.use_collector` already uses, so
no internal route signature carries a sink. `run_pipeline` (and the other
public entry points) bind the caller's `ProgressSink` once; emit points in the
route drivers call the active channel, which is a no-op object when nothing is
bound. Byte output never depends on a sink: a sink that raises is counted and
reported once at the terminal event, never propagated.

Events carry table and column NAMES and COUNTS only, never cell values,
expression text, paths beyond the declared table name, or `str(exc)`
(observability rule: where and how much, never what).

Schema versions. Events written before F29 are legacy version 0: the keys of
`event_to_legacy_dict`. Version 1 (`event_to_dict`) is a strict additive
superset of those keys: it adds the schema and registry versions, a stream-local
`sequence`, an explicit `phase` and `object_ref`, byte counters with their basis,
and the optional `reason`, `retry`, `checkpoint` and `publication` groups. Every
field is typed and bounded; an event can only be serialized through an explicit
per-variant allowlist, so an arbitrary attribute never rides out of the process.

Pattern for the reason, retry and checkpoint vocabulary: RFC 9457 (stable
identity apart from occurrence detail) and the gRPC status-code guidance (retrying an
operation, restarting a larger operation and repairing state first are different
actions). The retry and checkpoint groups are *reports*: only the owner of a retry
schedule or a durable checkpoint emits them, and this module never retries or
stores anything.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol, runtime_checkable

from decoy_engine._progress_events import (
    EVENT_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    CheckpointEvent,
    CheckpointInfo,
    PhaseEvent,
    ProgressEvent,
    PublicationInfo,
    RateSample,
    RetryEvent,
    RetryInfo,
    RouteEvent,
    TerminalEvent,
    WarningEvent,
    event_to_dict,
    event_to_legacy_dict,
    safe_error_class,
)
from decoy_engine.reasons import Phase, ReasonRecord, approve_ref

# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


@runtime_checkable
class ProgressSink(Protocol):
    """What a caller hands to `run_pipeline(progress=...)`."""

    def on_event(self, event: ProgressEvent) -> None: ...


class CallbackSink:
    """Adapt a plain callable to the sink protocol."""

    def __init__(self, fn: Callable[[ProgressEvent], None]) -> None:
        self._fn = fn

    def on_event(self, event: ProgressEvent) -> None:
        self._fn(event)


class StructuredEventsSink:
    """Bridge events onto the optional `StructuredEvents` logger surface
    (`decoy_engine.context`), so a logger such as the platform's `JobLogger`
    receives `step` / `throughput_sample` calls with no change on its side.

    A documented lossy adapter: the old logger signatures carry no reason, bytes, retry or
    checkpoint. A consumer that needs the complete record binds a `CallbackSink` (or any
    `ProgressSink`) and reads `event_to_dict`; both can be bound without duplicating delivery
    because each receives each event once."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def on_event(self, event: ProgressEvent) -> None:
        from decoy_engine.context import emit_step, emit_throughput_sample

        if isinstance(event, PhaseEvent):
            name = event.name if event.table is None else f"{event.name}:{event.table}"
            status = {"start": "running", "finish": "finished", "error": "error"}[event.status]
            emit_step(
                self._logger,
                name,
                status=status,
                rows_in=event.rows_in,
                rows_out=event.rows_out,
                error_class=event.error_class,
            )
        elif isinstance(event, RateSample):
            emit_throughput_sample(self._logger, event.rows_per_sec)
        elif isinstance(event, RouteEvent):
            emit_step(self._logger, f"route:{event.route}", status="finished")
        elif isinstance(event, WarningEvent):
            warn = getattr(self._logger, "warning", None)
            if callable(warn):
                warn(
                    "engine warning %s (table=%s column=%s)", event.code, event.table, event.column
                )
        elif isinstance(event, TerminalEvent):
            emit_step(
                self._logger,
                "run",
                status="finished" if event.status == "ok" else "error",
                error_class=event.error_class,
            )


# ---------------------------------------------------------------------------
# The active channel
# ---------------------------------------------------------------------------


class _Rate:
    __slots__ = ("bytes_done", "last_emit", "phase", "rows", "started", "total")

    def __init__(self, started: float, total: int | None, phase: str | None = None) -> None:
        self.phase = phase
        self.rows = 0
        self.bytes_done: int | None = None
        self.started = started
        self.last_emit = started
        self.total = total


def _logical_nbytes(chunk: Any) -> int | None:
    """The Arrow logical size of a batch or table, from its buffer metadata (no data pass)."""
    nbytes = getattr(chunk, "nbytes", None)
    if isinstance(nbytes, int) and not isinstance(nbytes, bool) and nbytes >= 0:
        return nbytes
    return None


_PHASE_NAMES = {p.value: p for p in Phase}


def _phase_of(name: str | None) -> Phase:
    return _PHASE_NAMES.get(name or "", Phase.UNKNOWN)


class ProgressChannel:
    """The bound sink plus the per-table rate throttle. Never raises."""

    def __init__(self, sink: ProgressSink | None, *, interval_s: float = 1.0) -> None:
        if interval_s <= 0:
            raise ValueError(f"progress_interval_s must be > 0; got {interval_s!r}")
        self._sink = sink
        self._interval = float(interval_s)
        self._rates: dict[str, _Rate] = {}
        self._open_phases: dict[tuple[str, str | None], float] = {}
        self._started = time.perf_counter()
        self._sequence = 0
        self._lock = threading.Lock()
        self._retry_floor: dict[str, int] = {}
        self._checkpoint_floor = -1
        self.sink_failures = 0
        # Owner reports refused for going backwards; observational, never an exception.
        self.rejected_reports = 0

    @property
    def active(self) -> bool:
        return self._sink is not None

    # -- raw emit ------------------------------------------------------------
    def emit(self, event: ProgressEvent) -> None:
        if self._sink is None:
            return
        try:
            if event.sequence is None:
                with self._lock:
                    self._sequence += 1
                    sequence = self._sequence
                event = dataclasses.replace(event, sequence=sequence)
            self._sink.on_event(event)
        except Exception:
            self.sink_failures += 1

    # -- phases --------------------------------------------------------------
    def phase_start(
        self, name: str, *, table: str | None = None, rows_in: int | None = None
    ) -> float:
        self.emit(
            PhaseEvent(
                name,
                "start",
                table=table,
                rows_in=rows_in,
                phase=_ident(name),
                object_ref=_ident(table),
            )
        )
        started = time.perf_counter()
        self._open_phases[(name, table)] = started
        return started

    def phase_finish(
        self,
        name: str,
        started: float,
        *,
        table: str | None = None,
        rows_in: int | None = None,
        rows_out: int | None = None,
        stored_bytes: int | None = None,
    ) -> None:
        self.emit(
            PhaseEvent(
                name,
                "finish",
                table=table,
                rows_in=rows_in,
                rows_out=rows_out,
                elapsed_s=time.perf_counter() - started,
                phase=_ident(name),
                object_ref=_ident(table),
                bytes_done=stored_bytes,
                byte_basis="stored_output" if stored_bytes is not None else None,
            )
        )
        self._open_phases.pop((name, table), None)

    @contextmanager
    def phase(
        self, name: str, *, table: str | None = None, rows_in: int | None = None
    ) -> Iterator[None]:
        started = self.phase_start(name, table=table, rows_in=rows_in)
        try:
            yield
        except BaseException as exc:
            self.emit(
                PhaseEvent(
                    name,
                    "error",
                    table=table,
                    elapsed_s=time.perf_counter() - started,
                    error_class=type(exc).__name__,
                    phase=_ident(name),
                    object_ref=_ident(table),
                    reason=_capture(exc, name),
                )
            )
            self._open_phases.pop((name, table), None)
            raise
        else:
            self.phase_finish(name, started, table=table, rows_in=rows_in)

    def publish_stored(self, table: str, stored_bytes: int) -> None:
        """Report the serialized size of one table's output after its writer closed.

        A `publish` phase for the table, with `stored_output` as the byte basis (never mixed
        with the Arrow-logical counters of the rate samples)."""
        started = self.phase_start("publish", table=table)
        self.phase_finish("publish", started, table=table, stored_bytes=int(stored_bytes))

    # -- routes / warnings ---------------------------------------------------
    def route(
        self,
        route: str,
        *,
        lane: str | None = None,
        reason_code: str | None = None,
        table: str | None = None,
    ) -> None:
        self.emit(
            RouteEvent(
                route,
                lane=lane,
                reason_code=reason_code,
                table=table,
                phase="resolve",
                object_ref=_ident(table),
            )
        )

    def warning(self, code: str, *, table: str | None = None, column: str | None = None) -> None:
        self.emit(WarningEvent(code, table=table, column=column, object_ref=_ident(table)))

    # -- owner reports (retry / checkpoint) ------------------------------------
    def retry_report(
        self,
        retry: RetryInfo,
        *,
        table: str | None = None,
        reason: ReasonRecord | None = None,
    ) -> bool:
        """Report a retry owner's schedule. Only the owner of the schedule calls this.

        Attempt numbers never go backwards within a scope; a report that would is refused and
        counted in `rejected_reports`. Returns whether the report was emitted. A channel with no
        sink bound keeps no state at all (the shared no-op channel is never a run's record)."""
        if self._sink is None:
            return False
        if retry.attempt < self._retry_floor.get(retry.scope, 1):
            self.rejected_reports += 1
            return False
        self._retry_floor[retry.scope] = retry.attempt
        self.emit(RetryEvent(table=table, retry=retry, reason=reason, object_ref=_ident(table)))
        return True

    def checkpoint_report(
        self,
        checkpoint: CheckpointInfo,
        *,
        table: str | None = None,
        reason: ReasonRecord | None = None,
    ) -> bool:
        """Report a durability owner's checkpoint. ``committed`` needs a reference (validated).

        The checkpoint sequence never regresses; a report that would is refused and counted in
        `rejected_reports`. Returns whether the report was emitted."""
        if self._sink is None:
            return False
        if checkpoint.sequence is not None:
            if checkpoint.sequence < self._checkpoint_floor:
                self.rejected_reports += 1
                return False
            self._checkpoint_floor = checkpoint.sequence
        self.emit(
            CheckpointEvent(
                table=table, checkpoint=checkpoint, reason=reason, object_ref=_ident(table)
            )
        )
        return True

    # -- rate samples --------------------------------------------------------
    def rows(
        self,
        table: str,
        n: int,
        *,
        total: int | None = None,
        nbytes: int | None = None,
        phase: str | None = None,
    ) -> None:
        """Account `n` more rows done on `table`; emits a sample when the
        interval has elapsed (time-based, so byte output never depends on
        chunk size). `nbytes` is the batch's Arrow logical size when known."""
        if self._sink is None:
            return
        now = time.perf_counter()
        rate = self._rates.get(table)
        if rate is None:
            rate = self._rates[table] = _Rate(now, total, phase)
        rate.rows += int(n)
        if nbytes is not None:
            rate.bytes_done = (rate.bytes_done or 0) + int(nbytes)
        if total is not None:
            rate.total = total
        if now - rate.last_emit >= self._interval:
            self._emit_rate(table, rate, now, final=False)

    def flush(self, table: str) -> None:
        """Emit the final sample for `table` (its last row count), once."""
        rate = self._rates.pop(table, None)
        if rate is None or self._sink is None:
            return
        self._emit_rate(table, rate, time.perf_counter(), final=True)

    def _emit_rate(self, table: str, rate: _Rate, now: float, *, final: bool) -> None:
        elapsed = max(now - rate.started, 1e-9)
        self.emit(
            RateSample(
                table,
                rate.rows,
                rate.rows / elapsed,
                elapsed,
                rows_total_estimate=rate.total,
                final=final,
                phase=_ident(rate.phase),
                object_ref=_ident(table),
                bytes_done=rate.bytes_done,
                byte_basis="arrow_logical" if rate.bytes_done is not None else None,
            )
        )
        rate.last_emit = now

    def count_rows(
        self,
        table: str,
        chunks: Iterable[Any],
        *,
        total: int | None = None,
        phase: str | None = None,
    ) -> Iterator[Any]:
        """Yield `chunks` unchanged, accounting each one's `num_rows` and logical bytes.

        Counting reads buffer metadata only, so the data is not walked a second time and the
        iterator stays lazy: a chunk is counted when the consumer pulls it, so closing early or
        failing after a batch counts exactly the batches that were consumed."""
        if self._sink is None:
            yield from chunks
            return
        try:
            for chunk in chunks:
                self.rows(
                    table,
                    getattr(chunk, "num_rows", 0),
                    total=total,
                    nbytes=_logical_nbytes(chunk),
                    phase=phase,
                )
                yield chunk
        finally:
            self.flush(table)

    # -- terminal ------------------------------------------------------------
    @contextmanager
    def run_scope(self) -> Iterator[None]:
        """Emit the terminal event for the enclosed run, ok or error."""
        try:
            yield
        except BaseException as exc:
            # Any phase started via the manual `phase_start`/`phase_finish` pair
            # (the sequential, out-of-core, generate and generate+mask routes
            # compute `rows_out` at finish, so they cannot use the `phase()` CM)
            # that the raising work never finished would otherwise leave a
            # consumer's step stuck "running". Close each as an error before the
            # terminal event so a monitor sees a terminal state for every step.
            error_class = type(exc).__name__
            open_phases = list(self._open_phases.items())
            # The innermost open boundary names the phase; an already-captured exception reuses
            # its record, so a failure is never relabelled by an outer scope.
            innermost = open_phases[-1][0][0] if open_phases else None
            record = _capture(exc, innermost)
            for (name, table), started in open_phases:
                self.emit(
                    PhaseEvent(
                        name,
                        "error",
                        table=table,
                        elapsed_s=time.perf_counter() - started,
                        error_class=error_class,
                        phase=_ident(name),
                        object_ref=_ident(table),
                        reason=record,
                    )
                )
            self._open_phases.clear()
            self.emit(
                TerminalEvent(
                    "error",
                    error_class=error_class,
                    elapsed_s=time.perf_counter() - self._started,
                    sink_failures=self.sink_failures,
                    reason=record,
                )
            )
            raise
        else:
            for table in list(self._rates):
                self.flush(table)
            self.emit(
                TerminalEvent(
                    "ok",
                    elapsed_s=time.perf_counter() - self._started,
                    sink_failures=self.sink_failures,
                )
            )


def _ident(name: str | None) -> str | None:
    """An event identifier field value: approved shape, else omitted."""
    return approve_ref(name) if name is not None else None


def _capture(exc: BaseException, phase_name: str | None) -> ReasonRecord | None:
    """The run's first causal failure for ``exc``; never raises (progress is observational)."""
    try:
        from decoy_engine.reason_adapter import capture_failure

        return capture_failure(exc, phase=_phase_of(phase_name))
    except Exception:
        return None


_NULL = ProgressChannel(None)
_thread_local = threading.local()


def get_active_channel() -> ProgressChannel:
    """The channel bound to this thread, or the shared no-op channel."""
    return getattr(_thread_local, "channel", None) or _NULL


@contextmanager
def use_progress(
    sink: ProgressSink | None, *, interval_s: float = 1.0
) -> Iterator[ProgressChannel]:
    """Bind `sink` as this thread's active progress channel.

    `sink=None` leaves any outer binding in place (an inner public entry called
    from `run_pipeline` keeps reporting to the caller's sink), so nesting is
    safe. Restores the previous binding on exit.
    """
    if sink is None:
        yield get_active_channel()
        return
    if not callable(getattr(sink, "on_event", None)):
        raise TypeError(
            "progress must implement on_event(event) (see decoy_engine.progress.ProgressSink)"
        )
    previous = getattr(_thread_local, "channel", None)
    channel = ProgressChannel(sink, interval_s=interval_s)
    _thread_local.channel = channel
    try:
        yield channel
    finally:
        _thread_local.channel = previous


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "LEGACY_SCHEMA_VERSION",
    "CallbackSink",
    "CheckpointEvent",
    "CheckpointInfo",
    "PhaseEvent",
    "ProgressChannel",
    "ProgressEvent",
    "ProgressSink",
    "PublicationInfo",
    "RateSample",
    "RetryEvent",
    "RetryInfo",
    "RouteEvent",
    "StructuredEventsSink",
    "TerminalEvent",
    "WarningEvent",
    "event_to_dict",
    "event_to_legacy_dict",
    "get_active_channel",
    "safe_error_class",
    "use_progress",
]
