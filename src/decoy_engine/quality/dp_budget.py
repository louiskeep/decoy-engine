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

import importlib.metadata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

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

# A single CONCRETE conservative ceiling on the requested fit-wide epsilon
# (guide section 4, comprehensive-review budget-calibration finding). The PLD
# exponential overflows at ~709.783 on py3.10 (709.782 still OK) and the exact
# boundary drifts by Python / dp_accounting version, so the ceiling is frozen
# well below every certified manifest row's observed overflow -- NOT probed at
# build time. A requested ceiling this large is already far outside any real DP
# regime; requests above it must fail closed with a coded error BEFORE any
# private data is read, never surface as a raw OverflowError mid-composition.
# The dependency-matrix CI workflow asserts, per manifest row, that this ceiling
# composes without overflow AND that the row's documented overflow point stays
# above it (a boundary probe that fails the build if a version bump moves the
# overflow at or below 700.0, forcing a deliberate re-pin).
_DP_EPSILON_CEILING = 700.0

# The exact versions of the two accounting libraries whose behaviour the
# calibration/composition result depends on (guide section 4, frozen cache
# key). They enter the budget cache key so a cached scalar allocation is never
# reused across a version bump that could move a calibration or a composition.
_OPENDP_VERSION = importlib.metadata.version("opendp")
_DP_ACCOUNTING_VERSION = importlib.metadata.version("dp-accounting")


def check_epsilon_supported(epsilon: float) -> None:
    """Fail-closed guard for the requested fit-wide epsilon ceiling (guide
    section 4). Raises a coded ``dp_epsilon_unsupported`` for any request above
    ``_DP_EPSILON_CEILING`` (and for a NaN request, which cannot be certified),
    so the fit refuses before reading private data instead of hitting a raw
    ``OverflowError`` deep in PLD composition.

    Wired into the composition path at ``OpenDpReleaseSession`` construction
    (DPS-CODEC phase 4): the session calls this BEFORE the allocation search
    runs any ``dp_accounting`` composition, so an over-ceiling request fails
    with this coded error rather than an ``OverflowError`` mid-search. It does
    not validate positivity -- that stays with the existing ceiling parse --
    only the upper bound."""
    # `not (epsilon <= ceiling)` (rather than `epsilon > ceiling`) so a NaN
    # request, for which every comparison is False, also fails closed.
    if not (epsilon <= _DP_EPSILON_CEILING):
        raise DpBudgetError(
            code="dp_epsilon_unsupported",
            message=(
                f"requested epsilon {epsilon!r} exceeds the supported ceiling "
                f"{_DP_EPSILON_CEILING!r}; a budget this large is outside the "
                "certified DP regime and would overflow PLD composition"
            ),
        )


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


def _binary_search_endpoint_aware(predicate: Any, *, bounds: tuple[Any, Any], T: Any = None) -> Any:
    """`dp.binary_search` calibrated to a crossing, made robust to a predicate
    that has NO crossing because it already holds at an endpoint (guide section
    4, comprehensive-review budget-calibration finding: "check BOTH
    ``binary_search`` endpoints; treat lower-endpoint-satisfies as feasible").

    OpenDP's ``binary_search`` locates the boundary between where a monotone
    predicate fails and where it holds, and RAISES ``ValueError`` when the
    predicate does not change truth value inside ``bounds``. A non-crossing is
    not always infeasible: when the predicate holds across the whole range it
    is satisfied at the LOWER endpoint, which -- for these calibration searches
    (smallest scale / smallest threshold that meets the target) -- is exactly
    the answer ``binary_search`` would return for "smallest satisfying x". So on
    a ``ValueError``: return the lower endpoint if the predicate holds there
    (the least-noise feasible calibration), else the upper endpoint if it holds
    there, else re-raise -- the predicate genuinely cannot be met anywhere in
    bounds, which the allocation search (section 4.3.2) reads as infeasible.

    The normal calibration regime (a real crossing inside bounds) never reaches
    the ``except`` branch, so this returns EXACTLY what the bare ``binary_search``
    would for every currently-calibrating fit; it only rescues the
    endpoint-feasible case that the bare call would have raised on."""
    try:
        if T is not None:
            return _dp.binary_search(predicate, bounds=bounds, T=T)
        return _dp.binary_search(predicate, bounds=bounds)
    except ValueError:
        lower, upper = bounds
        if predicate(lower):
            return lower
        if predicate(upper):
            return upper
        raise


# --- budget calibration cache (guide section 4, allocation-cost finding) ----
#
# The allocation search (`_allocate_epsilon`) re-certifies the whole schedule
# at many trial eps_q values, which is the fit's dominant compile-time cost. Two
# fits over the IDENTICAL public (schedule, epsilon, delta) on the IDENTICAL
# accounting-library versions allocate the IDENTICAL scalar eps_q -- the search
# is a pure function of exactly those inputs and never of any value. So the
# scalar allocation RESULT is cacheable. The cache stores ONLY that scalar
# (never a Measurement and never a certificate -- those must always be read from
# the object actually invoked, guide section 4/BLOCKER A2); a cache hit skips the
# search but release-time still rebuilds and re-certifies every measurement.
_ALLOCATION_CACHE: dict[tuple[object, ...], float] = {}

# The stable production cache namespace. `_RealOpenDpBackend` is the only
# backend that ever runs a real fit, and it is stateless + deterministic, so its
# calibration is safe to share across sessions/fits under one fixed namespace.
# Test doubles do NOT carry a namespace (they leave `cache_namespace` unset),
# which routes them past the cache entirely -- a stateful/fake backend must
# never read a scalar another session computed (guide section 4: "fake/stateful
# test backends bypass the cache or use an instance-unique token").
_REAL_BACKEND_CACHE_NAMESPACE = "real-opendp-contrib"


def _budget_cache_key(
    schedule: Schedule, epsilon: float, delta: float, namespace: str
) -> tuple[object, ...]:
    """The frozen budget cache key (guide section 4): the carrier-bearing
    schedule signature (which already carries edges/bins and each categorical's
    carrier), the requested epsilon and delta, the exact OpenDP and
    dp_accounting versions, and the backend cache namespace. Any drift in any
    component -- including a column's carrier -- yields a different key, so a
    cached scalar can only be reused for a provably identical calibration."""
    return (
        schedule.signature(),
        float(epsilon),
        float(delta),
        _OPENDP_VERSION,
        _DP_ACCOUNTING_VERSION,
        namespace,
    )


def _clear_budget_cache() -> None:
    """Drop every cached scalar allocation. Test-only helper for isolating the
    module-level cache between cases; production never clears it."""
    _ALLOCATION_CACHE.clear()


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


@runtime_checkable
class _FlagCapableBackend(Protocol):
    """The extra capability a backend needs to serve a `flag`-carrier
    categorical (guide section 3.4): the bool-domain measurement pair.

    Kept SEPARATE from `_OpenDpBackend` (not merged into it) on purpose: the
    existing str/number test doubles implement only the str-domain methods, and
    the str/number release path must stay behaviorally unchanged (same released
    values and certificates) -- adding a
    required method to `_OpenDpBackend` would break every one of those doubles
    under structural typing. The flag path is reached only for a `flag` carrier,
    where `OpenDpReleaseSession` narrows the backend to this protocol first
    (`runtime_checkable`, so a text-only double fails closed with a coded error
    rather than an `AttributeError`). `_RealOpenDpBackend`, the sole production
    backend, implements both protocols."""

    def categorical_measurements_flag(
        self, eps_q: float, delta_alloc: float
    ) -> tuple[Any, Any]: ...


class _RealOpenDpBackend:
    """The sole `_OpenDpBackend` implementation used outside tests: builds
    and invokes real OpenDP chains under `contrib` only (guide sections
    4.4/4.5, verified end to end in the build venv per section 3.4). It also
    implements `_FlagCapableBackend` (the bool-domain categorical pair)."""

    # Stable production namespace (guide section 4): a stateless, deterministic
    # backend may share its scalar calibration cache across every fit.
    cache_namespace = _REAL_BACKEND_CACHE_NAMESPACE

    def _count_over_domain(self, eps_q: float, domain: Any) -> _dp_mod.Measurement:
        """`make_count >> then_laplace` over a caller-chosen atom domain. A
        `make_count` release is defined over ANY recordwise vector under
        `symmetric_distance()` (one added/removed row changes the count by
        exactly 1 regardless of element type), so the same certified chain
        serves the str-domain projections (row-count, text non-null total) and
        the bool-domain non-null total for a `flag` column (guide section 3.4)."""
        import opendp.measurements as meas
        import opendp.transformations as tf

        counter = tf.make_count(domain, _dp.symmetric_distance(), TO=int)
        scale = _binary_search_endpoint_aware(
            lambda s: (counter >> meas.then_laplace(scale=s)).map(1) <= eps_q,
            bounds=_SCALE_SEARCH_BOUNDS,
        )
        return counter >> meas.then_laplace(scale=scale)

    def count_measurement(self, eps_q: float) -> _dp_mod.Measurement:
        """`make_count >> then_laplace`, shared by the row-count query and
        the categorical non-null-total query (guide section 4.5): both are
        a plain count over a recordwise string-typed projection under
        `symmetric_distance()`."""
        return self._count_over_domain(eps_q, _dp.vector_domain(_dp.atom_domain(T=str)))

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
        scale = _binary_search_endpoint_aware(
            lambda s: (transformation >> meas.then_laplace(scale=s)).map(1) <= eps_q,
            bounds=_SCALE_SEARCH_BOUNDS,
        )
        return transformation >> meas.then_laplace(scale=scale)

    def _grouped_over_domain(
        self, eps_q: float, delta_alloc: float, atom: Any
    ) -> _dp_mod.Measurement:
        """The thresholded grouped count `make_count_by(T) >>
        then_laplace_threshold` over a caller-chosen atom domain (guide section
        4.5): `T=str` for a `text` carrier, `T=bool` for a `flag` carrier
        (`make_count_by(bool)` probe-confirmed constructible in OpenDP 0.15.1).
        The scale and threshold are calibrated exactly as before, now through
        the endpoint-aware search."""
        import opendp.measurements as meas
        import opendp.transformations as tf

        cat_domain = _dp.vector_domain(atom)
        metric = _dp.symmetric_distance()
        count_by = tf.make_count_by(cat_domain, metric)

        def chain(scale: float, threshold: float) -> _dp_mod.Measurement:
            # `dp.binary_search`'s stub always types its predicate/return as
            # `float` regardless of `T=int` (the runtime value IS an int
            # under `T=int`, per OpenDP's own i32 threshold contract --
            # the stub just doesn't express a T-dependent return type), so
            # the cast here is a type-only correction, not a behavior change.
            return count_by >> meas.then_laplace_threshold(scale=scale, threshold=int(threshold))

        scale = _binary_search_endpoint_aware(
            lambda s: chain(s, _I32_MAX).map(1)[0] <= eps_q,
            bounds=_SCALE_SEARCH_BOUNDS,
        )
        threshold = _binary_search_endpoint_aware(
            lambda t: chain(scale, t).map(1)[1] <= delta_alloc,
            bounds=(1, _I32_MAX),
            T=int,
        )
        return chain(scale, threshold)

    def categorical_measurements(
        self, eps_q: float, delta_alloc: float
    ) -> tuple[_dp_mod.Measurement, _dp_mod.Measurement]:
        """The str-domain (`text` carrier) grouped + non-null-total pair. The
        legacy categorical path -- unchanged behaviour."""
        grouped = self._grouped_over_domain(eps_q, delta_alloc, _dp.atom_domain(T=str))
        total = self.count_measurement(eps_q)
        return grouped, total

    def categorical_measurements_flag(
        self, eps_q: float, delta_alloc: float
    ) -> tuple[_dp_mod.Measurement, _dp_mod.Measurement]:
        """The bool-domain (`flag` carrier) grouped + non-null-total pair
        (guide section 3.4). Same two-measurement categorical shape as the str
        path, but both halves are built over `atom_domain(T=bool)`:
        `make_count_by(bool)` for the thresholded grouped count and
        `make_count(bool)` for the non-null total. The str-domain
        `atom_domain(T=str)` cannot type a boolean vector ("inferred bool,
        expected String"), which is exactly why a `flag` column needs this
        variant. Both constructors are probe-confirmed constructible and
        composable in OpenDP 0.15.1 / dp_accounting 0.6.0."""
        grouped = self._grouped_over_domain(eps_q, delta_alloc, _dp.atom_domain(T=bool))
        total = self._count_over_domain(eps_q, _dp.vector_domain(_dp.atom_domain(T=bool)))
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


# `OpenDpReleaseSession` was split into `dp_session.py` on a size-cap crossing
# (CLAUDE.md's ~600-LOC cap). Re-export it here so the documented
# `decoy_engine.quality.dp_budget.OpenDpReleaseSession` path (`quality/dp.py`,
# the DP test suite) resolves unchanged.
if TYPE_CHECKING:
    pass


def __getattr__(name: str) -> Any:
    # Lazy re-export, NOT an eager bottom-of-module import: `dp_session` imports
    # the backend/protocols/calibration primitives defined ABOVE from this
    # module, so importing `dp_session` FIRST re-enters `dp_budget` and an eager
    # `dp_session.OpenDpReleaseSession` read here would touch a still-initializing
    # module (AttributeError, masked whenever `dp_budget` happens to import
    # first). Defer the import to first attribute access, by which point
    # `dp_session` is fully initialized regardless of which module was imported
    # first, breaking the cycle.
    if name == "OpenDpReleaseSession":
        from decoy_engine.quality.dp_session import OpenDpReleaseSession

        return OpenDpReleaseSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
