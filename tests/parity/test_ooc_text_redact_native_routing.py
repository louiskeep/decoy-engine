"""Out-of-core text_redact native slice: routing, config transport, declines, pinning, faults.

Runs on any box. The companion-presence gate is stubbed and the kernel call goes through a
marking wrapper (see `_ooc_text_redact_data`), so what is asserted is the route: which backend a
column takes, that the native result is the array the batch carries, that the resolved config
(above all the None-means-all detector selection) is what reaches the kernel, that a decline
reaches the existing Python `text_redact_array` unchanged, that a pinned column aborts rather than
switching path, and that a kernel fault propagates. Byte parity of the real compiled kernel
against the oracle is in `test_ooc_text_redact_native_real.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.detection import detectors as detectors_mod
from decoy_engine.detection import ner as ner_mod
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import _mask
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from decoy_engine.execution.out_of_core._mask_group_b import text_redact_array
from decoy_engine.plan._types import SeedEnvelope, TableSeed
from tests.parity._d1a_hash_data import (
    JOB_SEED,
    chain_plan,
    install_companion,
    run_chain,
)
from tests.parity._d1b2_categorical_data import categorical_seed, install_index_companion
from tests.parity._ooc_fpe_data import fpe_seed, install_fpe_kernel
from tests.parity._ooc_text_redact_data import (
    CLEAN,
    KEY,
    SOURCE,
    UNAVAILABLE,
    TextRedactConsumedWitness,
    forbid_python_text_redact,
    hide_text_redact_companion,
    install_marking,
    kernel_loads,
    replace_with_equal_python_array,
    tr_seed,
)
from tests.parity.test_out_of_core_group_b_parity import (
    _gate_admits,
    _gate_codes,
    _key_strategy_job,
)
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.parity.test_phase_d_d1a_hash_pinning import (
    _DRIVERS,
    RecordingSink,
    _as_large,
    _assert_aborted,
    _without_a,
    mutate_second_batch,
)

MODES = ["resident", "sink-batch", "sink-reorder"]
_SEEDS = (("a", tr_seed()),)
_ROWS = pa.table({"a": pa.array([f"mail p{i}@example.com" for i in range(5)], pa.string())})


def _payload(values: list[Any] = SOURCE) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


def entry(run: Any, column: str = "a") -> dict[str, Any]:
    (found,) = (e for e in run.result.quality_metrics[KEY] if e["column"] == column)
    return found


def reference(values: list[Any], seed: Any = None) -> list[str | None]:
    """The existing Python per-cell loop, the declined path and the byte oracle for the route."""
    seed = seed or tr_seed()
    return text_redact_array(pa.array(values, pa.string()), plan=seed).to_pylist()


def python_run(monkeypatch: pytest.MonkeyPatch, payload: pa.Table, seeds: Any = _SEEDS, **kw: Any):
    """The same job with the companion hidden: the existing Python `text_redact_array` path."""
    hide_text_redact_companion(monkeypatch)
    return run_chain(payload, seeds, **kw)


# --- T1: consumed-output witness and the string-type pin -----------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_native_result_is_the_array_the_batch_carries(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_marking(monkeypatch)
    forbid_python_text_redact(monkeypatch)
    witness = TextRedactConsumedWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    witness.assert_consumed(kernel)
    assert len(kernel.calls) == 4  # 16 rows at 4 per batch; one kernel call per batch
    assert entry(run)["executed_backend"] == RUST_COMPANION
    assert "reason" not in entry(run)
    got = run.tables["t"].column("a").to_pylist()
    assert got == reference(SOURCE)
    assert got != SOURCE  # PII was really redacted
    assert [v is None for v in got] == [v is None for v in SOURCE]
    assert all(g == s for g, s in zip(got, SOURCE, strict=True) if s in CLEAN)


@pytest.mark.parametrize("mode", MODES)
def test_a_discarded_native_result_fails_the_witness(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative control: the kernel runs and its result is then swapped for an equal-valued
    # Python array before the batch leaves mask_batch. Values still match; the witness must not.
    kernel = install_marking(monkeypatch)
    forbid_python_text_redact(monkeypatch)
    replace_with_equal_python_array(monkeypatch)
    witness = TextRedactConsumedWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    assert kernel.calls
    with pytest.raises(AssertionError, match="discarded"):
        witness.assert_consumed(kernel)
    assert run.tables["t"].column("a").to_pylist() == reference(SOURCE)


@pytest.mark.parametrize("mode", MODES)
def test_a_never_called_kernel_cannot_pass_the_witness(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_marking(monkeypatch, gate=False)
    hide_text_redact_companion(monkeypatch)
    witness = TextRedactConsumedWitness()
    with witness.observe():
        run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    assert kernel.calls == []
    with pytest.raises(AssertionError, match="no non-empty native result"):
        witness.assert_consumed(kernel)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "values",
    [
        [None, None, None],
        ["", "", ""],
        [None, "", None, ""],
        ["mail a@b.co", None, "", "no pii"],
        [],
    ],
    ids=["all-null", "all-empty", "null-and-empty", "mixed", "zero-rows"],
)
def test_output_is_string_typed_and_equals_the_python_path(
    values: list[Any], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(values)
    kernel = install_marking(monkeypatch)
    native = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path / "n"))
    assert native.tables["t"].column("a").type == pa.string()
    with pytest.MonkeyPatch.context() as mp:
        py = python_run(mp, payload, batch_rows=2, **mode_kwargs(mode, tmp_path / "p"))
    assert py.tables["t"].column("a").type == pa.string()
    assert native.tables["t"].column("a").to_pylist() == py.tables["t"].column("a").to_pylist()
    for result in kernel.results:
        assert result.type == pa.string()


def test_an_all_null_batch_reports_a_native_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_marking(monkeypatch)
    run = run_chain(_payload([None, None, None]), _SEEDS, batch_rows=2)
    assert len(kernel.calls) == 2
    assert entry(run)["executed_backend"] == RUST_COMPANION


# --- T2: config transport, above all None-means-all-detectors --------------------------------


@pytest.mark.parametrize(
    "config",
    [{}, {"detectors": None}, {"detectors": []}, {"detectors": ()}],
    ids=["absent", "null", "empty-list", "empty-tuple"],
)
@pytest.mark.parametrize("mode", MODES)
def test_an_empty_or_absent_selection_reaches_the_kernel_as_none_and_masks_everything(
    config: dict[str, Any], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_marking(monkeypatch)
    forbid_python_text_redact(monkeypatch)
    seeds = (("a", tr_seed(**config)),)
    run = run_chain(_payload(), seeds, batch_rows=8, **mode_kwargs(mode, tmp_path))
    assert {c.detectors for c in kernel.calls} == {None}  # () here would mean zero detectors
    got = run.tables["t"].column("a").to_pylist()
    assert got == reference(SOURCE)
    # Every cell that holds PII is changed: an empty selection masks all, not nothing.
    assert all(g != s for g, s in zip(got, SOURCE, strict=True) if s not in CLEAN)
    assert "[REDACTED]" in got[0] and "alice@example.com" not in got[0]


@pytest.mark.parametrize(
    ("config", "sent"),
    [
        ({"detectors": ["email"]}, ("email",)),
        ({"detectors": ("email", "ssn")}, ("email", "ssn")),
        ({"detectors": ["us_phone", "email", "url"]}, ("us_phone", "email", "url")),
    ],
    ids=["one", "tuple-with-lookaround", "ordered"],
)
def test_a_detector_subset_is_sent_as_given_and_only_those_are_masked(
    config: dict[str, Any], sent: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_marking(monkeypatch)
    run = run_chain(_payload(), (("a", tr_seed(**config)),), batch_rows=8)
    assert {c.detectors for c in kernel.calls} == {sent}
    got = run.tables["t"].column("a").to_pylist()
    assert got == reference(SOURCE, tr_seed(**config))
    assert "alice@example.com" not in got[0]  # every subset here holds the email detector
    phone_masked = "415-555-0132" not in got[1]
    assert phone_masked == ("us_phone" in sent)  # a detector outside the subset leaves its PII


@pytest.mark.parametrize(
    ("config", "token", "label"),
    [
        ({}, "[REDACTED]", False),
        ({"token": "###"}, "###", False),
        ({"token": ""}, "", False),
        ({"label_token": True}, "[REDACTED]", True),
        ({"token": "X", "label_token": True}, "X", True),
        ({"label_token": 1}, "[REDACTED]", True),
    ],
    ids=["default", "custom", "empty-token", "label", "custom-label", "truthy-label"],
)
def test_token_and_label_token_reach_the_kernel_resolved(
    config: dict[str, Any], token: str, label: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_marking(monkeypatch)
    run = run_chain(_payload(), (("a", tr_seed(**config)),), batch_rows=8)
    assert {(c.token, c.label_token) for c in kernel.calls} == {(token, label)}
    assert run.tables["t"].column("a").to_pylist() == reference(SOURCE, tr_seed(**config))


def test_the_config_is_resolved_once_not_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    install_marking(monkeypatch)
    plan = chain_plan((("a", tr_seed(detectors=["email"], token="#")),))
    table_plan = OocHashRun().prepare_table(plan, "t", _ROWS.schema, frozenset(), (), site="test")
    assert table_plan.text_redact is not None
    decision = table_plan.text_redact.decisions["a"]
    assert (decision.backend, decision.reason) == (RUST_COMPANION, None)
    assert decision.config is not None
    assert (decision.config.detectors, decision.config.token) == (("email",), "#")
    assert decision.pinned_type == pa.string()


# --- T3: declines route to the Python text_redact_array, byte-identical, with a reason -------


def _ner_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(texts: list[str], *, model: str, entities: Any) -> list[list[Any]]:
        return [[detectors_mod.Span("person_name", 0, min(3, len(t)), t[:3])] for t in texts]

    monkeypatch.setattr(ner_mod, "iter_ner_spans_batch", fake)


_PII = _payload(["mail alice@example.com", None, "Bob 415-555-0132", "", "ssn 123-45-6789"])
_DECLINES = {
    "ner": (tr_seed(ner=True), _PII, "text_redact_ner_not_native:a"),
    "ner_mapping": (tr_seed(ner={"model": "m"}), _PII, "text_redact_ner_not_native:a"),
    "token_not_string": (tr_seed(token=5), _PII, "text_redact_token_not_string:a"),
    "token_none": (tr_seed(token=None), _PII, "text_redact_token_not_string:a"),
    "detectors_string": (
        tr_seed(detectors="email"),
        _PII,
        "text_redact_detectors_malformed:a",
    ),
    "detectors_dict": (tr_seed(detectors={"x": 1}), _PII, "text_redact_detectors_malformed:a"),
    "large_string": (
        tr_seed(),
        pa.table({"a": pa.array(["mail alice@example.com", None, "x"], pa.large_string())}),
        "text_redact_source_type_not_string:a:large_string",
    ),
    "int_source": (
        tr_seed(),
        pa.table({"a": pa.array([123456, None, 234567], pa.int64())}),
        "text_redact_source_type_not_string:a:int64",
    ),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(_DECLINES))
def test_declined_columns_take_the_python_path_byte_identical(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, payload, reason = _DECLINES[name]
    _ner_stub(monkeypatch)
    kernel = install_marking(monkeypatch)
    declined = run_chain(payload, (("a", seed),), batch_rows=2, **mode_kwargs(mode, tmp_path / "d"))
    assert kernel.calls == []
    assert entry(declined) == {
        "table": "t",
        "column": "a",
        "path": "mask_batch",
        "executed_backend": PYTHON_SCALAR,
        "reason": reason,
    }
    with pytest.MonkeyPatch.context() as mp:
        py = python_run(
            mp, payload, (("a", seed),), batch_rows=2, **mode_kwargs(mode, tmp_path / "p")
        )
    assert declined.tables["t"].column("a").to_pylist() == py.tables["t"].column("a").to_pylist()
    assert declined.tables["t"].column("a").type == py.tables["t"].column("a").type


def test_a_declined_column_runs_the_real_python_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seed, payload, _ = _DECLINES["token_not_string"]
    install_marking(monkeypatch)
    real = _mask.text_redact_array
    seen = {"calls": 0}

    def counting(*args: Any, **kwargs: Any) -> pa.Array:
        seen["calls"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(_mask, "text_redact_array", counting)
    run = run_chain(payload, (("a", seed),), batch_rows=2)
    assert seen["calls"] == 3
    # The oracle passes the column through unchanged for a non-string token; so does the route.
    assert run.tables["t"].column("a").to_pylist() == payload.column("a").to_pylist()


def test_a_declined_malformed_detector_selection_passes_through_like_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, payload, _ = _DECLINES["detectors_string"]
    install_marking(monkeypatch)
    run = run_chain(payload, (("a", seed),), batch_rows=2)
    assert run.tables["t"].column("a").to_pylist() == payload.column("a").to_pylist()


# --- companion absent -----------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_companion_absent_runs_the_python_path_with_a_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hide_text_redact_companion(monkeypatch)
    run = run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    assert entry(run) == {
        "table": "t",
        "column": "a",
        "path": "mask_batch",
        "executed_backend": PYTHON_SCALAR,
        "reason": UNAVAILABLE,
    }
    assert run.tables["t"].column("a").to_pylist() == reference(SOURCE)


@pytest.mark.skipif(kernel_loads(), reason="needs a box without the companion (ci-mirror)")
def test_without_the_real_companion_the_unpatched_route_is_python() -> None:
    run = run_chain(_payload(), _SEEDS, batch_rows=4)
    assert entry(run)["executed_backend"] == PYTHON_SCALAR
    assert entry(run)["reason"] == UNAVAILABLE
    assert run.tables["t"].column("a").to_pylist() == reference(SOURCE)


# --- T4: pinning, missing column, on both drivers ----------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_as_large, "out_of_core_text_redact_source_type_drift"),
        (_without_a, "out_of_core_text_redact_column_missing"),
    ],
    ids=["type-drift", "missing-column"],
)
@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_pinned_column_that_changes_or_vanishes_aborts_the_sink(
    mode: str,
    module: Any,
    change: Callable[[pa.RecordBatch], pa.RecordBatch],
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_marking(monkeypatch)
    forbid_python_text_redact(monkeypatch)
    mutate_second_batch(monkeypatch, module, change)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(ExecutionError) as caught:
        run_chain(
            _ROWS,
            _SEEDS,
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    assert caught.value.code == code
    _assert_aborted(sink, out_dir)


@pytest.mark.parametrize("mode", ["sink-batch", "sink-reorder"])
def test_lazy_sources_bind_the_prep_time_type_to_the_streamed_type(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_marking(monkeypatch)
    forbid_python_text_redact(monkeypatch)
    lazy = tmp_path / "src"
    lazy.mkdir()
    run = run_chain(_payload(), _SEEDS, lazy_dir=lazy, batch_rows=8, **mode_kwargs(mode, tmp_path))
    assert len(kernel.calls) == 2
    assert run.tables["t"].column("a").type == pa.string()


# --- T6: mixed job, each strategy reports its own disposition; payload-only --------------------


@pytest.mark.parametrize("mode", MODES)
def test_text_redact_runs_native_beside_hash_fpe_and_categorical(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tr = install_marking(monkeypatch)
    fpe_kernel = install_fpe_kernel(monkeypatch)
    hash_state = install_companion(monkeypatch)
    index_state = install_index_companion(monkeypatch)
    payload = pa.table(
        {
            "a": pa.array(["mail a@b.co", "no pii", None, "call 415-555-0132"], pa.string()),
            "f": pa.array(["123456", "234567", None, "345678"], pa.string()),
            "c": pa.array(["x", "y", None, "z"], pa.string()),
            "h": pa.array(["p", "q", "r", None], pa.string()),
        }
    )
    from tests.parity._d1a_hash_data import column_seed

    seeds = (
        ("a", tr_seed()),
        ("f", fpe_seed()),
        ("c", categorical_seed()),
        ("h", column_seed()),
    )
    run = run_chain(payload, seeds, batch_rows=2, **mode_kwargs(mode, tmp_path))
    metrics = run.result.quality_metrics
    assert {e["column"] for e in metrics[KEY]} == {"a"}
    assert {e["column"] for e in metrics["out_of_core_fpe"]} == {"f"}
    assert {e["column"] for e in metrics["out_of_core_categorical"]} == {"c"}
    hash_columns = {e["column"] for e in metrics["out_of_core_hash"]}
    assert hash_columns.isdisjoint({"a", "f", "c"}) and "h" in hash_columns
    assert {e["executed_backend"] for e in metrics[KEY]} == {RUST_COMPANION}
    assert len(tr.calls) == 2
    assert fpe_kernel.calls == 2
    assert index_state["kernel"].calls == 2
    assert hash_state["kernel"].calls > 0
    assert run.tables["t"].column("a").to_pylist() == reference(payload.column("a").to_pylist())


def test_one_declined_text_redact_column_does_not_move_its_neighbour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_marking(monkeypatch)
    values = ["mail a@b.co", "call 415-555-0132"]
    payload = pa.table(
        {
            "a": pa.array(values, pa.string()),
            "b": pa.array(values, pa.large_string()),
            "n": pa.array(values, pa.string()),
        }
    )
    seeds = (("a", tr_seed()), ("b", tr_seed()), ("n", tr_seed(token=7)))
    run = run_chain(payload, seeds)
    assert entry(run, "a")["executed_backend"] == RUST_COMPANION
    assert entry(run, "b")["executed_backend"] == PYTHON_SCALAR
    assert entry(run, "b")["reason"] == "text_redact_source_type_not_string:b:large_string"
    assert entry(run, "n")["reason"] == "text_redact_token_not_string:n"
    assert len(kernel.calls) == 1
    assert run.tables["t"].column("a").to_pylist() == reference(values)
    assert run.tables["t"].column("b").to_pylist() == reference(values)
    assert run.tables["t"].column("n").to_pylist() == values  # non-string token: unchanged


def test_text_redact_is_not_on_the_hash_key_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    install_marking(monkeypatch)
    run = run_chain(_payload(), _SEEDS)
    assert "a" not in {e["column"] for e in run.result.quality_metrics.get("out_of_core_hash", [])}


def test_a_text_redact_parent_key_is_rejected_by_the_compat_gate() -> None:
    plan, _sources, graph = _key_strategy_job(tr_seed())
    assert not _gate_admits(plan, graph)
    assert "out_of_core_parent_strategy_unsupported" in _gate_codes(plan, graph)


def test_a_text_redact_child_key_is_skipped_by_skip_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_marking(monkeypatch)
    seed = tr_seed()
    plan = _two_column_plan(seed)
    schema = pa.schema([pa.field("a", pa.string()), pa.field("fk", pa.string())])
    # Control: with nothing skipped both columns get a decision.
    control = OocHashRun().prepare_table(plan, "t", schema, frozenset(), (), site="test")
    assert control.text_redact is not None
    assert set(control.text_redact.decisions) == {"a", "fk"}
    # The FK child key is in skip_columns: it is remapped from its parent, never masked here.
    run = OocHashRun()
    table_plan = run.prepare_table(plan, "t", schema, frozenset({"fk"}), (), site="test")
    assert table_plan.text_redact is not None
    assert set(table_plan.text_redact.decisions) == {"a"}
    assert {e["column"] for e in run.metrics()[KEY]} == {"a"}


def _two_column_plan(seed: Any) -> Any:
    from types import SimpleNamespace

    table = TableSeed(per_column=(("a", seed), ("fk", seed)), per_group=())
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=JOB_SEED, per_table=(("t", table),)))


# --- T7: runtime fault propagates; no catch-and-passthrough; result-shape guard ----------------


@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_mid_run_kernel_fault_aborts_without_a_python_switch(
    mode: str, module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_marking(monkeypatch, fail_on_call=2)
    forbid_python_text_redact(monkeypatch)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(
            _ROWS,
            _SEEDS,
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    assert len(kernel.calls) == 2  # the fault ended the run; nothing retried it on another path
    _assert_aborted(sink, out_dir)


def test_a_mid_run_kernel_fault_returns_no_partial_resident_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_marking(monkeypatch, fail_on_call=2)
    forbid_python_text_redact(monkeypatch)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(_ROWS, _SEEDS, batch_rows=2)


def test_a_first_batch_fault_does_not_pass_the_text_through_unredacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No catch-and-passthrough: a fault on the very first call must not leave the column raw.
    install_marking(monkeypatch, fail_on_call=1)
    forbid_python_text_redact(monkeypatch)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(_ROWS, _SEEDS, batch_rows=8)


_BAD_SHAPES = {
    "short": lambda arr: pa.array(["x"] * (len(arr) - 1), pa.string()),
    "long": lambda arr: pa.array(["x"] * (len(arr) + 1), pa.string()),
    "wrong_type": lambda arr: pa.array(["x"] * len(arr), pa.large_string()),
    "chunked": lambda arr: pa.chunked_array([pa.array(["x"] * len(arr), pa.string())]),
}


@pytest.mark.parametrize("shape", list(_BAD_SHAPES))
def test_an_invalid_result_shape_raises_and_is_never_emitted(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_marking(monkeypatch, bad_result=_BAD_SHAPES[shape])
    forbid_python_text_redact(monkeypatch)
    with pytest.raises(ExecutionError) as caught:
        run_chain(_ROWS, _SEEDS, batch_rows=8)
    assert caught.value.code == "out_of_core_text_redact_native_result_invalid"


# --- T5: run_pipeline routes a large text_redact-payload FK job to the out-of-core route -------

_N = 40


def _text_redact_payload_fk_config(tmp_path: Path) -> dict[str, Any]:
    """Parent->child FK job: hash FK keys plus a text_redact payload column on each table."""

    def note(prefix: str, i: int) -> str:
        return f"{prefix}{i}: mail user{i}@example.com or call 415-555-{1000 + i} ssn 123-45-{6000 + i}"

    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "note": pa.array([note("n", i) for i in range(_N)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "pid": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "memo": pa.array([note("m", i) for i in range(_N)], type=pa.string()),
        }
    )
    tables = {"parent": parent, "child": child}
    paths = {}
    for name, tbl in tables.items():
        paths[name] = str(tmp_path / f"{name}.parquet")
        pq.write_table(tbl, paths[name])
    return {
        "version": 2,
        "global_settings": {"job_name": "ooc-text-redact-routing", "seed": 11},
        "sources": {
            name: {"type": "file", "path": paths[name], "format": "parquet"} for name in tables
        },
        "targets": {
            name: {
                "type": "file",
                "path": str(tmp_path / f"{name}.out.parquet"),
                "format": "parquet",
            }
            for name in tables
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {"name": "id", "strategy": "hash", "namespace": "pns"},
                    {"name": "note", "strategy": "text_redact"},
                ],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "cid", "strategy": "hash", "namespace": "cns"},
                    {"name": "pid", "strategy": "hash", "namespace": "pns"},
                    {
                        "name": "memo",
                        "strategy": "text_redact",
                        "provider_config": {"token": "<X>"},
                    },
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "pns",
            },
        ],
    }


def test_run_pipeline_routes_a_text_redact_payload_job_and_reports_the_backend(
    tmp_path: Path,
) -> None:
    config = _text_redact_payload_fk_config(tmp_path)
    sources = {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}
    oracle = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    routed = run_pipeline(
        config,
        sources,
        engine_version="0.1.0",
        out_of_core_budget_bytes=64 * 1024 * 1024,
        use_probe_routing=False,
    )
    assert routed.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    entries = {(e["table"], e["column"]): e for e in routed.quality_metrics[KEY]}
    assert set(entries) == {("parent", "note"), ("child", "memo")}
    # The unpatched route: native under the companion, the Python loop on ci-mirror.
    if kernel_loads():
        assert {e["executed_backend"] for e in entries.values()} == {RUST_COMPANION}
        assert all("reason" not in e for e in entries.values())
    else:
        assert {e["executed_backend"] for e in entries.values()} == {PYTHON_SCALAR}
        assert {e["reason"] for e in entries.values()} == {UNAVAILABLE}
    for table, column in entries:
        got = routed.outputs[table].column(column).to_pylist()
        assert got == oracle.outputs[table].column(column).to_pylist()
        assert got != sources[table].column(column).to_pylist()  # really redacted
    assert {t: tbl.to_pydict() for t, tbl in routed.outputs.items()} == {
        t: tbl.to_pydict() for t, tbl in oracle.outputs.items()
    }
