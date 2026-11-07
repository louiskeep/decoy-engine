"""Edge-case adapter: seeded pools, order rule, errors, registry binding and capability rows."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import decoy_engine
from decoy_engine import reasons
from decoy_engine.generation.pool import PoolBuilder
from decoy_engine.providers_v2 import ProviderError, ProviderSpec, get_default_registry
from decoy_engine.providers_v2.edge_cases import EDGE_CASE_SPECS, EdgeCaseAdapter

SPECS = {spec.provider: spec for spec in EDGE_CASE_SPECS}
SEED = bytes(range(8))


def _spec(seed: bytes | None = SEED, *, deterministic: bool = False) -> ProviderSpec:
    return ProviderSpec(
        locale=None,
        deterministic=deterministic,
        namespace="ns" if deterministic else None,
        seed=seed,
    )


def _batch(name: str, count: int, seed: bytes | None = SEED) -> list:
    return list(EdgeCaseAdapter(SPECS[name]).generate_batch(name, spec=_spec(seed), count=count))


@pytest.mark.parametrize("name", sorted(SPECS))
def test_seeded_batch_is_reproducible_and_complete(name: str) -> None:
    n = len(SPECS[name].values)
    assert _batch(name, 3 * n) == _batch(name, 3 * n)
    for count in (n, 3 * n):
        assert {repr(v) for v in _batch(name, count)} == {repr(v) for v in SPECS[name].values}
    short = _batch(name, n - 1)
    assert len({repr(v) for v in short}) == n - 1


@pytest.mark.parametrize("name", sorted(SPECS))
def test_order_is_a_fresh_permutation_per_block(name: str) -> None:
    values = SPECS[name].values
    n = len(values)
    rng = np.random.default_rng(int.from_bytes(SEED, "big"))
    expected = [values[i] for _ in range(3) for i in rng.permutation(n)][: 2 * n + 5]
    assert [repr(v) for v in _batch(name, 2 * n + 5)] == [repr(v) for v in expected]


def test_different_seeds_give_different_orders() -> None:
    name = "edge_case_unicode"
    n = len(SPECS[name].values)
    base = _batch(name, n)
    assert _batch(name, n, bytes(8)) != base
    assert _batch(name, n, bytes(range(32))) != base


_SUBPROCESS = """
import json
from decoy_engine.providers_v2 import ProviderSpec
from decoy_engine.providers_v2.edge_cases import EDGE_CASE_SPECS, EdgeCaseAdapter
out = {}
for spec in EDGE_CASE_SPECS:
    batch = EdgeCaseAdapter(spec).generate_batch(
        spec.provider,
        spec=ProviderSpec(locale=None, deterministic=False, namespace=None, seed=bytes(range(8))),
        count=100,
    )
    out[spec.provider] = [repr(v) for v in batch]
print(json.dumps(out))
"""


def test_seeded_batch_is_identical_in_a_new_process() -> None:
    src = str(Path(decoy_engine.__file__).resolve().parents[1])
    proc = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", _SUBPROCESS],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": src, "PATH": ""},
    )
    other = json.loads(proc.stdout)
    for name in SPECS:
        assert other[name] == [repr(v) for v in _batch(name, 100)]


def test_unseeded_batches_differ() -> None:
    # Unseeded means OS entropy: two batches of 63 values agree with probability 1/63!.
    name = "edge_case_unicode"
    n = len(SPECS[name].values)
    assert _batch(name, n, None) != _batch(name, n, None)


def test_unseeded_calls_return_catalogue_values() -> None:
    for name, spec in SPECS.items():
        adapter = EdgeCaseAdapter(spec)
        reprs = {repr(v) for v in spec.values}
        assert {repr(v) for v in adapter.generate_batch(name, spec=_spec(None), count=20)} <= reprs
        assert repr(adapter.generate(name, spec=_spec(None))) in reprs
        assert repr(adapter.generate(name, spec=_spec())) in reprs
        assert list(adapter.generate_batch(name, spec=_spec(), count=0)) == []


def test_errors_use_existing_codes_and_carry_no_values() -> None:
    name = "edge_case_string"
    adapter = EdgeCaseAdapter(SPECS[name])
    with pytest.raises(ProviderError) as det:
        adapter.generate(name, spec=_spec(deterministic=True), source_value="secret-source")
    assert det.value.code == reasons.provider.CAPABILITY_VIOLATION
    assert "secret-source" not in str(det.value)
    with pytest.raises(ProviderError) as batch:
        adapter.generate_batch(name, spec=_spec(deterministic=True), count=3)
    assert batch.value.code == reasons.provider.BATCH_DETERMINISTIC_UNSUPPORTED
    for call in (
        lambda: adapter.generate("edge_case_float", spec=_spec()),
        lambda: adapter.capability_matrix("edge_case_float"),
    ):
        with pytest.raises(ProviderError) as wrong:
            call()
        assert wrong.value.code == reasons.provider.UNKNOWN_PROVIDER
    with pytest.raises(ValueError):
        adapter.generate_batch(name, spec=_spec(), count=-1)


@pytest.mark.parametrize("locale", [None, "en_US", "ja_JP"])
def test_a_pool_builds_for_any_locale(locale: str | None) -> None:
    pool = PoolBuilder(get_default_registry()).build(
        "edge_case_date", size=64, job_seed=SEED, locale=locale, namespace="ns"
    )
    assert len(pool.values) == 64
    assert {repr(v) for v in pool.values} == {repr(v) for v in SPECS["edge_case_date"].values}


def test_registry_binds_every_spec_with_the_pinned_row() -> None:
    registry = get_default_registry()
    bound = {
        name
        for name in registry.known_providers()
        if isinstance(registry.get_adapter(name), EdgeCaseAdapter)
    }
    assert bound == set(SPECS)
    for name in SPECS:
        assert registry.get_adapter(name).spec is SPECS[name]  # type: ignore[attr-defined]
        caps = registry.get_capabilities(name)
        assert caps.model_dump() == {
            "provider": name,
            "backend_type": "decoy_native",
            "backend_version": "edge_case/v1",
            "supports_deterministic": False,
            "supports_uniqueness": False,
            "supports_value_reuse": True,
            "preserves_source_cardinality": False,
            "participates_in_fk_pk": False,
            "poolable": True,
            "supported_locales": (),
            "supports_coherent_link": False,
            "format_regex": None,
            "blocklist_validators": (),
            "fallback_behavior": "fail_plan_compile",
        }


def test_no_default_config_or_disguise_names_the_providers() -> None:
    root = Path(decoy_engine.__file__).parent
    files = [*root.glob("disguises/*.yaml"), *root.rglob("templates/**/*.yaml")]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name in SPECS:
            assert name not in text, f"{name} named in {path.name}"
