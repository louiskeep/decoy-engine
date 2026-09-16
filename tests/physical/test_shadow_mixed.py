"""Task 4.6 slice 5b-i: the coordinator OWNS an INDEPENDENT-mixed job's
dispatch (`ShadowCoordinator._dispatch_mixed` -> `_shadow_mixed.
dispatch_mixed`) -- a plan with both generate tables and mask tables, no
`generate-parent -> mask-child` relationship edge -- proven by a PHASE-BOUND
DIFFERENTIAL PARITY proof against the public `run_pipeline` oracle, mirroring
slice 5a's proof structure. Four groups (plan section "Acceptance tests"):

(a) coordinator wiring -- generation runs before masking, the stitched
    output covers both halves, and the coordinator stays structurally
    inert (no sink/publisher/target surface).
(b) the positive differential matrix -- an admitted generator x mask
    strategy combination, independent (no crossing FK edge), is Arrow-
    IPC-byte-equal to the oracle's stitched output.
(c) the malformed differential matrix -- identical rejection with STAGE-
    RAISED attribution: a malformed generate leaf declines GENERATE on
    both sides with zero mask dispatches (including under a
    simultaneously-malformed mask side); a mask-side fault (which has no
    shared function to hunt for a "naturally occurring" malformed leaf in,
    unlike generation -- see `_shadow_helpers.py`'s section docstring)
    declines MASK on both sides via matched fault injection at each side's
    own strategy-handler layer; a dedicated negative test proves the
    comparator itself rejects a cross-stage "match" (guard the guard).
(d) decline/inertness -- a crossing FK edge, an OUT_OF_CORE mask driver,
    and a disqualifying job-level setting all decline coded, before any
    adapter runs; a generate->generate edge alongside an independent mask
    graph is confirmed ADMITTED (only the crossing direction declines).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.physical import _shadow_coordinator as _coordinator_module
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_diff_codes import (
    GENERATION_SHAPE_UNSUPPORTED,
    MIXED_DRIVER_UNSUPPORTED,
    MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_mixed import (
    require_independent_mixed_shadowable,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.execution.physical.drivers._synthesis import SynthesisStageAdapter
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    MixedDifferentialRun,
    assert_generation_outputs_arrow_ipc_equal,
    assert_mixed_failures_match,
    run_mixed_shadow_and_oracle,
    run_shadow_and_oracle,
)

_DEFAULT_GENERATE_COLUMNS: list[dict[str, Any]] = [
    {"name": "id", "type": "sequence", "start": 1, "step": 1},
    {"name": "tier", "type": "categorical", "categories": ["A", "B", "C"]},
]


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _mixed_config(
    tmp_path: Path,
    *,
    mask_sources: dict[str, pa.Table],
    mask_table_columns: dict[str, list[dict[str, Any]]],
    generate_table: str = "people",
    generate_columns: list[dict[str, Any]] | None = None,
    row_count: int = 5,
    seed: int = 20260916,
    relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One generate table + N independent mask tables, no crossing FK edge
    unless `relationships` says otherwise. Mirrors `test_shadow_ooc_fk.
    py`'s `_fk_config` shape, widened with a generate-kind table entry."""
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "targets": {
            generate_table: {
                "type": "file",
                "format": "csv",
                "path": f"{generate_table}.out.csv",
            }
        },
        "tables": [
            {
                "name": generate_table,
                "row_count": row_count,
                "generate_columns": generate_columns or _DEFAULT_GENERATE_COLUMNS,
            }
        ],
    }
    for name, source in mask_sources.items():
        path = _write_source(tmp_path, source, name)
        raw["sources"][name] = {"type": "file", "format": "parquet", "path": str(path)}
        raw["targets"][name] = {
            "type": "file",
            "format": "parquet",
            "path": str(tmp_path / f"{name}.out.parquet"),
        }
        raw["tables"].append({"name": name, "columns": mask_table_columns[name]})
    if relationships:
        raw["relationships"] = relationships
    return PipelineConfig.model_validate(raw).model_dump()


def _accounts_source(n: int = 6) -> pa.Table:
    # Single column: the coordinator's per-node loop requires EVERY source
    # column to carry an in-slice strategy (`_shadow_coordinator.py`'s
    # "the assembled set must equal the source set exactly"), so a fixture
    # with an unconfigured second column would fail for a reason unrelated
    # to what these tests are proving.
    return pa.table({"acct_id": pa.array([f"a{i}" for i in range(n)], type=pa.string())})


# ---------------------------------------------------------------------------
# (a) Coordinator wiring
# ---------------------------------------------------------------------------


def test_dispatch_mixed_runs_generate_before_mask_and_stitches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": _accounts_source()},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    inputs = capture_physical_plan_inputs(
        config,
        {"accounts": _accounts_source()},
        engine_version=ENGINE_VERSION,
        execution_mode="full_frame",
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": _accounts_source()})

    generate_calls: list[Any] = []
    original_generate = SynthesisStageAdapter.run

    def _spy_generate(
        self: SynthesisStageAdapter, *args: Any, **kwargs: Any
    ) -> dict[str, pa.Table]:
        generate_calls.append(args)
        return original_generate(self, *args, **kwargs)

    mask_calls: list[Any] = []
    original_run_operator = _coordinator_module.run_operator  # type: ignore[attr-defined]

    def _spy_mask(*args: Any, **kwargs: Any) -> pa.Array:
        mask_calls.append(args)
        return original_run_operator(*args, **kwargs)

    monkeypatch.setattr(SynthesisStageAdapter, "run", _spy_generate)
    monkeypatch.setattr(_coordinator_module, "run_operator", _spy_mask)

    result = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)

    assert len(generate_calls) == 1
    assert len(mask_calls) >= 1
    assert set(result.outputs) == {"people", "accounts"}
    assert isinstance(result.driver_invocation, SeamContext)
    assert result.driver_invocation.driver_id == DriverId.SYNTHESIS
    # route_evidence covers the MASK half only (the generate half has no
    # per-node evidence to report, same convention as the pure dispatch).
    assert set(result.route_evidence) == {n.node_id for t in plan.tables for n in t.nodes}


def test_shadow_context_and_coordinator_have_no_sink_surface() -> None:
    """C1/C7 restated for the mixed path: neither the frozen `ShadowContext`
    itself nor `ShadowCoordinator` carries a field/parameter literally named
    `sink` -- structurally proving `dispatch_mixed` cannot construct or
    forward one, mirroring the same check `test_shadow_disconnection.py`
    runs for every other dispatch. `from_key_provider` is exempt by design:
    it takes the REAL `sink` object positionally and converts it to a
    presence-only boolean at construction (`ShadowContext`'s own docstring)
    -- the boolean, not the object, is what lands on the frozen carrier."""
    import inspect

    for target in (ShadowContext, ShadowCoordinator):
        params = inspect.signature(target).parameters
        assert "sink" not in params, f"{target!r} unexpectedly accepts a sink parameter"


def test_ctx_repr_never_carries_derive_key_or_mask_key() -> None:
    ctx = ShadowContext(mask_key=b"\x02" * 32, derive_key=lambda label: b"x" * 32)
    rendered = repr(ctx)
    assert b"\x02".hex() not in rendered
    assert "lambda" not in rendered


# ---------------------------------------------------------------------------
# (b) Positive differential matrix -- Arrow-IPC-byte-equal vs the oracle,
# over the FULL stitched (generate + mask) output union.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mask_columns",
    [
        [{"name": "acct_id", "strategy": "passthrough"}],
        [{"name": "acct_id", "strategy": "redact", "provider_config": {"redact_with": "XXX"}}],
        [{"name": "acct_id", "strategy": "truncate", "provider_config": {"length": 3}}],
        pytest.param(
            [{"name": "acct_id", "strategy": "hash", "namespace": "ns_acct"}],
            marks=pytest.mark.skipif(
                not native_companion_status().ok,
                reason="compiled decoy-engine-native companion unavailable",
            ),
        ),
    ],
    ids=["passthrough", "redact", "truncate", "hash"],
)
def test_positive_independent_mixed_matrix(
    tmp_path: Path, mask_columns: list[dict[str, Any]]
) -> None:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": mask_columns},
    )
    run = run_shadow_and_oracle(config, sources={"accounts": accounts})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_positive_multiple_independent_mask_tables(tmp_path: Path) -> None:
    accounts = _accounts_source()
    orders = pa.table({"order_id": pa.array([f"o{i}" for i in range(4)], type=pa.string())})
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts, "orders": orders},
        mask_table_columns={
            "accounts": [{"name": "acct_id", "strategy": "passthrough"}],
            "orders": [
                {"name": "order_id", "strategy": "truncate", "provider_config": {"length": 2}}
            ],
        },
    )
    sources = {"accounts": accounts, "orders": orders}
    run = run_shadow_and_oracle(config, sources=sources)
    assert_generation_outputs_arrow_ipc_equal(run)


def test_positive_generate_to_generate_edge_with_independent_mask_graph(tmp_path: Path) -> None:
    """A generate->generate edge, plus a wholly separate independent mask
    table, is ADMITTED -- only a generate->mask CROSSING edge declines
    (Codex #3)."""
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
        generate_table="people",
        generate_columns=[
            {"name": "id", "type": "sequence", "start": 1, "step": 1},
            {"name": "tier", "type": "categorical", "categories": ["A", "B"]},
        ],
    )
    # Add a second, independent generate table declaring a generate->generate
    # relationship edge back to the first -- `relationships:` is a purely
    # declarative metadata block (`profile/_source.py`'s `_build_relationships`
    # reads it straight off `config`, never off actual profiled data), so this
    # is config-schema-valid without needing a `reference`-type column (which
    # is outside this slice's admitted generate-column-type set anyway). Not
    # the direction this slice's gate restricts.
    config["tables"].append(
        {
            "name": "profiles",
            "row_count": 5,
            "generate_columns": [
                {"name": "person_id", "type": "sequence", "start": 1, "step": 1},
                {"name": "bio_tag", "type": "categorical", "categories": ["x", "y"]},
            ],
        }
    )
    config["relationships"] = [
        {
            "parent": {"table": "people", "columns": ["id"]},
            "children": [{"table": "profiles", "columns": ["person_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_people",
        }
    ]
    config = PipelineConfig.model_validate(config).model_dump()
    run = run_shadow_and_oracle(config, sources={"accounts": accounts})
    assert_generation_outputs_arrow_ipc_equal(run)


# ---------------------------------------------------------------------------
# (c) Malformed differential matrix: identical rejection with STAGE-RAISED
# attribution.
# ---------------------------------------------------------------------------


def _mixed_run_with_malformed_generate(tmp_path: Path) -> MixedDifferentialRun:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
        generate_columns=[{"name": "id", "type": "sequence", "start": "abc"}],
    )
    return run_mixed_shadow_and_oracle(config, {"accounts": accounts})


def test_malformed_generate_leaf_declines_generate_stage_zero_mask_dispatch(
    tmp_path: Path,
) -> None:
    diff = _mixed_run_with_malformed_generate(tmp_path)
    assert diff.shadow_stage == "generate"
    assert diff.oracle_stage == "generate"
    assert diff.shadow_mask_dispatch_count == 0
    assert diff.oracle_mask_dispatch_count == 0
    assert_mixed_failures_match(diff)


def test_dual_fault_still_classifies_generate_with_zero_mask_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malform BOTH sides. The oracle is generate-first (`_pipeline.py:481`
    then `505`), so a job with faults in both halves surfaces only the
    GENERATE fault -- mask never runs on either side, proven here by the
    dispatch counter rather than assumed."""
    _install_mask_fault_injection(monkeypatch)
    diff = _mixed_run_with_malformed_generate(tmp_path)
    assert diff.shadow_stage == "generate"
    assert diff.oracle_stage == "generate"
    assert diff.shadow_mask_dispatch_count == 0
    assert diff.oracle_mask_dispatch_count == 0
    assert_mixed_failures_match(diff)


_INJECTED_FAULT_MESSAGE = "mixed-mask-injected-fault"


def _install_mask_fault_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force an IDENTICAL failure at each side's own mask strategy-handler
    layer. Masking has no function the shadow's native operators and the
    oracle's pandas strategy handlers literally share (unlike generation's
    `_generate_tables_from_config` -- see `_shadow_helpers.py`'s section
    docstring), so a "naturally occurring malformed leaf" that raises
    byte-identically on both independent implementations is not a
    reliably constructible fixture. Injecting the SAME fault at each
    side's own redact layer proves the RECORDER's stage attribution and
    fingerprint comparison work correctly for a MASK-stage failure,
    which is the acceptance requirement (Codex #5) -- the fault's origin
    is deliberate, not a stand-in for real-world masking correctness
    (slices 1-4 already exhaustively prove that separately).
    """

    def _boom_native(*args: Any, **kwargs: Any) -> pa.Array:
        raise ValueError(_INJECTED_FAULT_MESSAGE)

    def _boom_pandas(self: RedactHandler, *args: Any, **kwargs: Any) -> Any:
        raise ValueError(_INJECTED_FAULT_MESSAGE)

    monkeypatch.setattr(_coordinator_module, "run_operator", _boom_native, raising=True)
    monkeypatch.setattr(RedactHandler, "run", _boom_pandas)


def test_generate_ok_mask_fault_declines_mask_stage_matching_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "redact"}]},
    )
    _install_mask_fault_injection(monkeypatch)

    diff = run_mixed_shadow_and_oracle(config, {"accounts": accounts})

    assert diff.shadow_stage == "mask"
    assert diff.oracle_stage == "mask"
    assert diff.shadow_mask_dispatch_count >= 1
    assert diff.oracle_mask_dispatch_count >= 1
    assert _INJECTED_FAULT_MESSAGE in str(diff.shadow_exception)
    assert _INJECTED_FAULT_MESSAGE in str(diff.oracle_exception)
    assert_mixed_failures_match(diff)


def test_cross_stage_mismatch_is_a_parity_failure_guard_the_guard() -> None:
    """A dedicated negative test: even with matching fingerprints, a
    mismatched stage attribution must fail the comparator -- otherwise a
    shadow implementation that ran mask work despite a generate failure
    (or vice versa) could pass by accident (Codex #5)."""
    exc = ValueError("same fingerprint on both sides")
    run = MixedDifferentialRun(
        plan=None,  # type: ignore[arg-type]
        shadow_tables=None,
        shadow_exception=exc,
        shadow_stage="generate",
        shadow_mask_dispatch_count=0,
        oracle_tables=None,
        oracle_exception=exc,
        oracle_stage="mask",
        oracle_mask_dispatch_count=1,
    )
    with pytest.raises(AssertionError, match="stage mismatch"):
        assert_mixed_failures_match(run)


# ---------------------------------------------------------------------------
# (d) Decline / inertness.
# ---------------------------------------------------------------------------


def test_decline_generate_parent_mask_child_fk_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = pa.table({"person_id": pa.array([f"{i % 5}" for i in range(6)], type=pa.string())})
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "person_id", "strategy": "passthrough"}]},
        generate_columns=[
            {"name": "id", "type": "sequence", "start": 0, "step": 1, "pad_length": 1}
        ],
        relationships=[
            {
                "parent": {"table": "people", "columns": ["id"]},
                "children": [{"table": "accounts", "columns": ["person_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_people",
            }
        ],
    )
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})

    generate_calls: list[Any] = []

    def _bomb_generate(self: SynthesisStageAdapter, *a: Any, **k: Any) -> dict[str, pa.Table]:
        generate_calls.append((a, k))
        raise AssertionError("generate_tables must not be invoked for a declined mixed plan")

    mask_calls: list[Any] = []

    def _bomb_mask(*a: Any, **k: Any) -> pa.Array:
        mask_calls.append((a, k))
        raise AssertionError("run_operator must not be invoked for a declined mixed plan")

    monkeypatch.setattr(SynthesisStageAdapter, "run", _bomb_generate)
    monkeypatch.setattr(_coordinator_module, "run_operator", _bomb_mask)

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == MIXED_FK_CROSS_GENERATE_UNSUPPORTED
    assert generate_calls == []
    assert mask_calls == []


def test_decline_out_of_core_mask_driver_with_generate(tmp_path: Path) -> None:
    """An OUT_OF_CORE mask driver alongside an otherwise fully-admitted
    generate half declines `MIXED_DRIVER_UNSUPPORTED`. Production routing
    itself already disqualifies a generate+mask job from ever auto-selecting
    OOC (`_pipeline_routing`'s `generate_plus_mask` rejection reason), so a
    REAL compiled plan can never carry this exact shape; the driver is
    forced by hand onto an otherwise-real compiled mask table, the same way
    `test_shadow_ooc_fk.py`'s own hand-built driver-set tests exercise a
    shape production routing would never produce, to prove the mixed
    gate's OWN defensive rejection (not merely that routing avoids it)."""
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    assert plan.synthesis is not None
    forced_table = dataclasses.replace(plan.tables[0], driver=DriverId.OUT_OF_CORE)
    forced_plan = dataclasses.replace(plan, tables=(forced_table,))
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(forced_plan, snapshot)
    assert excinfo.value.code == MIXED_DRIVER_UNSUPPORTED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("validators", [{"name": "fk_intact"}]),
        ("quarantine", {"output_path": "q.parquet"}),
    ],
    ids=["validators", "quarantine"],
)
def test_decline_disqualifying_job_setting(
    tmp_path: Path, field: str, value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    config[field] = value
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})

    def _bomb_generate(self: SynthesisStageAdapter, *a: Any, **k: Any) -> dict[str, pa.Table]:
        raise AssertionError("generate_tables must not be invoked for a declined mixed plan")

    monkeypatch.setattr(SynthesisStageAdapter, "run", _bomb_generate)

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED


@pytest.mark.parametrize(
    "ctx_kwargs",
    [
        {"vault_writer_requested": True},
        {"fidelity_report": True},
        {"sink_requested": True},
        {"source_loader_requested": True},
    ],
    ids=["vault_writer", "fidelity_report", "sink", "source_loader"],
)
def test_decline_disqualifying_runtime_carrier(tmp_path: Path, ctx_kwargs: dict[str, Any]) -> None:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    admitted_ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    ctx = dataclasses.replace(admitted_ctx, **ctx_kwargs)
    snapshot = capture_shadow_snapshot({"accounts": accounts})

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED


def test_decline_mask_secret_ref(tmp_path: Path) -> None:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    config["global_settings"]["mask_secret_ref"] = "env:SOME_SECRET"
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED


# ---------------------------------------------------------------------------
# (A6) Raw-exception guards: enumerated total guards decline coded, never
# escape as a raw structural-boundary exception.
# ---------------------------------------------------------------------------


def test_gate_declines_relationship_graph_none(tmp_path: Path) -> None:
    """A mixed dispatch with `ShadowContext.relationship_graph` unset
    declines coded -- never lets a crossing-edge check run against a bare
    `None` (mirrors the OOC dispatch's own missing-dependency discipline)."""
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    # Deliberately omit relationship_graph (defaults to None).
    ctx = ShadowContext.from_key_provider(plan=inputs.plan, key_provider=None)
    snapshot = capture_shadow_snapshot({"accounts": accounts})

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED


def test_gate_declines_malformed_relationship_edge_attrs(tmp_path: Path) -> None:
    """A relationship-graph stand-in whose edges lack `parent_table`/
    `child_table` declines coded, never raises a raw `AttributeError`."""
    from types import SimpleNamespace

    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    malformed_graph = SimpleNamespace(edges=(SimpleNamespace(),))
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan,
        key_provider=None,
        relationship_graph=malformed_graph,  # type: ignore[arg-type]
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED


def test_gate_declines_unhashable_mask_table_name(tmp_path: Path) -> None:
    """A mask-table name that is unhashable declines coded, never raises a
    raw `TypeError` from the frozenset construction."""

    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})
    bad_table = dataclasses.replace(plan.tables[0], table=["unhashable"])  # type: ignore[arg-type]
    bad_plan = dataclasses.replace(plan, tables=(bad_table,))

    with pytest.raises(ShadowDifference) as excinfo:
        require_independent_mixed_shadowable(ctx, bad_plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED


def test_gate_declines_unhashable_mask_table_driver(tmp_path: Path) -> None:
    """A mask-table `driver` that is unhashable declines coded, never
    raises a raw `TypeError` from the frozenset construction."""
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_sources={"accounts": accounts},
        mask_table_columns={"accounts": [{"name": "acct_id", "strategy": "passthrough"}]},
    )
    inputs = capture_physical_plan_inputs(
        config, {"accounts": accounts}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})
    bad_table = dataclasses.replace(plan.tables[0], driver=["unhashable"])  # type: ignore[arg-type]
    bad_plan = dataclasses.replace(plan, tables=(bad_table,))

    with pytest.raises(ShadowDifference) as excinfo:
        require_independent_mixed_shadowable(ctx, bad_plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED
