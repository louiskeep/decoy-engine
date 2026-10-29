"""Edge branches of the local backends: ambiguous rename outcomes, partial corruption, relocation."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution._transactional_sink import ManagedDatasetSink, ParquetTransactionalSink
from decoy_engine.publication import _local_directory as ld
from decoy_engine.publication import _managed
from tests.unit.publication import _support as sup


def _req(target: Path, op: str = "op-1", **kw: object) -> pub.PublicationRequest:
    return pub.PublicationRequest(destination=target, operation_id=op, **kw)  # type: ignore[arg-type]


def test_rename_error_after_the_rename_took_effect_is_commit_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out"
    real = os.replace

    def replace_then_report_failure(src: object, dst: object) -> None:
        real(src, dst)  # type: ignore[arg-type]
        raise OSError(5, "ack lost after the rename")

    monkeypatch.setattr(ld.os, "replace", replace_then_report_failure)
    sink = ParquetTransactionalSink(target, request=_req(target))
    sink.write("alpha", sup.table_a())
    with pytest.raises(pub.CommitUncertainError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "rename_error_visible" and exc.value.receipt.visible
    sup.oracle(target)
    sink.abort()
    sup.oracle(target)  # never deleted


def test_pointer_switch_error_after_the_switch_took_effect_is_commit_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ds"
    real = os.replace

    def switch_then_report_failure(src: object, dst: object) -> None:
        real(src, dst)  # type: ignore[arg-type]
        if Path(str(dst)).name == "CURRENT":
            raise OSError(5, "ack lost after the switch")

    monkeypatch.setattr(_managed.os, "replace", switch_then_report_failure)
    request = pub.PublicationRequest(destination=root, unit=pub.UNIT_MANAGED_DATASET)
    sink = ManagedDatasetSink(root, request=request)
    sink.write("alpha", sup.table_a())
    with pytest.raises(pub.CommitUncertainError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "switch_error_visible"
    assert pub.resolve_current(root) is not None
    monkeypatch.undo()
    assert sink.reconcile().outcome == "already_committed"  # type: ignore[union-attr]


@pytest.mark.parametrize("victim", ["_decoy_evidence_core.json"])
def test_missing_or_changed_evidence_core_is_corruption(tmp_path: Path, victim: str) -> None:
    target = tmp_path / "out"
    sup.publish_two(target, request=_req(target))
    path = target / victim
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    assert pub.inspect_destination(target).reason == "evidence_changed"
    path.unlink()
    assert pub.inspect_destination(target).reason == "evidence_missing"


def test_a_relocated_committed_directory_is_not_adopted_as_the_new_destination(
    tmp_path: Path,
) -> None:
    original = tmp_path / "out"
    sup.publish_two(original, request=_req(original, "op-A"))
    moved = tmp_path / "moved"
    shutil.copytree(original, moved)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sup.publish_two(moved, request=_req(moved, "op-A"))
    assert exc.value.receipt.reason_code == "destination_mismatch"


def test_an_unexpected_staged_table_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target, request=_req(target, expected_tables=("alpha",)))
    sink.write("alpha", sup.table_a())
    sink.write("extra", sup.table_b())
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "unexpected_table"


def test_an_unknown_publication_unit_is_unsupported(tmp_path: Path) -> None:
    request = _req(tmp_path / "x", unit="object_store/v1")
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.validate_request(request)
    assert exc.value.receipt.outcome == "unsupported"
    assert exc.value.receipt.reason_code == "unknown_publication_unit"


def test_lookup_requires_the_named_identity_not_just_an_occupant(tmp_path: Path) -> None:
    target = tmp_path / "out"
    _, first = sup.publish_two(target, request=_req(target, "op-A"))
    assert pub.lookup_committed(_req(target, "someone-else")) is None
    assert pub.lookup_committed(_req(target, "someone-else", expected_result_id="0" * 64)) is None
    hit = pub.lookup_committed(_req(target, "someone-else", expected_result_id=first.result_id))
    assert hit is not None and hit.descriptor_digest == first.descriptor_digest


def test_managed_lookup_by_expected_identity_and_missing_dataset(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    request = pub.PublicationRequest(destination=root, unit=pub.UNIT_MANAGED_DATASET)
    assert pub.lookup_committed(request) is None
    sink = ManagedDatasetSink(root, request=request)
    sink.write("alpha", sup.table_a())
    first = sink.commit()
    probe = pub.PublicationRequest(
        destination=root,
        unit=pub.UNIT_MANAGED_DATASET,
        operation_id="other",
        expected_result_id=first.result_id,
    )
    assert pub.lookup_committed(probe).descriptor_digest == first.descriptor_digest  # type: ignore[union-attr]
    wrong = pub.PublicationRequest(
        destination=root,
        unit=pub.UNIT_MANAGED_DATASET,
        operation_id="other",
        expected_result_id="1" * 64,
    )
    assert pub.lookup_committed(wrong) is None
    assert pub.resolve_current(tmp_path / "nowhere") is None
    with pytest.raises(pub.ResolveError):
        with pub.pin_reader(tmp_path / "nowhere"):
            pass


def test_legacy_sink_commit_returning_none_leaves_the_receipt_absent(tmp_path: Path) -> None:
    from tests.unit.execution import _r3_scenarios as sc

    class Legacy:
        def __init__(self) -> None:
            self.committed = 0
            self.tables: list[str] = []

        def write(self, table: str, data: pa.Table) -> None:
            self.tables.append(table)

        def commit(self) -> None:
            self.committed += 1

        def abort(self) -> None:
            pass

    cfg, tables = sc.fk_job(tmp_path)
    sink = Legacy()
    result = sc._go(
        cfg, tables, sink=sink, out_of_core_threshold_rows=1_000, use_byte_estimate_routing=False
    )
    assert sink.committed == 1 and sink.tables  # the duck-typed sink really committed
    assert result.publication_receipt is None  # but it issues no receipt: no F22 claim
