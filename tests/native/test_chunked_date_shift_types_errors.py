"""C4 acceptance: the string output pin and format_error fail-closed parity.

Output type: date_shift is tokenizing, its output is always a strftime string, and pandas
infers `float64` for a zero-row chunk and `null` for an all-null one. A column's Arrow type
must not depend on chunk boundaries, so native-admissible date_shift is pinned to `string`
on both chunked legs and at both schema-rule construction sites, with or without the
companion. The whole-frame route still resolves the type at assembly; both diffs are the
accepted route-dependent difference.

format_error: a non-null value that does not parse is a per-row error. The oracle chunked
leg fails closed on it and reports a CHUNK-LOCAL `row_index` (it never adds the chunk's
global offset); the native leg must report the identical records.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._chunked import concat_masked_chunks
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution.native import _dispatch
from decoy_engine.execution.native._chunked_schema_rule import build_schema_rule
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_date_shift_support import (
    FORCE,
    FORMAT_ERROR_REASON,
    Outcome,
    date_value,
    ds_col,
    make_config,
    passthrough,
    run_one,
    run_outcome,
    run_pair,
    source,
    spy_index_kernel,
    with_force,
)
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    TABLE,
    force_oracle,
)
from tests.native.test_chunked_entry_values_schema import _full_frame
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a

# ---------------------------------------------------------------------------
# 1. Output type: pinned to `string` on both legs, companion-independent.
# ---------------------------------------------------------------------------


def _types(run_out: list[pa.Table]) -> list[pa.DataType]:
    return [t.schema.field("d").type for t in run_out]


def _reassembled(run_out: list[pa.Table]) -> pa.DataType:
    chunks = [t.drop_columns([FORCE]) if FORCE in t.column_names else t for t in run_out]
    return concat_masked_chunks(chunks, table=TABLE).schema.field("d").type


_TYPE_CASES: dict[str, Callable[[], list[pa.Table]]] = {
    "zero_row": lambda: [source([])],
    "non_empty_all_null": lambda: [source([None] * 5)],
    "valued": lambda: [source([date_value(i) for i in range(5)])],
    "null_then_valued": lambda: [source([None] * 4), source([date_value(i) for i in range(4)])],
    "valued_then_null": lambda: [source([date_value(i) for i in range(4)]), source([None] * 4)],
    "empty_then_valued": lambda: [source([]), source([date_value(i) for i in range(4)])],
    "all_empty": lambda: [source([]), source([])],
    "all_null_chunks": lambda: [source([None] * 3), source([None] * 2)],
}


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_TYPE_CASES))
def test_output_type_is_string_and_equal_on_both_legs(case: str) -> None:
    chunks = _TYPE_CASES[case]()
    native, forced = run_pair([ds_col(), passthrough("p")], chunks)
    assert native.ev[0].native_admitted is True
    assert _types(native.out) == [pa.string()] * len(chunks), "native per-chunk type"
    assert _types(forced.out) == [pa.string()] * len(chunks), "oracle per-chunk type"
    assert _reassembled(native.out) == _reassembled(forced.out) == pa.string()
    for got, want in zip(native.out, forced.out, strict=True):
        assert got.schema.field("d").equals(want.schema.field("d"), check_metadata=True)


def _no_index_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Any:
        raise CryptoExtensionUnavailableError("index kernel unavailable for the test")

    monkeypatch.setattr(_dispatch, "load_compiled_index_kernel", _raise)


@pytest.mark.parametrize("case", sorted(_TYPE_CASES))
def test_the_pin_does_not_depend_on_the_companion(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_index_kernel(monkeypatch)
    chunks = _TYPE_CASES[case]()
    run = run_one(make_config([ds_col(), passthrough("p")]), chunks)
    assert run.ev[0].native_admitted is False
    assert "index_extension_unavailable" in (run.ev[0].reroute_reason or "")
    assert _types(run.out) == [pa.string()] * len(chunks)


def _rule_strings(columns: list[dict[str, Any]], table: pa.Table) -> frozenset[str]:
    """What BOTH schema-rule construction sites call: the dispatcher passes the categorical
    set and nothing else; the streamed sink passes neither. The date_shift pin must not
    depend on a per-site argument."""
    rule = build_schema_rule(
        make_config(columns), table=TABLE, first=table, registry=get_default_registry()
    )
    return rule.string_columns


def test_the_pin_is_applied_by_the_shared_schema_rule_without_site_arguments() -> None:
    table = source([date_value(1), None])
    assert "d" in _rule_strings([ds_col(), passthrough("p")], table)


@pytest.mark.parametrize(
    ("column", "typ"),
    [
        (ds_col(), pa.int64()),
        (ds_col(), pa.large_string()),
        (ds_col(), pa.date32()),
        (ds_col(date_format=None), pa.string()),
        (ds_col(date_format=""), pa.string()),
        (ds_col(namespace=None), pa.string()),
        (ds_col(group_by="p"), pa.string()),
        (ds_col(when="d != ''"), pa.string()),
        (ds_col(date_format="%Y-%m-%d %z"), pa.string()),
        (ds_col(min_days=True), pa.string()),
        (ds_col(max_days=10**9), pa.string()),
    ],
    ids=[
        "int64_source",
        "large_string_source",
        "date32_source",
        "autodetect_absent",
        "autodetect_empty",
        "no_namespace",
        "group_by",
        "when",
        "timezone_directive",
        "bool_bound",
        "out_of_range_bound",
    ],
)
def test_a_non_admissible_date_shift_is_not_pinned(
    column: dict[str, Any], typ: pa.DataType
) -> None:
    values: list[Any] = [date_value(1), None]
    if typ == pa.int64():
        values = [20240101, None]
    elif typ == pa.date32():
        import datetime as dt

        values = [dt.date(2024, 1, 1), None]
    table = pa.table({"d": pa.array(values, typ), "p": pa.array([1, 2], pa.int64())})
    columns = [column, passthrough("p")]
    config = make_config(columns)
    if "when" in column:
        config["tables"][0]["columns"][0]["when"] = column["when"]
    rule = build_schema_rule(config, table=TABLE, first=table, registry=get_default_registry())
    assert "d" not in rule.string_columns


def _late_date_shift_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    """Two all-null leading chunks, then dates: with no pin the output type of `val` is
    unknown until the third chunk, so the stream would hold the first two back."""
    nulls = 2 * support.CHUNK
    dates = [f"2021-{1 + (i % 12):02d}-15" for i in range(16)]
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(nulls + 16)]),
            "val": pa.array([None] * nulls + dates, pa.string()),
        }
    )
    column = {
        "name": "val",
        "strategy": "date_shift",
        "namespace": "ds_ns",
        "provider_config": {"date_format": "%Y-%m-%d", "min_days": -30, "max_days": 30},
    }
    cfg = support.make_cfg(
        [support.hash_col("h"), column], path=support.write_source(src, tmp_path / "s.parquet")
    )
    return cfg, src


@pytest.mark.parametrize("companion", ["present", "absent"])
def test_the_streamed_sink_site_pins_the_type_so_nothing_is_held_back(
    companion: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if companion == "absent":
        _no_index_kernel(monkeypatch)
    cfg, src = _late_date_shift_job(tmp_path)
    expected = b6a.reference(cfg, src)[support.TABLE]
    assert expected.schema.field("val").type == pa.string()
    spill = tmp_path / "spill"
    spill.mkdir()
    sink, _target = b6a.real_sink(spill)
    result = b6a.run_streamed(cfg, src, sink)
    block = result.quality_metrics["auto_chunk"]["output"]
    assert block["held_back_chunks"] == 0
    assert sink.schemas[support.TABLE].field("val").type == pa.string()
    b6a.assert_streamed_equals(sink, expected)


def test_the_whole_frame_route_keeps_its_own_type_for_the_two_degenerate_shapes(
    tmp_path: Path,
) -> None:
    """The accepted route-dependent diffs: whole-frame all-null is `null` and zero-row is
    `double`, while the chunked legs above are always `string`."""
    config = make_config([ds_col(), passthrough("p")])
    (tmp_path / "a").mkdir()
    (tmp_path / "e").mkdir()
    all_null = _full_frame(config, source([None] * 4), tmp_path / "a")
    assert all_null.schema.field("d").type == pa.null()
    empty = _full_frame(config, source([]), tmp_path / "e")
    assert empty.schema.field("d").type == pa.float64()


# ---------------------------------------------------------------------------
# 2. format_error: fail-closed parity, CHUNK-LOCAL row positions.
# ---------------------------------------------------------------------------

_BAD = "not-a-date"


def _good(n: int, start: int = 0) -> list[str | None]:
    return [date_value(start + i) for i in range(n)]


def _legs(
    columns: list[dict[str, Any]], chunks: list[pa.Table], **kw: Any
) -> tuple[Outcome, Outcome]:
    native = run_outcome(make_config(columns), chunks, **kw)
    forced = run_outcome(
        make_config([*columns, force_oracle(FORCE)]), [with_force(c) for c in chunks], **kw
    )
    assert len(native.ev) == 1 and native.ev[0].native_admitted is True, native.ev
    assert len(forced.ev) == 1 and forced.ev[0].native_admitted is False, forced.ev
    assert "group_key_not_native_chunked_route" in (forced.ev[0].reroute_reason or "")
    return native, forced


def _records(outcome: Outcome) -> tuple[RowErrorRecord, ...]:
    assert isinstance(outcome.error, RowErrorsFailedError), outcome.error
    return tuple(outcome.error.records)


@NEEDS_COMPANION
@pytest.mark.parametrize("base_row_offset", [0, 100, 5_000])
def test_a_format_error_fails_closed_with_chunk_local_positions_on_both_legs(
    base_row_offset: int,
) -> None:
    chunks = [
        source(_good(2)),
        source([date_value(5), _BAD, date_value(6), "2021-13-45"]),
        source(_good(3, 9)),
    ]
    native, forced = _legs([ds_col(), passthrough("p")], chunks, base_row_offset=base_row_offset)
    want = (
        RowErrorRecord(TABLE, "d", 1, "format_error", FORMAT_ERROR_REASON),
        RowErrorRecord(TABLE, "d", 3, "format_error", FORMAT_ERROR_REASON),
    )
    assert _records(forced) == want
    assert _records(native) == want, "positions are chunk-local, never base_row_offset-shifted"
    assert type(native.error) is type(forced.error)
    assert str(native.error) == str(forced.error)


@NEEDS_COMPANION
def test_a_failure_in_the_first_chunk_reports_positions_within_that_chunk() -> None:
    chunks = [source([_BAD, date_value(1)]), source(_good(2))]
    native, forced = _legs([ds_col(), passthrough("p")], chunks, base_row_offset=7)
    assert _records(native) == _records(forced)
    assert [r.row_index for r in _records(native)] == [0]


@NEEDS_COMPANION
def test_multiple_bad_rows_and_columns_come_out_in_the_oracle_work_order() -> None:
    """Source order is `b` then `a`; the oracle emits per column in sorted work-list order,
    so `a`'s errors come first even though `b` is masked first by the native loop."""
    table = pa.table(
        {
            "b": pa.array([date_value(1), _BAD, date_value(2), "x"], pa.string()),
            "a": pa.array(["y", date_value(3), "z", date_value(4)], pa.string()),
        }
    )
    columns = [ds_col("b", namespace="ns_b"), ds_col("a", namespace="ns_a")]
    native, forced = _legs(columns, [table])
    assert [(r.column, r.row_index) for r in _records(forced)] == [
        ("a", 0),
        ("a", 2),
        ("b", 1),
        ("b", 3),
    ]
    assert _records(native) == _records(forced)


@NEEDS_COMPANION
def test_the_failing_chunk_is_recorded_before_the_raise_and_nothing_after_it_runs() -> None:
    chunks = [
        source(_good(3)),
        source([date_value(1), _BAD]),
        source([_BAD, "also bad"]),
        source(_good(2)),
    ]
    native, forced = _legs([ds_col(), passthrough("p")], chunks, with_vault=True)
    for leg in (native, forced):
        assert _records(leg) == (
            RowErrorRecord(TABLE, "d", 1, "format_error", FORMAT_ERROR_REASON),
        )
        assert len(leg.out) == 1, "only the chunk before the failing one was yielded"
        assert len(leg.sink) == 2, "the clean chunk and the failing chunk"
        assert leg.sink[-1].row_errors == _records(leg)
        assert leg.sink[0].row_errors == ()
        assert leg.vault_adds == 1, "the failing chunk never reaches the vault"
    assert native.chunks_pulled == forced.chunks_pulled
    assert native.chunks_pulled < len(chunks), "later chunks are never inspected"
    assert TABLE in native.sink[-1].outputs
    assert native.sink[-1].outputs[TABLE].num_rows == 2


@NEEDS_COMPANION
def test_a_source_null_is_not_a_format_error_but_an_unparseable_value_is() -> None:
    clean = run_outcome(
        make_config([ds_col(), passthrough("p")]), [source([None, date_value(1), None])]
    )
    assert clean.error is None
    bad = run_outcome(make_config([ds_col(), passthrough("p")]), [source([None, _BAD, None])])
    assert [r.row_index for r in _records(bad)] == [1]


@NEEDS_COMPANION
def test_the_error_chunk_evidence_of_an_all_unparseable_chunk_claims_no_compiled_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = spy_index_kernel(monkeypatch)
    chunks = [source([date_value(1), date_value(2)]), source([_BAD, "worse"])]
    native = run_outcome(make_config([ds_col(), passthrough("p")]), chunks)
    assert _records(native) == (
        RowErrorRecord(TABLE, "d", 0, "format_error", FORMAT_ERROR_REASON),
        RowErrorRecord(TABLE, "d", 1, "format_error", FORMAT_ERROR_REASON),
    )
    assert len(kernel.pool_sizes()) == 1, "only the first chunk derived anything"
    failing = native.sink[-1].quality_metrics["chunked_route"]
    col = {c["column"]: c for c in failing["columns"]}["d"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "arrow_python"
    assert native.ev[0].kernel_calls["date_shift"] == 2
