"""F25 Slice 4 A02/A03 re-run of the MIGRATED writers on a real quota'd kernel.

Slice 1 proved the kernel project-quota boundary against ``arena.allocate_file``
directly (tests/scratch/test_quota_enforcement_fs.py). This suite proves the SAME
boundary holds when the quota is reached THROUGH the migrated producers, across
both quota contracts:

* Typed-report writers (route the kernel EDQUOT to the shared report): the
  transactional sink (T09), the fixed-width writer (T16), and the isolated-run
  input-source staging (T12). Removing the ``route_write_failure`` translation
  would surface a bare ``OSError`` and fail these ``ScratchQuotaExceeded``
  assertions.
* Raw-OSError writers (no quota routing on the write): the subset materializer
  (T14, polars ``write_parquet``) and the quarantine JSONL sidecar (T15). Their
  only quota contract is that the kernel boundary holds and nothing is published.

It also proves the carried-item-4 provider-quota-evidence tier rests on real
kernel enforcement, not a diagnostic walk: with the arena genuinely at its cap,
the errno-less classifier the DuckDB/COPY (T13) path depends on returns
``provider_quota``. There is no Postgres on the provisioned host, so the live
Postgres COPY itself is not run here; the classifier it relies on is.

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
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

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
    # The typed shared report identifies the cap and dimension with no data
    # values: removing the route_write_failure translation would surface a bare
    # OSError here instead, failing the pytest.raises(ScratchQuotaExceeded).
    assert report.effective_bytes_cap == _QUOTA_BYTES
    assert report.effective_inodes_cap == _QUOTA_INODES
    assert report.dimension in ("bytes", "inodes")
    assert report.evidence in ("errno", "provider_quota")
    assert report.errno in (None, errno.EDQUOT, errno.ENOSPC)
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
    # The inode trip must surface as the typed shared report, not a bare OSError:
    # route_write_failure translates the kernel EDQUOT on the migrated writer.
    with pytest.raises(ScratchQuotaExceeded) as excinfo:
        for i in range(_QUOTA_INODES * 4):
            sink.write(f"table_{i:05d}", tiny)
            hit = True
    report = excinfo.value.report
    assert report.dimension in ("inodes", "bytes")
    assert report.effective_inodes_cap == _QUOTA_INODES
    assert report.evidence in ("errno", "provider_quota")
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


# ---------------------------------------------------------------------------
# Additional migrated-writer families under the real quota (Codex gate: the
# prior round exercised only T09). T16 and the sink route a kernel EDQUOT to the
# typed shared report; T14/T15 are raw-OSError writers whose only quota contract
# is that the kernel boundary holds and nothing is published; T12 routes the
# input-source staging trip; T13's errno-less translation is proven against the
# real kernel probe (no Postgres on this host, so the live COPY itself is not
# run here -- the classifier the COPY path depends on is).
# ---------------------------------------------------------------------------


def _fill_arena_to_byte_cap(arena: Arena) -> None:
    """Write into one registered file until the kernel trips the byte cap, so the
    arena root is genuinely at its hard quota for a subsequent probe."""
    rf = arena.allocate_file(ResourceKind.FILE, "fill")
    fd = os.open(unwrap_path(rf), os.O_WRONLY)
    chunk = b"Z" * (1 << 20)
    try:
        while True:
            try:
                os.write(fd, chunk)
            except OSError as exc:
                assert exc.errno in (errno.EDQUOT, errno.ENOSPC)
                return
    finally:
        os.close(fd)


def test_t16_fixed_width_byte_quota_raises_typed_report(manager: ScratchManager) -> None:
    """T16: a fixed-width write larger than the byte cap trips the kernel quota
    on the staged temp file and is routed to the typed shared report (errno
    path). Removing route_write_failure would surface a bare OSError and fail the
    ScratchQuotaExceeded assertion."""
    pd = pytest.importorskip("pandas")
    from decoy_engine.config._fixed_width import FixedWidthColumn, FixedWidthLayout
    from decoy_engine.profile._fixed_width_writer import write_fixed_width

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    width = 1000
    rows = (_QUOTA_BYTES * 3) // (width + 1)  # ~3x the cap of uncompressed text
    frame = pd.DataFrame({"c": ["X" * width] * rows})
    layout = FixedWidthLayout(columns=[FixedWidthColumn(name="c", start=0, width=width)])
    out = Path(_QUOTA_ROOT) / "t16_out.fw"
    with pytest.raises(ScratchQuotaExceeded) as excinfo:
        write_fixed_width(frame, layout, out, scratch_arena=arena)
    report = excinfo.value.report
    assert report.effective_bytes_cap == _QUOTA_BYTES
    assert report.dimension in ("bytes", "inodes")
    assert report.evidence in ("errno", "provider_quota")
    assert not out.exists(), "no fixed-width output may be published past the cap"


def test_t14_subset_materialize_byte_quota_stops(manager: ScratchManager) -> None:
    """T14: the subset materializer (a non-routing writer: polars write_parquet)
    must have the kernel boundary hold -- a write past the cap fails with a
    quota error (polars wraps the kernel EDQUOT, os error 122, in its own
    ``PolarsError`` rather than a Python ``OSError``). The materializer has no
    atomic contract of its own (the stage discard is ``run_subset``'s job), so
    this unit proves the boundary, not durability."""
    pl = pytest.importorskip("polars")
    from decoy_engine.subset._materialize import materialize_subset
    from decoy_engine.subset._types import SubsetSource

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    # Source parquet lives OFF the quota mount so building it never trips the cap.
    src_tmp = tempfile.mkdtemp(prefix="t14-src-")
    try:
        src = Path(src_tmp) / "t.parquet"
        pq.write_table(_incompressible(_QUOTA_BYTES * 4), src)
        nrows = pq.read_metadata(src).num_rows
        out_dir = Path(_QUOTA_ROOT) / "t14_out"
        with pytest.raises((OSError, pl.exceptions.PolarsError)) as excinfo:
            materialize_subset(
                sources={"t": SubsetSource(path=str(src), format="parquet")},
                survivors={"t": frozenset(range(nrows))},
                output_dir=out_dir,
                scratch_arena=arena,
            )
        msg = str(excinfo.value).lower()
        assert (
            "quota" in msg
            or "122" in msg
            or getattr(excinfo.value, "errno", None) in (errno.EDQUOT, errno.ENOSPC)
        ), f"write failure is not a quota trip: {excinfo.value!r}"
    finally:
        shutil.rmtree(src_tmp, ignore_errors=True)


def test_t15_quarantine_stage_byte_quota_stops_unpublished(manager: ScratchManager) -> None:
    """T15: the quarantine sidecar (a raw-OSError writer: raw fh.write of JSONL,
    no quota routing) must have the kernel boundary hold -- the staged write fails
    with EDQUOT/ENOSPC and nothing is published to output_path."""
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
    from decoy_engine.quarantine import finalize_committed_quarantine

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    sink = ParquetTransactionalSink(Path(_QUOTA_ROOT) / "t15_tables")
    output_path = str(Path(_QUOTA_ROOT) / "t15_quarantine.jsonl")
    # Enough raw-row entries that the JSONL exceeds the small byte cap.
    big = "Y" * 4096
    entries = [
        {"table": "t", "column": "c", "row_index": i, "trigger": "x", "reason": big}
        for i in range(_QUOTA_BYTES * 3 // 4096)
    ]
    with pytest.raises(OSError) as excinfo:
        finalize_committed_quarantine(
            sink=sink,
            is_genuine_transactional_sink=True,
            written_tables=[],
            output_path=output_path,
            entries=entries,
            counts_by_trigger={"x": len(entries)},
            scratch_arena=arena,
        )
    assert excinfo.value.errno in (errno.EDQUOT, errno.ENOSPC)
    assert not Path(output_path).exists(), "no quarantine sidecar may be published past the cap"


def test_t12_isolated_run_source_staging_byte_quota_typed_report(
    manager: ScratchManager,
) -> None:
    """T12: staging the input sources for an isolated run into the registered work
    root (on the quota mount) trips the kernel byte cap in the PARENT before any
    child is spawned, routed to the typed shared report (errno path)."""
    from decoy_engine.execution._isolated_run import run_pipeline_isolated

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    big = _incompressible(_QUOTA_BYTES * 4)
    with pytest.raises(ScratchQuotaExceeded) as excinfo:
        run_pipeline_isolated(
            {},  # never reaches the child; the source staging trips first
            {"t": big},
            scratch_arena=arena,
            isolate=True,
            timeout_s=60,
        )
    report = excinfo.value.report
    assert report.effective_bytes_cap == _QUOTA_BYTES
    assert report.dimension in ("bytes", "inodes")
    assert report.evidence in ("errno", "provider_quota")


def test_t13_errno_less_copy_classifies_provider_quota_on_real_kernel(
    manager: ScratchManager,
) -> None:
    """T13 translation (carried item 4): a DuckDB/COPY quota trip loses the kernel
    errno, so the snapshot's ``_attach_quota_report`` classifies via the bounded
    kernel probe. Proven against the REAL kernel here (no Postgres on this host):
    with the arena genuinely at its byte cap, the errno-less classification must
    be ``provider_quota`` and must attach the typed report to the error. Removing
    the typed-report translation would leave ``err.report`` unset and fail this."""
    from decoy_engine.connectors.postgres_snapshot import (
        PostgresSnapshotError,
        _attach_quota_report,
    )

    assert _QUOTA_ROOT is not None
    arena = _arena(manager)
    _fill_arena_to_byte_cap(arena)
    # The errno-less classifier (what the DuckDB COPY path calls) reads the real
    # kernel probe and returns provider-quota evidence, not a false claim.
    direct = arena.classify_attempt_io(errno=None)
    assert direct.evidence == "provider_quota"
    assert direct.errno is None
    # The T13 seam attaches that typed report onto the snapshot's coded error.
    err = PostgresSnapshotError("export_failed", cause_type="RuntimeError")
    _attach_quota_report(err, arena)
    report = getattr(err, "report", None)
    assert report is not None, "the errno-less translation must attach a typed report"
    assert report.evidence == "provider_quota"
    assert report.effective_bytes_cap == _QUOTA_BYTES
