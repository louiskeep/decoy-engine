"""F30 E1: the engine span recorder turns progress events into closed span records.

Every record carries only names from ``SPAN_NAMES``, keys from ``ATTRIBUTE_KEYS``, strings from
closed vocabularies and non-negative numbers, so no table, column, path, cell or exception text
can reach a telemetry backend through it. A recorder never changes a run.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pytest

from decoy_engine import progress
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.reasons import Phase

MARKER = "zqmarker7731f30"
_ENGINE_VERSION = "f30-e1"
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f30" / "telemetry-vocabulary.json"


def _telemetry():
    import decoy_engine.telemetry as telemetry

    return telemetry


class _Fanout:
    def __init__(self, *sinks: object) -> None:
        self._sinks = sinks

    def on_event(self, event: progress.ProgressEvent) -> None:
        for sink in self._sinks:
            sink.on_event(event)  # type: ignore[attr-defined]


def _job(tmp_path: Path, tag: str = "t", rows: int = 4):
    table, column, source = f"customers_{tag}", f"email_{tag}", f"src_{tag}"
    df = pd.DataFrame(
        {column: [f"{MARKER}-{i}@x.com" for i in range(rows)], "zip": ["90210"] * rows}
    )
    src = tmp_path / f"{source}.csv"
    df.to_csv(src, index=False)
    cfg = PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {
                table: {"type": "file", "format": "csv", "has_header": True, "path": str(src)}
            },
            "tables": [
                {
                    "name": table,
                    "columns": [
                        {"name": column, "strategy": "hash", "namespace": "e"},
                        {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
                    ],
                }
            ],
            "targets": {
                table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{source}_o.csv")}
            },
        }
    ).model_dump()
    return table, cfg, {table: pa.Table.from_pandas(df, preserve_index=False)}


def _run(tmp_path: Path, cfg: dict, sources: dict, out: str):
    """A full-frame run, then its output published through the transactional sink."""
    result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
    sink = ParquetTransactionalSink(tmp_path / out)
    for name, table in result.outputs.items():
        sink.write(name, table)
    sink.commit()
    return result


def _csv_bytes(table: pa.Table) -> bytes:
    buf = pa.BufferOutputStream()
    pacsv.write_csv(table, buf)
    return buf.getvalue().to_pybytes()


def _assert_closed(records) -> None:
    t = _telemetry()
    assert records
    for r in records:
        assert r.name in t.SPAN_NAMES
        assert type(r.ok) is bool and 0 <= r.start_ns <= r.end_ns
        for key, value in r.attributes:
            assert key in t.ATTRIBUTE_KEYS
            if type(value) is not str:
                assert type(value) in (int, float) and math.isfinite(value) and value >= 0


def _phase(name: str, status: str, table: str | None = None, **kw) -> progress.PhaseEvent:
    return progress.PhaseEvent(name, status, table=table, **kw)  # type: ignore[arg-type]


# E-T1
def test_phase_events_become_closed_span_records(tmp_path: Path) -> None:
    t = _telemetry()
    records: list = []
    recorder = t.SpanRecorder(records.append)
    table, cfg, sources = _job(tmp_path)
    with progress.use_progress(recorder):
        _run(tmp_path, cfg, sources, "out")
    recorder.on_event(_phase("transform", "start"))
    recorder.on_event(_phase("transform", "finish", elapsed_s=0.5))
    recorder.close()
    names = [r.name for r in records]
    for want in ("decoy.phase.resolve", "decoy.phase.mask", "decoy.phase.publish"):
        assert want in names
    assert names[-1] == "decoy.phase.unknown"
    _assert_closed(records)
    assert recorder.terminal_status == "ok"
    publish = next(r for r in records if r.name == "decoy.phase.publish")
    attrs = dict(publish.attributes)
    assert attrs["decoy.phase"] == "publish" and attrs["decoy.bytes"] > 0
    unknown = dict(records[-1].attributes)
    assert unknown["decoy.phase"] == "unknown" and unknown["decoy.elapsed_s"] == 0.5


# E-T2
def test_markers_never_reach_records(tmp_path: Path) -> None:
    t = _telemetry()
    records: list = []
    seen: list[progress.ProgressEvent] = []
    recorder = t.SpanRecorder(records.append)
    table, cfg, sources = _job(tmp_path, tag=MARKER)
    with progress.use_progress(_Fanout(recorder, progress.CallbackSink(seen.append))) as ch:
        _run(tmp_path, cfg, sources, "out")
        with pytest.raises(ValueError):
            with ch.phase("mask", table=table):
                raise ValueError(MARKER)
    recorder.close()
    tables = [e.table for e in seen if isinstance(e, progress.PhaseEvent)]
    assert any(tb is not None and MARKER in tb for tb in tables)  # non-vacuity
    _assert_closed(records)
    assert any(not r.ok for r in records)
    for r in records:
        assert MARKER not in repr(r)


# E-T3
def test_hostile_error_class_name_maps_to_exception() -> None:
    t = _telemetry()
    records: list = []
    recorder = t.SpanRecorder(records.append)
    leak = type(f"Leak_{MARKER}", (Exception,), {})
    with progress.use_progress(recorder) as ch:
        with pytest.raises(leak):
            with ch.phase("mask", table="t"):
                raise leak()
    # Events forged past their constructor (a class name, a path) are re-checked here.
    for i, forged_class in enumerate((leak.__name__, f"/tmp/{MARKER}/x.csv")):
        forged = _phase("mask", "error", table=f"u{i}", elapsed_s=0.1)
        object.__setattr__(forged, "error_class", forged_class)
        recorder.on_event(_phase("mask", "start", table=f"u{i}"))
        recorder.on_event(forged)
    assert len(records) == 3
    for r in records:
        attrs = dict(r.attributes)
        assert r.ok is False and attrs["error.type"] == "Exception"
        assert MARKER not in repr(r)


# E-T3r
def test_registered_reason_exported_unregistered_dropped() -> None:
    from decoy_engine import reasons

    t = _telemetry()
    records: list = []
    recorder = t.SpanRecorder(records.append)
    known = reasons.ReasonRecord.for_reason(reasons.decoy.FILE_ACCESS_DENIED)
    unregistered = reasons.ReasonRecord.for_reason(reasons.decoy.FILE_ACCESS_DENIED)
    object.__setattr__(unregistered, "code", f"leak_{MARKER}")
    hostile_ns = reasons.ReasonRecord.for_reason(reasons.decoy.FILE_ACCESS_DENIED)
    object.__setattr__(hostile_ns, "namespace", MARKER)
    for i, reason in enumerate((known, unregistered, hostile_ns)):
        recorder.on_event(_phase("mask", "start", table=f"t{i}"))
        recorder.on_event(_phase("mask", "error", table=f"t{i}", reason=reason))
    attrs = [dict(r.attributes) for r in records]
    assert len(records) == 3 and attrs[0]["decoy.reason"] == "decoy.file_access_denied"
    assert "decoy.reason" not in attrs[1] and "decoy.reason" not in attrs[2]
    assert all(MARKER not in repr(r) for r in records)


# E-T4
def test_span_cap_bounds_records_and_counts_drops() -> None:
    t = _telemetry()
    records: list = []
    recorder = t.SpanRecorder(records.append)
    for i in range(1000):
        recorder.on_event(_phase("mask", "start", table=f"t{i}"))
        recorder.on_event(_phase("mask", "finish", table=f"t{i}", elapsed_s=0.001))
    recorder.close()
    assert len(records) == 256
    assert recorder.dropped == 744


# E-T5
def test_barrier_interleaved_emitters_pair_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.telemetry import _recorder

    t = _telemetry()
    real_ns = _recorder._ns

    def slow_ns(value: float) -> int:
        time.sleep(0.0005)  # widen the critical section so an unlocked recorder interleaves
        return real_ns(value)

    monkeypatch.setattr(_recorder, "_ns", slow_ns)
    durations = {"a": 0.25, "b": 0.75}
    for _ in range(200):
        for cap in (256, 1):
            records: list = []
            lock = threading.Lock()

            def keep(r, records=records, lock=lock) -> None:
                with lock:
                    records.append(r)

            recorder = t.SpanRecorder(keep, max_spans=cap)
            barrier = threading.Barrier(2)

            def emit(table: str, recorder=recorder, barrier=barrier) -> None:
                barrier.wait()
                recorder.on_event(_phase("mask", "start", table=table, ts=1000.0))
                barrier.wait()
                recorder.on_event(
                    _phase("mask", "finish", table=table, elapsed_s=durations[table], ts=1001.0)
                )

            threads = [threading.Thread(target=emit, args=(tb,)) for tb in durations]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            recorder.close()
            assert len(records) + recorder.dropped == 2
            assert len(records) == min(cap, 2) and recorder.unmatched == 0
            got = sorted((r.end_ns - r.start_ns) for r in records)
            if cap == 256:
                assert got == [250_000_000, 750_000_000]


# E-T6
def test_close_ends_open_phases_without_terminal() -> None:
    t = _telemetry()
    records: list = []
    recorder = t.SpanRecorder(records.append)
    with progress.use_progress(recorder) as ch:
        ch.phase_start("mask", table="t")
        assert records == []
        with ch.run_scope():  # a nested run ends first
            pass
    assert recorder.terminal_status == "ok"
    assert records == []
    recorder.close()
    assert len(records) == 1 and records[0].ok is False
    assert records[0].name == "decoy.phase.mask"
    recorder.close()
    assert len(records) == 1

    records2: list = []
    recorder2 = t.SpanRecorder(records2.append)
    with progress.use_progress(recorder2) as ch:
        started = ch.phase_start("mask", table="t")
        with pytest.raises(RuntimeError):
            with ch.run_scope():
                raise RuntimeError("nested")
        ch.phase_finish("mask", started, table="t")  # its start was closed by the nested error
    assert recorder2.terminal_status == "error"
    assert [r.ok for r in records2] == [False]
    assert recorder2.unmatched == 1
    for _ in range(10_000):
        recorder2.on_event(_phase("mask", "finish", table="t", elapsed_s=0.0))
    assert recorder2.unmatched == 10_001
    recorder2.close()
    assert len(records2) == 1
    counters = (recorder2.unmatched, recorder2.dropped, recorder2.callback_failures)
    for i in range(10_000):  # every event after close() is ignored
        recorder2.on_event(_phase("mask", "start" if i % 2 else "finish", table=f"t{i % 3}"))
        recorder2.on_event(progress.TerminalEvent("ok"))
    recorder2.close()
    assert len(records2) == 1 and recorder2.terminal_status == "error"
    assert (recorder2.unmatched, recorder2.dropped, recorder2.callback_failures) == counters


# E-T7
def test_failing_on_span_never_reaches_run(tmp_path: Path) -> None:
    t = _telemetry()

    def boom(record) -> None:
        raise RuntimeError(MARKER)

    recorder = t.SpanRecorder(boom)
    table, cfg, sources = _job(tmp_path)
    plain = _run(tmp_path, cfg, sources, "plain")
    with progress.use_progress(recorder):
        observed = _run(tmp_path, cfg, sources, "observed")
    recorder.on_event(_phase("mask", "start", table="x"))
    recorder.on_event(_phase("mask", "finish", table="x", elapsed_s=0.0))
    recorder.close()
    assert _csv_bytes(observed.outputs[table]) == _csv_bytes(plain.outputs[table])
    plain_file = (tmp_path / "plain" / f"{table}.parquet").read_bytes()
    assert (tmp_path / "observed" / f"{table}.parquet").read_bytes() == plain_file
    assert recorder.callback_failures > 0


# E-T8
def test_vocabulary_snapshot() -> None:
    t = _telemetry()
    from decoy_engine.telemetry import _vocab

    want = json.loads(_FIXTURE.read_text())
    got = {
        "schema": t.TELEMETRY_SCHEMA,
        "span_names": sorted(t.SPAN_NAMES),
        "attribute_keys": sorted(t.ATTRIBUTE_KEYS),
        "resource_keys": sorted(t.RESOURCE_KEYS),
        "string_vocabularies": {k: sorted(v) for k, v in _vocab.STRING_VOCABULARIES.items()},
        "rule_vocabularies": dict(_vocab.RULE_VOCABULARIES),
        "integer_keys": sorted(_vocab.INTEGER_KEYS),
        "number_keys": sorted(_vocab.NUMBER_KEYS),
    }
    assert got == want
    assert len(t.SPAN_NAMES) == 3 + len(Phase) == 13
    assert len(t.ATTRIBUTE_KEYS) == 12


_OK = (("decoy.phase", "mask"),)


@pytest.mark.parametrize(
    ("name", "start", "end", "attributes"),
    [
        pytest.param(f"decoy.phase.{MARKER}", 0, 1, (), id="unknown_name"),
        pytest.param("decoy.phase.mask", 0, 1, ((f"decoy.{MARKER}", "t"),), id="unknown_key"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.phase", MARKER),), id="phase_vocab"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.mode", MARKER),), id="mode_vocab"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.reason", MARKER),), id="reason_rule"),
        pytest.param("decoy.phase.mask", 0, 1, (("error.type", f"Leak_{MARKER}"),), id="error"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.engine.version", MARKER),), id="ver"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.rows_in", -1),), id="negative_int"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.elapsed_s", math.nan),), id="nan"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.elapsed_s", math.inf),), id="inf"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.rows_in", True),), id="bool_as_int"),
        pytest.param("decoy.phase.mask", 0, 1, (("decoy.rows_in", 1.0),), id="float_as_int"),
        pytest.param("decoy.phase.mask", 0, 1, _OK + _OK, id="repeated_key"),
        pytest.param("decoy.phase.mask", 2, 1, _OK, id="unordered_bounds"),
        pytest.param("decoy.phase.mask", 0, 1, list(_OK), id="non_tuple_attributes"),
    ],
)
def test_span_record_refuses_open_values(name, start, end, attributes) -> None:
    t = _telemetry()
    with pytest.raises(ValueError) as info:
        t.SpanRecord(name, start, end, True, attributes)
    assert MARKER not in str(info.value)
    assert t.SpanRecord("decoy.phase.mask", 0, 1, True, _OK).attributes == _OK
