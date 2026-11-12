"""``SpanRecorder``: a progress sink that pairs phase events into closed ``SpanRecord`` values.

Pattern: OpenTelemetry's split between a library and its application. A library depends on
nothing but the API and the application owns the SDK and exporter; the engine goes one step
further and depends on no OpenTelemetry package at all. It hands finished, closed records to
the owner's ``on_span`` callback, and the owner (Platform, CLI) turns them into SDK spans. Sink
failure isolation follows ``ProgressChannel.emit``: a raising callback is counted, never raised.

The table name is a pairing key only. It is never copied into a record, so two tables running
the same phase on two threads stay two spans without a table name ever leaving the process.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from decoy_engine.progress import PhaseEvent, ProgressEvent, TerminalEvent, safe_error_class
from decoy_engine.reasons import ReasonRecord
from decoy_engine.telemetry._vocab import PHASE_VALUES, SpanRecord, reason_value

Attribute = tuple[str, "str | int | float"]


def _seconds(value: object) -> float | None:
    """A finite non-negative int or float (exact types) as a float, else ``None``."""
    if (type(value) is int or type(value) is float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _ns(seconds: float) -> int:
    """Seconds as whole nanoseconds; anything non-finite or negative is zero."""
    value = _seconds(seconds)
    return 0 if value is None else round(value * 1e9)


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _phase_value(name: object) -> str:
    return name if type(name) is str and name in PHASE_VALUES else "unknown"


class SpanRecorder:
    """Pairs ``start`` with ``finish``/``error`` per ``(phase, table)`` and reports each span once.

    Spans past ``max_spans`` are counted in ``dropped``. A ``TerminalEvent`` only records the
    last terminal status: a nested run emits its terminal before the outer run ends and some
    routes emit none, so the owner calls ``close()`` when its run ends, which closes every
    still-open phase as not ok. ``close()`` is idempotent; events after it are ignored.
    """

    def __init__(self, on_span: Callable[[SpanRecord], None], *, max_spans: int = 256) -> None:
        if type(max_spans) is not int or max_spans < 0:
            raise ValueError("max_spans must be a non-negative integer")
        self._on_span = on_span
        self._max = max_spans
        self._lock = threading.Lock()
        self._open: dict[tuple[str, str | None], int] = {}
        self._emitted = 0
        self._closed = False
        self.dropped = 0
        self.unmatched = 0
        self.callback_failures = 0
        self.terminal_status: str | None = None

    def on_event(self, event: ProgressEvent) -> None:
        if type(event) is PhaseEvent:
            self._on_phase(event)
        elif type(event) is TerminalEvent:
            with self._lock:
                if not self._closed:
                    self.terminal_status = event.status if event.status in ("ok", "error") else None

    def _on_phase(self, event: PhaseEvent) -> None:
        key = (event.name, event.table)
        if event.status == "start":
            with self._lock:
                if not self._closed:
                    self._open[key] = _ns(event.ts)
            return
        with self._lock:
            if self._closed:
                return
            start = self._open.pop(key, None)
            if start is None:
                self.unmatched += 1
                return
            if self._emitted >= self._max:
                self.dropped += 1
                return
            record = self._finished(event, start)
            self._emitted += 1
        self._deliver(record)

    def _finished(self, event: PhaseEvent, start_ns: int) -> SpanRecord:
        phase = _phase_value(event.name)
        if event.elapsed_s is not None:
            end_ns = start_ns + _ns(event.elapsed_s)
        else:
            end_ns = _ns(event.ts)
        attrs: dict[str, str | int | float] = {"decoy.phase": phase}
        for key, value in (
            ("decoy.rows_in", _count(event.rows_in)),
            ("decoy.rows_out", _count(event.rows_out)),
            ("decoy.bytes", _count(event.bytes_done)),
        ):
            if value is not None:
                attrs[key] = value
        elapsed = _seconds(event.elapsed_s)
        if elapsed is not None:
            attrs["decoy.elapsed_s"] = elapsed
        ok = event.status == "finish"
        if not ok:
            attrs["error.type"] = safe_error_class(event.error_class) or "Exception"
        reason = event.reason
        if type(reason) is ReasonRecord:
            code = reason_value(reason.namespace, reason.code)
            if code is not None:
                attrs["decoy.reason"] = code
        return SpanRecord(
            f"decoy.phase.{phase}",
            start_ns,
            max(end_ns, start_ns),
            ok,
            tuple(sorted(attrs.items())),
        )

    def close(self) -> None:
        """End every still-open phase as not ok; later calls do nothing."""
        records: list[SpanRecord] = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            now = time.time_ns()
            opened = list(self._open.items())
            self._open.clear()
            for (name, _table), start in opened:
                if self._emitted >= self._max:
                    self.dropped += 1
                    continue
                phase = _phase_value(name)
                records.append(
                    SpanRecord(
                        f"decoy.phase.{phase}",
                        start,
                        max(now, start),
                        False,
                        (("decoy.phase", phase),),
                    )
                )
                self._emitted += 1
        for record in records:
            self._deliver(record)

    def _deliver(self, record: SpanRecord) -> None:
        try:
            self._on_span(record)
        except Exception:
            with self._lock:
                self.callback_failures += 1
