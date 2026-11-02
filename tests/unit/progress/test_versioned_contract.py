"""F29 acceptance A6: the versioned event schema and its legacy projection.

Pins the exact key set of every event variant in both projections, that version 1 is a strict
additive superset of version 0, null versus zero, numeric bounds, opaque references and
positional-constructor compatibility. Each variant is pushed through a callback, the dictionary
projection, the legacy logger bridge and a complete versioned sink.
"""

from __future__ import annotations

import json
import math

import pyarrow as pa
import pytest

from decoy_engine import progress, reasons
from decoy_engine.errors import ValidationError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.progress import (
    CheckpointEvent,
    CheckpointInfo,
    PhaseEvent,
    PublicationInfo,
    RateSample,
    RetryEvent,
    RetryInfo,
    RouteEvent,
    TerminalEvent,
    WarningEvent,
)
from decoy_engine.reasons import Operation, Phase, ReasonRecord, RetryClass

_IDENTITY = {"event_schema_version", "registry_version", "sequence", "phase", "object_ref"}
_BYTES = {"bytes_done", "bytes_total_estimate", "byte_basis"}

V0_KEYS: dict[str, set[str]] = {
    "phase": {
        "name",
        "status",
        "table",
        "rows_in",
        "rows_out",
        "elapsed_s",
        "error_class",
        "ts",
        "kind",
    },
    "rate": {
        "table",
        "rows_done",
        "rows_per_sec",
        "elapsed_s",
        "rows_total_estimate",
        "final",
        "ts",
        "kind",
    },
    "route": {"route", "lane", "reason_code", "table", "ts", "kind"},
    "warning": {"code", "table", "column", "ts", "kind"},
    "terminal": {"status", "error_class", "elapsed_s", "sink_failures", "ts", "kind"},
    "retry": {"table", "ts", "kind"},
    "checkpoint": {"table", "ts", "kind"},
}
V1_KEYS: dict[str, set[str]] = {
    "phase": V0_KEYS["phase"]
    | _IDENTITY
    | _BYTES
    | {"reason", "retry", "checkpoint", "publication"},
    "rate": V0_KEYS["rate"] | _IDENTITY | _BYTES | {"retry"},
    "route": V0_KEYS["route"] | _IDENTITY | {"reason"},
    "warning": V0_KEYS["warning"] | _IDENTITY | {"reason"},
    "terminal": V0_KEYS["terminal"] | _IDENTITY | {"reason", "publication"},
    "retry": V0_KEYS["retry"] | _IDENTITY | {"retry", "reason"},
    "checkpoint": V0_KEYS["checkpoint"]
    | _IDENTITY
    | {"checkpoint", "bytes_done", "byte_basis", "reason"},
}

_RETRY = RetryInfo(scope="attempt", attempt=2, retries_completed=1, state="running")
_CHECKPOINT = CheckpointInfo(state="committed", reference="ckpt-7", sequence=7, rows=10, bytes=99)


def _one_of_each() -> list[progress.ProgressEvent]:
    record = ReasonRecord.for_reason(reasons.runtime.MISSING_MASK_SECRET, phase=Phase.RESOLVE)
    return [
        PhaseEvent("mask", "start", "t", 1, None, None, None, 5.0),
        PhaseEvent("mask", "error", table="t", reason=record, phase="mask", object_ref="t"),
        RateSample("t", 10, 100.0, 0.1, bytes_done=800, byte_basis="arrow_logical"),
        RouteEvent("sequential", reason_code="fk_relationship"),
        WarningEvent("top_code_generalized", table="t", column="c"),
        TerminalEvent("error", error_class="ValueError", reason=record),
        RetryEvent(retry=_RETRY),
        CheckpointEvent(checkpoint=_CHECKPOINT),
    ]


@pytest.mark.parametrize(
    "event", _one_of_each(), ids=lambda e: f"{e.kind}-{getattr(e, 'status', '')}"
)
def test_schema_and_legacy_projection(event: progress.ProgressEvent) -> None:
    v1 = progress.event_to_dict(event)
    v0 = progress.event_to_legacy_dict(event)
    assert set(v1) == V1_KEYS[event.kind]
    assert set(v0) == V0_KEYS[event.kind]
    assert set(v0) <= set(v1)  # v1 is a strict additive superset of v0
    assert all(v1[k] == v0[k] for k in v0)  # ...with the same values
    assert v1["event_schema_version"] == 1
    assert v1["registry_version"] == reasons.REGISTRY_VERSION
    assert "event_schema_version" not in v0
    json.dumps(v1)
    json.dumps(v0)


def test_every_event_variant_has_a_pinned_key_set() -> None:
    kinds = {e.kind for e in _one_of_each()}
    assert kinds == set(V1_KEYS) == set(V0_KEYS)


def test_all_variants_reach_callback_dict_legacy_logger_and_versioned_sink() -> None:
    calls: list[tuple] = []

    class Logger:
        def step(
            self, name, *, status="running", rows_in=None, rows_out=None, error_class=None, **_
        ):
            calls.append(("step", name, status, error_class))

        def throughput_sample(self, rows_per_sec):
            calls.append(("tp", round(rows_per_sec)))

        def warning(self, msg, *args):
            calls.append(("warn", msg % args))

        def info(self, *a, **k): ...
        def debug(self, *a, **k): ...
        def error(self, *a, **k): ...

    seen: list[progress.ProgressEvent] = []
    versioned: list[str] = []
    legacy = progress.StructuredEventsSink(Logger())

    class Fan:
        def on_event(self, event: progress.ProgressEvent) -> None:
            seen.append(event)
            versioned.append(json.dumps(progress.event_to_dict(event), sort_keys=True))
            legacy.on_event(event)

    with progress.use_progress(Fan()) as ch:
        for event in _one_of_each():
            ch.emit(event)
    assert len(seen) == len(versioned) == len(_one_of_each())
    # Each event reaches each consumer once; sequence is stream-local, strictly increasing from 1.
    assert [e.sequence for e in seen] == list(range(1, len(seen) + 1))
    # The lossy bridge keeps its old signatures and ignores the new variants.
    assert ("warn", "engine warning top_code_generalized (table=t column=c)") in calls
    assert not any("ckpt-7" in repr(c) or "attempt" in repr(c) for c in calls)
    # The complete record carries reason, retry and checkpoint groups.
    assert any('"retry": {"attempt": 2' in v for v in versioned)
    assert any('"reference": "ckpt-7"' in v for v in versioned)
    assert any('"code": "missing_mask_secret"' in v for v in versioned)


def test_legacy_positional_constructors_are_unchanged() -> None:
    e = PhaseEvent("mask", "finish", "t", 1, 2, 0.5, None, 9.0)
    assert (e.name, e.status, e.table, e.rows_in, e.rows_out, e.elapsed_s, e.ts) == (
        "mask",
        "finish",
        "t",
        1,
        2,
        0.5,
        9.0,
    )
    r = RateSample("t", 3, 4.0, 0.2, 5, True, 7.0)
    assert (r.rows_total_estimate, r.final, r.ts) == (5, True, 7.0)
    assert TerminalEvent("error", "ValueError", 1.0, 2, 3.0).sink_failures == 2
    assert RouteEvent("full_frame", "unified_slice", None, "t", 1.0).lane == "unified_slice"
    assert WarningEvent("top_code_generalized", "t", "c", 2.0).column == "c"


def test_null_is_not_zero() -> None:
    unknown = RateSample("t", 0, 0.0, 0.0)
    assert unknown.bytes_done is None and progress.event_to_dict(unknown)["bytes_done"] is None
    empty = pa.table({"a": pa.array([], pa.int64())})
    rec: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(rec.append), interval_s=3600) as ch:
        list(ch.count_rows("t", [empty]))
        list(ch.count_rows("u", [[1, 2]]))  # a chunk with no Arrow size: bytes stay unknown
    by_table = {e.table: e for e in rec if e.kind == "rate"}
    assert by_table["t"].bytes_done == 0 and by_table["t"].byte_basis == "arrow_logical"
    assert by_table["u"].bytes_done is None and by_table["u"].byte_basis is None


@pytest.mark.parametrize(
    "build",
    [
        lambda: PhaseEvent("x", "start", rows_in=True),
        lambda: PhaseEvent("x", "start", rows_in=-1),
        lambda: PhaseEvent("x", "start", elapsed_s=math.nan),
        lambda: PhaseEvent("x", "start", elapsed_s=math.inf),
        lambda: PhaseEvent("x", "start", elapsed_s=-0.1),
        lambda: RateSample("t", -1, 1.0, 1.0),
        lambda: RateSample("t", 1, math.nan, 1.0),
        lambda: RateSample("t", 1, 1.0, 1.0, bytes_done=5),  # a byte counter needs its basis
        lambda: RateSample("t", 1, 1.0, 1.0, bytes_done=5, byte_basis="mixed"),
        lambda: RateSample("t", 1, 1.0, 1.0, bytes_done=True, byte_basis="arrow_logical"),
        lambda: TerminalEvent("ok", sink_failures=-1),
        lambda: PhaseEvent("x", "start", sequence=-1),
        lambda: RetryInfo("attempt", 0, 0, "running"),
        lambda: RetryInfo("attempt", 1, 5, "running"),
        lambda: RetryInfo("attempt", 1, 0, "running", delay_s=1.0),
        lambda: RetryInfo("attempt", 1, 0, "scheduled", delay_s=math.nan),
        lambda: RetryInfo("fleet", 1, 0, "running"),
        lambda: CheckpointInfo("committed"),
        lambda: CheckpointInfo("committed", reference="r"),
        lambda: CheckpointInfo("unsupported", rows=3),
        lambda: CheckpointInfo("writing", reference="/tmp/ckpt"),
        lambda: PublicationInfo(storage_ref="s3://bucket/key"),
        lambda: PhaseEvent("x", "start", object_ref="a/b"),
        lambda: RetryEvent(),
        lambda: CheckpointEvent(),
    ],
)
def test_invalid_values_are_rejected_at_construction(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_zero_counters_are_valid_and_distinct_from_none() -> None:
    e = RateSample("t", 0, 0.0, 0.0, bytes_done=0, byte_basis="arrow_logical")
    assert progress.event_to_dict(e)["bytes_done"] == 0


def test_unknown_code_and_version_cannot_leak_or_become_success() -> None:
    hostile = "pwd=hunter2 postgres://u:p@h/db"
    w = WarningEvent(hostile)
    r = RouteEvent("full_frame", reason_code=hostile)
    blob = json.dumps([progress.event_to_dict(w), progress.event_to_dict(r)])
    assert "hunter2" not in blob and "postgres" not in blob
    assert w.code == "unregistered_reason" and r.reason_code == "unregistered_reason"
    forged = ReasonRecord.for_reason(reasons.runtime.MISSING_MASK_SECRET).to_payload()
    forged["schema_version"] = 99
    parsed = ReasonRecord.from_payload(forged)
    assert parsed.code == "unsupported_schema_version"
    assert parsed.retry_class is RetryClass.NEVER and parsed.outcome is reasons.Outcome.EXECUTION


def test_a_hostile_error_class_name_is_not_a_disclosure_route() -> None:
    leak = type("Leak_hunter2_" + "x" * 5, (Exception,), {})
    rec: list[progress.ProgressEvent] = []
    with (
        pytest.raises(Exception),
        progress.use_progress(progress.CallbackSink(rec.append)) as ch,
        ch.run_scope(),
    ):
        raise leak("hunter2")
    blob = json.dumps([progress.event_to_dict(e) for e in rec])
    assert "hunter2" not in blob
    assert rec[-1].error_class == "Exception"
    # Engine and builtin class names are still reported (the positive control).
    rec2: list[progress.ProgressEvent] = []
    with (
        pytest.raises(ValidationError),
        progress.use_progress(progress.CallbackSink(rec2.append)) as ch,
        ch.run_scope(),
    ):
        raise ValidationError("x")
    assert rec2[-1].error_class == "ValidationError"


def test_terminal_error_carries_the_first_failure_and_ok_carries_none() -> None:
    rec: list[progress.ProgressEvent] = []
    sink = progress.CallbackSink(rec.append)
    with pytest.raises(ExecutionError), progress.use_progress(sink) as ch, ch.run_scope():
        with ch.phase("mask", table="t"):
            raise ExecutionError(code=reasons.runtime.CYCLIC_WORK_ORDERING, message="secret-123")
    terminal = rec[-1]
    assert terminal.status == "error" and terminal.reason is not None
    assert terminal.reason.code == "cyclic_work_ordering" and terminal.reason.phase is Phase.MASK
    phase_error = next(e for e in rec if e.kind == "phase" and e.status == "error")
    assert phase_error.reason == terminal.reason  # one record, never relabelled by the outer scope
    assert "secret-123" not in json.dumps([progress.event_to_dict(e) for e in rec])
    ok: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(ok.append)) as ch, ch.run_scope():
        pass
    assert ok[-1].status == "ok" and ok[-1].reason is None


def test_publication_group_round_trips() -> None:
    info = PublicationInfo(
        Operation.COMMIT, "output", reasons.PublicationState.PARTIAL, reasons.CleanupState.FAILED
    )
    e = TerminalEvent("error", publication=info)
    assert progress.event_to_dict(e)["publication"] == {
        "operation": "commit",
        "storage_ref": "output",
        "publication_state": "partial",
        "cleanup_state": "failed",
    }
