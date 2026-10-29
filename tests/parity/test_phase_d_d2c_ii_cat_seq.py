"""Phase D / D2c-ii: the SEQ categorical routing and the generalized disposition, proven with a
distinguishable stand-in index kernel. No companion is needed.

Covers the native witness, config-aware decline, the kernel-fault abort and its sink-qualified
guarantee, the FK key boundary, the all-null/empty evidence, hash + fpe isolation (including
byte-identical disposition fragments), and descriptor-absent strategies. Real-kernel parity
lives in `test_phase_d_d2c_ii_cat_real.py`.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._sequential_native import (
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    SeqNativeRun,
)
from decoy_engine.execution._sequential_native_ops import (
    SEQ_CATEGORICAL_DISPOSITION_KEY,
    categorical_op,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import JOB_SEED, column_seed
from tests.parity._d2a_seq_data import RecordingSink, callable_sink
from tests.parity._d2b_shapes import run_full, run_seq, strs
from tests.parity._d2c_cat_data import (
    ADMITTED,
    CAT_NS,
    CATS,
    NFC,
    SRC,
    WEIGHTS,
    CatMarkingKernel,
    cat_key_shape,
    cat_seed,
    expect_first_category,
    hide_cat,
    install_cat,
    later_table_shape,
    payload_shape,
)
from tests.parity._d2c_fpe_data import FpeMarkingKernel, fpe_seed, install_fpe
from tests.parity._fpe_key_data import assert_keys_declined
from tests.parity._seq_witness import MarkingKernel
from tests.parity._seq_witness import install as install_hash

# Native witness.


@pytest.mark.parametrize("column", ADMITTED)
def test_admitted_categorical_column_is_consumed_from_the_kernel(
    column: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = payload_shape()
    oracle = run_full(shape).outputs["P"].column(column).to_pylist()
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    out = run_seq(shape).outputs["P"]
    assert out.schema.field(column).type == pa.string()
    assert out.column(column).to_pylist() == expect_first_category(
        oracle, CATS[:4] if column == "c_profile" else CATS
    )
    assert len(kernel.calls) == len(ADMITTED)


def test_a_discarded_native_result_fails_the_consumption_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    real = SeqNativeRun.native_column

    def discard(self: SeqNativeRun, *args: object, **kwargs: object) -> None:
        real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SeqNativeRun, "native_column", discard)
    shape = payload_shape()
    out = run_seq(shape).outputs["P"]
    assert len(kernel.calls) == len(ADMITTED)
    oracle = run_full(shape).outputs["P"].column("c_uniform").to_pylist()
    assert out.column("c_uniform").to_pylist() == oracle  # the Python pick, not the marker


def test_the_kernel_receives_the_handlers_pool_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    run_seq(payload_shape())
    by_ns = [c for c in kernel.calls if c.namespace == CAT_NS]
    pool_sizes = sorted(c.pool_size for c in by_ns)
    # uniform + nfc: len(categories); weighted and profile: the CDF resolution.
    assert pool_sizes == [len(CATS), len(CATS), 1_000_000, 1_000_000]
    assert all(
        c.mask_key == JOB_SEED and c.arrow_type == pa.string() and c.rows == len(SRC) for c in by_ns
    )
    assert [c.namespace for c in kernel.calls].count("ns_other") == 1


def test_disposition_reports_every_native_categorical_column_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cat(monkeypatch, CatMarkingKernel())
    entries = run_seq(payload_shape()).quality_metrics[SEQ_CATEGORICAL_DISPOSITION_KEY]
    assert [e["column"] for e in entries] == ADMITTED
    assert all(
        e["table"] == "P"
        and e["path"] == "scalar"
        and e["executed_backend"] == "rust_companion"
        and "reason" not in e
        for e in entries
    )


# Config-aware decline.


def _declines(seed_kwargs: dict[str, object], source: pa.Array, reason: str) -> None:
    admit = categorical_op(lambda _t: None).admit
    seed = cat_seed(**seed_kwargs)  # type: ignore[arg-type]
    assert admit("c", source.type, seed) == reason


def test_admission_reasons_are_coded_and_value_free() -> None:
    string = strs(["a"])
    _declines({"deterministic": False}, string, "categorical_not_deterministic:c")
    admit = categorical_op(lambda _t: None).admit
    assert admit("c", pa.string(), cat_seed(None)) == "categorical_not_deterministic:c"
    ns_none = cat_seed(None, deterministic=True)
    assert admit("c", pa.string(), ns_none) == "categorical_requires_namespace:c"
    assert (
        admit("c", pa.string(), cat_seed(categories=["a", 1]))
        == "categorical_categories_not_all_string:c"
    )
    assert (
        admit("c", pa.string(), cat_seed(categories=[]))
        == "categorical_categories_not_nonempty_list:c"
    )
    assert (
        admit("c", pa.string(), cat_seed(categories=["a", "b"], weights=[1e-9, 1.0]))
        == "categorical_weights_unbuildable_cdf:c"
    )
    assert (
        admit("c", pa.string(), cat_seed(categories=["a", "b"], weights=[float("nan"), 1.0]))
        == "categorical_weights_unbuildable_cdf:c"
    )
    assert (
        admit("c", pa.large_string(), cat_seed()) == "categorical_source_not_string:c:large_string"
    )
    assert admit("c", pa.int64(), cat_seed()) == "categorical_source_not_string:c:int64"
    assert admit("c", pa.string(), cat_seed()) is None


@pytest.mark.parametrize(
    ("name", "source", "seed", "reason"),
    [
        (
            "nondet",
            strs(SRC),
            cat_seed(deterministic=False),
            "categorical_not_deterministic:nondet",
        ),
        (
            "nonstr_cat",
            strs(SRC),
            cat_seed(categories=[1, 2, 3]),
            "categorical_categories_not_all_string:nonstr_cat",
        ),
        (
            "big",
            pa.array(SRC, pa.large_string()),
            cat_seed(),
            "categorical_source_not_string:big:large_string",
        ),
        (
            "ints",
            pa.array([1, 2, 3, 4, 5, 6, 7], pa.int64()),
            cat_seed(),
            "categorical_source_not_string:ints:int64",
        ),
    ],
)
def test_a_declined_column_runs_python_with_a_value_free_reason(
    name: str, source: pa.Array, seed: object, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape({name: (source, seed)})  # type: ignore[dict-item]
    result = run_seq(shape)
    [entry] = result.quality_metrics[SEQ_CATEGORICAL_DISPOSITION_KEY]
    assert entry["executed_backend"] == "python_scalar" and entry["reason"] == reason
    assert kernel.calls == []
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])
    text = repr(result.quality_metrics)
    assert "alice" not in text and "carol" not in text and "red" not in text


def test_an_unbuildable_cdf_declines_and_the_handler_raises_the_oracles_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape({"w": (strs(SRC), cat_seed(categories=["a", "b"], weights=[1e-9, 1.0]))})
    with pytest.raises(StrategyError) as native:
        run_seq(shape)
    with pytest.raises(StrategyError) as python:
        run_full(shape)
    assert native.value.code == python.value.code == "categorical_weight_below_resolution"
    assert kernel.calls == []


def test_a_non_namespaced_column_stays_python_and_the_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape({"n": (strs(SRC), cat_seed(None, deterministic=True))})
    with pytest.raises(StrategyError) as exc:
        run_seq(shape)
    assert exc.value.code == "categorical_requires_namespace"
    assert kernel.calls == []


def test_a_non_deterministic_column_is_masked_by_python_position_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape({"nd": (strs(SRC), cat_seed(deterministic=False))})
    result = run_seq(shape)
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])
    assert result.quality_metrics[SEQ_CATEGORICAL_DISPOSITION_KEY][0]["reason"] == (
        "categorical_not_deterministic:nd"
    )
    assert kernel.calls == []


def test_companion_absent_runs_the_python_handler_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_cat(monkeypatch)
    shape = payload_shape()
    result = run_seq(shape)
    entries = result.quality_metrics[SEQ_CATEGORICAL_DISPOSITION_KEY]
    assert {e["executed_backend"] for e in entries} == {"python_scalar"}
    assert {e["reason"] for e in entries} == {"crypto_extension_unavailable"}
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


# Kernel fault and the sink-qualified no-partial-output guarantee.


def test_a_kernel_fault_aborts_the_run_without_a_python_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cat(monkeypatch, CatMarkingKernel(fail_on_call=2))
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(payload_shape(), sink=sink)
    assert sink.commits == 0 and sink.committed == {}


def test_a_later_table_fault_leaves_a_transactional_sink_with_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cat(monkeypatch, CatMarkingKernel(fail_on_call=1))
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(later_table_shape(), sink=sink)
    assert sink.commits == 0 and sink.committed == {}
    assert "C" not in sink.staged
    assert sink.aborts == 1


def test_a_later_table_fault_keeps_what_a_callable_sink_already_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cat(monkeypatch, CatMarkingKernel(fail_on_call=1))
    published: dict[str, pa.Table] = {}
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(later_table_shape(), sink=callable_sink(published))
    assert list(published) == ["P"]  # the earlier table was delivered; the failed one never is


# FK key boundary: a categorical key column stays on Python.


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_categorical_key_columns_stay_python_with_an_explicit_decline_reason(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    hash_kernel = install_hash(monkeypatch, MarkingKernel())
    shape = cat_key_shape(policy)
    result = run_seq(shape)
    oracle = run_full(shape)
    for table in ("A", "B", "C"):
        assert result.outputs[table].equals(oracle.outputs[table]), table
    assert kernel.calls == [] and hash_kernel.calls == []
    # Stays Python and reports why, instead of staying silent.
    for key in (
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
    ):
        assert key not in result.quality_metrics
    assert_keys_declined(result, shape, "categorical", SEQ_CATEGORICAL_DISPOSITION_KEY)


def test_a_categorical_key_does_not_disturb_a_native_hash_payload_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cat(monkeypatch, CatMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    result = run_seq(cat_key_shape(OrphanPolicy.REMAP, hash_payload=True))
    assert [e["column"] for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]] == ["h"]
    assert {e["path"] for e in result.quality_metrics[SEQ_CATEGORICAL_DISPOSITION_KEY]} == {
        "fk_key_declined"
    }


def test_a_categorical_call_inside_a_remap_scope_returns_to_python_untouched() -> None:
    run = SeqNativeRun()
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", "t")
    object.__setattr__(ctx, "nested_outer_column", "")
    decision = _sequential_native.ColumnDecision(
        "t", "x", "rust_companion", None, pa.string(), strategy="categorical"
    )
    run._decisions[("t", "x")] = decision
    run._bound["categorical"] = object()
    df = pd.DataFrame({"x": ["a"]})
    with run.remap_scope():
        assert (
            run.categorical_column(
                ctx, df, "x", namespace="n", mask_key=b"k", categories=["a"], cdf=None
            )
            is None
        )
    assert decision.rows == 0


# Evidence reflects what actually ran.


def test_an_empty_selection_records_a_no_call_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape(
        {"cat": (strs(SRC), cat_seed())},
        extra_cols={"flag": pa.array([0] * len(SRC), pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"cat": "flag == 1"},
    )
    result = run_seq(shape)
    [entry] = result.quality_metrics[SEQ_CATEGORICAL_DISPOSITION_KEY]
    assert kernel.calls == []
    assert entry["executed_backend"] == "none" and entry["reason"] == "no_rows_hashed"
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_an_all_null_column_calls_the_kernel_and_is_recorded_as_a_native_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Decision: SEQ does not shortcut an all-null column (the raw native helper derives
    # indices unconditionally), so the kernel IS called and the entry says so.
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape({"cat": (strs([None] * 7), cat_seed())})
    result = run_seq(shape)
    [entry] = result.quality_metrics[SEQ_CATEGORICAL_DISPOSITION_KEY]
    assert len(kernel.calls) == 1 and kernel.calls[0].rows == 7
    assert entry["executed_backend"] == "rust_companion" and "reason" not in entry
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_a_when_subset_runs_only_the_selected_rows_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape(
        {"cat": (strs(SRC), cat_seed())},
        extra_cols={"flag": pa.array([1, 0, 0, 1, 0, 1, 1], pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"cat": "flag == 1"},
    )
    out = run_seq(shape).outputs["P"].column("cat").to_pylist()
    assert kernel.calls[0].rows == 4
    assert out == [CATS[0], "bob", None, CATS[0], "", CATS[0], CATS[0]]


# hash + fpe isolation and byte-identical fragments.


def _mixed_shape(order: list[str]) -> object:
    parts = {
        "h": (strs(["u0", "u1", None, "u3", "u4", "u5", "u6"]), column_seed(namespace="ns_h")),
        "f": (strs(["1234567890"] * 7), fpe_seed()),
        "c": (strs(SRC), cat_seed()),
    }
    return payload_shape({name: parts[name] for name in order})


@pytest.mark.parametrize(
    "order", [["h", "f", "c"], ["c", "f", "h"], ["f", "c", "h"], ["c", "h", "f"]]
)
def test_hash_fpe_and_categorical_mask_correctly_in_any_column_order(
    order: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _mixed_shape(order)
    oracle = run_full(shape).outputs["P"]  # type: ignore[arg-type]
    cat = install_cat(monkeypatch, CatMarkingKernel())
    fpe = install_fpe(monkeypatch, FpeMarkingKernel())
    hsh = install_hash(monkeypatch, MarkingKernel())
    result = run_seq(shape)  # type: ignore[arg-type]
    out = result.outputs["P"]
    assert [len(k.calls) for k in (cat, fpe, hsh)] == [1, 1, 2]  # hash: the payload and the key
    assert out.column("c").to_pylist() == expect_first_category(oracle.column("c").to_pylist())
    assert all(v is None or v.startswith("native:") for v in out.column("h").to_pylist())
    assert all(v is None or v.startswith("native:") for v in out.column("f").to_pylist())
    assert set(result.quality_metrics) == {
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
    }


def test_the_hash_and_fpe_fragments_are_byte_identical_with_and_without_categorical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    hide_cat(monkeypatch)
    with_cat = run_seq(_mixed_shape(["h", "f", "c"])).quality_metrics  # type: ignore[arg-type]
    alone = run_seq(_mixed_shape(["h", "f"])).quality_metrics  # type: ignore[arg-type]
    for key in (SEQ_HASH_DISPOSITION_KEY, SEQ_FPE_DISPOSITION_KEY):
        assert repr(with_cat[key]) == repr(alone[key])
    assert SEQ_CATEGORICAL_DISPOSITION_KEY in with_cat
    assert SEQ_CATEGORICAL_DISPOSITION_KEY not in alone


def test_the_hash_and_fpe_fragments_keep_their_literal_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Surface pin written as literals, never computed from the output under test.
    install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    metrics = run_seq(_mixed_shape(["h", "f"])).quality_metrics  # type: ignore[arg-type]
    native = "rust_companion"
    assert metrics[SEQ_HASH_DISPOSITION_KEY] == [
        {"table": "P", "column": "h", "path": "scalar", "executed_backend": native},
        {"table": "P", "column": "hk", "path": "scalar", "executed_backend": native},
        {
            "table": "C",
            "column": "ck",
            "path": "fk_inherited",
            "executed_backend": "inherited",
            "parent": "P.hk",
            "parent_backend": native,
        },
    ]
    assert metrics[SEQ_FPE_DISPOSITION_KEY] == [
        {"table": "P", "column": "f", "path": "scalar", "executed_backend": native}
    ]


def test_a_job_without_categorical_has_no_categorical_disposition_key() -> None:
    shape = payload_shape({"p": (strs(SRC), column_seed("passthrough", namespace=None))})
    assert SEQ_CATEGORICAL_DISPOSITION_KEY not in run_seq(shape).quality_metrics


def test_the_generalized_metrics_iterate_the_registered_descriptors() -> None:
    run = SeqNativeRun()
    for strategy in ("hash", "fpe", "categorical"):
        run._decisions[("t", strategy)] = _sequential_native.ColumnDecision(
            "t", strategy, "python_scalar", "r", pa.string(), strategy=strategy
        )
    assert list(run.metrics()) == [
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_FPE_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
    ]
    assert SeqNativeRun().metrics() == {}


# Accepted divergence and descriptor-absent strategies.


def test_nfc_columns_follow_the_native_index_not_byte_identity_on_the_kernel_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The index derives from the source text; the kernel (not the handler) normalizes it.
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    run_seq(payload_shape({"n": (strs(NFC), cat_seed())}))
    assert [c.rows for c in kernel.calls] == [len(NFC)]


@pytest.mark.parametrize("strategy", ["redact"])
def test_an_unwired_strategy_has_no_descriptor_and_runs_in_python(strategy: str) -> None:
    assert strategy not in _sequential_native._OPS
    assert set(_sequential_native._OPS) == {
        "hash",
        "fpe",
        "categorical",
        "bucket_perturb",
        "date_shift",
        "faker",
        "text_redact",
        "text_mask",
        "group_key",
    }


def test_weights_reach_the_kernel_as_a_cdf_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    run_seq(payload_shape({"w": (strs(SRC), cat_seed(weights=WEIGHTS))}))
    assert [c.pool_size for c in kernel.calls] == [1_000_000]
