"""C6b-ii slice a: text_mask EXECUTES on the sequential (SEQ) native route.

Registering the operator is not enough: `prepare_table` only records the decision, and the
`TextMaskHandler` seam call is what runs it. Each cell runs the full-frame oracle, the SEQ route
with the span kernel loaded, and the SEQ route with it hidden, and asserts all nine strict
dimensions between each pair (warnings included). The native witness is the `sequential_text_mask`
evidence entry plus a spy proving the pandas handler's per-cell loop did NOT run.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native, _sequential_native_text_mask
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._sequential_native_text_mask import (
    SEQ_TEXT_MASK_DISPOSITION_KEY,
    TEXT_MASK,
)
from decoy_engine.execution._strategies import _text_mask as handler_mod
from decoy_engine.execution.native._text_redact_kernel import SUPPORTED_IDS
from decoy_engine.relationships._graph import OrphanPolicy
from tests.native._c6b_i_support import CORPUS, SUB_FLOOR_TEXTS
from tests.parity import _d2b_shapes as s
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import hashed, make_plan, run_full, run_seq, table_seed
from tests.parity._d2c_cat_data import payload_shape
from tests.parity._fpe_key_data import assert_keys_declined
from tests.parity._text_redact_seq_data import (
    UNAVAILABLE,
    assert_trio_strict,
    kernel_loads,
    run_trio,
    spy_candidates,
    strs,
)

pytestmark = pytest.mark.skipif(not kernel_loads(), reason="needs the text_redact span kernel")

PII = ["mail a@b.com now", "ssn 123-45-6789 ok", "call 212-555-1234", "clean text", None]


def tm_seed(**config: Any) -> Any:
    return column_seed("text_mask", namespace=None, provider_config=tuple(config.items()))


def _shape(values: list[str | None], **config: Any) -> s.Shape:
    return payload_shape({"t": (strs(values), tm_seed(**config))})


def _entry(result: Any, column: str) -> dict[str, Any]:
    entries = result.quality_metrics.get(SEQ_TEXT_MASK_DISPOSITION_KEY, [])
    [entry] = [e for e in entries if e["column"] == column and e["path"] == "scalar"]
    return entry


def _assert_native(result: Any, column: str = "t", *, calls: int = 1) -> None:
    entry = _entry(result, column)
    assert entry["executed_backend"] == "rust_companion", entry
    assert "reason" not in entry, entry
    assert entry["native_calls"] == calls, entry


def _forbid_handler_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("the pandas TextMaskHandler loop ran; the native op did not")

    monkeypatch.setattr(handler_mod, "mask_cell", boom)


def test_a4_the_native_op_runs_and_the_pandas_loop_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _shape(list(CORPUS))
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    _assert_native(trio.native)
    python = _entry(trio.python, "t")
    assert python["executed_backend"] == "python_scalar" and python["reason"] == UNAVAILABLE
    assert python["native_calls"] == 0
    out = trio.native.outputs["P"].column("t").to_pylist()
    assert out != list(CORPUS)  # not vacuous: something was masked
    with monkeypatch.context() as m:
        _forbid_handler_loop(m)
        spy = spy_candidates(m)
        again = run_seq(shape)
    assert again.outputs["P"].equals(trio.native.outputs["P"])
    assert len(spy.calls) == 1  # exactly one batch call for the column


def test_a4_the_disposition_key_reports_text_mask_alone() -> None:
    result = run_seq(_shape(PII))
    assert SEQ_TEXT_MASK_DISPOSITION_KEY == "sequential_text_mask"
    assert [e["column"] for e in result.quality_metrics[SEQ_TEXT_MASK_DISPOSITION_KEY]] == ["t"]


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"detectors": None},
        {"detectors": []},
        {"detectors": ["email"]},
        {"unmatched_span_policy": "passthrough"},
        {"unmatched_span_policy": "replace_with_token", "token": "<X>"},
        {"per_detector_strategy": {"ssn": "faker", "email": "faker"}},
        {
            "per_detector_strategy": {"ssn": "date_shift"},
            "min_days": -10,
            "max_days": 10,
        },
    ],
    ids=[
        "default",
        "null",
        "empty-list",
        "subset",
        "passthrough",
        "replace",
        "faker",
        "date_shift",
    ],
)
def test_a4_configs_match_the_oracle_on_the_seq_route(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    trio = run_trio(_shape(list(CORPUS), **config), monkeypatch)
    assert_trio_strict(trio)
    _assert_native(trio.native)


@pytest.mark.parametrize("config", [{}, {"detectors": None}, {"detectors": []}])
def test_a4_an_absent_null_or_empty_selection_sends_every_detector(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`()` would send zero detectors and mask nothing; the handler resolves to None first."""
    spy = spy_candidates(monkeypatch)
    result = run_seq(_shape(PII, **config))
    assert spy.calls == [(len(PII), tuple(SUPPORTED_IDS))]
    out = result.outputs["P"].column("t").to_pylist()
    assert "a@b.com" not in out[0]


def test_a4_sub_floor_warning_comes_from_transported_counts_without_a_remask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _shape(list(SUB_FLOOR_TEXTS), sub_floor_span="redact")
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)  # includes warnings == oracle warnings, field for field
    codes = [w.code for w in trio.oracle.warnings]
    assert "text_mask_sub_floor_span_handled" in codes
    _assert_native(trio.native)
    calls = 0
    real = _sequential_native_text_mask.native_text_mask

    def counting(*a: Any, **k: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(_sequential_native_text_mask, "native_text_mask", counting)
        _forbid_handler_loop(m)
        result = run_seq(shape)
    assert calls == 1  # the warning was built from the counts, not by masking again
    assert result.warnings == trio.oracle.warnings


def test_a4_fail_closed_raises_the_handlers_strategy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _shape(list(SUB_FLOOR_TEXTS))  # no sub_floor_span policy
    with pytest.raises(StrategyError) as oracle:
        run_full(shape)
    with pytest.raises(StrategyError) as native:
        run_seq(shape)
    assert native.value.code == oracle.value.code == "fpe_unencryptable_domain"
    assert native.value.strategy == "text_mask"
    assert str(native.value) == str(oracle.value)
    # Raised by the native op: with the pandas loop forbidden, the same error still surfaces.
    _forbid_handler_loop(monkeypatch)
    with pytest.raises(StrategyError) as again:
        run_seq(shape)
    assert str(again.value) == str(oracle.value)


def _key_shape(policy: OrphanPolicy) -> s.Shape:
    a = {"k": tm_seed(), "p": tm_seed(), "h": hashed("ns_h")}
    plan = make_plan(
        {
            "A": table_seed(a),
            "B": table_seed({"k": tm_seed()}),
            "C": table_seed({"k": tm_seed()}),
        }
    )
    return s.Shape(
        plan,
        s.graph(
            s.edge("A", "k", "B", "k", "ns_ab", policy),
            s.edge("B", "k", "C", "k", "ns_bc", policy),
        ),
        {
            "A": pa.table(
                {
                    "k": strs(["alpha a@b.com", "beta"]),
                    "p": strs(["x 123-45-6789", "y"]),
                    "h": strs(["u", "v"]),
                }
            ),
            "B": pa.table({"k": strs(["alpha a@b.com", "beta", "gamma"])}),
            "C": pa.table({"k": strs(["alpha a@b.com", "beta", "gamma", "delta"])}),
        },
    )


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_a4_text_mask_keys_decline_and_the_payload_stays_native(policy: OrphanPolicy) -> None:
    shape = _key_shape(policy)
    result = run_seq(shape)
    oracle = run_full(shape)
    for table in ("A", "B", "C"):
        assert result.outputs[table].equals(oracle.outputs[table]), table
    entries = result.quality_metrics[SEQ_TEXT_MASK_DISPOSITION_KEY]
    by_path = {(e["column"], e["path"]): e["executed_backend"] for e in entries}
    assert by_path[("p", "scalar")] == "rust_companion"
    declined = [e for e in entries if e["path"] == "fk_key_declined"]
    assert {e["column"] for e in declined} == {"k"}
    assert all(
        e["reason"].startswith(f"{_sequential_native.REASON_KEY_NOT_INJECTIVE}:text_mask:")
        for e in declined
    )


def test_a4_a_key_only_job_reports_the_reason_exactly() -> None:
    plan = make_plan({"A": table_seed({"k": tm_seed()}), "B": table_seed({"k": tm_seed()})})
    shape = s.Shape(
        plan,
        s.graph(s.edge("A", "k", "B", "k", "ns_ab", OrphanPolicy.PRESERVE)),
        {"A": pa.table({"k": strs(["a", "b"])}), "B": pa.table({"k": strs(["a", "b", "c"])})},
    )
    assert_keys_declined(run_seq(shape), shape, "text_mask", SEQ_TEXT_MASK_DISPOSITION_KEY)


def test_a4_text_mask_is_not_a_key_strategy() -> None:
    assert TEXT_MASK not in _sequential_native._KEY_STRATEGIES
