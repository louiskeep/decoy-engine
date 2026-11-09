"""Fan-out of a key column: the degree histogram, its decoder and the allocator.

The fan-out of a key column is how many child rows each parent key has. Its
degree histogram maps `degree -> number of parents with that many children`,
over the parents that have at least one child. Fit time records it as counts
only (`fanout_stats`, applied by `add_fanout`); generation decodes it
(`fanout_spec_from_snapshot`), scales it to the generated parent and child
row counts (`allocate_degrees`) and attaches children to parents
(`fitted_reference_values`). This module is the one writer and the one reader
of the block, so its shape is defined here and nowhere else.

Pattern: degree-sequence sampling with systematic resampling and largest-remainder apportionment (Newman, Networks, 2nd ed., 2018, sec. 12.1; Kitagawa, JCGS 1996; Balinski and Young, Fair Representation, 2001; published methods, no licence). See: https://docs.sdv.dev/sdmetrics (CardinalityShapeSimilarity).

Method notes:
  - Degree-sequence sampling and the configuration model: fix each parent's
    degree, list each parent key `degree` times (the stubs), shuffle the
    list. Every child gets a real parent and every parent gets its degree.
  - Systematic resampling with the offset fixed at 0.5: K values are drawn
    from the empirical degree distribution at the inverse CDF of
    `(2i + 1) / 2K`. No random draw, and with K equal to the source parent
    count it returns the source multiset exactly.
  - Largest-remainder (Hamilton) apportionment scales those degrees to the
    child row count. All arithmetic is integer, so the result is the same on
    every platform.
  - SDV HMA1 models a per-parent child count and generates children for it.
    This is the unconditional form: the count is drawn from the marginal, not
    from parent attributes.
"""

from __future__ import annotations

import heapq
import random
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from decoy_engine import reasons
from decoy_engine.generation.statistical._spec import StatisticalSpecError
from decoy_engine.quality.snapshot import DistributionSnapshotError

_BLOCK_KEY = "fanout"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class FanoutSpec:
    """A decoded, validated fan-out block plus the digest of its snapshot."""

    rows: int
    keys: int
    degrees: tuple[tuple[int, int], ...]
    digest: str

    def __post_init__(self) -> None:
        # Seed derivation hashes this string, so a malformed one would silently
        # change every generated value instead of failing.
        if not isinstance(self.digest, str) or not _DIGEST_RE.fullmatch(self.digest):
            raise ValueError("digest must be 64 lowercase hex characters (a bare sha256)")

    @property
    def seed_digest(self) -> str:
        """The `sha256:<hex>` form seed derivation expects (`digest` is bare hex)."""
        return f"sha256:{self.digest}"


# ── writer ───────────────────────────────────────────────────────────────────


def fanout_stats(series: pd.Series) -> dict[str, Any]:
    """The degree histogram of `series` as counts only, never a key value.

    Keys compare as strings, the way the snapshot already treats a column's
    values.
    """
    counts = series.dropna().astype(str).value_counts()
    histogram = counts.value_counts().sort_index()
    return {
        "rows": int(counts.sum()),
        "keys": len(counts),
        "degrees": [[int(d), int(c)] for d, c in histogram.items()],
    }


def add_fanout(
    columns: dict[str, dict[str, Any]],
    df: pd.DataFrame,
    fanout_columns: Collection[str],
) -> None:
    """Attach a `fanout` block to each named column entry of a snapshot.

    An unknown name raises rather than being skipped: a skipped name would
    only surface later as a generation refusal.
    """
    if isinstance(fanout_columns, (str, bytes)):
        raise DistributionSnapshotError(
            code=reasons.quality.FANOUT_COLUMN_UNKNOWN,
            message=(
                "fanout_columns must be a collection of column names "
                "(e.g. a list/set/tuple), not a bare string."
            ),
        )
    known = {str(c) for c in df.columns}
    unknown = sorted({str(n) for n in fanout_columns} - known)
    if unknown:
        raise DistributionSnapshotError(
            code=reasons.quality.FANOUT_COLUMN_UNKNOWN,
            message=(
                f"fanout_columns names {len(unknown)} column(s) not in the frame; "
                "check the spelling against the source columns."
            ),
        )
    labels = {str(c): c for c in df.columns}
    for name in sorted({str(n) for n in fanout_columns}):
        columns[name][_BLOCK_KEY] = fanout_stats(df[labels[name]])


# ── reader ───────────────────────────────────────────────────────────────────


def _invalid(column: str, what: str) -> StatisticalSpecError:
    return StatisticalSpecError(
        code=reasons.plan.REFERENCE_FANOUT_INVALID,
        message=(
            f"fan-out block of column {column!r} is invalid: {what}. "
            "Re-run `decoy fit` with --fanout to rewrite it."
        ),
    )


def _is_count(value: object, *, minimum: int) -> bool:
    # `type(...) is int` on purpose: a bool, float or numeric string is refused, not coerced.
    return type(value) is int and value >= minimum


def fanout_spec_from_snapshot(
    snapshot: Mapping[str, Any], column: str, *, digest: str
) -> FanoutSpec:
    """Decode and validate the fan-out block of `column` from a pinned snapshot.

    Messages name fields and invariants only, never a stored value.
    """
    columns = snapshot.get("columns") if isinstance(snapshot, Mapping) else None
    entry = columns.get(column) if isinstance(columns, Mapping) else None
    block = entry.get(_BLOCK_KEY) if isinstance(entry, Mapping) else None
    if block is None:
        raise StatisticalSpecError(
            code=reasons.plan.REFERENCE_FANOUT_MISSING,
            message=(
                f"snapshot has no fan-out for column {column!r}. Re-run `decoy fit` "
                "with --fanout, or use distribution: random. DP snapshots do not "
                "carry fan-out."
            ),
        )
    if type(block) is not dict:
        raise _invalid(column, "the block is not an object")
    rows, keys, raw = block.get("rows"), block.get("keys"), block.get("degrees")
    if not _is_count(rows, minimum=0):
        raise _invalid(column, "'rows' is not a non-negative integer")
    if not _is_count(keys, minimum=0):
        raise _invalid(column, "'keys' is not a non-negative integer")
    if type(raw) is not list:
        raise _invalid(column, "'degrees' is not a list")
    pairs: list[tuple[int, int]] = []
    for item in raw:
        if type(item) is not list or len(item) != 2:
            raise _invalid(column, "a 'degrees' entry is not a [degree, parents] pair")
        degree, parents = item
        if not _is_count(degree, minimum=1) or not _is_count(parents, minimum=1):
            raise _invalid(column, "a 'degrees' entry is not a pair of positive integers")
        if pairs and degree <= pairs[-1][0]:
            raise _invalid(column, "'degrees' is not strictly ascending by degree")
        pairs.append((degree, parents))
    if sum(p for _, p in pairs) != keys:
        raise _invalid(column, "the parent counts do not sum to 'keys'")
    if sum(d * p for d, p in pairs) != rows:
        raise _invalid(column, "degree times parents does not sum to 'rows'")
    if keys == 0:
        raise StatisticalSpecError(
            code=reasons.plan.REFERENCE_FANOUT_INVALID,
            message=(
                f"fan-out block of column {column!r} is invalid: the source column "
                "has no non-null keys, so there is no fan-out to reproduce. "
                "Use distribution: random for this column."
            ),
        )
    return FanoutSpec(rows=rows, keys=keys, degrees=tuple(pairs), digest=digest)


# ── allocation ───────────────────────────────────────────────────────────────


def _half_even_div(a: int, b: int) -> int:
    q, r = divmod(a, b)
    if 2 * r > b or (2 * r == b and q % 2 == 1):
        q += 1
    return q


def _quantile_degrees(spec: FanoutSpec, k: int) -> list[int]:
    """Inverse CDF of the degree histogram at the fixed offsets `(2i + 1) / 2K`."""
    out: list[int] = []
    cum = 0
    j = -1
    for i in range(k):
        need = (2 * i + 1) * spec.keys
        while cum * 2 * k < need:
            j += 1
            cum += spec.degrees[j][1]
        out.append(spec.degrees[j][0])
    return out


def _largest_remainder(base: list[int], n: int) -> list[int]:
    """Scale `base` to sum exactly `n`, keeping every entry at least 1."""
    k = len(base)
    total = sum(base)
    shares = [b * n for b in base]
    out = [max(1, s // total) for s in shares]
    delta = n - sum(out)
    if delta > 0:
        order = sorted(range(k), key=lambda i: (shares[i] % total, base[i], i), reverse=True)
        for i in order[:delta]:
            out[i] += 1
    elif delta < 0:
        # Floors of 1 pushed the sum over `n`: shave the largest entries still above 1.
        heap = [(-v, -i) for i, v in enumerate(out) if v > 1]
        heapq.heapify(heap)
        for _ in range(-delta):
            neg_v, neg_i = heapq.heappop(heap)
            out[-neg_i] -= 1
            if out[-neg_i] > 1:
                heapq.heappush(heap, (neg_v + 1, neg_i))
    return out


def _cap(out: list[int], base: list[int], dmax: int) -> list[int]:
    """Lower every entry to `dmax`, moving the removed units to entries below it."""
    removed = 0
    for i, v in enumerate(out):
        if v > dmax:
            removed += v - dmax
            out[i] = dmax
    heap = [(-base[i], -i) for i, v in enumerate(out) if v < dmax]
    heapq.heapify(heap)
    for _ in range(removed):
        neg_b, neg_i = heapq.heappop(heap)
        out[-neg_i] += 1
        if out[-neg_i] < dmax:
            heapq.heappush(heap, (neg_b, neg_i))
    return out


def allocate_degrees(spec: FanoutSpec, parents: int, n: int) -> list[int]:
    """Ascending child counts for the active parents.

    `parents` is the size of the generated parent pool and `n` the child row
    count. The list has `K` entries, every one at least 1, summing to `n`
    exactly, where `K` is the active parent count scaled from the source and
    clamped to `[1, min(parents, n)]`. No entry exceeds the source maximum
    degree whenever `n <= K * dmax`. Returns `[]` when `n == 0`; `n > 0` with
    no parents has no valid answer and raises `ValueError`.
    """
    if n == 0:
        return []
    if parents == 0:
        raise ValueError("cannot place child rows without a parent pool")
    k = max(1, min(_half_even_div(n * spec.keys, spec.rows), parents, n))
    base = _quantile_degrees(spec, k)
    out = _largest_remainder(base, n)
    dmax = spec.degrees[-1][0]
    if n <= k * dmax:
        out = _cap(out, base, dmax)
    return sorted(out)


# ── assignment ───────────────────────────────────────────────────────────────


def fitted_reference_values(
    pool: Sequence[Any], spec: FanoutSpec, n: int, rng: random.Random
) -> list[Any]:
    """`n` child keys drawn from `pool`, each parent holding its allocated degree.

    Returns exactly `n` entries from `pool`, or raises when `n > 0` and the
    pool is empty (no valid foreign-key value exists). The two shuffles are
    the only random draws: degrees to parents, then the child row order.
    """
    if n == 0:
        return []
    if not pool:
        raise StatisticalSpecError(
            code=reasons.plan.REFERENCE_PARENT_POOL_EMPTY,
            message=(
                "a fitted reference column has child rows but its parent column "
                "has no non-null keys. Generate the parent table first or give "
                "it rows."
            ),
        )
    degrees = allocate_degrees(spec, len(pool), n)
    slots = degrees + [0] * (len(pool) - len(degrees))
    rng.shuffle(slots)
    stubs = [pool[j] for j, d in enumerate(slots) for _ in range(d)]
    rng.shuffle(stubs)
    return stubs
