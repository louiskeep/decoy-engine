"""Schema/config-resolved node requirements tests (native program, Task 0.2).

Static capabilities are strategy-only; RESOLVED requirements read the compiled
node + profile. The load-bearing example from the plan: a ``date_shift`` node
with an explicit ``date_format`` needs no prepass and has a determinate output
schema; without one it needs a ``format_detect`` prepass.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.execution._runner import WorkNode, build_work_list
from decoy_engine.execution.native._requirements import (
    NodeRequirements,
    requirements_for,
)
from decoy_engine.plan import compile_plan
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.profile import ColumnProfile, Profile, TableProfile


def _profile(dtype: str = "object") -> Profile:
    from datetime import datetime

    return Profile(
        schema_version=1,
        tables=(
            TableProfile(
                name="t",
                row_count=3,
                columns=(
                    ColumnProfile(
                        name="c",
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
                    ),
                    ColumnProfile(
                        name="anchor",
                        dtype="object",
                        row_count=3,
                        null_count=0,
                        distinct_count=3,
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=False,
                        fk_target=None,
                        pii_class=None,
                    ),
                ),
            ),
        ),
        relationships=(),
        profiled_at=datetime(2026, 8, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _scalar_node(strategy: str, *, provider_config: tuple = (), coherent: tuple = ()) -> WorkNode:
    seed = ColumnSeed(
        namespace="ns",
        strategy=strategy,
        provider=None,
        backend_type=None,
        backend_version="",
        cardinality_mode="reuse",
        provider_config=provider_config,
        coherent_with=coherent,
    )
    return WorkNode(
        table="t",
        columns=("c",),
        kind="scalar",
        strategy=strategy,
        provider=None,
        plan_slice=seed,
    )


def test_hash_node_is_native_with_determinate_schema() -> None:
    req = requirements_for(_scalar_node("hash"), plan=None, profile=_profile())
    assert isinstance(req, NodeRequirements)
    assert req.output_arrow_schema is not None
    assert req.required_prepasses == ()
    assert req.fallback_policy == "native"
    assert req.required_input_columns == ("c",)


def test_date_shift_with_explicit_format_needs_no_prepass() -> None:
    node = _scalar_node("date_shift", provider_config=(("date_format", "%Y-%m-%d"),))
    req = requirements_for(node, plan=None, profile=_profile())
    assert "format_detect" not in req.required_prepasses
    assert req.output_arrow_schema is not None


def test_date_shift_without_format_needs_format_detect_prepass() -> None:
    node = _scalar_node("date_shift")
    req = requirements_for(node, plan=None, profile=_profile())
    assert "format_detect" in req.required_prepasses


def test_formula_node_has_indeterminate_schema_and_is_not_native() -> None:
    req = requirements_for(_scalar_node("formula"), plan=None, profile=_profile())
    assert req.output_arrow_schema is None  # excluded from the native route
    assert req.fallback_policy != "native"


def test_passthrough_preserves_input_type() -> None:
    req = requirements_for(_scalar_node("passthrough"), plan=None, profile=_profile("int64"))
    assert req.output_arrow_schema is not None
    assert req.output_arrow_schema.field("c").type == pa.int64()


def test_shuffle_node_needs_global_row_number_prepass() -> None:
    req = requirements_for(_scalar_node("shuffle"), plan=None, profile=_profile())
    assert "global_row_number" in req.required_prepasses
    assert req.fallback_policy != "native"


def test_code_set_node_requires_corpus_state_table() -> None:
    req = requirements_for(_scalar_node("code_set"), plan=None, profile=_profile())
    assert "code_set_corpus" in req.required_state_tables


def test_diagnostic_reducers_present_for_warning_capable_node() -> None:
    req = requirements_for(_scalar_node("fpe"), plan=None, profile=_profile())
    assert req.diagnostic_reducers != ()


def test_admitted_set_has_no_diagnostic_reducers() -> None:
    for s in ("hash", "redact", "truncate", "passthrough"):
        req = requirements_for(_scalar_node(s), plan=None, profile=_profile())
        assert req.diagnostic_reducers == ()
        assert req.required_state_tables == ()


def test_coherent_columns_join_required_input() -> None:
    node = _scalar_node("hash", coherent=("d", "e"))
    req = requirements_for(node, plan=None, profile=_profile())
    assert set(req.required_input_columns) >= {"c", "d", "e"}


def test_requirements_cover_all_node_kinds() -> None:
    # A real composite + FK-group plan compiles to nodes whose kinds are not
    # "scalar"; requirements_for must resolve each via node.kind, not the
    # placeholder strategy string.
    profile = _profile()
    for kind, strategy in (
        ("composite", "<composite>"),
        ("composite_fk_group", "<group>"),
    ):
        from decoy_engine.plan._types import GroupSeed

        plan_slice = (
            GroupSeed(namespace="ns", coherent_columns=("c",))
            if kind == "composite_fk_group"
            else ColumnSeed(
                namespace="ns",
                strategy=strategy,
                provider="person_name",
                backend_type=None,
                backend_version="",
                cardinality_mode="reuse",
            )
        )
        node = WorkNode(
            table="t",
            columns=("c",),
            kind=kind,
            strategy=strategy,
            provider="person_name" if kind == "composite" else "<group>",
            plan_slice=plan_slice,
        )
        req = requirements_for(node, plan=None, profile=profile)
        assert isinstance(req, NodeRequirements)
        assert req.lowering_id.startswith(kind)


def test_compiled_plan_nodes_all_resolve() -> None:
    # End-to-end: every node build_work_list produces for a real compiled plan
    # resolves to NodeRequirements without raising.
    from decoy_engine.providers_v2 import get_default_registry

    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "c", "strategy": "hash", "namespace": "ns"},
                ],
            }
        ],
    }
    profile = _profile()
    plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
    for node in build_work_list(plan, get_default_registry()):
        req = requirements_for(node, plan=plan, profile=profile)
        assert isinstance(req, NodeRequirements)
