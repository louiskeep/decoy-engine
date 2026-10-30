"""F22 complete-unit mode: a required quarantine sidecar is prepared INSIDE the unit and
bound by the descriptor BEFORE commit; the post-commit truths in
`test_quarantine_commit_fate.py` still hold for the default (diagnostic) mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._pipeline import run_pipeline
from tests.integration.test_quarantine_commit_fate import _config, _sources
from tests.unit.publication import _support as sup


def _run(tmp_path: Path, qpath: Path, sink: ParquetTransactionalSink):  # type: ignore[no-untyped-def]
    config = _config(tmp_path, str(qpath))
    return run_pipeline(
        config,
        _sources(config),
        engine_version="0.1.0",
        sink=sink,
        use_byte_estimate_routing=False,
    )


def _unit_sink(target: Path, **kw: object) -> ParquetTransactionalSink:
    return ParquetTransactionalSink(
        target,
        request=pub.PublicationRequest(destination=target, sidecars_in_unit=True, **kw),  # type: ignore[arg-type]
    )


def test_the_quarantine_sidecar_is_bound_by_the_descriptor_before_binding(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(tmp_path, out / "quarantine.jsonl", _unit_sink(out))
    receipt = result.publication_receipt
    assert receipt is not None and receipt.outcome == "published"
    facts = sup.oracle(out)  # every file on disk is bound; sizes and hashes recomputed
    roles = {a["path"]: a["role"] for a in facts["descriptor"]["artifacts"]}
    assert roles["quarantine.jsonl"] == "quarantine"
    records = [json.loads(line) for line in (out / "quarantine.jsonl").read_text().splitlines()]
    assert [r["age"] for r in records] == ["badX"]
    assert result.quality_metrics["quarantine"]["total_quarantined"] == 1
    assert "badX" not in pq.read_table(out / "parent.parquet").column("age").to_pylist()


def test_a_commit_failure_leaves_no_quarantine_anywhere(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "legacy.txt").write_text("occupant")  # forces the commit to be refused
    with pytest.raises(pub.PublicationRefusedError):
        _run(tmp_path, out / "quarantine.jsonl", _unit_sink(out))
    assert [p.name for p in out.iterdir()] == ["legacy.txt"]
    assert sup.staging_dirs(tmp_path) == []  # run_sequential aborted the sink


def test_an_external_sidecar_is_refused_before_any_masking(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(pub.PublicationRefusedError) as exc:
        _run(tmp_path, tmp_path / "elsewhere" / "quarantine.jsonl", _unit_sink(out))
    assert exc.value.receipt.outcome == "unsupported"
    assert exc.value.receipt.reason_code == "sidecar_outside_unit"
    assert not out.exists() and sup.staging_dirs(tmp_path) == []  # nothing staged at all
    assert not (tmp_path / "elsewhere").exists()


def test_a_sidecar_cannot_alias_a_table_artifact(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="plain non-table"):
        _run(tmp_path, out / "parent.parquet", _unit_sink(out))
    assert not out.exists()


def test_identical_retry_of_a_unit_with_a_sidecar_is_already_committed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    first = _run(tmp_path, out / "quarantine.jsonl", _unit_sink(out, operation_id="op-1"))
    before = sup.tree_digest(out)
    again = _run(tmp_path, out / "quarantine.jsonl", _unit_sink(out, operation_id="op-1"))
    assert again.publication_receipt.outcome == "already_committed"  # type: ignore[union-attr]
    assert (
        again.publication_receipt.descriptor_digest == first.publication_receipt.descriptor_digest
    )  # type: ignore[union-attr]
    assert sup.tree_digest(out) == before


def test_tampering_with_the_bound_sidecar_is_detected(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(tmp_path, out / "quarantine.jsonl", _unit_sink(out, operation_id="op-1"))
    (out / "quarantine.jsonl").write_text("{}\n")
    assert pub.inspect_destination(out).kind == "corrupt"


def test_default_mode_keeps_the_post_commit_diagnostic_behaviour(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(tmp_path, out / "quarantine.jsonl", ParquetTransactionalSink(out))
    assert result.publication_receipt.outcome == "published"  # type: ignore[union-attr]
    bound = {a.sha256 for a in result.publication_receipt.artifacts}  # type: ignore[union-attr]
    assert sup.sha(out / "quarantine.jsonl") not in bound and (out / "quarantine.jsonl").exists()
