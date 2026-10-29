"""F22 remediation r2: regressions for the three Codex HIGH findings (fail on the pre-fix build)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import _transactional_sink as ts
from decoy_engine.execution._transactional_sink import ManagedDatasetSink, ParquetTransactionalSink
from decoy_engine.publication import _protocol
from tests.unit.publication import _support as sup

IMMUTABLE, MANAGED = pub.UNIT_IMMUTABLE_DIRECTORY, pub.UNIT_MANAGED_DATASET
PASS = (pub.CheckResult("fk", "pass"),)


def _sink(unit: str, dest: Path, **kw: object):  # type: ignore[no-untyped-def]
    cls = ManagedDatasetSink if unit == MANAGED else ParquetTransactionalSink
    return cls(dest, request=pub.PublicationRequest(destination=dest, unit=unit, **kw))  # type: ignore[arg-type]


def _staging(unit: str, dest: Path) -> list[str]:
    root = dest / ".staging" if unit == MANAGED else dest.parent
    pattern = "_decoy_stage_*"
    return sorted(p.name for p in root.glob(pattern)) if root.exists() else []


def _publish(unit: str, dest: Path, **kw: object) -> pub.CommitReceipt:
    sink = _sink(unit, dest, **kw)
    sink.write("alpha", sup.table_a())
    return sink.commit()


# ------------------------------------------------------------------ HIGH 1


@pytest.mark.parametrize("unit", [IMMUTABLE, MANAGED])
@pytest.mark.parametrize(
    ("retry", "outcome", "reason"),
    [
        (
            dict(validation_policy_fingerprint="policy-b", checks=PASS),
            "conflict",
            "acceptance_context_differs",
        ),
        (
            dict(validation_policy_fingerprint="policy-a", checks=(pub.CheckResult("fk", "fail"),)),
            "rejected",
            "required_check_failed",
        ),
        (
            dict(
                validation_policy_fingerprint="policy-a",
                checks=(pub.CheckResult("fk", "inconclusive"),),
            ),
            "rejected",
            "required_check_failed",
        ),
        (
            dict(validation_policy_fingerprint="policy-a", checks=()),
            "conflict",
            "acceptance_context_differs",
        ),
    ],
    ids=["policy_changed", "required_failed", "required_inconclusive", "checks_dropped"],
)
def test_h1_expected_identity_recovery_requires_the_same_acceptance_context(
    tmp_path: Path, unit: str, retry: dict[str, object], outcome: str, reason: str
) -> None:
    dest = tmp_path / "out"
    first = _publish(
        unit, dest, operation_id="op-1", validation_policy_fingerprint="policy-a", checks=PASS
    )
    before = sup.tree_digest(dest)

    request = pub.PublicationRequest(
        destination=dest,
        unit=unit,
        operation_id="op-1",
        expected_result_id=first.result_id,
        **retry,  # type: ignore[arg-type]
    )
    for attempt in (
        lambda: pub.lookup_committed(request),
        lambda: (_sink_for(request)).commit(),
    ):
        with pytest.raises(pub.PublicationRefusedError) as exc:
            attempt()
        assert (exc.value.receipt.outcome, exc.value.receipt.reason_code) == (outcome, reason)
    assert sup.tree_digest(dest) == before  # no destination change
    assert _staging(unit, dest) == []  # and nothing staged


def _sink_for(request: pub.PublicationRequest):  # type: ignore[no-untyped-def]
    cls = ManagedDatasetSink if request.unit == MANAGED else ParquetTransactionalSink
    return cls(request.destination, request=request)


@pytest.mark.parametrize("unit", [IMMUTABLE, MANAGED])
def test_h1_matching_context_still_recovers(tmp_path: Path, unit: str) -> None:
    dest = tmp_path / "out"
    kw = dict(validation_policy_fingerprint="policy-a", checks=PASS)
    first = _publish(unit, dest, operation_id="op-1", **kw)
    request = pub.PublicationRequest(
        destination=dest,
        unit=unit,
        operation_id="op-1",
        expected_result_id=first.result_id,
        **kw,  # type: ignore[arg-type]
    )
    again = _sink_for(request).commit()
    assert (
        again.outcome == "already_committed" and again.descriptor_digest == first.descriptor_digest
    )


def test_h1_a_historical_operation_retry_also_checks_context(tmp_path: Path) -> None:
    dest = tmp_path / "ds"
    kw = dict(validation_policy_fingerprint="policy-a", checks=PASS)
    a = _publish(MANAGED, dest, operation_id="op-A", **kw)
    _publish(MANAGED, dest, operation_id="op-B", overwrite=pub.Replace(a.descriptor_digest), **kw)
    request = pub.PublicationRequest(
        destination=dest,
        unit=MANAGED,
        operation_id="op-A",
        validation_policy_fingerprint="other",
        checks=PASS,
    )
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.lookup_committed(request)
    assert exc.value.receipt.reason_code == "acceptance_context_differs"


# ------------------------------------------------------------------ HIGH 2


def test_h2_replace_on_an_immutable_directory_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[object] = []
    real = pq.write_table
    monkeypatch.setattr(ts.pq, "write_table", lambda *a, **k: (writes.append(a), real(*a, **k)))
    dest = tmp_path / "out"
    sink = _sink(IMMUTABLE, dest, overwrite=pub.Replace("0" * 64))
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.write("alpha", sup.table_a())
    assert (exc.value.receipt.outcome, exc.value.receipt.reason_code) == (
        "unsupported",
        "replace_unsupported_for_unit",
    )
    assert writes == [] and _staging(IMMUTABLE, dest) == [] and not dest.exists()
    with pytest.raises(pub.PublicationRefusedError) as again:  # the no-write commit path too
        _sink(IMMUTABLE, dest, overwrite=pub.Replace("0" * 64)).commit()
    assert again.value.receipt.reason_code == "replace_unsupported_for_unit"
    assert not dest.exists() and _staging(IMMUTABLE, dest) == []


def test_h2_the_backend_itself_refuses_replace_on_the_immutable_unit(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    pq.write_table(sup.table_a(), stage / "alpha.parquet")
    request = pub.PublicationRequest(destination=tmp_path / "out", overwrite=pub.Replace("0" * 64))
    prepared = pub.prepare_directory(stage, request)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.commit_prepared(prepared)
    assert exc.value.receipt.reason_code == "replace_unsupported_for_unit"
    assert not (tmp_path / "out").exists()


def test_h2_the_sink_enforces_its_own_declaration_not_just_the_tests(tmp_path: Path) -> None:
    class NoBackupManaged(ManagedDatasetSink):
        protocol_declaration = replace(_protocol.MANAGED_DATASET, supports_backup=False)

    dest = tmp_path / "ds"
    first = _publish(MANAGED, dest, operation_id="op-A")
    request = pub.PublicationRequest(
        destination=dest,
        unit=MANAGED,
        overwrite=pub.Replace(first.descriptor_digest, pub.ProtectedBackup("p")),
    )
    with pytest.raises(pub.PublicationRefusedError) as exc:
        NoBackupManaged(dest, request=request).write("alpha", sup.table_a())
    assert exc.value.receipt.reason_code == "backup_unsupported"
    assert _staging(MANAGED, dest) == []


# ------------------------------------------------------------------ HIGH 3


def _staged(root: Path, name: str, ids: list[int]) -> Path:
    d = root / name
    d.mkdir()
    pq.write_table(pa.table({"id": ids}), d / "t.parquet")
    return d


def _completed_envelope(prep_dir: Path, *, rows: int = 2) -> dict[str, object]:
    return {
        "outcome": "completed",
        "peak_rss_mb": 1.0,
        "staging_output_dir": str(prep_dir),  # the legacy field the pre-fix parent trusted
        "staged_tables": ["t"],
        "row_counts": {"t": rows},
        "preparation": {
            "schema": "decoy-child-preparation/v1",
            "staging_dir": str(prep_dir),
            "tables": ["t"],
            "row_counts": {"t": rows},
        },
        "quality_metrics": {},
        "table_kinds": {},
    }


def test_h3_a_substituted_child_path_is_refused_and_nothing_is_published(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import _result_from_envelope

    assigned = _staged(tmp_path, "assigned", [1, 2])
    other = _staged(tmp_path, "other", [7, 8])
    out = tmp_path / "final"
    result = _result_from_envelope(_completed_envelope(other), out, 1, assigned)
    assert result.outcome == "crashed" and result.committed_output_dir is None
    assert result.publication_receipt is not None
    assert result.publication_receipt.reason_code == "child_envelope_invalid"
    assert not out.exists()
    assert (assigned / "t.parquet").exists() and (other / "t.parquet").exists()  # both preserved


def test_h3_a_valid_envelope_for_the_assigned_dir_is_promoted(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import _result_from_envelope

    assigned = _staged(tmp_path, "assigned", [1, 2])
    out = tmp_path / "final"
    result = _result_from_envelope(_completed_envelope(assigned), out, 1, assigned)
    assert result.outcome == "completed" and result.publication_receipt.outcome == "published"  # type: ignore[union-attr]
    sup.oracle(out)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p, d: p.update(schema="decoy-child-preparation/v2"),
        lambda p, d: p.update(tables=["../t"]),
        lambda p, d: p.update(tables=["t", "t"]),
        lambda p, d: p.update(row_counts={"t": -1}),
        lambda p, d: p.update(row_counts={"ghost": 1}),
        lambda p, d: p.update(row_counts={"t": 99}),  # footer says 2
        lambda p, d: p.pop("schema"),
    ],
    ids=[
        "version",
        "traversal",
        "duplicate",
        "negative",
        "unknown_table",
        "count_lie",
        "no_version",
    ],
)
def test_h3_malformed_or_lying_preparation_documents_are_refused(tmp_path: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    from decoy_engine.execution._isolated_run import _result_from_envelope

    assigned = _staged(tmp_path, "assigned", [1, 2])
    env = _completed_envelope(assigned)
    mutate(env["preparation"], env)
    out = tmp_path / "final"
    result = _result_from_envelope(env, out, 1, assigned)
    assert result.outcome == "crashed" and not out.exists()
    assert result.publication_receipt.reason_code in (
        "child_envelope_invalid",
        "row_count_mismatch",
    )  # type: ignore[union-attr]


def test_h3_promotion_accepts_only_the_validated_envelope_type(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_commit import atomic_commit

    assigned = _staged(tmp_path, "assigned", [1, 2])
    for bad in (assigned, {"staging_dir": str(assigned)}, str(assigned)):
        with pytest.raises(TypeError):
            atomic_commit(bad, tmp_path / "final")  # type: ignore[arg-type]
    assert not (tmp_path / "final").exists()


def test_h3_the_real_child_returns_a_versioned_preparation_document(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import run_pipeline_isolated
    from tests.unit.execution import _r3_scenarios as sc

    cfg, tables = sc.two_tables(tmp_path)
    out = tmp_path / "final"
    result = run_pipeline_isolated(cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=out)
    assert result.outcome == "completed" and result.isolated
    assert result.publication_receipt.outcome == "published"  # type: ignore[union-attr]
    sup.oracle(out)
