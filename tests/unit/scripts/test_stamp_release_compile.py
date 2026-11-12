"""AT-7: `stamp_release.py` compiles `changelog.d/` fragments into CHANGELOG.md at release prep.

The scratch repositories commit fragments out of name order, so a compiler that sorts by name
instead of by the commit that added each file goes red. One case runs the compile on a copy of
the real CHANGELOG.md and the real released-sections pin and re-runs the repo's own CHANGELOG
tests on the result.
"""

from __future__ import annotations

import json
import re
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

    # The next minor after the checked-in version, so the test still compiles on the release prep
    # branch, where CHANGELOG.md already holds this version's section.
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    current = re.search(r'(?m)^version = "(\d+)\.(\d+)\.\d+"$', pyproject)
    assert current is not None
    nxt = f"{current[1]}.{int(current[2]) + 1}.0"

    def build(with_fragments: bool) -> Path:
        r = ScratchRepo(tmp_path / ("with" if with_fragments else "without"))
        r.commit(
            {
                "CHANGELOG.md": (REPO / "CHANGELOG.md").read_text(encoding="utf-8"),
                "pyproject.toml": pyproject.replace(current[0], f'version = "{nxt}"', 1),
                "changelog.d/README.md": "fragments\n",
                "tests/fixtures/changelog_released_sections.json": (
                    REPO / "tests/fixtures/changelog_released_sections.json"
                ).read_text(encoding="utf-8"),
            },
            "chore: base",
        )
        if with_fragments:
            _add_three(r)
        sr.compile_fragments(r.path, nxt, "2026-11-10")
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


PIN = "tests/fixtures/changelog_released_sections.json"
OWNED = ("CHANGELOG.md", PIN, "changelog.d")


def _tracked_state(r: ScratchRepo) -> dict[str, str]:
    """Byte content of every file the compiler may touch."""
    out = {}
    for rel in ("CHANGELOG.md", PIN):
        out[rel] = (r.path / rel).read_text(encoding="utf-8")
    for p in sorted((r.path / "changelog.d").glob("*")):
        out[f"changelog.d/{p.name}"] = p.read_text(encoding="utf-8")
    return out


def _inject(monkeypatch, fail_at: int) -> None:
    """Raise OSError on the `fail_at`-th write step: pin, CHANGELOG, then one delete per fragment."""
    calls = {"n": 0}
    real_replace, real_remove = sr._replace, sr._remove

    def tick() -> None:
        calls["n"] += 1
        if calls["n"] == fail_at:
            raise OSError("injected")

    monkeypatch.setattr(sr, "_replace", lambda p, t: (tick(), real_replace(p, t))[1])
    monkeypatch.setattr(sr, "_remove", lambda p: (tick(), real_remove(p))[1])


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
def test_failure_after_any_write_step_restores_head(
    tmp_path: Path, monkeypatch, fail_at: int
) -> None:
    r = _seed(tmp_path)
    _add_three(r)
    before = _tracked_state(r)
    _inject(monkeypatch, fail_at)
    with pytest.raises(OSError, match="injected"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    monkeypatch.undo()
    assert _tracked_state(r) == before
    assert r.git("status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.parametrize(
    "dirty",
    ["CHANGELOG.md", PIN, "changelog.d/z.md", "changelog.d/untracked.md"],
)
def test_dirty_tree_is_refused_before_any_write(tmp_path: Path, dirty: str) -> None:
    r = _seed(tmp_path)
    _add_three(r)
    (r.path / dirty).write_text(
        (r.path / dirty).read_text() + "\n" if (r.path / dirty).exists() else fragment(),
        encoding="utf-8",
    )
    before = _tracked_state(r)
    with pytest.raises(sr.FragmentError, match="uncommitted") as info:
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    assert dirty in str(info.value)
    # An untracked hand-written fragment is the operator's work: the stamp never says to delete it.
    assert "rm -- " not in str(info.value)
    assert _tracked_state(r) == before


def test_a_crash_leaves_a_dirty_tree_that_a_git_restore_clears(tmp_path: Path, monkeypatch) -> None:
    r = _seed(tmp_path)
    _add_three(r)
    # Simulate the process dying before it could roll back.
    monkeypatch.setattr(sr, "_rollback", lambda root: None)
    _inject(monkeypatch, 3)
    with pytest.raises(OSError, match="injected"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    monkeypatch.undo()

    with pytest.raises(sr.FragmentError, match="git restore") as info:
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    assert "CHANGELOG.md" in str(info.value)

    r.git("restore", "--source=HEAD", "--staged", "--worktree", "--", *OWNED)
    assert r.git("status", "--porcelain", "--untracked-files=all") == ""
    # The operator drops a Breaking line, commits, and retries.
    r.commit({"changelog.d/a.md": fragment("Alpha text.", name="a-title")}, "feat: a, no break")
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    text = (r.path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "alpha now breaks" not in text
    assert "Alpha text." in text
    assert not list((r.path / "changelog.d").glob("[!R]*.md"))


def test_an_existing_section_for_the_version_is_a_plain_refusal(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    sr.compile_fragments(r.path, "0.3.0", "2026-11-10")
    r.git("add", "-A")
    r.git("commit", "-q", "-m", "chore: compiled")
    with pytest.raises(sr.FragmentError, match="already has a section"):
        sr.compile_fragments(r.path, "0.3.0", "2026-11-10")


def test_refusals_carry_no_exception_context(tmp_path: Path) -> None:
    r = _seed(tmp_path)
    r.commit({"changelog.d/bad.md": "no heading\n"}, "feat: bad")
    for version, date in (("0.3.0", "2026-02-30"), ("0.3.0", "2026-11-10")):
        with pytest.raises(sr.FragmentError) as info:
            sr.compile_fragments(r.path, version, date)
        assert info.value.__context__ is None


# AT-8: the release stamp moves pending registry facts to one numeric version in the same
# transaction as the changelog compile. The cases stamp a git copy of the engine tree.
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from scripts.release import reason_manifest_policy as pol  # noqa: E402
from tests.unit.test_reason_release_pin import (  # noqa: E402
    CONFIG1,
    CONFIG2,
    EMPTY_MARKERS,
    REGISTRY,
    add_pending_deprecation,
    add_pending_identity,
    add_pending_summary,
    edit_file,
    pin_cli,
)

F29 = "tests/fixtures/f29"
ENGINE_FILES = (
    "src", "scripts/release", "scripts/dev/registry_renumber.py",
    "scripts/dev/_registry_renumber_git.py", F29, PIN,
    "tests/unit/progress/test_consumer_contract.py", "docs/reasons.md", "CHANGELOG.md",
    "changelog.d/README.md", "pyproject.toml",
)  # fmt: skip
DOCSTRING = '"""Catalog of the ``config`` namespace, part 2."""'
PENDING_DOC = (
    '"""Catalog of the ``config`` namespace, part 2: since=UNRELEASED is "unreleased" here."""'
)


def copy_engine(dest: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in ENGINE_FILES:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        if (REPO / rel).is_dir():
            shutil.copytree(REPO / rel, dest / rel, ignore=ignore)
        else:
            shutil.copy2(REPO / rel, dest / rel)


def settle_pending(seed: ScratchRepo) -> None:
    """Stamp whatever the live tree still holds pending, so the seed registry is always clean.

    The cases below count identities and versions from a clean registry plus their own probes;
    a pending reason added to the live tree later must not change those counts.
    """
    if pin_cli(seed.path, "--release-ready").returncode == 0:
        return
    # The policy names every pending identity that no fragment claims, as "namespace.code: ...".
    unclaimed = pol.pending_fragment_violations(manifest(seed.path), [])
    pending = [f"`{v.split(':')[0].replace('.', '/', 1)}`" for v in unclaimed]
    if pending:
        seed.commit(
            {"changelog.d/settled.md": fragment("Adds " + ", ".join(pending) + ".")}, "feat: settle"
        )
    sr.stamp_release(seed.path, "0.0.1", "2026-01-01")
    seed.commit({}, "chore: settle the live registry")


def make_seed(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("engine") / "seed"
    dest.mkdir()
    copy_engine(dest)
    seed = ScratchRepo(dest)
    seed.commit({}, "chore: base")
    settle_pending(seed)
    return dest


def bump(version: str, *, minor: int = 0, patch: int = 0) -> str:
    major, mnr, pat = (int(x) for x in version.split("."))
    return f"{major}.{mnr + minor}.{0 if minor else pat + patch}"


@dataclass(frozen=True)
class Versions:
    """The registry facts of a seed; every expectation below is this plus the case's own probes."""

    base: str
    pin: str

    @property
    def next(self) -> str:
        return bump(self.base, minor=1)

    @property
    def patch1(self) -> str:
        return bump(self.base, patch=1)

    @property
    def patch2(self) -> str:
        return bump(self.base, patch=2)

    @property
    def next_fixture(self) -> str:
        major, minor, _ = self.next.split(".")
        return f"reason-manifest-v{major}.{minor}-release-0-23-0.json"


def versions(seed: Path) -> Versions:
    return Versions(manifest(seed)["registry_version"], _pin(seed)["registry_version"])


@pytest.fixture(scope="module")
def engine_seed(tmp_path_factory) -> Path:
    return make_seed(tmp_path_factory)


def engine_clone(seed: Path, tmp_path: Path) -> ScratchRepo:
    path = tmp_path / "engine"
    subprocess.run(["git", "clone", "-q", str(seed), str(path)], check=True)  # noqa: S603, S607
    clone = ScratchRepo.__new__(ScratchRepo)
    clone.path = path
    return clone


def manifest(path: Path) -> dict:
    code = "import json;from decoy_engine import reasons;print(json.dumps(reasons.manifest()))"
    env = {**os.environ, "PYTHONPATH": str(path / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=path.parent,
        check=True,
    )  # fmt: skip
    return json.loads(res.stdout)


def _fixtures(path: Path) -> set[str]:
    return {p.name for p in (path / F29).glob(pol.FIXTURE_GLOB)}


def _pin(path: Path) -> dict:
    return json.loads((path / F29 / "reason-registry-release.json").read_text(encoding="utf-8"))


PROBES = ("probe_one", "probe_two")


def two_identities(r: ScratchRepo) -> None:
    add_pending_identity(r.path, "probe_one")
    add_pending_identity(r.path, "probe_two")
    edit_file(r.path / CONFIG2, DOCSTRING, PENDING_DOC)
    body = "Adds `config/probe_one` and `config/probe_two`."
    r.commit({"changelog.d/probe.md": fragment(body)}, "feat: two pending reasons")


def deprecation(r: ScratchRepo) -> None:
    add_pending_deprecation(r.path)
    r.commit({}, "feat: deprecate a reason")


def summary(r: ScratchRepo) -> None:
    add_pending_summary(r.path)
    r.commit({}, "fix: correct a summary")


def nothing(r: ScratchRepo) -> None:
    pass


CASES = {
    "identity": two_identities,
    "deprecation": deprecation,
    "summary": summary,
    "noop": nothing,
}


def _stamp(r: ScratchRepo, version: str = "0.23.0") -> str:
    return sr.stamp_release(r.path, version, "2026-11-12")


def _both_gates_pass(r: ScratchRepo) -> None:
    for mode in ("--check", "--release-ready"):
        res = pin_cli(r.path, mode)
        assert res.returncode == 0, (mode, res.stderr[-800:])


def test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor(
    engine_seed: Path, tmp_path: Path
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    v = versions(engine_seed)
    before = _fixtures(r.path)
    two_identities(r)
    assert _stamp(r) == v.next
    live = manifest(r.path)
    assert live["registry_version"] == v.next
    probes = {e["code"]: e["since"] for e in live["entries"] if e["code"] in PROBES}
    assert probes == {"probe_one": v.next, "probe_two": v.next}
    name = v.next_fixture
    assert _fixtures(r.path) - before == {name}
    assert pol.parse_fixture_name(name) == (v.next, "release-0-23-0")
    assert pol.fixture_name(v.next, pol.release_fixture_slug("0.23.0")) == name
    fixture = json.loads((r.path / F29 / name).read_text(encoding="utf-8"))
    assert fixture["registry_version"] == v.next
    assert [e["code"] for e in fixture["entries"]] == ["probe_one", "probe_two"]
    pin = _pin(r.path)
    assert (pin["registry_version"], pin["baseline_ceiling"]) == (v.next, v.next)
    contract = json.loads((r.path / F29 / "consumer-contract.json").read_text(encoding="utf-8"))
    assert pol.contract_versions(contract) == {v.next}
    assert f"`registry_version` (`{v.next}`)" in (r.path / "docs/reasons.md").read_text(
        encoding="utf-8"
    )
    text = (r.path / CONFIG2).read_text(encoding="utf-8")
    assert text.count(f'since="{v.next}")') == 2 and "since=UNRELEASED)" not in text
    assert "import UNRELEASED" not in text and PENDING_DOC in text
    log = (r.path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.23.0] - 2026-11-12" in log and "`config/probe_one`" in log
    assert not (r.path / "changelog.d" / "probe.md").exists()
    _both_gates_pass(r)


@pytest.mark.parametrize("case", ["deprecation", "summary"])
def test_at8_a_patch_release_moves_the_registry_and_keeps_the_ceiling(
    engine_seed: Path, tmp_path: Path, case: str
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    v = versions(engine_seed)
    before = _fixtures(r.path)
    CASES[case](r)
    assert _stamp(r) == v.patch1
    live = manifest(r.path)
    assert live["registry_version"] == v.patch1 and _fixtures(r.path) == before
    pin = _pin(r.path)
    assert (pin["registry_version"], pin["baseline_ceiling"]) == (v.patch1, v.base)
    assert EMPTY_MARKERS in (r.path / REGISTRY).read_text(encoding="utf-8")
    if case == "deprecation":
        entry = next(e for e in live["entries"] if e["code"] == "pipeline_name_invalid")
        assert entry["deprecated_in"] == v.patch1
    _both_gates_pass(r)
    r.commit({}, "chore: release 0.23.0")
    edit_file(r.path / CONFIG1, '"Top level bad mode."', '"Top-level mode is not supported."')
    edit_file(
        r.path / REGISTRY,
        EMPTY_MARKERS,
        EMPTY_MARKERS.replace("frozenset()", 'frozenset({("config", "top_level.bad_mode")})'),
    )
    r.commit({}, "fix: correct another summary")
    assert _stamp(r, "0.24.0") == v.patch2
    pin = _pin(r.path)
    assert (pin["registry_version"], pin["baseline_ceiling"]) == (v.patch2, v.base)
    assert _fixtures(r.path) == before
    _both_gates_pass(r)


def test_at8_a_release_with_nothing_pending_recaptures_the_pin(
    engine_seed: Path, tmp_path: Path
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    v = versions(engine_seed)
    before = _fixtures(r.path)
    assert _pin(r.path)["registry_version"] == v.pin
    assert _stamp(r) == v.base
    pin = _pin(r.path)
    assert (pin["registry_version"], pin["baseline_ceiling"]) == (v.base, v.base)
    assert manifest(r.path)["registry_version"] == v.base and _fixtures(r.path) == before
    _both_gates_pass(r)


@pytest.mark.parametrize(
    ("case", "flag", "ok"),
    [("identity", "next", True), ("identity", "after", False), ("summary", "next", False),
     ("summary", "auto", True), ("noop", "next", False), ("noop", "auto", True)],
)  # fmt: skip
def test_at8_the_cli_accepts_only_the_automatic_registry_minor(
    engine_seed: Path, tmp_path: Path, case: str, flag: str, ok: bool
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    v = versions(engine_seed)
    flag = {"next": v.next, "after": bump(v.next, minor=1)}.get(flag, flag)
    CASES[case](r)
    args = ["--version", "0.23.0", "--date", "2026-11-12", "--repo", str(r.path)]
    assert (sr.main([*args, "--registry-minor", flag]) == 0) is ok
    if not ok:
        assert r.git("status", "--porcelain", "--untracked-files=all") == ""


def test_at8_a_failed_candidate_check_writes_nothing(
    engine_seed: Path, tmp_path: Path, monkeypatch
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    two_identities(r)
    monkeypatch.setattr(pol, "release_postcondition_violations", lambda *a, **k: ["forced"])
    with pytest.raises(sr.FragmentError, match="forced"):
        _stamp(r)
    assert r.git("status", "--porcelain", "--untracked-files=all") == ""


def test_at8_only_a_file_the_stamp_creates_is_named_in_the_rm_hint(
    engine_seed: Path, tmp_path: Path
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    leftover = f"{F29}/reason-manifest-v9.9-release-0-0-1.json"
    hand_written = "changelog.d/hand-written.md"
    (r.path / leftover).write_text("{}\n", encoding="utf-8")
    (r.path / hand_written).write_text(fragment(), encoding="utf-8")
    with pytest.raises(sr.FragmentError, match="uncommitted") as info:
        _stamp(r)
    message = str(info.value)
    assert leftover in message and hand_written in message
    assert message.split("rm -- ", 1)[1] == leftover


@pytest.mark.parametrize("fail_at", [1, 5, 7, 9])
def test_at8_a_failure_while_applying_restores_every_owned_path(
    engine_seed: Path, tmp_path: Path, monkeypatch, fail_at: int
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    two_identities(r)
    _inject(monkeypatch, fail_at)
    with pytest.raises(OSError, match="injected"):
        _stamp(r)
    monkeypatch.undo()
    assert r.git("status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.parametrize(
    ("names", "left"),
    [
        ("UNRELEASED, Kind", "Kind"),
        ("KINDS, UNRELEASED", "KINDS"),
        ("A, UNRELEASED, Kind", "A, Kind"),
    ],
)
def test_stamping_drops_the_unused_unreleased_import_wherever_it_sits(
    names: str, left: str
) -> None:
    head = "from decoy_engine.reasons._types import "
    src = f'{head}{names}\n\nX = _d("x", "X.", since=UNRELEASED)\n'
    assert (
        sr._tools()[2].stamp_source(src, "9.9.0")
        == f'{head}{left}\n\nX = _d("x", "X.", since="9.9.0")\n'
    )
