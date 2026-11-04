"""scripts/dev/*.sh must run on bash 3.2, the stock bash on the macOS gate runner.

mapfile once made the changed-test selection silently empty there, so the gate could pass
without running tests. This greps for the bash 4+ features that bite and seeds a case for each.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parents[2] / "scripts" / "dev").glob("*.sh"))

BASH4_ONLY = {
    "mapfile": re.compile(r"\bmapfile\b"),
    "readarray": re.compile(r"\breadarray\b"),
    "declare -A": re.compile(r"\b(declare|local|typeset)\s+-[a-zA-Z]*A"),
    "case-modifying expansion": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^|,|\^)[^}]*\}"),
    "&>>": re.compile(r"&>>"),
    "negative array index": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-[0-9]+\]\}"),
}


def findings(text: str) -> list[str]:
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0] if not re.search(r"""['"].*#.*['"]""", line) else line
        for name, rx in BASH4_ONLY.items():
            if rx.search(code):
                out.append(f"line {lineno}: {name}")
    return out


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_dev_scripts_avoid_bash4_features(path: Path) -> None:
    assert findings(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("snippet", "feature"),
    [
        ("mapfile -t A < <(ls)", "mapfile"),
        ("readarray -t A < f", "readarray"),
        ("declare -A m", "declare -A"),
        ("x=${v,,}", "case-modifying expansion"),
        ("x=${v^^}", "case-modifying expansion"),
        ("cmd &>> log", "&>>"),
        ('echo "${A[-1]}"', "negative array index"),
    ],
)
def test_seeded_bash4_features_are_caught(snippet: str, feature: str) -> None:
    assert any(feature in f for f in findings(snippet))


def test_bash3_safe_loop_is_clean() -> None:
    assert findings('while IFS= read -r p; do A+=("$p"); done < <(ls)') == []
