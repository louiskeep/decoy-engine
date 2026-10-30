"""F22 r5 HIGH 4: receipts, refusals, logs and the isolated result carry no values.

Three seeded markers stand in for what must never leave the process through a persisted
receipt or refusal: a path component (PATH), OS error text (ERR) and a table name (TBL).
Every surface is serialized the way a platform would persist it, then searched.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import logging
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import _isolated_run as run
from decoy_engine.execution._isolated_commit import PublishContext
from decoy_engine.execution._transactional_sink import ManagedDatasetSink, ParquetTransactionalSink
from decoy_engine.publication import _local_directory as ld
from decoy_engine.publication import _managed as mg
from tests.unit.execution import _r3_scenarios as sc
from tests.unit.execution.test_isolated_run import _install_fake_worker
from tests.unit.publication import _support as sup

PATH = "PATHMARK_7f3a"
ERR = "ERRTEXT_91c2"
TBL = "TBLMARK_55d0"
MARKERS = (PATH, ERR, TBL)
TOKEN = re.compile(r"^[a-z_]+(:[A-Za-z0-9_]+)?$")


def _os_error() -> OSError:
    return OSError(errno.ENOSPC, f"{ERR} no space left at /srv/{PATH}/{TBL}")


def _receipt_json(receipt: pub.CommitReceipt | None) -> str:
    return "null" if receipt is None else json.dumps(dataclasses.asdict(receipt), default=str)


def _refusal_json(exc: BaseException) -> str:
    receipt = getattr(exc, "receipt", None)
    return json.dumps(
        {"type": type(exc).__name__, "message": str(exc), "receipt": _receipt_json(receipt)}
    )


def _result_json(result: Any) -> str:
    return json.dumps(
        {
            "outcome": result.outcome,
            "error": result.error,
            "receipt": _receipt_json(result.publication_receipt),
            "evidence": repr(result.memory_evidence),
        }
    )


def _clean(*surfaces: str, extra: tuple[str, ...] = ()) -> None:
    for text in surfaces:
        for marker in (*MARKERS, *extra):
            assert marker not in text, f"{marker!r} leaked into {text[:400]!r}"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / PATH
    base.mkdir()
    return base


@pytest.fixture(autouse=True)
def _all_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level(logging.DEBUG)
    yield


def _tables() -> dict[str, pa.Table]:
    return {f"{TBL}_orders": sup.table_a(), f"{TBL}_items": sup.table_b()}


def _publish(dest: Path, **kw: Any) -> tuple[ParquetTransactionalSink, pub.CommitReceipt]:
    sink = ParquetTransactionalSink(
        dest, request=pub.PublicationRequest(destination=dest, operation_id="op-1", **kw)
    )
    for name, table in _tables().items():
        sink.write(name, table)
    return sink, sink.commit()


def _refused(call: Callable[[], Any]) -> pub.CommitError:
    with pytest.raises(pub.CommitError) as exc:
        call()
    return exc.value


# ---------------------------------------------- receipts: structure


def test_a_receipt_has_no_destination_path_and_no_artifact_paths(root: Path) -> None:
    _, receipt = _publish(root / "out")
    assert "destination" not in {f.name for f in dataclasses.fields(receipt)}
    for art in receipt.artifacts:
        assert {f.name for f in dataclasses.fields(art)}.isdisjoint({"path", "name"})
        assert art.role and art.sha256 and art.size > 0
    _clean(_receipt_json(receipt))
    sup.oracle(root / "out")  # the committed descriptor still binds the real artifact paths


def test_every_receipt_outcome_is_value_free(root: Path) -> None:
    dest = root / "out"
    _, first = _publish(dest)
    again_sink = ParquetTransactionalSink(
        dest, request=pub.PublicationRequest(destination=dest, operation_id="op-1")
    )
    for name, table in _tables().items():
        again_sink.write(name, table)
    again = again_sink.commit()
    assert again.outcome == "already_committed"
    other = root / "other"
    other.mkdir()
    (other / "legacy.bin").write_bytes(b"x")
    occupied = ParquetTransactionalSink(
        other, request=pub.PublicationRequest(destination=other, operation_id="op-2")
    )
    occupied.write(f"{TBL}_orders", sup.table_a())
    conflict = _refused(occupied.commit)
    _clean(_receipt_json(first), _receipt_json(again), _refusal_json(conflict))
    assert conflict.receipt.reason_code == "target_occupied_legacy"  # type: ignore[union-attr]


# ---------------------------------------------- refusals: OS text becomes tokens


def _failing_replace(match: Callable[[Path, Path], bool]) -> Callable[[Any, Any], None]:
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if match(Path(src), Path(dst)):
            raise _os_error()
        real(src, dst)

    return replace


def test_a_failed_rename_is_an_errno_name_token_not_os_text(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    dest = root / "out"
    monkeypatch.setattr(
        ld.os, "replace", _failing_replace(lambda s, d: s.name.startswith("_decoy_stage_"))
    )
    sink = ParquetTransactionalSink(
        dest, request=pub.PublicationRequest(destination=dest, operation_id="op-1")
    )
    sink.write(f"{TBL}_orders", sup.table_a())
    exc = _refused(sink.commit)
    assert exc.receipt.reason_code == "rename_failed:ENOSPC"  # type: ignore[union-attr]
    _clean(_refusal_json(exc), caplog.text)


@pytest.mark.parametrize(
    ("phase", "code"),
    [
        (lambda s, d: mg.GENERATIONS in d.parts, "promote_failed:ENOSPC"),
        (lambda s, d: d.name == mg.CURRENT_NAME, "switch_failed:ENOSPC"),
    ],
    ids=["promote", "switch"],
)
def test_managed_refusals_carry_errno_name_and_type_tokens_not_os_text(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: Callable[[Path, Path], bool],
    code: str,
) -> None:
    dest = root / "managed"
    monkeypatch.setattr(mg.os, "replace", _failing_replace(phase))
    request = pub.PublicationRequest(
        destination=dest, operation_id="op-1", unit=pub.UNIT_MANAGED_DATASET
    )
    sink = ManagedDatasetSink(dest, request=request)
    sink.write(f"{TBL}_orders", sup.table_a())
    exc = _refused(sink.commit)
    assert exc.receipt.reason_code == code  # type: ignore[union-attr]
    assert "ENOSPC" in str(exc) and "OSError" in str(exc)
    _clean(_refusal_json(exc), caplog.text)


def test_a_sync_failure_after_the_bind_is_a_token_not_os_text(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    dest = root / "out"
    real = ld.fsync_dir

    def fsync(p: Any) -> None:
        if Path(p) == dest.parent:
            raise _os_error()
        real(p)

    monkeypatch.setattr(ld, "fsync_dir", fsync)
    sink = ParquetTransactionalSink(
        dest, request=pub.PublicationRequest(destination=dest, operation_id="op-1")
    )
    sink.write(f"{TBL}_orders", sup.table_a())
    exc = _refused(sink.commit)
    assert isinstance(exc, pub.CommitUncertainError)
    _clean(_refusal_json(exc), caplog.text)


def test_an_unavailable_lock_is_a_token_not_os_text(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from decoy_engine.publication import _lock

    def broken_open(*_a: Any, **_k: Any) -> int:
        raise _os_error()

    monkeypatch.setattr(_lock.os, "open", broken_open)
    dest = root / "out"
    request = pub.PublicationRequest(destination=dest, operation_id="op-1")
    exc = _refused(lambda: pub.validate_request(request))
    assert exc.receipt.reason_code == "lock_unavailable"  # type: ignore[union-attr]
    _clean(_refusal_json(exc), str(exc.__cause__), caplog.text)


def test_a_corrupt_descriptor_names_a_stable_reason_not_decoded_text(root: Path) -> None:
    dest = root / "out"
    _publish(dest)
    (dest / "_decoy_publication.json").write_text(f'{{"{TBL}": "{ERR}",')
    sink = ParquetTransactionalSink(
        dest, request=pub.PublicationRequest(destination=dest, operation_id="op-9")
    )
    sink.write(f"{TBL}_orders", sup.table_a())
    exc = _refused(sink.commit)
    code = exc.receipt.reason_code  # type: ignore[union-attr]
    assert code == "target_corrupt:descriptor_invalid" and TOKEN.match(code)
    _clean(_refusal_json(exc))


# ---------------------------------------------- child envelope: names never echoed


@pytest.mark.parametrize(
    "doc_over",
    [
        {"tables": [f".{TBL}"], "row_counts": {f".{TBL}": 1}},  # unsafe (hidden) name
        {"tables": [f"{TBL}/x"], "row_counts": {f"{TBL}/x": 1}},  # path separator
        {"tables": ["t", "t"], "row_counts": {"t": 1}},  # duplicate
        {"tables": ["t"], "row_counts": {f"{TBL}_ghost": 1}},  # undeclared count
        {"tables": [f"{TBL}_t"], "row_counts": {f"{TBL}_t": -1}},  # invalid count
        {"tables": [f"{TBL}_t"], "row_counts": {f"{TBL}_t": True}},
        {"schema": f"{TBL}/v9"},  # unknown version
        {"staging_dir": f"/elsewhere/{PATH}"},  # foreign directory
    ],
    ids=["hidden", "sep", "dup", "undeclared", "negative", "bool", "version", "foreign"],
)
def test_child_envelope_defects_never_echo_child_controlled_text(
    root: Path, doc_over: dict[str, Any]
) -> None:
    assigned = root / "assigned"
    assigned.mkdir()
    doc = {
        "schema": "decoy-child-preparation/v1",
        "staging_dir": str(assigned),
        "tables": ["t"],
        "row_counts": {"t": 1},
        **doc_over,
    }
    with pytest.raises(pub.ChildEnvelopeError) as err:
        pub.parse_child_envelope(doc, assigned)
    assert TOKEN.match(str(err.value)), str(err.value)
    _clean(str(err.value), repr(err.value))
    env = {"outcome": "completed", "peak_rss_mb": 1.0, "preparation": doc}
    result = run._result_from_envelope(env, root / "final", 1, assigned, PublishContext())
    assert result.publication_receipt.reason_code == "child_envelope_invalid"  # type: ignore[union-attr]
    _clean(_result_json(result))


def test_a_refusal_raised_from_a_bad_child_claim_is_value_free(root: Path) -> None:
    assigned = root / "assigned"
    assigned.mkdir()
    doc = {
        "schema": "decoy-child-preparation/v1",
        "staging_dir": str(assigned),
        "tables": [f".{TBL}"],
        "row_counts": {},
    }
    with pytest.raises(pub.PublicationRefusedError) as exc:
        run._commit({"preparation": doc}, root / "final", PublishContext(), assigned)
    _clean(_refusal_json(exc.value))


def test_the_child_gate_refusals_are_value_free(root: Path) -> None:
    assigned = root / "assigned"
    assigned.mkdir()
    pq.write_table(pa.table({"id": [1]}), assigned / f"{TBL}_t.parquet")
    env = pub.ChildPreparationEnvelope(assigned, (f"{TBL}_t",), ((f"{TBL}_t", 1),))
    wrong = pub.PublicationRequest(
        destination=root / "final", operation_id="op-1", expected_tables=(f"{TBL}_other",)
    )
    exc = _refused(lambda: pub.verify_child_envelope(env, wrong))
    failed = pub.PublicationRequest(
        destination=root / "final",
        operation_id="op-1",
        checks=(pub.CheckResult(f"{TBL}_check", "fail"),),
    )
    exc2 = _refused(lambda: pub.verify_child_envelope(env, failed))
    _clean(_refusal_json(exc), _refusal_json(exc2))


# ---------------------------------------------- the isolated result and logs, end to end


def test_a_real_isolated_run_result_is_value_free_on_every_refusal(
    root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for kw in ({"checks": (pub.CheckResult("fk", "fail"),)}, {"cancel_check": lambda: True}):
        cfg, tables = sc.two_tables(root)
        result = run.run_pipeline_isolated(
            cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=root / "final", **kw
        )
        assert result.publication_receipt is not None
        _clean(_result_json(result), caplog.text)


def test_a_child_envelope_with_seeded_names_is_value_free_through_a_real_spawn(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker = (
        "import json, os, sys\n"
        "from decoy_engine.execution._isolated_common import RESULT_FILENAME\n"
        "payload = json.load(open(sys.argv[1]))\n"
        "stage = payload['staging_output_dir']\n"
        "os.makedirs(stage)\n"
        "doc = {'schema': 'decoy-child-preparation/v1', 'staging_dir': stage,\n"
        f"       'tables': ['.{TBL}'], 'row_counts': {{'.{TBL}': 1}}}}\n"
        "env = {'outcome': 'completed', 'peak_rss_mb': 1.0, 'preparation': doc}\n"
        "json.dump(env, open(os.path.join(os.path.dirname(sys.argv[1]), RESULT_FILENAME), 'w'))\n"
    )
    _install_fake_worker(root, monkeypatch, "r5_names", worker)
    result = run.run_pipeline_isolated(
        {}, None, engine_version="r5", output_dir=root / "final"
    )
    assert result.publication_receipt.reason_code == "child_envelope_invalid"  # type: ignore[union-attr]
    _clean(_result_json(result), caplog.text)


def test_an_in_process_commit_failure_result_is_value_free(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        ld.os, "replace", _failing_replace(lambda s, d: s.name.startswith("_decoy_isolated_stage_"))
    )
    cfg, tables = sc.two_tables(root)
    result = run.run_pipeline_isolated(
        cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=root / "final", isolate=False
    )
    assert result.publication_receipt.reason_code == "rename_failed:ENOSPC"  # type: ignore[union-attr]
    _clean(_result_json(result), caplog.text)


def test_the_published_isolated_result_receipt_is_value_free(
    root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, tables = sc.two_tables(root)
    result = run.run_pipeline_isolated(
        cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=root / "final"
    )
    assert result.publication_receipt.outcome == "published"  # type: ignore[union-attr]
    _clean(_result_json(result), caplog.text)
