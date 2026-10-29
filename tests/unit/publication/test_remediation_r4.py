"""F22 r4: the sink is a one-way state machine; every receipt return re-proves admission;
the child's footers and row counts are verified before any payload read."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution._isolated_commit import PublishContext
from decoy_engine.execution._transactional_sink import (
    ManagedDatasetSink,
    ParquetPreparationSink,
    ParquetTransactionalSink,
)
from decoy_engine.publication import _protocol as proto
from tests.unit.publication import _support as sup

FAIL = [pub.CheckResult("fk", "fail")]


def _req(dest: Path, **kw: object) -> pub.PublicationRequest:
    return pub.PublicationRequest(destination=dest, operation_id="op-1", **kw)  # type: ignore[arg-type]


def _committed(dest: Path) -> tuple[ParquetTransactionalSink, pub.CommitReceipt]:
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    sink.write("alpha", sup.table_a())
    return sink, sink.commit()


# ------------------------------------------------------------ Invariant A: state machine


def test_a_committed_sink_cannot_be_revalidated_and_its_repeat_commit_reproves_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, receipt = _committed(tmp_path / "out")
    with pytest.raises(pub.PublicationRefusedError):
        sink.record_validation(FAIL)  # mutation after seal is refused
    assert sink.request.checks == ()  # and did not take effect
    seen: list[str] = []
    real = proto.admit_request
    monkeypatch.setattr(proto, "admit_request", lambda r, s=None: (seen.append("x"), real(r, s))[1])
    assert sink.commit() is receipt  # repeat commit: same receipt ...
    assert seen, "... but only after admission + acceptance were re-proven"


def test_the_repeat_commit_refuses_if_the_request_no_longer_passes_admission(
    tmp_path: Path,
) -> None:
    sink, _ = _committed(tmp_path / "out")
    object.__setattr__(
        sink, "_request", sink.request.__class__(**{**sink.request.__dict__, "checks": tuple(FAIL)})
    )
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()  # a cached receipt is never emitted past a failing authority
    assert exc.value.receipt.reason_code == "required_check_failed"


@pytest.mark.parametrize("state", ["sealed", "aborted", "refused", "failed"])
def test_record_validation_is_rejected_in_every_terminal_state(tmp_path: Path, state: str) -> None:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    sink.write("alpha", sup.table_a())
    if state == "sealed":
        sink.commit()
    elif state == "aborted":
        sink.abort()
    elif state == "refused":
        sink.record_validation(FAIL)
        with pytest.raises(pub.PublicationRefusedError):
            sink.commit()
    else:
        sink._failed = True
    with pytest.raises(pub.PublicationRefusedError):
        sink.record_validation([pub.CheckResult("fk", "pass")])


@pytest.mark.parametrize("how", ["abort", "seal", "failed"])
def test_preparation_sink_commit_requires_an_open_sink(tmp_path: Path, how: str) -> None:
    target = tmp_path / "child"
    sink = ParquetPreparationSink(target)
    sink.write("t", sup.table_a())
    if how == "abort":
        sink.abort()
    elif how == "seal":
        sink.commit()
        before = sorted(p.name for p in target.iterdir())
    else:
        sink._failed = True
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()
    if how == "abort":
        assert not target.exists()  # no empty target with stale counts
    if how == "seal":
        assert sorted(p.name for p in target.iterdir()) == before


@pytest.mark.parametrize("state", ["failed", "aborted", "refused"])
@pytest.mark.parametrize("op", ["lookup_committed", "reconcile"])
def test_lookup_and_reconcile_refuse_on_a_terminal_sink_even_if_a_result_exists(
    tmp_path: Path, state: str, op: str
) -> None:
    dest = tmp_path / "out"
    _, first = _committed(dest)
    request = _req(dest, expected_result_id=first.result_id)
    sink = ParquetTransactionalSink(dest, request=request)
    if state == "failed":
        sink._failed = True
    elif state == "aborted":
        sink.abort()
    else:
        sink.write("alpha", sup.table_a())
        sink.record_validation(FAIL)
        with pytest.raises(pub.PublicationRefusedError):
            sink.commit()
    with pytest.raises(pub.PublicationRefusedError) as exc:
        getattr(sink, op)()
    assert exc.value.receipt.reason_code in (
        "attempt_invalid",
        "attempt_aborted",
        "attempt_rejected",
    )


def test_a_refusal_without_a_receipt_still_latches_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    sink.write("alpha", sup.table_a())

    def refuse(*_a: object, **_k: object) -> None:
        raise pub.PublicationRefusedError("no receipt attached")

    monkeypatch.setattr(pub, "prepare_directory", refuse)
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()
    assert sink._reason == "refused"
    monkeypatch.undo()
    with pytest.raises(pub.PublicationRefusedError) as again:
        sink.commit()
    assert again.value.receipt.reason_code == "attempt_rejected"


@pytest.mark.parametrize("cls", [ParquetTransactionalSink, ManagedDatasetSink])
def test_the_zero_write_recovery_hit_seals_the_sink(tmp_path: Path, cls: type) -> None:
    unit = pub.UNIT_MANAGED_DATASET if cls is ManagedDatasetSink else pub.UNIT_IMMUTABLE_DIRECTORY
    dest = tmp_path / "out"
    first = cls(
        dest, request=pub.PublicationRequest(destination=dest, unit=unit, operation_id="op-1")
    )
    first.write("alpha", sup.table_a())
    done = first.commit()
    again = cls(
        dest,
        request=pub.PublicationRequest(
            destination=dest, unit=unit, operation_id="op-1", expected_result_id=done.result_id
        ),
    )
    assert again.commit().outcome == "already_committed"
    with pytest.raises(pub.PublicationRefusedError):
        again.write("beta", sup.table_b())
    with pytest.raises(pub.PublicationRefusedError):
        again.record_validation(FAIL)
    assert sup.tree_digest(dest) is not None


def test_commit_after_uncertain_must_reconcile_not_recommit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.publication import _local_directory as ld

    dest = tmp_path / "out"
    real = ld.fsync_dir
    monkeypatch.setattr(
        ld,
        "fsync_dir",
        lambda p: (_ for _ in ()).throw(OSError(5, "x")) if Path(p) == dest.parent else real(p),
    )
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    sink.write("alpha", sup.table_a())
    with pytest.raises(pub.CommitUncertainError):
        sink.commit()
    monkeypatch.undo()
    sink.abort()
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "commit_uncertain"
    assert sink.reconcile().outcome == "already_committed"  # type: ignore[union-attr]
    assert sup.tree_digest(dest)


# ------------------------------------------------------------ Invariant B: footers before payload


def _staged(root: Path, name: str, ids: list[int]) -> Path:
    d = root / name
    d.mkdir()
    pq.write_table(pa.table({"id": ids}), d / "t.parquet")
    return d


def _env(d: Path, rows: int = 2, **over: object) -> dict[str, object]:
    prep = {
        "schema": "decoy-child-preparation/v1",
        "staging_dir": str(d),
        "tables": ["t"],
        "row_counts": {"t": rows},
        **over,
    }
    return {
        "outcome": "completed",
        "peak_rss_mb": 1.0,
        "preparation": prep,
        "quality_metrics": {},
        "table_kinds": {},
    }


def _no_reads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    from decoy_engine.execution import _isolated_run as run

    calls: list[object] = []

    def spy(*a: object, **k: object) -> None:
        calls.append(a)
        raise AssertionError("payload read before the child gate")

    monkeypatch.setattr(run.pq, "read_table", spy)
    monkeypatch.setattr(run, "read_staged_row_errors", spy)
    return calls


@pytest.mark.parametrize(
    "case",
    ["rows_lie", "missing", "corrupt", "symlink", "undeclared"],
)
def test_child_defects_are_refused_with_zero_payload_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from decoy_engine.execution._isolated_run import _result_from_envelope

    assigned = _staged(tmp_path, "assigned", [1, 2])
    rows = 2
    if case == "rows_lie":
        rows = 7
    elif case == "missing":
        (assigned / "t.parquet").unlink()
    elif case == "corrupt":
        (assigned / "t.parquet").write_bytes(b"PAR1 garbage")
    elif case == "symlink":
        (assigned / "t.parquet").unlink()
        (assigned / "t.parquet").symlink_to(assigned / "nowhere")
    else:
        pq.write_table(pa.table({"id": [9]}), assigned / "extra.parquet")
    calls = _no_reads(monkeypatch)
    out = tmp_path / "final"
    result = _result_from_envelope(_env(assigned, rows), out, 1, assigned, PublishContext())
    assert (
        result.outcome == "crashed"
        and result.publication_receipt.reason_code == "child_envelope_invalid"
    )  # type: ignore[union-attr]
    assert calls == [] and not out.exists()


def test_the_full_publish_request_reaches_the_child_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution._isolated_run import _result_from_envelope

    assigned = _staged(tmp_path, "assigned", [1, 2])
    calls = _no_reads(monkeypatch)
    out = tmp_path / "final"
    wrong_tables = _result_from_envelope(
        _env(assigned), out, 1, assigned, PublishContext(expected_tables=("other",))
    )
    assert wrong_tables.publication_receipt.reason_code == "child_envelope_invalid"  # type: ignore[union-attr]
    failed = _result_from_envelope(
        _env(assigned), out, 1, assigned, PublishContext(checks=tuple(FAIL))
    )
    assert failed.publication_receipt.reason_code == "required_check_failed"  # type: ignore[union-attr]
    assert calls == [] and not out.exists() and (assigned / "t.parquet").exists()


def test_a_well_formed_child_with_matching_footers_still_publishes(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import _result_from_envelope

    assigned = _staged(tmp_path, "assigned", [1, 2])
    out = tmp_path / "final"
    result = _result_from_envelope(
        _env(assigned), out, 1, assigned, PublishContext(expected_tables=("t",))
    )
    assert result.outcome == "completed" and result.publication_receipt.outcome == "published"  # type: ignore[union-attr]
    sup.oracle(out)
