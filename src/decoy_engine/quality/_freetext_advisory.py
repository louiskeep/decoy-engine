"""Compile-time clinical free-text advisory warn-gate (HC-7).

Mirrors `quality/_retention_gate.py`'s shape one-for-one: a pure scorer
plus a logger-only wrapper, never raising, never mutating anything it
scores, never changing output bytes.

Why this exists: an operator who leaves a `clinical_notes` /
`claim_description`-style long free-text column unmasked (explicit
`strategy: passthrough`) gets no signal that the column likely carries
unstructured PHI. The engine never auto-assigns a masking strategy --
`ColumnConfig.strategy` is a required user field and compile only
validates declared strategies, it never invents one (standing product
decision: auto-classification is abandoned by design, the user picks the
PII type per field). This gate is the visibility signal, nothing more:
it names the column and recommends `strategy: text_mask`; the operator
decides.

The heuristic (both branches gated on the column being a passthrough
string column -- see `plan/_checks_freetext_advisory.py` for how the
per-column view is assembled):

1. Name-hint (primary, always-available): `matches_freetext_name` fires
   regardless of length/distinctness data being available, so a column
   with no profile stats (e.g. a stale profile predating this feature,
   or `--no-profile`) still gets caught by name alone.
2. Length + distinctness (fallback, for oddly-named columns): fires only
   when both `avg_length` and `distinct_count` are known. LENGTH is the
   load-bearing discriminator, not optional -- a high-cardinality ICD-10
   *code* column (HC-5) also has high distinctness but SHORT values
   (avg ~6 chars); only average length separates long prose (avg ~200)
   from short high-cardinality codes. A distinctness-only heuristic would
   false-positive on exactly the code columns HC-5 protects.

A column already routed to a real masking strategy (anything other than
`passthrough`) is never considered here -- the operator already handled
it; `score_unmasked_freetext` enforces this directly (`strategy !=
"passthrough"` is the first gate in its loop), so it holds even if a
caller passes an unfiltered column list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from decoy_engine.quality._freetext_names import matches_freetext_name

_log = logging.getLogger(__name__)

DEFAULT_FREETEXT_ADVISORY_MIN_AVG_LENGTH = 40.0
DEFAULT_FREETEXT_ADVISORY_MIN_DISTINCTNESS = 0.5

# dtype labels `profile.ColumnProfile.dtype` uses for string-shaped columns
# (see `internal.pandas_compat.canonical_dtype_label`): plain object dtype
# (pandas' default for text) and the opt-in `string` / `string[pyarrow]`
# extension dtypes. Anything else (numeric, bool, datetime, category, ...)
# is definitively not a string column.
_STRING_DTYPE_PREFIXES = ("object", "string")


def is_string_dtype_label(dtype: str) -> bool:
    """True if a `ColumnProfile.dtype` label denotes a string-shaped column."""
    return dtype.startswith(_STRING_DTYPE_PREFIXES)


@dataclass(frozen=True)
class FreetextColumnView:
    """The minimal per-column view `score_unmasked_freetext` needs.

    `dtype_known_non_string` is a tri-state expressed as a bool: True only
    when the caller has POSITIVE evidence (a resolved profile column) that
    the dtype is not string-shaped. When the caller has no profile column
    to check (missing profile, or a stale profile predating this field),
    it must pass False here -- absence of evidence is not evidence of a
    non-string column, and the name-hint branch must still be able to fire
    (the compile-time degrade-to-name-hint-only contract).
    """

    name: str
    strategy: str
    dtype_known_non_string: bool
    avg_length: float | None
    distinct_count: int | None
    non_null_count: int | None


def freetext_advisory_min_avg_length(config: dict[str, Any]) -> float:
    """Read `global_settings.freetext_advisory_min_avg_length` with the
    model default. A value `<= 0` is the sentinel that disables the gate
    entirely (both branches) -- see `score_unmasked_freetext`.

    Compile accepts unvalidated dicts, so the default must be applied here
    as well as in the `GlobalSettings` model.
    """
    raw = (config.get("global_settings") or {}).get(
        "freetext_advisory_min_avg_length", DEFAULT_FREETEXT_ADVISORY_MIN_AVG_LENGTH
    )
    return float(raw)


def freetext_advisory_min_distinctness(config: dict[str, Any]) -> float:
    """Read `global_settings.freetext_advisory_min_distinctness` with the
    model default. Only consulted by the length+distinctness fallback
    branch; the name-hint branch ignores it.
    """
    raw = (config.get("global_settings") or {}).get(
        "freetext_advisory_min_distinctness", DEFAULT_FREETEXT_ADVISORY_MIN_DISTINCTNESS
    )
    return float(raw)


def score_unmasked_freetext(
    columns: list[FreetextColumnView],
    *,
    min_avg_length: float,
    min_distinctness: float,
) -> list[str]:
    """Score a table's passthrough string columns for likely clinical
    free-text and return one warning string per column that matches.

    A `min_avg_length <= 0` disables the gate entirely -- both the
    name-hint and the length+distinctness branches -- matching the
    sibling gates' "0 disables" contract (D3 / MED-1 precedent).

    Args:
        columns: per-column views for any strategy; only `strategy ==
            "passthrough"` columns are scored, so callers may pass an
            unfiltered column list.
        min_avg_length: length threshold for the fallback branch.
        min_distinctness: `distinct_count / non_null_count` threshold for
            the fallback branch.

    Returns:
        Warning strings, one per matching column, in input order. Never
        raises. Does not mutate `columns` or its elements (all frozen /
        read-only inputs) and has no side effects.
    """
    if min_avg_length <= 0:
        return []

    warnings: list[str] = []
    for col in columns:
        # "Unmasked" is part of the heuristic itself (see module docstring),
        # not merely a caller-side filter: a column already routed to a real
        # masking strategy is out of scope regardless of name or length.
        if col.strategy != "passthrough":
            continue
        if col.dtype_known_non_string:
            continue

        if matches_freetext_name(col.name):
            warnings.append(
                f"freetext_advisory_name_hint: column={col.name!r} "
                f"avg_length={col.avg_length} distinct_count={col.distinct_count} "
                "(column name matches known clinical/claims free-text "
                "vocabulary; unmasked -- consider strategy: text_mask)"
            )
            continue

        if (
            col.avg_length is not None
            and col.distinct_count is not None
            and col.non_null_count is not None
            and col.non_null_count > 0
        ):
            distinctness = col.distinct_count / col.non_null_count
            if col.avg_length >= min_avg_length and distinctness >= min_distinctness:
                warnings.append(
                    f"freetext_advisory_length_distinctness: column={col.name!r} "
                    f"avg_length={col.avg_length} min_avg_length={min_avg_length} "
                    f"distinct_count={col.distinct_count} distinctness={distinctness} "
                    f"min_distinctness={min_distinctness} "
                    "(long, highly-distinct unmasked text; consider strategy: text_mask)"
                )
    return warnings


def warn_on_unmasked_freetext(
    columns: list[FreetextColumnView],
    *,
    min_avg_length: float,
    min_distinctness: float,
) -> None:
    """Run the gate and log each warning. Advisory only: never raises,
    never mutates the plan/config, never changes output bytes."""
    for message in score_unmasked_freetext(
        columns, min_avg_length=min_avg_length, min_distinctness=min_distinctness
    ):
        _log.warning(message)
