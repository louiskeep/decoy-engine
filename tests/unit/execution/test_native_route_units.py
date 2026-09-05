"""Direct unit tests for the private helpers in `_native_route.py` and
`_native_route_exec.py`, closing mutation survivors the production-entry
suites (`tests/parity/native/test_native_route_production_seam.py`,
`tests/unit/execution/test_native_route_transactional_failures.py`) leave
uncovered because they only ever drive the happy-path config shapes those
suites construct through `run_pipeline`.

Every helper here is private by convention, not by enforcement, so importing
it directly is the cheapest way to pin a default value, a dict key, or an
arithmetic accumulator that the production entry never varies enough to
distinguish from its mutated neighbor.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _native_route as _route_mod
from decoy_engine.execution import _native_route_exec as _exec_mod
from decoy_engine.execution._native_route import NativeRouteLedger
from decoy_engine.profile._readers import LazySource
from decoy_engine.relationships import RelationshipGraph

_TABLE = "t"
_EMPTY_GRAPH = RelationshipGraph(edges=(), ordering=())


def _lazy_source(tmp_path: Path) -> LazySource:
    """A LazySource whose on-disk content is irrelevant: `static_candidacy`
    only ever checks `isinstance(source, LazySource)`, never reads it."""
    table = pa.table({"c": pa.array(["x"], type=pa.utf8())})
    path = tmp_path / "src.parquet"
    pq.write_table(table, path)
    return LazySource(path=path)


def _candidacy(tmp_path: Path, columns: list[Any]) -> Any:
    source = _lazy_source(tmp_path)
    config = {"tables": [{"name": _TABLE, "columns": columns}]}
    return _route_mod.static_candidacy(
        config=config,
        execution_mode="auto",
        table_kinds={_TABLE: "mask"},
        caller_sources={_TABLE: source},
        source_loader=None,
        sink=None,
        fidelity_report=False,
        graph=_EMPTY_GRAPH,
    )


# ---------------------------------------------------------------------------
# _find_table (defined separately in both modules)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "find_table",
    [_route_mod._find_table, _exec_mod._find_table],
    ids=["_native_route", "_native_route_exec"],
)
def test_find_table_matches_by_name_not_by_first_dict_entry(
    find_table: Callable[[dict[str, Any], str], dict[str, Any] | None],
) -> None:
    """A config with more than one table must resolve by `name`, not just by
    dict-ness of the first entry -- an `and`->`or` regression here would
    silently mask every table but the first one in the list."""
    config = {
        "tables": [
            {"name": "a", "columns": []},
            {"name": "b", "columns": [{"name": "x"}]},
        ]
    }
    result = find_table(config, "b")
    assert result is not None
    assert result["name"] == "b"


# ---------------------------------------------------------------------------
# static_candidacy
# ---------------------------------------------------------------------------


def test_static_candidacy_declines_execution_mode_not_auto() -> None:
    result = _route_mod.static_candidacy(
        config={},
        execution_mode="full_frame",
        table_kinds={},
        caller_sources={},
        source_loader=None,
        sink=None,
        fidelity_report=False,
        graph=_EMPTY_GRAPH,
    )
    assert result.candidate is False
    assert result.table is None
    assert result.reason == "execution_mode_not_auto"
    assert result.sink_mode is None


def test_static_candidacy_admits_resident_when_vault_key_is_absent(tmp_path: Path) -> None:
    """A column with no `vault` key at all must fall back to "not vaulted"
    (default False), never decline as though `vault: true` were set."""
    result = _candidacy(tmp_path, [{"name": "c", "strategy": "passthrough"}])
    assert result.candidate is True
    assert result.table == _TABLE
    assert result.reason is None
    assert result.sink_mode == "resident"


def test_static_candidacy_no_columns_configured_reason_exact(tmp_path: Path) -> None:
    result = _candidacy(tmp_path, [])
    assert result.candidate is False
    assert result.reason == "no_columns_configured"


def test_static_candidacy_invalid_column_config_reason_exact(tmp_path: Path) -> None:
    result = _candidacy(tmp_path, [42])
    assert result.candidate is False
    assert result.reason == "invalid_column_config"


def test_static_candidacy_missing_name_key_defaults_to_question_mark(tmp_path: Path) -> None:
    result = _candidacy(tmp_path, [{"strategy": "passthrough", "vault": True}])
    assert result.candidate is False
    assert result.reason == "vault_column:?"


def test_static_candidacy_unsupported_strategy_reason_exact(tmp_path: Path) -> None:
    result = _candidacy(tmp_path, [{"name": "c", "strategy": "bogus"}])
    assert result.candidate is False
    assert result.reason == "unsupported_strategy:c:bogus"


def test_static_candidacy_redact_rejection_threads_column_name(tmp_path: Path) -> None:
    result = _candidacy(
        tmp_path,
        [{"name": "myfield", "strategy": "redact", "provider_config": {"redact_with": 123}}],
    )
    assert result.candidate is False
    assert result.reason == "redact_with_not_string:myfield"


def test_static_candidacy_truncate_rejection_is_actually_checked(tmp_path: Path) -> None:
    """Locks the `elif strategy == "truncate"` branch itself: a config that
    `truncate_config_rejection` would reject must actually decline here, not
    silently admit because the branch condition never matched."""
    result = _candidacy(
        tmp_path,
        [{"name": "myfield", "strategy": "truncate", "provider_config": {"length": 0}}],
    )
    assert result.candidate is False
    assert result.reason == "truncate_length_invalid:myfield"


# ---------------------------------------------------------------------------
# peek_and_admit
# ---------------------------------------------------------------------------


class _StubSource:
    """Duck-typed `iter_batches`: `peek_and_admit` never checks the source's
    type, only calls this one method."""

    def __init__(self, batches: list[pa.RecordBatch]) -> None:
        self._batches = batches

    def iter_batches(self, batch_rows: int) -> Any:
        return iter(self._batches)


def test_peek_and_admit_zero_row_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_route_mod, "known_output_columns", lambda plan, table: {"a"})
    admission = _route_mod.peek_and_admit(_StubSource([]), table=_TABLE, plan=None, batch_rows=10)
    assert admission.admitted is False
    assert admission.reason == "zero_row_source"
    assert admission.column_order == ()
    assert admission.first_batch is None
    assert admission.rest is None


def test_peek_and_admit_unsupported_projection_reports_missing_and_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_route_mod, "known_output_columns", lambda plan, table: {"a", "b"})
    batch = pa.record_batch(
        {"a": pa.array(["x"], type=pa.utf8()), "c": pa.array(["y"], type=pa.utf8())}
    )
    admission = _route_mod.peek_and_admit(
        _StubSource([batch]), table=_TABLE, plan=None, batch_rows=10
    )
    assert admission.admitted is False
    assert admission.reason == "unsupported_projection:missing=['b']:extra=['c']"
    assert admission.column_order == ()
    assert admission.first_batch is None
    assert admission.rest is None


def test_peek_and_admit_non_utf8_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_route_mod, "known_output_columns", lambda plan, table: {"a"})
    batch = pa.record_batch({"a": pa.array([1], type=pa.int64())})
    admission = _route_mod.peek_and_admit(
        _StubSource([batch]), table=_TABLE, plan=None, batch_rows=10
    )
    assert admission.admitted is False
    assert admission.reason == "non_utf8_column:a:int64"
    assert admission.column_order == ()
    assert admission.first_batch is None
    assert admission.rest is None


# ---------------------------------------------------------------------------
# _resolve_truncate_keep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({"keep": "tail"}, "tail"),  # explicit `keep` wins over the from_end fallback
        ({"from_end": True}, "tail"),
        ({"from_end": False}, "head"),
        ({}, "head"),  # both absent -> the legacy default
    ],
)
def test_resolve_truncate_keep_resolution(cfg: dict[str, Any], expected: str) -> None:
    assert _exec_mod._resolve_truncate_keep(cfg) == expected


# ---------------------------------------------------------------------------
# _resolve_strategy_cfg
# ---------------------------------------------------------------------------


def test_resolve_strategy_cfg_resolves_kwargs_exactly() -> None:
    config = {
        "tables": [
            {
                "name": _TABLE,
                "columns": [
                    {"name": "pt", "strategy": "passthrough"},
                    {"name": "rd", "strategy": "redact", "provider_config": {"redact_with": "ZZZ"}},
                    {"name": "rd2", "strategy": "redact"},
                    {
                        "name": "tr",
                        "strategy": "truncate",
                        "provider_config": {"length": 2, "keep": "tail", "mask_char": "#"},
                    },
                ],
            }
        ]
    }
    resolved = _exec_mod._resolve_strategy_cfg(config, _TABLE, ("pt", "rd", "rd2", "tr"))
    assert resolved["pt"] == ("passthrough", {})
    assert resolved["rd"] == ("redact", {"redact_with": "ZZZ"})
    assert resolved["rd2"] == ("redact", {"redact_with": "REDACTED"})
    assert resolved["tr"] == ("truncate", {"length": 2, "keep": "tail", "mask_char": "#"})


# ---------------------------------------------------------------------------
# _schema_drift_reason
# ---------------------------------------------------------------------------


def test_schema_drift_reason_reports_missing_and_extra_column_names_exactly() -> None:
    """Only the type-changed branch is exercised elsewhere (the transactional
    failure suite); the name-mismatch branch's set-difference computation
    needs its own coverage."""
    expected = pa.schema([pa.field("pt", pa.utf8())])
    actual = pa.schema([pa.field("other", pa.utf8())])
    assert _exec_mod._schema_drift_reason(expected, actual) == "missing=['pt'];extra=['other']"


# ---------------------------------------------------------------------------
# _run_native_streaming (+ _mask_one_batch, exercised through it)
# ---------------------------------------------------------------------------


def _fake_clock(values: list[float]) -> Callable[[], float]:
    it = iter(values)

    def _tick() -> float:
        return next(it)

    return _tick


def test_run_native_streaming_field_integrity_and_timing_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives `_run_native_streaming` (and, through it, `_mask_one_batch`)
    directly with a fixed clock: real wall-clock timing on a two-row batch is
    too fast to reliably tell a dropped `+=` or a swapped `0.0`/`1.0` initial
    value apart from a genuine near-zero measurement, so the clock is pinned
    instead of measured.
    """
    config = {
        "tables": [
            {
                "name": _TABLE,
                "columns": [
                    {"name": "pt", "strategy": "passthrough"},
                    {"name": "rd", "strategy": "redact", "provider_config": {"redact_with": "ZZZ"}},
                    {
                        "name": "tr",
                        "strategy": "truncate",
                        "provider_config": {"length": 2, "keep": "tail", "mask_char": "#"},
                    },
                ],
            }
        ]
    }
    column_order = ("pt", "rd", "tr")
    first_batch = pa.record_batch(
        {
            "pt": pa.array(["a", "b"], type=pa.utf8()),
            "rd": pa.array(["secret1", "secret2"], type=pa.utf8()),
            "tr": pa.array(["hello", "world"], type=pa.utf8()),
        }
    )
    table_kinds = {_TABLE: "mask"}

    # One perf_counter() call for t_batch0, then (t0, elapsed) per column
    # (pt, rd, tr), then one for batch_total: 8 calls total.
    clock_values = [0.0, 10.0, 12.0, 20.0, 25.0, 30.0, 34.0, 100.0]
    monkeypatch.setattr(_exec_mod.time, "perf_counter", _fake_clock(clock_values))

    result, report = _exec_mod._run_native_streaming(
        table=_TABLE,
        plan=None,  # discarded immediately (`del plan`); unused by real logic
        config=config,
        column_order=column_order,
        first_batch=first_batch,
        rest_batches=iter(()),
        sink=None,
        streaming=False,
        table_kinds=table_kinds,
    )

    assert report.attempted is True
    assert report.admitted is True
    assert report.table == _TABLE
    assert report.reason is None
    ledger = report.ledger
    assert ledger is not None
    assert ledger.native_attempted == 3
    assert ledger.native_completed == 3
    assert ledger.native_rows_attempted == 6
    assert ledger.native_rows_completed == 6
    assert len(ledger.records) == 3
    assert all(r.table == _TABLE for r in ledger.records)
    assert {r.node for r in ledger.records} == {"pt", "rd", "tr"}

    timing_by_column = {r.column: r for r in result.timings}
    assert timing_by_column.keys() == {"pt", "rd", "tr"}
    assert timing_by_column["pt"].strategy_type == "passthrough"
    assert timing_by_column["pt"].elapsed_ms == 2000.0
    assert timing_by_column["rd"].strategy_type == "redact"
    assert timing_by_column["rd"].elapsed_ms == 5000.0
    assert timing_by_column["tr"].strategy_type == "truncate"
    assert timing_by_column["tr"].elapsed_ms == 4000.0
    assert all(r.peak_memory_delta_kb == 0 for r in result.timings)

    # batch_total (100 - 0) minus the summed per-column elapsed (2+5+4=11),
    # scaled to ms: (100 - 11) * 1000.0.
    assert result.boundary_conversion_ms == 89000.0

    assert result.outputs[_TABLE].column("pt").to_pylist() == ["a", "b"]
    assert result.outputs[_TABLE].column("rd").to_pylist() == ["ZZZ", "ZZZ"]
    assert result.outputs[_TABLE].column("tr").to_pylist() == ["###lo", "###ld"]

    assert result.table_kinds == table_kinds
    assert result.row_errors == ()
    assert result.quality_metrics == {
        "execution": {
            "execution_mode": "native",
            "route_reason": "native_route_admitted",
            "eviction": "per_batch",
            "outputs_streamed": False,
            "loaded_fully_in_memory": False,
        }
    }


def test_mask_one_batch_accumulates_across_calls_and_floors_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-batch run can't tell `+=` from `=`, or a `0.0` floor from a
    `1.0` one, when the starting accumulator is already zero and the real
    contribution dwarfs the floor -- both collapse to the same number. Two
    calls sharing one ledger/timing_acc/boundary_ms_box (as a real multi-batch
    job would) plus a sub-1.0 assembly gap make both distinguishable, and a
    nonzero clock epoch stops a `+`/`-` sign flip on `batch_total` from
    happening to cancel out against a zero epoch.
    """
    out_schema = pa.schema([pa.field("pt", pa.utf8())])
    strategy_cfg: dict[str, tuple[str, dict[str, Any]]] = {"pt": ("passthrough", {})}
    ledger = NativeRouteLedger()
    timing_acc: dict[tuple[str, str], float] = {}
    boundary_ms_box = [0.0]

    # Call 1: t_batch0=1000.0, t0=1000.0, elapsed-read=1000.5 (elapsed=0.5),
    # batch_total-read=1000.75 (batch_total=0.75). Assembly gap = 0.25s.
    # Every value is an exact binary fraction (halves/quarters/eighths) so
    # the arithmetic below has no floating-point rounding to account for.
    monkeypatch.setattr(
        _exec_mod.time, "perf_counter", _fake_clock([1000.0, 1000.0, 1000.5, 1000.75])
    )
    _exec_mod._mask_one_batch(
        pa.record_batch({"pt": pa.array(["a"], type=pa.utf8())}),
        0,
        table=_TABLE,
        column_order=("pt",),
        strategy_cfg=strategy_cfg,
        out_schema=out_schema,
        ledger=ledger,
        timing_acc=timing_acc,
        boundary_ms_box=boundary_ms_box,
    )
    assert timing_acc[("passthrough", "pt")] == 500.0
    assert boundary_ms_box[0] == 250.0

    # Call 2 (same column, same accumulators): t_batch0=2000.0, t0=2000.0,
    # elapsed-read=2000.25 (elapsed=0.25), batch_total-read=2000.375 (=0.375).
    # Assembly gap = 0.125s. Real result must ADD to call 1's numbers.
    monkeypatch.setattr(
        _exec_mod.time, "perf_counter", _fake_clock([2000.0, 2000.0, 2000.25, 2000.375])
    )
    _exec_mod._mask_one_batch(
        pa.record_batch({"pt": pa.array(["b"], type=pa.utf8())}),
        1,
        table=_TABLE,
        column_order=("pt",),
        strategy_cfg=strategy_cfg,
        out_schema=out_schema,
        ledger=ledger,
        timing_acc=timing_acc,
        boundary_ms_box=boundary_ms_box,
    )
    assert timing_acc[("passthrough", "pt")] == 750.0
    assert boundary_ms_box[0] == 375.0


# ---------------------------------------------------------------------------
# _validate_ledger
# ---------------------------------------------------------------------------


def test_validate_ledger_raises_on_attempted_completed_mismatch() -> None:
    ledger = NativeRouteLedger(native_attempted=2, native_completed=1)
    with pytest.raises(_exec_mod.ExecutionError) as excinfo:
        _exec_mod._validate_ledger(ledger, table=_TABLE)
    assert excinfo.value.code == "native_route_ledger_invalid"


@pytest.mark.parametrize(
    "field_name", ["oracle_calls", "oracle_rows", "fallback_calls", "fallback_rows"]
)
def test_validate_ledger_raises_on_any_single_nonzero_oracle_or_fallback_count(
    field_name: str,
) -> None:
    """Each of the four counts independently must trigger the raise -- an
    `or`->`and` regression on any pair of them would require BOTH nonzero
    before raising, silently accepting a ledger that proves oracle/fallback
    activity on a lane with no call site for either."""
    ledger = NativeRouteLedger(native_attempted=0, native_completed=0, **{field_name: 1})
    with pytest.raises(_exec_mod.ExecutionError) as excinfo:
        _exec_mod._validate_ledger(ledger, table=_TABLE)
    assert excinfo.value.code == "native_route_ledger_invalid"


# ---------------------------------------------------------------------------
# try_native_route (the glue: which report fields each reroute carries)
# ---------------------------------------------------------------------------
#
# The production-seam suite drives every reroute REASON through `run_pipeline`
# already; what it does not pin is the report's `attempted`/`table` fields on
# those same reroutes -- `static_candidacy` and `peek_and_admit` are stubbed
# out here so each branch is reachable without a real source or config.


def test_try_native_route_candidacy_decline_report_is_attempted_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _exec_mod,
        "static_candidacy",
        lambda **kwargs: _route_mod.NativeStaticCandidacy(
            candidate=False, table=None, reason="some_static_reason", sink_mode=None
        ),
    )
    result, report = _exec_mod.try_native_route(
        config={},
        plan=None,
        table_kinds={},
        caller_sources={},
        source_loader=None,
        sink=None,
        fidelity_report=False,
        execution_mode="auto",
        graph=_EMPTY_GRAPH,
    )
    assert result is None
    assert report.attempted is False
    assert report.admitted is False
    assert report.table is None
    assert report.reason == "some_static_reason"
    assert report.ledger is None


def test_try_native_route_admission_decline_report_is_attempted_true_with_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A batch WAS peeked (attempted=True) even though it was not admitted --
    and the report must still carry which table that peek happened on."""
    source = _lazy_source(tmp_path)
    monkeypatch.setattr(
        _exec_mod,
        "static_candidacy",
        lambda **kwargs: _route_mod.NativeStaticCandidacy(
            candidate=True, table=_TABLE, reason=None, sink_mode="resident"
        ),
    )
    monkeypatch.setattr(
        _exec_mod,
        "peek_and_admit",
        lambda *a, **k: _route_mod.NativeBatchAdmission(
            admitted=False,
            reason="zero_row_source",
            column_order=(),
            first_batch=None,
            rest=None,
        ),
    )
    result, report = _exec_mod.try_native_route(
        config={},
        plan=None,
        table_kinds={},
        caller_sources={_TABLE: source},
        source_loader=None,
        sink=None,
        fidelity_report=False,
        execution_mode="auto",
        graph=_EMPTY_GRAPH,
    )
    assert result is None
    assert report.attempted is True
    assert report.admitted is False
    assert report.table == _TABLE
    assert report.reason == "zero_row_source"
    assert report.ledger is None


def test_try_native_route_threads_table_kinds_into_run_native_streaming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`table_kinds` reaches `_run_native_streaming` unchanged -- the only way
    to observe this without running the real kernels is to stub the callee
    and inspect what it was actually called with."""
    source = _lazy_source(tmp_path)
    captured: dict[str, Any] = {}

    def _fake_run_native_streaming(**kwargs: Any) -> tuple[str, str]:
        captured.update(kwargs)
        return "RESULT_SENTINEL", "REPORT_SENTINEL"

    monkeypatch.setattr(
        _exec_mod,
        "static_candidacy",
        lambda **kwargs: _route_mod.NativeStaticCandidacy(
            candidate=True, table=_TABLE, reason=None, sink_mode="resident"
        ),
    )
    batch = pa.record_batch({"c": pa.array(["x"], type=pa.utf8())})
    monkeypatch.setattr(
        _exec_mod,
        "peek_and_admit",
        lambda *a, **k: _route_mod.NativeBatchAdmission(
            admitted=True, reason=None, column_order=("c",), first_batch=batch, rest=iter(())
        ),
    )
    monkeypatch.setattr(_exec_mod, "_run_native_streaming", _fake_run_native_streaming)

    table_kinds = {_TABLE: "mask"}
    result, report = _exec_mod.try_native_route(
        config={},
        plan=None,
        table_kinds=table_kinds,
        caller_sources={_TABLE: source},
        source_loader=None,
        sink=None,
        fidelity_report=False,
        execution_mode="auto",
        graph=_EMPTY_GRAPH,
    )
    assert result == "RESULT_SENTINEL"
    assert report == "REPORT_SENTINEL"
    assert captured["table_kinds"] == table_kinds
    assert captured["table_kinds"] is table_kinds
