"""Task 4.5 D3/D6: the unified-slice admission predicate + activation
overlay, unit-tested directly against the private helpers (no full
`run_pipeline` round-trip needed for these -- the differential harness in
`test_unified_slice_parity.py` covers the end-to-end behavior).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from decoy_engine.execution import _unified_slice_admission
from decoy_engine.execution.physical._activation import build_unified_slice_activation
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._live_inputs import build_live_physical_plan_inputs
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.plan import compile_plan
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships import RelationshipGraph
from tests.physical._shadow_helpers import build_config, write_read_only_fixture

ENGINE_VERSION = "unified-slice-admission-test"


def _build(tmp_path: Path, columns: list[dict], source: pa.Table) -> tuple[dict, pa.Table]:
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, columns)
    return config, source


def _profile_and_plan(config: dict, source: pa.Table):
    profile = profile_source(config, seed=20260914)
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION)
    return profile, plan


def _cheap_ok(config, profile, source, **overrides):
    kwargs = dict(
        route="full_frame",
        route_chunked=False,
        native_route_enabled=False,
        resolved_substrate="pandas",
        sink=None,
        source_loader=None,
        fidelity_report=False,
        vault_writer=None,
        config=config,
        profile=profile,
        table_kinds={"t": "mask"},
        caller_sources={"t": source},
    )
    kwargs.update(overrides)
    return _unified_slice_admission.cheap_admission(**kwargs)


def test_cheap_admission_admits_the_golden_shape(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    candidate = _cheap_ok(config, profile, source)
    assert candidate is not None
    assert candidate.table == "t"


def test_cheap_admission_declines_non_full_frame_route(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, route="sequential") is None


def test_cheap_admission_declines_when_chunked(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, route_chunked=True) is None


def test_cheap_admission_declines_when_native_route_enabled(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, native_route_enabled=True) is None


def test_cheap_admission_declines_non_pandas_substrate(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    # The lane returns a result identical to the PANDAS full-frame route only.
    # A polars-substrate job runs a different legacy adapter that stamps its own
    # provenance telemetry (executed_substrate, pa<->pl conversion timings), so
    # admitting it would diverge on the caller-consumed quality_metrics. It must
    # fall through to the unchanged old route.
    assert _cheap_ok(config, profile, source, resolved_substrate="polars") is None


def test_cheap_admission_declines_sink_present(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, sink=object()) is None


def test_cheap_admission_declines_source_loader_present(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, source_loader=lambda name: source) is None


def test_cheap_admission_declines_fidelity_report(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, fidelity_report=True) is None


def test_cheap_admission_declines_vault_writer(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, vault_writer=object()) is None


def test_cheap_admission_declines_validators(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    config = dict(config)
    config["validators"] = [{"name": "x"}]
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_relationships(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)

    class _FakeProfile:
        relationships = ({"fake": True},)

    assert _cheap_ok(config, _FakeProfile(), source) is None


def test_cheap_admission_declines_generate_table_present(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, table_kinds={"t": "mask", "g": "generate"}) is None


def test_cheap_admission_declines_non_resident_source(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, caller_sources={}) is None


def test_cheap_admission_declines_transforms_configured(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    config = dict(config)
    config["tables"] = [dict(config["tables"][0], transforms=[{"op": "filter_rows"}])]
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_undeclared_passthrough_column(tmp_path: Path) -> None:
    """D3: configured columns must equal the source schema EXACTLY -- an
    extra source column the config never declares must decline, not admit
    with a silent passthrough."""
    source = pa.table(
        {
            "c": pa.array(["a", "b", "c"], type=pa.string()),
            "extra": pa.array(["x", "y", "z"], type=pa.string()),
        }
    )
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_duplicate_column_declaration(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    columns = [
        {"name": "c", "strategy": "passthrough"},
        {"name": "c", "strategy": "redact"},
    ]
    path = write_read_only_fixture(tmp_path, source, "fixture")
    # build_config's PipelineConfig validation may itself reject a duplicate
    # column name; construct the raw dict directly to exercise the admission
    # check in isolation regardless.
    from decoy_engine.config import PipelineConfig

    raw = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
        "targets": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")}
        },
        "tables": [{"name": "t", "columns": [{"name": "c", "strategy": "passthrough"}]}],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    # Mutate post-validation to inject the duplicate the schema itself would
    # reject at submit time -- proving the admission check is a real,
    # independent guard, not merely inherited from schema validation.
    config["tables"][0]["columns"] = columns
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_vault_column(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(
        tmp_path, [{"name": "c", "strategy": "hash", "namespace": "n", "vault": True}], source
    )
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_quarantine_configured(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    config = dict(config)
    config["quarantine"] = {"enabled": True, "triggers": ["validation_fail"]}
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_run_storm(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    config = dict(config)
    config["run_storm"] = True
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


# ---------------------------------------------------------------------------
# Compiled-plan-level admission (needs a real compiled PhysicalPlan).
# ---------------------------------------------------------------------------


def _compile(config: dict, profile, plan, source: pa.Table):
    registry = get_default_registry()
    inputs = build_live_physical_plan_inputs(
        config=config,
        plan=plan,
        profile=profile,
        registry=registry,
        graph=RelationshipGraph(edges=(), ordering=()),
        table_kinds={"t": "mask"},
        caller_sources={"t": source},
        resolved_substrate="pandas",
        execution_mode="auto",
        fidelity_report=False,
        vault_writer_present=False,
        validators=(),
        auto_chunk=True,
        chunk_size_rows=50_000,
        auto_chunk_threshold_rows=100_000,
        out_of_core_threshold_rows=5_000_000,
        full_frame_reject_rows=7_500_000,
        use_byte_estimate_routing=True,
        use_probe_routing=True,
        native_route_enabled=False,
        fpe_chunk_count=4,
        max_workers=4,
        fallback_to_pandas=True,
        out_of_core_reorder_threshold_rows=None,
        out_of_core_budget_bytes=None,
        engine_version=ENGINE_VERSION,
    )
    return compile_physical_plan(inputs), registry


def test_compiled_plan_admission_admits_the_golden_shape(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)
    admitted = _unified_slice_admission.compiled_plan_admission(
        physical_plan, table="t", source=source
    )
    assert admitted is not None
    assert admitted.driver == DriverId.FULL_FRAME
    assert len(admitted.nodes) == 1
    assert admitted.nodes[0].execution is not None


def test_compiled_plan_admission_declines_an_unsupported_strategy(tmp_path: Path) -> None:
    """`fpe` has no entry in the four-operator allowlist, so its node
    compiles with `execution is None` -- the compiled-plan check must
    decline, not admit a partial slice."""
    source = pa.table({"c": pa.array(["12345", "23456", "34567"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "fpe"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)
    assert (
        _unified_slice_admission.compiled_plan_admission(physical_plan, table="t", source=source)
        is None
    )


def test_null_bearing_int_declines_admission(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([1, None, 3], type=pa.int64())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "hash", "namespace": "n"}], source)
    profile, plan = _profile_and_plan(config, source)
    registry = get_default_registry()
    ok = _unified_slice_admission.keyed_hash_and_null_int_admission(
        _compile(config, profile, plan, source)[0].tables[0],
        plan=plan,
        source=source,
        registry=registry,
        graph=RelationshipGraph(edges=(), ordering=()),
        table="t",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# D6: the activation overlay hash.
# ---------------------------------------------------------------------------


def test_activation_hash_is_deterministic(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)

    a = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=True
    )
    b = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=True
    )
    assert a.activation_hash == b.activation_hash
    assert a.admitted_nodes == b.admitted_nodes


def test_activation_hash_includes_the_flag(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)

    on = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=True
    )
    off = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=False
    )
    assert on.activation_hash != off.activation_hash
