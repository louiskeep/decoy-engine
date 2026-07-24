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
from unittest.mock import patch

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _budget as budget_mod
from decoy_engine.execution.out_of_core import _capacity_eval as capacity_eval_mod
from decoy_engine.execution.out_of_core import _memory_estimate as mem_mod
from decoy_engine.execution.out_of_core._budget import resolve_ooc_memory_limit
from decoy_engine.execution.out_of_core._capacity_eval import enforce_ooc_memory_preflight
from decoy_engine.execution.out_of_core._memory_estimate import (
    actual_duckdb_cap_bytes,
    declared_minimum_ceiling_bytes,
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

    def test_sub_1mib_split_raises_fanin_exceeds_budget_instead_of_flooring(self) -> None:
        # 64 MiB / 100 rounds below 1 binary MiB, AND 100 * 1e6 (100_000_000)
        # exceeds the 67_108_864-byte budget -- a genuine over-commit even at
        # DuckDB's decimal "1MB" floor, so this still raises fail-closed.
        with pytest.raises(ExecutionError) as excinfo:
            memory_limit_for(64 * _MIB, 100)
        assert excinfo.value.code == "out_of_core_fanin_exceeds_budget"

    def test_fan_in_67_on_64mib_budget_admits_at_one_mb(self) -> None:
        # ROUND-3 Fix C SUB-FIX 1 (Codex round-2 over-correction fix): 67 is
        # the exact safe boundary -- 67 * 1_000_000 = 67_000_000 B <=
        # 67_108_864 B (64 MiB) budget, so DuckDB's decimal "1MB" floor for
        # every one of the 67 co-live instances still sums within budget.
        # Round-2's binary-only guard wrongly refused this (64 MiB // 67 =
        # 1_001_624 B, below 1 binary MiB), a FALSE REFUSAL this round fixes.
        assert memory_limit_for(64 * _MIB, 67) == "1MB"
        assert 64 * _MIB >= 67 * 1_000_000

    def test_fan_in_68_on_64mib_budget_raises_fanin_exceeds_budget(self) -> None:
        # Codex's reproduced over-commit case: 68 instances x a floored "1MB"
        # cap each = 6.8e7 B > the 67_108_864 B (64 MiB) budget -- a genuine
        # over-commit (68 * 1e6 = 68_000_000 > 67_108_864), so this must
        # raise rather than silently emit an over-committing "1MB" string.
        with pytest.raises(ExecutionError) as excinfo:
            memory_limit_for(64 * _MIB, 68)
        assert excinfo.value.code == "out_of_core_fanin_exceeds_budget"

    def test_actual_duckdb_cap_bytes_raises_the_same_guard(self) -> None:
        # `actual_duckdb_cap_bytes` shares `_per_instance_mib`'s fail-closed
        # guard, so it never returns a phantom cap for an un-sizeable split.
        with pytest.raises(ExecutionError) as excinfo:
            actual_duckdb_cap_bytes(64 * _MIB, 68)
        assert excinfo.value.code == "out_of_core_fanin_exceeds_budget"

    def test_rejects_live_instances_below_one(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            memory_limit_for(1 * _GIB, 0)
        assert excinfo.value.code == "out_of_core_concurrency_invalid"


class TestActualDuckdbCapBytes:
    """FIX B: the ACTUAL decimal bytes DuckDB enforces -- `memory_limit_for`'s
    emitted "NNMB" string re-read as base-10 megabytes, strictly less than
    the binary `budget_bytes // live_instances` a naive caller might compare
    a floor against."""

    def test_matches_memory_limit_for_reread_as_decimal_mb(self) -> None:
        limit = memory_limit_for(2 * _GIB, 4)  # "512MB"
        assert limit == "512MB"
        assert actual_duckdb_cap_bytes(2 * _GIB, 4) == 512 * 1_000_000

    def test_strictly_below_the_binary_division(self) -> None:
        # 2 GiB // 4 = 512 MiB binary = 536_870_912 B; the actual decimal cap
        # (512 * 1_000_000 = 512_000_000 B) is smaller -- the exact gap FIX B
        # closes (a floor between the two was wrongly admitted pre-fix).
        budget = 2 * _GIB
        binary_cap = budget // 4
        assert actual_duckdb_cap_bytes(budget, 4) < binary_cap

    def test_rejects_live_instances_below_one(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            actual_duckdb_cap_bytes(1 * _GIB, 0)
        assert excinfo.value.code == "out_of_core_concurrency_invalid"

    def test_fan_in_67_sum_of_caps_invariant_holds_at_64mib(self) -> None:
        # SUB-FIX 1's pinned acceptance case, checked against the invariant
        # this whole module states as TRUE: live * actual_cap <= budget_bytes.
        cap = actual_duckdb_cap_bytes(64 * _MIB, 67)
        assert cap == 1_000_000
        assert 67 * cap <= 64 * _MIB


class TestResolvePhaseMemoryLimits:
    def test_falls_back_to_flat_memory_limit_when_budget_bytes_is_none(self) -> None:
        limits = resolve_phase_memory_limits(
            budget_bytes=None, memory_limit="256MB", incoming_edges=3, sink=True
        )
        assert limits == ("256MB", "256MB", "256MB", "256MB")
        limits = resolve_phase_memory_limits(
            budget_bytes=None, memory_limit="256MB", incoming_edges=3, sink=False
        )
        assert limits == ("256MB", "256MB", "256MB", "256MB")

    def test_sink_build_is_always_undivided_regardless_of_incoming_fan_in(self) -> None:
        for incoming in (0, 1, 5):
            _sj, _rj, sink_build, _resident = resolve_phase_memory_limits(
                budget_bytes=4 * _GIB, memory_limit=None, incoming_edges=incoming, sink=True
            )
            assert sink_build == "4096MB"

    def test_resident_build_divides_by_incoming_plus_one(self) -> None:
        _sj, _rj, _sink, resident_build = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit=None, incoming_edges=3, sink=False
        )
        assert resident_build == f"{4 * 1024 // 4}MB"

    def test_sink_joiner_divides_by_incoming_edge_count(self) -> None:
        sink_joiner, _rj, _sink, _resident = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit=None, incoming_edges=4, sink=True
        )
        assert sink_joiner == f"{4 * 1024 // 4}MB"

    def test_resident_joiner_divides_by_incoming_edge_count_plus_one(self) -> None:
        # HIGH remediation: a resident-path joiner stays live into the build
        # phase, so it must open at the SAME cap the build gets (budget //
        # (incoming + 1)), not the sink path's narrower budget // incoming --
        # otherwise the co-live sum during the build exceeds budget_bytes.
        _sj, resident_joiner, _sink, resident_build = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit=None, incoming_edges=3, sink=False
        )
        assert resident_joiner == resident_build == f"{4 * 1024 // 4}MB"

    def test_zero_incoming_edges_falls_back_for_the_unused_joiner_value(self) -> None:
        # No incoming edges: no joiner ever opens, so the active path's own
        # joiner value is unused -- must not raise (memory_limit_for(budget,
        # 0) would).
        sink_joiner, resident_joiner, sink_build, resident_build = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit="9MB", incoming_edges=0, sink=True
        )
        assert sink_joiner == "9MB"
        assert resident_joiner == "9MB"  # unopened path: flat fallback
        assert sink_build == "4096MB"
        assert resident_build == "9MB"  # unopened path: flat fallback

        sink_joiner, resident_joiner, sink_build, resident_build = resolve_phase_memory_limits(
            budget_bytes=4 * _GIB, memory_limit="9MB", incoming_edges=0, sink=False
        )
        assert sink_joiner == "9MB"  # unopened path: flat fallback
        assert resident_joiner == "9MB"
        assert sink_build == "9MB"  # unopened path: flat fallback
        assert resident_build == "4096MB"  # incoming(0) + 1 == 1, undivided

    def test_sink_true_fanin_67_does_not_raise_but_resident_does(self) -> None:
        # SUB-FIX 2's pinned acceptance case: a SINK job with fan-in 67 on a
        # 64 MiB budget must NOT raise (the unused resident pair is the flat
        # fallback, never sized), while the SAME fan-in on the RESIDENT path
        # (live = incoming + 1 = 68) DOES raise -- 68 * 1e6 > 64 MiB budget.
        limits = resolve_phase_memory_limits(
            budget_bytes=64 * _MIB, memory_limit=None, incoming_edges=67, sink=True
        )
        assert limits[0] == "1MB"  # sink_joiner: live = 67, floors to the safe "1MB" admit
        assert limits[2] == "64MB"  # sink_build: live = 1, the whole undivided budget

        with pytest.raises(ExecutionError) as excinfo:
            resolve_phase_memory_limits(
                budget_bytes=64 * _MIB, memory_limit=None, incoming_edges=67, sink=False
            )
        assert excinfo.value.code == "out_of_core_fanin_exceeds_budget"

    def test_unopened_path_never_raises_even_when_it_would_have(self) -> None:
        # The exact false-refusal SUB-FIX 2 closes: a fan-in of 67 makes the
        # RESIDENT pair un-sizeable at this budget (live = 68), but a SINK
        # job never computes that pair, so it must not raise at all.
        resolve_phase_memory_limits(
            budget_bytes=64 * _MIB, memory_limit=None, incoming_edges=67, sink=True
        )


class TestPredictOocBuildFloorBytes:
    # FIX 3 recalibration: a conservative envelope over the clean fixed-rlimit
    # devbox sweep AND the real-route cloud measurement (see the
    # `_BUILD_FLOOR_BYTES_PER_ROW` docstring for both datasets).

    # Clean fixed-rlimit (RLIMIT_DATA 8000 MiB) devbox floor brackets: the
    # (highest FAIL, lowest PASS] memory_limit MiB per row count. The SAFETY
    # property is `floor > highest_FAIL`: it guarantees that admitting a job
    # (cap >= floor) hands it a cap above every memory_limit KNOWN to OOM at
    # that row count. (Predicting >= lowest_PASS is NOT required and is in fact
    # impossible at the small end without breaking the byte-estimate routing
    # knob -- see `test_tiny_fixture_floor_fits_the_routing_knob_cap`.)
    # (highest FAIL, lowest PASS) MiB; only the FAIL edge is load-bearing for
    # the safety assertion below. 40M's pass edge was not cleanly narrowed (a
    # 768 MiB attempt died on a full spill disk, not memory), but its FAIL at
    # 512 is confirmed, which is all the safety check uses.
    _CLEAN_DEVBOX_BRACKET_MIB = {
        100_000: (32, 48),
        1_000_000: (96, 128),
        5_000_000: (64, 80),
        10_000_000: (128, 192),
        20_000_000: (256, 384),
        40_000_000: (512, None),
    }

    def test_floor_is_strictly_above_every_clean_devbox_fail_tier(self) -> None:
        for rows, (highest_fail_mib, _lowest_pass) in self._CLEAN_DEVBOX_BRACKET_MIB.items():
            floor = predict_ooc_build_floor_bytes(rows)
            assert floor > highest_fail_mib * _MIB, rows

    def test_never_under_predicts_the_real_route_cloud_floor(self) -> None:
        # The motivating failure: the real-route 33.3M-row BUILD OOMed at
        # memory_limit ~1638 MiB and completed at 2457 MiB. The model must
        # predict at or above the 2457 MiB completion level (never under the
        # observed floor), and over-predicts it with margin here.
        floor_33m = predict_ooc_build_floor_bytes(33_300_000)
        assert floor_33m >= 2457 * _MIB

    def test_tiny_fixture_floor_fits_the_routing_knob_cap(self) -> None:
        # The out-of-core byte-estimate routing knob is a 64 MiB budget; on a
        # resident fan-in-1 build that resolves to a 32 MiB cap. A genuinely
        # tiny fixture (the parity suite uses 40 rows) MUST fit it, or every
        # `tests/parity/test_out_of_core_*_routing.py` case regresses -- this
        # is the constraint that pins the base SMALL (FIX 5).
        assert predict_ooc_build_floor_bytes(40) <= 32 * _MIB

    def test_monotonic_in_row_count(self) -> None:
        assert predict_ooc_build_floor_bytes(0) < predict_ooc_build_floor_bytes(1_000_000)
        assert predict_ooc_build_floor_bytes(1_000_000) < predict_ooc_build_floor_bytes(20_000_000)

    def test_never_predicts_a_near_zero_floor(self) -> None:
        # Even a zero/near-zero parent table still opens one real DuckDB
        # instance with real baseline overhead: the floor never predicts near
        # zero and so never falsely clears an artificially tight cap.
        assert predict_ooc_build_floor_bytes(0) >= 16 * _MIB

    def test_negative_row_count_does_not_shrink_the_floor(self) -> None:
        assert predict_ooc_build_floor_bytes(-1) == predict_ooc_build_floor_bytes(0)


class TestEnforceOocMemoryPreflight:
    """FIX 1 remediation: gates against `cap(t)` (`budget_bytes` undivided on
    the sink path, `budget_bytes // (incoming_edges(t) + 1)` on the resident
    path) -- the EXACT cap `resolve_phase_memory_limits` hands the real
    connection -- never a fraction of the raw detected ceiling."""

    def test_comfortably_clear_of_the_warn_band_does_not_warn(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=capacity_eval_mod.__name__):
            result = enforce_ooc_memory_preflight(
                {"parent": 1_000}, budget_bytes=64 * _GIB, sink=True, incoming_edge_counts={}
            )
        assert result.ok is True
        assert result.warned is False
        assert not caplog.records

    def test_warn_band_emits_a_structured_warning_and_never_blocks(self, caplog) -> None:
        # A cap sized so the floor lands in [0.6, 1.0) of it: warn, not fail.
        floor = predict_ooc_build_floor_bytes(100_000)
        cap = int(floor / 0.7)  # comfortably inside [0.6, 1.0) * cap
        assert 0.6 * cap <= floor < cap
        with caplog.at_level(logging.WARNING, logger=capacity_eval_mod.__name__):
            result = enforce_ooc_memory_preflight(
                {"parent": 100_000}, budget_bytes=cap, sink=True, incoming_edge_counts={}
            )
        assert result.ok is True
        assert result.warned is True
        assert result.binding_table == "parent"
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "out-of-core memory advisory" in message
        assert "resident floor" in message
        assert "recommend" in message

    def test_hard_fail_raises_typed_error_before_any_run(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"parent": 20_000_000},
                budget_bytes=64 * _MIB,
                sink=True,
                incoming_edge_counts={},
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"
        message = excinfo.value.message
        assert "resident floor" in message
        assert "actual build cap" in message
        assert "GB of memory" in message

    def test_hard_fail_boundary_is_the_cap_itself_not_a_fraction_of_it(self, monkeypatch) -> None:
        # Pin the exact boundary against the ACTUAL DuckDB decimal cap
        # (round-2 Fix B): the invariant is `floor(t) <= cap(t)`, so
        # floor == cap FITS (admitted) and floor == cap + 1 FAILS. There is
        # no additional safety fraction baked into the hard-fail bound -- it
        # is the full actual cap, not a fraction of it (that fractional SAFE
        # bound is what round 1's remediation removed). `cap` is quantized to
        # whole decimal megabytes (`actual_duckdb_cap_bytes`), which does not
        # land on every value `predict_ooc_build_floor_bytes`'s base+slope
        # model can produce, so the floor is monkeypatched to the EXACT cap
        # value to pin this boundary independent of that model's own grid.
        budget = 100 * _MIB
        cap = actual_duckdb_cap_bytes(budget, 1)
        monkeypatch.setattr(mem_mod, "predict_ooc_build_floor_bytes", lambda rows: cap)
        result = enforce_ooc_memory_preflight(
            {"parent": 0}, budget_bytes=budget, sink=True, incoming_edge_counts={}
        )
        assert result.ok is True  # floor == cap fits exactly

        monkeypatch.setattr(mem_mod, "predict_ooc_build_floor_bytes", lambda rows: cap + 1)
        with pytest.raises(ExecutionError):
            enforce_ooc_memory_preflight(
                {"parent": 0}, budget_bytes=budget, sink=True, incoming_edge_counts={}
            )

    def test_fails_open_when_budget_bytes_is_none(self) -> None:
        # Mirrors `resolve_ooc_memory_limit`'s own fall-through (host-RAM
        # detection failed, no explicit budget given): Part A's phase-aware
        # caps fall back to the flat memory_limit in this same case, so
        # there is no real per-table cap left to gate a floor against.
        result = enforce_ooc_memory_preflight(
            {"parent": 50_000_000}, budget_bytes=None, sink=True, incoming_edge_counts={}
        )
        assert result.ok is True
        assert result.detectable is False
        assert result.cap_bytes is None

    def test_under_cap_job_runs_clean(self) -> None:
        result = enforce_ooc_memory_preflight(
            {"parent": 100}, budget_bytes=32 * _GIB, sink=True, incoming_edge_counts={}
        )
        assert result.ok is True
        assert result.warned is False

    def test_resident_path_uses_incoming_plus_one_as_the_cap_divisor(self) -> None:
        # A resident-path table with 3 incoming edges: cap = budget // 4. A
        # floor that fits budget // 1 but not budget // 4 must still fail --
        # this is the exact BLOCKER shape (a preflight that checked the
        # undivided budget instead of the real resident cap).
        budget = 4 * _GIB
        cap = budget // 4
        floor = predict_ooc_build_floor_bytes(0)
        assert floor <= budget  # would pass a (buggy) undivided check
        # Find a row count whose floor sits just above the real resident cap.
        rows = 1
        while predict_ooc_build_floor_bytes(rows) <= cap:
            rows *= 2
        with pytest.raises(ExecutionError):
            enforce_ooc_memory_preflight(
                {"hub": rows},
                budget_bytes=budget,
                sink=False,
                incoming_edge_counts={"hub": 3},
            )

    def test_no_parent_tables_is_always_clean(self) -> None:
        result = enforce_ooc_memory_preflight(
            {}, budget_bytes=64 * _MIB, sink=True, incoming_edge_counts={}
        )
        assert result.ok is True
        assert result.warned is False
        assert result.binding_table is None

    def test_pure_joiner_leaf_fanin_raises_up_front_before_any_runner_dispatch(self) -> None:
        # SUB-FIX 3: 68 tiny parent tables (no incoming edges of their own,
        # so their build-phase check is trivially safe) all feed ONE leaf
        # table with no outgoing edge of its own -- "leaf" is therefore NOT
        # in `parent_table_rows` (no floor(t) to price), only in
        # `incoming_edge_counts`. Before this round the leaf's own fan-in
        # was only caught when its joiner connection actually opened
        # mid-run (Codex's `runner_called=True` reproduction); now the
        # preflight itself raises `out_of_core_fanin_exceeds_budget` here,
        # so a caller never even reaches `run_fk_out_of_core`.
        parent_table_rows = {f"parent_{i}": 1 for i in range(68)}
        incoming_edge_counts = {"leaf": 68}
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                parent_table_rows,
                budget_bytes=64 * _MIB,
                sink=True,
                incoming_edge_counts=incoming_edge_counts,
            )
        assert excinfo.value.code == "out_of_core_fanin_exceeds_budget"

    def test_binding_table_is_the_argmax_of_floor_minus_cap(self) -> None:
        # Two failing tables at different budgets: the reported binding
        # table must be the WORSE one (larger floor - cap margin), not
        # merely the first/last one iterated.
        budget = 4 * _GIB
        small_rows = 1_000
        large_rows = 10_000_000_000
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"small": small_rows, "huge": large_rows},
                budget_bytes=budget,
                sink=True,
                incoming_edge_counts={},
            )
        assert "'huge'" in excinfo.value.message


class TestDeclaredMinimumCeiling:
    """FIX B (round 2): the declared minimum must be TRUTHFUL against the
    ACTUAL DuckDB decimal cap, not a binary approximation of it. Fed back
    through `resolve_ooc_memory_limit` then `actual_duckdb_cap_bytes`, the
    recommended ceiling must actually yield a build cap >= the floor that
    triggered the fail. It inverts `resolve_ooc_memory_limit`'s OWN reserve
    model plus `_per_instance_mib`'s decimal-MiB sizing, so this round-trips
    end to end -- the EMITTED cap, not the raw budget_bytes."""

    def _resolved_cap(self, ceiling_bytes: int, *, sink: bool, incoming: int) -> int:
        # Model the auto-detect resolution path: detected ceiling -> reserve
        # subtracted -> undivided budget -> phase-aware ACTUAL decimal cap
        # (round 2: was a binary `budget // live` approximation pre-fix).
        with patch.object(budget_mod, "detect_effective_memory_bytes", lambda: ceiling_bytes):
            budget = resolve_ooc_memory_limit(budget_bytes=None).budget_bytes
        live = 1 if sink else incoming + 1
        return actual_duckdb_cap_bytes(budget, live)

    @pytest.mark.parametrize("rows", [1_000, 18_500_000, 20_000_000, 33_300_000, 100_000_000])
    def test_declared_minimum_round_trips_to_a_sufficient_cap_sink(self, rows: int) -> None:
        floor = predict_ooc_build_floor_bytes(rows)
        ceiling = declared_minimum_ceiling_bytes(floor, incoming_edges=0, sink=True)
        cap = self._resolved_cap(ceiling, sink=True, incoming=0)
        assert cap >= floor

    @pytest.mark.parametrize("rows", [1_000, 9_200_000, 20_000_000, 33_300_000])
    @pytest.mark.parametrize("incoming", [1, 3, 7])
    def test_declared_minimum_round_trips_to_a_sufficient_cap_resident(
        self, rows: int, incoming: int
    ) -> None:
        floor = predict_ooc_build_floor_bytes(rows)
        ceiling = declared_minimum_ceiling_bytes(floor, incoming_edges=incoming, sink=False)
        cap = self._resolved_cap(ceiling, sink=False, incoming=incoming)
        assert cap >= floor

    def test_declared_minimum_is_a_whole_gib(self) -> None:
        ceiling = declared_minimum_ceiling_bytes(
            predict_ooc_build_floor_bytes(20_000_000), incoming_edges=0, sink=True
        )
        assert ceiling % _GIB == 0


class TestCodexRound2BoundaryCases:
    """The two admitted-then-OOM cases Codex's round-2 gate reproduced: a
    floor that fit the OLD binary `budget // live` cap but exceeded the true
    ACTUAL decimal DuckDB cap. Both must now be REFUSED before any DuckDB
    work, with a truthful declared minimum (see `TestDeclaredMinimumCeiling`
    for the general self-consistency property; these pin the exact reported
    rows/topology)."""

    def _resolved_host_budget(self, ceiling_bytes: int) -> int:
        with patch.object(budget_mod, "detect_effective_memory_bytes", lambda: ceiling_bytes):
            return budget_mod.resolve_ooc_memory_limit(budget_bytes=None).budget_bytes

    def test_18_5m_rows_sink_on_4gib_host_is_refused(self) -> None:
        budget = self._resolved_host_budget(4 * _GIB)
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"parent": 18_500_000}, budget_bytes=budget, sink=True, incoming_edge_counts={}
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"

    def test_9_2m_rows_resident_fanin_1_on_4gib_host_is_refused(self) -> None:
        budget = self._resolved_host_budget(4 * _GIB)
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"hub": 9_200_000},
                budget_bytes=budget,
                sink=False,
                incoming_edge_counts={"hub": 1},
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"
