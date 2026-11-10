"""AT-6: `scripts/dev/check_fragment.py` is the merge-base CI check for a changelog fragment.

A PR that changes `src/` must add one `changelog.d/<slug>.md` unless every non-merge commit is a
`test`, `docs`, `ci`, `chore` or `refactor` type. The fragment grammar is
`stamp_release.parse_fragment`, shared with the compiler; the checker holds no parser of its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _fragment_repo import REPO, ScratchRepo, fragment
from scripts.dev import check_fragment

SRC = "src/decoy_engine/thing.py"


@pytest.fixture
def repo(tmp_path: Path) -> ScratchRepo:
    r = ScratchRepo(tmp_path / "r")
    r.commit({SRC: "x = 1\n", "changelog.d/README.md": "fragments\n"}, "chore: base")
    r.git("checkout", "-q", "-b", "feature")
    return r


def _run(repo: ScratchRepo, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = check_fragment.main(["--base", "main", "--repo", str(repo.path)])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_feat_commit_touching_src_without_a_fragment_fails(repo, capsys) -> None:
    repo.commit({SRC: "x = 2\n"}, "feat(thing): change it")
    code, out = _run(repo, capsys)
    assert code == 1
    assert "no changelog fragment" in out


@pytest.mark.parametrize("kind", ["test", "docs", "ci", "chore", "refactor"])
def test_exempt_commit_types_pass_without_a_fragment(repo, capsys, kind) -> None:
    repo.commit({SRC: "x = 2\n"}, f"{kind}(thing): change it")
    assert _run(repo, capsys)[0] == 0


def test_one_feat_among_exempt_commits_still_needs_a_fragment(repo, capsys) -> None:
    repo.commit({"tests/t.py": "pass\n"}, "test: a test")
    repo.commit({SRC: "x = 2\n"}, "feat: change it")
    assert _run(repo, capsys)[0] == 1


def test_unparseable_commit_subject_is_not_exempt(repo, capsys) -> None:
    repo.commit({SRC: "x = 2\n"}, "change it somehow")
    assert _run(repo, capsys)[0] == 1


def test_feat_with_a_fragment_passes(repo, capsys) -> None:
    repo.commit({SRC: "x = 2\n", "changelog.d/thing.md": fragment()}, "feat: change it")
    assert _run(repo, capsys)[0] == 0


def test_a_diff_that_leaves_src_alone_passes(repo, capsys) -> None:
    repo.commit({"docs/a.md": "a\n"}, "feat: docs-only but typed feat")
    assert _run(repo, capsys)[0] == 0


@pytest.mark.parametrize(
    "text",
    [
        "no heading at all\n",
        "### Added (a, b, 2026-11-10)\n\ntext\n\n### Changed (d, e, 2026-11-10)\n\ntext\n",
        "Breaking:\n\n### Added (a, b, 2026-11-10)\n\ntext\n",
        "Breaking: x\nsecond line\n### Added (a, b, 2026-11-10)\n",
        "stray line\n### Added (a, b, 2026-11-10)\n\ntext\n",
        "### Added (a, b, 2026-11-10) trailing\n",
        "",
    ],
)
def test_malformed_fragment_fails_even_with_exempt_commits(repo, capsys, text) -> None:
    repo.commit({"changelog.d/bad.md": text}, "docs: add a fragment")
    code, out = _run(repo, capsys)
    assert code == 1
    assert "bad.md" in out


def test_fragment_with_a_bad_name_fails(repo, capsys) -> None:
    repo.commit({"changelog.d/Bad Name.txt": fragment()}, "docs: add a fragment")
    assert _run(repo, capsys)[0] == 1


def test_unknown_base_is_a_precondition_error(repo, capsys) -> None:
    code = check_fragment.main(["--base", "no-such-ref", "--repo", str(repo.path)])
    assert code == 2
    capsys.readouterr()


def test_checker_holds_no_fragment_grammar_of_its_own() -> None:
    """The heading and `Breaking:` syntax live only in stamp_release.parse_fragment."""
    tree = ast.parse((REPO / "scripts" / "dev" / "check_fragment.py").read_text(encoding="utf-8"))
    literals = [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not [s for s in literals if "Breaking:" in s or s.lstrip().startswith("###")]
    assert any(
        isinstance(n, ast.Attribute | ast.Name)
        and getattr(n, "attr", getattr(n, "id", "")) == "parse_fragment"
        for n in ast.walk(tree)
    )


def test_checker_holds_no_file_name_rule_of_its_own() -> None:
    """The `<slug>.md` rule and the README name come from stamp_release, like the heading."""
    tree = ast.parse((REPO / "scripts" / "dev" / "check_fragment.py").read_text(encoding="utf-8"))
    literals = [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not [s for s in literals if "a-z0-9" in s or s.endswith(".md")]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "is_fragment_name" in names


def test_renamed_fragment_passes_the_check_and_compiles(repo, capsys, tmp_path) -> None:
    """One rule for both tools: a path is ordered by the commit that created it."""
    from scripts.release import stamp_release as sr

    repo.commit({"changelog.d/old-name.md": fragment("Renamed text.")}, "feat: add fragment")
    repo.git("mv", "changelog.d/old-name.md", "changelog.d/new-name.md")
    repo.git("commit", "-q", "-m", "docs: rename fragment")
    repo.commit({SRC: "x = 2\n"}, "feat: change src")
    assert _run(repo, capsys)[0] == 0

    repo.commit(
        {
            "CHANGELOG.md": "# C\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-01\n\n- first\n",
            "tests/fixtures/changelog_released_sections.json": "{}\n",
        },
        "chore: seed changelog",
    )
    sr.compile_fragments(repo.path, "0.2.0", "2026-11-10")
    assert "Renamed text." in (repo.path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert not (repo.path / "changelog.d" / "new-name.md").exists()
