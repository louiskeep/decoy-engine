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
    _DP_EPSILON_CEILING,
    _EPS_Q_FLOOR,
    DpBudgetError,
    OpenDpReleaseSession,
    _FakeMeasurement,
    check_epsilon_supported,
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

    def test_refuses_duplicate_numeric_release_without_a_second_invocation(self):
        """HIGH H-B: the two H2 tests above cover `release_row_count` and
        `release_categorical` only, leaving `_admit`-before-invoke unpinned
        on the numeric path. Demonstrated: moving `self._admit(name)` in
        `release_numeric` from before construction/invoke to immediately
        before `_record` -- the exact parked H2 defect -- passed the whole
        `tests/unit/quality/` suite (389 passed) with only these two H2
        tests present. This exercises `release_numeric` the same way:
        legitimate release first, then a second call must be refused
        before the mechanism is invoked again."""
        backend = _SpyBackend()
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6, backend=backend)
        session.release_numeric("numeric:age", [1.0, 2.0, 3.0])
        assert backend.log == ["numeric"]
        with pytest.raises(DpBudgetError) as exc:
            session.release_numeric("numeric:age", [1.0, 2.0, 3.0])
        assert exc.value.code == "dp_duplicate_release"
        assert backend.log == ["numeric"]  # unchanged: the duplicate never invoked a second time

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

    def test_release_session_budget_allocation_is_data_independent(self):
        """D-M1 (dennis must-fix), the version of the above that actually
        VARIES data: two REAL OpenDP sessions built from the identical
        public (schedule, epsilon, delta), each released against a
        dataset differing in EVERY value (not merely a different row
        count or a couple of swapped labels) -- one dataset's numeric
        column at the low end of its domain with one label alphabet, the
        other at the high end with a disjoint label alphabet, disjoint
        row counts too. Certificates depend only on the CALIBRATED
        scale/threshold (a pure function of eps_q/delta_alloc), never on
        the values actually released, so every recorded certificate must
        be identical across the two regardless of how different the
        released data is."""
        schedule = _mixed_schedule()
        session_a = OpenDpReleaseSession(schedule, epsilon=1.0, delta=1e-6)
        session_b = OpenDpReleaseSession(schedule, epsilon=1.0, delta=1e-6)
        assert session_a._eps_q == session_b._eps_q

        session_a.release_row_count(3)
        session_b.release_row_count(9_000)
        session_a.release_numeric("numeric:age", [0.0, 0.0, 0.0])
        session_b.release_numeric("numeric:age", [float(x) for x in range(9_000)])
        session_a.release_categorical(
            "categorical_grouped:state", "categorical_total:state", ["only_a"] * 3
        )
        session_b.release_categorical(
            "categorical_grouped:state",
            "categorical_total:state",
            [f"distinct_b_{i}" for i in range(9_000)],
        )

        for name in schedule.query_names:
            assert session_a._releases[name].certificate == session_b._releases[name].certificate

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

    def test_categorical_only_schedule_allocation_is_not_collapsed_near_the_floor(self):
        """C-H2 (Codex HIGH), direct reproduction: `_allocate_epsilon`'s
        search predicate used `(composed_epsilon_or_none(e) or math.inf)
        <= self._epsilon`, which treats a valid composed epsilon of
        exactly `0.0` (common for a categorical-only schedule with target-
        delta headroom) as FALSY -- `0.0 or math.inf` is `math.inf` -- so
        the predicate reads false at its own lower bound and the search
        returns immediately without ever searching upward. Executed: one
        categorical column at `(epsilon=1.0, delta=0.02)` allocated
        `eps_q=1.8221832095805442e-09` under the defect (collapsed to the
        `_EPS_Q_FLOOR` region), while `eps_q=0.1` composes to `~0.225`,
        comfortably inside the request -- the defect defeats the locked
        allocation policy and makes categorical releases nearly useless,
        even though it under-spends (fails safe on privacy) rather than
        over-spends."""
        from decoy_engine.quality.dp_budget import OpenDpReleaseSession
        from decoy_engine.quality.dp_schedule import CategoricalQuerySpec, Schedule

        schedule = Schedule(
            row_count_name="rc", numeric=(), categorical=(CategoricalQuerySpec("g", "t"),)
        )
        session = OpenDpReleaseSession(schedule, epsilon=1.0, delta=0.02)
        # A correct allocation lands well above the floor -- the defect
        # collapses to ~1.8e-9; a sane allocation is many orders of
        # magnitude larger. Compare against a generous but discriminating
        # factor above the floor rather than pinning an exact value the
        # search's fixed-iteration bisection could shift slightly.
        assert session._eps_q > _EPS_Q_FLOOR * 1000

    def test_certificates_come_from_measurement_maps_not_the_calibration_target(self):
        """BLOCKER B-1 / A2, load-bearing over ALL FOUR `_record` call
        sites (`release_row_count`, `release_numeric`, and the grouped +
        total halves of `release_categorical`): each recorded certificate
        must equal `measurement.map(1)` for the object ACTUALLY invoked,
        not the eps_q target the session calibrated toward.

        D-B1 (dennis blocker): the previous fixture's `_FixedCertificate
        Backend` returned a CONSTANT certificate per kind, independent of
        the `eps_q` (and `delta_alloc`) the session called it with. That
        distinguishes measurement KIND, not measurement OBJECT: a defect
        that certifies a DIFFERENT measurement object than the one
        invoked --

            certificate = self._count_measurement(self._eps_q / 10.0).map(1)
            released    = measurement.invoke([""] * row_count)

        -- is invisible to a per-kind-constant fixture, because BOTH the
        correct object (built at `self._eps_q`) and the wrong one (built
        at `self._eps_q / 10.0`) return the identical constant through
        `.map()`. This version makes each fake certificate a function of
        the `eps_q` (and, for the grouped pair, `delta_alloc`) the backend
        was actually called with, so certifying a measurement built at a
        DIFFERENT eps_q necessarily yields a DIFFERENT recorded value --
        the wrong-object mutant above now changes what this test observes.

        Every fixed FACTOR here is far enough from 1.0 that a defect
        recording the bare `self._eps_q`/`self._delta_per_categorical`
        (the B-1 mutation evidence below) still cannot coincide with
        `eps_q * factor` for any `eps_q` this schedule allocates.

        Mutation evidence (each `_record` call site changed independently
        in `dp_budget.py`), confirmed to fail only its own assertion below,
        one call site at a time:
        - `self._record(<name>, self._eps_q, <released>)` (all four
          sites: records the bare eps_q/delta_alloc, not a certificate).
        - `certificate = self._count_measurement(self._eps_q / 10.0).map(1)`
          (row_count: certifies a measurement built at a different eps_q
          than the one invoked) -- and the same `/10.0` mutation applied
          independently to the numeric and grouped/total construction
          call sites.
        """
        ROW_COUNT_FACTOR = 0.37
        NUMERIC_FACTOR = 0.53
        GROUPED_EPS_FACTOR = 0.61
        GROUPED_DELTA_FACTOR = 0.29
        TOTAL_FACTOR = 0.71

        class _FixedCertificateBackend:
            """Certificates are a pure function of the eps_q/delta_alloc
            the session actually calls this backend with -- never a
            per-kind constant -- so certifying a measurement built at the
            WRONG eps_q (a different object than the one invoked) yields
            a visibly different recorded value."""

            def count_measurement(self, eps_q: float) -> _FakeMeasurement:
                return _FakeMeasurement(certificate=eps_q * ROW_COUNT_FACTOR, released=42)

            def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
                return _FakeMeasurement(
                    certificate=eps_q * NUMERIC_FACTOR, released=[0] * (len(interior_edges) + 1)
                )

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                return (
                    _FakeMeasurement(
                        certificate=(
                            eps_q * GROUPED_EPS_FACTOR,
                            delta_alloc * GROUPED_DELTA_FACTOR,
                        ),
                        released={},
                    ),
                    _FakeMeasurement(certificate=eps_q * TOTAL_FACTOR, released=0),
                )

        schedule = _mixed_schedule()  # row_count + 1 numeric + 1 categorical pair
        session = OpenDpReleaseSession(
            schedule, epsilon=1.0, delta=1e-6, backend=_FixedCertificateBackend()
        )

        session.release_row_count(500)
        session.release_numeric("numeric:age", [float(x % 120) for x in range(500)])
        session.release_categorical(
            "categorical_grouped:state", "categorical_total:state", ["CA", "NY", "TX"]
        )

        eps_q = session._eps_q
        delta_alloc = session._delta_per_categorical
        assert session._releases["row_count"].certificate == eps_q * ROW_COUNT_FACTOR
        assert session._releases["numeric:age"].certificate == eps_q * NUMERIC_FACTOR
        assert session._releases["categorical_grouped:state"].certificate == (
            eps_q * GROUPED_EPS_FACTOR,
            delta_alloc * GROUPED_DELTA_FACTOR,
        )
        assert session._releases["categorical_total:state"].certificate == eps_q * TOTAL_FACTOR
        # None of the recorded certificates coincide with the bare
        # eps_q/delta_alloc the B-1 mutation would record instead.
        assert session._releases["row_count"].certificate != eps_q
        assert session._releases["numeric:age"].certificate != eps_q
        assert session._releases["categorical_total:state"].certificate != eps_q

    def test_admit_reserves_the_name_so_a_second_admit_before_any_record_is_refused(self):
        """M-2: `_admit` alone -- before `_record` ever runs for `name` --
        must refuse a second admission of the same name. The previous
        implementation checked only `name in self._releases`, which
        `_record` populates AFTER the mechanism is invoked; "released only
        once" held only because every `release_*` method happens to call
        `_admit` immediately followed by an invoke and a `_record` within
        one synchronous call, never because `_admit` itself remembered a
        name it had already admitted. This calls `_admit` directly twice
        for the same name with no `_record` in between, which the old
        check could not catch."""
        session = OpenDpReleaseSession(_mixed_schedule(), epsilon=1.0, delta=1e-6)
        session._admit("row_count")
        with pytest.raises(DpBudgetError) as exc:
            session._admit("row_count")
        assert exc.value.code == "dp_duplicate_release"


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

    def test_ten_releases_at_point_one_epsilon_sum_to_exactly_one(self):
        """C-H1 (Codex HIGH): executed, plain `sum` over ten charges of
        epsilon=0.1 gives `0.9999999999999999` (binary floating-point
        term-by-term error), strictly LESS than a ceiling of exactly
        `1.0` -- so a caller enforcing `total_epsilon() <= 1.0` would
        accept a conservative total that is really `1.0`, not less.
        `math.fsum` sums with a single final rounding and gives exactly
        `1.0`. This pins the exact literal value, not an approx bound,
        since the whole point is the difference between the two."""
        ledger = ReleaseLedger()
        for _ in range(10):
            ledger.charge("r", epsilon=0.1, delta=0.1)
        assert sum([0.1] * 10) == 0.9999999999999999  # the plain-sum defect, restated
        assert ledger.total_epsilon() == 1.0
        assert ledger.total_delta() == 1.0


class TestEpsilonCeiling:
    """The frozen epsilon ceiling (guide section 4). A requested fit-wide
    epsilon above `_DP_EPSILON_CEILING` must fail closed with a coded error
    BEFORE any private data is read, never surface as a raw OverflowError deep
    in PLD composition. This is the pure guard; the composition call site wires
    it in at DPS-CODEC phase 4/5."""

    def test_ceiling_is_the_frozen_conservative_value(self) -> None:
        # Pinned literal, not a build-time probe: 700.0 sits comfortably below
        # the ~709.78 PLD overflow on every v1 manifest row.
        assert _DP_EPSILON_CEILING == 700.0

    @pytest.mark.parametrize("epsilon", [0.5, 1.0, 100.0, 699.999, 700.0])
    def test_accepts_at_or_below_ceiling(self, epsilon: float) -> None:
        check_epsilon_supported(epsilon)  # must not raise

    @pytest.mark.parametrize("epsilon", [700.0001, 709.783, 1e6, float("inf")])
    def test_rejects_above_ceiling(self, epsilon: float) -> None:
        with pytest.raises(DpBudgetError) as exc:
            check_epsilon_supported(epsilon)
        assert exc.value.code == "dp_epsilon_unsupported"

    def test_rejects_nan_request(self) -> None:
        # NaN cannot be certified; every comparison is False, so the
        # `not (eps <= ceiling)` form fails it closed rather than admitting it.
        with pytest.raises(DpBudgetError) as exc:
            check_epsilon_supported(float("nan"))
        assert exc.value.code == "dp_epsilon_unsupported"
