"""B8 revision 4.3/4.4 acceptance tests 14 to 19: composite providers and stored index fields.

A composite provider on a chunk-admitted strategy string (`redact`, `hash`, `passthrough`)
used to pass `check_chunked_compatibility` and come back with the other bundle columns as
source values (cleartext). The fix refuses composites by provider on both chunked entries,
and, as defense in depth, never lets a handler-written column be a carry or schema-rule
passthrough column. Stored pandas index fields that a config references are refused before
profiling, and a stored-index change between chunks is schema drift. Written before the
implementation; do not delete a test or loosen a comparison without a new plan gate.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution import _chunked
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._chunked_carry import passthrough_columns, plan_carry
from decoy_engine.execution._column_access import column_access, handler_written_columns
from decoy_engine.execution._transforms import TransformError
from decoy_engine.execution.native._chunked_schema_rule import build_schema_rule
from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked
from decoy_engine.generation.composite import CompositeAdapter, composite_capability
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import FORCE, with_force
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    categorical,
    key_provider,
    make_config,
    passthrough,
    redact,
    truncate,
)
from tests.unit.execution import _auto_chunk_support as support

REG = get_default_registry()
_WARN = {"unconfigured_column_policy": "warn"}
_FIXED = {
    "composite_name_email": ("first_name", ["last_name", "email"]),
    "composite_city_state_zip": ("city", ["state", "zip"]),
    "composite_person": ("first_name", ["dob", "email", "last_name"]),
    "composite_address": ("city", ["state", "street_address", "zip"]),
    "composite_provider": ("provider_name", ["npi", "practice_address"]),
}
_BUNDLE = [
    {"column": "a", "provider": "person_first_name"},
    {"column": "b", "provider": "person_last_name"},
    {"column": "c", "provider": "person_phone"},
]
_STRATEGIES = ["redact", "hash", "passthrough"]
N = 6


def _entry(name: str, provider: str, strategy: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "strategy": strategy,
        "provider": provider,
        "deterministic": True,
        "namespace": "ns",
    }
    out.update(extra)
    return out


def _cases() -> dict[str, tuple[str, list[dict[str, Any]], dict[str, list[str]], list[str]]]:
    """case id -> (provider, columns, source data, columns the composite writes)."""
    cases: dict[str, tuple[str, list[dict[str, Any]], dict[str, list[str]], list[str]]] = {}
    for strategy in _STRATEGIES:
        for provider, (own, others) in _FIXED.items():
            data = {c: [f"SECRET-{c}-{i}" for i in range(N)] for c in [own, *others]}
            cases[f"{provider}-{strategy}"] = (
                provider,
                [_entry(own, provider, strategy)],
                data,
                [own, *others],
            )
        pair = [
            _entry(
                name,
                "composite_custom",
                strategy,
                coherent_with=[other],
                provider_config={"bundle": _BUNDLE},
            )
            for name, other in (("a", "b"), ("b", "a"))
        ]
        cases[f"composite_custom-{strategy}"] = (
            "composite_custom",
            pair,
            {c: [f"SECRET-{c}-{i}" for i in range(N)] for c in ("a", "b", "c")},
            ["a", "b", "c"],
        )
    return cases


CASES = _cases()


def _source(data: dict[str, list[str]]) -> pa.Table:
    return pa.table({k: pa.array(v) for k, v in data.items()})


def _chunks(data: dict[str, list[str]]) -> list[pa.Table]:
    t = _source(data)
    return [t.slice(0, 3), t.slice(3, 3)]


# ---------------------------------------------------------------------------
# Test 14: composite provider refused on both chunked entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(CASES))
def test_composite_provider_is_refused_by_both_chunked_entries(case: str) -> None:
    provider, columns, data, _written = CASES[case]
    config = make_config(columns, global_settings=_WARN)
    for entry_point in (run_mask_chunked, run_mask_pipeline_chunked):
        sink: list[Any] = []
        with pytest.raises(PlanCompileError) as info:
            list(
                entry_point(
                    config,
                    _chunks(data),
                    table=TABLE,
                    engine_version=ENGINE_VERSION,
                    key_provider=key_provider(),
                    chunk_result_sink=sink,
                )
            )
        assert info.value.code == "strategy_not_chunk_safe"
        assert f"composite provider {provider}" in info.value.message
        assert columns[0]["name"] in info.value.message
        assert sink == []


@pytest.mark.parametrize("case", ["composite_name_email-redact", "composite_custom-passthrough"])
def test_run_pipeline_routes_a_composite_job_full_frame(case: str, tmp_path: Any) -> None:
    import pyarrow.parquet as pq

    _provider, columns, data, written = CASES[case]
    src = pa.table({k: pa.array(v) for k, v in data.items()})
    path = tmp_path / "s.parquet"
    pq.write_table(src, path)
    cfg = support.make_cfg(columns, path=str(path))
    auto = support.run_default(cfg, src)
    full = support.run_full_frame(cfg, src)
    assert auto.quality_metrics["auto_chunk"]["mode"] != "chunked"
    assert auto.outputs[support.TABLE].equals(full.outputs[support.TABLE])
    out = auto.outputs[support.TABLE]
    for name in written:
        assert out.column(name).to_pylist() != src.column(name).to_pylist(), name


# ---------------------------------------------------------------------------
# Test 15: no handler-written column is carried (12.1 patched out)
# ---------------------------------------------------------------------------


def _patch_out_composite_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    real = check_chunked_compatibility

    def lenient(config: Any, **kwargs: Any) -> None:
        try:
            real(config, **kwargs)
        except PlanCompileError as exc:
            if exc.code != "strategy_not_chunk_safe" or "composite provider" not in exc.message:
                raise

    monkeypatch.setattr(_chunked, "check_chunked_compatibility", lenient)


@pytest.mark.parametrize("case", sorted(CASES))
def test_handler_written_columns_are_never_carried_or_schema_passthrough(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provider, columns, data, written = CASES[case]
    config = make_config(columns, global_settings=_WARN)
    first = _source(data)
    carry = plan_carry(config, table=TABLE, first_schema=first.schema, adapter=None, registry=REG)
    assert not (set(written) & carry.carried)
    rule = build_schema_rule(config, table=TABLE, first=first, registry=REG)
    assert not (set(written) & set(rule.passthrough_types))
    assert not (set(written) & set(rule.passthrough_fields))
    listed = passthrough_columns(config, table=TABLE, names=first.column_names, registry=REG)
    assert not (set(written) & set(listed))
    _patch_out_composite_refusal(monkeypatch)
    out = list(
        run_mask_chunked(
            config,
            _chunks(data),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    source = _source(data)
    produced = pa.concat_tables(out)
    for name in written:
        got, src = produced.column(name).to_pylist(), source.column(name).to_pylist()
        assert all(a != b for a, b in zip(got, src, strict=True)), name


# ---------------------------------------------------------------------------
# Test 16: the caller registry drives declarations
# ---------------------------------------------------------------------------


def _scalar_rebound_registry() -> Any:
    base = get_default_registry()
    faker_caps = dataclasses.replace(
        base.get_capabilities("person_first_name"), provider="composite_name_email"
    )
    return base.override("composite_name_email", base.get_adapter("person_first_name"), faker_caps)


def _custom_composite_registry() -> Any:
    base = get_default_registry()
    caps = dataclasses.replace(composite_capability("composite_custom"), provider="composite_x")
    return base.override("composite_x", CompositeAdapter("composite_custom"), caps)


def test_a_registry_that_rebinds_a_composite_name_to_a_scalar_backend_is_honored() -> None:
    from decoy_engine.execution._chunked_carry import read_set

    reg = _scalar_rebound_registry()
    entry = {
        **redact("s"),
        "provider": "composite_name_email",
        "when": "x > 1",
    }
    assert column_access(entry, reg).everything is False
    assert read_set([entry], ["x", "last_name"], reg) == {"x"}
    assert {"x", "last_name"} <= read_set([entry], ["x", "last_name"], REG)


def test_a_caller_only_composite_is_refused_and_declared_written() -> None:
    reg = _custom_composite_registry()
    cfg_columns = [
        _entry(
            "a",
            "composite_x",
            "redact",
            coherent_with=["b"],
            provider_config={"bundle": _BUNDLE},
        ),
        _entry(
            "b",
            "composite_x",
            "redact",
            coherent_with=["a"],
            provider_config={"bundle": _BUNDLE},
        ),
    ]
    config = {
        "tables": [{"name": TABLE, "columns": cfg_columns}],
    }
    with pytest.raises(PlanCompileError) as info:
        check_chunked_compatibility(config, table=TABLE, registry=reg)
    assert info.value.code == "strategy_not_chunk_safe"
    assert "composite provider composite_x" in info.value.message
    # With the default registry the provider is unknown, so the entry is a scalar.
    check_chunked_compatibility(config, table=TABLE, registry=REG)
    # The caller's registry makes every column of the bundle a declared write.
    written = handler_written_columns(cfg_columns, reg)
    assert written is None or {"a", "b", "c"} <= written
    first = _source({"a": ["x"] * N, "b": ["y"] * N, "c": ["SECRET"] * N})
    full = make_config([redact("a")])
    full["tables"][0]["columns"] = cfg_columns
    rule = build_schema_rule(full, table=TABLE, first=first, registry=reg)
    assert not ({"a", "b", "c"} & set(rule.passthrough_types))
    carry = plan_carry(full, table=TABLE, first_schema=first.schema, adapter=None, registry=reg)
    assert not ({"a", "b", "c"} & carry.carried)


# ---------------------------------------------------------------------------
# Test 17: a configured stored-index column
# ---------------------------------------------------------------------------


def _indexed(n: int = 6) -> list[pa.Table]:
    df = pd.DataFrame(
        {
            "id": [f"id{i}" for i in range(n)],
            "s": [f"s{i}" for i in range(n)],
            "t": [f"abcdef{i}" for i in range(n)],
        }
    ).set_index("id")
    table = pa.Table.from_pandas(df)
    assert table.schema.pandas_metadata["index_columns"] == ["id"]
    return [table.slice(0, 3), table.slice(3, 3)]


def _stored_index_configs() -> dict[str, list[dict[str, Any]]]:
    return {
        "direct": [redact("s"), redact("id")],
        "passthrough_direct": [redact("s"), passthrough("id")],
        "when_reference": [{**redact("s"), "when": "id.notnull()"}],
        "group_by_reference": [
            redact("s"),
            passthrough("id"),
            {"name": "k", "strategy": "group_key", "provider_config": {"group_by": "id"}},
        ],
    }


@pytest.mark.parametrize("route", ["native_capable", "forced_oracle", "public_oracle"])
@pytest.mark.parametrize("case", sorted(_stored_index_configs()))
def test_a_configured_stored_index_column_is_refused_before_any_chunk(
    case: str, route: str
) -> None:
    columns = _stored_index_configs()[case]
    chunks = _indexed()
    if case == "group_by_reference":
        # `k` must exist as a source field for the plan to compile.
        chunks = [c.append_column("k", pa.array(["g"] * c.num_rows)) for c in chunks]
    if route == "forced_oracle":
        columns = [*columns, categorical(FORCE)]
        chunks = [with_force(c) for c in chunks]
    config = make_config(columns, global_settings=_WARN)
    entry_point = run_mask_pipeline_chunked if route == "public_oracle" else run_mask_chunked
    sink: list[Any] = []
    with pytest.raises(TransformError) as info:
        list(
            entry_point(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                chunk_result_sink=sink,
            )
        )
    assert info.value.code == "config_references_stored_index"
    assert sink == []


# ---------------------------------------------------------------------------
# Test 18: the legacy entry and the reroute reason
# ---------------------------------------------------------------------------


def test_legacy_entry_drops_a_stored_index_field_like_the_oracle() -> None:
    chunks = [c.select(["s", "t", "id"]) for c in _indexed()]
    config = make_config([redact("s"), truncate("t")])
    ev: list[Any] = []
    out = list(
        run_native_or_oracle_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=ev,
        )
    )
    assert ev[0].native_admitted is True
    assert all("id" not in o.column_names for o in out)
    oracle = list(
        run_mask_pipeline_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    assert [o.to_pydict() for o in out] == [o.to_pydict() for o in oracle]


def test_error_policy_reroute_reason_names_only_the_unconfigured_columns() -> None:
    df = pd.DataFrame({"s": ["a", "b"], "u": ["x", "y"], "id": ["i1", "i2"]}).set_index("id")
    table = pa.Table.from_pandas(df)
    config = make_config([redact("s")], global_settings={"unconfigured_column_policy": "error"})
    ev: list[Any] = []
    gen = run_mask_chunked(
        config,
        [table],
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        route_evidence_sink=ev,
    )
    assert ev[0].reroute_reason == "uncovered_columns:['u'];missing_configured_columns:[]"
    with pytest.raises(Exception):
        next(gen)


# ---------------------------------------------------------------------------
# Test 19: stored-index drift
# ---------------------------------------------------------------------------


def _outcome(config: dict[str, Any], chunks: list[pa.Table]) -> tuple[Any, int, int]:
    sink: list[Any] = []
    yielded = 0
    code = None
    try:
        for _ in run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            chunk_result_sink=sink,
        ):
            yielded += 1
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
    return code, yielded, len(sink)


@pytest.mark.parametrize("direction", ["index_then_plain", "plain_then_index"])
def test_a_stored_index_change_between_chunks_is_schema_drift_on_both_routes(
    direction: str,
) -> None:
    indexed = _indexed()
    plain = [c.replace_schema_metadata(None) for c in indexed]
    chunks = [indexed[0], plain[1]] if direction == "index_then_plain" else [plain[0], indexed[1]]
    config = make_config([redact("s"), truncate("t")], global_settings=_WARN)
    forced = make_config([redact("s"), truncate("t"), categorical(FORCE)], global_settings=_WARN)
    native = _outcome(config, chunks)
    oracle = _outcome(forced, [with_force(c) for c in chunks])
    assert native == oracle == ("native_chunk_schema_drift", 1, 1)
