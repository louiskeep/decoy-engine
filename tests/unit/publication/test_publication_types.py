"""F22 4.4: request and receipt types hold only closed tokens, digests, counts and checked ids.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 4.4 (slice a2; classes 1 and 10). A receipt
reaches user-visible surfaces (4.5), so every field is shape-checked when it is built and the
field lists are pinned: an added `path` or free-text field turns this red.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, get_args

import pytest

HEX = "a" * 64


def _types() -> Any:
    from decoy_engine.publication import _types

    return _types


def _receipt(**over: Any) -> Any:
    t = _types()
    fields: dict[str, Any] = {
        "outcome": "published",
        "operation_id": "job-42.attempt:1",
        "reason_code": None,
        "result_id": HEX,
        "descriptor_digest": HEX,
        "vault_digest": None,
        "artifacts": (t.ArtifactSummary(role="table", size=1, rows=0, sha256=HEX),),
        "visible": True,
        "durable": True,
        "os_error": None,
    }
    fields.update(over)
    return t.CommitReceipt(**fields)


def test_field_lists_are_the_planned_ones() -> None:
    t = _types()
    names = lambda cls: tuple(f.name for f in dataclasses.fields(cls))
    assert names(t.CommitReceipt) == (
        "outcome",
        "operation_id",
        "reason_code",
        "result_id",
        "descriptor_digest",
        "vault_digest",
        "artifacts",
        "visible",
        "durable",
        "os_error",
    )
    assert names(t.ArtifactSummary) == ("role", "size", "rows", "sha256")
    assert names(t.PublicationRequest) == (
        "operation_id",
        "unit",
        "overwrite",
        "vault",
        "lock_timeout_s",
    )
    assert names(t.VaultBinding) == ("writer", "out")
    assert set(get_args(t.Outcome)) == {
        "published",
        "already_committed",
        "operation_conflict",
        "refused",
        "failed",
        "commit_uncertain",
    }
    for cls in (t.CommitReceipt, t.ArtifactSummary, t.PublicationRequest, t.VaultBinding):
        assert cls.__dataclass_params__.frozen


def test_a_valid_receipt_builds() -> None:
    receipt = _receipt(os_error="ENOENT", reason_code="publication_target_occupied")
    assert receipt.os_error == "ENOENT" and receipt.durable is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "ok"),
        ("outcome", "published\n"),
        ("operation_id", "job-42\n"),
        ("operation_id", 42),
        ("reason_code", "Not A Code"),
        ("reason_code", "publication_target_occupied\n"),
        ("result_id", "A" * 64),
        ("result_id", HEX + "\n"),
        ("descriptor_digest", "x"),
        ("vault_digest", HEX[:-1]),
        ("artifacts", []),
        ("artifacts", ("table",)),
        ("visible", 1),
        ("durable", "true"),
        ("os_error", "No such file or directory"),
        ("os_error", "ENOENT\n"),
        ("os_error", "EFAKE"),
    ],
)
def test_receipt_refuses_unchecked_fields(field: str, value: Any) -> None:
    _receipt()  # non-vacuity: the unedited receipt builds
    with pytest.raises(ValueError) as caught:
        _receipt(**{field: value})
    assert str(caught.value) == f"invalid {field}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "sidecar"),
        ("size", True),
        ("size", -1),
        ("rows", 1.0),
        ("sha256", HEX + "\n"),
    ],
)
def test_artifact_summary_refuses(field: str, value: Any) -> None:
    t = _types()
    good = {"role": "table", "size": 1, "rows": 0, "sha256": HEX}
    t.ArtifactSummary(**good)  # non-vacuity
    with pytest.raises(ValueError) as caught:
        t.ArtifactSummary(**{**good, field: value})
    assert str(caught.value) == f"invalid {field}"


def test_summary_drops_the_path() -> None:
    from decoy_engine.publication import _descriptor

    art = _descriptor.Artifact(role="table", path="TBLMARK.parquet", size=3, sha256=HEX, rows=1)
    summary = art.summary()
    assert "TBLMARK" in art.path  # non-vacuity: the name is in the artifact
    assert "TBLMARK" not in repr(summary)
    assert (summary.role, summary.size, summary.rows, summary.sha256) == ("table", 3, 1, HEX)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", "op-1\n"),
        ("operation_id", ""),
        ("unit", "managed_dataset/v1"),
        ("overwrite", "replace"),
        ("vault", object()),
        ("lock_timeout_s", True),
        ("lock_timeout_s", -1.0),
        ("lock_timeout_s", math.nan),
        ("lock_timeout_s", math.inf),
        ("lock_timeout_s", "5"),
    ],
)
def test_request_refuses(field: str, value: Any) -> None:
    t = _types()
    request = t.PublicationRequest(operation_id="op-1")
    assert request.overwrite == "refuse_different" and request.vault is None  # non-vacuity
    with pytest.raises(ValueError) as caught:
        t.PublicationRequest(**{"operation_id": "op-1", field: value})
    assert str(caught.value) == f"invalid {field}"


def test_request_accepts_int_and_float_timeouts() -> None:
    t = _types()
    assert t.PublicationRequest(operation_id="op-1", lock_timeout_s=0).lock_timeout_s == 0
    assert t.PublicationRequest(operation_id="op-1", lock_timeout_s=2.5).lock_timeout_s == 2.5


def test_operation_id_is_one_check_for_request_receipt_and_descriptor() -> None:
    t = _types()
    from decoy_engine.publication import _descriptor

    good = "a" * 128
    bad = "a" * 129
    t.PublicationRequest(operation_id=good)
    _receipt(operation_id=good)
    _descriptor.build_descriptor(operation_id=good, artifacts=[], vault=None)
    for build in (
        lambda: t.PublicationRequest(operation_id=bad),
        lambda: _receipt(operation_id=bad),
        lambda: _descriptor.encode_descriptor(
            _descriptor.build_descriptor(operation_id=bad, artifacts=[], vault=None)
        ),
    ):
        with pytest.raises(ValueError):
            build()
    assert t.OPERATION_ID_PATTERN == r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
