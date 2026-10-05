"""C1 acceptance: admission, routing and evidence for chunked deterministic categorical.

Covers the chunk-safety preflight (`_chunked.py`), the lifted veto and its mirrors, the
string-source admission, the eight native config-gate codes, route evidence, the
companion-absent downgrade, and the single prepared CDF artifact.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution.native import _dispatch
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._dispatch import NativeRouteEvidence
from decoy_engine.execution.native._phase3_eligibility import phase3_c1_eligibility
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import identical, run_one
from tests.native._chunked_categorical_support import (
    FORCE,
    cat_col,
    make_config,
    passthrough,
    source,
    with_force,
)
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    force_oracle,
    forced_reason,
    key_provider,
    redact,
    split,
)

NONDET = "categorical_nondeterministic_not_chunk_safe"


def _check(columns: list[dict[str, Any]]) -> None:
    check_chunked_compatibility(make_config(columns), table=TABLE, registry=get_default_registry())


def _code(columns: list[dict[str, Any]]) -> str | None:
    try:
        _check(columns)
    except PlanCompileError as exc:
        return exc.code
    return None


def _chunk_stream(chunks: list[pa.Table], consumed: list[int]) -> Iterator[pa.Table]:
    for chunk in chunks:
        consumed.append(1)
        yield chunk


# ---------------------------------------------------------------------------
# 1 and 4. Preflight chunk-safety gate (`_chunked.py`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["deterministic", "allow_collisions"])
def test_either_determinism_spelling_passes_chunked_preflight(mode: str) -> None:
    assert _code([cat_col(mode=mode)]) is None
    assert _code([cat_col(mode=mode, weighted=True)]) is None


@pytest.mark.parametrize("explicit_false", [False, True], ids=["absent", "explicit_false"])
def test_a_non_deterministic_categorical_gets_the_distinct_code(explicit_false: bool) -> None:
    extra = {"deterministic": False} if explicit_false else {}
    # A config-complete seeded column is admitted by C1b-ii (see
    # test_chunked_nondet_categorical_admission.py); an incomplete one keeps the code.
    assert _code([cat_col(mode=None, namespace=None, **extra)]) == NONDET


def test_the_distinct_code_names_the_column_and_the_path() -> None:
    with pytest.raises(PlanCompileError) as info:
        _check([cat_col("tier", mode=None, namespace=None)])
    assert info.value.code == NONDET
    assert info.value.path == f"tables.{TABLE}.columns"
    assert "tier" in info.value.message


def test_a_non_deterministic_categorical_without_a_namespace_still_gets_the_distinct_code() -> None:
    assert _code([cat_col(mode=None, namespace=None)]) == NONDET


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_non_deterministic_categorical_fails_before_any_chunk_is_read(entry: str) -> None:
    consumed: list[int] = []
    chunks = split(source(["a", "b", "c", "a"]), 2)
    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    with pytest.raises(PlanCompileError) as info:
        run(
            make_config([cat_col(mode=None, namespace=None), passthrough("p")]),
            _chunk_stream(chunks, consumed),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    assert info.value.code == NONDET
    assert consumed == []


@pytest.mark.parametrize(
    "col",
    [
        cat_col(categories=[1, 2, 3]),
        cat_col(weighted=True, categories=["a", "b", "c", "d"]),
    ],
    ids=["numeric_categories", "weighted"],
)
def test_deterministic_but_not_native_admissible_passes_chunk_safety(col: dict[str, Any]) -> None:
    _check([col])


def test_a_deterministic_categorical_with_bad_weights_does_not_get_the_distinct_code() -> None:
    bad = cat_col()
    bad["provider_config"]["weights"] = [1.0, -1.0, 1.0, 1.0]
    assert _code([bad]) is None


def test_a_deterministic_categorical_without_a_namespace_is_still_an_eager_failure() -> None:
    assert _code([cat_col(namespace=None)]) == "chunked_strategy_conditions_unmet"


def test_missing_namespace_fails_eagerly_on_both_entries() -> None:
    consumed: list[int] = []
    chunks = split(source(["a", "b", "c", "a"]), 2)
    for run in (run_mask_chunked, run_mask_pipeline_chunked):
        with pytest.raises(PlanCompileError) as info:
            run(
                make_config([cat_col(namespace=None), passthrough("p")]),
                _chunk_stream(chunks, consumed),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        assert info.value.code == "chunked_strategy_conditions_unmet"
    assert consumed == []


def test_from_profile_with_explicit_categories_is_still_not_chunk_safe() -> None:
    col = cat_col()
    col["provider_config"]["from_profile"] = True
    assert _code([col]) == "chunked_strategy_conditions_unmet"


def test_from_profile_and_missing_categories_keep_the_generic_code() -> None:
    from_profile = cat_col()
    from_profile["provider_config"] = {"from_profile": True}
    no_categories = cat_col()
    no_categories["provider_config"] = {}
    assert _code([from_profile]) == "chunked_strategy_conditions_unmet"
    assert _code([no_categories]) == "chunked_strategy_conditions_unmet"


def test_faker_determinism_condition_is_unchanged() -> None:
    faker = {
        "name": "f",
        "strategy": "faker",
        "provider": "person_first_name",
        "namespace": "ns_f",
        "pool_size": 10,
    }
    assert _code([faker]) == "chunked_strategy_conditions_unmet"


# ---------------------------------------------------------------------------
# 2 and 5. The veto is lifted for admissible categorical, held otherwise.
# ---------------------------------------------------------------------------


def test_categorical_is_not_in_the_chunked_veto_set() -> None:
    from decoy_engine.execution.native._requirements import CHUNKED_ROUTE_VETOED_STRATEGIES

    assert "categorical" not in CHUNKED_ROUTE_VETOED_STRATEGIES
    assert frozenset() == CHUNKED_ROUTE_VETOED_STRATEGIES


def test_config_only_eligibility_mirror_admits_an_admissible_categorical() -> None:
    result = phase3_c1_eligibility(make_config([cat_col(), passthrough("p")]), table=TABLE)
    assert not any("categorical_not_native_chunked_route" in r for r in result.reasons)


def test_config_only_eligibility_mirror_still_declines_a_non_native_column() -> None:
    result = phase3_c1_eligibility(make_config([cat_col(), force_oracle("b")]), table=TABLE)
    assert result.admitted is False
    assert "categorical_categories_not_all_string:b" in result.reasons


@NEEDS_COMPANION
@pytest.mark.parametrize("mode", ["deterministic", "allow_collisions"])
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_admissible_categorical_runs_natively_beside_native_siblings(
    mode: str, weighted: bool
) -> None:
    config = make_config([redact("s"), passthrough("p"), cat_col(mode=mode, weighted=weighted)])
    table = source(["a", None, "b", "c", "a", "d"]).append_column(
        "s", pa.array(["x"] * 6, pa.string())
    )
    run = run_one(config, split(table, 4))
    evidence = run.ev[0]
    assert evidence.native_admitted is True and evidence.reroute_reason is None
    routes = {n.column: n.route for n in evidence.node_routes}
    assert routes == {"s": "native_kernel", "p": "native_kernel", "c": "native_kernel"}, routes
    assert evidence.kernel_calls["redact"] == 2


def test_a_still_vetoed_column_beside_categorical_sends_the_table_to_the_oracle() -> None:
    config = make_config([cat_col(), force_oracle(FORCE), passthrough("p")])
    run = run_one(config, [with_force(c) for c in split(source(["a", "b", "c"]), 2)])
    assert run.ev[0].native_admitted is False
    assert forced_reason(FORCE) in (run.ev[0].reroute_reason or "")


def test_non_native_admissible_categorical_still_routes_its_table_to_the_oracle() -> None:
    config = make_config([redact("s"), cat_col(categories=[1, 2, 3])])
    table = pa.table(
        {"s": pa.array(["x", "y", "z"], pa.string()), "c": pa.array(["a", "b", "c"], pa.string())}
    )
    run = run_one(config, [table])
    reason = run.ev[0].reroute_reason or ""
    assert run.ev[0].native_admitted is False
    assert "fallback_policy_not_native:c" in reason
    assert "categorical_not_native_chunked_route" not in reason


@pytest.mark.parametrize(
    "typ", [pa.int64(), pa.large_string(), pa.dictionary(pa.int32(), pa.string())]
)
def test_a_non_string_source_reroutes_before_masking(typ: pa.DataType) -> None:
    values = ["a", "b", "a"]
    column = pa.array(values, pa.string()).cast(typ) if typ != pa.int64() else pa.array([1, 2, 1])
    table = pa.table({"c": column, "p": pa.array([1, 2, 3], pa.int64())})
    run = run_one(make_config([cat_col(), passthrough("p")]), [table])
    assert run.ev[0].native_admitted is False
    assert f"categorical_source_type_not_string:c:{typ}" in (run.ev[0].reroute_reason or "")
    assert run.ev[0].kernel_calls == {}


# ---------------------------------------------------------------------------
# 8. The eight native config-gate codes still fire.
# ---------------------------------------------------------------------------


def _gate_config(case: str) -> dict[str, Any]:
    col = cat_col()
    cfg = col["provider_config"]
    if case == "categorical_not_deterministic":
        col.pop("deterministic")
    elif case == "categorical_requires_namespace":
        col.pop("namespace")
    elif case == "categorical_categories_not_nonempty_list":
        cfg["categories"] = []
    elif case == "categorical_categories_not_all_string":
        cfg["categories"] = ["a", 2]
    elif case == "categorical_weights_shape":
        cfg["weights"] = [1.0, 2.0]
    elif case == "categorical_weights_not_numeric":
        cfg["weights"] = [1.0, "x", 1.0, 1.0]
    elif case == "categorical_weights_negative":
        cfg["weights"] = [1.0, -1.0, 1.0, 1.0]
    elif case == "categorical_weights_unbuildable_cdf":
        cfg["weights"] = [1.0, 0.000000001, 1.0, 1.0]
    return make_config([col, passthrough("p")])


@pytest.mark.parametrize(
    "case",
    [
        "categorical_not_deterministic",
        "categorical_requires_namespace",
        "categorical_categories_not_nonempty_list",
        "categorical_categories_not_all_string",
        "categorical_weights_shape",
        "categorical_weights_not_numeric",
        "categorical_weights_negative",
        "categorical_weights_unbuildable_cdf",
    ],
)
def test_each_config_gate_code_still_fires(case: str) -> None:
    result = native_route_eligibility(_gate_config(case), table=TABLE)
    assert f"{case}:c" in result.rejections, result.rejections


# ---------------------------------------------------------------------------
# 6. Evidence: backend, kernel execution, planned and executed labels.
# ---------------------------------------------------------------------------


def _columns(agg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["column"]: c for c in agg["columns"]}


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_admitted_categorical_reports_rust_companion_and_one_kernel_call_per_chunk(
    weighted: bool,
) -> None:
    chunks = split(source(["a", "b", None, "c", "d", "a", "b", None, "c"]), 4)
    run = run_one(make_config([cat_col(weighted=weighted), passthrough("p")]), chunks)
    evidence = run.ev[0]
    assert evidence.native_admitted is True
    assert evidence.compiled_kernel_executed is True
    assert evidence.kernel_calls["categorical"] == len(chunks) == 3
    col = _columns(aggregate_chunked_route_evidence(run.sink))["c"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "rust_companion"
    assert col["calls"] == len(chunks)


def _no_index_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Any:
        raise CryptoExtensionUnavailableError("index kernel unavailable for the test")

    monkeypatch.setattr(_dispatch, "load_compiled_index_kernel", _raise)


def test_a_missing_index_companion_plans_rust_and_executes_on_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_index_kernel(monkeypatch)
    chunks = split(source(["a", "b", None, "c", "d", "a", "b"]), 3)
    run = run_one(make_config([cat_col(), passthrough("p")]), chunks)
    assert run.ev[0].native_admitted is False
    assert "index_extension_unavailable" in (run.ev[0].reroute_reason or "")
    assert run.ev[0].compiled_kernel_executed is False
    col = _columns(aggregate_chunked_route_evidence(run.sink))["c"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "pandas_oracle"
    assert {o.schema.field("c").type for o in run.out} == {pa.string()}


def test_non_admissible_categorical_plans_and_executes_on_the_oracle() -> None:
    run = run_one(make_config([cat_col(categories=[1, 2]), passthrough("p")]), [source(["a", "b"])])
    col = _columns(aggregate_chunked_route_evidence(run.sink))["c"]
    assert col["planned_backend"] == "pandas_oracle"
    assert col["executed_backend"] == "pandas_oracle"


# ---------------------------------------------------------------------------
# 7. Companion-absent: categorical reroutes to the oracle, whose output is pinned.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_companion_absent_run_matches_a_forced_oracle_run(
    weighted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_index_kernel(monkeypatch)
    chunks = split(source(["a", None, "b", None, None, None, "c", "d"]), 3)
    absent = run_one(make_config([cat_col(weighted=weighted), passthrough("p")]), chunks)
    forced = run_one(
        make_config([cat_col(weighted=weighted), passthrough("p"), force_oracle(FORCE)]),
        [with_force(c) for c in chunks],
    )
    assert absent.ev[0].native_admitted is False
    assert forced.ev[0].native_admitted is False
    assert forced_reason(FORCE) in (forced.ev[0].reroute_reason or "")
    assert {o.schema.field("c").type for o in absent.out} == {pa.string()}
    assert len(absent.out) == len(forced.out)
    for got, want in zip(absent.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


# ---------------------------------------------------------------------------
# 9. The CDF is built through one prepared artifact: its count ignores chunk count.
# ---------------------------------------------------------------------------


def _count_cdf_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("decoy_engine") or module is None:
            continue
        real = getattr(module, "_build_cdf", None)
        if real is None:
            continue

        def counting(weights: list[float], _real: Any = real) -> list[int]:
            calls.append(1)
            return _real(weights)

        monkeypatch.setattr(module, "_build_cdf", counting)
    return calls


@NEEDS_COMPANION
def test_cdf_build_count_does_not_scale_with_the_chunk_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = [None if i % 7 == 3 else f"v{i % 13}" for i in range(48)]
    config = make_config([cat_col(weighted=True), passthrough("p")])
    calls = _count_cdf_builds(monkeypatch)
    counts: dict[int, int] = {}
    for size in (48, 6, 1):
        calls.clear()
        evidence: list[NativeRouteEvidence] = []
        out = list(
            run_mask_chunked(
                config,
                split(source(values), size),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                route_evidence_sink=evidence,
            )
        )
        assert evidence[0].native_admitted is True
        assert len(out) == 48 // size
        counts[size] = len(calls)
    assert counts[48] > 0
    assert counts[48] == counts[6] == counts[1], counts


# ---------------------------------------------------------------------------
# Units: the one prepared-artifact predicate and the thread budget.
# ---------------------------------------------------------------------------


def _seed(**kw: Any) -> Any:
    from types import SimpleNamespace

    base = {
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "ns",
        "provider_config": (("categories", ("a", "b")),),
    }
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize(
    "seed",
    [
        _seed(strategy="faker"),
        _seed(deterministic=False, namespace=None),
        _seed(namespace=None),
        _seed(provider_config=(("categories", (1, 2)),)),
    ],
    ids=["other_strategy", "non_deterministic_no_namespace", "no_namespace", "numeric_categories"],
)
def test_prepared_chunked_categoricals_skips_non_admissible_seeds(seed: Any) -> None:
    from decoy_engine.execution.native._categorical_prepared import prepare_chunked_categoricals

    assert prepare_chunked_categoricals({"c": seed}, pa.schema([("c", pa.string())])) == {}


def test_prepared_chunked_categoricals_keeps_admissible_string_source_columns_only() -> None:
    from decoy_engine.execution.native._categorical_prepared import (
        PreparedCategorical,
        prepare_chunked_categoricals,
    )

    seeds = {"c": _seed(), "n": _seed(), "absent": _seed()}
    schema = pa.schema([("c", pa.string()), ("n", pa.int64())])
    assert prepare_chunked_categoricals(seeds, schema) == {
        "c": PreparedCategorical(("a", "b"), None)
    }


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 3])
def test_the_native_thread_budget_reaches_the_categorical_kernel_call(
    threads: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _chunk_masking

    seen: list[int | None] = []
    real = _chunk_masking.native_categorical

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["native_threads"])
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunk_masking, "native_categorical", spy)
    chunks = split(source(["a", "b", "c", "a", "b"]), 2)
    run_one(make_config([cat_col(), passthrough("p")]), chunks, native_threads=threads)
    assert seen == [threads] * len(chunks)
