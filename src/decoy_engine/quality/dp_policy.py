"""The DP fit's own description of what normalization released.

Split out of `quality/dp.py` to keep that module under the 600-LOC
orchestration cap, following the `dp_schedule.py`/`dp_ledger.py`
precedent. Small and cohesive: the policy text and the one log line that
states it, both of which must stay content-independent.
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


# Fixed, content-independent description of what normalization releases.
# Identical bytes in every artifact: a policy that varied with the frame
# would be an unnoised channel. Round 9 deleted the per-column drop
# COUNT this comment used to point at; nothing below logs one.
_DP_NORMALIZATION_POLICY = {
    "categorical_labels": (
        "a declared text column releases only genuine str cells, kept verbatim "
        "unless the value AS RECEIVED contains NUL or cannot be encoded as UTF-8 "
        "(numpy fixed-width string storage strips a trailing NUL before the fit "
        "sees it); a declared flag column releases the two canonical tokens 'true' "
        "and 'false'. The carrier declared for the column drives this, not how "
        "pandas happens to store the column"
    ),
    "categorical_unsupported": (
        "released as null: in a text column every non-str cell (a boolean, any "
        "number, decimal or complex value, a datetime or timedelta, a container, and "
        "any other type); in a flag column every non-boolean cell; and in either, "
        "text whose value AS RECEIVED carries NUL or is not UTF-8 encodable"
    ),
    "numeric_values": (
        "float64, values outside the declared domain clamped to it, "
        "infinities clamped to the nearer bound, NaN released as null"
    ),
    "numeric_unsupported": (
        "released as null (a list, tuple or array cell; a datetime or "
        "timedelta; a complex value with a nonzero imaginary part; and any "
        "value that cannot be converted to a float)"
    ),
}


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
    channel `dp_normalize` existed to close (now the carrier codecs',
    see `quality/carriers.py`), reintroduced by the remediation that was
    meant to improve the operator's signal.

    So the message is now fixed text on every fit: it never reads a
    value, never counts, and never branches. The operator learns that
    unlabellable values are released as nulls and can pair that with a
    column's own released `non_null_count` -- which is noised, so it is
    not a channel either. An exact per-column diagnostic, if one is ever
    wanted, belongs in a separately invoked, explicitly non-DP audit
    operation, never as a side effect of the protected fit.
    """
    _logger.info(
        "dp fit: a declared text column releases only genuine string values and a "
        "declared flag column releases 'true'/'false'; every other cell (numbers, a "
        "boolean in a text column, datetimes, timedeltas and other types) is released "
        "as null. This message is fixed and does not indicate whether any value in "
        "this frame was affected."
    )
