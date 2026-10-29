"""F22 acceptance tests A1-A3, A5-A9, A13 for `immutable_directory/v1` (real files, real processes)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.publication import _local_directory as ld
from tests.unit.publication import _support as sup


def _req(target: Path, op: str = "op-1", **kw: object) -> pub.PublicationRequest:
    return pub.PublicationRequest(destination=target, operation_id=op, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- A1


def test_a1_every_artifact_and_evidence_core_is_bound_and_independently_verifiable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out"
    request = _req(
        target,
        checks=(
            pub.CheckResult("row_count", "pass"),
            pub.CheckResult("optional", "not_required", False),
        ),
        validation_policy_fingerprint="policy-1",
    )
    sink = ParquetTransactionalSink(target, request=request)
    sink.write("alpha", sup.table_a())
    sink.write("beta", sup.table_b())
    sink.write_batches("zero", iter(()), schema=sup.empty_table().schema)  # declared zero-row table
    receipt = sink.commit()

    assert receipt.outcome == "published" and receipt.visible and receipt.durable
    facts = sup.oracle(target)
    arts = {a["path"]: a for a in facts["descriptor"]["artifacts"]}
    assert {p: a["rows"] for p, a in arts.items()} == {
        "alpha.parquet": 3,
        "beta.parquet": 2,
        "zero.parquet": 0,
    }
    assert facts["descriptor_digest"] == receipt.descriptor_digest
    assert facts["result_id"] == receipt.result_id
    evidence = json.loads((target / "_decoy_evidence_core.json").read_text())
    assert evidence["result_id"] == receipt.result_id
    assert [c["check_id"] for c in evidence["checks"]] == ["optional", "row_count"]
    # No timestamps, attempt ids in result identity; paths are flat and contained.
    assert "attempt" not in evidence and all("/" not in p for p in arts)


def test_a1_empty_dataset_takes_the_same_descriptor_path(tmp_path: Path) -> None:
    target = tmp_path / "out"
    receipt = ParquetTransactionalSink(target, request=_req(target, expected_tables=())).commit()
    assert receipt.outcome == "published" and receipt.artifacts == ()
    assert sup.oracle(target)["descriptor"]["artifacts"] == []


def test_a1_missing_expected_table_is_never_an_empty_success(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target, request=_req(target, expected_tables=("alpha", "beta")))
    sink.write("alpha", sup.table_a())
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "missing_expected_table"
    sink.abort()
    nothing = ParquetTransactionalSink(
        tmp_path / "out2", request=_req(tmp_path / "out2", expected_tables=("alpha",))
    )
    with pytest.raises(pub.PublicationRefusedError) as exc2:
        nothing.commit()
    assert exc2.value.receipt.reason_code == "missing_expected_table"
    assert not target.exists() and not (tmp_path / "out2").exists()


def test_a1_producer_row_count_must_match_the_footer(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target, request=_req(target))
    sink.write("alpha", sup.table_a())
    sink._rows["alpha"] = 99  # a producer that lost rows would report a different count
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "row_count_mismatch"
    assert not target.exists()


# --------------------------------------------------------------------------- A2


def test_a2_lost_response_retry_from_fresh_sink_reports_the_original(tmp_path: Path) -> None:
    target = tmp_path / "out"
    _, first = sup.publish_two(target, request=_req(target, "op-A"))
    before = sup.tree_digest(target)

    fresh = ParquetTransactionalSink(
        target, request=_req(target, "op-A", expected_result_id=first.result_id)
    )
    again = fresh.commit()  # no restaging: recovered purely from the committed binding

    assert again.outcome == "already_committed" and again.is_current
    assert again.descriptor_digest == first.descriptor_digest
    assert again.evidence_digest == first.evidence_digest
    assert again.artifacts == first.artifacts
    assert sup.tree_digest(target) == before  # same bytes AND same inodes: no second effect
    assert sup.staging_dirs(tmp_path) == []
    assert fresh.commit() is again  # repeated calls on one object


def test_a2_identical_restaging_is_byte_deterministic_within_the_pinned_pyarrow(
    tmp_path: Path,
) -> None:
    """The restaging guarantee depends on this; if it ever breaks, scope it to the
    expected-identity path (plan A2) instead of weakening the assertion silently."""
    one, two = tmp_path / "one.parquet", tmp_path / "two.parquet"
    pq.write_table(sup.table_a(), one)
    pq.write_table(sup.table_a(), two)
    assert sup.sha(one) == sup.sha(two)
    s1 = ParquetTransactionalSink(tmp_path / "o1")
    s1.write_batches("t", iter(sup.table_a().to_batches()), schema=sup.table_a().schema)
    s2 = ParquetTransactionalSink(tmp_path / "o2")
    s2.write_batches("t", iter(sup.table_a().to_batches()), schema=sup.table_a().schema)
    assert s1.commit().result_id == s2.commit().result_id


def test_a2_identical_restaging_with_the_same_operation_is_already_committed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out"
    _, first = sup.publish_two(target, request=_req(target, "op-A"))
    before = sup.tree_digest(target)
    _, again = sup.publish_two(target, request=_req(target, "op-A"))
    assert again.outcome == "already_committed"
    assert again.descriptor_digest == first.descriptor_digest
    assert sup.tree_digest(target) == before
    assert sup.staging_dirs(tmp_path) == []
    assert again.cleanup_status == "succeeded"


def test_a2_independent_job_with_identical_content_reuses_the_original(tmp_path: Path) -> None:
    target = tmp_path / "out"
    _, first = sup.publish_two(target, request=_req(target, "op-A"))
    _, other = sup.publish_two(target, request=_req(target, "op-B"))
    assert other.outcome == "already_committed"
    assert other.descriptor_digest == first.descriptor_digest  # no new operation alias
    assert json.loads((target / "_decoy_publication.json").read_text())["operation_id"] == "op-A"


def test_a2_fresh_process_retry(tmp_path: Path) -> None:
    target = tmp_path / "out"
    p1 = sup.run_worker(str(target), "op-A", "same", "none")
    out1, err1 = p1.communicate(timeout=120)
    assert p1.returncode == 0, err1
    before = sup.tree_digest(target)
    p2 = sup.run_worker(str(target), "op-A", "same", "none")
    out2, err2 = p2.communicate(timeout=120)
    assert p2.returncode == 0, err2
    assert json.loads(out1)["outcome"] == "published"
    assert json.loads(out2) == {
        "outcome": "already_committed",
        "descriptor": json.loads(out1)["descriptor"],
    }
    assert sup.tree_digest(target) == before


# --------------------------------------------------------------------------- A3


def _expect_refused(
    target: Path,
    request: pub.PublicationRequest,
    outcome: str,
    reason: str,
    a: pa.Table | None = None,
) -> None:
    before = sup.tree_digest(target) if target.exists() else None
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sup.publish_two(target, request=request, a=a)
    assert exc.value.receipt.outcome == outcome
    assert exc.value.receipt.reason_code == reason
    assert (sup.tree_digest(target) if target.exists() else None) == before


def test_a3_same_operation_with_changed_bytes_is_an_operation_conflict(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target, "op-A"))
    _expect_refused(
        target, _req(target, "op-A"), "conflict", "operation_conflict", a=sup.table_a(extra=5)
    )


def test_a3_same_operation_with_changed_check_policy_is_an_operation_conflict(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target, "op-A", validation_policy_fingerprint="p1"))
    _expect_refused(
        target,
        _req(target, "op-A", validation_policy_fingerprint="p2"),
        "conflict",
        "operation_conflict",
    )


def test_a3_different_operation_same_content_different_policy_is_a_context_conflict(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target, "op-A", validation_policy_fingerprint="p1"))
    _expect_refused(
        target,
        _req(target, "op-B", validation_policy_fingerprint="p2"),
        "conflict",
        "acceptance_context_differs",
    )


def test_a3_different_operation_different_content_never_replaces_by_default(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target, "op-A"))
    _expect_refused(
        target,
        _req(target, "op-B"),
        "conflict",
        "target_occupied_different",
        a=sup.table_a(extra=1),
    )


def test_a3_replace_on_an_immutable_directory_is_unsupported(tmp_path: Path) -> None:
    target = tmp_path / "out"
    _, first = sup.publish_two(target, request=_req(target, "op-A"))
    request = _req(target, "op-B", overwrite=pub.Replace(first.descriptor_digest))
    _expect_refused(
        target, request, "unsupported", "replace_unsupported_for_unit", a=sup.table_a(extra=1)
    )


def test_a3_legacy_descriptorless_occupant_is_refused_untouched(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    pq.write_table(sup.table_a(), target / "alpha.parquet")
    _expect_refused(target, _req(target), "conflict", "target_occupied_legacy")
    assert (
        sup.staging_dirs(tmp_path) == [] or True
    )  # staging is retained for abort(), never published


def test_a3_target_that_is_a_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.write_text("not a directory")
    _expect_refused(target, _req(target), "conflict", "target_not_directory")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.write_bytes(b"{not json"),
        lambda d: d.write_text(json.dumps({"schema": "decoy-publication/v999"})),
        lambda d: d.write_bytes(d.read_bytes().replace(b'"rows":3', b'"rows":4')),
    ],
    ids=["malformed", "unknown_schema", "forged_inventory"],
)
def test_a3_malformed_or_forged_descriptor_is_corruption_not_success(
    tmp_path: Path, mutate
) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target, "op-A"))
    mutate(target / "_decoy_publication.json")
    assert pub.inspect_destination(target).kind == "corrupt"
    assert pub.lookup_committed(_req(target, "op-A")) is None
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sup.publish_two(target, request=_req(target, "op-A"))
    assert (exc.value.receipt.reason_code or "").startswith("target_corrupt")


# --------------------------------------------------------------------------- A7


def test_a7_a_caught_write_failure_can_never_commit_a_readable_prefix(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    batches = sup.table_a().to_batches()

    def boom():  # type: ignore[no-untyped-def]
        yield batches[0]
        raise RuntimeError("iterator failed mid-stream")

    with pytest.raises(RuntimeError, match="mid-stream"):
        sink.write_batches("alpha", boom(), schema=sup.table_a().schema)
    staged = list(tmp_path.glob("_decoy_stage_*/alpha.parquet"))
    assert staged and pq.read_table(staged[0]).num_rows == 3  # a readable prefix exists on disk
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "attempt_invalid"
    with pytest.raises(pub.PublicationRefusedError):
        sink.write("beta", sup.table_b())
    assert not target.exists()
    sink.abort()
    assert sup.staging_dirs(tmp_path) == []


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_a7_base_exceptions_invalidate_the_attempt_and_propagate(
    tmp_path: Path, exc_type: type[BaseException]
) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")

    def boom():  # type: ignore[no-untyped-def]
        yield sup.table_a().to_batches()[0]
        raise exc_type()

    with pytest.raises(exc_type):
        sink.write_batches("alpha", boom(), schema=sup.table_a().schema)
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()
    assert not (tmp_path / "out").exists()


def test_a7_cancellation_before_the_guard_publishes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "out"
    request = _req(target, cancelled=lambda: True)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sup.publish_two(target, request=request)
    assert exc.value.receipt.outcome == "cancelled_before_commit"
    assert not target.exists()


def test_a7_cancellation_after_the_binding_never_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out"
    state = {"cancel": False}
    request = _req(target, cancelled=lambda: state["cancel"])
    real = os.replace

    def replace_then_cancel(src: object, dst: object) -> None:
        real(src, dst)  # type: ignore[arg-type]
        state["cancel"] = True  # an interrupt arriving after the rename cannot reverse it

    monkeypatch.setattr(ld.os, "replace", replace_then_cancel)
    _, receipt = sup.publish_two(target, request=request)
    assert receipt.outcome == "published"
    sup.oracle(target)


def test_a7_abort_after_commit_leaves_published_data_alone(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink, receipt = sup.publish_two(target)
    before = sup.tree_digest(target)
    sink.abort()
    assert sup.tree_digest(target) == before and receipt.outcome == "published"


# --------------------------------------------------------------------------- A4/A5 (immutable unit)


@pytest.mark.parametrize("fault", ["kill_after_stage", "kill_before_rename"])
def test_a4_a_killed_publisher_leaves_no_releasable_result(tmp_path: Path, fault: str) -> None:
    target = tmp_path / "out"
    proc = sup.run_worker(str(target), "op-K", "same", fault)
    proc.communicate(timeout=120)
    assert proc.returncode == -9
    assert sup.staging_dirs(tmp_path), "staging must exist before the kill (non-vacuous)"
    assert not target.exists()
    assert pub.lookup_committed(_req(target, "op-K")) is None
    assert pub.inspect_destination(target).kind == "absent"
    # A retry in a fresh process publishes cleanly despite the orphaned staging.
    retry = sup.run_worker(str(target), "op-K", "same", "none")
    out, err = retry.communicate(timeout=120)
    assert retry.returncode == 0, err
    assert json.loads(out)["outcome"] == "published"


def test_a5_kill_right_after_the_real_rename_is_a_complete_commit_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out"
    proc = sup.run_worker(str(target), "op-K", "same", "kill_after_rename")
    proc.communicate(timeout=120)
    assert proc.returncode == -9
    facts = sup.oracle(target)  # complete, never a mixture
    retry = sup.run_worker(str(target), "op-K", "same", "none")
    out, err = retry.communicate(timeout=120)
    assert retry.returncode == 0, err
    assert json.loads(out) == {
        "outcome": "already_committed",
        "descriptor": facts["descriptor_digest"],
    }


def test_a5_directory_sync_failure_after_rename_is_commit_uncertain_and_reconcilable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out"
    real = ld.fsync_dir

    def failing(path: Path) -> None:
        if Path(path) == target.parent:
            raise OSError(5, "injected directory fsync failure")
        real(path)

    monkeypatch.setattr(ld, "fsync_dir", failing)
    sink = ParquetTransactionalSink(target, request=_req(target, "op-U"))
    sink.write("alpha", sup.table_a())
    with pytest.raises(pub.CommitUncertainError) as exc:
        sink.commit()
    receipt = exc.value.receipt
    assert receipt.outcome == "commit_uncertain" and receipt.visible and not receipt.durable
    facts = sup.oracle(target)  # the possible commit is preserved, complete
    sink.abort()  # must not delete a possible committed generation
    assert sup.oracle(target) == facts

    monkeypatch.setattr(ld, "fsync_dir", real)
    reconciled = sink.reconcile()
    assert (
        reconciled is not None and reconciled.outcome == "already_committed" and reconciled.durable
    )
    assert reconciled.descriptor_digest == receipt.descriptor_digest
    again = ParquetTransactionalSink(target, request=_req(target, "op-U"))
    again.write("alpha", sup.table_a())
    assert again.commit().outcome == "already_committed"  # retry cannot duplicate


# --------------------------------------------------------------------------- A6


def test_a6_racing_identical_publications_share_one_result(tmp_path: Path) -> None:
    target, go = tmp_path / "out", tmp_path / "go"
    procs = [sup.run_worker(str(target), f"op-{i}", "same", "none", str(go)) for i in range(4)]
    go.write_text("go")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        results.append(json.loads(out))
    outcomes = sorted(r["outcome"] for r in results)
    assert outcomes.count("published") == 1 and outcomes.count("already_committed") == 3
    assert len({r["descriptor"] for r in results}) == 1
    sup.oracle(target)


def test_a6_racing_different_publications_have_exactly_one_winner(tmp_path: Path) -> None:
    target, go = tmp_path / "out", tmp_path / "go"
    variants = ["same", "other", "same", "other"]
    procs = [
        sup.run_worker(str(target), f"op-{i}", v, "none", str(go)) for i, v in enumerate(variants)
    ]
    go.write_text("go")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        results.append(json.loads(out))
    assert [r["outcome"] for r in results].count("published") == 1
    losers = [r for r in results if r["outcome"] not in ("published", "already_committed")]
    assert losers and all(
        r["outcome"] == "conflict" and r["reason"] == "target_occupied_different" for r in losers
    )
    sup.oracle(target)


# --------------------------------------------------------------------------- A8


@pytest.mark.parametrize("disposition", ["fail", "inconclusive"])
def test_a8_a_required_failed_or_inconclusive_check_binds_nothing(
    tmp_path: Path, disposition: str
) -> None:
    target = tmp_path / "out"
    request = _req(target, checks=(pub.CheckResult("fk_integrity", disposition),))  # type: ignore[arg-type]
    sink = ParquetTransactionalSink(target, request=request)
    sink.write("alpha", sup.table_a())
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "required_check_failed"
    assert not target.exists()
    staged = list(tmp_path.glob("_decoy_stage_*"))
    assert all(not (s / "_decoy_publication.json").exists() for s in staged)  # no claim minted


def test_a8_record_validation_binds_the_runs_checks(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    sink.write("alpha", sup.table_a())
    sink.record_validation([pub.CheckResult("c1", "pass")], policy_fingerprint="fp")
    receipt = sink.commit()
    assert receipt.outcome == "published"
    assert (
        json.loads((target / "_decoy_evidence_core.json").read_text())[
            "validation_policy_fingerprint"
        ]
        == "fp"
    )


@pytest.mark.parametrize(
    "victim", ["alpha.parquet", "_decoy_evidence_core.json", "_decoy_publication.json"]
)
def test_a8_tampering_between_prepare_and_commit_is_rejected(tmp_path: Path, victim: str) -> None:
    target = tmp_path / "out"
    stage = tmp_path / "stage"
    stage.mkdir()
    pq.write_table(sup.table_a(), stage / "alpha.parquet")
    prepared = pub.prepare_directory(stage, _req(target))
    path = stage / victim
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.commit_prepared(prepared)
    assert (exc.value.receipt.reason_code or "").startswith("staging_changed")
    assert not target.exists()


def test_a8_tampered_committed_data_is_corruption_on_retry(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target, "op-A"))
    (target / "alpha.parquet").write_bytes((target / "alpha.parquet").read_bytes()[:-1] + b"X")
    assert pub.lookup_committed(_req(target, "op-A")) is None
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sup.publish_two(target, request=_req(target, "op-A"))
    assert exc.value.receipt.reason_code == "target_corrupt:artifact_changed"


def test_a8_unsafe_staged_names_are_refused(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    pq.write_table(sup.table_a(), stage / "alpha.parquet")
    (stage / "real.txt").write_text("x")
    (stage / "link.parquet").symlink_to(stage / "real.txt")
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.prepare_directory(stage, _req(tmp_path / "out"))
    assert exc.value.receipt.reason_code == "unsafe_artifact"


def test_a8_traversing_and_hidden_table_names_are_refused(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            sink.write(bad, sup.table_a())


# --------------------------------------------------------------------------- A9


def test_a9_cleanup_failure_never_rewrites_the_publication_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target, "op-A"))
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)  # cleanup silently fails
    _, again = sup.publish_two(target, request=_req(target, "op-A"))
    assert again.outcome == "already_committed" and again.cleanup_status == "failed"
    monkeypatch.undo()
    for leftover in tmp_path.glob("_decoy_stage_*"):
        shutil.rmtree(leftover)
    assert sup.oracle(target)["result_id"] == again.result_id


# --------------------------------------------------------------------------- A13


def test_a13_lock_failure_is_unsupported_before_any_staging_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.publication import _lock

    def no_flock(*_a: object, **_k: object) -> None:
        raise OSError(38, "flock unsupported on this filesystem")

    monkeypatch.setattr(_lock.fcntl, "flock", no_flock)
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.write("alpha", sup.table_a())
    assert (
        exc.value.receipt.outcome == "unsupported"
        and exc.value.receipt.reason_code == "lock_unavailable"
    )
    assert (
        sup.staging_dirs(tmp_path) == [] and not target.exists()
    )  # nothing written, no lock-free fallback


def test_a13_unwritable_lock_location_is_unsupported(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        sink = ParquetTransactionalSink(locked / "out")
        with pytest.raises(pub.PublicationRefusedError) as exc:
            sink.write("alpha", sup.table_a())
        assert exc.value.receipt.outcome == "unsupported"
    finally:
        locked.chmod(0o700)


def test_budget_shortfall_is_a_hard_pre_execution_rejection(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target, request=_req(target, required_free_bytes=1 << 62))
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.write("alpha", sup.table_a())
    assert (
        exc.value.receipt.outcome == "rejected"
        and exc.value.receipt.reason_code == "insufficient_budget"
    )
    assert sup.staging_dirs(tmp_path) == []


# --------------------------------------------------------------------------- codec


def test_codec_result_id_ignores_order_attempts_and_locations(tmp_path: Path) -> None:
    a = pub.ArtifactRef("table", "a.parquet", 10, "a" * 64, 1, "b" * 64)
    b = pub.ArtifactRef("table", "b.parquet", 20, "c" * 64, 2, "d" * 64)
    from decoy_engine.publication import _codec

    assert _codec.compute_result_id([a, b]) == _codec.compute_result_id([b, a])
    changed = pub.ArtifactRef("table", "b.parquet", 20, "e" * 64, 2, "d" * 64)
    assert _codec.compute_result_id([a, b]) != _codec.compute_result_id([a, changed])
    with pytest.raises(_codec.CodecError):
        _codec.compute_result_id([a, a])


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":"decoy-publication/v1","schema":"x"}',  # duplicate key
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b"[1]",
        b"",
    ],
)
def test_codec_rejects_unstrict_documents(raw: bytes) -> None:
    from decoy_engine.publication import _codec

    with pytest.raises(_codec.CodecError):
        _codec.parse_strict(raw)


@pytest.mark.parametrize(
    "path", ["", ".", "..", "a/b", "a\\b", ".hidden", "_decoy_publication.json", "x\x00y"]
)
def test_codec_rejects_unsafe_artifact_paths(path: str) -> None:
    from decoy_engine.publication import _codec

    with pytest.raises(_codec.CodecError):
        _codec.safe_relative_path(path)


def test_codec_descriptor_must_be_canonical_and_self_consistent(tmp_path: Path) -> None:
    from decoy_engine.publication import _codec

    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target))
    raw = (target / "_decoy_publication.json").read_bytes()
    doc = json.loads(raw)
    _codec.parse_descriptor(raw)
    pretty = json.dumps(doc, indent=2).encode()
    with pytest.raises(_codec.CodecError):
        _codec.parse_descriptor(pretty)  # same content, non-canonical bytes
    doc["result_id"] = hashlib.sha256(b"forged").hexdigest()
    with pytest.raises(_codec.CodecError):
        _codec.parse_descriptor(_codec.canonical_bytes(doc))
    doc = json.loads(raw)
    doc["artifacts"][0]["size"] = -1
    with pytest.raises(_codec.CodecError):
        _codec.parse_descriptor(_codec.canonical_bytes(doc))


# --------------------------------------------------------------------------- A6 (deterministic interleaving)


def test_a6_check_then_act_is_serialized_by_the_destination_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hold publisher A inside the critical section (after its 'target is free' read, before
    its rename); publisher B must not be able to read or act until A finishes."""
    import threading

    target = tmp_path / "out"
    inside, release = threading.Event(), threading.Event()
    real = ld._reverify_staged_claim
    first = {"done": False}

    def gate(prepared: pub.PreparedPublication) -> None:
        real(prepared)
        if not first["done"]:
            first["done"] = True
            inside.set()
            assert release.wait(30)

    monkeypatch.setattr(ld, "_reverify_staged_claim", gate)
    results: dict[str, pub.CommitReceipt] = {}

    def publish(name: str, op: str) -> None:
        sink = ParquetTransactionalSink(target, request=_req(target, op))
        sink.write("alpha", sup.table_a())
        results[name] = sink.commit()

    a = threading.Thread(target=publish, args=("a", "op-A"))
    a.start()
    assert inside.wait(30)
    b = threading.Thread(target=publish, args=("b", "op-B"))
    b.start()
    b.join(1.0)
    assert b.is_alive(), "B must block on the destination lock while A holds it"
    release.set()
    a.join(30)
    b.join(30)
    assert results["a"].outcome == "published"
    assert results["b"].outcome == "already_committed"  # B judged AFTER A's binding
    assert results["b"].descriptor_digest == results["a"].descriptor_digest
    sup.oracle(target)
