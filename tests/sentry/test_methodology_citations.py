"""Sentry test: every module in the methodology registry has a citation.

The engine has an explicit "use established methodology" rule: crypto,
FK preservation, synth strategies, and similar non-trivial primitives
must cite the source pattern in the implementing module's docstring,
not in inline comments or PR descriptions.

This test enforces it. The registry at `docs/methodology-registry.yaml`
lists the modules that require citations and what each citation should
say. The test:

  1. Loads the registry.
  2. Verifies every path in the registry exists.
  3. Opens each module and reads its first (module-level) docstring.
  4. Asserts the docstring contains a `Pattern:` line that includes the
     name from the registry entry.

Adding or removing a registered module is a single-PR change covering
both the registry and the module docstring; CI fails if they drift.

Reference: docs/v2-app-audit-findings.md, V2 sprint plan §V2.0-prep,
engineering-best-practices.md §6.1.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
REGISTRY_PATH = REPO_ROOT / "docs" / "methodology-registry.yaml"


def _load_registry() -> list[dict]:
    """Parse the YAML registry into a list of entries. Returns an empty
    list if the file is missing so tests fail loudly rather than
    silently passing."""
    if not REGISTRY_PATH.exists():
        return []
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    modules = data.get("modules") or []
    return [m for m in modules if isinstance(m, dict)]


def _module_docstring(path: Path) -> str | None:
    """Extract the module-level docstring via the AST.

    AST extraction is more robust than regex: it handles triple-quoted
    or single-quoted strings, multi-line docstrings, and shebang/
    encoding lines correctly.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover -- defensive
        pytest.fail(f"could not parse {path}: {exc}")
    return ast.get_docstring(tree)


REGISTRY = _load_registry()


def test_registry_is_loadable() -> None:
    """Catch a YAML parse error or empty registry early so downstream
    parametrized tests have something to iterate on. Without this, an
    empty/broken registry would silently make every other test in this
    file pass.
    """
    assert REGISTRY_PATH.exists(), (
        f"methodology registry missing at {REGISTRY_PATH}. The registry "
        f"is the source of truth for which modules require citations; "
        f"deleting it disables the rule."
    )
    assert REGISTRY, (
        "methodology registry has no entries. If the rule no longer "
        "applies (rare), delete the registry file AND this test in the "
        "same PR."
    )


@pytest.mark.parametrize(
    "entry",
    REGISTRY,
    ids=lambda e: e.get("path", "<unknown>"),
)
def test_registered_module_exists_and_has_citation(entry: dict) -> None:
    """Every registry entry must point at a real file and that file's
    docstring must contain a `Pattern:` line naming the source pattern.
    """
    path_str = entry.get("path")
    expected_pattern = entry.get("pattern", "")
    source = entry.get("source", "")
    assert path_str, f"registry entry missing 'path': {entry!r}"
    path = REPO_ROOT / path_str
    assert path.exists(), (
        f"registered module {path_str} not found. Either the module was "
        f"renamed (update registry) or removed (delete the entry)."
    )
    docstring = _module_docstring(path)
    assert docstring, (
        f"{path_str} has no module docstring. Registered modules must "
        f"have a top-of-file docstring with a `Pattern:` line."
    )

    # Match on a line that starts with "Pattern:" (case-insensitive) and
    # contains the expected pattern name. The format the registry uses
    # is "Pattern: <name> (<source>, ...)". We check for the name with
    # forgiving whitespace + case so docstring formatting nits don't
    # block CI.
    lower = docstring.lower()
    assert "pattern:" in lower, (
        f"{path_str}: docstring is missing the `Pattern:` marker. "
        f"Add a line like `Pattern: {expected_pattern} ({source}).` "
        f"to the module docstring."
    )
    assert expected_pattern.lower() in lower, (
        f"{path_str}: `Pattern:` line does not name "
        f"{expected_pattern!r}. Either update the docstring to match "
        f"the registry, or update the registry entry to match the "
        f"current implementation."
    )


def test_no_orphan_pattern_lines_in_unregistered_modules() -> None:
    """Catch the inverse drift: a module that carries a `Pattern:` line
    in its docstring but is NOT in the registry. Either the module
    should be registered, or the docstring marker should not claim
    methodology citation.

    Limited to src/decoy_engine/transforms/*, forecast/*, storm/*, and
    a couple of high-risk modules. A broader scan would over-flag
    modules that mention 'Pattern:' in unrelated contexts.
    """
    candidate_globs = [
        "src/decoy_engine/transforms/*.py",
        "src/decoy_engine/forecast/*.py",
        "src/decoy_engine/storm/*.py",
        "src/decoy_engine/generators/*.py",
        "src/decoy_engine/graph/runner.py",
    ]
    registered = {(REPO_ROOT / e["path"]).resolve() for e in REGISTRY}

    for pattern in candidate_globs:
        for path in REPO_ROOT.glob(pattern):
            if path.name in {"__init__.py", "_base.py"}:
                continue
            doc = _module_docstring(path)
            if not doc:
                continue
            # Only flag modules that explicitly use the "Pattern:"
            # marker. Modules with other docstrings (without the
            # marker) are fine.
            first_lines = "\n".join(doc.splitlines()[:6]).lower()
            if "pattern:" in first_lines and path.resolve() not in registered:
                pytest.fail(
                    f"{path.relative_to(REPO_ROOT)} carries a `Pattern:` "
                    f"line in its docstring but is not in the methodology "
                    f"registry. Either add an entry to "
                    f"docs/methodology-registry.yaml or remove the marker."
                )
