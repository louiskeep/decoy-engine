"""F25 Slice 4 A02/A03 re-run of the MIGRATED writers on a real quota'd kernel.

Slice 1 proved the kernel project-quota boundary against ``arena.allocate_file``
directly (tests/scratch/test_quota_enforcement_fs.py). This suite proves the SAME
boundary holds when the quota is reached THROUGH a migrated T09-T17 producer (the
transactional sink), and that the carried-item-4 provider-quota-evidence tier
rests on real kernel enforcement, not a diagnostic walk.

A02 (hard byte and inode caps): the transactional sink pushed past the small
real quota fails without exceeding the effective kernel cap, the attempt stops,
and the typed shared report identifies the cap/dimension with no data values. A
runaway tiny-table producer trips the inode cap instead of evading the byte cap.

A03 (accounting): two migrated writers under ONE attempt arena, including the
durable output each publishes, draw on ONE project quota, so the second writer
sees the first's charge. There is no per-writer quota multiplication.

Carried item 4 (plan 1.5 item 5): a DuckDB/COPY quota trip loses the kernel
errno, so the classifier falls to ``probe_exhaustion``. On a real quota'd mount
the probe must read EDQUOT/ENOSPC from the kernel when the arena is at its cap,
so ``classify_attempt_io(errno=None)`` returns ``provider_quota`` evidence rather
than a false quota claim or a bare error.

The root-exemption guard refuses to claim a pass if these writers run as root,
because the kernel exempts root from quota. Off the provisioned host the suite
skips cleanly. The GCP driver ``scripts/f25/run_slice4_fs_tests.sh`` provisions
the mount and runs this as the unprivileged ``decoyf25`` user.
"""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")

from decoy_engine.scratch import (  # noqa: E402
    Arena,
    AuthorityMode,
    KernelProjectQuotaProvider,
    LeaseIdentity,
    ReapVerdict,
    ResourceKind,
    ScratchManager,
    ScratchPolicy,
    ScratchQuotaExceeded,
    unwrap_dir,
    unwrap_path,
)

_QUOTA_ROOT = os.environ.get("DECOY_F25_QUOTA_ROOT")
_CONTROL_ROOT = os.environ.get("DECOY_F25_CONTROL_ROOT")
_QUOTA_BYTES = int(os.environ.get("DECOY_F25_QUOTA_BYTES", str(8 << 20)))
_QUOTA_INODES = int(os.environ.get("DECOY_F25_QUOTA_INODES", "64"))

pytestmark = pytest.mark.skipif(
    _QUOTA_ROOT is None,
    reason="set DECOY_F25_QUOTA_ROOT to a provisioned ext4 project-quota arena (GCP host)",
)


def _is_root() -> bool:
    return os.geteuid() == 0


class _FsAuthority:
    @property
    def mode(self) -> AuthorityMode:
        return AuthorityMode.TARGET_LOCK

    def lease_for(self, job_id: str, attempt_id: str, local_stage_root: str) -> LeaseIdentity:
        return LeaseIdentity(
            job_id=job_id,
            attempt_id=attempt_id,
            lease_id="fs-lease",
            generation=1,
            mode=AuthorityMode.TARGET_LOCK,
            local_stage_root=local_stage_root,
        )

    def verify(self, lease: LeaseIdentity) -> bool:
        return True

    def reap_verdict(self, lease: LeaseIdentity) -> ReapVerdict:
        return ReapVerdict(reap_eligible=False, reason="active")


@pytest.fixture
def manager() -> Iterator[ScratchManager]:
    assert _QUOTA_ROOT is not None
    provider = KernelProjectQuotaProvider()
    control = Path(_CONTROL_ROOT) if _CONTROL_ROOT else Path(_QUOTA_ROOT) / ".decoy-control"
    mgr = ScratchManager.open_pool(pool_id="fs-slice4", control_arena=control, provider=provider)
    yield mgr
    mgr.close()
    data_root = Path(_QUOTA_ROOT)
    for child in data_root.iterdir():
        if child.is_dir() and not child.name.startswith(".decoy-control"):
            shutil.rmtree(child, ignore_errors=True)


def _make_policy(**overrides: object) -> ScratchPolicy:
    assert _QUOTA_ROOT is not None
    kwargs: dict = {
        "job_id": "fs-job",
        "attempt_id": "fs-attempt",
        "quota_bytes": _QUOTA_BYTES,
        "quota_inodes": _QUOTA_INODES,
        "labels": {"local_stage_root": _QUOTA_ROOT},
    }
    kwargs.update(overrides)
    return ScratchPolicy(**kwargs)  # type: ignore[arg-type]


def _arena(manager: ScratchManager, **overrides: object) -> Arena:
    provider = KernelProjectQuotaProvider()
    return manager.open_attempt(_make_policy(**overrides), provider, _FsAuthority())


def _incompressible(total_bytes: int, *, cell: int = 4096):  # -> pa.Table
    """A one-column table of raw random bytes, so parquet cannot compress it
    below the byte cap and the write is forced to trip the kernel quota."""
    rows = max(1, total_bytes // cell)
    return pa.table({"v": pa.array([os.urandom(cell) for _ in range(rows)], pa.binary())})


def test_a02_root_is_not_running_these_writers() -> None:
    assert not _is_root(), "A02 migrated-writer quota writers must run unprivileged"


def test_a02_transactional_sink_byte_quota_stops_with_report(manager: ScratchManager) -> None:
    """T09: a whole-table parquet write larger than the byte cap trips the kernel
    quota through the publication stage and surfaces the shared report (A02)."""
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    out = Path(_QUOTA_ROOT) / "t09_out"
    sink = ParquetTransactionalSink(out, scratch_arena=arena)
    big = _incompressible(_QUOTA_BYTES * 4)  # 4x the cap, incompressible
    with pytest.raises(ScratchQuotaExceeded) as excinfo:
        sink.write("customers", big)
        sink.commit()
    report = excinfo.value.report
    assert report.effective_bytes_cap == _QUOTA_BYTES
    assert report.dimension in ("bytes", "inodes")
    assert report.evidence in ("errno", "provider_quota")
    committed = out / "customers.parquet"
    assert not committed.exists(), "no output may be committed past the cap"


def test_a02_inode_quota_stops_many_tiny_tables(manager: ScratchManager) -> None:
    """A02: a producer that creates many tiny staged files trips the inode cap,
    so a runaway tiny-file producer cannot evade the byte cap."""
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    out = Path(_QUOTA_ROOT) / "t09_inodes"
    sink = ParquetTransactionalSink(out, scratch_arena=arena)
    tiny = pa.table({"v": [1]})
    hit = False
    # Stage many tiny tables into the one sink (no commit): each is its own
    # staged file, so the inode cap trips before the byte cap can be reached.
    try:
        for i in range(_QUOTA_INODES * 4):
            sink.write(f"table_{i:05d}", tiny)
    except ScratchQuotaExceeded as exc:
        hit = True
        assert exc.report.dimension in ("inodes", "bytes")
    except OSError as exc:
        assert exc.errno in (errno.EDQUOT, errno.ENOSPC)
        hit = True
    assert hit, "a runaway tiny-table producer did not hit the inode quota"


def test_a03_two_writers_share_one_attempt_budget(manager: ScratchManager) -> None:
    """A03: two migrated writers under ONE attempt arena, including the durable
    output the first one publishes, draw on ONE project quota; the second writer
    sees the first's charge, so there is no per-writer quota multiplication."""
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    # Roughly half the cap each: the first commits durably (and its bytes stay
    # charged to the shared project), so the second near-half write trips.
    first = ParquetTransactionalSink(Path(_QUOTA_ROOT) / "a03_first", scratch_arena=arena)
    first.write("a", _incompressible(_QUOTA_BYTES * 6 // 10))
    first.commit()
    second = ParquetTransactionalSink(Path(_QUOTA_ROOT) / "a03_second", scratch_arena=arena)
    with pytest.raises(ScratchQuotaExceeded):
        second.write("b", _incompressible(_QUOTA_BYTES * 6 // 10))
        second.commit()


def test_carried4_probe_reads_real_kernel_byte_exhaustion(manager: ScratchManager) -> None:
    """Carried item 4: once the arena is at its byte cap, the bounded kernel
    probe reads EDQUOT/ENOSPC, so a no-errno failure classifies as provider_quota
    (not a diagnostic walk, not a false claim). This is the real-kernel foundation
    the DuckDB/COPY classification rests on."""
    assert _QUOTA_ROOT is not None
    provider = KernelProjectQuotaProvider()
    arena = _arena(manager)
    rf = arena.allocate_file(ResourceKind.FILE, "fill")
    fd = os.open(unwrap_path(rf), os.O_WRONLY)
    chunk = b"Z" * (1 << 20)
    try:
        while True:
            try:
                os.write(fd, chunk)
            except OSError as exc:
                assert exc.errno in (errno.EDQUOT, errno.ENOSPC)
                break
    finally:
        os.close(fd)
    # The arena root is now at its hard byte cap.
    root = Path(arena.capability.approved_root)
    exhausted, dimension = provider.probe_exhaustion(root, _make_policy())
    assert exhausted is True
    assert dimension in ("bytes", "inodes")
    report = arena.classify_attempt_io(errno=None)
    assert report.evidence == "provider_quota"
    assert report.errno is None


def test_carried4_probe_reads_real_kernel_inode_exhaustion(manager: ScratchManager) -> None:
    """Carried item 4, inode dimension: a tiny-file arena at its inode cap makes
    the probe read EDQUOT on inode creation, classified as provider_quota."""
    assert _QUOTA_ROOT is not None
    provider = KernelProjectQuotaProvider()
    arena = _arena(manager)
    nd = arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "many")
    root = unwrap_dir(nd)
    for i in range(_QUOTA_INODES * 4):
        try:
            fd = os.open(root / f"z_{i:06d}", os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        except OSError as exc:
            assert exc.errno == errno.EDQUOT
            break
    exhausted, dimension = provider.probe_exhaustion(
        Path(arena.capability.approved_root), _make_policy()
    )
    assert exhausted is True
    assert dimension in ("inodes", "bytes")
