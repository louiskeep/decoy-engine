"""Sentry test: every deprecation declares when it goes away.

engineering-best-practices section 6.5: a deprecation shim must carry a
removal target and must not outlive one minor release. A `DeprecationWarning`
with no stated removal becomes a zombie nobody dares delete.

This sentry walks the source AST, finds every `DeprecationWarning` emission
(`warnings.warn(..., DeprecationWarning, ...)`, including aliased imports),
and requires that the emission declare a removal target in either the warning
message or the enclosing function's docstring. Accepted forms (case-insensitive):

    - "remove in <ver>" / "removed in 2026-Q4" / "remove by v2.1"
    - "Removal: v2.1" / "removal = ..."

Not every `DeprecationWarning` is a shim. A small number are *permanently*
discouraged APIs kept as a deliberate fallback (e.g. a legacy hash used only
when no master key is configured) that emit the warning to steer new callers
away but are not slated for removal. Those are listed in PERMANENT with a
one-line rationale, so the exemption is on the record and reviewed.

Adding a new deprecation therefore forces a choice in the same PR: declare a
removal target, or justify a PERMANENT exemption. Either way it is in the diff.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"
REPO = Path(__file__).parents[2]

# Deprecations that are deliberately permanent (not shims), keyed by
# (path relative to repo, enclosing function name) -> rationale.
PERMANENT: dict[tuple[str, str], str] = {
    (
        "src/decoy_engine/internal/crypto.py",
        "deterministic_hash",
    ): "Permanent legacy fallback used when no master key is configured; the "
    "DeprecationWarning steers new code to hmac_hex but the function is not "
    "slated for removal. Not a time-boxed shim.",
}

_REMOVAL_MARKER = re.compile(
    r"(?i)(\bremov(e|ed|al)\b\s*[:=])"  # "Removal:" / "remove ="
    r"|(\bremove[ds]?\b\s+(in|by)\b)"  # "remove in" / "removed by"
)


def _is_deprecation_warn(call: ast.Call) -> bool:
    """True if this call emits a DeprecationWarning. Handles `warnings.warn`
    and aliased `_warnings.warn`, with the category passed positionally or as
    `category=`."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "warn"):
        return False

    def _names_deprecation(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "DeprecationWarning"
        if isinstance(node, ast.Attribute):
            return node.attr == "DeprecationWarning"
        # Instance form: warnings.warn(DeprecationWarning("msg"), ...)
        if isinstance(node, ast.Call):
            return _names_deprecation(node.func)
        return False

    args_and_kw = list(call.args) + [kw.value for kw in call.keywords]
    return any(_names_deprecation(a) for a in args_and_kw)


def _string_args(call: ast.Call) -> str:
    """Concatenate constant string args of the call (the warning message)."""
    parts: list[str] = []
    for a in ast.walk(call):
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            parts.append(a.value)
    return " ".join(parts)


def _enclosing_funcdef(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    cur = getattr(node, "parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = getattr(cur, "parent", None)
    return None


def _link_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]


def _find_deprecations(py_file: Path) -> list[tuple[str, str]]:
    """Return (func_name, search_text) for each DeprecationWarning emission,
    where search_text is the warning message plus the enclosing docstring."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    _link_parents(tree)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_deprecation_warn(node):
            func = _enclosing_funcdef(node)
            func_name = func.name if func is not None else "<module>"
            docstring = ast.get_docstring(func) if func is not None else None
            text = " ".join(filter(None, [_string_args(node), docstring]))
            found.append((func_name, text))
    return found


@pytest.mark.parametrize(
    "py_file",
    sorted(SRC.rglob("*.py")),
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_deprecations_declare_removal_or_are_permanent(py_file: Path) -> None:
    rel = str(py_file.relative_to(REPO))
    for func_name, text in _find_deprecations(py_file):
        if (rel, func_name) in PERMANENT:
            continue
        assert _REMOVAL_MARKER.search(text), (
            f"{rel}:{func_name} emits a DeprecationWarning with no removal "
            f"target (best-practices section 6.5). State a removal target in "
            f'the warning message or docstring (e.g. "remove in v2.1"), or add '
            f'("{rel}", "{func_name}") to PERMANENT with a rationale.'
        )


def test_permanent_entries_still_emit_deprecation() -> None:
    """Keep PERMANENT honest: an entry whose deprecation was deleted should be
    removed from the list, not left as dead config that exempts nothing."""
    for (rel, func_name), _why in PERMANENT.items():
        path = REPO / rel
        assert path.exists(), f"PERMANENT lists a nonexistent file: {rel}"
        names = {fn for fn, _ in _find_deprecations(path)}
        assert func_name in names, (
            f"PERMANENT entry ({rel}, {func_name}) no longer emits a "
            f"DeprecationWarning. Remove the stale exemption."
        )


def test_sentry_catches_a_planted_violation(tmp_path: Path) -> None:
    """Meta-test: a deprecation with no removal target trips the check."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import warnings\n"
        "def old():\n"
        '    """Legacy thing."""\n'
        '    warnings.warn("use new()", DeprecationWarning, stacklevel=2)\n'
    )
    found = _find_deprecations(bad)
    assert found, "should detect the deprecation emission"
    assert not _REMOVAL_MARKER.search(found[0][1]), "no removal target present"


def test_sentry_accepts_declared_removal(tmp_path: Path) -> None:
    """Meta-test: a deprecation that states a removal target passes."""
    good = tmp_path / "good.py"
    good.write_text(
        "import warnings\n"
        "def old():\n"
        '    """Legacy thing. Remove in v2.1."""\n'
        '    warnings.warn("use new()", DeprecationWarning, stacklevel=2)\n'
    )
    _func, text = _find_deprecations(good)[0]
    assert _REMOVAL_MARKER.search(text)


def test_detects_instance_form_emission(tmp_path: Path) -> None:
    """Meta-test: warn(DeprecationWarning('msg')) (instance form) is detected,
    not just warn('msg', DeprecationWarning)."""
    f = tmp_path / "inst.py"
    f.write_text(
        "import warnings\n"
        "def old():\n"
        "    warnings.warn(DeprecationWarning('use new()'), stacklevel=2)\n"
    )
    assert _find_deprecations(f), "instance-form DeprecationWarning must be detected"
