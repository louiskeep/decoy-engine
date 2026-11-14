"""Each routed digest site writes the canonical form and refuses what JSON cannot hold."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

import pytest

from decoy_engine._canonical import CanonicalJsonError
from decoy_engine.execution.physical._inputs import _canonical_config_json
from decoy_engine.generation._reproducibility import PinnedEnvelope, build_envelope
from decoy_engine.phase import _encode
from decoy_engine.profile._serialize import _data_shape_bytes
from decoy_engine.profiling.model_pack.provenance import _canonical_payload
from decoy_engine.profiling.model_pack.types import ModelPackManifest
from decoy_engine.subset import FanOutPolicy, SeedSpec, SubsetRequest
from tests.unit.profile.test_hash_regression import build_regression_profile


def _utf8(value: Any) -> bytes:
    """The digest rule written out independently of the module under test."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def test_the_physical_input_config_is_compact_utf8_json() -> None:
    config = {"b": 1, "a": {"né": [1, 2]}}
    assert _canonical_config_json(config).encode("utf-8") == _utf8(config)


def test_the_physical_input_config_refuses_an_object_it_would_have_repr_hashed() -> None:
    class Odd:
        def __repr__(self) -> str:
            return "odd-secret"

    with pytest.raises(CanonicalJsonError) as caught:
        _canonical_config_json({"x": Odd()})
    assert "odd-secret" not in str(caught.value)


def test_the_subset_request_fingerprint_is_the_digest_of_compact_utf8_json() -> None:
    request = SubsetRequest(
        relationships=(),
        seeds=(SeedSpec(table="médecins", mode="keys", key_columns=("id",), keys=(("a",),)),),
        policy=FanOutPolicy(),
    )
    expected = hashlib.sha256(_utf8(dataclasses.asdict(request))).hexdigest()
    assert request.fingerprint() == expected


def test_the_subset_request_fingerprint_refuses_a_repr_only_literal() -> None:
    request = SubsetRequest(
        relationships=(),
        seeds=(SeedSpec(table="t", mode="keys", key_columns=("id",), keys=((object(),),)),),
        policy=FanOutPolicy(),
    )
    with pytest.raises(CanonicalJsonError):
        request.fingerprint()


def test_the_data_shape_bytes_of_a_profile_are_utf8_not_ascii_escapes() -> None:
    profile = build_regression_profile()
    first = dataclasses.replace(profile.tables[0], name="donn\u00e9es")
    renamed = dataclasses.replace(profile, tables=(first, *profile.tables[1:]))
    assert "données".encode() in _data_shape_bytes(renamed)


def test_the_phase_envelope_encoding_is_utf8() -> None:
    assert _encode({"b": "é", "a": 1}) == _utf8({"b": "é", "a": 1})


def test_the_model_pack_signing_payload_is_utf8() -> None:
    manifest = ModelPackManifest(pack_id="p", version="1", extra={"note": "café"})
    assert "café".encode() in _canonical_payload(manifest)


def test_the_pinned_envelope_digest_is_the_digest_of_compact_utf8_json() -> None:
    envelope = PinnedEnvelope(
        job_seed=7,
        config_digest="d",
        derive_key_digest=None,
        custom_provider_names=("café",),
        pinned_input_digests=(),
        column_locales=(),
        controlled_clock=None,
        engine_minor="0.0",
    )
    payload = [7, "d", None, ["café"], [], [], None, "0.0"]
    assert envelope.digest() == hashlib.sha256(_utf8(payload)).hexdigest()


def test_the_config_digest_refuses_a_value_it_would_have_str_coerced() -> None:
    config = {"global_settings": {"seed": 1}, "x": {"member"}}
    with pytest.raises(CanonicalJsonError):
        build_envelope(config)
