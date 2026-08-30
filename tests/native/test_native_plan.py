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
from decoy_engine.execution.native._capabilities import capabilities_for
from decoy_engine.execution.native._plan import (
    NativeEligibility,
    NativeExecutionPlan,
    compile_native_plan,
    native_route_eligibility,
)
from decoy_engine.execution.native._requirements import NodeRequirements
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile
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


# ---------------------------------------------------------------------------
# Provider-registry-aware agreement (review remediation). The two public APIs
# must never disagree on the same input; a composite provider fans a faker
# column out to a multi-column node the WorkNode path excludes, so the config-
# only eligibility query must exclude it too.
# ---------------------------------------------------------------------------


def _composite_providers() -> set[str]:
    r = get_default_registry()
    return {p for p in r.known_providers() if r.get_capabilities(p).backend_type == "composite"}


def test_eligibility_agrees_with_registry_for_every_provider() -> None:
    # Walk the LIVE ProviderRegistry (not a hand-typed list): a faker column
    # backed by a composite provider is excluded by the composite-fanout
    # check specifically (matching the WorkNode path's node.kind ==
    # "composite"), distinctly from a non-composite provider, which is
    # excluded by the native-kernel-availability gate instead (faker has no
    # native kernel this phase): neither is ever accepted, but the coded
    # reason still tells the two paths apart, which is the coverage that
    # was missing and would have caught the provider-blind bug this test
    # was written against.
    registry = get_default_registry()
    composite = _composite_providers()
    assert composite  # the registry ships composite bindings; guard the guard
    for provider in registry.known_providers():
        cfg = _config({"name": "c", "strategy": "faker", "provider": provider, "namespace": "ns"})
        result = native_route_eligibility(cfg, table="t")
        is_composite = provider in composite
        assert result.accepted is False, provider
        if is_composite:
            assert any(r.startswith("composite_provider_multi_column") for r in result.rejections)
        else:
            assert any(r.startswith("no_native_kernel") for r in result.rejections)


def test_composite_provider_faker_is_rejected_the_reported_bug() -> None:
    # The exact input from the review: a faker column backed by a composite
    # provider was classified accepted while compile_native_plan excluded it.
    provider = sorted(_composite_providers())[0]
    cfg = _config({"name": "c", "strategy": "faker", "provider": provider, "namespace": "ns"})
    assert native_route_eligibility(cfg, table="t").accepted is False


def test_eligibility_and_compile_native_plan_agree_on_composite() -> None:
    # End-to-end agreement with the WorkNode path: compile a real composite
    # config and assert both APIs reach the same accept/exclude verdict.
    cfg = {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "first_name",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "namespace": "ns",
                        "coherent_with": ["last_name", "email"],
                    },
                    {
                        "name": "last_name",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "namespace": "ns",
                        "coherent_with": ["first_name", "email"],
                    },
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "namespace": "ns",
                        "coherent_with": ["first_name", "last_name"],
                    },
                ],
            }
        ],
    }
    profile = _profile("first_name", "last_name", "email")
    plan = compile_native_plan(cfg, profile, engine_version="0.1.0")
    plan_all_native = all(node.fallback_policy == "native" for node in plan.nodes)
    assert plan_all_native is False  # the composite node is python_only
    assert native_route_eligibility(cfg, table="t").accepted is plan_all_native


def test_eligibility_and_compile_native_plan_agree_on_hash() -> None:
    # The positive control: a scalar hash column is native on both paths.
    cfg = _config(HASH_COL)
    profile = _profile("c")
    plan = compile_native_plan(cfg, profile, engine_version="0.1.0")
    plan_all_native = all(node.fallback_policy == "native" for node in plan.nodes)
    assert plan_all_native is True
    assert native_route_eligibility(cfg, table="t").accepted is plan_all_native


# ---------------------------------------------------------------------------
# FK-composite-group agreement (round-2 remediation). The composite_fk_group
# node kind is driven by profile.relationships (a multi-column parent key), not
# by any column string, so native_route_eligibility is structurally blind to it
# WITHOUT the profile. Threading the profile closes the gap; both APIs then
# classify the <group> node through the same shared capabilities.
# ---------------------------------------------------------------------------


def _fk_col(name: str, **flags: object) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=5,
        null_count=0,
        distinct_count=5,
        sampled=False,
        is_candidate_key_sampled=bool(flags.get("ck")),
        declared_pk=bool(flags.get("pk")),
        is_fk=bool(flags.get("fk")),
        fk_target=flags.get("target"),  # type: ignore[arg-type]
        pii_class=None,
    )


def _composite_fk_profile() -> Profile:
    enrollments = TableProfile(
        name="enrollments",
        row_count=5,
        columns=(
            _fk_col("member_id", pk=True, ck=True),
            _fk_col("plan_id", pk=True, ck=True),
            _fk_col("effective_date", pk=True, ck=True),
        ),
    )
    claims = TableProfile(
        name="claims",
        row_count=20,
        columns=(
            _fk_col("claim_id", pk=True, ck=True),
            _fk_col("member_id", fk=True, target=("enrollments", "member_id")),
            _fk_col("plan_id", fk=True, target=("enrollments", "plan_id")),
            _fk_col("effective_date", fk=True, target=("enrollments", "effective_date")),
        ),
    )
    return Profile(
        schema_version=1,
        tables=(enrollments, claims),
        relationships=(
            Relationship(
                parent_table="enrollments",
                parent_columns=("member_id", "plan_id", "effective_date"),
                child_table="claims",
                child_columns=("member_id", "plan_id", "effective_date"),
                namespace="enr",
            ),
        ),
        profiled_at=datetime(2026, 8, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _composite_fk_config() -> dict:
    return {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "enrollments",
                "columns": [{"name": "member_id", "strategy": "hash", "namespace": "enr"}],
            },
            {
                "name": "claims",
                "columns": [{"name": "claim_id", "strategy": "hash", "namespace": "c"}],
            },
        ],
        "relationships": [
            {
                "parent": {
                    "table": "enrollments",
                    "columns": ["member_id", "plan_id", "effective_date"],
                },
                "children": [
                    {"table": "claims", "columns": ["member_id", "plan_id", "effective_date"]}
                ],
                "orphan_policy": "fail",
                "namespace": "enr",
            }
        ],
        "namespaces": {"enr": {"declared_by": ["enrollments.member_id", "claims.member_id"]}},
    }


def test_compile_native_plan_emits_composite_fk_group_node() -> None:
    plan = compile_native_plan(
        _composite_fk_config(), _composite_fk_profile(), engine_version="0.1.0"
    )
    kinds = {node.kind for node in plan.nodes}
    assert "composite_fk_group" in kinds


def test_eligibility_with_profile_agrees_on_fk_group() -> None:
    # The end-to-end agreement the round-2 finding demanded: compile the plan
    # (which emits a composite_fk_group node) and assert native_route_eligibility,
    # given the profile, reaches the SAME accept/reject verdict for the child
    # table as the compiled plan's all-native verdict.
    cfg = _composite_fk_config()
    profile = _composite_fk_profile()
    plan = compile_native_plan(cfg, profile, engine_version="0.1.0")
    plan_all_native = all(node.fallback_policy == "native" for node in plan.nodes)
    result = native_route_eligibility(cfg, table="claims", profile=profile)
    assert result.accepted is plan_all_native


def test_fk_group_capabilities_are_native_ready_today() -> None:
    # Locks the current invariant AND the by-construction agreement: the <group>
    # placeholder resolves native-ready, so both APIs accept the FK-group node.
    # If these capabilities ever change to non-native, native_route_eligibility
    # (which routes <group> through the same capabilities_for + _native_rejection)
    # rejects in lockstep with compile_native_plan; this test then flips to
    # asserting rejection, never silent disagreement.
    c = capabilities_for("<group>")
    native_ready = (
        c.output_type_is_static
        and c.is_row_local
        and not c.is_global
        and not c.needs_global_row_identity
    )
    assert native_ready is True


def test_eligibility_without_profile_does_not_evaluate_fk_groups() -> None:
    # Documents the boundary: omitting the profile leaves FK-group nodes
    # unevaluated. Benign here because the child table's <group> node is
    # native-ready, so the config-only verdict (which sees only the scalar
    # claim_id column) still matches. The full-agreement path passes the profile.
    cfg = _composite_fk_config()
    assert native_route_eligibility(cfg, table="claims").accepted is True
