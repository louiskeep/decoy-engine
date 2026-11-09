"""Exception hierarchy for the pool package.

`GenerationError` covers value-generation failures at sample time
(canonicalization, dtype mismatches, uniqueness impossibility).
`PoolCapacityError` covers pool-construction + cache-budget failures.
Both are runtime errors; peers, not subclasses of `PlanCompileError`.
"""

from __future__ import annotations

from typing import NoReturn

from decoy_engine import reasons
from decoy_engine.errors import DecoyError


class GenerationError(DecoyError):
    """Runtime failure inside the pool sampler or canonicalizer.

    Codes used in S5:

    - `deterministic_requires_source_and_namespace`: PoolSampler.sample called
      with `deterministic=True` but `source` or `namespace` missing.
    - `uniqueness_impossible`: UNIQUE-mode sample request larger than pool.
    - `dtype_unsafe`: output dtype is not safely convertible to source dtype.
    - `float_canonicalization_unsupported`: float source value at canonicalize
      time (the encoding would lock into the determinism envelope per R3).
    - `timezone_naive_datetime`: datetime source value without an explicit tz.
    - `null_canonicalization_unreachable`: null reached `_canonicalize_source`
      (nulls should be filtered at the mask layer before canonicalize).
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class PoolCapacityError(DecoyError):
    """Pool-construction or cache-budget failure.

    Codes used in S5:

    - `provider_not_poolable`: PoolBuilder.build called for a provider whose
      CapabilityMatrix declares `poolable: False`.
    - `pool_exceeds_cache_budget`: PoolCache.put called with a pool larger
      than the entire cache budget (eviction cannot make room).
    - `pool_too_small_for_source`: pool_capacity_pre_flight detected a UNIQUE
      mode column whose source distinct count exceeds the configured pool size
      AND on_pool_exhaustion=fail.
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


# Fixed remedy sentence per refusal kind; never built from a value.
_DEFAULT_REMEDY = "Lower row_count, drop unique, or use a source with more values."
_REMEDY = {
    "pool sample": "Lower the output row count, or use a pool with more distinct values.",
    "bundle sample": "Lower the output row count, or use a bundle pool with more distinct tuples.",
}


def _count(name: str, value: int) -> int:
    # A bool or a str is refused without echoing it: the builder must never format a value
    # that came from generated or parent data.
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    return value


def raise_uniqueness_impossible(
    *,
    needed: int,
    available: int,
    table: str | None = None,
    column: str | None = None,
    kind: str | None = None,
    row: int | None = None,
) -> NoReturn:
    """Raise the one `uniqueness_impossible` refusal, naming only places and counts.

    The single place that builds `GenerationError(code=UNIQUENESS_IMPOSSIBLE)`, for the
    masking sampler and for generate-side unique draws, so no caller can put a generated or
    parent value into the message. `table`, `column` and `kind` are config names; `needed`,
    `available` and `row` are counts. `row` is the zero-based row index reached when the
    domain ran out, shown one-based.
    """
    needed = _count("needed", needed)
    available = _count("available", available)
    where = f"{table}.{column}" if table is not None and column is not None else "unique draw"
    what = f"unique {kind}" if kind is not None else "unique values"
    message = f"{where}: {what} needs {needed:,} distinct value(s) but only {available:,} exist"
    if row is not None:
        message += f"; the domain ran out at row {_count('row', row) + 1:,}"
    message += f". {_REMEDY.get(kind or '', _DEFAULT_REMEDY)}"
    raise GenerationError(code=reasons.runtime.UNIQUENESS_IMPOSSIBLE, message=message)
