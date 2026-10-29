"""F22 acceptance tests A2-A6, A9, A12 for `managed_dataset/v1` (replacement + protected backup)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution._transactional_sink import ManagedDatasetSink
from decoy_engine.publication import _managed
from tests.unit.publication import _support as sup


def _req(root: Path, op: str, overwrite: object = None, **kw: object) -> pub.PublicationRequest:
    return pub.PublicationRequest(
        destination=root,
        operation_id=op,
        unit=pub.UNIT_MANAGED_DATASET,
        overwrite=overwrite or pub.ErrorIfDifferent(),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def put(
    root: Path, op: str, ids: list[int], overwrite: object = None, **kw: object
) -> pub.CommitReceipt:
    sink = ManagedDatasetSink(root, request=_req(root, op, overwrite, **kw))
    sink.write("alpha", pa.table({"id": ids}))
    sink.write("beta", sup.table_b())
    try:
        return sink.commit()
    except pub.CommitError:
        if not isinstance(sys.exc_info()[1], pub.CommitUncertainError):
            sink.abort()  # the caller's job on any refusal: retained staging is not a result
        raise


def current_ids(root: Path) -> list[int]:
    pin = pub.resolve_current(root)
    assert pin is not None
    return pq.read_table(pin.path / "alpha.parquet").column("id").to_pylist()


def snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return sup.tree_digest(root)


def generations(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "generations").iterdir())


# --------------------------------------------------------------------------- publish / identity


def test_first_publish_binds_a_generation_through_current(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    receipt = put(root, "op-A", [1, 2])
    assert receipt.outcome == "published" and receipt.durable and receipt.generation
    pin = pub.resolve_current(root)
    assert pin is not None and pin.descriptor_digest == receipt.descriptor_digest
    sup.oracle(pin.path)
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert not (root / ".staging").exists() or not list((root / ".staging").iterdir())


def test_a2_retry_and_identical_content_are_reported_not_reapplied(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    first = put(root, "op-A", [1, 2])
    before = snapshot(root)
    same_op = put(root, "op-A", [1, 2])
    other_op = put(root, "op-B", [1, 2])
    for again in (same_op, other_op):
        assert again.outcome == "already_committed" and again.is_current
        assert again.descriptor_digest == first.descriptor_digest
    assert snapshot(root) == before
    recovered = ManagedDatasetSink(
        root, request=_req(root, "op-A", expected_result_id=first.result_id)
    )
    assert recovered.commit().descriptor_digest == first.descriptor_digest


def test_a3_default_policy_never_replaces_and_replace_is_cas_checked(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    first = put(root, "op-A", [1, 2])
    before = snapshot(root)
    for request_kwargs, outcome, reason in [
        (dict(op="op-B", ids=[9]), "conflict", "target_occupied_different"),
        (dict(op="op-A", ids=[9]), "conflict", "operation_conflict"),
        (
            dict(op="op-C", ids=[9], overwrite=pub.Replace("0" * 64)),
            "conflict",
            "stale_predecessor",
        ),
    ]:
        with pytest.raises(pub.PublicationRefusedError) as exc:
            put(root, request_kwargs["op"], request_kwargs["ids"], request_kwargs.get("overwrite"))  # type: ignore[arg-type]
        assert (exc.value.receipt.outcome, exc.value.receipt.reason_code) == (outcome, reason)
        assert snapshot(root) == before
    assert pub.resolve_current(root).descriptor_digest == first.descriptor_digest  # type: ignore[union-attr]


def test_replace_without_any_predecessor_conflicts(tmp_path: Path) -> None:
    with pytest.raises(pub.PublicationRefusedError) as exc:
        put(tmp_path / "ds", "op-A", [1], pub.Replace("a" * 64))
    assert exc.value.receipt.reason_code == "predecessor_missing"


# --------------------------------------------------------------------------- A4 / A5 replacement


def test_replacement_switches_current_keeps_a_intact_and_pins_readers(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    pinned = pub.resolve_current(root)  # a reader resolves CURRENT once
    a_tree = sup.tree_digest(pinned.path)  # type: ignore[union-attr]
    b = put(root, "op-B", [7, 8, 9], pub.Replace(a.descriptor_digest))
    assert b.outcome == "published"
    assert current_ids(root) == [7, 8, 9]
    assert pq.read_table(pinned.path / "alpha.parquet").column("id").to_pylist() == [1, 2]  # type: ignore[union-attr]
    assert (
        sup.tree_digest(pinned.path) == a_tree
    )  # prior bytes untouched  # type: ignore[union-attr]
    assert (
        json.loads(
            (root / "generations" / b.descriptor_digest / "_decoy_publication.json").read_text()
        )["lineage"]["predecessor_digest"]
        == a.descriptor_digest
    )
    assert len(generations(root)) == 2


def _boom(*_a: object, **_k: object) -> None:
    raise OSError(5, "injected failure")


@pytest.mark.parametrize(
    "boundary", ["write", "checks", "cancel", "generation_rename", "pointer_switch"]
)
def test_a4_failure_before_the_switch_leaves_a_current_and_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    before = snapshot(root)
    request = _req(root, "op-B", pub.Replace(a.descriptor_digest))
    if boundary == "checks":
        request = _req(
            root, "op-B", pub.Replace(a.descriptor_digest), checks=(pub.CheckResult("c", "fail"),)
        )
    if boundary == "cancel":
        request = _req(root, "op-B", pub.Replace(a.descriptor_digest), cancelled=lambda: True)
    sink = ManagedDatasetSink(root, request=request)
    if boundary == "write":

        def bad():  # type: ignore[no-untyped-def]
            yield pa.record_batch({"id": [5]})
            raise RuntimeError("iterator failed")

        with pytest.raises(RuntimeError):
            sink.write_batches("alpha", bad(), schema=pa.schema([("id", pa.int64())]))
        with pytest.raises(pub.PublicationRefusedError):
            sink.commit()
    else:
        sink.write("alpha", pa.table({"id": [5]}))
        real = os.replace
        if boundary == "generation_rename":
            monkeypatch.setattr(_managed.os, "replace", _boom)
        if boundary == "pointer_switch":

            def only_pointer(src: object, dst: object) -> None:
                if Path(str(dst)).name == "CURRENT":
                    raise OSError(5, "injected pointer switch failure")
                real(src, dst)  # type: ignore[arg-type]

            monkeypatch.setattr(_managed.os, "replace", only_pointer)
        with pytest.raises(pub.PublicationRefusedError) as exc:
            sink.commit()
        assert exc.value.receipt.outcome in ("rejected", "cancelled_before_commit")
    sink.abort()
    monkeypatch.undo()
    assert current_ids(root) == [1, 2]
    assert pub.lookup_committed(_req(root, "op-B")) is None  # nothing releasable for B
    for path, digest in before.items():
        assert snapshot(root).get(path) == digest, path


@pytest.mark.parametrize("fault", ["kill_after_stage", "kill_before_rename", "kill_before_switch"])
def test_a4_killed_replacement_leaves_a_current(
    tmp_path: Path, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2, 3])
    before = snapshot(root)
    monkeypatch.setenv("WORKER_UNIT", "managed")
    monkeypatch.setenv("WORKER_REPLACE", a.descriptor_digest)
    proc = sup.run_worker(str(root), "op-K", "other", fault)
    proc.communicate(timeout=120)
    assert proc.returncode == -9
    if fault == "kill_before_switch":  # the generation was promoted but never referenced
        assert len(generations(root)) == 2, "orphan generation must exist before the kill"
    else:
        assert list((root / ".staging").iterdir()), "staging must exist before the kill"
    for path, digest in before.items():
        assert snapshot(root).get(path) == digest, path
    assert current_ids(root) == [1, 2, 3]
    assert pub.lookup_committed(_req(root, "op-K")) is None
    # An orphaned, unreferenced generation (kill_before_switch) is not a releasable result.
    retry = sup.run_worker(str(root), "op-K", "other", "none")
    out, err = retry.communicate(timeout=120)
    assert retry.returncode == 0, err
    assert json.loads(out)["outcome"] == "published"
    assert current_ids(root) == [1, 2, 99]


def test_a5_kill_right_after_the_real_switch_is_a_complete_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2, 3])
    a_gen = root / "generations" / a.descriptor_digest
    a_tree = sup.tree_digest(a_gen)
    monkeypatch.setenv("WORKER_UNIT", "managed")
    monkeypatch.setenv("WORKER_REPLACE", a.descriptor_digest)
    proc = sup.run_worker(str(root), "op-K", "other", "kill_after_switch")
    proc.communicate(timeout=120)
    assert proc.returncode == -9
    assert current_ids(root) == [1, 2, 99]  # complete B, never a mixture
    assert sup.tree_digest(a_gen) == a_tree  # prior bytes intact
    retry = sup.run_worker(str(root), "op-K", "other", "none")
    out, err = retry.communicate(timeout=120)
    assert retry.returncode == 0, err
    assert json.loads(out)["outcome"] == "already_committed"
    assert len(generations(root)) == 2  # retry cannot duplicate B


def test_a5_post_switch_directory_sync_failure_is_uncertain_then_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2, 3])
    real = _managed.fsync_dir
    calls = {"after_switch": False}
    real_replace = os.replace

    def mark(src: object, dst: object) -> None:
        real_replace(src, dst)  # type: ignore[arg-type]
        if Path(str(dst)).name == "CURRENT":
            calls["after_switch"] = True

    def failing(path: Path) -> None:
        if calls["after_switch"] and Path(path) == root:
            raise OSError(5, "injected root fsync failure")
        real(path)

    monkeypatch.setattr(_managed.os, "replace", mark)
    monkeypatch.setattr(_managed, "fsync_dir", failing)
    sink = ManagedDatasetSink(root, request=_req(root, "op-B", pub.Replace(a.descriptor_digest)))
    sink.write("alpha", pa.table({"id": [4, 5]}))
    with pytest.raises(pub.CommitUncertainError) as exc:
        sink.commit()
    assert exc.value.receipt.outcome == "commit_uncertain" and exc.value.receipt.visible
    assert current_ids(root) == [4, 5]
    sink.abort()
    assert current_ids(root) == [4, 5]  # a possible commit is never deleted
    monkeypatch.undo()
    reconciled = sink.reconcile()
    assert (
        reconciled is not None and reconciled.outcome == "already_committed" and reconciled.durable
    )


# --------------------------------------------------------------------------- A6


def test_a6_retry_of_a_superseded_operation_reports_history_without_restoring(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    put(root, "op-B", [3], pub.Replace(a.descriptor_digest))
    gens = generations(root)
    historical = put(root, "op-A", [1, 2])  # restage identical content under the old operation
    assert historical.outcome == "already_committed" and historical.is_current is False
    assert historical.descriptor_digest == a.descriptor_digest
    assert current_ids(root) == [3]  # never restored
    assert generations(root) == gens
    with pytest.raises(pub.PublicationRefusedError) as exc:
        put(root, "op-A", [42])  # same operation, different content
    assert exc.value.receipt.reason_code == "operation_conflict"


def test_a6_stale_owner_cannot_switch(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1])
    put(root, "op-B", [2], pub.Replace(a.descriptor_digest))
    with pytest.raises(pub.PublicationRefusedError) as exc:
        put(root, "op-C", [3], pub.Replace(a.descriptor_digest))  # still believes A is current
    assert exc.value.receipt.reason_code == "stale_predecessor"
    assert current_ids(root) == [2]


def test_a6_racing_replacements_against_one_predecessor_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, go = tmp_path / "ds", tmp_path / "go"
    a = put(root, "op-A", [1])
    monkeypatch.setenv("WORKER_UNIT", "managed")
    monkeypatch.setenv("WORKER_REPLACE", a.descriptor_digest)
    procs = [
        sup.run_worker(str(root), f"op-{i}", ["same", "other"][i % 2], "none", str(go))
        for i in range(4)
    ]
    go.write_text("go")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        results.append(json.loads(out))
    assert [r["outcome"] for r in results].count("published") == 1
    others = [r for r in results if r["outcome"] != "published"]
    assert all(r["outcome"] in ("conflict", "already_committed") for r in others)
    assert len(generations(root)) == 2
    assert current_ids(root) in ([1, 2, 3], [1, 2, 99])


# --------------------------------------------------------------------------- A9 / A12 backup + restore


def replace_with_backup(
    root: Path, a: pub.CommitReceipt, op: str, ids: list[int]
) -> pub.CommitReceipt:
    return put(root, op, ids, pub.Replace(a.descriptor_digest, pub.ProtectedBackup("pol-1")))


def test_protected_backup_is_a_private_verified_held_copy(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    b = replace_with_backup(root, a, "op-B", [3])
    backup = root / "backups" / a.descriptor_digest
    assert b.backup_status == "succeeded" and b.backup_ref == f"backups/{a.descriptor_digest}"
    assert pub.inspect_destination(backup).kind == "committed"
    assert json.loads((backup / "_decoy_hold.json").read_text()) == {"policy_ref": "pol-1"}
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in backup.iterdir())
    assert not os.path.samefile(
        backup / "alpha.parquet", root / "generations" / a.descriptor_digest / "alpha.parquet"
    )
    assert (
        json.loads(
            (root / "generations" / b.descriptor_digest / "_decoy_publication.json").read_text()
        )["lineage"]["backup_ref"]
        == "pol-1"
    )


def test_a9_every_backup_boundary_failure_prevents_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_root = tmp_path / "probe"
    seen: list[str] = []
    monkeypatch.setattr(_managed, "_backup_step", seen.append)
    a = put(probe_root, "op-A", [1, 2])
    replace_with_backup(probe_root, a, "op-B", [3])
    steps = list(dict.fromkeys(seen))
    assert {"verify", "hold", "publish"} <= set(steps) and any(s.startswith("copy:") for s in steps)
    monkeypatch.undo()

    for index, step in enumerate(steps):
        root = tmp_path / f"ds{index}"
        a = put(root, "op-A", [1, 2])
        before = snapshot(root)

        def fail_at(name: str, step: str = step) -> None:
            if name == step:
                raise OSError(28, f"injected failure at {step}")

        monkeypatch.setattr(_managed, "_backup_step", fail_at)
        with pytest.raises(pub.PublicationRefusedError) as exc:
            replace_with_backup(root, a, "op-B", [3])
        monkeypatch.undo()
        r = exc.value.receipt
        assert (r.outcome, r.reason_code, r.backup_status) == (
            "rejected",
            "backup_failed",
            "failed",
        ), step
        assert current_ids(root) == [1, 2], step
        assert snapshot(root) == before, (
            step
        )  # no partial backup, no new generation, no new current
        assert not (root / "backups").exists() or not list((root / "backups").iterdir())


def test_a9_a_valid_backup_survives_a_failed_publication_and_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    real = os.replace

    def refuse_pointer(src: object, dst: object) -> None:
        if Path(str(dst)).name == "CURRENT":
            raise OSError(5, "injected")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(_managed.os, "replace", refuse_pointer)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        replace_with_backup(root, a, "op-B", [3])
    monkeypatch.undo()
    assert exc.value.receipt.backup_status == "succeeded"
    backup = root / "backups" / a.descriptor_digest
    assert pub.inspect_destination(backup).kind == "committed"  # held backup + old current
    assert current_ids(root) == [1, 2]
    steps: list[str] = []
    monkeypatch.setattr(_managed, "_backup_step", steps.append)
    again = replace_with_backup(root, a, "op-B2", [3])
    assert again.outcome == "published" and steps == []  # verified backup reused, not recreated


def test_identical_retry_creates_no_new_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    replace_with_backup(root, a, "op-B", [3])
    before = snapshot(root)
    steps: list[str] = []
    monkeypatch.setattr(_managed, "_backup_step", steps.append)
    again = replace_with_backup(root, a, "op-B", [3])
    assert again.outcome == "already_committed" and steps == [] and snapshot(root) == before


def test_a12_restore_is_a_verified_authorized_replacement(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    b = replace_with_backup(root, a, "op-B", [3])
    with pytest.raises(pub.PublicationRefusedError) as stale:
        pub.restore_backup(root, a.descriptor_digest, expected_current_digest=a.descriptor_digest)
    assert stale.value.receipt.reason_code == "stale_predecessor"  # A is not current any more
    restored = pub.restore_backup(
        root, a.descriptor_digest, expected_current_digest=b.descriptor_digest
    )
    assert restored.outcome == "published" and restored.result_id == a.result_id
    assert current_ids(root) == [1, 2]
    assert restored.descriptor_digest not in (
        a.descriptor_digest,
        b.descriptor_digest,
    )  # a new commit


def test_a12_a_corrupt_backup_is_not_restorable(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    b = replace_with_backup(root, a, "op-B", [3])
    victim = root / "backups" / a.descriptor_digest / "alpha.parquet"
    victim.write_bytes(victim.read_bytes()[:-1] + b"Z")
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.restore_backup(root, a.descriptor_digest, expected_current_digest=b.descriptor_digest)
    assert exc.value.receipt.reason_code == "backup_unverifiable"
    assert current_ids(root) == [3]


def test_corrupt_current_pointer_is_refused_not_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    put(root, "op-A", [1])
    (root / "CURRENT").write_text("{broken")
    with pytest.raises(pub.ResolveError):
        pub.resolve_current(root)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        put(root, "op-B", [2])
    assert exc.value.receipt.reason_code == "target_corrupt:current"
    assert (root / "CURRENT").read_text() == "{broken"


def test_a13_managed_lock_failure_is_refused_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.publication import _lock

    monkeypatch.setattr(_lock.fcntl, "flock", _boom)
    root = tmp_path / "ds"
    sink = ManagedDatasetSink(root)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.write("alpha", sup.table_a())
    assert exc.value.receipt.reason_code == "lock_unavailable"
    assert not (root / ".staging").exists()


def test_managed_sink_rejects_a_mismatched_request_unit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ManagedDatasetSink(
            tmp_path / "ds", request=pub.PublicationRequest(destination=tmp_path / "ds")
        )


def test_budget_includes_prior_output_and_requested_backup(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = put(root, "op-A", [1, 2])
    request = _req(
        root,
        "op-B",
        pub.Replace(a.descriptor_digest, pub.ProtectedBackup("p")),
        required_free_bytes=1 << 62,
    )
    sink = ManagedDatasetSink(root, request=request)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.write("alpha", pa.table({"id": [1]}))
    assert exc.value.receipt.reason_code == "insufficient_budget"
