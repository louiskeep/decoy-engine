"""F13 §5 acceptance 12: the legacy KeyProvider contract and duck-typed providers keep working."""

from __future__ import annotations

import inspect

import pytest

from decoy_engine import KeyProvider, SecretKeyProvider, SeedKeyProvider, run_mask_pipeline_chunked
from decoy_engine.keyprovider import KeyedStrategyRequiresSecret
from decoy_engine.secret_providers import MASK_ROOT_V1, REQUIREMENTS, prepare_legacy
from tests.secret_providers._helpers import (
    ENGINE_VERSION,
    GOOD_SECRET,
    TouchedSource,
    keyed_config,
    one_chunk,
)


class DuckProvider:
    """A third-party provider that only satisfies the original two-member protocol."""

    key_version = "duck-1"

    def mask_key(self) -> bytes:
        return b"d" * 32


def test_key_provider_protocol_is_unchanged():
    assert set(KeyProvider.__protocol_attrs__) == {"key_version", "mask_key"}
    assert isinstance(DuckProvider(), KeyProvider)
    assert list(inspect.signature(SecretKeyProvider).parameters) == ["secret", "key_version"]


def test_duck_typed_provider_runs_keyed_work_through_admission(tmp_path):
    out = list(
        run_mask_pipeline_chunked(
            keyed_config(tmp_path),
            [one_chunk()],
            table="t",
            engine_version=ENGINE_VERSION,
            key_provider=DuckProvider(),
        )
    )
    assert out and out[0].num_rows == 8


def test_legacy_adapter_rejects_seed_and_short_providers():
    req = (REQUIREMENTS[MASK_ROOT_V1],)
    with pytest.raises(KeyedStrategyRequiresSecret):
        prepare_legacy(SeedKeyProvider(b"12345678"), req)

    class Short:
        key_version = "s"

        def mask_key(self) -> bytes:
            return b"x" * 8

    with pytest.raises(KeyedStrategyRequiresSecret):
        prepare_legacy(Short(), req)
    prepared = prepare_legacy(SecretKeyProvider(GOOD_SECRET), req)
    assert prepared.key_version == "v1" and len(prepared.mask_key()) == 32


def test_a_legacy_provider_may_not_satisfy_a_typed_requirement():
    from decoy_engine.secret_providers import FERNET_V1

    with pytest.raises(KeyedStrategyRequiresSecret):
        prepare_legacy(DuckProvider(), (REQUIREMENTS[FERNET_V1],))


def test_unkeyed_config_ignores_a_supplied_seed_provider(tmp_path):
    from tests.secret_providers._helpers import unkeyed_config

    out = list(
        run_mask_pipeline_chunked(
            unkeyed_config(tmp_path),
            TouchedSource(),
            table="t",
            engine_version=ENGINE_VERSION,
            key_provider=SeedKeyProvider(b"12345678"),
        )
    )
    assert out
