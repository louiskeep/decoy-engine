"""engine-v2 S11: polars I/O boundary + PolarsExecutionAdapter.

Covers the four S11 deliverables: the polars-direct source/target I/O, the
ConversionBoundary instrument, the PolarsExecutionAdapter (protocol conformance +
fallback-to-pandas byte-for-byte parity), and the DECOY_SUBSTRATE selector. At
S11 close no strategy is polars-native, so every adapter result must match the
pandas adapter exactly; that identity is the parity gate S12 builds on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import (
    ExecutionAdapter,
    ExecutionError,
    PandasExecutionAdapter,
    PolarsExecutionAdapter,
    get_default_executor,
    resolve_substrate,
    select_execution_adapter,
)
from decoy_engine.execution._pandas_adapter import _reset_default_executor_for_tests
from decoy_engine.execution.polars import (
    ConversionBoundary,
    read_source_polars,
    write_target_polars,
)
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
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


def _plan(per_table: list[tuple[str, TableSeed]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=b"\x00" * 8, per_table=tuple(per_table))
    )


def _hash_col(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _fk_setup() -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """A single-column FK job (customers -> orders); FK resolution is the
    remaining non-polars-native work after the 11 scalar strategies migrate."""
    plan = _plan(
        [
            (
                "customers",
                TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
            ),
            ("orders", TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=())),
        ]
    )
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2", "c3"]}),
        "orders": pa.table({"customer_id": ["c1", "c2", "c1"]}),
    }
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    return plan, sources, RelationshipGraph(edges=(edge,), ordering=())


def _pandas_outputs(plan: Any, sources: dict[str, pa.Table]) -> dict[str, list[Any]]:
    res = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    return {t: tbl.to_pydict() for t, tbl in res.outputs.items()}


def _polars_run(plan: Any, sources: dict[str, pa.Table], **kwargs: Any) -> Any:
    return PolarsExecutionAdapter(**kwargs).run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class TestConversionBoundary:
    def test_round_trip_preserves_columns_and_values(self) -> None:
        table = pa.table(
            {"s": ["x", "y", None], "i": [1, 2, 3], "f": [1.5, 2.5, None], "b": [True, False, True]}
        )
        boundary = ConversionBoundary()
        back = boundary.to_arrow(boundary.to_polars(table))
        assert back.column_names == table.column_names
        assert back.to_pydict() == table.to_pydict()

    def test_round_trip_accrues_both_conversion_legs(self) -> None:
        boundary = ConversionBoundary()
        boundary.to_arrow(boundary.to_polars(pa.table({"a": [1, 2, 3]})))
        assert boundary.pa_to_pl_ms >= 0.0
        assert boundary.pl_to_pa_ms >= 0.0
        assert boundary.total_ms == pytest.approx(
            boundary.source_read_ms
            + boundary.target_write_ms
            + boundary.pa_to_pl_ms
            + boundary.pl_to_pa_ms
        )

    def test_as_dict_keys(self) -> None:
        keys = set(ConversionBoundary().as_dict())
        assert keys == {
            "source_read_ms",
            "target_write_ms",
            "pa_to_pl_ms",
            "pl_to_pa_ms",
            "total_ms",
        }


class TestSourceReader:
    def test_csv_read(self, tmp_path: Any) -> None:
        path = tmp_path / "src.csv"
        pl.DataFrame({"a": ["x", "y"], "b": [1, 2]}).write_csv(path)
        boundary = ConversionBoundary()
        table = read_source_polars(str(path), file_type="csv", boundary=boundary)
        assert table.column_names == ["a", "b"]
        assert table.column("a").to_pylist() == ["x", "y"]
        assert boundary.source_read_ms >= 0.0
        assert boundary.pl_to_pa_ms >= 0.0

    def test_parquet_read(self, tmp_path: Any) -> None:
        path = tmp_path / "src.parquet"
        pl.DataFrame({"a": ["x", "y"], "b": [1, 2]}).write_parquet(path)
        table = read_source_polars(str(path), file_type="parquet")
        assert table.column_names == ["a", "b"]
        assert table.column("b").to_pylist() == [1, 2]

    def test_ipc_read(self, tmp_path: Any) -> None:
        path = tmp_path / "src.arrow"
        pl.DataFrame({"a": ["x", "y"]}).write_ipc(path)
        table = read_source_polars(str(path), file_type="ipc")
        assert table.column("a").to_pylist() == ["x", "y"]

    def test_unsupported_source_type_raises(self, tmp_path: Any) -> None:
        with pytest.raises(ExecutionError) as exc:
            read_source_polars(str(tmp_path / "x.json"), file_type="json")
        assert exc.value.code == "unsupported_source_file_type"


class TestTargetWriter:
    def test_csv_write_round_trips(self, tmp_path: Any) -> None:
        path = tmp_path / "out.csv"
        table = pa.table({"a": ["x", "y"], "b": [1, 2]})
        boundary = ConversionBoundary()
        write_target_polars(table, str(path), file_type="csv", boundary=boundary)
        back = pl.read_csv(path)
        assert back.columns == ["a", "b"]
        assert back["a"].to_list() == ["x", "y"]
        assert boundary.pa_to_pl_ms >= 0.0
        assert boundary.target_write_ms >= 0.0

    def test_parquet_write_preserves_schema(self, tmp_path: Any) -> None:
        path = tmp_path / "out.parquet"
        table = pa.table({"a": ["x"], "b": [1], "f": [1.5]})
        write_target_polars(table, str(path), file_type="parquet")
        back = pq.read_table(path)
        assert back.column_names == ["a", "b", "f"]
        assert back.to_pydict() == table.to_pydict()

    def test_unsupported_target_type_raises(self, tmp_path: Any) -> None:
        with pytest.raises(ExecutionError) as exc:
            write_target_polars(pa.table({"a": [1]}), str(tmp_path / "x.json"), file_type="json")
        assert exc.value.code == "unsupported_target_file_type"


class TestPolarsAdapterConformance:
    def test_is_execution_adapter(self) -> None:
        adapter = PolarsExecutionAdapter()
        assert isinstance(adapter, ExecutionAdapter)
        # static conformance: assignable to the protocol type
        _: ExecutionAdapter = adapter
        assert adapter.adapter_name == "polars"
        assert adapter.adapter_version == pl.__version__

    def test_supports_all_eleven_strategies(self) -> None:
        # S12 close: all 11 mask strategies are migrated (native or pandas-port).
        adapter = PolarsExecutionAdapter()
        for native in (
            "passthrough",
            "redact",
            "truncate",
            "shuffle",
            "hash",
            "categorical",
            "date_shift",
            "bucketize",
            "fpe",
            "faker",
            "formula",
        ):
            assert adapter.supports_strategy(native) is True
        assert adapter.supports_strategy("not_a_strategy") is False

    def test_shutdown_idempotent(self) -> None:
        adapter = PolarsExecutionAdapter()
        adapter.shutdown()
        adapter.shutdown()


class TestFallbackParity:
    def test_scalar_strategies_match_pandas_byte_for_byte(self) -> None:
        src = pa.table({"email": ["a@b.com", None, "c@d.com"], "zip": ["12345", "67890", "00000"]})
        plan = _plan(
            [
                (
                    "people",
                    TableSeed(
                        per_column=(
                            ("email", _col("redact")),
                            ("zip", _col("truncate", provider_config=(("length", 3),))),
                        ),
                        per_group=(),
                    ),
                )
            ]
        )
        res = _polars_run(plan, {"people": src})
        assert res.outputs["people"].to_pydict() == _pandas_outputs(plan, {"people": src})["people"]

    def test_exotic_dtype_round_trip_matches_pandas(self) -> None:
        # The other parity fixtures only exercise utf8/int64/float64/bool. The
        # pa->pl->pa ingest round-trip can drift the ARROW TYPE of large_string,
        # dictionary, large_list, large_binary, and time64 (Polars 1.x widens
        # them on `from_arrow`/`to_arrow`), but it must not drift the LOGICAL
        # VALUES the pandas oracle masks or emits. This locks that: the adapter's
        # `outputs[t].to_pydict()` stays identical to a direct pandas run across
        # the drifting dtypes (Dennis S11 review, the round-trip parity gate).
        import datetime
        import decimal

        src = pa.table(
            {
                "ls": pa.array(["alpha", "beta", None], type=pa.large_string()),
                "dct": pa.array(["x", "y", "x"], type=pa.dictionary(pa.int32(), pa.string())),
                "ts": pa.array(
                    [datetime.datetime(2020, 1, 1, 12, 0)] * 3,
                    type=pa.timestamp("us", tz="America/New_York"),
                ),
                "dec": pa.array(
                    [decimal.Decimal("1.50"), decimal.Decimal("2.25"), None],
                    type=pa.decimal128(10, 2),
                ),
            }
        )
        plan = _plan(
            [
                (
                    "t",
                    TableSeed(
                        per_column=(
                            ("ls", _col("redact")),
                            ("dct", _col("passthrough")),
                            ("ts", _col("passthrough")),
                            ("dec", _col("passthrough")),
                        ),
                        per_group=(),
                    ),
                )
            ]
        )
        res = _polars_run(plan, {"t": src})
        assert res.outputs["t"].to_pydict() == _pandas_outputs(plan, {"t": src})["t"]

    def test_fpe_backend_strategy_matches_pandas(self) -> None:
        src = pa.table({"acct": [f"{i:05d}" for i in range(50)]})
        plan = _plan([("t", TableSeed(per_column=(("acct", _fpe_col()),), per_group=()))])
        res = _polars_run(plan, {"t": src})
        pandas_out = _pandas_outputs(plan, {"t": src})["t"]
        assert res.outputs["t"].to_pydict() == pandas_out
        # the FPE strategy actually masked (format-preserving digits, not source)
        assert res.outputs["t"].column("acct").to_pylist() != src.column("acct").to_pylist()

    def test_multi_table_outputs_dict(self) -> None:
        sources = {
            "a": pa.table({"x": ["p", "q"]}),
            "b": pa.table({"y": ["r", "s"]}),
        }
        plan = _plan(
            [
                ("a", TableSeed(per_column=(("x", _col("redact")),), per_group=())),
                ("b", TableSeed(per_column=(("y", _col("redact")),), per_group=())),
            ]
        )
        res = _polars_run(plan, sources)
        assert set(res.outputs) == {"a", "b"}
        pandas_out = _pandas_outputs(plan, sources)
        assert res.outputs["a"].to_pydict() == pandas_out["a"]
        assert res.outputs["b"].to_pydict() == pandas_out["b"]

    def test_boundary_metrics_recorded(self) -> None:
        src = pa.table({"a": ["x", "y"]})
        plan = _plan([("t", TableSeed(per_column=(("a", _col("redact")),), per_group=()))])
        res = _polars_run(plan, {"t": src})
        assert res.boundary_conversion_ms >= 0.0
        breakdown = res.quality_metrics["conversion_breakdown"]
        assert set(breakdown) == {
            "source_read_ms",
            "target_write_ms",
            "pa_to_pl_ms",
            "pl_to_pa_ms",
            "total_ms",
        }
        # redact is polars-native at S12 cheap-band close: it ran on the polars path
        assert res.quality_metrics["executed_substrate"] == {"redact": "polars"}

    def test_fk_job_uses_oracle_substrate(self) -> None:
        # All 11 scalar strategies are polars-native at S12 close; the remaining
        # non-native work is FK resolution (and composite bundles), which still
        # runs via the pandas oracle and reports pandas.
        plan, sources, graph = _fk_setup()
        res = PolarsExecutionAdapter().run(
            plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
        )
        assert res.quality_metrics["executed_substrate"] == {"hash": "pandas"}
        assert set(res.outputs) == {"customers", "orders"}

    def test_fallback_disabled_raises_on_fk(self) -> None:
        # FK resolution is not yet polars-native; with the migration-window
        # fallback off it hard-fails rather than silently routing through pandas
        # (PQ6 / S13 close: no silent downgrade).
        plan, sources, graph = _fk_setup()
        with pytest.raises(ExecutionError) as exc:
            PolarsExecutionAdapter(fallback_to_pandas=False).run(
                plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
            )
        assert exc.value.code == "polars_substrate_strategy_unmigrated"
        assert "fk_resolution" in str(exc.value)


class TestEndToEndRouting:
    """S12 M2: DECOY_SUBSTRATE, consumed at get_default_executor(), must route a
    FULL job through the selected substrate, not merely construct the adapter."""

    def _two_table_job(self) -> tuple[Any, dict[str, pa.Table]]:
        sources = {"a": pa.table({"x": ["p", "q"]}), "b": pa.table({"y": ["r", "s"]})}
        plan = _plan(
            [
                ("a", TableSeed(per_column=(("x", _col("redact")),), per_group=())),
                ("b", TableSeed(per_column=(("y", _col("redact")),), per_group=())),
            ]
        )
        return plan, sources

    def test_polars_substrate_routes_full_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DECOY_SUBSTRATE", "polars")
        _reset_default_executor_for_tests()
        adapter = get_default_executor()
        assert isinstance(adapter, PolarsExecutionAdapter)
        plan, sources = self._two_table_job()
        res = adapter.run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        # the full job actually ran on polars (not just constructed the adapter)
        assert res.quality_metrics["executed_substrate"] == {"redact": "polars"}
        assert res.outputs["a"].column("x").to_pylist() == ["REDACTED", "REDACTED"]

    def test_pandas_substrate_routes_full_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DECOY_SUBSTRATE", "pandas")
        _reset_default_executor_for_tests()
        adapter = get_default_executor()
        assert isinstance(adapter, PandasExecutionAdapter)
        plan, sources = self._two_table_job()
        res = adapter.run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        assert res.outputs["b"].column("y").to_pylist() == ["REDACTED", "REDACTED"]


class TestSubstrateSelector:
    def test_default_is_pandas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        _reset_default_executor_for_tests()
        assert resolve_substrate() == "pandas"
        assert isinstance(select_execution_adapter(), PandasExecutionAdapter)

    def test_polars_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DECOY_SUBSTRATE", "polars")
        assert resolve_substrate() == "polars"
        assert isinstance(select_execution_adapter(), PolarsExecutionAdapter)

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DECOY_SUBSTRATE", "Polars")
        assert isinstance(select_execution_adapter(), PolarsExecutionAdapter)

    def test_invalid_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DECOY_SUBSTRATE", "duckdb")
        with pytest.raises(ExecutionError) as exc:
            resolve_substrate()
        assert exc.value.code == "invalid_substrate"
