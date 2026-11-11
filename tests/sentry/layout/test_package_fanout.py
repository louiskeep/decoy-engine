"""Sentry: no package directory holds more than 25 modules, and the oversize ones only shrink.

A package with dozens of loose modules has no structure a reader can learn, so the cap is
25 direct ``*.py`` files per directory (``__init__.py`` and sub-packages are not counted).
Packages already over the cap are frozen at their count in ``FANOUT_ALLOWLIST``: the count
must match exactly and may only fall. The next module goes into a named sub-package from the
"Where Things Go" map in ``CODEMAP.md``, which the last test ties to the live directory tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.sentry.layout import (
    FANOUT_TERMS,
    direct_modules,
    ignore_keys,
    ratchet_verdict,
    section,
)

ROOT = Path(__file__).resolve().parents[3]
PKG = ROOT / "src" / "decoy_engine"
LIMIT = 25
LAYOUT_HEADING = "## Where Things Go"

# Frozen packages: exact direct-module count at the build base; may only shrink.
# Each entry names the map's target sub-package for the next module.
FANOUT_ALLOWLIST: dict[str, int] = {
    # next route cluster: execution/chunked/, execution/sequential/ or execution/isolated/.
    "src/decoy_engine/execution": 104,
    # next family: execution/_strategies/<family>/.
    "src/decoy_engine/execution/_strategies": 29,
    # next modules: execution/native/draw/ or execution/native/chunked/.
    "src/decoy_engine/execution/native": 55,
    # next stream modules: execution/out_of_core/stream/.
    "src/decoy_engine/execution/out_of_core": 44,
    # next checks: plan/checks/.
    "src/decoy_engine/plan": 30,
    # new tools go in a topic folder such as scripts/dev/ or scripts/audit/.
    "scripts": 26,
}


def fanout_dirs() -> list[Path]:
    """Every directory under ``src/decoy_engine`` and ``scripts`` that holds a module."""
    found: set[Path] = set()
    for top in (PKG, ROOT / "scripts"):
        for d in [top, *top.rglob("*")]:
            if d.is_dir() and "__pycache__" not in d.parts and direct_modules(d):
                found.add(d)
    return sorted(found)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("directory", fanout_dirs(), ids=rel)
def test_package_fanout_within_limit(directory: Path) -> None:
    key = rel(directory)
    reason = ratchet_verdict(
        key,
        direct_modules(directory),
        FANOUT_ALLOWLIST.get(key),
        goal=LIMIT,
        limit=LIMIT,
        margin=0,
        terms=FANOUT_TERMS,
    )
    assert reason is None, reason


def test_allowlist_entries_exist_and_are_still_over_the_limit() -> None:
    for key, count in FANOUT_ALLOWLIST.items():
        assert (ROOT / key).is_dir(), f"FANOUT_ALLOWLIST lists a missing directory: {key}"
        assert count > LIMIT, f"{key}: frozen at {count}, not over {LIMIT}; delete the entry"


def span_paths(markdown: str) -> set[str]:
    """Repo-relative paths named in code spans of the map, without a trailing slash."""
    spans: set[str] = set()
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        for chunk in line.split("`")[1::2]:
            spans.add(chunk.removesuffix("/"))
    return spans


def unmapped_subpackages(frozen: list[Path], mapped: set[str], base: Path) -> list[str]:
    """Direct sub-directories of the frozen packages that no map cell names."""
    out = []
    for pkg in frozen:
        for sub in sorted(p for p in pkg.iterdir() if p.is_dir() and p.name != "__pycache__"):
            path = sub.relative_to(base).as_posix()
            if path not in mapped:
                out.append(path)
    return out


def test_every_subpackage_of_a_frozen_package_is_in_the_map() -> None:
    text = section((ROOT / "CODEMAP.md").read_text(encoding="utf-8"), LAYOUT_HEADING)
    frozen = [ROOT / key for key in FANOUT_ALLOWLIST]
    assert unmapped_subpackages(frozen, span_paths(text), ROOT) == []


# --- meta-tests: seeded violations prove each guard sees them ----------------------------


def verdict(count: int, recorded: int | None) -> str | None:
    return ratchet_verdict(
        "pkg", count, recorded, goal=LIMIT, limit=LIMIT, margin=0, terms=FANOUT_TERMS
    )


def test_direct_modules_counts_modules_only(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.py").write_text("", encoding="utf-8")
    assert direct_modules(tmp_path) == 3


def test_a_module_planted_in_a_frozen_package_is_red() -> None:
    message = verdict(105, 104)
    assert message is not None and "may only shrink" in message


def test_a_module_removed_from_a_frozen_package_demands_a_ratchet_down() -> None:
    message = verdict(103, 104)
    assert message is not None and "down to 103" in message


def test_a_26th_module_in_an_unfrozen_package_is_red() -> None:
    assert verdict(25, None) is None
    message = verdict(26, None)
    assert message is not None and "26 modules" in message


def test_an_unmapped_subpackage_is_reported(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "known").mkdir(parents=True)
    (tmp_path / "pkg" / "fresh").mkdir()
    mapped = {"pkg/known"}
    assert unmapped_subpackages([tmp_path / "pkg"], mapped, tmp_path) == ["pkg/fresh"]


def test_ignore_keys_reads_every_ignore_line() -> None:
    config = (
        "[importlinter]\nroot_package = decoy_engine\n\n"
        "[importlinter:contract:layers]\nname = Layers\ntype = layers\n"
        "ignore_imports =\n    decoy_engine.plan -> decoy_engine.execution._errors\n"
        "    decoy_engine.errors  ->  decoy_engine.execution._row_error_summary\n"
    )
    assert ignore_keys(config) == {
        "decoy_engine.plan -> decoy_engine.execution._errors",
        "decoy_engine.errors -> decoy_engine.execution._row_error_summary",
    }
