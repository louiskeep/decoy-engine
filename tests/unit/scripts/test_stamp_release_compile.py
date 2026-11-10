"""AT-7: `stamp_release.py` compiles `changelog.d/` fragments into CHANGELOG.md at release prep.

The scratch repositories commit fragments out of name order, so a compiler that sorts by name
instead of by the commit that added each file goes red. One case runs the compile on a copy of
the real CHANGELOG.md and the real released-sections pin and re-runs the repo's own CHANGELOG
tests on the result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _fragment_repo import REPO, ScratchRepo, fragment
from scripts.release import stamp_release as sr

OLD_CHANGELOG = """# Changelog

## [Unreleased]

### Added (old body one, 2026-11-01)

Old body text one.

### Breaking for consumers

- An old break.

### Fixed (old body two)

Old body text two.

## [0.2.0] - 2026-11-01

### Added

- thing

## [0.1.0] - 2026-10-01

- first
"""


def _seed(tmp_path: Path) -> ScratchRepo:
    r = ScratchRepo(tmp_path / "r")
    r.commit(
        {
            "CHANGELOG.md": OLD_CHANGELOG,
            "changelog.d/README.md": "fragments\n",
            "tests/fixtures/changelog_released_sections.json": '{\n  "0.1.0": "x"\n}\n',
        },
        "chore: base",
    )
    return r


def _add_three(r: ScratchRepo) -> None:
    """Names sort m, a, z but were added in the order m, a, z: newest is z, then a, then m."""
    r.commit({"changelog.d/m.md": fragment("Middle text.", name="m-title")}, "feat: m")
    r.commit(
        {"changelog.d/a.md": fragment("Alpha text.", name="a-title", breaking="alpha now breaks")},
        "feat: a",
    )
    r.commit({"changelog.d/z.md": fragment("Zed text.", name="z-title")}, "feat: z")


def test_compile_orders_by_commit_and_folds_the_old_body(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    _add_three(r)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    text = (r.path / "CHANGELOG.md").read_text(encoding="utf-8")

    assert text.count("## [0.3.0] - 2026-11-10") == 1
    assert text.index("## [Unreleased]") < text.index("## [0.3.0]") < text.index("## [0.2.0]")
    unreleased = text[text.index("## [Unreleased]") : text.index("## [0.3.0]")]
    assert unreleased.strip() == "## [Unreleased]"

    section = text[text.index("## [0.3.0]") : text.index("## [0.2.0]")]
    order = [section.index(s) for s in ("Zed text.", "Alpha text.", "Middle text.")]
    assert order == sorted(order), "fragments must be newest first"
    assert section.index("Middle text.") < section.index("Old body text one.")
    assert section.index("Old body text one.") < section.index("Old body text two.")

    assert section.count("### Breaking for consumers") == 1
    breaking = section[section.index("### Breaking for consumers") :]
    breaking = breaking[: breaking.index("\n### ", 5)]
    assert "- alpha now breaks" in breaking and "- An old break." in breaking
    assert "Breaking:" not in section

    assert not list((r.path / "changelog.d").glob("[!R]*.md")), "fragments are deleted"
    assert (r.path / "changelog.d" / "README.md").exists()


def test_compile_adds_the_previous_release_to_the_pin(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    _add_three(r)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    text = (r.path / "CHANGELOG.md").read_text(encoding="utf-8")
    pins = json.loads((r.path / "tests/fixtures/changelog_released_sections.json").read_text())
    sections = dict(sr.release_sections(text))
    assert pins["0.2.0"] == sr.section_digest(sections["0.2.0"])
    assert pins["0.1.0"] == "x", "existing rows are never rewritten"
    assert next(iter(pins)) == "0.2.0", "newest first, like the existing pin file"


def test_compile_with_no_fragments_still_folds_the_unreleased_body(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    text = (r.path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Old body text one." in text[text.index("## [0.3.0]") :]


@pytest.mark.parametrize(
    "bad",
    [
        "no heading\n",
        "### Added (a, b, 2026-11-10)\n\nx\n\n### Added (d, e, 2026-11-10)\n\ny\n",
        "Breaking:\n### Added (a, b, 2026-11-10)\n",
    ],
)
def test_compile_refuses_a_malformed_fragment_and_writes_nothing(tmp_path: Path, bad: str) -> None:
    r = _seed(tmp_path)
    r.commit({"changelog.d/ok.md": fragment()}, "feat: ok")
    r.commit({"changelog.d/bad.md": bad}, "feat: bad")
    before = (r.path / "CHANGELOG.md").read_text(encoding="utf-8")
    with pytest.raises(sr.FragmentError, match="bad.md"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    assert (r.path / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert (r.path / "changelog.d" / "ok.md").exists()


def test_compile_refuses_a_shallow_clone(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    _add_three(r)
    shallow = tmp_path / "shallow"
    r.git("clone", "-q", "--depth", "1", f"file://{r.path}", str(shallow))
    with pytest.raises(sr.FragmentError, match="shallow"):
        sr.compile_fragments(shallow, "0.3.0", "2026-11-10")


def test_compile_refuses_an_uncommitted_fragment(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    (r.path / "changelog.d" / "loose.md").write_text(fragment(), encoding="utf-8")
    with pytest.raises(sr.FragmentError, match="loose.md"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")


@pytest.mark.parametrize("version", ["0.3", "v0.3.0", "0.3.0\n", ""])
def test_compile_refuses_a_bad_version(tmp_path: Path, version: str) -> None:
    r = _seed(tmp_path)
    with pytest.raises(sr.FragmentError, match="version"):
        sr.compile_fragments(r.path, version, "2026-11-10")


def test_compiler_parses_through_parse_fragment(tmp_path: Path, monkeypatch) -> None:
    r = _seed(tmp_path)
    _add_three(r)
    seen: list[str] = []
    real = sr.parse_fragment

    def spy(text: str) -> sr.Fragment:
        seen.append(text)
        return real(text)

    monkeypatch.setattr(sr, "parse_fragment", spy)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    assert len(seen) == 3


def test_compile_on_the_real_changelog_keeps_the_repo_changelog_tests_green(
    tmp_path: Path, monkeypatch
) -> None:
    from tests.unit import test_changelog_breaking_ratchet as ratchet
    from tests.unit import test_version_agreement as agreement

    def build(with_fragments: bool) -> Path:
        r = ScratchRepo(tmp_path / ("with" if with_fragments else "without"))
        r.commit(
            {
                "CHANGELOG.md": (REPO / "CHANGELOG.md").read_text(encoding="utf-8"),
                "pyproject.toml": (REPO / "pyproject.toml")
                .read_text(encoding="utf-8")
                .replace('version = "0.19.0"', 'version = "0.20.0"', 1),
                "changelog.d/README.md": "fragments\n",
                "tests/fixtures/changelog_released_sections.json": (
                    REPO / "tests/fixtures/changelog_released_sections.json"
                ).read_text(encoding="utf-8"),
            },
            "chore: base",
        )
        if with_fragments:
            _add_three(r)
        sr.compile_fragments(r.path, "0.20.0", "2026-11-10")
        return r.path

    base = build(False)
    root = build(True)
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    monkeypatch.setattr(agreement, "ROOT", root)
    agreement.test_newest_changelog_section_is_this_version_and_dated()
    agreement.test_changelog_released_sections_descend()

    monkeypatch.setattr(ratchet, "CHANGELOG", root / "CHANGELOG.md")
    monkeypatch.setattr(ratchet, "PINS", root / "tests/fixtures/changelog_released_sections.json")
    ratchet.test_released_sections_match_their_pins()
    # The old body brings its own untriaged break phrases (a release-prep job, not the compiler's);
    # the fragments must add none beyond those.
    baseline, _ = ratchet._untriaged_and_stale((base / "CHANGELOG.md").read_text(encoding="utf-8"))
    now, _ = ratchet._untriaged_and_stale(text)
    assert now == baseline
    assert text.count("### Breaking for consumers") >= 1
    assert "- alpha now breaks" in text


def _clean_result(tmp_path: Path) -> tuple[str, str]:
    r = _seed(tmp_path / "clean")
    _add_three(r)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    pin = "tests/fixtures/changelog_released_sections.json"
    return (r.path / "CHANGELOG.md").read_text(), (r.path / pin).read_text()


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
def test_failure_after_any_write_step_is_recoverable(
    tmp_path: Path, monkeypatch, fail_at: int
) -> None:
    """Steps: pin write, CHANGELOG write, then one delete per fragment (5 in all)."""
    expected = _clean_result(tmp_path)
    r = _seed(tmp_path / "faulty")
    _add_three(r)
    calls = {"n": 0}
    real_replace, real_remove = sr._replace, sr._remove

    def tick() -> None:
        calls["n"] += 1
        if calls["n"] == fail_at:
            raise OSError("injected")

    monkeypatch.setattr(sr, "_replace", lambda p, t: (tick(), real_replace(p, t))[1])
    monkeypatch.setattr(sr, "_remove", lambda p: (tick(), real_remove(p))[1])
    with pytest.raises(OSError, match="injected"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    monkeypatch.undo()

    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    pin = "tests/fixtures/changelog_released_sections.json"
    assert ((r.path / "CHANGELOG.md").read_text(), (r.path / pin).read_text()) == expected
    assert not list((r.path / "changelog.d").glob("[!R]*.md"))
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")  # finished state: a no-op
    assert (r.path / "CHANGELOG.md").read_text() == expected[0]


def test_rerun_with_a_different_date_after_a_finished_compile_is_refused(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    with pytest.raises(sr.FragmentError, match="already"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-11")


def test_unrelated_fragment_beside_a_compiled_section_is_refused(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    r.commit({"changelog.d/late.md": fragment("Late text.", name="late")}, "feat: late")
    with pytest.raises(sr.FragmentError, match="already"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")


def test_refusals_carry_no_exception_context(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    r.commit({"changelog.d/bad.md": "no heading\n"}, "feat: bad")
    for version, date in (("0.3.0", "2026-02-30"), ("0.3.0", "2026-11-10")):
        with pytest.raises(sr.FragmentError) as info:
            sr.compile_fragments(r.path, version, date)
        assert info.value.__context__ is None
