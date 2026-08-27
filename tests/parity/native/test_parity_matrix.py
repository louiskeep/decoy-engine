"""Pinned-oracle parity harness self-test + live-registry golden matrix.

Task 0.6 (engine-efficiency program, Phase 0, LAST task). This is the safety
net every later phase depends on: it proves a native result is LOGICALLY
identical to today's pandas result, allowing ONLY enumerated physical
differences.

Two halves:

1. The harness self-test (`TestHarnessStrictness`) proves `assert_logical_parity`
   is NOT loose: it PASSES on an exact match (and on the one enumerated
   null-typed normalization) and FAILS on each of a differing value, a
   reordered row, a null-vs-value swap, a missing warning, a missing row error,
   and an un-enumerated Arrow type difference. A harness that passed on any of
   those would be a broken net.

2. The golden matrix (`STRATEGY_MATRIX` / `PROVIDER_MATRIX`, generated from the
   LIVE registries) enumerates one case per strategy and one per provider
   binding. Every case runs the candidate against the pinned pandas oracle. The
   candidate routes through the native substrate, which does not exist yet, so
   each case is `xfail(strict=False)`: the matrix is COMPLETE now and each case
   flips to PASS as its strategy's native phase lands. The completeness tests
   guarantee a newly added strategy or provider MUST appear here rather than be
   silently omitted.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.providers_v2 import get_default_registry
from tests.parity.native._fixtures import (
    DEFAULT_ALLOWED_PHYSICAL_DIFFS,
    PROVIDER_MATRIX,
    STRATEGY_MATRIX,
    LogicalResult,
    MatrixCase,
    assert_logical_parity,
    run_candidate,
    run_oracle,
)

# ---------------------------------------------------------------------------
# Small builders for hand-constructed LogicalResults (self-test only)
# ---------------------------------------------------------------------------


_STR = pa.string()


def _one_table(values: list[object], *, arrow_type: pa.DataType = _STR) -> dict[str, pa.Table]:
    return {"t": pa.table({"c": pa.array(values, type=arrow_type)})}


def _result(
    values: list[object],
    *,
    arrow_type: pa.DataType = _STR,
    warnings: tuple[QualityWarning, ...] = (),
    row_errors: tuple[RowErrorRecord, ...] = (),
) -> LogicalResult:
    return LogicalResult(
        outputs=_one_table(values, arrow_type=arrow_type),
        warnings=warnings,
        row_errors=row_errors,
    )


_WARNING = QualityWarning(
    code="low_distinct_ratio", provider="p", column="c", detail={"ratio": 0.1}
)
_ROW_ERROR = RowErrorRecord(
    table="t", column="c", row_index=2, trigger="format_error", reason="unparseable"
)


class TestHarnessStrictness:
    """The harness must be STRICT. Each divergence below MUST raise."""

    def test_passes_on_exact_match(self) -> None:
        oracle = _result(["a", None, "b"])
        candidate = _result(["a", None, "b"])
        assert_logical_parity(candidate, oracle)  # must not raise

    def test_passes_on_enumerated_null_typed_normalization(self) -> None:
        # Oracle infers `string` for an all-null column; the candidate's
        # equivalent chunk came back Arrow `null`-typed. Same logical data
        # (every value null); the ONE enumerated physical difference allows it.
        oracle = _result([None, None], arrow_type=pa.string())
        candidate = _result([None, None], arrow_type=pa.null())
        assert_logical_parity(candidate, oracle)  # must not raise

    def test_fails_on_differing_value(self) -> None:
        oracle = _result(["a", "b", "c"])
        candidate = _result(["a", "X", "c"])
        with pytest.raises(AssertionError):
            assert_logical_parity(candidate, oracle)

    def test_fails_on_reordered_row(self) -> None:
        oracle = _result(["a", "b", "c"])
        candidate = _result(["b", "a", "c"])
        with pytest.raises(AssertionError):
            assert_logical_parity(candidate, oracle)

    def test_fails_on_null_vs_value_swap(self) -> None:
        oracle = _result([None, "b"])
        candidate = _result(["a", "b"])
        with pytest.raises(AssertionError):
            assert_logical_parity(candidate, oracle)

    def test_fails_on_missing_warning(self) -> None:
        oracle = _result(["a", "b"], warnings=(_WARNING,))
        candidate = _result(["a", "b"], warnings=())
        with pytest.raises(AssertionError):
            assert_logical_parity(candidate, oracle)

    def test_fails_on_missing_row_error(self) -> None:
        oracle = _result(["a", "b", "c"], row_errors=(_ROW_ERROR,))
        candidate = _result(["a", "b", "c"], row_errors=())
        with pytest.raises(AssertionError):
            assert_logical_parity(candidate, oracle)

    def test_fails_on_unenumerated_arrow_type_difference(self) -> None:
        # Identical values, but the candidate widened string -> large_string.
        # That is NOT the null-typed normalization, so the default allow-list
        # must reject it (a generic "widen Arrow types" escape hatch would
        # defeat the harness).
        oracle = _result(["a", "b"], arrow_type=pa.string())
        candidate = _result(["a", "b"], arrow_type=pa.large_string())
        with pytest.raises(AssertionError):
            assert_logical_parity(candidate, oracle)

    def test_null_typed_allowance_does_not_admit_width_drift(self) -> None:
        # Belt-and-suspenders: even passing an explicit allow-list of the one
        # enumerated diff must not admit a non-null width drift.
        oracle = _result(["a", "b"], arrow_type=pa.string())
        candidate = _result(["a", "b"], arrow_type=pa.large_string())
        with pytest.raises(AssertionError):
            assert_logical_parity(
                candidate, oracle, allowed_physical_diffs=DEFAULT_ALLOWED_PHYSICAL_DIFFS
            )


# ---------------------------------------------------------------------------
# Matrix completeness: enumerate the LIVE registries
# ---------------------------------------------------------------------------


def test_strategy_matrix_covers_every_live_strategy() -> None:
    """Every strategy in the live handler registry appears in STRATEGY_MATRIX.

    A newly added strategy must show up here (as an xfail case) rather than be
    silently omitted from the safety net.
    """
    covered = {case.strategy for case in STRATEGY_MATRIX}
    live = set(SCALAR_HANDLERS)
    assert covered == live, f"matrix drift: missing {live - covered}, extra {covered - live}"


def test_provider_matrix_covers_every_live_provider() -> None:
    """Every provider binding in the live registry appears in PROVIDER_MATRIX."""
    covered = {case.provider for case in PROVIDER_MATRIX}
    live = set(get_default_registry().known_providers())
    assert covered == live, f"matrix drift: missing {live - covered}, extra {covered - live}"


# ---------------------------------------------------------------------------
# Oracle smoke: the pinned oracle produces a comparable LogicalResult NOW for
# every runnable case (this is what later phases diff against).
# ---------------------------------------------------------------------------

_RUNNABLE = [c for c in (*STRATEGY_MATRIX, *PROVIDER_MATRIX) if c.oracle_runnable]


@pytest.mark.parametrize("case", _RUNNABLE, ids=[c.case_id for c in _RUNNABLE])
def test_pinned_oracle_runs_for_runnable_case(case: MatrixCase) -> None:
    result = run_oracle(case.config, case.sources)
    assert set(result.outputs) == {case.table}
    assert case.table in result.outputs


# ---------------------------------------------------------------------------
# Native parity matrix: complete now, each case flips to PASS as its phase
# lands. The candidate routes through the native substrate (not wired yet),
# so every case is an expected failure today.
# ---------------------------------------------------------------------------


def _native_parity(case: MatrixCase) -> None:
    oracle = run_oracle(case.config, case.sources)
    candidate = run_candidate(case.config, case.sources)
    assert_logical_parity(candidate, oracle, allowed_physical_diffs=case.allowed_physical_diffs)


@pytest.mark.xfail(
    strict=False, reason="native substrate not wired yet (Phase 1+); flips to PASS per phase"
)
@pytest.mark.parametrize("case", STRATEGY_MATRIX, ids=[c.case_id for c in STRATEGY_MATRIX])
def test_strategy_matrix_native_parity(case: MatrixCase) -> None:
    _native_parity(case)


@pytest.mark.xfail(
    strict=False, reason="native substrate not wired yet (Phase 1+); flips to PASS per phase"
)
@pytest.mark.parametrize("case", PROVIDER_MATRIX, ids=[c.case_id for c in PROVIDER_MATRIX])
def test_provider_matrix_native_parity(case: MatrixCase) -> None:
    _native_parity(case)
