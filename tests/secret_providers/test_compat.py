"""F13 §5 acceptance 12: the legacy KeyProvider contract and duck-typed providers keep working."""

from __future__ import annotations

import inspect
import typing
from typing import Protocol

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


def protocol_members(proto: type) -> set[str]:
    """Public members of a Protocol on every supported Python (3.10+).

    ``typing.get_protocol_members`` (3.13+) where available; otherwise the members the Protocol
    declares itself (annotations and attributes across its own MRO, minus typing internals).
    """
    get = getattr(typing, "get_protocol_members", None)
    if get is not None:
        return set(get(proto))
    members: set[str] = set()
    for base in proto.__mro__:
        if base in (object, Protocol, typing.Generic):
            continue
        members |= set(vars(base)) | set(vars(base).get("__annotations__", {}))
    return {m for m in members if not m.startswith("_")}


def test_protocol_members_helper_sees_declared_members():
    class Probe(Protocol):
        attr: int

        def method(self) -> None: ...

    assert protocol_members(Probe) == {"attr", "method"}


def test_key_provider_protocol_is_unchanged():
    assert protocol_members(KeyProvider) == {"key_version", "mask_key"}
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
