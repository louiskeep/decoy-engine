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

DPS-CODEC phase 5 wiring (guide sections 3.5/3.6/3.8/3.9). The recordwise
projection is now ONE canonical typed-carrier layer (`quality/carriers.py`)
converted through a single total, boxing-invariant codec per carrier, not the
scattered pandas value-level inference of `dp_normalize.py`. A `DataFrame`
source routes through the lazily-imported pandas adapter
(`quality/carrier_adapter.py`); a directly-supplied `CarrierTable` routes
through `sanitize_carrier_table` WITHOUT importing the adapter (OpenDP itself
still loads pandas transitively -- the pandas-free guarantee is over the codec
CORE, `carriers.py`, not the full OpenDP-backed fit). The number/text carriers
keep the exact str/number OpenDP release the fit landed with (the phase-1
codecs reproduce `_normalize_*` byte-for-byte on the values in their domain);
the `flag` carrier takes the phase-4 bool-domain measurement pair. Before any
private cell is read the fit fails closed on an uncertified proof stack
(`check_fit_environment`, section 3.8) and records that identity into the
`dps-marginal/v3` artifact so generation can re-validate it.

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
from typing import TYPE_CHECKING, Any

from decoy_engine.quality.carriers import (
    CarrierError,
    CarrierTable,
    _validate_bound,
    released_values,
    sanitize_carrier_table,
)
from decoy_engine.quality.dp_budget import (
    DpBudgetError,
    OpenDpReleaseSession,
    _OpenDpBackend,
)
from decoy_engine.quality.dp_policy import (
    _DP_NORMALIZATION_POLICY,
    _log_normalization_policy,
)
from decoy_engine.quality.dp_provenance import check_fit_environment, current_provenance
from decoy_engine.quality.dp_schedule import CategoricalQuerySpec, NumericQuerySpec, Schedule
from decoy_engine.quality.dp_schema import (
    DP_CODEC_ID,
    DP_CODEC_VERSION,
    DP_SNAPSHOT_SCHEMA_VERSION,
)
from decoy_engine.quality.snapshot import DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION

if TYPE_CHECKING:
    # Annotation-only: the direct-`CarrierTable` path must not import pandas,
    # so `pandas` is never imported at module load (the eager `import pandas as
    # pd` this module carried through phase 4 is gone). The DataFrame adapter is
    # imported lazily, inside the fit, only when a DataFrame is actually passed.
    import pandas as pd

DP_RELEASE_SCOPE = "single-column-marginals"
DP_ADJACENCY = "add-remove-one-row"
DP_ACCOUNTANT_LABEL = "dp_accounting PLD composition over OpenDP privacy maps"

_DEFAULT_NUMERIC_BINS = 10

# The emitted `dtype` label is a function of the caller's public carrier
# declaration, never of the private frame's storage dtype (guide section
# 4.2.1): number releases float64 counts, text releases `str` labels
# ("object"), flag releases the two canonical bool tokens ("bool"). Reading the
# frame's own pandas dtype would be content-dependent (an int column upcasts to
# float64 the moment a null enters it) -- the leak the carrier redesign closes.
_DP_NUMERIC_DTYPE_LABEL = "float64"
_DP_CATEGORICAL_DTYPE_LABEL = "object"
_DP_FLAG_DTYPE_LABEL = "bool"

# The closed release-kind x carrier table (guide section 3.3). `kind` is
# OPTIONAL in `column_schema` (the carrier alone drives the codec and the
# mechanism), but when a caller supplies a `kind` it is validated against this
# table so an impossible pair (categorical + number, which OpenDP has no float
# `make_count_by` for) fails loud rather than silently mis-releasing. Kept in
# sync with `carrier_adapter._KIND_TO_CARRIERS` so the DataFrame and direct
# paths accept exactly the same schema.
_KIND_TO_CARRIERS: dict[str, tuple[str, ...]] = {
    "numeric": ("number",),
    "categorical": ("text", "flag"),
}

_logger = logging.getLogger(__name__)


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


def _validate_fit_params(*, epsilon: float, delta: float, numeric_bins: int) -> None:
    """Validate the fit-level knobs. Reads only public request parameters, never
    a value (guide section 4.2). Positivity/range checks here mirror what the
    landed fit enforced; the per-column carrier/bounds validation moved to the
    carrier layer (`_parse_column_schema` + `sanitize_carrier_table` / the
    adapter)."""
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


def _parse_column_schema(
    column_schema: Any,
) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """Split a validated `column_schema` into numeric bounds and categorical
    carriers for schedule construction.

    Reads only the public schema, never a value, so it runs BEFORE the proof
    stack gate and BEFORE any private cell is fetched. Structural problems (not
    a dict, a bad carrier, a malformed/misordered number bound, an impossible
    kind x carrier pair) fail loud with the SAME coded `CarrierError` the
    carrier layer raises, so the DataFrame and direct paths reject an identical
    schema identically. The authoritative per-cell FFI-safety validation is
    still `sanitize_carrier_table`'s (run on every input); this is only what the
    schedule needs to know a column's mechanism domain and bin edges."""
    if not isinstance(column_schema, dict):
        raise CarrierError(
            code="dp_schema_type",
            message=f"column_schema must be a dict, got {type(column_schema).__name__}",
        )
    numeric_bounds: dict[str, tuple[float, float]] = {}
    categorical_carriers: dict[str, str] = {}
    for name, spec in column_schema.items():
        if not isinstance(spec, dict):
            raise CarrierError(
                code="dp_schema_column_type",
                message=f"column {name!r}: schema entry must be a dict, got {type(spec).__name__}",
            )
        carrier = spec.get("carrier")
        if not isinstance(carrier, str) or carrier not in ("number", "flag", "text"):
            raise CarrierError(
                code="dp_carrier_unknown",
                message=(
                    f"column {name!r}: unknown carrier {carrier!r}, expected one of "
                    "('number', 'flag', 'text')"
                ),
            )
        kind = spec.get("kind")
        if kind is not None:
            if not isinstance(kind, str) or kind not in _KIND_TO_CARRIERS:
                raise CarrierError(
                    code="dp_kind_unknown",
                    message=(
                        f"column {name!r}: unknown kind {kind!r}, expected one of "
                        f"{tuple(_KIND_TO_CARRIERS)}"
                    ),
                )
            allowed = _KIND_TO_CARRIERS[kind]
            if carrier not in allowed:
                raise CarrierError(
                    code="dp_kind_carrier_mismatch",
                    message=(
                        f"column {name!r}: kind {kind!r} does not allow carrier {carrier!r} "
                        f"(allowed: {allowed})"
                    ),
                )
        if carrier == "number":
            bounds = spec.get("bounds")
            if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                raise CarrierError(
                    code="dp_carrier_bounds_missing",
                    message=f"column {name!r}: a 'number' carrier requires (lower, upper) bounds",
                )
            lower = _validate_bound(name, "lower", bounds[0])
            upper = _validate_bound(name, "upper", bounds[1])
            if not lower < upper:
                raise CarrierError(
                    code="dp_carrier_bounds_order",
                    message=f"column {name!r}: bounds must satisfy lower < upper, got ({lower}, {upper})",
                )
            numeric_bounds[name] = (lower, upper)
        else:
            categorical_carriers[name] = carrier
    return numeric_bounds, categorical_carriers


def _interior_edges(lower: float, upper: float, numeric_bins: int) -> tuple[float, ...]:
    """Interior cut points only: `numeric_bins` categories need `numeric_bins -
    1` interior edges spanning the declared domain (guide section 4.4); derived
    from the public domain + numeric_bins, never from data."""
    return tuple(lower + (upper - lower) * i / numeric_bins for i in range(1, numeric_bins))


def _check_derived_edges(name: str, lower: float, upper: float, numeric_bins: int) -> None:
    """Reject a declaration whose DERIVED bin edges overflow or collapse to
    non-unique values (guide section 4.4; carried forward from the landed fit's
    D-M-B guard).

    A finite `lower < upper` is not enough: `(0.0, 1.7e308)` overflows the edge
    arithmetic and a pair of adjacent floats under a high `numeric_bins`
    collapses to non-strictly-increasing edges, which OpenDP would otherwise
    reject with a raw `OpenDPException` at the FFI -- AFTER the row-count release
    has already charged the session. Derived here from PUBLIC declarations only,
    before any value is touched, and raised as a coded `DpError`."""
    full = [lower, *_interior_edges(lower, upper, numeric_bins), upper]
    if not all(math.isfinite(e) for e in full) or any(b <= a for a, b in itertools.pairwise(full)):
        raise DpError(
            code="dp_numeric_domain_invalid",
            message=(
                f"column {name!r}=({lower!r}, {upper!r}) with numeric_bins={numeric_bins} "
                "derives bin edges that are not finite and strictly increasing (the domain is "
                "too wide, or too narrow for this many bins). Narrow the domain or reduce "
                "numeric_bins."
            ),
        )


def _flag_token(value: object) -> str:
    """The canonical serialized token for a flag category (guide section 3.4):
    `"true"`/`"false"`, never Python `str(bool)` `"True"/"False"` and never
    `"0"/"1"`. The generation-side decoder (phase 6) is the inverse."""
    return "true" if value else "false"


def fit_dp_snapshot(
    source: pd.DataFrame | CarrierTable,
    column_schema: dict[str, dict],
    *,
    epsilon: float,
    delta: float,
    numeric_bins: int = _DEFAULT_NUMERIC_BINS,
) -> dict[str, Any]:
    """Public DP fit entrypoint. Always uses the real OpenDP-backed session.

    The fit routes the source through the canonical typed-carrier layer (guide
    sections 3.5/3.6): a `pandas.DataFrame` goes through the lazily-imported
    adapter (`dataframe_to_carrier_table`), a directly-supplied `CarrierTable`
    goes straight through `sanitize_carrier_table` without importing the
    adapter. Either way the per-column released vector comes from the sanitized
    `CarrierTable`, so `DataFrame -> CarrierTable -> OpenDP vector` (and the
    direct path) is stability-1 by construction, subject to the guide section
    3.7 residual exclusions.

    Before any private cell is read the fit fails closed on an uncertified proof
    stack (`check_fit_environment`) and on an over-ceiling epsilon
    (`check_epsilon_supported`, inside the session). It records the certified
    proof-stack identity into the `dps-marginal/v3` artifact so generation can
    re-validate it.

    Args:
        source: A `pandas.DataFrame` (routed through the pandas adapter) OR a
            canonical `CarrierTable` (routed directly, pandas-free). Not mutated.
        column_schema: `{name: {"carrier": ..., "kind"?: ..., "bounds"?: ...}}`.
            `carrier` is `"number"` (histogram-count marginal; requires
            `(lower, upper)` `bounds`), `"text"` (str-domain categorical), or
            `"flag"` (bool-domain categorical); an optional `kind`
            (`"numeric"`/`"categorical"`) is checked against the closed kind x
            carrier table. This is the ONLY per-column declaration; it replaces
            the removed `categorical_columns`/`numeric_domains` (pre-GA hard
            break, guide section 6). For a `DataFrame` the schema's columns must
            exist in the frame (extra frame columns are ignored); for a
            `CarrierTable` the schema's keys must equal the table's columns.
        epsilon: Fit-wide privacy budget request. Finite, > 0.
        delta: Fit-wide failure-probability request. Finite, in (0, 1) --
            rejected at 0 even for an all-numeric fit.
        numeric_bins: Bin count per numeric column. Int >= 2, default 10; one
            fit-level value for v1, recorded in the artifact so bin edges are
            reproducible from public metadata alone.

    Returns:
        A `distribution-snapshot/v1` artifact whose additive `dp` block carries
        schema `dps-marginal/v3` (guide section 3.9): the accountant's composed
        `(epsilon_total, delta_total)`, a data-independent `release_id`, the
        `column_schema` with per-column carriers, the codec id/version, the
        recorded proof-stack identity, the source `boundary`, and the public
        schedule metadata. No exact per-column scalar, no suppressed label, and
        no calibrated scale or threshold is ever emitted.

    Raises:
        DpError: malformed fit-level configuration (`dp_epsilon_invalid`,
            `dp_delta_invalid`, `dp_numeric_bins_invalid`,
            `dp_numeric_domain_invalid`).
        CarrierError: malformed `column_schema`, bounds, or a per-cell/shape
            violation the carrier layer refuses (see `quality/carriers.py` /
            `quality/carrier_adapter.py`).
        ProvenanceError: the running proof stack is not a certified row
            (`dp_platform_uncertified`, `dp_stack_uncertified`).
        DpBudgetError: `dp_epsilon_unsupported` (over the ceiling) or
            `dp_budget_infeasible` (the request cannot fund the schedule).
    """
    return _fit_dp_snapshot_with_backend(
        source,
        column_schema,
        epsilon=epsilon,
        delta=delta,
        numeric_bins=numeric_bins,
        _session_backend=None,
    )


def _fit_dp_snapshot_with_backend(
    source: pd.DataFrame | CarrierTable,
    column_schema: dict[str, dict],
    *,
    epsilon: float,
    delta: float,
    numeric_bins: int = _DEFAULT_NUMERIC_BINS,
    _session_backend: _OpenDpBackend | None = None,
) -> dict[str, Any]:
    """Unsupported internal seam. `fit_dp_snapshot` is a thin wrapper that
    always passes `_session_backend=None`; this module does not export this
    name, and no non-test code path calls it directly (guide section 4
    private-seam finding). `_session_backend`, when set, passes an
    `_OpenDpBackend` double into the `OpenDpReleaseSession` this call constructs,
    so a test can observe the session's OWN certificate/schedule bookkeeping
    independent of real OpenDP noise. Production callers cannot reach this
    function at all, only `fit_dp_snapshot`."""
    _validate_fit_params(epsilon=epsilon, delta=delta, numeric_bins=numeric_bins)
    epsilon = float(epsilon)
    delta = float(delta)
    numeric_bins = int(numeric_bins)

    # Parse the schema (public-only) into the mechanism domains and bounds the
    # schedule commits to, and reject degenerate derived bin edges -- all BEFORE
    # the proof-stack gate and before any private cell is read.
    numeric_bounds, categorical_carriers = _parse_column_schema(column_schema)
    numeric_cols = sorted(numeric_bounds)
    categorical_cols = sorted(categorical_carriers)
    for col in numeric_cols:
        lower, upper = numeric_bounds[col]
        _check_derived_edges(col, lower, upper, numeric_bins)

    # Fail closed on an uncertified proof stack BEFORE reading private data
    # (guide section 3.8): the (epsilon, delta) guarantee is only honest on a
    # tested platform + locked distribution set. Record the certified identity
    # into the artifact so generation can re-validate it without recomputing a
    # fingerprint from its own (possibly different) libraries.
    check_fit_environment()
    provenance = current_provenance()

    # The release ID is minted once, before any value is touched, and depends on
    # nothing but this call happening -- data-independent by construction. A
    # byte-for-byte copy of one artifact retains its ID (copying JSON does not
    # re-run this line).
    release_id = uuid.uuid4().hex

    schedule = Schedule(
        row_count_name="row_count",
        numeric=tuple(
            NumericQuerySpec(
                name=f"numeric:{c}",
                interior_edges=_interior_edges(
                    numeric_bounds[c][0], numeric_bounds[c][1], numeric_bins
                ),
            )
            for c in numeric_cols
        ),
        categorical=tuple(
            CategoricalQuerySpec(
                grouped_name=f"categorical_grouped:{c}",
                total_name=f"categorical_total:{c}",
                carrier=categorical_carriers[c],
            )
            for c in categorical_cols
        ),
    )
    # Session construction runs the allocation search (data-independent) and
    # fails closed on an over-ceiling / NaN epsilon via `check_epsilon_supported`
    # before any composition -- still before any private cell is read.
    session = OpenDpReleaseSession(schedule, epsilon=epsilon, delta=delta, backend=_session_backend)

    # Convert the source to a certified `CarrierTable`. THIS reads private data,
    # so it comes after the proof-stack gate. A `CarrierTable` routes directly
    # through `sanitize_carrier_table` (no pandas adapter imported); a DataFrame
    # routes through the lazily-imported adapter.
    if isinstance(source, CarrierTable):
        boundary = "direct"
        table = sanitize_carrier_table(source, column_schema)
    else:
        boundary = "adapter"
        from decoy_engine.quality.carrier_adapter import dataframe_to_carrier_table

        table = dataframe_to_carrier_table(source, column_schema)
    released = released_values(table)

    row_count_released = _serialize_count(session.release_row_count(table.row_count))

    columns_block: dict[str, dict[str, Any]] = {}
    for col in numeric_cols:
        lower, upper = numeric_bounds[col]
        numeric_values = [float(v) for v in released[col]]
        raw_counts = session.release_numeric(f"numeric:{col}", numeric_values)
        bin_counts = [_serialize_count(c) for c in raw_counts]
        bin_edges = [lower, *_interior_edges(lower, upper, numeric_bins), upper]
        non_null_count = sum(bin_counts)
        columns_block[col] = {
            "dtype": _DP_NUMERIC_DTYPE_LABEL,
            "kind": "numeric",
            "carrier": "number",
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
        carrier = categorical_carriers[col]
        cat_values: list[Any]
        if carrier == "flag":
            cat_values = [bool(v) for v in released[col]]
        else:
            cat_values = list(released[col])
        grouped_raw, total_raw = session.release_categorical(
            f"categorical_grouped:{col}", f"categorical_total:{col}", cat_values
        )
        # Serialize (round) only AFTER OpenDP has completed threshold selection:
        # `grouped_raw` already holds only the labels OpenDP retained. A flag
        # column's bool keys serialize to the canonical "true"/"false" tokens
        # (guide section 3.4). Sort by (-released_count, token) -- a total order
        # because released tokens are distinct -- so the mechanism's
        # emission/insertion order can never leak into the artifact.
        if carrier == "flag":
            retained = [
                (_flag_token(label), _serialize_count(count))
                for label, count in grouped_raw.items()
            ]
            dtype_label = _DP_FLAG_DTYPE_LABEL
        else:
            retained = [(label, _serialize_count(count)) for label, count in grouped_raw.items()]
            dtype_label = _DP_CATEGORICAL_DTYPE_LABEL
        retained.sort(key=lambda pair: (-pair[1], pair[0]))
        non_null_total = _serialize_count(total_raw)
        other_count = max(0, non_null_total - sum(count for _label, count in retained))
        columns_block[col] = {
            "dtype": dtype_label,
            "kind": "categorical",
            "carrier": carrier,
            "null_count": max(0, row_count_released - non_null_total),
            "non_null_count": non_null_total,
            "distinct_count": len(retained) + (1 if other_count > 0 else 0),
            "stats": {
                "top_values": [{"value": label, "count": count} for label, count in retained],
                "other_count": other_count,
            },
        }

    if session.certificate_count() != schedule.query_count:
        # Section 4.3.5 mitigation 4, restated at the call site: this can only
        # fire if a future change bypasses the loop structure above, since every
        # scheduled query is released exactly once by construction here. Kept as
        # a hard assertion because it is what proves the schedule was exhausted.
        raise DpBudgetError(
            code="dp_schedule_mismatch",
            message=(
                f"certified {session.certificate_count()} releases but the schedule "
                f"declares {schedule.query_count} queries."
            ),
        )
    epsilon_total, delta_total = session.composed_loss()

    opendp_version, dp_accounting_version = _dp_versions()
    recorded_schema = {
        col: (
            {"kind": "numeric", "carrier": "number", "bounds": list(numeric_bounds[col])}
            if col in numeric_bounds
            else {"kind": "categorical", "carrier": categorical_carriers[col]}
        )
        for col in (*numeric_cols, *categorical_cols)
    }
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
                c: [numeric_bounds[c][0], numeric_bounds[c][1]] for c in numeric_cols
            },
            # v3 additions (guide section 3.9).
            "column_schema": recorded_schema,
            "codec": {"id": DP_CODEC_ID, "version": DP_CODEC_VERSION},
            "boundary": boundary,
            "provenance": {
                "platform": dict(provenance.platform._asdict()),
                "cpython": provenance.cpython,
                "fingerprint": provenance.fingerprint,
            },
        },
    }


def _serialize_count(value: float) -> int:
    """`max(0, int(round(v)))` -- the one place a released noisy value becomes a
    serialized count, and only after OpenDP has completed whatever selection it
    was going to do (guide section 4.4 step 7 / 4.5 step 6)."""
    return max(0, round(float(value)))
