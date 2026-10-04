"""C1b-ii acceptance: two-stage admission, fail-closed, `when:`/FK, deferred routes.

Stage A is config-only (non-deterministic + namespace + explicit all-string categories + no
`from_profile` + a buildable CDF) and is shared by four chunked-only consumers: the
compatibility veto, `_static_route_decision`, `plan_column_backends` and
`prepare_chunked_categoricals`. Stage B is the string source known at the first chunk,
shared by `prepare_chunked_categoricals` and the positional fail-closed. A non-admissible
seeded column must never reach the oracle route (plan section 5, tests 4-6, 9).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution.native import _chunked_entry, _dispatch
from decoy_engine.execution.native._categorical_prepared import (
    PreparedCategorical,
    prepare_categorical,
    prepare_chunked_categoricals,
)
from decoy_engine.execution.native._chunked_evidence import plan_column_backends
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.plan import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import run_one
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
    column_values,
    force_oracle,
    hash_col,
    key_provider,
    split,
)

NONDET = "categorical_nondeterministic_not_chunk_safe"
WHEN_CODE = "chunked_categorical_nondeterministic_when_not_supported"
_REG = get_default_registry()


def _nd(weighted: bool = False, **kw: Any) -> dict[str, Any]:
    return cat_col(mode=None, weighted=weighted, **kw)


def _bad_weights(weights: list[Any]) -> dict[str, Any]:
    col = _nd()
    col["provider_config"]["weights"] = weights
    return col


def _check(columns: list[dict[str, Any]]) -> None:
    check_chunked_compatibility(make_config(columns), table=TABLE, registry=_REG)


def _code(columns: list[dict[str, Any]]) -> str | None:
    try:
        _check(columns)
    except PlanCompileError as exc:
        return exc.code
    return None


def _from_profile() -> dict[str, Any]:
    col = _nd()
    col["provider_config"]["from_profile"] = True
    return col


def _with_cfg(**cfg: Any) -> dict[str, Any]:
    col = _nd()
    col["provider_config"].update(cfg)
    return col


def _no_categories() -> dict[str, Any]:
    col = _nd()
    col["provider_config"] = {}
    return col


# Stage-A cases: (id, column, config_admissible).
_STAGE_A: list[tuple[str, dict[str, Any], bool]] = [
    ("uniform", _nd(), True),
    ("weighted", _nd(weighted=True), True),
    ("zero_weight_slot_ok", _bad_weights([1.0, 1.0, 0.0, 2.0]), True),
    ("int_weights", _bad_weights([1, 2, 3, 4]), True),
    ("no_namespace", _nd(namespace=None), False),
    ("from_profile", _from_profile(), False),
    ("no_categories", _no_categories(), False),
    ("numeric_categories", _nd(categories=[1, 2, 3]), False),
    ("mixed_categories", _nd(categories=["a", 2]), False),
    ("empty_categories", _nd(categories=[]), False),
    ("weights_shape", _bad_weights([1.0, 2.0]), False),
    ("weights_not_numeric", _bad_weights([1.0, "x", 1.0, 1.0]), False),
    ("weights_bool", _bad_weights([True, 1.0, 1.0, 1.0]), False),
    ("weights_negative", _bad_weights([1.0, -1.0, 1.0, 1.0]), False),
    ("weights_all_zero", _bad_weights([0.0, 0.0, 0.0, 0.0]), False),
    ("weights_below_resolution", _bad_weights([1.0, 0.000000001, 1.0, 1.0]), False),
    ("weights_nan", _bad_weights([1.0, math.nan, 1.0, 1.0]), False),
    ("weights_inf", _bad_weights([1.0, math.inf, 1.0, 1.0]), False),
    ("weights_sum_overflows", _bad_weights([1e308, 1e308, 1e308, 1e308]), False),
]
_STAGE_A_IDS = [c[0] for c in _STAGE_A]
_COMPILE_REJECTS = frozenset({"no_categories", "empty_categories"})


# ---------------------------------------------------------------------------
# 4/5/6a. The config veto (stage A): admissible passes, everything else fails closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "col", "admissible"), _STAGE_A, ids=_STAGE_A_IDS)
def test_the_config_veto_admits_only_stage_a_candidates(
    case: str, col: dict[str, Any], admissible: bool
) -> None:
    if admissible:
        assert _code([col]) is None
    else:
        # A non-namespaced or no-categories shape is already caught by the retained code.
        assert _code([col]) == NONDET


def test_the_veto_message_names_the_column_and_keeps_the_path() -> None:
    with pytest.raises(PlanCompileError) as info:
        _check([_nd(namespace=None, name="tier")])
    assert info.value.code == NONDET
    assert info.value.path == f"tables.{TABLE}.columns"
    assert "tier" in info.value.message


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
@pytest.mark.parametrize("case", [c for c in _STAGE_A if not c[2]], ids=lambda c: c[0])
def test_a_non_admissible_config_fails_before_any_chunk_and_never_reaches_the_oracle(
    entry: str, case: tuple[str, dict[str, Any], bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle_calls: list[int] = []
    real = _chunked_entry._oracle_route
    monkeypatch.setattr(
        _chunked_entry, "_oracle_route", lambda *a, **k: oracle_calls.append(1) or real(*a, **k)
    )
    consumed: list[int] = []

    def stream() -> Iterator[pa.Table]:
        for chunk in split(source(["a", "b", "c", "a"]), 2):
            consumed.append(1)
            yield chunk

    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    ev: list[Any] = []
    kwargs: dict[str, Any] = {"route_evidence_sink": ev} if entry == "run_mask_chunked" else {}
    with pytest.raises(PlanCompileError) as info:
        list(
            run(
                make_config([case[1], passthrough("p")]),
                stream(),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                **kwargs,
            )
        )
    assert info.value.code == NONDET
    assert consumed == [] and oracle_calls == [] and ev == []


def test_the_deterministic_variant_with_bad_weights_still_does_not_get_the_distinct_code() -> None:
    bad = cat_col()
    bad["provider_config"]["weights"] = [1.0, -1.0, 1.0, 1.0]
    assert _code([bad]) is None


# ---------------------------------------------------------------------------
# 5. Stage-A agreement across the four config consumers.
# ---------------------------------------------------------------------------


def _first_chunk() -> pa.Table:
    return source(["a", "b", "c", "a"])


def _seed_for(config: dict[str, Any]) -> Any:
    profile = first_chunk_profile(_first_chunk(), table=TABLE, engine_version=ENGINE_VERSION)
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION, no_profile=True)
    table_seed = next(ts for (name, ts) in plan.seed_envelope.per_table if name == TABLE)
    return profile, dict(table_seed.per_column)


@pytest.mark.parametrize(("case", "col", "admissible"), _STAGE_A, ids=_STAGE_A_IDS)
def test_the_four_stage_a_consumers_return_the_identical_config_verdict(
    case: str, col: dict[str, Any], admissible: bool
) -> None:
    config = make_config([col, passthrough("p")])
    veto = _code([col]) is None
    try:
        profile, seeds = _seed_for(config)
    except PlanCompileError as exc:
        # The plan compiler refuses a missing/empty category list itself, before the
        # three downstream consumers can run; the veto verdict is the only one left.
        assert exc.code == "categorical_categories_missing" and case in _COMPILE_REJECTS
        assert veto is False
        return
    static = _dispatch._static_route_decision(
        config, profile, table=TABLE, engine_version=ENGINE_VERSION, registry=_REG
    )
    backends = {
        c.column: c.planned_backend
        for c in plan_column_backends(
            config, profile, table=TABLE, engine_version=ENGINE_VERSION, registry=_REG
        )
    }
    prepared = prepare_chunked_categoricals(seeds, _first_chunk().schema)
    verdicts = {
        "veto": veto,
        "static_route": static.native_admitted,
        "evidence": backends["c"] == "rust_companion",
        "prepared": "c" in prepared,
    }
    assert set(verdicts.values()) == {admissible}, verdicts


# ---------------------------------------------------------------------------
# 5. Stage-B agreement: preparation and the positional fail-closed.
# ---------------------------------------------------------------------------

_SOURCE_TYPES: list[tuple[str, pa.DataType, bool]] = [
    ("string", pa.string(), True),
    ("large_string", pa.large_string(), False),
    ("int64", pa.int64(), False),
    ("float64", pa.float64(), False),
    ("dictionary", pa.dictionary(pa.int32(), pa.string()), False),
]


def _typed_source(typ: pa.DataType) -> pa.Table:
    if pa.types.is_integer(typ) or pa.types.is_floating(typ):
        col = pa.array([1, 2, 1, 3]).cast(typ)
    else:
        col = pa.array(["a", "b", "a", "c"], pa.string()).cast(typ)
    return pa.table({"c": col, "p": pa.array([1, 2, 3, 4], pa.int64())})


def _fail_closed(typ: pa.DataType, monkeypatch: pytest.MonkeyPatch) -> tuple[bool, list[Any], int]:
    oracle_calls: list[int] = []
    real = _chunked_entry._oracle_route
    monkeypatch.setattr(
        _chunked_entry, "_oracle_route", lambda *a, **k: oracle_calls.append(1) or real(*a, **k)
    )
    ev: list[Any] = []
    try:
        run_one(
            make_config([_nd(), passthrough("p")]),
            [_typed_source(typ)],
            route_evidence_sink=ev,
        )
    except PlanCompileError as exc:
        assert exc.code == NONDET
        return True, ev, len(oracle_calls)
    return False, ev, len(oracle_calls)


@pytest.mark.parametrize(("case", "typ", "is_string"), _SOURCE_TYPES, ids=lambda c: str(c)[:14])
def test_the_two_stage_b_consumers_agree_on_the_source_type(
    case: str, typ: pa.DataType, is_string: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config([_nd(), passthrough("p")])
    table = _typed_source(typ)
    profile = first_chunk_profile(table, table=TABLE, engine_version=ENGINE_VERSION)
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION, no_profile=True)
    seeds = dict(next(ts for (n, ts) in plan.seed_envelope.per_table if n == TABLE).per_column)
    prepared = "c" in prepare_chunked_categoricals(seeds, table.schema)
    failed, _ev, _oracle = _fail_closed(typ, monkeypatch)
    assert prepared is is_string
    assert failed is (not is_string)


@pytest.mark.parametrize(
    ("case", "typ", "is_string"),
    [t for t in _SOURCE_TYPES if not t[2]],
    ids=lambda c: str(c)[:14],
)
def test_a_non_string_source_fails_closed_and_is_not_downgraded_to_the_oracle(
    case: str, typ: pa.DataType, is_string: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed, ev, oracle_calls = _fail_closed(typ, monkeypatch)
    assert failed
    assert ev == [], "no route decision (native or oracle) may be recorded for a refused column"
    assert oracle_calls == []


@pytest.mark.parametrize("typ", [pa.int64(), pa.large_string()], ids=["int64", "large_string"])
def test_the_deterministic_non_string_oracle_fallback_is_unchanged(typ: pa.DataType) -> None:
    ev: list[Any] = []
    run = run_one(
        make_config([cat_col(), passthrough("p")]), [_typed_source(typ)], route_evidence_sink=ev
    )
    assert run.ev[0].native_admitted is False
    assert f"categorical_source_type_not_string:c:{typ}" in (run.ev[0].reroute_reason or "")


# ---------------------------------------------------------------------------
# 5. Full-frame physical stays closed (B3 no-widening).
# ---------------------------------------------------------------------------


def test_prepare_categorical_still_declines_non_deterministic() -> None:
    prepared, reason = prepare_categorical(
        "c", deterministic=False, namespace="ns", provider_config={"categories": ["A", "B"]}
    )
    assert prepared is None and reason == "categorical_not_deterministic:c"


def test_the_config_only_native_eligibility_query_still_rejects_non_deterministic() -> None:
    result = native_route_eligibility(make_config([_nd(), passthrough("p")]), table=TABLE)
    assert "categorical_not_deterministic:c" in result.rejections


def test_the_physical_operator_assertion_is_unchanged() -> None:
    import inspect

    from decoy_engine.execution.physical import _shadow_operators

    src = inspect.getsource(_shadow_operators)
    assert "if not binding.categorical_deterministic:" in src


# ---------------------------------------------------------------------------
# 4. `when:` rejected on the chunked route; FK-affected stays on the oracle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_a_seeded_categorical_with_when_gets_the_new_exact_code(weighted: bool) -> None:
    col = _nd(weighted, when="p > 1")
    assert _code([col, passthrough("p")]) == WHEN_CODE


def test_the_when_rejection_names_the_column_and_path() -> None:
    with pytest.raises(PlanCompileError) as info:
        _check([_nd(name="tier", when="p > 1"), passthrough("p")])
    assert info.value.code == WHEN_CODE
    assert info.value.path == f"tables.{TABLE}.columns"
    assert "tier" in info.value.message


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_the_when_rejection_fires_before_any_chunk_on_both_entries(entry: str) -> None:
    consumed: list[int] = []

    def stream() -> Iterator[pa.Table]:
        for chunk in split(source(["a", "b", "c", "a"]), 2):
            consumed.append(1)
            yield chunk

    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    with pytest.raises(PlanCompileError) as info:
        list(
            run(
                make_config([_nd(when="p > 1"), passthrough("p")]),
                stream(),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert info.value.code == WHEN_CODE and consumed == []


def test_a_deterministic_categorical_with_when_is_unaffected() -> None:
    assert _code([cat_col(when="p > 1"), passthrough("p")]) is None


def test_a_non_admissible_seeded_column_with_when_keeps_the_retained_code() -> None:
    assert _code([_nd(namespace=None, when="p > 1"), passthrough("p")]) == NONDET


def _fk_config() -> dict[str, Any]:
    parent = {"name": "parent", "columns": [hash_col("id", "ns_k")]}
    child = [hash_col("k", "ns_k"), _nd(), passthrough("p")]
    return make_config(
        child,
        extra_tables=[parent],
        relationships=[
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": TABLE, "columns": ["k"]}],
                "orphan_policy": "remap",
            }
        ],
    )


def _fk_source(n: int) -> pa.Table:
    return pa.table(
        {
            "k": pa.array([f"key{i}" for i in range(n)], pa.string()),
            "c": pa.array([f"v{i % 4}" for i in range(n)], pa.string()),
            "p": pa.array(list(range(n)), pa.int64()),
        }
    )


def test_an_fk_affected_seeded_categorical_stays_off_native_and_is_reproducible() -> None:
    config = _fk_config()
    table = _fk_source(40)
    small = run_one(config, split(table, 7))
    large = run_one(config, split(table, 40))
    again = run_one(config, split(table, 7))
    for run in (small, large, again):
        assert run.ev[0].native_admitted is False
        assert run.ev[0].reroute_reason == "fk_relationship_not_native_route"
        assert run.ev[0].compiled_kernel_executed is False
    got = column_values(small.out, "c")
    assert got == column_values(large.out, "c") == column_values(again.out, "c")
    assert len(set(got)) > 1


# ---------------------------------------------------------------------------
# 9. Deferred routes stay closed (the C1b-iii / C1b-iv boundary).
# ---------------------------------------------------------------------------


def test_the_multi_table_split_gate_still_vetoes_the_seeded_categorical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_multi_table as pmt
    from decoy_engine.execution import run_pipeline
    from tests.unit.execution import _auto_chunk_support as support
    from tests.unit.execution import _multi_table_support as mt

    n = mt.SMALL * 40
    col = {
        "name": "v",
        "strategy": "categorical",
        "deterministic": False,
        "namespace": "v_ns",
        "provider_config": {"categories": ["x", "y", "z"]},
    }
    values = pa.table(
        {
            "v": pa.array([["red", "green", "blue"][i % 3] for i in range(n)]),
            "h": pa.array([f"u{i}" for i in range(n)]),
        }
    )
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "tiny": ([col, support.hash_col("h", "r_ns")], values.slice(0, mt.SMALL)),
            "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
            "big2": (mt.std_columns("big2_ns"), mt.string_table(mt.BIG, "c")),
        },
    )
    deferred: list[Any] = []
    decisions: list[Any] = []
    real_nodes = pmt.position_keyed_deferred_nodes
    real_decide = pmt.decide_multi_table_split

    def spy_nodes(plan: Any) -> Any:
        deferred.append(real_nodes(plan))
        return deferred[-1]

    def spy_decide(*a: Any, **k: Any) -> Any:
        decisions.append(real_decide(*a, **k))
        return decisions[-1]

    monkeypatch.setattr(pmt, "position_keyed_deferred_nodes", spy_nodes)
    monkeypatch.setattr(pmt, "decide_multi_table_split", spy_decide)
    calls = mt.spy_split(monkeypatch)
    run_pipeline(cfg, sources=sources, **mt.kw())
    assert decisions == [None]
    assert deferred and deferred[0] == (("tiny", "v", "categorical"),)
    assert calls == [], "no sibling table may be dispatched"


def test_the_out_of_core_veto_still_rejects_with_the_exact_code() -> None:

    from tests.unit.execution import test_c1b_i_route_regression as base

    plan, graph = base._fk_job(base._cat_seed())
    from decoy_engine.execution._runner import build_work_list, order_work
    from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility

    work = order_work(build_work_list(plan, _REG), graph)
    compat = check_out_of_core_compatibility(plan, work, graph)
    assert not compat.accepted
    codes = {r.code for r in compat.rejections}
    assert "out_of_core_categorical_nondeterministic_unsupported" in codes
    assert any("C1b-iv" in r.message for r in compat.rejections)


# ---------------------------------------------------------------------------
# 10. Forced-oracle proof (C2 lesson): the oracle leg is the oracle, with its exact reason.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_the_forced_oracle_leg_runs_the_seeded_column_on_the_oracle_with_the_exact_reason() -> None:
    chunks = [with_force(c) for c in split(source(["a", "b", "c", "a", "b"]), 2)]
    forced = run_one(make_config([_nd(), passthrough("p"), force_oracle(FORCE)]), chunks)
    assert forced.ev[0].native_admitted is False
    assert f"date_shift_not_native_chunked_route:{FORCE}" in (forced.ev[0].reroute_reason or "")
    assert forced.ev[0].compiled_kernel_executed is False
    native = run_one(make_config([_nd(), passthrough("p")]), split(source(["a", "b", "c", "a", "b"]), 2))
    assert native.ev[0].native_admitted is True
    assert column_values(native.out, "c") == column_values(forced.out, "c")


# ---------------------------------------------------------------------------
# Companion-absent: planned rust, executed on the oracle, byte-identical.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_companion_absent_run_matches_a_forced_oracle_run(
    weighted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError

    def _raise() -> Any:
        raise CryptoExtensionUnavailableError("index kernel unavailable for the test")

    monkeypatch.setattr(_dispatch, "load_compiled_index_kernel", _raise)
    chunks = split(source(["a", None, "b", None, None, None, "c", "d"]), 3)
    absent = run_one(make_config([_nd(weighted), passthrough("p")]), chunks)
    forced = run_one(
        make_config([_nd(weighted), passthrough("p"), force_oracle(FORCE)]),
        [with_force(c) for c in chunks],
    )
    assert absent.ev[0].native_admitted is False
    assert "index_extension_unavailable" in (absent.ev[0].reroute_reason or "")
    assert absent.ev[0].compiled_kernel_executed is False
    assert column_values(absent.out, "c") == column_values(forced.out, "c")
    assert {o.schema.field("c").type for o in absent.out} == {pa.string()}


def test_prepared_artifact_for_the_seeded_variant_carries_categories_and_cdf() -> None:
    from types import SimpleNamespace

    seed = SimpleNamespace(
        strategy="categorical",
        deterministic=False,
        namespace="ns",
        provider_config=(("categories", ("a", "b")),),
    )
    schema = pa.schema([("c", pa.string())])
    assert prepare_chunked_categoricals({"c": seed}, schema) == {
        "c": PreparedCategorical(("a", "b"), None)
    }
