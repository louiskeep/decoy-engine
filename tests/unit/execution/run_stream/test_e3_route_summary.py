"""RUN-STREAM E3 (AT-E3a): `route_summary` reads configured caps and applied values.

An applied value comes only from evidence a route wrote. A configured cap alone never renders
as applied (M-E3a), unknown keys are ignored, and missing blocks give `route="unknown"`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.execution import run_pipeline
from decoy_engine.execution.pipeline import RouteSummary, route_summary
from tests.unit.execution.run_stream import _support as rs
from tests.unit.execution.run_stream.test_e1c_duckdb_threads import _run as _run_ooc

_ELIGIBLE = str(reasons.route.ELIGIBLE)


def test_no_blocks_give_unknown_and_nothing_applied() -> None:
    for empty in (None, {}, {"unrelated": 1}, {"execution": "bad", "resources": []}):
        got = route_summary(empty)
        assert got.route == "unknown"
        assert (got.reason_code, got.native, got.native_threads_applied) == (None, None, None)
        assert got.duckdb_threads_applied == ()
        assert got.out_of_core_memory_limits_applied_bytes == ()


def test_summary_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        route_summary({}).route = "x"  # type: ignore[misc]


def test_flat_route_marks_native_and_out_of_core_not_used() -> None:
    metrics = {
        "execution": {"execution_mode": "full_frame", "route_reason": _ELIGIBLE},
        "future_block": {"anything": 1},
    }
    got = route_summary(metrics)
    assert got.route == "full_frame"
    assert got.reason_code == _ELIGIBLE
    assert got.reason_text == reasons.route.ELIGIBLE.spec.summary
    assert got.native is None
    assert got.native_threads_applied is None
    assert got.duckdb_threads_applied == ()
    assert got.out_of_core_memory_cap_bytes is None
    assert got.out_of_core_memory_limits_applied_bytes == ()


def test_a_native_cap_without_clamp_evidence_is_not_applied() -> None:
    # M-E3a: threads were asked for, but no kernel answered, so nothing is "applied".
    got = route_summary({"auto_chunk": {"lane": "dispatcher", "native_threads": 6}})
    assert got.route == "auto_chunk"
    assert got.thread_cap == 6
    assert got.native_threads_applied is None


def test_native_evidence_reports_the_applied_clamp() -> None:
    metrics = {
        "auto_chunk": {
            "lane": "dispatcher",
            "lane_reason": None,
            "native_threads": 6,
            "native_thread_clamp": {"requested": 6, "applied": 4},
            "input": {"mode": "lazy", "reason": _ELIGIBLE},
            "output": {"mode": "streamed", "reason": _ELIGIBLE},
        },
        "chunked_route": {"native_admitted": True, "reroute_reason": None},
    }
    got = route_summary(metrics, thread_cap_source="auto")
    assert (got.thread_cap, got.thread_cap_source, got.native_threads_applied) == (6, "auto", 4)
    assert (got.input_mode, got.output_mode, got.native) == ("lazy", "streamed", "admitted")


def test_native_status_distinguishes_missing_companion_from_a_decline() -> None:
    def status(code: str) -> str | None:
        return route_summary(
            {"chunked_route": {"native_admitted": False, "reroute_reason": code}}
        ).native

    assert status(str(reasons.physical.NATIVE_COMPANION_UNAVAILABLE)) == "not_installed"
    assert status("some_decline_code") == "declined: some_decline_code"


def test_out_of_core_reports_distinct_sorted_applied_pairs() -> None:
    metrics = {
        "execution": {"execution_mode": "out_of_core", "route_reason": _ELIGIBLE},
        "resources": {
            "out_of_core": {
                "thread_cap": 4,
                "memory_cap_bytes": 8 << 30,
                "applied": [
                    {"memory_limit_bytes": 4 << 30, "threads": 2},
                    {"memory_limit_bytes": 2 << 30, "threads": 1},
                    {"memory_limit_bytes": 4 << 30, "threads": 2},
                    {"memory_limit_bytes": None, "threads": None},
                ],
            }
        },
    }
    got = route_summary(metrics, thread_cap_source="flag", out_of_core_memory_cap_source="env")
    assert got.route == "out_of_core"
    assert got.thread_cap == 4 and got.thread_cap_source == "flag"
    assert got.out_of_core_memory_cap_bytes == 8 << 30
    assert got.out_of_core_memory_cap_source == "env"
    assert got.duckdb_threads_applied == (1, 2)
    assert got.out_of_core_memory_limits_applied_bytes == (2 << 30, 4 << 30)


def test_a_source_label_without_a_cap_is_dropped() -> None:
    got = route_summary({}, thread_cap_source="auto", out_of_core_memory_cap_source="auto")
    assert (got.thread_cap_source, got.out_of_core_memory_cap_source) == (None, None)


def test_real_out_of_core_run_round_trips(tmp_path: Path) -> None:
    result = _run_ooc(tmp_path, out_of_core_threads=2)
    got = route_summary(result.quality_metrics)
    block = result.quality_metrics["resources"]["out_of_core"]
    assert got.route == "out_of_core"
    assert got.thread_cap == 2
    assert got.out_of_core_memory_cap_bytes == block["memory_cap_bytes"]
    assert got.duckdb_threads_applied == tuple(sorted({p["threads"] for p in block["applied"]}))
    assert got.native is None


def test_real_lane_run_reports_modes_and_never_a_value(tmp_path: Path) -> None:
    cfg, source = rs.single_table_job(tmp_path, rs.two_column_table(200))
    sink, _ = rs.real_sink(tmp_path)
    result: Any = run_pipeline(
        cfg, sources={"t": source}, sink=sink, engine_version=rs.ENGINE_VERSION
    )
    got: RouteSummary = route_summary(result.quality_metrics)
    assert got.route in {"auto_chunk", "full_frame"}
    assert got.out_of_core_memory_limits_applied_bytes == ()
    assert got.duckdb_threads_applied == ()
    assert rs.CANARY not in repr(got)
