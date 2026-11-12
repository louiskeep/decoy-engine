"""F22 5.0 D2: a retry fsyncs the unit tree and every directory to the anchor before it reports
`already_committed` (slice a3).

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 row `test_adopt_syncs_ancestor_chain`.
A `spawn` child publishes into `a/b/out` with `a` and `b` absent and SIGKILLs itself after the
bind rename, just before each chain fsync in turn (the kill marker is the non-vacuity line). A
retry in this process must fsync the unit tree, `b`, `a` and the anchor, in that order, before
its receipt; EIO on any of them gives `commit_uncertain`, `durable=False`.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest

from tests.fileaccess.publication._chain_support import Recorder, anchor_at, key
from tests.unit.publication._publish_support import (
    DESCRIPTOR,
    KILL_MARKER,
    POLICIES,
    close_root,
    crash_child,
    make_out,
    run_spec,
    tables,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable publish")

CHAIN = ["b", "a", "anchor"]


@pytest.mark.parametrize("kill_before", CHAIN)
@pytest.mark.parametrize("policy", POLICIES)
def test_adopt_syncs_ancestor_chain(
    tmp_path: Path, policy: str, kill_before: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.publication import PublicationRequest, publish_tables

    base = tmp_path / "root"
    base.mkdir()
    spec = {
        "base": str(base),
        "policy": policy,
        "rel": "a/b/out",
        "seed": "one",
        "op": "op-1",
        "kill_at": CHAIN.index(kill_before) + 1,
    }
    code, _ = run_spec(crash_child, spec, tmp_path)
    assert code == -signal.SIGKILL
    assert (tmp_path / KILL_MARKER).exists()  # non-vacuity: killed after the rename
    a, b = base / "a", base / "a" / "b"
    leaf = b / "out"
    assert (leaf / DESCRIPTOR).is_file()  # the rename completed: the unit is bound

    if policy == "ambient":
        anchor_at(monkeypatch, base)
    chain = {"b": key(b), "a": key(a), "anchor": key(base)}
    unit = {key(leaf / "customers.parquet"), key(leaf / DESCRIPTOR)}
    out, root_fd = make_out(policy, base, "a/b/out")
    try:
        with monkeypatch.context() as patch:
            rec = Recorder(patch)
            receipt = publish_tables(out, tables("one"), PublicationRequest("op-1"))
        assert (receipt.outcome, receipt.durable) == ("already_committed", True)
        synced = rec.synced()
        assert set(synced[:2]) == unit
        assert synced[2:] == [key(leaf), chain["b"], chain["a"], chain["anchor"]]

        for name, failing in chain.items():
            with monkeypatch.context() as patch:
                Recorder(patch, fail=lambda e, k=failing: e == ("fsync", k))
                uncertain = publish_tables(out, tables("one"), PublicationRequest("op-1"))
            assert (uncertain.outcome, uncertain.reason_code) == (
                "commit_uncertain",
                "publication_commit_uncertain",
            ), name
            assert (uncertain.visible, uncertain.durable, uncertain.os_error) == (
                True,
                False,
                "EIO",
            )
    finally:
        close_root(root_fd)
