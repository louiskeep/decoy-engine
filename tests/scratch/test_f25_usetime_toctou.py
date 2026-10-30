"""F25 use-time TOCTOU acceptance for the identity-bound capability model.

The 3-round publication-TOCTOU cap forced the correct-by-construction design
(argon 2026-10-30 plan-remediation): a capability is an identity-bound handle,
and ``unwrap_path`` / ``unwrap_dir`` / ``release`` re-verify the pinned parent
and target ``(st_dev, st_ino)`` under ``O_NOFOLLOW`` at the moment of use. The
earlier suite (test_f25_slice4_classification.py) only swapped the parent BEFORE
``allocate_publication`` ran, so it proved allocation-time hardening, not the
use-time guarantee.

These tests swap the parent or the child AFTER a successful allocation and prove
the swap is refused at the unwrap/release seam for all three writable capability
types, and that the attacker's out-of-arena target is never touched. Without the
use-time verification in ``_capabilities.open_verified_target`` every one of
these calls returns the stale path and the swap succeeds silently; that is the
RED the fix turns green.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from decoy_engine.scratch import (
    Arena,
    NativeDirectory,
    PublicationStage,
    RegisteredFile,
    ScratchManager,
    ScratchPathUnsafe,
    unwrap_dir,
    unwrap_path,
)
from decoy_engine.scratch._policy import ResourceKind
from tests.scratch._fakes import FakeAuthority, QualifiedTmpfsProvider, make_policy


def _open_arena(tmp_path: Path) -> tuple[ScratchManager, Arena]:
    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    provider = QualifiedTmpfsProvider()
    manager = ScratchManager.open_pool(
        pool_id="toctou", control_arena=tmp_path / "control", provider=provider
    )
    arena = manager.open_attempt(make_policy(stage_root), provider, FakeAuthority())
    return manager, arena


# Each entry allocates one capability type and returns the capability, the
# use-time call that must re-verify it, and the target path on disk.
def _alloc_file(arena: Arena, tmp_path: Path) -> tuple[RegisteredFile, Callable[[], Path], Path]:
    cap = arena.allocate_file(ResourceKind.FILE, "opaque")
    return cap, (lambda: unwrap_path(cap)), unwrap_path(cap)


def _alloc_native(arena: Arena, tmp_path: Path) -> tuple[NativeDirectory, Callable[[], Path], Path]:
    cap = arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "duckdb")
    return cap, (lambda: unwrap_dir(cap)), unwrap_dir(cap)


def _alloc_publication(
    arena: Arena, tmp_path: Path
) -> tuple[PublicationStage, Callable[[], Path], Path]:
    dest = tmp_path / "out" / "customers.parquet"
    dest.parent.mkdir()
    cap = arena.allocate_publication(dest, retention_owner="t09")
    return cap, (lambda: unwrap_path(cap)), unwrap_path(cap)


_ALLOCATORS = {
    "registered_file": _alloc_file,
    "native_directory": _alloc_native,
    "publication_stage": _alloc_publication,
}


@pytest.mark.parametrize("kind", sorted(_ALLOCATORS))
def test_parent_swap_after_allocation_rejected_at_unwrap(tmp_path: Path, kind: str) -> None:
    """The target's parent directory is swapped for a symlink to an attacker
    directory AFTER allocation. The pinned parent opened ``O_NOFOLLOW`` trips
    ``ELOOP`` (or an identity mismatch), so the unwrap seam raises
    ``ScratchPathUnsafe`` and never resolves onto the attacker's matching entry.
    """

    manager, arena = _open_arena(tmp_path)
    try:
        _cap, use, target = _ALLOCATORS[kind](arena, tmp_path)
        parent = target.parent
        was_dir = target.is_dir()
        # Move the real parent aside and stand up an attacker directory that
        # carries a decoy entry with the SAME basename the capability expects.
        parent_real = parent.with_name(parent.name + ".real")
        parent.rename(parent_real)
        outside = tmp_path / "attacker"
        outside.mkdir()
        decoy = outside / target.name
        if was_dir:
            decoy.mkdir()
        else:
            decoy.write_text("attacker payload")
        parent.symlink_to(outside, target_is_directory=True)

        with pytest.raises(ScratchPathUnsafe):
            use()

        # The attacker's matching entry was never opened, written or removed.
        if decoy.is_dir():
            assert list(decoy.iterdir()) == []
        else:
            assert decoy.read_text() == "attacker payload"
        # Restore the real parent so manager teardown operates on the arena, not
        # through the attacker symlink.
        parent.unlink()
        parent_real.rename(parent)
    finally:
        manager.close()


@pytest.mark.parametrize("kind", sorted(_ALLOCATORS))
def test_child_symlink_swap_after_allocation_rejected_at_unwrap(tmp_path: Path, kind: str) -> None:
    """The allocated child is replaced in place with a symlink pointing OUTSIDE
    the arena after allocation. The child opened ``O_NOFOLLOW`` trips ``ELOOP``,
    so the unwrap seam raises ``ScratchPathUnsafe`` and the outside target is left
    untouched."""

    manager, arena = _open_arena(tmp_path)
    try:
        _cap, use, target = _ALLOCATORS[kind](arena, tmp_path)
        outside = tmp_path / "attacker_target"
        if target.is_dir():
            outside.mkdir()
            target.rmdir()
        else:
            outside.write_text("secret")
            target.unlink()
        target.symlink_to(outside)

        with pytest.raises(ScratchPathUnsafe):
            use()

        if outside.is_dir():
            assert list(outside.iterdir()) == []
        else:
            assert outside.read_text() == "secret"
    finally:
        manager.close()


@pytest.mark.parametrize("kind", sorted(_ALLOCATORS))
def test_child_fresh_inode_swap_after_allocation_rejected_at_unwrap(
    tmp_path: Path, kind: str
) -> None:
    """The allocated child is replaced with a decoy of the SAME type and mode but
    a DIFFERENT inode, swapped in with ``os.replace`` after allocation.
    ``O_NOFOLLOW`` passes (no symlink) but the pinned ``(st_dev, st_ino)`` no
    longer matches, so the identity check (not just the no-follow guard) raises
    ``ScratchPathUnsafe``.

    The decoy is created WHILE the original still exists so the kernel cannot
    hand it the original's just-freed inode number (inode reuse would otherwise
    make an unlink+recreate at the same name indistinguishable)."""

    manager, arena = _open_arena(tmp_path)
    try:
        _cap, use, target = _ALLOCATORS[kind](arena, tmp_path)
        decoy = target.with_name(target.name + ".decoy")
        if target.is_dir():
            decoy.mkdir(mode=0o700)
        else:
            fd = os.open(decoy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        assert os.stat(decoy).st_ino != os.stat(target).st_ino
        os.replace(decoy, target)  # target's entry now points at the decoy inode

        with pytest.raises(ScratchPathUnsafe):
            use()
    finally:
        manager.close()


def test_release_refuses_to_reclaim_a_child_swapped_for_a_symlink(tmp_path: Path) -> None:
    """``release`` re-verifies the recorded identity before the destructive
    reclaim, so a native dir swapped for a symlink to an outside tree after
    allocation cannot trick the provider into deleting outside the arena."""

    manager, arena = _open_arena(tmp_path)
    try:
        cap = arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "duckdb")
        target = unwrap_dir(cap)
        outside = tmp_path / "victim_tree"
        outside.mkdir()
        (outside / "keepme.txt").write_text("do not delete")
        target.rmdir()
        target.symlink_to(outside, target_is_directory=True)

        with pytest.raises(ScratchPathUnsafe):
            arena.release(cap)

        # The victim tree the symlink pointed at is intact.
        assert (outside / "keepme.txt").read_text() == "do not delete"
    finally:
        manager.close()


def test_release_reclaim_refuses_parent_swapped_for_symlink(tmp_path: Path) -> None:
    """The destructive reclaim is bound to the pinned parent/child inodes, not a
    re-resolved path. If the arena root is swapped for a symlink to an attacker
    tree after allocation, ``release`` must fail closed (``ScratchPathUnsafe``)
    and the attacker tree must be left intact: a path-based ``rmtree`` would have
    followed the symlink and deleted the attacker tree at the target's name."""

    manager, arena = _open_arena(tmp_path)
    try:
        cap = arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "duckdb")
        target = unwrap_dir(cap)
        root = target.parent
        # Attacker tree carrying an entry with the SAME basename the reclaim path
        # would resolve to through the swapped-in symlink.
        outside = tmp_path / "attacker_root"
        (outside / target.name).mkdir(parents=True)
        (outside / target.name / "keepme.txt").write_text("do not delete")

        root_real = root.with_name(root.name + ".real")
        root.rename(root_real)
        root.symlink_to(outside, target_is_directory=True)

        with pytest.raises(ScratchPathUnsafe):
            arena.release(cap)

        assert (outside / target.name / "keepme.txt").read_text() == "do not delete"
        # Restore the real root so teardown operates on the arena, not the link.
        root.unlink()
        root_real.rename(root)
    finally:
        manager.close()


def test_unreclaimable_stage_raises_and_never_activates(tmp_path: Path, monkeypatch) -> None:
    """If a publication stage fails verification AND it cannot be removed (a racer
    chmod or a permission change blocks ``rmdir``/``unlink``), reclaim refuses to
    return as if clean: it raises ``ScratchPathUnsafe`` and the resource is never
    transitioned to ACTIVE (argon guaranteed-reclaim)."""

    manager, arena = _open_arena(tmp_path)
    try:
        from decoy_engine.scratch import _arena as arena_mod

        dest = tmp_path / "pubout" / "customers.parquet"
        dest.parent.mkdir()
        # Force verification to fail: the stage looks wrong-owner.
        real_euid = os.geteuid()
        monkeypatch.setattr(arena_mod.os, "geteuid", lambda: real_euid + 1)
        # And make reclamation impossible: removal under the pinned fd always fails.
        monkeypatch.setattr(
            arena_mod.os, "rmdir", lambda *a, **k: (_ for _ in ()).throw(PermissionError("blocked"))
        )
        monkeypatch.setattr(
            arena_mod.os,
            "unlink",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("blocked")),
        )

        with pytest.raises(ScratchPathUnsafe):
            arena.allocate_publication(dest, retention_owner="t09")

        # No resource for this attempt ever reached ACTIVE.
        states = {r.state for r in manager.ledger.list_resources(arena.arena_id)}
        from decoy_engine.scratch import ResourceState

        assert ResourceState.ACTIVE.value not in states and ResourceState.ACTIVE not in states
    finally:
        manager.close()
