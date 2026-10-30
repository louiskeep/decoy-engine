"""F06 construction sentries: the casting boundary cannot drift, leak, or fork per route.

Each scanner is paired with seeded mutants so the build breaks if a scanner stops being able to
fail. Scans are AST-based; a docstring that mentions `float` or `locale` is not a violation.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "decoy_engine"
CASTING = SRC / "declared_casting"
CONTRACT = SRC / "contracts" / "declared_casting.py"

# Route owners: every production function that admits, selects or runs an execution route must
# route its F06 decision through the one central evaluator. (module, function) pairs.
ROUTE_OWNERS = {
    ("execution/_chunked.py", "check_chunked_compatibility"),
    ("execution/_mixed_static.py", "admit_mixed_static"),
    ("execution/_pipeline_chunk_route.py", "decide_chunk_route"),
    ("execution/_pipeline_mixed_route.py", "run_mixed_route"),
    ("execution/_pipeline_route_exec.py", "run_mixed_route"),
    ("execution/_pipeline_route_exec.py", "run_out_of_core_route"),
    ("execution/_pipeline_route_exec.py", "run_sequential_route"),
    ("execution/_planner.py", "classify_job"),
    ("execution/_sequential.py", "run_sequential"),
    ("execution/_unified_slice.py", "maybe_run_unified_slice"),
    ("execution/out_of_core/_compat.py", "check_out_of_core_compatibility"),
    ("execution/out_of_core/_runner.py", "run_fk_out_of_core"),
}
# Pure delegators: they pick a route only through predicates that are themselves route owners
# above (the OOC compatibility check, the chunk planner, the mixed static admission, the
# unified-slice admission), so a second evaluator call here would be a second, divergent vote.
DELEGATING_ROUTERS = {
    ("execution/_pipeline_context.py", "decide_route"),
    ("execution/_pipeline_routing_signals.py", "resolve_execution_route"),
}
# Who APPLIES the boundary for each route the capability table lets run a declared column. A route
# that claims a capability without an entry here (the r1 defect: capabilities declared for every
# route, boundary wired for one) fails `test_every_capable_route_is_wired`.
ROUTE_APPLIERS = {
    "full_frame": (
        "execution/_pipeline_full_frame.py",
        "_run_full_frame",
        {"pre_mask", "post_mask"},
    ),
    "unified_slice": (
        "execution/_pipeline_full_frame.py",
        "_render_and_seal",
        {"post_mask", "finish"},
    ),
    "chunked": (
        "execution/_pipeline_auto_chunk.py",
        "_declared_wrap",
        {"cast_stream", "format_stream"},
    ),
    "mixed": ("execution/_pipeline_mixed_route.py", "_run_admitted", {"run_auto_chunk", "finish"}),
    "sequential": (
        "execution/_sequential.py",
        "run_sequential",
        {"pre_mask", "post_mask", "finish"},
    ),
    "out_of_core": (
        "execution/out_of_core/_runner.py",
        "run_fk_out_of_core",
        {"stage_typed_sources", "finish"},
    ),
}
# Route executors that own the run's lifetime: they open the boundary and abort it on failure.
RUNTIME_ADAPTERS = {
    ("execution/_pipeline_full_frame.py", "run_full_frame_route"),
    ("execution/_pipeline_mixed_route.py", "run_mixed_route"),
    ("execution/_pipeline_route_dispatch.py", "execute_out_of_core_route"),
    ("execution/_pipeline_route_dispatch.py", "execute_sequential_route"),
}
ROUTE_FN = re.compile(
    r"^(run_.*route|run_sequential|run_fk_out_of_core|check_.*compat.*|admit_.*|decide_chunk_route"
    r"|resolve_execution_route|maybe_run_unified_slice|classify_job)$"
)
# Module-level functions only: methods are internals of an owner already in the inventory.
EXTRA_DISCOVERED = {("execution/_pipeline_context.py", "decide_route")}


def _functions(source: str) -> dict[str, ast.AST]:
    tree = ast.parse(source)
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


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
            node, {"evaluate_declared_casting", "declared_route_guard", "declared_rejection"}
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
        for name in _functions(path.read_text()):
            if ROUTE_FN.match(name) and not name.startswith("_"):
                found.add((rel, name))
    known = ROUTE_OWNERS | RUNTIME_ADAPTERS | DELEGATING_ROUTERS
    found |= EXTRA_DISCOVERED
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
        assert _calls_name(node, {"DeclaredCastRun", "open_cast_run"}), (rel, fn)
        assert _calls_name(node, {"abort"}), (rel, fn, "must abort the quarantine on failure")


def unwired_routes(capable: set[str], appliers: dict[str, tuple], read) -> list[str]:
    """Capable routes with no applier, or whose applier does not call the shared kernel."""
    bad = sorted(capable - set(appliers))
    for route, (rel, fn, calls) in sorted(appliers.items()):
        node = _functions(read(rel)).get(fn)
        if route in capable and (node is None or not all(_calls_name(node, {c}) for c in calls)):
            bad.append(route)
    return bad


def test_every_capable_route_is_wired() -> None:
    from decoy_engine.declared_casting._eligibility import ROUTE_CAPABILITIES

    capable = {route for route, caps in ROUTE_CAPABILITIES.items() if caps}
    assert unwired_routes(capable, ROUTE_APPLIERS, _read) == []


def test_a_capable_route_without_a_wired_applier_fails_the_inventory() -> None:
    sources = {"x.py": "def apply(run):\n    return run.pre_mask({})\n"}
    applier = {"chunked": ("x.py", "apply", {"cast_stream"})}
    assert unwired_routes({"chunked"}, applier, sources.__getitem__) == ["chunked"]
    assert unwired_routes({"chunked", "mixed"}, {}, sources.__getitem__) == ["chunked", "mixed"]
    good = {"chunked": ("x.py", "apply", {"pre_mask"})}
    assert unwired_routes({"chunked"}, good, sources.__getitem__) == []


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
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FORBIDDEN_NAMES
        ):
            out.append(f"name:{node.func.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS and not (
                node.attr == "parse" and not _is_dateutil(node)
            ):
                out.append(f"attr:{node.attr}")
            if node.attr == "quantize" and not allow_quantize:
                out.append("attr:quantize")
            if node.attr == "cast":  # Arrow's unchecked/unsafe cast has no place in the boundary
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
    for path in [*_casting_files(), CONTRACT]:
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
    "contracts/_declared_base.py",
    "contracts/_declared_formats.py",
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


def _plan_column_for(desc, *, with_width: bool):
    """A one-column declared plan whose output format is the descriptor's registered vector."""
    from decoy_engine.contracts.declared_casting import build_contract
    from decoy_engine.declared_casting import descriptor_for
    from decoy_engine.plan._types import PlanDeclaredColumn, freeze_json

    output = dict(desc.vectors[0][0])
    if with_width and "width" in desc.model.model_fields and "width" not in output:
        output.update(width=40, width_unit="code_points")
    parse = dict(descriptor_for(desc.inverse, "parse").vectors[0][0])
    if desc.logical in ("decimal", "currency"):
        parse.update({k: output[k] for k in ("precision", "scale") if k in output})
    contract = build_contract(
        {
            "logical_type": desc.logical,
            "parse_format": parse,
            "error_policy": "null",
            "output_format": output,
        }
    )
    return PlanDeclaredColumn("t", "c", "passthrough", freeze_json(contract.to_payload()))


def test_matrix_every_output_descriptor_has_exact_width_or_a_registered_rejection() -> None:
    from decoy_engine import reasons
    from decoy_engine.declared_casting import (
        ROUTES,
        descriptors,
        evaluate_declared_casting,
        output_width,
    )
    from decoy_engine.declared_casting._eligibility import ROUTE_CAPABILITIES, contract_model

    outputs = [d for d in descriptors() if d.direction == "output"]
    assert outputs
    for d in outputs:
        for with_width in (False, True):
            column = _plan_column_for(d, with_width=with_width)
            for route in ROUTES:
                for target in ("fixed_width", "csv", "parquet"):
                    decision = evaluate_declared_casting(
                        route=route,
                        plan_contracts=(column,),
                        source_facts={},
                        target_facts={"kind": target},
                    )
                    if decision.admitted:
                        assert route in ROUTE_CAPABILITIES, (d.format_id, route)
                        if target == "fixed_width":
                            assert decision.output_width(d.format_id) is not None, d.format_id
                    else:
                        assert reasons.is_registered(str(decision.rejection)), decision
                    out_fmt = contract_model(column).output_format
                    if target == "fixed_width" and output_width(out_fmt) is None:
                        assert str(decision.rejection) == "declared_output_width_unknown"


def test_unknown_width_is_never_admitted_to_a_fixed_width_target() -> None:
    from decoy_engine.declared_casting import descriptor_for, evaluate_declared_casting

    for fid in ("integer/ascii/v1", "decimal/plain/v1", "string/exact/v1", "currency/explicit/v1"):
        column = _plan_column_for(descriptor_for(fid, "output"), with_width=False)
        decision = evaluate_declared_casting(
            route="full_frame", plan_contracts=(column,), target_facts={"kind": "fixed_width"}
        )
        assert not decision.admitted
        assert str(decision.rejection) == "declared_output_width_unknown", fid


# -- issue accounting and mutation harness -----------------------------------------------------


def test_every_issue_is_counted_exactly_once() -> None:
    from decoy_engine import reasons
    from decoy_engine.declared_casting import CastIssue, CastIssueLedger

    ledger = CastIssueLedger()
    issues = [
        CastIssue(
            table="t",
            column="a",
            row=0,
            stage="parse",
            reason=reasons.runtime.DECLARED_DATE_INVALID,
            disposition="null",
        ),
        CastIssue(
            table="t",
            column="b",
            row=0,
            stage="parse",
            reason=reasons.runtime.DECLARED_INTEGER_INVALID,
            disposition="quarantine",
        ),
        CastIssue(
            table="t",
            column="a",
            row=1,
            stage="format",
            reason=reasons.runtime.DECLARED_OUTPUT_WIDTH_OVERFLOW,
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


GUARDS = {
    "_numeric.py": (
        "numeric",
        {"integer_range", "decimal_precision", "decimal_scale_loss", "width_overflow_zero_pad"},
    ),
    "_cobol.py": ("cobol", {"cobol_length", "cobol_digit", "cobol_sign", "comp3_pad_nibble"}),
    "_date.py": ("date", {"date_shape"}),
}


@pytest.mark.parametrize("module", sorted(GUARDS))
def test_every_load_bearing_guard_is_tagged_and_every_mutant_is_killed(module: str) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _f06_mutation_battery import run_battery

    kind, required = GUARDS[module]
    src = (CASTING / module).read_text()
    mutants = _mutants(src)
    assert required <= {n for n, _ in mutants}, f"untagged guards in {module}"
    survivors = [n for n, text in mutants if run_battery(text, kind)]  # still passing: survived
    assert survivors == []
    assert run_battery(src, kind) is True  # the unmutated kernel passes the same battery
