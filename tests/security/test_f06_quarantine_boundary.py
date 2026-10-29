"""F06 C1-C6: protected quarantine capability, real-filesystem protection, access/retention,
publication failure, leakage across every diagnostic channel, and F29 registry compatibility.

Real temp filesystems, real Fernet; the only injected collaborator is the clock.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import traceback
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.acceptance._f06_support import (
    INT64,
    ISO_DATE,
    PROTECTED_QUARANTINE,
    column,
    config,
    run,
    text_table,
    write_csv,
)

MARKER = "ZXQ-LEAK-7f3a91"
DATE_OUT = {"id": "date/iso8601/v1", "width": 10, "width_unit": "code_points"}


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _kp(byte: bytes = b"q"):
    from decoy_engine.keyprovider import SecretKeyProvider

    return SecretKeyProvider(secret=byte * 32)


def _runtime(root: Path, clock: Clock | None = None, key_provider: Any = None, owner: str = "tenant-1"):
    from decoy_engine.declared_casting import LocalProtectedQuarantineSink, ProtectedQuarantineRuntime

    sink = LocalProtectedQuarantineSink(root, key_provider=key_provider or _kp(), clock=clock or Clock())
    return ProtectedQuarantineRuntime(sink=sink, owner=owner)


def _ctx(owner: str = "tenant-1"):
    from decoy_engine.declared_casting import QuarantineAccessContext

    return QuarantineAccessContext(owner=owner)


def _cfg(tmp_path: Path, **top: Any) -> dict[str, Any]:
    src = write_csv(tmp_path / "people.csv", ["id", "start"], [["1", "2026-02-01"]])
    cols = [
        column("id"),
        column("start", logical_type="date", parse_format=ISO_DATE, error_policy="quarantine", output_format=DATE_OUT),
    ]
    return config(cols, source_path=src, top={"protected_quarantine": PROTECTED_QUARANTINE, **top})


def _quarantine_run(tmp_path: Path, runtime: Any, values: list[str] | None = None) -> Any:
    start = values or ["2026-02-01", f"bad-{MARKER}", "2026-03-05"]
    return run(
        _cfg(tmp_path),
        {"people": text_table(id=[str(i) for i in range(len(start))], start=start)},
        quarantine_runtime=runtime,
    )


def _walk(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*"))


# -- C1 ---------------------------------------------------------------------------------------


def test_c1_quarantine_policy_without_a_sink_fails_before_the_source_is_read(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.profile._readers import LazySource

    cfg = _cfg(tmp_path)
    poisoned = LazySource(path=tmp_path / "missing.parquet")

    def boom(_n: str) -> pa.Table:
        raise AssertionError("source read before capability check")

    with pytest.raises(ExecutionError) as exc:
        run(cfg, {"people": poisoned}, source_loader=boom)
    assert exc.value.code == "protected_quarantine_unavailable"


def test_c1_legacy_quarantine_output_path_does_not_satisfy_the_capability(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    cfg = _cfg(tmp_path, quarantine={"enabled": True, "output_path": str(tmp_path / "legacy.jsonl"), "triggers": ["format_error"]})
    with pytest.raises(ExecutionError) as exc:
        run(cfg, {"people": text_table(id=["1"], start=["bad"])})
    assert exc.value.code == "protected_quarantine_unavailable"
    assert not (tmp_path / "legacy.jsonl").exists()


def test_c1_quarantine_column_without_protected_block_is_a_config_error(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from tests.acceptance._f06_support import validate

    cfg = _cfg(tmp_path)
    del cfg["protected_quarantine"]
    with pytest.raises(ValidationError) as exc:
        validate(cfg)
    assert "declared_cast_contract_invalid" in {e["type"] for e in exc.value.errors()}


@pytest.mark.parametrize(
    "patch",
    [
        {"retention_seconds": 0},
        {"retention_seconds": 31_536_001},
        {"max_rows": 0},
        {"max_bytes": -1},
        {"access_policy": ""},
        {"access_policy": "has space"},
        {"required_protection": "none"},
        {"output_path": "/tmp/x"},
        {"schema_version": 2},
    ],
)
def test_c1_protected_block_is_validated(tmp_path: Path, patch: dict) -> None:
    from pydantic import ValidationError

    from tests.acceptance._f06_support import validate

    cfg = _cfg(tmp_path)
    cfg["protected_quarantine"] = {**PROTECTED_QUARANTINE, **patch}
    with pytest.raises(ValidationError):
        validate(cfg)


def test_c1_seed_only_key_provider_is_rejected_before_source_read(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.keyprovider import SeedKeyProvider
    from decoy_engine.profile._readers import LazySource

    runtime = _runtime(tmp_path / "q", key_provider=SeedKeyProvider(job_seed=b"\x01" * 8))
    with pytest.raises(ExecutionError) as exc:
        run(_cfg(tmp_path), {"people": LazySource(path=tmp_path / "missing.parquet")}, quarantine_runtime=runtime)
    assert exc.value.code == "protected_quarantine_unavailable"


def test_c1_sink_protocol_conformance_and_missing_capability_rejected(tmp_path: Path) -> None:
    from decoy_engine.declared_casting import ProtectedQuarantineRuntime, ProtectedQuarantineSink
    from decoy_engine.execution._errors import ExecutionError

    runtime = _runtime(tmp_path / "q")
    assert isinstance(runtime.sink, ProtectedQuarantineSink)
    for name in ("begin", "write_row", "seal", "abort", "get_retrieval_token", "open", "purge_expired"):
        assert callable(getattr(runtime.sink, name))

    class NoExpiry:  # a sink missing purge_expired / get_retrieval_token is not qualified
        def begin(self, *a: Any, **k: Any) -> Any: ...
        def write_row(self, *a: Any, **k: Any) -> Any: ...
        def seal(self, *a: Any, **k: Any) -> Any: ...
        def abort(self, *a: Any, **k: Any) -> Any: ...
        def open(self, *a: Any, **k: Any) -> Any: ...

    bad = ProtectedQuarantineRuntime(sink=NoExpiry(), owner="tenant-1")
    with pytest.raises(ExecutionError) as exc:
        run(_cfg(tmp_path), {"people": text_table(id=["1"], start=["bad"])}, quarantine_runtime=bad)
    assert exc.value.code == "protected_quarantine_unavailable"


# -- C2 real local protection -----------------------------------------------------------------


def test_c2_modes_ciphertext_and_digest(tmp_path: Path) -> None:
    root = tmp_path / "q"
    runtime = _runtime(root)
    result = _quarantine_run(tmp_path, runtime)
    files = [p for p in _walk(root) if p.is_file()]
    dirs = [p for p in _walk(root) if p.is_dir()] + [root]
    assert files, "a sealed artifact must exist"
    for d in dirs:
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
        assert MARKER.encode() not in f.read_bytes()
        assert b"2026-03-05" not in f.read_bytes()
    receipt = result.quality_metrics["declared_casting"]["quarantine"]
    assert receipt["rows"] == 1 and receipt["protection"] == "encrypted"
    assert MARKER not in json.dumps(result.quality_metrics, default=str)
    records = runtime.sink.open(result.quarantine_access, _ctx())
    assert records[0].row["start"] == f"bad-{MARKER}"
    assert runtime.sink.verify_digest(result.quarantine_access, _ctx()) is True


def test_c2_symlinked_root_and_symlinked_artifact_are_refused(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    runtime = _runtime(link)
    with pytest.raises(ExecutionError) as exc:
        _quarantine_run(tmp_path, runtime)
    assert exc.value.code in {"protected_quarantine_unavailable", "protected_quarantine_write_failed"}
    assert not list(real.rglob("*"))


def test_c2_wrong_key_and_tamper_fail_reads(tmp_path: Path) -> None:
    from decoy_engine.errors import DecoyError

    root = tmp_path / "q"
    runtime = _runtime(root)
    result = _quarantine_run(tmp_path, runtime)
    token = result.quarantine_access
    other = _runtime(root, key_provider=_kp(b"z"))
    with pytest.raises(DecoyError) as exc:
        other.sink.open(token, _ctx())
    assert getattr(exc.value, "code", None) == "protected_quarantine_access_denied"
    artifact = next(p for p in _walk(root) if p.is_file() and p.suffix == ".dqa")
    raw = bytearray(artifact.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    artifact.write_bytes(bytes(raw))
    with pytest.raises(DecoyError) as exc2:
        runtime.sink.open(token, _ctx())
    assert getattr(exc2.value, "code", None) == "protected_quarantine_access_denied"


def test_c2_partial_files_are_not_receipts_and_abort_leaves_nothing(tmp_path: Path) -> None:
    from decoy_engine.declared_casting import QuarantineEnvelope, QuarantinePolicy, QuarantineRun

    root = tmp_path / "q"
    runtime = _runtime(root)
    sink = runtime.sink
    policy = QuarantinePolicy(**PROTECTED_QUARANTINE)
    session = sink.begin(QuarantineRun(owner="tenant-1", run_id="r1"), policy)
    sink.write_row(session, QuarantineEnvelope(table="t", row_position=0, reasons=(), row={"a": f"x{MARKER}"}))
    assert not [p for p in _walk(root) if p.suffix == ".dqa"]  # unsealed: no readable artifact
    sink.abort(session)
    assert not [p for p in _walk(root) if p.is_file()]


def test_c2_budgets_fail_closed(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    cfg = _cfg(tmp_path)
    cfg["protected_quarantine"] = {**PROTECTED_QUARANTINE, "max_rows": 1}
    runtime = _runtime(tmp_path / "q")
    with pytest.raises(ExecutionError) as exc:
        run(cfg, {"people": text_table(id=["1", "2", "3"], start=["a", "b", "2026-01-01"])}, quarantine_runtime=runtime)
    assert exc.value.code == "protected_quarantine_write_failed"
    assert not [p for p in _walk(tmp_path / "q") if p.suffix == ".dqa"]
    cfg["protected_quarantine"] = {**PROTECTED_QUARANTINE, "max_bytes": 64}
    with pytest.raises(ExecutionError) as exc2:
        run(cfg, {"people": text_table(id=["1"], start=["x" * 500])}, quarantine_runtime=_runtime(tmp_path / "q2"))
    assert exc2.value.code == "protected_quarantine_write_failed"


# -- C3 access and retention ------------------------------------------------------------------


def test_c3_token_owner_run_and_key_version_gate_access(tmp_path: Path) -> None:
    from decoy_engine.declared_casting import OpaqueRetrievalToken
    from decoy_engine.errors import DecoyError

    runtime = _runtime(tmp_path / "q")
    result = _quarantine_run(tmp_path, runtime)
    token = result.quarantine_access
    assert isinstance(token, OpaqueRetrievalToken)
    assert len(runtime.sink.open(token, _ctx())) == 1
    wire = token.to_wire()  # portable: a remote tenant-job-owner can present it later
    assert isinstance(wire, str) and MARKER not in wire and str(tmp_path) not in wire
    assert len(runtime.sink.open(OpaqueRetrievalToken.from_wire(wire), _ctx())) == 1
    with pytest.raises(DecoyError) as exc:
        runtime.sink.open(token, _ctx("someone-else"))
    assert exc.value.code == "protected_quarantine_access_denied"
    with pytest.raises(DecoyError) as exc2:
        runtime.sink.open(OpaqueRetrievalToken.from_wire("forged-" + wire), _ctx())
    assert exc2.value.code == "protected_quarantine_access_denied"
    assert wire not in repr(token) and wire not in repr(result)


def test_c3_get_retrieval_token_is_issued_from_the_receipt(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "q")
    result = _quarantine_run(tmp_path, runtime)
    receipt_ref = result.quality_metrics["declared_casting"]["quarantine"]["ref"]
    assert str(tmp_path) not in receipt_ref
    again = runtime.sink.get_retrieval_token(receipt_ref, _ctx())
    assert len(runtime.sink.open(again, _ctx())) == 1


def test_c3_expiry_uses_the_injected_clock_and_purge_removes_only_owned_artifacts(tmp_path: Path) -> None:
    from decoy_engine.errors import DecoyError

    clock = Clock()
    root = tmp_path / "q"
    mine = _runtime(root, clock)
    result = _quarantine_run(tmp_path, mine)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    theirs = _quarantine_run(other_dir, _runtime(root, clock, owner="tenant-2"))
    clock.now += PROTECTED_QUARANTINE["retention_seconds"] - 1
    assert len(mine.sink.open(result.quarantine_access, _ctx())) == 1
    clock.now += 1
    with pytest.raises(DecoyError) as exc:
        mine.sink.open(result.quarantine_access, _ctx())
    assert exc.value.code == "protected_quarantine_expired"
    outcome = mine.sink.purge_expired(clock.now, _ctx())
    assert outcome.purged == 1 and outcome.failed == 0
    remaining = [p for p in _walk(root) if p.suffix == ".dqa"]
    assert len(remaining) == 1  # tenant-2's artifact is not this authority's to purge
    del theirs


# -- C4 publication failure -------------------------------------------------------------------


def test_c4_write_failure_aborts_the_run_and_leaves_no_readable_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.declared_casting import LocalProtectedQuarantineSink
    from decoy_engine.execution._errors import ExecutionError

    runtime = _runtime(tmp_path / "q")

    def fail(self: Any, *a: Any, **k: Any) -> None:
        raise OSError(f"disk full {MARKER}")

    monkeypatch.setattr(LocalProtectedQuarantineSink, "_append_chunk", fail)
    with pytest.raises(ExecutionError) as exc:
        _quarantine_run(tmp_path, runtime)
    assert exc.value.code == "protected_quarantine_write_failed"
    assert MARKER not in str(exc.value) and MARKER not in "".join(traceback.format_exception(exc.value))
    assert not [p for p in _walk(tmp_path / "q") if p.suffix == ".dqa"]


def test_c4_seal_failure_and_cleanup_failure_are_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.declared_casting import LocalProtectedQuarantineSink
    from decoy_engine.execution._errors import ExecutionError

    runtime = _runtime(tmp_path / "q")

    def fail_seal(self: Any, *a: Any, **k: Any) -> None:
        raise OSError("rename failed")

    def fail_cleanup(self: Any, *a: Any, **k: Any) -> None:
        raise OSError("unlink failed")

    monkeypatch.setattr(LocalProtectedQuarantineSink, "_publish_atomic", fail_seal)
    monkeypatch.setattr(LocalProtectedQuarantineSink, "_remove_partial", fail_cleanup)
    with pytest.raises(ExecutionError) as exc:
        _quarantine_run(tmp_path, runtime)
    assert exc.value.code == "protected_quarantine_seal_failed"  # the original failure is not masked
    assert exc.value.cleanup_state == "failed"  # cleanup failure reported as its own outcome


# -- C5 no leakage ----------------------------------------------------------------------------


class _Capture:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


def _blob(*objs: Any) -> str:
    return "\n".join(repr(o) if not isinstance(o, str) else o for o in objs)


@pytest.mark.parametrize(
    "spec",
    [
        ("integer", INT64, f"12{MARKER}"),
        ("date", ISO_DATE, f"2026-{MARKER}"),
        (
            "currency",
            {"id": "currency/explicit/v1", "currency_code": "USD", "token": "$", "token_position": "prefix",
             "sign_style": "leading_minus", "precision": 12, "scale": 2, "radix": ".", "group": ","},
            f"${MARKER}",
        ),
        ("decimal", {"id": "decimal/plain/v1", "precision": 5, "scale": 2}, f"1.{MARKER}"),
        ("integer", INT64, "9" * 40 + MARKER),
    ],
)
@pytest.mark.parametrize("policy", ["fail", "null", "quarantine"])
def test_c5_marker_never_reaches_an_ordinary_channel(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, spec: tuple, policy: str
) -> None:
    from decoy_engine.errors import DecoyError
    from decoy_engine.reason_adapter import capture_failure

    logical, parse_format, bad = spec
    src_dir = tmp_path / f"dir-{MARKER}"
    src_dir.mkdir()
    src = write_csv(src_dir / f"file-{MARKER}.csv", ["id", "v"], [["1", "1"]])
    col = column("v", logical_type=logical, parse_format=parse_format, error_policy=policy)
    top = {"protected_quarantine": PROTECTED_QUARANTINE} if policy == "quarantine" else None
    cfg = config([column("id"), col], source_path=src, top=top)
    progress = _Capture()
    runtime = _runtime(tmp_path / "q") if policy == "quarantine" else None
    kwargs: dict[str, Any] = {"progress": progress, "explain_plan": True}
    if runtime is not None:
        kwargs["quarantine_runtime"] = runtime
    caplog.set_level(logging.DEBUG)
    table = text_table(id=["1", "2"], v=["", bad])
    table = pa.table({"id": ["1", "2"], "v": pa.array([None, bad], pa.string())})
    exc_blob = ""
    result = None
    try:
        result = run(cfg, {"people": table}, **kwargs)
    except DecoyError as exc:
        exc_blob = _blob(str(exc), repr(exc), "".join(traceback.format_exception(exc)), capture_failure(exc))
        evidence = getattr(exc, "declared_casting_counts", None)
        exc_blob += json.dumps(evidence, default=str)
    channels = _blob(
        exc_blob,
        caplog.text,
        [type(e).__name__ + repr(vars(e)) if hasattr(e, "__dict__") else repr(e) for e in progress.events],
        json.dumps(result.quality_metrics, default=str) if result is not None else "",
        repr(result) if result is not None else "",
    )
    # The source path carries a marker too: filenames must stay out of ordinary evidence.
    assert MARKER not in channels
    if policy == "quarantine":
        assert result is not None
        records = runtime.sink.open(result.quarantine_access, _ctx())  # type: ignore[union-attr]
        assert any(MARKER in json.dumps(r.row, default=str) for r in records)
    else:
        assert (policy == "fail") == (result is None)


def test_c5_cobol_bytes_marker(tmp_path: Path) -> None:
    from decoy_engine.errors import DecoyError

    src = write_csv(tmp_path / "s.csv", ["id", "v"], [["1", "1"]])
    spec = {"id": "cobol/zoned-ebcdic-trailing/v1", "precision": 3, "scale": 0, "signed": True}
    cfg = config([column("id"), column("v", logical_type="decimal", parse_format=spec, error_policy="fail")], source_path=src)
    bad = MARKER.encode()[:3]
    table = pa.table({"id": ["1"], "v": pa.array([bad], pa.binary())})
    with pytest.raises(DecoyError) as exc:
        run(cfg, {"people": table})
    assert MARKER not in _blob(str(exc.value), repr(exc.value), "".join(traceback.format_exception(exc.value)))


def test_c5_configured_sentinel_may_live_in_config_but_not_in_diagnostics(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    sentinel = f"SENT-{MARKER}"
    src = write_csv(tmp_path / "s.csv", ["id", "v"], [["1", "1"]])
    cfg = config(
        [column("id"), column("v", logical_type="date", parse_format=ISO_DATE, error_policy="fail",
                              sentinel_to_null={"id": "sentinel/exact/v1", "values": [sentinel]})],
        source_path=src,
    )
    caplog.set_level(logging.DEBUG)
    progress = _Capture()
    result = run(cfg, {"people": text_table(id=["1", "2"], v=[sentinel, "2026-01-01"])}, progress=progress, explain_plan=True)
    assert sentinel in json.dumps(cfg)  # the privileged contract reproduces the rule
    ordinary = _blob(caplog.text, json.dumps(result.quality_metrics, default=str), repr(result), [repr(e) for e in progress.events])
    assert sentinel not in ordinary and MARKER not in ordinary


# -- C6 F29 compatibility ---------------------------------------------------------------------

F06_CODES = {
    "plan": [
        "declared_cast_contract_invalid", "declared_cast_precision_invalid", "declared_cast_scale_invalid",
        "declared_cast_sentinel_config_invalid", "declared_cast_currency_code_invalid",
        "declared_cast_format_unknown", "declared_cast_directive_unsupported",
        "declared_output_width_unknown", "declared_type_strategy_incompatible",
    ],
    "route": [
        "declared_cast_route_unsupported", "declared_cast_input_type_incompatible",
        "protected_quarantine_unavailable",
    ],
    "runtime": [
        "declared_integer_invalid", "declared_integer_overflow", "declared_decimal_invalid",
        "declared_decimal_precision_loss", "declared_decimal_overflow", "declared_date_invalid",
        "declared_currency_invalid", "declared_cobol_invalid", "declared_output_width_overflow",
        "protected_quarantine_write_failed", "protected_quarantine_seal_failed",
        "protected_quarantine_access_denied", "protected_quarantine_expired",
        "fixed_width_record_overlong",
    ],
}


@pytest.mark.parametrize(("namespace", "code"), [(ns, c) for ns, cs in F06_CODES.items() for c in cs])
def test_c6_every_reason_is_registered_once_with_safe_semantics(namespace: str, code: str) -> None:
    from decoy_engine import reasons

    spec = reasons.find(namespace, code)
    assert spec is not None, f"{namespace}.{code} is not in the registry"
    matches = [s for s in reasons.specs() if s.identity == (namespace, code)]
    assert len(matches) == 1
    assert spec.summary.endswith(".") and MARKER not in spec.summary
    assert spec.symbol == code.upper()
    record = reasons.ReasonRecord.for_reason(getattr(getattr(reasons, namespace), spec.symbol), phase=spec.default_rule.phase)
    assert record.code == code
    assert reasons.check_integrity() == []
    if namespace == "runtime" and code.startswith("protected_quarantine"):
        assert spec.default_rule.retry in {reasons.RetryClass.UNKNOWN, reasons.RetryClass.ENVIRONMENT_REPAIR, reasons.RetryClass.NEVER}


def test_c6_registry_version_bumped_and_append_only_vs_the_v1_fixture() -> None:
    from decoy_engine import reasons

    manifest = json.loads(json.dumps(reasons.manifest()))
    assert manifest["registry_version"].split(".")[:2] == ["1", "1"]
    pinned = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.json").read_text())
    live_ids = {(e["namespace"], e["code"]) for e in manifest["entries"]}
    assert {(e["namespace"], e["code"]) for e in pinned["entries"]} <= live_ids
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.1-f06.json"
    assert fixture.is_file(), "the F06 additions are pinned in their own fixture"
    f06 = json.loads(fixture.read_text())
    assert {(e["namespace"], e["code"]) for e in f06["entries"]} == {(ns, c) for ns, cs in F06_CODES.items() for c in cs}
