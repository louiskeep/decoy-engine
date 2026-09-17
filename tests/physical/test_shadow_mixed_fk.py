"""Task 4.6 slice 5b-ii: the coordinator OWNS a COUPLED mixed job's FK
dispatch -- a mask table whose FK parent is a generate table, so the mask
side reads the generate output as its FK pool (`_shadow_fk.py` +
`_shadow_mixed._select_admitted_coupled_edge`). Extends slice 5b-i's
independent-mixed dispatch (`test_shadow_mixed.py`), proven by a
DIFFERENTIAL PARITY proof against the public `run_pipeline` oracle. Six
groups (plan section "Acceptance tests" A1-A9):

(a) positive parity -- PRESERVE and WARN, plus FK integrity (child keys are
    a subset of the generated parent's keys) (A1, A2).
(b) dtype byte-parity characterization -- signed/unsigned int widths,
    values beyond 2**53, null child FKs, an all-null child output, and
    string keys, each admitted and byte-matching the oracle (A3).
(c) orphan identical-rejections -- FAIL and REMAP both fail identically on
    both sides (A4).
(d) topology declines -- every complete-graph-rule violation declines coded
    before any adapter runs (A5).
(e) round-trip decline -- the 5b-i output-stability gate still applies to a
    coupled job's generated parent (A6).
(f) behavior-preservation, inertness, and module-size pins (A7-A9).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError, run_pipeline
from decoy_engine.execution import _fk_resolve as fk_resolve_module
from decoy_engine.execution import _stitch as stitch_module
from decoy_engine.execution.physical import _shadow_coordinator as _coordinator_module
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_diff_codes import (
    GENERATION_SHAPE_UNSUPPORTED,
    MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
    MIXED_FK_TOPOLOGY_UNSUPPORTED,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.execution.physical.drivers._synthesis import SynthesisStageAdapter
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_diagnostics_multisets_equal,
    assert_generation_outputs_arrow_ipc_equal,
    run_shadow_and_oracle,
)

# ---------------------------------------------------------------------------
# Config builder: one generate-kind parent table + N mask-kind tables, an
# explicit `relationships` block (the crossing edge plus, for the topology
# tests, whatever extra edges each case needs).
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _fk_mixed_config(
    tmp_path: Path,
    *,
    generate_table: str,
    generate_columns: list[dict[str, Any]],
    mask_tables: dict[str, tuple[pa.Table, list[dict[str, Any]]]],
    relationships: list[dict[str, Any]],
    row_count: int = 1,
    seed: int = 20260917,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "targets": {
            generate_table: {"type": "file", "format": "csv", "path": f"{generate_table}.out.csv"}
        },
        "tables": [
            {"name": generate_table, "row_count": row_count, "generate_columns": generate_columns}
        ],
        "relationships": relationships,
    }
    for name, (source, columns) in mask_tables.items():
        path = _write(tmp_path, source, name)
        raw["sources"][name] = {"type": "file", "format": "parquet", "path": str(path)}
        raw["targets"][name] = {
            "type": "file",
            "format": "parquet",
            "path": str(tmp_path / f"{name}.out.parquet"),
        }
        raw["tables"].append({"name": name, "columns": columns})
    return PipelineConfig.model_validate(raw).model_dump()


def _fk_edge(
    *,
    parent_table: str = "people",
    parent_columns: list[str] | None = None,
    child_table: str = "accounts",
    child_columns: list[str] | None = None,
    orphan_policy: str = "preserve",
    namespace: str = "ns_people",
) -> dict[str, Any]:
    return {
        "parent": {"table": parent_table, "columns": parent_columns or ["id"]},
        "children": [{"table": child_table, "columns": child_columns or ["person_id"]}],
        "orphan_policy": orphan_policy,
        "namespace": namespace,
    }


def _passthrough_columns(*names: str) -> list[dict[str, Any]]:
    return [{"name": n, "strategy": "passthrough"} for n in names]


# ---------------------------------------------------------------------------
# (a) Positive parity + FK integrity (A1, A2).
# ---------------------------------------------------------------------------


def test_positive_preserve_matches_and_fk_subset_of_parent_keys(tmp_path: Path) -> None:
    """A1 (PRESERVE) + A2: every child FK value matches a real generated
    parent key (no orphans), full whole-job byte parity, and the resolved
    child column is a subset of the generated parent's actual keys."""
    accounts = pa.table({"person_id": pa.array([f"{i % 5}" for i in range(6)], type=pa.string())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={"accounts": (accounts, _passthrough_columns("person_id"))},
        relationships=[_fk_edge(orphan_policy="preserve")],
        row_count=5,
    )
    run = run_shadow_and_oracle(config, sources={"accounts": accounts})
    assert_generation_outputs_arrow_ipc_equal(run)

    parent_keys = set(run.shadow.outputs["people"].column("id").to_pylist())
    child_keys = set(run.shadow.outputs["accounts"].column("person_id").to_pylist())
    assert child_keys <= parent_keys
    assert parent_keys == {"0", "1", "2", "3", "4"}  # sequence(start=0, step=1) is deterministic


def test_positive_warn_orphan_matches_and_warns(tmp_path: Path) -> None:
    """A1 (WARN): an orphan child row parity-matches the oracle's stitched
    output AND its aggregated `orphan_fk` warning."""
    accounts = pa.table({"person_id": pa.array(["0", "1", "zzz", "zzz"], type=pa.string())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={"accounts": (accounts, _passthrough_columns("person_id"))},
        relationships=[_fk_edge(orphan_policy="warn")],
        row_count=3,
    )
    run = run_shadow_and_oracle(config, sources={"accounts": accounts})
    assert_generation_outputs_arrow_ipc_equal(run)
    assert_diagnostics_multisets_equal(run.shadow.warnings, tuple(run.oracle.warnings), "warnings")
    assert len(run.oracle.warnings) == 1
    assert run.oracle.warnings[0].code == "orphan_fk"


# ---------------------------------------------------------------------------
# (b) Dtype byte-parity characterization (A3). Every case admits a single
# matched row (parent key `1`, a deterministic single-category generate
# column) plus one exercised shape via an ORPHAN row under PRESERVE -- the
# orphan's raw value is what `_write_back_fk_column` must reproduce
# byte-for-byte, decoupling this matrix from generate-column mechanics
# (`_pandas_adapter.py`'s write-back operates on the RESOLVED value
# regardless of whether it came from a parent match or a preserved orphan).
# ---------------------------------------------------------------------------


def _single_key_parent_config(
    tmp_path: Path, child: pa.Table, *, seed: int = 20260917
) -> dict[str, Any]:
    return _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "categorical", "categories": [1]}],
        mask_tables={"accounts": (child, _passthrough_columns("person_id"))},
        relationships=[_fk_edge(orphan_policy="preserve")],
        row_count=1,
        seed=seed,
    )


@pytest.mark.parametrize(
    ("dtype", "orphan_value"),
    [
        (pa.int8(), -128),
        (pa.uint32(), 4294967295),
        (pa.int64(), 2**53 + 12345),  # beyond exact float64 precision
        (pa.uint64(), 2**64 - 1),  # beyond Int64's range; needs UInt64
    ],
    ids=["int8_min", "uint32_max", "int64_beyond_2_53", "uint64_beyond_int64_max"],
)
def test_dtype_characterization_matched_and_orphan(
    tmp_path: Path, dtype: pa.DataType, orphan_value: int
) -> None:
    child = pa.table({"person_id": pa.array([1, orphan_value], type=dtype)})
    config = _single_key_parent_config(tmp_path, child)
    run = run_shadow_and_oracle(config, sources={"accounts": child})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_dtype_characterization_null_child_fk(tmp_path: Path) -> None:
    """A3: a null child FK is preserved as null (never an orphan), mixed
    with a matched and a genuinely orphaned row."""
    child = pa.table({"person_id": pa.array([1, None, 5], type=pa.int64())})
    config = _single_key_parent_config(tmp_path, child)
    run = run_shadow_and_oracle(config, sources={"accounts": child})
    assert_generation_outputs_arrow_ipc_equal(run)


@pytest.mark.parametrize(
    "dtype", [pa.int32(), pa.uint16()], ids=["all_null_int32", "all_null_uint16"]
)
def test_dtype_characterization_all_null_child_output(tmp_path: Path, dtype: pa.DataType) -> None:
    """A3: every child row null -- `fk_all_null_array` must preserve the
    child's OWN pre-resolution dtype, not force a blanket Int64."""
    child = pa.table({"person_id": pa.array([None, None, None], type=dtype)})
    config = _single_key_parent_config(tmp_path, child)
    run = run_shadow_and_oracle(config, sources={"accounts": child})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_dtype_characterization_string_keys(tmp_path: Path) -> None:
    """A3: string keys (both sides), matched + orphan."""
    child = pa.table({"person_id": pa.array(["0", "zzz"], type=pa.string())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={"accounts": (child, _passthrough_columns("person_id"))},
        relationships=[_fk_edge(orphan_policy="preserve")],
        row_count=1,
    )
    run = run_shadow_and_oracle(config, sources={"accounts": child})
    assert_generation_outputs_arrow_ipc_equal(run)


# ---------------------------------------------------------------------------
# (c) Orphan identical-rejections (A4): FAIL and REMAP both fail identically
# on both sides, MASK-stage attribution (generation always succeeds first
# in these fixtures; the failure is provably the FK-resolution step, since
# generate_tables never raises for an admitted sequence/categorical config).
# ---------------------------------------------------------------------------


def _fk_orphan_config(tmp_path: Path, accounts: pa.Table, orphan_policy: str) -> dict[str, Any]:
    return _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={"accounts": (accounts, _passthrough_columns("person_id"))},
        relationships=[_fk_edge(orphan_policy=orphan_policy)],
        row_count=3,
    )


def _run_shadow_raises(config: dict[str, Any], sources: dict[str, pa.Table]) -> ExecutionError:
    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot(sources)
    with pytest.raises(ExecutionError) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    return excinfo.value


def _run_oracle_raises(config: dict[str, Any], sources: dict[str, pa.Table]) -> ExecutionError:
    with pytest.raises(ExecutionError) as excinfo:
        run_pipeline(
            config,
            sources,
            engine_version=ENGINE_VERSION,
            substrate="pandas",
            execution_mode="full_frame",
            native_route_enabled=False,
            key_provider=None,
            sink=None,
        )
    return excinfo.value


def test_fail_orphan_identical_rejection(tmp_path: Path) -> None:
    accounts = pa.table({"person_id": pa.array(["0", "1", "zzz"], type=pa.string())})
    config = _fk_orphan_config(tmp_path, accounts, "fail")
    shadow_exc = _run_shadow_raises(config, {"accounts": accounts})
    oracle_exc = _run_oracle_raises(config, {"accounts": accounts})
    assert shadow_exc.code == oracle_exc.code == "orphan_fk_violation"
    assert str(shadow_exc) == str(oracle_exc)


def test_remap_orphan_is_identical_parent_missing_rejection(tmp_path: Path) -> None:
    """A generate parent is never a masked `WorkNode`, so REMAP always fails
    `orphan_remap_parent_missing` identically on both sides (plan item 6)."""
    accounts = pa.table({"person_id": pa.array(["0", "1", "zzz"], type=pa.string())})
    config = _fk_orphan_config(tmp_path, accounts, "remap")
    shadow_exc = _run_shadow_raises(config, {"accounts": accounts})
    oracle_exc = _run_oracle_raises(config, {"accounts": accounts})
    assert shadow_exc.code == oracle_exc.code == "orphan_remap_parent_missing"
    assert str(shadow_exc) == str(oracle_exc)


# ---------------------------------------------------------------------------
# (d) Topology declines (A5): every complete-graph-rule violation, zero mask
# side effects on decline (mirrors test_shadow_mixed.py's own decline style).
# ---------------------------------------------------------------------------


def _assert_declines_with_no_dispatch(
    config: dict[str, Any],
    sources: dict[str, pa.Table],
    monkeypatch: pytest.MonkeyPatch,
    expected_code: str,
) -> None:
    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot(sources)

    def _bomb_generate(self: SynthesisStageAdapter, *a: Any, **k: Any) -> dict[str, pa.Table]:
        raise AssertionError("generate_tables must not be invoked for a declined coupled plan")

    def _bomb_mask(*a: Any, **k: Any) -> pa.Array:
        raise AssertionError("run_operator must not be invoked for a declined coupled plan")

    monkeypatch.setattr(SynthesisStageAdapter, "run", _bomb_generate)
    monkeypatch.setattr(_coordinator_module, "run_operator", _bomb_mask)

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    assert excinfo.value.code == expected_code


def test_decline_multiple_crossing_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two DIFFERENT generate parents, each crossing into its own mask
    child -- more than one crossing edge in the whole run."""
    accounts = pa.table({"person_id": pa.array(["0", "1"], type=pa.string())})
    orders = pa.table({"buyer_id": pa.array(["0", "1"], type=pa.string())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={
            "accounts": (accounts, _passthrough_columns("person_id")),
            "orders": (orders, _passthrough_columns("buyer_id")),
        },
        relationships=[
            _fk_edge(child_table="accounts", child_columns=["person_id"]),
            _fk_edge(
                parent_table="buyers",
                child_table="orders",
                child_columns=["buyer_id"],
                namespace="ns_buyers",
            ),
        ],
        row_count=2,
    )
    config["tables"].append(
        {
            "name": "buyers",
            "row_count": 2,
            "generate_columns": [{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        }
    )
    config = PipelineConfig.model_validate(config).model_dump()
    _assert_declines_with_no_dispatch(
        config,
        {"accounts": accounts, "orders": orders},
        monkeypatch,
        MIXED_FK_TOPOLOGY_UNSUPPORTED,
    )


def test_decline_composite_fk_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = pa.table(
        {
            "person_id": pa.array(["0", "1"], type=pa.string()),
            "person_id2": pa.array(["0", "1"], type=pa.string()),
        }
    )
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[
            {"name": "id", "type": "sequence", "start": 0, "step": 1},
            {"name": "id2", "type": "sequence", "start": 0, "step": 1},
        ],
        mask_tables={"accounts": (accounts, _passthrough_columns("person_id", "person_id2"))},
        relationships=[
            _fk_edge(
                parent_columns=["id", "id2"],
                child_columns=["person_id", "person_id2"],
            )
        ],
        row_count=2,
    )
    _assert_declines_with_no_dispatch(
        config,
        {"accounts": accounts},
        monkeypatch,
        MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
    )


def test_decline_non_admitted_child_key_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complete-graph rule admits int/string keys only; a float child
    key declines before generation ever runs (the child's type is known
    from the resident snapshot, so this is checked pre-generation)."""
    accounts = pa.table({"person_id": pa.array([0.0, 1.0], type=pa.float64())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={"accounts": (accounts, _passthrough_columns("person_id"))},
        relationships=[_fk_edge()],
        row_count=2,
    )
    _assert_declines_with_no_dispatch(
        config,
        {"accounts": accounts},
        monkeypatch,
        MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
    )


def test_decline_another_incoming_edge_to_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two DIFFERENT generate parents each declare an edge into the SAME
    mask child -- another incoming edge to the admitted child."""
    accounts = pa.table(
        {
            "person_id": pa.array(["0", "1"], type=pa.string()),
            "org_id": pa.array(["0", "1"], type=pa.string()),
        }
    )
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={"accounts": (accounts, _passthrough_columns("person_id", "org_id"))},
        relationships=[
            _fk_edge(child_columns=["person_id"]),
            _fk_edge(child_columns=["org_id"], namespace="ns_orgs"),
        ],
        row_count=2,
    )
    config["tables"].append(
        {
            "name": "orgs",
            "row_count": 2,
            "generate_columns": [{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        }
    )
    config["relationships"][1]["parent"]["table"] = "orgs"
    config = PipelineConfig.model_validate(config).model_dump()
    _assert_declines_with_no_dispatch(
        config, {"accounts": accounts}, monkeypatch, MIXED_FK_TOPOLOGY_UNSUPPORTED
    )


def test_decline_outgoing_edge_from_child_reaches_a_mask_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The admitted edge's own mask CHILD is also the PARENT of another
    mask table (via a DIFFERENT column, so the namespace-per-column
    uniqueness check does not collide with the admitted edge's own
    `person_id` binding) -- an outgoing edge from the child reaching a mask
    table."""
    accounts = pa.table(
        {
            "person_id": pa.array(["0", "1"], type=pa.string()),
            "acct_key": pa.array(["a0", "a1"], type=pa.string()),
        }
    )
    orders = pa.table({"acct_id": pa.array(["a0", "a1"], type=pa.string())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={
            "accounts": (accounts, _passthrough_columns("person_id", "acct_key")),
            "orders": (orders, _passthrough_columns("acct_id")),
        },
        relationships=[
            _fk_edge(),
            {
                "parent": {"table": "accounts", "columns": ["acct_key"]},
                "children": [{"table": "orders", "columns": ["acct_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_accounts",
            },
        ],
        row_count=2,
    )
    _assert_declines_with_no_dispatch(
        config,
        {"accounts": accounts, "orders": orders},
        monkeypatch,
        MIXED_FK_TOPOLOGY_UNSUPPORTED,
    )


def test_decline_unrelated_mask_to_mask_fk_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wholly separate FK edge between two OTHER mask tables (no relation
    to the admitted coupling at all) still declines the whole job."""
    accounts = pa.table({"person_id": pa.array(["0", "1"], type=pa.string())})
    warehouses = pa.table({"w_id": pa.array(["0", "1"], type=pa.string())})
    bins = pa.table({"w_ref": pa.array(["0", "1"], type=pa.string())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        mask_tables={
            "accounts": (accounts, _passthrough_columns("person_id")),
            "warehouses": (warehouses, _passthrough_columns("w_id")),
            "bins": (bins, _passthrough_columns("w_ref")),
        },
        relationships=[
            _fk_edge(),
            {
                "parent": {"table": "warehouses", "columns": ["w_id"]},
                "children": [{"table": "bins", "columns": ["w_ref"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_warehouses",
            },
        ],
        row_count=2,
    )
    _assert_declines_with_no_dispatch(
        config,
        {"accounts": accounts, "warehouses": warehouses, "bins": bins},
        monkeypatch,
        MIXED_FK_TOPOLOGY_UNSUPPORTED,
    )


# ---------------------------------------------------------------------------
# (e) Round-trip decline (A6): the 5b-i output-stability gate still applies
# to a coupled job's generated parent.
# ---------------------------------------------------------------------------


def test_decline_unstable_generated_parent_key_in_coupled_job(tmp_path: Path) -> None:
    accounts = pa.table({"person_id": pa.array(["0", "1"], type=pa.string())})
    config = _fk_mixed_config(
        tmp_path,
        generate_table="people",
        generate_columns=[
            {"name": "id", "type": "sequence", "start": 0, "step": 1, "null_probability": 1.0}
        ],
        mask_tables={"accounts": (accounts, _passthrough_columns("person_id"))},
        relationships=[_fk_edge()],
        row_count=2,
    )
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
# (f) Behavior-preservation, inertness/seam, and LOC pins (A7-A9).
# ---------------------------------------------------------------------------


def test_run_pipeline_fk_output_pinned_after_fk_resolve_extraction(tmp_path: Path) -> None:
    """A7: a pinned mask-only (no generate table) FK job exercises
    `run_pipeline` end to end through the extracted `_fk_resolve.py` helpers
    (`resolve_fk_keys`/`gather_errored_parent_keys`/`cascade_row_errors`,
    now imported by `_pandas_adapter.py` from their new home) -- exact
    expected values pinned by hand, so a behavioral drift from the
    verbatim-body extraction would fail this test."""
    parent = pa.table({"pk": pa.array(["p0", "p1", "p2"], type=pa.string())})
    child = pa.table({"fk": pa.array(["p0", "p1", "orphan"], type=pa.string())})
    config: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 20260917},
        "sources": {},
        "targets": {},
        "tables": [
            {"name": "parent", "columns": [{"name": "pk", "strategy": "passthrough"}]},
            {"name": "child", "columns": [{"name": "fk", "strategy": "passthrough"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "child", "columns": ["fk"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_pin",
            }
        ],
    }
    for name, source in (("parent", parent), ("child", child)):
        path = _write(tmp_path, source, name)
        config["sources"][name] = {"type": "file", "format": "parquet", "path": str(path)}
        config["targets"][name] = {
            "type": "file",
            "format": "parquet",
            "path": str(tmp_path / f"{name}.out.parquet"),
        }
    config = PipelineConfig.model_validate(config).model_dump()

    result = run_pipeline(
        config,
        {"parent": parent, "child": child},
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        native_route_enabled=False,
        key_provider=None,
        sink=None,
    )
    assert result.outputs["parent"].column("pk").to_pylist() == ["p0", "p1", "p2"]
    # passthrough is identity, and the orphan is PRESERVEd raw (unmasked).
    assert result.outputs["child"].column("fk").to_pylist() == ["p0", "p1", "orphan"]


def test_fk_resolve_and_stitch_sit_at_the_parent_execution_level() -> None:
    """A8: the shared FK-policy seam and the output-stitch helper both sit
    at the PARENT `execution` level (never under `execution.physical`), the
    seam reason both `_pandas_adapter.py` (a production module) and the
    shadow coordinator can import them without the disconnection sentry's
    allowlist ever needing to widen."""
    assert fk_resolve_module.__name__ == "decoy_engine.execution._fk_resolve"
    assert stitch_module.__name__ == "decoy_engine.execution._stitch"
    assert "execution.physical" not in (fk_resolve_module.__doc__ or "").split("import")[0]


def test_shadow_coordinator_stays_under_the_loc_cap() -> None:
    """A9: `_shadow_coordinator.py` stays under the 600-LOC orchestration
    cap (the FK dispatch logic itself lives in `_shadow_fk.py`); also
    enforced generally by `tests/sentry/test_module_size.py`."""
    module_path = Path(_coordinator_module.__file__)
    loc = sum(1 for _ in module_path.read_text().splitlines())
    assert loc < 600, f"_shadow_coordinator.py is {loc} LOC, over the 600-LOC cap"
