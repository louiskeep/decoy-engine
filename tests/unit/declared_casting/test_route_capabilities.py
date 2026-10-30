"""Route capability truth table. The expected sets are written out here, not read back from the
engine, so a route cannot widen or narrow what it claims without this table changing."""

from __future__ import annotations

from typing import Any

import pytest

from tests.acceptance._f06_support import (
    ISO_DATE,
    PROTECTED_QUARANTINE,
    column,
    config,
    validate,
)

ALL = {
    "pre_mask_cast",
    "issue_counts",
    "post_mask_format",
    "binary_cells",
    "row_quarantine",
    "post_mask_row_quarantine",
    "cast_before_join",
}
EXPECTED = {
    "full_frame": ALL,
    "sequential": ALL,
    "unified_slice": ALL - {"cast_before_join"},
    "chunked": ALL - {"cast_before_join"},
    "mixed": {"pre_mask_cast", "issue_counts", "post_mask_format", "binary_cells"},
    "out_of_core": {"pre_mask_cast", "issue_counts", "post_mask_format", "binary_cells"},
}


def test_capability_sets_are_exactly_the_documented_table() -> None:
    from decoy_engine.declared_casting._eligibility import ROUTE_CAPABILITIES

    assert {k: set(v) for k, v in ROUTE_CAPABILITIES.items()} == EXPECTED


def _contracts(policy: str = "null", *, fmt: bool = True, binary: bool = False) -> list[Any]:
    from decoy_engine.plan._checks_declared_casting import compile_declared_casting_contract

    if binary:
        cols = [
            column(
                "amt",
                logical_type="decimal",
                parse_format={"id": "cobol/comp-3/v1", "precision": 5, "scale": 2, "signed": True},
                error_policy=policy,
            )
        ]
    else:
        cols = [
            column(
                "start",
                logical_type="date",
                parse_format=ISO_DATE,
                error_policy=policy,
                **(
                    {
                        "output_format": {
                            "id": "date/iso8601/v1",
                            "width": 10,
                            "width_unit": "code_points",
                        }
                    }
                    if fmt
                    else {}
                ),
            )
        ]
    top = {"protected_quarantine": PROTECTED_QUARANTINE} if policy == "quarantine" else None
    cfg = validate(config(cols, source_path="/nonexistent.parquet", table="t", top=top))
    cfg["sources"]["t"]["format"] = "parquet" if binary else "csv"
    block = compile_declared_casting_contract(cfg)
    assert block is not None
    return list(block.columns)


@pytest.mark.parametrize(
    ("route", "policy", "fmt", "admitted"),
    [
        ("chunked", "null", True, True),
        ("chunked", "quarantine", True, True),
        ("mixed", "null", True, True),
        ("mixed", "quarantine", True, False),  # row removal would desync the pre-resolved key
        ("out_of_core", "null", True, True),
        ("out_of_core", "quarantine", False, False),
        ("sequential", "quarantine", True, True),
        ("unified_slice", "quarantine", True, True),
    ],
)
def test_decisions_follow_the_needs_of_the_contract(
    route: str, policy: str, fmt: bool, admitted: bool
) -> None:
    from decoy_engine.declared_casting import RuntimeCapabilities, evaluate_declared_casting

    decision = evaluate_declared_casting(
        route=route,
        plan_contracts=_contracts(policy, fmt=fmt),
        capabilities=RuntimeCapabilities(quarantine_sink=True),
    )
    assert decision.admitted is admitted
    if not admitted:
        assert str(decision.rejection) == "declared_cast_route_unsupported"
        assert decision.missing  # names the capability, never a format


def test_binary_cells_are_admitted_where_bytes_survive() -> None:
    from decoy_engine.declared_casting import evaluate_declared_casting

    contracts = _contracts(binary=True, fmt=False)
    for route in ("full_frame", "sequential", "chunked", "mixed", "out_of_core", "unified_slice"):
        assert evaluate_declared_casting(route=route, plan_contracts=contracts).admitted, route


def _edges(table: str, column_name: str) -> list[dict[str, Any]]:
    return [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": table, "columns": [column_name]}],
        }
    ]


def test_cast_before_join_is_needed_only_for_declared_relationship_endpoints() -> None:
    from decoy_engine.declared_casting import evaluate_declared_casting

    contracts = _contracts()  # declares t.start
    unrelated = evaluate_declared_casting(
        route="chunked", plan_contracts=contracts, relationships=_edges("t", "other")
    )
    assert unrelated.admitted and "cast_before_join" not in unrelated.requirements
    endpoint = evaluate_declared_casting(
        route="chunked", plan_contracts=contracts, relationships=_edges("t", "start")
    )
    assert not endpoint.admitted and endpoint.missing == ("cast_before_join",)
    assert evaluate_declared_casting(
        route="sequential", plan_contracts=contracts, relationships=_edges("t", "start")
    ).admitted


def test_no_route_provides_parent_row_quarantine() -> None:
    from decoy_engine.declared_casting._eligibility import ROUTE_CAPABILITIES

    for capability in ("post_mask_parent_row_quarantine", "pre_mask_parent_row_quarantine"):
        assert all(capability not in v for v in ROUTE_CAPABILITIES.values())


@pytest.mark.parametrize("orphan_policy", ["preserve", "remap", "warn", "fail", None])
def test_parse_quarantine_on_a_parent_needs_a_failing_orphan_policy(
    orphan_policy: str | None,
) -> None:
    from decoy_engine.declared_casting import RuntimeCapabilities, evaluate_declared_casting

    edge: dict[str, Any] = {
        "parent": {"table": "t", "columns": ["id"]},
        "children": [{"table": "c", "columns": ["pid"]}],
    }
    if orphan_policy is not None:
        edge["orphan_policy"] = orphan_policy
    decision = evaluate_declared_casting(
        route="sequential",
        plan_contracts=_contracts("quarantine", fmt=False),
        capabilities=RuntimeCapabilities(quarantine_sink=True),
        relationships=[edge],
    )
    if orphan_policy == "fail":
        assert decision.admitted
        assert "pre_mask_parent_row_quarantine" not in decision.requirements
    else:
        assert not decision.admitted
        assert decision.missing == ("pre_mask_parent_row_quarantine",)
        assert str(decision.rejection) == "declared_cast_route_unsupported"
