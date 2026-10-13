"""Phase D / D2c-i: the SEQ fpe routing and the generalized descriptor seam, proven with a
distinguishable stand-in kernel. No companion is needed.

Covers the native witness, config-aware decline, the kernel error channel (fail closed, no
output), the FK key boundary, preserved security warnings, per-strategy kernel isolation with a
hash column beside an fpe one, and descriptor-absent strategies. Real-kernel parity lives in
`test_phase_d_d2c_fpe_real.py`.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._sequential_native import (
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    SeqNativeRun,
)
from decoy_engine.execution.native._crypto_ext import FpeConfig
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import JOB_SEED, column_seed
from tests.parity._d2a_seq_data import RecordingSink, callable_sink
from tests.parity._d2b_shapes import hashed, run_full, run_seq, strs
from tests.parity._d2c_fpe_data import (
    DIGITS,
    HK,
    MARK,
    FpeMarkingKernel,
    assert_marked_like,
    fpe_key_shape,
    fpe_seed,
    hide_fpe,
    install_fpe,
    payload_shape,
    warning_view,
)
from tests.parity._fpe_key_data import fpe_marked_expected
from tests.parity._seq_witness import MarkingKernel
from tests.parity._seq_witness import install as install_hash

_ADMITTED = ["f_digits", "f_ssn", "f_alnum", "f_nosep", "f_luhn", "f_partial", "f_group", "f_nfc"]


# Native witness.


@pytest.mark.parametrize("column", _ADMITTED)
def test_admitted_fpe_column_is_consumed_from_the_kernel(
    column: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = payload_shape()
    oracle = run_full(shape).outputs["P"].column(column).to_pylist()
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    out = run_seq(shape).outputs["P"]
    assert out.schema.field(column).type == pa.string()
    emitted = out.column(column).to_pylist()
    assert_marked_like(emitted, oracle)
    assert len(kernel.calls) == len(_ADMITTED)


def test_a_discarded_native_result_fails_the_consumption_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    real = SeqNativeRun.native_column

    def discard(self: SeqNativeRun, *args: object, **kwargs: object) -> None:
        real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SeqNativeRun, "native_column", discard)
    out = run_seq(payload_shape()).outputs["P"]
    assert len(kernel.calls) == len(_ADMITTED)
    emitted = out.column("f_digits").to_pylist()
    assert not any(isinstance(v, str) and v.startswith(MARK) for v in emitted)


def test_native_ciphertext_matches_python_ciphertext_through_the_kernel_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Config parity: every setting that decides the ciphertext reaches the kernel.
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    run_seq(payload_shape())
    by_column = {c.tweak_column: c for c in kernel.calls}
    digits = by_column["f_digits"]
    assert digits.namespace == "ns_fpe" and digits.mask_key == JOB_SEED
    assert digits.config == FpeConfig()
    assert digits.arrow_type == pa.string() and digits.rows == len(DIGITS)
    assert by_column["f_alnum"].config == FpeConfig(charset="alphanum")
    assert by_column["f_nosep"].config == FpeConfig(preserve_separators=False)
    assert by_column["f_luhn"].config == FpeConfig(validate_luhn=True)
    # The join group is the tweak identity inside the kernel; the column name is what we pass.
    assert by_column["f_group"].config == FpeConfig(join_group="grp")


def test_disposition_reports_every_native_fpe_column_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    result = run_seq(payload_shape())
    entries = result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]
    assert [e["column"] for e in entries] == _ADMITTED
    assert all(
        e["table"] == "P"
        and e["path"] == "scalar"
        and e["executed_backend"] == "rust_companion"
        and "reason" not in e
        for e in entries
    )


def test_the_hash_disposition_is_byte_identical_with_and_without_fpe_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The regression pin for the generalization: adding native fpe columns to a job changes
    # nothing in the `sequential_hash` fragment.
    hide_fpe(monkeypatch)
    with_fpe = run_seq(payload_shape()).quality_metrics[SEQ_HASH_DISPOSITION_KEY]
    alone = run_seq(payload_shape({})).quality_metrics[SEQ_HASH_DISPOSITION_KEY]
    assert repr(with_fpe) == repr(alone)
    assert SEQ_FPE_DISPOSITION_KEY in run_seq(payload_shape()).quality_metrics


def test_a_job_without_fpe_has_no_fpe_disposition_key() -> None:
    result = run_seq(payload_shape({}))
    assert SEQ_FPE_DISPOSITION_KEY not in result.quality_metrics


# Config-aware decline.


def test_a_checksum_column_declines_before_the_type_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    cols = {
        "chk": (strs(["4539578763621486"] * 7), fpe_seed(checksum="luhn")),
        # A checksum column over a non-string source reports the checksum reason, not the type.
        "chk_large": (
            pa.array(["4539578763621486"] * 7, pa.large_string()),
            fpe_seed(checksum="luhn"),
        ),
    }
    result = run_seq(payload_shape(cols))
    by_column = {e["column"]: e for e in result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]}
    for name in cols:
        assert by_column[name]["executed_backend"] == "python_scalar"
        assert by_column[name]["reason"] == f"fpe_checksum_not_native:{name}"
    assert kernel.calls == []  # neither reached the kernel
    assert (
        run_seq(payload_shape(cols)).outputs["P"].equals(run_full(payload_shape(cols)).outputs["P"])
    )


@pytest.mark.parametrize("scheme", ["luhn", "npi", "iban", "vin", "isbn13", "ean13", "gtin"])
def test_every_truthy_checksum_scheme_declines_at_admission(scheme: str) -> None:
    from decoy_engine.execution._sequential_native_ops import fpe_op

    admit = fpe_op(lambda _threads: None).admit
    assert admit("c", pa.string(), fpe_seed(checksum=scheme)) == "fpe_checksum_not_native:c"
    assert admit("c", pa.large_string(), fpe_seed(checksum=scheme)) == "fpe_checksum_not_native:c"
    assert admit("c", pa.string(), fpe_seed()) is None


def test_a_large_string_column_declines_with_a_value_free_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    cols = {"big": (pa.array(DIGITS, pa.large_string()), fpe_seed())}
    result = run_seq(payload_shape(cols))
    [entry] = result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]
    assert entry["executed_backend"] == "python_scalar"
    assert entry["reason"] == "fpe_source_type_not_string:big:large_string"
    assert kernel.calls == []
    text = repr(result.quality_metrics)
    assert "1234567890" not in text and "9999999999" not in text


def test_a_config_the_handler_would_raise_on_stays_python_and_the_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    dup = payload_shape({"d": (strs(DIGITS), fpe_seed(charset="0123456789a0"))})
    with pytest.raises(StrategyError) as exc:
        run_seq(dup)
    assert exc.value.code == "fpe_charset_duplicate_symbols"
    no_ns = payload_shape({"n": (strs(DIGITS), fpe_seed(namespace=None))})
    with pytest.raises(StrategyError) as exc:
        run_seq(no_ns)
    assert exc.value.code == "fpe_requires_namespace"
    assert kernel.calls == []


def test_companion_absent_runs_the_python_handler_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_fpe(monkeypatch)
    shape = payload_shape()
    result = run_seq(shape)
    entries = result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]
    assert {e["executed_backend"] for e in entries} == {"python_scalar"}
    assert {e["reason"] for e in entries} == {"crypto_extension_unavailable"}
    oracle = run_full(shape)
    assert result.outputs["P"].equals(oracle.outputs["P"])
    assert warning_view(result.warnings) == warning_view(oracle.warnings)


def test_a_kernel_fault_aborts_the_run_without_a_python_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel(fail_on_call=2))
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_seq(payload_shape(), sink=sink)
    assert sink.commits == 0 and sink.committed == {}


def test_a_malformed_kernel_result_aborts_with_a_coded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Short(FpeMarkingKernel):
        def run(self, values: pa.Array, **kwargs: object) -> object:
            result = super().run(values, **kwargs)  # type: ignore[arg-type]
            return type(result)(values=result.values.slice(1), errors=(), warnings=())

    install_fpe(monkeypatch, Short())
    with pytest.raises(ExecutionError) as exc:
        run_seq(payload_shape())
    assert exc.value.code == "sequential_fpe_native_result_invalid"


# Per-row kernel errors fail the run closed.


def _error_shape(cells: list[str | None], **config: object) -> object:
    return payload_shape({"bad": (strs(cells), fpe_seed(**config))})


_GOOD = "1234567890"
_ERROR_CASES = [
    ({"preserve_separators": False}, "12ab34", "fpe_unencryptable_value"),
    ({}, "123", "fpe_unencryptable_domain"),
    ({}, "1" * 300, "fpe_unencryptable_length"),
]


@pytest.mark.parametrize(("config", "cell", "code"), _ERROR_CASES)
def test_a_row_error_raises_the_coded_error_and_publishes_nothing(
    config: dict[str, object], cell: str, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    cells = [_GOOD, cell, _GOOD, None, _GOOD, _GOOD, _GOOD]
    shape = _error_shape(cells, **config)
    with pytest.raises(StrategyError) as native:
        run_seq(shape)
    with pytest.raises(StrategyError) as python:
        run_full(shape)
    assert native.value.code == python.value.code == code
    assert native.value.strategy == "fpe"
    assert cell not in str(native.value)  # no cell value in the message

    sink = RecordingSink()
    with pytest.raises(StrategyError):
        run_seq(shape, sink=sink)
    assert sink.commits == 0 and sink.committed == {} and "P" not in sink.staged
    published: dict[str, pa.Table] = {}
    with pytest.raises(StrategyError):
        run_seq(shape, sink=callable_sink(published))
    assert "P" not in published


def test_the_lowest_failing_row_decides_the_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    for first, second, code in (
        ("123", "1" * 300, "fpe_unencryptable_domain"),
        ("1" * 300, "123", "fpe_unencryptable_length"),
    ):
        shape = _error_shape([_GOOD, first, _GOOD, second, _GOOD, _GOOD, _GOOD])
        with pytest.raises(StrategyError) as exc:
            run_seq(shape)
        assert exc.value.code == code


def test_a_row_error_under_a_when_subset_still_fails_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    cells = [_GOOD, "123", _GOOD, None, _GOOD, _GOOD, _GOOD]
    shape = payload_shape(
        {"bad": (strs(cells), fpe_seed())},
        extra_cols={"flag": pa.array([1, 1, 0, 0, 1, 1, 1], pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"bad": "flag == 1"},
    )
    with pytest.raises(StrategyError) as exc:
        run_seq(shape)
    assert exc.value.code == "fpe_unencryptable_domain"


# FK key: an fpe key column is native (hash and fpe are the injective key strategies).


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_fpe_key_columns_get_a_native_decision_and_match_the_python_oracle(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    hash_kernel = install_hash(monkeypatch, MarkingKernel())
    shape = fpe_key_shape(policy)
    result = run_seq(shape)
    oracle = run_full(shape)
    for table in ("A", "B", "C"):
        # The oracle's value, with the stand-in's mark on every cell the kernel derived.
        assert result.outputs[table].to_pydict() == fpe_marked_expected(
            oracle.outputs[table], shape.sources[table], {"k"}
        ), table
    assert hash_kernel.calls == []
    # A scalar derives A.k; REMAP adds one call per hop's orphan. B.k is a chain middle, so it
    # is never rederived as a scalar.
    assert sorted(c.rows for c in kernel.calls) == (
        [1, 1, 2] if policy is OrphanPolicy.REMAP else [2]
    )
    assert SEQ_HASH_DISPOSITION_KEY not in result.quality_metrics
    paths = {
        (e["table"], e["column"], e["path"])
        for e in result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]
    }
    remap = (
        {("A", "k", "fk_remap"), ("B", "k", "fk_remap")} if policy is OrphanPolicy.REMAP else set()
    )
    assert (
        paths
        == {("A", "k", "scalar"), ("B", "k", "fk_inherited"), ("C", "k", "fk_inherited")} | remap
    )


def test_an_fpe_key_does_not_disturb_a_native_hash_payload_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    result = run_seq(fpe_key_shape(OrphanPolicy.REMAP, hash_payload=True))
    assert [e["column"] for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]] == ["h"]
    assert {e["column"] for e in result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]} == {"k"}


def test_a_non_hash_strategy_inside_a_remap_scope_returns_to_python_untouched() -> None:
    run = SeqNativeRun()
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", "t")
    object.__setattr__(ctx, "nested_outer_column", "")
    from decoy_engine.execution._sequential_native import ColumnDecision

    decision = ColumnDecision("t", "x", "rust_companion", None, pa.string(), strategy="fpe")
    run._decisions[("t", "x")] = decision
    run._bound["fpe"] = object()
    df = pd.DataFrame({"x": ["1234567890"]})
    with run.remap_scope():
        assert run.native_column(ctx, df, "x", "fpe", object()) is None
    assert decision.rows == 0
    assert run._remap == {} and run.metrics()["sequential_fpe"][0]["executed_backend"] == "none"


def test_a_decision_is_never_consumed_by_another_strategys_handler() -> None:
    run = SeqNativeRun()
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", "t")
    object.__setattr__(ctx, "nested_outer_column", "")
    from decoy_engine.execution._sequential_native import ColumnDecision

    run._decisions[("t", "x")] = ColumnDecision(
        "t", "x", "rust_companion", None, pa.string(), strategy="fpe"
    )
    assert run.native_column(ctx, pd.DataFrame({"x": ["a"]}), "x", "hash", object()) is None


# Security warnings survive the native path.


def test_the_residual_risk_warnings_match_the_python_path_in_content_count_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = payload_shape()
    oracle = run_full(shape)
    install_fpe(monkeypatch, FpeMarkingKernel())
    result = run_seq(shape)
    codes = [w.code for w in result.warnings]
    assert "fpe_partial_plaintext_disclosure" in codes and "fpe_join_group_active" in codes
    assert warning_view(result.warnings) == warning_view(oracle.warnings)
    [partial] = [
        w
        for w in result.warnings
        if w.code == "fpe_partial_plaintext_disclosure" and w.column == "f_partial"
    ]
    assert partial.detail["affected_values"] == 4


def test_warnings_use_the_when_subset_only(monkeypatch: pytest.MonkeyPatch) -> None:
    shape = payload_shape(
        {"f_partial": (strs(["M10000000", "M20000001", "X30000002", "10000000"] * 2), fpe_seed())},
        extra_cols={"flag": pa.array([1, 0, 0, 1, 0, 0, 1, 1], pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"f_partial": "flag == 1"},
    )
    oracle = run_full(shape)
    install_fpe(monkeypatch, FpeMarkingKernel())
    result = run_seq(shape)
    assert warning_view(result.warnings) == warning_view(oracle.warnings)
    [partial] = [w for w in result.warnings if w.code == "fpe_partial_plaintext_disclosure"]
    assert partial.detail["total_values"] == 4  # four selected rows, not eight


def test_a_when_subset_with_a_gapped_index_lands_on_the_right_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flags = [0, 1, 0, 1, 1, 0, 1]
    shape = payload_shape(
        {"c": (strs(DIGITS), fpe_seed())},
        extra_cols={"flag": pa.array(flags, pa.int64())},
        extra_seeds={"flag": column_seed("passthrough", namespace=None)},
        when={"c": "flag == 1"},
    )
    oracle = run_full(shape).outputs["P"].column("c").to_pylist()
    install_fpe(monkeypatch, FpeMarkingKernel())
    emitted = run_seq(shape).outputs["P"].column("c").to_pylist()
    for i, flag in enumerate(flags):
        if flag:
            assert_marked_like([emitted[i]], [oracle[i]])
        else:
            assert emitted[i] == DIGITS[i], i  # untouched


# Mixed strategies and per-strategy kernel isolation.


def _mixed_shape(fpe_first: bool) -> object:
    cols = {"f": (strs(DIGITS), fpe_seed())}
    hashed_col = {"h": (strs(["a", "b", "c", "d", "e", "f", "g"]), hashed("ns_h"))}
    return payload_shape({**cols, **hashed_col} if fpe_first else {**hashed_col, **cols})


@pytest.mark.parametrize("fpe_first", [True, False])
def test_a_table_with_a_hash_and_an_fpe_column_masks_both_natively(
    fpe_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fpe = install_fpe(monkeypatch, FpeMarkingKernel())
    hash_kernel = install_hash(monkeypatch, MarkingKernel())
    shape = _mixed_shape(fpe_first)
    result = run_seq(shape)
    out = result.outputs["P"]
    assert all(v.startswith(MARK) for v in out.column("h").to_pylist() if v)
    assert next(v for v in out.column("f").to_pylist() if v).startswith(MARK)
    assert len(fpe.calls) == 1 and len(hash_kernel.calls) == 2  # h and hk
    assert [e["column"] for e in result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]] == ["f"]
    assert {e["column"] for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]} == {
        "h",
        "hk",
        "ck",
    }


@pytest.mark.parametrize("fpe_first", [True, False])
def test_fpe_companion_absent_degrades_only_fpe(
    fpe_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    hide_fpe(monkeypatch)
    install_hash(monkeypatch, MarkingKernel())
    result = run_seq(_mixed_shape(fpe_first))
    [fpe_entry] = result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]
    assert fpe_entry["executed_backend"] == "python_scalar"
    hash_scalar = [
        e for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY] if e["path"] == "scalar"
    ]
    assert {e["executed_backend"] for e in hash_scalar} == {"rust_companion"}


@pytest.mark.parametrize("fpe_first", [True, False])
def test_hash_companion_absent_degrades_only_hash(
    fpe_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.parity._seq_witness import hide_companion

    hide_companion(monkeypatch)
    install_fpe(monkeypatch, FpeMarkingKernel())
    result = run_seq(_mixed_shape(fpe_first))
    [fpe_entry] = result.quality_metrics[SEQ_FPE_DISPOSITION_KEY]
    assert fpe_entry["executed_backend"] == "rust_companion"
    hash_scalar = [
        e for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY] if e["path"] == "scalar"
    ]
    assert {e["executed_backend"] for e in hash_scalar} == {"python_scalar"}


def test_each_strategy_loads_its_kernel_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    loads = {"fpe": 0, "hash": 0}
    fpe_kernel, hash_kernel = FpeMarkingKernel(), MarkingKernel()

    def load_fpe() -> FpeMarkingKernel:
        loads["fpe"] += 1
        return fpe_kernel

    def load_hash() -> MarkingKernel:
        loads["hash"] += 1
        return hash_kernel

    monkeypatch.setattr(_sequential_native, "load_compiled_fpe_kernel", load_fpe)
    monkeypatch.setattr(_sequential_native, "load_compiled_crypto_kernel", load_hash)
    run_seq(payload_shape(extra_seeds={"h": hashed("ns_h")}, extra_cols={"h": strs(HK)}))
    assert loads == {"fpe": 1, "hash": 1}


# A strategy with no descriptor keeps running in Python.


def test_a_strategy_with_no_descriptor_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    install_hash(monkeypatch, MarkingKernel())
    extra = {
        "r": column_seed("redact", namespace=None),
        "t": column_seed("truncate", namespace=None, provider_config=(("length", 3),)),
    }
    shape = payload_shape(
        {}, extra_seeds=extra, extra_cols={"r": strs(HK), "t": strs(["abcdef"] * 7)}
    )
    result = run_seq(shape)
    oracle = run_full(shape)
    assert (
        result.outputs["P"].column("r").to_pylist() == oracle.outputs["P"].column("r").to_pylist()
    )
    assert (
        result.outputs["P"].column("t").to_pylist() == oracle.outputs["P"].column("t").to_pylist()
    )
    assert kernel.calls == []
    assert SEQ_FPE_DISPOSITION_KEY not in result.quality_metrics


def test_reason_codes_carry_no_cell_or_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    cols = {
        "big": (pa.array(DIGITS, pa.large_string()), fpe_seed()),
        "chk": (strs(["4539578763621486"] * 7), fpe_seed(checksum="luhn")),
    }
    text = repr(run_seq(payload_shape(cols)).quality_metrics)
    for needle in ("4539578763621486", "1234567890", "0000000001"):
        assert needle not in text
