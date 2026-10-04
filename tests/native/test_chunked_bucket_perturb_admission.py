"""C2 acceptance: admission, routing, error boundaries and evidence for chunked bucket_perturb.

Covers the lifted veto and its mirrors, the exact-`string` native source domain, the exact
code of every non-happy boundary, route evidence (including the honest executed backend
when no compiled kernel ran), the companion-absent downgrade, and the branch arguments.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._chunked_output_sink import OutputEvidenceAccumulator
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native import _chunk_masking, _dispatch
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._phase3_eligibility import phase3_c1_eligibility
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import identical, run_one
from tests.native._chunked_bucket_perturb_support import (
    FORCE,
    assert_same_as_oracle,
    bp_col,
    date_value,
    make_config,
    passthrough,
    run_pair,
    source,
    spy_index_kernel,
    with_force,
)
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    force_oracle,
    key_provider,
    redact,
    split,
)

VETOED = "date_shift_not_native_chunked_route"


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


# ---------------------------------------------------------------------------
# 1. The veto is lifted for bucket_perturb only, and every mirror agrees.
# ---------------------------------------------------------------------------


def test_bucket_perturb_is_not_in_the_chunked_veto_set() -> None:
    from decoy_engine.execution.native._requirements import CHUNKED_ROUTE_VETOED_STRATEGIES

    assert "bucket_perturb" not in CHUNKED_ROUTE_VETOED_STRATEGIES
    assert {"group_key", "date_shift"} == CHUNKED_ROUTE_VETOED_STRATEGIES


def test_config_only_eligibility_mirror_admits_an_admissible_bucket_perturb() -> None:
    result = phase3_c1_eligibility(make_config([bp_col(), passthrough("p")]), table=TABLE)
    assert not any("bucket_perturb_not_native_chunked_route" in r for r in result.reasons)


def test_config_only_eligibility_mirror_still_vetoes_the_other_strategies() -> None:
    result = phase3_c1_eligibility(make_config([bp_col(), force_oracle("b")]), table=TABLE)
    assert result.admitted is False
    assert f"{VETOED}:b" in result.reasons
    assert not any(r.startswith("bucket_perturb_not_native_chunked_route") for r in result.reasons)


def test_static_route_decision_no_longer_emits_the_bucket_perturb_veto() -> None:
    from decoy_engine.execution._chunked_profile import first_chunk_profile

    config = make_config([bp_col(), passthrough("p")])
    profile = first_chunk_profile(_valued(), table=TABLE, engine_version=ENGINE_VERSION)
    decision = _dispatch._static_route_decision(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert "bucket_perturb_not_native_chunked_route" not in (decision.reroute_reason or "")
    assert decision.native_admitted is True


@NEEDS_COMPANION
def test_admissible_bucket_perturb_runs_natively_beside_native_siblings() -> None:
    table = _valued().append_column("s", pa.array(["x"] * 6, pa.string()))
    run = run_one(make_config([redact("s"), passthrough("p"), bp_col()]), split(table, 4))
    evidence = run.ev[0]
    assert evidence.native_admitted is True and evidence.reroute_reason is None
    routes = {n.column: n.route for n in evidence.node_routes}
    assert routes == {"s": "native_kernel", "p": "native_kernel", "d": "native_kernel"}, routes
    assert evidence.kernel_calls["bucket_perturb"] == 2
    assert evidence.kernel_calls["redact"] == 2


def test_a_still_vetoed_column_beside_bucket_perturb_sends_the_table_to_the_oracle() -> None:
    config = make_config([bp_col(), force_oracle(FORCE), passthrough("p")])
    run = run_one(config, [with_force(c) for c in split(_valued(), 4)])
    assert run.ev[0].native_admitted is False
    assert VETOED in (run.ev[0].reroute_reason or "")
    assert "bucket_perturb_not_native_chunked_route" not in (run.ev[0].reroute_reason or "")


# ---------------------------------------------------------------------------
# 2. The native source domain is exactly `string`.
# ---------------------------------------------------------------------------


def test_real_type_gate_accepts_string_and_names_every_other_type() -> None:
    from decoy_engine.execution.native._real_type_admission import (
        bucket_perturb_source_type_rejection,
    )

    schema = pa.schema(
        [("a", pa.string()), ("b", pa.large_string()), ("c", pa.int64()), ("e", pa.date32())]
    )
    assert bucket_perturb_source_type_rejection("a", schema) is None
    assert bucket_perturb_source_type_rejection("b", schema) == (
        "bucket_perturb_source_type_not_string:b:large_string"
    )
    assert bucket_perturb_source_type_rejection("c", schema) == (
        "bucket_perturb_source_type_not_string:c:int64"
    )
    assert bucket_perturb_source_type_rejection("e", schema) == (
        "bucket_perturb_source_type_not_string:e:date32[day]"
    )


@NEEDS_COMPANION
def test_a_large_string_source_declines_natively_and_matches_the_oracle() -> None:
    chunks = split(source([date_value(i) for i in range(7)] + [None], typ=pa.large_string()), 3)
    native, forced = run_pair([bp_col(), passthrough("p")], chunks)
    assert native.ev[0].native_admitted is False
    assert "bucket_perturb_source_type_not_string:d:large_string" in (
        native.ev[0].reroute_reason or ""
    )
    assert native.ev[0].kernel_calls == {}
    assert len(native.out) == len(forced.out)
    for got, want in zip(native.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


# ---------------------------------------------------------------------------
# 3. Boundary case table: each non-happy case keeps its own exact code.
# ---------------------------------------------------------------------------


def _manual(config: dict[str, Any], chunks: list[pa.Table]) -> list[pa.Table]:
    return list(
        run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )


def test_non_string_source_on_the_manual_route_raises_the_source_dtype_code() -> None:
    table = pa.table({"d": pa.array([20240101, None], pa.int64()), "p": pa.array([1, 2])})
    with pytest.raises(PlanCompileError) as info:
        _manual(make_config([bp_col(), passthrough("p")]), split(table, 1))
    assert info.value.code == "chunked_bucket_perturb_source_dtype_unsupported"


def test_non_string_source_on_the_auto_route_falls_back_to_full_frame(tmp_path: Any) -> None:
    import pandas as pd

    from decoy_engine import run_pipeline

    table = pa.table({"d": pa.array([1, 2, 3, 4, 5, 6], pa.int64())})
    cfg = make_config([bp_col()])
    cfg["sources"][TABLE]["path"] = str(tmp_path / "t.csv")
    pd.DataFrame({"d": [1, 2, 3, 4, 5, 6]}).to_csv(tmp_path / "t.csv", index=False)
    result = run_pipeline(
        cfg,
        sources={TABLE: table},
        engine_version=ENGINE_VERSION,
        auto_chunk_threshold_rows=10,
        chunk_size_rows=2,
        key_provider=key_provider(),
    )
    assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"


@pytest.mark.parametrize("date_format", [None, ""], ids=["absent", "empty"])
def test_autodetect_fails_chunk_safety_with_its_exact_code(date_format: str | None) -> None:
    config = make_config([bp_col(date_format=date_format), passthrough("p")])
    assert _code(config) == "chunked_strategy_conditions_unmet"
    with pytest.raises(PlanCompileError) as info:
        _manual(config, [_valued()])
    assert info.value.code == "chunked_strategy_conditions_unmet"


@NEEDS_COMPANION
def test_an_invalid_but_truthy_format_raises_the_same_error_on_both_legs() -> None:
    columns = [bp_col(date_format="%Q"), passthrough("p")]
    table = source(["2024-01-15"])
    with pytest.raises(ValueError, match="bad directive") as native_exc:
        run_one(make_config(columns), [table])
    with pytest.raises(ValueError, match="bad directive") as oracle_exc:
        run_one(make_config([*columns, force_oracle(FORCE)]), [with_force(table)])
    assert type(native_exc.value) is type(oracle_exc.value)


@NEEDS_COMPANION
def test_a_timezone_directive_declines_natively_and_the_oracle_runs() -> None:
    values = ["2021-03-15 +0000", "2020-11-02 +0000", None, "2022-01-09 +0000"]
    native, forced = run_pair(
        [bp_col(date_format="%Y-%m-%d %z"), passthrough("p")], split(source(values), 2)
    )
    config = make_config([bp_col(date_format="%Y-%m-%d %z"), passthrough("p")])
    assert (
        "bucket_perturb_timezone_directive:d"
        in native_route_eligibility(config, table=TABLE).rejections
    )
    assert native.ev[0].native_admitted is False
    assert "fallback_policy_not_native:d" in (native.ev[0].reroute_reason or "")
    assert len(native.out) == len(forced.out)
    for got, want in zip(native.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


def test_an_invalid_bucket_declines_natively_then_the_oracle_raises_its_coded_error() -> None:
    config = make_config([bp_col(bucket="decade"), passthrough("p")])
    assert (
        "bucket_perturb_unsupported_bucket:d"
        in native_route_eligibility(config, table=TABLE).rejections
    )
    with pytest.raises(StrategyError) as info:
        run_one(config, [_valued()])
    assert info.value.code == "bucket_perturb_invalid_config"


def test_missing_namespace_fails_eagerly_before_any_chunk_is_masked() -> None:
    consumed: list[int] = []

    def stream() -> Any:
        for chunk in split(_valued(), 2):
            consumed.append(1)
            yield chunk

    with pytest.raises(StrategyError) as info:
        _manual(make_config([bp_col(namespace=None), passthrough("p")]), stream())  # type: ignore[arg-type]
    assert info.value.code == "bucket_perturb_requires_namespace"
    assert len(consumed) <= 1, "fails before any chunk is masked; only the profile peek reads one"


def test_a_when_predicate_is_rejected_with_its_exact_code() -> None:
    config = make_config([bp_col(when="d != ''"), passthrough("p")])
    assert _code(config) == "chunked_bucket_perturb_when_not_supported"


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


def _bp(name: str) -> dict[str, Any]:
    return bp_col(name, namespace="fk_ns")


def test_fk_parent_key_on_a_chunked_parent_has_its_single_code() -> None:
    cfg = _fk([_bp("id")], [{"name": "pid", "strategy": "bucket_perturb"}])
    assert _code(cfg, "parent") == "chunked_bucket_perturb_fk_key_unsupported"


def test_fk_both_keys_bucket_perturb_on_a_chunked_child_has_its_single_code() -> None:
    cfg = _fk([_bp("id")], [_bp("pid")])
    assert _code(cfg, "child") == "chunked_fk_parent_strategy_not_self_mask_safe"


def test_fk_hash_parent_with_bucket_perturb_child_has_its_single_code() -> None:
    parent = {"name": "id", "strategy": "hash", "namespace": "fk_ns"}
    cfg = _fk([parent], [_bp("pid")])
    assert _code(cfg, "child") == "chunked_fk_child_strategy_mismatch"


# ---------------------------------------------------------------------------
# 4. Companion-absent: bucket_perturb reroutes to the oracle, byte-identical.
# ---------------------------------------------------------------------------


def _columns(agg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["column"]: c for c in agg["columns"]}


def test_a_missing_index_companion_plans_rust_and_executes_on_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_index_kernel(monkeypatch)
    chunks = split(source([date_value(i) for i in range(5)] + [None, "junk"]), 3)
    run = run_one(make_config([bp_col(), passthrough("p")]), chunks)
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
        source([date_value(9), "junk"]),
    ]
    absent = run_one(make_config([bp_col(), passthrough("p")]), chunks)
    forced = run_one(
        make_config([bp_col(), passthrough("p"), force_oracle(FORCE)]),
        [with_force(c) for c in chunks],
    )
    assert absent.ev[0].native_admitted is False
    assert forced.ev[0].native_admitted is False
    assert VETOED in (forced.ev[0].reroute_reason or "")
    assert len(absent.out) == len(forced.out)
    for got, want in zip(absent.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


# ---------------------------------------------------------------------------
# 5. Evidence: backend, compiled execution, call counters.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_admitted_bucket_perturb_reports_rust_companion_and_one_branch_call_per_chunk() -> None:
    chunks = split(source([date_value(i) for i in range(9)]), 4)
    run = run_one(make_config([bp_col(), passthrough("p")]), chunks)
    evidence = run.ev[0]
    assert evidence.native_admitted is True
    assert evidence.compiled_kernel_executed is True
    assert evidence.kernel_calls["bucket_perturb"] == len(chunks) == 3
    col = _columns(aggregate_chunked_route_evidence(run.sink))["d"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "rust_companion"
    assert col["calls"] == len(chunks)


@NEEDS_COMPANION
def test_kernel_calls_scale_with_bucket_columns_times_chunks() -> None:
    table = pa.table(
        {
            "a": pa.array([date_value(i) for i in range(8)], pa.string()),
            "b": pa.array([date_value(i + 3) for i in range(8)], pa.string()),
        }
    )
    columns = [bp_col("a", namespace="ns_a"), bp_col("b", bucket="week", namespace="ns_b")]
    run = run_one(make_config(columns), split(table, 3))
    assert run.ev[0].native_admitted is True
    assert run.ev[0].kernel_calls["bucket_perturb"] == 2 * 3


_DEGENERATE = {
    "empty": [source([]), source([])],
    "all_null": [source([None] * 3), source([None] * 2)],
    "all_unparseable": [source(["junk", "more"]), source(["2021-13-45"])],
    "mixed_degenerate": [source([]), source([None] * 3), source(["junk"])],
}


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_DEGENERATE))
def test_a_column_whose_every_chunk_ran_no_kernel_does_not_claim_the_companion(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_index_kernel(monkeypatch)
    chunks = _DEGENERATE[case]
    run = run_one(make_config([bp_col(), passthrough("p")]), chunks)
    evidence = run.ev[0]
    assert evidence.native_admitted is True
    assert evidence.kernel_calls["bucket_perturb"] == len(chunks)
    assert evidence.compiled_kernel_executed is False
    assert spy.pool_sizes() == []
    col = _columns(aggregate_chunked_route_evidence(run.sink))["d"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "arrow_python"
    assert col["executed_backend"] != "rust_companion"
    assert col["calls"] == len(chunks)


def _both_aggregates(run: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    resident = _columns(aggregate_chunked_route_evidence(run.sink))["d"]
    acc = OutputEvidenceAccumulator()
    for result in run.sink:
        acc.append(result)
    streamed = _columns(acc.route_evidence())["d"]
    return resident, streamed


_ORDERINGS = {
    "degenerate_then_valued": [source([None, None]), source([date_value(1), date_value(2)])],
    "valued_then_degenerate": [source([date_value(1), date_value(2)]), source([None, None])],
    "degenerate_valued_degenerate": [
        source([]),
        source([date_value(3)]),
        source(["junk"]),
    ],
    "unparseable_then_valued": [source(["junk"]), source([date_value(4)])],
}


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_ORDERINGS))
def test_one_valued_chunk_makes_the_column_report_rust_on_both_aggregation_paths(
    case: str,
) -> None:
    run = run_one(make_config([bp_col(), passthrough("p")]), _ORDERINGS[case])
    assert run.ev[0].compiled_kernel_executed is True
    resident, streamed = _both_aggregates(run)
    for col in (resident, streamed):
        assert col["planned_backend"] == "rust_companion"
        assert col["executed_backend"] == "rust_companion", case
        assert col["calls"] == len(_ORDERINGS[case])
    assert resident["executed_backend"] == streamed["executed_backend"]


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_DEGENERATE))
def test_all_degenerate_columns_report_arrow_python_on_both_aggregation_paths(case: str) -> None:
    run = run_one(make_config([bp_col(), passthrough("p")]), _DEGENERATE[case])
    resident, streamed = _both_aggregates(run)
    assert resident["executed_backend"] == streamed["executed_backend"] == "arrow_python"


# ---------------------------------------------------------------------------
# 6. The branch receives the exact arguments and the one preflight kernel.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 3])
def test_the_branch_passes_exact_config_and_reuses_the_preflight_index_kernel(
    threads: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = spy_index_kernel(monkeypatch)
    seen: list[dict[str, Any]] = []
    real = _chunk_masking.native_bucket_perturb

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunk_masking, "native_bucket_perturb", spy)
    chunks = split(source([date_value(i) for i in range(7)]), 3)
    config = make_config([bp_col(bucket="quarter", date_format="%Y-%m-%d"), passthrough("p")])
    run_one(config, chunks, native_threads=threads)
    assert len(seen) == len(chunks)
    for kwargs in seen:
        assert kwargs["bucket"] == "quarter"
        assert kwargs["date_format"] == "%Y-%m-%d"
        assert kwargs["namespace"] == "ns_d"
        assert kwargs["native_threads"] == threads
        assert kwargs["index_kernel"] is kernel
    assert len({id(k["index_kernel"]) for k in seen}) == 1


# ---------------------------------------------------------------------------
# 7. Native and oracle agree on the admitted shape end to end.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_admitted_run_equals_the_forced_oracle_run() -> None:
    chunks = split(source([date_value(i) for i in range(11)] + [None, "junk"]), 4)
    native, forced = run_pair([bp_col(), passthrough("p")], chunks)
    assert_same_as_oracle(native, forced)
