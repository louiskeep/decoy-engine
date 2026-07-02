"""P2 (job-performance sprints): observe-only execution-mode planner.

`classify_job` classifies a job into exactly one execution mode
(`polars_native` / `chunked` / `sequential_relationship` /
`out_of_core_relationship` / `pandas_fallback`) and records a rejection
reason for every faster mode not chosen. P2 is OBSERVE-ONLY: the planner
never changes routing, and the default `run_pipeline` call stays
byte-identical (the P1 golden `quality_metrics == {}` contract).

The relationship modes are honest here: main does not carry the FK stack
(`_sequential.py` / `out_of_core/` live on the FK branches), so an FK job
is marked a relationship-route CANDIDATE with a DEFERRED reason rather
than pretending sequential-vs-out-of-core compatibility was evaluated.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._planner import (
    EXECUTION_MODES,
    RELATIONSHIP_ROUTE_DEFERRED,
    ExecutionPlan,
    classify_job,
)

_ENGINE_VERSION = "p2-planner-test"


def _validated_dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _write_csv(tmp_path, name: str, df: pd.DataFrame) -> str:
    path = tmp_path / f"{name}.csv"
    df.to_csv(path, index=False)
    return str(path)


def _base_config(tmp_path, tables: list[dict], sources: dict[str, str]) -> dict:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                name: {"type": "file", "format": "csv", "path": path}
                for name, path in sources.items()
            },
            "tables": tables,
            "targets": {
                t["name"]: {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / f"{t['name']}_out.csv"),
                }
                for t in tables
            },
        }
    )


def _scalar_chunk_safe_job(tmp_path) -> tuple[dict, dict[str, pa.Table]]:
    """Scalar no-FK job of chunk-safe, polars-native strategies."""
    df = pd.DataFrame({"email": ["a@x.com", "b@x.com"], "zip": ["90210", "10001"]})
    path = _write_csv(tmp_path, "customers", df)
    cfg = _base_config(
        tmp_path,
        tables=[
            {
                "name": "customers",
                "columns": [
                    {"name": "email", "strategy": "hash", "namespace": "email_ns"},
                    {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
                ],
            }
        ],
        sources={"customers": path},
    )
    return cfg, {"customers": pa.Table.from_pandas(df, preserve_index=False)}


def _shuffle_job(tmp_path) -> tuple[dict, dict[str, pa.Table]]:
    """shuffle is polars-native but NOT chunk-safe (whole-column permutation)."""
    df = pd.DataFrame({"val": ["a", "b", "c"]})
    path = _write_csv(tmp_path, "t", df)
    cfg = _base_config(
        tmp_path,
        tables=[{"name": "t", "columns": [{"name": "val", "strategy": "shuffle"}]}],
        sources={"t": path},
    )
    return cfg, {"t": pa.Table.from_pandas(df, preserve_index=False)}


def _composite_job(tmp_path) -> tuple[dict, dict[str, pa.Table]]:
    df = pd.DataFrame(
        {"first_name": ["Ann", "Bob"], "last_name": ["Lee", "Kim"], "email": ["a@x", "b@x"]}
    )
    path = _write_csv(tmp_path, "people", df)

    def col(name: str, others: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "strategy": "faker",
            "provider": "composite_name_email",
            "deterministic": True,
            "namespace": "ne",
            "coherent_with": others,
        }

    cfg = _base_config(
        tmp_path,
        tables=[
            {
                "name": "people",
                "columns": [
                    col("first_name", ["last_name", "email"]),
                    col("last_name", ["first_name", "email"]),
                    col("email", ["first_name", "last_name"]),
                ],
            }
        ],
        sources={"people": path},
    )
    return cfg, {"people": pa.Table.from_pandas(df, preserve_index=False)}


def _mixed_generate_job(tmp_path) -> tuple[dict, dict[str, pa.Table]]:
    df = pd.DataFrame({"val": ["a", "b"]})
    path = _write_csv(tmp_path, "t", df)
    cfg = _base_config(
        tmp_path,
        tables=[
            {"name": "t", "columns": [{"name": "val", "strategy": "hash", "namespace": "ns"}]},
            {
                "name": "g",
                "row_count": 3,
                "generate_columns": [{"name": "id", "type": "sequence", "start": 1}],
            },
        ],
        sources={"t": path},
    )
    return cfg, {"t": pa.Table.from_pandas(df, preserve_index=False)}


def _fk_job(tmp_path) -> tuple[dict, dict[str, pa.Table]]:
    customers = pd.DataFrame({"id": ["C1", "C2"]})
    orders = pd.DataFrame({"customer_id": ["C1", "C1"]})
    cpath = _write_csv(tmp_path, "customers", customers)
    opath = _write_csv(tmp_path, "orders", orders)
    cfg = _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {"type": "file", "format": "csv", "path": cpath},
                "orders": {"type": "file", "format": "csv", "path": opath},
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "id_ns"}],
                },
                {
                    "name": "orders",
                    "columns": [{"name": "customer_id", "strategy": "hash", "namespace": "id_ns"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "id_ns",
                }
            ],
            "targets": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers_out.csv"),
                },
                "orders": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "orders_out.csv"),
                },
            },
        }
    )
    return cfg, {
        "customers": pa.Table.from_pandas(customers, preserve_index=False),
        "orders": pa.Table.from_pandas(orders, preserve_index=False),
    }


def _planner_inputs(cfg: dict, *, substrate: str) -> dict[str, Any]:
    """Build classify_job's inputs the same way run_pipeline does (steps
    2-4 of its sequencing contract), without executing any masking."""
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    profile = profile_source(cfg, seed=_normalize_job_seed_int(cfg))
    plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
    ns_registry = build_namespace_registry(cfg, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(cfg, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships, namespace_registry=ns_registry, orphan_policy_lookup=lookup
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    return {
        "plan": plan,
        "registry": get_default_registry(),
        "relationship_graph": graph,
        "substrate": substrate,
    }


def _classify(cfg: dict, *, substrate: str) -> ExecutionPlan:
    return classify_job(cfg, **_planner_inputs(cfg, substrate=substrate))


def _explain(cfg, sources, *, monkeypatch, **kwargs) -> dict[str, Any]:
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    result = run_pipeline(
        cfg,
        sources=sources,
        engine_version=_ENGINE_VERSION,
        explain_plan=True,
        **kwargs,
    )
    block = result.quality_metrics["execution_plan"]
    assert isinstance(block, dict)
    return block


# --------------------------------------------------------------------------
# Mode classification: the doc's required cases with rejection reasons
# --------------------------------------------------------------------------


class TestModeClassification:
    def test_scalar_no_fk_polars_admissible_is_polars_native(self, tmp_path, monkeypatch):
        cfg, sources = _scalar_chunk_safe_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch, substrate="polars")
        assert block["mode"] == "polars_native"
        # polars_native is the fastest mode: nothing faster was rejected.
        assert block["rejections"] == {}
        assert block["reason"]

    def test_chunk_safe_single_table_on_pandas_is_chunked(self, tmp_path, monkeypatch):
        """With the substrate pinned pandas, polars_native is rejected for
        the substrate pin and the chunk-safe single-table job classifies
        chunked (P3 will route it; P2 only observes)."""
        cfg, sources = _scalar_chunk_safe_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch)
        assert block["mode"] == "chunked"
        assert set(block["rejections"]) == {"polars_native"}
        assert "pandas" in block["rejections"]["polars_native"]

    def test_non_chunk_safe_strategy_rejects_chunked_naming_strategy(self, tmp_path, monkeypatch):
        cfg, sources = _shuffle_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch)
        assert block["mode"] == "pandas_fallback"
        assert "shuffle" in block["rejections"]["chunked"]
        assert "strategy_not_chunk_safe" in block["rejections"]["chunked"]

    def test_composite_bundle_rejects_polars_native_with_composite_reason(
        self, tmp_path, monkeypatch
    ):
        cfg, sources = _composite_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch, substrate="polars")
        assert block["mode"] == "pandas_fallback"
        assert "composite" in block["rejections"]["polars_native"]
        assert block["rejections"]["chunked"]

    def test_generate_table_rejects_chunked_with_generation_reason(self, tmp_path, monkeypatch):
        cfg, sources = _mixed_generate_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch)
        assert block["mode"] == "pandas_fallback"
        assert "g" in block["rejections"]["chunked"]
        assert "generat" in block["rejections"]["chunked"]  # generate/generation

    def test_fk_edges_mark_relationship_routes_deferred(self, tmp_path, monkeypatch):
        cfg, sources = _fk_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch)
        assert block["mode"] == "pandas_fallback"
        assert block["rejections"]["sequential_relationship"] == RELATIONSHIP_ROUTE_DEFERRED
        assert block["rejections"]["out_of_core_relationship"] == RELATIONSHIP_ROUTE_DEFERRED
        assert "DEFERRED" in RELATIONSHIP_ROUTE_DEFERRED
        assert "FK stack" in RELATIONSHIP_ROUTE_DEFERRED
        # The chosen-mode reason declares the job a relationship-route candidate.
        assert "relationship-route candidate" in block["reason"]
        # FK also rejects the non-relationship fast modes.
        assert "fk" in block["rejections"]["polars_native"].lower()
        assert "relationship" in block["rejections"]["chunked"].lower()

    def test_pandas_fallback_populates_every_faster_mode_rejection(self, tmp_path, monkeypatch):
        cfg, sources = _shuffle_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch)
        assert block["mode"] == "pandas_fallback"
        faster = [m for m in EXECUTION_MODES if m != "pandas_fallback"]
        assert list(block["rejections"]) == faster
        assert all(isinstance(r, str) and r for r in block["rejections"].values())

    def test_pure_generate_job_falls_back_with_synthesize_reason(self, tmp_path, monkeypatch):
        cfg = _validated_dump(
            {
                "version": 1,
                "global_settings": {"seed": 42},
                "sources": {},
                "tables": [
                    {
                        "name": "g",
                        "row_count": 3,
                        "generate_columns": [{"name": "id", "type": "sequence", "start": 1}],
                    }
                ],
                "targets": {
                    "g": {"type": "file", "format": "csv", "path": str(tmp_path / "g_out.csv")}
                },
            }
        )
        block = _explain(cfg, None, monkeypatch=monkeypatch, substrate="polars")
        assert block["mode"] == "pandas_fallback"
        assert "mask" in block["rejections"]["polars_native"]
        assert block["rejections"]["chunked"]


# --------------------------------------------------------------------------
# Determinism and result-type invariants
# --------------------------------------------------------------------------


class TestDeterminismAndResultType:
    def test_same_job_classifies_identically_twice(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, _ = _fk_job(tmp_path)
        inputs = _planner_inputs(cfg, substrate="pandas")
        first = classify_job(cfg, **inputs)
        second = classify_job(cfg, **inputs)
        assert first == second
        assert list(first.rejections) == list(second.rejections)

    def test_rebuilt_inputs_classify_identically(self, tmp_path, monkeypatch):
        """Determinism holds across independently rebuilt planner inputs
        (fresh profile/plan/graph), not just object reuse."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, _ = _shuffle_job(tmp_path)
        assert _classify(cfg, substrate="pandas") == _classify(cfg, substrate="pandas")

    def test_execution_plan_is_frozen(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, _ = _scalar_chunk_safe_job(tmp_path)
        plan = _classify(cfg, substrate="pandas")
        with pytest.raises(AttributeError):
            plan.mode = "polars_native"  # type: ignore[misc]
        with pytest.raises(TypeError):
            plan.rejections["polars_native"] = "mutated"  # type: ignore[index]

    def test_execution_plan_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            ExecutionPlan(mode="duckdb_native", rejections={}, reason="nope")


# --------------------------------------------------------------------------
# Observe-only: default runs stamp nothing and stay byte-identical
# --------------------------------------------------------------------------


class TestObserveOnly:
    def test_explain_plan_stamps_execution_plan_block(self, tmp_path, monkeypatch):
        cfg, sources = _scalar_chunk_safe_job(tmp_path)
        block = _explain(cfg, sources, monkeypatch=monkeypatch)
        assert set(block) == {"mode", "reason", "rejections"}
        assert block["mode"] in EXECUTION_MODES

    def test_default_run_stamps_nothing_and_output_is_byte_identical(self, tmp_path, monkeypatch):
        """The P1 golden holds: a default run carries quality_metrics == {}
        and its output bytes match the explain run exactly (the planner
        only ever adds the metrics block, never touches data)."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = _scalar_chunk_safe_job(tmp_path)
        default_result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        explain_result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, explain_plan=True
        )
        assert default_result.quality_metrics == {}
        assert set(explain_result.quality_metrics) == {"execution_plan"}
        assert default_result.outputs["customers"].equals(explain_result.outputs["customers"])

    def test_explain_plan_never_changes_routing(self, tmp_path, monkeypatch):
        """P2 is observe-only: the same adapter runs with and without the
        flag (chunked/polars routing is P3+, behind the routing seam)."""
        from decoy_engine.execution import _substrate

        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = _scalar_chunk_safe_job(tmp_path)
        adapters: list[Any] = []
        real = _substrate.select_execution_adapter

        def spy(**kwargs):
            adapter = real(**kwargs)
            adapters.append(type(adapter).__name__)
            return adapter

        monkeypatch.setattr(_substrate, "select_execution_adapter", spy)
        run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, explain_plan=True)
        assert adapters == ["PandasExecutionAdapter", "PandasExecutionAdapter"]
