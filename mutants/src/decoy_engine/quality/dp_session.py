"""OpenDP release session: one fit's schedule allocation, release, and composition.

Split out of `quality/dp_budget.py` on a size-cap crossing (CLAUDE.md's
~600-LOC orchestration cap), mirroring the earlier `dp_schedule.py` /
`dp_ledger.py` splits. `dp_budget.py` remains the SOLE OpenDP construction
and invocation site (guide section 4.3.5 mitigation 1): the mechanism
backend (`_RealOpenDpBackend`), the endpoint-aware calibration search, and
the `import opendp.prelude` all stay there. This module holds only the
`OpenDpReleaseSession` orchestration, which touches no OpenDP name of its
own -- it drives the fit entirely through the backend protocol and the
calibration/composition primitives it imports from `dp_budget`. Keeping it
free of any `opendp` import is what lets the single-OpenDP-site sentry
(`tests/unit/quality/test_opendp_dependency.py`) still pass after the move.

`OpenDpReleaseSession` is re-exported from `dp_budget` (bottom-of-module) so
the documented `decoy_engine.quality.dp_budget.OpenDpReleaseSession` path
(`quality/dp.py`, the DP test suite) resolves unchanged.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from decoy_engine.quality.dp_budget import (
    _ALLOCATION_CACHE,
    _EPS_Q_FLOOR,
    _PLD_SEARCH_ITERATIONS,
    Certificate,
    DpBudgetError,
    _budget_cache_key,
    _compose,
    _FlagCapableBackend,
    _InfeasibleAtEpsQError,
    _OpenDpBackend,
    _RealOpenDpBackend,
    _Release,
    _search_largest,
    check_epsilon_supported,
)
from decoy_engine.quality.dp_schedule import Schedule

if TYPE_CHECKING:
    # `_dp_mod` (bound to `opendp.mod` under TYPE_CHECKING in `dp_budget`) is
    # re-imported by NAME from `dp_budget`, never as `import opendp...`, so this
    # module carries no `opendp` import for the single-site sentry to catch
    # while the `_dp_mod.Measurement` return annotations still type-check.
    from decoy_engine.quality.dp_budget import _dp_mod


class OpenDpReleaseSession:
    """Owns one fit's OpenDP measurements, certificates, and composition.

    Construction touches no data: it stores the public schedule and the
    fit-wide `(epsilon, delta)` request, and runs the section 4.3.2
    allocation search (a pure function of the request and the public
    query counts). Data is only touched by the `release_*` methods.
    """

    def __init__(
        self,
        schedule: Schedule,
        *,
        epsilon: float,
        delta: float,
        backend: _OpenDpBackend | None = None,
    ) -> None:
        # Fail closed on an over-ceiling (or NaN) requested epsilon BEFORE the
        # allocation search runs any dp_accounting composition (guide section 4,
        # phase 4 wiring): a request above `_DP_EPSILON_CEILING` would otherwise
        # surface as a raw `OverflowError` deep in PLD composition instead of a
        # coded `dp_epsilon_unsupported`. The strictly-positive check stays at
        # the fit's config parse; this guards only the upper bound.
        check_epsilon_supported(epsilon)
        self._schedule = schedule
        self._epsilon = epsilon
        self._delta = delta
        self._delta_per_categorical = schedule.delta_per_categorical(delta)
        self._releases: dict[str, _Release] = {}
        self._reserved: set[str] = set()  # admitted, not yet recorded (M-2 -- see `_admit`)
        # Production never passes `backend` (guide section 5 step 3): the
        # default is the real OpenDP-backed implementation. Only tests
        # substitute a double, and only to observe THIS session's own
        # bookkeeping (schedule enforcement, certificate provenance) --
        # never to fabricate a privacy guarantee.
        self._backend: _OpenDpBackend = backend or _RealOpenDpBackend()
        self._eps_q = self._allocate_epsilon_cached()

    # -- allocation (guide section 4.3.2) --------------------------------

    def _certify_schedule(self, eps_q: float) -> list[Certificate]:
        """Build every scheduled measurement at per-query epsilon `eps_q`
        and read its certificate, WITHOUT touching data. Used only by the
        allocation search; the real fit re-certifies each measurement at
        release time against the values it actually sees.

        Raises `_InfeasibleAtEpsQError` when OpenDP's own calibration search
        cannot find a scale/threshold reaching `eps_q` (and, for the
        thresholded chain, the allocated delta) within its search bounds --
        this eps_q is simply not achievable, which the allocation search
        (guide section 4.3.2) treats as "try a smaller eps_q", not a fit
        failure.
        """
        try:
            certificates: list[Certificate] = [self._count_measurement(eps_q).map(1)]
            for q in self._schedule.numeric:
                certificates.append(self._numeric_measurement(eps_q, q.interior_edges).map(1))
            for cat in self._schedule.categorical:
                grouped, total = self._categorical_measurements(
                    eps_q, self._delta_per_categorical, cat.carrier
                )
                certificates.append(grouped.map(1))
                certificates.append(total.map(1))
        except ValueError as exc:
            raise _InfeasibleAtEpsQError(str(exc)) from exc
        return certificates

    def _allocate_epsilon_cached(self) -> float:
        """`_allocate_epsilon`, memoized on the frozen budget cache key (guide
        section 4). The scalar allocation result is a pure function of the
        public (schedule, epsilon, delta) and the accounting-library versions,
        so a second fit with an identical request reuses the scalar instead of
        re-running the search. Only a namespaced (production) backend is cached;
        a test double without a `cache_namespace` bypasses the cache so it can
        never read a scalar another session computed. Infeasible requests raise
        out of `_allocate_epsilon` before anything is stored, so a failed
        allocation is never cached."""
        namespace = getattr(self._backend, "cache_namespace", None)
        if namespace is None:
            return self._allocate_epsilon()
        key = _budget_cache_key(self._schedule, self._epsilon, self._delta, namespace)
        cached = _ALLOCATION_CACHE.get(key)
        if cached is not None:
            return cached
        eps_q = self._allocate_epsilon()
        _ALLOCATION_CACHE[key] = eps_q
        return eps_q

    def _allocate_epsilon(self) -> float:
        """Largest per-query epsilon whose schedule composes within the
        request (guide section 4.3.2), found by fixed-iteration bisection
        over `[_EPS_Q_FLOOR, epsilon]`.

        Two-phase, not a single bisection, for a reason the guide's search
        pseudocode does not anticipate: OpenDP's own `binary_search` for the
        thresholded categorical chain raises (rather than returning a
        value) when no scale/threshold in its search bounds reaches a
        target this small -- confirmed in the build venv, a sufficiently
        small `eps_q` makes the required scale so large that even
        `threshold = i32::MAX` cannot bring delta down to the per-query
        allocation. That failure is a genuine property of the mechanism at
        extreme `eps_q`, not a bug to route around: below some feasibility
        floor, no measurement can be constructed at all. Feasibility is
        monotone in `eps_q` (larger `eps_q` needs less noise, which only
        ever makes the delta bound easier to reach), so:

        1. Bisect for the smallest feasible `eps_q` in the range.
        2. Bisect for the largest `eps_q`, within the now-feasible range,
           whose composed epsilon still fits the request (this is the
           monotone predicate the guide's search literally names).

        Both phases use `_PLD_SEARCH_ITERATIONS`/`_EPS_Q_FLOOR`; nothing
        about the allocation POLICY (largest eps_q admitted by the
        accountant) or the search's fixed-iteration bisection shape
        changes -- this only makes that search robust to an OpenDP search
        failure the guide's single-phase pseudocode does not handle.
        """

        def composed_epsilon_or_none(eps_q: float) -> float | None:
            try:
                certificates = self._certify_schedule(eps_q)
            except _InfeasibleAtEpsQError:
                return None
            return _compose(certificates).get_epsilon_for_delta(self._delta)

        def infeasible() -> DpBudgetError:
            return DpBudgetError(
                code="dp_budget_infeasible",
                message=(
                    f"the requested budget (epsilon={self._epsilon!r}, "
                    f"delta={self._delta!r}) cannot fund a schedule of "
                    f"{self._schedule.query_count} quer"
                    f"{'y' if self._schedule.query_count == 1 else 'ies'}. Raise epsilon or "
                    "delta, or fit fewer columns."
                ),
            )

        # Phase 1: smallest feasible eps_q (monotone: feasible(eps_q) is
        # False below a cutoff, True above; assumes upper=epsilon itself
        # is feasible -- if it is not, no eps_q in range works at all).
        if composed_epsilon_or_none(self._epsilon) is None:
            raise infeasible()
        lo, hi = _EPS_Q_FLOOR, self._epsilon
        for _ in range(_PLD_SEARCH_ITERATIONS):
            mid = (lo + hi) / 2.0
            if composed_epsilon_or_none(mid) is not None:
                hi = mid
            else:
                lo = mid
        feasible_floor = hi

        # Phase 2: within [feasible_floor, epsilon], composed_epsilon is
        # defined and monotone increasing in eps_q; find the largest eps_q
        # whose composed loss still fits the request.
        floor_composed = composed_epsilon_or_none(feasible_floor)
        if floor_composed is None or floor_composed > self._epsilon:
            raise infeasible()

        # C-H2 (Codex HIGH): `x or math.inf` treated a valid composed
        # epsilon of exactly `0.0` as falsy, turning it into infinity and
        # making the predicate false at its own lower bound. `is None` is
        # the real feasibility check.
        def _within_request(e: float) -> bool:
            composed = composed_epsilon_or_none(e)
            return composed is not None and composed <= self._epsilon

        eps_q = _search_largest(_within_request, lower=feasible_floor, upper=self._epsilon)
        composed = composed_epsilon_or_none(eps_q)
        if composed is None or composed > self._epsilon:
            raise infeasible()
        return eps_q

    # -- measurement construction (delegates to `self._backend`) ---------
    #
    # These three methods are thin delegators, never the construction
    # site themselves: `_RealOpenDpBackend` (the default) is where the
    # actual OpenDP calls live, so a test can substitute `self._backend`
    # wholesale without this session's release/admission logic changing
    # at all (guide section 5 step 3).

    def _count_measurement(self, eps_q: float) -> _dp_mod.Measurement:
        return self._backend.count_measurement(eps_q)

    def _numeric_measurement(
        self, eps_q: float, interior_edges: tuple[float, ...]
    ) -> _dp_mod.Measurement:
        return self._backend.numeric_measurement(eps_q, interior_edges)

    def _categorical_measurements(
        self, eps_q: float, delta_alloc: float, carrier: str = "text"
    ) -> tuple[_dp_mod.Measurement, _dp_mod.Measurement]:
        """Select the categorical measurement pair by the column's carrier
        (guide section 3.4). `"text"` keeps the existing str-domain pair --
        behaviorally identical to before (same released values and certificates),
        the only carrier any legacy schedule or test double exercises. `"flag"` takes the bool-domain pair, which
        requires a `_FlagCapableBackend`; a text-only backend on a flag column
        fails closed with a coded error rather than an `AttributeError`. Any
        other carrier is rejected: `categorical + number` has no OpenDP float
        `make_count_by` (guide section 3.3), and an unknown carrier is a
        schedule-construction bug, not a release the session should attempt."""
        if carrier == "text":
            return self._backend.categorical_measurements(eps_q, delta_alloc)
        if carrier == "flag":
            backend = self._backend
            if not isinstance(backend, _FlagCapableBackend):
                raise DpBudgetError(
                    code="dp_carrier_backend_unsupported",
                    message=(
                        "a 'flag' categorical needs a bool-domain-capable backend "
                        "(_FlagCapableBackend); this backend provides only the "
                        "str-domain categorical pair."
                    ),
                )
            return backend.categorical_measurements_flag(eps_q, delta_alloc)
        raise DpBudgetError(
            code="dp_carrier_invalid",
            message=(
                f"categorical carrier {carrier!r} is not releasable; only 'text' "
                "and 'flag' have an OpenDP categorical mechanism (guide section 3.3)."
            ),
        )

    # -- release (data touched here, and only here) ----------------------

    def _admit(self, name: str) -> None:
        """Refuse an unscheduled or already-used query name BEFORE any
        measurement is constructed or invoked (H2 / guide section 4.3.5
        mitigation 2). This is deliberately a separate, PRE-invocation
        step from `_record`: refusing AFTER the mechanism ran is not
        refusing -- the mechanism would already have spent real privacy
        budget that this refusal would then let vanish, never entering
        the ledger. Every `release_*` method below calls this before
        constructing or invoking anything. Reserves `name` (M-2) so a second
        admission fails structurally, not by accident of ordering."""
        if name not in self._schedule.query_names:
            raise DpBudgetError(
                code="dp_unscheduled_release",
                message=(
                    f"query {name!r} is not in this fit's frozen schedule "
                    f"({self._schedule.query_names!r}). OpenDpReleaseSession refuses any "
                    "release outside the schedule fixed at construction."
                ),
            )
        if name in self._releases or name in self._reserved:
            raise DpBudgetError(
                code="dp_duplicate_release",
                message=f"query {name!r} has already released once; a query may release only once.",
            )
        self._reserved.add(name)

    def _record(self, name: str, certificate: Certificate, value: object) -> None:
        """Record an already-invoked release and clear its reservation (M-2).
        Called only AFTER `_admit`; performs no admission check itself."""
        self._reserved.discard(name)
        self._releases[name] = _Release(certificate=certificate, value=value)

    def release_row_count(self, row_count: int) -> int:
        """Row-count query: `make_count >> then_laplace` over a dummy
        constant-string vector with one element per row -- a `make_count`
        release is defined over ANY recordwise vector under
        `symmetric_distance()` (one added/removed row changes the count by
        exactly 1 regardless of the vector's element values), so this is
        the same certified chain as the categorical non-null total, applied
        to the table's own row-projection rather than one column's."""
        name = self._schedule.row_count_name
        self._admit(name)
        measurement = self._count_measurement(self._eps_q)
        certificate = measurement.map(1)  # L-1: read before invoke (never after)
        released = measurement.invoke([""] * row_count)
        self._record(name, certificate, released)
        return int(released)

    def release_numeric(self, name: str, values: list[float]) -> list[int]:
        """One numeric marginal (guide section 4.4). `values` is the
        already clamped, already null-excluded, recordwise projection of
        one column. `interior_edges` come from the SAME `NumericQuerySpec`
        the allocation search certified against (looked up by `name`, not
        re-supplied by the caller), so the certified map(1) and the
        actually-invoked measurement are provably the same chain -- the
        mandated `make_find_bin >> then_count_by_categories >>
        then_laplace` chain, never a bare mechanism over a pre-aggregated
        Python count (binding decision 15)."""
        spec = next((q for q in self._schedule.numeric if q.name == name), None)
        if spec is None:
            raise DpBudgetError(
                code="dp_unscheduled_release",
                message=f"query {name!r} is not a scheduled numeric query.",
            )
        self._admit(name)
        measurement = self._numeric_measurement(self._eps_q, spec.interior_edges)
        certificate = measurement.map(1)  # L-1
        released = measurement.invoke(values)
        self._record(name, certificate, released)
        return list(released)

    def release_categorical(
        self, grouped_name: str, total_name: str, values: list[Any]
    ) -> tuple[dict[Any, int], int]:
        """One categorical column's pair of releases (guide section 4.5):
        the thresholded unknown-key grouped count (`make_count_by >>
        then_laplace_threshold`) and the noised non-null total
        (`make_count >> then_laplace`), each its own scheduled query with
        its own certificate. `values` is the already normalized,
        null-excluded projection of one column (str for a `text` carrier,
        bool for a `flag` carrier). Both names are admitted BEFORE either
        measurement is constructed (H2): a refusal on `total_name` must not
        leave `grouped_name` already invoked.

        The column's carrier is read from its scheduled `CategoricalQuerySpec`
        (guide section 3.4), never re-supplied by the caller, so the domain the
        measurements are built over is provably the same the schedule committed
        to and the allocation search certified against."""
        self._admit(grouped_name)
        self._admit(total_name)
        spec = next(
            (
                c
                for c in self._schedule.categorical
                if c.grouped_name == grouped_name and c.total_name == total_name
            ),
            None,
        )
        if spec is None:
            # Both names were admitted individually, but they do not resolve to
            # ONE scheduled categorical spec, so the carrier is unknown. Fail
            # closed with a coded error rather than silently defaulting to the
            # str/text domain (which would only surface later as a bare OpenDP
            # TypeError on a genuine flag column).
            raise DpBudgetError(
                code="dp_budget_categorical_pair_unscheduled",
                message=(
                    f"no scheduled categorical query pairs grouped={grouped_name!r} "
                    f"with total={total_name!r}; cannot determine the carrier"
                ),
            )
        carrier = spec.carrier
        grouped_meas, total_meas = self._categorical_measurements(
            self._eps_q, self._delta_per_categorical, carrier
        )
        grouped_certificate = grouped_meas.map(1)  # L-1
        grouped_released = grouped_meas.invoke(values)
        self._record(grouped_name, grouped_certificate, grouped_released)
        total_certificate = total_meas.map(1)  # L-1
        total_released = total_meas.invoke(values)
        self._record(total_name, total_certificate, total_released)
        return dict(grouped_released), int(total_released)

    # -- composition and receipt ------------------------------------------

    def certificate_count(self) -> int:
        return len(self._releases)

    def composed_loss(self) -> tuple[float, float]:
        """`(epsilon_total, delta_total)` per guide section 4.3.4. Raises
        `DpBudgetError` unless every scheduled query has released exactly
        once (section 4.3.5 mitigation 3) and the composed result is
        finite (a non-finite result means the composed mechanism's delta
        floor exceeds the requested delta -- a real failure, not an
        infinity to report)."""
        missing = [n for n in self._schedule.query_names if n not in self._releases]
        if missing:
            raise DpBudgetError(
                code="dp_schedule_incomplete",
                message=(
                    f"cannot report a fit-wide loss: {len(missing)} scheduled quer"
                    f"{'y has' if len(missing) == 1 else 'ies have'} not released yet "
                    f"({missing!r})."
                ),
            )
        certificates = [self._releases[n].certificate for n in self._schedule.query_names]
        composed = _compose(certificates)
        epsilon_total = composed.get_epsilon_for_delta(self._delta)
        if not math.isfinite(epsilon_total):
            raise DpBudgetError(
                code="dp_budget_infeasible",
                message=(
                    "the composed mechanism's delta floor exceeds the requested delta "
                    f"({self._delta!r}); no finite epsilon_total exists at this delta."
                ),
            )
        if epsilon_total > self._epsilon:
            raise DpBudgetError(
                code="dp_budget_infeasible",
                message=(
                    f"composed epsilon_total={epsilon_total!r} exceeds the requested "
                    f"epsilon={self._epsilon!r}."
                ),
            )
        return epsilon_total, self._delta
