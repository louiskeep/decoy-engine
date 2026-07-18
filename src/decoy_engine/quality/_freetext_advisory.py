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
assembled). MEASURED EVIDENCE WINS: whatever length/distinctness is
available constrains the decision, and the name hint never overrides it.

0. TWO independent HARD suppression gates -- either measured signal, on its
   own, rules the column OUT of free text, regardless of the other signal or
   the name:
   - Known-short length (`avg_length < min_avg_length`, whenever avg_length is
     measurable): code-shaped. A high-cardinality ICD-10 *code* column (HC-5)
     has high distinctness but SHORT values (avg ~6); only length separates it
     from long prose (avg ~200). This is the HC-5 false-positive protection.
   - Known-low distinctness (`distinctness < min_distinctness`, whenever it is
     measurable): categorical / templated, not prose. Applied independent of
     length, which closes the mirror partial-stats hole where length is
     unknown (a stale profile predating `avg_length`) but distinct_count is
     present.
1. Strong warn (both signals measured and neither suppressed): a long,
   highly-distinct unmasked column warns via the length+distinctness message.
2. Name-hint (tiebreaker, when at least one signal is unmeasurable and neither
   contradicts -- a stale profile, `--no-profile`, or an unconfirmable dtype):
   `matches_freetext_name` alone warns. High distinctness ALONE with unknown
   length does NOT warn without a name (a short high-cardinality code column is
   indistinguishable here), and a known-short or measurably-low-distinctness
   column never reaches this branch (gate 0 suppressed it).

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

        # TWO INDEPENDENT HARD SUPPRESSION GATES from measured evidence.
        # Either measured signal, ON ITS OWN, can rule a column OUT of free
        # text -- and does so regardless of the other signal or the name,
        # because measured evidence wins over a name hint.
        #
        # (1) Known-short length -> code-shaped. A high-cardinality ICD-10
        #     *code* column (HC-5) has short values (avg ~6); only length
        #     separates it from long prose. Applied whenever avg_length is
        #     measurable, independent of distinct_count.
        if col.avg_length is not None and col.avg_length < min_avg_length:
            continue
        # (2) Known-low distinctness -> categorical / templated, not prose.
        #     Applied whenever distinctness is measurable, independent of
        #     avg_length -- this closes the mirror partial-stats hole where
        #     length is unknown (e.g. a stale profile predating avg_length)
        #     but a low distinct_count is present: the name must not rescue a
        #     measurably-categorical column.
        distinctness: float | None = None
        if (
            col.distinct_count is not None
            and col.non_null_count is not None
            and col.non_null_count > 0
        ):
            distinctness = col.distinct_count / col.non_null_count
            if distinctness < min_distinctness:
                continue

        # Past both suppression gates -> nothing measured contradicts free text.
        # Strong case: BOTH signals measured and supportive (long AND distinct).
        if col.avg_length is not None and distinctness is not None:
            warnings.append(
                f"freetext_advisory_length_distinctness: column={col.name!r} "
                f"avg_length={col.avg_length} min_avg_length={min_avg_length} "
                f"distinct_count={col.distinct_count} distinctness={distinctness} "
                f"min_distinctness={min_distinctness} "
                "(long, highly-distinct unmasked text; consider strategy: text_mask)"
            )
            continue

        # At least one signal is unmeasurable and neither contradicts. The name
        # hint is the tiebreaker -- best-effort for a stale/absent profile, and
        # the only path on which a name match, by itself, warns. High
        # distinctness ALONE (length unknown) does not warn without a name,
        # since a short high-cardinality code column looks identical here.
        if matches_freetext_name(col.name):
            warnings.append(
                f"freetext_advisory_name_hint: column={col.name!r} "
                f"avg_length={col.avg_length} distinct_count={col.distinct_count} "
                "(column name matches known clinical/claims free-text "
                "vocabulary; not measurably ruled out; unmasked -- consider "
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
