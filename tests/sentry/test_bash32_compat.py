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
    # bash < 4.4 under set -u dies on an empty ${a[@]} or ${a[*]}, quoted or not; write the
    # guarded form ${a[@]+"${a[@]}"} (or ${a[*]+${a[*]}}). Counts like ${#a[@]} are safe.
    "bare array expansion": re.compile(
        r"(?<!\[[@*]\]\+\")(?<!\[[@*]\]\+)\$\{[A-Za-z_][A-Za-z0-9_]*\[[@*]\](?!\+)"
    ),
}


def _strip_comment(line: str) -> str:
    """The line up to its comment: the first # outside quotes at line start or after whitespace.

    A # inside quotes or inside ${#...} is not a comment. A quote is escaped only by an odd run
    of backslashes, outside single quotes, as in bash."""
    quote = ""
    backslashes = 0
    for i, ch in enumerate(line):
        escaped = backslashes % 2 == 1
        backslashes = backslashes + 1 if ch == "\\" and quote != "'" else 0
        if quote:
            if ch == quote and (quote == "'" or not escaped):
                quote = ""
        elif ch in "'\"" and not escaped:
            quote = ch
        elif ch == "#" and not escaped and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def findings(text: str) -> list[str]:
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        code = _strip_comment(line)
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
        ('for p in "${A[@]}"; do :; done', "bare array expansion"),
        ("echo ${A[*]}", "bare array expansion"),
        ('echo "x ${A[@]}"', "bare array expansion"),
        ('f "${A[@]:1}"', "bare array expansion"),
        ('[ ${#A[@]} -eq 0 ] && f "${A[@]}"', "bare array expansion"),
    ],
)
def test_seeded_bash4_features_are_caught(snippet: str, feature: str) -> None:
    assert any(feature in f for f in findings(snippet))


def test_bash3_safe_loop_is_clean() -> None:
    assert findings('while IFS= read -r p; do A+=("$p"); done < <(ls)') == []
    assert findings('for p in ${A[@]+"${A[@]}"}; do :; done') == []
    assert findings('echo "${A[*]+${A[*]}}" ${#A[@]}  # count is fine') == []
    assert findings('echo "# literal"  # f "${A[@]}"') == []
    assert findings('echo "\\\\"  # f "${A[@]}"') == []
    assert findings('echo "\\\\" "quoted # text" f "${A[@]}"') != []
    assert findings('echo \\\\" # literal" "${A[@]}"') != []
    assert findings("echo 'a # b' \"${A[@]}\"") != []
