"""B1 rev9 acceptance tests 20 and 21: the Arrow-native profile of a carried
column, and the FK passthrough guard over read keys only."""

from __future__ import annotations

import dataclasses
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution import _chunked_oracle
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.execution.native._chunked_evidence import plan_column_backends
from decoy_engine.execution.native._dispatch import plan_native_route, run_native_or_oracle_chunked
from decoy_engine.plan import compile_plan, plan_to_yaml
from decoy_engine.profile import _walk
from decoy_engine.profile._types import ColumnProfile
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    faker_col,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    truncate,
)
from tests.native._rev9_profile_cases import CASES, N, case_ids, long_text_cases

_STRATEGIES = {
    "redact": [redact("s")],
    "hash": [hash_col("s")],
    "truncate": [truncate("s")],
    "faker": [faker_col("s")],
}


def _first(name: str, xname: str) -> pa.Table:
    return pa.table({"s": pa.array([f"v{i}" for i in range(N)]), xname: CASES[name]})


def _real_profile(first: pa.Table) -> Any:
    return first_chunk_profile(first, table=TABLE, engine_version=ENGINE_VERSION)


def _r3_profile(first: pa.Table, carried: set[str]) -> Any:
    return first_chunk_profile(
        first, table=TABLE, engine_version=ENGINE_VERSION, carried=frozenset(carried)
    )


def _placeholder_profile(first: pa.Table, xname: str) -> Any:
    i = first.schema.get_field_index(xname)
    return _real_profile(first.set_column(i, xname, pa.nulls(first.num_rows)))


def _compiled(config: dict[str, Any], profile: Any) -> tuple[str, tuple[str, ...]]:
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION, no_profile=True)
    yaml = "\n".join(line for line in plan_to_yaml(plan).splitlines() if "profile_hash" not in line)
    return yaml, tuple(plan.plan_compile.warnings)


@pytest.mark.parametrize("xname", ["x", "notes"])
@pytest.mark.parametrize("case", case_ids())
def test_arrow_column_profile_equals_the_pandas_profile_field_for_field(
    case: str, xname: str
) -> None:
    from decoy_engine.execution._chunked_profile import arrow_column_profile

    first = _first(case, xname)
    real = next(c for c in _real_profile(first).tables[0].columns if c.name == xname)
    ours = arrow_column_profile(xname, first.column(xname))
    for field in dataclasses.fields(ColumnProfile):
        assert getattr(ours, field.name) == getattr(real, field.name), (case, field.name)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
@pytest.mark.parametrize("strategy", list(_STRATEGIES))
@pytest.mark.parametrize("xname", ["x", "notes"])
@pytest.mark.parametrize("case", case_ids())
def test_r3_profile_compiles_the_same_plan_and_route(
    case: str, xname: str, strategy: str, configured: bool
) -> None:
    first = _first(case, xname)
    config = make_config(_STRATEGIES[strategy] + ([passthrough(xname)] if configured else []))
    real, r3 = _real_profile(first), _r3_profile(first, {xname})
    assert _compiled(config, r3) == _compiled(config, real)
    args = {"table": TABLE, "engine_version": ENGINE_VERSION}
    assert (
        plan_native_route(config, r3, first_schema=first.schema, **args).evidence
        == plan_native_route(config, real, first_schema=first.schema, **args).evidence
    )
    assert plan_column_backends(config, r3, **args) == plan_column_backends(config, real, **args)


def test_placeholder_alone_is_not_enough() -> None:
    """Control: replacing the column by nulls without the Arrow-native profile loses the
    HC-7 free-text advisories, so the plan differs from the full profile's."""
    differing = 0
    for case, xname, configured in (
        ("payload_nonull", "x", False),
        ("payload_nonull", "x", True),
        ("time64us", "notes", True),
    ):
        first = _first(case, xname)
        config = make_config(_STRATEGIES["redact"] + ([passthrough(xname)] if configured else []))
        real = _compiled(config, _real_profile(first))
        placeholder = _compiled(config, _placeholder_profile(first, xname))
        r3 = _compiled(config, _r3_profile(first, {xname}))
        assert r3 == real
        if placeholder != real:
            differing += 1
    assert differing >= 2
    assert "payload_nonull" in long_text_cases()


def test_legacy_entry_keeps_the_full_pandas_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, list[Any]]] = []
    real = _walk.walk_dataframe

    def spy(df: Any, *a: Any, **kw: Any) -> Any:
        seen.append({c: list(df[c]) for c in df.columns})
        return real(df, *a, **kw)

    monkeypatch.setattr(_walk, "walk_dataframe", spy)
    table = pa.table({"s": pa.array(["a", "b", "c"]), "x": pa.array([7, 8, 9], pa.int64())})
    config = make_config([redact("s"), passthrough("x")])
    list(
        run_native_or_oracle_chunked(
            config,
            [table],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    assert seen and seen[0]["x"] == [7, 8, 9]


# ---------------------------------------------------------------------------
# Test 21: FK passthrough keys
# ---------------------------------------------------------------------------

_BIG = 2**53 + 1


def _fk_config(*, when: str | None = None) -> dict[str, Any]:
    name_col: dict[str, Any] = {"name": "label", "strategy": "redact"}
    if when is not None:
        name_col["when"] = when
    return {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [{"name": "id", "strategy": "passthrough"}, name_col]},
            {
                "name": "orders",
                "columns": [{"name": "customer_id", "strategy": "hash", "namespace": "ns"}],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }


def _keys(kind: pa.DataType) -> pa.Array:
    big = _BIG if kind == pa.int64() else 2**63 + 1
    return pa.array([1, None, big], kind)


def _chunk(kind: pa.DataType) -> pa.Table:
    return pa.table({"id": _keys(kind), "label": pa.array(["a", "b", "c"])})


def _guard_spy(monkeypatch: pytest.MonkeyPatch) -> list[set[str]]:
    seen: list[set[str]] = []
    real = _chunked_oracle.reject_lossy_chunked_fk_passthrough

    def spy(chunk: pa.Table, *, table: str, passthrough_fk_columns: set[str]) -> None:
        seen.append(set(passthrough_fk_columns))
        real(chunk, table=table, passthrough_fk_columns=passthrough_fk_columns)

    monkeypatch.setattr(_chunked_oracle, "reject_lossy_chunked_fk_passthrough", spy)
    return seen


def _run(fn: Any, config: dict[str, Any], chunks: list[pa.Table]) -> list[pa.Table]:
    return list(
        fn(
            config,
            chunks,
            table="customers",
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )


@pytest.mark.parametrize("kind", [pa.int64(), pa.uint64()], ids=["int64", "uint64"])
def test_carried_fk_key_is_yielded_exactly(
    kind: pa.DataType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _guard_spy(monkeypatch)
    out = _run(run_mask_chunked, _fk_config(), [_chunk(kind)])
    assert out[0].column("id").to_pylist() == _keys(kind).to_pylist()
    assert out[0].column("id").type == kind
    assert all("id" not in cols for cols in seen)


@pytest.mark.parametrize("kind", [pa.int64(), pa.uint64()], ids=["int64", "uint64"])
def test_read_fk_key_keeps_the_refusal(kind: pa.DataType, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _guard_spy(monkeypatch)
    with pytest.raises(ExecutionError) as info:
        _run(run_mask_chunked, _fk_config(when="id.notnull()"), [_chunk(kind)])
    assert info.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
    assert seen == [{"id"}]


@pytest.mark.parametrize("entry", [run_mask_pipeline_chunked, run_native_or_oracle_chunked])
@pytest.mark.parametrize("kind", [pa.int64(), pa.uint64()], ids=["int64", "uint64"])
def test_public_oracle_and_legacy_entry_refuse_as_before(
    entry: Any, kind: pa.DataType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _guard_spy(monkeypatch)
    with pytest.raises(ExecutionError) as info:
        _run(entry, _fk_config(), [_chunk(kind)])
    assert info.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
    assert seen == [{"id"}]
