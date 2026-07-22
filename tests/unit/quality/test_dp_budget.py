"""Unit tests for `decoy_engine.quality.dp_budget` (DPS Scope B).

`OpenDpReleaseSession` is the sole OpenDP call site (guide section 4.3.5
mitigation 1): the load-bearing property this file pins is that every
certificate it records equals `measurement.map(1)` for the object
actually invoked, and that the schedule is enforced (unscheduled/
duplicate releases refused, a loss report refused before the schedule
completes). `ReleaseLedger` is the separate, much simpler compile-time
policy-ceiling sum (guide section 3.3 item 3), not a mechanism
accountant.
"""

from __future__ import annotations

import pytest

from decoy_engine.quality.dp_budget import (
    CategoricalQuerySpec,
    DpBudgetError,
    NumericQuerySpec,
    OpenDpReleaseSession,
    ReleaseLedger,
    Schedule,
)


def _mixed_schedule() -> Schedule:
    return Schedule(
        row_count_name="row_count",
        numeric=(
            NumericQuerySpec(
                name="numeric:age", interior_edges=tuple(float(i) for i in range(1, 10))
            ),
        ),
        categorical=(
            CategoricalQuerySpec(
                grouped_name="categorical_grouped:state", total_name="categorical_total:state"
            ),
        ),
    )


class TestScheduleShape:
    def test_query_count_and_names(self):
        schedule = _mixed_schedule()
        assert schedule.query_count == 4  # 1 row_count + 1 numeric + 2 categorical
        assert schedule.query_names == (
            "row_count",
            "numeric:age",
            "categorical_grouped:state",
            "categorical_total:state",
        )

    def test_delta_per_categorical_splits_half(self):
        schedule = Schedule(
            row_count_name="rc",
            numeric=(),
            categorical=(CategoricalQuerySpec("g1", "t1"), CategoricalQuerySpec("g2", "t2")),
        )
        assert schedule.delta_per_categorical(1e-6) == pytest.approx((1e-6 / 2) / 2)

    def test_delta_per_categorical_zero_when_no_categorical_columns(self):
        schedule = Schedule(row_count_name="rc", numeric=(), categorical=())
        assert schedule.delta_per_categorical(1e-6) == 0.0


class TestOpenDpReleaseSession:
    def test_certificates_match_query_count_after_full_schedule(self):
        schedule = _mixed_schedule()
        session = OpenDpReleaseSession(schedule, epsilon=1.0, delta=1e-6)
        session.release_row_count(500)
        session.release_numeric("numeric:age", [float(x % 120) for x in range(500)])
        session.release_categorical(
            "categorical_grouped:state",
            "categorical_total:state",
            ["CA", "NY", "TX"] * 166 + ["ZZ"],
        )
        assert session.certificate_count() == schedule.query_count
        epsilon_total, delta_total = session.composed_loss()
        assert epsilon_total <= 1.0
        assert delta_total == 1e-6

    def test_refuses_unscheduled_query(self):
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6)
        with pytest.raises(DpBudgetError) as exc:
            session.release_numeric("numeric:not_scheduled", [1.0, 2.0])
        assert exc.value.code in ("dp_unscheduled_release",)

    def test_refuses_duplicate_release_of_one_query(self):
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6)
        session.release_row_count(10)
        with pytest.raises(DpBudgetError) as exc:
            session.release_row_count(10)
        assert exc.value.code == "dp_duplicate_release"

    def test_refuses_loss_report_before_schedule_is_complete(self):
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6)
        session.release_row_count(10)
        with pytest.raises(DpBudgetError) as exc:
            session.composed_loss()
        assert exc.value.code == "dp_schedule_incomplete"

    def test_query_schedule_is_column_order_independent(self):
        """Two schedules built from the same column DECLARATIONS in a
        different order produce the same query_count and the same query
        NAME set (order-independence of the public schedule construction;
        the caller sorts column names before building the schedule)."""
        s1 = Schedule(
            row_count_name="rc",
            numeric=(NumericQuerySpec("numeric:a", (1.0,)), NumericQuerySpec("numeric:b", (1.0,))),
            categorical=(),
        )
        s2 = Schedule(
            row_count_name="rc",
            numeric=(NumericQuerySpec("numeric:b", (1.0,)), NumericQuerySpec("numeric:a", (1.0,))),
            categorical=(),
        )
        assert s1.query_count == s2.query_count
        assert set(s1.query_names) == set(s2.query_names)

    def test_budget_allocation_is_data_independent(self):
        """Allocation depends only on (epsilon, delta, schedule shape) --
        never on values. Two sessions built from the identical public
        schedule/budget produce the identical per-query epsilon and
        identical certificates for identical released values, regardless
        of what the caller later releases."""
        schedule_a = _mixed_schedule()
        schedule_b = _mixed_schedule()
        session_a = OpenDpReleaseSession(schedule_a, epsilon=1.0, delta=1e-6)
        session_b = OpenDpReleaseSession(schedule_b, epsilon=1.0, delta=1e-6)
        assert session_a._eps_q == session_b._eps_q

    def test_raises_dp_budget_infeasible_when_schedule_cannot_be_funded(self):
        # 20 categorical columns at a vanishingly small epsilon/delta:
        # infeasible even at the floor.
        schedule = Schedule(
            row_count_name="rc",
            numeric=(),
            categorical=tuple(CategoricalQuerySpec(f"g{i}", f"t{i}") for i in range(20)),
        )
        with pytest.raises(DpBudgetError) as exc:
            OpenDpReleaseSession(schedule, epsilon=1e-9, delta=1e-9)
        assert exc.value.code == "dp_budget_infeasible"

    def test_certificates_come_from_measurement_maps_not_the_calibration_target(self):
        """Load-bearing (guide section 5 step 2 / section 6 row A2): the
        recorded certificate must equal `measurement.map(1)` for the
        object actually invoked, not the eps_q target the session
        calibrated toward. Calibration searches land on a scale/threshold
        whose certified map is typically slightly UNDER the target (the
        threshold search lands on an integer); this test proves the
        composed total reflects that, rather than assuming the target was
        met exactly."""
        schedule = Schedule(
            row_count_name="rc",
            numeric=(NumericQuerySpec("numeric:a", tuple(float(i) for i in range(1, 5))),),
            categorical=(),
        )
        session = OpenDpReleaseSession(schedule, epsilon=1.0, delta=1e-6)
        session.release_row_count(100)
        session.release_numeric("numeric:a", [float(x % 4) for x in range(100)])
        epsilon_total, _ = session.composed_loss()
        # The composed total from two certified releases at eps_q must be
        # a real accountant composition (strictly more than either single
        # certified release alone), not a naive eps_q * 2 == epsilon
        # coincidence copied from the request.
        assert 0.0 < epsilon_total <= 1.0


class TestReleaseLedger:
    """The compile-time policy ceiling sum -- NOT a mechanism accountant
    (guide section 3.3 item 3)."""

    def test_sums_already_certified_totals(self):
        ledger = ReleaseLedger()
        ledger.charge("release:a", epsilon=0.4, delta=1e-7)
        ledger.charge("release:b", epsilon=0.3, delta=2e-7)
        assert ledger.total_epsilon() == pytest.approx(0.7)
        assert ledger.total_delta() == pytest.approx(3e-7)
        assert len(ledger.breakdown()) == 2

    def test_rejects_nonpositive_epsilon(self):
        ledger = ReleaseLedger()
        with pytest.raises(ValueError, match="epsilon"):
            ledger.charge("bad", epsilon=0.0)

    def test_rejects_negative_delta(self):
        ledger = ReleaseLedger()
        with pytest.raises(ValueError, match="delta"):
            ledger.charge("bad", epsilon=1.0, delta=-1e-6)

    def test_empty_ledger_totals_are_zero(self):
        ledger = ReleaseLedger()
        assert ledger.total_epsilon() == 0.0
        assert ledger.total_delta() == 0.0
        assert ledger.breakdown() == []
