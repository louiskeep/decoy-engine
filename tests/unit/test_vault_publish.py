"""The file vault is published privately, durably and exclusively, and never deleted (B-6 D1).

Steps (both policies): open the parent directory, create the stage `_decoy_vault_stage_*` with
creation mode 0600 (`os.open`, so no umask widens it), write every byte, fsync and close the
stage, `link(2)` it to the leaf (the compare-and-set; an existing leaf is `vault_exists`), fsync
the parent (the commit point), unlink the stage. A failure before the link leaves no vault and
no stage; a failure after it never removes the vault.

T4 mode under a permissive and a strict umask; T5 a SIGKILL after each step on a real
filesystem (`vault_publish_child.py`) and an injected failure at each step; T6 two concurrent
writers; T7 the error text. The two in-place pins of `test_char_vault_in_place.py` are
replaced here (that file pinned the rewrite this removes).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.fileaccess import FileRequest, FileRole, Root, RootedFileAccess
from decoy_engine.vault import VaultWriter, load_vault

KEY = b"12345678"
LEAF = "v.dcyvault"
STAGE_PREFIX = "_decoy_vault_stage_"
CHILD = Path(__file__).with_name("vault_publish_child.py")
CHILD_ENTRIES = {("ns", f"m{i}"): f"s{i}" for i in range(50)}
ROUTES = ["ambient", "strict"]


class _Route:
    """One vault target under one policy: a path string (ambient) or an `OutputDir` (strict)."""

    def __init__(self, route: str, directory: Path) -> None:
        self.route = route
        self.directory = directory
        self.leaf = directory / LEAF
        self._fd = -1
        self.policy: RootedFileAccess | None = None
        if route == "strict":
            self._fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            self.policy = RootedFileAccess(
                [
                    Root(
                        "main",
                        self._fd,
                        frozenset({FileRole.VAULT}),
                        frozenset({"read", "write", "stat"}),
                    )
                ]
            )

    def writer(self, entries: Any = (("ns", "m", "s"),)) -> VaultWriter:
        writer = VaultWriter(KEY, file_access=self.policy)
        writer.add(list(entries))
        return writer

    def target(self) -> Any:
        if self.policy is None:
            return str(self.leaf)
        return self.policy.output_dir(FileRequest(FileRole.VAULT, "write", LEAF, "vault"))

    def write(self, writer: VaultWriter | None = None) -> int:
        return (writer or self.writer()).write(self.target())

    def load(self) -> dict[tuple[str, str], str]:
        """The vault's entries, read under this route's policy (a process that used a strict
        policy may not fall back to ambient reads)."""
        if self.policy is None:
            return load_vault(self.leaf, KEY)[0]
        with self.policy.open_read(FileRequest(FileRole.VAULT, "read", LEAF, "vault")) as h:
            return load_vault(None, KEY, vault=h, file_access=self.policy)[0]

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)


@pytest.fixture(params=ROUTES)
def route(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[_Route]:
    r = _Route(request.param, tmp_path)
    try:
        yield r
    finally:
        r.close()


def _stages(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.name.startswith(STAGE_PREFIX))


def _summary(code: str) -> str:
    return reasons.get("runtime", code).summary


# ---------------------------------------------------------------------------
# T4: creation mode 0600 whatever the umask
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o000, 0o077], ids=["permissive", "strict"])
def test_vault_is_0600_under_any_umask(route: _Route, umask: int) -> None:
    previous = os.umask(umask)
    try:
        route.write()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(route.leaf.stat().st_mode) == 0o600
    assert _stages(route.directory) == []


# ---------------------------------------------------------------------------
# T5a: SIGKILL right after each step, on a real filesystem
# ---------------------------------------------------------------------------

# point -> (leaf present, stage present, child exit code, second write publishes)
_KILL_TABLE = {
    "K1": (False, True, -9, True),  # stage created, nothing written
    "K2": (False, True, -9, True),  # first write returned
    "K3": (False, True, -9, True),  # stage fsynced
    "K4": (True, True, -9, False),  # linked: the leaf is the vault
    "K5": (True, True, -9, False),  # parent fsynced: committed
    "K6": (True, False, -9, False),  # stage unlinked
    "K7": (True, False, 0, False),  # completed
}


@pytest.mark.parametrize("point", sorted(_KILL_TABLE))
@pytest.mark.parametrize("route_name", ROUTES)
def test_sigkill_after_each_publish_step(route_name: str, point: str, tmp_path: Path) -> None:
    leaf_present, stage_present, rc, second_publishes = _KILL_TABLE[point]
    proc = subprocess.run(  # noqa: S603 -- static argv (sys.executable + this test's child)
        [sys.executable, str(CHILD), route_name, point, str(tmp_path)],
        capture_output=True,
        timeout=300,
        cwd=tmp_path,
    )
    if rc == -9:
        assert proc.returncode == -9, f"kill point {point} not reached: {proc.stderr[-400:]!r}"
    else:
        assert proc.returncode == 0, proc.stderr[-400:]
    facts_file = tmp_path / "_child_facts.json"
    facts = json.loads(facts_file.read_text()) if facts_file.exists() else {}
    facts_file.unlink(missing_ok=True)

    leaf = tmp_path / LEAF
    stages = _stages(tmp_path)
    second = _Route(route_name, tmp_path)
    assert leaf.exists() is leaf_present
    assert bool(stages) is stage_present and len(stages) <= 1
    if leaf_present:
        # never a partial vault: whenever the leaf exists it is complete
        assert second.load() == CHILD_ENTRIES
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o600
    if stages:
        st = stages[0].stat()
        assert stat.S_IMODE(st.st_mode) == 0o600
        if point == "K1":
            assert st.st_size == 0
        if point == "K2":
            assert st.st_size == facts["written"]
        if leaf_present:
            assert st.st_ino == leaf.stat().st_ino and st.st_nlink == 2
    if leaf_present and not stages:
        assert leaf.stat().st_nlink == 1
    residue = {p.name: p.read_bytes() for p in stages}
    leaf_before = leaf.read_bytes() if leaf_present else None

    try:
        if second_publishes:
            assert (
                second.write(
                    second.writer(sorted((ns, m, s) for (ns, m), s in CHILD_ENTRIES.items()))
                )
                == 50
            )
            assert second.load() == CHILD_ENTRIES
        else:
            with pytest.raises(FileExistsError):
                second.write()
            assert leaf.read_bytes() == leaf_before
    finally:
        second.close()
    # whatever the kill left is byte-identical, and the second write left no stage of its own
    assert {p.name: p.read_bytes() for p in _stages(tmp_path)} == residue


# ---------------------------------------------------------------------------
# N3: the two in-place pins, replaced
# ---------------------------------------------------------------------------


def test_an_existing_hardlinked_vault_is_refused_and_both_names_are_unchanged(
    tmp_path: Path,
) -> None:
    vault = tmp_path / LEAF
    vault.write_bytes(b"old")
    twin = tmp_path / "twin"
    os.link(vault, twin)
    with pytest.raises(FileExistsError):
        _Route("ambient", tmp_path).write()
    for name in (vault, twin):
        st = name.stat()
        assert name.read_bytes() == b"old" and st.st_nlink == 2
        assert st.st_ino == vault.stat().st_ino
    assert _stages(tmp_path) == []


def test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    directory = tmp_path / "ro"
    directory.mkdir()
    vault = directory / LEAF
    vault.write_bytes(b"old")
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(PermissionError) as exc:
            _Route("ambient", directory).write()
    finally:
        directory.chmod(stat.S_IRWXU)
    assert str(vault) in str(exc.value)
    assert vault.read_bytes() == b"old"
    assert sorted(p.name for p in directory.iterdir()) == [LEAF]
