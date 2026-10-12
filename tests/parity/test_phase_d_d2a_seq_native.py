"""Phase D / D2a stage 2: the SEQ hash routing, proven with a distinguishable stand-in kernel.

No companion is needed: `MarkingKernel` plays the compiled kernel and marks every token it
returns, so a test can tell the kernel's output from a Python recomputation of the same
value. The real-kernel parity runs live in `test_phase_d_d2a_seq_real.py`.

Covers the decision per column, the three FK paths (native parent, inherited child, REMAP),
the pandas to Arrow boundary, the existing ingest guards, and the sink-scoped failure
behavior. Expected tokens are the frozen literals of stage 1.
"""

from __future__ import annotations

import datetime as dt
import decimal
import logging

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter, _sequential_native
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._sequential_native import SEQ_HASH_DISPOSITION_KEY, SeqNativeRun
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import EXPECTED_TOKENS, chain_sources, column_seed
from tests.parity._d2a_seq_data import (
    RecordingSink,
    callable_sink,
    payload_seeds,
    payload_table,
    run_full,
    run_seq,
    sources_with_orphans,
    typed_columns,
    with_when,
)
from tests.parity._seq_witness import (
    MARK,
    MarkingKernel,
    assert_native_consumed,
    assert_python_consumed,
    hide_companion,
    install,
    marked,
    spy_python_hash,
)

_COLUMNS = [*typed_columns()[0], "c_trunc", "c_other_ns"]
_ORPHAN_TOKENS = [
    "eec76cc0854f45170489845b902c4b25c8a159db8e3703ffe0672d377a6c6cdf",
    "112054fc4071e2e01bace616c8f68ef3c7595d55b0f11633046610a06e1c3aef",
]
_INT_KEY_TOKENS = [
    "fff6d07feba156f6826ff1d01f734e0c546250ad326e8928f64e6616ba752ccd",
    "2281ce17d19092cdd3131a04ca5674fb4a18e93d6b00b11753d43108a6134f1f",
    "27b0f2ce7c10728f371130bacdafc5cf362ca3eb8966093ca21972b07289e4b2",
    "fb2f5df29035d1a294711f9e858996e775abf22ce35f564c666b5655da592885",
]
_INT_REMAP_300 = "fa05044540ef148d161877d3a9510e6dd2aa48a5c3562ee79e5f43f06b5c9ea0"


def _entries(result: object, path: str) -> list[dict[str, object]]:
    return [e for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY] if e["path"] == path]  # type: ignore[attr-defined]


# Path 1: native parent and payload derivation.


@pytest.mark.parametrize("column", _COLUMNS)
def test_admitted_payload_column_is_consumed_from_the_kernel(
    column: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, MarkingKernel())
    python_rows = spy_python_hash(monkeypatch)
    out = run_seq(payload_table(), payload_seeds()).outputs["t"]
    assert_native_consumed(out, column, typed_columns()[1][column])
    assert python_rows == []  # nothing fell back


def test_parent_key_is_consumed_from_the_kernel_and_the_child_inherits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    out = run_seq(payload_table(), payload_seeds()).outputs
    assert_native_consumed(out["t"], "hk", EXPECTED_TOKENS["hk"])
    # Path 2: the child holds the parent's marked value. A re-derivation would have called the
    # kernel again (15 calls, not 14) or produced an unmarked Python token.
    assert out["child"].column("ck").to_pylist() == marked(EXPECTED_TOKENS["hk"])
    assert len(kernel.calls) == 14


def test_inherited_child_is_reported_inherited_with_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    result = run_seq(payload_table(), payload_seeds())
    assert _entries(result, "fk_inherited") == [
        {
            "table": "child",
            "column": "ck",
            "path": "fk_inherited",
            "executed_backend": "inherited",
            "parent": "t.hk",
            "parent_backend": "rust_companion",
        }
    ]
    assert not [e for e in _entries(result, "scalar") if e["column"] == "ck"]


def test_scalar_disposition_names_every_native_column_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    result = run_seq(payload_table(), payload_seeds())
    scalar = _entries(result, "scalar")
    assert [e["column"] for e in scalar] == [*_COLUMNS, "hk"]
    assert all(e["executed_backend"] == "rust_companion" and "reason" not in e for e in scalar)
    assert {e["path"] for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]} == {
        "scalar",
        "fk_inherited",
    }


def test_a_discarded_native_result_fails_the_consumption_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A deliberately broken route: the kernel runs, its result is dropped and the Python
    # handler recomputes equal tokens. A call count and a value comparison both pass it; the
    # marker does not.
    kernel = install(monkeypatch, MarkingKernel())
    real = SeqNativeRun.hash_column

    def discard(self: SeqNativeRun, *args: object, **kwargs: object) -> None:
        real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SeqNativeRun, "hash_column", discard)
    out = run_seq(payload_table(), payload_seeds()).outputs["t"]
    assert len(kernel.calls) == 14
    assert out.column("c_utf8").to_pylist() == typed_columns()[1]["c_utf8"]  # values still equal
    with pytest.raises(AssertionError):
        assert_native_consumed(out, "c_utf8", typed_columns()[1]["c_utf8"])


# Declined columns stay Python, with a value-free reason.


def _declined_table() -> pa.Table:
    return pa.table(
        {
            "tz": pa.array(
                [dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), None], pa.timestamp("us", "UTC")
            ),
            "dec": pa.array([decimal.Decimal("1.5"), None], pa.decimal128(5, 1)),
            "ok": pa.array(["a", "b"]),
        }
    )


def test_a_type_the_kernel_does_not_admit_is_hashed_in_python_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    python_rows = spy_python_hash(monkeypatch)
    seeds = (("tz", column_seed()), ("dec", column_seed()), ("ok", column_seed()))
    result = run_seq(_declined_table(), seeds)
    by_column = {e["column"]: e for e in _entries(result, "scalar")}
    assert by_column["tz"]["executed_backend"] == "python_scalar"
    assert by_column["tz"]["reason"] == "hash_input_type_tz_timestamp_deferred:tz"
    assert by_column["dec"]["reason"] == "hash_input_type_not_native_admitted:dec:decimal128(5, 1)"
    assert by_column["ok"]["executed_backend"] == "rust_companion"
    assert python_rows == [2, 2]  # tz and dec only
    assert len(kernel.calls) == 2  # ok and hk
    out = result.outputs["t"]
    assert_python_consumed(out, "tz", out.column("tz").to_pylist())
    assert_python_consumed(out, "dec", out.column("dec").to_pylist())
    assert out.column("ok").to_pylist() == marked(
        [out.column("ok").to_pylist()[0][len(MARK) :], out.column("ok").to_pylist()[1][len(MARK) :]]
    )


def test_companion_absent_runs_the_python_handler_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_companion(monkeypatch)
    python_rows = spy_python_hash(monkeypatch)
    result = run_seq(payload_table(), payload_seeds())
    assert len(python_rows) == 14
    scalar = _entries(result, "scalar")
    assert {e["executed_backend"] for e in scalar} == {"python_scalar"}
    assert {e["reason"] for e in scalar} == {"crypto_extension_unavailable"}
    assert_python_consumed(result.outputs["t"], "c_utf8", typed_columns()[1]["c_utf8"])


def test_reason_codes_carry_no_cell_or_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, MarkingKernel())
    seeds = (("tz", column_seed()), ("dec", column_seed()), ("ok", column_seed()))
    result = run_seq(_declined_table(), seeds)
    text = repr(result.quality_metrics)
    for needle in ("1.5", "2020", *EXPECTED_TOKENS["hk"][:3]):
        assert needle not in text


# The pandas to Arrow boundary.


def test_noncontiguous_when_selection_lands_on_the_selected_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    payload = pa.table({"a": pa.array(["w", "x", "y", "z"])})
    out = run_seq(payload, (("a", with_when("fk == 1 or fk == 3")),)).outputs["t"]
    assert out.column("a").to_pylist() == [
        "w",
        MARK + "396638be5d87d395d352c03a410a27cfe6531419520e706e69b01fa9f04b1eff",
        "y",
        MARK + "7931cf8b1bb2ea09557876229a27206c8449485be75ba2d2dfae80e42fe8269f",
    ]


def test_when_stitching_with_a_large_string_source_and_nulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    payload = pa.table({"a": pa.array(["w", "x", None, "z", "v"], pa.large_string())})
    out = run_seq(payload, (("a", with_when("fk >= 1 and fk <= 3")),)).outputs["t"]
    # The kernel sees the pinned large_string type, not pandas' inferred string.
    assert kernel.calls[0].arrow_type == pa.large_string()
    assert out.column("a").type == pa.string()
    assert out.column("a").to_pylist() == [
        "w",
        MARK + "396638be5d87d395d352c03a410a27cfe6531419520e706e69b01fa9f04b1eff",
        None,
        MARK + "7931cf8b1bb2ea09557876229a27206c8449485be75ba2d2dfae80e42fe8269f",
        "v",
    ]


def test_zero_match_when_never_calls_the_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    payload = pa.table({"a": pa.array(["w", "x"])})
    out = run_seq(payload, (("a", with_when("fk > 100")),)).outputs["t"]
    assert out.column("a").to_pylist() == ["w", "x"]
    assert len(kernel.calls) == 1  # only the parent key `hk`


def test_all_null_string_column_hashes_to_all_null_with_the_frozen_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    nulls = pa.table(
        {"a": pa.array([None] * 3, pa.string()), "c": pa.array([None] * 3, pa.bool_())}
    )
    out = run_seq(nulls, (("a", column_seed()), ("c", column_seed()))).outputs["t"]
    assert [c.arrow_type for c in kernel.calls[:2]] == [pa.string(), pa.bool_()]
    assert out.schema.field("a").type == pa.null() and out.schema.field("c").type == pa.null()
    assert out.column("a").to_pylist() == [None] * 3


def test_empty_table_keeps_the_handler_dtype_and_skips_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    empty = pa.table({"a": pa.array([], pa.string()), "i": pa.array([], pa.int64())})
    result = run_seq(empty, (("a", column_seed()), ("i", column_seed())))
    assert [str(f.type) for f in result.outputs["t"].schema] == [
        "double",
        "double",
        "int64",
        "double",
    ]
    assert kernel.calls == []
    assert {e["executed_backend"] for e in _entries(result, "scalar")} == {"none"}


@pytest.mark.parametrize(
    ("values", "arrow_type"),
    [([1, 2, 3, 4], pa.int32()), ([True, False, True, False], pa.bool_())],
)
def test_partial_when_on_a_non_text_column_still_fails_on_conversion(
    values: list[object], arrow_type: pa.DataType, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, MarkingKernel())
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError)):
        run_seq(pa.table({"x": pa.array(values, arrow_type)}), (("x", with_when("fk == 1")),))


def test_a_column_that_stops_converting_to_its_pinned_type_aborts_without_leaking_the_cell() -> (
    None
):
    run = SeqNativeRun()
    run._bound = _sequential_native.BoundHashKernel(MarkingKernel())  # type: ignore[arg-type]
    run._loaded = True
    from decoy_engine.execution._adapter import StrategyContext

    decision = _sequential_native.ColumnDecision("t", "x", "rust_companion", None, pa.int64())
    run._decisions[("t", "x")] = decision
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", "t")
    object.__setattr__(ctx, "nested_outer_column", "")
    df = pd.DataFrame({"x": ["secret-cell-value"]})
    with pytest.raises(ExecutionError) as exc:
        run.hash_column(ctx, df, "x", namespace="ns", mask_key=b"k" * 8, truncate=None)
    assert exc.value.code == "sequential_hash_input_type_drift"
    assert "secret-cell-value" not in str(exc.value)
    assert exc.value.__context__ is None


def test_expected_pandas_representation_changes_are_not_drift() -> None:
    # large_string and an all-null column come back from pandas as str and None objects.
    for arrow_type, values in (
        (pa.large_string(), ["a", None]),
        (pa.string(), [None, None]),
        (pa.bool_(), [True, None]),
    ):
        pinned = pa.array(values, arrow_type)
        series = pd.Series(pinned.to_pandas().tolist(), dtype="object")
        rebuilt = SeqNativeRun._pinned_array(
            series, _sequential_native.ColumnDecision("t", "x", "rust_companion", None, arrow_type)
        )
        assert rebuilt.type == arrow_type and rebuilt.to_pylist() == values


# Path 3: REMAP.


def test_remap_mints_orphans_through_the_native_parent_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    result = run_seq(payload_table(), payload_seeds(), policy=OrphanPolicy.REMAP, sources=srcs)
    ck = result.outputs["child"].column("ck").to_pylist()
    assert ck[:-2] == marked(EXPECTED_TOKENS["hk"])
    assert ck[-2:] == marked(_ORPHAN_TOKENS)
    assert [c.rows for c in kernel.calls] == [10] * 14 + [2]
    assert _entries(result, "fk_remap") == [
        {"table": "t", "column": "hk", "path": "fk_remap", "executed_backend": "rust_companion"}
    ]


def test_remap_input_type_is_the_orphan_keys_not_the_parent_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    srcs = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = srcs["t"]
    srcs["t"] = t.set_column(
        t.schema.get_field_index("hk"), "hk", pa.array([0, 1, 2, 3], pa.int8())
    )
    srcs["child"] = pa.table({"ck": pa.array([0, None, 2, 300], pa.int64())})
    out = run_seq(None, (("a", column_seed()),), policy=OrphanPolicy.REMAP, sources=srcs).outputs
    assert out["child"].column("ck").to_pylist() == [
        MARK + _INT_KEY_TOKENS[0],
        None,
        MARK + _INT_KEY_TOKENS[2],
        MARK + _INT_REMAP_300,
    ]
    assert [c.arrow_type for c in kernel.calls] == [pa.string(), pa.int8(), pa.int64()]


def test_remap_stays_python_when_the_parent_column_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_companion(monkeypatch)
    python_rows = spy_python_hash(monkeypatch)
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    result = run_seq(payload_table(), payload_seeds(), policy=OrphanPolicy.REMAP, sources=srcs)
    assert python_rows == [10] * 14 + [2]
    assert result.outputs["child"].column("ck").to_pylist()[-2:] == _ORPHAN_TOKENS
    (entry,) = _entries(result, "fk_remap")
    assert entry["executed_backend"] == "python_scalar"
    assert entry["reason"] == "hash_remap_parent_stays_python:hk"


def test_remap_declines_a_mixed_type_orphan_batch_with_a_reason() -> None:
    run = SeqNativeRun()
    run._bound = _sequential_native.BoundHashKernel(MarkingKernel())  # type: ignore[arg-type]
    run._loaded = True
    run._decisions[("t", "hk")] = _sequential_native.ColumnDecision(
        "t", "hk", "rust_companion", None, pa.string()
    )
    mixed = pd.Series(["a", 7], dtype="object")
    tokens = run._remap_tokens("t", "hk", mixed, "ns", b"k" * 8, None)
    assert tokens is None
    (entry,) = [r.entry() for r in run._remap.values()]
    assert entry["executed_backend"] == "python_scalar"
    assert entry["reason"] == "hash_remap_input_not_arrow:hk"


def test_remap_calls_on_two_child_tables_can_mix_backends() -> None:
    record = _sequential_native.RemapRecord("t", "hk", native_rows=3, python_rows=1)
    assert record.entry()["executed_backend"] == "mixed"


# The existing ingest guards run first and are unchanged.


def test_nullable_integer_payload_is_rejected_before_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    nullable = pa.table({"c_i8": pa.array([-128, None, 127], pa.int8())})
    with pytest.raises(ExecutionError) as exc:
        run_seq(nullable, (("c_i8", column_seed()),))
    assert exc.value.code == "null_bearing_int_unsupported"
    assert kernel.calls == []


def test_nullable_integer_parent_key_is_rejected_before_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    srcs = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = srcs["t"]
    srcs["t"] = t.set_column(
        t.schema.get_field_index("hk"), "hk", pa.array([0, None, 2, 3], pa.int64())
    )
    with pytest.raises(ExecutionError) as exc:
        run_seq(None, (("a", column_seed()),), sources=srcs)
    assert exc.value.code == "null_bearing_int_unsupported"
    assert kernel.calls == []


def test_nullable_child_still_inherits_and_keeps_its_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    srcs = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = srcs["t"]
    srcs["t"] = t.set_column(
        t.schema.get_field_index("hk"), "hk", pa.array([0, 1, 2, 3], pa.int64())
    )
    srcs["child"] = pa.table({"ck": pa.array([0, None, 2, 3], pa.int64())})
    out = run_seq(None, (("a", column_seed()),), sources=srcs).outputs
    assert out["child"].column("ck").to_pylist() == [
        MARK + _INT_KEY_TOKENS[0],
        None,
        MARK + _INT_KEY_TOKENS[2],
        MARK + _INT_KEY_TOKENS[3],
    ]


# Failure behavior is scoped to the sink; a native fault is never retried in Python.


def test_kernel_fault_aborts_a_transactional_sink_with_nothing_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Call 15 is the REMAP mint on the child table: `up` and `t` are already staged.
    kernel = install(monkeypatch, MarkingKernel(fail_on_call=15))
    python_rows = spy_python_hash(monkeypatch)
    sink = RecordingSink()
    srcs = sources_with_orphans(payload_table(), ["orph1"])
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(
            payload_table(), payload_seeds(), policy=OrphanPolicy.REMAP, sources=srcs, sink=sink
        )
    assert len(kernel.calls) == 15
    assert set(sink.staged) == set() and sink.committed == {}  # abort cleared the staging
    assert (sink.commits, sink.aborts) == (0, 1)
    assert python_rows == []  # no Python retry after the native selection


def test_kernel_fault_mid_table_is_not_retried_in_python(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install(monkeypatch, MarkingKernel(fail_on_call=3))
    python_rows = spy_python_hash(monkeypatch)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(payload_table(), payload_seeds())
    assert len(kernel.calls) == 3
    assert python_rows == []


def test_callable_sink_keeps_earlier_tables_after_a_kernel_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel(fail_on_call=15))
    delivered: dict[str, pa.Table] = {}
    srcs = sources_with_orphans(payload_table(), ["orph1"])
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(
            payload_table(),
            payload_seeds(),
            policy=OrphanPolicy.REMAP,
            sources=srcs,
            sink=callable_sink(delivered),
        )
    # A callable has no commit or abort, so the tables written before the fault stay delivered.
    assert set(delivered) == {"up", "t"}
    assert_native_consumed(delivered["t"], "c_utf8", typed_columns()[1]["c_utf8"])


def test_transactional_sink_commits_the_native_tables_once(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, MarkingKernel())
    sink = RecordingSink()
    run_seq(payload_table(), payload_seeds(), sink=sink)
    assert (sink.commits, sink.aborts) == (1, 0)
    assert_native_consumed(sink.committed["t"], "c_utf8", typed_columns()[1]["c_utf8"])


# Scope: SEQ only, hash only, nothing else moves.


def test_the_full_frame_route_never_reaches_the_sequential_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    out = run_full(payload_table(), payload_seeds()).outputs["t"]
    assert kernel.calls == []
    assert_python_consumed(out, "c_utf8", typed_columns()[1]["c_utf8"])


def test_a_plan_without_hash_columns_loads_no_kernel_and_adds_no_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.parity._d2a_seq_data import run_plain

    loads: list[int] = []
    monkeypatch.setattr(
        _sequential_native,
        "load_compiled_crypto_kernel",
        lambda: loads.append(1) or MarkingKernel(),
    )
    result = run_plain()
    assert loads == []
    assert SEQ_HASH_DISPOSITION_KEY not in result.quality_metrics


def test_the_sequential_route_reuses_the_out_of_core_kernel_entry_unchanged() -> None:
    from decoy_engine.execution.out_of_core import _hash_native

    assert _sequential_native.native_hash_array is _hash_native.native_hash_array
    assert _sequential_native.decline_reason is _hash_native.decline_reason
    assert _sequential_native.BoundHashKernel is _hash_native.BoundHashKernel


def test_no_cell_or_key_value_reaches_the_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, MarkingKernel())
    srcs = sources_with_orphans(payload_table(), ["orph1"])
    with caplog.at_level(logging.DEBUG):
        run_seq(payload_table(), payload_seeds(), policy=OrphanPolicy.REMAP, sources=srcs)
    text = caplog.text
    for needle in ("orph1", "alice", *EXPECTED_TOKENS["hk"][:2]):
        assert needle not in text


def test_adapter_instances_do_not_share_native_state(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, MarkingKernel())
    a, b = PandasExecutionAdapter(), PandasExecutionAdapter()
    ra = run_seq(payload_table(), payload_seeds(), adapter=a)
    rb = run_seq(payload_table(), payload_seeds(), adapter=b)
    assert ra.quality_metrics == rb.quality_metrics
