"""The `publish_unit` exit paths of plan 4.1, each set up and injected in-process (slice a3).

`run_exit` builds one exit, runs one publish and returns what the tests assert on. E7 here is
the presence half that a3 can reach (a stored vault digest, a request without a vault); its
digest and row halves, E8 and E9 arrive with vault ordering (b1, b2). E15 (SIGKILL) is
`test_publish_crash_chain.py`.
"""

from __future__ import annotations

import errno
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.fileaccess.publication._chain_support import Recorder, key
from tests.unit.publication._publish_support import (
    DESCRIPTOR,
    OSMARK,
    make_out,
    snapshot,
    tables,
)

# exit id -> (outcome, reason code, binding afterwards)
EXITS: dict[str, tuple[str, str | None, str]] = {
    "E1_lock_unavailable": ("refused", "publication_lock_unavailable", "absent"),
    "E2_lock_timeout": ("refused", "publication_lock_timeout", "absent"),
    "E3_invalid_descriptor": ("refused", "publication_target_occupied", "untouched"),
    "E3_oversize_descriptor": ("refused", "publication_target_occupied", "untouched"),
    "E3_unreadable_descriptor": ("refused", "publication_target_occupied", "untouched"),
    "E3_nonempty_without_descriptor": ("refused", "publication_target_occupied", "untouched"),
    "E3_leaf_is_a_file": ("refused", "publication_target_occupied", "untouched"),
    "E4_already_committed": ("already_committed", None, "untouched"),
    "E4a_chain_fsync_fails": ("commit_uncertain", "publication_commit_uncertain", "untouched"),
    "E5_operation_conflict": ("operation_conflict", "publication_operation_conflict", "untouched"),
    "E6_different_operation": ("refused", "publication_target_occupied", "untouched"),
    "E7_vault_presence": ("refused", "publication_vault_mismatch", "untouched"),
    "E10_descriptor_write_fails": ("failed", "publication_failed", "absent"),
    "E10_staged_table_not_parquet": ("failed", "publication_failed", "absent"),
    "E11_stage_fsync_fails": ("failed", "publication_failed", "absent"),
    "E12_rename_fails": ("failed", "publication_failed", "absent"),
    "E13_chain_fsync_after_rename_fails": (
        "commit_uncertain",
        "publication_commit_uncertain",
        "bound",
    ),
    "E14_published": ("published", None, "bound"),
}


@dataclass
class ExitRun:
    receipt: Any
    leaf: Path
    before: Any  # what was at the leaf before the publish: a snapshot, bytes, or None


def _prior(out: Any, data: dict[str, Any], op: str = "op-1") -> None:
    from decoy_engine.publication import PublicationRequest, publish_tables

    assert publish_tables(out, data, PublicationRequest(op)).outcome == "published"


def run_exit(
    case: str,
    policy: str,
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leaf_name: str = "out",
    table: str = "customers.parquet",
) -> ExitRun:
    from decoy_engine.publication import PublicationRequest, publish_tables, publish_unit

    parent = base / "a"
    parent.mkdir(parents=True, exist_ok=True)
    leaf = parent / leaf_name
    out, root_fd = make_out(policy, base, f"a/{leaf_name}")
    one, two = tables("one", name=table), tables("two", name=table)
    request = PublicationRequest("op-1")
    before: Any = None
    try:
        if case in {
            "E4_already_committed",
            "E4a_chain_fsync_fails",
            "E5_operation_conflict",
            "E6_different_operation",
        }:
            _prior(out, one)
            before = snapshot(leaf)
        if case == "E1_lock_unavailable":

            def no_locks(fd: int, op: int) -> None:
                raise OSError(errno.ENOLCK, f"no locks {OSMARK}")

            monkeypatch.setattr(fcntl, "flock", no_locks)
        elif case == "E2_lock_timeout":
            holder = os.open(parent / f".{leaf_name}.decoy-lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(holder, fcntl.LOCK_EX)
            request = PublicationRequest("op-1", lock_timeout_s=0.2)
        elif case.startswith("E3_"):
            if case == "E3_leaf_is_a_file":
                leaf.write_bytes(b"a plain file")
            else:
                leaf.mkdir()
                if case == "E3_invalid_descriptor":
                    (leaf / DESCRIPTOR).write_bytes(b"{}")
                elif case == "E3_oversize_descriptor":
                    (leaf / DESCRIPTOR).write_bytes(b" " * ((1 << 20) + 1))
                elif case == "E3_unreadable_descriptor":
                    (leaf / DESCRIPTOR).mkdir()
                else:
                    (leaf / "x.parquet").write_bytes(b"left by someone")
            before = leaf.read_bytes() if leaf.is_file() else _tree(leaf)
        elif case == "E4a_chain_fsync_fails":
            parent_key = key(parent)
            Recorder(monkeypatch, fail=lambda e: e == ("fsync", parent_key))
        elif case == "E5_operation_conflict":
            one = two
        elif case == "E6_different_operation":
            one, request = two, PublicationRequest("op-2")
        elif case == "E7_vault_presence":
            _prior(out, one)
            _store_vault_digest(leaf)
            before = snapshot(leaf)
        elif case == "E11_stage_fsync_fails":
            Recorder(monkeypatch, fail=lambda e: e[0] == "fsync")
        elif case == "E12_rename_fails":
            Recorder(monkeypatch, fail=lambda e: e[0] == "rename")
        elif case == "E13_chain_fsync_after_rename_fails":
            parent_key = key(parent)
            Recorder(monkeypatch, fail=lambda e: e == ("fsync", parent_key))
        if case == "E10_descriptor_write_fails":  # a producer planted the descriptor's name
            from decoy_engine.fileaccess.adapters import arrow as arrow_adapter

            txn = out.begin("dir", "exclusive", label="publication")
            arrow_adapter.write_table(one[table], txn, table)
            os.close(txn.create_file(DESCRIPTOR))
            receipt = publish_unit(out, txn, request, tables=[table])
        elif case == "E10_staged_table_not_parquet":
            txn = out.begin("dir", "exclusive", label="publication")
            fd = txn.create_file(table)
            os.write(fd, f"not a table {OSMARK}".encode())
            os.close(fd)
            receipt = publish_unit(out, txn, request, tables=[table])
        else:
            receipt = publish_tables(out, one, request)
        if case == "E2_lock_timeout":
            os.close(holder)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    return ExitRun(receipt, leaf, before)


def _tree(leaf: Path) -> list[tuple[str, int, bytes | None]]:
    return [
        (str(p.relative_to(leaf)), os.lstat(p).st_ino, p.read_bytes() if p.is_file() else None)
        for p in sorted([leaf, *leaf.rglob("*")])
    ]


def _store_vault_digest(leaf: Path) -> None:
    """Rewrite the bound descriptor as one bound with a vault (same artifacts, same result)."""
    from decoy_engine.publication import (
        Artifact,
        DescriptorVault,
        build_descriptor,
        decode_descriptor,
        encode_descriptor,
    )

    stored = decode_descriptor((leaf / DESCRIPTOR).read_bytes())
    vaulted = build_descriptor(
        operation_id=stored.operation_id,
        artifacts=[Artifact(**vars(a)) for a in stored.artifacts],
        vault=DescriptorVault(sha256="5" * 64, size=10, entry_count=1),
    )
    path = leaf / DESCRIPTOR
    os.chmod(path, 0o600)
    path.write_bytes(encode_descriptor(vaulted))


def binding_after(run: ExitRun) -> str:
    leaf = run.leaf
    if not leaf.exists():
        return "absent"
    if run.before is not None:
        now: Any
        if isinstance(run.before, bytes):
            now = leaf.read_bytes()
        elif isinstance(run.before, dict):
            now = snapshot(leaf)
        else:
            now = _tree(leaf)
        return "untouched" if now == run.before else "changed"
    if (leaf / DESCRIPTOR).is_file():
        return "bound"
    return "other"
