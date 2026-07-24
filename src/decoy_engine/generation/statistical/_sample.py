"""Deterministic samplers over a validated StatisticalSpec.

Methodology:

- Numeric: histogram inverse-CDF -- pick a bin by cumulative bin_counts,
  then uniform within the bin. The standard inversion method over an
  empirical histogram (Devroye, Non-Uniform Random Variate Generation,
  1986, ch. II.2 inversion / III histogram methods). Pure-Python
  `bisect` over the cumulative table; no BLAS, no numpy RNG, so output
  is bit-stable across platforms.
- Categorical: weighted draw over the snapshot's top_values. Tail mass
  (`other_count`) either redistributes proportionally onto the top
  values (default) or emits the OTHER_TOKEN placeholder
  (`other_mode: emit`). A `flag`-carrier column (DPS-CODEC phase 6, guide
  section 3.4) decodes the artifact's canonical "true"/"false" tokens to
  Python `bool` instead of the legacy `str()`-forced categorical value --
  one representation in, one representation out.
- Datetime: weighted year choice from year_bins, uniform timestamp
  within the year, clamped to the observed [min, max].
- Freetext: LENGTH-ONLY surrogate text. Target length drawn from the
  fitted length histogram via the same Devroye inverse-CDF as numeric,
  filled with deterministic lorem tokens cut to exactly that length.
  Shape-preserving filler for load-shaped test data; content is
  deliberately meaningless and no source tokens are retained.
- Conditional: per-row draw from the joint contingency row for the
  parent's value, falling back to the marginal when the parent value
  has no joint cells -- sequential conditional sampling as in synthpop
  (Nowok, Raab, Dibben, JSS 2016).

Determinism: one `random.Random` instance reseeded per row with
`col_seed + i` (the established per-row idiom, e.g. _apply_null_probability
in generation/synthesize.py). Output for row i depends only on
(col_seed, i, spec, parent value), so any chunking of rows is
byte-identical to a serial pass -- the WS4 chunk-safety contract.
"""

from __future__ import annotations

import random
from bisect import bisect_right
from datetime import datetime, timedelta
from typing import Any

from decoy_engine.generation.statistical._spec import (
    OTHER_TOKEN,
    StatisticalSpec,
    StatisticalSpecError,
)

# The classic lorem-ipsum vocabulary (fixed literal, no dependency).
# Surrogate freetext draws words from this list; the fitted distribution
# constrains LENGTH only, never content.
_LOREM_WORDS: tuple[str, ...] = (
    "lorem",
    "ipsum",
    "dolor",
    "sit",
    "amet",
    "consectetur",
    "adipiscing",
    "elit",
    "sed",
    "do",
    "eiusmod",
    "tempor",
    "incididunt",
    "ut",
    "labore",
    "et",
    "dolore",
    "magna",
    "aliqua",
    "enim",
    "ad",
    "minim",
    "veniam",
    "quis",
    "nostrud",
    "exercitation",
    "ullamco",
    "laboris",
    "nisi",
    "aliquip",
    "ex",
    "ea",
    "commodo",
    "consequat",
    "duis",
    "aute",
    "irure",
    "in",
    "reprehenderit",
    "voluptate",
    "velit",
    "esse",
    "cillum",
    "fugiat",
    "nulla",
    "pariatur",
    "excepteur",
    "sint",
    "occaecat",
    "cupidatat",
    "non",
    "proident",
    "sunt",
    "culpa",
    "qui",
    "officia",
    "deserunt",
    "mollit",
    "anim",
    "id",
    "est",
    "laborum",
)


def _lorem_text(rng: random.Random, length: int) -> str:
    """Deterministic filler of EXACTLY `length` characters.

    Space-joined draws from `_LOREM_WORDS`, sliced to the target. The
    exact-length cut (mid-word when needed) is what preserves the fitted
    length distribution; surrogate text has no readability contract.
    """
    if length <= 0:
        return ""
    parts: list[str] = []
    joined_len = 0
    while joined_len < length:
        word = _LOREM_WORDS[rng.randrange(len(_LOREM_WORDS))]
        # The separator precedes every word but the first, so the joined
        # length grows by len(word) + 1 only from the second word on.
        joined_len += len(word) + (1 if parts else 0)
        parts.append(word)
    return " ".join(parts)[:length]


def _cumulative(counts: list[float]) -> list[float]:
    total = 0.0
    out: list[float] = []
    for c in counts:
        total += c
        out.append(total)
    return out


def _is_integer_dtype(dtype: str) -> bool:
    return dtype.lstrip("uU").startswith("int") or dtype.startswith("Int")


def _numeric_row(rng: random.Random, edges: list[float], cum: list[float]) -> float:
    u = rng.random() * cum[-1]
    bin_idx = min(bisect_right(cum, u), len(cum) - 1)
    lo, hi = edges[bin_idx], edges[bin_idx + 1]
    return lo + rng.random() * (hi - lo)


def _decode_flag_token(token: object, column: str) -> bool:
    """The one legal decode for a `flag`-carrier top_values entry: the
    artifact always serializes the canonical `"true"`/`"false"` tokens
    (`quality/dp.py::_flag_token`), never Python `str(bool)` `"True"`/
    `"False"` and never `"0"`/`"1"`. Anything else is a corrupted or
    hand-edited artifact that `plan._checks_dp_carrier.
    check_flag_tokens_canonical` should already have refused at compile
    time; this is defense-in-depth for a direct/unvalidated-dict caller."""
    if token == "true":  # noqa: S105 -- an artifact token literal, not a credential
        return True
    if token == "false":  # noqa: S105 -- an artifact token literal, not a credential
        return False
    raise StatisticalSpecError(
        code="statistical_flag_token_invalid",
        message=(
            f"statistical column {column!r}: flag top_values entry {token!r} is "
            "not the canonical 'true'/'false'."
        ),
    )


def _categorical_tables(spec: StatisticalSpec) -> tuple[list[Any], list[float]]:
    top = spec.stats.get("top_values") or []
    if spec.carrier == "flag":
        values: list[Any] = [_decode_flag_token(e["value"], spec.column) for e in top]
    else:
        values = [str(e["value"]) for e in top]
    weights = [float(e["count"]) for e in top]
    other = float(spec.stats.get("other_count") or 0)
    if other > 0 and spec.other_mode == "emit":
        # load_spec rejects other_mode="emit" for a flag carrier (guide
        # section 3.4), so OTHER_TOKEN never joins a bool values list here;
        # this branch is reached by flag columns only defensively.
        values.append(OTHER_TOKEN)
        weights.append(other)
    # redistribute: tail mass is dropped from the table, which scales the
    # top weights up proportionally -- exactly the redistribute semantics.
    return values, weights


def _conditional_tables(
    spec: StatisticalSpec,
) -> dict[str, tuple[list[str], list[float]]]:
    """parent value -> (child values, weights) from the joint cells."""
    if spec.joint is None:  # load_spec guarantees this; defensive for direct callers
        return {}
    parent_idx = 0 if spec.parent_first else 1
    child_idx = 1 - parent_idx
    by_parent: dict[str, tuple[list[str], list[float]]] = {}
    for cell in spec.joint.get("cells") or []:
        key = cell.get("key") or []
        if len(key) != 2:
            continue
        parent, child = str(key[parent_idx]), str(key[child_idx])
        values, weights = by_parent.setdefault(parent, ([], []))
        values.append(child)
        weights.append(float(cell.get("count") or 0))
    return by_parent


def sample_column(
    spec: StatisticalSpec,
    n: int,
    *,
    col_seed: int,
    parent_values: list[Any] | None = None,
) -> list[Any]:
    """Draw `n` deterministic synthetic values for `spec`.

    `parent_values` is required when the spec declares `condition_on`:
    the already-generated values of the conditioning column, one per row.
    """
    if spec.condition_on is not None and parent_values is None:
        raise StatisticalSpecError(
            code="statistical_condition_column_unavailable",
            message=(
                f"statistical column {spec.column!r} conditions on "
                f"{spec.condition_on!r}, which has not been generated yet. "
                f"Declare {spec.condition_on!r} BEFORE {spec.column!r}."
            ),
        )

    rng = random.Random()

    if spec.kind == "numeric":
        edges = [float(e) for e in spec.stats.get("bin_edges") or []]
        counts = [float(c) for c in spec.stats.get("bin_counts") or []]
        if len(edges) < 2 or len(counts) != len(edges) - 1 or sum(counts) <= 0:
            raise StatisticalSpecError(
                code="statistical_stats_degenerate",
                message=(
                    f"statistical column {spec.column!r}: snapshot histogram is "
                    f"degenerate (edges={len(edges)}, counts={len(counts)})."
                ),
            )
        cum = _cumulative(counts)
        as_int = _is_integer_dtype(spec.dtype)
        lo_bound, hi_bound = edges[0], edges[-1]
        out: list[Any] = []
        for i in range(n):
            rng.seed(col_seed + i)
            x = _numeric_row(rng, edges, cum)
            if as_int:
                out.append(int(min(max(round(x), lo_bound), hi_bound)))
            else:
                out.append(x)
        return out

    if spec.kind == "categorical":
        values, weights = _categorical_tables(spec)
        if not values:
            raise StatisticalSpecError(
                code="statistical_stats_degenerate",
                message=f"statistical column {spec.column!r}: snapshot has no top_values.",
            )
        if sum(weights) <= 0:
            # An aggressively DP-noised snapshot can clamp every count to
            # zero; surface the typed code instead of random.choices's bare
            # ValueError.
            raise StatisticalSpecError(
                code="statistical_stats_degenerate",
                message=(
                    f"statistical column {spec.column!r}: every top_values count "
                    "is zero (nothing to weight a draw by). Refit, or use a "
                    "larger epsilon if the snapshot was DP-noised."
                ),
            )
        conditional = _conditional_tables(spec) if spec.condition_on else {}
        out = []
        for i in range(n):
            rng.seed(col_seed + i)
            table = (values, weights)
            if spec.condition_on is not None and parent_values is not None:
                parent = parent_values[i] if i < len(parent_values) else None
                table = conditional.get(str(parent), (values, weights))
            out.append(rng.choices(table[0], weights=table[1], k=1)[0])
        return out

    if spec.kind == "datetime":
        year_bins = spec.stats.get("year_bins") or []
        years = [int(b["year"]) for b in year_bins]
        weights = [float(b["count"]) for b in year_bins]
        if not years:
            raise StatisticalSpecError(
                code="statistical_stats_degenerate",
                message=f"statistical column {spec.column!r}: snapshot has no year_bins.",
            )
        if sum(weights) <= 0:
            raise StatisticalSpecError(
                code="statistical_stats_degenerate",
                message=(
                    f"statistical column {spec.column!r}: every year_bins count "
                    "is zero (nothing to weight a draw by). Refit, or use a "
                    "larger epsilon if the snapshot was DP-noised."
                ),
            )
        lo = datetime.fromisoformat(str(spec.stats["min"]))
        hi = datetime.fromisoformat(str(spec.stats["max"]))
        out = []
        for i in range(n):
            rng.seed(col_seed + i)
            year = rng.choices(years, weights=weights, k=1)[0]
            year_start = datetime(year, 1, 1)
            year_seconds = (datetime(year + 1, 1, 1) - year_start).total_seconds()
            ts = year_start + timedelta(seconds=rng.random() * year_seconds)
            out.append(min(max(ts, lo), hi))
        return out

    if spec.kind == "freetext":
        edges = [float(e) for e in spec.stats.get("length_bin_edges") or []]
        counts = [float(c) for c in spec.stats.get("length_bin_counts") or []]
        if len(edges) < 2 or len(counts) != len(edges) - 1 or sum(counts) <= 0:
            raise StatisticalSpecError(
                code="statistical_stats_degenerate",
                message=(
                    f"statistical column {spec.column!r}: snapshot length histogram "
                    f"is degenerate (edges={len(edges)}, counts={len(counts)}). "
                    "Refit, or use a larger epsilon if the snapshot was DP-noised."
                ),
            )
        cum = _cumulative(counts)
        lo_len, hi_len = int(edges[0]), int(edges[-1])
        out = []
        for i in range(n):
            rng.seed(col_seed + i)
            target = int(min(max(round(_numeric_row(rng, edges, cum)), lo_len), hi_len))
            out.append(_lorem_text(rng, target))
        return out

    raise StatisticalSpecError(  # load_spec already rejects; defensive only
        code="statistical_kind_unsupported",
        message=f"statistical column {spec.column!r}: kind {spec.kind!r} has no sampler.",
    )
