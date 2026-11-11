"""F22 5.0 rule (1): output parents are made by `make_parent_dir_at` at the real name.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 row `test_make_parent_dir_modes_and_crash`
(slice a1a; the durable-walk part lands with a1b). Every case publishes from a `spawn` child
into `p/a/out` with `p` and `a` absent. Named limit L1u: a SIGKILL between the `mkdir` of a parent
and its owner-bit repair, under a umask that strips an owner bit, leaves a directory Decoy cannot
open; the retry refuses it with nothing bound, and the operator fixes the mode.
"""

from __future__ import annotations

import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from tests.fileaccess.publication._dirmode_support import (
    KILL_MARKER,
    UNIT_BYTES,
    UNIT_FILE,
    mode,
    publish_case,
    run_child,
    setgid_case,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")

POLICIES = ["strict", "ambient"]
PARENT = 0o777  # the create mode only `make_parent_dir_at` passes to the directory core


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o755)  # noqa: S103 - the walk above the created parents must be searchable
    return root


def _publish(base: Path, policy: str, umask: int, **extra: object) -> tuple[int, dict]:
    spec = {"base": str(base), "policy": policy, "umask": umask, "rel": "p/a/out", **extra}
    return run_child(publish_case, spec, base.parent)


def _bound(base: Path) -> bool:
    return (base / "p" / "a" / "out" / UNIT_FILE).exists()


@pytest.mark.parametrize("policy", POLICIES)
def test_i_umask_0477_parents_are_0700_through_the_entry_point(base: Path, policy: str) -> None:
    code, got = _publish(base, policy, 0o477, spies=True)
    assert got["probe"] == 0o200  # non-vacuity: the umask strips owner read from a new file
    assert code == 0 and got["outcome"] == "published", got
    # file modes are a1b; under this umask the unit file is born 0200, so its owner makes it
    # readable before the content check
    os.chmod(base / "p" / "a" / "out" / UNIT_FILE, 0o600)
    assert (base / "p" / "a" / "out" / UNIT_FILE).read_bytes() == UNIT_BYTES
    assert mode(base / "p") == 0o700 and mode(base / "p" / "a") == 0o700
    assert ["p", PARENT] in got["core"] and ["a", PARENT] in got["core"]


@pytest.mark.parametrize("policy", POLICIES)
def test_ii_killed_between_mkdir_and_repair_retry_refuses_then_publishes(
    base: Path, policy: str
) -> None:
    code, _ = _publish(base, policy, 0o477, kill_after_mkdir="a")
    assert code == -signal.SIGKILL
    assert (base.parent / KILL_MARKER).exists() and mode(base / "p" / "a") == 0o300  # non-vacuity

    code, got = _publish(base, policy, 0o477)
    assert code == 0 and got["outcome"] == "refused", got
    assert not _bound(base)

    os.chmod(base / "p" / "a", 0o700)  # the operator fixes the mode (L1u)
    code, got = _publish(base, policy, 0o477)
    assert code == 0 and got["outcome"] == "published", got
    assert _bound(base)


@pytest.mark.parametrize("policy", POLICIES)
def test_iii_eexist_continues_through_the_existing_directory(base: Path, policy: str) -> None:
    code, got = _publish(base, policy, 0o022, eexist_hook="a")
    assert "ino" in got  # non-vacuity: the hook fired
    assert code == 0 and got["outcome"] == "published", got
    st = os.lstat(base / "p" / "a")
    assert [st.st_dev, st.st_ino] == got["ino"]
    assert _bound(base)


@pytest.mark.parametrize("policy", POLICIES)
def test_iv_umask_022_parents_keep_the_born_mode(base: Path, policy: str) -> None:
    code, got = _publish(base, policy, 0o022)
    assert got["probe"] == 0o644  # non-vacuity: the umask is the ordinary one
    assert code == 0 and got["outcome"] == "published", got
    assert mode(base / "p") == 0o755 and mode(base / "p" / "a") == 0o755


@pytest.mark.parametrize("umask", [0o022, 0o477])
def test_v_set_group_id_parent(tmp_path: Path, umask: int) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    gid = 4242 if os.geteuid() == 0 else os.getegid()
    os.chown(shared, -1, gid)
    os.chmod(shared, 0o2770)  # noqa: S103 - a group-shared parent is the case under test
    assert os.lstat(shared).st_mode & stat.S_ISGID  # non-vacuity: the parent shares its group
    code, got = run_child(setgid_case, {"shared": str(shared), "umask": umask}, tmp_path)
    assert code == 0 and got["outcome"] == "made", got
    assert mode(shared / "s") == 0o700
    born = 0o777 & ~umask
    assert mode(shared / "d") == stat.S_ISGID | born | 0o700
    assert os.lstat(shared / "d").st_gid == gid and os.lstat(shared / "d" / "e").st_gid == gid


def test_vi_ambient_user_components_keep_path_semantics(base: Path) -> None:
    (base / "real").mkdir()
    os.symlink(base / "real", base / "link")
    parts = [" sp ace", ".dot", "_under", "ünï"]
    rel = "/".join(["link", *parts, "out"])
    assert os.path.islink(base / "link") and not (base / "real" / parts[0]).exists()
    spec = {"base": str(base), "policy": "ambient", "umask": 0o022, "rel": rel, "spies": True}
    code, got = run_child(publish_case, spec, base.parent)
    assert code == 0 and got["outcome"] == "published", got
    assert (base / "real").joinpath(*parts, "out", UNIT_FILE).read_bytes() == UNIT_BYTES
    assert not set(parts) & set(got["validated"]), got["validated"]


@pytest.mark.parametrize("policy", POLICIES)
def test_vii_ensure_dir_under_0477(base: Path, policy: str) -> None:
    code, got = _publish(base, policy, 0o477, op="ensure_dir", spies=True)
    assert got["probe"] == 0o200  # non-vacuity: the umask strips owner read from a new file
    assert code == 0 and got["outcome"] == "published", got
    for made in (base / "p", base / "p" / "a", base / "p" / "a" / "out"):
        assert mode(made) == 0o700, made
    assert [c for c in got["core"] if c[0] in {"p", "a", "out"}] == [
        ["p", PARENT],
        ["a", PARENT],
        ["out", PARENT],
    ]


@pytest.mark.parametrize("policy", POLICIES)
def test_viii_no_umask_round_trip_and_no_proc_read(base: Path, policy: str) -> None:
    code, got = _publish(base, policy, 0o477, spies=True)
    assert code == 0 and got["outcome"] == "published", got
    assert got["core"]  # non-vacuity: the spies were live while parents were made
    assert got["umask"] == 0 and got["proc"] == 0
