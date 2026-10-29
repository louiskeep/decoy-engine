"""F25 Slice 4 (local-driven) acceptance for the Slice-3 carry-forwards.

Two carry-forwards close here, both exercised on the dev box against real
filesystem semantics and a provider double (the real kernel project-quota
evidence is re-proven on the GCP host, tests/scratch/test_f25_slice4_migrated_fs.py):

* Carried item 3 (plan 1.5 / 1.6): ``Arena.allocate_publication`` gets the same
  symlink / owner / mode / single-device hardening the data-arena allocation
  already uses (the provider's ``create_arena`` checks), not just the
  same-device check it shipped with in Slice 3.
* Carried item 4 (plan 1.5 item 5): DuckDB/COPY (T03-T05 OOC, T13 postgres)
  quota trips raise provider errors with NO errno, so errno classification alone
  misses them. The tiered classifier uses errno where preserved, a bounded
  authoritative kernel probe for provider-quota evidence otherwise, and an
  explicit unknown-I/O classification as the honest fallback, so the failure
  surfaces as a typed shared-report outcome rather than a bare coded error.
"""

from __future__ import annotations

import errno as _errno
import os
from pathlib import Path

import pytest

from decoy_engine.scratch import (
    Arena,
    ScratchManager,
    ScratchPathUnsafe,
    unwrap_path,
)
from decoy_engine.scratch._local_provider import KernelProjectQuotaProvider
from tests.scratch._fakes import FakeAuthority, QualifiedTmpfsProvider, make_policy


class _ExhaustedProvider(QualifiedTmpfsProvider):
    """A provider double whose bounded quota probe reports the attempt is at its
    hard cap, standing in for the GCP host where the kernel actually enforces it.
    Used to exercise the provider-quota-evidence tier without a real quota'd mount.
    """

    def __init__(self, dimension: str = "bytes") -> None:
        self._dimension = dimension

    def probe_exhaustion(self, arena: Path, policy) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
        return True, self._dimension


def _open_arena(tmp_path: Path, provider=None) -> tuple[ScratchManager, Arena]:
    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    provider = provider or QualifiedTmpfsProvider()
    manager = ScratchManager.open_pool(
        pool_id="slice4", control_arena=tmp_path / "control", provider=provider
    )
    arena = manager.open_attempt(make_policy(stage_root), provider, FakeAuthority())
    return manager, arena


# ---------------------------------------------------------------------------
# Carried item 3: publication path symlink / owner / mode / device hardening
# ---------------------------------------------------------------------------


def test_publication_stage_is_owned_0700_and_single_device(tmp_path: Path) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        dest = tmp_path / "out" / "customers.parquet"
        dest.parent.mkdir()
        stage = arena.allocate_publication(dest, retention_owner="t09")
        stage_path = unwrap_path(stage)
        st = os.lstat(stage_path)
        assert not os.path.islink(stage_path)
        assert st.st_mode & 0o777 == 0o700
        assert st.st_uid == os.geteuid()
        assert os.stat(stage_path).st_dev == os.stat(dest.parent).st_dev
    finally:
        manager.close()


def test_publication_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        real_parent = tmp_path / "real_out"
        real_parent.mkdir()
        link_parent = tmp_path / "link_out"
        link_parent.symlink_to(real_parent, target_is_directory=True)
        dest = link_parent / "customers.parquet"
        with pytest.raises(ScratchPathUnsafe):
            arena.allocate_publication(dest, retention_owner="t09")
        # The guard fires before any stage directory is created under the link.
        assert list(real_parent.iterdir()) == []
    finally:
        manager.close()


def test_publication_rejects_wrong_owner_and_reclaims_stage(tmp_path: Path, monkeypatch) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        dest = tmp_path / "out2" / "customers.parquet"
        dest.parent.mkdir()
        from decoy_engine.scratch import _arena as arena_mod

        other_uid = os.geteuid() + 1
        monkeypatch.setattr(arena_mod.os, "geteuid", lambda: other_uid)
        with pytest.raises(ScratchPathUnsafe):
            arena.allocate_publication(dest, retention_owner="t09")
        # A rejected stage is removed, never left owned-but-unverified.
        leftover = [p for p in dest.parent.iterdir() if p.name.startswith(".decoy-stage-")]
        assert leftover == []
    finally:
        manager.close()


def test_publication_rejects_wrong_mode_and_reclaims_stage(tmp_path: Path, monkeypatch) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        dest = tmp_path / "out3" / "customers.parquet"
        dest.parent.mkdir()
        from decoy_engine.scratch import _arena as arena_mod

        real_mkdir = arena_mod.os.mkdir

        def _loose_mkdir(path, mode=0o777):  # type: ignore[no-untyped-def]
            real_mkdir(path, 0o777)

        monkeypatch.setattr(arena_mod.os, "mkdir", _loose_mkdir)
        with pytest.raises(ScratchPathUnsafe):
            arena.allocate_publication(dest, retention_owner="t09")
        leftover = [p for p in dest.parent.iterdir() if p.name.startswith(".decoy-stage-")]
        assert leftover == []
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# Carried item 4: tiered classification of no-errno I/O failures
# ---------------------------------------------------------------------------


def test_classify_errno_quota_is_errno_evidence(tmp_path: Path) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        report = arena.classify_attempt_io(errno=_errno.EDQUOT)
        assert report.evidence == "errno"
        assert report.errno == _errno.EDQUOT
        assert report.dimension in ("bytes", "inodes")
    finally:
        manager.close()


def test_classify_no_errno_with_provider_evidence_is_provider_quota(tmp_path: Path) -> None:
    provider = _ExhaustedProvider("bytes")
    manager, arena = _open_arena(tmp_path, provider=provider)
    try:
        report = arena.classify_attempt_io(errno=None)
        assert report.evidence == "provider_quota"
        assert report.errno is None
        assert report.dimension == "bytes"
    finally:
        manager.close()


def test_classify_no_errno_no_evidence_is_unknown_io(tmp_path: Path) -> None:
    manager, arena = _open_arena(tmp_path)  # tmpfs probe finds headroom
    try:
        report = arena.classify_attempt_io(errno=None)
        assert report.evidence == "unknown_io"
        assert report.errno is None
        assert report.dimension == "unknown"
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# Carried item 4 wiring: OOC runner choke point (T03-T05 DuckDB COPY)
# ---------------------------------------------------------------------------


def test_reraise_ooc_quota_elevates_no_errno_provider_quota(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.execution.out_of_core._budget import reraise_ooc_quota

    manager, arena = _open_arena(tmp_path, provider=_ExhaustedProvider("bytes"))
    try:
        # A DuckDB COPY quota trip arrives as a provider exception with no errno.
        boom = RuntimeError("duckdb IO error")
        with pytest.raises(ExecutionError) as excinfo:
            reraise_ooc_quota(boom, arena)
        assert excinfo.value.code == "out_of_core_temp_disk_exceeded"
        report = getattr(excinfo.value, "report", None)
        assert report is not None
        assert report.evidence == "provider_quota"
        assert report.errno is None
    finally:
        manager.close()


def test_reraise_ooc_quota_propagates_no_errno_without_evidence(tmp_path: Path) -> None:
    from decoy_engine.execution.out_of_core._budget import reraise_ooc_quota

    manager, arena = _open_arena(tmp_path)  # headroom: no quota evidence
    try:
        boom = RuntimeError("some other duckdb error")
        # No elevation: reraise_ooc_quota returns and the caller re-raises boom.
        reraise_ooc_quota(boom, arena)
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# Carried item 4 wiring: postgres COPY (T13) attaches the shared report
# ---------------------------------------------------------------------------


def test_postgres_attach_quota_report_provider_quota(tmp_path: Path) -> None:
    from decoy_engine.connectors.postgres_snapshot import (
        PostgresSnapshotError,
        _attach_quota_report,
    )

    manager, arena = _open_arena(tmp_path, provider=_ExhaustedProvider("bytes"))
    try:
        err = PostgresSnapshotError("export_failed", cause_type="RuntimeError")
        _attach_quota_report(err, arena)
        report = getattr(err, "report", None)
        assert report is not None
        assert report.evidence == "provider_quota"
    finally:
        manager.close()


def test_postgres_attach_quota_report_unknown_io(tmp_path: Path) -> None:
    from decoy_engine.connectors.postgres_snapshot import (
        PostgresSnapshotError,
        _attach_quota_report,
    )

    manager, arena = _open_arena(tmp_path)  # headroom -> unknown-I/O classification
    try:
        err = PostgresSnapshotError("export_failed", cause_type="RuntimeError")
        _attach_quota_report(err, arena)
        report = getattr(err, "report", None)
        assert report is not None
        assert report.evidence == "unknown_io"
    finally:
        manager.close()


def test_postgres_attach_quota_report_no_arena_is_noop() -> None:
    from decoy_engine.connectors.postgres_snapshot import (
        PostgresSnapshotError,
        _attach_quota_report,
    )

    err = PostgresSnapshotError("export_failed", cause_type="RuntimeError")
    _attach_quota_report(err, None)
    assert getattr(err, "report", None) is None


# ---------------------------------------------------------------------------
# The reference provider's probe: a non-quota tmpdir finds headroom (no false
# provider-quota evidence); the real EDQUOT probe is proven on the GCP host.
# ---------------------------------------------------------------------------


def test_reference_provider_probe_finds_headroom_on_plain_dir(tmp_path: Path) -> None:
    provider = KernelProjectQuotaProvider()
    policy = make_policy(tmp_path)
    arena_dir = tmp_path / "arena"
    arena_dir.mkdir(mode=0o700)
    exhausted, _dim = provider.probe_exhaustion(arena_dir, policy)
    assert exhausted is False
