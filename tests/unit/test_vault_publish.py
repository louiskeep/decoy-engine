"""The file vault is published privately, durably and exclusively, and never deleted (B-6 D1).

Steps (both policies): open the parent directory, create the stage `_decoy_vault_stage_*` with
creation mode 0600, restored with `fchmod` before the first byte (`open(2)` applies the
umask), write every byte, fsync and close the stage, `link(2)` it to the leaf (the compare-and-set; an existing leaf is `vault_exists`), fsync
the parent (the commit point), unlink the stage. A failure before the link leaves no vault and
no stage; a failure after it never removes the vault.

T4 mode under a permissive and a strict umask; T5 a SIGKILL after each step on a real
filesystem (`vault_publish_child.py`) and an injected failure at each step; T6 two concurrent
writers; T7 the error text. The two in-place pins of `test_char_vault_in_place.py` are
replaced here (that file pinned the rewrite this removes).
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.fileaccess import FileRequest, FileRole, Root, RootedFileAccess
from decoy_engine.vault import VaultError, VaultWriter, load_vault

KEY = b"12345678" * 4
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


@pytest.mark.parametrize(
    "umask",
    [0o000, 0o077, 0o700, 0o770, 0o777],
    ids=["permissive", "strict", "owner-masked", "owner-group-masked", "all-masked"],
)
def test_vault_is_0600_under_any_umask(route: _Route, umask: int) -> None:
    """`open(2)` applies `mode & ~umask`; a umask that masks the owner bits must not publish
    a vault its owner cannot reopen (0000 under 0777)."""
    previous = os.umask(umask)
    try:
        route.write()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(route.leaf.stat().st_mode) == 0o600
    assert _stages(route.directory) == []
    assert route.load() == {("ns", "m"): "s"}


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
            with pytest.raises(VaultError) as exc:
                second.write()
            assert exc.value.code == "vault_exists"
            assert leaf.read_bytes() == leaf_before
    finally:
        second.close()
    # whatever the kill left is byte-identical, and the second write left no stage of its own
    assert {p.name: p.read_bytes() for p in _stages(tmp_path)} == residue


# ---------------------------------------------------------------------------
# T5b: an injected failure at each step (fault injection, not a crash test)
# ---------------------------------------------------------------------------


class _Faults:
    """Wraps one `os` call to raise for the matching invocation; everything else is real."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
        self.mp = monkeypatch
        self.directory = directory
        self.stage_fds: set[int] = set()
        real_open = os.open

        def tracking_open(path: Any, flags: int, *args: Any, **kw: Any) -> int:
            fd = real_open(path, flags, *args, **kw)
            name = os.path.basename(os.fsdecode(path)) if isinstance(path, (str, bytes)) else ""
            if name.startswith(STAGE_PREFIX) and flags & os.O_CREAT:
                self.stage_fds.add(fd)
            return fd

        monkeypatch.setattr(os, "open", tracking_open)

    def fail(
        self, name: str, when: Callable[..., bool], err: int = errno.EIO, *, after: bool = False
    ) -> None:
        real = getattr(os, name)

        def wrapped(*args: Any, **kw: Any) -> Any:
            if when(*args, **kw):
                if after:
                    real(*args, **kw)
                raise OSError(err, os.strerror(err), str(self.directory / LEAF))
            return real(*args, **kw)

        self.mp.setattr(os, name, wrapped)


def _arm(case: str, faults: _Faults) -> None:
    is_stage = lambda fd, *a, **k: fd in faults.stage_fds
    is_dir = lambda fd, *a, **k: stat.S_ISDIR(os.fstat(fd).st_mode)
    if case == "dir_open":
        real_open = os.open

        def refuse(path: Any, flags: int, *args: Any, **kw: Any) -> int:
            if flags & os.O_DIRECTORY:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fsdecode(path))
            return real_open(path, flags, *args, **kw)

        faults.mp.setattr(os, "open", refuse)
    elif case == "write":
        faults.fail("write", is_stage)
    elif case == "file_fsync":
        faults.fail("fsync", is_stage)
    elif case == "stage_close":
        faults.fail("close", is_stage, after=True)
    elif case == "link_eio":
        faults.fail("link", lambda *a, **k: True)
    elif case == "dir_fsync":
        faults.fail("fsync", is_dir)
    elif case == "unlink":
        faults.fail("unlink", lambda p, *a, **k: os.fsdecode(p).startswith(STAGE_PREFIX))
    elif case == "dir_close":
        faults.fail("close", is_dir, after=True)


# case -> (expected error code or None, leaf after: "absent" | "kept", stage remains)
_FAULT_TABLE = {
    "write": ("vault_commit_failed", "absent", False),
    "file_fsync": ("vault_commit_failed", "absent", False),
    "stage_close": ("vault_commit_failed", "absent", False),
    "link_eio": ("vault_commit_failed", "absent", False),
    "dir_fsync": ("vault_commit_failed", "kept", False),
    "unlink": (None, "kept", True),
    "dir_close": (None, "kept", False),
}


@pytest.mark.parametrize("case", sorted(_FAULT_TABLE))
def test_an_injected_failure_at_each_step(
    route: _Route, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, leaf_after, stage_remains = _FAULT_TABLE[case]
    writer = route.writer()
    target = route.target()
    faults = _Faults(monkeypatch, route.directory)
    _arm(case, faults)
    if code is None:
        assert writer.write(target) == 1
    else:
        with pytest.raises(VaultError) as exc:
            writer.write(target)
        assert exc.value.code == code
        text = str(exc.value)
        assert text == f"[{code}] {_summary(code)}"
        assert str(route.directory) not in text and os.strerror(errno.EIO) not in text
        assert exc.value.__cause__ is None and exc.value.__context__ is None
    monkeypatch.undo()
    if leaf_after == "absent":
        assert not route.leaf.exists()
    else:
        assert route.load() == {("ns", "m"): "s"}
    assert bool(_stages(route.directory)) is stage_remains


def test_a_parent_that_cannot_be_opened_keeps_the_ambient_error_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = _Route("ambient", tmp_path)
    writer = route.writer()
    faults = _Faults(monkeypatch, tmp_path)
    _arm("dir_open", faults)
    with pytest.raises(PermissionError) as exc:
        writer.write(str(route.leaf))
    monkeypatch.undo()
    assert str(route.leaf) in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_link_eexist_is_vault_exists_and_keeps_the_existing_vault(route: _Route) -> None:
    route.write(route.writer([("ns", "old", "x")]))
    before = (route.leaf.read_bytes(), route.leaf.stat().st_ino)
    with pytest.raises(VaultError) as exc:
        route.write()
    assert exc.value.code == "vault_exists"
    assert str(exc.value) == f"[vault_exists] {_summary('vault_exists')}"
    assert (route.leaf.read_bytes(), route.leaf.stat().st_ino) == before
    assert _stages(route.directory) == []


# ---------------------------------------------------------------------------
# T6: two concurrent writers to one vault path
# ---------------------------------------------------------------------------


def _race(route_name: str, directory: Path) -> list[Any]:
    """Two writers stage in parallel and are released together onto `link(2)`."""
    routes = [_Route(route_name, directory) for _ in range(2)]
    writers = [r.writer([("ns", f"m{i}", f"s{i}")]) for i, r in enumerate(routes)]
    targets = [r.target() for r in routes]
    barrier = threading.Barrier(2)
    real_link = os.link
    results: list[Any] = [None, None]

    def run(i: int) -> None:
        try:
            results[i] = writers[i].write(targets[i])
        except VaultError as exc:
            results[i] = exc.code

    def synced_link(*a: Any, **k: Any) -> None:
        barrier.wait(timeout=30)  # both stages are complete before either links
        real_link(*a, **k)

    os.link = synced_link  # type: ignore[assignment]
    try:
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        os.link = real_link  # type: ignore[assignment]
        for r in routes:
            r.close()
    return results


@pytest.mark.parametrize("route_name", ROUTES)
def test_two_real_writers_one_publishes_one_fails_closed(route_name: str, tmp_path: Path) -> None:
    for trial in range(10):
        directory = tmp_path / f"t{trial}"
        directory.mkdir()
        results = _race(route_name, directory)
        assert sorted(results, key=str) == [1, "vault_exists"], results
        winner = results.index(1)
        check = _Route(route_name, directory)
        try:
            assert check.load() == {("ns", f"m{winner}"): f"s{winner}"}
        finally:
            check.close()
        assert _stages(directory) == []


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
    with pytest.raises(VaultError) as exc:
        _Route("ambient", tmp_path).write()
    assert exc.value.code == "vault_exists"
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
