"""Input sentry: `subset/` reads table files only through `_inputs.py`.

Every other subset module sees tables as `TableInput`s, so a path-based Parquet read added
elsewhere would skip the resident and lazy kinds and the policy the input classes carry.
Covered shapes: a call or attribute named `scan_parquet`, `read_parquet` or `read_table`, and a
`from x import` of one of those names.
"""

from __future__ import annotations

import ast
from pathlib import Path

SUBSET = Path(__file__).parents[2] / "src" / "decoy_engine" / "subset"
READERS = frozenset({"scan_parquet", "read_parquet", "read_table"})
ALLOWED = "_inputs.py"


def offenders(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name == ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: set[str] = set()
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.ImportFrom):
                names.update(a.name for a in node.names)
            for name in names & READERS:
                found.append(f"{path.name}:{node.lineno}: {name}")
    return found


def test_subset_reads_parquet_only_through_inputs() -> None:
    assert offenders(SUBSET) == []


def test_the_sentry_is_red_on_a_planted_read(tmp_path: Path) -> None:
    (tmp_path / "_keys.py").write_text(
        "import polars as pl\nframe = pl.scan_parquet('x')\n", encoding="utf-8"
    )
    (tmp_path / "_inputs.py").write_text("frame = pl.scan_parquet('y')\n", encoding="utf-8")
    assert offenders(tmp_path) == ["_keys.py:2: scan_parquet"]


def test_the_sentry_sees_the_import_from_and_arrow_spellings(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("from polars import read_parquet\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import pyarrow.parquet as pq\npq.read_table('x')\n")
    assert len(offenders(tmp_path)) == 2
