"""C3 acceptance: admission, routing, boundaries and evidence for chunked group_key.

Covers the emptied veto set and its mirrors, the order-dependence decline (a masked or
self-anchored sibling), the {string, int64, bool} native sibling domain and the oracle
fallback for wider types, the raw-hex kernel preflight and its downgrade, route evidence
(including the honest executed backend for an empty chunk), FK and `when:` boundaries, the
auto-router end to end, and the forced-oracle helper that replaced group_key as the
oracle-forcing stand-in.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution import _chunked_profile
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution.native import _chunk_masking, _chunked_evidence, _dispatch
from decoy_engine.execution.native import _group_key_kernel as gk_kernel
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._chunked_evidence import plan_column_backends
from decoy_engine.execution.native._chunked_group_key_gate import (
    group_by_columns,
    sibling_resident_sources,
)
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._group_key_ext import (
    RAW_HEX_KAT,
    load_compiled_raw_hex_kernel,
)
from decoy_engine.execution.native._group_key_kernel import native_group_key
from decoy_engine.execution.native._real_type_admission import real_type_rejection
from decoy_engine.execution.native._requirements import CHUNKED_ROUTE_VETOED_STRATEGIES
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    FORCE_ORACLE_VALUE,
    NEEDS_COMPANION,
    TABLE,
    force_oracle,
    forced_reason,
    hash_col,
    is_forced,
    key_provider,
)
from tests.native._chunked_group_key_support import (
    FORCE,
    FORCE_REASON,
    GB,
    TARGET,
    columns,
    expected_key,
    gk_col,
    gk_source,
    make_config,
    passthrough,
    redact,
    run_one,
    run_pair,
    split,
    with_force,
)
from tests.native.test_chunked_entry_values_schema import _full_frame
from tests.unit.execution import _auto_chunk_support as auto

_STRATEGY = "group_key"
_STRINGS: list[str | None] = ["a", "b", None, "a", "c", "b", "a", "d", None, "c"]


def _table(values: list[Any] | None = None, typ: pa.DataType | None = None) -> pa.Table:
    return gk_source(values if values is not None else _STRINGS, typ)


def _oracle(config: dict[str, Any], chunks: list[pa.Table]) -> list[pa.Table]:
    """The public oracle chunked entry (no dispatcher, no native route)."""
    return list(
        run_mask_pipeline_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )


def _values(chunks: list[pa.Table], name: str = TARGET) -> list[Any]:
    return [v for c in chunks for v in c.column(name).to_pylist()]


def _no_raw_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Any:
        raise CryptoExtensionUnavailableError("raw-hex kernel unavailable for the test")

    monkeypatch.setattr(_dispatch, "load_compiled_raw_hex_kernel", _raise)


# ---------------------------------------------------------------------------
# 1 / 8. The veto set is empty and every mirror agrees.
# ---------------------------------------------------------------------------


def test_the_chunked_veto_set_is_exactly_empty() -> None:
    assert frozenset() == CHUNKED_ROUTE_VETOED_STRATEGIES


def test_static_route_decision_admits_group_key() -> None:
    config = make_config(columns())
    profile = _chunked_profile.first_chunk_profile(
        _table(), table=TABLE, engine_version=ENGINE_VERSION
    )
    decision = _dispatch._static_route_decision(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert f"{_STRATEGY}_not_native" not in (decision.reroute_reason or "")
    assert decision.native_admitted is True
    assert {n.column: n.route for n in decision.node_routes}[TARGET] == "native_kernel"


def test_the_evidence_planner_plans_the_companion_for_group_key() -> None:
    config = make_config(columns())
    profile = _chunked_profile.first_chunk_profile(
        _table(), table=TABLE, engine_version=ENGINE_VERSION
    )
    plans = plan_column_backends(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert {p.column: p.planned_backend for p in plans}[TARGET] == "rust_companion"


def test_the_defensive_refusal_sites_still_honor_a_non_empty_veto_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam is kept for a future strategy: a strategy placed in the set is refused by
    the dispatcher and planned onto the oracle by the evidence planner."""
    strategy = _STRATEGY
    monkeypatch.setattr(_dispatch, "CHUNKED_ROUTE_VETOED_STRATEGIES", frozenset({strategy}))
    monkeypatch.setattr(_chunked_evidence, "CHUNKED_ROUTE_VETOED_STRATEGIES", frozenset({strategy}))
    config = make_config(columns())
    profile = _chunked_profile.first_chunk_profile(
        _table(), table=TABLE, engine_version=ENGINE_VERSION
    )
    decision = _dispatch._static_route_decision(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert decision.native_admitted is False
    assert f"{strategy}_not_native_chunked_route:{TARGET}" in (decision.reroute_reason or "")
    plans = plan_column_backends(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert {p.column: p.planned_backend for p in plans}[TARGET] == "pandas_oracle"


@NEEDS_COMPANION
def test_an_admissible_group_key_runs_natively_beside_native_siblings() -> None:
    config = make_config([redact("s"), *columns()])
    table = _table().append_column("s", pa.array(["x"] * len(_STRINGS), pa.string()))
    run = run_one(config, split(table, 4))
    evidence = run.ev[0]
    assert evidence.native_admitted is True and evidence.reroute_reason is None
    routes = {n.column: n.route for n in evidence.node_routes}
    assert routes == {
        "s": "native_kernel",
        GB: "native_kernel",
        TARGET: "native_kernel",
        "p": "native_kernel",
    }, routes
    assert evidence.kernel_calls["group_key"] == 3
    assert evidence.compiled_kernel_executed is True


def test_an_unconfigured_sibling_cannot_be_named_by_group_by() -> None:
    """The plan compiler requires `group_by` to name a configured column, so the shape the
    native route reads (an unconfigured sibling) never reaches it."""
    with pytest.raises(PlanCompileError) as exc:
        list(
            run_mask_chunked(
                make_config(columns(sibling=False)),
                [_table()],
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert exc.value.code == "group_key_missing_group_by_ref"


# ---------------------------------------------------------------------------
# 4. Order-dependence decline: a masked or self-anchored sibling stays on the oracle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("masker", ["redact", "truncate"])
def test_a_masked_sibling_declines_to_the_oracle_and_reads_the_post_mask_value(
    masker: str, tmp_path: Path
) -> None:
    sibling = (
        redact(GB)
        if masker == "redact"
        else {
            "name": GB,
            "strategy": "truncate",
            "provider_config": {"length": 1, "keep": "head"},
        }
    )
    config = make_config([sibling, gk_col(), passthrough("p")])
    table = _table(["aa", "ab", "ba", "aa", "bb", "ab"])
    chunks = split(table, 4)
    run = run_one(config, chunks)
    assert run.ev[0].native_admitted is False
    assert f"group_key_masked_sibling_not_native_chunked_route:{TARGET}:{GB}" in (
        run.ev[0].reroute_reason or ""
    )
    want = _oracle(config, chunks)
    assert _values(run.out) == _values(want)
    assert _values(run.out) == _full_frame(config, table, tmp_path).column(TARGET).to_pylist()


def test_a_sibling_masked_by_another_group_key_declines_with_the_masked_reason() -> None:
    config = make_config(
        [
            passthrough(GB),
            gk_col("g2", group_by=GB),
            gk_col(TARGET, group_by="g2"),
            passthrough("p"),
        ]
    )
    table = _table(["a", "b", "a", "c"]).append_column("g2", pa.array(["x"] * 4, pa.string()))
    run = run_one(config, split(table, 3))
    assert run.ev[0].native_admitted is False
    assert f"group_key_masked_sibling_not_native_chunked_route:{TARGET}:g2" in (
        run.ev[0].reroute_reason or ""
    )


def test_a_self_anchor_declines_with_its_own_distinct_reason(tmp_path: Path) -> None:
    config = make_config([gk_col(group_by=TARGET), passthrough("p")])
    table = _table(["a", "b", "a", "c", "b"])
    table = table.set_column(1, TARGET, pa.array(["u", "v", "u", "w", "v"], pa.string()))
    chunks = split(table, 2)
    run = run_one(config, chunks)
    assert run.ev[0].native_admitted is False
    reason = run.ev[0].reroute_reason or ""
    assert f"group_key_self_anchor_not_native_chunked_route:{TARGET}" in reason
    assert "masked_sibling" not in reason
    assert _values(run.out) == _values(_oracle(config, chunks))
    assert _values(run.out) == _full_frame(config, table, tmp_path).column(TARGET).to_pylist()


def test_a_group_by_missing_from_the_first_chunk_schema_declines_cleanly() -> None:
    """Not a KeyError: the check is a clean decline to the oracle."""
    node_routes = (
        _dispatch.NodeRouteRecord(column=TARGET, strategy="group_key", route="native_kernel"),
    )
    schema = pa.schema([pa.field(TARGET, pa.string()), pa.field("p", pa.int64())])
    reason = real_type_rejection(
        make_config(columns()), node_routes, schema, table=TABLE, profile=None
    )
    assert reason is not None
    assert reason.startswith("group_key_sibling_missing_not_native_chunked_route:")
    assert TARGET in reason and GB in reason


# ---------------------------------------------------------------------------
# 5. Type domain: {string, int64, bool} runs natively; wider oracle-safe types downgrade.
# ---------------------------------------------------------------------------

_WIDER: dict[str, tuple[pa.DataType, list[Any]]] = {
    "int32": (pa.int32(), [1, 2, None, 1, 3, 2]),
    "uint64": (pa.uint64(), [1, 2, None, 1, 3, 2]),
    "dictionary_string": (
        pa.dictionary(pa.int32(), pa.string()),
        ["a", "b", None, "a", "c", "b"],
    ),
    "large_string": (pa.large_string(), ["a", "b", None, "a", "c", "b"]),
    "date32": (pa.date32(), [18000, 18001, None, 18000, 18002, 18001]),
    "timestamp": (
        pa.timestamp("us"),
        [1_600_000_000_000_000, None, 1_600_000_000_000_000, 5, 6, 5],
    ),
}


@NEEDS_COMPANION
@pytest.mark.parametrize("label", sorted(_WIDER))
def test_a_wider_oracle_safe_sibling_downgrades_to_the_oracle_and_matches_it(
    label: str, tmp_path: Path
) -> None:
    typ, values = _WIDER[label]
    arr = (
        pa.array(values, type=typ) if label != "date32" else pa.array(values, pa.int32()).cast(typ)
    )
    table = pa.table(
        {
            GB: arr,
            TARGET: pa.array(["x"] * len(values), pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )
    config = make_config(columns())
    chunks = split(table, 4)
    run = run_one(config, chunks)
    assert run.ev[0].native_admitted is False, run.ev[0]
    reason = run.ev[0].reroute_reason or ""
    # Integrated, the static plan rejects a wider sibling from its real first-chunk type
    # (fallback_policy_not_native); the group_key-specific real-type-gate code is defense-in-depth
    # for exotic/union siblings or a sibling absent from the first chunk. Either is accepted here.
    assert reason.startswith(
        (
            f"group_key_sibling_type_not_native:{TARGET}:{GB}:",
            f"fallback_policy_not_native:{TARGET}:",
        )
    ), reason
    assert _values(run.out) == _values(_oracle(config, chunks))
    assert _values(run.out) == _full_frame(config, table, tmp_path).column(TARGET).to_pylist()
    assert {o.schema.field(TARGET).type for o in run.out} == {pa.string()}


_GK_NODE = (_dispatch.NodeRouteRecord(column=TARGET, strategy="group_key", route="native_kernel"),)


@pytest.mark.parametrize(
    "typ", [pa.string(), pa.int64(), pa.bool_()], ids=["string", "int64", "bool"]
)
def test_the_real_type_gate_accepts_exactly_the_native_sibling_domain(typ: pa.DataType) -> None:
    schema = pa.schema([pa.field(GB, typ), pa.field(TARGET, pa.string())])
    assert (
        real_type_rejection(make_config(columns()), _GK_NODE, schema, table=TABLE, profile=None)
        is None
    )


@pytest.mark.parametrize("label", sorted(_WIDER))
def test_the_real_type_gate_names_a_wider_sibling_on_its_own(label: str) -> None:
    """The static plan usually rejects a wider sibling first; the gate is the authority when it
    could not (no resident type for the compiler), so it is checked by itself."""
    typ = _WIDER[label][0]
    schema = pa.schema([pa.field(GB, typ), pa.field(TARGET, pa.string())])
    reason = real_type_rejection(
        make_config(columns()), _GK_NODE, schema, table=TABLE, profile=None
    )
    assert reason == f"group_key_sibling_type_not_native:{TARGET}:{GB}:{typ}"


def test_the_real_type_gate_declines_a_null_typed_first_chunk_sibling() -> None:
    schema = pa.schema([pa.field(GB, pa.null()), pa.field(TARGET, pa.string())])
    reason = real_type_rejection(
        make_config(columns()), _GK_NODE, schema, table=TABLE, profile=None
    )
    assert reason == f"group_key_sibling_type_not_native:{TARGET}:{GB}:null"


def test_the_gate_reads_group_by_from_the_config_and_gives_the_real_sibling_type() -> None:
    config = make_config(columns())
    assert group_by_columns(config, TABLE) == {TARGET: GB}
    assert group_by_columns(config, "elsewhere") == {}
    schema = pa.schema([pa.field(GB, pa.int64()), pa.field(TARGET, pa.string())])
    resident = sibling_resident_sources(config, TABLE, schema)
    assert resident is not None and list(resident) == [TABLE]
    assert resident[TABLE].schema.names == [GB], (
        "only the sibling, every other column stays on the profile"
    )
    assert resident[TABLE].schema.field(GB).type == pa.int64()


def test_the_gate_gives_no_resident_source_when_there_is_nothing_to_give() -> None:
    schema = pa.schema([pa.field(GB, pa.string()), pa.field(TARGET, pa.string())])
    assert sibling_resident_sources(make_config(columns()), TABLE, None) is None
    no_group_key = make_config([passthrough(GB), passthrough(TARGET)])
    assert sibling_resident_sources(no_group_key, TABLE, schema) is None
    assert (
        sibling_resident_sources(
            make_config(columns()), TABLE, pa.schema([pa.field("x", pa.string())])
        )
        is None
    )
    union = pa.union([pa.field("a", pa.int64()), pa.field("b", pa.string())], mode="dense")
    exotic = pa.schema([pa.field(GB, union), pa.field(TARGET, pa.string())])
    assert sibling_resident_sources(make_config(columns()), TABLE, exotic) is None


@pytest.mark.parametrize("typ", [pa.float64(), pa.decimal128(10, 2)], ids=["float64", "decimal"])
def test_a_float_or_decimal_sibling_keeps_the_existing_trap_e_rejection(typ: pa.DataType) -> None:
    values = (
        [1.5, 2.5, 1.5] if pa.types.is_floating(typ) else [Decimal("1"), Decimal("2"), Decimal("1")]
    )
    table = gk_source(values, typ)
    with pytest.raises(PlanCompileError) as exc:
        list(
            run_mask_chunked(
                make_config(columns()),
                [table],
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert exc.value.code == "chunked_group_key_group_by_dtype_unsupported"


# ---------------------------------------------------------------------------
# 6. Raw-hex companion-absent downgrade: one reason for all four failure modes.
# ---------------------------------------------------------------------------


def _install_fake_kernel(
    monkeypatch: pytest.MonkeyPatch, *, abi: str, derive: Callable[..., pa.Array] | None
) -> None:
    kernel = types.ModuleType("decoy_engine_native._kernel")
    kernel.abi_version = lambda: abi  # type: ignore[attr-defined]
    if derive is not None:
        kernel.derive_hex_raw_batch = derive  # type: ignore[attr-defined]
    pkg = types.ModuleType("decoy_engine_native")
    pkg._kernel = kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", kernel)


def _wrong_kat(values: pa.Array, **kwargs: Any) -> pa.Array:
    return pa.array(["deadbeef"] * len(values), type=pa.string())


def _break_raw_hex(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "missing_companion":
        monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
    elif mode == "abi_mismatch":
        _install_fake_kernel(monkeypatch, abi="decoy-native-abi-0-stale", derive=None)
    elif mode == "missing_symbol":
        _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive=None)
    else:
        _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive=_wrong_kat)


@pytest.mark.parametrize(
    "mode", ["missing_companion", "abi_mismatch", "missing_symbol", "failed_kat"]
)
def test_a_missing_or_broken_raw_hex_kernel_runs_the_oracle_leg_with_one_reason(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _break_raw_hex(monkeypatch, mode)
    config = make_config(columns())
    table = _table()
    chunks = split(table, 4)
    run = run_one(config, chunks)
    assert run.ev[0].native_admitted is False
    assert run.ev[0].reroute_reason == "raw_hex_extension_unavailable"
    assert {n.route for n in run.ev[0].node_routes} == {"oracle"}
    assert run.ev[0].compiled_kernel_executed is False
    assert _values(run.out) == _values(_oracle(config, chunks))
    assert _values(run.out) == _full_frame(config, table, tmp_path).column(TARGET).to_pylist()
    assert {o.schema.field(TARGET).type for o in run.out} == {pa.string()}


def test_the_raw_hex_probe_does_not_veto_a_table_without_group_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_raw_hex(monkeypatch)
    run = run_one(make_config([redact("s"), passthrough("p")]), [pa.table({"s": ["a"], "p": [1]})])
    assert run.ev[0].native_admitted is True


@NEEDS_COMPANION
def test_preflight_carries_the_raw_hex_kernel_only_for_an_admitted_group_key() -> None:
    config = make_config(columns())
    table = _table()
    profile = _chunked_profile.first_chunk_profile(
        table, table=TABLE, engine_version=ENGINE_VERSION
    )

    def preflight(cfg: dict[str, Any]) -> Any:
        return _dispatch.plan_native_route(
            cfg,
            profile,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            first_schema=table.schema,
            unconfigured_policy="warn",
            registry=get_default_registry(),
        )

    admitted = preflight(config)
    assert admitted.evidence.native_admitted is True
    assert admitted.raw_hex_kernel is not None
    assert admitted.index_kernel is None, "group_key is not an index-kernel strategy"
    declined = preflight(make_config([redact(GB), gk_col(), passthrough("p")]))
    assert declined.evidence.native_admitted is False
    assert declined.raw_hex_kernel is None


@NEEDS_COMPANION
def test_the_raw_hex_kernel_is_loaded_once_at_preflight_and_never_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[str] = []
    real = load_compiled_raw_hex_kernel

    def counting() -> Any:
        loads.append("preflight")
        return real()

    def forbidden() -> Any:
        loads.append("per_chunk")
        raise AssertionError("the per-chunk operator reloaded the raw-hex kernel")

    monkeypatch.setattr(_dispatch, "load_compiled_raw_hex_kernel", counting)
    monkeypatch.setattr(gk_kernel, "load_compiled_raw_hex_kernel", forbidden)
    run = run_one(make_config(columns()), split(_table(), 3))
    assert run.ev[0].native_admitted is True
    assert len(run.out) == 4
    assert loads == ["preflight"]


# ---------------------------------------------------------------------------
# 9. Evidence: branch counter, truthful compiled work, idle empty chunk.
# ---------------------------------------------------------------------------


def _agg(run: Any) -> dict[str, dict[str, Any]]:
    return {c["column"]: c for c in aggregate_chunked_route_evidence(run.sink)["columns"]}


@NEEDS_COMPANION
def test_a_populated_run_reports_the_companion_and_counts_the_kernel_work() -> None:
    run = run_one(make_config(columns()), split(_table(), 4))
    ev = run.ev[0]
    assert ev.native_admitted is True and ev.compiled_kernel_executed is True
    assert ev.kernel_calls["group_key"] == 3
    col = _agg(run)[TARGET]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "rust_companion"
    assert col["calls"] == 3


@NEEDS_COMPANION
def test_an_empty_chunk_is_branch_counted_but_reports_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = native_group_key

    def spy(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        calls.append(sum(kwargs["derive_calls"]))
        return out

    monkeypatch.setattr(_chunk_masking, "native_group_key", spy)
    empty = _table([], pa.string())
    run = run_one(make_config(columns()), [empty])
    assert run.ev[0].native_admitted is True
    assert run.ev[0].kernel_calls["group_key"] == 1, "the branch counter counts an idle chunk"
    assert run.ev[0].compiled_kernel_executed is False
    assert calls == [0]
    col = _agg(run)[TARGET]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "arrow_python"
    assert (
        run.sink[0].quality_metrics["chunked_route"]["columns"][1]["executed_backend"]
        == "arrow_python"
    )


@NEEDS_COMPANION
def test_an_empty_chunk_between_valued_chunks_keeps_the_column_on_the_companion() -> None:
    chunks = [_table(["a", "b"]), _table([], pa.string()), _table(["a", "c"])]
    run = run_one(make_config(columns()), chunks)
    ev = run.ev[0]
    assert ev.kernel_calls["group_key"] == 3 and ev.compiled_kernel_executed is True
    assert _agg(run)[TARGET]["executed_backend"] == "rust_companion"
    per_chunk = [
        c.quality_metrics["chunked_route"]["columns"][1]["executed_backend"] for c in run.sink
    ]
    assert per_chunk == ["rust_companion", "arrow_python", "rust_companion"]


@NEEDS_COMPANION
def test_the_derive_spy_sees_work_only_when_the_kernel_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []
    real = native_group_key

    def spy(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        seen.append(sum(kwargs["derive_calls"]))
        return out

    monkeypatch.setattr(_chunk_masking, "native_group_key", spy)
    run_one(make_config(columns()), [_table(["a", "b"]), _table([], pa.string()), _table(["a"])])
    assert seen[0] > 0 and seen[1] == 0 and seen[2] > 0


def test_companion_absent_downgrade_reports_the_oracle_for_the_planned_companion_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_raw_hex(monkeypatch)
    run = run_one(make_config(columns()), split(_table(), 4))
    assert run.ev[0].native_admitted is False
    col = _agg(run)[TARGET]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "pandas_oracle"


# ---------------------------------------------------------------------------
# 10. FK, `when:` and read-set boundaries.
# ---------------------------------------------------------------------------


def _fk_config() -> dict[str, Any]:
    parent = hash_col("id", "ns_id")
    parent["dtype"] = "int64"
    child = gk_col(namespace="ns_id")
    child["dtype"] = "int64"
    relationships = [
        {
            "parent": {"table": "parents", "columns": ["id"]},
            "children": [{"table": TABLE, "columns": [TARGET]}],
            "orphan_policy": "remap",
        }
    ]
    return make_config(
        columns(child),
        extra_tables=[{"name": "parents", "columns": [parent]}],
        relationships=relationships,
    )


def test_a_group_key_fk_key_stays_rejected_and_not_natively_admitted() -> None:
    """group_key stays out of the chunk-safe set, so an FK edge keyed on it keeps the existing
    strategy-mismatch rejection, and the native route does not newly admit the table."""
    config = _fk_config()
    with pytest.raises(PlanCompileError) as exc:
        check_chunked_compatibility(config, table=TABLE, registry=get_default_registry())
    assert exc.value.code == "chunked_fk_child_strategy_mismatch"
    profile = _chunked_profile.first_chunk_profile(
        _table(), table=TABLE, engine_version=ENGINE_VERSION
    )
    decision = _dispatch._static_route_decision(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert decision.native_admitted is False
    assert decision.reroute_reason == "fk_relationship_not_native_route"


def test_a_group_key_with_when_keeps_the_existing_rejection_on_both_legs() -> None:
    col = gk_col()
    col["when"] = "p > 1"
    config = make_config([passthrough(GB), col, passthrough("p")])
    with pytest.raises(PlanCompileError) as exc:
        list(
            run_mask_chunked(
                config,
                [_table()],
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert exc.value.code == "chunked_group_key_when_not_supported"
    with pytest.raises(PlanCompileError) as oracle_exc:
        _oracle(config, [_table()])
    assert oracle_exc.value.code == "chunked_group_key_when_not_supported"


@NEEDS_COMPANION
def test_the_sibling_is_read_from_the_source_chunk_whether_carried_or_read_by_pandas() -> None:
    native, forced = run_pair(columns(), split(_table(), 4))
    assert native.ev[0].native_admitted is True
    read = native.sink[0].quality_metrics["chunked_route"]["pandas_read_passthrough"]
    assert read == forced.sink[0].quality_metrics["chunked_route"]["pandas_read_passthrough"]
    assert GB in read, "the group_by sibling is in the read set (SIBLING_REFERENCE_KEYS)"
    assert [c.column(GB).to_pylist() for c in native.out] == [
        c.column(GB).to_pylist() for c in split(_table(), 4)
    ]


# ---------------------------------------------------------------------------
# 11. The forced-oracle helper is the numeric-categories categorical stand-in.
# ---------------------------------------------------------------------------


def test_force_oracle_is_the_deterministic_numeric_categorical_with_a_namespace() -> None:
    assert force_oracle("x") == {
        "name": "x",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "force_oracle/x",
        "provider_config": {"categories": [1, 2, 3]},
    }
    assert is_forced(force_oracle("x")) and not is_forced(gk_col())
    assert forced_reason("x") == "fallback_policy_not_native:x:python_only"
    assert forced_reason(FORCE) == FORCE_REASON


def test_force_oracle_routes_the_table_to_the_oracle_with_the_exact_reason() -> None:
    config = make_config([redact("s"), force_oracle(FORCE)])
    chunk = pa.table({"s": ["a", "b"], FORCE: [FORCE_ORACLE_VALUE] * 2})
    run = run_one(config, [chunk])
    assert run.ev[0].native_admitted is False
    assert run.ev[0].reroute_reason == FORCE_REASON


def test_no_test_still_expects_the_retired_group_key_veto_reason() -> None:
    root = Path(__file__).resolve().parents[1]
    needle = "group_key_not_native" + "_chunked_route"
    hits = [str(p.relative_to(root)) for p in root.rglob("*.py") if needle in p.read_text()]
    assert hits == []


# ---------------------------------------------------------------------------
# 7. Auto-router end to end: admissible -> native leg, everything else -> oracle leg,
#    and every outcome equals the full-frame run.
# ---------------------------------------------------------------------------


def _routed(tmp_path: Path, cols: list[dict[str, Any]], data: pa.Table) -> tuple[Any, Any, Any]:
    cfg = auto.make_cfg(cols, path=auto.write_source(data, tmp_path / "s.parquet"))
    return auto.run_default(cfg, data), auto.run_full_frame(cfg, data), auto.run_legacy(cfg, data)


def _auto_table(typ: pa.DataType, values: list[Any]) -> pa.Table:
    n = auto.ROWS
    return pa.table(
        {
            GB: pa.array([values[i % len(values)] for i in range(n)], typ),
            TARGET: pa.array([f"x{i}" for i in range(n)], pa.string()),
            "p": pa.array(list(range(n)), pa.int64()),
        }
    )


@pytest.mark.parametrize(
    ("label", "typ", "values", "native"),
    [
        pytest.param("string", pa.string(), ["h1", "h2", None, "h3"], True, marks=NEEDS_COMPANION),
        pytest.param("int64", pa.int64(), [1, 2, 3, 4], True, marks=NEEDS_COMPANION),
        pytest.param("bool", pa.bool_(), [True, False, None], True, marks=NEEDS_COMPANION),
        pytest.param("int32", pa.int32(), [1, 2, 3, 4], False, marks=NEEDS_COMPANION),
        pytest.param("large_string", pa.large_string(), ["a", "b", None], False),
    ],
)
def test_auto_routed_group_key_succeeds_and_equals_the_full_frame_run(
    label: str, typ: pa.DataType, values: list[Any], native: bool, tmp_path: Path
) -> None:
    data = _auto_table(typ, values)
    cols = [auto.pass_col(GB), gk_col(), auto.pass_col("p")]
    dispatcher, full, legacy = _routed(tmp_path, cols, data)
    assert dispatcher.quality_metrics["auto_chunk"]["lane"] == "dispatcher"
    evidence = dispatcher.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is native, evidence
    expected = full.outputs[auto.TABLE].column(TARGET).to_pylist()
    assert dispatcher.outputs[auto.TABLE].column(TARGET).to_pylist() == expected
    assert legacy.outputs[auto.TABLE].column(TARGET).to_pylist() == expected
    by_col = {c["column"]: c for c in evidence["columns"]}
    assert by_col[TARGET]["executed_backend"] == ("rust_companion" if native else "pandas_oracle")


@pytest.mark.parametrize("typ", [pa.int64(), pa.int32()])
def test_an_integer_sibling_holding_nulls_is_still_not_auto_chunked(
    typ: pa.DataType, tmp_path: Path
) -> None:
    """The auto-router has never chunked an integer column that holds nulls (its pandas round
    trip widens by chunk), so C3 changes nothing there: the job runs full-frame."""
    data = _auto_table(typ, [1, 2, None, 3])
    cols = [auto.pass_col(GB), gk_col(), auto.pass_col("p")]
    dispatcher, full, _ = _routed(tmp_path, cols, data)
    assert dispatcher.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    assert (
        dispatcher.outputs[auto.TABLE].column(TARGET).to_pylist()
        == full.outputs[auto.TABLE].column(TARGET).to_pylist()
    )


@NEEDS_COMPANION
def test_auto_routed_masked_sibling_stays_on_the_oracle_leg_and_equals_full_frame(
    tmp_path: Path,
) -> None:
    data = _auto_table(pa.string(), ["h1", "h2", None, "h3"])
    cols = [auto.redact_col(GB), gk_col(), auto.pass_col("p")]
    dispatcher, full, _ = _routed(tmp_path, cols, data)
    evidence = dispatcher.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is False
    assert f"group_key_masked_sibling_not_native_chunked_route:{TARGET}:{GB}" in (
        evidence["reroute_reason"] or ""
    )
    assert (
        dispatcher.outputs[auto.TABLE].column(TARGET).to_pylist()
        == full.outputs[auto.TABLE].column(TARGET).to_pylist()
    )


def test_the_forced_oracle_helper_still_forces_the_auto_router_to_the_oracle(
    tmp_path: Path,
) -> None:
    data = _auto_table(pa.string(), ["h1", "h2", "h3"]).append_column(
        FORCE, pa.array([FORCE_ORACLE_VALUE] * auto.ROWS, pa.string())
    )
    cols = [auto.pass_col(GB), gk_col(), force_oracle(FORCE), auto.pass_col("p")]
    cfg = auto.make_cfg(cols, path=auto.write_source(data, tmp_path / "s.parquet"))
    result = auto.run_default(cfg, data)
    evidence = result.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is False
    assert forced_reason(FORCE) in (evidence["reroute_reason"] or "")


def test_with_force_adds_the_forced_column_value() -> None:
    chunk = with_force(_table(["a"]))
    assert chunk.column(FORCE).to_pylist() == [FORCE_ORACLE_VALUE]


def test_the_raw_hex_kat_is_the_loader_vector() -> None:
    """Pins the embedded vector the preflight loader self-tests, so a drifted kernel is a
    downgrade to the oracle and not wrong output."""
    assert RAW_HEX_KAT.values[0] == "alice"
    assert (
        expected_key("alice", column="household_id", mask_key=RAW_HEX_KAT.mask_key)
        == (RAW_HEX_KAT.expected[0])
    )
