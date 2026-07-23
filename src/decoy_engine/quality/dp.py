"""Differentially private fit of a distribution snapshot (DPS Scope B).

`fit_dp_snapshot` replaces the parked Option A `apply_dp_noise`. Where
Option A noised an already-computed EXACT snapshot (rank, threshold
comparison, and moment removal all happening on private values Decoy had
already materialized), this fit never materializes an exact distribution
at all: every released quantity -- a numeric bin count, a categorical
label's count, the categorical non-null total, the table row count --
comes directly out of a chained OpenDP `Transformation >> Measurement`
invoked on a normalized, recordwise projection of the source frame
(binding decision 15). `quality.dp_budget.OpenDpReleaseSession` is the
sole place in this codebase that constructs or invokes an OpenDP
measurement (guide section 4.3.5 mitigation 1); this module drives the
session through the fixed public schedule and serializes what it
releases. It does not import `opendp` or `dp_accounting` itself.

Source patterns (per CLAUDE.md's "use established methodology" rule):
OpenDP's [transformation user guide](
https://docs.opendp.org/en/stable/api/user-guide/transformations/index.html)
for the `make_find_bin >> then_count_by_categories` / `make_count_by` /
`make_count` chain shapes, and its [thresholded noise mechanisms](
https://docs.opendp.org/en/stable/api/user-guide/measurements/
thresholded-noise-mechanisms.html) for `then_laplace_threshold`'s
propose-test-release semantics (Korolova, Kenthapadi, Mishra, Ntoulas,
"Releasing Search Queries and Clicks Privately", WWW 2009; Dwork & Roth,
*Algorithmic Foundations of DP*, Sec. 3). `dp_accounting.pld`'s
dominating-pair composition is cited in `quality/dp_budget.py`, which
owns that half of the accounting split (guide section 3.3).

What Scope B covers, precisely (see `docs/what-we-cannot-prove.md` for
the full statement): single-column numeric and categorical marginals
under one fit-wide `(epsilon, delta)`, add-or-remove-one-row adjacency.
Joint distributions, cross-column correlation, conditional sampling, and
non-numeric/non-categorical kinds remain out of scope; `scope` is an
explicit literal discriminator in the artifact (`"single-column-
marginals"`) so a future joint mechanism has a seam without this build
inventing dormant fields for it (guide section 1).

The embedded-artifact 16 MiB cap (`plan/_generation.py`) follows the
HC-5 high-cardinality precedent (`quality/snapshot.py`'s
`_HIGH_CARDINALITY_MAX_LABEL_BYTES`): a fail-closed size fence is a typed
compile error, never a silent truncation.
"""

from __future__ import annotations

import importlib.metadata
import math
import uuid
import warnings
from collections.abc import Collection, Mapping
from typing import Any

import pandas as pd

from decoy_engine.quality.dp_budget import (
    DpBudgetError,
    OpenDpReleaseSession,
    _OpenDpBackend,
)
from decoy_engine.quality.dp_schedule import CategoricalQuerySpec, NumericQuerySpec, Schedule
from decoy_engine.quality.snapshot import DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION

DP_SNAPSHOT_SCHEMA_VERSION = "dps-marginal/v2"
DP_RELEASE_SCOPE = "single-column-marginals"
DP_ADJACENCY = "add-remove-one-row"
DP_ACCOUNTANT_LABEL = "dp_accounting PLD composition over OpenDP privacy maps"

_DEFAULT_NUMERIC_BINS = 10

# C-B2 (Codex round-3 blocker): the emitted `dtype` label used to be
# `canonical_dtype_label(frame[col].dtype)` -- the FRAME's own pandas
# dtype, which is content-dependent (pandas upcasts an integer column to
# float64 the moment a null enters it, and an object column's numpy dtype
# can otherwise vary with content). Codex demonstrated the two-neighbour
# leak directly: `[1]` (int64) versus `[1, None]` (float64), identical
# public declarations. `dtype` must be "a function of the caller's public
# declaration" (guide section 4.2.1), and the only public declaration a DP
# fit has is column KIND (numeric vs categorical, from `categorical_
# columns`/`numeric_domains`) -- there is no finer public dtype signal to
# report. These are fixed labels matching what each normalizer actually
# produces (`_normalize_numeric` always yields Python floats;
# `_normalize_categorical` always yields `str`), so the label is honest
# about the released shape without being read off the private frame.
_DP_NUMERIC_DTYPE_LABEL = "float64"
_DP_CATEGORICAL_DTYPE_LABEL = "object"


class DpError(Exception):
    """Invalid DP fit request, or a budget the accountant could not
    certify. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _dp_versions() -> tuple[str, str]:
    return (
        importlib.metadata.version("opendp"),
        importlib.metadata.version("dp-accounting"),
    )


def _validate_config(
    *,
    frame: pd.DataFrame,
    categorical_columns: Collection[str],
    numeric_domains: Mapping[str, tuple[float, float]],
    epsilon: float,
    delta: float,
    numeric_bins: int,
) -> None:
    """Every check here reads only public declarations and `frame.columns`
    (never a value). Configuration validation runs before any value is
    inspected (guide section 4.2)."""
    try:
        epsilon = float(epsilon)
    except (TypeError, ValueError) as exc:
        raise DpError(
            code="dp_epsilon_invalid", message=f"epsilon must be a number; got {epsilon!r}."
        ) from exc
    if not math.isfinite(epsilon) or epsilon <= 0:
        raise DpError(
            code="dp_epsilon_invalid",
            message=f"epsilon must be a finite float > 0; got {epsilon!r}.",
        )
    try:
        delta = float(delta)
    except (TypeError, ValueError) as exc:
        raise DpError(
            code="dp_delta_invalid", message=f"delta must be a number; got {delta!r}."
        ) from exc
    # Settled (guide section 9.10 item 2): delta=0 is rejected even for a
    # numeric-only fit. Uniformity across fits wins over the marginally
    # stronger pure-epsilon claim a numeric-only fit could otherwise carry.
    if not math.isfinite(delta) or not (0.0 < delta < 1.0):
        raise DpError(
            code="dp_delta_invalid",
            message=(
                f"delta must be a finite float in (0, 1); got {delta!r}. delta=0 is "
                "rejected even for an all-numeric fit (uniformity across fits), and "
                "delta>=1 is not a meaningful failure probability."
            ),
        )
    if not isinstance(numeric_bins, int) or isinstance(numeric_bins, bool) or numeric_bins < 2:
        raise DpError(
            code="dp_numeric_bins_invalid",
            message=f"numeric_bins must be an int >= 2; got {numeric_bins!r}.",
        )

    categorical_set = frozenset(str(c) for c in categorical_columns)
    numeric_set = frozenset(str(c) for c in numeric_domains)
    overlap = categorical_set & numeric_set
    if overlap:
        raise DpError(
            code="dp_column_declaration_overlap",
            message=(
                f"columns declared as both categorical and numeric: {sorted(overlap)!r}. "
                "Each column must be declared exactly once."
            ),
        )
    declared = categorical_set | numeric_set
    frame_columns = frozenset(str(c) for c in frame.columns)
    if declared != frame_columns:
        missing = frame_columns - declared
        extra = declared - frame_columns
        raise DpError(
            code="dp_column_declaration_incomplete",
            message=(
                "categorical_columns + numeric_domains must cover frame.columns exactly "
                f"once: missing from declarations={sorted(missing)!r}, declared but not in "
                f"frame={sorted(extra)!r}."
            ),
        )
    for col, bounds in numeric_domains.items():
        try:
            lower, upper = float(bounds[0]), float(bounds[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise DpError(
                code="dp_numeric_domain_invalid",
                message=f"numeric_domains[{col!r}] must be a (lower, upper) pair; got {bounds!r}.",
            ) from exc
        if not math.isfinite(lower) or not math.isfinite(upper) or not lower < upper:
            raise DpError(
                code="dp_numeric_domain_invalid",
                message=(
                    f"numeric_domains[{col!r}]=({lower!r}, {upper!r}) must have finite "
                    "lower < upper."
                ),
            )


def _normalize_numeric(series: pd.Series, *, lower: float, upper: float) -> list[float]:
    """Total, recordwise projection (guide section 4.2/7.1): every row
    contributes at most one element. Conversion failures and NaN become
    null (excluded); +-inf clamp to the declared bound; finite
    out-of-domain values clamp into [lower, upper]. Content can never
    raise -- there is no kind-selection branch here, only clamping.

    C-B2 (Codex round-3 blocker): this used to call the vectorized
    `pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)`. Codex
    demonstrated two content-dependent side channels in that path: (1) a
    column containing a Python/numpy `complex` value deterministically
    emits `ComplexWarning` on the cast to real, while an otherwise-
    identical neighbour without one emits no warning at all -- an
    observable with probability 0 on one neighbour and 1 on the other,
    which alone violates any (epsilon, delta) guarantee with delta < 1,
    independent of anything the fit releases; (2) `pd.to_numeric`'s
    object/complex coercion can silently produce garbage floats (an
    uninitialized buffer reinterpretation, not a `NaN`) instead of
    treating the value as a null conversion failure. Both close the same
    way: convert element-by-element with a blanket warning suppression
    around the whole pass, so NO warning is ever emitted regardless of
    content (not a per-type special case -- any future warning-emitting
    input closes the same way), and explicitly treat any `complex` value
    (covers the builtin type and `numpy.complex128`, which subclasses it)
    as an unconvertible failure before calling `float()`, so it becomes
    null like any other unconvertible value rather than silently keeping
    a real part.

    C-B4 (Codex round-4 blocker): the `except` here used to name
    `(TypeError, ValueError)`, which made the "content can never raise"
    claim above false. `float(10**10000)` raises `OverflowError`, so a
    one-row neighbour carrying a large Python int made the whole fit
    raise instead of emitting an artifact. Fit success/failure is itself
    an observable, and one with probability 0 on one neighbour and 1 on
    the other breaks (epsilon, delta) for any delta < 1 before a single
    released number is considered. Totality is the invariant, so the
    handler is now the totality itself rather than a list of the
    conversion errors we happened to think of: ANY failure converting a
    row's value drops that row, exactly as an unconvertible value
    already did. Each row still contributes at most one element, so
    add-or-remove-one-row stability is unchanged."""
    out: list[float] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for raw in series:
            if isinstance(raw, complex):
                continue  # unconvertible, like any other non-numeric value
            try:
                v = float(raw)
            except Exception:  # broad by design: totality, see docstring
                continue
            if math.isnan(v):
                continue
            if math.isinf(v):
                v = upper if v > 0 else lower
            else:
                v = min(max(v, lower), upper)
            out.append(v)
    return out


def _normalize_categorical(series: pd.Series) -> list[str]:
    """Total, recordwise projection: nulls excluded via `Series.dropna()`
    (uniform across None/NaN/NaT/pd.NA), every remaining scalar mapped to
    its `str()` representation -- the one documented canonical string
    form for every supported scalar dtype declared categorical. Wrapped in
    the same blanket warning suppression as `_normalize_numeric` (C-B2):
    `str()` on an exotic scalar type is not known to warn today, but
    suppression here costs nothing and keeps both normalizers under the
    same "no warning ever, regardless of content" invariant.

    C-B4 (Codex round-4 blocker): `str()` was assumed total and called
    bare. It is not. CPython caps integer-to-string conversion at 4300
    digits, so `str(10**10000)` raises `ValueError`, and a one-row
    neighbour carrying such a value made the entire fit raise rather
    than emit an artifact -- the same probability-0-vs-1 fit-success
    channel closed in `_normalize_numeric`. Any failure rendering a
    row's value now drops that row, which is what an unrepresentable
    label should have meant all along; each row still contributes at
    most one element.

    Totality is defined against what the release boundary can consume,
    not merely against `str()` returning. A lone surrogate such as
    `"\\ud800"` is a perfectly good Python `str` that cannot be encoded
    as UTF-8, and it raised `UnicodeEncodeError` further downstream when
    the label crossed into OpenDP -- the same fit-success channel, just
    relocated. Labels are therefore required to be UTF-8 encodable here.
    `str.isascii()` is a cached flag check, so the overwhelmingly common
    all-ASCII label pays no encoding cost."""
    out: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for raw in series.dropna():
            try:
                label = str(raw)
                if not label.isascii():
                    label.encode("utf-8")
            except Exception:  # broad by design: totality, see docstring
                continue
            out.append(label)
    return out


def fit_dp_snapshot(
    frame: pd.DataFrame,
    *,
    categorical_columns: Collection[str],
    numeric_domains: Mapping[str, tuple[float, float]],
    epsilon: float,
    delta: float,
    numeric_bins: int = _DEFAULT_NUMERIC_BINS,
) -> dict[str, Any]:
    """Public DP fit entrypoint. Always uses the real OpenDP-backed session.

    C-B1 (Codex round-3 blocker): the previous signature accepted a
    `_session_backend` keyword that let ANY caller of this public function
    substitute the mechanism entirely -- Codex demonstrated a backend whose
    `invoke()` returned exact source counts and whose `map()` claimed an
    arbitrary certificate, producing an artifact with the normal
    `dp_accounting` label and a plausible `epsilon_total` around exact
    values. A leading underscore is convention, not enforcement: nothing
    stopped a caller from passing it. This function now takes no
    mechanism/backend parameter of any name and always constructs
    `OpenDpReleaseSession`'s default real backend. The test seam moves to
    the private `_fit_dp_snapshot_with_backend` below, which this function
    is a thin, backend-fixed wrapper around; tests that need to observe
    `OpenDpReleaseSession`'s own bookkeeping (BLOCKER A2; defects 1a/1b)
    import and call that private function directly, never this one.

    Every released quantity is read back from an OpenDP `Measurement.map()`
    certificate (via `OpenDpReleaseSession`) and composed by `dp_accounting`
    (`quality/dp_budget.py`); this function computes no epsilon, delta,
    noise scale, or threshold of its own (guide section 3.3/4.3).

    Args:
        frame: Source data. Not mutated; never persisted exactly.
        categorical_columns: Column names to release as thresholded
            categorical marginals (guide section 4.5). Mandatory
            (may be empty).
        numeric_domains: `{column: (lower, upper)}` public domain per
            numeric column (guide section 4.4). Mandatory (may be
            empty). The union of `categorical_columns` and this mapping's
            keys must equal `frame.columns` exactly, with no overlap.
        epsilon: Fit-wide privacy budget request. Finite, > 0.
        delta: Fit-wide failure-probability request. Finite, strictly
            between 0 and 1 -- rejected at 0 even for an all-numeric fit
            (guide section 9.10 item 2).
        numeric_bins: Bin count per numeric column. Int >= 2, default 10
            (guide section 9.10 item 1); recorded in the artifact so bin
            edges are reproducible from public metadata alone.

    Returns:
        A `distribution-snapshot/v1` artifact whose additive `dp` block
        carries schema `dps-marginal/v2` (guide section 4.6): the
        accountant's composed `(epsilon_total, delta_total)`, a
        data-independent `release_id`, and the public schedule metadata
        needed to recompute `query_count` at compile time. No exact
        per-column scalar, no suppressed label, and no calibrated scale
        or threshold is ever emitted (guide section 4.2.1/4.6).

    Raises:
        DpError: malformed configuration (`dp_epsilon_invalid`,
            `dp_delta_invalid`, `dp_numeric_bins_invalid`,
            `dp_column_declaration_incomplete`,
            `dp_column_declaration_overlap`, `dp_numeric_domain_invalid`).
        DpBudgetError: ``code='dp_budget_infeasible'`` when the requested
            `(epsilon, delta)` cannot fund the fixed query schedule.
    """
    return _fit_dp_snapshot_with_backend(
        frame,
        categorical_columns=categorical_columns,
        numeric_domains=numeric_domains,
        epsilon=epsilon,
        delta=delta,
        numeric_bins=numeric_bins,
        _session_backend=None,
    )


def _fit_dp_snapshot_with_backend(
    frame: pd.DataFrame,
    *,
    categorical_columns: Collection[str],
    numeric_domains: Mapping[str, tuple[float, float]],
    epsilon: float,
    delta: float,
    numeric_bins: int = _DEFAULT_NUMERIC_BINS,
    _session_backend: _OpenDpBackend | None = None,
) -> dict[str, Any]:
    """Private implementation. `fit_dp_snapshot` (the public entrypoint) is
    a thin wrapper that always passes `_session_backend=None`; this module
    does not export this function's name, and no non-test code path in the
    codebase calls it directly (guide section 5 step 3; C-B1 above).

    `_session_backend`, when set, passes an `_OpenDpBackend` double
    straight into the `OpenDpReleaseSession` this call constructs, so a
    test can observe the session's OWN certificate/schedule bookkeeping
    independent of real OpenDP noise. `None` (the only value the public
    wrapper ever passes) always constructs the real OpenDP-backed session
    -- this function contains no branch that weakens the guarantee for a
    production caller, since production callers cannot reach this function
    at all, only `fit_dp_snapshot`.
    """
    _validate_config(
        frame=frame,
        categorical_columns=categorical_columns,
        numeric_domains=numeric_domains,
        epsilon=epsilon,
        delta=delta,
        numeric_bins=numeric_bins,
    )
    epsilon = float(epsilon)
    delta = float(delta)
    numeric_bins = int(numeric_bins)
    numeric_cols = sorted(str(c) for c in numeric_domains)
    categorical_cols = sorted(str(c) for c in categorical_columns)

    # The release ID is minted once, before any value is touched, and
    # depends on nothing but this call happening -- data-independent by
    # construction (guide section 4.2/9.7). Two calls over identical
    # inputs still mint distinct IDs; a byte-for-byte copy of one
    # artifact retains its ID (copying JSON does not re-run this line).
    release_id = uuid.uuid4().hex

    # Interior edges only: `numeric_bins` categories need `numeric_bins - 1`
    # interior cut points spanning the declared domain (guide section 4.4);
    # derived from the public domain + numeric_bins, never from data.
    def _interior_edges(lower: float, upper: float) -> tuple[float, ...]:
        return tuple(lower + (upper - lower) * i / numeric_bins for i in range(1, numeric_bins))

    schedule = Schedule(
        row_count_name="row_count",
        numeric=tuple(
            NumericQuerySpec(
                name=f"numeric:{c}",
                interior_edges=_interior_edges(
                    float(numeric_domains[c][0]), float(numeric_domains[c][1])
                ),
            )
            for c in numeric_cols
        ),
        categorical=tuple(
            CategoricalQuerySpec(
                grouped_name=f"categorical_grouped:{c}", total_name=f"categorical_total:{c}"
            )
            for c in categorical_cols
        ),
    )
    session = OpenDpReleaseSession(schedule, epsilon=epsilon, delta=delta, backend=_session_backend)

    row_count_released = _serialize_count(session.release_row_count(len(frame)))

    columns_block: dict[str, dict[str, Any]] = {}
    for col in numeric_cols:
        lower, upper = float(numeric_domains[col][0]), float(numeric_domains[col][1])
        values = _normalize_numeric(frame[col], lower=lower, upper=upper)
        raw_counts = session.release_numeric(f"numeric:{col}", values)
        bin_counts = [_serialize_count(c) for c in raw_counts]
        bin_edges = [lower, *_interior_edges(lower, upper), upper]
        non_null_count = sum(bin_counts)
        columns_block[col] = {
            "dtype": _DP_NUMERIC_DTYPE_LABEL,
            "kind": "numeric",
            "null_count": max(0, row_count_released - non_null_count),
            "non_null_count": non_null_count,
            "distinct_count": sum(1 for c in bin_counts if c > 0),
            "stats": {
                "bin_edges": bin_edges,
                "bin_counts": bin_counts,
                "min": lower,
                "max": upper,
                "mean": None,
                "std": None,
                "quantiles": {},
            },
        }

    for col in categorical_cols:
        cat_values = _normalize_categorical(frame[col])
        grouped_raw, total_raw = session.release_categorical(
            f"categorical_grouped:{col}", f"categorical_total:{col}", cat_values
        )
        # Serialize (round) only AFTER OpenDP has completed threshold
        # selection: `grouped_raw` already contains only the labels
        # OpenDP retained (guide section 4.5 step 5/6). Sort by
        # (-released_count, label) -- a total order because released
        # labels are distinct -- so the mechanism's emission/insertion
        # order can never leak into the artifact (section 4.5 step 7).
        retained = [(label, _serialize_count(count)) for label, count in grouped_raw.items()]
        retained.sort(key=lambda pair: (-pair[1], pair[0]))
        non_null_total = _serialize_count(total_raw)
        other_count = max(0, non_null_total - sum(count for _label, count in retained))
        columns_block[col] = {
            "dtype": _DP_CATEGORICAL_DTYPE_LABEL,
            "kind": "categorical",
            "null_count": max(0, row_count_released - non_null_total),
            "non_null_count": non_null_total,
            "distinct_count": len(retained) + (1 if other_count > 0 else 0),
            "stats": {
                "top_values": [{"value": label, "count": count} for label, count in retained],
                "other_count": other_count,
            },
        }

    if session.certificate_count() != schedule.query_count:
        # Section 4.3.5 mitigation 4, restated at the call site: this can
        # only fire if a future change bypasses the loop structure above,
        # since every scheduled query is released exactly once by
        # construction here. Kept as a hard assertion, not a comment,
        # because it is what proves the schedule was actually exhausted.
        raise DpBudgetError(
            code="dp_schedule_mismatch",
            message=(
                f"certified {session.certificate_count()} releases but the schedule "
                f"declares {schedule.query_count} queries."
            ),
        )
    epsilon_total, delta_total = session.composed_loss()

    opendp_version, dp_accounting_version = _dp_versions()
    return {
        "schema_version": DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
        "row_count": row_count_released,
        "columns": columns_block,
        "joints": [],
        "dp": {
            "schema": DP_SNAPSHOT_SCHEMA_VERSION,
            "release_id": release_id,
            "scope": DP_RELEASE_SCOPE,
            "adjacency": DP_ADJACENCY,
            "epsilon_total": epsilon_total,
            "delta_total": delta_total,
            "accountant": DP_ACCOUNTANT_LABEL,
            "opendp_version": opendp_version,
            "dp_accounting_version": dp_accounting_version,
            "query_count": schedule.query_count,
            "numeric_bins": numeric_bins,
            "categorical_columns": categorical_cols,
            "numeric_domains": {
                c: [float(numeric_domains[c][0]), float(numeric_domains[c][1])]
                for c in numeric_cols
            },
        },
    }


def _serialize_count(value: float) -> int:
    """`max(0, int(round(v)))` -- the one place a released noisy value
    becomes a serialized count, and only after OpenDP has completed
    whatever selection it was going to do (guide section 4.4 step 7 /
    4.5 step 6)."""
    return max(0, round(float(value)))
