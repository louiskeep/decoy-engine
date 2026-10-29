"""Out-of-core text_mask native slice (B5): the REAL compiled span kernel against the oracle.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the companion-absent
case covers the Python path. Compared here: the OOC native route vs the full-frame pandas oracle
on the strict parity contract, vs the per-cell Python reference, across batch sizes and drivers;
the redaction/passthrough/token/fpe-span/faker-span/date-span configs; the aggregate sub-floor
warning totals; and the consumption witness that the compiled path (not the per-cell mask_cell
loop) drove the output. Each consumption assertion fails main's per-cell loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._operator_registry import RUST_COMPANION
from decoy_engine.execution.out_of_core import _text_mask_native
from tests.native._c6b_i_support import CORPUS, SUB_FLOOR_TEXTS
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import chain_graph, chain_plan, chain_sources, run_chain
from tests.parity._ooc_text_mask_data import (
    KEY,
    SOURCE,
    UNAVAILABLE,
    forbid_old_loop,
    hide_text_mask_companion,
    install_marking,
    kernel_loads,
    tm_seed,
)
from tests.parity.test_out_of_core_fk_parity import _run_oracle
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.parity.test_phase_d_d1a_hash_pinning import (
    _DRIVERS,
    RecordingSink,
    _as_large,
    _assert_aborted,
    _without_a,
    mutate_second_batch,
)

pytestmark = pytest.mark.skipif(
    not kernel_loads(),
    reason="decoy-engine-native text_redact span kernel not loadable; the companion-present job covers this",
)

MODES = ["resident", "sink-batch", "sink-reorder"]

# Config variants that stay native: default redaction, an explicit detector set, the two unmatched
# policies, and the keyed span strategies (faker/date_shift/fpe). CORPUS holds no value that drives
# an fpe span below the FF1 floor, so the fpe-span config masks it without a sub-floor policy.
_CONFIGS: dict[str, dict[str, Any]] = {
    "redact": {},
    "detectors": {"detectors": ["email"]},
    "passthrough": {"unmatched_span_policy": "passthrough"},
    "token": {"unmatched_span_policy": "replace_with_token", "token": "<X>"},
    "faker-span": {"per_detector_strategy": {"ssn": "faker", "email": "faker"}},
    "date-span": {"per_detector_strategy": {"ssn": "date_shift"}, "min_days": -10, "max_days": 10},
    "fpe-span": {"per_detector_strategy": {"ssn": "fpe", "us_phone": "fpe"}},
}


def _payload(values: list[Any] = SOURCE) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


def _seeds(config: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return (("a", tm_seed(**config)),)


def _oracle_tables(payload: pa.Table, seeds: Any) -> Any:
    return _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph())


def _assert_strict_equal_to_oracle(ooc: dict[str, pa.Table], oracle: dict[str, pa.Table]) -> None:
    for table in ("up", "t", "child"):
        report = sc.compare_strict(oracle[table], ooc[table])
        assert {dim for dim, agree in report.items() if not agree} <= {"field_metadata"}, table


@pytest.mark.parametrize("config_id", list(_CONFIGS))
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("batch_rows", [None, 1, 5])
def test_native_output_is_the_oracle_and_is_consumed(
    config_id: str,
    mode: str,
    batch_rows: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeds = _seeds(_CONFIGS[config_id])
    payload = _payload(list(CORPUS))
    kernel = install_marking(monkeypatch)
    forbid_old_loop(monkeypatch)  # a column decided native must never reach the per-cell loop
    run = run_chain(payload, seeds, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    assert kernel.calls and all(c.column == "a" for c in kernel.calls)  # the compiled path ran
    got = run.tables["t"].column("a")
    assert got.type == pa.string()
    _assert_strict_equal_to_oracle(run.tables, _oracle_tables(payload, seeds).outputs)
    disposition = {e["column"]: e for e in run.result.quality_metrics[KEY]}
    assert disposition["a"]["executed_backend"] == RUST_COMPANION
    assert "reason" not in disposition["a"]


def test_native_matches_the_python_route_with_the_companion_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(list(CORPUS))
    seeds = _seeds(_CONFIGS["faker-span"])
    native = run_chain(payload, seeds, batch_rows=3).tables["t"].column("a").to_pylist()
    hide_text_mask_companion(monkeypatch)
    python_run = run_chain(payload, seeds, batch_rows=3)
    assert python_run.tables["t"].column("a").to_pylist() == native
    disposition = {e["column"]: e for e in python_run.result.quality_metrics[KEY]}
    assert disposition["a"]["executed_backend"] == "python_scalar"
    assert disposition["a"]["reason"] == UNAVAILABLE


def _sub_floor_warning_total(result: Any) -> int:
    total = 0
    for warning in result.warnings:
        if warning.code == "text_mask_sub_floor_span_handled":
            total += int(warning.detail.get("total", 0))
    return total


@pytest.mark.parametrize("policy", ["redact", "synthetic"])
@pytest.mark.parametrize("batch_rows", [None, 3])
def test_sub_floor_warning_totals_match_the_oracle(policy: str, batch_rows: int | None) -> None:
    payload = _payload([*SUB_FLOOR_TEXTS, *CORPUS])
    seeds = _seeds({"sub_floor_span": policy})
    run = run_chain(payload, seeds, batch_rows=batch_rows)
    oracle = _oracle_tables(payload, seeds)
    assert _sub_floor_warning_total(run.result) == _sub_floor_warning_total(oracle)
    assert _sub_floor_warning_total(run.result) > 0  # not vacuous: the sub-floor path ran
    _assert_strict_equal_to_oracle(run.tables, oracle.outputs)


def test_an_fpe_span_below_the_floor_fails_closed_identically(tmp_path: Path) -> None:
    # No sub_floor_span policy: an un-encryptable fpe span raises on both routes with one code.
    payload = _payload(list(SUB_FLOOR_TEXTS))
    seeds = _seeds({})  # us_zip defaults to the fpe span strategy
    with pytest.raises(StrategyError) as native:
        run_chain(payload, seeds, batch_rows=2)
    with pytest.raises(StrategyError) as oracle:
        _oracle_tables(payload, seeds)
    assert native.value.code == oracle.value.code == "fpe_unencryptable_domain"
    assert native.value.strategy == "text_mask"


@pytest.mark.parametrize(
    "values",
    [[None, None, None], ["", "", ""], [None, "", None], []],
    ids=["all-null", "all-empty", "null-and-empty", "zero-rows"],
)
@pytest.mark.parametrize("batch_rows", [None, 2])
def test_null_and_empty_columns_are_string_typed_and_match_the_oracle(
    values: list[Any], batch_rows: int | None
) -> None:
    payload = _payload(values)
    seeds = _seeds({})
    ooc = run_chain(payload, seeds, batch_rows=batch_rows).tables
    oracle = _oracle_tables(payload, seeds).outputs
    assert ooc["t"].column("a").type == pa.string()
    assert ooc["t"].column("a").to_pylist() == oracle["t"].column("a").to_pylist() == values


def test_ner_and_non_string_sources_stay_on_the_python_path() -> None:
    # Admission unit checks: the compiled path is declined for a truthy ner (the spaCy branch it
    # does not reproduce) and for a non-string source, so each keeps the oracle's Python path.
    ner = tm_seed(ner=True)
    schema = pa.schema([pa.field("a", pa.string())])
    assert _text_mask_native.decline_reason("a", schema, ner)[1] == "text_mask_ner_not_native:a"
    int_schema = pa.schema([pa.field("a", pa.int64())])
    config, reason = _text_mask_native.decline_reason("a", int_schema, tm_seed())
    assert config is None and reason is not None and reason.startswith("text_mask")


# --- fault, pinning, the invalid-result guard and the witness negative control ----------------
# The fail-closed / abort / no-partial-output surface, ported from the text_redact routing suite.


@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_mid_run_kernel_fault_aborts_without_a_python_switch_or_partial_output(
    mode: str, module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The second batch's kernel call raises. The route must abort (never silently switch to the
    # per-cell loop, which forbid_old_loop would catch) and commit nothing.
    install_marking(monkeypatch, fail_on_call=2)
    forbid_old_loop(monkeypatch)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(RuntimeError, match="injected text_mask kernel fault"):
        run_chain(
            _payload(),
            _seeds(_CONFIGS["redact"]),
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    _assert_aborted(sink, out_dir)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_as_large, "out_of_core_text_mask_source_type_drift"),
        (_without_a, "out_of_core_text_mask_column_missing"),
    ],
    ids=["type-drift", "missing-column"],
)
@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_pinned_native_column_that_changes_or_vanishes_aborts_the_sink(
    mode: str,
    module: Any,
    change: Any,
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A native column is pinned to its prep-time string type; a batch that retypes or drops it
    # aborts rather than changing path mid-column or masking the wrong shape.
    install_marking(monkeypatch)
    forbid_old_loop(monkeypatch)
    mutate_second_batch(monkeypatch, module, change)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(ExecutionError) as caught:
        run_chain(
            _payload(),
            _seeds(_CONFIGS["redact"]),
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    assert caught.value.code == code
    _assert_aborted(sink, out_dir)


def test_a_native_result_of_the_wrong_row_count_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A kernel that returns fewer rows than the batch must abort, never silently truncate a column.
    def short(array: Any, **_: Any) -> Any:
        return pa.array(["x"], pa.string()), {}

    monkeypatch.setattr(_text_mask_native, "native_text_mask", short)
    with pytest.raises(ExecutionError) as caught:
        run_chain(_payload(), _seeds(_CONFIGS["redact"]), batch_rows=4)
    assert caught.value.code == "out_of_core_text_mask_native_result_invalid"


def test_the_consumption_witness_is_a_live_trap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Negative control for the consumption witness: with the companion hidden the column runs the
    # per-cell loop, so forbid_old_loop MUST fire. This proves the positive witness's pass is
    # meaningful (the trap is live, not dead code that would pass on a no-op).
    hide_text_mask_companion(monkeypatch)
    forbid_old_loop(monkeypatch)
    with pytest.raises(AssertionError, match="per-cell mask_cell loop ran"):
        run_chain(_payload(), _seeds(_CONFIGS["redact"]), batch_rows=4)


@pytest.mark.parametrize(
    "config",
    [{}, {"detectors": None}, {"detectors": []}, {"detectors": ()}],
    ids=["absent", "none", "empty-list", "empty-tuple"],
)
def test_an_empty_or_absent_detector_selection_reaches_the_kernel_as_none(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # None means EVERY detector; () would mean ZERO detectors (a silent PII leak). The resolver
    # must send None for an absent, null, empty-list or empty-tuple selection.
    kernel = install_marking(monkeypatch)
    # CORPUS holds PII but no value that drives an fpe span below the FF1 floor, so masking with
    # every detector does not fail closed here; the assertion is on the detector selection sent.
    payload = _payload(list(CORPUS))
    run = run_chain(payload, _seeds(config), batch_rows=8)
    assert kernel.calls and {c.detectors for c in kernel.calls} == {None}
    _assert_strict_equal_to_oracle(run.tables, _oracle_tables(payload, _seeds(config)).outputs)
