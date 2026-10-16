"""A `when:`-gated hash of an integer column is rejected cleanly, before its table masks.

Conditional hashing of an integer column is deferred. Left alone it crashes late: string tokens
cannot be written back into an integer frame column, and which chunk trips it depends on where a
null falls. The rejection keys on the Arrow schema, so it does not depend on nulls, the profile
or the chunk partition. Each case also pins that no masking work started for the affected table.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution import PandasExecutionAdapter, _pandas_adapter, _sequential
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._guards import reject_gated_hash_on_integer
from decoy_engine.execution._strategies._hash import HashStrategyHandler
from decoy_engine.plan._types import SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    hash_col,
    key_provider,
    make_config,
)
from tests.parity._d1a_hash_data import JOB_SEED, column_seed

_CODE = "gated_hash_on_integer_unsupported"
_REGISTRY = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_PLAIN = column_seed()


def _gated(expr: str = "n == 1") -> Any:
    return replace(_PLAIN, when=expr)


def _plan(**tables: tuple[tuple[str, Any], ...]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=JOB_SEED,
            per_table=tuple(
                (name, TableSeed(per_column=cols, per_group=())) for name, cols in tables.items()
            ),
        )
    )


class _Spies:
    """Counts every entry into masking work for a table."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.handler_tables: list[str] = []
        real = HashStrategyHandler.run

        def spy(handler: Any, df: Any, column: str, plan: Any, ctx: Any) -> Any:
            self.handler_tables.append(ctx.current_table)
            return real(handler, df, column, plan, ctx)

        monkeypatch.setattr(HashStrategyHandler, "run", spy)
        self.conversions: list[int] = []
        for module in (_pandas_adapter, _sequential):
            real_convert = module.to_pandas_fk_safe

            def convert(table: Any, cols: Any, _real: Any = real_convert) -> Any:
                self.conversions.append(table.num_rows)
                return _real(table, cols)

            monkeypatch.setattr(module, "to_pandas_fk_safe", convert)


class _RecordingSink:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.aborts = 0

    def write(self, table: str, data: pa.Table) -> None:
        self.written.append(table)

    def commit(self) -> None: ...

    def abort(self) -> None:
        self.aborts += 1


def _run_full(plan: Any, sources: dict[str, pa.Table]) -> None:
    PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REGISTRY,
        relationship_graph=_GRAPH,
        namespace_registry=NamespaceRegistry(bindings=()),
    )


def _run_seq(plan: Any, sources: dict[str, pa.Table], sink: Any = None) -> None:
    PandasExecutionAdapter().run_sequential(
        plan,
        lambda t: sources[t],
        registry=_REGISTRY,
        relationship_graph=_GRAPH,
        namespace_registry=NamespaceRegistry(bindings=()),
        sink=sink,
    )


_INT_TABLES = {
    "with-null": pa.table({"n": pa.array([1, None, 3], pa.int64())}),
    "non-null": pa.table({"n": pa.array([1, 2, 3], pa.int64())}),
    "all-null": pa.table({"n": pa.array([None, None], pa.int64())}),
    "uint64": pa.table({"n": pa.array([1, 2**64 - 1], pa.uint64())}),
}


@pytest.mark.parametrize("source", list(_INT_TABLES))
@pytest.mark.parametrize("route", ["full", "seq"])
def test_pandas_routes_reject_before_any_masking_work(
    route: str, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _Spies(monkeypatch)
    plan = _plan(t=(("n", _gated()),))
    run = _run_full if route == "full" else _run_seq
    with pytest.raises(ExecutionError) as exc:
        run(plan, {"t": _INT_TABLES[source]})
    assert exc.value.code == _CODE
    assert spies.handler_tables == []
    assert spies.conversions == []


def test_a_later_table_is_rejected_at_its_own_admission_and_does_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spies = _Spies(monkeypatch)
    sink = _RecordingSink()
    plan = _plan(A=(("s", _PLAIN),), B=(("n", _gated()),))
    sources = {
        "A": pa.table({"s": ["x", "y"]}),
        "B": pa.table({"n": pa.array([1, None], pa.int64())}),
    }
    with pytest.raises(ExecutionError) as exc:
        _run_seq(plan, sources, sink)
    assert exc.value.code == _CODE
    assert "B.n" in str(exc.value)
    # Table A (supported) may mask first, as with the null guard; B does nothing.
    assert set(spies.handler_tables) <= {"A"}
    assert "B" not in sink.written
    assert sink.aborts == 1


def test_a_same_named_column_of_another_type_is_not_confused() -> None:
    plan = _plan(a=(("n", _gated()),), b=(("n", _gated()),))
    ok = {"a": pa.schema([("n", pa.string())])}
    reject_gated_hash_on_integer(plan, ok, _REGISTRY, _GRAPH)  # string: no raise
    with pytest.raises(ExecutionError) as exc:
        reject_gated_hash_on_integer(
            plan, {**ok, "b": pa.schema([("n", pa.int64())])}, _REGISTRY, _GRAPH
        )
    assert exc.value.code == _CODE
    assert "b.n" in str(exc.value)
    assert "a.n" not in str(exc.value)


def test_same_name_different_type_runs_the_string_table_and_rejects_only_the_integer_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spies = _Spies(monkeypatch)
    plan = _plan(a=(("n", _gated("n == 'x'")),), b=(("n", _gated()),))
    sources = {
        "a": pa.table({"n": ["x", "y"]}),
        "b": pa.table({"n": pa.array([1, 2], pa.int64())}),
    }
    with pytest.raises(ExecutionError) as exc:
        _run_seq(plan, sources, _RecordingSink())
    assert exc.value.code == _CODE and "b.n" in str(exc.value)
    assert "b" not in spies.handler_tables


@pytest.mark.parametrize("non_gated_type", [pa.string(), pa.float64(), pa.bool_()])
def test_only_integer_columns_are_rejected(non_gated_type: pa.DataType) -> None:
    plan = _plan(t=(("n", _gated()),))
    reject_gated_hash_on_integer(plan, {"t": pa.schema([("n", non_gated_type)])}, _REGISTRY, _GRAPH)


def test_ungated_integer_hash_and_other_strategies_are_not_rejected() -> None:
    schemas = {"t": pa.schema([("n", pa.int64())])}
    reject_gated_hash_on_integer(_plan(t=(("n", _PLAIN),)), schemas, _REGISTRY, _GRAPH)
    passthrough = replace(column_seed("passthrough", namespace=None), when="n == 1")
    reject_gated_hash_on_integer(_plan(t=(("n", passthrough),)), schemas, _REGISTRY, _GRAPH)


def test_a_table_missing_from_the_schema_map_is_skipped() -> None:
    reject_gated_hash_on_integer(_plan(t=(("n", _gated()),)), {}, _REGISTRY, _GRAPH)


# ---------------------------------------------------------------------------
# The chunked entrypoints: rejected when the output iterator is CREATED, with the late-null
# partition that used to pass the first chunk and crash on the second.
# ---------------------------------------------------------------------------

_LATE_NULL = [
    pa.table({"c": pa.array([1, 2], pa.int64())}),
    pa.table({"c": pa.array([None, 3], pa.int64())}),
]
_CHUNK_PARTITIONS = {
    "late-null": _LATE_NULL,
    "mixed-first": [pa.table({"c": pa.array([1, None], pa.int64())})],
    "all-null": [pa.table({"c": pa.array([None, None], pa.int64())})],
    "non-null": [pa.table({"c": pa.array([1, 2, 3], pa.int64())})],
}


@NEEDS_COMPANION
@pytest.mark.parametrize("partition", list(_CHUNK_PARTITIONS))
@pytest.mark.parametrize("fn", [run_mask_chunked, run_mask_pipeline_chunked])
def test_chunked_entrypoints_reject_on_creation_with_no_chunk_masked(
    fn: Any, partition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _chunked_entry

    adapter_runs: list[int] = []
    native_chunks: list[int] = []
    real_run = PandasExecutionAdapter.run
    real_native = _chunked_entry._mask_chunk_native

    def run_spy(self: Any, *a: Any, **k: Any) -> Any:
        adapter_runs.append(1)
        return real_run(self, *a, **k)

    def native_spy(*a: Any, **k: Any) -> Any:
        native_chunks.append(1)
        return real_native(*a, **k)

    monkeypatch.setattr(PandasExecutionAdapter, "run", run_spy)
    monkeypatch.setattr(_chunked_entry, "_mask_chunk_native", native_spy)
    config = make_config([{**hash_col("c"), "when": "c == 1"}])
    # No iteration: the rejection must be raised by the call itself.
    with pytest.raises(ExecutionError) as exc:
        fn(
            config,
            list(_CHUNK_PARTITIONS[partition]),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    assert exc.value.code == _CODE
    assert adapter_runs == [] and native_chunks == []


def test_a_gated_hash_over_a_string_column_still_runs_on_the_chunked_route() -> None:
    config = make_config([{**hash_col("c"), "when": "c == 'a'"}])
    chunks = [pa.table({"c": pa.array(["a", "b"])}), pa.table({"c": pa.array([None, "a"])})]
    out = list(
        run_mask_pipeline_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    assert sum(t.num_rows for t in out) == 4
