"""Task 4.6 slice 3: OOC FK route parity through the unified coordinator, in
the SHADOW test path (docs/plans/2026-09-16-task46-slice3-ooc-fk-shadow-
plan.md). Proves the compiler assigns `DriverId.OUT_OF_CORE` for a compiled
FK job and the `ShadowCoordinator` faithfully dispatches that plan through
the existing Task 4.2 `OutOfCoreAdapter` (which delegates to
`run_fk_out_of_core`) -- forwarding the seed-plan, sources, registry,
relationship graph, resolved key material, and batch budget -- producing
output cell-for-cell equal to the pandas full_frame oracle.

DOES NOT re-prove `run_fk_out_of_core`'s own FK parity: `tests/parity/
test_out_of_core_fk_parity.py` already pins that (admitted single-edge,
chain, fan-out, composite, and failure cases). This file rides on that and
adds the compile+dispatch+adaptation link -- the FK machinery stays
single-owner in `execution/_runner.py` + `execution/out_of_core/`; nothing
here reimplements it.

SHADOW-ONLY, like every file in this package: no production caller, no
default flip. `run_shadow_and_oracle` runs the compiler + `ShadowCoordinator`
directly and never touches `cheap_admission` / `resident_contract_admission`.

`_diag_key` (`_shadow_helpers.py`, fixed by this slice) is exercised for
real here for the first time on a NONEMPTY `QualityWarning.detail` (the WARN-
orphan payload nests a `dict` with `list` values,
`execution/out_of_core/_join.py`'s `orphan_fk_warning`) -- every prior shadow
test's diagnostics were empty, so the old unhashable-dict key was never
actually exercised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._plan import PhysicalPlan, PhysicalTable
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import (
    ShadowCoordinator,
    _adapt_ooc_result,
)
from decoy_engine.execution.physical._shadow_diff_codes import (
    MIXED_DRIVER_UNSUPPORTED,
    OOC_DISPATCH_MISSING_DEPENDENCY,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.execution.physical._types import DriverId, ExecutionScope
from decoy_engine.execution.physical.drivers._out_of_core import OutOfCoreAdapter
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._types import Plan
from decoy_engine.relationships import RelationshipGraph
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_diagnostics_multisets_equal,
    assert_ooc_shadow_matches_oracle,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_MASK_KEY = bytes(range(1, 33))


def _key_provider() -> SecretKeyProvider:
    # A secret whose derived mask_key differs from the job_seed the fixtures
    # below use -- the HIGH-1 regression the keyed test below depends on.
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


# ---------------------------------------------------------------------------
# Multi-table config-backed FK fixtures. Reuse the FK SHAPES `_ooc_fixtures.py`
# / `test_out_of_core_fk_parity.py` already pin (a passthrough parent + FK
# child with a deliberate orphan; the non-representable-float/int dtype
# boundary), rebuilt as real pipeline configs so `capture_physical_plan_
# inputs` compiles them through the genuine profile -> compile -> graph path
# (fixes MEDIUM-2: config-backed, not a handcrafted `OocEdgeFixture`).
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _fk_config(
    tmp_path: Path,
    tables: dict[str, tuple[pa.Table, list[dict[str, Any]]]],
    relationships: list[dict[str, Any]],
    *,
    seed: int = 20260916,
) -> dict[str, Any]:
    """A multi-table pipeline config: `tables` maps table name -> (source,
    column-config-list); `relationships` is the raw relationships block.
    Mirrors `build_config`'s single-table shape, widened to N tables."""
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "targets": {},
        "tables": [],
        "relationships": relationships,
    }
    for name, (source, columns) in tables.items():
        path = _write(tmp_path, source, name)
        raw["sources"][name] = {"type": "file", "format": "parquet", "path": str(path)}
        raw["targets"][name] = {
            "type": "file",
            "format": "parquet",
            "path": str(tmp_path / f"{name}.out.parquet"),
        }
        raw["tables"].append({"name": name, "columns": columns})
    return PipelineConfig.model_validate(raw).model_dump()


def _single_edge_sources(*, n_parent: int = 5, n_child: int = 10) -> dict[str, pa.Table]:
    parent = pa.table({"pk": pa.array([f"p{i}" for i in range(n_parent)], type=pa.string())})
    child = pa.table(
        {
            "fk": pa.array([f"p{i % n_parent}" for i in range(n_child)], type=pa.string()),
            "amount": pa.array(list(range(n_child)), type=pa.int64()),
        }
    )
    return {"parent": parent, "child": child}


def _single_edge_config(tmp_path: Path, sources: dict[str, pa.Table]) -> dict[str, Any]:
    return _fk_config(
        tmp_path,
        {
            "parent": (sources["parent"], [{"name": "pk", "strategy": "passthrough"}]),
            "child": (
                sources["child"],
                [
                    {"name": "fk", "strategy": "passthrough"},
                    {"name": "amount", "strategy": "passthrough"},
                ],
            ),
        },
        [
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "child", "columns": ["fk"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_fk",
            }
        ],
    )


def _keyed_sources() -> dict[str, pa.Table]:
    parent = pa.table({"pk": pa.array([f"p{i}" for i in range(5)], type=pa.string())})
    child = pa.table(
        {
            "fk": pa.array([f"p{i % 5}" for i in range(10)], type=pa.string()),
            "payload": pa.array([f"secret{i}" for i in range(10)], type=pa.string()),
        }
    )
    return {"parent": parent, "child": child}


def _keyed_config(tmp_path: Path, sources: dict[str, pa.Table]) -> dict[str, Any]:
    return _fk_config(
        tmp_path,
        {
            "parent": (sources["parent"], [{"name": "pk", "strategy": "passthrough"}]),
            "child": (
                sources["child"],
                [
                    {"name": "fk", "strategy": "passthrough"},
                    {"name": "payload", "strategy": "hash", "namespace": "ns_payload"},
                ],
            ),
        },
        [
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "child", "columns": ["fk"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_fk",
            }
        ],
    )


def _chain_sources() -> dict[str, pa.Table]:
    a = pa.table({"pk_a": pa.array([f"a{i}" for i in range(4)], type=pa.string())})
    b = pa.table(
        {
            "fk_a": pa.array([f"a{i % 4}" for i in range(8)], type=pa.string()),
            "pk_b": pa.array([f"b{i}" for i in range(8)], type=pa.string()),
        }
    )
    c = pa.table(
        {
            "fk_b": pa.array([f"b{i % 8}" for i in range(16)], type=pa.string()),
            "val": pa.array(list(range(16)), type=pa.int64()),
        }
    )
    return {"a": a, "b": b, "c": c}


def _chain_config(tmp_path: Path, sources: dict[str, pa.Table]) -> dict[str, Any]:
    return _fk_config(
        tmp_path,
        {
            "a": (sources["a"], [{"name": "pk_a", "strategy": "passthrough"}]),
            "b": (
                sources["b"],
                [
                    {"name": "fk_a", "strategy": "passthrough"},
                    {"name": "pk_b", "strategy": "passthrough"},
                ],
            ),
            "c": (
                sources["c"],
                [
                    {"name": "fk_b", "strategy": "passthrough"},
                    {"name": "val", "strategy": "passthrough"},
                ],
            ),
        },
        [
            {
                "parent": {"table": "a", "columns": ["pk_a"]},
                "children": [{"table": "b", "columns": ["fk_a"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_ab",
            },
            {
                "parent": {"table": "b", "columns": ["pk_b"]},
                "children": [{"table": "c", "columns": ["fk_b"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_bc",
            },
        ],
    )


def _fan_out_sources() -> dict[str, pa.Table]:
    parent = pa.table({"pk": pa.array([f"p{i}" for i in range(4)], type=pa.string())})
    c1 = pa.table(
        {
            "fk": pa.array([f"p{i % 4}" for i in range(6)], type=pa.string()),
            "v1": pa.array(list(range(6)), type=pa.int64()),
        }
    )
    c2 = pa.table(
        {
            "fk": pa.array([f"p{i % 4}" for i in range(6)], type=pa.string()),
            "v2": pa.array(list(range(6)), type=pa.int64()),
        }
    )
    return {"parent": parent, "c1": c1, "c2": c2}


def _fan_out_config(tmp_path: Path, sources: dict[str, pa.Table]) -> dict[str, Any]:
    return _fk_config(
        tmp_path,
        {
            "parent": (sources["parent"], [{"name": "pk", "strategy": "passthrough"}]),
            "c1": (
                sources["c1"],
                [
                    {"name": "fk", "strategy": "passthrough"},
                    {"name": "v1", "strategy": "passthrough"},
                ],
            ),
            "c2": (
                sources["c2"],
                [
                    {"name": "fk", "strategy": "passthrough"},
                    {"name": "v2", "strategy": "passthrough"},
                ],
            ),
        },
        [
            # A fan-out shares ONE namespace across both edges: the parent
            # key column may belong to exactly one namespace
            # (`NamespaceConfigError(namespace_ambiguity)` otherwise).
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "c1", "columns": ["fk"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_fanout",
            },
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "c2", "columns": ["fk"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_fanout",
            },
        ],
    )


def _orphan_warn_sources() -> dict[str, pa.Table]:
    parent = pa.table({"pk": pa.array(["p0", "p1", "p2"], type=pa.string())})
    child = pa.table(
        {
            "fk": pa.array(
                ["p0", "orphanA", None, "p1", "orphanB", "p2", None, "p0"], type=pa.string()
            ),
            "amount": pa.array(list(range(8)), type=pa.int64()),
        }
    )
    return {"parent": parent, "child": child}


def _orphan_warn_config(tmp_path: Path, sources: dict[str, pa.Table]) -> dict[str, Any]:
    return _fk_config(
        tmp_path,
        {
            "parent": (sources["parent"], [{"name": "pk", "strategy": "passthrough"}]),
            "child": (
                sources["child"],
                [
                    {"name": "fk", "strategy": "passthrough"},
                    {"name": "amount", "strategy": "passthrough"},
                ],
            ),
        },
        [
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "child", "columns": ["fk"]}],
                "orphan_policy": "warn",
                "namespace": "ns_fk",
            }
        ],
    )


def _dtype_unsupported_sources() -> dict[str, pa.Table]:
    # A float PARENT key with an int64 CHILD orphan beyond +/-2**53 -- not
    # exactly representable as a double, so the route cannot narrow a
    # streamed float column after the fact and must fail closed
    # (`out_of_core_fk_key_dtype_unsupported`), mirroring
    # `test_non_representable_int_orphan_float_parent_fails_closed`.
    parent = pa.table({"pk": pa.array([1.0, 2.0], type=pa.float64())})
    child = pa.table({"fk": pa.array([9007199254740993, 9007199254740995], type=pa.int64())})
    return {"parent": parent, "child": child}


def _dtype_unsupported_config(tmp_path: Path, sources: dict[str, pa.Table]) -> dict[str, Any]:
    return _fk_config(
        tmp_path,
        {
            "parent": (sources["parent"], [{"name": "pk", "strategy": "passthrough"}]),
            "child": (sources["child"], [{"name": "fk", "strategy": "passthrough"}]),
        },
        [
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "child", "columns": ["fk"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_fk",
            }
        ],
    )


# `use_byte_estimate_routing=False` + `out_of_core_threshold_rows=1` forces
# OUT_OF_CORE on any nonempty, pure-mask, OOC-compatible, acyclic FK fixture
# (Codex-confirmed; probe routing cannot override it) -- every fixture above
# is a handful of rows, so this is what makes the corpus genuinely route
# OOC instead of vacuously compiling FULL_FRAME/SEQUENTIAL.
_FORCE_OOC: dict[str, Any] = {"out_of_core_threshold_rows": 1, "use_byte_estimate_routing": False}


def _assert_all_ooc(plan: PhysicalPlan) -> None:
    assert plan.tables, "expected at least one mask table"
    for table in plan.tables:
        assert table.driver == DriverId.OUT_OF_CORE, (
            f"{table.table}: expected OUT_OF_CORE, got {table.driver} ({table.driver_reason})"
        )


# ---------------------------------------------------------------------------
# The core proof: single-edge parity, ALWAYS-RUN (no companion guard --
# OOC's shared Arrow kernel needs no compiled companion for an unkeyed job).
# ---------------------------------------------------------------------------


def test_single_edge_ooc_parity_always_run(tmp_path: Path) -> None:
    sources = _single_edge_sources()
    config = _single_edge_config(tmp_path, sources)

    run = run_shadow_and_oracle(config, sources=sources, **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    assert_ooc_shadow_matches_oracle(run)

    # Delegate-vs-adapted schema-identity: the coordinator's adaptation must
    # not reshape what the delegate produced.
    for table, tbl in run.shadow.outputs.items():
        assert tbl.schema.equals(run.oracle.outputs[table].schema)

    assert run.shadow.driver_invocation is not None
    assert run.shadow.driver_invocation.driver_id == DriverId.OUT_OF_CORE
    assert run.shadow.driver_invocation.scope == ExecutionScope.RELATIONSHIP_JOB
    assert set(run.shadow.driver_invocation.tables) == {"parent", "child"}
    # route_evidence is deliberately empty on the OOC branch -- there is no
    # per-node adapter call to report.
    assert run.shadow.route_evidence == {}


# ---------------------------------------------------------------------------
# Keyed (hash) FK payload -- ALSO ALWAYS-RUN (Codex: OOC hash uses the
# shared Arrow kernel, needs no native companion). This IS the HIGH-1
# key-forwarding regression test: it fails if `key_provider` is dropped
# between `ShadowContext` and the `OutOfCoreAdapter.run` call.
# ---------------------------------------------------------------------------


def test_keyed_hash_payload_parity_proves_key_provider_forwarded(tmp_path: Path) -> None:
    sources = _keyed_sources()
    config = _keyed_config(tmp_path, sources)

    run = run_shadow_and_oracle(config, sources=sources, key_provider=_key_provider(), **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    assert_ooc_shadow_matches_oracle(run)


# ---------------------------------------------------------------------------
# Chain (A<-B<-C) and fan-out (two children of one parent): the coordinator's
# relationship-JOB scope and full source/graph forwarding, not just a single
# edge.
# ---------------------------------------------------------------------------


def test_chain_ooc_parity(tmp_path: Path) -> None:
    sources = _chain_sources()
    config = _chain_config(tmp_path, sources)

    run = run_shadow_and_oracle(config, sources=sources, **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    assert_ooc_shadow_matches_oracle(run)
    assert run.shadow.driver_invocation is not None
    assert set(run.shadow.driver_invocation.tables) == {"a", "b", "c"}


def test_fan_out_ooc_parity(tmp_path: Path) -> None:
    sources = _fan_out_sources()
    config = _fan_out_config(tmp_path, sources)

    run = run_shadow_and_oracle(config, sources=sources, **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    assert_ooc_shadow_matches_oracle(run)
    assert run.shadow.driver_invocation is not None
    assert set(run.shadow.driver_invocation.tables) == {"parent", "c1", "c2"}


# ---------------------------------------------------------------------------
# Orphan + null FK keys (WARN-orphan): parity AND warning-multiset parity --
# the diagnostic side, not output values alone. Also the first REAL exercise
# of the fixed `_diag_key` against a nonempty, nested `QualityWarning.detail`.
# ---------------------------------------------------------------------------


def test_orphan_and_null_fk_keys_warn_parity(tmp_path: Path) -> None:
    sources = _orphan_warn_sources()
    config = _orphan_warn_config(tmp_path, sources)

    run = run_shadow_and_oracle(config, sources=sources, **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    assert_ooc_shadow_matches_oracle(run)

    # Non-vacuous: a real orphan warning was actually emitted on both sides
    # (not just "zero warnings on both sides", which the multiset check
    # alone cannot distinguish from a silently-dropped diagnostic).
    assert len(run.shadow.warnings) == 1
    assert len(run.oracle.warnings) == 1
    shadow_warning = run.shadow.warnings[0]
    assert isinstance(shadow_warning, QualityWarning)
    assert shadow_warning.code == "orphan_fk"
    assert shadow_warning.detail["orphan_rows"] == 2


# ---------------------------------------------------------------------------
# Fail-closed dtype: the OOC route rejects an admitted-but-unreproducible FK
# key dtype rather than emit a drifted value; the coordinator dispatch must
# surface the SAME fail-closed code, never partial or wrong output.
# ---------------------------------------------------------------------------


def test_fail_closed_dtype_surfaces_the_same_code(tmp_path: Path) -> None:
    sources = _dtype_unsupported_sources()
    config = _dtype_unsupported_config(tmp_path, sources)

    with pytest.raises(ExecutionError) as excinfo:
        run_shadow_and_oracle(config, sources=sources, **_FORCE_OOC)
    assert excinfo.value.code == "out_of_core_fk_key_dtype_unsupported"


# ---------------------------------------------------------------------------
# Mixed-driver reject: a handcrafted plan whose mask-table driver set mixes
# OUT_OF_CORE with another masking driver must never mask part of the plan
# through the adapter and the rest scalar.
# ---------------------------------------------------------------------------


def _bare_table(name: str, driver: DriverId) -> PhysicalTable:
    return PhysicalTable(
        table=name,
        driver=driver,
        driver_reason="test",
        driver_reason_detail=None,
        rejected_alternatives=(),
        relationship_role="independent",
        substrate="pandas",
        nodes=(),
    )


def test_mixed_driver_plan_is_rejected() -> None:
    plan = PhysicalPlan(
        engine_version=ENGINE_VERSION,
        plan_hash="h",
        synthesis=None,
        tables=(_bare_table("a", DriverId.OUT_OF_CORE), _bare_table("b", DriverId.FULL_FRAME)),
    )
    ctx = ShadowContext(mask_key=b"\x01" * 32)
    snapshot = capture_shadow_snapshot({"a": pa.table({"x": [1]}), "b": pa.table({"x": [1]})})

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx).run(plan, snapshot)
    assert excinfo.value.code == MIXED_DRIVER_UNSUPPORTED
    assert "full_frame" in excinfo.value.detail
    assert "out_of_core" in excinfo.value.detail


def test_ooc_plus_synthesis_plan_is_rejected() -> None:
    from decoy_engine.execution.physical._plan import SynthesisStage

    plan = PhysicalPlan(
        engine_version=ENGINE_VERSION,
        plan_hash="h",
        synthesis=SynthesisStage(tables=("gen",)),
        tables=(_bare_table("a", DriverId.OUT_OF_CORE),),
    )
    ctx = ShadowContext(mask_key=b"\x01" * 32)
    snapshot = capture_shadow_snapshot({"a": pa.table({"x": [1]})})

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx).run(plan, snapshot)
    assert excinfo.value.code == MIXED_DRIVER_UNSUPPORTED


# ---------------------------------------------------------------------------
# batch_rows forwarding: the coordinator's own budget must reach the
# adapter unchanged, for more than one value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size_rows", [2, 500])
def test_batch_rows_forwarded_to_adapter(
    tmp_path: Path, batch_size_rows: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = _single_edge_sources()
    config = _single_edge_config(tmp_path, sources)

    seen: list[int | None] = []
    orig_run = OutOfCoreAdapter.run

    def _spy_run(self: OutOfCoreAdapter, *args: Any, **kwargs: Any) -> ExecutionResult:
        seen.append(kwargs.get("batch_rows"))
        return orig_run(self, *args, **kwargs)

    monkeypatch.setattr(OutOfCoreAdapter, "run", _spy_run)
    run = run_shadow_and_oracle(
        config, sources=sources, batch_size_rows=batch_size_rows, **_FORCE_OOC
    )

    _assert_all_ooc(run.plan)
    assert seen == [batch_size_rows]


# ---------------------------------------------------------------------------
# Driver/source/graph forwarding + evidence: spy the adapter with a sentinel
# result so the exact plan/sources/relationship_graph reaching it can be
# asserted directly, and `driver_invocation` carries all three SeamContext
# properties.
# ---------------------------------------------------------------------------


def test_dispatch_forwards_plan_sources_graph_and_records_seam_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = _single_edge_sources()
    config = _single_edge_config(tmp_path, sources)

    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, **_FORCE_OOC
    )
    plan = compile_physical_plan(inputs)
    _assert_all_ooc(plan)

    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot(sources)

    captured: dict[str, Any] = {}
    sentinel_outputs = {
        "parent": pa.table({"pk": ["sp0"]}),
        "child": pa.table({"fk": ["sp0"], "amount": [1]}),
    }
    sentinel_result = ExecutionResult(outputs=sentinel_outputs)

    def _fake_run(
        self: OutOfCoreAdapter,
        plan_arg: Plan,
        sources_arg: Any,
        *,
        registry: Any,
        relationship_graph: RelationshipGraph,
        sink: Any = None,
        **kwargs: Any,
    ) -> ExecutionResult:
        captured["plan"] = plan_arg
        captured["sources"] = dict(sources_arg)
        captured["relationship_graph"] = relationship_graph
        captured["registry"] = registry
        captured["sink"] = sink
        self.last_invocation = SeamContext(
            driver_id=DriverId.OUT_OF_CORE,
            scope=ExecutionScope.RELATIONSHIP_JOB,
            tables=tuple(sources_arg),
        )
        return sentinel_result

    monkeypatch.setattr(OutOfCoreAdapter, "run", _fake_run)

    result = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)

    assert captured["plan"] is inputs.plan
    assert captured["sources"] == dict(sources)
    assert captured["relationship_graph"] is inputs.graph
    assert captured["registry"] is inputs.registry
    assert captured["sink"] is None

    assert result.outputs is not sentinel_outputs  # _adapt_ooc_result copies the dict...
    assert result.outputs["parent"] is sentinel_outputs["parent"]  # ...never the tables
    assert result.outputs["child"] is sentinel_outputs["child"]

    assert result.driver_invocation is not None
    assert result.driver_invocation.driver_id == DriverId.OUT_OF_CORE
    assert result.driver_invocation.scope == ExecutionScope.RELATIONSHIP_JOB
    assert set(result.driver_invocation.tables) == {"parent", "child"}


# ---------------------------------------------------------------------------
# Missing-dependency dispatch: each runtime OOC carrier absent in turn must
# raise the coded difference naming it, never a bare runner exception.
# ---------------------------------------------------------------------------


def test_missing_plan_dependency_raises_coded_difference(tmp_path: Path) -> None:
    sources = _single_edge_sources()
    config = _single_edge_config(tmp_path, sources)
    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, **_FORCE_OOC
    )
    plan = compile_physical_plan(inputs)
    snapshot = capture_shadow_snapshot(sources)

    base_ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    ctx_missing = ShadowContext(
        mask_key=base_ctx.mask_key,
        job_seed=base_ctx.job_seed,
        relationship_graph=base_ctx.relationship_graph,
        key_provider=base_ctx.key_provider,
        plan=None,
    )

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx_missing, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == OOC_DISPATCH_MISSING_DEPENDENCY
    assert "plan" in excinfo.value.detail


def test_missing_relationship_graph_dependency_raises_coded_difference(tmp_path: Path) -> None:
    sources = _single_edge_sources()
    config = _single_edge_config(tmp_path, sources)
    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, **_FORCE_OOC
    )
    plan = compile_physical_plan(inputs)
    snapshot = capture_shadow_snapshot(sources)

    base_ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    ctx_missing = ShadowContext(
        mask_key=base_ctx.mask_key,
        job_seed=base_ctx.job_seed,
        plan=base_ctx.plan,
        key_provider=base_ctx.key_provider,
        relationship_graph=None,
    )

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx_missing, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == OOC_DISPATCH_MISSING_DEPENDENCY
    assert "relationship_graph" in excinfo.value.detail


def test_missing_registry_dependency_raises_coded_difference(tmp_path: Path) -> None:
    sources = _single_edge_sources()
    config = _single_edge_config(tmp_path, sources)
    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, **_FORCE_OOC
    )
    plan = compile_physical_plan(inputs)
    snapshot = capture_shadow_snapshot(sources)

    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=None).run(plan, snapshot)
    assert excinfo.value.code == OOC_DISPATCH_MISSING_DEPENDENCY
    assert "registry" in excinfo.value.detail


# ---------------------------------------------------------------------------
# `_adapt_ooc_result` fidelity: a pure helper, tested directly with a
# SENTINEL `ExecutionResult`/`SeamContext` -- proves the ExecutionResult ->
# ShadowRunResult conversion returns the SAME objects and mutates no schema
# a value-equal fold in a comparator could hide.
# ---------------------------------------------------------------------------


def test_adapt_ooc_result_preserves_identity() -> None:
    sentinel_outputs = {"t": pa.table({"c": [1, 2, 3]})}
    sentinel_warnings = (QualityWarning(code="orphan_fk", provider="ns", column="c"),)
    sentinel_row_errors = (
        RowErrorRecord(table="t", column="c", row_index=0, trigger="mask", reason="sentinel"),
    )
    sentinel_result = ExecutionResult(
        outputs=sentinel_outputs, warnings=sentinel_warnings, row_errors=sentinel_row_errors
    )
    sentinel_seam = SeamContext(
        driver_id=DriverId.OUT_OF_CORE, scope=ExecutionScope.RELATIONSHIP_JOB, tables=("t",)
    )

    adapted = _adapt_ooc_result(sentinel_result, sentinel_seam)

    assert adapted.outputs is not sentinel_outputs  # a fresh dict...
    for name, table in sentinel_outputs.items():
        assert adapted.outputs[name] is table  # ...of the SAME table objects
    assert adapted.route_evidence == {}
    assert adapted.warnings is sentinel_warnings
    assert adapted.row_errors is sentinel_row_errors
    assert adapted.driver_invocation is sentinel_seam


# ---------------------------------------------------------------------------
# `_diag_key` canonicalization regression: a nonempty `QualityWarning(detail=
# {...})` must no longer raise TypeError, and must match an equal warning /
# reject an unequal one.
# ---------------------------------------------------------------------------


def test_diag_key_canonicalizes_nested_quality_warning_detail() -> None:
    warning_a = QualityWarning(
        code="orphan_fk",
        provider="ns_fk",
        column="fk",
        detail={
            "parent_table": "parent",
            "parent_columns": ["pk"],
            "child_table": "child",
            "child_columns": ["fk"],
            "orphan_rows": 2,
        },
    )
    # Same content, differently-ordered dict -- must still match (dict order
    # is not part of a dict's value).
    warning_a_reordered = QualityWarning(
        code="orphan_fk",
        provider="ns_fk",
        column="fk",
        detail={
            "orphan_rows": 2,
            "child_columns": ["fk"],
            "child_table": "child",
            "parent_columns": ["pk"],
            "parent_table": "parent",
        },
    )
    warning_b = QualityWarning(
        code="orphan_fk",
        provider="ns_fk",
        column="fk",
        detail={
            "parent_table": "parent",
            "parent_columns": ["pk"],
            "child_table": "child",
            "child_columns": ["fk"],
            "orphan_rows": 3,  # differs
        },
    )

    # No TypeError (the old bug: an unhashable dict/list inside vars()).
    assert_diagnostics_multisets_equal((warning_a,), (warning_a_reordered,), "equal-case")

    with pytest.raises(ShadowDifference):
        assert_diagnostics_multisets_equal((warning_a,), (warning_b,), "unequal-case")


# ---------------------------------------------------------------------------
# Harness-contract tests: legacy / sources-only / both-modes / neither /
# partial-legacy.
# ---------------------------------------------------------------------------


def test_harness_legacy_positional_call_still_works(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "t")
    config = build_config(tmp_path, "t", path, [{"name": "c", "strategy": "passthrough"}])

    run = run_shadow_and_oracle(config, "t", source)

    assert run.shadow_identity is not None
    assert run.shadow_identities == {}
    assert_shadow_matches_oracle(run)


def test_harness_sources_only_call_sets_shadow_identities(tmp_path: Path) -> None:
    sources = _single_edge_sources()
    config = _single_edge_config(tmp_path, sources)

    run = run_shadow_and_oracle(config, sources=sources, **_FORCE_OOC)

    assert run.shadow_identity is None
    assert set(run.shadow_identities) == {"parent", "child"}
    assert all(isinstance(v, str) and v for v in run.shadow_identities.values())


def test_harness_rejects_both_legacy_pair_and_sources(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "t")
    config = build_config(tmp_path, "t", path, [{"name": "c", "strategy": "passthrough"}])

    with pytest.raises(ValueError, match="not both"):
        run_shadow_and_oracle(config, "t", source, sources={"t": source})


def test_harness_rejects_neither_legacy_pair_nor_sources(tmp_path: Path) -> None:
    config = {"version": 1}
    with pytest.raises(ValueError, match="must pass either"):
        run_shadow_and_oracle(config)


@pytest.mark.parametrize(
    "table_name, source",
    [("t", None), (None, pa.table({"c": [1]}))],
)
def test_harness_rejects_partial_legacy_pair(
    table_name: str | None, source: pa.Table | None
) -> None:
    config = {"version": 1}
    with pytest.raises(ValueError, match="must both be given"):
        run_shadow_and_oracle(config, table_name, source)


# ---------------------------------------------------------------------------
# No-regression: a plain single-table job (no relationships) still compiles
# FULL_FRAME and runs the pre-existing scalar loop unchanged -- the new
# driver-set branch never fires for it.
# ---------------------------------------------------------------------------


def test_single_table_no_relationships_stays_full_frame_no_regression(tmp_path: Path) -> None:
    source = pa.table({"note": pa.array([f"n{i}" for i in range(20)], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "t")
    config = build_config(tmp_path, "t", path, [{"name": "note", "strategy": "redact"}])

    run = run_shadow_and_oracle(config, "t", source)

    for table in run.plan.tables:
        assert table.driver == DriverId.FULL_FRAME
    assert run.shadow.driver_invocation is None
    assert_shadow_matches_oracle(run)
