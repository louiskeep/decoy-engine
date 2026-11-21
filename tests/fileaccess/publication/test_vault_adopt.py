"""F22 5.5 and 5.1 (slice b1): `OutputDir.adopt_file`, `VaultWriter.publish`, `VaultCommit`,
`BytesHandle` and `same_rows`.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 rows `test_vault_adopt_or_conflict`
and `test_vault_ancestor_chain_durable`, and the vault half of
`test_unreadable_ancestor_fail_closed`. A vault leaf is adopted only after its bytes and its
whole chain were fsynced; anything else is refused with a fixed code and no text of the vault.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess, reasons
from decoy_engine import vault as vault_module
from decoy_engine._canonical import canonical_json_bytes
from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.publication import _vault_order
from decoy_engine.vault import VaultCommit, VaultError, _format, load_vault
from tests.fileaccess.publication._chain_support import (
    Recorder,
    anchor_at,
    deny_reads,
    key,
    restat,
)
from tests.fileaccess.publication._dirmode_support import POLICIES, run_child
from tests.fileaccess.publication._vault_support import (
    CHILD_MARKER,
    KEY,
    LEAF,
    OTHER_KEY,
    OTHER_ROWS,
    IssueProbe,
    fingerprint,
    kill_child,
    vault_out,
    writer,
)
from tests.unit.vault._support import forge_payloads

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable vault")
REL = f"vaults/{LEAF}"
COMMIT_FAILED = reasons.runtime.VAULT_COMMIT_FAILED


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "vaults").mkdir(parents=True)
    return root


def _setup(
    policy: str, base: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, int, Recorder, IssueProbe]:
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = vault_out(policy, base, REL)
    rec = Recorder(monkeypatch)
    return out, root_fd, rec, IssueProbe(monkeypatch, rec.synced)


def _chain(base: Path) -> list[Any]:
    leaf = base / REL
    return [key(leaf), key(base / "vaults"), key(base)]


def _text_is_value_free(exc: VaultError, base: Path) -> None:
    text = str(exc)
    for secret in (str(base), LEAF, "<vault>", KEY.decode(), "s1", "m1", "injected"):
        assert secret not in text
    assert exc.__cause__ is None and exc.__context__ is None


@pytest.mark.parametrize("policy", POLICIES)
def test_publish_issues_commit_after_the_chain(
    base: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, root_fd, rec, probe = _setup(policy, base, monkeypatch)
    try:
        commit = writer().publish(out)
        assert isinstance(commit, VaultCommit) and commit.adopted is False
        assert commit.entry_count == 2
        data = (base / REL).read_bytes()
        assert commit.size == len(data)
        assert commit.sha256 == hashlib.sha256(data).hexdigest()
        assert probe.issued == [commit]  # non-vacuity: the probe saw the one issue
        assert probe.synced_at_issue[0][-3:] == _chain(base)
        assert load_vault(None, KEY, vault=_vault_order.BytesHandle(data))[0][("ns", "m1")] == "s1"
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def test_commit_is_issued_only_by_the_vault_module() -> None:
    with pytest.raises(fileaccess.FileAccessUnavailable):
        VaultCommit("0" * 64, 1, 1, False)  # type: ignore[call-arg]
    with pytest.raises(fileaccess.FileAccessUnavailable):
        VaultCommit("0" * 64, 1, 1, False, object())  # type: ignore[call-arg, arg-type]


def test_bytes_handle_is_a_constant_display_read_only_handle() -> None:
    handle = _vault_order.BytesHandle(b"not a vault")
    assert handle.display == "<vault>" and isinstance(handle, fileaccess.FileHandle)
    assert handle.read_all() == b"not a vault" and handle.read_all(3) == b"not"
    with handle.open_binary() as fh:
        assert fh.read() == b"not a vault"
        fh.write(b"XX")  # a copy: the handle's bytes cannot change
    assert handle.read_all() == b"not a vault"
    with pytest.raises(VaultError) as caught:  # the one text that names the handle
        load_vault(None, KEY, vault=handle)
    assert "<vault>" in str(caught.value)
    for method in (handle.fileno, handle.library_path, handle.stat):
        with pytest.raises(fileaccess.FileAccessUnavailable):
            method()


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("via", ["publish", "adopt"])
def test_vault_ancestor_chain_durable(
    base: Path, policy: str, via: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaf, `vaults` and each ancestor to the anchor are fsynced before the commit exists; EIO
    on each of them is `vault_commit_failed` with the leaf kept; after the fault clears, a
    retry gets an adopted commit."""
    out, root_fd, rec, probe = _setup(policy, base, monkeypatch)
    try:
        if via == "adopt":
            writer().publish(out)
            probe.issued.clear(), probe.synced_at_issue.clear()
            before = len(rec.events)
            commit = _vault_order.adopt_vault(out, writer())
            assert commit.adopted is True and probe.issued == [commit]
            assert [k for kind, k in rec.events[before:] if kind == "fsync"] == _chain(base)
            return
        commit = writer().publish(out)
        assert probe.synced_at_issue[0][-3:] == _chain(base) and commit.adopted is False
    finally:
        if root_fd >= 0:
            os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("step", [0, 1, 2])
@pytest.mark.parametrize("via", ["publish", "adopt"])
def test_vault_chain_fsync_eio_keeps_the_leaf(
    base: Path, policy: str, step: int, via: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = vault_out(policy, base, REL)
    leaf = base / REL
    state = {"armed": False}
    try:
        if via == "adopt":
            writer().publish(out)
            state["armed"] = True
        else:
            real_link = os.link

            def link(*args: Any, **kwargs: Any) -> None:
                real_link(*args, **kwargs)
                state["armed"] = True

            monkeypatch.setattr(os, "link", link)
        chain = [leaf, base / "vaults", base]
        calls: list[Any] = []
        rec = Recorder(
            monkeypatch,
            fail=lambda e: bool(
                state["armed"] and e == ("fsync", key(chain[step])) and not calls.append(1)
            ),
        )
        probe = IssueProbe(monkeypatch, rec.synced)
        with pytest.raises(VaultError) as caught:
            if via == "publish":
                writer().publish(out)
            else:
                _vault_order.adopt_vault(out, writer())
        assert caught.value.code == COMMIT_FAILED and calls  # non-vacuity: the fault fired
        _text_is_value_free(caught.value, base)
        assert probe.issued == [] and leaf.exists()
        monkeypatch.undo()
        retry = _recorder_after_clear(policy, base, monkeypatch)
        out2, fd2 = vault_out(policy, base, REL)
        try:
            adopted = _vault_order.adopt_vault(out2, writer())
            assert adopted.adopted is True
            assert [k for kind, k in retry.events if kind == "fsync"][-3:] == _chain(base)
        finally:
            if fd2 >= 0:
                os.close(fd2)
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _recorder_after_clear(policy: str, base: Path, mp: pytest.MonkeyPatch) -> Recorder:
    if policy == "ambient":
        anchor_at(mp, base)
    return Recorder(mp)


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kill_at", [1, 2, 3])
def test_sigkill_between_link_and_each_chain_fsync(
    base: Path, policy: str, kill_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = {"base": str(base), "policy": policy, "rel": REL, "kill_at": kill_at}
    code, _ = run_child(kill_child, spec, base.parent)
    assert code == -signal.SIGKILL and (base.parent / CHILD_MARKER).exists()  # non-vacuity
    assert (base / REL).exists()  # the link happened
    out, root_fd, rec, probe = _setup(policy, base, monkeypatch)
    try:
        commit = _vault_order.adopt_vault(out, writer())
        assert commit.adopted is True
        assert probe.synced_at_issue[0][-3:] == _chain(base)
    finally:
        if root_fd >= 0:
            os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
def test_vault_adopt_or_conflict(base: Path, policy: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal rows are adopted (a 0400 leaf too) only after the leaf and then the chain were
    fsynced; a different vault, an oversize one or one under another key is a conflict."""
    leaf = base / REL
    out, root_fd = vault_out(policy, base, REL)
    try:
        writer().publish(out)
        os.chmod(leaf, 0o400)  # MEDIUM-4: a read-only vault still opens
        before = fingerprint(leaf)
        if policy == "ambient":
            anchor_at(monkeypatch, base)
        rec = Recorder(monkeypatch)
        probe = IssueProbe(monkeypatch, rec.synced)
        flags: list[int] = []
        real_open = os.open

        def spy_open(path: Any, fl: int, *args: Any, **kw: Any) -> int:
            if os.fsdecode(path).endswith(LEAF):
                flags.append(fl)
            return real_open(path, fl, *args, **kw)

        monkeypatch.setattr(os, "open", spy_open)
        seen_at_compare: list[list[Any]] = []
        real_own = type(writer()).reads_as_own

        def spied(self: Any, handle: Any) -> bool:
            seen_at_compare.append(rec.synced())  # what was durable when the rows were compared
            return real_own(self, handle)

        monkeypatch.setattr(type(writer()), "reads_as_own", spied)
        commit = _vault_order.adopt_vault(out, writer())
        monkeypatch.undo()
        assert len(seen_at_compare) == 1 and seen_at_compare[0][-3:] == _chain(base)
        data = leaf.read_bytes()
        assert commit.adopted is True and commit.size == len(data) and commit.entry_count == 2
        assert flags and all(f & os.O_ACCMODE == os.O_RDONLY for f in flags)
        assert probe.synced_at_issue[0][-3:] == _chain(base)  # leaf, then the chain
        assert fingerprint(leaf) == before

        for other in (writer(OTHER_ROWS), writer(key=OTHER_KEY)):  # different rows; another key
            assert isinstance(_vault_order.adopt_vault(out, other), _vault_order.VaultConflict)
            assert fingerprint(leaf) == before
        big = base / "vaults" / "big.dcyvault"
        big.write_bytes(os.urandom(200_000))
        bigout, bigfd = vault_out(policy, base, "vaults/big.dcyvault")
        try:
            decrypted: list[int] = []
            real_reads = type(writer()).reads_as_own

            def counting(self: Any, handle: Any) -> bool:
                decrypted.append(1)
                return real_reads(self, handle)

            monkeypatch.setattr(type(writer()), "reads_as_own", counting)
            # over the cap: a conflict, and no decrypt attempted
            assert isinstance(
                _vault_order.adopt_vault(bigout, writer()), _vault_order.VaultConflict
            )
            assert decrypted == []
            monkeypatch.undo()
        finally:
            if bigfd >= 0:
                os.close(bigfd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("fault", ["leaf_fsync", "leaf_open"])
def test_adopt_io_failure_is_commit_failed(
    base: Path, policy: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, root_fd = vault_out(policy, base, REL)
    try:
        writer().publish(out)
        before = fingerprint(base / REL)
        probe_rec = Recorder(
            monkeypatch, fail=lambda e: fault == "leaf_fsync" and e[1] == key(base / REL)
        )
        probe = IssueProbe(monkeypatch, probe_rec.synced)
        if fault == "leaf_open":
            real_open = os.open

            def refuse(path: Any, fl: int, *args: Any, **kw: Any) -> int:
                if os.fsdecode(path).endswith(LEAF):
                    raise PermissionError(errno.EACCES, "injected")
                return real_open(path, fl, *args, **kw)

            monkeypatch.setattr(os, "open", refuse)
        with pytest.raises(VaultError) as caught:
            _vault_order.adopt_vault(out, writer())
        assert caught.value.code == COMMIT_FAILED and probe.issued == []
        _text_is_value_free(caught.value, base)
        monkeypatch.undo()
        assert fingerprint(base / REL) == before
    finally:
        if root_fd >= 0:
            os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
def test_adopt_file_reads_one_fd_and_refuses_the_wrong_kinds(base: Path, policy: str) -> None:
    out, root_fd = vault_out(policy, base, REL)
    leaf = base / REL
    try:
        assert out.adopt_file(limit=100) is None  # absent leaf
        leaf.write_bytes(b"abcdef")
        assert out.adopt_file(limit=6) == b"abcdef"
        with pytest.raises(fileaccess.LeafTooLarge):
            out.adopt_file(limit=5)
        leaf.unlink()
        leaf.mkdir()  # a directory is not a vault
        with pytest.raises(OSError) as caught:
            out.adopt_file(limit=100)
        assert str(base) not in str(caught.value)
        leaf.rmdir()
        (base / "elsewhere").write_bytes(b"x")
        leaf.symlink_to(base / "elsewhere")  # never followed
        with pytest.raises(OSError):
            out.adopt_file(limit=100)
        leaf.unlink()
        os.mkfifo(leaf)  # a FIFO cannot block the open
        with pytest.raises(OSError):
            out.adopt_file(limit=100)
        out_gone, gone_fd = vault_out(policy, base, "nope/" + LEAF)
        try:
            assert out_gone.adopt_file(limit=10) is None  # absent parent
        finally:
            if gone_fd >= 0:
                os.close(gone_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
def test_unreadable_ancestor_fail_closed_vault(
    base: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(i) `p/vaults` with `p` a same-uid directory Decoy cannot read: neither a publish nor an
    adopt ever yields a commit."""
    (base / "p" / "vaults").mkdir(parents=True)
    out, root_fd = vault_out(policy, base, f"p/vaults/{LEAF}")
    leaf = base / "p" / "vaults" / LEAF
    try:
        if policy == "ambient":
            anchor_at(monkeypatch, base)
            fresh = writer().publish(out)  # made before `p` becomes unreadable
            assert fresh.adopted is False
        else:
            leaf.write_bytes(b"x")  # strict cannot reach `p`; the leaf is only there to adopt
            leaf.unlink()
        os.chmod(base / "p", 0o300)
        seen: list[str] = []
        deny_reads(monkeypatch, base / "p", seen)
        rec = Recorder(monkeypatch)
        probe = IssueProbe(monkeypatch, rec.synced)
        with pytest.raises((VaultError, fileaccess.FileAccessDenied)):
            writer().publish(out)
        if policy == "ambient":
            with pytest.raises(VaultError) as caught:
                _vault_order.adopt_vault(out, writer())
            assert caught.value.code == COMMIT_FAILED
        assert seen and probe.issued == []  # non-vacuity: the read open of `p` was refused
    finally:
        os.chmod(base / "p", 0o700)
        if root_fd >= 0:
            os.close(root_fd)


def test_unreadable_ancestor_foreign_owner_stops_the_vault_walk(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(iii) ambient: an unreadable directory above the vault directory, owned by another uid,
    ends the walk and the vault is issued, so a path through a 0711 home still works."""
    from decoy_engine.fileaccess import _local

    h = base / "h"
    (h / "vaults").mkdir(parents=True)
    seen: list[str] = []
    real = _local._probe_dir  # type: ignore[attr-defined]
    top = os.path.realpath(h)

    def probe_dir(path: Any) -> os.stat_result:
        st = real(path)
        return restat(st, uid=os.geteuid() + 1) if os.path.realpath(path) == top else st

    monkeypatch.setattr(_local, "_probe_dir", probe_dir)
    out, _ = vault_out("ambient", base, f"h/vaults/{LEAF}")
    deny_reads(monkeypatch, h, seen)
    rec = Recorder(monkeypatch)
    probe = IssueProbe(monkeypatch, rec.synced)
    commit = writer().publish(out)
    assert seen and probe.issued == [commit]  # non-vacuity: the walk met the unreadable `h`
    assert stat.S_ISREG(os.lstat(h / "vaults" / LEAF).st_mode)


@pytest.mark.parametrize("policy", POLICIES)
def test_write_chain_fsync_eio_keeps_the_leaf_and_a_retry_is_vault_exists(
    base: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VaultWriter.write` has no adoption: an ancestor fsync EIO after the link is
    `vault_commit_failed` with the leaf kept, and the retry through `write` is `vault_exists`."""
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = vault_out(policy, base, REL)
    state = {"armed": False}
    real_link = os.link

    def link(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        state["armed"] = True

    monkeypatch.setattr(os, "link", link)
    calls: list[int] = []
    Recorder(
        monkeypatch,
        fail=lambda e: bool(
            state["armed"] and e == ("fsync", key(base / "vaults")) and not calls.append(1)
        ),
    )
    try:
        with pytest.raises(VaultError) as caught:
            writer().write(out)
        assert caught.value.code == COMMIT_FAILED and calls  # non-vacuity: the fault fired
        assert (base / REL).exists()
        monkeypatch.undo()
        _recorder_after_clear(policy, base, monkeypatch)
        out2, fd2 = vault_out(policy, base, REL)
        try:
            with pytest.raises(VaultError) as retry:
                writer().write(out2)
            assert retry.value.code == reasons.runtime.VAULT_EXISTS
        finally:
            if fd2 >= 0:
                os.close(fd2)
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _vault_blob_with_header(header: Any, blob: bytes | None = None) -> bytes:
    """A real vault whose header frame is replaced by `header` (a segment is left as written)."""
    blob = writer().serialize()[0] if blob is None else blob
    magic = _format._MAGIC
    _, rest = _format._read_frame(blob, len(magic), 1 << 20)
    return magic + _format._frame(canonical_json_bytes(header)) + blob[rest:]


def _real_header() -> dict[str, Any]:
    blob, _ = writer().serialize()
    start = len(_format._MAGIC)
    length = int.from_bytes(blob[start : start + 4], "big")
    header: dict[str, Any] = json.loads(blob[start + 4 : start + 4 + length])
    return header


_GOOD_HEADER = {
    "format": vault_module.VAULT_FORMAT_VERSION,
    "seed_protocol_version": SEED_PROTOCOL_VERSION,
}
_BAD_HEADERS: dict[str, Any] = {
    "list": [1],
    "str": "x",
    "none": None,
    "int": 7,
    "missing-seven-fields": _GOOD_HEADER,  # the closed field set is missing the other seven
    "good-plus-str-dropped": {**_GOOD_HEADER, "ambiguous_dropped": "5"},
    "good-plus-float-dropped": {**_GOOD_HEADER, "ambiguous_dropped": 1.5},
    "good-plus-none-dropped": {**_GOOD_HEADER, "ambiguous_dropped": None},
    "good-plus-bool-dropped": {**_GOOD_HEADER, "ambiguous_dropped": True},
    "real-str-dropped": "str",
    "real-float-dropped": 1.5,
    "real-none-dropped": None,
    "real-bool-dropped": True,
}


@pytest.mark.parametrize("name", list(_BAD_HEADERS))
def test_a_malformed_vault_header_is_vault_unreadable_and_not_the_same_rows(name: str) -> None:
    """A non-dict header or a non-int `ambiguous_dropped` is `vault_unreadable` from `load_vault`
    (not an AttributeError, ValueError or TypeError), so `same_rows` answers False."""
    header = _BAD_HEADERS[name]
    if name.startswith("real-"):
        # Built here, not at import: the real header holds random ids, which would give each
        # xdist worker different test ids.
        header = {**_real_header(), "ambiguous_dropped": header}
    blob = _vault_blob_with_header(header)
    with pytest.raises(VaultError) as caught:
        load_vault(None, KEY, vault=_vault_order.BytesHandle(blob))
    assert caught.value.code == reasons.runtime.VAULT_UNREADABLE
    assert _vault_order.same_rows(blob, writer()) is False


def test_a_header_reframed_unchanged_still_reads_as_own() -> None:
    """Non-vacuity for the forgery helper: the writer's own header, re-framed, is the same rows."""
    blob, _ = writer().serialize()
    start = len(_format._MAGIC)
    length = int.from_bytes(blob[start : start + 4], "big")
    reframed = _vault_blob_with_header(json.loads(blob[start + 4 : start + 4 + length]), blob)
    assert _vault_order.same_rows(reframed, writer()) is True


def test_a_header_edit_that_keeps_the_rules_is_tampered_and_not_the_same_rows() -> None:
    blob = _vault_blob_with_header({**_real_header(), "ambiguous_dropped": 5})
    with pytest.raises(VaultError) as caught:
        load_vault(None, KEY, vault=_vault_order.BytesHandle(blob))
    assert caught.value.code == reasons.runtime.VAULT_TAMPERED
    assert _vault_order.same_rows(blob, writer()) is False


@pytest.mark.parametrize("policy", POLICIES)
def test_adopt_refuses_a_character_device_leaf_before_reading(
    base: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `S_ISREG` check runs on the opened descriptor: a leaf fd that reports a character
    device is an OSError and `os.read` never touches it."""
    out, root_fd = vault_out(policy, base, REL)
    (base / REL).write_bytes(b"abcdef")
    leaf_fds: set[int] = set()
    reads: list[int] = []
    real_open, real_fstat, real_read = os.open, os.fstat, os.read

    def spy_open(path: Any, fl: int, *args: Any, **kw: Any) -> int:
        fd = real_open(path, fl, *args, **kw)
        if os.fsdecode(path).endswith(LEAF):
            leaf_fds.add(fd)
        return fd

    def fake_fstat(fd: int) -> Any:
        st = real_fstat(fd)
        if fd not in leaf_fds:
            return st
        fields = list(st)
        fields[0] = stat.S_IFCHR | 0o600
        return os.stat_result(fields)

    def spy_read(fd: int, n: int) -> bytes:
        reads.append(fd)
        return real_read(fd, n)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(os, "read", spy_read)
    try:
        with pytest.raises(OSError):
            out.adopt_file(limit=100)
        assert leaf_fds and not set(reads) & leaf_fds  # non-vacuity: the leaf was opened
    finally:
        monkeypatch.undo()
        if root_fd >= 0:
            os.close(root_fd)


def _vault_blob_with_chunks(*payloads: bytes) -> bytes:
    """An authenticated v3 file with one segment per payload (the last is the final one)."""
    return forge_payloads(list(payloads), key=KEY)


def _parquet(rows: Any, names: Any = ("namespace", "masked", "source")) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {n: pa.array([r[i] for r in rows], type=pa.string()) for i, n in enumerate(names)}
    )
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    return buf.getvalue().to_pybytes()


_SAME = (("ns", "m1", "s1"), ("ns", "m2", "s2"))
_DUPLICATE_VAULTS = {
    "one chunk": [_parquet((("ns", "m1", "wrong"), *_SAME))],
    "two chunks": [_parquet(_SAME), _parquet((("ns", "m1", "wrong"),))],
    "same value twice": [_parquet((*_SAME, ("ns", "m1", "s1")))],
}


@pytest.mark.parametrize("name", list(_DUPLICATE_VAULTS))
def test_a_vault_with_a_duplicate_key_is_tampered_and_never_adopted(base: Path, name: str) -> None:
    """The writer folds duplicates away, so a stored duplicate `(namespace, masked)` is corruption:
    it must not read as the writer's rows even when the last value equals the writer's."""
    blob = _vault_blob_with_chunks(*_DUPLICATE_VAULTS[name])
    with pytest.raises(VaultError) as caught:
        load_vault(None, KEY, vault=_vault_order.BytesHandle(blob))
    assert caught.value.code == reasons.runtime.VAULT_TAMPERED
    assert "wrong" not in str(caught.value) and "s1" not in str(caught.value)
    assert _vault_order.same_rows(blob, writer()) is False
    (base / REL).write_bytes(blob)
    out, root_fd = vault_out("strict", base, REL)
    try:
        assert isinstance(_vault_order.adopt_vault(out, writer()), _vault_order.VaultConflict)
    finally:
        os.close(root_fd)


def test_the_control_vault_built_from_chunks_reads_as_own() -> None:
    blob = _vault_blob_with_chunks(_parquet(_SAME))
    assert _vault_order.same_rows(blob, writer()) is True


_NAMES = ("namespace", "masked", "source")


def _arrow_payload(columns: dict[str, Any]) -> bytes:
    buf = pa.BufferOutputStream()
    pq.write_table(pa.table(columns), buf)
    return buf.getvalue().to_pybytes()


_MALFORMED_PAYLOADS = {
    "not parquet": b"not a parquet file",
    "wrong schema": _parquet(_SAME, names=("namespace", "masked", "other")),
    "missing columns": _parquet([("a",)], names=("namespace",)),
    "int64 columns": _arrow_payload({n: pa.array([1, 2], type=pa.int64()) for n in _NAMES}),
    "null-typed column": _arrow_payload(
        {"namespace": pa.array(["n1", "n2"]), "masked": pa.nulls(2), "source": pa.array(["a", "b"])}
    ),
    "null values": _arrow_payload(
        {
            "namespace": pa.array(["ns", "ns"]),
            "masked": pa.array(["m1", None], type=pa.string()),
            "source": pa.array(["a", "b"]),
        }
    ),
    "reordered columns": _arrow_payload({n: pa.array(["x", "y"]) for n in reversed(_NAMES)}),
}


@pytest.mark.parametrize("name", list(_MALFORMED_PAYLOADS))
def test_an_authenticated_malformed_payload_is_tampered_not_a_crash(name: str) -> None:
    blob = _vault_blob_with_chunks(_MALFORMED_PAYLOADS[name])
    with pytest.raises(VaultError) as caught:
        load_vault(None, KEY, vault=_vault_order.BytesHandle(blob))
    assert caught.value.code == reasons.runtime.VAULT_TAMPERED
    assert caught.value.__cause__ is None
    assert writer().reads_as_own(_vault_order.BytesHandle(blob)) is False


@pytest.mark.parametrize("kind", ["retired_v2", "tampered_v3"])
def test_adopt_refuses_a_v2_or_tampered_leaf_and_leaves_its_bytes(base: Path, kind: str) -> None:
    if kind == "retired_v2":
        blob = (
            Path(__file__).parents[2]
            / "unit"
            / "vault"
            / "fixtures"
            / "decoy-vault-v2__retired.vault"
        ).read_bytes()
    else:
        mutable = bytearray(writer().serialize()[0])
        mutable[-1] ^= 0xFF
        blob = bytes(mutable)
    (base / REL).write_bytes(blob)
    out, root_fd = vault_out("strict", base, REL)
    try:
        assert isinstance(_vault_order.adopt_vault(out, writer()), _vault_order.VaultConflict)
    finally:
        os.close(root_fd)
    assert (base / REL).read_bytes() == blob
