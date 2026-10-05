"""C4 acceptance: admission, routing, boundaries and evidence for chunked date_shift.

Covers the lifted veto and its mirrors, the exact-`string` native source domain and the
oracle fallback for everything else, route evidence (including the honest executed backend
for the three chunk shapes that run no compiled kernel), the companion-absent downgrade,
the branch arguments, the `derive_calls` spy contract, and the forced-oracle helper that
replaced date_shift as the oracle-forcing stand-in.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._chunked_output_sink import OutputEvidenceAccumulator
from decoy_engine.execution._planner import _whole_column_state_rejections
from decoy_engine.execution.native import _chunk_masking, _dispatch
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._chunked_evidence import plan_column_backends
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._date_shift_ext import native_date_shift
from decoy_engine.execution.native._phase3_eligibility import phase3_c1_eligibility
from decoy_engine.execution.native._requirements import CHUNKED_ROUTE_VETOED_STRATEGIES
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import identical
from tests.native._chunked_date_shift_support import (
    FORCE,
    assert_same_as_oracle,
    date_value,
    ds_col,
    make_config,
    passthrough,
    run_one,
    run_outcome,
    run_pair,
    source,
    spy_index_kernel,
    with_force,
)
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    FORCE_ORACLE_VALUE,
    NEEDS_COMPANION,
    TABLE,
    force_oracle,
    key_provider,
    redact,
    split,
)

VETOED = "group_key_not_native_chunked_route"


def _check(config: dict[str, Any], table: str = TABLE) -> None:
    check_chunked_compatibility(config, table=table, registry=get_default_registry())


def _code(config: dict[str, Any], table: str = TABLE) -> str | None:
    try:
        _check(config, table)
    except PlanCompileError as exc:
        return exc.code
    return None


def _valued(n: int = 6) -> pa.Table:
    return source([date_value(i) for i in range(n)])


def _no_index_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Any:
        raise CryptoExtensionUnavailableError("index kernel unavailable for the test")

    monkeypatch.setattr(_dispatch, "load_compiled_index_kernel", _raise)


def _columns(agg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["column"]: c for c in agg["columns"]}


# ---------------------------------------------------------------------------
# 1. The veto is lifted for date_shift only, and every mirror agrees.
# ---------------------------------------------------------------------------


def test_the_chunked_veto_set_is_exactly_group_key() -> None:
    assert frozenset({"group_key"}) == CHUNKED_ROUTE_VETOED_STRATEGIES


def test_config_only_eligibility_mirror_admits_an_admissible_date_shift() -> None:
    result = phase3_c1_eligibility(make_config([ds_col(), passthrough("p")]), table=TABLE)
    assert not any("date_shift_not_native_chunked_route" in r for r in result.reasons)


def test_config_only_eligibility_mirror_still_vetoes_group_key() -> None:
    result = phase3_c1_eligibility(make_config([ds_col(), force_oracle("b")]), table=TABLE)
    assert result.admitted is False
    assert f"{VETOED}:b" in result.reasons
    assert not any(r.startswith("date_shift_not_native_chunked_route") for r in result.reasons)


def test_static_route_decision_no_longer_emits_the_date_shift_veto() -> None:
    from decoy_engine.execution._chunked_profile import first_chunk_profile

    config = make_config([ds_col(), passthrough("p")])
    profile = first_chunk_profile(_valued(), table=TABLE, engine_version=ENGINE_VERSION)
    decision = _dispatch._static_route_decision(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert "date_shift_not_native_chunked_route" not in (decision.reroute_reason or "")
    assert decision.native_admitted is True


def test_the_evidence_planner_plans_the_companion_for_date_shift() -> None:
    from decoy_engine.execution._chunked_profile import first_chunk_profile

    config = make_config([ds_col(), passthrough("p")])
    profile = first_chunk_profile(_valued(), table=TABLE, engine_version=ENGINE_VERSION)
    plans = plan_column_backends(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert {p.column: p.planned_backend for p in plans}["d"] == "rust_companion"


@NEEDS_COMPANION
def test_admissible_date_shift_runs_natively_beside_native_siblings() -> None:
    table = _valued().append_column("s", pa.array(["x"] * 6, pa.string()))
    run = run_one(make_config([redact("s"), passthrough("p"), ds_col()]), split(table, 4))
    evidence = run.ev[0]
    assert evidence.native_admitted is True and evidence.reroute_reason is None
    routes = {n.column: n.route for n in evidence.node_routes}
    assert routes == {"s": "native_kernel", "p": "native_kernel", "d": "native_kernel"}, routes
    assert evidence.kernel_calls["date_shift"] == 2
    assert evidence.kernel_calls["redact"] == 2


def test_a_group_key_column_beside_date_shift_sends_the_table_to_the_oracle() -> None:
    config = make_config([ds_col(), force_oracle(FORCE), passthrough("p")])
    run = run_one(config, [with_force(c) for c in split(_valued(), 4)])
    assert run.ev[0].native_admitted is False
    assert f"{VETOED}:{FORCE}" in (run.ev[0].reroute_reason or "")
    assert "date_shift_not_native_chunked_route" not in (run.ev[0].reroute_reason or "")


# ---------------------------------------------------------------------------
# 2. The native source domain is exactly `string`; everything else keeps the oracle.
# ---------------------------------------------------------------------------


def test_real_type_gate_accepts_string_and_names_every_other_type() -> None:
    from decoy_engine.execution.native._real_type_admission import date_shift_source_type_rejection

    schema = pa.schema(
        [("a", pa.string()), ("b", pa.large_string()), ("c", pa.int64()), ("e", pa.date32())]
    )
    assert date_shift_source_type_rejection("a", schema) is None
    assert date_shift_source_type_rejection("b", schema) == (
        "date_shift_source_type_not_string:b:large_string"
    )
    assert date_shift_source_type_rejection("c", schema) == (
        "date_shift_source_type_not_string:c:int64"
    )
    assert date_shift_source_type_rejection("e", schema) == (
        "date_shift_source_type_not_string:e:date32[day]"
    )


@NEEDS_COMPANION
def test_a_large_string_source_runs_the_oracle_leg_and_matches_full_run(tmp_path: Path) -> None:
    chunks = split(source([date_value(i) for i in range(7)] + [None], typ=pa.large_string()), 3)
    native, forced = run_pair([ds_col(), passthrough("p")], chunks)
    assert native.ev[0].native_admitted is False
    assert "date_shift_source_type_not_string:d:large_string" in (native.ev[0].reroute_reason or "")
    assert native.ev[0].kernel_calls == {}
    assert len(native.out) == len(forced.out)
    for got, want in zip(native.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


@NEEDS_COMPANION
def test_a_non_string_source_fails_closed_through_the_oracle_not_the_kernel() -> None:
    """An int64 source never reaches the compiled kernel: the profile-typed static gate
    declines it (`fallback_policy_not_native`) before the real-type gate is consulted. The
    oracle's own behavior for it (every value is unparseable under the format) is
    reproduced exactly: same error."""
    chunks = [pa.table({"d": pa.array([20240101, None], pa.int64()), "p": pa.array([1, 2])})]
    native = run_outcome(make_config([ds_col(), passthrough("p")]), chunks)
    forced = run_outcome(
        make_config([ds_col(), passthrough("p"), force_oracle(FORCE)]),
        [with_force(c) for c in chunks],
    )
    assert native.ev[0].native_admitted is False
    assert "fallback_policy_not_native:d" in (native.ev[0].reroute_reason or "")
    assert isinstance(native.error, RowErrorsFailedError)
    assert isinstance(forced.error, RowErrorsFailedError)
    assert native.error.records == forced.error.records
    assert native.ev[0].kernel_calls == {}


@NEEDS_COMPANION
def test_a_zero_row_large_string_chunk_keeps_the_oracle_type_it_has_today() -> None:
    """A non-admissible source is outside the string pin, so it keeps the type the oracle
    gives a zero-row chunk (`double`) exactly as before the veto lifted."""
    chunks = [source([], typ=pa.large_string())]
    native, forced = run_pair([ds_col(), passthrough("p")], chunks)
    assert native.ev[0].native_admitted is False
    assert native.out[0].schema.field("d").type == pa.float64()
    assert identical(native.out[0], forced.out[0].drop_columns([FORCE]))


# ---------------------------------------------------------------------------
# 3. Companion-absent: date_shift reroutes to the oracle, byte-identical.
# ---------------------------------------------------------------------------


def test_a_missing_index_companion_plans_rust_and_executes_on_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_index_kernel(monkeypatch)
    chunks = split(source([date_value(i) for i in range(5)] + [None]), 3)
    run = run_one(make_config([ds_col(), passthrough("p")]), chunks)
    assert run.ev[0].native_admitted is False
    assert "index_extension_unavailable" in (run.ev[0].reroute_reason or "")
    assert run.ev[0].compiled_kernel_executed is False
    col = _columns(aggregate_chunked_route_evidence(run.sink))["d"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "pandas_oracle"


def test_companion_absent_run_matches_a_forced_oracle_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_index_kernel(monkeypatch)
    chunks = [
        source([date_value(i) for i in range(4)]),
        source([None, None]),
        source([]),
        source([date_value(9)]),
    ]
    absent = run_one(make_config([ds_col(), passthrough("p")]), chunks)
    forced = run_one(
        make_config([ds_col(), passthrough("p"), force_oracle(FORCE)]),
        [with_force(c) for c in chunks],
    )
    assert absent.ev[0].native_admitted is False
    assert forced.ev[0].native_admitted is False
    assert f"{VETOED}:{FORCE}" in (forced.ev[0].reroute_reason or "")
    assert len(absent.out) == len(forced.out)
    for got, want in zip(absent.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


# ---------------------------------------------------------------------------
# 4. Evidence: backend, compiled execution, branch counter vs compiled work.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_admitted_date_shift_reports_rust_companion_and_one_branch_call_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = spy_index_kernel(monkeypatch)
    chunks = split(source([date_value(i) for i in range(9)]), 4)
    run = run_one(make_config([ds_col(), passthrough("p")]), chunks)
    evidence = run.ev[0]
    assert evidence.native_admitted is True
    assert evidence.compiled_kernel_executed is True
    assert evidence.kernel_calls["date_shift"] == len(chunks) == 3
    assert len(spy.pool_sizes()) == 3, "one derive call per parseable chunk"
    col = _columns(aggregate_chunked_route_evidence(run.sink))["d"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "rust_companion"
    assert col["calls"] == len(chunks)


@NEEDS_COMPANION
def test_kernel_calls_scale_with_date_shift_columns_times_chunks() -> None:
    table = pa.table(
        {
            "a": pa.array([date_value(i) for i in range(8)], pa.string()),
            "b": pa.array([date_value(i + 3) for i in range(8)], pa.string()),
        }
    )
    columns = [ds_col("a", namespace="ns_a"), ds_col("b", namespace="ns_b")]
    run = run_one(make_config(columns), split(table, 3))
    assert run.ev[0].native_admitted is True
    assert run.ev[0].kernel_calls["date_shift"] == 2 * 3


_DEGENERATE = {
    "empty": [source([]), source([])],
    "all_null": [source([None] * 3), source([None] * 2)],
    "mixed_degenerate": [source([]), source([None] * 3)],
}


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_DEGENERATE))
def test_a_column_whose_every_chunk_ran_no_kernel_does_not_claim_the_companion(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_index_kernel(monkeypatch)
    chunks = _DEGENERATE[case]
    run = run_one(make_config([ds_col(), passthrough("p")]), chunks)
    evidence = run.ev[0]
    assert evidence.native_admitted is True
    assert evidence.kernel_calls["date_shift"] == len(chunks), "the branch counter still counts"
    assert evidence.compiled_kernel_executed is False
    assert spy.pool_sizes() == []
    col = _columns(aggregate_chunked_route_evidence(run.sink))["d"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "arrow_python"
    assert col["calls"] == len(chunks)


@NEEDS_COMPANION
def test_an_all_unparseable_chunk_claims_no_compiled_work_even_though_the_run_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = spy_index_kernel(monkeypatch)
    out = run_outcome(make_config([ds_col(), passthrough("p")]), [source(["junk", "more"])])
    assert isinstance(out.error, RowErrorsFailedError)
    assert out.ev[0].compiled_kernel_executed is False
    assert out.ev[0].kernel_calls["date_shift"] == 1
    assert spy.pool_sizes() == []
    col = _columns(out.sink[-1].quality_metrics["chunked_route"])["d"]
    assert col["executed_backend"] == "arrow_python"


def _both_aggregates(run: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    resident = _columns(aggregate_chunked_route_evidence(run.sink))["d"]
    acc = OutputEvidenceAccumulator()
    for result in run.sink:
        acc.append(result)
    return resident, _columns(acc.route_evidence())["d"]


_ORDERINGS = {
    "degenerate_then_valued": [source([None, None]), source([date_value(1), date_value(2)])],
    "valued_then_degenerate": [source([date_value(1), date_value(2)]), source([None, None])],
    "degenerate_valued_degenerate": [source([]), source([date_value(3)]), source([None])],
}


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_ORDERINGS))
def test_one_valued_chunk_makes_the_column_report_rust_on_both_aggregation_paths(
    case: str,
) -> None:
    run = run_one(make_config([ds_col(), passthrough("p")]), _ORDERINGS[case])
    assert run.ev[0].compiled_kernel_executed is True
    resident, streamed = _both_aggregates(run)
    for col in (resident, streamed):
        assert col["executed_backend"] == "rust_companion", case
        assert col["calls"] == len(_ORDERINGS[case])
    assert resident["executed_backend"] == streamed["executed_backend"]


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_DEGENERATE))
def test_all_degenerate_columns_report_arrow_python_on_both_aggregation_paths(case: str) -> None:
    run = run_one(make_config([ds_col(), passthrough("p")]), _DEGENERATE[case])
    resident, streamed = _both_aggregates(run)
    assert resident["executed_backend"] == streamed["executed_backend"] == "arrow_python"


# ---------------------------------------------------------------------------
# 5. The kernel's optional derive_calls spy and the branch arguments.
# ---------------------------------------------------------------------------


class _CountingKernel:
    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls = 0

    def derive_index_batch(self, values: Any, **kw: Any) -> Any:
        self.calls += 1
        return self.real.derive_index_batch(values, **kw)


@NEEDS_COMPANION
def test_native_date_shift_keeps_its_two_tuple_return_and_reports_derive_calls_on_request() -> None:
    kernel = _CountingKernel(_dispatch.load_compiled_index_kernel())
    common: dict[str, Any] = {
        "min_days": -3,
        "max_days": 3,
        "date_format": "%Y-%m-%d",
        "mask_key": bytes(range(32)),
        "namespace": "ns",
        "index_kernel": kernel,
    }
    valued = pa.array([date_value(1), None, "junk"], pa.string())
    result = native_date_shift(valued, **common)
    assert isinstance(result, tuple) and len(result) == 2
    assert result[1] == (2,)
    assert kernel.calls == 1

    for values, expected in (
        (valued, 1),
        (pa.array([], pa.string()), 0),
        (pa.array([None, None], pa.string()), 0),
        (pa.array(["junk", "more"], pa.string()), 0),
    ):
        calls: list[int] = []
        before = kernel.calls
        native_date_shift(values, derive_calls=calls, **common)
        assert sum(calls) == expected
        assert kernel.calls - before == expected


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 3])
def test_the_branch_passes_exact_config_and_reuses_the_preflight_index_kernel(
    threads: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = spy_index_kernel(monkeypatch)
    seen: list[dict[str, Any]] = []
    real = _chunk_masking.native_date_shift

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunk_masking, "native_date_shift", spy)
    chunks = split(source([date_value(i) for i in range(7)]), 3)
    config = make_config(
        [ds_col(date_format="%Y-%m-%d", min_days=-5, max_days=9), passthrough("p")]
    )
    run_one(config, chunks, native_threads=threads)
    assert len(seen) == len(chunks)
    for kwargs in seen:
        assert kwargs["min_days"] == -5
        assert kwargs["max_days"] == 9
        assert kwargs["date_format"] == "%Y-%m-%d"
        assert kwargs["namespace"] == "ns_d"
        assert kwargs["native_threads"] == threads
        assert kwargs["index_kernel"] is kernel
        assert isinstance(kwargs["derive_calls"], list)
    assert len({id(k["index_kernel"]) for k in seen}) == 1


# ---------------------------------------------------------------------------
# 6. Upstream boundaries are unchanged.
# ---------------------------------------------------------------------------


def test_a_when_predicate_keeps_the_table_off_the_native_route_and_off_auto_chunk() -> None:
    config = make_config([ds_col(), passthrough("p")])
    config["tables"][0]["columns"][0]["when"] = "d != ''"
    run = run_one(config, [_valued()])
    assert run.ev[0].native_admitted is False
    assert "when_predicate_not_native" in (run.ev[0].reroute_reason or "")
    joined = "; ".join(_whole_column_state_rejections(config, table=TABLE))
    assert "when_predicate_not_chunk_stable" in joined


def test_autodetect_date_shift_stays_off_the_auto_chunk_route() -> None:
    config = make_config([ds_col(date_format=None), passthrough("p")])
    assert "date_shift_requires_explicit_format" in "; ".join(
        _whole_column_state_rejections(config, table=TABLE)
    )


def _fk(parent_cols: list[dict[str, Any]], child_cols: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tables": [
            {"name": "parent", "columns": parent_cols},
            {"name": "child", "columns": child_cols},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "remap",
            }
        ],
    }


def test_a_date_shift_fk_self_mask_edge_keeps_its_existing_codes() -> None:
    both = _fk([ds_col("id", namespace="fk_ns")], [ds_col("pid", namespace="fk_ns")])
    assert _code(both, "child") == "chunked_fk_parent_strategy_not_self_mask_safe"
    parent = {"name": "id", "strategy": "hash", "namespace": "fk_ns"}
    mismatch = _fk([parent], [ds_col("pid", namespace="fk_ns")])
    assert _code(mismatch, "child") == "chunked_fk_child_strategy_mismatch"


# ---------------------------------------------------------------------------
# 7. Auto-router end to end: no success or failure outcome changes.
# ---------------------------------------------------------------------------

_ROWS = 10
_PARSEABLE = [date_value(i) for i in range(_ROWS)]


def _auto_source(kind: str) -> pa.Table:
    column = {
        "string": pa.array(_PARSEABLE, pa.string()),
        "unparseable": pa.array([*_PARSEABLE[:6], "not-a-date", *_PARSEABLE[7:]], pa.string()),
        "int64": pa.array(range(_ROWS), pa.int64()),
    }[kind]
    return pa.table({"d": column, "p": pa.array(range(_ROWS), pa.int64())})


def _auto_config(kind: str, tmp_path: Path) -> dict[str, Any]:
    cfg = make_config([ds_col(), passthrough("p")])
    path = str(tmp_path / f"{kind}.parquet")
    pq.write_table(_auto_source(kind), path)
    cfg["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    cfg["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    return cfg


def _auto(cfg: dict[str, Any], kind: str, **kw: Any) -> Any:
    return run_pipeline(
        copy.deepcopy(cfg),
        {TABLE: _auto_source(kind)},
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **kw,
    )


def _outcome(fn: Any) -> Any:
    try:
        return fn()
    except Exception as exc:
        return exc


@NEEDS_COMPANION
def test_a_parseable_string_column_auto_routes_to_the_native_leg_and_equals_full_frame(
    tmp_path: Path,
) -> None:
    cfg = _auto_config("string", tmp_path)
    auto = _auto(cfg, "string", auto_chunk_threshold_rows=3, chunk_size_rows=4)
    full = _auto(cfg, "string", auto_chunk=False)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert auto.quality_metrics["chunked_route"]["native_admitted"] is True
    col = _columns(auto.quality_metrics["chunked_route"])["d"]
    assert col["executed_backend"] == "rust_companion"
    a, f = auto.outputs[TABLE], full.outputs[TABLE]
    assert a.schema.names == f.schema.names
    assert [fl.type for fl in a.schema] == [fl.type for fl in f.schema]
    assert a.column("d").to_pylist() == f.column("d").to_pylist()
    assert a.column("d").to_pylist() != _PARSEABLE


@NEEDS_COMPANION
def test_a_non_string_column_auto_routes_to_the_oracle_leg_with_the_full_frame_outcome(
    tmp_path: Path,
) -> None:
    cfg = _auto_config("int64", tmp_path)
    auto = _outcome(lambda: _auto(cfg, "int64", auto_chunk_threshold_rows=3, chunk_size_rows=4))
    full = _outcome(lambda: _auto(cfg, "int64", auto_chunk=False))
    assert type(auto) is type(full)
    assert isinstance(auto, RowErrorsFailedError)
    assert auto.records[0].trigger == full.records[0].trigger == "format_error"


@NEEDS_COMPANION
def test_an_unparseable_value_fails_closed_on_the_native_leg_like_the_full_frame(
    tmp_path: Path,
) -> None:
    cfg = _auto_config("unparseable", tmp_path)
    auto = _outcome(
        lambda: _auto(cfg, "unparseable", auto_chunk_threshold_rows=3, chunk_size_rows=4)
    )
    full = _outcome(lambda: _auto(cfg, "unparseable", auto_chunk=False))
    assert isinstance(auto, RowErrorsFailedError)
    assert isinstance(full, RowErrorsFailedError)
    assert [(r.table, r.column, r.trigger, r.reason) for r in auto.records] == [
        (r.table, r.column, r.trigger, r.reason) for r in full.records
    ]
    assert len(auto.records) == len(full.records) == 1
    # The value sits at table row 6, which is index 2 of the second chunk of 4. The chunked
    # leg reports the chunk-local position; the whole-frame route reports the table position.
    assert auto.records[0].row_index == 2
    assert full.records[0].row_index == 6


# ---------------------------------------------------------------------------
# 8. The forced-oracle helper is the single stand-in, and it keeps forcing the oracle.
# ---------------------------------------------------------------------------


def test_force_oracle_emits_the_group_key_self_anchor() -> None:
    assert force_oracle("x") == {
        "name": "x",
        "strategy": "group_key",
        "provider_config": {"group_by": "x"},
    }


@pytest.mark.parametrize("name", [FORCE, "other"])
def test_force_oracle_routes_to_the_oracle_with_the_group_key_reason(name: str) -> None:
    table = pa.table({name: pa.array([FORCE_ORACLE_VALUE] * 3, pa.string())})
    ev: list[Any] = []
    from decoy_engine import run_mask_chunked

    list(
        run_mask_chunked(
            make_config([force_oracle(name)]),
            [table],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=ev,
        )
    )
    assert ev[0].native_admitted is False
    assert f"group_key_not_native_chunked_route:{name}" in (ev[0].reroute_reason or "")


@NEEDS_COMPANION
def test_a_real_date_shift_column_is_not_the_forcing_stand_in() -> None:
    """Only the forcing column keeps the table on the oracle; the date_shift column beside
    it is what the comparison measures."""
    native, forced = run_pair([ds_col(), passthrough("p")], split(_valued(), 4))
    assert_same_as_oracle(native, forced)
    assert {t.strategy_type for r in native.sink for t in r.timings} == {
        "date_shift",
        "passthrough",
    }
    assert {t.strategy_type for r in forced.sink for t in r.timings} == {
        "date_shift",
        "passthrough",
        "group_key",
    }
