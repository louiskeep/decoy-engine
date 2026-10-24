"""Unit tests for `decoy_engine.progress` (parity E1a): the channel, the
throttle, sink isolation, the structured-events adapter, and nesting."""

from __future__ import annotations

import json

import pyarrow as pa
import pytest

from decoy_engine import progress as P


class _Recorder:
    def __init__(self) -> None:
        self.events: list[P.ProgressEvent] = []

    def on_event(self, event: P.ProgressEvent) -> None:
        self.events.append(event)


class _Raising:
    def on_event(self, event: P.ProgressEvent) -> None:
        raise RuntimeError("sink broke")


def test_no_binding_is_a_shared_no_op_channel() -> None:
    ch = P.get_active_channel()
    assert ch.active is False
    ch.emit(P.PhaseEvent("x", "start"))  # never raises
    with ch.phase("x"):
        pass
    assert list(ch.count_rows("t", [pa.table({"a": [1]})])) == [pa.table({"a": [1]})]


def test_phase_and_terminal_events_in_order() -> None:
    rec = _Recorder()
    with P.use_progress(rec) as ch, ch.run_scope():
        with ch.phase("resolve"):
            pass
        ch.route("sequential", reason_code="fk")
        with ch.phase("mask", table="t", rows_in=3):
            pass
    kinds = [(e.kind, getattr(e, "status", None), getattr(e, "table", None)) for e in rec.events]
    assert kinds == [
        ("phase", "start", None),
        ("phase", "finish", None),
        ("route", None, None),
        ("phase", "start", "t"),
        ("phase", "finish", "t"),
        ("terminal", "ok", None),
    ]
    assert rec.events[-1].sink_failures == 0
    assert rec.events[1].elapsed_s is not None and rec.events[1].elapsed_s >= 0


def test_error_inside_phase_emits_error_and_terminal_error_then_reraises() -> None:
    rec = _Recorder()
    with pytest.raises(ValueError), P.use_progress(rec) as ch, ch.run_scope():
        with ch.phase("mask", table="t"):
            raise ValueError("secret-value-123")
    phase_err = [e for e in rec.events if e.kind == "phase" and e.status == "error"][0]
    assert phase_err.error_class == "ValueError"
    assert rec.events[-1].status == "error" and rec.events[-1].error_class == "ValueError"
    assert "secret-value-123" not in json.dumps([P.event_to_dict(e) for e in rec.events])


def test_raising_sink_never_changes_the_run_and_is_counted() -> None:
    with P.use_progress(_Raising()) as ch, ch.run_scope():
        with ch.phase("mask"):
            ch.rows("t", 10)
            ch.flush("t")
    assert ch.sink_failures >= 3


def test_rate_samples_are_time_throttled_and_flush_is_final() -> None:
    rec = _Recorder()
    with P.use_progress(rec, interval_s=3600) as ch:
        for _ in range(50):
            ch.rows("t", 100, total=5000)
        ch.flush("t")
    samples = [e for e in rec.events if e.kind == "rate"]
    assert len(samples) == 1
    assert samples[0].final is True
    assert samples[0].rows_done == 5000
    assert samples[0].rows_total_estimate == 5000
    assert samples[0].rows_per_sec > 0


def test_rate_samples_emit_when_interval_elapses() -> None:
    rec = _Recorder()
    with P.use_progress(rec, interval_s=1e-9) as ch:
        for _ in range(3):
            ch.rows("t", 1)
        ch.flush("t")
    samples = [e for e in rec.events if e.kind == "rate"]
    # The first call can land inside the same clock tick as the bind; every
    # later one is past the interval, plus the final flush.
    assert 3 <= len(samples) <= 4
    assert samples[-1].final is True and samples[-1].rows_done == 3


def test_count_rows_accounts_every_chunk_and_flushes() -> None:
    rec = _Recorder()
    chunks = [pa.table({"a": list(range(n))}) for n in (3, 4, 5)]
    with P.use_progress(rec, interval_s=3600) as ch:
        out = list(ch.count_rows("t", chunks))
    assert out == chunks
    final = [e for e in rec.events if e.kind == "rate"][-1]
    assert final.rows_done == 12 and final.final


def test_nested_none_keeps_the_outer_sink() -> None:
    rec = _Recorder()
    with P.use_progress(rec) as outer, P.use_progress(None) as inner:
        assert inner is outer
    assert P.get_active_channel().active is False


def test_invalid_sink_and_interval_are_rejected() -> None:
    with pytest.raises(TypeError):
        with P.use_progress(object()):  # type: ignore[arg-type]
            pass
    with pytest.raises(ValueError):
        P.ProgressChannel(_Recorder(), interval_s=0)


def test_structured_events_adapter_maps_onto_logger_methods() -> None:
    calls: list[tuple] = []

    class Logger:
        def step(self, name, *, status="running", rows_in=None, rows_out=None, error_class=None, error_msg=None, node_id=None):
            calls.append(("step", name, status, rows_in, rows_out, error_class))

        def throughput_sample(self, rows_per_sec):
            calls.append(("tp", round(rows_per_sec)))

        def warning(self, msg, *args):
            calls.append(("warn", msg % args))

        def info(self, *a, **k): ...
        def debug(self, *a, **k): ...
        def error(self, *a, **k): ...

    sink = P.StructuredEventsSink(Logger())
    sink.on_event(P.PhaseEvent("mask", "start", table="t", rows_in=5))
    sink.on_event(P.PhaseEvent("mask", "finish", table="t", rows_out=5))
    sink.on_event(P.RateSample("t", 10, 100.0, 0.1))
    sink.on_event(P.RouteEvent("sequential"))
    sink.on_event(P.WarningEvent("x_code", table="t", column="c"))
    sink.on_event(P.TerminalEvent("error", error_class="ValueError"))
    assert calls == [
        ("step", "mask:t", "running", 5, None, None),
        ("step", "mask:t", "finished", None, 5, None),
        ("tp", 100),
        ("step", "route:sequential", "finished", None, None, None),
        ("warn", "engine warning x_code (table=t column=c)"),
        ("step", "run", "error", None, None, "ValueError"),
    ]


def test_callback_sink_and_event_dicts_are_json_ready() -> None:
    seen: list[dict] = []
    sink = P.CallbackSink(lambda e: seen.append(P.event_to_dict(e)))
    with P.use_progress(sink) as ch, ch.run_scope():
        ch.route("full_frame", lane="unified_slice")
    json.dumps(seen)
    assert seen[0]["kind"] == "route" and seen[0]["lane"] == "unified_slice"
    assert seen[-1]["kind"] == "terminal"
