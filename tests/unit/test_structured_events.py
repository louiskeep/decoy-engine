"""Tests for the optional structured-event surface (LOGGING_GUIDE §5d).

The engine emits step boundaries, lineage, fidelity, quarantines, and
throughput samples through module-level ``emit_*`` helpers — not direct
method calls — so a logger that doesn't implement them (e.g. a bare
stdlib ``logging.Logger`` the engine falls back to when no caller
context is provided) never raises AttributeError. These tests pin that
contract:

  - present method → called with the right arguments
  - missing method → silent no-op
  - logger is None → silent no-op
  - method raises → exception swallowed, engine keeps going
"""

import logging

import pytest

from decoy_engine import (
    StructuredEvents,
    emit_fidelity,
    emit_lineage,
    emit_quarantine,
    emit_step,
    emit_throughput_sample,
)


class CapturingStructured:
    """Records every structured emission for later assertion."""

    def __init__(self) -> None:
        self.steps: list[tuple] = []
        self.lineages: list[tuple] = []
        self.fidelities: list[tuple] = []
        self.quarantines: list[tuple] = []
        self.throughputs: list[float] = []

    def step(self, name, *, status="running", rows_in=None, rows_out=None):
        self.steps.append((name, status, rows_in, rows_out))

    def lineage(self, kind, label, type_):
        self.lineages.append((kind, label, type_))

    def fidelity(self, metric, value):
        self.fidelities.append((metric, value))

    def quarantine(self, step, reason, count):
        self.quarantines.append((step, reason, count))

    def throughput_sample(self, rows_per_sec):
        self.throughputs.append(rows_per_sec)


class NarrativeOnly:
    """Has the four narrative methods but no structured surface — like a
    stdlib logger, or the existing CapturingLogger in
    test_context_injection.py before this slice."""

    def debug(self, msg, *args, **kwargs): pass
    def info(self, msg, *args, **kwargs): pass
    def warning(self, msg, *args, **kwargs): pass
    def error(self, msg, *args, **kwargs): pass


class Exploder:
    """Implements the structured surface but raises on every call. The
    engine must not let a logger failure escalate into a job failure."""

    def step(self, *a, **kw): raise RuntimeError("boom")
    def lineage(self, *a, **kw): raise RuntimeError("boom")
    def fidelity(self, *a, **kw): raise RuntimeError("boom")
    def quarantine(self, *a, **kw): raise RuntimeError("boom")
    def throughput_sample(self, *a, **kw): raise RuntimeError("boom")


class TestEmitStep:
    def test_calls_through_when_method_present(self):
        cap = CapturingStructured()
        emit_step(cap, "read", status="start")
        emit_step(cap, "read", status="finish", rows_in=1000, rows_out=998)
        assert cap.steps == [
            ("read", "start", None, None),
            ("read", "finish", 1000, 998),
        ]

    def test_noop_when_method_missing(self):
        emit_step(NarrativeOnly(), "read", status="start")  # no AttributeError

    def test_noop_when_logger_none(self):
        emit_step(None, "read", status="start")

    def test_noop_when_method_raises(self):
        # Engine must not crash because the logger crashed.
        emit_step(Exploder(), "read", status="start")

    def test_works_with_stdlib_logger(self):
        # The most-common engine fallback. No `step` method, must no-op.
        emit_step(logging.getLogger("decoy_engine.test"), "read")


class TestEmitLineage:
    def test_calls_through_when_method_present(self):
        cap = CapturingStructured()
        emit_lineage(cap, "source", "customers", "csv")
        emit_lineage(cap, "transform", "mask_pii", "mask")
        emit_lineage(cap, "output", "out.csv", "csv")
        assert cap.lineages == [
            ("source", "customers", "csv"),
            ("transform", "mask_pii", "mask"),
            ("output", "out.csv", "csv"),
        ]

    def test_noop_when_method_missing(self):
        emit_lineage(NarrativeOnly(), "source", "x", "csv")

    def test_noop_when_logger_none(self):
        emit_lineage(None, "source", "x", "csv")

    def test_noop_when_method_raises(self):
        emit_lineage(Exploder(), "source", "x", "csv")


class TestEmitFidelity:
    def test_calls_through_when_method_present(self):
        cap = CapturingStructured()
        emit_fidelity(cap, "ks_test", 0.91)
        emit_fidelity(cap, "cardinality", 0.78)
        assert cap.fidelities == [("ks_test", 0.91), ("cardinality", 0.78)]

    def test_noop_when_method_missing(self):
        emit_fidelity(NarrativeOnly(), "ks_test", 0.91)

    def test_noop_when_logger_none(self):
        emit_fidelity(None, "ks_test", 0.91)

    def test_noop_when_method_raises(self):
        emit_fidelity(Exploder(), "ks_test", 0.91)


class TestEmitQuarantine:
    def test_calls_through_when_method_present(self):
        cap = CapturingStructured()
        emit_quarantine(cap, "mask_pii", "email regex failed", 162)
        assert cap.quarantines == [("mask_pii", "email regex failed", 162)]

    def test_noop_when_method_missing(self):
        emit_quarantine(NarrativeOnly(), "mask_pii", "reason", 1)

    def test_noop_when_logger_none(self):
        emit_quarantine(None, "mask_pii", "reason", 1)

    def test_noop_when_method_raises(self):
        emit_quarantine(Exploder(), "mask_pii", "reason", 1)


class TestEmitThroughputSample:
    def test_calls_through_when_method_present(self):
        cap = CapturingStructured()
        emit_throughput_sample(cap, 12500.0)
        emit_throughput_sample(cap, 13100.5)
        assert cap.throughputs == [12500.0, 13100.5]

    def test_noop_when_method_missing(self):
        emit_throughput_sample(NarrativeOnly(), 12500.0)

    def test_noop_when_logger_none(self):
        emit_throughput_sample(None, 12500.0)

    def test_noop_when_method_raises(self):
        emit_throughput_sample(Exploder(), 12500.0)


class TestStructuredEventsProtocol:
    """Static-typing surface check — the Protocol is not runtime_checkable
    by design (see context.py docstring), so a positive isinstance test
    isn't available. We assert structurally instead: CapturingStructured
    can be assigned to a name typed as ``StructuredEvents`` without
    runtime error, and every advertised method is present."""

    def test_full_implementation_exposes_every_method(self):
        cap: StructuredEvents = CapturingStructured()  # noqa: F841 — type-check intent
        for name in ("step", "lineage", "fidelity", "quarantine", "throughput_sample"):
            assert callable(getattr(CapturingStructured(), name)), name

    def test_not_runtime_checkable_against_stdlib(self):
        # If StructuredEvents *were* runtime_checkable, stdlib loggers
        # would fail isinstance() — which is exactly why it isn't. This
        # test fixes that design choice in place.
        with pytest.raises(TypeError):
            # runtime_checkable Protocols allow isinstance; non-runtime
            # ones raise TypeError when used in isinstance().
            isinstance(logging.getLogger("x"), StructuredEvents)
