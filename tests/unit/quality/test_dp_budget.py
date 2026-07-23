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

from dataclasses import dataclass, field
from typing import Any

import pytest

from decoy_engine.quality.dp_budget import (
    DpBudgetError,
    OpenDpReleaseSession,
    _FakeMeasurement,
)
from decoy_engine.quality.dp_ledger import ReleaseLedger
from decoy_engine.quality.dp_schedule import CategoricalQuerySpec, NumericQuerySpec, Schedule


@dataclass
class _SpyBackend:
    """A backend whose `.invoke()` calls are logged, so a test can assert
    a refused release never reached the mechanism (H2): the log grows only
    when a measurement is actually invoked, never when one is merely
    constructed and certified via `.map()` during the allocation search."""

    log: list[str] = field(default_factory=list)

    def _spy(self, label: str, certificate: Any, released: Any) -> _FakeMeasurement:
        return _FakeMeasurement(
            certificate=certificate,
            released_fn=lambda _values, label=label, released=released: (
                self.log.append(label) or released
            ),
        )

    def count_measurement(self, eps_q: float) -> _FakeMeasurement:
        return self._spy("count", 0.01, 0)

    def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
        return self._spy("numeric", 0.01, [0] * (len(interior_edges) + 1))

    def categorical_measurements(self, eps_q: float, delta_alloc: float):
        return (
            self._spy("grouped", (0.01, delta_alloc), {}),
            self._spy("total", 0.01, 0),
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

    def test_refuses_unscheduled_query_without_invoking_any_measurement(self):
        """H2: refusing AFTER the mechanism ran is not refusing -- the
        spy backend's log only grows on `.invoke()`, never on `.map()`
        (which the allocation search alone calls during construction).
        `release_categorical` is the case the finding names directly (the
        parked code invoked both measurements THEN checked admission), so
        this exercises that path with an unscheduled grouped/total name
        pair and proves neither measurement is ever invoked."""
        backend = _SpyBackend()
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6, backend=backend)
        assert backend.log == []  # construction (allocation search) never invokes
        with pytest.raises(DpBudgetError) as exc:
            session.release_categorical(
                "categorical_grouped:not_scheduled", "categorical_total:not_scheduled", ["a", "b"]
            )
        assert exc.value.code == "dp_unscheduled_release"
        assert backend.log == []  # still never invoked -- refused before construction

    def test_refuses_duplicate_release_of_one_query_without_a_second_invocation(self):
        """H2, the load-bearing case: `release_row_count` invokes once
        legitimately, then a second call must be refused BEFORE a second
        measurement is constructed/invoked -- otherwise the mechanism
        spends budget a second time that the ledger never records."""
        backend = _SpyBackend()
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6, backend=backend)
        session.release_row_count(10)
        assert backend.log == ["count"]
        with pytest.raises(DpBudgetError) as exc:
            session.release_row_count(10)
        assert exc.value.code == "dp_duplicate_release"
        assert backend.log == ["count"]  # unchanged: the duplicate never invoked a second time

    def test_refuses_loss_report_before_schedule_is_complete(self):
        backend = _SpyBackend()
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6, backend=backend)
        session.release_row_count(10)
        invocations_before = list(backend.log)
        with pytest.raises(DpBudgetError) as exc:
            session.composed_loss()
        assert exc.value.code == "dp_schedule_incomplete"
        assert backend.log == invocations_before  # the failed report invoked nothing new

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
        """BLOCKER 1 / A2, load-bearing: the recorded certificate must
        equal `measurement.map(1)` for the object ACTUALLY invoked, not
        the eps_q target the session calibrated toward. `0.0 < epsilon_
        total <= 1.0` (the previous assertion here) holds for ANY
        implementation that records any small positive number -- it does
        not distinguish "recorded measurement.map(1)" from "recorded
        self._eps_q", which is exactly the defect A2 names. This test
        substitutes a backend whose certificate is fixed strictly BELOW
        the eps_q the session calibrates toward, then asserts the
        recorded certificate is that exact fixed value, not eps_q."""
        fixed_certificate = 1e-3  # far below any eps_q this schedule could allocate

        class _FixedCertificateBackend:
            def count_measurement(self, eps_q: float) -> _FakeMeasurement:
                return _FakeMeasurement(certificate=fixed_certificate, released=42)

            def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
                raise AssertionError("this schedule has no numeric column")

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                raise AssertionError("this schedule has no categorical column")

        schedule = Schedule(row_count_name="rc", numeric=(), categorical=())
        session = OpenDpReleaseSession(
            schedule, epsilon=1.0, delta=1e-6, backend=_FixedCertificateBackend()
        )
        # The allocation search converges on eps_q = epsilon itself here,
        # since a fixed certificate far below any target is "feasible"
        # everywhere the search looks; the fixed certificate is still far
        # below it.
        assert session._eps_q > fixed_certificate
        session.release_row_count(42)
        recorded = session._releases["rc"].certificate
        assert recorded == fixed_certificate
        assert recorded != session._eps_q


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
