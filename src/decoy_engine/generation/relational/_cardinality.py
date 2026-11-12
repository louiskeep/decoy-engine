"""Per-parent cardinality repair for generated reference columns.

Moved from ``generators/columns.py`` so the V1 ``ColumnGenerator`` and the v2 reference draw
share one repair.
"""

from __future__ import annotations

import logging
import random
from collections import Counter


def apply_cardinality_bounds(
    values: list,
    ref_values: list,
    min_per_parent: int,
    max_per_parent: int,
    rng: random.Random,
    *,
    logger: logging.Logger,
) -> list:
    """Repair a generated value list to honor per-parent cardinality bounds.

    Repair algorithm:
      1. Free over-max slots: for any parent value above max, mark
         the excess slot positions for replacement.
      2. Compute under-min deficits per parent value.
      3. If the deficit exceeds the over-max free slots, pull donor
         slots from over-min values (values that have more than
         min). Each donor can supply at most ``count - min`` slots
         without violating its own min.
      4. Build a replacement queue: under-min injections first, any
         remaining slots filled randomly from eligible parent values
         (those not yet at max).
      5. Shuffle + apply to the freed slots.

    Best-effort on impossible constraints (warn, never raise):
      - ``min * |pool| > num_rows``: cannot satisfy min for every
        parent; partially satisfy and warn.
      - All values at max while slots remain: over-fill the pool
        uniformly and warn (the constraint is unsatisfiable but the
        caller wanted num_rows back).
    """
    n = len(values)
    max_eff = max_per_parent if max_per_parent > 0 else n + 1
    counts = Counter(values)
    free_slots: list[int] = []

    # 1. Mandatory: free over-max excess slots.
    for pv in ref_values:
        if counts[pv] > max_eff:
            excess = counts[pv] - max_eff
            indices = [i for i, v in enumerate(values) if v == pv]
            rng.shuffle(indices)
            free_slots.extend(indices[:excess])
            counts[pv] = max_eff

    # 2. Compute under-min deficits using post-truncation counts.
    deficits = {pv: max(0, min_per_parent - counts[pv]) for pv in ref_values}
    total_deficit = sum(deficits.values())

    # 3. Optional: pull donor slots from over-min values when the
    #    deficit exceeds what step 1 freed. Each donor pv can give
    #    up to (counts[pv] - min_per_parent) slots without dropping
    #    below its own min.
    if total_deficit > len(free_slots):
        needed = total_deficit - len(free_slots)
        already_free = set(free_slots)
        donor_indices: list[int] = []
        for pv in ref_values:
            surplus = counts[pv] - min_per_parent
            if surplus <= 0:
                continue
            candidates = [i for i, v in enumerate(values) if v == pv and i not in already_free]
            rng.shuffle(candidates)
            donor_indices.extend(candidates[:surplus])
        rng.shuffle(donor_indices)
        taken = donor_indices[:needed]
        for i in taken:
            counts[values[i]] -= 1
        free_slots.extend(taken)

    # 4. Build replacement queue: deficits first, then eligible fills.
    queue: list = []
    for pv in ref_values:
        if deficits[pv] > 0:
            queue.extend([pv] * deficits[pv])

    if len(queue) > len(free_slots):
        logger.warning(
            f"Cardinality: min_per_parent={min_per_parent} cannot be "
            f"fully satisfied - would need {len(queue)} injections, only "
            f"{len(free_slots)} slots available without violating other "
            f"min bounds. Partial satisfaction."
        )
        queue = queue[: len(free_slots)]

    remaining = len(free_slots) - len(queue)
    if remaining > 0:
        tally = Counter(counts)
        for q in queue:
            tally[q] += 1
        eligible = [v for v in ref_values if tally[v] < max_eff]
        if not eligible:
            logger.warning(
                f"Cardinality: all values at max_per_parent={max_per_parent}; "
                f"over-filling proportionally for {remaining} rows."
            )
            eligible = ref_values
        queue.extend(rng.choices(eligible, k=remaining))

    # 5. Apply replacements in shuffled order so the repair doesn't
    #    bias position.
    rng.shuffle(queue)
    for slot, replacement in zip(free_slots, queue, strict=False):
        values[slot] = replacement

    return values
