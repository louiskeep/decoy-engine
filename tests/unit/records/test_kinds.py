"""A1: the payload models the records package owns."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from decoy_engine import records
from decoy_engine.records import RecordError
from tests.unit.records._helpers import corpus, make, sample_payload

MANIFESTS = ["evidence-cli-local-1.json", "evidence-cli-local-1-legacy.json"]


@pytest.mark.parametrize("name", MANIFESTS)
def test_the_cli_manifest_corpus_validates_and_is_stored_unchanged(name: str) -> None:
    """A1-evidence-model: the payload is the manifest, key for key."""
    manifest = corpus(name)
    env = make("evidence", manifest)
    assert env.payload == manifest
    decoded = records.decode(records.encode(env))
    assert isinstance(decoded, records.DecodedRecord)
    assert decoded.view.manifest_hash == manifest["manifest_hash"]  # type: ignore[attr-defined]


def test_evidence_model_refuses_a_manifest_field_it_does_not_declare() -> None:
    manifest = corpus("evidence-cli-local-1.json")
    manifest["free_text"] = "note"
    with pytest.raises(RecordError) as caught:
        make("evidence", manifest)
    assert caught.value.fields == ("payload.free_text",)


def test_evidence_read_ignores_a_later_optional_field() -> None:
    manifest = deepcopy(corpus("evidence-cli-local-1.json"))
    manifest["output_fingerprints"]["claims"]["compression"] = "zstd"
    raw = make("evidence", corpus("evidence-cli-local-1.json")).to_dict()
    raw["payload"] = manifest
    from decoy_engine.records import canonical_bytes
    from tests.unit.records._helpers import seal

    got = records.decode(canonical_bytes(seal(raw)))
    assert isinstance(got, records.DecodedRecord)
    assert got.raw["payload"]["output_fingerprints"]["claims"]["compression"] == "zstd"


@pytest.mark.parametrize(
    "change",
    [
        {"sample_size": 0},
        {"ttl_seconds": 0},
        {"sample_size": True},
        {"table_rows": {"a": -1}},
        {"outputs": [{"table": "t", "path": "p", "sha256": "md5:abc"}]},
        {"scratch_dir": ""},
    ],
)
def test_sample_run_fields_are_typed(change: dict[str, Any]) -> None:
    with pytest.raises(RecordError):
        make(payload=sample_payload(**change))


def test_sample_run_declares_its_path_fields() -> None:
    spec = records.kind_spec("sample-run")
    assert spec is not None
    assert spec.path_fields == ("/payload/scratch_dir", "/payload/outputs/*/path")


def _cli_manifest_hash(manifest: dict[str, Any]) -> bytes:
    """The CLI's `evidence.compute_manifest_hash` body, copied here as the reference form."""
    import json

    strip = {"manifest_hash", "signature", "signature_alg", "signature_key_id"}
    body = {k: v for k, v in manifest.items() if k not in strip}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    ).encode("utf-8")


@pytest.mark.parametrize("name", MANIFESTS)
def test_canonical_form_matches_the_cli_manifest_hash(name: str) -> None:
    """A1-canonical-shared: same bytes, so the same hash, including non-ASCII names."""
    from decoy_engine.records._canonical import sha256_tag

    manifest = corpus(name)
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    assert records.canonical_bytes(body) == _cli_manifest_hash(manifest)
    assert sha256_tag(records.canonical_bytes(body)) == manifest["manifest_hash"]
