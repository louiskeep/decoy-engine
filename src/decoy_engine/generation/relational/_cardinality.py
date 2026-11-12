"""Per-parent cardinality for generated reference columns: the pool check and the repair.

Two questions about a child column that references a parent key list are answered here and
nowhere else:

- ``check_reference_pool``: may this draw start? It holds the empty-parent rule (the
  relationship's orphan policy decides), the bounds feasibility rule and the weights rule. The
  whole-table draw and the streamed draw both call it before the first value, so no route can
  enforce a different rule. ``distribution: fitted`` is the one named exemption: it keeps its
  own empty-pool refusal and refuses bounds through ``check_fitted_bounds``.
- ``apply_cardinality_bounds``: move a drawn value list inside ``min_per_parent`` and
  ``max_per_parent``. The V1 ``ColumnGenerator`` and the v2 reference draw share it.

Pattern: pigeonhole feasibility and room-aware fill. ``P`` parents that each take at most
``max`` children hold ``N`` rows exactly when ``N <= max * P``, and each can get ``min`` exactly
when ``min * P <= N`` (the pigeonhole principle; the degree constraints of a bipartite
b-matching, Schrijver, *Combinatorial Optimization*, ch. 21). The comparison is the engine's
one domain-versus-rows rule, ``unique_capacity_ok``. The fill draws uniformly from the parents
that still have room and swap-removes a parent when its last slot is taken, the constant-time
removal of sampling without replacement (Knuth, *TAOCP* vol. 2, sec. 3.4.2), so it allocates
O(P + N) and cannot pass ``max``.

Every map and counter is keyed by ``value_key``, so a list or dict parent key is tracked by
equality; the values written back are the original parent objects. Messages are the registered
summary plus the table and column names: no key, count, bound or weight reaches one.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from decoy_engine import _capacity, reasons, relationships
from decoy_engine.config import generate_columns_of, value_key
from decoy_engine.generation.pool import GenerationError, QualityWarning, emit_generation_warning
from decoy_engine.generators.derivation import TABLE_KEY

__all__ = [
    "apply_cardinality_bounds",
    "check_fitted_bounds",
    "check_reference_pool",
    "reference_policies",
]

_LOG = logging.getLogger(__name__)


def _where(table: Any, column: Any) -> str:
    if isinstance(table, str) and isinstance(column, str):
        return f" Table {table!r}, column {column!r}."
    return ""


def _text(code: reasons.ReasonCode, table: Any, column: Any) -> str:
    """The whole text of a reference-draw refusal or warning: fixed summary plus names."""
    return f"{code.spec.summary}{_where(table, column)}"


def _infeasible(table: Any, column: Any) -> GenerationError:
    """The one builder of the run-time infeasible-bounds refusal."""
    return GenerationError(
        code=reasons.runtime.GENERATE_PER_PARENT_INFEASIBLE,
        message=_text(reasons.runtime.GENERATE_PER_PARENT_INFEASIBLE, table, column),
    )


def _bound(col: dict[str, Any], key: str) -> int:
    """A per-parent bound; zero, absent and null all mean unset."""
    return max(int(col.get(key) or 0), 0)


def _feasible(lo: int, hi: int, parents: int, rows: int) -> bool:
    """`lo * parents <= rows` and, with a maximum, `rows <= hi * parents`. Equality fits."""
    if lo > 0 and not _capacity.unique_capacity_ok(rows, lo * parents):
        return False
    return hi <= 0 or _capacity.unique_capacity_ok(hi * parents, rows)


def reference_policies(config: dict[str, Any]) -> dict[tuple[str, str], str]:
    """`(table, column) -> orphan policy` for each generate reference a relationship covers.

    A reference column is covered when a relationship child entry names exactly its
    `(reference_table, [reference_column], table, [column])`. Read once per run through the
    relationship package's own normalizer; a column with no entry has no policy.
    """
    lookup = relationships.normalize_orphan_policy_lookup(config)
    found: dict[tuple[str, str], str] = {}
    if not lookup:
        return found
    for table in config.get("tables") or []:
        if not isinstance(table, dict):
            continue
        for col in generate_columns_of(table):
            if not isinstance(col, dict) or col.get("type") != "reference":
                continue
            name, column = table.get("name"), col.get("name")
            key = (col.get("reference_table"), (col.get("reference_column"),), name, (column,))
            policy = lookup.get(key)  # type: ignore[arg-type]
            if policy is not None and isinstance(name, str) and isinstance(column, str):
                found[(name, column)] = policy.value
    return found


def reference_draw_extras(
    col: dict[str, Any],
    table: str,
    fanouts: dict[tuple[str, str], Any] | None,
    policy: str | None,
    on_warning: Callable[[QualityWarning], None] | None,
) -> dict[str, Any]:
    """The keyword arguments a reference draw takes beyond its plain five-argument call.

    A `fitted` column carries its pinned fan-out (`None` when the plan has none, which the
    draw refuses); the orphan policy and warning sink ride along only when either is set, so
    a column with neither is drawn through the same call shape as before they existed.
    """
    extra: dict[str, Any] = {}
    if col.get("distribution") == "fitted":
        extra["fanout"] = (fanouts or {}).get((table, str(col.get("name"))))
    if policy is not None or on_warning is not None:
        extra = {**extra, "policy": policy, "on_warning": on_warning}
    return extra


def check_reference_pool(
    col: dict[str, Any],
    keys: Sequence[Any],
    n: int,
    policy: str | None,
    table: str | None,
    on_warning: Callable[[QualityWarning], None] | None,
) -> None:
    """Refuse a non-fitted reference draw that cannot give the shape its config asks for.

    `keys` is the real distinct parent key list the draw reads, not the parent's `row_count`.
    With no keys and rows to fill, `policy` decides: `fail` refuses, `warn` emits one coded
    warning, and `preserve`, `remap` or no relationship stay silent; the caller then writes
    nulls. With keys, positive bounds must be feasible and `weighted` weights must have one
    entry per key. Called once per column, before the first value.
    """
    column = col.get("name")
    if not keys:
        if n <= 0:
            return
        if policy == relationships.OrphanPolicy.FAIL.value:
            raise GenerationError(
                code=reasons.runtime.REFERENCE_PARENT_POOL_EMPTY,
                message=_text(reasons.runtime.REFERENCE_PARENT_POOL_EMPTY, table, column),
            )
        if policy == relationships.OrphanPolicy.WARN.value:
            emit_generation_warning(
                QualityWarning(
                    code=reasons.warning.REFERENCE_PARENT_POOL_EMPTY,
                    provider="reference",
                    column=column,
                    detail={"table": table, "column": column},
                ),
                _LOG,
                _text(reasons.warning.REFERENCE_PARENT_POOL_EMPTY, table, column),
                on_warning,
            )
        return
    lo, hi = _bound(col, "min_per_parent"), _bound(col, "max_per_parent")
    if not _feasible(lo, hi, len(keys), n):
        raise _infeasible(table, column)
    weights = col.get("weights")
    if (
        col.get("distribution") == "weighted"
        and isinstance(weights, (list, tuple))
        and len(weights) != len(keys)
    ):
        raise GenerationError(
            code=reasons.runtime.REFERENCE_WEIGHTS_LENGTH_MISMATCH,
            message=_text(reasons.runtime.REFERENCE_WEIGHTS_LENGTH_MISMATCH, table, column),
        )


def check_fitted_bounds(col: dict[str, Any], keys: Sequence[Any]) -> None:
    """Refuse a positive bound on a `fitted` reference that has parents to draw from.

    Fitted degrees come from the pinned fan-out, so a bound would be dropped without a word.
    An empty pool is left to the fitted allocator, whose refusal has precedence.
    """
    if keys and (_bound(col, "min_per_parent") or _bound(col, "max_per_parent")):
        raise GenerationError(
            code=reasons.runtime.GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT,
            message=_text(
                reasons.runtime.GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT,
                col.get(TABLE_KEY),
                col.get("name"),
            ),
        )


def apply_cardinality_bounds(
    values: list[Any],
    ref_values: Sequence[Any],
    min_per_parent: int,
    max_per_parent: int,
    rng: random.Random,
    *,
    table: str | None = None,
    column: str | None = None,
) -> list[Any]:
    """Repair `values` in place so every parent holds between `min` and `max` of them.

    1. Free the slots of any parent above `max`.
    2. Count each parent's deficit below `min`.
    3. When the deficits exceed the freed slots, take donor slots from parents above `min`;
       a donor gives at most `count - min`.
    4. Queue the deficits, then fill what is left from the parents that still have room.
    5. Shuffle the queue and write it to the freed slots.

    Feasible bounds always end inside both limits. The final count is checked on an explicit
    branch, not an `assert`, so `python -O` keeps it: bounds that do not hold are refused with
    `runtime.reference_bounds_unsatisfied`, never patched on a best-effort basis.
    """
    n = len(values)
    max_eff = max_per_parent if max_per_parent > 0 else n + 1
    keys = [value_key(pv) for pv in ref_values]
    parent_of = {key: pv for key, pv in zip(keys, ref_values, strict=True)}
    slot_keys = [value_key(v) for v in values]
    counts: Counter[Any] = Counter()
    positions: dict[Any, list[int]] = {}
    for i, key in enumerate(slot_keys):
        counts[key] += 1
        positions.setdefault(key, []).append(i)
    free_slots: list[int] = []

    for key in keys:
        if counts[key] > max_eff:
            excess = counts[key] - max_eff
            indices = list(positions[key])
            rng.shuffle(indices)
            free_slots.extend(indices[:excess])
            counts[key] = max_eff

    # Deficits use the counts after step 1 trimmed each parent to `max`.
    deficits = {key: max(0, min_per_parent - counts[key]) for key in keys}
    total_deficit = sum(deficits.values())

    if total_deficit > len(free_slots):
        needed = total_deficit - len(free_slots)
        already_free = set(free_slots)
        donor_indices: list[int] = []
        for key in keys:
            surplus = counts[key] - min_per_parent
            if surplus <= 0:
                continue
            candidates = [i for i in positions.get(key, ()) if i not in already_free]
            rng.shuffle(candidates)
            donor_indices.extend(candidates[:surplus])
        rng.shuffle(donor_indices)
        taken = donor_indices[:needed]
        for i in taken:
            counts[slot_keys[i]] -= 1
        free_slots.extend(taken)

    queue: list[Any] = []
    for key in keys:
        queue.extend([key] * deficits[key])
    del queue[len(free_slots) :]

    remaining = len(free_slots) - len(queue)
    if remaining > 0:
        tally = Counter(counts)
        for key in queue:
            tally[key] += 1
        # Parents with room; one leaves the array the moment its last slot is taken, so a
        # pick never lands on a full parent and nothing is sized by `max`.
        open_keys = [key for key in keys if tally[key] < max_eff]
        for _ in range(remaining):
            if not open_keys:
                break
            pick = rng.randrange(len(open_keys))
            key = open_keys[pick]
            queue.append(key)
            tally[key] += 1
            if tally[key] >= max_eff:
                open_keys[pick] = open_keys[-1]
                open_keys.pop()

    # Shuffled so the repair does not bias position.
    rng.shuffle(queue)
    for slot, key in zip(free_slots, queue, strict=False):
        values[slot] = parent_of[key]

    final: Counter[Any] = Counter()
    for value in values:
        final[value_key(value)] += 1
    if any(final[key] < min_per_parent or final[key] > max_eff for key in keys):
        raise GenerationError(
            code=reasons.runtime.REFERENCE_BOUNDS_UNSATISFIED,
            message=_text(reasons.runtime.REFERENCE_BOUNDS_UNSATISFIED, table, column),
        )
    return values
