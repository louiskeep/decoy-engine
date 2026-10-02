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
    faker_caps = base.get_capabilities("person_first_name").model_copy(
        update={"provider": "composite_name_email"}
    )
    return base.override("composite_name_email", base.get_adapter("person_first_name"), faker_caps)


def _custom_composite_registry() -> Any:
    base = get_default_registry()
    caps = composite_capability("composite_custom").model_copy(update={"provider": "composite_x"})
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


# ---------------------------------------------------------------------------
# Units that pin the registry plumbing and the "everything" fallbacks
# ---------------------------------------------------------------------------


def test_an_undeclarable_entry_leaves_no_passthrough_column() -> None:
    reg = _custom_composite_registry()
    entries = [_entry("a", "composite_x", "redact", provider_config={"bundle": _BUNDLE})]
    config = {"tables": [{"name": TABLE, "columns": entries}]}
    assert column_access(entries[0], reg).everything is True
    assert handler_written_columns(entries, reg) is None
    assert passthrough_columns(config, table=TABLE, names=["a", "b", "c"], registry=reg) == []


def test_an_undeclarable_entry_refuses_any_stored_index_field() -> None:
    from decoy_engine.execution._transforms import reject_config_references_stored_index

    bad = {"name": "v", "strategy": "derived", "provider_config": {"expression": "a +"}}
    config = {"tables": [{"name": TABLE, "columns": [bad]}]}
    schema = _indexed()[0].schema
    with pytest.raises(TransformError) as info:
        reject_config_references_stored_index(config, TABLE, schema, REG)
    assert info.value.code == "config_references_stored_index"
    reject_config_references_stored_index(
        {"tables": [{"name": TABLE, "columns": [redact("s")]}]}, TABLE, schema, REG
    )


def test_a_composite_entry_keeps_its_own_name_in_the_touched_set() -> None:
    from decoy_engine.execution._column_access import touched_columns

    lone = _entry("first_name", "composite_name_email", "redact")
    assert touched_columns([lone], REG) == {"first_name", "last_name", "email"}
    plain = {"name": "x", "strategy": "redact", "provider_config": {"group_by": "x"}}
    assert touched_columns([plain], REG) == frozenset()


def test_the_run_registry_reaches_the_compatibility_check(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = _chunked.check_chunked_compatibility

    def spy(config: Any, **kwargs: Any) -> None:
        seen.append(kwargs.get("registry"))
        real(config, **kwargs)

    monkeypatch.setattr(_chunked, "check_chunked_compatibility", spy)
    reg = _scalar_rebound_registry()
    chunk = pa.table({"s": ["a", "b"]})
    list(
        run_mask_chunked(
            make_config([redact("s")]),
            [chunk],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            registry=reg,
        )
    )
    assert seen == [reg]


# ---------------------------------------------------------------------------
# Test 15 (revision 4.5): the row-error path and a caller-only composite end to end
# ---------------------------------------------------------------------------


def _row_error_run(config: dict[str, Any], chunks: list[pa.Table], **kw: Any) -> list[Any]:
    from decoy_engine.errors import RowErrorsFailedError

    sink: list[Any] = []
    with pytest.raises(RowErrorsFailedError):
        list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                chunk_result_sink=sink,
                **kw,
            )
        )
    return sink


@pytest.mark.parametrize("case", ["composite_name_email-redact", "composite_custom-passthrough"])
def test_row_error_chunk_reports_generated_values_for_written_columns(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provider, columns, data, written = CASES[case]
    bad = {
        "name": "age",
        "strategy": "bucketize",
        "provider_config": {"width": 10},
    }
    data = {**data, "age": ["23", "x1", "47", "50", "51", "52"]}
    config = make_config([*columns, bad], global_settings=_WARN)
    _patch_out_composite_refusal(monkeypatch)
    sink = _row_error_run(config, [_source(data)])
    assert sink, "the failing chunk is reported to the sink"
    out = sink[0].outputs[TABLE]
    source = _source(data)
    for name in written:
        got, src = out.column(name).to_pylist(), source.column(name).to_pylist()
        assert all(a != b for a, b in zip(got, src, strict=True)), name


def _compile_with_registry(monkeypatch: pytest.MonkeyPatch, reg: Any) -> None:
    """`compile_plan` resolves providers through the default registry; scope that lookup to
    the compile call so the registry under test is the only one the run itself sees."""
    import decoy_engine.plan as plan_mod
    import decoy_engine.providers_v2 as providers_mod

    real_compile = plan_mod.compile_plan
    real_default = providers_mod.get_default_registry

    def compile_plan(*args: Any, **kwargs: Any) -> Any:
        providers_mod.get_default_registry = lambda: reg  # type: ignore[assignment]
        try:
            return real_compile(*args, **kwargs)
        finally:
            providers_mod.get_default_registry = real_default  # type: ignore[assignment]

    monkeypatch.setattr(plan_mod, "compile_plan", compile_plan)


@pytest.mark.parametrize("row_errors", [False, True], ids=["normal", "row_error"])
def test_caller_only_composite_end_to_end_through_run_mask_chunked(
    row_errors: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _custom_composite_registry()
    columns = [
        _entry(
            name,
            "composite_x",
            "redact",
            coherent_with=[other],
            provider_config={"bundle": _BUNDLE},
        )
        for name, other in (("a", "b"), ("b", "a"))
    ]
    data = {c: [f"SECRET-{c}-{i}" for i in range(N)] for c in ("a", "b", "c")}
    if row_errors:
        columns.append({"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}})
        data["age"] = ["23", "x1", "47", "50", "51", "52"]
    config = make_config(columns, global_settings=_WARN)
    _patch_out_composite_refusal(monkeypatch)
    _compile_with_registry(monkeypatch, reg)
    source = _source(data)
    if row_errors:
        sink = _row_error_run(config, [source], registry=reg)
        produced = sink[0].outputs[TABLE]
    else:
        out = list(
            run_mask_chunked(
                config,
                [source],
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                registry=reg,
            )
        )
        produced = pa.concat_tables(out)
    for name in ("a", "b", "c"):
        got, src = produced.column(name).to_pylist(), source.column(name).to_pylist()
        assert all(x != y for x, y in zip(got, src, strict=True)), name


# ---------------------------------------------------------------------------
# Test 16 (revision 4.5): output equal to the public oracle under the rebound registry
# ---------------------------------------------------------------------------


def test_rebound_registry_output_equals_the_public_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _scalar_rebound_registry()
    columns = [
        {**redact("s"), "provider": "composite_name_email", "when": "x > 1"},
        passthrough("x"),
    ]
    config = make_config(columns, global_settings=_WARN)
    chunks = [
        pa.table({"s": ["a", "b", "c"], "x": pa.array([1, 5, 9], pa.int64())}),
        pa.table({"s": ["d", "e", "f"], "x": pa.array([9, 1, 5], pa.int64())}),
    ]
    kwargs: dict[str, Any] = {
        "table": TABLE,
        "engine_version": ENGINE_VERSION,
        "key_provider": key_provider(),
        "registry": reg,
    }
    ours = list(run_mask_chunked(config, chunks, **kwargs))
    oracle = list(run_mask_pipeline_chunked(config, chunks, **kwargs))
    assert [o.to_pydict() for o in ours] == [o.to_pydict() for o in oracle]
    assert [o.schema.types for o in ours] == [o.schema.types for o in oracle]


# ---------------------------------------------------------------------------
# Test 21: an unparsable `when:` keeps passthrough exact
# ---------------------------------------------------------------------------

_UNPARSABLE = "r != b'zz'"


def _when_config(extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return make_config(
        [{**redact("r"), "when": _UNPARSABLE}, *(extra or [])], global_settings=_WARN
    )


def test_unparsable_when_keeps_a_big_int_passthrough_exact() -> None:
    from tests.native._rev9_support import run_entry, run_public

    big = 2**53 + 1
    chunks = [
        pa.table(
            {
                "r": pa.array(["a", "b", "c"]),
                "big": pa.array([big, None, 3], pa.int64()),
            }
        )
        for _ in range(2)
    ]
    out, sink, _ev = run_entry(_when_config(), chunks)
    for got, src in zip(out, chunks, strict=True):
        assert got.schema.field("big").equals(src.schema.field("big"), check_metadata=True)
        assert got.column("big").to_pylist() == [big, None, 3]
    assert [r.quality_metrics["chunked_route"]["pandas_read_passthrough"] for r in sink] == [
        ["big"]
    ] * 2
    # The unchanged public oracle rounds it through float64; only characterized here.
    public = run_public(_when_config(), chunks)
    assert public[0].schema.field("big").type == pa.float64()
    assert public[0].column("big").to_pylist()[0] == float(2**53)


def test_unparsable_when_still_raises_for_an_unrepresentable_value() -> None:
    from tests.native._rev9_support import BY_NAME, run_public

    t64 = BY_NAME["time64ns_unaligned"]
    chunks = [
        pa.table({"r": pa.array(["a", "b", "c"]), "t": t64.good}),
        pa.table({"r": pa.array(["a", "b", "c"]), "t": t64.bad}),
    ]
    gen = run_mask_chunked(
        _when_config(),
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
    )
    next(gen)
    with pytest.raises(Exception) as ours:
        next(gen)
    assert getattr(ours.value, "code", None) == "chunked_passthrough_value_unrepresentable"
    with pytest.raises(pa.ArrowInvalid):
        run_public(_when_config(), chunks)


def test_unparsable_when_does_not_refuse_an_unrelated_stored_index() -> None:
    config = make_config(
        [{**redact("s"), "when": "s != b'zz'"}, truncate("t")], global_settings=_WARN
    )
    for entry_point in (run_mask_chunked, run_mask_pipeline_chunked):
        list(
            entry_point(
                config,
                _indexed(),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )


def test_unparsable_when_naming_a_named_stored_index_runs_on_the_reconstructed_index() -> None:
    config = make_config(
        [{**redact("s"), "when": "id != b'zz'"}, truncate("t")], global_settings=_WARN
    )
    kwargs: dict[str, Any] = {
        "table": TABLE,
        "engine_version": ENGINE_VERSION,
        "key_provider": key_provider(),
    }
    ours = list(run_mask_chunked(config, _indexed(), **kwargs))
    oracle = list(run_mask_pipeline_chunked(config, _indexed(), **kwargs))
    assert all("id" not in o.column_names for o in ours)
    assert [o.column("s").to_pylist() for o in ours] == [o.column("s").to_pylist() for o in oracle]


def test_unparsable_when_naming_an_unnamed_stored_index_keeps_the_typed_error() -> None:
    df = pd.DataFrame({"s": ["a", "b"], "t": ["abcdef", "ghijkl"]})
    df.index = pd.Index(["i1", "i2"])
    table = pa.Table.from_pandas(df)
    assert "__index_level_0__" in table.column_names
    config = make_config(
        [{**redact("s"), "when": "__index_level_0__ != b'zz'"}, truncate("t")],
        global_settings=_WARN,
    )
    for entry_point in (run_mask_chunked, run_mask_pipeline_chunked):
        with pytest.raises(Exception) as info:
            list(
                entry_point(
                    config,
                    [table],
                    table=TABLE,
                    engine_version=ENGINE_VERSION,
                    key_provider=key_provider(),
                )
            )
        assert getattr(info.value, "code", None) == "when_expression_error"


# ---------------------------------------------------------------------------
# Test 22: required registry
# ---------------------------------------------------------------------------


def test_registry_is_a_required_keyword() -> None:
    from decoy_engine.execution import _pipeline_auto_chunk
    from decoy_engine.execution._column_access import composite_provider_offenders

    config = {"tables": [{"name": TABLE, "columns": [redact("s")]}]}
    with pytest.raises(TypeError):
        check_chunked_compatibility(config, table=TABLE)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        composite_provider_offenders([redact("s")])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        _pipeline_auto_chunk._legacy_route_evidence(  # type: ignore[call-arg]
            config,
            pa.table({"s": ["a"]}),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            chunk_size_rows=1,
            chunk_count=1,
            lane_reason=None,
        )


def test_no_internal_module_feeds_the_registry_from_the_default() -> None:
    """Only the public entry points and the existing registry-owning modules resolve the
    default registry; the declaration and admission helpers never do."""
    import pathlib
    import re

    root = pathlib.Path(_chunked.__file__).parent
    offenders = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "physical" not in path.parts and re.search(r"get_default_registry\(\)", path.read_text())
    }
    helpers = {
        "_column_access.py",
        "_chunked.py",
        "_chunked_carry.py",
        "native/_chunked_schema_rule.py",
        "_pipeline_auto_chunk.py",
        "_planner.py",
        "_transforms.py",
    }
    assert not (offenders & helpers), sorted(offenders & helpers)


# ---------------------------------------------------------------------------
# Test 23: public-entry drift and the evidence registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["index_then_plain", "plain_then_index"])
@pytest.mark.parametrize("entry_name", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_stored_index_drift_is_raised_by_both_public_entries(
    entry_name: str, direction: str
) -> None:
    entry_point = (
        run_mask_chunked if entry_name == "run_mask_chunked" else run_mask_pipeline_chunked
    )
    indexed = _indexed()
    plain = [c.replace_schema_metadata(None) for c in indexed]
    chunks = [indexed[0], plain[1]] if direction == "index_then_plain" else [plain[0], indexed[1]]
    sink: list[Any] = []
    gen = entry_point(
        make_config([redact("s"), truncate("t")], global_settings=_WARN),
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
    )
    next(gen)
    with pytest.raises(Exception) as info:
        next(gen)
    assert getattr(info.value, "code", None) == "native_chunk_schema_drift"
    assert len(sink) == 1


def test_a_rebound_composite_name_is_reported_with_the_scalar_route() -> None:
    reg = _scalar_rebound_registry()
    config = make_config(
        [{**redact("s"), "provider": "composite_name_email"}], global_settings=_WARN
    )
    chunk = pa.table({"s": ["a", "b"]})
    sink: list[Any] = []
    ev: list[Any] = []
    list(
        run_mask_chunked(
            config,
            [chunk],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            registry=reg,
            chunk_result_sink=sink,
            route_evidence_sink=ev,
        )
    )
    assert ev[0].native_admitted is True and ev[0].reroute_reason is None
    columns = sink[0].quality_metrics["chunked_route"]["columns"]
    assert [(c["column"], c["planned_backend"], c["executed_backend"]) for c in columns] == [
        ("s", "arrow_python", "arrow_python")
    ]


# ---------------------------------------------------------------------------
# Test 24: access-flag cross-product and fail-closed restoration
# ---------------------------------------------------------------------------

_BAD_WHEN = "first_name != b'zz'"


def _flags(entry: dict[str, Any]) -> tuple[bool, bool]:
    access = column_access(entry, REG)
    return access.reads_unknown, access.writes_unknown


def _bad_bundle_pair() -> list[dict[str, Any]]:
    return [
        _entry(
            name,
            "composite_custom",
            "redact",
            coherent_with=[other],
            provider_config={"bundle": "not-a-list"},
        )
        for name, other in (("a", "b"), ("b", "a"))
    ]


def test_access_flags_for_each_reads_and_writes_combination() -> None:
    scalar = {**redact("s"), "when": "s != b'zz'"}
    assert _flags(scalar) == (True, False)
    fixed = {**_entry("first_name", "composite_name_email", "redact"), "when": _BAD_WHEN}
    assert _flags(fixed) == (True, False)
    assert column_access(fixed, REG).writes == {"first_name", "last_name", "email"}
    custom_ok = {
        **_entry(
            "a",
            "composite_custom",
            "redact",
            coherent_with=["b"],
            provider_config={"bundle": _BUNDLE},
        ),
        "when": "a != b'zz'",
    }
    assert _flags(custom_ok) == (True, False)
    assert column_access(custom_ok, REG).writes >= {"a", "b", "c"}
    assert _flags(_bad_bundle_pair()[0]) == (False, True)
    assert _flags({"name": "x", "strategy": "bogus_strategy"}) == (False, True)
    both = {**_bad_bundle_pair()[0], "when": "a != b'zz'"}
    assert _flags(both) == (True, True)


def test_reads_unknown_keeps_candidates_and_writes_unknown_empties_them() -> None:
    from decoy_engine.execution._chunked_carry import read_set

    first = _source({"s": ["a"] * N, "big": ["x"] * N})
    scalar = {**redact("s"), "when": "s != b'zz'"}
    config = {"tables": [{"name": TABLE, "columns": [scalar]}]}
    assert handler_written_columns([scalar], REG) == frozenset()
    assert passthrough_columns(config, table=TABLE, names=first.column_names, registry=REG) == [
        "big"
    ]
    rule = build_schema_rule(config, table=TABLE, first=first, registry=REG)
    assert set(rule.passthrough_types) == {"big"}
    assert read_set([scalar], ["big"], REG) == {"big"}
    carry = plan_carry(config, table=TABLE, first_schema=first.schema, adapter=None, registry=REG)
    assert carry.carried == frozenset() and carry.read == ("big",)
    for entries in (_bad_bundle_pair(), [{"name": "x", "strategy": "bogus_strategy"}]):
        config = {"tables": [{"name": TABLE, "columns": entries}]}
        assert handler_written_columns(entries, REG) is None
        assert passthrough_columns(config, table=TABLE, names=["a", "b", "c"], registry=REG) == []
        wide = _source({"a": ["x"] * N, "b": ["y"] * N, "c": ["z"] * N})
        assert (
            build_schema_rule(config, table=TABLE, first=wide, registry=REG).passthrough_types == {}
        )


@pytest.mark.parametrize("row_errors", [False, True], ids=["normal", "row_error"])
def test_a_composite_with_an_unparsable_when_still_generates_every_output(
    row_errors: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = {**_entry("first_name", "composite_name_email", "redact"), "when": _BAD_WHEN}
    columns: list[dict[str, Any]] = [entry]
    data = {c: [f"SECRET-{c}-{i}" for i in range(N)] for c in ("first_name", "last_name", "email")}
    if row_errors:
        columns.append({"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}})
        data["age"] = ["23", "x1", "47", "50", "51", "52"]
    config = make_config(columns, global_settings=_WARN)
    _patch_out_composite_refusal(monkeypatch)
    source = _source(data)
    if row_errors:
        produced = _row_error_run(config, [source])[0].outputs[TABLE]
    else:
        produced = pa.concat_tables(
            list(
                run_mask_chunked(
                    config,
                    [source],
                    table=TABLE,
                    engine_version=ENGINE_VERSION,
                    key_provider=key_provider(),
                )
            )
        )
    for name in ("first_name", "last_name", "email"):
        got, src = produced.column(name).to_pylist(), source.column(name).to_pylist()
        assert all(a != b for a, b in zip(got, src, strict=True)), name


def test_writes_unknown_entries_are_refused_by_the_public_entries() -> None:
    bad_bundle = make_config(_bad_bundle_pair(), global_settings=_WARN)
    source = _source({"a": ["x"] * N, "b": ["y"] * N, "c": ["z"] * N})
    unknown = {"tables": [{"name": TABLE, "columns": [{"name": "a", "strategy": "bogus"}]}]}
    for config in (bad_bundle, unknown):
        for entry_point in (run_mask_chunked, run_mask_pipeline_chunked):
            sink: list[Any] = []
            with pytest.raises(Exception):
                list(
                    entry_point(
                        config,
                        [source],
                        table=TABLE,
                        engine_version=ENGINE_VERSION,
                        key_provider=key_provider(),
                        chunk_result_sink=sink,
                    )
                )
            assert sink == []
