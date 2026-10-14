"""Out-of-core fpe native slice: routing, declines, pinning, fail-closed and the witness.

Runs on any box. The kernel is the marking reference double (or the real companion where a
test says so), so what is asserted is the route: which backend a column takes, that the native
result is the array the batch carries, that a decline reaches the existing Python `fpe_array`
unchanged, and that a pinned column aborts rather than switching path. Byte parity of the real
compiled kernel against the oracle is in `test_ooc_fpe_native_real.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import _mask
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from tests.parity._d1a_hash_data import chain_plan, install_companion, run_chain
from tests.parity._d1b2_categorical_data import categorical_seed, install_index_companion
from tests.parity._ooc_fpe_data import (
    BAD_DOMAIN,
    BAD_VALUE,
    DIGITS,
    KEY,
    SOURCE,
    FpeConsumedWitness,
    MarkingFpeKernel,
    companion_loads,
    forbid_python_fpe,
    fpe_seed,
    hide_fpe_companion,
    install_fpe_kernel,
    replace_with_equal_python_array,
)
from tests.parity.test_out_of_core_group_b_routing import _fpe_payload_fk_config, _sources
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
_SEEDS = (("a", fpe_seed()),)
_ROWS = pa.table({"a": pa.array(["123456", "234567", "345678", "456789", "567890"], pa.string())})


def _payload(values: list[Any] = SOURCE) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


def entry(run: Any, column: str = "a") -> dict[str, Any]:
    (found,) = (e for e in run.result.quality_metrics[KEY] if e["column"] == column)
    return found


def python_run(monkeypatch: pytest.MonkeyPatch, payload: pa.Table, seeds: Any = _SEEDS, **kw: Any):
    """The same job with the companion hidden: the existing Python `fpe_array` path."""
    hide_fpe_companion(monkeypatch)
    return run_chain(payload, seeds, **kw)


# --- O1: consumed-output witness and the string-type pin -----------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_native_fpe_result_is_the_array_the_batch_carries(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_fpe_kernel(monkeypatch)
    forbid_python_fpe(monkeypatch)
    witness = FpeConsumedWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    witness.assert_consumed(kernel)
    assert kernel.calls == 3  # 12 rows at 4 per batch; one kernel call per batch
    assert entry(run)["executed_backend"] == RUST_COMPANION
    assert "reason" not in entry(run)
    got = run.tables["t"].column("a").to_pylist()
    assert got != SOURCE  # the column was really encrypted
    assert [v is None for v in got] == [v is None for v in SOURCE]
    assert [v == "" for v in got] == [v == "" for v in SOURCE]


@pytest.mark.parametrize("mode", MODES)
def test_a_discarded_native_result_fails_the_witness(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative control: the kernel runs and its result is then swapped for an equal-valued
    # Python array before the batch leaves mask_batch. Values still match; the witness must not.
    kernel = install_fpe_kernel(monkeypatch)
    forbid_python_fpe(monkeypatch)
    replace_with_equal_python_array(monkeypatch)
    witness = FpeConsumedWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    assert kernel.calls > 0
    with pytest.raises(AssertionError, match="discarded"):
        witness.assert_consumed(kernel)
    plain = run_chain(_payload(), _SEEDS)
    assert run.tables["t"].column("a").to_pylist() == plain.tables["t"].column("a").to_pylist()


@pytest.mark.parametrize("mode", MODES)
def test_a_never_called_kernel_cannot_pass_the_witness(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_fpe_kernel(monkeypatch)
    hide_fpe_companion(monkeypatch)
    witness = FpeConsumedWitness()
    with witness.observe():
        run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    assert kernel.calls == 0
    with pytest.raises(AssertionError, match="no non-empty native result"):
        witness.assert_consumed(kernel)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "values",
    [
        [None, None, None],
        ["", "", ""],
        [None, "", None, ""],
        ["123456", None, "", "234567"],
        [],
    ],
    ids=["all-null", "all-empty", "null-and-empty", "mixed", "zero-rows"],
)
def test_output_is_string_typed_and_equals_the_python_path(
    values: list[Any], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(values)
    kernel = install_fpe_kernel(monkeypatch)
    native = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path / "n"))
    assert native.tables["t"].column("a").type == pa.string()
    with pytest.MonkeyPatch.context() as mp:
        py = python_run(mp, payload, batch_rows=2, **mode_kwargs(mode, tmp_path / "p"))
    assert py.tables["t"].column("a").type == pa.string()
    assert native.tables["t"].column("a").to_pylist() == py.tables["t"].column("a").to_pylist()
    for result in kernel.results:
        assert result.type == pa.string()


def test_an_all_null_batch_reports_a_native_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    # fpe has no short-circuit: the kernel validates key and tweak even for a null slice.
    kernel = install_fpe_kernel(monkeypatch)
    run = run_chain(_payload([None, None, None]), _SEEDS, batch_rows=2)
    assert kernel.calls == 2
    assert entry(run)["executed_backend"] == RUST_COMPANION


# --- O2: declines route to the Python fpe_array, byte-identical, with a coded reason --------

_DECLINES = {
    "checksum": (
        fpe_seed(config=(*DIGITS, ("checksum", "luhn"))),
        _payload(["4111111111111111", "5555555555554444", None, "4012888888881881"]),
        "fpe_checksum_not_native:a",
    ),
    "large_string": (
        fpe_seed(),
        pa.table({"a": pa.array(["123456", None, "234567", "345678"], pa.large_string())}),
        "fpe_source_type_not_string:a:large_string",
    ),
    "int_source": (
        fpe_seed(),
        pa.table({"a": pa.array([123456, None, 234567, 345678], pa.int64())}),
        "fpe_source_type_not_string:a:int64",
    ),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(_DECLINES))
def test_declined_columns_take_the_python_path_byte_identical(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, payload, reason = _DECLINES[name]
    kernel = install_fpe_kernel(monkeypatch)
    declined = run_chain(payload, (("a", seed),), batch_rows=2, **mode_kwargs(mode, tmp_path / "d"))
    assert kernel.calls == 0
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
    seed, payload, _ = _DECLINES["checksum"]
    install_fpe_kernel(monkeypatch)
    real = _mask.fpe_array
    seen = {"calls": 0}

    def counting(*args: Any, **kwargs: Any) -> pa.Array:
        seen["calls"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(_mask, "fpe_array", counting)
    run_chain(payload, (("a", seed),), batch_rows=2)
    assert seen["calls"] == 2


_INVALID = {
    "missing_namespace": (
        fpe_seed(namespace=None),
        "fpe_requires_namespace_stays_python:a",
        "fpe_requires_namespace",
    ),
    "duplicate_charset": (
        fpe_seed(config=(("charset", "aab"),)),
        "fpe_config_stays_python:a",
        "fpe_charset_duplicate_symbols",
    ),
    "degenerate_charset": (
        fpe_seed(config=(("charset", "a"),)),
        "fpe_config_stays_python:a",
        "fpe_charset_degenerate",
    ),
}


@pytest.mark.parametrize("name", list(_INVALID))
def test_an_invalid_config_declines_and_the_python_path_then_raises(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, reason, code = _INVALID[name]
    kernel = install_fpe_kernel(monkeypatch)
    plan = chain_plan((("a", seed),))
    table_plan = OocHashRun().prepare_table(plan, "t", _ROWS.schema, frozenset(), (), site="test")
    assert table_plan.fpe is not None
    decision = table_plan.fpe.decisions["a"]
    assert (decision.backend, decision.reason) == (PYTHON_SCALAR, reason)
    # The decline is not a silent pass: the existing Python loop raises its own coded error.
    with pytest.raises(StrategyError) as caught:
        run_chain(_ROWS, (("a", seed),), batch_rows=2)
    assert caught.value.code == code
    assert kernel.calls == 0


def test_a_string_source_with_a_valid_config_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fpe_kernel(monkeypatch)
    plan = chain_plan(_SEEDS)
    table_plan = OocHashRun().prepare_table(plan, "t", _ROWS.schema, frozenset(), (), site="test")
    assert table_plan.fpe is not None
    decision = table_plan.fpe.decisions["a"]
    assert (decision.backend, decision.reason) == (RUST_COMPANION, None)
    assert decision.config is not None
    assert decision.pinned_type == pa.string()


# --- companion absent -----------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_companion_absent_runs_the_python_path_with_a_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hide_fpe_companion(monkeypatch)
    run = run_chain(_payload(), _SEEDS, batch_rows=4, **mode_kwargs(mode, tmp_path))
    assert entry(run) == {
        "table": "t",
        "column": "a",
        "path": "mask_batch",
        "executed_backend": PYTHON_SCALAR,
        "reason": "fpe_extension_unavailable",
    }
    got = run.tables["t"].column("a").to_pylist()
    assert got != SOURCE
    assert got[2] == "" and got[3] is None


@pytest.mark.skipif(companion_loads(), reason="needs a box without the companion (ci-mirror)")
def test_without_the_real_companion_the_unpatched_route_is_python() -> None:
    run = run_chain(_payload(), _SEEDS, batch_rows=4)
    assert entry(run)["executed_backend"] == PYTHON_SCALAR
    assert entry(run)["reason"] == "fpe_extension_unavailable"


# --- O3 / O7: fail-closed parity -------------------------------------------------------------


def _error_code(payload: pa.Table, **kw: Any) -> str:
    with pytest.raises(StrategyError) as caught:
        run_chain(payload, _SEEDS, **kw)
    assert caught.value.strategy == "fpe"
    return caught.value.code


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("bad", "code"),
    [(BAD_VALUE, "fpe_unencryptable_value"), (BAD_DOMAIN, "fpe_unencryptable_domain")],
)
def test_an_unencryptable_value_raises_the_python_paths_code(
    bad: str, code: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(["123456", "234567", bad, "345678"])
    kernel = install_fpe_kernel(monkeypatch)
    native = _error_code(payload, batch_rows=2, **mode_kwargs(mode, tmp_path / "n"))
    assert kernel.calls >= 1
    hide_fpe_companion(monkeypatch)
    python = _error_code(payload, batch_rows=2, **mode_kwargs(mode, tmp_path / "p"))
    assert native == python == code


def test_the_failure_is_raised_at_the_batch_that_holds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Per-batch granularity, as the Python loop: batches before the bad one are processed,
    # batches after it are never reached.
    payload = _payload(["123456", "234567", "345678", BAD_VALUE, "456789", "567890"])
    kernel = install_fpe_kernel(monkeypatch)
    _error_code(payload, batch_rows=2)
    assert kernel.calls == 2


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("first", "second"),
    [(BAD_DOMAIN, BAD_VALUE), (BAD_VALUE, BAD_DOMAIN)],
    ids=["domain-first", "value-first"],
)
def test_the_first_failing_row_decides_the_code_when_a_batch_holds_several(
    first: str, second: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(["123456", first, "234567", second, BAD_DOMAIN])
    kernel = install_fpe_kernel(monkeypatch)
    native = _error_code(payload, **mode_kwargs(mode, tmp_path / "n"))
    assert kernel.calls == 1  # one batch holds every failing row
    hide_fpe_companion(monkeypatch)
    python = _error_code(payload, **mode_kwargs(mode, tmp_path / "p"))
    expected = "fpe_unencryptable_domain" if first == BAD_DOMAIN else "fpe_unencryptable_value"
    assert native == python == expected


# --- O4 / O7: pinning, missing column, kernel fault -------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_as_large, "out_of_core_fpe_source_type_drift"),
        (_without_a, "out_of_core_fpe_column_missing"),
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
    install_fpe_kernel(monkeypatch)
    forbid_python_fpe(monkeypatch)
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


@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_mid_run_kernel_fault_aborts_without_a_python_switch(
    mode: str, module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_fpe_kernel(monkeypatch, MarkingFpeKernel(fail_on_call=2))
    forbid_python_fpe(monkeypatch)
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
    assert kernel.calls == 2  # the fault ended the run; nothing retried it on another path
    _assert_aborted(sink, out_dir)


def test_a_mid_run_kernel_fault_returns_no_partial_resident_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fpe_kernel(monkeypatch, MarkingFpeKernel(fail_on_call=2))
    forbid_python_fpe(monkeypatch)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(_ROWS, _SEEDS, batch_rows=2)


@pytest.mark.parametrize("mode", ["sink-batch", "sink-reorder"])
def test_lazy_sources_bind_the_prep_time_type_to_the_streamed_type(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_fpe_kernel(monkeypatch)
    forbid_python_fpe(monkeypatch)
    lazy = tmp_path / "src"
    lazy.mkdir()
    run = run_chain(_payload(), _SEEDS, lazy_dir=lazy, batch_rows=5, **mode_kwargs(mode, tmp_path))
    assert kernel.calls == 3
    assert run.tables["t"].column("a").type == pa.string()


# --- O6: mixed job, each strategy reports its own disposition --------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_fpe_runs_native_beside_hash_and_categorical(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fpe_kernel = install_fpe_kernel(monkeypatch)
    hash_state = install_companion(monkeypatch)
    index_state = install_index_companion(monkeypatch)
    payload = pa.table(
        {
            "a": pa.array(["123456", "234567", None, "345678"], pa.string()),
            "c": pa.array(["x", "y", None, "z"], pa.string()),
            "h": pa.array(["p", "q", "r", None], pa.string()),
        }
    )
    from tests.parity._d1a_hash_data import column_seed

    seeds = (("a", fpe_seed()), ("c", categorical_seed()), ("h", column_seed()))
    run = run_chain(payload, seeds, batch_rows=2, **mode_kwargs(mode, tmp_path))
    metrics = run.result.quality_metrics
    assert {e["column"] for e in metrics[KEY]} == {"a"}
    assert {e["column"] for e in metrics["out_of_core_categorical"]} == {"c"}
    hash_columns = {e["column"] for e in metrics["out_of_core_hash"]}
    assert "a" not in hash_columns and "c" not in hash_columns and "h" in hash_columns
    assert {e["executed_backend"] for e in metrics[KEY]} == {RUST_COMPANION}
    assert fpe_kernel.calls == 2
    assert index_state["kernel"].calls == 2
    assert hash_state["kernel"].calls > 0


def test_one_declined_fpe_column_does_not_move_its_neighbour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_fpe_kernel(monkeypatch)
    payload = pa.table(
        {
            "a": pa.array(["123456", "234567"], pa.string()),
            "b": pa.array(["123456", "234567"], pa.large_string()),
        }
    )
    run = run_chain(payload, (("a", fpe_seed()), ("b", fpe_seed(namespace="ns_b"))))
    assert entry(run, "a")["executed_backend"] == RUST_COMPANION
    assert entry(run, "b")["executed_backend"] == PYTHON_SCALAR
    assert entry(run, "b")["reason"] == "fpe_source_type_not_string:b:large_string"
    assert kernel.calls == 1


def test_fpe_is_not_on_the_native_key_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    # fpe stays payload-only: the hash disposition never lists an fpe column.
    install_fpe_kernel(monkeypatch)
    run = run_chain(_payload(), _SEEDS)
    assert "a" not in {e["column"] for e in run.result.quality_metrics.get("out_of_core_hash", [])}


# --- O5: run_pipeline routes a large fpe-payload FK job to the out-of-core route ---------------


def test_run_pipeline_routes_an_fpe_payload_job_and_reports_the_fpe_backend(
    tmp_path: Path,
) -> None:
    config = _fpe_payload_fk_config(tmp_path)
    sources = _sources(config)
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
    assert set(entries) == {("parent", "ssn"), ("child", "acct")}
    # The unpatched route: native under the companion, the Python loop on ci-mirror.
    if companion_loads():
        assert {e["executed_backend"] for e in entries.values()} == {RUST_COMPANION}
        assert all("reason" not in e for e in entries.values())
    else:
        assert {e["executed_backend"] for e in entries.values()} == {PYTHON_SCALAR}
        assert {e["reason"] for e in entries.values()} == {"fpe_extension_unavailable"}
    for table, column in entries:
        assert (
            routed.outputs[table].column(column).to_pylist()
            == oracle.outputs[table].column(column).to_pylist()
        )
    assert {t: tbl.to_pydict() for t, tbl in routed.outputs.items()} == {
        t: tbl.to_pydict() for t, tbl in oracle.outputs.items()
    }
