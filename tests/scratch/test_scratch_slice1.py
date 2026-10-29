"""F25 Slice 1 acceptance + unit tests (dev-box, no kernel quota).

These cover the Slice-1 A-contract rows that do not need a real ext4 project
quota: A01 ownership/isolation, A03 accounting (no per-connection quota
multiplication), A13 fail-closed, plus the ledger CAS / authority-fault and
control-bootstrap behaviour the Slice-1 gate requires. The real kernel hard
quota (A02) is proven on the GCP quota host by test_quota_enforcement_fs.py.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from decoy_engine.scratch import (
    Arena,
    AuthorityMode,
    AuthorityUnavailable,
    Disposition,
    KernelProjectQuotaProvider,
    LeaseAuthorityDisabledError,
    PlatformLeaseAuthority,
    ResourceKind,
    ResourceLedger,
    ResourceState,
    ScratchBackendUnavailable,
    ScratchCleanupError,
    ScratchControlUnavailable,
    ScratchManager,
    ScratchOwnershipError,
    ScratchPathUnsafe,
    TargetLockAuthority,
)
from tests.scratch._fakes import (
    FakeAuthority,
    FakePlatformClient,
    FakeTargetLockClient,
    QualifiedTmpfsProvider,
    make_policy,
)


def _open_pool(tmp_path: Path) -> tuple[ScratchManager, QualifiedTmpfsProvider]:
    provider = QualifiedTmpfsProvider()
    control = tmp_path / ".decoy-control"
    manager = ScratchManager.open_pool(pool_id="pool-1", control_arena=control, provider=provider)
    return manager, provider


def _open_attempt(tmp_path: Path, **policy_overrides: object) -> tuple[ScratchManager, Arena]:
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    manager, provider = _open_pool(tmp_path)
    policy = make_policy(stage, **policy_overrides)
    arena = manager.open_attempt(policy, provider, FakeAuthority())
    return manager, arena


# --- A01 ownership and isolation -------------------------------------------


def test_a01_arenas_0700_and_metadata_0600_under_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0o000)
    try:
        manager, arena = _open_attempt(tmp_path)
    finally:
        os.umask(old)
    root = arena.capability.approved_root
    assert os.stat(root).st_mode & 0o777 == 0o700
    db = tmp_path / ".decoy-control" / "ledger.db"
    assert db.exists()
    assert os.stat(db).st_mode & 0o777 == 0o600
    manager.close()


def test_a01_two_attempts_have_distinct_arenas(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    manager, provider = _open_pool(tmp_path)
    a1 = manager.open_attempt(make_policy(stage, attempt_id="a1"), provider, FakeAuthority())
    a2 = manager.open_attempt(make_policy(stage, attempt_id="a2"), provider, FakeAuthority())
    assert a1.arena_id != a2.arena_id
    assert a1.capability.approved_root != a2.capability.approved_root
    manager.close()


def test_a01_registration_precedes_first_write(tmp_path: Path, monkeypatch) -> None:
    manager, arena = _open_attempt(tmp_path)
    events: list[str] = []

    real_register = arena._ledger.register_resource

    def spy_register(**kwargs):  # type: ignore[no-untyped-def]
        events.append("register")
        return real_register(**kwargs)

    import decoy_engine.scratch._arena as arena_mod

    real_open = arena_mod.os.open

    def spy_open(path, flags, mode=0o777):  # type: ignore[no-untyped-def]
        events.append("fs_create")
        return real_open(path, flags, mode)

    monkeypatch.setattr(arena._ledger, "register_resource", spy_register)
    monkeypatch.setattr(arena_mod.os, "open", spy_open)
    arena.allocate_file(ResourceKind.FILE, "opaque-1")
    assert events[0] == "register", f"registration must precede fs create, got {events}"
    assert "fs_create" in events
    manager.close()


def test_a01_opaque_name_is_never_a_path_component(tmp_path: Path) -> None:
    """A table/tenant-looking opaque_name cannot become traversal: the physical
    filename is a generated id, so the file stays directly under the arena."""
    manager, arena = _open_attempt(tmp_path)
    rf = arena.allocate_file(ResourceKind.FILE, "../../etc/passwd")
    from decoy_engine.scratch import unwrap_path

    path = unwrap_path(rf)
    assert (
        path.parent == arena.capability.approved_root.resolve()
        or path.parent == arena.capability.approved_root
    )
    manager.close()


def test_a01_provider_rejects_traversal_arena_id(tmp_path: Path) -> None:
    provider = KernelProjectQuotaProvider()
    with pytest.raises(ScratchPathUnsafe):
        provider.create_arena(tmp_path, "../escape")


def test_a01_provider_rejects_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    provider = KernelProjectQuotaProvider()
    with pytest.raises(ScratchPathUnsafe):
        provider.create_arena(link, "arena1")


# --- A03 accounting: one attempt budget, no per-connection multiplication ---


def test_a03_resources_share_one_attempt_quota_domain(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path, quota_bytes=32 << 20, quota_inodes=1000)
    arena.allocate_file(ResourceKind.FILE, "f1")
    arena.allocate_file(ResourceKind.FILE, "f2")
    arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "duckdb-1")
    arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "duckdb-2")
    resources = manager.ledger.list_resources(arena.arena_id)
    domains = {r.quota_domain_id for r in resources}
    assert len(domains) == 1, f"per-connection quota multiplication: {domains}"
    arena_row = manager.ledger.get_arena(arena.arena_id)
    assert arena_row is not None
    # The attempt holds a single cap, not one per connection/writer.
    assert arena_row.quota_bytes == 32 << 20
    manager.close()


# --- A13 fail-closed: no permissive fallback --------------------------------


def test_a13_real_provider_fails_closed_without_project_quota(tmp_path: Path) -> None:
    """The real provider on a dev tmpdir (no prjquota) does not qualify, so
    open_attempt refuses before any byte is written (no silent /tmp fallback)."""
    stage = tmp_path / "stage"
    stage.mkdir()
    real_provider = KernelProjectQuotaProvider()
    manager, _ = _open_pool(tmp_path)
    qual = real_provider.qualify(stage)
    assert not qual.qualified
    with pytest.raises(ScratchBackendUnavailable):
        manager.open_attempt(make_policy(stage), real_provider, FakeAuthority())
    manager.close()


def test_a13_platform_lease_authority_disabled_fails_closed(tmp_path: Path) -> None:
    authority = PlatformLeaseAuthority(FakePlatformClient(), enabled=False)
    with pytest.raises(LeaseAuthorityDisabledError):
        authority.lease_for("job", "attempt", str(tmp_path))


def test_a13_authority_outage_fails_closed_before_any_arena(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    manager, provider = _open_pool(tmp_path)
    before = len(manager.ledger.list_arenas())
    with pytest.raises(AuthorityUnavailable):
        manager.open_attempt(make_policy(stage), provider, FakeAuthority(outage=True))
    # No data arena was registered when the authority was unavailable.
    assert len(manager.ledger.list_arenas()) == before
    manager.close()


def test_a13_control_version_mismatch_refuses(tmp_path: Path) -> None:
    control = tmp_path / "ctl"
    control.mkdir()
    ledger = ResourceLedger.bootstrap(control, "pool-x")
    ledger.close()
    # Corrupt the stored format version.
    conn = sqlite3.connect(str(control / "ledger.db"))
    conn.execute("UPDATE ledger_meta SET value = '999' WHERE key = 'format_version'")
    conn.commit()
    conn.close()
    with pytest.raises(ScratchControlUnavailable):
        ResourceLedger.bootstrap(control, "pool-x")


def test_a13_control_split_device_refuses(tmp_path: Path) -> None:
    """The control bootstrap verifies its sidecars share one device (plan 1.5).
    Skip where /dev/shm is the same device as tmp_path (nothing to prove)."""
    shm = Path("/dev/shm")
    if not shm.exists() or os.stat(shm).st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("no distinct second device available to simulate a split")
    from decoy_engine.scratch._ledger import _verify_single_device

    with pytest.raises(ScratchControlUnavailable):
        _verify_single_device((tmp_path / "ledger.db", shm / "ledger.lock"))


# --- Ledger CAS + authority-fault determinism -------------------------------


def test_transition_wrong_generation_raises(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    rf = arena.allocate_file(ResourceKind.FILE, "f1")
    with pytest.raises(ScratchOwnershipError):
        manager.ledger.transition(
            rf.resource_id, frm=ResourceState.ACTIVE, to=ResourceState.REAPED, generation=999999
        )
    manager.close()


def test_transition_wrong_prior_state_raises(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    rf = arena.allocate_file(ResourceKind.FILE, "f1")
    row = manager.ledger.get_resource(rf.resource_id)
    assert row is not None
    with pytest.raises(ScratchOwnershipError):
        manager.ledger.transition(
            rf.resource_id,
            frm=ResourceState.CREATING,
            to=ResourceState.REAPED,
            generation=row.generation,
        )
    manager.close()


def test_platform_lease_authority_claim_failure_is_outage(tmp_path: Path) -> None:
    authority = PlatformLeaseAuthority(FakePlatformClient(fail=True), enabled=True)
    with pytest.raises(AuthorityUnavailable):
        authority.lease_for("job", "attempt", str(tmp_path))


def test_platform_lease_authority_enabled_returns_identity(tmp_path: Path) -> None:
    authority = PlatformLeaseAuthority(FakePlatformClient(), enabled=True)
    lease = authority.lease_for("job", "attempt", str(tmp_path))
    assert lease.mode is AuthorityMode.PLATFORM_LEASE
    assert lease.generation == 3
    assert authority.reap_verdict(lease).reap_eligible is True


def test_target_lock_authority_identity_and_verdict(tmp_path: Path) -> None:
    authority = TargetLockAuthority(FakeTargetLockClient())
    lease = authority.lease_for("job", "attempt", str(tmp_path))
    assert lease.mode is AuthorityMode.TARGET_LOCK
    assert authority.verify(lease) is True
    assert authority.reap_verdict(lease).reap_eligible is False


# --- Sweep: Slice-1 safe retain-by-default ----------------------------------


def test_sweep_retains_when_not_reap_eligible(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    arena.allocate_file(ResourceKind.FILE, "f1")
    report = manager.sweep("startup", FakeAuthority(reap_eligible=False))
    assert report.reaped == ()
    assert report.retained, "a non-reap-eligible resource must be retained"
    manager.close()


def test_sweep_retains_on_authority_outage(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    arena.allocate_file(ResourceKind.FILE, "f1")
    report = manager.sweep("startup", FakeAuthority(outage=True))
    assert report.reaped == ()
    reasons = {r.reason for r in report.retained}
    assert any("authority unavailable" in r for r in reasons)
    manager.close()


def test_sweep_retains_valid_checkpoint_even_if_reap_eligible(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    rf = arena.allocate_file(ResourceKind.FILE, "f1")
    arena.checkpoint(rf, recovery_descriptor=None, validator_or_manifest=None)
    # Even with the authority saying reap-eligible, a CHECKPOINT_VALID resource
    # is never swept on lease expiry (plan 1.8 step 2).
    report = manager.sweep("startup", FakeAuthority(reap_eligible=True))
    assert report.reaped == ()
    assert any("never swept" in r.reason for r in report.retained)
    manager.close()


# --- Cleanup accounting ------------------------------------------------------


def test_release_deletes_and_reports(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    nd = arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "duckdb")
    report = arena.release(nd)
    assert report.disposition is Disposition.DELETED
    row = manager.ledger.get_resource(nd.resource_id)
    assert row is not None and row.state is ResourceState.REAPED
    manager.close()


def test_release_retains_on_failed_reclaim(tmp_path: Path) -> None:
    class StubbornProvider(QualifiedTmpfsProvider):
        def reclaim(self, arena: Path) -> tuple[bool, int, int]:
            return (False, 4096, 1)

    stage = tmp_path / "stage"
    stage.mkdir()
    provider = StubbornProvider()
    control = tmp_path / ".decoy-control"
    manager = ScratchManager.open_pool(pool_id="p", control_arena=control, provider=provider)
    arena = manager.open_attempt(make_policy(stage), provider, FakeAuthority())
    nd = arena.allocate_native_dir(ResourceKind.NATIVE_DIR, "duckdb")
    with pytest.raises(ScratchCleanupError) as excinfo:
        arena.release(nd)
    assert excinfo.value.report.disposition is Disposition.RETAINED
    assert excinfo.value.report.retained_bytes == 4096
    row = manager.ledger.get_resource(nd.resource_id)
    assert row is not None and row.state is ResourceState.FAILED
    manager.close()


# --- Checkpoint API surface --------------------------------------------------


def test_checkpoint_transitions_to_valid(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    rf = arena.allocate_file(ResourceKind.FILE, "f1")
    ckpt = arena.checkpoint(rf, recovery_descriptor=None, validator_or_manifest=None)
    row = manager.ledger.get_resource(ckpt.resource_id)
    assert row is not None and row.state is ResourceState.CHECKPOINT_VALID
    assert row.retention.value == "resumable"
    manager.close()


# --- Control bootstrap -------------------------------------------------------


def test_control_arena_registered_with_distinct_quota_domain(tmp_path: Path) -> None:
    manager, arena = _open_attempt(tmp_path)
    arenas = manager.ledger.list_arenas()
    control = [a for a in arenas if a.is_control]
    data = [a for a in arenas if not a.is_control]
    assert len(control) == 1, "control arena must be registered (plan 1.3)"
    assert data, "data arena must be registered"
    assert control[0].quota_domain_id != data[0].quota_domain_id
    assert control[0].quota_domain_id.endswith("::control")
    manager.close()


def test_can_record_terminal_failure_in_control_domain(tmp_path: Path) -> None:
    """The control DB lives in its own quota domain, so recording a terminal
    disposition for a data resource does not depend on the data arena's cap
    (plan 1.5 control-arena sizing). The near-cap kernel proof is A02 on GCP."""
    manager, arena = _open_attempt(tmp_path)
    rf = arena.allocate_file(ResourceKind.FILE, "f1")
    manager.ledger.set_disposition(
        rf.resource_id, disposition="scratch_quota_exceeded", state=ResourceState.FAILED
    )
    row = manager.ledger.get_resource(rf.resource_id)
    assert row is not None and row.disposition == "scratch_quota_exceeded"
    manager.close()
