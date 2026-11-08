"""The local protected-quarantine store follows A-4: its root is a caller path, so it is used
only under the ambient file-access policy and refuses a strict one before any I/O."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_the_local_store_refuses_a_strict_policy_before_any_io(tmp_path: Path) -> None:
    """The local store acts on the root its constructor was given, a caller path, so like every
    caller-path route it is ambient-only: under a strict policy it reports itself unavailable
    (the run refuses before reading a source) and touches nothing."""
    from decoy_engine import fileaccess
    from decoy_engine.declared_casting import LocalProtectedQuarantineSink
    from decoy_engine.keyprovider import SecretKeyProvider
    from tests.fileaccess.support import RootRelative

    key = SecretKeyProvider(secret=b"q" * 32)
    assert LocalProtectedQuarantineSink(tmp_path / "ambient", key_provider=key).capability.qualified
    root = tmp_path / "q"
    sink = LocalProtectedQuarantineSink(root, key_provider=key)
    policy = RootRelative(tmp_path)
    try:
        with fileaccess.scope(policy):
            capability = sink.capability
    finally:
        policy.close()
    assert not capability.qualified
    assert capability.detail == "arena_needs_ambient_policy"
    assert not root.exists()


def test_a_strict_policy_run_with_the_local_store_refuses_before_reading(tmp_path: Path) -> None:
    from decoy_engine.declared_casting import (
        LocalProtectedQuarantineSink,
        ProtectedQuarantineRuntime,
    )
    from decoy_engine.keyprovider import SecretKeyProvider
    from tests.acceptance._f06_support import text_table
    from tests.fileaccess.support import RootRelative
    from tests.security.test_f06_quarantine_boundary import _cfg, run

    job = tmp_path / "job"
    job.mkdir()
    root = tmp_path / "q"
    sink = LocalProtectedQuarantineSink(root, key_provider=SecretKeyProvider(secret=b"q" * 32))
    runtime = ProtectedQuarantineRuntime(sink=sink, owner="tenant-1", run_id="run-A")
    policy = RootRelative(job)
    try:
        with pytest.raises(Exception) as info:
            run(
                _cfg(job),
                {"people": text_table(id=["0", "1"], start=["2026-02-01", "bad"])},
                quarantine_runtime=runtime,
                file_access=policy,
            )
    finally:
        policy.close()
    assert str(getattr(info.value, "code", "")) == "protected_quarantine_unavailable"
    assert not root.exists()
