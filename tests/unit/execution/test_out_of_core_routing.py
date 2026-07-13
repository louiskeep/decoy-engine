"""SC2 part 2: out-of-core auto-routing + reject-before-read.

Two layers of coverage:

1. `decide_execution_route` in isolation -- the pure decision over
   already-computed signals (out-of-core compatibility, largest-table size).
   Fast, no I/O; pins the priority order (out_of_core > sequential > full_frame),
   the reject-before-read raise, and the fail-closed forced-mode overrides.

2. `run_pipeline` end to end -- proves the decision is actually DISPATCHED:
   an eligible large FK job really runs through `run_fk_out_of_core` (byte-parity
   vs the full-frame oracle), an ineligible-large FK job is REJECTED before the
   mask step with a coded error (constructed to be a job full-frame would OOM at
   scale), and an ineligible-small / below-threshold job is unchanged.

The size thresholds are `run_pipeline` kwargs (calibrated to the 32 GB box by
default so the SC5 estimator can override them); the tests lower them so a
20-row fixture exercises the same routing a 5M-row job would, without the data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_N = 20


# ---------------------------------------------------------------------------
# 1. decide_execution_route decision unit tests (fast, no I/O)
# ---------------------------------------------------------------------------


class _FakeProfile:
    def __init__(self, relationships: tuple[Any, ...]) -> None:
        self.relationships = relationships


def _acyclic_graph() -> RelationshipGraph:
    return RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("id",),
                child_table="child",
                child_columns=("parent_id",),
                namespace="ns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )


def _decide(**overrides: Any) -> tuple[str, str]:
    """decide_execution_route with pure-mask FK defaults, overridable per test.

    Pinned to `use_byte_estimate_routing=False` (the TB-5 ROLLBACK path): this
    file exercises the row-count size-gate + reject decision logic, which TB-5
    preserved behind the rollback flag. The default byte-estimate routing is
    covered in `test_byte_estimate_routing.py`.
    """
    kwargs: dict[str, Any] = {
        "has_generate_table": False,
        "has_mask_table": True,
        "validators": [],
        "fidelity_report": False,
        "vault_writer": None,
        "execution_mode": "auto",
        "graph": _acyclic_graph(),
        "resolved_substrate": "pandas",
        "out_of_core_compatible": True,
        "out_of_core_reject_code": None,
        "largest_table_rows": 1_000,
        "out_of_core_threshold_rows": 100,
        "full_frame_reject_rows": 500,
        "use_byte_estimate_routing": False,
    }
    profile = overrides.pop("profile", _FakeProfile((object(),)))
    kwargs.update(overrides)
    return decide_execution_route(profile, **kwargs)


class TestOutOfCorePriority:
    def test_eligible_compatible_large_routes_out_of_core(self) -> None:
        route, reason = _decide(largest_table_rows=1_000, out_of_core_threshold_rows=100)
        assert (route, reason) == ("out_of_core", "out_of_core_large_fk")

    def test_eligible_compatible_below_threshold_stays_sequential(self) -> None:
        # A pure-mask FK job the gate admits but too small for the route's
        # overhead: keep the existing sequential route.
        route, reason = _decide(largest_table_rows=50, out_of_core_threshold_rows=100)
        assert (route, reason) == ("sequential", "pure_mask_fk")

    def test_eligible_not_compatible_large_stays_sequential(self) -> None:
        # Sequential-eligible (pure-mask FK) but out-of-core does not support the
        # recipe (e.g. an unsupported strategy): sequential, never rejected --
        # sequential is the best bounded route it can take.
        route, reason = _decide(
            out_of_core_compatible=False,
            out_of_core_reject_code="out_of_core_strategy_unsupported",
            largest_table_rows=10_000,
        )
        assert (route, reason) == ("sequential", "pure_mask_fk")

    def test_size_unknown_never_routes_out_of_core(self) -> None:
        # Lazy-source path (no resident sources -> None): fall back to sequential.
        route, reason = _decide(largest_table_rows=None)
        assert (route, reason) == ("sequential", "pure_mask_fk")


class TestRejectBeforeReadDecision:
    def test_ineligible_large_relationship_job_rejects(self) -> None:
        # generate+mask FK: not sequential-eligible AND out-of-core cannot
        # generate -> no bounded route -> reject before read.
        with pytest.raises(ExecutionError) as exc:
            _decide(
                has_generate_table=True,
                out_of_core_compatible=False,
                out_of_core_reject_code="out_of_core_strategy_unsupported",
                largest_table_rows=10_000,
                full_frame_reject_rows=500,
            )
        assert exc.value.code == "fk_full_frame_oom_risk_rejected"
        assert "10,000" in exc.value.message

    def test_reject_fires_even_when_compat_gate_admits_structure(self) -> None:
        # THE fix: the compat gate can admit an FK STRUCTURE (hash edges) for a
        # job whose SHAPE still bars out-of-core (a generate+mask FK the route
        # cannot generate). It must still reject, not fall through to full-frame.
        with pytest.raises(ExecutionError) as exc:
            _decide(
                has_generate_table=True,  # not sequential-eligible
                out_of_core_compatible=True,  # gate admits the FK structure
                largest_table_rows=10_000,
                full_frame_reject_rows=500,
            )
        assert exc.value.code == "fk_full_frame_oom_risk_rejected"

    def test_no_relationships_large_job_never_rejects(self) -> None:
        # A large flat single table is the auto-chunk route's concern, not a
        # full-frame FK OOM: no reject here.
        route, reason = _decide(
            profile=_FakeProfile(()),
            graph=RelationshipGraph(edges=(), ordering=()),
            has_generate_table=False,
            out_of_core_compatible=False,
            largest_table_rows=10_000,
            full_frame_reject_rows=500,
        )
        assert (route, reason) == ("full_frame", "no_relationships")

    def test_size_unknown_never_rejects(self) -> None:
        route, reason = _decide(
            has_generate_table=True,
            out_of_core_compatible=False,
            largest_table_rows=None,
        )
        assert route == "full_frame"

    def test_full_frame_override_bypasses_reject(self) -> None:
        # Explicit escape hatch: the operator owns the OOM risk they requested.
        route, reason = _decide(
            execution_mode="full_frame",
            has_generate_table=True,
            out_of_core_compatible=False,
            largest_table_rows=10_000,
            full_frame_reject_rows=500,
        )
        assert (route, reason) == ("full_frame", "override_full_frame")


class TestForcedOutOfCoreMode:
    def test_forced_out_of_core_on_compatible_job(self) -> None:
        route, reason = _decide(
            execution_mode="out_of_core",
            largest_table_rows=1,  # size ignored when forced
        )
        assert (route, reason) == ("out_of_core", "override_out_of_core")

    def test_forced_out_of_core_incompatible_raises(self) -> None:
        with pytest.raises(ConfigError, match="not.*out-of-core-compatible"):
            _decide(
                execution_mode="out_of_core",
                out_of_core_compatible=False,
                out_of_core_reject_code="out_of_core_strategy_unsupported",
            )

    def test_forced_out_of_core_ineligible_raises(self) -> None:
        with pytest.raises(ConfigError, match="not out-of-core-eligible"):
            _decide(execution_mode="out_of_core", has_generate_table=True)


# ---------------------------------------------------------------------------
# 2. run_pipeline end-to-end dispatch
# ---------------------------------------------------------------------------


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _fk_ooc_config(tmp_path: Path) -> dict[str, Any]:
    """A pure-mask FK job whose every strategy is in the out-of-core supported
    set (hash keys + a redact payload), so `check_out_of_core_compatibility`
    ADMITS it -- the shape that auto-routes to out-of-core once it is large."""
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(_N)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "sc2-ooc-fk", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    _hash_col("id", "ns"),
                    {"name": "note", "strategy": "redact"},
                ],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }


def _generate_plus_mask_hash_config(tmp_path: Path) -> dict[str, Any]:
    """A generate+mask FK job with HASH keys: the compat gate admits the FK
    structure (so out_of_core_compatible is True), but the job is NOT
    sequential-eligible (a generate table) and out-of-core cannot generate --
    the reject case that must fire despite the gate admitting the structure."""
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "sc2-reject-genmask", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
            "extra": {
                "type": "file",
                "path": str(tmp_path / "extra.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {"name": "parent", "columns": [_hash_col("id", "ns")]},
            {"name": "child", "columns": [_hash_col("parent_id", "ns")]},
            {
                "name": "extra",
                "row_count": 3,
                "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _values(result_outputs: dict[str, pa.Table]) -> dict[str, dict[str, list[Any]]]:
    return {t: tbl.to_pydict() for t, tbl in result_outputs.items()}


class TestRunPipelineOutOfCoreDispatch:
    def test_auto_large_fk_job_routes_out_of_core_with_parity(self, tmp_path: Path) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)

        # Full-frame oracle.
        full = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
        # Auto with a lowered threshold so the 20-row fixture is "large". Pinned
        # to the rollback row-count path (byte-estimate routing, the TB-5
        # default, would size a 20-row job as fitting full_frame; the
        # byte-estimate out_of_core dispatch + parity is covered in
        # test_out_of_core_routing_parity.py).
        auto = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            out_of_core_threshold_rows=10,
            use_byte_estimate_routing=False,
        )

        assert auto.quality_metrics["execution"]["execution_mode"] == "out_of_core"
        assert auto.quality_metrics["execution"]["route_reason"] == "out_of_core_large_fk"
        # Really dispatched there AND byte-parity with the oracle.
        assert set(auto.outputs) == set(full.outputs) == {"parent", "child"}
        assert _values(auto.outputs) == _values(full.outputs)

    def test_below_threshold_stays_sequential(self, tmp_path: Path) -> None:
        # Rollback row-count path: below the out-of-core threshold, an
        # OOC-compatible job stays sequential (the byte-estimate default routes
        # by bytes-vs-budget, not this threshold -- see test_byte_estimate_routing).
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            out_of_core_threshold_rows=1_000,
            use_byte_estimate_routing=False,
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"

    def test_forced_out_of_core_mode_parity(self, tmp_path: Path) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        full = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
        forced = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")
        assert forced.quality_metrics["execution"]["execution_mode"] == "out_of_core"
        assert forced.quality_metrics["execution"]["route_reason"] == "override_out_of_core"
        assert _values(forced.outputs) == _values(full.outputs)

    def test_forced_out_of_core_streams_to_sink(self, tmp_path: Path) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        full = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
        sink = ParquetTransactionalSink(tmp_path / "ooc_out")
        streamed = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode="out_of_core", sink=sink
        )
        assert streamed.outputs == {}
        assert streamed.quality_metrics["execution"]["outputs_streamed"] is True
        for table in full.outputs:
            sunk = pq.read_table(tmp_path / "ooc_out" / f"{table}.parquet")
            assert sunk.to_pydict() == full.outputs[table].to_pydict(), f"{table} sink differs"


class TestRunPipelineRejectBeforeRead:
    def test_ineligible_large_fk_job_rejects_before_read(self, tmp_path: Path) -> None:
        config = _generate_plus_mask_hash_config(tmp_path)
        sources = _sources(config)
        # A sink proves "rejected before read" is not just an exception-timing
        # claim: if the job had reached any write step, the sink would have
        # created its target directory.
        sink = ParquetTransactionalSink(tmp_path / "reject_out")
        # Lower the reject threshold so the 20-row fixture is "too big to
        # full-frame safely"; this stands in for a real 8M+-row FK job that
        # would OOM full-frame on a 32 GB box.
        with pytest.raises(ExecutionError) as exc:
            run_pipeline(
                config, sources, engine_version="0.1.0", full_frame_reject_rows=10, sink=sink
            )
        assert exc.value.code == "fk_full_frame_oom_risk_rejected"
        # The generate table is NOT produced: rejected BEFORE the mask/generate
        # step, not after (no partial work).
        assert "not sequential-eligible" in exc.value.message
        assert not (tmp_path / "reject_out").exists()

    def test_full_frame_override_runs_instead_of_rejecting(self, tmp_path: Path) -> None:
        config = _generate_plus_mask_hash_config(tmp_path)
        sources = _sources(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            full_frame_reject_rows=10,
            execution_mode="full_frame",
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert result.outputs["extra"].num_rows == 3

    def test_ineligible_small_fk_job_runs_full_frame_unchanged(self, tmp_path: Path) -> None:
        # Below the reject threshold (default 32 GB-box constant): the ineligible
        # FK job runs full-frame exactly as before SC2.
        config = _generate_plus_mask_hash_config(tmp_path)
        sources = _sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0")
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert result.outputs["extra"].num_rows == 3
