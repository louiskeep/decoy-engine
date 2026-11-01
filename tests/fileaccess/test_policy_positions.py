"""Every place that resolves a code_set corpus names the table and column in a denial."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine.fileaccess import FileAccessDenied, scope
from decoy_engine.plan._types import ColumnSeed
from tests.fileaccess.test_policy_denials import TOKEN, World, _assert_denial, world  # noqa: F401

KEY = "tables.orders.columns.code.code_set.corpus_source"


def _provider_config(outside: Any) -> tuple[tuple[str, Any], ...]:
    return (("code_set", "mine"), ("corpus_source", f"customer:{outside}"))


def _column_seed(outside: Any) -> ColumnSeed:
    seed = object.__new__(ColumnSeed)
    object.__setattr__(seed, "provider_config", _provider_config(outside))
    object.__setattr__(seed, "strategy", "code_set")
    return seed


def _outside(world: World) -> Any:  # noqa: F811
    return world.sentinel(f"{TOKEN}.corpus.parquet", b"x")


def test_the_pandas_handler_names_its_column(world: World) -> None:  # noqa: F811
    import pandas as pd

    from decoy_engine.execution._strategies._code_set import CodeSetHandler

    plan = SimpleNamespace(
        provider_config=_provider_config(_outside(world)), namespace="n", when=None
    )
    ctx = SimpleNamespace(
        current_table="orders", nested_outer_column="", code_set_records={}, mask_key=b"k"
    )
    with scope(world.policy), pytest.raises(FileAccessDenied) as info:
        CodeSetHandler().run(pd.DataFrame({"code": ["a"]}), "code", plan, ctx)  # type: ignore[arg-type]
    _assert_denial(info.value, KEY)


def test_the_chunked_resolver_names_its_column(world: World, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from decoy_engine.execution import _runner
    from decoy_engine.execution._chunked_code_set import resolve_pinned_code_set_records

    node = SimpleNamespace(
        table="orders",
        kind="scalar",
        strategy="code_set",
        columns=("code",),
        plan_slice=_column_seed(_outside(world)),
    )
    monkeypatch.setattr(_runner, "build_work_list", lambda plan, registry: [node])
    monkeypatch.setattr(_runner, "order_work", lambda work, graph: work)
    with scope(world.policy), pytest.raises(FileAccessDenied) as info:
        resolve_pinned_code_set_records(None, None, None, table="orders")  # type: ignore[arg-type]
    _assert_denial(info.value, KEY)


def test_the_out_of_core_stream_driver_names_its_column(world: World, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from decoy_engine.execution.out_of_core import _stream_driver_support as support

    seed = SimpleNamespace(per_column=[("code", _column_seed(_outside(world)))])
    monkeypatch.setattr(support, "table_seed", lambda plan, table: seed)
    with scope(world.policy), pytest.raises(FileAccessDenied) as info:
        support._code_set_records_and_evidence_for_table(
            None,  # type: ignore[arg-type]
            "orders",
            ["code"],
            skip_columns=frozenset(),
        )
    _assert_denial(info.value, KEY)


def test_the_out_of_core_runner_uses_the_same_resolver() -> None:
    """The runner had its own copy of the stream driver's resolver; there is now one."""
    from decoy_engine.execution.out_of_core import _runner
    from decoy_engine.execution.out_of_core import _stream_driver_support as support

    assert (
        _runner._code_set_records_and_evidence_for_table
        is support._code_set_records_and_evidence_for_table
    )
