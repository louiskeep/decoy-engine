"""LIB evidence binding: the optional ``path_base`` / ``project_manifest`` pair of a project run."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from decoy_engine import records
from decoy_engine.records import RecordError, canonical_bytes
from decoy_engine.records._payloads import EvidencePayload
from decoy_engine.records._read_model import read_model_of
from tests.unit.records._helpers import PRODUCER, T0, corpus, make, raw_of

PROJECT = "evidence-cli-local-1-project.json"
CANARY = "CANARY-7f3a"


def _manifest(**edit: Any) -> dict[str, Any]:
    manifest = deepcopy(corpus("evidence-cli-local-1.json"))
    manifest.update(edit)
    return manifest


def _bound(**edit: Any) -> dict[str, Any]:
    return _manifest(**{"path_base": "project", "project_manifest": "out/run.json", **edit})


def _refused(manifest: dict[str, Any]) -> RecordError:
    with pytest.raises(RecordError) as caught:
        make("evidence", manifest, labels={})
    return caught.value


def test_eb_build_stores_the_pair_and_keeps_the_canonical_bytes() -> None:
    manifest = _bound()
    env = make("evidence", manifest, labels={})
    assert env.payload["path_base"] == "project"
    assert env.payload["project_manifest"] == "out/run.json"
    assert canonical_bytes(env.payload) == canonical_bytes(manifest)


def test_eb_null_keeps_an_explicit_null() -> None:
    manifest = _manifest(path_base="project", project_manifest=None)
    env = make("evidence", manifest, labels={})
    assert "project_manifest" in env.payload and env.payload["project_manifest"] is None
    assert canonical_bytes(env.payload) == canonical_bytes(manifest)
    decoded = records.decode(records.encode(env))
    assert isinstance(decoded, records.DecodedRecord)
    assert decoded.raw["payload"]["project_manifest"] is None


def test_eb_absent_stores_neither_key() -> None:
    manifest = _manifest()
    env = make("evidence", manifest, labels={})
    assert "path_base" not in env.payload and "project_manifest" not in env.payload
    assert canonical_bytes(env.payload) == canonical_bytes(manifest)
    raw = raw_of(env)
    assert "path_base" not in raw["payload"] and "project_manifest" not in raw["payload"]


@pytest.mark.parametrize("bad", ["cwd", "", None, "Project", "project "])
def test_eb_enum_refuses_any_other_path_base(bad: Any) -> None:
    err = _refused(_manifest(path_base=bad, project_manifest="out/run.json"))
    assert err.fields == ("payload.path_base",)
    assert "cwd" not in str(err) and "Project" not in str(err)


def test_eb_enum_refuses_a_non_string_project_manifest() -> None:
    err = _refused(_manifest(path_base="project", project_manifest=7))
    assert err.fields == ("payload.project_manifest",)


def test_eb_pair_refuses_path_base_alone() -> None:
    err = _refused(_manifest(path_base="project"))
    assert err.fields == ("payload.project_manifest",)


def test_eb_pair_refuses_project_manifest_alone() -> None:
    for value in ("out/run.json", None):
        err = _refused(_manifest(project_manifest=value))
        assert err.fields == ("payload.path_base",)


def _export_payload(manifest: dict[str, Any], **kw: Any) -> dict[str, Any]:
    rec = raw_of(make("evidence", manifest, labels={}))
    lines = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0, **kw))
    payload: dict[str, Any] = list(records.read_export(lines))[1].record.raw["payload"]
    return payload


def test_eb_export_reduces_project_manifest_like_pipeline_path() -> None:
    manifest = _manifest(
        path_base="project", project_manifest="deep/er/run.json", pipeline_path="a/b/claims.yaml"
    )
    kept = _export_payload(manifest)
    assert kept["pipeline_path"] == "claims.yaml"
    assert kept["project_manifest"] == "run.json"
    assert kept["path_base"] == "project"
    full = _export_payload(manifest, keep_paths=True)
    assert full["project_manifest"] == "deep/er/run.json"


def test_eb_export_keeps_a_null_project_manifest() -> None:
    kept = _export_payload(_manifest(path_base="project", project_manifest=None))
    assert kept["project_manifest"] is None and kept["path_base"] == "project"


def test_eb_canary_never_reaches_a_default_export() -> None:
    manifest = _bound(
        project_manifest=f"/home/{CANARY}/proj/out/run.json",
        pipeline_path=f"/home/{CANARY}/proj/p.yaml",
    )
    manifest["input_fingerprints"]["claims"]["path"] = f"/home/{CANARY}/in.csv"
    manifest["output_fingerprints"]["claims"]["path"] = f"/home/{CANARY}/out.parquet"
    manifest["input_fingerprints"]["médecins"]["path"] = f"/home/{CANARY}/m.csv"
    rec = raw_of(make("evidence", manifest, labels={}))
    lines = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0))
    assert CANARY not in b"".join(lines).decode()


def test_eb_corpus_project_sample_reads_under_both_models() -> None:
    sample = corpus(PROJECT)
    assert sample["path_base"] == "project" and sample["project_manifest"] == "evidence/run-1.json"
    assert EvidencePayload.model_validate(sample).path_base == "project"
    env = make("evidence", sample, labels={})
    assert canonical_bytes(env.payload) == canonical_bytes(sample)


@pytest.mark.parametrize(
    "name", ["evidence-cli-local-1.json", "evidence-cli-local-1-legacy.json", PROJECT]
)
def test_eb_readmodel_reads_every_sample(name: str) -> None:
    sample = corpus(name)
    got = read_model_of(EvidencePayload).model_validate(sample)
    assert got.manifest_hash == sample["manifest_hash"]  # type: ignore[attr-defined]
    assert ("path_base" in got.model_fields_set) == ("path_base" in sample)


def test_eb_older_samples_stay_without_the_pair() -> None:
    for name in ("evidence-cli-local-1.json", "evidence-cli-local-1-legacy.json"):
        sample = corpus(name)
        assert "path_base" not in sample and "project_manifest" not in sample
        env = make("evidence", sample, labels={})
        assert "path_base" not in env.payload
