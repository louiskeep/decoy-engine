"""F06 construction sentries: the casting boundary cannot drift, leak, or fork per route.

Each scanner is paired with seeded mutants so the build breaks if a scanner stops being able to
fail. Scans are AST-based; a docstring that mentions `float` or `locale` is not a violation.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "decoy_engine"
CASTING = SRC / "declared_casting"
CONTRACT = SRC / "contracts" / "declared_casting.py"

# Route owners: every production function that admits, selects or runs an execution route must
# route its F06 decision through the one central evaluator. (module, function) pairs.
ROUTE_OWNERS = {
    ("execution/_pipeline_context.py", "decide_route"),
    ("execution/_pipeline_routing.py", "resolve_execution_route"),
    ("execution/_pipeline_routing.py", "decide_chunk_route"),
    ("execution/_planner.py", "classify_job"),
    ("execution/_chunked.py", "check_chunked_compatibility"),
    ("execution/out_of_core/_compat.py", "check_out_of_core_compatibility"),
    ("execution/_mixed_static.py", "admit_mixed_static"),
    ("execution/_unified_slice.py", "maybe_run_unified_slice"),
    ("execution/_sequential.py", "run_sequential"),
    ("execution/out_of_core/_runner.py", "run_fk_out_of_core"),
}
RUNTIME_ADAPTERS = {
    ("execution/_pipeline_full_frame.py", "run_full_frame_route"),
}
ROUTE_FN = re.compile(
    r"^(run_.*route|run_sequential|run_fk_out_of_core|check_.*compat.*|admit_.*|decide_.*route.*|resolve_execution_route|maybe_run_unified_slice)$"
)


def _functions(source: str) -> dict[str, ast.AST]:
    tree = ast.parse(source)
    return {
        n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls_name(node: ast.AST, names: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            n = (
                f.id
                if isinstance(f, ast.Name)
                else f.attr
                if isinstance(f, ast.Attribute)
                else None
            )
            if n in names:
                return True
    return False


def missing_evaluator(owners: set[tuple[str, str]], read) -> list[tuple[str, str]]:
    bad = []
    for rel, fn in sorted(owners):
        node = _functions(read(rel)).get(fn)
        if node is None or not _calls_name(
            node, {"evaluate_declared_casting", "declared_route_guard"}
        ):
            bad.append((rel, fn))
    return bad


def _read(rel: str) -> str:
    return (SRC / rel).read_text()


def test_every_route_owner_calls_the_central_evaluator() -> None:
    assert missing_evaluator(ROUTE_OWNERS, _read) == []


def test_route_owner_inventory_is_complete() -> None:
    """A new production route function must be added to the inventory (and therefore the guard)."""
    found: set[tuple[str, str]] = set()
    for path in sorted((SRC / "execution").rglob("*.py")):
        rel = str(path.relative_to(SRC))
        for name, node in _functions(path.read_text()).items():
            if ROUTE_FN.match(name) and not name.startswith("_"):
                found.add((rel, name))
    known = ROUTE_OWNERS | RUNTIME_ADAPTERS
    unknown = sorted(f for f in found if f not in known)
    # entries that exist in the inventory but vanished are also drift
    gone = sorted(k for k in known if k not in found and not (SRC / k[0]).exists())
    assert not unknown, f"route owners missing from the F06 inventory: {unknown}"
    assert not gone, f"inventory points at removed modules: {gone}"


def test_seeded_route_without_the_evaluator_fails_the_inventory() -> None:
    sources = {"x.py": "def run_new_route(ctx):\n    return ctx\n"}
    assert missing_evaluator({("x.py", "run_new_route")}, sources.__getitem__) == [
        ("x.py", "run_new_route")
    ]
    good = {"x.py": "def run_new_route(ctx):\n    evaluate_declared_casting(ctx)\n"}
    assert missing_evaluator({("x.py", "run_new_route")}, good.__getitem__) == []


def test_runtime_adapters_call_the_shared_boundary_not_a_private_parser() -> None:
    for rel, fn in sorted(RUNTIME_ADAPTERS):
        node = _functions(_read(rel))[fn]
        assert _calls_name(node, {"cast_sources_pre_mask", "apply_declared_casting"}), (rel, fn)


# -- forbidden constructs in the casting package ----------------------------------------------

FORBIDDEN_NAMES = {"float", "round"}
FORBIDDEN_ATTRS = {
    "to_numeric",
    "to_datetime",
    "setlocale",
    "localeconv",
    "nl_langinfo",
    "parse",
    "isoparse",
}
FORBIDDEN_IMPORTS = {"locale", "dateutil", "babel"}


def casting_violations(source: str, *, allow_quantize: bool = False) -> list[str]:
    out: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id in FORBIDDEN_NAMES
            and isinstance(node.ctx, ast.Load)
        ):
            out.append(f"name:{node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS and not (
                node.attr == "parse" and not _is_dateutil(node)
            ):
                out.append(f"attr:{node.attr}")
            if node.attr == "quantize" and not allow_quantize:
                out.append("attr:quantize")
            if (
                node.attr == "cast"
                and isinstance(node.value, (ast.Name, ast.Attribute))
                and "compute" in ast.dump(node.value)
            ):
                out.append("attr:arrow-cast")
        elif isinstance(node, ast.Import):
            out += [
                f"import:{a.name}" for a in node.names if a.name.split(".")[0] in FORBIDDEN_IMPORTS
            ]
        elif (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS
        ):
            out.append(f"import:{node.module}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "astype"
        ):
            out.append("attr:astype")
    return out


def _is_dateutil(node: ast.Attribute) -> bool:
    return "dateutil" in ast.dump(node)


def _casting_files() -> list[Path]:
    return sorted(CASTING.glob("*.py")) if CASTING.exists() else []


def test_casting_package_exists() -> None:
    assert CASTING.is_dir() and CONTRACT.is_file()


def test_casting_package_has_no_float_locale_inference_or_unchecked_arithmetic() -> None:
    files = _casting_files()
    assert files, "declared_casting package is missing"
    problems: dict[str, list[str]] = {}
    for path in files:
        # `quantize` is the exactness oracle and may only live in the numeric kernel.
        found = casting_violations(path.read_text(), allow_quantize=path.name == "_numeric.py")
        if found:
            problems[path.name] = found
    assert problems == {}


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("x = float(v)", "name:float"),
        ("x = round(v, 2)", "name:round"),
        ("import locale", "import:locale"),
        ("from dateutil import parser", "import:dateutil"),
        ("import pandas as pd\nx = pd.to_numeric(s)", "attr:to_numeric"),
        ("x = d.quantize(q)", "attr:quantize"),
        ("x = s.astype('float64')", "attr:astype"),
        ("import locale as l\nl.setlocale(0, '')", "attr:setlocale"),
        ("import pyarrow.compute as pc\nx = pc.cast(a, 'float64')", "attr:arrow-cast"),
    ],
)
def test_seeded_mutants_trip_the_casting_scanner(snippet: str, expected: str) -> None:
    assert expected in casting_violations(snippet)


def test_docstring_mentions_are_not_violations() -> None:
    assert casting_violations('"""float locale quantize"""\nx = 1\n') == []


def test_no_module_in_the_casting_package_imports_locale() -> None:
    for path in _casting_files() + [CONTRACT]:
        assert "locale" not in {
            a.name.split(".")[0]
            for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.Import)
            for a in n.names
        }, path


# -- single catalog source ---------------------------------------------------------------------

FORMAT_ID = re.compile(
    r"""["'](?:string|integer|decimal|date|currency|cobol|sentinel)/[a-z0-9\-]+/v1["']"""
)
CATALOG_HOMES = {
    "contracts/declared_casting.py",
    "declared_casting/_catalog.py",
}


def test_format_ids_live_only_in_the_catalog_and_contract() -> None:
    assert all((SRC / home).is_file() for home in CATALOG_HOMES)
    stray = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        if rel in CATALOG_HOMES:
            continue
        if FORMAT_ID.search(path.read_text()):
            stray.append(rel)
    assert stray == [], f"format-name allowlists copied outside the catalog: {stray}"


def test_every_descriptor_declares_an_inverse_width_kind_and_vectors() -> None:
    from decoy_engine.declared_casting import capability_manifest, descriptors

    all_desc = descriptors()
    assert all_desc
    ids = [d.format_id for d in all_desc]
    assert len(ids) == len(set(ids)) or len({(d.format_id, d.direction) for d in all_desc}) == len(
        all_desc
    )
    for d in all_desc:
        assert d.inverse is not None, d.format_id
        assert d.physical_kind in {"text", "binary"}
        assert d.vectors, f"{d.format_id} has no registered vectors"
        assert d.width_rule in {"exact", "bounded", "unknown"}
    manifest = capability_manifest()
    assert [f["id"] for f in manifest["formats"]] == sorted(f["id"] for f in manifest["formats"])


def test_catalog_manifest_snapshot_is_pinned() -> None:
    import json

    from decoy_engine.declared_casting import capability_manifest

    pinned = ROOT / "tests" / "fixtures" / "f06" / "capability-manifest-v1.json"
    assert pinned.is_file()
    assert json.loads(pinned.read_text()) == json.loads(json.dumps(capability_manifest()))


def test_matrix_every_output_descriptor_has_exact_width_or_a_registered_rejection() -> None:
    from decoy_engine.declared_casting import (
        ROUTES,
        descriptors,
        evaluate_declared_casting,
        sample_contract_for,
    )

    for d in descriptors():
        for route in ROUTES:
            decision = evaluate_declared_casting(
                route=route,
                plan_contracts=(sample_contract_for(d),),
                source_facts={},
                target_facts={"kind": "fixed_width"},
                capabilities=None,
            )
            assert decision.admitted or decision.rejection is not None
            if decision.admitted:
                assert decision.output_width(d.format_id) is not None or d.direction == "parse"
            else:
                from decoy_engine import reasons

                assert reasons.is_registered(str(decision.rejection))


def test_unknown_width_allowed_mutant_is_caught() -> None:
    from decoy_engine.declared_casting import (
        ROUTES,
        evaluate_declared_casting,
        sample_unknown_width_contract,
    )

    decision = evaluate_declared_casting(
        route=ROUTES[0],
        plan_contracts=(sample_unknown_width_contract(),),
        source_facts={},
        target_facts={"kind": "fixed_width"},
        capabilities=None,
    )
    assert not decision.admitted and str(decision.rejection) == "declared_output_width_unknown"


# -- issue accounting and mutation harness -----------------------------------------------------


def test_every_issue_is_counted_exactly_once() -> None:
    from decoy_engine.declared_casting import CastIssue, CastIssueLedger

    ledger = CastIssueLedger()
    issues = [
        CastIssue(
            table="t",
            column="a",
            row=0,
            stage="parse",
            reason="declared_date_invalid",
            disposition="null",
        ),
        CastIssue(
            table="t",
            column="b",
            row=0,
            stage="parse",
            reason="declared_integer_invalid",
            disposition="quarantine",
        ),
        CastIssue(
            table="t",
            column="a",
            row=1,
            stage="format",
            reason="declared_output_width_overflow",
            disposition="quarantine",
        ),
    ]
    for i in issues:
        ledger.add(i)
    counts = ledger.counts()
    assert counts["total_failures"] == 3
    assert counts["total_substitutions"] == 1
    assert ledger.quarantined_rows() == {("t", 0), ("t", 1)}
    with pytest.raises(Exception):
        ledger.add(issues[0])  # the same (table, column, row, stage) cannot be counted twice


GUARD_TAG = re.compile(r"#\s*guard:(?P<name>[a-z0-9_]+)\s*$")


def _mutants(source: str) -> list[tuple[str, str]]:
    """One mutant per `# guard:<name>` line: the guarded condition is replaced with `False`."""
    out = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        m = GUARD_TAG.search(line)
        if not m:
            continue
        stripped = line[: m.start()].rstrip()
        mm = re.match(r"^(\s*)(if|elif)\s+(.*):$", stripped)
        assert mm, f"guard tag must sit on an if/elif line: {line!r}"
        mutated = list(lines)
        mutated[i] = f"{mm.group(1)}{mm.group(2)} False:"
        out.append((m.group("name"), "\n".join(mutated) + "\n"))
    return out


def test_every_numeric_guard_is_tagged_and_a_mutant_is_killed_by_the_vectors() -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _f06_mutation_battery import run_battery

    src = (CASTING / "_numeric.py").read_text()
    mutants = _mutants(src)
    names = {n for n, _ in mutants}
    assert {
        "integer_range",
        "decimal_precision",
        "decimal_scale_loss",
        "width_overflow",
        "decimal_trap",
    } <= names
    survivors = []
    for name, text in mutants:
        if run_battery(textwrap.dedent(text)):  # the battery still passes: the mutant survived
            survivors.append(name)
    assert survivors == []
    assert run_battery(src) is True  # the unmutated module passes the same battery
