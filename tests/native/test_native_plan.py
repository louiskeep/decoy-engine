"""NativeExecutionPlan compiler + public native-route eligibility (Task 0.2b).

The compiler turns a validated config + profile into a backend-neutral plan
whose nodes carry resolved requirements; the eligibility query is the public,
drift-sentried replacement for the platform's copied strategy set.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa

from decoy_engine.config._tables import GENERATE_TYPES
from decoy_engine.execution._runner import build_work_list
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.native._plan import (
    NativeEligibility,
    NativeExecutionPlan,
    compile_native_plan,
    native_route_eligibility,
)
from decoy_engine.execution.native._requirements import NodeRequirements
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry


def _col(name: str, dtype: str = "object") -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=3,
        null_count=0,
        distinct_count=3,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


def _profile(*names: str) -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=3, columns=tuple(_col(n) for n in names)),),
        relationships=(),
        profiled_at=datetime(2026, 8, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _config(*columns: dict) -> dict:
    return {
        "global_settings": {"seed": 42},
        "tables": [{"name": "t", "columns": list(columns)}],
    }


HASH_COL = {"name": "c", "strategy": "hash", "namespace": "ns"}
FORMULA_COL = {"name": "f", "strategy": "formula", "provider_config": {"formula": "value"}}


def test_mask_config_compiles_to_native_plan_with_requirements() -> None:
    plan = compile_native_plan(_config(HASH_COL), _profile("c"), engine_version="0.1.0")
    assert isinstance(plan, NativeExecutionPlan)
    assert plan.nodes
    for node in plan.nodes:
        assert isinstance(node.requirements, NodeRequirements)
        assert node.input_projection == node.requirements.required_input_columns


def test_work_nodes_carry_requirements_after_compile() -> None:
    plan = compile_native_plan(_config(HASH_COL), _profile("c"), engine_version="0.1.0")
    for wn in plan.work_nodes:
        assert wn.requirements is not None


def test_hash_only_config_is_accepted() -> None:
    result = native_route_eligibility(_config(HASH_COL), table="t")
    assert isinstance(result, NativeEligibility)
    assert result.accepted
    assert result.rejections == ()


def test_formula_column_is_rejected_output_type_indeterminate() -> None:
    result = native_route_eligibility(_config(FORMULA_COL), table="t")
    assert result.accepted is False
    assert any("output_type_indeterminate" in r for r in result.rejections)


def test_mixed_config_rejects_only_when_a_node_is_non_native() -> None:
    result = native_route_eligibility(_config(HASH_COL, FORMULA_COL), table="t")
    assert result.accepted is False
    assert result.rejections != ()


def test_per_output_schema_present_for_static_nodes() -> None:
    plan = compile_native_plan(_config(HASH_COL), _profile("c"), engine_version="0.1.0")
    schema = plan.output_schema_for("t")
    assert schema is not None
    assert isinstance(schema, pa.Schema)


def test_engine_version_recorded() -> None:
    plan = compile_native_plan(_config(HASH_COL), _profile("c"), engine_version="9.9.9")
    assert plan.engine_version == "9.9.9"


def test_eligibility_total_over_live_mask_registry() -> None:
    # Drift sentinel: every live mask strategy either accepts or yields coded
    # rejections, never raises. (A generation-only kind is not a mask strategy,
    # so we drive this over the mask registry.)
    for strategy in SCALAR_HANDLERS:
        cfg = _config({"name": "c", "strategy": strategy, "namespace": "ns"})
        result = native_route_eligibility(cfg, table="t")
        assert isinstance(result, NativeEligibility)
        if not result.accepted:
            assert result.rejections != ()


def test_generation_registry_is_covered_by_capabilities() -> None:
    # The eligibility query resolves each generation kind through the same
    # capabilities table, so a new generation kind surfaces here too.
    from decoy_engine.execution.native._capabilities import capabilities_for

    for kind in GENERATE_TYPES:
        assert capabilities_for(kind) is not None


def test_missing_table_yields_empty_accepted_plan() -> None:
    # A table not present in the config has no nodes to reject.
    result = native_route_eligibility(_config(HASH_COL), table="does_not_exist")
    assert result.accepted
    assert result.rejections == ()


def test_build_work_list_unaffected_by_native_field() -> None:
    # The inert WorkNode.requirements field defaults None on the normal path.
    plan_config = _config(HASH_COL)
    from decoy_engine.plan import compile_plan

    plan = compile_plan(plan_config, _profile("c"), decoy_engine_version="0.1.0")
    for wn in build_work_list(plan, get_default_registry()):
        assert wn.requirements is None
