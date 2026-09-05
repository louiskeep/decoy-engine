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

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _native_route_preflight as _pf
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._native_route_preflight import (
    ColumnState,
    ExecutionDigestState,
    PreflightColumnAccumulator,
    RouteAdmission,
    TypeFamily,
    classify_and_preflight,
    combine_column_digests,
    resolve_admission,
    resolve_column_state,
    run_preflight,
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
    """ "ab"+"c" and "a"+"bc" naive-concatenate to the same bytes; the
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
    state = ExecutionDigestState(column_order=("c",), schema=schema, expected_digest=b"\x00" * 32)
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


# ---------------------------------------------------------------------------
# Digest codec: byte-exact characterization (golden), digest size, slice offset
# ---------------------------------------------------------------------------

# A fixed, mixed-type, mixed-null input exercising every codec branch: a
# null-free and a null-bearing utf8 column (the second holds the ambiguous
# "ab"/"c" boundary), a null-bearing signed int, a null-free unsigned int, a
# null-bearing bool, and tz-naive + tz-aware null-bearing timestamps. The
# per-column and combined digests are pinned byte-for-byte. Any change to the
# hasher parameters (digest size, domain key, version byte), a type token, the
# value / validity / length framing, the framing byte-lengths, or a fill value
# shifts these constants, so a codec mutation that keeps the preflight and
# execution passes self-consistent (and so slips past the equality-only
# mismatch tests) is still caught here. The codec casts to explicit numpy
# dtypes, so the bytes are stable across the pinned pyarrow/numpy toolchain.
_GOLDEN_INPUT: list[tuple[str, pa.Array]] = [
    ("u8_nn", pa.array(["ab", "c", ""], type=pa.utf8())),
    ("u8_nb", pa.array(["x", None, "yz"], type=pa.utf8())),
    ("i_nb", pa.array([-5, None, 7], type=pa.int16())),
    ("u_nn", pa.array([0, 1, 255], type=pa.uint8())),
    ("b_nb", pa.array([True, None, False], type=pa.bool_())),
    ("ts_naive_nb", pa.array([-86400, None, 123456789], type=pa.timestamp("ms"))),
    ("ts_tz_nb", pa.array([0, None, 1000], type=pa.timestamp("s", tz="America/New_York"))),
]
_GOLDEN_PER_COLUMN: dict[str, str] = {
    "u8_nn": "2c375cd0b4978cf447840318be7d64e8cd4fd16de411132741cd5d7fdfd032dc",
    "u8_nb": "34ab8a994cc6b8980f27414d47844b079f0193b4203f8314c1650fdb0e3abd7b",
    "i_nb": "06d23c297aaf400237007a34fef61e9ae929de29a07341b2c43376790a4dc026",
    "u_nn": "eec38f781bf12a96d65bafb6fd43b576652489d6b123de5f8146e3a9defdbf9d",
    "b_nb": "9a5866aa46fbf53d28e8fa81084469ad7fbbc9bb1be0b814cd41ab401e55d0fd",
    "ts_naive_nb": "b954f96916829219246590b6577500e1aeadf6ec51c08d2db94a32e539f0f89b",
    "ts_tz_nb": "e8c7005d34e1c56f278d4617dfaf4d3653f5264e68c42daeacee9cae4e121e1c",
}
_GOLDEN_COMBINED = "ff9759ff696e66f5e1404fcadd50a8519f15c0776168d0253de8eb177e0998e5"


def _accumulate(name: str, array: pa.Array) -> PreflightColumnAccumulator:
    acc = PreflightColumnAccumulator(name=name, arrow_type=array.type)
    acc.observe(array)
    return acc


def test_digest_golden_per_column_bytes() -> None:
    for name, array in _GOLDEN_INPUT:
        assert _accumulate(name, array).digest().hex() == _GOLDEN_PER_COLUMN[name], name


def test_digest_golden_combined_bytes() -> None:
    digests = [_accumulate(name, array).digest() for name, array in _GOLDEN_INPUT]
    assert combine_column_digests(digests).hex() == _GOLDEN_COMBINED


def test_digest_sizes_are_exactly_32_bytes() -> None:
    """Pins the `digest_size=_DIGEST_SIZE` argument: dropping it defaults blake2b
    to a 64-byte digest, which stays self-consistent across both passes and so
    would otherwise survive."""
    digests = [_accumulate(name, array).digest() for name, array in _GOLDEN_INPUT]
    for name, digest in zip((n for n, _ in _GOLDEN_INPUT), digests, strict=True):
        assert len(digest) == 32, name
    assert len(combine_column_digests(digests)) == 32


def test_digest_honors_array_slice_offset() -> None:
    """A sliced array carries a non-zero `offset`; the utf8 length/value framing
    must read from that offset (a batch iterator can hand back slices). Pins the
    `offset=filled.offset * 4` term of the offsets read."""
    sliced = pa.array(["a", "bc", "def"], type=pa.utf8()).slice(1)
    standalone = pa.array(["bc", "def"], type=pa.utf8())
    assert _accumulate("s", sliced).digest() == _accumulate("s", standalone).digest()


def test_digest_honors_middle_slice_offset_length() -> None:
    """A MIDDLE slice leaves parent offsets BEYOND the child in the same buffer;
    the offsets read must be bounded to `count=n+1`, not run to the buffer end
    (which would fold the trailing element's length into a shorter column)."""
    parent = pa.array(["a", "bc", "def", "gh"], type=pa.utf8())
    middle = parent.slice(1, 2)  # ["bc", "def"], with "gh"'s offset still trailing
    standalone = pa.array(["bc", "def"], type=pa.utf8())
    assert _accumulate("s", middle).digest() == _accumulate("s", standalone).digest()


def test_digest_null_fill_value_is_observable_per_type() -> None:
    """The per-type null fill (utf8 "", bool False, int 0, timestamp epoch-0)
    feeds the value stream; a different fill value must change the digest even
    though validity is hashed separately. Uses a single null-bearing value so
    the fill lands in exactly one slot."""
    for arrow_type, other in (
        (pa.int64(), pa.array([9, None], type=pa.int64())),
        (pa.bool_(), pa.array([True, None], type=pa.bool_())),
        (pa.timestamp("ms"), pa.array([5, None], type=pa.timestamp("ms"))),
    ):
        with_null = pa.array([None, None], type=arrow_type)
        # A digest that ignored the fill value would collapse these two
        # distinct value streams to the same bytes.
        assert _accumulate("c", other).digest() != _accumulate("c", with_null).digest()


# ---------------------------------------------------------------------------
# run_preflight: the PreflightResult contract on every branch (direct)
# ---------------------------------------------------------------------------


class _BatchSource:
    """Duck-typed `iter_batches` for `run_preflight`, which reads nothing else
    off the source."""

    def __init__(self, batches: list[pa.RecordBatch]) -> None:
        self._batches = list(batches)

    def iter_batches(self, batch_rows: int) -> Any:
        return iter(self._batches)


def test_run_preflight_admit_result_contract() -> None:
    schema = pa.schema([pa.field("c", pa.int64())])
    batches = [
        pa.record_batch({"c": pa.array([1, 2], type=pa.int64())}),
        pa.record_batch({"c": pa.array([3, 4], type=pa.int64())}),
    ]
    result = run_preflight(
        _BatchSource(batches),  # type: ignore[arg-type]  # duck-typed iter_batches stub
        baseline_schema=schema,
        column_order=("c",),
        strategy_by_column={"c": "passthrough"},
        batch_rows=10,
    )
    assert result.admitted is True
    assert result.reason is None
    assert result.schema is schema
    assert result.digest is not None and len(result.digest) == 32
    assert result.column_states == {"c": "no_null"}


def test_run_preflight_reroute_result_contract() -> None:
    schema = pa.schema([pa.field("c", pa.int64())])
    batches = [pa.record_batch({"c": pa.array([1, None], type=pa.int64())})]
    result = run_preflight(
        _BatchSource(batches),  # type: ignore[arg-type]  # duck-typed iter_batches stub
        baseline_schema=schema,
        column_order=("c",),
        strategy_by_column={"c": "passthrough"},
        batch_rows=10,
    )
    assert result.admitted is False
    assert result.reason == "native_preflight_reroute:c:passthrough:integer:partial_null"
    assert result.schema is schema
    assert result.digest is None
    assert result.column_states == {"c": "partial_null"}


def test_run_preflight_schema_drift_result_contract() -> None:
    baseline = pa.schema([pa.field("c", pa.int64())])
    good = pa.record_batch({"c": pa.array([1, 2], type=pa.int64())})
    drifted = pa.record_batch({"c": pa.array([1.0, 2.0], type=pa.float64())})
    result = run_preflight(
        _BatchSource([good, drifted]),  # type: ignore[arg-type]  # duck-typed iter_batches stub
        baseline_schema=baseline,
        column_order=("c",),
        strategy_by_column={"c": "passthrough"},
        batch_rows=10,
    )
    assert result.admitted is False
    assert result.reason == "native_preflight_schema_drift:type_changed:c:int64->double"
    assert result.schema is baseline
    assert result.digest is None
    assert result.column_states == {}


def test_run_preflight_utf8_column_in_mixed_table_is_skipped_not_matrixed() -> None:
    """A utf8 column has no matrix row; the loop must `continue` past it, never
    call `resolve_admission(strategy, "utf8", state)` (a KeyError). Pins the
    `family is None or family == "utf8"` skip guard."""
    baseline = pa.schema([pa.field("s", pa.utf8()), pa.field("n", pa.int64())])
    batch = pa.record_batch(
        {"s": pa.array(["a", "b"], type=pa.utf8()), "n": pa.array([1, 2], type=pa.int64())}
    )
    result = run_preflight(
        _BatchSource([batch]),  # type: ignore[arg-type]  # duck-typed iter_batches stub
        baseline_schema=baseline,
        column_order=("s", "n"),
        strategy_by_column={"s": "passthrough", "n": "passthrough"},
        batch_rows=10,
    )
    assert result.admitted is True
    assert result.column_states == {"s": "no_null", "n": "no_null"}


def test_run_preflight_continue_reaches_later_reroute_column() -> None:
    """A utf8 column (skipped) ahead of a reroute-worthy int column: the skip
    must `continue`, not `break`, so the later column's reject is still seen."""
    baseline = pa.schema([pa.field("s", pa.utf8()), pa.field("n", pa.int64())])
    batch = pa.record_batch(
        {"s": pa.array(["a", "b"], type=pa.utf8()), "n": pa.array([1, None], type=pa.int64())}
    )
    result = run_preflight(
        _BatchSource([batch]),  # type: ignore[arg-type]  # duck-typed iter_batches stub
        baseline_schema=baseline,
        column_order=("s", "n"),
        strategy_by_column={"s": "passthrough", "n": "passthrough"},
        batch_rows=10,
    )
    assert result.admitted is False
    assert result.reason == "native_preflight_reroute:n:passthrough:integer:partial_null"


# ---------------------------------------------------------------------------
# classify_and_preflight: the RouteAdmission contract on every branch (direct)
# ---------------------------------------------------------------------------


class _SchemaSource:
    """Duck-typed source for `classify_and_preflight`: a fixed footer `schema`
    plus `iter_batches` for the preflight pass."""

    def __init__(self, schema: pa.Schema, batches: list[pa.RecordBatch]) -> None:
        self.schema = schema
        self._batches = list(batches)

    def iter_batches(self, batch_rows: int) -> Any:
        return iter(self._batches)


def _classify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared: set[str],
    schema: pa.Schema,
    batches: list[pa.RecordBatch],
    columns: list[dict[str, Any]],
) -> RouteAdmission:
    monkeypatch.setattr(_pf, "known_output_columns", lambda plan, table: declared)
    config = {"tables": [{"name": "t", "columns": columns}]}
    return classify_and_preflight(
        _SchemaSource(schema, batches),  # type: ignore[arg-type]  # duck-typed stub source
        table="t",
        plan=None,  # type: ignore[arg-type]  # known_output_columns is monkeypatched, plan unused
        config=config,
        batch_rows=10,
    )


def test_classify_unsupported_projection_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = pa.schema([pa.field("a", pa.utf8()), pa.field("c", pa.utf8())])
    result = _classify(monkeypatch, declared={"a", "b"}, schema=schema, batches=[], columns=[])
    assert result.mode == "widened"
    assert result.admitted is False
    assert result.reason == "unsupported_projection:missing=['b']:extra=['c']"


def test_classify_non_utf8_column_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = pa.schema([pa.field("a", pa.float64())])
    result = _classify(monkeypatch, declared={"a"}, schema=schema, batches=[], columns=[])
    assert result.mode == "widened"
    assert result.admitted is False
    assert result.reason == "non_utf8_column:a:double"


def test_classify_utf8_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = pa.schema([pa.field("a", pa.utf8()), pa.field("b", pa.utf8())])
    result = _classify(monkeypatch, declared={"a", "b"}, schema=schema, batches=[], columns=[])
    assert result.mode == "utf8_only"


def test_classify_widened_reroute_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = pa.schema([pa.field("a", pa.int64())])
    batch = pa.record_batch({"a": pa.array([1, None], type=pa.int64())})
    result = _classify(
        monkeypatch,
        declared={"a"},
        schema=schema,
        batches=[batch],
        columns=[{"name": "a", "strategy": "passthrough"}],
    )
    assert result.mode == "widened"
    assert result.admitted is False
    assert result.reason == "native_preflight_reroute:a:passthrough:integer:partial_null"


def test_classify_widened_admit_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = pa.schema([pa.field("a", pa.int64())])
    batch = pa.record_batch({"a": pa.array([1, 2], type=pa.int64())})
    result = _classify(
        monkeypatch,
        declared={"a"},
        schema=schema,
        batches=[batch],
        columns=[{"name": "a", "strategy": "passthrough"}],
    )
    assert result.mode == "widened"
    assert result.admitted is True
    assert result.schema is schema
    assert result.digest is not None and len(result.digest) == 32
    assert result.column_order == ("a",)


# ---------------------------------------------------------------------------
# _strategy_by_column / _find_table
# ---------------------------------------------------------------------------


def test_strategy_by_column_unresolved_raises_coded_error() -> None:
    """A declared column with no `strategy` key raises the coded guard, not a
    bare KeyError. Pins `code="native_preflight_strategy_unresolved"`."""
    config = {"tables": [{"name": "t", "columns": [{"name": "a"}]}]}
    with pytest.raises(ExecutionError) as excinfo:
        _pf._strategy_by_column(config, "t", ("a",))
    assert excinfo.value.code == "native_preflight_strategy_unresolved"


def test_strategy_by_column_resolves_declared_strategy() -> None:
    config = {"tables": [{"name": "t", "columns": [{"name": "a", "strategy": "redact"}]}]}
    assert _pf._strategy_by_column(config, "t", ("a",)) == {"a": "redact"}


def test_find_table_selects_by_name_not_first_dict() -> None:
    """`_find_table` must match on the table NAME, not return the first dict it
    sees. Pins the `isinstance(tbl, dict) and tbl.get("name") == table` guard."""
    config = {
        "tables": [
            {"name": "other", "columns": []},
            {"name": "t", "columns": [{"name": "a", "strategy": "passthrough"}]},
        ]
    }
    found = _pf._find_table(config, "t")
    assert found is not None and found["name"] == "t"
