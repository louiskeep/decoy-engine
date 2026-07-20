"""SPRINT-1: phase-aware DuckDB memory caps (Part A) and the hybrid
capacity preflight (Part B), `out_of_core/_memory_estimate.py`.

`memory_limit_for` / `resolve_phase_memory_limits` must give every DuckDB
connection a cap sized to its own phase-local liveness, with the invariant
the gate checks: the SUM of every live instance's cap never exceeds
`budget_bytes`. `enforce_ooc_memory_preflight` must WARN (never block) in
the warn band, HARD-FAIL with a typed, coded error beyond the safe bound
BEFORE any DuckDB work, and fail OPEN only when the ceiling itself is
undetectable.
"""

from __future__ import annotations

import logging

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _memory_estimate as mem_mod
from decoy_engine.execution.out_of_core._memory_estimate import (
    enforce_ooc_memory_preflight,
    memory_limit_for,
    predict_ooc_build_floor_bytes,
    resolve_phase_memory_limits,
)

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


class TestMemoryLimitFor:
    def test_single_instance_gets_the_undivided_budget(self) -> None:
        assert memory_limit_for(2 * _GIB, 1) == "2048MB"

    def test_n_instance_phase_gets_budget_over_n(self) -> None:
        assert memory_limit_for(2 * _GIB, 4) == "512MB"

    @pytest.mark.parametrize("fan_in", range(1, 65))
    def test_sum_of_live_caps_never_exceeds_budget_across_fan_in(self, fan_in: int) -> None:
        budget = 8 * _GIB
        per_instance_mib = int(memory_limit_for(budget, fan_in).removesuffix("MB"))
        assert per_instance_mib * fan_in * _MIB <= budget

    def test_one_mb_min_string_guard_beyond_64_way_split_on_a_near_floor_budget(self) -> None:
        # 64 MiB / 100 rounds below 1 MiB: DuckDB rejects a literal "0MB"
        # limit outright, so the string floors at "1MB" (the sum can then
        # nominally exceed budget_bytes; spilling, not the cap, carries the
        # run in that regime -- documented in the function's own docstring).
        assert memory_limit_for(64 * _MIB, 100) == "1MB"

    def test_rejects_live_instances_below_one(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            memory_limit_for(1 * _GIB, 0)
        assert excinfo.value.code == "out_of_core_concurrency_invalid"


class TestResolvePhaseMemoryLimits:
    def test_falls_back_to_flat_memory_limit_when_budget_bytes_is_none(self) -> None:
        joiner, sink_build, resident_build = resolve_phase_memory_limits(
            budget_bytes=None, memory_limit="256MB", incoming_edges=3
        )
        assert (joiner, sink_build, resident_build) == ("256MB", "256MB", "256MB")

    def test_sink_build_is_always_undivided_regardless_of_incoming_fan_in(self) -> None:
        for incoming in (0, 1, 5):
            _joiner, sink_build, _resident = resolve_phase_memory_limits(
                budget_bytes=4 * _GIB, memory_limit=None, incoming_edges=incoming
            )
            assert sink_build == "4096MB"

    def test_resident_build_divides_by_incoming_plus_one(self) -> None:
        _joiner, _sink, resident_build = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit=None, incoming_edges=3
        )
        assert resident_build == f"{4 * 1024 // 4}MB"

    def test_joiner_divides_by_incoming_edge_count(self) -> None:
        joiner, _sink, _resident = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit=None, incoming_edges=4
        )
        assert joiner == f"{4 * 1024 // 4}MB"

    def test_zero_incoming_edges_falls_back_for_the_unused_joiner_value(self) -> None:
        # No incoming edges: no joiner ever opens, so this value is unused by
        # its caller -- must not raise (memory_limit_for(budget, 0) would).
        joiner, sink_build, resident_build = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit="9MB", incoming_edges=0
        )
        assert joiner == "9MB"
        assert sink_build == "4096MB"
        assert resident_build == "4096MB"  # incoming(0) + 1 == 1, undivided


class TestPredictOocBuildFloorBytes:
    def test_never_under_predicts_at_the_measured_anchor_tiers(self) -> None:
        # SPRINT-1 B4 devbox acceptance probe (build_probe.py against a real
        # 20M-row parent.parquet, pinned DuckDB memory_limit, RLIMIT_DATA
        # safety-capped, real duckdb_memory() sampling): the relation-build
        # dedup FAILS at 256/512/1024/2048 MiB (bad allocation each time) and
        # COMPLETES at the pre-established 3276 MiB undivided-budget
        # measurement. The predicted floor at 20M rows must sit strictly
        # above the highest empirical FAILURE tier (2048 MiB) -- an
        # under-prediction is the one outcome the hybrid preflight exists to
        # prevent -- and at or below the known-good completion tier (3276
        # MiB) is not required, since over-prediction is the safe direction.
        floor_20m = predict_ooc_build_floor_bytes(20_000_000)
        assert floor_20m > 2048 * _MIB
        assert floor_20m <= 3276 * _MIB

    def test_monotonic_in_row_count(self) -> None:
        assert predict_ooc_build_floor_bytes(0) < predict_ooc_build_floor_bytes(1_000_000)
        assert predict_ooc_build_floor_bytes(1_000_000) < predict_ooc_build_floor_bytes(20_000_000)

    def test_never_predicts_a_near_zero_floor(self) -> None:
        # Even a zero/near-zero parent table still opens one real DuckDB
        # instance with real baseline overhead: the floor never predicts near
        # zero and so never falsely clears an artificially tight ceiling.
        assert predict_ooc_build_floor_bytes(0) >= 64 * _MIB

    def test_negative_row_count_does_not_shrink_the_floor(self) -> None:
        assert predict_ooc_build_floor_bytes(-1) == predict_ooc_build_floor_bytes(0)


class TestEnforceOocMemoryPreflight:
    def test_comfortably_clear_of_the_warn_band_does_not_warn(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=mem_mod.__name__):
            result = enforce_ooc_memory_preflight(1_000, ceiling_bytes=64 * _GIB)
        assert result.ok is True
        assert result.warned is False
        assert not caplog.records

    def test_warn_band_emits_a_structured_warning_and_never_blocks(self, caplog) -> None:
        # floor(100_000 rows) ~= 128 MiB base + ~13.8 MiB ~= 141.9 MiB. A
        # 200 MiB ceiling puts that floor in [0.6, 0.85) * ceiling: warn, not
        # fail (120 MiB <= 141.9 MiB < 170 MiB).
        ceiling = 200 * _MIB
        floor = predict_ooc_build_floor_bytes(100_000)
        assert 0.6 * ceiling <= floor < 0.85 * ceiling
        with caplog.at_level(logging.WARNING, logger=mem_mod.__name__):
            result = enforce_ooc_memory_preflight(100_000, ceiling_bytes=ceiling)
        assert result.ok is True
        assert result.warned is True
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "out-of-core memory advisory" in message
        assert "resident floor" in message
        assert "recommend" in message

    def test_hard_fail_raises_typed_error_before_any_run(self) -> None:
        # floor(20_000_000) is several hundred MiB (see the calibration test
        # above); a 64 MiB ceiling puts it far past the safe bound.
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(20_000_000, ceiling_bytes=64 * _MIB)
        assert excinfo.value.code == "out_of_core_insufficient_memory"
        message = excinfo.value.message
        assert "resident floor" in message
        assert "safe bound" in message
        assert "GB of memory" in message

    def test_hard_fail_boundary_is_the_safe_fraction_not_the_warn_fraction(self) -> None:
        # Pin the exact boundary: floor == safe_fraction * ceiling must fail
        # (>=), floor just below it must not.
        ceiling = 1 * _GIB
        floor = predict_ooc_build_floor_bytes(0)  # the fixed baseline alone
        safe_bound = int(mem_mod._OOC_MEM_SAFE_FRACTION * ceiling)
        assert floor < safe_bound  # sanity: baseline floor is small
        # Construct a ceiling where the baseline floor sits exactly at the
        # safe bound.
        boundary_ceiling = int(floor / mem_mod._OOC_MEM_SAFE_FRACTION)
        with pytest.raises(ExecutionError):
            enforce_ooc_memory_preflight(0, ceiling_bytes=boundary_ceiling)

    def test_fails_open_when_ceiling_is_undetectable(self, monkeypatch) -> None:
        def broken_detect() -> int:
            raise ExecutionError(
                code="out_of_core_memory_detection_failed", message="no ram signal"
            )

        monkeypatch.setattr(mem_mod, "detect_effective_memory_bytes", broken_detect)
        result = enforce_ooc_memory_preflight(50_000_000)  # would hard-fail if detectable
        assert result.ok is True
        assert result.detectable is False
        assert result.ceiling_bytes is None

    def test_under_safe_bound_job_runs_clean(self) -> None:
        result = enforce_ooc_memory_preflight(100, ceiling_bytes=32 * _GIB)
        assert result.ok is True
        assert result.warned is False
        assert result.floor_bytes < result.safe_bound_bytes
