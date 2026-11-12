"""GEN-UNIQUE E1: `unique_set_bytes` prices the seen set and the `random.sample` working set."""

from __future__ import annotations

from decoy_engine.generation import _bounds
from decoy_engine.generation._bounds import STR_OBJECT_OVERHEAD_BYTES, unique_set_bytes


def test_zero_rows_cost_nothing():
    assert unique_set_bytes(0, 100) == 0


def test_linear_in_rows():
    assert unique_set_bytes(2_000, 40) == 2 * unique_set_bytes(1_000, 40)


def test_wider_payload_costs_more_by_the_payload_difference_per_row():
    assert unique_set_bytes(1_000, 50) - unique_set_bytes(1_000, 40) == 1_000 * 10


def test_covers_the_str_objects_the_seen_set_holds():
    rows, payload = 1_000, 40
    assert unique_set_bytes(rows, payload) > rows * (payload + STR_OBJECT_OVERHEAD_BYTES)


def test_covers_the_sample_positions_even_for_a_zero_width_payload():
    assert unique_set_bytes(1_000, 0) > 1_000 * STR_OBJECT_OVERHEAD_BYTES


def test_is_exported():
    assert "unique_set_bytes" in _bounds.__all__
