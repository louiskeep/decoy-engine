"""DPS-CODEC phase 4: carrier plumbing through the DP budget/schedule/ledger.

These tests cover exactly the phase-4 surface (guide
``docs/plans/2026-07-23-dps-codec-implementation-guide.md`` sections 3.4 and 4):
the `flag` carrier on the schedule, bool-domain OpenDP measurements, the frozen
carrier-bearing budget cache key, the endpoint-aware calibration search, the
epsilon-ceiling guard wired into the composition path, and the zero-epsilon
ledger. The str/number path is left byte-for-byte unchanged; its regression
guard is the existing ``test_dp.py`` / ``test_dp_budget.py`` suites.
"""

from __future__ import annotations

import pytest

from decoy_engine.quality.dp_budget import (
    _ALLOCATION_CACHE,
    _DP_EPSILON_CEILING,
    _REAL_BACKEND_CACHE_NAMESPACE,
    DpBudgetError,
    OpenDpReleaseSession,
    _binary_search_endpoint_aware,
    _budget_cache_key,
    _clear_budget_cache,
    _compose,
    _FlagCapableBackend,
    _RealOpenDpBackend,
    check_epsilon_supported,
)
from decoy_engine.quality.dp_ledger import ReleaseLedger
from decoy_engine.quality.dp_schedule import (
    CategoricalQuerySpec,
    Schedule,
)


def _text_schedule() -> Schedule:
    return Schedule(
        row_count_name="rc",
        numeric=(),
        categorical=(CategoricalQuerySpec("categorical_grouped:c", "categorical_total:c"),),
    )


def _flag_schedule() -> Schedule:
    return Schedule(
        row_count_name="rc",
        numeric=(),
        categorical=(
            CategoricalQuerySpec("categorical_grouped:c", "categorical_total:c", carrier="flag"),
        ),
    )


class TestBoolDomainMeasurements:
    """Guide section 3.4: a `flag` categorical releases through the bool-domain
    `make_count_by(bool)` + `make_count(bool)` pair, which the str-domain
    `atom_domain(T=str)` cannot type."""

    def test_flag_categorical_grouped_and_total_construct_compose_and_release(self) -> None:
        backend = _RealOpenDpBackend()
        grouped, total = backend.categorical_measurements_flag(eps_q=0.5, delta_alloc=1e-6)

        # Grouped is a thresholded (epsilon, delta) release; total is pure
        # epsilon -- read the certificate from each object actually built.
        g_eps, g_delta = grouped.map(1)
        t_eps = total.map(1)
        assert g_eps > 0 and g_delta > 0
        assert t_eps > 0

        # Both release over a BOOL vector (the str domain would reject bools).
        # The total is a NOISED count, so assert only that it releases an int
        # over a bool vector -- the str domain would raise here, not return.
        released_total = total.invoke([True, False, True, True, False])
        assert isinstance(released_total, int)
        released_grouped = dict(grouped.invoke([True] * 50 + [False] * 40))
        assert all(isinstance(k, bool) for k in released_grouped)

        # The two bool-domain certificates compose through dp_accounting, so a
        # flag column has a well-defined fit-wide loss (guide section 3.3).
        composed = _compose([grouped.map(1), total.map(1)]).get_epsilon_for_delta(1e-6)
        assert composed > 0

    def test_str_domain_pair_is_the_text_carrier_default(self) -> None:
        # The legacy str-domain pair still constructs and is what `text` selects
        # -- the additive guarantee that the flag work did not disturb it.
        backend = _RealOpenDpBackend()
        grouped, total = backend.categorical_measurements(eps_q=0.5, delta_alloc=1e-6)
        # Noised counts, so assert the shape (int total over a str vector, dict
        # grouped) rather than exact values.
        assert isinstance(total.invoke(["a", "b", "c"]), int)
        assert isinstance(dict(grouped.invoke(["x"] * 50)), dict)

    def test_flag_release_end_to_end_through_a_real_session(self) -> None:
        _clear_budget_cache()
        session = OpenDpReleaseSession(_flag_schedule(), epsilon=2.0, delta=1e-3)
        session.release_row_count(90)
        grouped, total = session.release_categorical(
            "categorical_grouped:c",
            "categorical_total:c",
            [True] * 60 + [False] * 30,
        )
        assert isinstance(total, int)
        assert all(isinstance(k, bool) for k in grouped)
        epsilon_total, delta_total = session.composed_loss()
        assert 0 <= epsilon_total <= 2.0
        assert delta_total == 1e-3

    def test_flag_carrier_on_a_text_only_backend_fails_closed(self) -> None:
        # A backend that provides only the str-domain pair cannot serve a flag
        # column; the session refuses with a coded error, not an AttributeError.
        from decoy_engine.quality.dp_budget import _FakeMeasurement

        class _TextOnlyBackend:
            cache_namespace = None  # bypass the cache

            # A working str-domain backend (row-count + str categorical), but
            # WITHOUT `categorical_measurements_flag` -- so it is not a
            # `_FlagCapableBackend`, and a flag column must fail closed.
            def count_measurement(self, eps_q: float):
                return _FakeMeasurement(certificate=0.1, released=5)

            def numeric_measurement(self, eps_q: float, interior_edges):
                return _FakeMeasurement(certificate=0.1, released=[1])

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                return (
                    _FakeMeasurement(certificate=(0.1, 1e-9), released={}),
                    _FakeMeasurement(certificate=0.1, released=0),
                )

        assert not isinstance(_TextOnlyBackend(), _FlagCapableBackend)
        with pytest.raises(DpBudgetError) as exc:
            OpenDpReleaseSession(
                _flag_schedule(), epsilon=2.0, delta=1e-3, backend=_TextOnlyBackend()
            )
        assert exc.value.code == "dp_carrier_backend_unsupported"

    def test_crossed_grouped_total_pair_fails_closed(self) -> None:
        # A grouped/total name pair that does not resolve to ONE scheduled spec
        # (both names valid, but from different queries) must fail closed with a
        # coded error, not silently default the carrier to text (dennis LOW-2).
        from decoy_engine.quality.dp_budget import _FakeMeasurement

        class _StrBackend:
            cache_namespace = None

            def count_measurement(self, eps_q: float):
                return _FakeMeasurement(certificate=0.1, released=5)

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                return (
                    _FakeMeasurement(certificate=(0.1, 1e-9), released={}),
                    _FakeMeasurement(certificate=0.1, released=0),
                )

        schedule = Schedule(
            row_count_name="rc",
            numeric=(),
            categorical=(
                CategoricalQuerySpec("categorical_grouped:a", "categorical_total:a"),
                CategoricalQuerySpec("categorical_grouped:b", "categorical_total:b"),
            ),
        )
        session = OpenDpReleaseSession(schedule, epsilon=4.0, delta=1e-3, backend=_StrBackend())
        session.release_row_count(10)
        with pytest.raises(DpBudgetError) as exc:
            # grouped from query 'a', total from query 'b' -> no single spec pairs them.
            session.release_categorical("categorical_grouped:a", "categorical_total:b", ["x"])
        assert exc.value.code == "dp_budget_categorical_pair_unscheduled"


class TestCarrierInCacheKey:
    """Guide section 3.4/4: the carrier enters the schedule signature and the
    budget cache key, so a str-domain and a bool-domain categorical -- identical
    in every other respect -- never share a cached scalar calibration."""

    def test_signature_and_cache_key_differ_by_carrier_alone(self) -> None:
        text, flag = _text_schedule(), _flag_schedule()
        # The only difference between the two schedules is the carrier.
        assert text.signature() != flag.signature()
        ns = _REAL_BACKEND_CACHE_NAMESPACE
        key_text = _budget_cache_key(text, 1.0, 1e-6, ns)
        key_flag = _budget_cache_key(flag, 1.0, 1e-6, ns)
        assert key_text != key_flag

    def test_cache_key_carries_epsilon_delta_versions_and_namespace(self) -> None:
        import importlib.metadata

        sched = _text_schedule()
        base = _budget_cache_key(sched, 1.0, 1e-6, _REAL_BACKEND_CACHE_NAMESPACE)
        # epsilon, delta, and namespace each move the key.
        assert base != _budget_cache_key(sched, 2.0, 1e-6, _REAL_BACKEND_CACHE_NAMESPACE)
        assert base != _budget_cache_key(sched, 1.0, 2e-6, _REAL_BACKEND_CACHE_NAMESPACE)
        assert base != _budget_cache_key(sched, 1.0, 1e-6, "some-other-namespace")
        # The exact library versions are IN the key.
        assert importlib.metadata.version("opendp") in base
        assert importlib.metadata.version("dp-accounting") in base

    def test_real_session_caches_the_scalar_and_a_fake_backend_bypasses(self) -> None:
        from decoy_engine.quality.dp_budget import _FakeMeasurement

        _clear_budget_cache()
        # A real (namespaced) backend stores the scalar allocation; a second
        # identical request reuses it (byte-identical eps_q).
        sched = _flag_schedule()
        s1 = OpenDpReleaseSession(sched, epsilon=2.0, delta=1e-3)
        key = _budget_cache_key(sched, 2.0, 1e-3, _REAL_BACKEND_CACHE_NAMESPACE)
        assert key in _ALLOCATION_CACHE
        s2 = OpenDpReleaseSession(sched, epsilon=2.0, delta=1e-3)
        assert s1._eps_q == s2._eps_q == _ALLOCATION_CACHE[key]

        # A cache-less test double never populates the cache (guide section 4:
        # a stateful/fake backend must not read a scalar another session made).
        class _FakeBackend:
            def count_measurement(self, eps_q: float):
                return _FakeMeasurement(certificate=0.1, released=5)

            def numeric_measurement(self, eps_q: float, interior_edges):
                return _FakeMeasurement(certificate=0.1, released=[1])

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                return (
                    _FakeMeasurement(certificate=(0.1, 1e-9), released={}),
                    _FakeMeasurement(certificate=0.1, released=0),
                )

        before = dict(_ALLOCATION_CACHE)
        OpenDpReleaseSession(_text_schedule(), epsilon=2.0, delta=1e-3, backend=_FakeBackend())
        assert dict(_ALLOCATION_CACHE) == before  # unchanged: the fake bypassed


class TestEpsilonCeilingWiredIntoComposition:
    """Guide section 4: an over-ceiling requested epsilon fails closed with
    `dp_epsilon_unsupported` at session construction, BEFORE any composition --
    never a raw OverflowError mid-search."""

    def test_over_ceiling_request_fails_before_the_backend_is_touched(self) -> None:
        class _SpyBackend:
            def __init__(self) -> None:
                self.calls = 0

            def count_measurement(self, eps_q: float):
                self.calls += 1
                raise AssertionError("must not be reached")

            def numeric_measurement(self, eps_q: float, interior_edges):
                self.calls += 1
                raise AssertionError("must not be reached")

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                self.calls += 1
                raise AssertionError("must not be reached")

        spy = _SpyBackend()
        with pytest.raises(DpBudgetError) as exc:
            OpenDpReleaseSession(
                _text_schedule(), epsilon=_DP_EPSILON_CEILING + 1.0, delta=1e-3, backend=spy
            )
        assert exc.value.code == "dp_epsilon_unsupported"
        assert spy.calls == 0  # no measurement built, no composition run

    def test_nan_epsilon_request_fails_closed_at_construction(self) -> None:
        with pytest.raises(DpBudgetError) as exc:
            OpenDpReleaseSession(_text_schedule(), epsilon=float("nan"), delta=1e-3)
        assert exc.value.code == "dp_epsilon_unsupported"

    def test_at_ceiling_request_passes_the_guard(self) -> None:
        # The guard itself must admit exactly the ceiling (the pure-guard
        # contract); this is the composition-path entry the session calls.
        check_epsilon_supported(_DP_EPSILON_CEILING)  # must not raise


class TestEndpointAwareCalibration:
    """Guide section 4 (budget-calibration finding): a `binary_search` with no
    crossing because the predicate holds at an endpoint is feasible, not
    infeasible."""

    def test_predicate_true_everywhere_returns_the_lower_endpoint(self) -> None:
        assert _binary_search_endpoint_aware(lambda x: True, bounds=(1e-12, 1e12)) == 1e-12
        assert _binary_search_endpoint_aware(lambda x: True, bounds=(1, 2**31 - 1), T=int) == 1

    def test_normal_crossing_is_unchanged(self) -> None:
        # A real crossing never reaches the endpoint fallback: byte-identical to
        # the bare binary_search for every calibrating fit.
        assert _binary_search_endpoint_aware(
            lambda x: x >= 5.0, bounds=(0.0, 10.0)
        ) == pytest.approx(5.0, abs=1e-6)

    def test_predicate_false_everywhere_re_raises_infeasible(self) -> None:
        with pytest.raises(ValueError):
            _binary_search_endpoint_aware(lambda x: False, bounds=(1, 10), T=int)


class TestZeroEpsilonLedger:
    """Guide section 4 (zero-epsilon finding): the ledger sums composed release
    TOTALS, and a composed epsilon_total of exactly 0 is legitimate, so
    `charge` accepts `>= 0` while a negative total still fails closed."""

    def test_charge_accepts_zero_and_rejects_negative(self) -> None:
        ledger = ReleaseLedger()
        ledger.charge("zero-composed", epsilon=0.0, delta=0.0)
        ledger.charge("positive", epsilon=0.4, delta=1e-7)
        assert ledger.total_epsilon() == pytest.approx(0.4)
        with pytest.raises(ValueError, match="epsilon"):
            ledger.charge("negative", epsilon=-1e-12)
