"""engine-v2 S9 slice 2a: PandasExecutionAdapter end-to-end (no-backend strategies).

Proves the Arrow boundary + the work-list-from-seed-envelope BLOCKER fix end to
end through the real adapter (the no-FK single-table job masks ALL columns).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import (
    ExecutionError,
    ExecutionResult,
    PandasExecutionAdapter,
    get_default_executor,
)
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _col(
    strategy: str,
    provider: str = "x_nobackend",
    *,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=tuple(provider_config),
        coherent_with=(),
    )


def _plan(per_table: list[tuple[str, TableSeed]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=b"\x00" * 8, per_table=tuple(per_table))
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class TestPandasAdapter:
    def test_passthrough_unchanged(self) -> None:
        src = pa.table({"a": ["x", "y", None]})
        plan = _plan([("t", TableSeed(per_column=(("a", _col("passthrough")),), per_group=()))])
        res = _run(plan, src)
        assert res.output.column("a").to_pylist() == ["x", "y", None]

    def test_redact_replaces_nonnull_preserves_null(self) -> None:
        src = pa.table({"email": ["a@b.com", None, "c@d.com"]})
        plan = _plan([("people", TableSeed(per_column=(("email", _col("redact")),), per_group=()))])
        res = _run(plan, src)
        assert res.output.column("email").to_pylist() == ["REDACTED", None, "REDACTED"]

    def test_redact_custom_value(self) -> None:
        src = pa.table({"email": ["a@b.com"]})
        plan = _plan(
            [
                (
                    "people",
                    TableSeed(
                        per_column=(
                            ("email", _col("redact", provider_config=(("redact_with", "X"),))),
                        ),
                        per_group=(),
                    ),
                )
            ]
        )
        assert _run(plan, src).output.column("email").to_pylist() == ["X"]

    def test_truncate_keeps_prefix(self) -> None:
        src = pa.table({"zip": ["12345", "67890", None]})
        plan = _plan(
            [
                (
                    "t",
                    TableSeed(
                        per_column=(("zip", _col("truncate", provider_config=(("length", 3),))),),
                        per_group=(),
                    ),
                )
            ]
        )
        assert _run(plan, src).output.column("zip").to_pylist() == ["123", "678", None]

    def test_h1_no_fk_single_table_masks_all_columns(self) -> None:
        # The BLOCKER fix end-to-end: no FK -> plan.ordering would be empty, but
        # BOTH columns mask because the work list comes from the seed envelope.
        src = pa.table({"a": ["foo"], "b": ["bar"]})
        ts = TableSeed(per_column=(("a", _col("redact")), ("b", _col("redact"))), per_group=())
        res = _run(_plan([("t", ts)]), src)
        assert res.output.column("a").to_pylist() == ["REDACTED"]
        assert res.output.column("b").to_pylist() == ["REDACTED"]

    def test_empty_table(self) -> None:
        src = pa.table({"a": pa.array([], type=pa.string())})
        ts = TableSeed(per_column=(("a", _col("redact")),), per_group=())
        assert _run(_plan([("t", ts)]), src).output.num_rows == 0

    def test_boundary_conversion_recorded(self) -> None:
        src = pa.table({"a": ["x"]})
        ts = TableSeed(per_column=(("a", _col("passthrough")),), per_group=())
        res = _run(_plan([("t", ts)]), src)
        assert isinstance(res, ExecutionResult)
        assert res.boundary_conversion_ms >= 0.0

    def test_unsupported_strategy_raises(self) -> None:
        ts = TableSeed(per_column=(("a", _col("not_a_strategy")),), per_group=())
        with pytest.raises(ExecutionError) as exc:
            _run(_plan([("t", ts)]), pa.table({"a": ["x"]}))
        assert exc.value.code == "unsupported_strategy"

    def test_supports_strategy_and_shutdown_idempotent(self) -> None:
        adapter = PandasExecutionAdapter()
        assert adapter.supports_strategy("redact") is True
        assert adapter.supports_strategy("nope") is False
        adapter.shutdown()
        adapter.shutdown()

    def test_get_default_executor_is_singleton(self) -> None:
        assert get_default_executor() is get_default_executor()


def _fpe_col() -> ColumnSeed:
    return ColumnSeed(
        namespace="fpe_ns",
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("charset", "digits"),),
        coherent_with=(),
    )


class TestParallelismParity:
    """Spec section 5.2 + acceptance criterion 7: the FPE chunk count is a
    wall-clock knob only; output is byte-identical regardless. (Faker per-column
    runner parallelism, section 5.1 / criterion 6, is deferred to S13: the S4
    faker adapter shares a per-locale Faker instance, so concurrent pool builds
    are not thread-safe; that fix + its >=10x gate live in S13.)"""

    def test_fpe_chunk_count_knob_honored_and_parity(self) -> None:
        # 50 rows so chunk_count=4 does not short-circuit to serial inside the
        # FPE handler; the adapter knob must thread through and be byte-identical.
        src = pa.table({"acct": [f"{i:05d}" for i in range(50)]})
        plan = _plan([("t", TableSeed(per_column=(("acct", _fpe_col()),), per_group=()))])
        one = PandasExecutionAdapter(fpe_chunk_count=1).run_single(
            plan, src, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        four = PandasExecutionAdapter(fpe_chunk_count=4).run_single(
            plan, src, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        assert one.output.column("acct").to_pylist() == four.output.column("acct").to_pylist()
        # The knob actually masked (format-preserving digits, not the source).
        assert one.output.column("acct").to_pylist() != src.column("acct").to_pylist()
