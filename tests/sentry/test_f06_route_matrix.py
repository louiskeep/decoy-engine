"""The format x route x policy matrix is generated from the registries and pinned.

Every cell is either `ok` (the route's capability set covers the contract and a route parity test
exercises it) or a registered refusal naming the missing capability. Adding a format, a route, or
a capability changes the generated matrix, so the committed copy must be regenerated and reviewed;
a route that claims support must also have a named parity test that actually exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PINNED = Path(__file__).resolve().parents[1] / "fixtures" / "f06" / "route-format-matrix-v1.json"
POLICIES = ("fail", "null", "quarantine")
# The route parity test that exercises each route end to end (and proves which route ran).
PARITY_TESTS = {
    "full_frame": "test_chunked_route_matches_full_frame_and_is_really_chunked",
    "chunked": "test_chunked_route_matches_full_frame_and_is_really_chunked",
    "sequential": "test_sequential_route_matches_full_frame_for_declared_relationship_keys",
    "out_of_core": "test_out_of_core_matches_full_frame_when_no_declared_column_is_a_key",
    "mixed": "test_mixed_route_matches_the_full_frame_oracle",
    "unified_slice": "test_unified_slice_route_matches_the_full_frame_oracle",
}


def _column(desc: Any, policy: str) -> Any:
    from decoy_engine.contracts.declared_casting import build_contract
    from decoy_engine.declared_casting import descriptor_for
    from decoy_engine.plan._types import PlanDeclaredColumn, freeze_json

    spec = dict(desc.vectors[0][0])
    if desc.direction == "parse":
        fields: dict[str, Any] = {
            "logical_type": desc.logical,
            "parse_format": spec,
            "error_policy": policy,
        }
    else:
        parse = dict(descriptor_for(desc.inverse, "parse").vectors[0][0])
        if "width" in desc.model.model_fields and "width" not in spec:
            spec.update(width=40, width_unit="code_points")
        if desc.logical in ("decimal", "currency"):
            parse.update({k: spec[k] for k in ("precision", "scale") if k in spec})
        fields = {
            "logical_type": desc.logical,
            "parse_format": parse,
            "error_policy": policy,
            "output_format": spec,
        }
    return PlanDeclaredColumn(
        "t", "c", "passthrough", freeze_json(build_contract(fields).to_payload())
    )


def build_matrix() -> dict[str, Any]:
    from decoy_engine.declared_casting import (
        ROUTES,
        RuntimeCapabilities,
        descriptors,
        evaluate_declared_casting,
    )
    from decoy_engine.declared_casting._eligibility import ROUTE_CAPABILITIES

    rows: dict[str, Any] = {}
    for desc in sorted(descriptors(), key=lambda d: (d.format_id, d.direction)):
        cells: dict[str, dict[str, str]] = {}
        for route in ROUTES:
            cells[route] = {}
            for policy in POLICIES:
                decision = evaluate_declared_casting(
                    route=route,
                    plan_contracts=(_column(desc, policy),),
                    capabilities=RuntimeCapabilities(quarantine_sink=True),
                )
                cells[route][policy] = (
                    "ok"
                    if decision.admitted
                    else f"refused:{decision.rejection}:{'+'.join(decision.missing)}"
                )
        rows[f"{desc.format_id}|{desc.direction}"] = {
            "physical_kind": desc.physical_kind,
            "logical": desc.logical,
            "width_rule": desc.width_rule,
            "inverse": desc.inverse,
            "vector_count": len(desc.vectors),
            "routes": cells,
        }
    return {
        "matrix_version": 1,
        "routes": {r: sorted(ROUTE_CAPABILITIES[r]) for r in ROUTES},
        "formats": rows,
    }


def test_the_matrix_equals_the_pinned_generated_copy() -> None:
    generated = json.loads(json.dumps(build_matrix()))
    assert PINNED.is_file(), "generate it with tests/sentry/test_f06_route_matrix.py::regenerate"
    assert json.loads(PINNED.read_text()) == generated


def test_every_format_has_a_cell_for_every_route_and_policy() -> None:
    from decoy_engine.declared_casting import ROUTES, descriptors

    matrix = build_matrix()
    assert len(matrix["formats"]) == len(descriptors())
    for row in matrix["formats"].values():
        assert set(row["routes"]) == set(ROUTES)
        for cells in row["routes"].values():
            assert set(cells) == set(POLICIES)
            for cell in cells.values():
                assert cell == "ok" or cell.startswith("refused:declared_cast_route_unsupported:")


def test_every_route_that_admits_a_cell_has_an_existing_parity_test() -> None:
    import ast

    source = Path(__file__).resolve().parents[1] / "acceptance" / "test_f06_route_parity.py"
    defined = {
        n.name for n in ast.walk(ast.parse(source.read_text())) if isinstance(n, ast.FunctionDef)
    }
    matrix = build_matrix()
    admitting = {
        route
        for row in matrix["formats"].values()
        for route, cells in row["routes"].items()
        if "ok" in cells.values()
    }
    assert admitting == set(PARITY_TESTS), admitting ^ set(PARITY_TESTS)
    for route in admitting:
        assert PARITY_TESTS[route] in defined, (route, PARITY_TESTS[route])


# Cells a route admits by capability but declines at its physical admission (a documented rule in
# tests/acceptance/_f06_cells.py::DECLINE_RULES). The corpus still runs them and checks the
# fallback publishes what the contract promises; growing this census is a reviewed decision.
PINNED_DECLINED_CELLS = {"chunked": 18, "mixed": 12, "unified_slice": 48}


def _admitted(matrix: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (route, key, policy)
        for key, row in matrix["formats"].items()
        for route, cells in row["routes"].items()
        for policy, verdict in cells.items()
        if verdict == "ok"
    }


def test_the_corpus_runs_every_admitted_cell_and_only_those() -> None:
    """Each admitted (route, format, policy) cell is a test case of the real-route corpus."""
    from tests.acceptance import test_f06_route_cells as corpus

    case = corpus.test_admitted_cell_runs_on_its_route
    (mark,) = [m for m in case.pytestmark if m.name == "parametrize"]
    assert set(mark.args[1]) == _admitted(build_matrix())
    assert len(mark.args[1]) == len(set(mark.args[1]))


def test_every_policy_of_an_admitted_format_route_pair_is_in_the_corpus() -> None:
    from tests.acceptance import _f06_cells as cells

    matrix = build_matrix()
    for key, row in matrix["formats"].items():
        for route, verdicts in row["routes"].items():
            admitted = {p for p, v in verdicts.items() if v == "ok"}
            assert admitted == {p for r, k, p in cells.admitted_cells() if (r, k) == (route, key)}


def test_the_corpus_drives_every_route_and_asserts_its_identity() -> None:
    import inspect

    from decoy_engine.declared_casting import ROUTES
    from tests.acceptance import _f06_cells as cells

    assert set(cells.DRIVERS) == set(ROUTES)
    for route, driver in cells.DRIVERS.items():
        # A driver that never checks which route ran would let a cell run elsewhere.
        body = inspect.getsource(driver) + inspect.getsource(cells._drive_relational)
        assert any(t in body for t in ("check_route", "execution_mode")), route


def test_physically_declined_cells_are_a_pinned_documented_census() -> None:
    from tests.acceptance import _f06_cells as cells

    declined: dict[str, int] = {}
    for route, key, _policy in cells.admitted_cells():
        if cells.physical_decline(route, key) is not None:
            declined[route] = declined.get(route, 0) + 1
    assert declined == PINNED_DECLINED_CELLS
    assert set(cells.DECLINE_RULES) == set(PINNED_DECLINED_CELLS)


def regenerate() -> None:  # pragma: no cover - maintenance entry point
    PINNED.write_text(json.dumps(build_matrix(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":  # pragma: no cover
    regenerate()
