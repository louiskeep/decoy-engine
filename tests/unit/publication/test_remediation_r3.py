"""F22 r3: every public entrypoint goes through ONE admission + acceptance authority.

Entry points covered: module-level lookup_committed/reconcile, the sink's lookup_committed/
reconcile/commit/write, reconcile_managed, restore_backup, and the child-envelope promotion.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution._transactional_sink import ManagedDatasetSink, ParquetTransactionalSink
from decoy_engine.publication import _managed
from decoy_engine.publication import _protocol as proto
from tests.unit.publication import _support as sup

IMMUTABLE, MANAGED = pub.UNIT_IMMUTABLE_DIRECTORY, pub.UNIT_MANAGED_DATASET
PASS = (pub.CheckResult("fk", "pass"),)
KW = dict(validation_policy_fingerprint="policy-a", checks=PASS)


def _cls(unit: str):  # type: ignore[no-untyped-def]
    return ManagedDatasetSink if unit == MANAGED else ParquetTransactionalSink


def _publish(
    unit: str, dest: Path, op: str = "op-1", ids: int = 0, **kw: object
) -> pub.CommitReceipt:
    merged = {**KW, **kw}
    request = pub.PublicationRequest(destination=dest, unit=unit, operation_id=op, **merged)  # type: ignore[arg-type]
    sink = _cls(unit)(dest, request=request)
    sink.write("alpha", sup.table_a(extra=ids))
    return sink.commit()


def _entrypoints(request: pub.PublicationRequest) -> dict[str, Callable[[], object]]:
    cls = _cls(request.unit)
    dest = request.destination

    def sink_write() -> object:
        return cls(dest, request=request).write("alpha", sup.table_a())

    return {
        "module_lookup": lambda: pub.lookup_committed(request),
        "module_reconcile": lambda: pub.reconcile(request),
        "sink_lookup": lambda: cls(dest, request=request).lookup_committed(),
        "sink_reconcile": lambda: cls(dest, request=request).reconcile(),
        "sink_commit": lambda: cls(dest, request=request).commit(),
        "sink_write": sink_write,
    }


def _leftovers(unit: str, dest: Path) -> list[str]:
    root = dest / ".staging" if unit == MANAGED else dest.parent
    return sorted(p.name for p in root.glob("_decoy_stage_*")) if root.exists() else []


NAMES = [
    "module_lookup",
    "module_reconcile",
    "sink_lookup",
    "sink_reconcile",
    "sink_commit",
    "sink_write",
]


# --------------------------------------------------------------- Replace against occupied immutable


@pytest.mark.parametrize("entry", NAMES)
def test_replace_against_an_occupied_immutable_target_is_refused_everywhere(
    tmp_path: Path, entry: str
) -> None:
    dest = tmp_path / "out"
    first = _publish(IMMUTABLE, dest)
    before = sup.tree_digest(dest)
    request = pub.PublicationRequest(
        destination=dest,
        operation_id="op-1",
        expected_result_id=first.result_id,
        overwrite=pub.Replace(first.descriptor_digest),
        **KW,  # type: ignore[arg-type]
    )
    with pytest.raises(pub.PublicationRefusedError) as exc:
        _entrypoints(request)[entry]()
    assert (exc.value.receipt.outcome, exc.value.receipt.reason_code) == (
        "unsupported",
        "replace_unsupported_for_unit",
    )
    assert sup.tree_digest(dest) == before and _leftovers(IMMUTABLE, dest) == []


# --------------------------------------------------------------- acceptance context, every entrypoint


@pytest.mark.parametrize("unit", [IMMUTABLE, MANAGED])
@pytest.mark.parametrize("entry", NAMES)
@pytest.mark.parametrize(
    ("override", "reason"),
    [
        (dict(validation_policy_fingerprint="policy-b"), "acceptance_context_differs"),
        (dict(checks=(pub.CheckResult("fk", "fail"),)), "required_check_failed"),
        (dict(checks=(pub.CheckResult("fk", "inconclusive"),)), "required_check_failed"),
    ],
    ids=["policy", "failed", "inconclusive"],
)
def test_changed_context_or_failed_check_is_refused_by_every_entrypoint(
    tmp_path: Path, unit: str, entry: str, override: dict[str, object], reason: str
) -> None:
    dest = tmp_path / "out"
    first = _publish(unit, dest)
    before = sup.tree_digest(dest)
    request = pub.PublicationRequest(
        destination=dest,
        unit=unit,
        operation_id="op-1",
        expected_result_id=first.result_id,
        **{**KW, **override},  # type: ignore[arg-type]
    )
    if entry == "sink_write" and reason == "acceptance_context_differs":
        pytest.skip("a different policy is a legitimate new attempt at write time")
    with pytest.raises(pub.PublicationRefusedError) as exc:
        _entrypoints(request)[entry]()
    assert exc.value.receipt.reason_code == reason
    assert sup.tree_digest(dest) == before and _leftovers(unit, dest) == []


@pytest.mark.parametrize("unit", [IMMUTABLE, MANAGED])
@pytest.mark.parametrize(
    "entry", ["module_lookup", "module_reconcile", "sink_lookup", "sink_reconcile", "sink_commit"]
)
def test_matching_context_recovers_through_every_entrypoint(
    tmp_path: Path, unit: str, entry: str
) -> None:
    dest = tmp_path / "out"
    first = _publish(unit, dest)
    request = pub.PublicationRequest(
        destination=dest,
        unit=unit,
        operation_id="op-1",
        expected_result_id=first.result_id,
        **KW,  # type: ignore[arg-type]
    )
    got = _entrypoints(request)[entry]()
    assert got is not None and got.outcome == "already_committed"  # type: ignore[attr-defined]
    assert got.descriptor_digest == first.descriptor_digest  # type: ignore[attr-defined]


# --------------------------------------------------------------- sibling 2: reconcile == lookup


@pytest.mark.parametrize("unit", [IMMUTABLE, MANAGED])
def test_same_operation_with_a_different_expected_result_is_none_from_both(
    tmp_path: Path, unit: str
) -> None:
    dest = tmp_path / "out"
    _publish(unit, dest)
    request = pub.PublicationRequest(
        destination=dest,
        unit=unit,
        operation_id="op-1",
        expected_result_id="0" * 64,
        **KW,  # type: ignore[arg-type]
    )
    assert pub.lookup_committed(request) is None
    assert pub.reconcile(request) is None


@pytest.mark.parametrize("unit", [IMMUTABLE, MANAGED])
def test_reconcile_is_exactly_lookup_under_the_lock(
    tmp_path: Path, unit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out"
    first = _publish(unit, dest)
    request = pub.PublicationRequest(
        destination=dest,
        unit=unit,
        operation_id="someone-else",
        expected_result_id=first.result_id,
        **KW,  # type: ignore[arg-type]
    )
    a, b = pub.lookup_committed(request), pub.reconcile(request)
    assert a is not None and b is not None and a.descriptor_digest == b.descriptor_digest


def test_reconcile_managed_applies_the_same_gate(tmp_path: Path) -> None:
    dest = tmp_path / "ds"
    first = _publish(MANAGED, dest)
    bad = pub.PublicationRequest(
        destination=dest,
        unit=MANAGED,
        operation_id="op-1",
        expected_result_id=first.result_id,
        validation_policy_fingerprint="policy-b",
        checks=PASS,
    )
    with pytest.raises(pub.PublicationRefusedError) as exc:
        _managed.reconcile_managed(bad)
    assert exc.value.receipt.reason_code == "acceptance_context_differs"


# --------------------------------------------------------------- sibling 3: latching + validate


@pytest.mark.parametrize("entry", ["sink_lookup", "sink_reconcile"])
def test_a_refused_sink_lookup_or_reconcile_latches_the_attempt(tmp_path: Path, entry: str) -> None:
    dest = tmp_path / "out"
    first = _publish(IMMUTABLE, dest)
    request = pub.PublicationRequest(
        destination=dest,
        operation_id="op-1",
        expected_result_id=first.result_id,
        validation_policy_fingerprint="policy-b",
        checks=PASS,
    )
    sink = ParquetTransactionalSink(dest, request=request)
    call = sink.lookup_committed if entry == "sink_lookup" else sink.reconcile
    with pytest.raises(pub.PublicationRefusedError):
        call()
    with pytest.raises(pub.PublicationRefusedError) as again:
        sink.commit()
    assert again.value.receipt.reason_code == "attempt_rejected"  # latched, consistently


# --------------------------------------------------------------- sibling 4: managed lineage + backup


def test_managed_already_committed_requires_the_authorized_predecessor(tmp_path: Path) -> None:
    dest = tmp_path / "ds"
    a = _publish(MANAGED, dest, "op-A")
    b = _publish(MANAGED, dest, "op-B", ids=1, overwrite=pub.Replace(a.descriptor_digest))
    wrong = pub.PublicationRequest(
        destination=dest,
        unit=MANAGED,
        operation_id="op-B",
        overwrite=pub.Replace("0" * 64),
        **KW,  # type: ignore[arg-type]
    )
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.lookup_committed(wrong)
    assert exc.value.receipt.reason_code == "stale_predecessor"
    needs_backup = pub.PublicationRequest(
        destination=dest,
        unit=MANAGED,
        operation_id="op-B",
        overwrite=pub.Replace(a.descriptor_digest, pub.ProtectedBackup("pol")),
        **KW,  # type: ignore[arg-type]
    )
    with pytest.raises(pub.PublicationRefusedError) as exc2:
        pub.lookup_committed(needs_backup)
    assert exc2.value.receipt.reason_code == "backup_missing"
    ok = pub.PublicationRequest(
        destination=dest,
        unit=MANAGED,
        operation_id="op-B",
        overwrite=pub.Replace(a.descriptor_digest),
        **KW,  # type: ignore[arg-type]
    )
    got = pub.lookup_committed(ok)
    assert got is not None and got.descriptor_digest == b.descriptor_digest


# --------------------------------------------------------------- restore_backup + facade gating


def test_restore_backup_is_gated_by_the_admission_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "ds"
    a = _publish(MANAGED, dest, "op-A")
    b = _publish(
        MANAGED,
        dest,
        "op-B",
        ids=1,
        overwrite=pub.Replace(a.descriptor_digest, pub.ProtectedBackup("pol")),
    )
    seen: list[str] = []
    real = proto.admit_request
    monkeypatch.setattr(
        proto, "admit_request", lambda r, s=None: (seen.append(r.operation_id), real(r, s))[1]
    )
    restored = pub.restore_backup(
        dest, a.descriptor_digest, expected_current_digest=b.descriptor_digest, operation_id="op-R"
    )
    assert restored.outcome == "published" and "op-R" in seen


def test_every_facade_request_op_runs_admission_first(tmp_path: Path) -> None:
    bad = pub.PublicationRequest(destination=tmp_path / "x", unit="object_store/v1")
    stage = tmp_path / "stage"
    stage.mkdir()
    pq.write_table(sup.table_a(), stage / "alpha.parquet")
    prepared = pub.prepare_directory(stage, pub.PublicationRequest(destination=tmp_path / "ok"))
    from dataclasses import replace

    forged = replace(prepared, request=bad)
    calls = {
        "lookup_committed": lambda: pub.lookup_committed(bad),
        "reconcile": lambda: pub.reconcile(bad),
        "validate_request": lambda: pub.validate_request(bad),
        "prepare_directory": lambda: pub.prepare_directory(stage, bad),
        "commit_prepared": lambda: pub.commit_prepared(forged),
    }
    for name, call in calls.items():
        with pytest.raises(pub.PublicationRefusedError) as exc:
            call()
        assert exc.value.receipt.reason_code == "unknown_publication_unit", name
    assert not (tmp_path / "ok").exists()


# --------------------------------------------------------------- D. child envelope verify-before-read


def _prep(tmp_path: Path) -> Path:
    d = tmp_path / "assigned"
    d.mkdir()
    pq.write_table(pa.table({"id": [1, 2]}), d / "t.parquet")
    return d


def _env(d: Path, **over: object) -> dict[str, object]:
    prep = {
        "schema": "decoy-child-preparation/v1",
        "staging_dir": str(d),
        "tables": ["t"],
        "row_counts": {"t": 2},
        **over,
    }
    return {
        "outcome": "completed",
        "peak_rss_mb": 1.0,
        "preparation": prep,
        "quality_metrics": {},
        "table_kinds": {},
    }


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d: (d / "t.parquet").unlink(),
        lambda d: pq.write_table(pa.table({"id": [9]}), d / "extra.parquet"),
        lambda d: (d / "t.parquet").write_bytes(b"not parquet"),
        lambda d: (d / "t.parquet").unlink() or (d / "t.parquet").symlink_to(d / "elsewhere"),
    ],
    ids=["missing", "extra", "corrupt", "symlink"],
)
def test_child_envelope_defects_are_refused_before_any_read(tmp_path: Path, tamper) -> None:  # type: ignore[no-untyped-def]
    from decoy_engine.execution._isolated_run import _result_from_envelope

    assigned = _prep(tmp_path)
    tamper(assigned)
    before = sorted(p.name for p in assigned.iterdir())
    out = tmp_path / "final"
    result = _result_from_envelope(_env(assigned), out, 1, assigned)  # must not raise
    assert (
        result.outcome == "crashed"
        and result.publication_receipt.reason_code == "child_envelope_invalid"
    )  # type: ignore[union-attr]
    assert not out.exists() and sorted(p.name for p in assigned.iterdir()) == before


def test_child_envelope_row_mismatch_and_key_mismatch_are_refused(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import _result_from_envelope

    for over in (dict(row_counts={"t": 99}), dict(row_counts={})):
        assigned = _prep(tmp_path / "x") if (tmp_path / "x").exists() else None
        base = tmp_path / f"case{len(list(tmp_path.iterdir()))}"
        base.mkdir()
        assigned = _prep(base)
        out = base / "final"
        result = _result_from_envelope(_env(assigned, **over), out, 1, assigned)
        assert (
            result.outcome == "crashed"
            and result.publication_receipt.reason_code == "child_envelope_invalid"
        )  # type: ignore[union-attr]
        assert not out.exists() and (assigned / "t.parquet").exists()
