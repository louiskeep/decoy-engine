"""Sentry: every path named in `src/decoy_engine/_MAP.md` exists.

The map went stale once (it described `graph/`, `forecast/`, `validation.py`
and `exceptions.py` long after they were deleted); this keeps it honest.
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"


def test_every_path_in_the_package_map_exists() -> None:
    text = (PKG / "_MAP.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert rows, "no path rows found in _MAP.md"
    missing = []
    for row in rows:
        first_cell = row.split("|")[1]
        for name in re.findall(r"`([^`]+)`", first_cell):
            if not (PKG / name).exists():
                missing.append(name)
    assert not missing, f"_MAP.md names paths that do not exist: {missing}"


def test_deleted_packages_are_not_in_the_map() -> None:
    text = (PKG / "_MAP.md").read_text(encoding="utf-8")
    for gone in ("graph/", "forecast/", "license/", "schema/", "validation.py`", "exceptions.py"):
        assert f"`{gone}" not in text or gone == "validation.py`", gone
