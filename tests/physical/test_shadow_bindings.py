"""Task 4.4 C0: `PhysicalNode.execution` binding construction."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import KeyBinding
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs

_ENGINE_VERSION = "shadow-bindings-test"


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _plan_for(tmp_path: Path, source: pa.Table, columns: list[dict]):
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": columns}],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=_ENGINE_VERSION)
    return compile_physical_plan(inputs)


def test_passthrough_node_gets_a_native_passthrough_binding(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    plan = _plan_for(tmp_path, source, [{"name": "c", "strategy": "passthrough"}])
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    binding = node.execution
    assert binding.operator_id == "native_passthrough"
    assert binding.key_binding is None
    assert binding.determinism_family is None
    assert binding.output_schema.field("c").type == pa.string()
    assert binding.required_prepasses == ()


def test_redact_node_binding(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    plan = _plan_for(tmp_path, source, [{"name": "c", "strategy": "redact"}])
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    assert node.execution.operator_id == "native_redact"
    assert node.execution.output_schema.field("c").type == pa.string()


def test_truncate_node_binding_resolves_keep_from_from_end(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["abcdef", "ghijkl"], type=pa.string())})
    plan = _plan_for(
        tmp_path,
        source,
        [{"name": "c", "strategy": "truncate", "provider_config": {"length": 4, "from_end": True}}],
    )
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    resolved = dict(node.execution.resolved_config)
    assert resolved["keep"] == "tail"
    assert resolved["length"] == 4


def test_hash_node_binding_carries_key_source_and_namespace_not_bytes(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    plan = _plan_for(tmp_path, source, [{"name": "c", "strategy": "hash", "namespace": "ns_1"}])
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    binding = node.execution
    assert binding.operator_id == "native_keyed_hash"
    assert binding.key_binding == KeyBinding(key_source="mask_key", namespace="ns_1")
    assert binding.determinism_family == "source_keyed_hmac"
    assert binding.determinism_version >= 0
    # No bytes/KeyProvider anywhere on the binding -- see
    # test_shadow_no_secret_serialization.py for the full recursive proof.
    for value in vars(binding).values():
        assert not isinstance(value, (bytes, bytearray))


def test_faker_node_is_not_slice_bound(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    plan = _plan_for(
        tmp_path,
        source,
        [
            {
                "name": "c",
                "strategy": "faker",
                "provider": "person_first_name",
                "namespace": "n",
            }
        ],
    )
    node = plan.tables[0].nodes[0]
    assert node.strategy == "faker"
    assert node.execution is None


def test_hash_without_namespace_is_not_bound(tmp_path: Path) -> None:
    """A hash column with no namespace is a config error the schema
    normally rejects; if it ever reached compilation as a scalar node
    regardless, it must never bind (no KeySource without a namespace)."""
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.execution.native._requirements import requirements_for
    from decoy_engine.execution.physical._shadow_bindings import execution_binding_for_slice_node
    from decoy_engine.plan._types import ColumnSeed

    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "c", "strategy": "passthrough"}]}],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=_ENGINE_VERSION)
    # A hand-built node bypassing schema validation, to exercise the guard directly.
    seed = ColumnSeed(
        namespace=None,
        strategy="hash",
        provider=None,
        backend_type="scalar",
        backend_version="1",
        cardinality_mode="reuse",
    )
    node = WorkNode(
        table="t", columns=("c",), kind="scalar", strategy="hash", provider=None, plan_slice=seed
    )
    requirements = requirements_for(node, plan=inputs.plan, profile=inputs.profile)
    binding = execution_binding_for_slice_node(
        node, table="t", inputs=inputs, requirements=requirements
    )
    assert binding is None
