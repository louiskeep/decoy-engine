"""`tests/_changelog.unreleased_text()` is the one reader of not-yet-released CHANGELOG text.

Since the changelog fragments move text out of `[Unreleased]` into `changelog.d/`, a test that
reads only the CHANGELOG section stops seeing it.
"""

from __future__ import annotations

from pathlib import Path

from tests._changelog import unreleased_text

CHANGELOG = (
    "# Changelog\n\n## [Unreleased]\n\nIn the section.\n\n## [0.1.0] - 2026-10-01\n\nReleased.\n"
)


def _root(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("Readme text.\n", encoding="utf-8")
    return tmp_path


def test_returns_the_section_and_every_fragment(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "changelog.d" / "b.md").write_text(
        "### Added (b, S, d)\n\nBee text.\n", encoding="utf-8"
    )
    (root / "changelog.d" / "a.md").write_text(
        "### Added (a, S, d)\n\nAy text.\n", encoding="utf-8"
    )
    text = unreleased_text(root)
    assert "In the section." in text
    assert "Ay text." in text and "Bee text." in text
    assert text.index("Ay text.") < text.index("Bee text."), "fragments read in name order"


def test_leaves_out_the_readme_and_released_sections(tmp_path: Path) -> None:
    text = unreleased_text(_root(tmp_path))
    assert "Readme text." not in text
    assert "Released." not in text


def test_works_without_a_fragment_folder(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    assert "In the section." in unreleased_text(tmp_path)
