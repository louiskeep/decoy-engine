"""Shared test-only harness for the Task 4.4 shadow-vs-oracle comparison
(C3): builds the C0-extended `PhysicalPlan` the same way `_helpers.py` builds
a Task 4.2/4.3 job, runs the `ShadowCoordinator` over the resident C5
snapshot, runs the pinned pandas oracle over the SAME resident source
object, and asserts the two are identical -- coded per
`_shadow_diff_codes.py`, never a bare `AssertionError`.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import PhysicalPlan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator, ShadowRunResult
from decoy_engine.execution.physical._shadow_diff_codes import (
    CELL_VALUE_DIFF,
    DIAGNOSTICS_DIFF,
    NULL_MASK_DIFF,
    ROW_COUNT_DIFF,
    ROW_ORDER_DIFF,
    SCHEMA_DIFF,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.keyprovider import KeyProvider

ENGINE_VERSION = "shadow-coordinator-4.4"


def write_read_only_fixture(tmp_path: Path, table: pa.Table, name: str) -> Path:
    """Write `table` once, then chmod it read-only (C5's "the harness ALSO
    writes the fixture once, then makes it read-only for the duration, so an
    accidental in-test rewrite is impossible")."""
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    os.chmod(path, 0o444)
    return path


def build_config(
    tmp_path: Path,
    table_name: str,
    source_path: Path,
    columns: list[dict[str, Any]],
    *,
    seed: int = 20260914,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {table_name: {"type": "file", "format": "parquet", "path": str(source_path)}},
        "targets": {
            table_name: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{table_name}.out.parquet"),
            }
        },
        "tables": [{"name": table_name, "columns": columns}],
    }
    return PipelineConfig.model_validate(raw).model_dump()


@dataclass(frozen=True)
class ShadowRun:
    plan: PhysicalPlan
    shadow: ShadowRunResult
    oracle: ExecutionResult
    shadow_identity: str


def run_shadow_and_oracle(
    config: dict[str, Any],
    table_name: str,
    source: pa.Table,
    *,
    key_provider: KeyProvider | None = None,
    batch_size_rows: int = 50_000,
) -> ShadowRun:
    """Run the shadow coordinator and the pinned oracle over the SAME
    resident `source` object (C5's same-input proof: both sides are handed
    the identical `pa.Table` instance)."""
    inputs = capture_physical_plan_inputs(
        config, {table_name: source}, engine_version=ENGINE_VERSION
    )
    plan = compile_physical_plan(inputs)

    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=key_provider, batch_size_rows=batch_size_rows
    )
    snapshot = capture_shadow_snapshot({table_name: source})
    shadow_result = ShadowCoordinator(ctx=ctx).run(plan, snapshot)

    oracle_result = run_pipeline(
        config,
        {table_name: source},
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        native_route_enabled=False,
        key_provider=key_provider,
        sink=None,
    )

    return ShadowRun(
        plan=plan,
        shadow=shadow_result,
        oracle=oracle_result,
        shadow_identity=snapshot.identity(table_name),
    )


def _diag_key(item: Any) -> tuple[Any, ...]:
    # Order-independent multiset key for a warning or row-error record: every
    # field except a wall-clock timing one (this slice's zero-diagnostic
    # strategies never emit either, but the key stays generic on purpose).
    return tuple(sorted(vars(item).items())) if hasattr(item, "__dict__") else (repr(item),)


def assert_diagnostics_multisets_equal(
    shadow_items: tuple[Any, ...], oracle_items: tuple[Any, ...], label: str
) -> None:
    got, want = (
        Counter(_diag_key(i) for i in shadow_items),
        Counter(_diag_key(i) for i in oracle_items),
    )
    if got != want:
        raise ShadowDifference(
            code=DIAGNOSTICS_DIFF,
            detail=f"{label}: missing={list((want - got).elements())} extra={list((got - want).elements())}",
        )


def assert_shadow_matches_oracle(run: ShadowRun) -> None:
    """The C3 exit-gate comparison: value/null/order/row-count/schema hard
    failures, diagnostics as order-independent multisets, and (already
    enforced inside `ShadowCoordinator.run` itself) planned==actual operator
    per node. Raises the first coded `ShadowDifference` found.
    """
    # Same-input is a property of the fixture, not a cross-check here: the
    # harness hands the IDENTICAL resident `pa.Table` object to both the shadow
    # and the oracle, and the fixture file is made read-only (see
    # `run_shadow_and_oracle` / the read-only fixture test). Re-hashing that one
    # `source` for "both sides" would be tautological (dennis MEDIUM-2), so the
    # snapshot digest is recorded (`run.shadow_identity`) but not cross-asserted
    # here; the value/null/order/row-count/schema hard-compare below is what
    # actually proves the two masked identical bytes. `SNAPSHOT_IDENTITY_DIFF`
    # stays a catalog code for the Task 4.5 single-open production reader, where
    # the two sides read the source independently and the check is non-circular.
    assert_diagnostics_multisets_equal(run.shadow.warnings, tuple(run.oracle.warnings), "warnings")
    assert_diagnostics_multisets_equal(
        run.shadow.row_errors, tuple(run.oracle.row_errors), "row_errors"
    )

    shadow_tables, oracle_tables = set(run.shadow.outputs), set(run.oracle.outputs)
    if shadow_tables != oracle_tables:
        raise ShadowDifference(
            code=SCHEMA_DIFF,
            detail=f"output table set differs: shadow={sorted(shadow_tables)} oracle={sorted(oracle_tables)}",
        )

    for table in sorted(oracle_tables):
        candidate = run.shadow.outputs[table]
        oracle = run.oracle.outputs[table]
        if candidate.column_names != oracle.column_names:
            raise ShadowDifference(
                code=SCHEMA_DIFF,
                detail=f"{table}: column names/order differ: shadow={candidate.column_names} oracle={oracle.column_names}",
            )
        if candidate.num_rows != oracle.num_rows:
            raise ShadowDifference(
                code=ROW_COUNT_DIFF,
                detail=f"{table}: shadow={candidate.num_rows} oracle={oracle.num_rows}",
            )
        for name in oracle.column_names:
            oracle_type = oracle.schema.field(name).type
            candidate_type = candidate.schema.field(name).type
            if not oracle_type.equals(candidate_type):
                raise ShadowDifference(
                    code=SCHEMA_DIFF,
                    detail=f"{table}.{name}: shadow={candidate_type} oracle={oracle_type}",
                )
            oracle_values = oracle.column(name).to_pylist()
            candidate_values = candidate.column(name).to_pylist()
            if candidate_values == oracle_values:
                continue
            oracle_null = [v is None for v in oracle_values]
            candidate_null = [v is None for v in candidate_values]
            if candidate_null != oracle_null:
                raise ShadowDifference(
                    code=NULL_MASK_DIFF, detail=f"{table}.{name}: null positions differ"
                )
            if sorted(map(repr, candidate_values)) == sorted(map(repr, oracle_values)):
                raise ShadowDifference(
                    code=ROW_ORDER_DIFF, detail=f"{table}.{name}: same values, different order"
                )
            raise ShadowDifference(code=CELL_VALUE_DIFF, detail=f"{table}.{name}: values differ")


def assert_route_evidence_matches_plan(run: ShadowRun) -> None:
    """Planned==actual operator per node (C3), asserted independently of the
    coordinator's own internal check, directly against the frozen plan."""
    for table in run.plan.tables:
        for node in table.nodes:
            if node.execution is None:
                continue
            evidence = run.shadow.route_evidence[node.node_id]
            assert evidence.planned_operator == node.execution.operator_id
            assert evidence.actual_operator == node.execution.operator_id
            assert evidence.executed is True
            if node.execution.operator_id == "native_keyed_hash":
                # Positive Rust-call evidence (C2): a hash node must show the
                # compiled kernel actually ran, never inferred from success alone.
                assert evidence.compiled_kernel_executed is True


def assert_every_node_bound(plan: PhysicalPlan) -> None:
    """Every configured slice-strategy node must have reached native
    admission (`node.execution is not None`); an admission miss would let
    the coordinator silently skip a column instead of exercising it, making
    the corpus's parity claim vacuous for that column."""
    for table in plan.tables:
        for node in table.nodes:
            assert node.execution is not None, (
                f"{node.node_id!r} (strategy={node.strategy!r}) did not reach native "
                "admission; the acceptance corpus must only use native-admissible configs"
            )
