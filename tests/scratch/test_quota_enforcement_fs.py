"""F25 Slice 1 A02: real kernel project-quota enforcement.

These tests only run where a provisioned ext4 arena root with a small project
quota is available, named by ``DECOY_F25_QUOTA_ROOT``. The privileged setup
(mkfs/mount/quotaon/setquota/chattr +P) is done by the deployment adapter as
root; the WRITERS here run as the unprivileged user, because the kernel exempts
root from quota enforcement so a root writer would give a false pass (Slice-0
report condition 1). The GCP driver ``scripts/f25/run_slice1_fs_tests.sh``
provisions the root and runs this suite as ``decoyf25``.

Off that host the suite skips cleanly, so the dev-box loop stays green while the
quota guarantee is only ever claimed where it was actually observed.
"""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from decoy_engine.scratch import (
    KernelProjectQuotaProvider,
    ResourceKind,
    ScratchManager,
    ScratchPolicy,
    ScratchQuotaExceeded,
    unwrap_path,
)

_QUOTA_ROOT = os.environ.get("DECOY_F25_QUOTA_ROOT")
# The control arena MUST be a different quota domain (project) than the data
# arena, so recording a terminal data-quota breach never fails on the data cap
# (plan 1.5 control-arena sizing). The GCP driver provisions it under its own
# project; it falls back to a sibling only when the caller did not set it.
_CONTROL_ROOT = os.environ.get("DECOY_F25_CONTROL_ROOT")
_QUOTA_BYTES = int(os.environ.get("DECOY_F25_QUOTA_BYTES", str(8 << 20)))
_QUOTA_INODES = int(os.environ.get("DECOY_F25_QUOTA_INODES", "64"))

pytestmark = pytest.mark.skipif(
    _QUOTA_ROOT is None,
    reason="set DECOY_F25_QUOTA_ROOT to a provisioned ext4 project-quota arena (GCP host)",
)


def _is_root() -> bool:
    return os.geteuid() == 0


@pytest.fixture
def manager() -> Iterator[ScratchManager]:
    assert _QUOTA_ROOT is not None
    provider = KernelProjectQuotaProvider()
    control = Path(_CONTROL_ROOT) if _CONTROL_ROOT else Path(_QUOTA_ROOT) / ".decoy-control"
    mgr = ScratchManager.open_pool(pool_id="fs-pool", control_arena=control, provider=provider)
    yield mgr
    mgr.close()
    # Reclaim every data arena so the shared data project frees for the next
    # test (each attempt made a uuid child under the quota'd data root).
    data_root = Path(_QUOTA_ROOT)
    for child in data_root.iterdir():
        if child.is_dir() and not child.name.startswith(".decoy-control"):
            shutil.rmtree(child, ignore_errors=True)


def _make_policy() -> ScratchPolicy:
    assert _QUOTA_ROOT is not None
    return ScratchPolicy(
        job_id="fs-job",
        attempt_id="fs-attempt",
        quota_bytes=_QUOTA_BYTES,
        quota_inodes=_QUOTA_INODES,
        labels={"local_stage_root": _QUOTA_ROOT},
    )


def test_a02_root_is_not_running_these_writers() -> None:
    """Guard: if these writers ran as root the kernel would not enforce the
    quota and every assertion below would be a false pass (Slice-0 condition 1)."""
    assert not _is_root(), "A02 quota writers must run unprivileged; root is quota-exempt"


def test_a02_byte_hard_quota_stops_the_attempt(manager: ScratchManager) -> None:
    provider = KernelProjectQuotaProvider()
    arena = manager.open_attempt(_make_policy(), provider, _FsAuthority())
    rf = arena.allocate_file(ResourceKind.FILE, "overflow")
    path = unwrap_path(rf)
    chunk = b"D" * (1 << 20)
    raised: ScratchQuotaExceeded | None = None
    fd = os.open(path, os.O_WRONLY)
    try:
        written = 0
        while written < _QUOTA_BYTES * 4:
            try:
                os.write(fd, chunk)
                written += len(chunk)
            except OSError as exc:
                assert exc.errno in (errno.EDQUOT, errno.ENOSPC)
                try:
                    arena.report_write_failure(rf, exc)
                except ScratchQuotaExceeded as sqe:
                    raised = sqe
                break
    finally:
        os.close(fd)
    assert raised is not None, "writing past the project quota did not raise EDQUOT"
    assert raised.report.effective_bytes_cap == _QUOTA_BYTES
    assert raised.report.dimension in ("bytes", "inodes")
    assert raised.code == "scratch_quota_exceeded"


def test_a02_inode_hard_quota_stops_tiny_file_producer(manager: ScratchManager) -> None:
    provider = KernelProjectQuotaProvider()
    arena = manager.open_attempt(_make_policy(), provider, _FsAuthority())
    nd = arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "many")
    from decoy_engine.scratch import unwrap_dir

    root = unwrap_dir(nd)
    hit = False
    for i in range(_QUOTA_INODES * 4):
        try:
            fd = os.open(root / f"z_{i:06d}", os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        except OSError as exc:
            assert exc.errno == errno.EDQUOT
            hit = True
            break
    assert hit, "creating many zero-byte files did not hit the inode quota"


class _FsAuthority:
    """Minimal authority for the FS suite (no platform needed)."""

    @property
    def mode(self):  # type: ignore[no-untyped-def]
        from decoy_engine.scratch import AuthorityMode

        return AuthorityMode.TARGET_LOCK

    def lease_for(self, job_id: str, attempt_id: str, local_stage_root: str):  # type: ignore[no-untyped-def]
        from decoy_engine.scratch import AuthorityMode, LeaseIdentity

        return LeaseIdentity(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_id="fs-lease",
            generation=1,
            mode=AuthorityMode.TARGET_LOCK,
            local_stage_root=local_stage_root,
        )

    def verify(self, lease) -> bool:  # type: ignore[no-untyped-def]
        return True

    def reap_verdict(self, lease):  # type: ignore[no-untyped-def]
        from decoy_engine.scratch import ReapVerdict

        return ReapVerdict(reap_eligible=False, reason="active")
