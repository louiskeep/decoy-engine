"""Sentry test: only expressions.py may invoke eval / exec / compile.

The engine has an explicit expression-safety policy: every dynamic Python
expression eval flows through the sandboxed `safe_eval` in
`decoy_engine.expressions`. That module has the only legitimate `eval(`
call in the engine. Any new `eval(`, `exec(`, or `compile(` call elsewhere
is treated as a security regression.

This test walks the source tree and fails when an unsafe call lands in any
file outside the documented allowlist. Comments and docstrings are
stripped before matching so prose mentioning eval/exec/compile doesn't
trigger false positives.

If you have a legitimate new use case (extremely rare), update SAFE_PATHS
below in the same PR that adds the call, and the PR carries the tech
lead's review on the safety rationale.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"

# The single audited entry point. Any new entry must be added here and
# carry tech-lead sign-off in the PR that adds it.
SAFE_PATHS = {
    SRC / "expressions.py",
}

# Strip triple-quoted strings (docstrings) and end-of-line comments so
# prose mentioning eval/exec/compile (which is common in safety
# documentation) does not register as a regression. Single-quoted
# strings stay in the search surface, but real eval calls always use
# parentheses, so the pattern below is robust.
_DOCSTRING = re.compile(r'""".*?"""', re.S)
_LINE_COMMENT = re.compile(r"#.*$", re.M)
# Lookbehind avoids false positives on:
#   - re.compile(...)          (preceded by `.`)
#   - safe_eval(...)           (preceded by `_`, a word char)
#   - obj.eval(...)            (preceded by `.`)
# Bare eval/exec/compile/__import__ at the start of an expression
# still match.
_UNSAFE_CALL = re.compile(r"(?<![.\w])(?:eval|exec|compile|__import__)\s*\(")


def _strip_inert(text: str) -> str:
    text = _DOCSTRING.sub("", text)
    text = _LINE_COMMENT.sub("", text)
    return text


@pytest.mark.parametrize(
    "py_file",
    sorted(SRC.rglob("*.py")),
    ids=lambda p: str(p.relative_to(SRC.parent)),
)
def test_no_unsafe_eval_outside_sandbox(py_file: Path) -> None:
    """Fail if any non-safe path contains an unannotated eval/exec/compile.

    Justification text inside a `# noqa: S307` or `# noqa: PLR1722` does
    NOT exempt the call from this sentry. The sentry exists precisely to
    catch any add-back. The only legitimate way around it is to extend
    SAFE_PATHS, which is part of the PR's diff and reviewer's attention.
    """
    if py_file in SAFE_PATHS:
        pytest.skip("audited safe path")
    text = py_file.read_text(encoding="utf-8")
    stripped = _strip_inert(text)
    match = _UNSAFE_CALL.search(stripped)
    assert match is None, (
        f"Unsafe call {match.group(0)!r} found in "
        f"{py_file.relative_to(SRC.parent)}. Route through "
        f"decoy_engine.expressions.safe_eval, or add this file to "
        f"SAFE_PATHS with tech-lead sign-off."
    )


def test_safe_paths_actually_exist() -> None:
    """Catch typos in SAFE_PATHS that would silently make the sentry
    skip nothing meaningful (and therefore catch nothing).
    """
    for p in SAFE_PATHS:
        assert p.exists(), f"SAFE_PATHS lists a nonexistent file: {p}"


def test_sentry_catches_a_planted_violation(tmp_path: Path) -> None:
    """Meta-test: prove the regex actually catches a violation. Without
    this, a refactor that silently disables the regex would not surface
    until a real eval gets added in production code.
    """
    bad_file = tmp_path / "evil.py"
    bad_file.write_text("# benign comment\nresult = eval('1+1')\n")
    text = _strip_inert(bad_file.read_text())
    assert _UNSAFE_CALL.search(text) is not None


def test_sentry_does_not_flag_documentation() -> None:
    """Meta-test: prose in docstrings or comments mentioning eval should
    not register as a regression. Without this, the sentry would block
    legitimate safety documentation.
    """
    sample = '''
"""Module docstring that talks about eval and exec safety."""
# A comment about why we never call eval(
x = 1 + 1  # NOT a call: eval is mentioned but not invoked
'''
    text = _strip_inert(sample)
    assert _UNSAFE_CALL.search(text) is None
