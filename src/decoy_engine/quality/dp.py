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
import itertools
import logging
import math
import uuid
from collections.abc import Collection, Mapping
from typing import Any

import pandas as pd

from decoy_engine.quality.dp_budget import (
    DpBudgetError,
    OpenDpReleaseSession,
    _OpenDpBackend,
)
from decoy_engine.quality.dp_normalize import _normalize_categorical, _normalize_numeric
from decoy_engine.quality.dp_schedule import CategoricalQuerySpec, NumericQuerySpec, Schedule
from decoy_engine.quality.snapshot import (
    DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
    DP_SNAPSHOT_SCHEMA_VERSION,
)

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

# Fixed, content-independent description of what normalization releases.
# Identical bytes in every artifact: a policy that varied with the frame
# would be an unnoised channel, which is precisely why the per-column
# drop COUNT below goes to the log and never in here.
_DP_NORMALIZATION_POLICY = {
    "categorical_labels": (
        "text kept verbatim unless it contains NUL or cannot be encoded as UTF-8; "
        "boolean, real, decimal and zero-imaginary complex rendered from the float64 "
        "image; integers beyond float64 range rendered exactly, up to the interpreter's "
        "decimal-conversion limit"
    ),
    "categorical_unsupported": (
        "released as null (datetime, timedelta, NUL-bearing or non-UTF-8 text, and any other type)"
    ),
    "numeric_values": (
        "float64, values outside the declared domain clamped to it, "
        "infinities clamped to the nearer bound, NaN released as null"
    ),
}

_logger = logging.getLogger(__name__)


def _log_normalization_policy() -> None:
    """State the policy once per fit, unconditionally.

    Round 8 shipped this as a per-column count of dropped values, and
    round 9 blocked it twice over.

    It was an observable (Codex): the record was emitted only when a drop
    occurred, so its presence is a probability-0-vs-1 function of the
    data, carrying an exact count. The round-8 rationale -- that the
    fitting party already holds the frame, so a local signal discloses
    nothing -- does not hold, because a logger is not intrinsically
    local; a caller can attach a centralized handler. It also
    contradicted this program's own rule that no scalar may "warn, or
    otherwise become observable".

    It reopened C-B4 (dennis): the count called `series.notna()`, a
    vectorized nullness check that runs each value's own dunders, from
    OUTSIDE the conversion guard. `pandas.isna(Decimal("sNaN"))` raises
    `InvalidOperation`, so a one-row neighbour made the whole fit raise
    where its neighbour emitted an artifact -- the exact fit-success
    channel `dp_normalize` exists to close, reintroduced by the
    remediation that was meant to improve the operator's signal.

    So the message is now fixed text on every fit: it never reads a
    value, never counts, and never branches. The operator learns that
    unlabellable values are released as nulls and can pair that with a
    column's own released `non_null_count` -- which is noised, so it is
    not a channel either. An exact per-column diagnostic, if one is ever
    wanted, belongs in a separately invoked, explicitly non-DP audit
    operation, never as a side effect of the protected fit.
    """
    _logger.info(
        "dp fit: categorical columns release only text, boolean and numeric values; "
        "datetimes, timedeltas and other types are released as nulls. This message is "
        "fixed and does not indicate whether any value in this frame was affected."
    )


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

    # D-MEDIUM-1 (dennis round 6) / C-L-1 (Codex round 6): round 5 closed
    # the non-string LABEL case on the frame side only. The mirror case
    # is a non-string DECLARATION key: with a string frame column "5" and
    # `numeric_domains={5: (0.0, 1.0)}`, every check below passed (the
    # sets are compared stringified, and the bounds loop reads the
    # original int key), and the fit then indexed `numeric_domains["5"]`
    # and died on a bare `KeyError` from an entrypoint documenting only
    # `DpError`. The categorical side was worse than inconsistent: it
    # SUCCEEDED silently, since that path only ever needs the name.
    # Reject both, under the same code, before anything is read.
    non_string_decls = sorted(
        repr(c) for c in (*categorical_columns, *numeric_domains) if not isinstance(c, str)
    )
    if non_string_decls:
        raise DpError(
            code="dp_column_label_not_a_string",
            message=(
                f"non-string column declarations: {non_string_decls}. Declare columns under "
                "their exact string names; a stringified key does not address the frame."
            ),
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
    # D-M4: `frame.columns` is compared as a SET below, so duplicate
    # labels pass the coverage check. `frame[col]` then returns a
    # DataFrame rather than a Series, and both normalizers iterate it as
    # column LABELS -- the fit is accepted and releases a distribution
    # over the label string instead of the data. Nothing leaks (it
    # describes none of the values), but a DP artifact that silently
    # describes the wrong thing is worse than a rejected one.
    # D-L-A (dennis round 5): every comparison below is on `str(label)`,
    # but the fit loop indexes the frame with that STRINGIFIED name. An
    # integer column `5` declared as `numeric_domains={5: ...}` therefore
    # passed validation and then died on `frame["5"]` with a bare
    # `KeyError`. Same class as the derived-edges break above: fail
    # closed, no leak, wrong exception type. Rejected with a coded error
    # rather than silently supported, since indexing by the original
    # label is a wider change than this build should make and the path
    # does not work today.
    non_string = sorted(repr(c) for c in frame.columns if not isinstance(c, str))
    if non_string:
        raise DpError(
            code="dp_column_label_not_a_string",
            message=(
                f"frame has non-string column labels: {non_string}. Declare and fit columns "
                "under string names; rename the frame's columns before fitting."
            ),
        )
    frame_labels = [str(c) for c in frame.columns]
    duplicated = sorted({label for label in frame_labels if frame_labels.count(label) > 1})
    if duplicated:
        raise DpError(
            code="dp_column_declaration_duplicated",
            message=(
                f"frame has duplicate column labels: {duplicated!r}. Each column must be "
                "declared and fit exactly once; deduplicate or rename before fitting."
            ),
        )
    declared = categorical_set | numeric_set
    frame_columns = frozenset(frame_labels)
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
        # D-M-B (dennis round 5): finite `lower < upper` is not enough.
        # The bin edges DERIVED from the declaration can still overflow
        # or collapse to non-unique values, and OpenDP then rejects them
        # at the FFI with a raw `OpenDPException` ("edges must be unique
        # and ordered") -- after the row-count release has already
        # charged the session. `(0.0, 1.7e308)` is a plausible "just give
        # it a wide domain" input, and so is a pair of adjacent floats
        # under a high `numeric_bins`. This module documents that it
        # raises coded `DpError`, so the same edges are derived here,
        # from PUBLIC declarations only, and checked before any value is
        # touched.
        edges = [lower + (upper - lower) * i / numeric_bins for i in range(1, numeric_bins)]
        full = [lower, *edges, upper]
        if not all(math.isfinite(e) for e in full) or any(
            b <= a for a, b in itertools.pairwise(full)
        ):
            raise DpError(
                code="dp_numeric_domain_invalid",
                message=(
                    f"numeric_domains[{col!r}]=({lower!r}, {upper!r}) with "
                    f"numeric_bins={numeric_bins} derives bin edges that are not finite and "
                    "strictly increasing (the domain is too wide, or too narrow for this many "
                    "bins). Narrow the domain or reduce numeric_bins."
                ),
            )


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
    # D-LOW-1 (dennis round 6): validate what you actually use. The
    # declarations used to be read three separate times -- once by the
    # validation loop, once building the `Schedule`, once by the fit loop
    # -- so a `Mapping` whose reads differ could pass every check and
    # then hand OpenDP different bounds, landing a raw `OpenDPException`
    # AFTER `release_row_count` had already charged the session, which is
    # the exact failure the derived-edge guard exists to prevent. Read
    # the caller's declarations exactly once here; everything downstream,
    # validation included, sees only this snapshot.
    numeric_domains = dict(numeric_domains)
    categorical_columns = tuple(categorical_columns)
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

    _log_normalization_policy()

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
            "normalization_policy": _DP_NORMALIZATION_POLICY,
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
