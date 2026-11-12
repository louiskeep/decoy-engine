"""Shared tables and lattices for the search tests."""

from __future__ import annotations

import pandas as pd

from decoy_engine.anonymize.hierarchies import Lattice, interval_hierarchy, prefix_hierarchy


def small_lattice() -> Lattice:
    """age: value, [10s), *; zip: value, two-char prefix, * (3 x 3 = 9 nodes)."""
    return Lattice({"age": interval_hierarchy((10,)), "zip": prefix_hierarchy(3, keep=(2,))})


def six_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "age": [21, 23, 27, 34, 36, 38],
            "zip": ["120", "120", "121", "130", "130", "131"],
            "dx": ["a", "b", "a", "a", "b", "b"],
        }
    )


def seven_rows() -> pd.DataFrame:
    """``six_rows`` plus one far-away row that only suppression or the top can absorb."""
    frame = six_rows()
    extra = pd.DataFrame({"age": [55], "zip": ["210"], "dx": ["c"]})
    return pd.concat([frame, extra], ignore_index=True)


def two_level_lattice() -> Lattice:
    """age: value, 5s, 10s, *; zip: value, 2-char, 1-char, * (4 x 4 = 16 nodes)."""
    return Lattice({"age": interval_hierarchy((5, 10)), "zip": prefix_hierarchy(3, keep=(2, 1))})


# Found by a scripted search against the brute-force reference and pinned here.
# Entropy l = 2 with suppression is not monotone: from node (0, 2) to (1, 2) the
# outlier rows go from {4, 5, 8, 9} to {5, 6, 7, 9}. With suppression_limit 0.3
# and discernibility, pruning on the loss instead of its bound loses the
# optimum (0, 3) and returns (2, 1).
NON_MONOTONE_ENTROPY = {
    "age": [4, 4, 0, 0, 6, 6, 5, 5, 9, 10],
    "zip": ["121", "121", "121", "121", "210", "130", "120", "130", "210", "121"],
    "dx": ["a", "b", "a", "b", "b", "c", "c", "a", "c", "c"],
}

# Equal t = 0.25 with suppression is not monotone: from (0, 3) to (1, 3) the
# outliers go from {0, 2, 7} to {0, 2, 3, 6, 7}.
NON_MONOTONE_EQUAL_T = {
    "age": [9, 15, 10, 5, 15, 15, 5, 1],
    "zip": ["130", "120", "130", "210", "120", "210", "130", "210"],
    "dx": ["c", "a", "b", "a", "c", "a", "c", "a"],
}

# Tie fixtures on ``two_level_lattice`` with KAnonymity(k):
# (table, k, suppression_limit, metric, winner, runner-up with the same loss)
TIES = {
    "suppressed": (
        {"age": [12, 7, 18, 10, 6], "zip": ["121", "121", "210", "130", "120"]},
        2,
        0.3,
        "discernibility",
        (2, 3),
        (1, 2),
    ),
    # Same loss and suppressed rows; the runner-up is lexicographically smaller,
    # so only the level sum (3 against 5) picks the winner.
    "level_sum": (
        {"age": [11, 4, 1, 13, 9], "zip": ["121", "210", "121", "210", "210"]},
        3,
        0.5,
        "discernibility",
        (3, 0),
        (2, 3),
    ),
    "lexicographic": (
        {"age": [16, 9, 0, 2, 18], "zip": ["120", "210", "120", "130", "210"]},
        2,
        0,
        "discernibility",
        (2, 3),
        (3, 2),
    ),
}
