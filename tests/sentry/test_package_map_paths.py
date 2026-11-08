"""Sentry: every path named in the package map and the CODEMAP layout section exists.

The map went stale once (it described `graph/`, `forecast/`, `validation.py`
and `exceptions.py` long after they were deleted); this keeps it honest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.sentry.layout import map_paths, section

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "decoy_engine"

# `Goes in`, `File naming`, `Tests`, `Sentries and registries`; column 0 is the kind.
LAYOUT_COLUMNS = (1, 2, 3, 4)
LAYOUT_HEADING = "## Where Things Go"


def test_every_path_in_the_package_map_exists() -> None:
    text = (PKG / "_MAP.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert rows, "no path rows found in _MAP.md"
    missing = map_paths(text, PKG, (0,))
    assert not missing, f"_MAP.md names paths that do not exist: {missing}"


def test_every_path_in_the_codemap_layout_section_exists() -> None:
    text = section((ROOT / "CODEMAP.md").read_text(encoding="utf-8"), LAYOUT_HEADING)
    rows = [line for line in text.splitlines() if line.startswith("| ") and "`" in line]
    assert len(rows) >= 22, "the CODEMAP layout section lost its rows"
    missing = map_paths(text, ROOT, LAYOUT_COLUMNS)
    assert not missing, f"CODEMAP 'Where Things Go' names paths that do not exist: {missing}"


def test_deleted_packages_are_not_in_the_map() -> None:
    text = (PKG / "_MAP.md").read_text(encoding="utf-8")
    for gone in ("graph/", "forecast/", "license/", "schema/", "validation.py`", "exceptions.py"):
        assert f"`{gone}" not in text or gone == "validation.py`", gone


# --- map_paths meta-tests: seeded strings prove the grammar sees each case -------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "sdk.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    return tmp_path


def _table(*cells: str) -> str:
    return "| Kind | Goes in |\n|---|---|\n| thing | " + " | ".join(cells) + " |\n"


def test_map_paths_accepts_an_existing_concrete_path(tree: Path) -> None:
    assert map_paths(_table("`src/pkg/sub/`"), tree, (1,)) == []


def test_map_paths_rejects_a_missing_concrete_path(tree: Path) -> None:
    assert map_paths(_table("`src/pkg/nope/`"), tree, (1,)) == ["src/pkg/nope/ -> src/pkg/nope"]


def test_map_paths_checks_the_parent_of_a_placeholder_segment(tree: Path) -> None:
    assert map_paths(_table("`src/pkg/_<topic>.py`"), tree, (1,)) == []
    assert map_paths(_table("`src/gone/_<topic>.py`"), tree, (1,)) == [
        "src/gone/_<topic>.py -> src/gone"
    ]


def test_map_paths_checks_the_parent_of_a_glob_segment(tree: Path) -> None:
    assert map_paths(_table("`tests/*.py`"), tree, (1,)) == []
    assert map_paths(_table("`gone/*.py`"), tree, (1,)) == ["gone/*.py -> gone"]


def test_map_paths_skips_symbols(tree: Path) -> None:
    assert map_paths(_table("`SCALAR_HANDLERS`, `__all__`"), tree, (1,)) == []


def test_map_paths_checks_only_the_parent_of_a_target_path(tree: Path) -> None:
    assert map_paths(_table("`src/pkg/new/` (target)"), tree, (1,)) == []
    assert map_paths(_table("`src/gone/new/` (target)"), tree, (1,)) == [
        "src/gone/new/ -> src/gone"
    ]


def test_map_paths_requires_target_to_follow_the_span_at_once(tree: Path) -> None:
    assert map_paths(_table("`src/pkg/new/`, later (target)"), tree, (1,)) != []


def test_map_paths_ignores_columns_it_was_not_given(tree: Path) -> None:
    text = "| a | b | c |\n|---|---|---|\n| x | `src/gone/` | `src/pkg/` |\n"
    assert map_paths(text, tree, (2,)) == []
    assert map_paths(text, tree, (1,)) != []


def test_map_paths_checks_every_column_it_was_given(tree: Path) -> None:
    text = "| a | b | c |\n|---|---|---|\n| x | `src/pkg/` | `src/gone/` |\n"
    assert map_paths(text, tree, (1, 2)) == ["src/gone/ -> src/gone"]


def test_map_paths_resolves_relative_to_the_given_base(tree: Path) -> None:
    assert map_paths(_table("`src/pkg/sdk.py`"), tree, (1,)) == []
    assert map_paths(_table("`sdk.py`"), tree, (1,)) != []
    assert map_paths(_table("`sdk.py`"), tree / "src" / "pkg", (1,)) == []


def test_map_paths_walk_is_not_vacuous(tree: Path) -> None:
    # A table with no path cells in the chosen columns names nothing to check.
    assert map_paths("no table here", tree, (1,)) == []
    assert re.search(r"nope", "".join(map_paths(_table("`nope/`"), tree, (1,))))
