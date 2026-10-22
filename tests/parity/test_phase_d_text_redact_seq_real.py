"""text_redact SEQ-native on the REAL compiled span kernel, against the Python handler.

Needs the companion with the text_redact span kernel (skipped without it). Every masking cell
runs the full-frame oracle, the SEQ route with the kernel, and the SEQ route with the kernel
hidden, and asserts all nine strict dimensions between each pair. The native witness is the
`sequential_text_redact` entry; the config-transport cells (T8) additionally spy on the
compiled candidate call, because the `native_calls` counter cannot tell a call that received
the wrong detector list from one that received the right one.

Acceptance T1 to T3, T5 to T8 and the payload half of T_key of
docs/plans/2026-10-14-c-text-redact-seq-native.md. Declines, handler config errors, routing and
the decline half of T_key are in `test_phase_d_text_redact_seq.py`.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._sequential_native_ops import (
    SEQ_FAKER_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    SEQ_TEXT_REDACT_DISPOSITION_KEY,
)
from decoy_engine.execution.native._text_redact_kernel import SUPPORTED_IDS
from decoy_engine.relationships._graph import OrphanPolicy
from tests.native._c6c_ii_support import (
    CORPUS,
    DETECTOR_SELECTIONS,
    ELIGIBLE_TEXTS,
    INELIGIBLE_TEXTS,
    SHAPES,
)
from tests.parity import _d2b_shapes as s
from tests.parity._d2b_shapes import hashed, make_plan, run_full, run_seq, table_seed
from tests.parity._d2c_cat_data import payload_shape
from tests.parity._faker_seq_data import FIRST, faker_seed
from tests.parity._text_redact_seq_data import (
    PASSTHROUGH,
    UNAVAILABLE,
    assert_native_witness,
    assert_python_ran,
    assert_trio_strict,
    kernel_loads,
    run_trio,
    spy_candidates,
    strs,
    tr_entry,
    tr_seed,
)

pytestmark = pytest.mark.skipif(not kernel_loads(), reason="needs the text_redact span kernel")

PII = ["mail a@b.com now", "ssn 123-45-6789 ok", "call 212-555-1234", "clean text", None]


def _shape(values: list[str | None], **config: Any) -> s.Shape:
    return payload_shape({"t": (strs(values), tr_seed(**config))})


def _cell(shape: s.Shape, monkeypatch: pytest.MonkeyPatch, *, calls: int = 1) -> Any:
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    assert_native_witness(trio.native, "t", calls=calls)
    assert_python_ran(trio.python, "t", UNAVAILABLE)
    return trio


def _selection(name: str) -> dict[str, Any]:
    ids = DETECTOR_SELECTIONS[name]
    return {} if ids is None else {"detectors": list(ids)}


# T1: Rust-eligible detectors on an ASCII payload.


@pytest.mark.parametrize("name", ["all", "email_only", "rust_subset", "tie_npi_then_phone"])
def test_t1_rust_eligible_detectors_match_the_oracle(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    trio = _cell(_shape(list(ELIGIBLE_TEXTS), **_selection(name)), monkeypatch)
    out = trio.native.outputs["P"].column("t").to_pylist()
    assert out != list(ELIGIBLE_TEXTS)  # the witness is not vacuous: something was redacted


@pytest.mark.parametrize("n", [1, 5, 257, 5000])
def test_t1_across_row_counts(n: int, monkeypatch: pytest.MonkeyPatch) -> None:
    values = [None if i % 11 == 4 else ELIGIBLE_TEXTS[i % len(ELIGIBLE_TEXTS)] for i in range(n)]
    _cell(_shape(values), monkeypatch)


# T2: lookaround detectors now run in the Rust kernel (ported); output still matches the oracle.


@pytest.mark.parametrize("name", ["python_only", "mixed", "dup_email", "unknown_id"])
def test_t2_lookaround_and_odd_selections_match_the_oracle(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cell(_shape(list(ELIGIBLE_TEXTS), **_selection(name)), monkeypatch)


# T3: non-ASCII cells mixed with ASCII ones split per cell.


@pytest.mark.parametrize("name", ["all", "mixed", "rust_subset"])
def test_t3_mixed_ascii_and_non_ascii_match_the_oracle(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cell(_shape(list(CORPUS), **_selection(name)), monkeypatch)


def test_t3_a_column_of_only_non_ascii_cells_still_runs_the_native_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cell(_shape(list(INELIGIBLE_TEXTS)), monkeypatch)


# T5: nulls, empty and all-null evidence.


def test_t5_nulls_in_null_out(monkeypatch: pytest.MonkeyPatch) -> None:
    trio = _cell(_shape(list(SHAPES["ragged"])), monkeypatch)
    out = trio.native.outputs["P"].column("t").to_pylist()
    src = SHAPES["ragged"]
    assert [v is None for v in out] == [v is None for v in src]


def test_t5_all_null_column_books_exactly_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    trio = _cell(_shape([None] * 9), monkeypatch)
    assert trio.native.outputs["P"].column("t").to_pylist() == [None] * 9


def test_t5_empty_table_books_zero_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    shape = payload_shape({"t": (pa.array([], pa.string()), tr_seed())})
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    entry = tr_entry(trio.native, "t")
    assert entry["native_calls"] == 0
    assert entry["executed_backend"] == "none"
    assert entry["reason"] == "no_rows_hashed"


# T6: a mixed job reports each strategy under its own key.


def test_t6_text_redact_beside_hash_and_faker_keeps_each_fragment_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = {
        "t": (strs(list(ELIGIBLE_TEXTS)), tr_seed()),
        "f": (strs(list(ELIGIBLE_TEXTS)), faker_seed(provider=FIRST)),
    }
    trio = run_trio(payload_shape(parts), monkeypatch)
    assert_trio_strict(trio)
    metrics = trio.native.quality_metrics
    assert [e["column"] for e in metrics[SEQ_TEXT_REDACT_DISPOSITION_KEY]] == ["t"]
    assert [e["column"] for e in metrics[SEQ_FAKER_DISPOSITION_KEY]] == ["f"]
    assert SEQ_HASH_DISPOSITION_KEY in metrics
    assert_native_witness(trio.native, "t")
    only_faker = run_seq(payload_shape({"f": parts["f"]})).quality_metrics
    assert repr(metrics[SEQ_FAKER_DISPOSITION_KEY]) == repr(only_faker[SEQ_FAKER_DISPOSITION_KEY])
    assert SEQ_TEXT_REDACT_DISPOSITION_KEY not in only_faker


# T7: deterministic, no draw.


def test_t7_the_same_job_twice_is_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    shape = _shape(list(CORPUS))
    first = run_seq(shape).outputs["P"]
    second = run_seq(shape).outputs["P"]
    assert first.equals(second)


# T8: config transport, with a spy on the compiled candidate call.


def _spied(shape: s.Shape, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[Any]]:
    spy = spy_candidates(monkeypatch)
    result = run_seq(shape)
    return result, spy.calls


@pytest.mark.parametrize(
    "config",
    [{}, {"detectors": None}, {"detectors": []}],
    ids=["absent", "null", "empty-list"],
)
def test_t8_an_absent_null_or_empty_selection_masks_all_not_nothing(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _shape(PII, **config)
    _cell(shape, monkeypatch)
    result, calls = _spied(shape, monkeypatch)
    # The kernel received every supported detector. `()` would send none and mask nothing.
    assert calls == [(len(PII), tuple(SUPPORTED_IDS))]
    out = result.outputs["P"].column("t").to_pylist()
    assert out[0] == "mail [REDACTED] now"
    assert out[1] == "ssn [REDACTED] ok"
    assert out[2] == "call [REDACTED]"
    assert out[3] == "clean text" and out[4] is None


def test_t8_a_detector_subset_masks_only_those(monkeypatch: pytest.MonkeyPatch) -> None:
    shape = _shape(PII, detectors=["email"])
    _cell(shape, monkeypatch)
    result, calls = _spied(shape, monkeypatch)
    assert calls == [(len(PII), ("email",))]
    out = result.outputs["P"].column("t").to_pylist()
    assert out[0] == "mail [REDACTED] now"
    assert out[1] == PII[1] and out[2] == PII[2]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"token": "<X>"}, "mail <X> now"),
        ({"token": ""}, "mail  now"),
        ({"label_token": True}, "mail [REDACTED:email] now"),
        ({"label_token": True, "token": "<X>"}, "mail [REDACTED:email] now"),
    ],
    ids=["custom-token", "empty-token", "label-token", "label-beats-token"],
)
def test_t8_token_and_label_token_reach_the_splice(
    config: dict[str, Any], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _shape(PII, **config)
    trio = _cell(shape, monkeypatch)
    assert trio.native.outputs["P"].column("t").to_pylist()[0] == expected


def test_t8_a_when_gated_selection_with_a_gapped_index_matches_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = [PII[i % 4] for i in range(12)]
    flag = [1, 0, 0, 1, 1, 0, 1, 0, 0, 0, 1, 1]
    shape = payload_shape(
        {"t": (strs(values), tr_seed())},
        extra_cols={"flag": pa.array(flag, pa.int64())},
        extra_seeds={"flag": PASSTHROUGH},
        when={"t": "flag == 1"},
    )
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    assert_native_witness(trio.native, "t")
    out = trio.native.outputs["P"].column("t").to_pylist()
    for i, (v, f) in enumerate(zip(values, flag, strict=True)):
        if f == 0:
            assert out[i] == v  # an unselected row is untouched
    assert out != values  # and the selected ones were masked


def test_t8_a_zero_match_column_is_unchanged_but_the_kernel_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean = ["nothing here", "still clean", None, "plain words"]
    shape = _shape(clean)
    trio = _cell(shape, monkeypatch)
    assert trio.native.outputs["P"].column("t").to_pylist() == clean
    _, calls = _spied(shape, monkeypatch)
    assert len(calls) == 1 and calls[0][0] == len(clean)


# T_key (payload half): a text_redact payload stays native beside text_redact FK keys.


def _key_shape(policy: OrphanPolicy) -> s.Shape:
    a = {"k": tr_seed(), "p": tr_seed(), "h": hashed("ns_h")}
    plan = make_plan(
        {
            "A": table_seed(a),
            "B": table_seed({"k": tr_seed()}),
            "C": table_seed({"k": tr_seed()}),
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
def test_t_key_a_text_redact_payload_beside_text_redact_keys_stays_native(
    policy: OrphanPolicy,
) -> None:
    shape = _key_shape(policy)
    result = run_seq(shape)
    oracle = run_full(shape)
    for table in ("A", "B", "C"):
        assert result.outputs[table].equals(oracle.outputs[table]), table
    entries = result.quality_metrics[SEQ_TEXT_REDACT_DISPOSITION_KEY]
    by_path = {(e["column"], e["path"]): e["executed_backend"] for e in entries}
    assert by_path[("p", "scalar")] == "rust_companion"
    assert {e["path"] for e in entries if e["column"] == "k"} == {"fk_key_declined"}


def test_the_hidden_kernel_run_is_the_python_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    trio = run_trio(_shape(PII), monkeypatch)
    assert_python_ran(trio.python, "t", UNAVAILABLE)
    assert_trio_strict(trio)
