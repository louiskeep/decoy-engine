"""Sentries for the R1 operator registry: single source, leaf module, phase3 removal."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXEC_ROOT = REPO_ROOT / "src" / "decoy_engine" / "execution"
REGISTRY = EXEC_ROOT / "_operator_registry.py"

OPERATOR_IDS = frozenset(
    {
        "native_passthrough",
        "native_redact",
        "native_truncate",
        "native_keyed_hash",
        "native_categorical",
        "native_bucket_perturb",
        "native_group_key",
        "native_date_shift",
        "native_faker_select",
    }
)


def _exported_function_names(tree: ast.Module) -> set[str]:
    """String elements of a module-level `__all__` that name a function defined there."""
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported |= {
                    e.value
                    for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
    return exported & defined


def _operator_id_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt_nodes: set[int] = set()
    exported = _exported_function_names(tree)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and sub.value in exported:
                    exempt_nodes.add(id(sub))
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value in OPERATOR_IDS
        and id(n) not in exempt_nodes
    ]


def test_operator_id_literals_live_only_in_the_registry() -> None:
    offenders = []
    for path in sorted(EXEC_ROOT.rglob("*.py")):
        if path == REGISTRY:
            continue
        for lineno, value in _operator_id_literals(path):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} {value!r}")
    assert not offenders, "operator ids must come from _operator_registry:\n  " + "\n  ".join(
        offenders
    )


def test_the_sentry_exempts_only_exported_function_names(tmp_path: Path) -> None:
    sample = tmp_path / "m.py"
    sample.write_text(
        '__all__ = ["native_redact"]\n\ndef native_redact():\n    pass\n\nX = "native_redact"\n'
    )
    assert _operator_id_literals(sample) == [(6, "native_redact")]
    sample.write_text('__all__ = ["native_redact"]\n')
    assert _operator_id_literals(sample) == [(1, "native_redact")]


def test_registry_imports_only_stdlib_and_pyarrow() -> None:
    tree = ast.parse(REGISTRY.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in the registry"
            roots.add((node.module or "").split(".")[0])
    assert roots <= stdlib | {"pyarrow"}, roots - stdlib - {"pyarrow"}


def test_admission_import_does_not_pull_in_the_physical_seam() -> None:
    code = (
        "import sys, decoy_engine.execution._unified_slice_admission as m; "
        "bad = [k for k in sys.modules if k.startswith('decoy_engine.execution.physical')]; "
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True)  # noqa: S603
    assert result.returncode == 0, result.stderr.decode()


def test_phase3_predicate_is_gone() -> None:
    names = ("phase3_c1_" + "eligibility", "Phase3" + "Eligibility", "_phase3_" + "eligibility")
    me = Path(__file__).resolve()
    hits = []
    for root in (REPO_ROOT / "src", REPO_ROOT / "tests"):
        for path in root.rglob("*.py"):
            if path.resolve() == me:
                continue
            text = path.read_text(encoding="utf-8")
            hits += [f"{path.relative_to(REPO_ROOT)}: {n}" for n in names if n in text]
    assert not hits, "\n".join(hits)


def test_native_route_eligibility_is_still_importable() -> None:
    from decoy_engine.execution.native._plan import native_route_eligibility

    assert callable(native_route_eligibility)
