"""Engine progress contract: value-free run events for callers' monitors.

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
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseEvent:
    """A named phase (resolve, mask, generate, finalize, publish) starting,
    finishing or erroring, optionally scoped to one table."""

    name: str
    status: Literal["start", "finish", "error"]
    table: str | None = None
    rows_in: int | None = None
    rows_out: int | None = None
    elapsed_s: float | None = None
    error_class: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="phase", init=False)


@dataclass(frozen=True)
class RateSample:
    """Rows done so far on one table and the running rate, emitted at most once
    per `interval_s` per table plus once at the end of that table's stream."""

    table: str
    rows_done: int
    rows_per_sec: float
    elapsed_s: float
    rows_total_estimate: int | None = None
    final: bool = False
    ts: float = field(default_factory=time.time)
    kind: str = field(default="rate", init=False)


@dataclass(frozen=True)
class RouteEvent:
    """The execution route the planner chose and, when it declined a lane, the
    coded reason."""

    route: str
    lane: str | None = None
    reason_code: str | None = None
    table: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="route", init=False)


@dataclass(frozen=True)
class WarningEvent:
    """A coded warning surfaced mid-run (never its message text)."""

    code: str
    table: str | None = None
    column: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="warning", init=False)


@dataclass(frozen=True)
class TerminalEvent:
    """The run's end: `ok` or `error` with the exception class name."""

    status: Literal["ok", "error"]
    error_class: str | None = None
    elapsed_s: float | None = None
    sink_failures: int = 0
    ts: float = field(default_factory=time.time)
    kind: str = field(default="terminal", init=False)


ProgressEvent = PhaseEvent | RateSample | RouteEvent | WarningEvent | TerminalEvent


def event_to_dict(event: ProgressEvent) -> dict[str, Any]:
    """A JSON-ready dict of the event (`kind` names the type)."""
    return dataclasses.asdict(event)


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
    receives `step` / `throughput_sample` calls with no change on its side."""

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
    __slots__ = ("last_emit", "rows", "started", "total")

    def __init__(self, started: float, total: int | None) -> None:
        self.rows = 0
        self.started = started
        self.last_emit = started
        self.total = total


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
        self.sink_failures = 0

    @property
    def active(self) -> bool:
        return self._sink is not None

    # -- raw emit ------------------------------------------------------------
    def emit(self, event: ProgressEvent) -> None:
        if self._sink is None:
            return
        try:
            self._sink.on_event(event)
        except Exception:
            self.sink_failures += 1

    # -- phases --------------------------------------------------------------
    def phase_start(
        self, name: str, *, table: str | None = None, rows_in: int | None = None
    ) -> float:
        self.emit(PhaseEvent(name, "start", table=table, rows_in=rows_in))
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
    ) -> None:
        self.emit(
            PhaseEvent(
                name,
                "finish",
                table=table,
                rows_in=rows_in,
                rows_out=rows_out,
                elapsed_s=time.perf_counter() - started,
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
                )
            )
            self._open_phases.pop((name, table), None)
            raise
        else:
            self.phase_finish(name, started, table=table, rows_in=rows_in)

    # -- routes / warnings ---------------------------------------------------
    def route(
        self,
        route: str,
        *,
        lane: str | None = None,
        reason_code: str | None = None,
        table: str | None = None,
    ) -> None:
        self.emit(RouteEvent(route, lane=lane, reason_code=reason_code, table=table))

    def warning(self, code: str, *, table: str | None = None, column: str | None = None) -> None:
        self.emit(WarningEvent(code, table=table, column=column))

    # -- rate samples --------------------------------------------------------
    def rows(self, table: str, n: int, *, total: int | None = None) -> None:
        """Account `n` more rows done on `table`; emits a sample when the
        interval has elapsed (time-based, so byte output never depends on
        chunk size)."""
        if self._sink is None:
            return
        now = time.perf_counter()
        rate = self._rates.get(table)
        if rate is None:
            rate = self._rates[table] = _Rate(now, total)
        rate.rows += int(n)
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
            )
        )
        rate.last_emit = now

    def count_rows(
        self, table: str, chunks: Iterable[Any], *, total: int | None = None
    ) -> Iterator[Any]:
        """Yield `chunks` unchanged, accounting each one's `num_rows`."""
        if self._sink is None:
            yield from chunks
            return
        try:
            for chunk in chunks:
                self.rows(table, getattr(chunk, "num_rows", 0), total=total)
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
            for (name, table), started in list(self._open_phases.items()):
                self.emit(
                    PhaseEvent(
                        name,
                        "error",
                        table=table,
                        elapsed_s=time.perf_counter() - started,
                        error_class=error_class,
                    )
                )
            self._open_phases.clear()
            self.emit(
                TerminalEvent(
                    "error",
                    error_class=error_class,
                    elapsed_s=time.perf_counter() - self._started,
                    sink_failures=self.sink_failures,
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
    "CallbackSink",
    "PhaseEvent",
    "ProgressChannel",
    "ProgressEvent",
    "ProgressSink",
    "RateSample",
    "RouteEvent",
    "StructuredEventsSink",
    "TerminalEvent",
    "WarningEvent",
    "event_to_dict",
    "get_active_channel",
    "use_progress",
]
