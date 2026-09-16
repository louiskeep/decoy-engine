"""Task 4.4 C0 (extended by Task 4.6 slice 1): `PhysicalNode.execution`
binding construction, including the deterministic-faker admission
predicate."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

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
    assert binding.operator_reason == "slice_native_admitted:passthrough"
    assert binding.key_binding is None
    assert binding.determinism_family is None
    assert binding.output_schema.field("c").type == pa.string()
    assert binding.input_schema.field("c").type == pa.string()
    assert binding.diagnostic_obligations == ()
    assert binding.required_prepasses == ()
    assert binding.batch_estimate == 2


def test_binding_resolves_the_real_input_arrow_type_not_always_string(tmp_path: Path) -> None:
    """Guards `resolve_input_arrow_type`'s call arguments and the `or
    pa.string()` fallback: a real int column must resolve to int64 on
    `input_schema`, never be forced to string."""
    source = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())})
    plan = _plan_for(tmp_path, source, [{"name": "c", "strategy": "passthrough"}])
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    assert node.execution.input_schema.field("c").type == pa.int64()
    assert node.execution.batch_estimate == 3


def test_hash_int_binding_resolves_int_input_type(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([1, 22, 333], type=pa.int64())})
    plan = _plan_for(tmp_path, source, [{"name": "c", "strategy": "hash", "namespace": "n"}])
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    assert node.execution.input_schema.field("c").type == pa.int64()


def test_redact_with_non_string_value_is_not_slice_bound(tmp_path: Path) -> None:
    """`redact_with` must be a string for the native kernel (`native_redact`
    pins its output to `pa.string()`); a non-string value fails the
    config gate (`fallback_policy="python_only"`) while `redact`'s static
    output type stays determinate -- the exact combination that could slip
    past an `and`-weakened admission guard."""
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    plan = _plan_for(
        tmp_path,
        source,
        [{"name": "c", "strategy": "redact", "provider_config": {"redact_with": 123}}],
    )
    node = plan.tables[0].nodes[0]
    assert node.fallback_policy == "python_only"
    assert node.execution is None


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


# ---------------------------------------------------------------------------
# Task 4.6 slice 1: the deterministic-faker admission predicate (§3.2). Every
# miss below leaves the node unbound; only a single-table, non-FK, C1-
# allowlisted, string-sourced, when/vault-free faker node binds.
# ---------------------------------------------------------------------------


def _faker_column(
    name: str = "c",
    *,
    provider: str = "person_first_name",
    namespace: str = "ns_faker",
    pool_size: int = 30,
    **extra,
) -> dict:
    col = {
        "name": name,
        "strategy": "faker",
        "provider": provider,
        "deterministic": True,
        "namespace": namespace,
        "pool_size": pool_size,
    }
    col.update(extra)
    return col


def test_deterministic_reuse_c1_faker_column_binds(tmp_path: Path) -> None:
    """The positive control: a compliant single-table, non-FK, C1-allowlisted,
    string-sourced faker column reaches admission with both bindings set."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    plan = _plan_for(tmp_path, source, [_faker_column()])
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    binding = node.execution
    assert binding.operator_id == "native_faker_select"
    assert binding.key_binding == KeyBinding(key_source="mask_key", namespace="ns_faker")
    assert binding.pool_binding is not None
    assert binding.pool_binding.provider == "person_first_name"
    assert binding.pool_binding.plan_pool_size == 30
    # The required_prepasses hard-fail guard (a faker node declaring one
    # would raise AssertionError rather than silently bind) stays
    # satisfied: faker's own capabilities never declare a prepass.
    assert binding.required_prepasses == ()


def test_faker_when_gated_column_is_unbound(tmp_path: Path) -> None:
    """`ColumnConfig` has no `when` field (`extra="forbid"`), so a validated
    config can never carry one -- but `compile_plan`/`_seed_envelope.py` read
    it straight off the raw dict, so a hand-mutated post-dump config still
    exercises the guard for real, matching `_phase3_eligibility`'s own
    when-honoring contract."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [_faker_column()]}],
        }
    ).model_dump()
    config["tables"][0]["columns"][0]["when"] = "c == 'a'"
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=_ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    node = plan.tables[0].nodes[0]
    assert node.execution is None


def test_faker_vault_column_is_unbound(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    plan = _plan_for(tmp_path, source, [_faker_column(vault=True)])
    node = plan.tables[0].nodes[0]
    assert node.execution is None


def test_faker_non_poolable_provider_never_compiles(tmp_path: Path) -> None:
    """`uuid` is a real registered Faker provider with `poolable=False`
    (`_provider_class.py`'s python_only class); `check_non_poolable_provider_
    with_pool_backend` (`plan/_checks.py`) already rejects this combination
    at PLAN COMPILE, before `execution_binding_for_slice_node` ever runs --
    the same "enforced upstream too" shape as the namespace-less hash case
    above."""
    from decoy_engine.plan._errors import PlanCompileError

    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    with pytest.raises(PlanCompileError, match="non_poolable_provider_with_pool_backend"):
        _plan_for(tmp_path, source, [_faker_column(provider="uuid")])


def test_faker_non_poolable_provider_hand_built_node_is_unbound(tmp_path: Path) -> None:
    """Defense in depth for the compile-time guard above: a hand-built
    `WorkNode` bypassing `check_non_poolable_provider_with_pool_backend`
    (mirroring `test_hash_without_namespace_is_not_bound`'s pattern) must
    still be rejected by the shared predicate's own poolable/allowlist
    check, not silently bound because an earlier guard usually catches it."""
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.execution.native._requirements import requirements_for
    from decoy_engine.execution.physical._shadow_bindings import execution_binding_for_slice_node
    from decoy_engine.plan._types import ColumnSeed

    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    plan_inputs_source = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "t": {"type": "file", "format": "parquet", "path": str(plan_inputs_source)}
            },
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "c", "strategy": "passthrough"}]}],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=_ENGINE_VERSION)
    seed = ColumnSeed(
        namespace="ns_faker",
        strategy="faker",
        provider="uuid",
        backend_type="faker",
        backend_version="",
        cardinality_mode="reuse",
        deterministic=True,
        pool_size=30,
    )
    node = WorkNode(
        table="t", columns=("c",), kind="scalar", strategy="faker", provider="uuid", plan_slice=seed
    )
    requirements = requirements_for(node, plan=inputs.plan, profile=inputs.profile)
    binding = execution_binding_for_slice_node(
        node, table="t", inputs=inputs, requirements=requirements
    )
    assert binding is None


def test_faker_provider_outside_c1_allowlist_is_unbound(tmp_path: Path) -> None:
    """A pool-native provider that is nonetheless NOT one of the two frozen
    C1 providers (`person_first_name`/`person_last_name`) must stay unbound --
    the allowlist, not bare poolability, is the gate."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    plan = _plan_for(tmp_path, source, [_faker_column(provider="person_email")])
    node = plan.tables[0].nodes[0]
    assert node.execution is None


def test_faker_non_string_source_is_unbound(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())})
    plan = _plan_for(tmp_path, source, [_faker_column()])
    node = plan.tables[0].nodes[0]
    assert node.execution is None


def test_faker_large_string_source_binds(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.large_string())})
    plan = _plan_for(tmp_path, source, [_faker_column()])
    node = plan.tables[0].nodes[0]
    assert node.execution is not None
    assert node.execution.pool_binding is not None


def _fk_plan_for(tmp_path: Path, *, parent_columns: list[dict], child_columns: list[dict]):
    # Both tables carry an extra string column ("name_p"/"name_c") beyond the
    # FK key itself, so a faker node on a NON-key column can still be
    # declared without inventing a column the resident source lacks.
    parent = pa.table(
        {
            "id": pa.array(["p1", "p2", "p3"], type=pa.string()),
            "name_p": pa.array(["alice", "bob", "carol"], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "pid": pa.array(["p1", "p2", "p1"], type=pa.string()),
            "name_c": pa.array(["dave", "erin", "frank"], type=pa.string()),
        }
    )
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
                {"name": "parent", "columns": parent_columns},
                {"name": "child", "columns": child_columns},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "fk_ns",
                }
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version=_ENGINE_VERSION
    )
    return compile_physical_plan(inputs)


def test_faker_on_fk_parent_table_is_unbound(tmp_path: Path) -> None:
    """A faker column on the FK-key column itself would collide with the
    relationship's own namespace resolution; use a SEPARATE non-key column on
    each side so the only thing under test is table-level FK participation."""
    plan = _fk_plan_for(
        tmp_path,
        parent_columns=[
            {"name": "id", "strategy": "hash", "namespace": "n"},
            _faker_column("name_p"),
        ],
        child_columns=[
            {"name": "pid", "strategy": "hash", "namespace": "n"},
            {"name": "name_c", "strategy": "passthrough"},
        ],
    )
    parent_table = next(t for t in plan.tables if t.table == "parent")
    faker_node = next(n for n in parent_table.nodes if n.strategy == "faker")
    assert faker_node.execution is None


def test_faker_on_fk_child_table_is_unbound(tmp_path: Path) -> None:
    plan = _fk_plan_for(
        tmp_path,
        parent_columns=[
            {"name": "id", "strategy": "hash", "namespace": "n"},
            {"name": "name_p", "strategy": "passthrough"},
        ],
        child_columns=[
            {"name": "pid", "strategy": "hash", "namespace": "n"},
            _faker_column("name_c"),
        ],
    )
    child_table = next(t for t in plan.tables if t.table == "child")
    faker_node = next(n for n in child_table.nodes if n.strategy == "faker")
    assert faker_node.execution is None
