"""F22 Slice 5 / A9 / A12: rejected-staging lifecycle, holds, and historical expiry."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution._transactional_sink import ManagedDatasetSink, ParquetTransactionalSink
from decoy_engine.publication import _retention as ret
from tests.unit.publication import _support as sup


def private_store(tmp_path: Path, name: str = "retained") -> Path:
    store = tmp_path / name
    store.mkdir(mode=0o700)
    store.chmod(0o700)
    return store


def rejected_sink(
    tmp_path: Path, policy: pub.RetentionPolicy | None, op: str = "op-R"
) -> ParquetTransactionalSink:
    target = tmp_path / "out"
    request = pub.PublicationRequest(
        destination=target,
        operation_id=op,
        retention=policy,
    )
    sink = ParquetTransactionalSink(target, request=request)
    sink.write("alpha", sup.table_a())
    sink.record_validation([pub.CheckResult("fk", "fail")])
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()
    return sink


def policy(store: Path, **kw: object) -> pub.RetentionPolicy:
    args: dict[str, object] = dict(store=store, owner="job-1", expiry_s=3600.0, byte_budget=1 << 30)
    args.update(kw)
    return pub.RetentionPolicy(**args)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- default discard


def test_default_is_best_effort_discard_with_a_typed_report(tmp_path: Path) -> None:
    sink = rejected_sink(tmp_path, None)
    assert sup.staging_dirs(tmp_path)
    sink.abort()
    assert sup.staging_dirs(tmp_path) == []
    assert sink.abort_report is not None and sink.abort_report.status == "succeeded"
    assert sink.abort_report.reason == "required_check_failed"
    assert not (tmp_path / "out").exists()


def test_an_aborted_or_refused_sink_can_never_commit_or_write_again(tmp_path: Path) -> None:
    sink = rejected_sink(tmp_path, None)
    with pytest.raises(pub.PublicationRefusedError) as again:
        sink.commit()  # refused attempts are terminal
    assert again.value.receipt.reason_code == "attempt_rejected"
    sink.abort()
    with pytest.raises(pub.PublicationRefusedError):
        sink.write("beta", sup.table_b())
    with pytest.raises(pub.PublicationRefusedError) as aborted:
        sink.commit()
    assert aborted.value.receipt.reason_code == "attempt_aborted"
    assert not (tmp_path / "out").exists()


def test_abort_alone_makes_a_sink_terminal(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    sink.write("alpha", sup.table_a())
    sink.abort()  # no commit was ever attempted: abort itself must latch
    with pytest.raises(pub.PublicationRefusedError):
        sink.write("beta", sup.table_b())
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "attempt_aborted"
    assert not (tmp_path / "out").exists() and sup.staging_dirs(tmp_path) == []


def test_abort_deletion_failure_is_reported_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup.publish_two(
        tmp_path / "out",
        request=pub.PublicationRequest(destination=tmp_path / "out", operation_id="op-A"),
    )
    good = sup.tree_digest(tmp_path / "out")
    other = tmp_path / "out2"
    sink = ParquetTransactionalSink(
        other,
        request=pub.PublicationRequest(destination=other),
    )
    sink.write("alpha", sup.table_a())
    sink.record_validation([pub.CheckResult("c", "fail")])
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    sink.abort()  # must not raise
    report = sink.abort_report
    assert report is not None and report.status == "failed" and report.resource is not None
    monkeypatch.undo()
    assert ret.discard(report.resource, None).status == "succeeded"  # recoverable record
    assert sup.staging_dirs(tmp_path) == []
    assert sup.tree_digest(tmp_path / "out") == good  # old output untouched


# --------------------------------------------------------------------------- retained


def test_retained_rejected_staging_is_private_unreleasable_and_described(tmp_path: Path) -> None:
    store = private_store(tmp_path)
    sink = rejected_sink(tmp_path, policy(store))
    sink.abort()
    report = sink.abort_report
    assert report is not None and report.status == "retained" and report.retained_path
    assert sup.staging_dirs(tmp_path) == []
    (record,) = pub.inventory(store)
    assert (record.owner, record.operation_id, record.reason_code) == (
        "job-1",
        "op-R",
        "required_check_failed",
    )
    assert record.classification == "rejected_staging" and record.expires_at > time.time()
    payload = report.retained_path
    assert (payload / "alpha.parquet").exists()
    assert not (payload / "_decoy_publication.json").exists()  # no claim was ever minted
    # Every resolution path fails for retained data.
    assert pub.inspect_destination(payload).kind != "committed"
    assert pub.inspect_destination(record.path).kind != "committed"
    assert (
        pub.lookup_committed(
            pub.PublicationRequest(destination=tmp_path / "out", operation_id="op-R")
        )
        is None
    )
    assert not (tmp_path / "out").exists()


def test_a_prepared_claim_is_neutralized_when_its_staging_is_retained(tmp_path: Path) -> None:
    store = private_store(tmp_path)
    target = tmp_path / "out"
    target.mkdir()
    (target / "legacy.txt").write_text("occupant")
    request = pub.PublicationRequest(
        destination=target, operation_id="op-C", retention=policy(store)
    )
    sink = ParquetTransactionalSink(target, request=request)
    sink.write("alpha", sup.table_a())
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()  # prepared (descriptor written), then refused by the occupant
    assert exc.value.receipt.reason_code == "target_occupied_legacy"
    staged = next(tmp_path.glob("_decoy_stage_*"))
    assert (staged / "_decoy_publication.json").exists()
    sink.abort()
    payload = sink.abort_report.retained_path  # type: ignore[union-attr]
    assert not (payload / "_decoy_publication.json").exists()
    assert (payload / "_decoy_publication.json.rejected").exists()
    assert pub.inspect_destination(payload).kind != "committed"
    assert [p.name for p in target.iterdir()] == ["legacy.txt"]


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ("not_private", "retention_store_not_private"),
        ("budget", "retention_budget_exceeded"),
        ("expiry", "retention_expiry_not_finite"),
    ],
)
def test_unauthorized_or_unbounded_retention_degrades_to_discard_and_says_so(
    tmp_path: Path, setup: str, why: str
) -> None:
    store = private_store(tmp_path)
    kw: dict[str, object] = {}
    if setup == "not_private":
        store.chmod(0o755)
    if setup == "budget":
        kw["byte_budget"] = 1
    if setup == "expiry":
        kw["expiry_s"] = 0
    sink = rejected_sink(tmp_path, policy(store, **kw))
    sink.abort()
    assert sink.abort_report is not None
    assert (sink.abort_report.status, sink.abort_report.reason) == ("failed", why)
    assert (
        sup.staging_dirs(tmp_path) == [] and pub.inventory(store) == []
    )  # never silently retained
    store.chmod(0o700)


def test_cleanup_checks_ownership_holds_expiry_and_exact_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = private_store(tmp_path)
    sink = rejected_sink(tmp_path, policy(store, expiry_s=100.0))
    sink.abort()
    (record,) = pub.inventory(store)
    rid = record.record_id
    now = record.expires_at + 1

    assert (
        pub.cleanup_retained(store, rid, owner="intruder", now=now).reason == "ownership_mismatch"
    )
    assert (
        pub.cleanup_retained(store, rid, owner="job-1", now=record.expires_at - 5).reason
        == "not_expired"
    )
    assert pub.add_hold(store, rid, "active_attempt", "attempt-9")
    held = pub.cleanup_retained(store, rid, owner="job-1", now=now)
    assert (held.status, held.reason) == ("retained", "held")
    assert pub.remove_hold(store, rid, "active_attempt", "attempt-9")

    real = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    failed = pub.cleanup_retained(store, rid, owner="job-1", now=now)
    assert (failed.status, failed.reason) == ("failed", "cleanup_failed")
    assert pub.inventory(store)  # record intact and retryable
    monkeypatch.setattr(shutil, "rmtree", real)
    assert pub.cleanup_retained(store, rid, owner="job-1", now=now).status == "succeeded"
    assert pub.inventory(store) == []


def test_cleanup_never_follows_symlinks_or_unsafe_ids(tmp_path: Path) -> None:
    store = private_store(tmp_path)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (store / "abc123").symlink_to(outside, target_is_directory=True)
    for bad in ("abc123", "../precious", "", "abc/def", "ZZ"):
        result = pub.cleanup_retained(store, bad, owner="job-1", now=time.time() + 1e9)
        assert result.status == "failed" and result.reason == "unknown_or_unsafe_record", bad
    assert (outside / "keep.txt").read_text() == "keep"
    assert not pub.add_hold(store, "abc123", "reader", "x")
    assert not pub.add_hold(store, "abc123", "not_a_kind", "x")


def test_a_retained_record_never_blocks_or_aliases_a_later_publication(tmp_path: Path) -> None:
    store = private_store(tmp_path)
    rejected_sink(tmp_path, policy(store)).abort()
    receipt = sup.publish_two(tmp_path / "out")[1]
    assert receipt.outcome == "published"
    assert len(pub.inventory(store)) == 1


# --------------------------------------------------------------------------- managed history expiry (A12)


def _put(root: Path, op: str, ids: list[int], overwrite: object = None) -> pub.CommitReceipt:
    request = pub.PublicationRequest(
        destination=root,
        operation_id=op,
        unit=pub.UNIT_MANAGED_DATASET,
        overwrite=overwrite or pub.ErrorIfDifferent(),  # type: ignore[arg-type]
    )
    sink = ManagedDatasetSink(root, request=request)
    sink.write("alpha", pa.table({"id": ids}))
    return sink.commit()


def test_a12_historical_expiry_respects_current_readers_and_backups(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = _put(root, "op-A", [1, 2])
    b = _put(root, "op-B", [3], pub.Replace(a.descriptor_digest, pub.ProtectedBackup("pol")))
    gen_a = root / "generations" / a.descriptor_digest

    current = pub.expire_historical_payload(root, b.descriptor_digest)
    assert (current.status, current.reason) == ("retained", "is_current")
    assert pub.expire_historical_payload(root, "f" * 64).reason == "not_in_committed_lineage"

    with pub.pin_reader(root):  # a reader pinning CURRENT (B) cannot block A, but pins B
        pass
    # A reader pinning A (it resolved before B committed) holds A's payload.
    hold_dir = root / ".holds" / a.descriptor_digest
    hold_dir.mkdir(parents=True)
    (hold_dir / "reader-1").write_text(str(os.getpid()))
    held = pub.expire_historical_payload(root, a.descriptor_digest)
    assert (held.status, held.reason) == ("retained", "reader_hold")
    assert (gen_a / "alpha.parquet").exists()
    (hold_dir / "reader-1").unlink()

    done = pub.expire_historical_payload(root, a.descriptor_digest)
    assert done.status == "succeeded"
    assert not (gen_a / "alpha.parquet").exists()
    assert (gen_a / "_decoy_publication.json").exists()  # history tombstone survives
    backup = root / "backups" / a.descriptor_digest
    assert pub.inspect_destination(backup).kind == "committed"  # protected backup survives

    with pytest.raises(pub.PublicationRefusedError) as exc:  # retry of an expired op: no replay
        _put(root, "op-A", [1, 2])
    assert exc.value.receipt.reason_code == "committed_artifacts_expired"
    assert pub.resolve_current(root).descriptor_digest == b.descriptor_digest  # type: ignore[union-attr]

    restored = pub.restore_backup(
        root, a.descriptor_digest, expected_current_digest=b.descriptor_digest
    )
    assert restored.result_id == a.result_id  # expired history is still restorable from the backup


def test_dead_reader_holds_do_not_block_expiry(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = _put(root, "op-A", [1])
    _put(root, "op-B", [2], pub.Replace(a.descriptor_digest))
    hold_dir = root / ".holds" / a.descriptor_digest
    hold_dir.mkdir(parents=True)
    (hold_dir / "ghost").write_text("2147483646")  # no such process
    assert pub.expire_historical_payload(root, a.descriptor_digest).status == "succeeded"


def test_pin_reader_pins_a_consistent_snapshot_across_a_commit(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = _put(root, "op-A", [1, 2])
    with pub.pin_reader(root) as pinned:
        _put(root, "op-B", [9], pub.Replace(a.descriptor_digest))
        refused = pub.expire_historical_payload(root, pinned.descriptor_digest)
        assert refused.reason == "reader_hold"
        import pyarrow.parquet as pq

        assert pq.read_table(pinned.path / "alpha.parquet").column("id").to_pylist() == [1, 2]
    assert not list((root / ".holds" / a.descriptor_digest).iterdir())


def test_an_unreferenced_generation_is_not_a_result_and_not_expirable(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    a = _put(root, "op-A", [1])
    orphan = root / "generations" / ("0" * 64)
    shutil.copytree(root / "generations" / a.descriptor_digest, orphan)
    assert pub.resolve_current(root).descriptor_digest == a.descriptor_digest  # type: ignore[union-attr]
    assert pub.expire_historical_payload(root, "0" * 64).reason == "not_in_committed_lineage"
    assert (orphan / "alpha.parquet").exists()
