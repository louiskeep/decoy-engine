"""Direct unit tests for `_native_route_preflight.py` (Q3 slice 2): the
four-state resolver, the normative admission matrix, the schema-drift guard,
and the source-snapshot digest codec.

Production-entry parity for the matrix (an Admit cell byte-matching the
pandas oracle, a Reroute cell falling back correctly) lives in
`tests/parity/native/test_native_route_wider_types.py`; this file pins the
matrix TABLE itself and the digest codec's byte-level contract, both of
which a production-entry test cannot exercise directly.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._native_route_preflight import (
    ColumnState,
    ExecutionDigestState,
    PreflightColumnAccumulator,
    TypeFamily,
    combine_column_digests,
    resolve_admission,
    resolve_column_state,
    schema_drift_reason,
    type_family,
)

# ---------------------------------------------------------------------------
# resolve_column_state: the four-state resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "total_rows,null_count,expected",
    [
        (0, 0, "empty"),
        (5, 0, "no_null"),
        (5, 5, "all_null"),
        (5, 2, "partial_null"),
        (1, 0, "no_null"),
        (1, 1, "all_null"),
    ],
)
def test_resolve_column_state(total_rows: int, null_count: int, expected: ColumnState) -> None:
    assert resolve_column_state(total_rows, null_count) == expected


# ---------------------------------------------------------------------------
# type_family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arrow_type,expected",
    [
        (pa.utf8(), "utf8"),
        (pa.bool_(), "boolean"),
        (pa.int8(), "integer"),
        (pa.int64(), "integer"),
        (pa.uint64(), "integer"),
        (pa.timestamp("ns"), "timestamp"),
        (pa.timestamp("ms", tz="UTC"), "timestamp"),
        (pa.large_utf8(), None),
        (pa.float64(), None),
        (pa.decimal128(10, 2), None),
        (pa.binary(), None),
    ],
)
def test_type_family(arrow_type: pa.DataType, expected: TypeFamily | None) -> None:
    assert type_family(arrow_type) == expected


# ---------------------------------------------------------------------------
# resolve_admission: the normative matrix (plan section 3), pinned directly
# ---------------------------------------------------------------------------

# (strategy, family) -> (no_null, partial_null, all_null, empty), each True=Admit.
# Transcribed from the plan's own table so this test fails the moment the
# matrix dict drifts from the normative spec, independent of any oracle probe.
_EXPECTED_MATRIX: dict[tuple[str, TypeFamily], tuple[bool, bool, bool, bool]] = {
    ("passthrough", "integer"): (True, False, False, True),
    ("passthrough", "boolean"): (True, True, False, True),
    ("passthrough", "timestamp"): (True, True, True, True),
    ("redact", "integer"): (True, True, False, False),
    ("redact", "boolean"): (True, True, False, False),
    ("redact", "timestamp"): (True, True, False, False),
    ("truncate", "integer"): (True, False, False, False),
    ("truncate", "boolean"): (True, True, False, False),
    ("truncate", "timestamp"): (True, True, False, False),
}
_STATES: tuple[ColumnState, ...] = ("no_null", "partial_null", "all_null", "empty")


@pytest.mark.parametrize(
    "strategy,family,state,expected",
    [
        (strategy, family, state, expected[i])
        for (strategy, family), expected in _EXPECTED_MATRIX.items()
        for i, state in enumerate(_STATES)
    ],
)
def test_admission_matrix_matches_normative_table(
    strategy: str, family: TypeFamily, state: ColumnState, expected: bool
) -> None:
    assert resolve_admission(strategy, family, state) is expected


def test_admission_matrix_utf8_has_no_row() -> None:
    """utf8 admission stays schema-only (slice 1, unchanged); asking the
    matrix for it is a caller bug the matrix must not silently answer."""
    with pytest.raises(KeyError):
        resolve_admission("passthrough", "utf8", "no_null")


# ---------------------------------------------------------------------------
# schema_drift_reason
# ---------------------------------------------------------------------------


def test_schema_drift_reason_none_when_identical() -> None:
    schema = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.utf8())])
    assert schema_drift_reason(schema, schema) is None


def test_schema_drift_reason_detects_missing_and_extra_columns() -> None:
    baseline = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.utf8())])
    actual = pa.schema([pa.field("a", pa.int64()), pa.field("c", pa.utf8())])
    reason = schema_drift_reason(baseline, actual)
    assert reason is not None and reason.startswith("columns_changed:")
    assert "'b'" in reason and "'c'" in reason


def test_schema_drift_reason_detects_reorder() -> None:
    baseline = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.utf8())])
    reordered = pa.schema([pa.field("b", pa.utf8()), pa.field("a", pa.int64())])
    reason = schema_drift_reason(baseline, reordered)
    assert reason is not None and reason.startswith("columns_changed:")


def test_schema_drift_reason_detects_type_change() -> None:
    baseline = pa.schema([pa.field("a", pa.int64())])
    changed = pa.schema([pa.field("a", pa.float64())])
    reason = schema_drift_reason(baseline, changed)
    assert reason == "type_changed:a:int64->double"


# ---------------------------------------------------------------------------
# Digest codec: injective framing, partition independence
# ---------------------------------------------------------------------------


def _digest_of(batches: list[pa.RecordBatch], schema: pa.Schema) -> bytes:
    column_order = tuple(schema.names)
    accumulators = {
        name: PreflightColumnAccumulator(name=name, arrow_type=schema.field(name).type)
        for name in column_order
    }
    for batch in batches:
        for name in column_order:
            accumulators[name].observe(batch.column(name))
    return combine_column_digests([accumulators[name].digest() for name in column_order])


def test_digest_no_ambiguous_utf8_concatenation() -> None:
    """"ab"+"c" and "a"+"bc" naive-concatenate to the same bytes; the
    length-prefixed framing must tell them apart."""
    schema = pa.schema([pa.field("c", pa.utf8())])
    batch_a = pa.record_batch({"c": pa.array(["ab", "c"], type=pa.utf8())})
    batch_b = pa.record_batch({"c": pa.array(["a", "bc"], type=pa.utf8())})
    assert _digest_of([batch_a], schema) != _digest_of([batch_b], schema)


def test_digest_changes_on_validity_only_change() -> None:
    schema = pa.schema([pa.field("c", pa.int64())])
    no_null = pa.record_batch({"c": pa.array([1, 2, 3], type=pa.int64())})
    with_null = pa.record_batch({"c": pa.array([1, None, 3], type=pa.int64())})
    assert _digest_of([no_null], schema) != _digest_of([with_null], schema)


def test_digest_changes_on_row_permutation() -> None:
    schema = pa.schema([pa.field("c", pa.utf8())])
    forward = pa.record_batch({"c": pa.array(["x", "y", "z"], type=pa.utf8())})
    reversed_ = pa.record_batch({"c": pa.array(["z", "y", "x"], type=pa.utf8())})
    assert _digest_of([forward], schema) != _digest_of([reversed_], schema)


def test_digest_changes_on_timezone_change() -> None:
    utc_schema = pa.schema([pa.field("c", pa.timestamp("ms", tz="UTC"))])
    ny_schema = pa.schema([pa.field("c", pa.timestamp("ms", tz="America/New_York"))])
    utc_batch = pa.record_batch({"c": pa.array([0, 1000], type=pa.timestamp("ms", tz="UTC"))})
    ny_batch = pa.record_batch(
        {"c": pa.array([0, 1000], type=pa.timestamp("ms", tz="America/New_York"))}
    )
    assert _digest_of([utc_batch], utc_schema) != _digest_of([ny_batch], ny_schema)


def test_digest_partition_independent() -> None:
    """Re-partitioning the SAME logical data into different batch sizes must
    digest equal -- batch partitioning is explicitly not part of identity."""
    schema = pa.schema([pa.field("c", pa.int64())])
    values = list(range(97))  # a prime length so partitions land unevenly
    whole = [pa.record_batch({"c": pa.array(values, type=pa.int64())})]
    chunked = [
        pa.record_batch({"c": pa.array(values[i : i + 11], type=pa.int64())})
        for i in range(0, len(values), 11)
    ]
    assert _digest_of(whole, schema) == _digest_of(chunked, schema)


def test_digest_partition_independent_with_nulls_and_utf8() -> None:
    schema = pa.schema([pa.field("c", pa.utf8())])
    values = [f"row-{i}" if i % 7 else None for i in range(50)]
    whole = [pa.record_batch({"c": pa.array(values, type=pa.utf8())})]
    chunked = [
        pa.record_batch({"c": pa.array(values[i : i + 6], type=pa.utf8())})
        for i in range(0, len(values), 6)
    ]
    assert _digest_of(whole, schema) == _digest_of(chunked, schema)


# ---------------------------------------------------------------------------
# ExecutionDigestState.verify
# ---------------------------------------------------------------------------


def test_execution_digest_state_verify_raises_on_mismatch() -> None:
    schema = pa.schema([pa.field("c", pa.int64())])
    state = ExecutionDigestState(
        column_order=("c",), schema=schema, expected_digest=b"\x00" * 32
    )
    state.observe_batch(pa.record_batch({"c": pa.array([1, 2, 3], type=pa.int64())}))
    with pytest.raises(ExecutionError) as excinfo:
        state.verify(table="t")
    assert excinfo.value.code == "native_source_snapshot_digest_mismatch"


def test_execution_digest_state_verify_passes_on_match() -> None:
    schema = pa.schema([pa.field("c", pa.int64())])
    batch = pa.record_batch({"c": pa.array([1, 2, 3], type=pa.int64())})
    expected = _digest_of([batch], schema)
    state = ExecutionDigestState(column_order=("c",), schema=schema, expected_digest=expected)
    state.observe_batch(batch)
    state.verify(table="t")  # no raise
