"""F22 5.3: `publish_unit` judges and binds under the destination lock (slice a3).

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 rows `test_retry_already_committed`,
`test_refused_overwrite_survivor`, `test_exit_matrix_publish` and the a3 part of
`test_marker_corpus_user_surfaces`. The two-writer rows are in `test_publish_two_writers.py`,
`test_adopt_syncs_ancestor_chain` in `test_publish_crash_chain.py`.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.fileaccess.publication._chain_support import Recorder, anchor_at, key
from tests.unit.publication._exits import EXITS, binding_after, run_exit
from tests.unit.publication._publish_support import (
    DESCRIPTOR,
    OSMARK,
    POLICIES,
    close_root,
    descriptor,
    make_out,
    publish_child,
    run_spec,
    snapshot,
    stages_left,
    tables,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX publication")

HEX64 = re.compile(r"[0-9a-f]{64}")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.mark.parametrize("policy", POLICIES)
def test_retry_already_committed(
    base: Path, tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.publication import PublicationRequest, publish_tables

    parent = base / "a"
    parent.mkdir()
    leaf = parent / "out"
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = make_out(policy, base, "a/out")
    try:
        first = publish_tables(out, tables("one"), PublicationRequest("op-1"))
        assert (first.outcome, first.visible, first.durable) == ("published", True, True)
        before = snapshot(leaf)

        rec = Recorder(monkeypatch)
        again = publish_tables(out, tables("one"), PublicationRequest("op-1"))
        assert (again.outcome, again.reason_code) == ("already_committed", None)
        assert (again.visible, again.durable, again.os_error) == (True, True, None)
        assert (again.result_id, again.descriptor_digest) == (
            first.result_id,
            first.descriptor_digest,
        )
        assert again.artifacts == first.artifacts
        # D2 before the receipt: the unit tree (files, then the leaf), then the chain to the anchor.
        files = {key(leaf / "customers.parquet"), key(leaf / DESCRIPTOR)}
        synced = rec.synced()
        assert set(synced[:2]) == files  # non-vacuity: the spy saw the unit files
        assert synced[2:] == [key(leaf), key(parent), key(base)]
        assert snapshot(leaf) == before and stages_left(parent) == []

        # a unit of 0400 files and 0500 directories is still fsynced (O_RDONLY descriptors)
        for path in (leaf / "customers.parquet", leaf / DESCRIPTOR):
            os.chmod(path, 0o400)
        os.chmod(leaf, 0o500)
        try:
            read_only = publish_tables(out, tables("one"), PublicationRequest("op-1"))
        finally:
            os.chmod(leaf, 0o700)
        assert (read_only.outcome, read_only.durable) == ("already_committed", True)

        # a fresh process with the same operation and tables
        code, result = run_spec(
            publish_child,
            {"base": str(base), "policy": policy, "rel": "a/out", "seed": "one", "op": "op-1"},
            tmp_path,
        )
        assert code == 0 and result == {"outcome": "already_committed", "durable": True}
        assert snapshot(leaf) == before
    finally:
        close_root(root_fd)


@pytest.mark.parametrize("prior", ["one_table", "zero_tables"])
@pytest.mark.parametrize("policy", POLICIES)
def test_refused_overwrite_survivor(base: Path, policy: str, prior: str) -> None:
    from decoy_engine.publication import PublicationRequest, publish_tables

    parent = base / "a"
    parent.mkdir()
    leaf = parent / "out"
    out, root_fd = make_out(policy, base, "a/out")
    try:
        bound = tables("one") if prior == "one_table" else {}
        assert publish_tables(out, bound, PublicationRequest("op-1")).outcome == "published"
        before = snapshot(leaf)
        bound_result = descriptor(leaf)["result_id"]
        if prior == "zero_tables":  # the unit holds only its descriptor (probe P4's empty unit)
            assert descriptor(leaf)["artifacts"] == [] and sorted(os.listdir(leaf)) == [DESCRIPTOR]
        other = tables("two")
        for op, outcome, code in (
            ("op-1", "operation_conflict", "publication_operation_conflict"),
            ("op-2", "refused", "publication_target_occupied"),
        ):
            receipt = publish_tables(out, other, PublicationRequest(op))
            assert (receipt.outcome, receipt.reason_code) == (outcome, code)
            assert (receipt.visible, receipt.durable) == (False, False)
            assert receipt.result_id != bound_result
            assert snapshot(leaf) == before  # tree, bytes, inodes and descriptor digest
            assert stages_left(parent) == []
    finally:
        close_root(root_fd)


@pytest.mark.parametrize("case", sorted(EXITS))
@pytest.mark.parametrize("policy", POLICIES)
def test_exit_matrix_publish(
    base: Path, policy: str, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, code, binding = EXITS[case]
    run = run_exit(case, policy, base, monkeypatch)
    receipt = run.receipt
    assert (receipt.outcome, receipt.reason_code) == (outcome, code)
    assert binding_after(run) == binding
    assert stages_left(run.leaf.parent) == []  # every non-bind exit aborted the stage
    assert receipt.visible is (outcome in {"published", "already_committed", "commit_uncertain"})
    assert receipt.durable is (outcome in {"published", "already_committed"})
    if case in {"E4a_chain_fsync_fails", "E11_stage_fsync_fails", "E12_rename_fails"} or (
        case == "E13_chain_fsync_after_rename_fails"
    ):
        assert receipt.os_error == "EIO"
    if case == "E1_lock_unavailable":
        assert receipt.os_error == "ENOLCK"
    if case == "E10_descriptor_write_fails":
        assert receipt.os_error == errno.errorcode[errno.EEXIST]
    if binding == "bound":
        assert descriptor(run.leaf)["result_id"] == receipt.result_id


# -- G5: receipts, error text and reason records carry no value, path or OS text ---------------

PATHMARK, TBLMARK = "PATHMARK", "TBLMARK"
MARKERS = (PATHMARK, TBLMARK, OSMARK)


def _check_receipt_fields(receipt: Any) -> None:
    from decoy_engine import reasons
    from decoy_engine.publication import ArtifactSummary
    from decoy_engine.publication._types import is_operation_id

    for name, value in vars(receipt).items():
        if name == "artifacts":
            assert all(type(a) is ArtifactSummary for a in value)
            for a in value:
                assert a.role == "table" and type(a.size) is int and type(a.rows) is int
                assert HEX64.fullmatch(a.sha256)
        elif name == "operation_id":
            assert is_operation_id(value)
        elif name == "reason_code":
            assert value is None or reasons.is_registered(value, ("decoy",))
        elif name in {"result_id", "descriptor_digest", "vault_digest"}:
            assert value is None or HEX64.fullmatch(value)
        elif name == "os_error":
            assert value is None or value in errno.errorcode.values()
        elif name == "outcome":
            assert value in {
                "published",
                "already_committed",
                "operation_conflict",
                "refused",
                "failed",
                "commit_uncertain",
            }
        else:
            assert type(value) is bool, name


@pytest.mark.parametrize("policy", POLICIES)
def test_marker_corpus_user_surfaces(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.publication import raise_for_outcome
    from decoy_engine.reason_adapter import reason_from_exception
    from decoy_engine.reasons import Phase

    seen_tables = False
    for n, case in enumerate(sorted(EXITS)):
        base = tmp_path / f"{n}-{PATHMARK}"
        base.mkdir()
        with monkeypatch.context() as patch:
            run = run_exit(
                case, policy, base, patch, leaf_name=PATHMARK, table=f"{TBLMARK}.parquet"
            )
        assert PATHMARK in str(run.leaf)  # non-vacuity: the destination carries the marker
        receipt = run.receipt
        _check_receipt_fields(receipt)
        surfaces = [repr(receipt), str(receipt)]
        try:
            raise_for_outcome(receipt)
        except Exception as exc:  # the typed error a caller raises for this outcome
            surfaces.append(str(exc))
            surfaces.append(repr(exc.args))
            record = reason_from_exception(exc, phase=Phase.PUBLISH)
            surfaces.append(json.dumps(record.to_payload()))
            assert record.namespace == "decoy" and record.code == receipt.reason_code
        else:
            assert receipt.outcome in {"published", "already_committed"}
        for text in surfaces:
            for marker in MARKERS:
                assert marker not in text, (case, marker)
        if receipt.outcome == "published":
            paths = [a["path"] for a in descriptor(run.leaf)["artifacts"]]
            seen_tables = seen_tables or any(TBLMARK in p for p in paths)
    assert seen_tables  # non-vacuity: the table marker is in the descriptor's artifact paths
