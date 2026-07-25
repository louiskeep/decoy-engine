"""Property + metamorphic invariants for DP privacy-loss composition
(`quality/dp_budget.py`, DPS Scope B).

TQ-crown-jewels pilot follow-on module. `_compose` / `_certificate_to_pld`
are the sole `dp_accounting` call site: every certified per-column
`(epsilon, delta)` OpenDP certificate reaches the fit-wide privacy-loss
total ONLY through these two functions (module docstring, section 3.3:
"`dp_accounting` composes those certificates"). A wrong composition here
silently understates or misreports the actual privacy loss of a release,
which is the worst-blast-radius failure mode this module owns -- exactly
the DP analogue of the RI-graph pilot's "wrong mask order corrupts FK
integrity" (`tests/property/test_ri_graph_invariants.py`). `check_epsilon_
supported` is the module's other budget-accounting primitive: the
fail-closed ceiling guard a fit must clear before any composition search
runs at all.

The invariants come from `dp_accounting.pld`'s own composition contract
(dominating-pair privacy loss distributions, guide section 3.3) and from
Dwork & Roth, *The Algorithmic Foundations of Differential Privacy* (2014),
Theorem 3.16 (basic/sequential composition: k mechanisms each
`(epsilon_i, delta_i)`-DP compose to at most `(sum(epsilon_i),
sum(delta_i))`-DP) -- cited in this repo's own DP survey docs (`docs/plans/
2026-07-22-dps-established-library-survey.md`, `docs/backlog/
dps-dp-synthetic-implementation.md`). `dp_accounting.pld`'s dominating-pair
construction is an exact realization of that composition for a mechanism
known only by its worst-case `(epsilon, delta)` (guide section 3.2/3.3), so
composing k PURE-epsilon (delta=0) certificates at delta=0 should recover
the Theorem 3.16 sum, up to the library's own PLD grid-discretization
error (`_PLD_DISCRETIZATION = 1e-4`, `dp_budget.py`). Empirical probing
(see this file's tolerance constants) confirms `dp_accounting` rounds that
discretization error in the CONSERVATIVE direction: the composed epsilon
is never observed below the naive sum, only slightly above it -- the
correct direction for a privacy accountant (it must never UNDER-report a
release's true privacy loss).

- MONOTONICITY: composing one more mechanism, or raising one column's
  epsilon, never DECREASES the composed total at a fixed target delta.
- NON-NEGATIVITY / bounds: a finite composed epsilon at delta=0 for
  finite, non-negative certificates is itself finite, non-negative, and
  never NaN.
- DETERMINISM: `_compose` is a pure function of its certificate list; the
  same list composes to the identical epsilon at the same target delta on
  every call.
- ORDER-INDEPENDENCE (metamorphic): `dp_accounting.pld`'s `.compose()` is
  commutative (each PLD is an independent random variable's loss
  distribution; convolution of independent distributions does not depend
  on order), so shuffling the certificate list must not change the
  composed epsilon at a shared feasible target delta.
- ADDITIVITY baseline: pure-epsilon (delta=0) composition at delta=0
  recovers Theorem 3.16's naive sum, within the discretization tolerance
  above, and never falls meaningfully below it (the conservative
  direction).
- EMPTY / SINGLE: one certificate composes to its own certified loss, up
  to one PLD grid cell (`_compose` returns `plds[0]` untouched -- no
  COMPOSITION discretization loss -- but `from_privacy_parameters` itself
  still snaps to its discretization grid on construction). Zero certificates is NOT a
  documented base case -- see `test_composing_zero_certificates_is_
  unreachable_in_production` for why that gap is not a mutation-bar item.
- BUDGET-EXCEEDED refusal: `check_epsilon_supported` fails closed with the
  coded `dp_epsilon_unsupported` error for any request above
  `_DP_EPSILON_CEILING` (or NaN), before any `dp_accounting` composition
  runs.

Run:  pytest tests/property/test_dp_budget_invariants.py -q
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from decoy_engine.quality.dp_budget import (
    _DP_EPSILON_CEILING,
    Certificate,
    DpBudgetError,
    _compose,
    check_epsilon_supported,
)

# Match the pilot property suite's audit profile (tests/property/
# test_ri_graph_invariants.py): more examples than the 100-example default,
# no deadline (dp_accounting PLD composition is more expensive per call
# than a graph build, and Hypothesis shrinking can trip the 200ms wall),
# print_blob so any counterexample is replayable.
settings.register_profile(
    "audit",
    max_examples=300,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")

# Per-column epsilon kept well under `_DP_EPSILON_CEILING` (700.0): even at
# the strategy's max list size (6) and max per-column epsilon (5.0), the
# naive sum tops out at 30.0, nowhere near the ~709.78 PLD overflow point
# the ceiling guards against -- these tests exercise composition, not the
# ceiling/overflow boundary (that is `TestEpsilonCeiling` in
# tests/unit/quality/test_dp_budget.py).
_MAX_COLUMN_EPSILON = 5.0
_MAX_COLUMN_DELTA = 1e-3
_MAX_CERTS = 6

# Additivity tolerance: empirical probing (5000 random (n<=6, epsilon in
# [1e-6, 5.0]) pure-epsilon compositions) found the composed epsilon at
# delta=0 exceeds the Theorem 3.16 naive sum by at most ~6e-4 and is NEVER
# observed below it. This bound is a generous multiple of that observed
# worst case, not a re-derivation of `_PLD_DISCRETIZATION` -- the oracle
# stays independent of the module's internal discretization constant.
_ADDITIVITY_TOL = 0.05
# A composed epsilon must never fall meaningfully below the naive sum
# (the conservative direction); this is float-noise-only slack.
_UNDER_SUM_TOL = 1e-6
# General cross-call/metamorphic float-noise tolerance (order-independence,
# determinism, monotonicity comparisons): observed float-noise deltas
# between differently-ordered `.compose()` call chains were ~1e-13.
_NOISE_TOL = 1e-6
# A LONE certificate's own grid snap on construction (`from_privacy_
# parameters` discretizes to the PLD's `value_discretization_interval`,
# 1e-4, even with no `.compose()` call): empirically up to half a grid cell
# either side (e.g. epsilon=1.28125 -> 1.2813). Two full grid cells is a
# generous margin over that observed half-cell snap.
_SINGLE_CERT_TOL = 2e-4


def _finite_epsilon(
    min_value: float = 1e-6, max_value: float = _MAX_COLUMN_EPSILON
) -> st.SearchStrategy[float]:
    return st.floats(
        min_value=min_value, max_value=max_value, allow_nan=False, allow_infinity=False
    )


def _finite_delta(max_value: float = _MAX_COLUMN_DELTA) -> st.SearchStrategy[float]:
    return st.floats(min_value=0.0, max_value=max_value, allow_nan=False, allow_infinity=False)


@st.composite
def pure_epsilon_certs(
    draw: st.DrawFn, min_size: int = 1, max_size: int = _MAX_CERTS
) -> list[float]:
    """A list of bare-float certificates (`Certificate`'s `float` branch:
    `_certificate_to_pld` treats a bare float as `(epsilon, delta=0.0)`)."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [draw(_finite_epsilon()) for _ in range(n)]


@st.composite
def mixed_certs(
    draw: st.DrawFn, min_size: int = 1, max_size: int = _MAX_CERTS
) -> list[tuple[float, float]]:
    """A list of `(epsilon, delta)` tuple certificates (`Certificate`'s
    tuple branch), delta possibly 0 -- covers both thresholded and
    pure-epsilon released mechanisms in one strategy."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [(draw(_finite_epsilon()), draw(_finite_delta())) for _ in range(n)]


def _delta_of(cert: Certificate) -> float:
    return cert[1] if isinstance(cert, tuple) else 0.0


def _feasible_delta(certs: list[Certificate], margin: float = 1e-6) -> float:
    """A target delta at which `_compose(certs).get_epsilon_for_delta(...)`
    is guaranteed finite: `dp_accounting.pld` needs the query delta to
    exceed the composed mechanism's own delta floor (empirically, the sum
    of the component deltas plus a small margin is always sufficient --
    probed over 500 random mixed certificate lists at n<=6, epsilon in
    [1e-6, 5.0], delta in [0, 1e-3]). Querying below the true floor is a
    legitimate `inf` result (an infeasible budget), not a bug; these tests
    stay in the feasible region on purpose since they assert on the
    FINITE composed value."""
    return sum(_delta_of(c) for c in certs) + margin


# --------------------------------------------------------------------------
# check_epsilon_supported: the budget-exceeded refusal guard
# --------------------------------------------------------------------------


@given(
    st.floats(min_value=-1e6, max_value=_DP_EPSILON_CEILING, allow_nan=False, allow_infinity=False)
)
def test_check_epsilon_supported_accepts_any_finite_epsilon_at_or_below_ceiling(
    epsilon: float,
) -> None:
    """Bound is `epsilon <= _DP_EPSILON_CEILING`; the guard's own docstring
    says it "does not validate positivity", so a negative epsilon is
    accepted here too (that is a documented gap, not this guard's job --
    positivity is checked at the fit's config parse)."""
    check_epsilon_supported(epsilon)  # must not raise


@given(
    st.floats(
        min_value=_DP_EPSILON_CEILING, max_value=1e12, allow_nan=False, allow_infinity=False
    ).filter(lambda e: e > _DP_EPSILON_CEILING)
)
def test_check_epsilon_supported_rejects_any_epsilon_strictly_above_ceiling(epsilon: float) -> None:
    with pytest.raises(DpBudgetError) as exc:
        check_epsilon_supported(epsilon)
    assert exc.value.code == "dp_epsilon_unsupported"


@given(
    st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
    st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
)
def test_check_epsilon_supported_acceptance_is_monotone_in_epsilon(e1: float, e2: float) -> None:
    """If a LARGER epsilon `e2` is accepted, a smaller `e1 <= e2` must be
    accepted too -- the guard is one upper threshold, not a two-sided or
    off-by-one comparison. Targets boundary-operator mutants
    (`<=` -> `<`, `>` -> `>=`) that a single fixed-ceiling example test
    cannot distinguish from the correct comparison."""
    assume(e1 <= e2)
    try:
        check_epsilon_supported(e2)
    except DpBudgetError:
        return  # e2 rejected tells us nothing about e1
    check_epsilon_supported(e1)  # e2 accepted => e1 (smaller) must also be accepted


def test_check_epsilon_supported_rejects_nan() -> None:
    # NaN fails every comparison, so `not (epsilon <= ceiling)` is the only
    # form that fails it closed; `epsilon > ceiling` would wrongly accept it.
    with pytest.raises(DpBudgetError) as exc:
        check_epsilon_supported(float("nan"))
    assert exc.value.code == "dp_epsilon_unsupported"


# --------------------------------------------------------------------------
# _compose / _certificate_to_pld: the composition invariants
# --------------------------------------------------------------------------


@given(_finite_epsilon())
def test_single_pure_epsilon_certificate_composes_to_its_own_value(epsilon: float) -> None:
    """EMPTY/SINGLE base case: with nothing to `.compose()` with, `_compose`
    returns the lone certificate's own PLD untouched -- no COMPOSITION
    discretization loss, since the `for pld in plds[1:]` loop body never
    runs. `from_privacy_parameters` itself still snaps to the PLD's own
    `value_discretization_interval` (1e-4) grid on construction, so this is
    "own value up to one grid cell", not bit-exact (that grid snap is what
    `_SINGLE_CERT_TOL` bounds, not a fresh tolerance invented for this
    test)."""
    composed = _compose([epsilon])
    assert composed.get_epsilon_for_delta(0.0) == pytest.approx(epsilon, abs=_SINGLE_CERT_TOL)


@given(_finite_epsilon(), _finite_delta())
def test_single_mixed_certificate_composes_to_its_own_epsilon_at_its_own_delta(
    epsilon: float, delta: float
) -> None:
    composed = _compose([(epsilon, delta)])
    assert composed.get_epsilon_for_delta(delta) == pytest.approx(epsilon, abs=_SINGLE_CERT_TOL)


def test_composing_zero_certificates_is_unreachable_in_production() -> None:
    """Characterizes, rather than asserts as correct, the current
    zero-certificate behavior: `_compose([])` raises a bare `IndexError`
    (`plds[0]` on an empty list), not a documented `DpBudgetError` and not
    a "zero loss" base case. This is DELIBERATELY not treated as a bug to
    fix here (this suite may only add tests, not change `dp_budget.py`):
    the only production caller, `OpenDpReleaseSession.composed_loss` /
    `_allocate_epsilon` (`dp_session.py`), always composes
    `schedule.query_names`, and `Schedule.row_count_name` is a mandatory
    (non-optional) dataclass field, so `query_count >= 1` always and this
    path is unreachable through the public API. Recorded here so a future
    change to that invariant (e.g. an optional row-count query) is caught
    by this test flipping from "raises IndexError" to something else,
    which is the signal that `_compose`'s empty-list behavior now needs a
    real decision."""
    with pytest.raises(IndexError):
        _compose([])


@given(mixed_certs())
def test_compose_is_deterministic(certs: list[tuple[float, float]]) -> None:
    """Pure function: the identical certificate list composes to the
    identical epsilon at the identical target delta on every call."""
    target = _feasible_delta(certs)
    first = _compose(list(certs)).get_epsilon_for_delta(target)
    second = _compose(list(certs)).get_epsilon_for_delta(target)
    assert first == second


@given(mixed_certs(min_size=2), st.randoms(use_true_random=False))
def test_compose_is_order_independent(certs: list[tuple[float, float]], rng) -> None:
    """Metamorphic: `dp_accounting.pld`'s pairwise `.compose()` is
    convolution of independent distributions, which is commutative, so
    permuting the certificate list must not change the composed epsilon at
    a shared feasible target delta (float-noise tolerance only)."""
    shuffled = list(certs)
    rng.shuffle(shuffled)
    target = _feasible_delta(certs)
    base = _compose(list(certs)).get_epsilon_for_delta(target)
    permuted = _compose(shuffled).get_epsilon_for_delta(target)
    assert base == pytest.approx(permuted, abs=_NOISE_TOL)


@given(mixed_certs(min_size=1, max_size=_MAX_CERTS - 1), _finite_epsilon(), _finite_delta())
def test_composing_an_extra_mechanism_never_decreases_the_total(
    certs: list[tuple[float, float]], extra_epsilon: float, extra_delta: float
) -> None:
    """MONOTONICITY: `total(mechanisms + extra) >= total(mechanisms)` at a
    fixed target delta -- composing one more certified release can only
    add to (never subtract from) the fit-wide privacy loss."""
    extended = [*certs, (extra_epsilon, extra_delta)]
    target = _feasible_delta(extended)
    base_total = _compose(list(certs)).get_epsilon_for_delta(target)
    extended_total = _compose(extended).get_epsilon_for_delta(target)
    assert extended_total >= base_total - _NOISE_TOL


@given(
    mixed_certs(min_size=1, max_size=_MAX_CERTS - 1),
    st.integers(min_value=0),
    _finite_epsilon(min_value=1e-3, max_value=2.0),
)
def test_increasing_one_columns_epsilon_never_decreases_the_total(
    certs: list[tuple[float, float]], raw_index: int, increment: float
) -> None:
    """MONOTONICITY, the other axis: raising a SINGLE column's certified
    epsilon (holding every other column and every delta fixed) never
    decreases the composed total at a fixed target delta."""
    index = raw_index % len(certs)
    epsilon, delta = certs[index]
    bigger = list(certs)
    bigger[index] = (epsilon + increment, delta)
    # Deltas are unchanged between `certs` and `bigger`, so one shared
    # target delta is feasible for both.
    target = _feasible_delta(certs)
    base_total = _compose(list(certs)).get_epsilon_for_delta(target)
    bigger_total = _compose(bigger).get_epsilon_for_delta(target)
    assert bigger_total >= base_total - _NOISE_TOL


@given(pure_epsilon_certs())
def test_pure_epsilon_composition_is_non_negative_and_finite(certs: list[float]) -> None:
    """NON-NEGATIVITY / bounds: for finite, non-negative certified
    epsilons, the composed loss at delta=0 is itself finite, >= 0, and
    never NaN -- a fit-wide privacy loss can never be reported as
    unbounded, negative, or undefined when every input is well-formed."""
    composed_epsilon = _compose(list(certs)).get_epsilon_for_delta(0.0)
    assert math.isfinite(composed_epsilon)
    assert not math.isnan(composed_epsilon)
    assert composed_epsilon >= 0.0


@given(pure_epsilon_certs())
def test_pure_epsilon_composition_matches_basic_sequential_composition(certs: list[float]) -> None:
    """ADDITIVITY baseline (Dwork & Roth Thm 3.16, cited in this repo's own
    DP survey docs): k pure-epsilon mechanisms compose to at most
    `sum(epsilon_i)` at delta=0. `dp_accounting.pld`'s dominating-pair
    composition should recover that sum up to its own grid-discretization
    error, and -- per this file's empirical probing -- rounds that error
    in the CONSERVATIVE direction only (composed >= naive sum, never
    meaningfully below it)."""
    composed_epsilon = _compose(list(certs)).get_epsilon_for_delta(0.0)
    naive_sum = math.fsum(certs)
    assert composed_epsilon == pytest.approx(naive_sum, abs=_ADDITIVITY_TOL)
    assert composed_epsilon >= naive_sum - _UNDER_SUM_TOL
