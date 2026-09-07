"""P4 Task 7 slim-sort: `_key_width.SlimRowWidthTracker` bounds the widest
materialized SLIM sorter row (row_nr + match token + masked components), the
quantity `_route_policy.decide_route` gates on.

These pin the bound's rules the plan (§3.4) calls out: an empty/all-null
relation still contributes schema-derived overhead (never 0); dictionary masked
types resolve to their value type; an unknown/nested masked type is UNBOUNDED
(a route fall-back, never 0); composite components are summed within the edge;
and the reported bound is a true UPPER bound on the real single-row `nbytes` the
sorter measures.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.execution.out_of_core._external_sort_bounding import _materialize
from decoy_engine.execution.out_of_core._key_width import (
    _UNBOUNDED_ROW_BYTES,
    SlimRowWidthTracker,
)


def _staging_batch(masked_columns, masked_arrays):
    """A relation-staging batch: row_nr, join_key, then masked columns."""
    n = len(masked_arrays[0]) if masked_arrays else 0
    columns = [
        pa.array(range(n), type=pa.int64()),
        pa.array([f"k{i}" for i in range(n)], type=pa.string()),
        *masked_arrays,
    ]
    names = ["__decoy_row_nr", "__decoy_fk_join_key", *masked_columns]
    return pa.record_batch(columns, names=names)


def _real_slim_row_nbytes(masked_values):
    """The nbytes the sorter actually measures for a single slim row carrying
    `masked_values` (row_nr + bool token + the masked components)."""
    columns = [pa.array([0], type=pa.int64()), pa.array([True], type=pa.bool_())]
    names = ["__decoy_row_nr", "__decoy_parent_match"]
    for idx, value in enumerate(masked_values):
        columns.append(value)
        names.append(f"__decoy_parent_masked_{idx}")
    return _materialize(pa.record_batch(columns, names=names)).nbytes


def test_bound_is_upper_bound_of_real_row_nbytes() -> None:
    masked_types = (pa.string(),)
    tracker = SlimRowWidthTracker(("__decoy_masked_key",), masked_types)
    widest = "z" * 4096
    for batch in tracker.wrap(
        [_staging_batch(["__decoy_masked_key"], [pa.array(["a", widest, "bb"], type=pa.string())])]
    ):
        assert batch.num_rows == 3
    real = _real_slim_row_nbytes([pa.array([widest], type=pa.string())])
    assert tracker.max_sort_payload_row_bytes >= real
    # And not absurdly loose: within a small per-array margin of the real width.
    assert tracker.max_sort_payload_row_bytes <= real + 64


def test_empty_relation_contributes_schema_overhead_not_zero() -> None:
    tracker = SlimRowWidthTracker(("__decoy_masked_key",), (pa.string(),))
    # No batches at all (an empty parent relation): the bound is still the
    # schema-derived row_nr + token + offset/validity overhead, never 0.
    assert 0 < tracker.max_sort_payload_row_bytes < 128


def test_all_null_relation_contributes_schema_overhead_not_zero() -> None:
    tracker = SlimRowWidthTracker(("__decoy_masked_key",), (pa.string(),))
    list(tracker.wrap([_staging_batch(["__decoy_masked_key"], [pa.nulls(3, pa.string())])]))
    assert 0 < tracker.max_sort_payload_row_bytes < 128


def test_dictionary_masked_type_resolves_to_value_type() -> None:
    dict_type = pa.dictionary(pa.int32(), pa.string())
    tracker = SlimRowWidthTracker(("__decoy_masked_key",), (dict_type,))
    widest = "d" * 500
    values = pa.array(["a", widest, "a"], type=pa.string()).dictionary_encode()
    list(tracker.wrap([_staging_batch(["__decoy_masked_key"], [values])]))
    bound = tracker.max_sort_payload_row_bytes
    assert bound != _UNBOUNDED_ROW_BYTES
    # Sized by the decoded VALUE (500 bytes), not the tiny dictionary index.
    assert bound >= 500


def test_unknown_masked_type_is_unbounded() -> None:
    # A nested masked type the slim bound cannot size -> UNBOUNDED (fall back),
    # never silently mapped to 0.
    tracker = SlimRowWidthTracker(("__decoy_masked_key",), (pa.list_(pa.int64()),))
    list(
        tracker.wrap(
            [
                _staging_batch(
                    ["__decoy_masked_key"], [pa.array([[1], [2], [3]], pa.list_(pa.int64()))]
                )
            ]
        )
    )
    assert tracker.max_sort_payload_row_bytes == _UNBOUNDED_ROW_BYTES


def test_composite_components_summed_within_edge() -> None:
    cols = ("__decoy_masked_key", "__decoy_masked_key_1")
    types = (pa.string(), pa.string())
    tracker = SlimRowWidthTracker(cols, types)
    a = pa.array(["x" * 1000, "y", "z"], type=pa.string())
    b = pa.array(["p", "q" * 2000, "r"], type=pa.string())
    list(tracker.wrap([_staging_batch(list(cols), [a, b])]))
    # Conservative: both components' maxima summed into one row (they share the
    # sorter row), even though the 1000- and 2000-byte maxima are in DIFFERENT
    # rows. So the bound is >= a synthetic row carrying BOTH maxima.
    both = _real_slim_row_nbytes(
        [pa.array(["x" * 1000], pa.string()), pa.array(["q" * 2000], pa.string())]
    )
    assert tracker.max_sort_payload_row_bytes >= both


def test_fixed_width_masked_type_bounded() -> None:
    tracker = SlimRowWidthTracker(("__decoy_masked_key",), (pa.int64(),))
    list(tracker.wrap([_staging_batch(["__decoy_masked_key"], [pa.array([1, 2, 3], pa.int64())])]))
    bound = tracker.max_sort_payload_row_bytes
    assert bound != _UNBOUNDED_ROW_BYTES
    assert bound >= _real_slim_row_nbytes([pa.array([1], pa.int64())])
