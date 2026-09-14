"""Dense, exact-value unit tests for the pure helper functions in
`execution/physical/_compiler.py` that back the mutation-testing bar (plan
R2: "coverage + branch + MUTATION on the compiler decision logic; routing-
decision mutants must be killed").

`_native_rejected_entry` / `_chunked_rejected_entry` take primitives
(`NativeAdmissionFact`, `ExecutionPlan | None`) directly, so they are tested
here without building a full `PhysicalPlanInputs` -- fast, and precise about
which exact field value each branch must produce (a Yoda-value mutation like
`route_reason` -> `None` is invisible to a membership assertion but not to
an equality one).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._planner import ExecutionPlan
from decoy_engine.execution.physical import (
    DriverId,
    NativeAdmissionFact,
    capture_physical_plan_inputs,
)
from decoy_engine.execution.physical._compiler import (
    DriverSelection,
    _chunked_rejected_entry,
    _native_rejected_entry,
    _relationship_alternatives,
    compile_physical_plan,
    out_of_core_not_ready_reason,
    select_driver,
)
from decoy_engine.execution.physical._inputs import OutOfCoreRoutingFacts
from decoy_engine.execution.physical._plan import RejectedAlternative
from decoy_engine.profile._readers import LazySource

# ---------------------------------------------------------------------------
# _native_rejected_entry
# ---------------------------------------------------------------------------


def _admission(*, admitted: bool = False, reason: str | None = None) -> NativeAdmissionFact:
    return NativeAdmissionFact(
        table="t",
        static_candidate=True,
        static_reason=None,
        sink_mode="resident",
        lane="utf8_only",
        admitted=admitted,
        reason=reason,
    )


def test_native_rejected_entry_not_applicable() -> None:
    entry = _native_rejected_entry(False, _admission())
    assert entry == RejectedAlternative(
        DriverId.NATIVE_STREAM, "native_route_disabled_or_no_mask_table", attempted=False
    )


def test_native_rejected_entry_applies_with_reason() -> None:
    entry = _native_rejected_entry(True, _admission(reason="unsupported_strategy:col:faker"))
    assert entry == RejectedAlternative(
        DriverId.NATIVE_STREAM, "unsupported_strategy:col:faker", attempted=True
    )


def test_native_rejected_entry_applies_with_no_reason_falls_back() -> None:
    entry = _native_rejected_entry(True, _admission(admitted=False, reason=None))
    assert entry == RejectedAlternative(
        DriverId.NATIVE_STREAM, "native_admission_declined", attempted=True
    )


# ---------------------------------------------------------------------------
# _chunked_rejected_entry
# ---------------------------------------------------------------------------


def test_chunked_rejected_entry_no_decision() -> None:
    entry = _chunked_rejected_entry(None)
    assert entry == RejectedAlternative(
        DriverId.CHUNKED, "auto_chunk_disabled_or_no_mask_table", attempted=False
    )


def test_chunked_rejected_entry_decision_chunked() -> None:
    decision = ExecutionPlan(mode="chunked", rejections={}, reason="chunk-admissible")
    entry = _chunked_rejected_entry(decision)
    assert entry == RejectedAlternative(DriverId.CHUNKED, "chunked_admitted", attempted=True)


def test_chunked_rejected_entry_decision_rejected_uses_chunked_key() -> None:
    decision = ExecutionPlan(
        mode="pandas_fallback",
        rejections={"chunked": "no mask-kind tables to stream"},
        reason="fallback",
    )
    entry = _chunked_rejected_entry(decision)
    assert entry == RejectedAlternative(DriverId.CHUNKED, "no_mask_tables", attempted=True)


def test_chunked_rejected_entry_decision_rejected_falls_back_to_reason() -> None:
    decision = ExecutionPlan(
        mode="pandas_fallback", rejections={}, reason="no mask-kind tables to stream"
    )
    entry = _chunked_rejected_entry(decision)
    assert entry == RejectedAlternative(DriverId.CHUNKED, "no_mask_tables", attempted=True)


# ---------------------------------------------------------------------------
# _relationship_alternatives + select_driver / compile_physical_plan
# end-to-end EXACT assertions (route_reason threading, not just driver id).
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _fk_config(tmp_path: Path, parent: pa.Table, child: pa.Table) -> dict[str, Any]:
    parent_path = _write(tmp_path, parent, "parent")
    child_path = _write(tmp_path, child, "child")
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
                "child": {"type": "file", "format": "parquet", "path": str(child_path)},
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


def test_relationship_alternatives_byte_estimate_fit_shares_reason_on_both_entries(
    tmp_path: Path,
) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="mutation-coverage"
    )
    alts = _relationship_alternatives(inputs, "byte_estimate_full_frame_fits")
    assert alts == (
        RejectedAlternative(DriverId.OUT_OF_CORE, "byte_estimate_full_frame_fits", attempted=True),
        RejectedAlternative(DriverId.SEQUENTIAL, "byte_estimate_full_frame_fits", attempted=True),
    )


def test_relationship_alternatives_not_preferred_uses_out_of_core_not_ready_reason(
    tmp_path: Path,
) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config,
        {"parent": parent, "child": child},
        engine_version="mutation-coverage",
        use_byte_estimate_routing=False,
    )
    alts = _relationship_alternatives(inputs, "pure_mask_fk")
    assert alts == (
        RejectedAlternative(DriverId.OUT_OF_CORE, "out_of_core_below_threshold:2", attempted=True),
        RejectedAlternative(DriverId.SEQUENTIAL, "pure_mask_fk", attempted=True),
    )


def test_relationship_alternatives_no_relationships_is_empty(tmp_path: Path) -> None:
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
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version="mutation-coverage")
    assert _relationship_alternatives(inputs, "no_relationships") == ()


# ---------------------------------------------------------------------------
# select_driver unreachable-precondition branch: force the contradiction
# (route_chunked True with decision None) via monkeypatch, purely to kill
# the defensive AssertionError's mutants -- unreachable through any real
# input, since `layer2_chunk_decision` only returns `route_chunked=True`
# together with a non-None `decision`.
# ---------------------------------------------------------------------------


def test_select_driver_raises_on_impossible_route_chunked_without_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version="mutation-coverage")
    monkeypatch.setattr(
        "decoy_engine.execution.physical._compiler.layer2_chunk_decision",
        lambda _inputs: (None, True),
    )
    with pytest.raises(AssertionError) as excinfo:
        select_driver(inputs)
    # Exact (anchored) equality, not `pytest.raises(match=...)`'s `re.search`
    # substring semantics: a `"XX...XX"`-wrapped or upper-cased mutation of
    # the message would still satisfy a substring/prefix match.
    assert str(excinfo.value) == "route_chunked is True but classify_job produced no decision"


# ---------------------------------------------------------------------------
# compile_physical_plan / _build_nodes: exact PhysicalNode field assertions.
# ---------------------------------------------------------------------------


def test_compile_physical_plan_node_fields_for_a_scalar_kernel_strategy(tmp_path: Path) -> None:
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
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version="mutation-coverage")
    plan = compile_physical_plan(inputs)
    assert len(plan.tables) == 1
    table = plan.tables[0]
    assert len(table.nodes) == 1
    node = table.nodes[0]
    assert node.table == "t"
    assert node.columns == ("note",)
    assert node.kind == "scalar"
    assert node.strategy == "redact"
    assert node.node_id == "t:note:scalar:redact"
    assert node.fallback_policy in ("native", "python_only")
    assert node.provider_class is None  # redact has no `provider`


def test_compile_physical_plan_node_provider_class_for_faker_strategy(tmp_path: Path) -> None:
    source = pa.table({"name": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "name",
                            "strategy": "faker",
                            "provider": "person_first_name",
                            "namespace": "n",
                        }
                    ],
                }
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version="mutation-coverage")
    plan = compile_physical_plan(inputs)
    node = plan.tables[0].nodes[0]
    assert node.strategy == "faker"
    assert node.provider_class in ("pool_native", "python_only", "reject_large")


def test_compile_physical_plan_synthesis_none_for_pure_mask_job(tmp_path: Path) -> None:
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
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version="mutation-coverage")
    plan = compile_physical_plan(inputs)
    assert plan.synthesis is None
    assert plan.engine_version == "mutation-coverage"


def test_compile_physical_plan_no_tables_for_pure_generate_job(tmp_path: Path) -> None:
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {},
            "targets": {
                "people": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "people.out.csv"),
                },
            },
            "tables": [
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                },
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(config, {}, engine_version="mutation-coverage")
    plan = compile_physical_plan(inputs)
    assert plan.tables == ()
    assert plan.synthesis is not None
    assert plan.synthesis.tables == ("people",)


# ---------------------------------------------------------------------------
# select_driver: exact reason/detail assertions per branch (not just driver
# identity), across the full corpus this module already exercises via
# capture_physical_plan_inputs.
# ---------------------------------------------------------------------------


def test_select_driver_full_frame_reason_and_detail_are_route_reason(tmp_path: Path) -> None:
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
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version="mutation-coverage")
    selection = select_driver(inputs)
    assert selection.driver == DriverId.FULL_FRAME
    assert selection.reason == "no_relationships"
    assert selection.reason_detail is None


def test_select_driver_chunked_reason_detail_is_decision_reason(tmp_path: Path) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
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
        {"t": source},
        engine_version="mutation-coverage",
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=3,
    )
    selection = select_driver(inputs)
    assert selection.driver == DriverId.CHUNKED
    assert selection.reason == "chunked_admitted"
    assert selection.reason_detail is not None
    assert "chunk-safe" in selection.reason_detail


def test_select_driver_native_stream_reason_and_detail(tmp_path: Path) -> None:
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
        engine_version="mutation-coverage",
        native_route_enabled=True,
    )
    selection = select_driver(inputs)
    assert selection.driver == DriverId.NATIVE_STREAM
    assert selection.reason == "native_admission_admitted"
    assert selection.reason_detail is None


# ---------------------------------------------------------------------------
# select_driver: FULL DriverSelection equality (every field, including
# `rejected_alternatives` content) per branch -- catches a mutated arg
# threaded into a helper call that a driver-id-only assertion would miss.
# ---------------------------------------------------------------------------


def test_select_driver_sequential_full_selection_equality(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config,
        {"parent": parent, "child": child},
        engine_version="mutation-coverage",
        use_byte_estimate_routing=False,
    )
    selection = select_driver(inputs)
    assert selection == DriverSelection(
        DriverId.SEQUENTIAL,
        "pure_mask_fk",
        None,
        (
            RejectedAlternative(
                DriverId.OUT_OF_CORE, "out_of_core_below_threshold:2", attempted=True
            ),
        ),
    )


def test_select_driver_out_of_core_full_selection_equality(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config,
        {"parent": parent, "child": child},
        engine_version="mutation-coverage",
        execution_mode="out_of_core",
    )
    selection = select_driver(inputs)
    assert selection == DriverSelection(DriverId.OUT_OF_CORE, "override_out_of_core", None, ())


def test_select_driver_full_frame_with_relationships_full_selection_equality(
    tmp_path: Path,
) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="mutation-coverage"
    )
    selection = select_driver(inputs)
    assert selection == DriverSelection(
        DriverId.FULL_FRAME,
        "byte_estimate_full_frame_fits",
        None,
        (
            RejectedAlternative(
                DriverId.OUT_OF_CORE, "byte_estimate_full_frame_fits", attempted=True
            ),
            RejectedAlternative(
                DriverId.SEQUENTIAL, "byte_estimate_full_frame_fits", attempted=True
            ),
            RejectedAlternative(
                DriverId.NATIVE_STREAM, "native_route_disabled_or_no_mask_table", attempted=False
            ),
            RejectedAlternative(
                DriverId.CHUNKED,
                "masks_one_table_per_run;chunked_relationships_unsupported",
                attempted=True,
            ),
        ),
    )


def test_select_driver_chunked_full_selection_equality(tmp_path: Path) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
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
        {"t": source},
        engine_version="mutation-coverage",
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=3,
    )
    selection = select_driver(inputs)
    assert selection.driver == DriverId.CHUNKED
    assert selection.reason == "chunked_admitted"
    assert selection.rejected_alternatives == (
        RejectedAlternative(
            DriverId.NATIVE_STREAM, "native_route_disabled_or_no_mask_table", attempted=False
        ),
    )


# ---------------------------------------------------------------------------
# out_of_core_not_ready_reason: direct calls with hand-crafted facts (via
# `dataclasses.replace`) to cover every branch, including the "ready"
# terminal branch that is unreachable through `select_driver` itself (a
# route that IS out_of_core-ready never calls this helper -- see the
# function's own `# pragma: no cover` note).
# ---------------------------------------------------------------------------


def test_out_of_core_not_ready_reason_no_size_signal(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="mutation-coverage"
    )
    from dataclasses import replace

    no_size_facts = replace(inputs.out_of_core_facts, compatible=True, largest_table_rows=None)
    inputs = replace(inputs, out_of_core_facts=no_size_facts)
    assert out_of_core_not_ready_reason(inputs) == "out_of_core_no_size_signal"


def test_out_of_core_not_ready_reason_exact_threshold_boundary(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="mutation-coverage"
    )
    from dataclasses import replace

    at_threshold_facts = replace(
        inputs.out_of_core_facts, compatible=True, largest_table_rows=1_000
    )
    inputs = replace(inputs, out_of_core_facts=at_threshold_facts, out_of_core_threshold_rows=1_000)
    # rows == threshold: NOT below threshold (`<`, not `<=`), so this falls
    # through to the terminal "ready" branch -- unreachable via select_driver
    # (a truly-ready OOC job never calls this helper), but a real return
    # value of this free function nonetheless.
    assert out_of_core_not_ready_reason(inputs) == "out_of_core_ready"


def test_out_of_core_not_ready_reason_below_threshold_boundary(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="mutation-coverage"
    )
    from dataclasses import replace

    below_facts = replace(inputs.out_of_core_facts, compatible=True, largest_table_rows=999)
    inputs = replace(inputs, out_of_core_facts=below_facts, out_of_core_threshold_rows=1_000)
    assert out_of_core_not_ready_reason(inputs) == "out_of_core_below_threshold:999"


def test_out_of_core_routing_facts_replace_smoke() -> None:
    """Sanity check that `OutOfCoreRoutingFacts` is a plain frozen dataclass
    `dataclasses.replace` can target (guards the two tests above against a
    silent no-op if the type ever stops being a dataclass)."""
    from dataclasses import replace

    facts = OutOfCoreRoutingFacts(
        compatible=False,
        reject_code=None,
        largest_table_rows=None,
        largest_table_rows_exact=True,
        full_frame_fits_estimate=None,
        probe_recovers_full_frame=None,
        budget_bytes=None,
        reorder_threshold_rows=2_000_000,
    )
    replaced = replace(facts, compatible=True)
    assert replaced.compatible is True
    assert facts.compatible is False


# ---------------------------------------------------------------------------
# select_driver: native `applies=True` but declined, distinguishing
# `_native_rejected_entry(applies, ...)` from a mutated `None` passthrough
# (`not None` is also truthy, so a scenario where `applies` is already False
# cannot tell the two apart -- this one makes native genuinely APPLY and
# decline, so `not applies` (`False`) and `not None` (`True`) diverge).
# ---------------------------------------------------------------------------


def test_select_driver_full_frame_native_applies_but_declines_resident_source(
    tmp_path: Path,
) -> None:
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
    # A resident pa.Table (not a LazySource) declines native with
    # "non_lazy_source" even though native_route_enabled=True makes it
    # APPLY; auto_chunk stays off the default threshold so full_frame wins.
    inputs = capture_physical_plan_inputs(
        config, {"t": source}, engine_version="mutation-coverage", native_route_enabled=True
    )
    selection = select_driver(inputs)
    assert selection.driver == DriverId.FULL_FRAME
    by_driver = {alt.driver: alt for alt in selection.rejected_alternatives}
    assert by_driver[DriverId.NATIVE_STREAM] == RejectedAlternative(
        DriverId.NATIVE_STREAM, "non_lazy_source", attempted=True
    )


def test_select_driver_chunked_native_applies_but_declines_resident_source(tmp_path: Path) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
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
        {"t": source},
        engine_version="mutation-coverage",
        native_route_enabled=True,
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=3,
    )
    selection = select_driver(inputs)
    assert selection.driver == DriverId.CHUNKED
    by_driver = {alt.driver: alt for alt in selection.rejected_alternatives}
    assert by_driver[DriverId.NATIVE_STREAM] == RejectedAlternative(
        DriverId.NATIVE_STREAM, "non_lazy_source", attempted=True
    )


# ---------------------------------------------------------------------------
# _build_nodes: a multi-table job proves the per-table filter does not
# short-circuit the whole work list (a `continue` -> `break` mutation would
# silently return zero nodes for a table whose matching entries are not
# first in `build_work_list`'s output).
# ---------------------------------------------------------------------------


def test_build_nodes_does_not_short_circuit_across_tables(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="mutation-coverage"
    )
    plan = compile_physical_plan(inputs)
    by_table = {table.table: table for table in plan.tables}
    assert len(by_table["parent"].nodes) == 1
    assert len(by_table["child"].nodes) == 1
    assert by_table["parent"].nodes[0].columns == ("id",)
    assert by_table["child"].nodes[0].columns == ("pid",)


# ---------------------------------------------------------------------------
# compile_physical_plan: every PhysicalTable / PhysicalPlan field wired from
# the right source, checked by exact value (a field silently swapped for
# `None`, or `relationship_role` called with the wrong table name, is
# otherwise invisible to a test that only checks `.driver`).
# ---------------------------------------------------------------------------


def test_compile_physical_plan_wires_every_physical_table_field(tmp_path: Path) -> None:
    # A chunked-admitted job, so `selection.reason_detail` is a real non-None
    # string (`decision.reason`) -- a `driver_reason_detail=None` mutation
    # would be invisible on a scenario where that field is None regardless.
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
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
        {"t": source},
        engine_version="mutation-coverage",
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=3,
    )
    selection = select_driver(inputs)
    plan = compile_physical_plan(inputs)
    table = plan.tables[0]
    assert table.driver_reason == selection.reason
    assert table.driver_reason_detail == selection.reason_detail
    assert table.driver_reason_detail is not None
    assert table.rejected_alternatives == selection.rejected_alternatives
    assert table.relationship_role == "independent"
    assert table.substrate == "pandas"
    assert plan.plan_hash == inputs.plan_hash()
    assert plan.plan_hash != ""


def test_compile_physical_plan_relationship_role_uses_the_right_table_name(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="mutation-coverage"
    )
    plan = compile_physical_plan(inputs)
    by_table = {table.table: table for table in plan.tables}
    # A `relationship_role(None, ...)` mutation would raise inside
    # `relationship_role` (comparing `None` against edge table names is
    # fine, but produces "independent" for BOTH tables regardless of their
    # real role) -- assert each table gets its OWN, distinct role.
    assert by_table["parent"].relationship_role == "parent"
    assert by_table["child"].relationship_role == "child"


# ---------------------------------------------------------------------------
# layer2_chunk_decision: `auto_chunk and has_mask_table` vs. a mutated `or`
# -- a pure-generate job (has_mask_table=False) with auto_chunk left at its
# True default must short-circuit to `(None, False)` WITHOUT ever calling
# `classify_job`; the mutated `or` would call it instead (`decision` would
# come back non-None even though `route_chunked` still ends up False either
# way, since a job with no mask tables never classifies as `"chunked"`).
# ---------------------------------------------------------------------------


def test_layer2_chunk_decision_short_circuits_for_pure_generate_job(tmp_path: Path) -> None:
    from decoy_engine.execution.physical._compiler import layer2_chunk_decision

    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {},
            "targets": {
                "people": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "people.out.csv"),
                },
            },
            "tables": [
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                },
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(config, {}, engine_version="mutation-coverage")
    assert inputs.has_mask_table is False
    assert inputs.auto_chunk is True  # the production default
    decision, route_chunked = layer2_chunk_decision(inputs)
    assert decision is None
    assert route_chunked is False
