"""Value-free kind pin, version shapes, corpus location and identity newlines for the run result.

Split from ``test_model.py`` to keep that file under the size guideline; the helpers are shared.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from decoy_engine.reasons import ReasonRecord, _registry
from decoy_engine.run_result import RunResult, RunResultRead, _model
from tests.unit.run_result.test_model import (
    FIXTURES,
    _named_types,
    failed_doc,
    live_reason,
    load,
)

EXPECTED_VALUE_FREE_KINDS = frozenset(
    {
        "schema_tag",
        "token",
        "safe_name",
        "safe_ref",
        "error_class",
        "sha256",
        "hex64",
        "version",
        "registry_version",
        "utc_timestamp",
        "invocation_id",
        "rel_path",
        "reason_payload",
        "reason_payload_read",
    }
)


def test_the_value_free_kind_set_is_pinned() -> None:
    """A new kind is a decision about what may ride in a document: it needs this edit."""
    assert set(_named_types()) == EXPECTED_VALUE_FREE_KINDS


def test_run_result_samples_live_in_the_corpus_subfolder() -> None:
    root = Path(__file__).resolve().parents[2]
    assert root / "integration" / "compat_corpus" / "run_result" == FIXTURES
    assert len(list(FIXTURES.glob("run-result-v1*.json"))) == 9
    assert not (root / "fixtures" / "compat").exists()


@pytest.mark.parametrize("version", ["1.5.0", "1.999.0", "0.0.0"])
def test_registry_version_is_the_strict_reason_registry_shape(version: str) -> None:
    doc = load("run-result-v1.json")
    doc["producer"]["registry_version"] = version
    assert RunResult.model_validate(doc).producer.registry_version == version


@pytest.mark.parametrize(
    "version", ["1.0", "1.2.3rc1", "1.5.0.post1", "1.5.0\n", "1.5.0+x", "v1.5.0"]
)
def test_registry_version_refuses_wider_release_shapes(version: str) -> None:
    doc = load("run-result-v1.json")
    doc["producer"]["registry_version"] = version
    for model in (RunResult, RunResultRead):
        with pytest.raises(ValidationError):
            model.model_validate(doc)


def test_registry_version_reuses_the_reasons_shape() -> None:
    from decoy_engine import reasons

    assert _model.REGISTRY_VERSION_RE is reasons.REGISTRY_VERSION_RE
    assert not hasattr(reasons._record, "_VERSION")


def test_the_read_dump_is_the_read_projection() -> None:
    doc = failed_doc(live_reason())
    read = RunResultRead.model_validate(doc)
    reason = read.to_payload()["failure"]["reason"]
    assert reason == {**live_reason(), "written_registry_version": _registry.REGISTRY_VERSION}
    assert "written_registry_version" not in doc["failure"]["reason"]
    assert "read projection" in (_model._ReadBase.to_payload.__doc__ or "")
    assert _model._ReadBase.to_payload is not _model._Base.to_payload


@pytest.mark.parametrize(
    "edit",
    [
        {"namespace": "zz_ns\n"},
        {"code": "zz_code\n"},
        {"namespace": "zz_ns\n", "code": "zz_code\n"},
    ],
)
def test_an_identity_ending_in_a_newline_is_not_kept(edit: dict[str, str]) -> None:
    base = {"namespace": "zz_ns", "code": "zz_code"}
    record, written = ReasonRecord.parse_payload(live_reason(**{**base, **edit}))
    assert (record.code, written) == ("unknown_reason_from_future", _registry.REGISTRY_VERSION)
    assert record.context.original_identity is None

    control, _ = ReasonRecord.parse_payload(live_reason(**base))
    assert control.context.original_identity == ("zz_ns", "zz_code")


@pytest.mark.parametrize("edit", [{"namespace": "zz_ns\n"}, {"code": "zz_code\n"}])
def test_write_side_refuses_an_identity_ending_in_a_newline(edit: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        RunResult.model_validate(
            failed_doc(live_reason(**{"namespace": "zz_ns", "code": "zz_code", **edit}))
        )
    read = RunResultRead.model_validate(
        failed_doc(live_reason(**{"namespace": "zz_ns", "code": "zz_code", **edit}))
    )
    assert read.failure.reason["context"]["original_identity"] is None


@pytest.mark.parametrize(
    "version",
    [
        "0.18.0",
        "0.1.0",
        "0.18.1.dev3+g1a2b3c4.d20261106",
        "1.2.3rc1",
        "2.0.0.post1",
    ],
)
def test_released_and_dev_builds_are_versions(version: str) -> None:
    doc = load("run-result-v1.json")
    doc["producer"]["engine_version"] = version
    for model in (RunResult, RunResultRead):
        assert model.model_validate(doc).producer.engine_version == version


@pytest.mark.parametrize(
    "version",
    [
        "1.0.0 ",
        "v1.0.0",
        "1.0.0RC1",
        "01.0",
        "1.0.0-1",
        "1.0.0_post1",
        "1!2.0.0",
        "1." + "0" * 80,
    ],
)
def test_a_version_must_be_canonical_and_epoch_free(version: str) -> None:
    doc = load("run-result-v1.json")
    doc["producer"]["engine_version"] = version
    for model in (RunResult, RunResultRead):
        with pytest.raises(ValidationError):
            model.model_validate(doc)


def test_the_public_read_model_keeps_its_import_path_and_pickles() -> None:
    import pickle

    from decoy_engine import RunResultRead

    assert RunResultRead.__module__ == "decoy_engine.run_result._model"
    assert pickle.loads(pickle.dumps(RunResultRead)) is RunResultRead  # noqa: S301  # own bytes
