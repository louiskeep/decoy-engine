"""engine-v2 S8 composite-generator tests (slice 1: core + determinism).

Namespace auto-binding (S2), the row-8 compile check, registry fold-in, and the
golden-fixture wiring are covered in slice 2.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.determinism import derive_index
from decoy_engine.generation.composite import (
    CompositeAdapter,
    CompositeError,
    composite_city_state_zip,
    composite_name_email,
    load_locality_table,
)
from decoy_engine.generation.composite._bundle_pool import BundlePool
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.providers_v2._adapter import ProviderSpec

SEED = (0x0123456789).to_bytes(8, "big")
_SMALL = 500  # small name pools to keep tests fast


def _det_spec(ns: str, **extra: object) -> ProviderSpec:
    return ProviderSpec(
        locale="en_US", deterministic=True, namespace=ns, seed=SEED, extra=dict(extra)
    )


class TestCityStateZip:
    def test_deterministic_reproducible(self) -> None:
        src = pd.Series(["k1", "k2", "k3"], dtype=object)
        a = composite_city_state_zip(coherent_namespace="loc").generate_bundle(
            _det_spec("loc"), 3, source=src, deterministic=True
        )
        b = composite_city_state_zip(coherent_namespace="loc").generate_bundle(
            _det_spec("loc"), 3, source=src, deterministic=True
        )
        for col in ("city", "state", "zip"):
            assert list(a[col]) == list(b[col])

    def test_all_triples_in_locality_table(self) -> None:
        table = set(load_locality_table())
        src = pd.Series([f"row{i}" for i in range(200)], dtype=object)
        out = composite_city_state_zip(coherent_namespace="loc").generate_bundle(
            _det_spec("loc"), 200, source=src, deterministic=True
        )
        triples = list(zip(out["city"], out["state"], out["zip"], strict=True))
        assert all(t in table for t in triples)

    def test_same_source_same_triple(self) -> None:
        src = pd.Series(["x", "y", "x"], dtype=object)
        out = composite_city_state_zip(coherent_namespace="loc").generate_bundle(
            _det_spec("loc"), 3, source=src, deterministic=True
        )
        assert (out["city"].iloc[0], out["zip"].iloc[0]) == (
            out["city"].iloc[2],
            out["zip"].iloc[2],
        )

    def test_null_source_null_outputs(self) -> None:
        src = pd.Series(["a", None, "c"], dtype=object)
        out = composite_city_state_zip(coherent_namespace="loc").generate_bundle(
            _det_spec("loc"), 3, source=src, deterministic=True
        )
        assert all(pd.isna(out[c].iloc[1]) for c in ("city", "state", "zip"))

    def test_non_deterministic_triples_are_valid(self) -> None:
        table = set(load_locality_table())
        out = composite_city_state_zip(coherent_namespace="loc").generate_bundle(
            ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=SEED), 50
        )
        triples = list(zip(out["city"], out["state"], out["zip"], strict=True))
        assert all(t in table for t in triples)


class TestNameEmail:
    def _gen(self) -> object:
        return composite_name_email(coherent_namespace="ne", pool_size=_SMALL)

    def test_deterministic_reproducible(self) -> None:
        src = pd.Series(["k1", "k2", "k3"], dtype=object)
        a = self._gen().generate_bundle(_det_spec("ne"), 3, source=src, deterministic=True)
        b = self._gen().generate_bundle(_det_spec("ne"), 3, source=src, deterministic=True)
        for col in ("first_name", "last_name", "email"):
            assert list(a[col]) == list(b[col])

    def test_email_localpart_is_first_dot_last(self) -> None:
        src = pd.Series([f"id{i}" for i in range(40)], dtype=object)
        out = self._gen().generate_bundle(_det_spec("ne"), 40, source=src, deterministic=True)
        for i in range(40):
            first = str(out["first_name"].iloc[i]).lower()
            last = str(out["last_name"].iloc[i]).lower()
            assert str(out["email"].iloc[i]).startswith(f"{first}.{last}@")

    def test_null_source_null_outputs(self) -> None:
        src = pd.Series(["a", None, "c"], dtype=object)
        out = self._gen().generate_bundle(_det_spec("ne"), 3, source=src, deterministic=True)
        assert all(pd.isna(out[c].iloc[1]) for c in ("first_name", "last_name", "email"))

    def test_domain_pool_honored(self) -> None:
        src = pd.Series(["a", "b"], dtype=object)
        out = self._gen().generate_bundle(
            _det_spec("ne", domain_pool=["acme.test"]), 2, source=src, deterministic=True
        )
        assert all(str(e).endswith("@acme.test") for e in out["email"])

    def test_empty_domain_pool_raises(self) -> None:
        with pytest.raises(CompositeError) as exc:
            self._gen().generate_bundle(
                _det_spec("ne", domain_pool=[]),
                1,
                source=pd.Series(["a"], dtype=object),
                deterministic=True,
            )
        assert exc.value.code == "empty_domain_pool"

    def test_different_namespace_different_output(self) -> None:
        src = pd.Series(["same_key"], dtype=object)
        a = composite_name_email(coherent_namespace="ns_a", pool_size=_SMALL).generate_bundle(
            _det_spec("ns_a"), 1, source=src, deterministic=True
        )
        b = composite_name_email(coherent_namespace="ns_b", pool_size=_SMALL).generate_bundle(
            _det_spec("ns_b"), 1, source=src, deterministic=True
        )
        # Different coherent_namespace -> different latent draw -> (almost surely) different bundle.
        assert (a["first_name"].iloc[0], a["email"].iloc[0]) != (
            b["first_name"].iloc[0],
            b["email"].iloc[0],
        )

    def test_canonicalize_normalizes_int_and_numpy_int(self) -> None:
        # R3 (corrected): the composite keys through _canonicalize_source, which
        # normalizes python int and numpy int identically. A python-int-keyed row
        # and a numpy-int64-keyed row of the same value produce the SAME bundle.
        # (Note: int and str-of-int do NOT match; the canonicalise envelope encodes them
        # differently -- so the proof is the int/numpy-int equivalence, not the
        # int/str equivalence the draft spec described.)
        py = self._gen().generate_bundle(
            _det_spec("ne"), 1, source=pd.Series([42], dtype=object), deterministic=True
        )
        npy = self._gen().generate_bundle(
            _det_spec("ne"), 1, source=pd.Series([np.int64(42)], dtype=object), deterministic=True
        )
        assert py["email"].iloc[0] == npy["email"].iloc[0]


class TestBundlePool:
    def _pool(self) -> BundlePool:
        return composite_city_state_zip(coherent_namespace="loc").build_pool(
            ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=SEED)
        )

    def test_identity_uses_composite_name_no_override(self) -> None:
        pool = self._pool()
        assert pool.identity[0] == "composite_city_state_zip"
        assert pool.identity == (
            "composite_city_state_zip",
            "en_US",
            "locality/v1",
            SEED,
            pool.size,
        )

    def test_bundlepool_round_trips_cache(self) -> None:
        pool = self._pool()
        cache = PoolCache()
        cache.put(pool)
        got = cache.get(pool.identity)
        assert got is pool
        assert got.output_columns == ("city", "state", "zip")

    def test_scalar_pool_still_round_trips(self) -> None:
        # No regression to S5 cache behavior with a plain ValuePool.
        from decoy_engine.generation.pool._builder import PoolBuilder
        from decoy_engine.providers_v2 import get_default_registry

        scalar = PoolBuilder(get_default_registry()).build(
            provider="person_first_name",
            size=64,
            job_seed=SEED,
            locale="en_US",
            config={},
            namespace="ns",
        )
        cache = PoolCache()
        cache.put(scalar)
        assert cache.get(scalar.identity) is scalar


class TestSampleBundle:
    def _pool(self) -> BundlePool:
        return composite_city_state_zip(coherent_namespace="loc").build_pool(
            ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=SEED)
        )

    def test_returns_series_per_output_column(self) -> None:
        pool = self._pool()
        src = pd.Series([f"r{i}" for i in range(10)], dtype=object)
        out = PoolSampler().sample_bundle(
            pool,
            10,
            mode=CardinalityMode.REUSE,
            seed=SEED,
            source=src,
            namespace="loc",
            deterministic=True,
        )
        assert set(out) == {"city", "state", "zip"}
        assert all(len(out[c]) == 10 for c in out)

    def test_deterministic_selection_matches_scalar_derive_index(self) -> None:
        pool = self._pool()
        src = pd.Series(["pick_me"], dtype=object)
        out = PoolSampler().sample_bundle(
            pool,
            1,
            mode=CardinalityMode.REUSE,
            seed=SEED,
            source=src,
            namespace="loc",
            deterministic=True,
        )
        idx = derive_index(
            seed=SEED, namespace="loc", source=_canonicalize_source("pick_me"), pool_size=pool.size
        )
        expected = pool.values[idx]
        assert (out["city"].iloc[0], out["state"].iloc[0], out["zip"].iloc[0]) == expected


class TestCompositeAdapter:
    def test_capabilities(self) -> None:
        cap = CompositeAdapter("composite_name_email").capability_matrix("composite_name_email")
        assert cap.supports_coherent_link is True
        assert cap.poolable is True
        assert cap.supports_deterministic is False
        assert cap.backend_type == "composite"

    def test_single_column_generate_raises(self) -> None:
        ad = CompositeAdapter("composite_city_state_zip")
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        with pytest.raises(CompositeError) as exc:
            ad.generate("composite_city_state_zip", spec=spec)
        assert exc.value.code == "composite_requires_bundle_path"

    def test_single_column_batch_raises(self) -> None:
        ad = CompositeAdapter("composite_city_state_zip")
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        with pytest.raises(CompositeError) as exc:
            ad.generate_batch("composite_city_state_zip", spec=spec, count=5)
        assert exc.value.code == "composite_requires_bundle_path"
