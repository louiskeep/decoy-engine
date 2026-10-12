"""Phase D / D2-cycle: how an admitted cross-table FK cycle reaches the SEQ route.

The decision function takes the cycle gate's verdict as an input, so these tests pin what each
verdict does to every routing branch, then drive `run_pipeline` end to end on a hash-keyed cycle
to prove the gate, the dispatch and the key pass are wired together.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.profile._readers import LazySource
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_ROWS = 7_500_000


class _Profile:
    def __init__(self) -> None:
        self.relationships = (object(),)
        self.tables = ()


def _cyclic_graph() -> RelationshipGraph:
    def edge(parent: str, child: str, column: str) -> RelationshipEdge:
        return RelationshipEdge(
            parent_table=parent,
            parent_columns=("id",),
            child_table=child,
            child_columns=(column,),
            namespace="n",
            orphan_policy=OrphanPolicy.PRESERVE,
        )

    return RelationshipGraph(edges=(edge("a", "b", "ref_a"), edge("b", "a", "ref_b")), ordering=())


def _route(**kwargs: Any) -> tuple[str, str]:
    base: dict[str, Any] = dict(
        has_generate_table=False,
        has_mask_table=True,
        validators=[],
        fidelity_report=False,
        vault_writer=None,
        execution_mode="auto",
        graph=_cyclic_graph(),
        out_of_core_compatible=False,
        largest_table_rows=10,
        use_byte_estimate_routing=False,
    )
    base.update(kwargs)
    return decide_execution_route(_Profile(), **base)


# The decision function.


def test_a_declined_cycle_keeps_todays_routing() -> None:
    assert _route() == ("full_frame", "cross_table_cycle")
    with pytest.raises(ExecutionError) as caught:
        _route(largest_table_rows=_ROWS, full_frame_reject_rows=_ROWS)
    assert caught.value.code == "fk_full_frame_oom_risk_rejected"


def test_an_admitted_small_cycle_stays_on_full_frame_on_the_row_count_path() -> None:
    assert _route(cycle_admitted=True) == ("full_frame", "cross_table_cycle")


def test_an_admitted_large_cycle_runs_bounded_instead_of_being_rejected() -> None:
    assert _route(cycle_admitted=True, largest_table_rows=_ROWS, full_frame_reject_rows=_ROWS) == (
        "sequential",
        "pure_mask_fk",
    )


def test_an_admitted_cycle_goes_sequential_when_the_estimate_does_not_fit() -> None:
    for fits in (False, None):
        assert _route(
            cycle_admitted=True, use_byte_estimate_routing=True, full_frame_fits_estimate=fits
        ) == ("sequential", "pure_mask_fk")


def test_an_admitted_cycle_stays_full_frame_when_the_estimate_confirms_a_fit() -> None:
    assert _route(
        cycle_admitted=True, use_byte_estimate_routing=True, full_frame_fits_estimate=True
    ) == ("full_frame", "byte_estimate_full_frame_fits")


def test_a_declined_cycle_still_rejects_when_the_estimate_does_not_fit() -> None:
    with pytest.raises(ExecutionError) as caught:
        _route(use_byte_estimate_routing=True, full_frame_fits_estimate=False)
    assert caught.value.code == "fk_full_frame_oom_risk_rejected"


def test_out_of_core_never_takes_a_cycle_even_when_admitted_and_large() -> None:
    route, _ = _route(
        cycle_admitted=True,
        out_of_core_compatible=True,
        largest_table_rows=10 * _ROWS,
        use_byte_estimate_routing=True,
        full_frame_fits_estimate=False,
    )
    assert route == "sequential"
    route, _ = _route(
        cycle_admitted=True, out_of_core_compatible=True, largest_table_rows=10 * _ROWS
    )
    assert route != "out_of_core"


def test_explicit_sequential_follows_the_gate() -> None:
    assert _route(execution_mode="sequential", cycle_admitted=True) == (
        "sequential",
        "pure_mask_fk",
    )
    with pytest.raises(ConfigError) as caught:
        _route(execution_mode="sequential", cycle_decline_code="sequential_cycle_x")
    text = str(caught.value)
    assert "cross-table cycle" in text and "sequential_cycle_x" in text


def test_the_gate_does_not_change_an_acyclic_job() -> None:
    from tests.parity._d2b_shapes import graph as shape_graph

    acyclic = shape_graph(
        RelationshipEdge(
            parent_table="p",
            parent_columns=("id",),
            child_table="c",
            child_columns=("pid",),
            namespace="n",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
    )
    assert _route(graph=acyclic) == _route(graph=acyclic, cycle_admitted=True)
    assert _route(graph=acyclic)[0] == "sequential"


# End to end through run_pipeline.


def _cycle_config(tmp_path: Path, *, n: int = 6, strategy: str = "hash") -> dict[str, Any]:
    ids = [f"k{i:03d}" for i in range(n)]
    a = pa.table(
        {
            "id": pa.array(ids, pa.string()),
            "bid": pa.array([f"m{i:03d}" for i in range(n)], pa.string()),
            "pay": pa.array([f"payload-{i}" for i in range(n)], pa.string()),
        }
    )
    b = pa.table(
        {
            "id": pa.array([f"m{i:03d}" for i in range(n)], pa.string()),
            "aid": pa.array(ids, pa.string()),
        }
    )
    for name, table in (("a", a), ("b", b)):
        pq.write_table(table, tmp_path / f"{name}.parquet")

    def key(name: str, ns: str) -> dict[str, Any]:
        if strategy == "faker":
            return {
                "name": name,
                "strategy": "faker",
                "provider": "person_email",
                "deterministic": True,
                "namespace": ns,
            }
        return {"name": name, "strategy": strategy, "namespace": ns}

    return {
        "version": 1,
        "global_settings": {"job_name": "d2-cycle", "seed": 9},
        "sources": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.parquet"), "format": "parquet"}
            for t in ("a", "b")
        },
        "targets": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.out.parquet"), "format": "parquet"}
            for t in ("a", "b")
        },
        "tables": [
            {
                "name": "a",
                "columns": [
                    key("id", "na"),
                    key("bid", "nb"),
                    {"name": "pay", "strategy": "redact"},
                ],
            },
            {"name": "b", "columns": [key("id", "nb"), key("aid", "na")]},
        ],
        "relationships": [
            {
                "parent": {"table": "a", "columns": ["id"]},
                "children": [{"table": "b", "columns": ["aid"]}],
                "orphan_policy": "preserve",
                "namespace": "na",
            },
            {
                "parent": {"table": "b", "columns": ["id"]},
                "children": [{"table": "a", "columns": ["bid"]}],
                "orphan_policy": "preserve",
                "namespace": "nb",
            },
        ],
    }


def _tables(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _lazy(config: dict[str, Any]) -> dict[str, LazySource]:
    return {name: LazySource(Path(spec["path"])) for name, spec in config["sources"].items()}


def _assert_same_outputs(left: Any, right: Any) -> None:
    assert set(left) == set(right)
    for name in left:
        assert left[name].equals(right[name]), name


def test_run_pipeline_runs_an_admitted_cycle_on_the_sequential_route(tmp_path: Path) -> None:
    config = _cycle_config(tmp_path)
    oracle = run_pipeline(
        config, _tables(config), engine_version="t", execution_mode="full_frame"
    ).outputs
    result = run_pipeline(config, _tables(config), engine_version="t", execution_mode="sequential")
    execution = result.quality_metrics["execution"]
    assert execution["execution_mode"] == "sequential"
    assert result.quality_metrics["sequential_cycle"] == {"components": [["a", "b"]]}
    _assert_same_outputs(result.outputs, oracle)


def test_auto_routes_a_large_admitted_cycle_sequential_instead_of_rejecting(
    tmp_path: Path,
) -> None:
    config = _cycle_config(tmp_path)
    oracle = run_pipeline(
        config, _tables(config), engine_version="t", execution_mode="full_frame"
    ).outputs
    result = run_pipeline(
        config,
        _tables(config),
        engine_version="t",
        use_byte_estimate_routing=False,
        full_frame_reject_rows=1,
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
    _assert_same_outputs(result.outputs, oracle)


def test_a_lazy_cycle_is_read_by_column_first_and_streams_to_a_transactional_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _cycle_config(tmp_path)
    oracle = run_pipeline(
        config, _tables(config), engine_version="t", execution_mode="full_frame"
    ).outputs
    reads: list[tuple[str, tuple[str, ...]]] = []
    real = LazySource.read_columns

    def spy(self: LazySource, columns: Any) -> pa.Table:
        reads.append((self.path.name, tuple(columns)))
        return real(self, columns)

    monkeypatch.setattr(LazySource, "read_columns", spy)
    sink = ParquetTransactionalSink(tmp_path / "out")
    result = run_pipeline(
        config, _lazy(config), engine_version="t", execution_mode="sequential", sink=sink
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
    assert sorted(reads) == [("a.parquet", ("bid", "id")), ("b.parquet", ("aid", "id"))]
    written = {t: pq.read_table(tmp_path / "out" / f"{t}.parquet") for t in ("a", "b")}
    _assert_same_outputs(written, oracle)


def test_a_declined_cycle_names_its_reason_to_an_explicit_sequential_request(
    tmp_path: Path,
) -> None:
    config = _cycle_config(tmp_path, strategy="faker")
    with pytest.raises(ConfigError) as caught:
        run_pipeline(config, _tables(config), engine_version="t", execution_mode="sequential")
    text = str(caught.value)
    assert "cross-table cycle" in text
    assert "sequential_cycle_key_strategy_unsupported" in text
    for leaked in ("k000", "m000", "payload-0"):
        assert leaked not in text


def test_a_declined_cycle_still_runs_on_full_frame_under_auto(tmp_path: Path) -> None:
    config = _cycle_config(tmp_path, strategy="faker")
    result = run_pipeline(
        config, _tables(config), engine_version="t", use_byte_estimate_routing=False
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert result.quality_metrics["execution"]["route_reason"] == "cross_table_cycle"


def test_quarantine_triggers_send_the_cycle_back_to_full_frame(tmp_path: Path) -> None:
    config = _cycle_config(tmp_path)
    config["quarantine"] = {
        "enabled": True,
        "triggers": ["mask_error"],
        "output_path": str(tmp_path / "q.jsonl"),
    }
    with pytest.raises(ConfigError) as caught:
        run_pipeline(config, _tables(config), engine_version="t", execution_mode="sequential")
    assert "sequential_cycle_quarantine_enabled" in str(caught.value)


# The transforms preflight and routing agree about a cycle that shares a job with a
# transform-bearing acyclic table.


def _with_transform_table(config: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    pq.write_table(
        pa.table({"x": pa.array([f"x{i}" for i in range(8)], pa.string())}), tmp_path / "d.parquet"
    )
    config["sources"]["d"] = {
        "type": "file",
        "path": str(tmp_path / "d.parquet"),
        "format": "parquet",
    }
    config["targets"]["d"] = {
        "type": "file",
        "path": str(tmp_path / "d.out.parquet"),
        "format": "parquet",
    }
    config["tables"].append(
        {
            "name": "d",
            "transforms": [{"op": "limit", "n": 5}],
            "columns": [{"name": "x", "strategy": "redact"}],
        }
    )
    return config


def test_preflight_and_routing_admit_a_cycle_beside_a_transform_bearing_table(
    tmp_path: Path,
) -> None:
    config = _with_transform_table(_cycle_config(tmp_path), tmp_path)
    resident = run_pipeline(
        config, _tables(config), engine_version="t", execution_mode="sequential"
    )
    assert resident.quality_metrics["execution"]["execution_mode"] == "sequential"
    assert resident.outputs["d"].num_rows == 5
    lazy = {**_tables(config), "d": LazySource(tmp_path / "d.parquet")}
    auto = run_pipeline(
        config,
        lazy,
        engine_version="t",
        use_byte_estimate_routing=False,
        full_frame_reject_rows=1,
    )
    assert auto.quality_metrics["execution"]["execution_mode"] == "sequential"


def test_preflight_and_routing_both_reject_a_declined_cycle_beside_a_transform_table(
    tmp_path: Path,
) -> None:
    config = _with_transform_table(_cycle_config(tmp_path, strategy="faker"), tmp_path)
    with pytest.raises(ConfigError) as forced:
        run_pipeline(config, _tables(config), engine_version="t", execution_mode="sequential")
    assert "sequential_cycle_key_strategy_unsupported" in str(forced.value)
    lazy = {**_tables(config), "d": LazySource(tmp_path / "d.parquet")}
    with pytest.raises(ExecutionError) as auto:
        run_pipeline(
            config,
            lazy,
            engine_version="t",
            use_byte_estimate_routing=False,
            full_frame_reject_rows=1,
        )
    assert auto.value.code == "fk_full_frame_oom_risk_rejected"


def test_a_key_column_missing_from_the_source_is_a_coded_decline(tmp_path: Path) -> None:
    config = _cycle_config(tmp_path)
    pq.write_table(pq.read_table(tmp_path / "a.parquet").drop(["bid"]), tmp_path / "a.parquet")
    with pytest.raises(ExecutionError) as caught:
        run_pipeline(
            config,
            _lazy(config),
            engine_version="t",
            execution_mode="sequential",
        )
    assert caught.value.code == "sequential_cycle_projected_read_unavailable"
    assert str(tmp_path) not in str(caught.value)
