"""A key batch wider than its declared type is refused before any INSERT, without echoing a value."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfigError
from decoy_engine.profiling import KeyBatches, KeyFacts, key_facts, passes_through

# Above the int32 range: a wrap or a DuckDB overflow message would carry it.
CANARY_VALUE = 4_000_000_007


def _keys(declared: pa.DataType, *batches: pa.Array) -> KeyBatches:
    made = [pa.RecordBatch.from_arrays([b], names=["k0"]) for b in batches]
    return KeyBatches(pa.schema([("k0", declared)]), lambda: iter(made))


def _facts(tmp_path: Path, parent: KeyBatches, *children: KeyBatches) -> KeyFacts:
    return key_facts(parent, children, scratch=tmp_path / "s", memory_limit="64MB")


def test_a_wider_batch_raises_a_coded_error_that_passes_through_and_names_no_value(
    tmp_path: Path,
) -> None:
    wide = _keys(pa.int32(), pa.array([1, CANARY_VALUE], pa.int64()))
    with pytest.raises(PipelineConfigError) as info:
        _facts(tmp_path, wide)
    error = info.value
    assert passes_through(error)
    text = str(error)
    assert str(CANARY_VALUE) not in text and "4000000007" not in text
    assert "k0" in text and "parent" in text
    for chained in (error.__cause__, error.__context__):
        assert chained is None or str(CANARY_VALUE) not in str(chained)


def test_the_guard_also_covers_a_child(tmp_path: Path) -> None:
    parent = _keys(pa.int32(), pa.array([1], pa.int32()))
    wide = _keys(pa.int32(), pa.array([CANARY_VALUE], pa.int64()))
    with pytest.raises(PipelineConfigError, match="child"):
        _facts(tmp_path, parent, wide)


def test_a_wide_batch_after_valid_ones_is_refused(tmp_path: Path) -> None:
    mixed = _keys(pa.int32(), pa.array([1], pa.int32()), pa.array([CANARY_VALUE], pa.int64()))
    with pytest.raises(PipelineConfigError):
        _facts(tmp_path, mixed)


def test_a_batch_equal_to_the_declared_types_loads_as_before(tmp_path: Path) -> None:
    facts = _facts(tmp_path, _keys(pa.int32(), pa.array([1, 1, 2], pa.int32())))
    assert (facts.status, facts.parent_rows, facts.parent_duplicate_excess) == ("checked", 3, 1)


def test_a_narrower_batch_in_the_same_family_loads(tmp_path: Path) -> None:
    facts = _facts(tmp_path, _keys(pa.int32(), pa.array([1, 2], pa.int16())))
    assert (facts.status, facts.parent_rows) == ("checked", 2)


def test_a_union_whose_batches_are_wider_than_its_first_schema_is_not_refused(
    tmp_path: Path,
) -> None:
    # The multi-parent union declares the first parent's int32 schema over an int64 parent's
    # batches; the other end widens the table to BIGINT, so the batches fit.
    union = _keys(pa.int32(), pa.array([1, CANARY_VALUE], pa.int64()))
    child = _keys(pa.int64(), pa.array([CANARY_VALUE, 7], pa.int64()))
    facts = _facts(tmp_path, union, child)
    assert facts.status == "checked"
    assert facts.children[0].orphan_rows == 1
