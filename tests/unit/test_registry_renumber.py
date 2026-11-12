"""AT-R: scripts/dev/registry_renumber.py renumbers a slice's registry minor and refuses unsafe ones.

Every case clones a scratch repo seeded from the working tree, builds slices the way a builder
does, and runs the real script as a subprocess. Slice codes are synthetic probes. No registry
version literal appears here: versions are read from the clone and computed with ``nxt``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.release.reason_manifest_policy import (
    FIXTURE_GLOB,
    entries_since,
    fixture_name,
    minor_fixture_violations,
    renumber_classification,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "registry_renumber.py"
REG = "src/decoy_engine/reasons/_registry.py"
PLAT, CONF = (
    "src/decoy_engine/reasons/platform/_part1.py",
    "src/decoy_engine/reasons/config/_part1.py",
)
RUNT = "src/decoy_engine/reasons/runtime/_f06.py"
SRC_KINDS = {"a": PLAT, "b": CONF, "c": RUNT}
F29 = "tests/fixtures/f29"
CONTRACT = f"{F29}/consumer-contract.json"
BASE_SRC = "tests/sentry/_baseline_sources.py"
RENUM = "Registry renumbered at merge from"
COPY = ("CHANGELOG.md", "docs/reasons.md", "docs/compatibility-contract.md", F29, BASE_SRC)


def git_res(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        capture_output=True,
        text=True,
    )


def git(repo: Path, *args: str) -> str:
    res = git_res(repo, *args)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def py(repo: Path, *args: str, tree: Path | None = None) -> str:
    env = {**os.environ, "PYTHONPATH": str((tree or repo) / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo.parent,
        env=env,
    ).stdout


def live(repo: Path) -> dict:
    code = "import json;from decoy_engine import reasons;print(json.dumps(reasons.manifest()))"
    return json.loads(py(repo, "-c", code))


def regen_contract(repo: Path) -> None:
    py(repo, str(repo / "tests/unit/progress/test_consumer_contract.py"))


def script(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd
    )


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return script(*args, "--repo", str(repo), "--main", "main", cwd=repo)


def out(res: subprocess.CompletedProcess) -> str:
    return res.stdout + res.stderr


def mver(repo: Path) -> str:
    return re.search(r'^REGISTRY_VERSION = "([^"]*)"$', (repo / REG).read_text(), re.M)[1]


def nxt(version: str, n: int = 1) -> str:
    major, minor, _ = version.split(".")
    return f"{major}.{int(minor) + n}.0"


def fix_name(version: str, slug: str) -> str:
    return fixture_name(version, slug)


def edit(repo: Path, rel: str, fn) -> None:
    path = repo / rel
    path.write_text(fn(path.read_text()), encoding="utf-8")


def commit(repo: Path, msg: str = "c") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def add_code(repo, module: str, ident: str, version: str | None, summary: str = "Probe.") -> None:
    since = f", since={version!r}" if version else ""
    sym = ident.upper()
    edit(repo, module, lambda t: t + f'\n{sym} = _d("{ident}", "{summary}"{since})\n')


def add_slice(repo: Path, slug: str, version: str, *, code: str | None = None, summary=None,
              fixture: bool = True, module: str | None = None) -> None:  # fmt: skip
    """What a builder does: one probe code, version carriers, one fixture, tests and docs."""
    base = mver(repo)
    ident = code or f"probe_{slug}"
    add_code(repo, module or SRC_KINDS[slug], ident, version, summary or f"Probe {slug}.")
    edit(repo, REG, lambda t: re.sub(r'(?m)^(REGISTRY_VERSION = ")[^"]*"', rf'\g<1>{version}"', t))
    edit(
        repo,
        "docs/reasons.md",
        lambda t: t.replace(f"`registry_version` (`{base}`)", f"`registry_version` (`{version}`)"),
    )
    name = fix_name(version, slug)
    if fixture:
        entries = entries_since(live(repo), version)
        body = {**{k: v for k, v in live(repo).items() if k != "entries"}, "entries": entries}
        (repo / F29 / name).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    row = '    "reason-manifest-v1.4-f06.json"'
    edit(
        repo, BASE_SRC, lambda t: t.replace(row, f'    "{name}": "pinned slice fixture",\n{row}', 1)
    )
    (repo / f"tests/unit/test_probe_{slug}.py").write_text(f'FIXTURE = "{name}"\n')
    edit(
        repo,
        "CHANGELOG.md",
        lambda t: t.replace(
            "## [Unreleased]\n",
            f"## [Unreleased]\n\nProbe {slug} lands at registry {version}.\n",
            1,
        ),
    )
    (repo / f"docs/records/probe-{slug}.md").parent.mkdir(exist_ok=True)
    (repo / f"docs/records/probe-{slug}.md").write_text(f"Registry: {base} to {version}.\nThe slice lands at {version}.\n")  # fmt: skip
    regen_contract(repo)
    commit(repo, f"slice {slug}")


@pytest.fixture(scope="session")
def seed(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("seed")
    shutil.copytree(
        ROOT / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    for rel in COPY:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if (ROOT / rel).is_dir() else shutil.copy)(ROOT / rel, dest / rel)
    (dest / "tests/unit/progress").mkdir(parents=True)
    shutil.copy(
        ROOT / "tests/unit/progress/test_consumer_contract.py", dest / "tests/unit/progress"
    )
    (dest / "docs/records").mkdir(parents=True, exist_ok=True)
    git(dest, "init", "-q", "-b", "main")
    commit(dest, "main")
    return dest


@pytest.fixture
def repo(seed: Path, tmp_path: Path) -> Path:
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(seed), str(clone)], check=True)  # noqa: S603, S607
    attrs = clone / ".git/info/attributes"
    attrs.write_text(f"CHANGELOG.md merge=union\n{BASE_SRC} merge=union\n")
    return clone


def land_main(repo: Path) -> None:
    """Merge main into the current branch; an adjacent-line conflict is resolved by hand-edit."""
    if git_res(repo, "merge", "--no-ff", "-q", "-m", "m", "main").returncode:
        unmerged = set(git(repo, "diff", "--name-only", "--diff-filter=U").split())
        assert unmerged <= {REG, CONTRACT, "docs/reasons.md"}, unmerged
        for rel in unmerged & {"docs/reasons.md"}:  # the anchor line moved on both sides
            git(repo, "checkout", "--theirs", rel)
        if REG in unmerged:  # main's schema root next to this branch's version line
            branch_version = re.search(
                r'REGISTRY_VERSION = "([^"]*)"', git_res(repo, "show", f"HEAD:{REG}").stdout
            )[1]
            git(repo, "checkout", "--theirs", REG)
            edit(
                repo,
                REG,
                lambda t: re.sub(
                    r'(?m)^(REGISTRY_VERSION = ")[^"]*"', rf'\g<1>{branch_version}"', t
                ),
            )
        regen_contract(repo)
        commit(repo, "merge main")


def slices(repo: Path, *slugs: str, version: str | None = None, merged: bool = True, **kw) -> str:
    """Branches a (and b, c...) from main at the same minor; a lands in main; b merges main."""
    m0 = mver(repo)
    version = version or nxt(m0)
    for slug in slugs:
        git(repo, "checkout", "-q", "-b", slug, "main")
        add_slice(repo, slug, version, **kw)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", "a", "a")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    git(repo, "checkout", "-q", slugs[1] if len(slugs) > 1 else "a")
    if merged and len(slugs) > 1:
        land_main(repo)
    return m0


# The pending-era --check refuses any numeric slice. A slice the numeric
# renumber left consistent fails with these keys only, so the older carrier checks still hold.
PENDING_ERA = ("registry-version-bump ", "branch-added-fixture", "numeric-since ")


def numeric_only(res: subprocess.CompletedProcess) -> bool:
    lines = [x for x in res.stdout.splitlines() if x]
    return res.returncode == 1 and bool(lines) and all(x.startswith(PENDING_ERA) for x in lines)


def added_tokens(repo: Path, version: str) -> list[str]:
    diff = git(repo, "diff", "-U0", "main", "HEAD").splitlines()
    pat = re.compile(rf"(?<![\w.]){re.escape(version)}(?!\w|\.\d)")
    return [x for x in diff if x.startswith("+") and not x.startswith("+++") and pat.search(x) and RENUM not in x]  # fmt: skip


def test_second_slice_is_renumbered_and_every_artifact_agrees(repo):  # AT-R1
    m0 = slices(repo, "a", "b")
    va, vt = nxt(m0), nxt(m0, 2)
    res = run(repo)
    assert res.returncode == 0, out(res)
    man = {f"{e['namespace']}.{e['code']}": e for e in live(repo)["entries"]}
    assert mver(repo) == vt
    assert man["config.probe_b"]["since"] == vt and man["platform.probe_a"]["since"] == va
    new, old = repo / F29 / fix_name(vt, "b"), repo / F29 / fix_name(va, "b")
    assert new.exists() and not old.exists()
    fx = json.loads(new.read_text())
    assert fx["registry_version"] == vt
    assert fx["entries"] == [man["config.probe_b"]]
    a_fx = f"{F29}/{fix_name(va, 'a')}"
    assert (repo / a_fx).read_text() == git_res(repo, "show", f"main:{a_fx}").stdout
    before = (repo / CONTRACT).read_bytes()
    regen_contract(repo)
    assert (repo / CONTRACT).read_bytes() == before
    assert f'"registry_version": "{vt}"' in before.decode() and f'"{va}"' not in before.decode()
    assert f"`registry_version` (`{vt}`)" in (repo / "docs/reasons.md").read_text()
    log = (repo / "CHANGELOG.md").read_text()
    assert f"Probe b lands at registry {vt}." in log and f"Probe a lands at registry {va}." in log
    rows = (repo / BASE_SRC).read_text()
    assert fix_name(vt, "b") in rows and fix_name(va, "b") not in rows and fix_name(va, "a") in rows
    assert fix_name(vt, "b") in (repo / "tests/unit/test_probe_b.py").read_text()
    rec = (repo / "docs/records/probe-b.md").read_text().splitlines()
    assert vt in rec[0] and rec[1].endswith(f"at {vt}.") and rec[-1].startswith(RENUM)
    commit(repo, "renumber")
    assert added_tokens(repo, va) == []
    assert numeric_only(run(repo, "--check"))


def test_added_lines_that_look_like_diff_headers_are_still_rewritten(repo):  # AT-R21
    m0 = slices(repo, "a", "b", merged=False)
    va, vt = nxt(m0), nxt(m0, 2)
    note = repo / "docs/records/probe-b.md"
    note.write_text(note.read_text() + f"++ note\nlands at {va}.\nb\nalso {va}\n-- tail at {va}\n")
    commit(repo, "headers")
    land_main(repo)
    res = run(repo)
    assert res.returncode == 0, out(res)
    text = note.read_text()
    assert f"lands at {vt}." in text and f"also {vt}" in text and f"-- tail at {vt}" in text
    commit(repo, "renumber")
    assert added_tokens(repo, va) == []
    note.write_text(note.read_text() + f"stale at {va}.\n")
    commit(repo, "leftover")
    res = run(repo, "--check")
    assert res.returncode == 1 and "leftover" in out(res)


def test_a_branch_cut_from_the_main_tip_with_a_copied_since_is_renumbered(repo):  # AT-R22
    m0 = slices(repo, "a", "b", merged=False)
    git(repo, "checkout", "-q", "-b", "c", "main")
    add_slice(repo, "c", nxt(m0), fixture=False)  # main already holds this minor
    res = run(repo)
    assert res.returncode == 0, out(res)
    man = {f"{e['namespace']}.{e['code']}": e for e in live(repo)["entries"]}
    assert man["runtime.probe_c"]["since"] == nxt(m0, 2) and man["platform.probe_a"][
        "since"
    ] == nxt(m0)


def test_renumber_is_idempotent(repo):  # AT-R2
    slices(repo, "a", "b")
    assert run(repo).returncode == 0
    commit(repo, "renumber")
    res = run(repo)
    assert res.returncode == 0 and "nothing to do" in res.stdout
    assert git(repo, "status", "--porcelain") == ""


def test_a_slice_at_main_plus_one_is_a_noop(repo):  # AT-R3
    git(repo, "checkout", "-q", "-b", "a")
    add_slice(repo, "a", nxt(mver(repo)))
    assert run(repo).returncode == 0
    assert numeric_only(run(repo, "--check"))
    assert git(repo, "status", "--porcelain") == ""


def _gap(repo, m0):
    git(repo, "checkout", "-q", "-b", "c", "main")
    add_slice(repo, "c", nxt(m0, 2))


def _doc_only(repo, m0):
    git(repo, "checkout", "-q", "-b", "c", "main")
    edit(repo, "docs/reasons.md", lambda t: t + "\nA note.\n")
    commit(repo)


def _contract_back(repo, m0):
    slices(repo, "a", "b")
    assert run(repo).returncode == 0
    edit(
        repo,
        CONTRACT,
        lambda t: t.replace(
            f'"registry_version": "{nxt(m0, 2)}"', f'"registry_version": "{nxt(m0)}"', 1
        ),
    )
    commit(repo)  # fmt: skip


def _leftover(repo, m0):
    slices(repo, "a", "b")
    assert run(repo).returncode == 0
    edit(repo, "docs/records/probe-b.md", lambda t: t + f"A stale mention at {nxt(m0)}.\n")
    commit(repo)


def _unrenumbered(repo, m0):
    slices(repo, "a", "b")


@pytest.mark.parametrize(
    ("setup", "code", "needle"),
    [(_unrenumbered, 1, "is not"), (_gap, 1, "is not"), (_doc_only, 0, ""),
     (_contract_back, 1, "consumer-contract"), (_leftover, 1, "leftover")],
)  # fmt: skip
def test_check_rejects_a_version_that_is_not_main_plus_one(repo, setup, code, needle):  # AT-R4
    m0 = mver(repo)
    setup(repo, m0)
    res = run(repo, "--check")
    assert res.returncode == code and needle in out(res), out(res)
    if setup is _unrenumbered:
        assert nxt(m0, 2) in out(res)


def _wrong_type(repo, m0):
    vt = nxt(m0, 2)
    edit(
        repo,
        CONTRACT,
        lambda t: t.replace(f'"registry_version": "{vt}"', '"registry_version": 7', 1),
    )
    commit(repo)


def _missing_key(repo, m0):
    pattern = rf'\n\s*"registry_version": "{re.escape(nxt(m0, 2))}",'
    edit(repo, CONTRACT, lambda t: re.sub(pattern, "", t, count=1))
    commit(repo)


def _two_fixtures(repo, m0):
    name = fix_name(nxt(m0, 2), "b")
    shutil.copy(repo / F29 / name, repo / F29 / fix_name(nxt(m0, 2), "x"))
    commit(repo)


def _duplicate_anchor(repo, m0):
    edit(repo, "docs/reasons.md", lambda t: t + f"\nAgain `registry_version` (`{nxt(m0, 2)}`).\n")
    commit(repo)


def _stale_duplicate_anchor(repo, m0):
    edit(repo, "docs/reasons.md", lambda t: t + f"\nOld `registry_version` (`{nxt(m0)}`).\n")
    commit(repo)


def _no_fixture(repo, m0):
    git(repo, "rm", "-q", f"{F29}/{fix_name(nxt(m0, 2), 'b')}")
    commit(repo)


@pytest.mark.parametrize(
    ("break_it", "needle"),
    [(_wrong_type, "consumer-contract"), (_missing_key, "consumer-contract"),
     (_two_fixtures, "several manifest fixtures"), (_duplicate_anchor, "exactly one anchor"),
     (_stale_duplicate_anchor, "exactly one anchor"), (_no_fixture, "no manifest fixture")],
)  # fmt: skip
def test_check_fails_closed_on_bad_carriers(repo, break_it, needle):  # AT-R23
    m0 = mver(repo)
    slices(repo, "a", "b")
    assert run(repo).returncode == 0
    commit(repo, "renumber")
    assert numeric_only(run(repo, "--check"))
    break_it(repo, m0)
    res = run(repo, "--check")
    assert res.returncode == 1 and needle in out(res), out(res)


def _refusal(repo, code, key, setup=None):
    if setup:
        setup(repo)
    res = run(repo)
    assert res.returncode == code and out(res).lstrip().startswith(key), out(res)
    assert git(repo, "status", "--porcelain") == ""


def test_refuses_an_edit_to_an_existing_code(repo):  # AT-R5
    git(repo, "checkout", "-q", "-b", "a")
    add_slice(repo, "a", nxt(mver(repo)))
    edit(
        repo,
        PLAT,
        lambda t: t.replace("Job complete: failed. The server log has the details.", "Changed."),
    )
    commit(repo)
    _refusal(repo, 3, "edited-existing platform.job_failed")


@pytest.mark.parametrize("case", ["duplicate", "both-changed"])
def test_refuses_when_main_and_branch_define_the_same_code(repo, case):  # AT-R6
    va = nxt(mver(repo))
    git(repo, "checkout", "-q", "-b", "a")
    add_slice(repo, "a", va, code="probe_shared")
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "b")
    add_slice(repo, "b", va, fixture=False, module=CONF)
    if case == "duplicate":
        line = '_d = _catalog.platform("PlatformJobLog")\n'
        edit(
            repo,
            PLAT,
            lambda t: t.replace(
                line, line + f'DUP = _d("probe_shared", "Dup.", since="{va}")\n', 1
            ),
        )
        commit(repo)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", "a", "a")
    git(repo, "checkout", "-q", "b")
    land_main(repo)
    if case == "both-changed":
        edit(repo, PLAT, lambda t: t.replace('"Probe a."', '"Probe changed."'))
        commit(repo)
    key = "duplicate-identity" if case == "duplicate" else "main-and-branch-changed"
    _refusal(repo, 3, f"{key} platform.probe_shared")


def test_refuses_more_than_one_new_minor(repo):  # AT-R7
    m0 = mver(repo)
    git(repo, "checkout", "-q", "-b", "a")
    add_slice(repo, "a", nxt(m0))
    add_code(repo, CONF, "probe_far", nxt(m0, 2))
    commit(repo)
    _refusal(repo, 3, "several-minors")


def test_refuses_preconditions(repo):  # AT-R8
    slices(repo, "a", "b", merged=False)
    res = run(repo)
    assert res.returncode == 2 and "main-not-merged" in out(res)
    land_main(repo)
    (repo / "stray.txt").write_text("x")
    res = run(repo)
    assert res.returncode == 2 and "dirty-tree" in out(res)


def test_refuses_a_version_token_on_an_unknown_surface(repo):  # AT-R9
    m0 = slices(repo, "a", "b", merged=False)
    va = nxt(m0)
    edit(repo, "src/decoy_engine/_progress_events.py", lambda t: t + f"\n# probe at {va}\n")
    commit(repo)
    land_main(repo)
    res = run(repo)
    assert res.returncode == 3 and "unknown-surface src/decoy_engine/_progress_events.py:" in out(
        res
    )
    assert git(repo, "status", "--porcelain") == ""
    edit(
        repo, "src/decoy_engine/_progress_events.py", lambda t: t.replace(f"# probe at {va}\n", "")
    )
    edit(repo, f"{F29}/reason-registry-release.json", lambda t: t + f"{va}\n")
    commit(repo)
    res = run(repo)
    assert res.returncode == 3 and "frozen-fixture" in out(res)
    assert git(repo, "status", "--porcelain") == ""


def test_refuses_a_manifest_imported_from_another_tree(repo):  # AT-R10
    git(repo, "checkout", "-q", "-b", "a")
    git(repo, "rm", "-rq", "src/decoy_engine")
    commit(repo)
    res = run(repo)
    assert res.returncode == 2 and "foreign-import" in out(res)


def test_refuses_a_stale_consumer_contract(repo):  # AT-R11
    slices(repo, "a", "b")
    edit(repo, CONTRACT, lambda t: t.replace('"consumer_rule": "', '"consumer_rule": "x', 1))
    commit(repo)
    res = run(repo)
    assert res.returncode == 4 and "consumer contract was stale" in out(res)
    assert git(repo, "status", "--porcelain") == ""


def test_a_second_renumber_keeps_both_record_lines_true(repo):  # AT-R12
    m0 = slices(repo, "a", "b", "c", merged=False)
    v = [nxt(m0, n) for n in (1, 2, 3)]
    for slug, step in (("b", "renumber b"), ("c", "renumber c")):
        git(repo, "checkout", "-q", slug)
        land_main(repo)
        assert run(repo).returncode == 0
        commit(repo, step)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", "c", "c")
    git(repo, "checkout", "-q", "b")
    land_main(repo)
    assert run(repo).returncode == 0
    lines = [
        x
        for x in (repo / "docs/records/probe-b.md").read_text().splitlines()
        if x.startswith(RENUM)
    ]
    assert len(lines) == 2 and lines[0].startswith(f"{RENUM} {v[0]} to {v[1]} ")
    assert lines[1].startswith(f"{RENUM} {v[1]} to {v[2]} ")
    assert (repo / F29 / fix_name(v[2], "b")).exists()
    commit(repo, "renumber b again")
    assert numeric_only(run(repo, "--check"))


@pytest.mark.parametrize("a_names_version", [True, False])
def test_refuses_a_mixed_line(repo, a_names_version):  # AT-R13
    m0, va = mver(repo), nxt(mver(repo))
    line21 = lambda extra: (
        lambda t: re.sub(r"(?m)^(`registry_version` \(.*)$", lambda m: m[1] + extra, t, count=1)
    )
    git(repo, "checkout", "-q", "-b", "a", "main")
    add_slice(repo, "a", va)
    edit(
        repo, "docs/reasons.md", line21(f" A clause at {va}." if a_names_version else " A clause.")
    )
    commit(repo)
    git(repo, "checkout", "-q", "-b", "b", "main")
    add_slice(repo, "b", va)
    edit(repo, "docs/reasons.md", line21(f" B clause at {va}."))
    commit(repo)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", "a", "a")
    git(repo, "checkout", "-q", "b")
    res = git_res(repo, "merge", "--no-ff", "-q", "-m", "m", "main")
    assert res.returncode and "docs/reasons.md" in git(
        repo, "diff", "--name-only", "--diff-filter=U"
    )
    for rel in git(repo, "diff", "--name-only", "--diff-filter=U").split():
        if rel != "docs/reasons.md":
            regen_contract(repo) if rel == CONTRACT else pytest.fail(rel)
    (repo / "docs/reasons.md").write_text(git(repo, "show", "main:docs/reasons.md") + "\n")
    edit(repo, "docs/reasons.md", line21(f" B clause at {va}."))
    commit(repo, "merge main")
    res = run(repo)
    if a_names_version:
        assert res.returncode == 3 and "mixed-line docs/reasons.md:" in out(res), out(res)
        assert git(repo, "status", "--porcelain") == ""
    else:
        assert res.returncode == 0, out(res)
        text = (repo / "docs/reasons.md").read_text()
        assert f"B clause at {nxt(m0, 2)}." in text and " A clause." in text


@pytest.mark.parametrize("case", ["no-since", "copied-since"])
def test_refuses_a_stale_since(repo, case):  # AT-R14
    m0 = mver(repo)
    git(repo, "checkout", "-q", "-b", "a", "main")
    add_slice(repo, "a", nxt(m0))
    git(repo, "checkout", "-q", "-b", "b", "main")
    add_slice(repo, "b", nxt(m0), fixture=False)
    edit(
        repo,
        CONF,
        lambda t: t.replace(
            f", since={nxt(m0)!r})", f", since={m0!r})" if case == "copied-since" else ")"
        ),
    )
    regen_contract(repo)
    commit(repo)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", "a", "a")
    git(repo, "checkout", "-q", "b")
    land_main(repo)
    _refusal(repo, 3, "stale-since config.probe_b")


def test_only_the_first_of_two_green_heads_can_land(repo):  # AT-R16
    floor = re.search(
        r'FLOOR = "([^"]*)"', (ROOT / "tests/sentry/test_reason_minor_fixtures.py").read_text()
    )[1]

    def landable(head: str) -> bool:
        git(repo, "checkout", "-q", head)
        fixtures = {p.name: json.loads(p.read_text()) for p in (repo / F29).glob(FIXTURE_GLOB)}
        sentry = minor_fixture_violations(fixtures, live(repo), floor)
        return numeric_only(run(repo, "--check")) and not sentry

    m0 = mver(repo)
    for slug in ("a", "b"):
        git(repo, "checkout", "-q", "-b", slug, "main")
        add_slice(repo, slug, nxt(m0))
    assert landable("a") and landable("b")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", "a", "a")
    git(repo, "checkout", "-q", "b")
    res = run(repo, "--check")
    assert res.returncode == 2 and "main-not-merged" in out(res)
    assert not landable("b")
    land_main(repo)
    assert not landable("b")
    assert run(repo).returncode == 0
    commit(repo, "renumber")
    assert landable("b") and mver(repo) == nxt(m0, 2)


@pytest.mark.parametrize(
    "case",
    [
        "deleted-existing",
        "major-change",
        "version-without-identity",
        "several-fixtures",
        "fixture-exists",
        "anchor-missing",
    ],
)
def test_refuses_before_writing(repo, case):  # AT-R17
    m0, va = mver(repo), nxt(mver(repo))
    if case == "fixture-exists":
        slices(repo, "a", "b")
        (repo / F29 / fix_name(nxt(m0, 2), "b")).write_text("{}\n")
        git(repo, "checkout", "-q", "main")
        commit(repo, "stub")
        git(repo, "checkout", "-q", "b")
        land_main(repo)
        _refusal(repo, 3, "fixture-exists")
        return
    git(repo, "checkout", "-q", "-b", "c", "main")
    if case == "deleted-existing":
        add_slice(repo, "c", va)
        edit(
            repo,
            PLAT,
            lambda t: re.sub(r"V2_CONFIG_INVALID = _d\(.*?\n\)\n", "", t, flags=re.S).replace(
                '    "V2_CONFIG_INVALID",\n', ""
            ),
        )
        commit(repo)
        key = "deleted-existing platform.v2_config_invalid"
    elif case == "major-change":
        add_slice(repo, "c", f"{int(va.split('.')[0]) + 1}.0.0", fixture=False)
        key = "major-change"
    elif case == "version-without-identity":
        edit(
            repo,
            REG,
            lambda t: t.replace(f'REGISTRY_VERSION = "{m0}"', f'REGISTRY_VERSION = "{va}"'),
        )
        commit(repo)
        key = "version-without-identity"
    elif case == "several-fixtures":
        add_slice(repo, "c", va)
        shutil.copy(repo / F29 / fix_name(va, "c"), repo / F29 / fix_name(va, "x"))
        commit(repo)
        key = "several-fixtures"
    else:
        add_slice(repo, "c", va)
        edit(
            repo,
            "docs/reasons.md",
            lambda t: t.replace("`registry_version` (", "registry_version ("),
        )
        commit(repo)
        key = "anchor-missing"
    _refusal(repo, 3, key)


def _roots_branch(repo, root_name: str, where: str):
    m0 = mver(repo)
    bump = lambda t: t.replace(f"{root_name} = 1\n", f"{root_name} = 2\n")
    git(repo, "checkout", "-q", "-b", "b", "main")
    add_slice(repo, "b", nxt(m0))
    if where in ("branch", "both"):
        edit(repo, REG, bump)
        regen_contract(repo)
        commit(repo)
    if where == "side":  # merged in from a side branch: no first-parent commit owns the change
        git(repo, "checkout", "-q", "-b", "side")
        edit(repo, REG, bump)
        regen_contract(repo)
        commit(repo)
        git(repo, "checkout", "-q", "b")
        git(repo, "merge", "--no-ff", "-q", "-m", "side", "side")
    if where in ("main", "both"):
        git(repo, "checkout", "-q", "main")
        edit(repo, REG, bump)
        regen_contract(repo)
        commit(repo)
        git(repo, "checkout", "-q", "b")
        land_main(repo)


@pytest.mark.parametrize(
    ("root", "where", "code"),
    [("REASON_SCHEMA_VERSION", "branch", 3), ("EVENT_SCHEMA_VERSION", "branch", 3),
     ("REASON_SCHEMA_VERSION", "side", 3), ("EVENT_SCHEMA_VERSION", "side", 3),
     ("REASON_SCHEMA_VERSION", "main", 0), ("REASON_SCHEMA_VERSION", "both", 3)],
)  # fmt: skip
def test_schema_root_changes_are_attributed(repo, root, where, code):  # AT-R18
    _roots_branch(repo, root, where)
    res = run(repo)
    assert res.returncode == code, out(res)
    if code:
        assert (
            f"schema-root {root.lower()}" in out(res) and git(repo, "status", "--porcelain") == ""
        )
        assert run(repo, "--check").returncode == 1
    else:
        assert numeric_only(run(repo, "--check"))


def test_the_policy_returns_the_branch_owned_root_it_is_given():  # AT-R18 (direct)
    man = {
        "registry_version": "7.1.0",
        "reason_schema_version": 1,
        "event_schema_version": 1,
        "entries": [],
    }
    facts = {"branch_owned_schema_roots": frozenset({"reason_schema_version"}), "main_moved": False}
    assert renumber_classification(man, man, man, facts) == ["schema-root reason_schema_version"]


def test_refuses_a_version_in_a_summary(repo):  # AT-R19
    m0 = mver(repo)
    git(repo, "checkout", "-q", "-b", "a", "main")
    add_slice(repo, "a", nxt(m0), summary=f"Added at registry {nxt(m0, 2)}.")
    res = run(repo)
    assert res.returncode == 3 and "summary-version platform.probe_a" in out(res)
    assert run(repo, "--check").returncode == 1
    assert git(repo, "status", "--porcelain") == ""


def test_merge_procedure_commands_run_by_file_path(repo, tmp_path):  # AT-R20
    slices(repo, "a", "b")
    res = script("--print-tests", cwd=repo)
    assert res.returncode == 0 and "tests/unit/progress/test_consumer_contract.py" in res.stdout
    assert script("--check", cwd=repo).returncode == 1
    assert script(cwd=repo).returncode == 0
    commit(repo, "renumber")
    res = script("--check", "--repo", str(repo), cwd=tmp_path)
    assert numeric_only(res), out(res)


CONF2 = "src/decoy_engine/reasons/config/_part2.py"
CONTRACT_PY = "tests/unit/progress/test_consumer_contract.py"
NO_REASON = '"an_old_engine_with_no_reason": None,'
MARKERS = "PENDING_SUMMARY_EDITS: frozenset[tuple[str, str]] = frozenset()"


def _claimed_branch(repo: Path, v: str) -> str:
    """An in-flight identity-addition slice that claimed minor ``v`` on every carrier (AT-10)."""
    git(repo, "checkout", "-q", "-b", "a", "main")
    add_code(repo, CONF2, "probe_a", v)
    edit(repo, REG, lambda t: re.sub(r'(?m)^(REGISTRY_VERSION = ")[^"]*"', rf'\g<1>{v}"', t))
    edit(
        repo, "docs/reasons.md", lambda t: re.sub(r"(`registry_version` \(`)[^`]*", rf"\g<1>{v}", t)
    )
    edit(repo, "docs/reasons.md", lambda t: t + f"\nThe probe `config/probe_a` lands at `{v}`.\n")
    (repo / "tests/unit/test_probe_a.py").write_text(
        f'EXPECTED = {{"since": "{v}"}}\nOTHER = \'{v}\'\n'
    )
    (repo / "tests/fixtures/probe").mkdir(parents=True)
    (repo / "tests/fixtures/probe/a.json").write_text(json.dumps({"since": v}) + "\n")
    (repo / "docs/records").mkdir(exist_ok=True)
    (repo / "docs/records/probe-a.md").write_text(f'Registry: "{v}".\nThe slice lands at {v}.\n')
    (repo / "changelog.d").mkdir(exist_ok=True)
    (repo / "changelog.d/probe-a.md").write_text(
        f"### Added (probe, A, 2026-11-10)\n\nAdds `config/probe_a` at `{v}`.\n"
    )
    edit(
        repo,
        CONTRACT_PY,
        lambda t: t.replace(NO_REASON, NO_REASON + '\n        "a_branch_fact": None,'),
    )
    body = {k: x for k, x in live(repo).items() if k != "entries"}
    body["entries"] = entries_since(live(repo), v)
    name = f"{F29}/{fix_name(v, 'a')}"
    (repo / name).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    regen_contract(repo)
    commit(repo, "slice a")
    return name


def test_to_unreleased_converts_an_identity_addition_branch(repo):  # AT-10
    m0 = mver(repo)
    v = nxt(m0)
    fixture = _claimed_branch(repo, v)
    res = run(repo, "--to-unreleased")
    assert res.returncode == 0, out(res)
    cat = (repo / CONF2).read_text()
    assert (
        "since=UNRELEASED)" in cat and "from decoy_engine.reasons._types import UNRELEASED\n" in cat
    )
    assert v not in cat and 'since="unreleased"' not in cat and "since='unreleased'" not in cat
    data = (repo / "tests/unit/test_probe_a.py").read_text()
    assert data == 'EXPECTED = {"since": "unreleased"}\nOTHER = \'unreleased\'\n'
    assert json.loads((repo / "tests/fixtures/probe/a.json").read_text()) == {"since": "unreleased"}
    doc = (repo / "docs/reasons.md").read_text()
    assert (
        f"`registry_version` (`{m0}`)" in doc and "`config/probe_a` lands at `unreleased`." in doc
    )
    rec = (repo / "docs/records/probe-a.md").read_text()
    assert rec == 'Registry: "unreleased".\nThe slice lands at unreleased.\n'
    frag = (repo / "changelog.d/probe-a.md").read_text()
    assert frag.endswith("Adds `config/probe_a` at `unreleased`.\n")
    doc_line = next(x for x in doc.splitlines() if "`config/probe_a`" in x)
    for text in (data, doc_line, rec, frag, (repo / "tests/fixtures/probe/a.json").read_text()):
        assert "UNRELEASED" not in text and v not in text
    assert v not in doc
    assert mver(repo) == m0 and not (repo / fixture).exists()
    changed = set(git(repo, "diff", "--name-only", "HEAD", "--", F29).split())
    assert changed == {fixture, CONTRACT}
    contract = json.loads((repo / CONTRACT).read_text())
    assert "a_branch_fact" in contract["degraded_readings"]
    assert {
        x for x in re.findall(r'"registry_version": "([^"]*)"', (repo / CONTRACT).read_text())
    } == {m0}
    probe = next(e for e in live(repo)["entries"] if e["code"] == "probe_a")
    assert probe["since"] == "unreleased"
    commit(repo, "to unreleased")
    res = run(repo, "--check")
    assert res.returncode == 0, out(res)


def _patch(version: str) -> str:
    major, minor, _ = version.split(".")
    return f"{major}.{minor}.1"


def _first_deprecation(repo: Path, m0: str) -> None:
    doc = '"The pipeline configuration failed schema validation.", since="1.5.0"'
    edit(
        repo,
        PLAT,
        lambda t: t.replace(
            doc, doc + f', deprecated_in="{_patch(m0)}", replaced_by=("platform", "job_failed")'
        ),
    )
    edit(
        repo,
        REG,
        lambda t: t.replace(f'REGISTRY_VERSION = "{m0}"', f'REGISTRY_VERSION = "{_patch(m0)}"'),
    )


def _marked_summary(repo: Path, m0: str) -> None:
    edit(
        repo,
        PLAT,
        lambda t: t.replace("Job complete: failed. The server log has the details.", "Changed."),
    )
    edit(repo, REG, lambda t: t.replace(MARKERS, MARKERS[:-2] + '({("platform", "job_failed")})'))
    edit(
        repo,
        REG,
        lambda t: t.replace(f'REGISTRY_VERSION = "{m0}"', f'REGISTRY_VERSION = "{_patch(m0)}"'),
    )


def _mixed(repo: Path, m0: str) -> None:
    add_code(repo, CONF2, "probe_m", nxt(m0))
    edit(
        repo,
        PLAT,
        lambda t: t.replace("Job complete: failed. The server log has the details.", "Changed."),
    )
    edit(
        repo,
        REG,
        lambda t: t.replace(f'REGISTRY_VERSION = "{m0}"', f'REGISTRY_VERSION = "{nxt(m0)}"'),
    )


@pytest.mark.parametrize(
    ("setup", "keys"),
    [(_first_deprecation, ["edited-existing platform.v2_config_invalid", "version-without-identity"]),
     (_marked_summary, ["edited-existing platform.job_failed", "version-without-identity"]),
     (_mixed, ["edited-existing platform.job_failed"])],
)  # fmt: skip
def test_to_unreleased_refuses_an_edit_to_an_existing_identity(repo, setup, keys):  # AT-10
    m0 = mver(repo)
    git(repo, "checkout", "-q", "-b", "e", "main")
    setup(repo, m0)
    regen_contract(repo)
    commit(repo, "edit")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 3 and all(k in out(res).split("\n") for k in keys), out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.parametrize(
    ("rel", "line"),
    [("tests/unit/test_probe_v.py", 'assert manifest()["registry_version"] == "{v}"'),
     ("tests/unit/test_probe_v.py", "PIN = dict(registry_version='{v}', since='{v}')"),
     ("tests/fixtures/probe/v.json", '{{"registry_version": "{v}", "since": "{v}"}}')],
)  # fmt: skip
def test_to_unreleased_refuses_a_registry_version_value_it_cannot_reset(repo, rel, line):  # AT-10
    # Only the two named anchors return to main's version; any other carrier of the registry
    # version would become the pending string, which a registry version never is.
    v = nxt(mver(repo))
    _claimed_branch(repo, v)
    (repo / rel).write_text(line.format(v=v) + "\n")
    commit(repo, "registry version carrier")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 3 and f"unconverted {rel}:1" in out(res).split("\n"), out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_to_unreleased_refuses_a_claim_that_is_not_above_main(repo):  # AT-10
    # Main already holds the claimed minor, so a branch line naming it may mean main's version.
    v = mver(repo)
    git(repo, "checkout", "-q", "-b", "a", "main")
    add_code(repo, CONF2, "probe_a", v)
    (repo / "tests/unit/test_probe_v.py").write_text(f'MAIN_IS_AT = "{v}"\n')
    regen_contract(repo)
    commit(repo, "claims main's version")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 3 and out(res).startswith(f"claim-not-above-main {v} "), out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_to_unreleased_leaves_the_worktree_alone_when_the_candidate_fails(repo):  # AT-10
    _claimed_branch(repo, nxt(mver(repo)))
    edit(repo, CONTRACT, lambda t: t.replace('"consumer_rule": "', '"consumer_rule": "x', 1))
    commit(repo, "stale contract")
    head = git(repo, "rev-parse", "HEAD")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 4, out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(repo, "rev-parse", "HEAD") == head
