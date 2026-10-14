"""Phase D / D2c-iv: the SEQ date_shift routing, proven with stand-in index kernels. No
companion is needed.

Two boundaries are kept apart. At the HANDLER seam the native path produces the same output
frame and the same appended `format_error` RowErrors as the Python handler. The PIPELINE
(`run_sequential`) then raises on an uncovered error or quarantines a covered one, and it does so
identically for either route. Error-bearing cases therefore compare native-SEQ with Python-SEQ
(the same run with the index companion hidden), not with a different enforcement boundary.
Real-kernel parity lives in `test_phase_d_d2c_iv_date_shift_real.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _sequential_native
from decoy_engine.execution._sequential_native import (
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    SeqNativeRun,
)
from decoy_engine.execution._sequential_native_ops import (
    SEQ_BUCKET_DISPOSITION_KEY,
    SEQ_CATEGORICAL_DISPOSITION_KEY,
    SEQ_DATE_SHIFT_DISPOSITION_KEY,
    date_shift_op,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import JOB_SEED, column_seed
from tests.parity._d1c_data import operator_config, run_oracle
from tests.parity._d2a_seq_data import RecordingSink
from tests.parity._d2b_shapes import (
    Shape,
    edge,
    graph,
    hashed,
    make_plan,
    run_full,
    run_seq,
    strs,
    table_seed,
)
from tests.parity._d2c_bucket_data import bucket_seed
from tests.parity._d2c_cat_data import SRC, cat_seed
from tests.parity._d2c_date_shift_data import (
    ADMITTED,
    DS_NS,
    FORMAT_ERROR_REASON_LITERAL,
    ISO,
    MIXED,
    MIXED_ERROR_ROWS,
    PARSEABLE,
    DateShiftMarkingKernel,
    ReferenceDateShiftKernel,
    date_payload_shape,
    date_shift_seed,
    expect_shift,
    hide_date_shift,
    install_date_shift,
    marked_days,
    run_seq_quarantine,
)
from tests.parity._d2c_fpe_data import FpeMarkingKernel, fpe_seed, install_fpe
from tests.parity._fpe_key_data import assert_keys_declined
from tests.parity._seq_witness import MarkingKernel
from tests.parity._seq_witness import install as install_hash

NATIVE = "rust_companion"
PYTHON = "python_scalar"
NO_CALLS = "no_native_date_shift_calls"


def _entries(result: Any) -> list[dict[str, Any]]:
    return result.quality_metrics[SEQ_DATE_SHIFT_DISPOSITION_KEY]


def _preserve(*dates: str) -> tuple[tuple[str, Any], ...]:
    return (("preserve_values", list(dates)),)


def _flag_kwargs(flags: list[int], column: str = "d") -> dict[str, Any]:
    return {
        "extra_cols": {"flag": pa.array(flags, pa.int64())},
        "extra_seeds": {"flag": column_seed("passthrough", namespace=None)},
        "when": {column: "flag == 1"},
    }


def _error_shape(cells: list[str | None], seed: Any = None, **kwargs: Any) -> Shape:
    return date_payload_shape({"d": (strs(cells), seed or date_shift_seed())}, **kwargs)


def _both_routes(
    monkeypatch: pytest.MonkeyPatch, shape: Shape, tmp_path: Path
) -> tuple[Any, Any, Any]:
    """Native-SEQ (reference kernel) and Python-SEQ under the same quarantine config."""
    kernel = install_date_shift(monkeypatch, ReferenceDateShiftKernel())
    native = run_seq_quarantine(shape, tmp_path / "native.jsonl")
    hide_date_shift(monkeypatch)
    python = run_seq_quarantine(shape, tmp_path / "python.jsonl")
    return native, python, kernel


def _sidecar(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _assert_same_outcome(native: Any, python: Any, tmp_path: Path) -> None:
    assert native.row_errors == python.row_errors
    for table in python.outputs:
        assert native.outputs[table].equals(python.outputs[table]), table
    assert native.quality_metrics.get("date_preserve") == python.quality_metrics.get(
        "date_preserve"
    )
    # The sidecar is only written when some row was quarantined.
    paths = [tmp_path / "native.jsonl", tmp_path / "python.jsonl"]
    assert [p.exists() for p in paths] == [bool(python.row_errors)] * 2
    if python.row_errors:
        assert _sidecar(paths[0]) == _sidecar(paths[1])


# 1-2. Native witness (consumption).


@pytest.mark.parametrize("column", ADMITTED)
def test_admitted_date_shift_column_is_consumed_from_the_kernel(
    column: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = date_payload_shape()
    source = shape.sources["P"].column(column).to_pylist()
    oracle = run_full(shape).outputs["P"].column(column).to_pylist()
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    out = run_seq(shape).outputs["P"]
    days, fmt = marked_days(column)
    expected = expect_shift(source, days, fmt)
    assert out.schema.field(column).type == pa.string()
    assert out.column(column).to_pylist() == expected
    assert expected != oracle  # the marker is distinguishable from the Python keyed pick
    assert kernel.calls


def test_a_discarded_native_result_fails_the_consumption_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    real = SeqNativeRun.date_shift_column

    def discard(self: SeqNativeRun, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)

    monkeypatch.setattr(SeqNativeRun, "date_shift_column", discard)
    shape = date_payload_shape()
    out = run_seq(shape).outputs["P"]
    assert kernel.calls
    assert (
        out.column("s_wide").to_pylist()
        == run_full(shape).outputs["P"].column("s_wide").to_pylist()
    )


def test_the_kernel_receives_the_handlers_namespace_and_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    run_seq(date_payload_shape({"d": (strs(PARSEABLE[:1] * 3), date_shift_seed(-5, 10))}))
    [call] = [c for c in kernel.calls if c.namespace == DS_NS]
    assert call.pool_size == 16 and call.arrow_type == pa.string() and call.rows == 3


def test_disposition_reports_every_native_date_shift_column_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_date_shift(monkeypatch, DateShiftMarkingKernel())
    entries = _entries(run_seq(date_payload_shape()))
    assert [e["column"] for e in entries] == ADMITTED
    assert all(
        e["table"] == "P"
        and e["path"] == "scalar"
        and e["executed_backend"] == NATIVE
        and "reason" not in e
        and "preserved_rows" not in e
        for e in entries
    )


# 3. Admission reasons, straight from the descriptor.


def test_admission_reasons_are_coded_and_value_free() -> None:
    admit = date_shift_op(lambda _t: None).admit
    string = pa.string()
    assert admit("c", string, date_shift_seed()) is None
    assert admit("c", string, date_shift_seed(namespace=None)) == "date_shift_requires_namespace:c"
    assert (
        admit("c", string, date_shift_seed(extra=(("group_by", "g"),)))
        == "date_shift_group_by_not_native:c"
    )
    assert admit("c", string, date_shift_seed(date_format=None)) == (
        "date_shift_requires_date_format:c"
    )
    assert admit("c", string, date_shift_seed(date_format="mixed")) == (
        "date_shift_special_date_format:c"
    )
    assert admit("c", string, date_shift_seed(date_format="%Y-%m-%d %z")) == (
        "date_shift_timezone_directive:c"
    )
    assert admit("c", string, date_shift_seed(extra=_preserve("not-a-date"))) == (
        "date_shift_preserve_values_invalid:c"
    )
    assert admit("c", string, date_shift_seed(min_days="3")) == "date_shift_min_days_not_int:c"  # type: ignore[arg-type]
    assert admit("c", string, date_shift_seed(max_days=2.5)) == "date_shift_max_days_not_int:c"  # type: ignore[arg-type]
    assert admit("c", string, date_shift_seed(min_days=-200_000)) == (
        "date_shift_min_days_out_of_range:c"
    )
    assert admit("c", string, date_shift_seed(max_days=200_000)) == (
        "date_shift_max_days_out_of_range:c"
    )
    assert admit("c", pa.large_string(), date_shift_seed()) == (
        "date_shift_source_not_string:c:large_string"
    )
    assert admit("c", pa.date32(), date_shift_seed()) == (
        "date_shift_source_not_string:c:date32[day]"
    )


# 3b. Fallback behavior, separate from admission.


def test_a_non_native_but_valid_config_runs_python_and_the_run_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    cells = strs(["2024-03-05 +0000", None, "2023-07-19 +0000"])
    shape = date_payload_shape({"d": (cells, date_shift_seed(date_format="%Y-%m-%d %z"))})
    result = run_seq(shape)
    [entry] = _entries(result)
    assert entry["executed_backend"] == PYTHON
    assert entry["reason"] == "date_shift_timezone_directive:d"
    assert kernel.calls == []
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


@pytest.mark.parametrize(
    ("seed", "code"),
    [
        (date_shift_seed(namespace=None), "date_shift_requires_namespace"),
        (date_shift_seed(extra=_preserve("not-a-date")), "date_shift_invalid_config"),
    ],
    ids=["no_namespace", "invalid_preserve"],
)
def test_a_config_the_handler_rejects_raises_the_same_error_with_and_without_the_seam(
    seed: Any, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = date_payload_shape({"d": (strs(PARSEABLE), seed)})
    install_date_shift(monkeypatch, DateShiftMarkingKernel())
    with pytest.raises(Exception) as native:
        run_seq(shape)
    hide_date_shift(monkeypatch)
    with pytest.raises(Exception) as python:
        run_seq(shape)
    assert type(native.value) is type(python.value)
    assert native.value.code == python.value.code == code  # type: ignore[attr-defined]
    assert str(native.value) == str(python.value)


def test_a_bound_the_handler_cannot_coerce_raises_the_same_error_with_and_without_the_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = date_payload_shape({"d": (strs(PARSEABLE), date_shift_seed(min_days="soon"))})  # type: ignore[arg-type]
    install_date_shift(monkeypatch, DateShiftMarkingKernel())
    with pytest.raises(ValueError) as native:
        run_seq(shape)
    hide_date_shift(monkeypatch)
    with pytest.raises(ValueError) as python:
        run_seq(shape)
    assert str(native.value) == str(python.value)


# 4-5. Row-error parity at the handler seam, and the pipeline policy on top of it.


def test_format_errors_match_python_seq_and_the_pipeline_quarantines_them_identically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shape = _error_shape(MIXED)
    native, python, kernel = _both_routes(monkeypatch, shape, tmp_path)
    assert [c.rows for c in kernel.calls] == [3]  # the kernel only ever saw parseable rows
    _assert_same_outcome(native, python, tmp_path)
    errors = [(e.table, e.column, e.row_index, e.trigger, e.reason) for e in native.row_errors]
    assert errors == [
        ("P", "d", i, "format_error", FORMAT_ERROR_REASON_LITERAL) for i in MIXED_ERROR_ROWS
    ]
    out = native.outputs["P"]
    assert out.num_rows == len(MIXED) - len(MIXED_ERROR_ROWS)
    assert None in out.column("d").to_pylist()  # the null row is kept and never reported
    assert "junk" not in out.column("d").to_pylist()
    assert [r["d"] for r in _sidecar(tmp_path / "native.jsonl")] == ["junk", "", "2024-13-45"]


def test_without_a_quarantine_trigger_both_routes_raise_and_the_sink_commits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _error_shape(MIXED)
    install_date_shift(monkeypatch, ReferenceDateShiftKernel())
    native_sink = RecordingSink()
    with pytest.raises(RowErrorsFailedError) as native:
        run_seq(shape, sink=native_sink)
    hide_date_shift(monkeypatch)
    python_sink = RecordingSink()
    with pytest.raises(RowErrorsFailedError) as python:
        run_seq(shape, sink=python_sink)
    assert native.value.records == python.value.records
    assert [r.row_index for r in native.value.records] == MIXED_ERROR_ROWS
    for sink in (native_sink, python_sink):
        assert sink.commits == 0 and sink.committed == {} and sink.aborts == 1


# 6. A kernel fault aborts regardless of quarantine.


def test_a_kernel_fault_aborts_even_under_a_covering_quarantine_with_no_python_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel(fail_on_call=1))
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq_quarantine(_error_shape(MIXED), tmp_path / "q.jsonl", sink=sink)
    assert len(kernel.calls) == 1
    assert sink.commits == 0 and sink.committed == {}
    assert not (tmp_path / "q.jsonl").exists()


def test_a_kernel_fault_aborts_without_a_quarantine_too(monkeypatch: pytest.MonkeyPatch) -> None:
    install_date_shift(monkeypatch, DateShiftMarkingKernel(fail_on_call=1))
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(date_payload_shape(), sink=sink)
    assert sink.commits == 0 and sink.committed == {}


# 7. preserve_values and the position rebasing.


def test_an_unparseable_row_after_a_preserved_row_keeps_its_full_frame_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = "2000-01-01"
    cells = [sentinel, "2024-03-05", "junk", sentinel, None, "bad", "2023-07-19"]
    shape = _error_shape(cells, date_shift_seed(extra=_preserve(sentinel)))
    native, python, kernel = _both_routes(monkeypatch, shape, tmp_path)
    _assert_same_outcome(native, python, tmp_path)
    assert [e.row_index for e in native.row_errors] == [2, 5]
    assert sum(c.rows for c in kernel.calls) == 2
    out = native.outputs["P"].column("d").to_pylist()
    # Rows 2 and 5 were quarantined away; the sentinels (source rows 0 and 3) sit at 0 and 2.
    assert out[0] == sentinel and out[2] == sentinel and out[1] != "2024-03-05"
    assert native.quality_metrics["date_preserve"][0]["preserved_rows"] == 2
    [entry] = _entries(native)
    assert entry["executed_backend"] == NATIVE and entry["preserved_rows"] == 2


def test_an_all_preserved_column_makes_no_kernel_call_and_records_the_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cells: list[str | None] = ["2024-03-05"] * 5
    shape = _error_shape(cells, date_shift_seed(extra=_preserve("2024-03-05")))
    native, python, kernel = _both_routes(monkeypatch, shape, tmp_path)
    _assert_same_outcome(native, python, tmp_path)
    [entry] = _entries(native)
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == NO_CALLS
    assert entry["preserved_rows"] == 5
    assert native.outputs["P"].column("d").to_pylist() == cells


def test_a_preserve_list_that_matches_nothing_records_a_zero_count_like_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cells = ["2024-03-05", None, "2023-07-19", "junk"]
    shape = _error_shape(cells, date_shift_seed(extra=_preserve("2000-01-01")))
    native, python, _kernel = _both_routes(monkeypatch, shape, tmp_path)
    _assert_same_outcome(native, python, tmp_path)
    [entry] = _entries(native)
    assert entry["preserved_rows"] == 0
    assert "date_preserve" not in native.quality_metrics  # Python records no zero count


# 8. FK key boundary: a date_shift key column stays on Python.


def _date_key_shape(policy: OrphanPolicy, *, hash_payload: bool = False) -> Shape:
    keys_a = ["2024-03-05", "2023-07-19"]
    keys_b = [*keys_a, "2022-01-10"]
    plan = make_plan(
        {
            "A": table_seed(
                {
                    "k": date_shift_seed(namespace="ns_ab"),
                    **({"h": hashed("ns_h")} if hash_payload else {}),
                }
            ),
            "B": table_seed({"k": date_shift_seed(namespace="ns_ab")}),
        }
    )
    sources = {"A": pa.table({"k": strs(keys_a)}), "B": pa.table({"k": strs(keys_b)})}
    if hash_payload:
        sources["A"] = pa.table({"k": strs(keys_a), "h": strs(["u", "v"])})
    return Shape(plan, graph(edge("A", "k", "B", "k", "ns_ab", policy)), sources)


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_date_shift_key_columns_stay_python_with_an_explicit_decline_reason(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    hash_kernel = install_hash(monkeypatch, MarkingKernel())
    shape = _date_key_shape(policy)
    result, oracle = run_seq(shape), run_full(shape)
    for table in ("A", "B"):
        assert result.outputs[table].equals(oracle.outputs[table]), table
    assert kernel.calls == [] and hash_kernel.calls == []
    # Stays Python and reports why, instead of staying silent.
    for key in (
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        SEQ_BUCKET_DISPOSITION_KEY,
    ):
        assert key not in result.quality_metrics
    assert_keys_declined(result, shape, "date_shift", SEQ_DATE_SHIFT_DISPOSITION_KEY)


def test_a_date_shift_key_does_not_disturb_a_native_hash_payload_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_date_shift(monkeypatch, DateShiftMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    result = run_seq(_date_key_shape(OrphanPolicy.REMAP, hash_payload=True))
    assert [e["column"] for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]] == ["h"]
    assert {e["path"] for e in result.quality_metrics[SEQ_DATE_SHIFT_DISPOSITION_KEY]} == {
        "fk_key_declined"
    }


def test_a_date_shift_call_inside_a_remap_scope_returns_to_python_untouched() -> None:
    from decoy_engine.execution._adapter import StrategyContext

    run = SeqNativeRun()
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", "t")
    object.__setattr__(ctx, "nested_outer_column", "")
    decision = _sequential_native.ColumnDecision(
        "t", "x", NATIVE, None, pa.string(), strategy="date_shift"
    )
    run._decisions[("t", "x")] = decision
    run._bound["date_shift"] = object()
    df = pd.DataFrame({"x": ["2024-03-05"]})
    kwargs: dict[str, Any] = {
        "namespace": "n",
        "mask_key": b"k",
        "min_days": -1,
        "max_days": 1,
        "date_format": ISO,
        "preserve": None,
    }
    with run.remap_scope():
        assert run.date_shift_column(ctx, df, "x", **kwargs) is None
    assert decision.rows == 0
    decision.strategy = "hash"  # another strategy's decision is not this column's to run
    assert run.date_shift_column(ctx, df, "x", **kwargs) is None


# 9. Evidence reflects the compiled calls that actually ran.


def test_an_all_null_column_makes_no_kernel_call_and_reports_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    shape = _error_shape([None] * 6)
    result = run_seq(shape)
    [entry] = _entries(result)
    assert kernel.calls == [] and result.row_errors == ()
    assert entry["executed_backend"] == "none" and entry["reason"] == NO_CALLS
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_an_all_unparseable_column_reports_none_and_still_reports_its_row_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cells = ["junk", "", "2024-13-45", None, "x" * 20]
    native, python, kernel = _both_routes(monkeypatch, _error_shape(cells), tmp_path)
    _assert_same_outcome(native, python, tmp_path)
    [entry] = _entries(native)
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == NO_CALLS
    assert [e.row_index for e in native.row_errors] == [0, 1, 2, 4]


def test_a_single_parseable_row_is_enough_to_claim_the_kernel_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    result = run_seq_quarantine(
        _error_shape([None, "junk", "2024-03-05", ""]), tmp_path / "q.jsonl"
    )
    [entry] = _entries(result)
    assert [c.rows for c in kernel.calls] == [1]
    assert entry["executed_backend"] == NATIVE and "reason" not in entry


def test_an_empty_selection_records_a_no_rows_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    shape = _error_shape(PARSEABLE, **_flag_kwargs([0] * len(PARSEABLE)))
    result = run_seq(shape)
    [entry] = _entries(result)
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == "no_rows_hashed"
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


# 10. A when subset.


def test_a_when_subset_shifts_only_the_selected_rows_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    cells = ["2024-03-05", "2023-07-19", None, "2021-11-14", "2024-02-29", "2022-02-02"]
    shape = _error_shape(cells, **_flag_kwargs([1, 0, 0, 1, 0, 1]))
    out = run_seq(shape).outputs["P"].column("d").to_pylist()
    assert sum(c.rows for c in kernel.calls) == 3
    selected = expect_shift([cells[0], cells[3], cells[5]], 30, ISO)
    assert out == [selected[0], cells[1], None, selected[1], cells[4], selected[2]]


def test_when_subset_row_errors_carry_table_positions_like_python_seq(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cells = ["2024-03-05", "junk", "2023-07-19", "bad", None, "2021-11-14", "worse", "2022-02-02"]
    flags = [1, 0, 0, 1, 1, 0, 1, 1]
    shape = _error_shape(cells, **_flag_kwargs(flags))
    native, python, kernel = _both_routes(monkeypatch, shape, tmp_path)
    _assert_same_outcome(native, python, tmp_path)
    # Row 1 is unparseable but unselected, so the shift never looks at it.
    assert [e.row_index for e in native.row_errors] == [3, 6]
    assert sum(c.rows for c in kernel.calls) == 2


# 11. Isolation from the other native strategies, and byte-identical fragments.


def _mixed_shape(order: list[str]) -> Shape:
    parts = {
        "h": (strs(["u0", "u1", None, "u3", "u4", "u5", "u6"]), column_seed(namespace="ns_h")),
        "f": (strs(["1234567890"] * 7), fpe_seed()),
        "c": (strs(SRC), cat_seed()),
        "b": (
            strs(["2024-03-05", None, "junk", "2021-11-14", "2020-02-29", "", "2023-07-19"]),
            bucket_seed("month"),
        ),
        "d": (strs(PARSEABLE), date_shift_seed()),
    }
    return date_payload_shape({name: parts[name] for name in order})


@pytest.mark.parametrize(
    "order",
    [
        ["h", "f", "c", "b", "d"],
        ["d", "b", "c", "f", "h"],
        ["c", "d", "h", "b", "f"],
        ["f", "h", "d", "c", "b"],
    ],
)
def test_five_native_strategies_mask_correctly_in_any_column_order(
    order: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Covers hash/fpe/categorical/bucket_perturb/date_shift together; faker, the 6th native
    # strategy, gets the equivalent mixed-job column-isolation coverage in test_phase_d_faker_seq.
    shape = _mixed_shape(order)
    oracle = run_full(shape).outputs["P"]
    # One index loader serves categorical, bucket_perturb and date_shift, so one reference
    # kernel (which matches the Python draw for all three) stands in for it.
    index = install_date_shift(monkeypatch, ReferenceDateShiftKernel())
    install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    result = run_seq(shape)
    out = result.outputs["P"]
    for name in ("b", "c", "d"):
        assert out.column(name).to_pylist() == oracle.column(name).to_pylist(), name
    assert any(c.namespace == DS_NS for c in index.calls)
    assert all(v is None or v.startswith("native:") for v in out.column("h").to_pylist())
    assert all(v is None or v.startswith("native:") for v in out.column("f").to_pylist())
    assert set(result.quality_metrics) == {
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        SEQ_BUCKET_DISPOSITION_KEY,
        SEQ_DATE_SHIFT_DISPOSITION_KEY,
    }


def test_the_other_fragments_are_byte_identical_with_and_without_date_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_date_shift(monkeypatch, ReferenceDateShiftKernel())
    install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    with_date = run_seq(_mixed_shape(["h", "f", "c", "b", "d"])).quality_metrics
    alone = run_seq(_mixed_shape(["h", "f", "c", "b"])).quality_metrics
    for key in (
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        SEQ_BUCKET_DISPOSITION_KEY,
    ):
        assert repr(with_date[key]) == repr(alone[key])
    assert SEQ_DATE_SHIFT_DISPOSITION_KEY in with_date
    assert SEQ_DATE_SHIFT_DISPOSITION_KEY not in alone


def test_the_date_shift_fragment_keeps_its_literal_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # Surface pin written as literals, never computed from the output under test.
    install_date_shift(monkeypatch, ReferenceDateShiftKernel())
    cells = ["2024-03-05", "2023-07-19", "2023-07-19"]
    seed = date_shift_seed(extra=_preserve("2023-07-19"))
    metrics = run_seq(date_payload_shape({"d": (strs(cells), seed)})).quality_metrics
    assert metrics[SEQ_DATE_SHIFT_DISPOSITION_KEY] == [
        {
            "table": "P",
            "column": "d",
            "path": "scalar",
            "executed_backend": NATIVE,
            "preserved_rows": 2,
        }
    ]


# 12-13. Registry and the absent key.


def test_the_registry_holds_the_seven_native_strategies() -> None:
    assert set(_sequential_native._OPS) == {
        "hash",
        "fpe",
        "categorical",
        "bucket_perturb",
        "date_shift",
        "faker",
        "text_redact",
    }


def test_the_unused_apply_slot_raises_if_routing_is_ever_bypassed() -> None:
    op = _sequential_native._OPS["date_shift"]
    with pytest.raises(AssertionError, match="date_shift uses date_shift_column"):
        op.apply(object(), pa.array([], pa.string()), None)
    assert op.fail_closed is None and op.warn is None


def test_a_job_without_date_shift_has_no_date_shift_disposition_key() -> None:
    shape = date_payload_shape({"p": (strs(SRC), column_seed("passthrough", namespace=None))})
    assert SEQ_DATE_SHIFT_DISPOSITION_KEY not in run_seq(shape).quality_metrics


# 14-15. Companion absence and group_by.


def test_companion_absent_runs_the_python_handler_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_date_shift(monkeypatch)
    shape = date_payload_shape()
    result = run_seq(shape)
    entries = _entries(result)
    assert {e["executed_backend"] for e in entries} == {PYTHON}
    assert {e["reason"] for e in entries} == {"crypto_extension_unavailable"}
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_a_group_by_column_stays_on_python_and_runs_the_snapshot_anchor_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    seed = date_shift_seed(extra=(("group_by", "g"),))
    shape = date_payload_shape(
        {"d": (strs(PARSEABLE), seed)},
        extra_cols={"g": strs(["a", "a", "b", "b", "a", "c", "c"])},
        extra_seeds={"g": column_seed("passthrough", namespace=None)},
    )
    result = run_seq(shape)
    [entry] = _entries(result)
    assert kernel.calls == []
    assert entry["executed_backend"] == PYTHON  # declined, not "none": the seam books the row
    assert entry["reason"] == "date_shift_group_by_not_native:d"
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


# 16. The thread budget.


def test_the_thread_budget_reaches_the_index_kernel_through_a_date_shift_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution._adapter import StrategyContext

    kernel = install_date_shift(monkeypatch, DateShiftMarkingKernel())
    run = SeqNativeRun(native_threads=3)
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", "t")
    object.__setattr__(ctx, "nested_outer_column", "")
    object.__setattr__(ctx, "row_errors", [])
    run._decisions[("t", "x")] = run._decide(
        _sequential_native._OPS["date_shift"], "t", "x", pa.string(), date_shift_seed()
    )
    assert run._bound["date_shift"].native_threads == 3
    out = run.date_shift_column(
        ctx,
        pd.DataFrame({"x": ["2024-03-05", "2024-02-10"]}),
        "x",
        namespace="n",
        mask_key=JOB_SEED,
        min_days=-30,
        max_days=30,
        date_format=ISO,
        preserve=None,
    )
    assert out is not None
    assert kernel.threads and set(kernel.threads) == {3}


# 17. The unusable/unparseable predicate matches the Python handler.


def test_native_format_errors_match_the_handlers_own_error_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cells = ["2024-03-05", "junk", None, "", "2023-07-19", "2024-13-45", "31/12/2024", None]
    oracle = run_oracle("date_shift", operator_config("date_shift"), strs(cells))
    kernel = install_date_shift(monkeypatch, ReferenceDateShiftKernel())
    shape = date_payload_shape({"d": (strs(cells), date_shift_seed(-30, 30))})
    result = run_seq_quarantine(shape, tmp_path / "q.jsonl")
    assert kernel.calls
    assert [(e.column, e.row_index, e.trigger, e.reason) for e in result.row_errors] == [
        (e.column, e.row_index, e.trigger, e.reason) for e in oracle.errors
    ]
    assert len(oracle.errors) == 4


# 18. Calendar overflow is a parity, not a guard.


@pytest.mark.parametrize(
    ("cell", "days"), [("2262-04-11", 1), ("1677-09-22", -1)], ids=["near_max", "near_min"]
)
def test_calendar_overflow_aborts_identically_on_native_and_python(
    cell: str, days: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = date_payload_shape({"d": (strs([cell, "2024-03-05"]), date_shift_seed(days, days))})
    install_date_shift(monkeypatch, ReferenceDateShiftKernel())
    with pytest.raises(Exception) as native:
        run_seq(shape)
    hide_date_shift(monkeypatch)
    with pytest.raises(Exception) as python:
        run_seq(shape)
    assert type(native.value) is type(python.value)
    assert str(native.value) == str(python.value)
