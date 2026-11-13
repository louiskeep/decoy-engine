"""Sentry: one staging writer. Declared casting and the subset stage both stage lazy sources
through `declared_casting.stage_typed_sources`; neither opens a writer of its own.

Covers: the two staging callers only (`CALLERS`), for the names in `WRITER_NAMES`. Does not
cover: any other module under `execution/` or `declared_casting/` (the out-of-core stage, the
sinks and the isolated-run writers hold writers of their own and are not callers of this
function), a differently named equivalent, or direct scratch allocation. The scan stays at two
files because widening it to those packages finds existing writers that this sentry cannot
classify without an allowlist.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
OWNER = ("declared_casting/_stream.py", "stage_typed_sources")
CALLERS = ("declared_casting/_stream.py", "execution/pipeline/_subset_stage.py")
WRITER_NAMES = frozenset({"open_private", "ParquetWriter", "parquet_writer", "write_table"})


def writer_uses(source: str) -> set[tuple[str, int]]:
    """(enclosing top-level function, line) of every writer name in `source`."""
    found: set[tuple[str, int]] = set()
    for top in ast.parse(source).body:
        scope = top.name if isinstance(top, (ast.FunctionDef, ast.ClassDef)) else "<module>"
        for node in ast.walk(top):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.name
                if isinstance(node, ast.alias)
                else None
            )
            if name in WRITER_NAMES:
                found.add((scope, node.lineno))
    return found


def test_only_the_staging_function_holds_a_writer() -> None:
    for rel in CALLERS:
        uses = writer_uses((SRC / rel).read_text(encoding="utf-8"))
        outside = sorted(u for u in uses if not (rel == OWNER[0] and u[0] == OWNER[1]))
        assert not outside, f"{rel}: a writer outside {OWNER[1]}: {outside}"


def test_the_scan_sees_a_planted_writer() -> None:
    planted = (
        "import pyarrow.parquet as pq\n\ndef rogue(sink):\n    return pq.ParquetWriter(sink, s)\n"
    )
    assert writer_uses(planted) == {("rogue", 4)}


def test_the_subset_stage_calls_the_shared_function() -> None:
    text = (SRC / CALLERS[1]).read_text(encoding="utf-8")
    assert "stage_typed_sources(" in text
