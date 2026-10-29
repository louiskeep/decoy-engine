"""F22 golden: the publication contract through the PUBLIC API only.

Pinned two-table fixture with known row/schema digests, required validation evidence and a
prior approved generation. The oracle re-reads descriptor bytes and recomputes every
count/hash from raw files; it never calls the production equality or verification helpers.
Covers A2 (lost response), A4/A5 (replacement + switch), A7 (failed/cancelled pre-binding
attempts), A8 (failed evidence is not approval).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._transactional_sink import ManagedDatasetSink

ORDERS = pa.table(
    {
        "order_id": pa.array([1, 2, 3, 4, 5], pa.int64()),
        "customer": pa.array(["c1", "c2", "c1", "c3", "c2"]),
        "amount": pa.array([10.5, 20.0, 5.25, 99.0, 1.0], pa.float64()),
    }
)
CUSTOMERS = pa.table(
    {
        "customer": pa.array(["c1", "c2", "c3"]),
        "tier": pa.array(["gold", "silver", "gold"]),
    }
)
PINNED = {
    "orders": (
        5,
        "667e61a9b4a60496a26668875c97c442b4d105595dcf9dc8936e617a13f61809",
        "c686a153fde9422b385b2c76c0e68235a0d5c9336d7a54358eee4203fd07c1a1",
    ),
    "customers": (
        3,
        "8aaca50bb6a1a944555d4762cd597a05174638d3aef26b3f7a18633284fac71b",
        "f197b146313f396ceae464df3aba25618e0bc3237fd1c16dfa97510c22908f8b",
    ),
}
CHECKS = (pub.CheckResult("fk_integrity", "pass"), pub.CheckResult("row_counts", "pass"))
DESCRIPTOR_KEYS = {
    "schema", "unit", "destination", "job_id", "operation_id", "attempt_id", "result_id",
    "artifacts", "validation_policy_fingerprint", "evidence", "lineage",
}  # fmt: skip


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _independent_schema_digest(path: Path) -> str:
    fields = [[f.name, str(f.type), f.nullable] for f in pq.read_schema(path)]
    blob = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _row_digest(path: Path) -> str:
    rows = [
        json.dumps(r, sort_keys=True, separators=(",", ":"))
        for r in pq.read_table(path).to_pylist()
    ]
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _oracle(unit_dir: Path) -> dict[str, object]:
    raw = (unit_dir / "_decoy_publication.json").read_bytes()
    doc = json.loads(raw)
    assert set(doc) == DESCRIPTOR_KEYS and doc["schema"] == "decoy-publication/v1"
    for art in doc["artifacts"]:
        path = unit_dir / art["path"]
        assert path.stat().st_size == art["size"] and _sha(path) == art["sha256"]
        name = path.stem
        rows, schema_d, row_d = PINNED[name]
        assert (art["rows"], art["schema_digest"]) == (rows, schema_d), name
        assert (
            pq.read_metadata(path).num_rows == rows and _independent_schema_digest(path) == schema_d
        )
        assert _row_digest(path) == row_d, name
    evidence = json.loads((unit_dir / doc["evidence"]["path"]).read_text())
    assert evidence["result_id"] == doc["result_id"]
    assert [c["check_id"] for c in evidence["checks"]] == ["fk_integrity", "row_counts"]
    assert all(c["disposition"] == "pass" for c in evidence["checks"])
    return {"digest": hashlib.sha256(raw).hexdigest(), "result_id": doc["result_id"]}


def _publish(
    sink_cls: type[ParquetTransactionalSink] | type[ManagedDatasetSink],
    dest: Path,
    op: str,
    *,
    orders: pa.Table = ORDERS,
    **request_kw: object,
) -> pub.CommitReceipt:
    unit = (
        pub.UNIT_MANAGED_DATASET if sink_cls is ManagedDatasetSink else pub.UNIT_IMMUTABLE_DIRECTORY
    )
    kw: dict[str, object] = {"checks": CHECKS, "validation_policy_fingerprint": "policy-v1"}
    kw.update(request_kw)
    request = pub.PublicationRequest(destination=dest, operation_id=op, unit=unit, **kw)  # type: ignore[arg-type]
    sink = sink_cls(dest, request=request)
    sink.write("orders", orders)
    sink.write("customers", CUSTOMERS)
    try:
        return sink.commit()
    except pub.CommitError:
        sink.abort()
        raise


def test_immutable_directory_commit_truth_and_lost_response(tmp_path: Path) -> None:
    dest = tmp_path / "approved"
    first = _publish(ParquetTransactionalSink, dest, "op-1")
    facts = _oracle(dest)
    assert (first.descriptor_digest, first.result_id) == (facts["digest"], facts["result_id"])
    inodes = {p.name: p.stat().st_ino for p in dest.iterdir()}

    for _ in range(2):  # lost response, retried from a fresh sink, twice
        again = _publish(ParquetTransactionalSink, dest, "op-1")
        assert again.outcome == "already_committed" and again.descriptor_digest == facts["digest"]
    assert {p.name: p.stat().st_ino for p in dest.iterdir()} == inodes
    assert _oracle(dest) == facts  # still exactly the original, no duplicate data


def test_pre_binding_failures_never_release_a_new_result(tmp_path: Path) -> None:
    dest = tmp_path / "ds"
    a = _publish(ManagedDatasetSink, dest, "op-A")
    a_facts = _oracle(pub.resolve_current(dest).path)  # type: ignore[union-attr]
    changed = ORDERS.set_column(2, "amount", pa.array([1.0] * 5, pa.float64()))
    replace = pub.Replace(a.descriptor_digest)

    failing_check = {"checks": (pub.CheckResult("fk_integrity", "fail"),)}
    attempts: list[tuple[str, dict[str, object]]] = [
        ("required check failed", failing_check),
        ("cancelled before the guard", {"cancelled": lambda: True}),
        ("stale predecessor", {"overwrite": pub.Replace("0" * 64)}),
    ]
    for label, extra in attempts:
        kw: dict[str, object] = {"overwrite": replace, **extra}
        request_checks = kw.pop("checks", CHECKS)
        request = pub.PublicationRequest(
            destination=dest,
            operation_id=f"op-{label}",
            unit=pub.UNIT_MANAGED_DATASET,
            checks=request_checks,  # type: ignore[arg-type]
            **kw,  # type: ignore[arg-type]
        )
        sink = ManagedDatasetSink(dest, request=request)
        sink.write("orders", changed)
        sink.write("customers", CUSTOMERS)
        with pytest.raises(pub.PublicationRefusedError):
            sink.commit()
        sink.abort()
        current = pub.resolve_current(dest)
        assert current is not None and current.descriptor_digest == a.descriptor_digest, label
        assert _oracle(current.path) == a_facts, label
        if label == "required check failed":  # recovery is denied up front for failed checks
            with pytest.raises(pub.PublicationRefusedError):
                pub.lookup_committed(request)
        else:
            assert pub.lookup_committed(request) is None, label

    class BoomError(Exception):
        pass

    def stream():  # type: ignore[no-untyped-def]
        yield changed.to_batches()[0]
        raise BoomError

    sink = ManagedDatasetSink(
        dest,
        request=pub.PublicationRequest(
            destination=dest,
            operation_id="op-boom",
            unit=pub.UNIT_MANAGED_DATASET,
            overwrite=replace,
        ),
    )
    with pytest.raises(BoomError):
        sink.write_batches("orders", stream(), schema=changed.schema)
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()  # a caught write failure can never commit its readable prefix
    sink.abort()
    assert pub.resolve_current(dest).descriptor_digest == a.descriptor_digest  # type: ignore[union-attr]


def test_replacement_switches_atomically_and_a_lost_response_is_idempotent(tmp_path: Path) -> None:
    dest = tmp_path / "ds"
    a = _publish(ManagedDatasetSink, dest, "op-A")
    pinned = pub.resolve_current(dest)
    assert pinned is not None
    new_orders = ORDERS.set_column(2, "amount", pa.array([2.0] * 5, pa.float64()))
    b = _publish(
        ManagedDatasetSink,
        dest,
        "op-B",
        orders=new_orders,
        overwrite=pub.Replace(a.descriptor_digest, pub.ProtectedBackup("policy-hold")),
    )
    assert b.outcome == "published" and b.backup_status == "succeeded"
    assert _oracle(pinned.path)["digest"] == a.descriptor_digest  # A untouched, readable
    current = pub.resolve_current(dest)
    assert current is not None and current.descriptor_digest == b.descriptor_digest
    assert pq.read_table(current.path / "orders.parquet").column("amount").to_pylist() == [2.0] * 5

    again = _publish(
        ManagedDatasetSink,
        dest,
        "op-B",
        orders=new_orders,
        overwrite=pub.Replace(a.descriptor_digest, pub.ProtectedBackup("policy-hold")),
    )
    assert again.outcome == "already_committed" and again.descriptor_digest == b.descriptor_digest
    assert len(list((dest / "generations").iterdir())) == 2  # no duplicate generation
    assert len(list((dest / "backups").iterdir())) == 1  # no second backup


def test_a_failed_check_report_never_implies_approval(tmp_path: Path) -> None:
    dest = tmp_path / "approved"
    with pytest.raises(pub.PublicationRefusedError):
        _publish(ParquetTransactionalSink, dest, "op-bad", checks=(pub.CheckResult("x", "fail"),))
    assert not dest.exists()
    assert (
        pub.lookup_committed(pub.PublicationRequest(destination=dest, operation_id="op-bad"))
        is None
    )
