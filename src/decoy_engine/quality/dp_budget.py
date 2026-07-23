"""OpenDP release session and cross-library privacy accounting (DPS Scope B).

`OpenDpReleaseSession` is the SOLE construction and invocation site for
OpenDP measurements in this codebase (binding decision 15, guide section
4.3.5 mitigation 1): `quality/dp.py` drives a fit entirely through this
session and never imports `opendp` itself. That is what makes "every
column's mechanism was reached through the mandated chain" a checkable
property rather than a convention.

Two libraries split responsibility for one fit's privacy accounting
(guide section 3.3), and the split is deliberate:

- **OpenDP certifies each column.** Every scheduled query is one chained
  `Transformation >> Measurement` built and invoked here. `Measurement.
  map(d_in)` (with `d_in = 1` under `symmetric_distance()`, i.e. one added
  or removed row) is what reports that release's privacy loss -- the
  certificate is read back from the exact object invoked, never assumed
  from the calibration target.
- **`dp_accounting` composes those certificates.** Each certified
  `(epsilon_i, delta_i)` becomes a dominating-pair privacy loss
  distribution (`dp_accounting.pld.privacy_loss_distribution.
  from_privacy_parameters`); composing them and reading
  `get_epsilon_for_delta` at the fit's requested delta gives the fit-wide
  loss. This is the correct constructor because a caller here knows each
  OpenDP measurement only by its certified `(epsilon, delta)`, not by a
  tighter mechanism-specific PLD -- the composed result is a valid, if
  looser, upper bound (guide section 3.3).

Source patterns (cited per CLAUDE.md's "use established methodology"
rule): OpenDP's [transformation user guide](
https://docs.opendp.org/en/stable/api/user-guide/transformations/index.html)
and [thresholded noise mechanisms](
https://docs.opendp.org/en/stable/api/user-guide/measurements/
thresholded-noise-mechanisms.html) for the chain shapes; OpenDP's
[parameter search utilities](
https://docs.opendp.org/en/stable/api/user-guide/utilities/
parameter-search.html) (`dp.binary_search`) for calibrating a scale/
threshold to a target certified loss without inverting a mechanism
formula; `dp_accounting.pld.privacy_loss_distribution.
from_privacy_parameters`, the dominating-pair construction for a
mechanism known only by its `(epsilon, delta)` (guide section 3.2/3.3).

What this module does NOT do, and it is exactly this much (guide section
3.3 "what Decoy owns"): no epsilon, delta, noise scale, or threshold is
computed from a Decoy formula. The one arithmetic Decoy performs is the
budget-allocation policy (guide section 4.3.2), a pure function of the
request and the public query counts, never of a mechanism's output.

What dropping OpenDP's Polars-integrated `Context` compositor costs
(guide section 4.3.5, spike closed in `docs/plans/
2026-07-22-dps-scope-b-spike-result.md`): the Context used to refuse an
unscheduled query itself, at the library boundary. Without it, nothing
outside Decoy stops a code path from constructing and invoking an OpenDP
measurement without registering a certificate. `OpenDpReleaseSession` is
the mitigation: refusing an unscheduled or duplicate query name, refusing
to report a loss before every scheduled query has released, and (at the
call site in `quality/dp.py`) asserting the certificate count equals the
schedule length before serialization.

`ReleaseLedger` (previously `PrivacyBudget`; now `quality/dp_ledger.py`,
a size-cap split, not a design change) is NOT this session: it is the
plan-compile-time policy ceiling (`plan/_checks_dp.py`) that sums
already-certified release totals across distinct release IDs, never a
second mechanism accountant. `NumericQuerySpec`/`CategoricalQuerySpec`/
`Schedule` (guide section 4.3.1) similarly live in `quality/dp_schedule.
py`, re-imported here since this session builds/enforces/certifies
against them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import opendp.prelude as _dp

from decoy_engine.quality.dp_schedule import Schedule

if TYPE_CHECKING:
    import opendp.mod as _dp_mod

# The single feature enablement site for the whole DP path (guide section
# 9.1 / 3.2 item 2): every constructor this build uses is reachable under
# `contrib` alone (verified in the build venv, section 3.4); enabling
# `honest-but-curious` is never required and never done.
_dp.enable_features("contrib")

# `opendp.transformations` / `opendp.measurements` are imported lazily
# inside methods below purely to keep the module-level surface small and
# the "what this module imports" story legible in one place (the `_dp`
# import above is the only module-level OpenDP import); there is no
# import-cycle reason for the deferral. Both are pure-Python re-exports of
# the same `opendp` package `_dp` already enabled `contrib` on.

_PLD_DISCRETIZATION = 1e-4  # dp_accounting.pld dominating-pair discretization interval
_PLD_SEARCH_ITERATIONS = 40  # fixed-iteration bisection depth for eps_q search (section 4.3.2)
_EPS_Q_FLOOR = 1e-9  # per-query epsilon floor; below this a schedule cannot be certified
_I32_MAX = 2**31 - 1  # make_laplace_threshold's threshold argument is i32 (section 3.4)
_SCALE_SEARCH_BOUNDS = (1e-12, 1e12)

# A certified privacy loss is either a scalar epsilon (pure-epsilon release,
# e.g. a Laplace count/histogram) or an (epsilon, delta) pair (a thresholded
# release). One uniform representation enters composition either way
# (section 3.3): every certificate becomes a dp_accounting PLD via
# `from_privacy_parameters`.
Certificate = float | tuple[float, float]


class _InfeasibleAtEpsQError(Exception):
    """Internal signal: no scale/threshold within OpenDP's search bounds
    calibrates a measurement to the requested per-query epsilon/delta at
    THIS eps_q. Caught only by the allocation search (`_allocate_epsilon`),
    which treats it as "this eps_q does not work, try a smaller one" --
    OpenDP's own `binary_search` raises a bare `ValueError` for both "no
    crossing point in bounds" and "threshold not representable by i32"
    (guide section 3.4); either means this eps_q is not achievable, not
    that the fit is broken."""


class DpBudgetError(Exception):
    """A DP fit's schedule or budget could not be satisfied or was violated.
    Machine-readable code, mirroring `quality.dp.DpError`'s shape."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _certificate_to_pld(certificate: Certificate):
    from dp_accounting.pld import common
    from dp_accounting.pld import privacy_loss_distribution as pldist

    epsilon, delta = (certificate, 0.0) if isinstance(certificate, float) else certificate
    return pldist.from_privacy_parameters(
        common.DifferentialPrivacyParameters(epsilon, delta),
        value_discretization_interval=_PLD_DISCRETIZATION,
    )


def _compose(certificates: list[Certificate]):
    """Compose every certificate through `dp_accounting.pld`. Returns the
    composed `PrivacyLossDistribution`; the caller reads
    `get_epsilon_for_delta(delta)` off it."""
    plds = [_certificate_to_pld(c) for c in certificates]
    composed = plds[0]
    for pld in plds[1:]:
        composed = composed.compose(pld)
    return composed


def _search_largest(predicate, *, lower: float, upper: float) -> float:
    """Fixed-iteration bisection for the largest `x` in `[lower, upper]`
    where `predicate(x)` holds, assuming `predicate` is monotone
    non-increasing in `x`. Returns `lower` (the floor) if the predicate
    fails everywhere in the range -- the caller (guide section 4.3.2) is
    responsible for treating that as budget-infeasible."""
    if not predicate(lower):
        return lower
    lo, hi = lower, upper
    for _ in range(_PLD_SEARCH_ITERATIONS):
        mid = (lo + hi) / 2.0
        if predicate(mid):
            lo = mid
        else:
            hi = mid
    return lo


@dataclass
class _Release:
    certificate: Certificate
    value: object


class _OpenDpBackend(Protocol):
    """Seam for mechanism-level test doubles (guide section 5 step 3).

    Production always uses `_RealOpenDpBackend`, the only implementation
    that ever runs outside a test: it builds and invokes real OpenDP
    `Transformation >> Measurement` chains under `contrib` (section 9.1).
    A test double supplies released values and a certificate that comes
    from a `.map()`-shaped object on the double itself (`_FakeMeasurement`
    below); it must never fabricate an epsilon or delta out of thin air,
    because the property BLOCKER A2 pins -- "the recorded certificate is
    `measurement.map(1)` on the object actually invoked, never the
    calibration target" -- is only checkable if the double's `.map()` is
    itself the source of truth the test asserts against.
    """

    def count_measurement(self, eps_q: float) -> Any: ...

    def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]) -> Any: ...

    def categorical_measurements(self, eps_q: float, delta_alloc: float) -> tuple[Any, Any]: ...


class _RealOpenDpBackend:
    """The sole `_OpenDpBackend` implementation used outside tests: builds
    and invokes real OpenDP chains under `contrib` only (guide sections
    4.4/4.5, verified end to end in the build venv per section 3.4)."""

    def count_measurement(self, eps_q: float) -> _dp_mod.Measurement:
        """`make_count >> then_laplace`, shared by the row-count query and
        the categorical non-null-total query (guide section 4.5): both are
        a plain count over a recordwise string-typed projection under
        `symmetric_distance()`."""
        import opendp.measurements as meas
        import opendp.transformations as tf

        domain = _dp.vector_domain(_dp.atom_domain(T=str))
        metric = _dp.symmetric_distance()
        counter = tf.make_count(domain, metric, TO=int)
        scale = _dp.binary_search(
            lambda s: (counter >> meas.then_laplace(scale=s)).map(1) <= eps_q,
            bounds=_SCALE_SEARCH_BOUNDS,
        )
        return counter >> meas.then_laplace(scale=scale)

    def numeric_measurement(
        self, eps_q: float, interior_edges: tuple[float, ...]
    ) -> _dp_mod.Measurement:
        import opendp.measurements as meas
        import opendp.transformations as tf

        domain = _dp.vector_domain(_dp.atom_domain(T=float, nan=False))
        metric = _dp.symmetric_distance()
        # Interior edges only: `bins` categories need `bins - 1` interior
        # cut points (section 4.4), which makes `make_find_bin`'s category
        # range exactly 0..bins-1 with no overflow bin. The SAME edges are
        # used here (calibration/certification) and at release time -- no
        # placeholder shape, so the certified map(1) is the actual
        # release's certificate, not a structurally-similar stand-in.
        numeric_bins = len(interior_edges) + 1
        transformation = tf.make_find_bin(domain, metric, edges=list(interior_edges)) >> (
            tf.then_count_by_categories(categories=list(range(numeric_bins)), null_category=False)
        )
        scale = _dp.binary_search(
            lambda s: (transformation >> meas.then_laplace(scale=s)).map(1) <= eps_q,
            bounds=_SCALE_SEARCH_BOUNDS,
        )
        return transformation >> meas.then_laplace(scale=scale)

    def categorical_measurements(
        self, eps_q: float, delta_alloc: float
    ) -> tuple[_dp_mod.Measurement, _dp_mod.Measurement]:
        import opendp.measurements as meas
        import opendp.transformations as tf

        cat_domain = _dp.vector_domain(_dp.atom_domain(T=str))
        metric = _dp.symmetric_distance()
        count_by = tf.make_count_by(cat_domain, metric)

        def chain(scale: float, threshold: float):
            # `dp.binary_search`'s stub always types its predicate/return as
            # `float` regardless of `T=int` (the runtime value IS an int
            # under `T=int`, per OpenDP's own i32 threshold contract --
            # the stub just doesn't express a T-dependent return type), so
            # the cast here is a type-only correction, not a behavior change.
            return count_by >> meas.then_laplace_threshold(scale=scale, threshold=int(threshold))

        scale = _dp.binary_search(
            lambda s: chain(s, _I32_MAX).map(1)[0] <= eps_q,
            bounds=_SCALE_SEARCH_BOUNDS,
        )
        threshold = _dp.binary_search(
            lambda t: chain(scale, t).map(1)[1] <= delta_alloc,
            bounds=(1, _I32_MAX),
            T=int,
        )
        grouped = chain(scale, threshold)
        total = self.count_measurement(eps_q)
        return grouped, total


@dataclass(frozen=True)
class _FakeMeasurement:
    """Test double for a certified OpenDP `Measurement` (guide section 5
    step 3). `.map(d_in)` returns a FIXED, already-certified value --
    never computed from `d_in`, and never the calibration target a
    session happened to search for -- and `.invoke(values)` returns a
    FIXED released value regardless of `values` (or one derived by
    `released_fn`, for a double whose release must still vary with its
    input shape). This replaces the MEASUREMENT OBJECT a session invokes;
    it never seeds or replaces production randomness, and it never
    supplies a certificate that didn't come from a `.map()`-shaped source
    -- the double simply IS that source, by construction."""

    certificate: Certificate
    released: Any = None
    released_fn: Any = None  # Callable[[Any], Any] | None

    def map(self, d_in: int) -> Certificate:
        del d_in
        return self.certificate

    def invoke(self, values: Any) -> Any:
        if self.released_fn is not None:
            return self.released_fn(values)
        return self.released


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
        self._eps_q = self._allocate_epsilon()

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
            for _q in self._schedule.categorical:
                grouped, total = self._categorical_measurements(eps_q, self._delta_per_categorical)
                certificates.append(grouped.map(1))
                certificates.append(total.map(1))
        except ValueError as exc:
            raise _InfeasibleAtEpsQError(str(exc)) from exc
        return certificates

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
        eps_q = _search_largest(
            lambda e: (composed_epsilon_or_none(e) or math.inf) <= self._epsilon,
            lower=feasible_floor,
            upper=self._epsilon,
        )
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
        self, eps_q: float, delta_alloc: float
    ) -> tuple[_dp_mod.Measurement, _dp_mod.Measurement]:
        return self._backend.categorical_measurements(eps_q, delta_alloc)

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
        self, grouped_name: str, total_name: str, values: list[str]
    ) -> tuple[dict[str, int], int]:
        """One categorical column's pair of releases (guide section 4.5):
        the thresholded unknown-key grouped count (`make_count_by >>
        then_laplace_threshold`) and the noised non-null total
        (`make_count >> then_laplace`), each its own scheduled query with
        its own certificate. `values` is the already normalized,
        null-excluded projection of one column. Both names are admitted
        BEFORE either measurement is constructed (H2): a refusal on
        `total_name` must not leave `grouped_name` already invoked."""
        self._admit(grouped_name)
        self._admit(total_name)
        grouped_meas, total_meas = self._categorical_measurements(
            self._eps_q, self._delta_per_categorical
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
