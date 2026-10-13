"""Phase D / D2c-iii: the SEQ bucket_perturb routing, proven with stand-in index kernels. No
companion is needed.

Covers consumption of the kernel result, config-aware and data-aware decline, `preserve_values`
ordering, the kernel-fault abort and its sink-qualified guarantee, the FK key boundary, the
derive-call evidence, hash + fpe + categorical isolation (byte-identical fragments), and the
shared range predicate. Real-kernel parity lives in `test_phase_d_d2c_iii_bucket_real.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._sequential_native import (
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    SeqNativeRun,
)
from decoy_engine.execution._sequential_native_ops import (
    SEQ_BUCKET_DISPOSITION_KEY,
    SEQ_CATEGORICAL_DISPOSITION_KEY,
    bucket_perturb_op,
)
from decoy_engine.execution.native._bucket_perturb_ext import native_range_exceeded
from decoy_engine.execution.out_of_core import _bucket_native
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d1b1_bucket_data import (
    TZ_COLUMNS,
    all_null_column,
    declined_sources,
    unparseable_column,
)
from tests.parity._d2a_seq_data import RecordingSink, callable_sink
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
from tests.parity._d2c_bucket_data import (
    ADMITTED,
    BUCKET_NS,
    DMY,
    ISO,
    STAMP,
    BucketMarkingKernel,
    ReferenceBucketKernel,
    bucket_payload_shape,
    bucket_seed,
    expect_last_offset,
    hide_bucket,
    install_bucket,
)
from tests.parity._d2c_cat_data import SRC, cat_seed, later_table_shape
from tests.parity._d2c_fpe_data import FpeMarkingKernel, fpe_seed, install_fpe
from tests.parity._fpe_key_data import assert_keys_declined
from tests.parity._seq_witness import MarkingKernel
from tests.parity._seq_witness import install as install_hash

NATIVE = "rust_companion"
PYTHON = "python_scalar"
SENTINEL = "2262-04-10"
NEAR_BOUNDARY = ["2024-03-05", SENTINEL, None, "2023-07-19", "not-a-date", "2021-01-01"]


def _entries(result: Any) -> list[dict[str, Any]]:
    return result.quality_metrics[SEQ_BUCKET_DISPOSITION_KEY]


def _preserve(*dates: str) -> tuple[tuple[str, Any], ...]:
    return (("preserve_values", list(dates)),)


# Native witness (consumption).

_BUCKET_OF = {
    "b_week": ("week", ISO),
    "b_month": ("month", ISO),
    "b_quarter": ("quarter", ISO),
    "b_other_ns": ("month", ISO),
    "b_dmy": ("month", DMY),
    "b_stamp": ("month", STAMP),
}


@pytest.mark.parametrize("column", ADMITTED)
def test_admitted_bucket_column_is_consumed_from_the_kernel(
    column: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = bucket_payload_shape()
    source = shape.sources["P"].column(column).to_pylist()
    oracle = run_full(shape).outputs["P"].column(column).to_pylist()
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    out = run_seq(shape).outputs["P"]
    bucket, fmt = _BUCKET_OF[column]
    expected = expect_last_offset(source, bucket, fmt)
    assert out.schema.field(column).type == pa.string()
    assert out.column(column).to_pylist() == expected
    assert expected != oracle  # the marker is distinguishable from the Python keyed pick
    assert kernel.calls


def test_a_discarded_native_result_fails_the_consumption_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    real = SeqNativeRun.bucket_column

    def discard(self: SeqNativeRun, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)

    monkeypatch.setattr(SeqNativeRun, "bucket_column", discard)
    shape = bucket_payload_shape()
    out = run_seq(shape).outputs["P"]
    assert kernel.calls
    assert (
        out.column("b_month").to_pylist()
        == run_full(shape).outputs["P"].column("b_month").to_pylist()
    )


def test_the_kernel_receives_the_handlers_namespace_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    run_seq(bucket_payload_shape({"b": (strs(NEAR_BOUNDARY[:1] * 3), bucket_seed("week"))}))
    [call] = [c for c in kernel.calls if c.namespace == BUCKET_NS]
    assert call.pool_size == 7 and call.arrow_type == pa.string() and call.rows == 3


def test_disposition_reports_every_native_bucket_column_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_bucket(monkeypatch, BucketMarkingKernel())
    entries = _entries(run_seq(bucket_payload_shape()))
    assert [e["column"] for e in entries] == ADMITTED
    assert all(
        e["table"] == "P"
        and e["path"] == "scalar"
        and e["executed_backend"] == NATIVE
        and "reason" not in e
        and "preserved_rows" not in e
        for e in entries
    )


# Config-aware decline.


def test_admission_reasons_are_coded_and_value_free() -> None:
    admit = bucket_perturb_op(lambda _t: None).admit
    string = pa.string()
    assert admit("c", string, bucket_seed("month")) is None
    assert (
        admit("c", string, bucket_seed("month", namespace=None))
        == "bucket_perturb_config_declined:c:bucket_perturb_requires_namespace"
    )
    assert (
        admit("c", string, bucket_seed("year"))
        == "bucket_perturb_config_declined:c:bucket_perturb_unsupported_bucket"
    )
    assert (
        admit("c", string, bucket_seed("month", None))
        == "bucket_perturb_config_declined:c:bucket_perturb_requires_date_format"
    )
    assert (
        admit("c", string, bucket_seed("month", "%Y-%m-%d %z"))
        == "bucket_perturb_config_declined:c:bucket_perturb_timezone_directive"
    )
    assert (
        admit("c", string, bucket_seed("month", "mixed"))
        == "bucket_perturb_config_declined:c:bucket_perturb_special_date_format"
    )
    assert (
        admit("c", string, bucket_seed("month", extra=_preserve("not-a-date")))
        == "bucket_perturb_config_declined:c:bucket_perturb_preserve_values_invalid"
    )
    assert (
        admit("c", pa.large_string(), bucket_seed("month"))
        == "bucket_perturb_source_not_string:c:large_string"
    )
    assert admit("c", pa.date32(), bucket_seed("month")) == (
        "bucket_perturb_source_not_string:c:date32[day]"
    )


def _decline_cases() -> list[tuple[str, pa.Array, Any, str]]:
    cases: list[tuple[str, pa.Array, Any, str]] = [
        (
            "d_date32",
            declined_sources()["d_date32"],
            bucket_seed("month"),
            "bucket_perturb_source_not_string:d_date32:date32[day]",
        ),
        (
            "d_noformat",
            strs(["2024-03-05", None, "2021-11-14", "junk"]),
            bucket_seed("month", None),
            "bucket_perturb_config_declined:d_noformat:bucket_perturb_requires_date_format",
        ),
    ]
    for name, (fmt, values) in TZ_COLUMNS.items():
        cases.append(
            (
                name,
                strs(values),
                bucket_seed("month", fmt),
                f"bucket_perturb_config_declined:{name}:bucket_perturb_timezone_directive",
            )
        )
    return cases


@pytest.mark.parametrize(
    ("name", "source", "seed", "reason"), _decline_cases(), ids=[c[0] for c in _decline_cases()]
)
def test_a_declined_column_runs_python_with_a_value_free_reason(
    name: str, source: pa.Array, seed: Any, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    shape = bucket_payload_shape({name: (source, seed)})
    result = run_seq(shape)
    [entry] = _entries(result)
    assert entry["executed_backend"] == PYTHON and entry["reason"] == reason
    assert kernel.calls == []
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])
    assert "2024" not in repr(result.quality_metrics)


@pytest.mark.parametrize(
    ("seed", "code"),
    [
        (bucket_seed("month", namespace=None), "bucket_perturb_requires_namespace"),
        (bucket_seed("year"), "bucket_perturb_invalid_config"),
        (bucket_seed("month", extra=_preserve("not-a-date")), "bucket_perturb_invalid_config"),
    ],
    ids=["no_namespace", "unsupported_bucket", "invalid_preserve"],
)
def test_a_config_the_handler_rejects_declines_and_the_handler_raises_the_oracles_error(
    seed: Any, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    shape = bucket_payload_shape({"b": (strs(NEAR_BOUNDARY), seed)})
    with pytest.raises(StrategyError) as native:
        run_seq(shape)
    with pytest.raises(StrategyError) as python:
        run_full(shape)
    assert native.value.code == python.value.code == code
    assert kernel.calls == []


def test_companion_absent_runs_the_python_handler_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_bucket(monkeypatch)
    shape = bucket_payload_shape()
    result = run_seq(shape)
    entries = _entries(result)
    assert {e["executed_backend"] for e in entries} == {PYTHON}
    assert {e["reason"] for e in entries} == {"crypto_extension_unavailable"}
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


# Data-aware range decline: the shared predicate decides, SEQ declines and never raises.


def test_a_near_boundary_non_preserved_date_declines_the_whole_column_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_bucket(monkeypatch, ReferenceBucketKernel())
    shape = bucket_payload_shape({"b": (strs(NEAR_BOUNDARY), bucket_seed("month"))})
    result = run_seq(shape)
    [entry] = _entries(result)
    assert kernel.calls == []
    assert entry["executed_backend"] == PYTHON
    assert entry["reason"] == "sequential_bucket_perturb_date_out_of_native_range"
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])  # the Python pick succeeds
    assert SENTINEL not in repr(result.quality_metrics)


def test_a_declined_column_leaves_the_preserved_count_to_the_python_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_bucket(monkeypatch, ReferenceBucketKernel())
    cells = [*NEAR_BOUNDARY, "9999-12-31"]
    seed = bucket_seed("month", extra=_preserve("9999-12-31"))
    shape = bucket_payload_shape({"b": (strs(cells), seed)})
    result = run_seq(shape)
    oracle = run_full(shape)
    [entry] = _entries(result)
    assert entry["reason"] == "sequential_bucket_perturb_date_out_of_native_range"
    assert "preserved_rows" not in entry
    assert result.quality_metrics["date_preserve"] == oracle.quality_metrics["date_preserve"]
    assert result.quality_metrics["date_preserve"][0]["preserved_rows"] == 1
    assert result.outputs["P"].equals(oracle.outputs["P"])


# preserve_values parity.


def _preserve_run(
    monkeypatch: pytest.MonkeyPatch, cells: Sequence[str | None], seed: Any, **shape_kwargs: Any
) -> tuple[Any, Any, ReferenceBucketKernel]:
    kernel = install_bucket(monkeypatch, ReferenceBucketKernel())
    shape = bucket_payload_shape({"b": (strs(cells), seed)}, **shape_kwargs)
    return run_seq(shape), run_full(shape), kernel


def _assert_matches_oracle(result: Any, oracle: Any) -> None:
    assert result.outputs["P"].equals(oracle.outputs["P"])
    assert result.quality_metrics.get("date_preserve") == oracle.quality_metrics.get(
        "date_preserve"
    )


def test_a_preserved_near_boundary_sentinel_survives_without_triggering_the_range_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = bucket_seed("month", extra=_preserve(SENTINEL))
    result, oracle, kernel = _preserve_run(monkeypatch, NEAR_BOUNDARY, seed)
    [entry] = _entries(result)
    assert entry["executed_backend"] == NATIVE and entry["preserved_rows"] == 1
    assert result.outputs["P"].column("b").to_pylist()[1] == SENTINEL
    # The kernel saw the three parseable non-preserved rows and never the sentinel.
    assert sum(c.rows for c in kernel.calls) == 3
    _assert_matches_oracle(result, oracle)
    assert result.quality_metrics["date_preserve"][0]["preserved_rows"] == 1


def test_the_same_column_without_the_sentinel_in_the_preserve_list_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = bucket_seed("month", extra=_preserve("2000-01-01"))
    result, oracle, kernel = _preserve_run(monkeypatch, NEAR_BOUNDARY, seed)
    assert kernel.calls == []
    assert _entries(result)[0]["reason"] == "sequential_bucket_perturb_date_out_of_native_range"
    _assert_matches_oracle(result, oracle)


def test_a_sentinel_outside_the_pandas_range_survives_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 9999-12-31 coerces to NaT in the range check, so only preserve can explain the match.
    cells = ["2024-03-05", "9999-12-31", None, "2023-07-19", "9999-12-31"]
    seed = bucket_seed("month", extra=_preserve("9999-12-31"))
    result, oracle, kernel = _preserve_run(monkeypatch, cells, seed)
    assert result.outputs["P"].column("b").to_pylist()[1] == "9999-12-31"
    assert sum(c.rows for c in kernel.calls) == 2
    assert _entries(result)[0]["preserved_rows"] == 2
    _assert_matches_oracle(result, oracle)


def test_an_all_preserved_column_makes_no_kernel_call_and_reports_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells = ["2024-03-05"] * 5
    seed = bucket_seed("month", extra=_preserve("2024-03-05"))
    result, oracle, kernel = _preserve_run(monkeypatch, cells, seed)
    [entry] = _entries(result)
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == "no_native_bucket_calls"
    assert entry["preserved_rows"] == 5
    assert result.outputs["P"].column("b").to_pylist() == cells
    _assert_matches_oracle(result, oracle)


def test_a_preserve_list_that_matches_nothing_records_a_zero_count_like_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells = ["2024-03-05", None, "2023-07-19", "junk"]
    seed = bucket_seed("month", extra=_preserve("2000-01-01"))
    result, oracle, kernel = _preserve_run(monkeypatch, cells, seed)
    [entry] = _entries(result)
    assert entry["executed_backend"] == NATIVE and entry["preserved_rows"] == 0
    assert [c.rows for c in kernel.calls] == [2]
    assert result.quality_metrics["date_preserve"][0]["preserved_rows"] == 0
    _assert_matches_oracle(result, oracle)


def test_a_gapped_when_selection_counts_and_masks_only_the_selected_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells = ["2024-03-05", SENTINEL, "2023-07-19", "2024-03-05", "2021-01-01", SENTINEL]
    # The SELECTED rows (1, 3, 4) hold the sentinel once; row 5's sentinel is not selected.
    seed = bucket_seed("month", extra=_preserve(SENTINEL))
    result, oracle, kernel = _preserve_run(
        monkeypatch,
        cells,
        seed,
        extra_cols={"flag": pa.array([0, 1, 0, 1, 1, 0], pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"b": "flag == 1"},
    )
    assert sum(c.rows for c in kernel.calls) == 2
    assert _entries(result)[0]["preserved_rows"] == 1
    out = result.outputs["P"].column("b").to_pylist()
    assert out[0] == cells[0] and out[1] == SENTINEL and out[5] == SENTINEL
    _assert_matches_oracle(result, oracle)


# Kernel fault and the sink-qualified no-partial-output guarantee.


def _later_table_bucket_shape() -> Shape:
    return later_table_shape({"b": (strs(NEAR_BOUNDARY[:1] * 7), bucket_seed("month"))})


def test_a_kernel_fault_aborts_the_run_without_a_python_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_bucket(monkeypatch, BucketMarkingKernel(fail_on_call=1))
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(bucket_payload_shape(), sink=sink)
    assert sink.commits == 0 and sink.committed == {}


def test_a_later_table_fault_leaves_a_transactional_sink_with_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_bucket(monkeypatch, BucketMarkingKernel(fail_on_call=1))
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(_later_table_bucket_shape(), sink=sink)
    assert sink.commits == 0 and sink.committed == {}
    assert "C" not in sink.staged
    assert sink.aborts == 1


def test_a_later_table_fault_keeps_what_a_callable_sink_already_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_bucket(monkeypatch, BucketMarkingKernel(fail_on_call=1))
    published: dict[str, pa.Table] = {}
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(_later_table_bucket_shape(), sink=callable_sink(published))
    assert list(published) == ["P"]


# FK key boundary: a bucket_perturb key column stays on Python.


def _bucket_key_shape(policy: OrphanPolicy, *, hash_payload: bool = False) -> Shape:
    keys_a = ["2024-03-05", "2023-07-19"]
    keys_b = [*keys_a, "2022-01-10"]
    plan = make_plan(
        {
            "A": table_seed(
                {
                    "k": bucket_seed("month", namespace="ns_ab"),
                    **({"h": hashed("ns_h")} if hash_payload else {}),
                }
            ),
            "B": table_seed({"k": bucket_seed("month", namespace="ns_ab")}),
        }
    )
    sources = {"A": pa.table({"k": strs(keys_a)}), "B": pa.table({"k": strs(keys_b)})}
    if hash_payload:
        sources["A"] = pa.table({"k": strs(keys_a), "h": strs(["u", "v"])})
    return Shape(plan, graph(edge("A", "k", "B", "k", "ns_ab", policy)), sources)


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_bucket_key_columns_stay_python_with_an_explicit_decline_reason(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    hash_kernel = install_hash(monkeypatch, MarkingKernel())
    shape = _bucket_key_shape(policy)
    result, oracle = run_seq(shape), run_full(shape)
    for table in ("A", "B"):
        assert result.outputs[table].equals(oracle.outputs[table]), table
    assert kernel.calls == [] and hash_kernel.calls == []
    # Stays Python and reports why, instead of staying silent.
    for key in (
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
    ):
        assert key not in result.quality_metrics
    assert_keys_declined(result, shape, "bucket_perturb", SEQ_BUCKET_DISPOSITION_KEY)


def test_a_bucket_key_does_not_disturb_a_native_hash_payload_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_bucket(monkeypatch, BucketMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    result = run_seq(_bucket_key_shape(OrphanPolicy.REMAP, hash_payload=True))
    assert [e["column"] for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]] == ["h"]
    assert {e["path"] for e in result.quality_metrics[SEQ_BUCKET_DISPOSITION_KEY]} == {
        "fk_key_declined"
    }


def test_a_bucket_call_inside_a_remap_scope_returns_to_python_untouched() -> None:
    from decoy_engine.execution._adapter import StrategyContext

    run = SeqNativeRun()
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", "t")
    object.__setattr__(ctx, "nested_outer_column", "")
    decision = _sequential_native.ColumnDecision(
        "t", "x", NATIVE, None, pa.string(), strategy="bucket_perturb"
    )
    run._decisions[("t", "x")] = decision
    run._bound["bucket_perturb"] = object()
    df = pd.DataFrame({"x": ["2024-03-05"]})
    kwargs: dict[str, Any] = {
        "namespace": "n",
        "mask_key": b"k",
        "bucket": "month",
        "date_format": ISO,
        "preserve": None,
    }
    with run.remap_scope():
        assert run.bucket_column(ctx, df, "x", **kwargs) is None
    assert decision.rows == 0
    # A decision for another strategy is not this column's to run.
    decision.strategy = "hash"
    assert run.bucket_column(ctx, df, "x", **kwargs) is None


# Evidence reflects the compiled calls that actually ran.


@pytest.mark.parametrize(
    "source",
    [
        all_null_column(),
        unparseable_column(),
        pa.array(["not-a-date", None, "2024-13-45", "9999-12-31"], pa.string()),
    ],
    ids=["all_null", "all_unparseable", "null_unparseable_out_of_range"],
)
def test_a_column_that_parses_nothing_makes_no_kernel_call_and_claims_no_backend(
    source: pa.Array, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    shape = bucket_payload_shape({"b": (source, bucket_seed("week"))})
    result = run_seq(shape)
    [entry] = _entries(result)
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == "no_native_bucket_calls"
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_a_single_parseable_row_is_enough_to_claim_the_kernel_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    cells = [None, "junk", "2024-03-05", ""]
    result = run_seq(bucket_payload_shape({"b": (strs(cells), bucket_seed("week"))}))
    [entry] = _entries(result)
    assert [c.rows for c in kernel.calls] == [1]
    assert entry["executed_backend"] == NATIVE and "reason" not in entry


def test_an_empty_selection_records_a_no_rows_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    shape = bucket_payload_shape(
        {"b": (strs(NEAR_BOUNDARY), bucket_seed("month"))},
        extra_cols={"flag": pa.array([0] * len(NEAR_BOUNDARY), pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"b": "flag == 1"},
    )
    result = run_seq(shape)
    [entry] = _entries(result)
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == "no_rows_hashed"
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_a_when_subset_runs_only_the_selected_rows_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_bucket(monkeypatch, BucketMarkingKernel())
    cells = ["2024-03-05", "2023-07-19", None, "2021-11-14", "2024-02-29", "2022-02-02"]
    shape = bucket_payload_shape(
        {"b": (strs(cells), bucket_seed("month"))},
        extra_cols={"flag": pa.array([1, 0, 0, 1, 0, 1], pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"b": "flag == 1"},
    )
    out = run_seq(shape).outputs["P"].column("b").to_pylist()
    assert sum(c.rows for c in kernel.calls) == 3
    selected = expect_last_offset([cells[0], cells[3], cells[5]], "month", ISO)
    assert out == [selected[0], cells[1], None, selected[1], cells[4], selected[2]]


# Reference-equivalent kernel: oracle parity without the companion.


def test_a_reference_kernel_reproduces_the_python_handler_on_every_admitted_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = bucket_payload_shape()
    install_bucket(monkeypatch, ReferenceBucketKernel())
    result, oracle = run_seq(shape), run_full(shape)
    assert result.outputs["P"].equals(oracle.outputs["P"])
    assert result.outputs["C"].equals(oracle.outputs["C"])
    assert {e["executed_backend"] for e in _entries(result)} == {NATIVE}


# Isolation from hash, fpe and categorical, and byte-identical fragments.


def _mixed_shape(order: list[str]) -> Shape:
    parts = {
        "h": (strs(["u0", "u1", None, "u3", "u4", "u5", "u6"]), column_seed(namespace="ns_h")),
        "f": (strs(["1234567890"] * 7), fpe_seed()),
        "c": (strs(SRC), cat_seed()),
        "b": (
            strs(["2024-03-05", None, "junk", "2021-11-14", "2020-02-29", "", "2023-07-19"]),
            bucket_seed("month"),
        ),
    }
    return bucket_payload_shape({name: parts[name] for name in order})


@pytest.mark.parametrize(
    "order",
    [["h", "f", "c", "b"], ["b", "c", "f", "h"], ["c", "b", "h", "f"], ["f", "h", "b", "c"]],
)
def test_all_four_native_strategies_mask_correctly_in_any_column_order(
    order: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _mixed_shape(order)
    oracle = run_full(shape).outputs["P"]
    # One index loader serves both categorical and bucket_perturb, so one reference kernel
    # (which matches the Python draw for both) stands in for it.
    index = install_bucket(monkeypatch, ReferenceBucketKernel())
    install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    result = run_seq(shape)
    out = result.outputs["P"]
    assert out.column("b").to_pylist() == oracle.column("b").to_pylist()
    assert out.column("c").to_pylist() == oracle.column("c").to_pylist()
    assert any(c.namespace == BUCKET_NS for c in index.calls)
    assert all(v is None or v.startswith("native:") for v in out.column("h").to_pylist())
    assert all(v is None or v.startswith("native:") for v in out.column("f").to_pylist())
    assert set(result.quality_metrics) == {
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        SEQ_BUCKET_DISPOSITION_KEY,
    }


def test_the_other_fragments_are_byte_identical_with_and_without_bucket_perturb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_bucket(monkeypatch, ReferenceBucketKernel())
    install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    with_bucket = run_seq(_mixed_shape(["h", "f", "c", "b"])).quality_metrics
    alone = run_seq(_mixed_shape(["h", "f", "c"])).quality_metrics
    for key in (
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
    ):
        assert repr(with_bucket[key]) == repr(alone[key])
    assert SEQ_BUCKET_DISPOSITION_KEY in with_bucket
    assert SEQ_BUCKET_DISPOSITION_KEY not in alone


def test_the_bucket_fragment_keeps_its_literal_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # Surface pin written as literals, never computed from the output under test.
    install_bucket(monkeypatch, ReferenceBucketKernel())
    cells = ["2024-03-05", "2023-07-19", "2023-07-19"]
    seed = bucket_seed("month", extra=_preserve("2023-07-19"))
    metrics = run_seq(bucket_payload_shape({"b": (strs(cells), seed)})).quality_metrics
    assert metrics[SEQ_BUCKET_DISPOSITION_KEY] == [
        {
            "table": "P",
            "column": "b",
            "path": "scalar",
            "executed_backend": NATIVE,
            "preserved_rows": 2,
        }
    ]


def test_a_job_without_bucket_perturb_has_no_bucket_disposition_key() -> None:
    shape = bucket_payload_shape({"p": (strs(SRC), column_seed("passthrough", namespace=None))})
    assert SEQ_BUCKET_DISPOSITION_KEY not in run_seq(shape).quality_metrics


# Descriptor registry.


def test_the_registry_holds_the_native_strategies_and_not_text_mask() -> None:
    assert set(_sequential_native._OPS) == {
        "hash",
        "fpe",
        "categorical",
        "bucket_perturb",
        "date_shift",
        "faker",
    }
    assert "text_mask" not in _sequential_native._OPS


def test_the_generalized_metrics_iterate_the_registered_descriptors() -> None:
    run = SeqNativeRun()
    for strategy in ("hash", "fpe", "categorical", "bucket_perturb"):
        run._decisions[("t", strategy)] = _sequential_native.ColumnDecision(
            "t", strategy, PYTHON, "r", pa.string(), strategy=strategy
        )
    assert list(run.metrics()) == [
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        SEQ_BUCKET_DISPOSITION_KEY,
    ]


# The shared range predicate, and the out-of-core wrapper that must keep its exact error.


def _values(*cells: str | None) -> pa.Array:
    return pa.array(list(cells), pa.string())


@pytest.mark.parametrize(
    ("cells", "bucket", "expected"),
    [
        (["2024-03-05", None, "junk"], "month", False),
        ([None, None], "week", False),
        (["9999-12-31", "0001-01-01"], "quarter", False),  # outside the range: NaT, no guard
        (["2262-04-10"], "month", True),
        (["1677-09-22"], "month", True),
        (["2262-04-04"], "week", True),
        (["2262-04-03"], "week", False),
        (["2262-02-01"], "quarter", True),
        (["2262-01-01"], "quarter", False),
    ],
)
def test_the_range_predicate_flags_dates_within_one_bucket_of_the_pandas_bounds(
    cells: list[str | None], bucket: str, expected: bool
) -> None:
    assert native_range_exceeded(_values(*cells), bucket, ISO) is expected


def test_the_out_of_core_guard_keeps_its_exact_code_and_message() -> None:
    assert _bucket_native.REASON_OUT_OF_NATIVE_RANGE == (
        "out_of_core_bucket_perturb_date_out_of_native_range"
    )
    _bucket_native._guard_native_range(_values("2024-03-05"), "month", ISO, "d")  # in range: no-op
    with pytest.raises(ExecutionError) as exc:
        _bucket_native._guard_native_range(_values(SENTINEL), "month", ISO, "d")
    assert exc.value.code == "out_of_core_bucket_perturb_date_out_of_native_range"
    assert exc.value.message == (
        "bucket_perturb column 'd' holds a date within one month of the range the native "
        "operator can compute (1677-09-21 to 2262-04-11); the run is aborted rather than "
        "consuming a possibly wrapped result. Run without the compiled companion to mask the "
        "full calendar range."
    )
