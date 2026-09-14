"""D1/D2 unit tests for `PhysicalPlanInputs` (`plan_hash`) and the compiler's
free functions (`relationship_role`, `native_applies`,
`out_of_core_not_ready_reason`, `select_driver`) that
`test_compiler_preflight_equivalence.py`'s real-job corpus does not exercise
directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution.physical import (
    DriverId,
    NativeAdmissionFact,
    OutOfCoreRoutingFacts,
    capture_physical_plan_inputs,
)
from decoy_engine.execution.physical._compiler import (
    native_applies,
    out_of_core_not_ready_reason,
    relationship_role,
    select_driver,
)


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _flat_inputs(tmp_path: Path, **kwargs: Any) -> Any:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()
    return capture_physical_plan_inputs(config, {"t": source}, engine_version="unit-test", **kwargs)


def _fk_inputs(tmp_path: Path, **kwargs: Any) -> Any:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    pp = _write(tmp_path, parent, "parent")
    cp = _write(tmp_path, child, "child")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(pp)},
                "child": {"type": "file", "format": "parquet", "path": str(cp)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()
    return capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="unit-test", **kwargs
    )


# ---------------------------------------------------------------------------
# plan_hash (D1)
# ---------------------------------------------------------------------------


def test_plan_hash_stable_for_identical_inputs(tmp_path: Path) -> None:
    inputs_a = _flat_inputs(tmp_path)
    inputs_b = _flat_inputs(tmp_path)
    assert inputs_a.plan_hash() == inputs_b.plan_hash()


def test_plan_hash_changes_with_execution_mode(tmp_path: Path) -> None:
    inputs_auto = _flat_inputs(tmp_path)
    inputs_forced = _flat_inputs(tmp_path, execution_mode="full_frame")
    assert inputs_auto.plan_hash() != inputs_forced.plan_hash()


def test_plan_hash_changes_with_native_route_enabled(tmp_path: Path) -> None:
    inputs_off = _flat_inputs(tmp_path)
    inputs_on = _flat_inputs(tmp_path, native_route_enabled=True)
    assert inputs_off.plan_hash() != inputs_on.plan_hash()


def test_registry_fingerprint_stable_for_the_same_registry(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    assert inputs.registry_fingerprint() == inputs.registry_fingerprint()
    assert len(inputs.registry_fingerprint()) == 64  # sha256 hex digest


# ---------------------------------------------------------------------------
# relationship_role
# ---------------------------------------------------------------------------


def test_relationship_role_independent_for_flat_table(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    assert relationship_role("t", inputs.graph) == "independent"


def test_relationship_role_parent_and_child(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path)
    assert relationship_role("parent", inputs.graph) == "parent"
    assert relationship_role("child", inputs.graph) == "child"


# ---------------------------------------------------------------------------
# native_applies
# ---------------------------------------------------------------------------


def test_native_applies_false_when_disabled(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    assert native_applies(inputs) is False


def test_native_applies_true_when_enabled_with_mask_table(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path, native_route_enabled=True)
    assert native_applies(inputs) is True


# ---------------------------------------------------------------------------
# out_of_core_not_ready_reason
# ---------------------------------------------------------------------------


def test_out_of_core_not_ready_reason_reports_incompatible_code(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)  # no relationships -> not out_of_core-compatible
    facts = inputs.out_of_core_facts
    assert facts.compatible is False
    assert out_of_core_not_ready_reason(inputs) == (facts.reject_code or "out_of_core_incompatible")


def test_out_of_core_not_ready_reason_reports_below_threshold(tmp_path: Path) -> None:
    inputs = _fk_inputs(
        tmp_path, use_byte_estimate_routing=False, out_of_core_threshold_rows=1_000_000
    )
    facts = inputs.out_of_core_facts
    assert facts.compatible is True
    assert facts.largest_table_rows is not None
    reason = out_of_core_not_ready_reason(inputs)
    assert reason == f"out_of_core_below_threshold:{facts.largest_table_rows}"


# ---------------------------------------------------------------------------
# select_driver: RejectedAlternative content
# ---------------------------------------------------------------------------


def test_select_driver_full_frame_records_unattempted_native_and_attempted_chunked(
    tmp_path: Path,
) -> None:
    # auto_chunk defaults True (`_pipeline_finalize.AUTO_CHUNK_DEFAULT`), so
    # classify_job runs and declines chunked for the 2-row table (below the
    # default 100k auto-chunk threshold) -- chunked IS attempted here, just
    # not admitted; native never applies (native_route_enabled defaults False).
    inputs = _flat_inputs(tmp_path)
    selection = select_driver(inputs)
    assert selection.driver == DriverId.FULL_FRAME
    by_driver = {alt.driver: alt for alt in selection.rejected_alternatives}
    assert by_driver[DriverId.NATIVE_STREAM].attempted is False
    assert by_driver[DriverId.CHUNKED].attempted is True
    assert by_driver[DriverId.CHUNKED].reason == "chunked_source_below_threshold"
    # A non-FK table never had out_of_core/sequential as plausible
    # alternatives, so neither appears.
    assert DriverId.OUT_OF_CORE not in by_driver
    assert DriverId.SEQUENTIAL not in by_driver


def test_select_driver_native_stream_records_relationship_free_alternatives(tmp_path: Path) -> None:
    from decoy_engine.profile._readers import LazySource

    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(
        config,
        {"t": LazySource(path=path)},
        engine_version="unit-test",
        native_route_enabled=True,
    )
    selection = select_driver(inputs)
    assert selection.driver == DriverId.NATIVE_STREAM
    by_driver = {alt.driver: alt for alt in selection.rejected_alternatives}
    assert DriverId.OUT_OF_CORE not in by_driver
    assert DriverId.SEQUENTIAL not in by_driver
    # chunked declines here too: classify_job's per-table runtime gate
    # (`_planner._runtime_source_rejections`) rejects a LazySource outright.
    assert by_driver[DriverId.CHUNKED].attempted is True
    assert by_driver[DriverId.CHUNKED].reason == "chunked_lazy_source_unsupported"


def test_select_driver_sequential_records_out_of_core_alternative(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path, use_byte_estimate_routing=False)
    selection = select_driver(inputs)
    assert selection.driver == DriverId.SEQUENTIAL
    assert len(selection.rejected_alternatives) == 1
    assert selection.rejected_alternatives[0].driver == DriverId.OUT_OF_CORE
    assert selection.rejected_alternatives[0].attempted is True


def test_select_driver_out_of_core_records_no_alternatives(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path, execution_mode="out_of_core")
    selection = select_driver(inputs)
    assert selection.driver == DriverId.OUT_OF_CORE
    assert selection.rejected_alternatives == ()


# ---------------------------------------------------------------------------
# NativeAdmissionFact / OutOfCoreRoutingFacts construction sanity
# ---------------------------------------------------------------------------


def test_native_admission_fact_disabled_sentinel_is_consistent(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    admission = inputs.native_admission
    assert admission.static_candidate is False
    assert admission.admitted is False
    assert admission.reason == "native_route_disabled_or_no_mask_table"
    assert admission.table is None
    assert admission.lane is None


def test_out_of_core_facts_is_frozen(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path)
    facts = inputs.out_of_core_facts
    assert isinstance(facts, OutOfCoreRoutingFacts)
    try:
        facts.compatible = not facts.compatible  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("OutOfCoreRoutingFacts must be frozen")


def test_native_admission_fact_is_frozen() -> None:
    fact = NativeAdmissionFact(
        table=None,
        static_candidate=False,
        static_reason=None,
        sink_mode=None,
        lane=None,
        admitted=False,
        reason=None,
    )
    try:
        fact.admitted = True  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("NativeAdmissionFact must be frozen")
