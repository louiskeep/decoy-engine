"""FR3: the position-keyed index selection that backs the unified core's uniform and weighted draws.

These are unit proofs of `generation/_kernel_select.py`: the weight mapping declines what it cannot
hold, a draw depends only on the key and the global row, and the compiled kernel and the Python
reference agree. Distribution proofs over a real generation live in
`tests/parity/test_phase_f_fr3_selection.py`.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pytest

from decoy_engine.determinism import derive_index
from decoy_engine.generation import _kernel_select as ks
from decoy_engine.generation.pool import _sampler
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.kernel import encode_int


def _ctx(name: str = "c") -> GenDeriveContext:
    return GenDeriveContext.for_column(None, {"name": name, "type": "categorical"}, 7)


# ---- the weight mapping ----------------------------------------------------------------------


def test_thresholds_split_the_draw_space_in_proportion_to_the_weights() -> None:
    thresholds = ks.cdf_thresholds([9, 1], 2)
    assert thresholds is not None
    assert thresholds[-1] == ks.CDF_RESOLUTION
    assert abs(thresholds[0] / ks.CDF_RESOLUTION - 0.9) <= 2**-32


@pytest.mark.parametrize(
    "weights",
    [
        [1, 2],  # wrong length
        [1, -1, 3],  # negative
        [1, float("nan"), 1],
        [1, float("inf"), 1],
        [0, 0, 0],  # no mass
        [1, 1e-12, 2],  # a positive weight below one slot
        ["a", "b", "c"],
        [None, 1, 1],
    ],
)
def test_weights_the_mapping_cannot_hold_are_declined_not_approximated(weights: list) -> None:
    assert ks.cdf_thresholds(weights, 3) is None
    assert ks.weighted_indexer(_ctx(), weights, 3) is None


def test_a_zero_weight_choice_owns_no_slot_and_is_never_drawn() -> None:
    indexer = ks.weighted_indexer(_ctx(), [1, 0, 1], 3)
    assert indexer is not None
    drawn = set(indexer.draw(0, 5_000).tolist())
    assert drawn == {0, 2}


def test_choice_indexer_never_turns_weights_into_a_uniform_draw() -> None:
    ctx = _ctx()
    assert isinstance(ks.choice_indexer(ctx, 4, None), ks.PositionalIndexer)
    assert isinstance(ks.choice_indexer(ctx, 2, [9, 1]), ks.WeightedIndexer)
    # Declined weights return None (the stream keeps the Python RNG), never a PositionalIndexer.
    assert ks.choice_indexer(ctx, 3, [1, -1, 3]) is None


def test_empty_or_oversized_index_sets_have_no_indexer() -> None:
    ctx = _ctx()
    assert ks.uniform_indexer(ctx, 0) is None
    assert ks.uniform_indexer(ctx, ks.MAX_POOL + 1) is None
    assert ks.uniform_indexer(ctx, ks.MAX_POOL) is not None


# ---- a draw is a pure function of (key, row) -------------------------------------------------


def test_draw_matches_the_determinism_primitive_row_by_row() -> None:
    indexer = ks.uniform_indexer(_ctx(), 37)
    assert indexer is not None
    got = indexer.draw(11, 40).tolist()
    want = [
        derive_index(indexer.key, ks.UNIFORM_NAMESPACE, encode_int(g), pool_size=37)
        for g in range(11, 51)
    ]
    assert got == want


def test_batching_cannot_change_a_draw() -> None:
    indexer = ks.weighted_indexer(_ctx(), [5, 3, 2, 1], 4)
    assert indexer is not None
    whole = indexer.draw(0, 300).tolist()
    pieces: list[int] = []
    for start, size in ((0, 1), (1, 0), (1, 99), (100, 200)):
        pieces.extend(indexer.draw(start, size).tolist())
    assert pieces == whole


def test_a_zero_row_draw_is_empty() -> None:
    indexer = ks.uniform_indexer(_ctx(), 5)
    assert indexer is not None
    assert indexer.draw(0, 0).tolist() == []


def test_columns_with_different_configuration_draw_different_streams() -> None:
    a = ks.uniform_indexer(_ctx("a"), 1000)
    other = GenDeriveContext.for_column(None, {"name": "a", "type": "categorical", "x": 1}, 7)
    b = ks.uniform_indexer(other, 1000)
    assert a is not None and b is not None
    assert a.key != b.key
    assert a.draw(0, 64).tolist() != b.draw(0, 64).tolist()


def test_a_rename_does_not_move_a_draw() -> None:
    a = ks.uniform_indexer(_ctx("first"), 1000)
    b = ks.uniform_indexer(_ctx("renamed"), 1000)
    assert a is not None and b is not None
    assert a.draw(0, 64).tolist() == b.draw(0, 64).tolist()


def test_rows_past_the_uint64_domain_fail_closed() -> None:
    indexer = ks.uniform_indexer(_ctx(), 5)
    assert indexer is not None
    with pytest.raises(GenerationError, match="uint64"):
        indexer.draw(2**64 - 2, 5)
    with pytest.raises(GenerationError, match="uint64"):
        indexer.draw(-1, 5)


# ---- the Python reference and the compiled kernel agree --------------------------------------


def test_reference_kernel_gives_the_same_draw_as_whatever_the_host_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexer = ks.uniform_indexer(_ctx(), 10_000)
    weighted = ks.weighted_indexer(_ctx(), [5, 3, 2, 1], 4)
    assert indexer is not None and weighted is not None
    host = (indexer.draw(3, 500).tolist(), weighted.draw(3, 500).tolist())
    monkeypatch.setattr(_sampler, "_compiled_index_kernel", lambda: None)
    assert (indexer.draw(3, 500).tolist(), weighted.draw(3, 500).tolist()) == host


# ---- the distribution is what the configuration says -----------------------------------------


def _z_ok(observed: int, n: int, p: float, z: float = 5.0) -> bool:
    return abs(observed / n - p) <= z * math.sqrt(p * (1 - p) / n) + 1e-9


def test_uniform_draw_is_uniform() -> None:
    n, k = 120_000, 6
    indexer = ks.uniform_indexer(_ctx(), k)
    assert indexer is not None
    counts = Counter(indexer.draw(0, n).tolist())
    assert set(counts) == set(range(k))
    assert all(_z_ok(counts[i], n, 1 / k) for i in range(k))


def test_ninety_ten_stays_ninety_ten() -> None:
    n = 120_000
    indexer = ks.weighted_indexer(_ctx(), [90, 10], 2)
    assert indexer is not None
    counts = Counter(indexer.draw(0, n).tolist())
    assert _z_ok(counts[0], n, 0.9)
    assert _z_ok(counts[1], n, 0.1)
    # A uniform draw would put each near 0.5; this is the flattening the mapping must never do.
    assert counts[0] / n > 0.85


def test_skewed_many_way_weights_keep_their_proportions() -> None:
    weights = [50, 25, 12, 6, 3, 2, 1, 1]
    n = 160_000
    indexer = ks.weighted_indexer(_ctx(), weights, len(weights))
    assert indexer is not None
    counts = Counter(indexer.draw(0, n).tolist())
    total = sum(weights)
    assert all(_z_ok(counts[i], n, w / total) for i, w in enumerate(weights))


def test_draws_are_not_a_sorted_or_periodic_sequence() -> None:
    """A degenerate mapping (index = row mod n) would be uniform in aggregate but ordered."""
    drawn = ks.uniform_indexer(_ctx(), 10)
    assert drawn is not None
    values = drawn.draw(0, 2_000)
    assert not np.array_equal(values, np.arange(2_000) % 10)
    assert len(set(values[:50].tolist())) > 3
