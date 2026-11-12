"""The progress collector: a ``ProgressSink`` that keeps typed per-table counters.

It reads only phase names, statuses, approved table names, row counts and seconds from events,
never an error message or a value. ``snapshot()`` returns the ``CollectedFacts`` that
``project_tables`` consumes. ``on_event`` never raises: a run must not change because a record of
it is being kept.
"""

from __future__ import annotations

import threading

from decoy_engine.progress import PhaseEvent, ProgressEvent, RateSample
from decoy_engine.reasons import approve_name
from decoy_engine.run_result._project import CollectedFacts

# Only these phases change per-table facts. ``resolve`` ran no table (its failure must read as
# every table not run) and ``publish`` is delivery's fact, so both are timing only.
_TABLE_WORK = frozenset({"mask", "generate"})


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


class RunResultCollector:
    """Counts what the progress stream says each table did. Thread safe: the channel calls the
    sink on whichever thread emits, outside its own lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started: set[str] = set()
        self._finished: dict[str, int | None] = {}
        self._errored: set[str] = set()
        self._rate_rows: dict[str, int] = {}
        self._elapsed: dict[str, float] = {}
        self._aggregate_error = False
        self._phases: dict[str, float] = {}

    def on_event(self, event: ProgressEvent) -> None:
        try:
            if isinstance(event, PhaseEvent):
                self._phase(event)
            elif isinstance(event, RateSample):
                self._rate(event)
        except Exception:
            # Counted as nothing. Every read happens before the lock, so a failed read leaves
            # the counters as they were.
            return

    def _phase(self, event: PhaseEvent) -> None:
        name, status, raw_table = event.name, event.status, event.table
        table = None if raw_table is None else approve_name(raw_table)
        if raw_table is not None and table is None:
            return
        rows_out, seconds = _count(event.rows_out), _seconds(event.elapsed_s)
        work = name in _TABLE_WORK
        with self._lock:
            if status != "start" and seconds is not None:
                self._phases[name] = self._phases.get(name, 0.0) + seconds
            if not work:
                return
            if table is None:
                # The full-frame mask phase spans the run: its rows are never split across tables.
                self._aggregate_error |= status == "error"
                return
            if status == "start":
                self._started.add(table)
                return
            if status == "finish":
                self._finished[table] = rows_out
            else:
                self._errored.add(table)
            if seconds is not None:
                self._elapsed[table] = self._elapsed.get(table, 0.0) + seconds

    def _rate(self, event: RateSample) -> None:
        table = approve_name(event.table)
        rows_done, final = _count(event.rows_done), event.final is True
        if table is None or rows_done is None:
            return
        with self._lock:
            if final:
                self._rate_rows[table] = rows_done
            else:
                self._started.add(table)

    def snapshot(self) -> CollectedFacts:
        with self._lock:
            return CollectedFacts(
                started=frozenset(self._started),
                finished=dict(self._finished),
                errored=frozenset(self._errored),
                rate_rows=dict(self._rate_rows),
                elapsed=dict(self._elapsed),
                aggregate_error=self._aggregate_error,
                phases=tuple(self._phases.items()),
            )
