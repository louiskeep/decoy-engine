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
    resolve_substrate,
    select_execution_adapter,
)
from decoy_engine.execution.polars import (
    ConversionBoundary,
    read_source_polars,
    write_target_polars,
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

    def test_supports_no_strategy_at_s11_close(self) -> None:
        adapter = PolarsExecutionAdapter()
        for strategy in ("redact", "truncate", "passthrough", "fpe", "hash", "anything"):
            assert adapter.supports_strategy(strategy) is False

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
        # every strategy fell back to pandas at S11 close
        assert res.quality_metrics["executed_substrate"] == {"redact": "pandas"}

    def test_fallback_disabled_raises_on_unmigrated(self) -> None:
        src = pa.table({"a": ["x"]})
        plan = _plan([("t", TableSeed(per_column=(("a", _col("redact")),), per_group=()))])
        with pytest.raises(ExecutionError) as exc:
            _polars_run(plan, {"t": src}, fallback_to_pandas=False)
        assert exc.value.code == "polars_substrate_strategy_unmigrated"


class TestSubstrateSelector:
    def test_default_is_pandas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
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
