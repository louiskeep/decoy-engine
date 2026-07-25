"""Property + metamorphic invariants for the DP mechanism layer (`quality/dp.py`).

TQ program, module 2 (Test-Quality Program; playbook `docs/quality/module-
test-quality-playbook.md`). `tests/unit/quality/test_dp.py` already gives
`quality/dp.py` a large example-based suite (config validation, disclosure-
channel regressions, artifact shape, categorical release ordering, the
unseeded-statistical-mechanism tests from guide section 7.2); the tests below
are oracles LAYERED ON TOP, not a restatement -- each states an invariant the
module's own docstring, `quality/dp_fit_schema.py`, or the DPS-CODEC guide
(`docs/plans/2026-07-23-dps-codec-implementation-guide.md`) commits to, and
lets Hypothesis search for a counterexample rather than pinning one more
example.

Per `quality/dp.py`'s own framing: the PRIVACY guarantee -- that the released
noise actually satisfies (epsilon, delta) -- is OpenDP's and `dp_accounting`'s
to prove, not this engine's (see `quality/dp_budget.py`'s "what this module
does NOT do"). These tests stay on the STRUCTURAL/behavioral guarantees
`dp.py` itself owns:

- DETERMINISM. The real mechanism is UNSEEDED by design (guide section 7.2;
  `dp.py` exposes no seed/rng parameter, pinned in `test_dp.py::
  TestConfigValidation::test_production_dp_fit_exposes_no_seed_or_rng_
  parameter`), so the released VALUES cannot be tested for determinism --
  what CAN and must be exactly reproducible is the module's own pure
  arithmetic scaffolding around that noise: `_serialize_count`, `_flag_token`,
  `_interior_edges`, `_check_derived_edges`, `_validate_fit_params`. None of
  these touch OpenDP or a private cell.
- SHAPE / DOMAIN PRESERVATION. The artifact's declared columns/dtype/kind
  match the schema; every released quantity documented as a count
  (row_count, bin_counts, non_null_count, distinct_count, other_count) is a
  non-negative int; a categorical column's retained labels are drawn only
  from its own carrier domain (a `text` label OpenDP actually saw, or one of
  the two canonical `flag` tokens) -- OpenDP's grouped count can only ever
  retain a key it observed, never invent one.
- EPSILON MONOTONICITY. `OpenDpReleaseSession`'s per-query epsilon (`_eps_q`)
  is monotone non-decreasing in the requested fit-wide epsilon for a fixed
  schedule/delta (guide section 4.3.2's allocation search). Since a Laplace
  mechanism's noise scale is sensitivity / epsilon, a smaller `_eps_q` can
  only ever mean equal-or-more noise -- `_eps_q` is the module's own exposed,
  directly testable proxy for "noise grows as epsilon shrinks" without
  re-deriving OpenDP's calibration formula.
- FAIL-CLOSED. The config/schema/domain guards `dp.py` owns
  (`dp_epsilon_invalid`, `dp_delta_invalid`, `dp_numeric_bins_invalid`,
  `dp_numeric_domain_invalid`) and the schema guards `dp_fit_schema.py`
  delegates to (`dp_carrier_unknown`, `dp_kind_carrier_mismatch`) all run
  BEFORE `check_fit_environment()` (the proof-stack gate) and before any
  private cell is read -- `_fit_dp_snapshot_with_backend`'s own ordering is
  `_validate_fit_params` -> `freeze_column_schema` -> `parse_column_schema`
  -> per-numeric-column `_check_derived_edges` -> only then
  `check_fit_environment()`. These are exercised here over a wide Hypothesis
  domain through the PUBLIC `fit_dp_snapshot` entrypoint, with NO
  `dp_certified` marker: a privacy-critical fit must never even reach the
  proof-stack gate on a malformed request. The gate itself has its own unit
  suite (`test_dp_provenance.py`); what is untested at the `dp.py` call site
  is that a refusal there actually propagates (rather than being swallowed)
  and fires before the source object is touched -- pinned separately below,
  also without `dp_certified`, since it only monkeypatches the gate.
- INPUT COVERAGE. Empty, single-row, and all-same-value frames are ordinary
  (non-adversarial) inputs, not a special case the module carves out; gated
  `dp_certified` since they exercise the real OpenDP-backed fit, and skip
  cleanly off the certified 77-dist dev+lint+vault Python 3.10.20 profile
  exactly like the rest of `test_dp.py` (see `tests/_dp_cert.py`).

Source patterns cited per CLAUDE.md's "use established methodology" rule
match `quality/dp.py`'s own citations: OpenDP's transformation user guide
(https://docs.opendp.org/en/stable/api/user-guide/transformations/index.html)
and thresholded noise mechanisms guide
(https://docs.opendp.org/en/stable/api/user-guide/measurements/
thresholded-noise-mechanisms.html); Dwork & Roth, *Algorithmic Foundations of
Differential Privacy*, Sec. 3, for the sensitivity/epsilon noise-scale
relationship the epsilon-monotonicity tests below rely on.

Run:  pytest tests/property/test_dp_mechanisms_invariants.py -q
"""

from __future__ import annotations

import itertools
import math

import pandas as pd
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from decoy_engine.quality.carriers import CarrierError
from decoy_engine.quality.dp import (
    DpError,
    _check_derived_edges,
    _flag_token,
    _interior_edges,
    _serialize_count,
    _validate_fit_params,
    fit_dp_snapshot,
)
from decoy_engine.quality.dp_budget import OpenDpReleaseSession
from decoy_engine.quality.dp_provenance import ProvenanceError
from decoy_engine.quality.dp_schedule import Schedule

# Match the pilot property suite's audit profile (see
# `tests/property/test_ri_graph_invariants.py`): more examples than the
# 100-example default, no deadline, print_blob so a counterexample is
# replayable. Applies to the PURE, fast tests below; the tests that touch a
# real OpenDP calibration or a real fit override `max_examples` locally
# (documented at each such test) since a single such call costs low
# single-digit seconds, unlike the arithmetic-only helpers.
settings.register_profile(
    "audit",
    max_examples=300,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")


# ---------------------------------------------------------------------------
# Pure-arithmetic helpers: `_serialize_count`, `_flag_token`, `_interior_
# edges`, `_check_derived_edges`, `_validate_fit_params`. None of these touch
# OpenDP or read a private cell -- they are the module's own public-
# declaration arithmetic, fully deterministic, and the fixed scaffolding
# around whatever noise OpenDP adds.
# ---------------------------------------------------------------------------


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_serialize_count_matches_its_documented_formula(v: float) -> None:
    """`_serialize_count`'s documented contract (its own docstring): `max(0,
    int(round(v)))` -- restated independently here rather than by calling the
    function under test a second time."""
    result = _serialize_count(v)
    assert isinstance(result, int)
    assert result >= 0
    assert result == max(0, round(v))


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_serialize_count_is_deterministic(v: float) -> None:
    assert _serialize_count(v) == _serialize_count(v)


@given(
    st.floats(allow_nan=False, allow_infinity=False),
    st.floats(allow_nan=False, allow_infinity=False),
)
def test_serialize_count_is_monotonic_nondecreasing(a: float, b: float) -> None:
    """Metamorphic: a larger raw (pre-serialization) value can never
    serialize to a SMALLER count. `round` is monotone and the `max(0, ...)`
    floor cannot invert an ordering; a comparison-operator mutant (`<` for
    `<=`, or a dropped floor) would break this on a pair straddling zero."""
    lo, hi = (a, b) if a <= b else (b, a)
    assert _serialize_count(lo) <= _serialize_count(hi)


_FLAG_TOKEN_DOMAIN = st.one_of(
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=5),
    st.none(),
    st.lists(st.integers(), max_size=3),
)


@given(_FLAG_TOKEN_DOMAIN)
def test_flag_token_is_total_and_reflects_python_truthiness(value: object) -> None:
    """`_flag_token`'s documented contract (its own docstring, guide section
    3.4): `"true"` iff the value is truthy, `"false"` otherwise -- never
    `str(bool(value))`'s `"True"/"False"` and never a `"1"/"0"` token."""
    token = _flag_token(value)
    assert token in ("true", "false")
    assert token == ("true" if value else "false")
    assert _flag_token(value) == token  # deterministic


@given(st.floats())
def test_validate_fit_params_epsilon_boundary_is_isfinite_and_positive(eps: float) -> None:
    """`fit_dp_snapshot`'s documented epsilon contract: "Finite, > 0." Delta
    and numeric_bins are held at fixed-valid values so only the epsilon leg
    of the (epsilon, delta, numeric_bins) check chain is exercised --
    `_validate_fit_params` validates epsilon FIRST and raises immediately, so
    a fixed-valid delta/bins isolates this leg without ever reaching the
    others."""
    should_be_valid = math.isfinite(eps) and eps > 0
    if should_be_valid:
        _validate_fit_params(epsilon=eps, delta=0.5, numeric_bins=10)  # must not raise
    else:
        with pytest.raises(DpError) as exc:
            _validate_fit_params(epsilon=eps, delta=0.5, numeric_bins=10)
        assert exc.value.code == "dp_epsilon_invalid"


@given(st.floats())
def test_validate_fit_params_delta_boundary_is_isfinite_and_in_open_unit_interval(
    delta: float,
) -> None:
    """`fit_dp_snapshot`'s documented delta contract: "Finite, in (0, 1) --
    rejected at 0 even for an all-numeric fit" (guide 9.10 item 2). Epsilon
    fixed-valid so only the delta leg is exercised."""
    should_be_valid = math.isfinite(delta) and 0.0 < delta < 1.0
    if should_be_valid:
        _validate_fit_params(epsilon=1.0, delta=delta, numeric_bins=10)  # must not raise
    else:
        with pytest.raises(DpError) as exc:
            _validate_fit_params(epsilon=1.0, delta=delta, numeric_bins=10)
        assert exc.value.code == "dp_delta_invalid"


@given(st.one_of(st.integers(max_value=20), st.booleans(), st.floats(allow_nan=True)))
def test_validate_fit_params_numeric_bins_boundary(bins: object) -> None:
    """`numeric_bins` must be `int >= 2` and NOT `bool` (`bool` is an `int`
    subclass in Python, so the guard needs its own explicit check -- both
    `True`/`False` must be rejected regardless of their numeric value)."""
    should_be_valid = isinstance(bins, int) and not isinstance(bins, bool) and bins >= 2
    if should_be_valid:
        _validate_fit_params(epsilon=1.0, delta=0.5, numeric_bins=bins)  # must not raise
    else:
        with pytest.raises(DpError) as exc:
            _validate_fit_params(epsilon=1.0, delta=0.5, numeric_bins=bins)
        assert exc.value.code == "dp_numeric_bins_invalid"


@given(
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.integers(min_value=2, max_value=50),
)
def test_interior_edges_returns_exactly_bins_minus_one_strictly_increasing_points(
    lower: float, width: float, bins: int
) -> None:
    """`_interior_edges`'s own contract (its docstring): `numeric_bins`
    categories need `numeric_bins - 1` interior cut points spanning the
    declared domain -- exactly that many, strictly between the bounds, and
    strictly increasing (so `make_find_bin`'s category range is exactly
    `0..bins-1` with no overflow bin, guide section 4.4)."""
    upper = lower + width
    assume(upper > lower)
    edges = _interior_edges(lower, upper, bins)
    assert len(edges) == bins - 1
    assert all(lower < e < upper for e in edges)
    assert list(edges) == sorted(edges)
    assert len(set(edges)) == len(edges)


@given(
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.integers(min_value=2, max_value=50),
)
def test_check_derived_edges_accepts_every_well_conditioned_domain(
    lower: float, width: float, bins: int
) -> None:
    """The D-M-B guard (guide section 4.4; `test_dp.py::
    test_ordinary_domains_still_fit`'s own regression story: an earlier
    revision used `zip(..., strict=True)`, whose operands differ in length
    by one by construction, so it raised on EVERY domain) must reject only
    DEGENERATE declarations. This sweeps a wide domain of ordinary
    `(lower, width, bins)` triples -- width bounded well above float64 ULP at
    this magnitude range, so no example here is actually degenerate -- and
    asserts none of them raises."""
    upper = lower + width
    assume(upper > lower)
    _check_derived_edges("col", lower, upper, bins)  # must not raise


@given(
    st.floats(min_value=-1e10, max_value=1e10, allow_nan=False, allow_infinity=False),
    st.integers(min_value=2, max_value=1000),
)
def test_check_derived_edges_rejects_a_domain_whose_edges_collapse(lower: float, bins: int) -> None:
    """The other side of the D-M-B guard: a domain so narrow relative to
    `bins` that two adjacent derived edges round to the SAME float64 must be
    REJECTED, not silently released as a non-monotonic `bin_edges` list.
    `upper` is the smallest float64 strictly greater than `lower`
    (`math.nextafter`), so for any `bins >= 2` every interior cut point must
    round to `lower` or `upper` -- there is no representable value strictly
    between them -- which independently proves (without calling the function
    under test a second time) that this domain is degenerate under the same
    math `_check_derived_edges` derives from."""
    upper = math.nextafter(lower, lower + 1.0)
    assume(upper > lower)
    edges = [lower, *(lower + (upper - lower) * i / bins for i in range(1, bins)), upper]
    is_degenerate = not all(math.isfinite(e) for e in edges) or any(
        b <= a for a, b in itertools.pairwise(edges)
    )
    assume(is_degenerate)
    with pytest.raises(DpError) as exc:
        _check_derived_edges("col", lower, upper, bins)
    assert exc.value.code == "dp_numeric_domain_invalid"


# ---------------------------------------------------------------------------
# `OpenDpReleaseSession`'s allocation search: EPSILON MONOTONICITY. Session
# CONSTRUCTION touches no data (the class's own docstring: "Construction
# touches no data: it stores the public schedule and the fit-wide
# (epsilon, delta) request, and runs the section 4.3.2 allocation search, a
# pure function of the request and the public query counts"), so this needs
# no `dp_certified` marker -- the proof-stack gate guards READING PRIVATE
# DATA, not calibrating against a public request.
#
# Real allocation runs real `dp_accounting` composition plus real OpenDP
# calibration searches (each session construction costs low single-digit
# seconds), so this draws from a SMALL fixed pool via `st.sampled_from`
# rather than the audit profile's continuous-float 300 examples: repeated
# draws of the same epsilon value hit `OpenDpReleaseSession`'s own
# production allocation cache (`dp_budget._ALLOCATION_CACHE`), so wall-clock
# cost is bounded by the pool size, not the example count.
# ---------------------------------------------------------------------------

_EPS_POOL = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)


@given(st.sampled_from(_EPS_POOL), st.sampled_from(_EPS_POOL))
@settings(max_examples=12, deadline=None)
def test_session_eps_q_is_monotone_nondecreasing_in_requested_epsilon(
    eps_a: float, eps_b: float
) -> None:
    """Smaller fit-wide epsilon request -> smaller-or-equal per-query budget
    `_eps_q` -> (Laplace scale = sensitivity / epsilon) equal-or-more noise.
    Also checks the allocation search never hands out MORE per-query budget
    than the fit-wide request itself allows (guide section 4.3.2's own
    invariant, restated independently of the search's internal bisection)."""
    assume(eps_a != eps_b)
    lo, hi = (eps_a, eps_b) if eps_a < eps_b else (eps_b, eps_a)
    schedule = Schedule(row_count_name="rc", numeric=(), categorical=())
    session_lo = OpenDpReleaseSession(schedule, epsilon=lo, delta=1e-6)
    session_hi = OpenDpReleaseSession(schedule, epsilon=hi, delta=1e-6)
    assert session_lo._eps_q <= session_hi._eps_q
    assert session_lo._eps_q <= lo
    assert session_hi._eps_q <= hi


# ---------------------------------------------------------------------------
# FAIL-CLOSED, pre-gate. Every guard below runs and raises BEFORE
# `check_fit_environment()` (see the ordering note in the module docstring
# above), so these call the PUBLIC `fit_dp_snapshot` entrypoint directly,
# over a wide Hypothesis domain, with NO `dp_certified` marker.
# ---------------------------------------------------------------------------

_INVALID_EPSILONS = st.one_of(
    st.just(0.0),
    st.floats(max_value=0.0, exclude_max=True, allow_nan=False, allow_infinity=False),
    st.just(math.nan),
    st.just(math.inf),
    st.just(-math.inf),
)


@given(_INVALID_EPSILONS)
def test_fit_rejects_every_non_positive_or_nonfinite_epsilon_before_the_cert_gate(
    eps: float,
) -> None:
    with pytest.raises(DpError) as exc:
        fit_dp_snapshot(pd.DataFrame({"age": [1.0, 2.0]}), {}, epsilon=eps, delta=1e-6)
    assert exc.value.code == "dp_epsilon_invalid"


_INVALID_DELTAS = st.one_of(
    st.just(0.0),
    st.floats(min_value=1.0, allow_infinity=False),  # >= 1.0
    st.just(math.nan),
    st.just(math.inf),
    st.floats(max_value=0.0, exclude_max=True, allow_nan=False, allow_infinity=False),  # < 0
)


@given(_INVALID_DELTAS)
def test_fit_rejects_every_delta_outside_the_open_unit_interval_before_the_cert_gate(
    delta: float,
) -> None:
    with pytest.raises(DpError) as exc:
        fit_dp_snapshot(pd.DataFrame({"age": [1.0, 2.0]}), {}, epsilon=1.0, delta=delta)
    assert exc.value.code == "dp_delta_invalid"


@given(st.one_of(st.integers(max_value=1), st.booleans(), st.floats(allow_nan=True)))
def test_fit_rejects_every_invalid_numeric_bins_before_the_cert_gate(bins: object) -> None:
    assume(not (isinstance(bins, int) and not isinstance(bins, bool) and bins >= 2))
    with pytest.raises(DpError) as exc:
        fit_dp_snapshot(
            pd.DataFrame({"age": [1.0, 2.0]}),
            {"age": {"carrier": "number", "bounds": (0.0, 10.0)}},
            epsilon=1.0,
            delta=1e-6,
            numeric_bins=bins,
        )
    assert exc.value.code == "dp_numeric_bins_invalid"


@given(st.text(min_size=1, max_size=12).filter(lambda s: s not in ("number", "flag", "text")))
def test_fit_rejects_every_carrier_outside_the_closed_set_before_the_cert_gate(
    carrier: str,
) -> None:
    """`column_schema`'s carrier is a closed 3-member set (`dp_fit_schema.
    parse_column_schema`, guide section 3.3/3.5); anything else is rejected
    BEFORE the cert gate."""
    df = pd.DataFrame({"c": [1, 2, 3]})
    with pytest.raises(CarrierError) as exc:
        fit_dp_snapshot(df, {"c": {"carrier": carrier}}, epsilon=1.0, delta=1e-6)
    assert exc.value.code == "dp_carrier_unknown"


@given(st.sampled_from(["number", "flag", "text"]), st.sampled_from(["numeric", "categorical"]))
def test_fit_rejects_an_impossible_kind_carrier_pair_before_the_cert_gate(
    carrier: str, kind: str
) -> None:
    """The closed kind x carrier table (`dp_fit_schema._KIND_TO_CARRIERS`,
    guide section 3.3): `categorical` + `number` has no OpenDP float
    `make_count_by`, and `numeric` + `flag`/`text` is the same impossibility
    from the other side. Rejected BEFORE the cert gate rather than silently
    mis-releasing."""
    allowed = {"numeric": ("number",), "categorical": ("text", "flag")}
    assume(carrier not in allowed[kind])
    df = pd.DataFrame({"c": [1, 2, 3]})
    with pytest.raises(CarrierError) as exc:
        fit_dp_snapshot(df, {"c": {"carrier": carrier, "kind": kind}}, epsilon=1.0, delta=1e-6)
    assert exc.value.code == "dp_kind_carrier_mismatch"


class _PoisonedSource:
    """A source object that raises the moment ANYTHING reads it. Stands in
    for "the caller's private data": if `check_fit_environment` ran AFTER
    even one attribute or iteration probe of this object, that probe would
    already have touched private data ahead of the gate."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"source was read ({name!r}) before the proof-stack gate ran")

    def __iter__(self) -> object:
        raise AssertionError("source was iterated before the proof-stack gate ran")


def test_fit_propagates_a_provenance_refusal_and_never_reads_the_source_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_fit_environment` has its own dedicated unit suite
    (`test_dp_provenance.py`); untested at the `dp.py` call site is that a
    refusal there actually PROPAGATES out of `fit_dp_snapshot` rather than
    being caught or downgraded, and that it fires before the source object is
    touched at all. Both are independent of which profile happens to run
    this suite (the gate is forced via monkeypatch), so this needs no
    `dp_certified` marker."""
    import decoy_engine.quality.dp as dp_module

    def _refuse() -> None:
        raise ProvenanceError(code="dp_stack_uncertified", message="forced for this test")

    monkeypatch.setattr(dp_module, "check_fit_environment", _refuse)
    with pytest.raises(ProvenanceError) as exc:
        fit_dp_snapshot(
            _PoisonedSource(),
            {"age": {"carrier": "number", "bounds": (0.0, 10.0)}},
            epsilon=1.0,
            delta=1e-6,
        )
    assert exc.value.code == "dp_stack_uncertified"


# ---------------------------------------------------------------------------
# SHAPE / DOMAIN PRESERVATION (dp_certified: exercises the real OpenDP-backed
# fit). The specific noised VALUES are unseeded by design (guide section 7.2)
# so these assert only the artifact's own STABLE structural contract: the
# declared schema's columns, non-negative int counts everywhere a count is
# documented, and categorical labels drawn only from the carrier's own
# domain (never a label OpenDP could not have seen).
#
# Small `max_examples` (not the audit profile's 300): each example runs a
# REAL OpenDP fit (session construction plus multiple calibration searches),
# unlike the pre-gate arithmetic above.
# ---------------------------------------------------------------------------

_CAT_LABELS = st.sampled_from(["alpha", "beta", "gamma", "delta"])


@pytest.mark.dp_certified
@given(
    st.lists(st.floats(min_value=0.0, max_value=100.0, allow_nan=False), min_size=1, max_size=40),
    st.lists(_CAT_LABELS, min_size=1, max_size=40),
)
@settings(max_examples=15, deadline=None)
def test_fit_artifact_shape_matches_declared_schema_for_arbitrary_columns(
    nums: list[float], cats: list[str]
) -> None:
    """The artifact's `columns` keys are exactly the declared schema's keys
    (`fit_dp_snapshot`'s own docstring), every documented count is a
    non-negative int, the numeric column's public `bin_edges` reproduce the
    declared domain exactly (`edges[0] == lower`, `edges[-1] == upper`), and
    a text column's retained labels are a SUBSET of the labels actually
    present in the input -- OpenDP's grouped count can only ever retain a key
    it saw, never invent one."""
    n = max(len(nums), len(cats))
    nums = (nums * n)[:n]
    cats = (cats * n)[:n]
    df = pd.DataFrame({"n": nums, "c": cats})
    schema = {
        "n": {"carrier": "number", "kind": "numeric", "bounds": (0.0, 100.0)},
        "c": {"carrier": "text", "kind": "categorical"},
    }
    snap = fit_dp_snapshot(df, schema, epsilon=8.0, delta=1e-3, numeric_bins=5)
    assert set(snap["columns"]) == set(schema)

    row_count = snap["row_count"]
    assert isinstance(row_count, int) and row_count >= 0

    n_col = snap["columns"]["n"]
    assert n_col["dtype"] == "float64"
    assert n_col["kind"] == "numeric"
    bin_counts = n_col["stats"]["bin_counts"]
    assert len(bin_counts) == 5
    assert all(isinstance(c, int) and c >= 0 for c in bin_counts)
    assert 0 <= n_col["null_count"] <= row_count
    edges = n_col["stats"]["bin_edges"]
    assert edges[0] == 0.0
    assert edges[-1] == 100.0
    assert edges == sorted(edges) and len(set(edges)) == len(edges)

    c_col = snap["columns"]["c"]
    assert c_col["dtype"] == "object"
    assert c_col["kind"] == "categorical"
    observed = set(cats)
    for entry in c_col["stats"]["top_values"]:
        assert entry["value"] in observed  # never an unobserved label
        assert isinstance(entry["count"], int) and entry["count"] >= 0
    assert isinstance(c_col["stats"]["other_count"], int) and c_col["stats"]["other_count"] >= 0
    assert 0 <= c_col["null_count"] <= row_count


@pytest.mark.dp_certified
@given(st.lists(st.booleans(), min_size=1, max_size=60))
@settings(max_examples=12, deadline=None)
def test_fit_flag_carrier_retained_labels_are_always_the_two_canonical_tokens(
    flags: list[bool],
) -> None:
    """A `flag` carrier's retained categories can only ever be the two
    canonical serialized tokens (guide section 3.4) -- never `str(bool)`'s
    `"True"/"False"` and never `"1"/"0"` -- whatever the true/false mix in
    the input."""
    df = pd.DataFrame({"f": flags})
    snap = fit_dp_snapshot(
        df, {"f": {"carrier": "flag", "kind": "categorical"}}, epsilon=8.0, delta=1e-3
    )
    tokens = {entry["value"] for entry in snap["columns"]["f"]["stats"]["top_values"]}
    assert tokens <= {"true", "false"}
    assert snap["columns"]["f"]["dtype"] == "bool"


# ---------------------------------------------------------------------------
# INPUT COVERAGE (dp_certified: exercises the real OpenDP-backed fit). An
# empty frame, a single row, and a constant-valued column are ordinary
# inputs a real caller can hand the fit, not adversarial edge cases -- none
# is a special case the module carves out.
# ---------------------------------------------------------------------------


@pytest.mark.dp_certified
@pytest.mark.parametrize(
    "df",
    [
        pd.DataFrame({"n": pd.Series([], dtype=float), "c": pd.Series([], dtype=object)}),
        pd.DataFrame({"n": [42.0], "c": ["only"]}),
        pd.DataFrame({"n": [7.0] * 25, "c": ["same"] * 25}),
    ],
    ids=["empty", "single_row", "all_same_value"],
)
def test_fit_handles_empty_single_row_and_all_same_value_input_without_raising(
    df: pd.DataFrame,
) -> None:
    schema = {
        "n": {"carrier": "number", "kind": "numeric", "bounds": (0.0, 100.0)},
        "c": {"carrier": "text", "kind": "categorical"},
    }
    snap = fit_dp_snapshot(df, schema, epsilon=8.0, delta=1e-3, numeric_bins=5)
    assert set(snap["columns"]) == {"n", "c"}
    assert snap["row_count"] >= 0
    assert len(snap["columns"]["n"]["stats"]["bin_counts"]) == 5
