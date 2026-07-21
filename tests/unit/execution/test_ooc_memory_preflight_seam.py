"""MEDIUM remediation: the seam test that would have caught the BLOCKER.

The BLOCKER was a denomination mismatch: the preflight compared a predicted
floor against a fraction of the raw detected memory ceiling, while Part A
actually hands DuckDB's build connection a DIFFERENT number -- `resolve_ooc_
memory_limit`'s resolved `budget_bytes` (the raw ceiling MINUS a reserve),
divided again by phase-local liveness. A job could clear the preflight's
fraction-of-ceiling check and still be starved by the real, smaller cap.

This module asserts the two are now structurally the same number: for every
HOST ceiling tested, the LARGEST row count the preflight admits genuinely
fits under the cap `resolve_ooc_memory_limit` + `resolve_phase_memory_limits`
would hand the real connection, on both the sink path (cap = ACTUAL decimal
DuckDB cap for 1 live instance) and a resident fan-in path (cap = ACTUAL
decimal DuckDB cap for incoming_edges + 1 live instances).

ROUND-2 NOTE: this file's own helpers originally modeled that cap as the
BINARY `budget // live` -- the same denomination mismatch round 2's Fix B
closes in the module under test (DuckDB reads `memory_limit_for`'s emitted
string as base-10 megabytes, a smaller number than the binary division
suggests). `_max_admitted_rows` is now driven by `actual_duckdb_cap_bytes`,
the exact number `enforce_ooc_memory_preflight` gates against post-fix, so
this seam test's own cap concept matches the module under test again.

The budget is resolved via the SAME auto-detect path a real host takes
(`resolve_ooc_memory_limit(budget_bytes=None)` against the detected ceiling),
so the reserve subtraction -- the exact term the BLOCKER's raw-ceiling
fraction ignored -- is in the loop. A 4 GiB host resolves to a 2 GiB build
budget, which is why a 20M-row parent is refused there.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _budget as budget_mod
from decoy_engine.execution.out_of_core._memory_estimate import (
    actual_duckdb_cap_bytes,
    enforce_ooc_memory_preflight,
    predict_ooc_build_floor_bytes,
)

_GIB = 1024**3
_MAX_ROWS_SEARCHED = 50_000_000_000  # comfortably above any tested ceiling's admitted row count


def _resolved_host_budget(ceiling_bytes: int) -> int:
    """The `OutOfCoreBudget.budget_bytes` a real host with `ceiling_bytes` of
    memory resolves to -- ceiling MINUS `resolve_ooc_memory_limit`'s reserve,
    the SAME undivided number `run_out_of_core_route` threads into the
    preflight. Auto-detect path (`budget_bytes=None`), NOT an explicit budget:
    an explicit budget skips the reserve, which would model a different (and
    wrong-for-this-test) scenario. Concurrency does not affect the UNDIVIDED
    return, so `max_concurrent_instances` is left at its default."""
    with patch.object(budget_mod, "detect_effective_memory_bytes", lambda: ceiling_bytes):
        return budget_mod.resolve_ooc_memory_limit(budget_bytes=None).budget_bytes


def _max_admitted_rows(cap_bytes: int) -> int:
    """Largest row count whose predicted floor still fits under `cap_bytes`.

    Binary search over `predict_ooc_build_floor_bytes` rather than a closed-
    form inversion: the re-fit model (FIX 3) is a conservative envelope, so
    this stays correct regardless of its exact shape, as long as the model is
    monotonic non-decreasing in rows (which the over-predict-on-doubt
    calibration discipline guarantees).
    """
    if predict_ooc_build_floor_bytes(0) > cap_bytes:
        return -1  # not even zero rows fits; caller-specific, not exercised here
    lo, hi = 0, _MAX_ROWS_SEARCHED
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if predict_ooc_build_floor_bytes(mid) <= cap_bytes:
            lo = mid
        else:
            hi = mid - 1
    return lo


@pytest.mark.parametrize("ceiling_gib", [4, 8, 16, 32])
class TestSeamSinkPath:
    """cap(t) = actual_duckdb_cap_bytes(budget_bytes, 1) -- the run's only
    live instance once joiners close (`_emit.py`'s `on_stream_consumed`)."""

    def test_max_admitted_rows_is_actually_admitted(self, ceiling_gib: int) -> None:
        budget = _resolved_host_budget(ceiling_gib * _GIB)
        cap = actual_duckdb_cap_bytes(budget, 1)
        rows = _max_admitted_rows(cap)
        result = enforce_ooc_memory_preflight(
            {"parent": rows}, budget_bytes=budget, sink=True, incoming_edge_counts={}
        )
        assert result.ok is True
        assert predict_ooc_build_floor_bytes(rows) <= cap

    def test_one_row_past_the_boundary_is_refused(self, ceiling_gib: int) -> None:
        budget = _resolved_host_budget(ceiling_gib * _GIB)
        cap = actual_duckdb_cap_bytes(budget, 1)
        rows = _max_admitted_rows(cap) + 1
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"parent": rows}, budget_bytes=budget, sink=True, incoming_edge_counts={}
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"


@pytest.mark.parametrize("ceiling_gib", [4, 8, 16, 32])
class TestSeamResidentFanIn:
    """A resident-path fan-in-3 child table: cap(t) =
    actual_duckdb_cap_bytes(budget_bytes, 4) -- the HIGH's own shape (FIX 2),
    and the tightest real-world cap a table sees."""

    _INCOMING = 3

    def test_max_admitted_rows_is_actually_admitted(self, ceiling_gib: int) -> None:
        budget = _resolved_host_budget(ceiling_gib * _GIB)
        cap = actual_duckdb_cap_bytes(budget, self._INCOMING + 1)
        rows = _max_admitted_rows(cap)
        result = enforce_ooc_memory_preflight(
            {"hub": rows},
            budget_bytes=budget,
            sink=False,
            incoming_edge_counts={"hub": self._INCOMING},
        )
        assert result.ok is True
        assert predict_ooc_build_floor_bytes(rows) <= cap

    def test_one_row_past_the_boundary_is_refused(self, ceiling_gib: int) -> None:
        budget = _resolved_host_budget(ceiling_gib * _GIB)
        cap = actual_duckdb_cap_bytes(budget, self._INCOMING + 1)
        rows = _max_admitted_rows(cap) + 1
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"hub": rows},
                budget_bytes=budget,
                sink=False,
                incoming_edge_counts={"hub": self._INCOMING},
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"


class TestKnownOperatingPoints:
    """The exact operating points behind the BLOCKER: on a 4 GiB HOST the
    build budget resolves to 2 GiB (ceiling minus the 2 GiB reserve). A
    100M-row job was ADMITTED by the pre-fix ceiling-fraction check, then
    OOMed inside DuckDB; a 20M-row job has a floor already past its real
    2 GiB build cap. Both must now be REFUSED, and a job whose floor genuinely
    fits its cap must still be ADMITTED."""

    def test_100m_rows_refused_on_a_4gib_host_sink(self) -> None:
        budget = _resolved_host_budget(4 * _GIB)
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"parent": 100_000_000}, budget_bytes=budget, sink=True, incoming_edge_counts={}
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"

    def test_20m_rows_refused_on_a_4gib_host_sink(self) -> None:
        # floor(20M) ~= 2226 MiB > the 2048 MiB build cap a 4 GiB host yields.
        budget = _resolved_host_budget(4 * _GIB)
        assert budget == 2 * _GIB  # 4 GiB ceiling minus the 2 GiB reserve
        with pytest.raises(ExecutionError) as excinfo:
            enforce_ooc_memory_preflight(
                {"parent": 20_000_000}, budget_bytes=budget, sink=True, incoming_edge_counts={}
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"
        assert "GB of memory" in excinfo.value.message

    def test_a_job_that_genuinely_fits_is_still_admitted(self) -> None:
        # A 20M-row parent on a generous 64 GiB host: floor ~2.2 GiB fits the
        # ~48 GiB build cap easily -- the gate must not become so conservative
        # it refuses jobs that genuinely fit.
        budget = _resolved_host_budget(64 * _GIB)
        result = enforce_ooc_memory_preflight(
            {"parent": 20_000_000}, budget_bytes=budget, sink=True, incoming_edge_counts={}
        )
        assert result.ok is True
        assert result.warned is False
