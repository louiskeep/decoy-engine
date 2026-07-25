"""S5 (Sprint 2 honesty pack): the row-error channel primitives.

TDD: written before the implementation. `RowError` / `RowErrorRecord` are
frozen; `StrategyContext.row_errors` is a mutable sink on an otherwise frozen
dataclass (trap T6: append, never reassign); the drain helper attributes
table + clears the sink.
"""

from __future__ import annotations

import dataclasses

import pytest

from decoy_engine.execution._row_errors import RowError, RowErrorRecord, drain_row_errors


class TestRowErrorFrozen:
    def test_row_error_is_frozen(self) -> None:
        err = RowError(column="c", row_index=0, trigger="format_error", reason="not numeric")
        with pytest.raises(dataclasses.FrozenInstanceError):
            err.column = "other"  # type: ignore[misc]

    def test_row_error_record_is_frozen(self) -> None:
        rec = RowErrorRecord(
            table="t", column="c", row_index=0, trigger="format_error", reason="not numeric"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rec.table = "other"  # type: ignore[misc]


class TestDrainRowErrors:
    def test_drain_attributes_table_and_clears_sink(self) -> None:
        sink: list[RowError] = [
            RowError(column="c", row_index=0, trigger="format_error", reason="bad"),
            RowError(column="c", row_index=3, trigger="format_error", reason="bad2"),
        ]
        records = drain_row_errors(sink, table="orders")
        assert records == (
            RowErrorRecord(
                table="orders", column="c", row_index=0, trigger="format_error", reason="bad"
            ),
            RowErrorRecord(
                table="orders", column="c", row_index=3, trigger="format_error", reason="bad2"
            ),
        )
        assert sink == []  # drained

    def test_drain_empty_sink_returns_empty(self) -> None:
        sink: list[RowError] = []
        assert drain_row_errors(sink, table="orders") == ()
        assert sink == []


class TestStrategyContextRowErrorsSink:
    def test_ctx_row_errors_defaults_empty_and_is_appendable(self) -> None:
        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.generation.pool._cache import PoolCache
        from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

        ctx = StrategyContext(
            registry=None,  # type: ignore[arg-type]
            pool_cache=PoolCache(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
            job_seed=b"\x00" * 8,
        )
        assert ctx.row_errors == []
        ctx.row_errors.append(RowError(column="c", row_index=0, trigger="format_error", reason="x"))
        assert len(ctx.row_errors) == 1
        # Frozen dataclass forbids reassigning the field itself.
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.row_errors = []  # type: ignore[misc]
