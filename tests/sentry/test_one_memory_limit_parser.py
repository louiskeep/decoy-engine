"""Sentry: the `"NNMB"` memory-limit string has one parser, `decoy_engine.internal.memory_limit`.

Three copies of the grammar (post-scan runner, post-scan gate, DuckDB helper) had drifted in
what they accept. A new `(\\d+)MB` pattern or `removesuffix("MB")` anywhere else in src fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
CHOKE_POINT = SRC / "internal" / "memory_limit.py"
STRAY = (
    re.compile(r"\(\\d\+\)\s*MB"),
    re.compile(r"""removesuffix\(\s*["']MB["']\s*\)"""),
    re.compile(r"""\.endswith\(\s*["']MB["']\s*\)"""),
)


def stray_parsers(root: Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*.py")):
        if path == CHOKE_POINT:
            continue
        text = path.read_text(encoding="utf-8")
        hits += [f"{path.name}" for rx in STRAY if rx.search(text)]
    return hits


def test_no_second_memory_limit_parser_in_src() -> None:
    assert stray_parsers(SRC) == []


@pytest.mark.parametrize(
    "line",
    ['re.compile(r"^(\\d+)MB\\Z")', 'int(limit.removesuffix("MB"))', "limit.endswith('MB')"],
)
def test_the_scan_sees_a_stray_parser(tmp_path: Path, line: str) -> None:
    (tmp_path / "stray.py").write_text(f"x = {line}\n", encoding="utf-8")
    assert stray_parsers(tmp_path) == ["stray.py"]
