"""The route-coverage choke point: every route has a cell that proves the gate ran.

`GATE_TESTS` is keyed on the imported `ROUTES` plus `stream` and `isolated`. Each value is
`path::test_function`; the function must exist in that file, the file must name the route as a
string constant, and the function must read the report's `keys`. A route added to `ROUTES`
without an entry here, or an entry whose test went away, is a site.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping

from decoy_engine.declared_casting._eligibility import ROUTES
from tests.sentry.post_scan._scan import ROOT

EXTRA_ROUTES = ("stream", "isolated")
CELLS = "tests/unit/execution/post_scan/test_route_cells.py::test_gate_cell"
GATE_TESTS: dict[str, str] = {route: CELLS for route in (*ROUTES, *EXTRA_ROUTES)}


def coverage_sites(
    gate_tests: Mapping[str, str] | None = None, routes: tuple[str, ...] = ROUTES
) -> list[str]:
    declared = GATE_TESTS if gate_tests is None else gate_tests
    found: list[str] = []
    for route in (*routes, *EXTRA_ROUTES):
        target = declared.get(route)
        if target is None:
            found.append(f"route-coverage::{route}::missing-entry::0")
        elif not _proves_the_gate(route, target):
            found.append(f"route-coverage::{route}::no-gate-proof::0")
    return found


def _proves_the_gate(route: str, target: str) -> bool:
    path, _, name = target.partition("::")
    file = ROOT / path
    if not file.is_file():
        return False
    tree = ast.parse(file.read_text(encoding="utf-8"))
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name), None
    )
    if func is None:
        return False
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    inside = {n.value for n in ast.walk(func) if isinstance(n, ast.Constant)}
    return route in strings and {"keys", "post_scan_integrity"} <= inside
