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

The heuristic (all branches gated on the column being a passthrough
string column with at least one non-null value -- see
`plan/_checks_freetext_advisory.py` for how the per-column view is
assembled). MEASURED EVIDENCE WINS: when length and distinctness are
available they are the sole criterion, and the name hint never overrides
them.

1. Length + distinctness (primary, whenever `avg_length` /
   `distinct_count` / a positive non-null count are known): warn iff
   `avg_length >= min_avg_length` AND `distinctness >= min_distinctness`.
   LENGTH is the load-bearing discriminator -- a high-cardinality ICD-10
   *code* column (HC-5) also has high distinctness but SHORT values
   (avg ~6 chars); only average length separates long prose (avg ~200)
   from short high-cardinality codes. Because evidence decides here, a
   column merely NAMED like free text but holding short codes does NOT
   warn -- that is exactly the HC-5 code-column false-positive the length
   gate prevents.
2. Name-hint (fallback, ONLY when length/distinctness are genuinely
   unmeasurable -- a stale profile predating this feature, `--no-profile`,
   or an unconfirmable dtype): `matches_freetext_name` alone warns, a
   best-effort signal for the case where we cannot measure the column.

An all-null / empty column carries no PHI and never warns, even when its
name matches. A column already routed to a real masking strategy (anything
other than `passthrough`) is never considered -- the operator already
handled it; `score_unmasked_freetext` enforces both directly, so they hold
even if a caller passes an unfiltered column list.
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
        # An all-null / empty column carries no PHI to leak, so it never
        # warns -- even when its name matches the free-text vocabulary and
        # even though its length is unmeasurable (avg_length is None). This
        # gate precedes the name-only fallback so a hinted all-null column
        # cannot slip through it.
        if col.non_null_count is not None and col.non_null_count <= 0:
            continue

        # Measured evidence decides; the name hint does NOT override it. A
        # column NAMED `clinical_notes`/`diagnosis_text` that actually holds
        # short codes (avg length well under the threshold) is not free text --
        # letting the name warn anyway would re-introduce the exact
        # false-positive on HC-5 high-cardinality code columns that the length
        # gate exists to prevent. When length/distinctness are measurable, they
        # are the sole criterion and we never fall through to the name branch.
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
            continue

        # Stats genuinely unavailable (missing/stale profile, or a non-string
        # dtype we could not confirm): fall back to the name hint alone. This
        # is best-effort -- we cannot measure length here -- and is the only
        # path on which a name match, by itself, produces a warning.
        if matches_freetext_name(col.name):
            warnings.append(
                f"freetext_advisory_name_hint: column={col.name!r} "
                f"avg_length={col.avg_length} distinct_count={col.distinct_count} "
                "(column name matches known clinical/claims free-text "
                "vocabulary; length unmeasured; unmasked -- consider "
                "strategy: text_mask)"
            )
    return warnings


def warn_on_unmasked_freetext(
    columns: list[FreetextColumnView],
    *,
    min_avg_length: float,
    min_distinctness: float,
) -> list[str]:
    """Score the columns, log each advisory once, and return the messages.

    Returning the scored messages (rather than `None` like the sibling gates'
    wrappers) lets the single compile call-site surface each advisory through
    BOTH channels without re-scoring: the log line here is the operator-visible
    nudge at compile time, and the returned list is folded into
    `PlanCompileResult.warnings` for programmatic/serialized consumers. Advisory
    only: never raises, never mutates the plan/config, never changes output
    bytes.
    """
    messages = score_unmasked_freetext(
        columns, min_avg_length=min_avg_length, min_distinctness=min_distinctness
    )
    for message in messages:
        _log.warning(message)
    return messages
