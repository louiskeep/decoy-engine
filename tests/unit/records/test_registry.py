"""A1: the kind registry, the pointed-at models and the registered reason codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decoy_engine import reasons, records
from decoy_engine.profiling import scan_set
from decoy_engine.records import _kinds, _registry
from decoy_engine.records._errors import _CODES
from decoy_engine.records._registry import KindSpec
from decoy_engine.run_result import RunResult, RunResultRead

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "f29" / "reason-manifest-v1.19-lib-l1a.json"
)


def test_registry_lists_every_kind_defined_in_kinds_module() -> None:
    """A1-registry-walk: walked from the module, not from a hand list."""
    defined = {v.kind for v in vars(_kinds).values() if isinstance(v, KindSpec)}
    assert defined and defined == {s.kind for s in records.registered_kinds()}


def test_v1_kinds_and_reserved_flags() -> None:
    table = {s.kind: s.reserved for s in records.registered_kinds()}
    assert table == {
        "checkpoint": True,
        "evidence": False,
        "publication": True,
        "run-result": False,
        "sample-run": False,
        "scan-set": False,
    }
    assert records.kind_spec("nope") is None


def test_run_result_points_at_the_engine_models() -> None:
    """A1-run-result-model."""
    spec = records.kind_spec("run-result")
    assert spec is not None
    assert spec.write_model is RunResult and spec.read_model is RunResultRead
    assert spec.path_fields == (
        "/payload/tables/*/delivery/output",
        "/payload/links/log/path",
    )


def test_scan_set_points_at_the_scan_core_model() -> None:
    """A1-scan-model-same: the kind points at the engine's ScanSetCore, not a copy."""
    spec = records.kind_spec("scan-set")
    assert spec is not None
    assert not spec.reserved
    assert spec.write_model is scan_set.ScanSetCore
    assert spec.read_model is scan_set.ScanSetCoreRead


def test_a_kind_is_registered_once() -> None:
    with pytest.raises(ValueError):
        _registry.register_kind(KindSpec(kind="evidence", payload_schema="evidence/v1"))


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "Bad Name", "payload_schema": "x/v1"},
        {"kind": "x", "payload_schema": "no-major"},
        {"kind": "x", "payload_schema": "x/v1", "path_fields": ("/other/a",)},
        {"kind": "x", "payload_schema": "x/v1", "diff_rule": "d"},
    ],
)
def test_kind_spec_refuses_bad_shapes(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        KindSpec(**bad)  # type: ignore[arg-type]


def test_payload_tag_majors() -> None:
    assert _registry.major_of("cli-local-1") == ("cli-local", 1)
    assert _registry.major_of("decoy-run-result/v1") == ("decoy-run-result", 1)
    assert _registry.major_of("scan-set/v12") == ("scan-set", 12)
    assert _registry.major_of("free text") is None


def test_every_l1a_code_is_registered_and_pinned() -> None:
    """A1-codes-registered: the fixture lists the minor's identities; each one resolves."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["registry_version"] == "1.19.0"
    ids = {(e["namespace"], e["code"]) for e in fixture["entries"]}
    assert ids
    for namespace, code in ids:
        assert reasons.find(namespace, code) is not None
        assert reasons.is_registered(code)
    # The failure codes a RecordError can carry, and the four CLI-side event codes.
    assert {("runtime", c) for c in _CODES} <= ids
    assert {code for ns, code in ids if ns == "warning"} == {
        "job_log_write_failed",
        "library_degraded",
        "library_delete_skipped",
        "library_write_failed",
    }
