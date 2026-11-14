"""One seed per composite spec, resolved once at the entry point.

A spec with no seed used to draw three different seeds in one call: an all-zero seed for the
street pool and bundle build, `BundlePool.seed` zeros, and a fresh `os.urandom(8)` for the
sample. `resolve_seed` now runs first in `generate_bundle` and `build_pool`, and every draw of
the call reads the resolved spec. The cached pools are keyed by the seed they were built with.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.determinism import _seeds
from decoy_engine.generation.composite import (
    composite_address,
    composite_city_state_zip,
    composite_custom,
    composite_name_email,
    composite_person,
)
from decoy_engine.generation.composite._provider import composite_provider
from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.providers_v2._adapter import ProviderSpec

_ZEROS = b"\x00" * 8
_SEED_A = bytes.fromhex("0102030405060708")
_SEED_B = bytes.fromhex("1112131415161718")
_KEY32 = bytes(range(32))
_SOURCE = pd.Series([f"v{i}" for i in range(12)])

_Factory = Callable[[], Any]
_FACTORIES: dict[str, _Factory] = {
    "address": lambda: composite_address(coherent_namespace="ns", pool_size=200),
    "city_state_zip": lambda: composite_city_state_zip(coherent_namespace="ns"),
    "custom": lambda: composite_custom(
        coherent_namespace="ns",
        pool_size=200,
        bundle=[
            {"column": "fn", "provider": "person_first_name"},
            {"column": "ln", "provider": "person_last_name"},
        ],
    ),
    "name_email": lambda: composite_name_email(coherent_namespace="ns", pool_size=200),
    "person": lambda: composite_person(coherent_namespace="ns", pool_size=200),
    "provider": lambda: composite_provider(coherent_namespace="ns", pool_size=200),
}

# Captured from `origin/main` before the change: a seeded spec's output must not move.
_MAIN_NONDET = {
    "address": "f6e72f1a52add2a0a4777d76c9769dee9fce5f4662c489eaf055304a4ab9f5f2",
    "city_state_zip": "a16b5f295512ad6a78d654057c8bd06ab99116e6cb595cf416e10b095d9e569f",
    "custom": "b7ad6150af467404d971d836a5ee64cd54b6f240fc38fe34c0c1d389da2f4ec8",
    "name_email": "fd153794d01609a62f52ad2297f6b18ebda8698292b7a4d54b6677fa525b7ee5",
    "person": "245ece07a61f885e0509ce3a0a77e699d0ed6e01611314e54f2b17f98ebe3498",
    "provider": "1ecea31c536c91d4cda35f75299ada9ee3b35b990624b1e9e7df103fd37aaac7",
}
_MAIN_DET8 = {
    "address": "88a0c3c3715a07bc489213620d2c920c58d2c5d7b3bd78a77bdee310f6ec173d",
    "city_state_zip": "8b7cab89b6bb40a5ea9677983461ffa179b3adc3eb9109a67600661fc9961e94",
    "custom": "49e580f1aa1e18a55986227e57ca50c476b327df02fc27deda2f11c6aecdadd8",
    "name_email": "11a0b4f112a5ddece42789ee5b64fea7901dde4699c84e47beb8052fe2ff5e3f",
    "person": "f70ecf4a86f244d52bfb38e0d9ff45cfefaef735b8dfe3cd7a4572e7a2cdf853",
    "provider": "55091b8df2a28a02661446f7b4f9f5a14ebb13349e9ddf7508ef14b4d82dabfc",
}
_MAIN_DET32 = {
    "address": "b97313e4026583db4bd53888dc5909bfcf3f31278ede6b6bc8f1b37d88caa156",
    "city_state_zip": "3dbc930488f0e2e36e081864149de37b633e76aec4e1de1ed7d446d3837d3ff9",
    "custom": "fccb7afca8ef517238a9335dd82d0e70ad4422630b76cd8f974146731cae706d",
    "name_email": "f902bf12601da02583677b7cb9fa51162c6467fe567ef00c8fd73395cd311991",
    "person": "52f485147be1a4ac140676c81a2b6b483f3160a814ca6f04e43076692e46da47",
    "provider": "bbb76a074e00fa5bf35ca9dde80b8298f92cfe9bcee7e6b30f630cd2f49c1c91",
}


def _spec(seed: bytes | None, *, deterministic: bool = False) -> ProviderSpec:
    namespace = "ns" if deterministic else None
    return ProviderSpec(
        locale="en_US", deterministic=deterministic, namespace=namespace, seed=seed, extra={}
    )


def _digest(bundle: dict[str, pd.Series]) -> str:
    value = {k: v.tolist() for k, v in sorted(bundle.items())}
    return hashlib.sha256(repr(value).encode()).hexdigest()


class _Trace:
    """Seeds seen by the pool builder and the sampler, and the pools handed to the sampler.

    Use as a context manager so two traces in one test never stack their patches."""

    def __init__(self) -> None:
        self._patch = pytest.MonkeyPatch()
        self.builds: list[tuple[str, bytes]] = []
        self.sample_seeds: list[bytes] = []
        self.pool_seeds: list[bytes] = []
        self.pool_values: list[str] = []
        real_build, real_sample = PoolBuilder.build, PoolSampler.sample_bundle
        trace = self

        def build(builder: Any, *args: Any, **kwargs: Any) -> Any:
            trace.builds.append((kwargs["provider"], kwargs["job_seed"]))
            return real_build(builder, *args, **kwargs)

        def sample(sampler: Any, pool: Any, count: int, **kwargs: Any) -> Any:
            trace.sample_seeds.append(kwargs["seed"])
            trace.pool_seeds.append(pool.seed)
            trace.pool_values.append(repr(list(pool.values)))
            return real_sample(sampler, pool, count, **kwargs)

        self._patch.setattr(PoolBuilder, "build", build)
        self._patch.setattr(PoolSampler, "sample_bundle", sample)

    def __enter__(self) -> _Trace:
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.undo()

    def metadata(self) -> tuple[list[tuple[str, bytes]], list[bytes], list[str]]:
        return list(self.builds), list(self.pool_seeds), list(self.pool_values)

    def all_seeds(self) -> list[bytes]:
        return [s for _, s in self.builds] + self.sample_seeds + self.pool_seeds


@pytest.mark.parametrize("name", sorted(_FACTORIES))
def test_a_spec_with_no_seed_uses_one_fresh_seed_for_the_whole_call(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-E2-5: street pool, bundle build, BundlePool.seed and the sample share one seed."""
    drawn: list[bytes] = []

    def fake_fresh() -> bytes:
        drawn.append(_SEED_B)
        return _SEED_B

    monkeypatch.setattr(_seeds, "fresh_seed", fake_fresh)
    with _Trace() as trace:
        out = _FACTORIES[name]().generate_bundle(_spec(None), 10)
    assert all(len(col) == 10 for col in out.values())
    assert len(drawn) == 1
    assert trace.all_seeds() and set(trace.all_seeds()) == {_SEED_B}
    assert _ZEROS not in trace.all_seeds()


@pytest.mark.parametrize("name", sorted(_FACTORIES))
def test_build_pool_resolves_a_missing_seed_once_and_never_to_zeros(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    drawn: list[bytes] = []
    monkeypatch.setattr(_seeds, "fresh_seed", lambda: (drawn.append(_SEED_B), _SEED_B)[1])
    pool = _FACTORIES[name]().build_pool(_spec(None))
    assert len(drawn) == 1
    assert pool.seed == _SEED_B


@pytest.mark.parametrize("name", sorted(_FACTORIES))
def test_a_seeded_spec_is_byte_identical_to_main(name: str) -> None:
    """AT-E2-5 control: non-deterministic and deterministic (8 and 32 byte) output."""
    factory = _FACTORIES[name]
    nondet = factory().generate_bundle(_spec(_SEED_A), 15)
    assert _digest(nondet) == _MAIN_NONDET[name]
    det8 = factory().generate_bundle(
        _spec(_SEED_A, deterministic=True), 12, source=_SOURCE, deterministic=True
    )
    assert _digest(det8) == _MAIN_DET8[name]
    det32 = factory().generate_bundle(
        _spec(_KEY32, deterministic=True), 12, source=_SOURCE, deterministic=True
    )
    assert _digest(det32) == _MAIN_DET32[name]


@pytest.mark.parametrize("name", sorted(_FACTORIES))
def test_a_second_seed_on_one_instance_equals_a_fresh_instance(
    name: str,
) -> None:
    """AT-E2-5: the cached pools are keyed by seed, so the second call rebuilds them."""
    reused = _FACTORIES[name]()
    reused.generate_bundle(_spec(_SEED_A), 15)
    with _Trace() as second_call:
        second = reused.generate_bundle(_spec(_SEED_B), 15)
    with _Trace() as fresh_call:
        expected = _FACTORIES[name]().generate_bundle(_spec(_SEED_B), 15)
    assert _digest(second) == _digest(expected)
    assert second_call.metadata() == fresh_call.metadata()
    assert set(second_call.all_seeds()) == {_SEED_B}


@pytest.mark.parametrize("name", sorted(_FACTORIES))
def test_a_wrong_width_seed_is_refused_at_the_entry_point(name: str) -> None:
    spec = _spec(_SEED_A)
    object.__setattr__(spec, "seed", b"\x01" * 4)  # past ProviderSpec's own check
    with pytest.raises(ValueError):
        _FACTORIES[name]().generate_bundle(spec, 3)
    with pytest.raises(ValueError):
        _FACTORIES[name]().build_pool(spec)


def test_a_deterministic_spec_without_a_seed_is_still_refused() -> None:
    from decoy_engine.generation.composite._errors import CompositeError

    spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None, extra={})
    with pytest.raises(CompositeError):
        composite_person(coherent_namespace="ns", pool_size=50).generate_bundle(
            spec, 3, source=pd.Series(["a", "b", "c"]), deterministic=True
        )


def test_the_keyed_32_byte_composite_path_keeps_the_full_width_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AT-E2-8: the deterministic composite_person mask key seeds NumPy as before."""
    seen: list[Any] = []
    real = np.random.default_rng

    def spy(arg: Any = None) -> Any:
        seen.append(arg)
        return real(arg)

    monkeypatch.setattr(np.random, "default_rng", spy)
    person = composite_person(coherent_namespace="ns", pool_size=200)
    out = person.generate_bundle(
        _spec(_KEY32, deterministic=True), 12, source=_SOURCE, deterministic=True
    )
    assert _digest(out) == _MAIN_DET32["person"]
    person.build_pool(_spec(_KEY32, deterministic=True))
    assert int.from_bytes(_KEY32, "big") in seen
