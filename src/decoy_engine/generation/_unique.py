"""Unique generate draws: `unique: true` columns and `unique_together` junction pairs.

The size rule is `unique_capacity_ok` (`generation/pool/_capacity.py`) and every refusal goes
through `raise_uniqueness_impossible` (`generation/pool/_errors.py`), the two pieces the masking
pool's UNIQUE mode already uses. This module only draws. Draws copy two established methods:

- Faker's `fake.unique` proxy: keep a seen set, retry a repeat, give up after 1,000 attempts
  (`faker/proxy.py`, `_UNIQUE_ATTEMPTS`). The proxy is not used itself: its retries advance one
  instance's state, so row `i` would depend on how many retries earlier rows took. Here attempt
  `k` of row `i` reseeds from its own HMAC (`faker_unique` family), so a row is a function of
  its own history only.
- Sampling without replacement uses CPython `random.Random.sample` (partial Fisher-Yates or a
  selected set), while the masking sampler's UNIQUE mode uses NumPy permutation. `categorical`,
  `reference` and pair draws have no engine-level per-value retry loop.

State is function-local: the seen set and counters live and die inside one call.
Pattern: Faker unique proxy and sampling without replacement (Faker, MIT; CPython, PSF).
"""

from __future__ import annotations

import random
import sys
from typing import Any

from decoy_engine.config import UNIQUE_FAKER_ATTEMPTS, distinct_categories, value_key
from decoy_engine.generation._synthesize_derived import parent_keys
from decoy_engine.generation.pool import raise_uniqueness_impossible, unique_capacity_ok
from decoy_engine.generators.derivation import TABLE_KEY, GenDeriveContext, with_table

__all__ = ["pair_hooks", "unique_categorical", "unique_faker", "unique_pair", "unique_reference"]


def _where(col: dict[str, Any]) -> tuple[str | None, str | None]:
    table, name = col.get(TABLE_KEY), col.get("name")
    return (table if isinstance(table, str) else None, name if isinstance(name, str) else None)


def _py_rng(col: dict[str, Any], seed: int, derive_key: Any) -> random.Random:
    base = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    ).base_int("py")
    return random.Random(base)


def _key_or_none(value: Any) -> tuple[Any] | None:
    """`(value_key(value),)`, or None when the value cannot be keyed (so cannot be tracked)."""
    try:
        return (value_key(value),)
    except TypeError:
        return None


def unique_faker(
    inst: Any,
    provider: Any,
    kwargs: dict[str, Any],
    gen_ctx: GenDeriveContext,
    n: int,
    *,
    table: str | None,
    column: str | None,
) -> list[Any]:
    """`n` provider values, no two equal. Attempt 0 of row `i` is the plain per-row draw.

    A `None` is emitted without entering the seen set (SQL nulls do not collide). A row whose
    1,000 attempts all repeat, or a value that cannot be keyed, refuses with the row index and
    the distinct count so far; neither the value nor the provider output is quoted.
    """
    seen: set[Any] = set()
    out: list[Any] = []
    for i in range(n):
        inst.seed_instance(gen_ctx.row_int("faker", i))
        value = provider(**kwargs)
        attempts = 1
        while True:
            keyed = None if value is None else _key_or_none(value)
            if value is None or (keyed is not None and keyed[0] not in seen):
                if keyed is not None:
                    seen.add(keyed[0])
                break
            # Raised outside any `except` block, so a key error is not chained.
            if keyed is None or attempts >= UNIQUE_FAKER_ATTEMPTS:
                raise_uniqueness_impossible(
                    needed=n,
                    available=len(seen),
                    table=table,
                    column=column,
                    kind="faker value" if keyed is None else "faker",
                    row=i,
                )
            inst.seed_instance(
                gen_ctx.row_int("faker_unique", i * UNIQUE_FAKER_ATTEMPTS + attempts - 1)
            )
            value = provider(**kwargs)
            attempts += 1
        out.append(value)
    return out


def unique_categorical(col: dict[str, Any], n: int, seed: int, derive_key: Any = None) -> list[Any]:
    """`n` distinct categories in a seeded order; a `None` category fills the overflow rows."""
    distinct, has_null = distinct_categories(col.get("categories", ["Category A", "Category B"]))
    if not unique_capacity_ok(len(distinct), n, null_reusable=has_null):
        table, name = _where(col)
        raise_uniqueness_impossible(
            needed=n, available=len(distinct), table=table, column=name, kind="categorical"
        )
    take = min(n, len(distinct))
    values: list[Any] = _py_rng(col, seed, derive_key).sample(distinct, take)
    values.extend([None] * (n - take))
    return values


def unique_reference(
    col: dict[str, Any], ref_vals: list[Any], n: int, seed: int, derive_key: Any = None
) -> list[Any]:
    """`n` distinct parent keys. Checked before the empty-parent return, so an all-null parent
    refuses instead of producing all `None`. `sequential` keeps its order and refuses, rather
    than wraps, when the parent is short."""
    if not unique_capacity_ok(len(ref_vals), n):
        table, name = _where(col)
        raise_uniqueness_impossible(
            needed=n, available=len(ref_vals), table=table, column=name, kind="reference"
        )
    rng = _py_rng(col, seed, derive_key)
    if col.get("distribution", "random") == "sequential":
        return ref_vals[:n]
    return rng.sample(ref_vals, n)


def pair_hooks(table: dict[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Per `unique_together` entry, keyed by the member declared first: (first, second) columns.

    The table loop draws the pair at the first member's turn and holds the second member's values
    until the loop reaches it, so declared column order and "later columns only" reads stay as
    they are without the pair.
    """
    cols: list[dict[str, Any]] = table["generate_columns"]
    order = [c["name"] for c in cols]
    by_name = {c["name"]: c for c in cols}
    hooks: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for names in table.get("unique_together") or []:
        first, second = sorted(names, key=order.index)
        hooks[first] = (by_name[first], by_name[second])
    return hooks


def unique_pair(
    col_a: dict[str, Any],
    col_b: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any,
    pools: dict[str, Any],
    *,
    table_name: str,
) -> tuple[list[Any], list[Any]]:
    """`n` distinct (a, b) key pairs: indices sampled without replacement from the product."""
    keys_a = parent_keys(pools, col_a["reference_table"], col_a["reference_column"])
    keys_b = parent_keys(pools, col_b["reference_table"], col_b["reference_column"])
    size = len(keys_a) * len(keys_b)
    # `range` cannot be longer than sys.maxsize; a product past it is refused, never truncated.
    if size > sys.maxsize or not unique_capacity_ok(size, n):
        raise_uniqueness_impossible(
            needed=n,
            available=min(size, sys.maxsize),
            table=table_name,
            column=f"({col_a['name']}, {col_b['name']})",
            kind="pair",
        )
    pair_col = with_table(col_a, table_name)
    base = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=pair_col, fallback_seed=seed
    ).base_int("unique_pair")
    picks = random.Random(base).sample(range(size), n)
    width = len(keys_b)
    return [keys_a[i // width] for i in picks], [keys_b[i % width] for i in picks]
