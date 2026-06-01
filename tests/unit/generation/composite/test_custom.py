"""MG-4 Step 1 (2026-05-31): composite_custom regression cells.

Locks:
- Bundle size rejection at the 0 and 5+ boundaries.
- Sizes 1 + 4 accepted.
- Missing-key + bad-shape items rejected.
- Nested composite_ providers rejected.
- Identity stability across runs (same source -> same bundle).
- Deterministic + non-deterministic paths each work.
- Null source rows passthrough.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.generation.composite._custom import CompositeCustom
from decoy_engine.generation.composite._errors import CompositeError
from decoy_engine.providers_v2._adapter import ProviderSpec


_SEED = (0x0123456789).to_bytes(8, "big")


def _spec(deterministic: bool = True) -> ProviderSpec:
    return ProviderSpec(
        locale="en_US",
        deterministic=deterministic,
        namespace="cust",
        seed=_SEED,
        extra={},
    )


def _bundle(size: int) -> list[dict]:
    providers = [
        "person_first_name",
        "person_last_name",
        "person_email",
        "person_phone",
    ]
    return [
        {"column": f"c{i}", "provider": providers[i % len(providers)]}
        for i in range(size)
    ]


# ── bundle-size boundaries ────────────────────────────────────────────


class TestBundleSizeBoundaries:
    def test_custom_bundle_size_0_rejects(self):
        with pytest.raises(CompositeError) as exc:
            CompositeCustom(coherent_namespace="cust", bundle=[])
        assert exc.value.code == "composite_custom_bundle_size"

    def test_custom_bundle_size_5_rejects_composite_custom_bundle_size(self):
        with pytest.raises(CompositeError) as exc:
            CompositeCustom(coherent_namespace="cust", bundle=_bundle(5))
        assert exc.value.code == "composite_custom_bundle_size"

    def test_custom_bundle_size_4_accepted(self):
        c = CompositeCustom(coherent_namespace="cust", bundle=_bundle(4))
        assert len(c.bundle) == 4
        assert len(c.output_columns) == 4

    def test_custom_bundle_size_1_accepted(self):
        c = CompositeCustom(coherent_namespace="cust", bundle=_bundle(1))
        assert len(c.bundle) == 1


# ── bundle-item shape ─────────────────────────────────────────────────


class TestBundleItemShape:
    def test_custom_bundle_item_missing_provider_rejects(self):
        with pytest.raises(CompositeError) as exc:
            CompositeCustom(
                coherent_namespace="cust",
                bundle=[{"column": "a"}],
            )
        assert exc.value.code == "composite_custom_bundle_item_missing_keys"

    def test_custom_bundle_item_missing_column_rejects(self):
        with pytest.raises(CompositeError) as exc:
            CompositeCustom(
                coherent_namespace="cust",
                bundle=[{"provider": "person_first_name"}],
            )
        assert exc.value.code == "composite_custom_bundle_item_missing_keys"

    def test_custom_bundle_item_non_dict_rejects(self):
        with pytest.raises(CompositeError) as exc:
            CompositeCustom(
                coherent_namespace="cust",
                bundle=["a", "person_first_name"],  # type: ignore[list-item]
            )
        assert exc.value.code == "composite_custom_bundle_item_shape"

    def test_custom_bundle_provider_composite_rejects_composite_custom_no_nesting(self):
        with pytest.raises(CompositeError) as exc:
            CompositeCustom(
                coherent_namespace="cust",
                bundle=[
                    {"column": "a", "provider": "person_first_name"},
                    {"column": "b", "provider": "composite_name_email"},
                ],
            )
        assert exc.value.code == "composite_custom_no_nesting"


# ── identity stability + determinism ─────────────────────────────────


class TestIdentityStability:
    def test_custom_identity_stability_same_source_same_bundle_across_runs(self):
        """Two compiles of the same bundle config against the same source
        produce identical output."""
        bundle = [
            {"column": "first", "provider": "person_first_name"},
            {"column": "last", "provider": "person_last_name"},
        ]
        c1 = CompositeCustom(coherent_namespace="cust", bundle=bundle, pool_size=200)
        c2 = CompositeCustom(coherent_namespace="cust", bundle=bundle, pool_size=200)
        src = pd.Series(["alpha", "beta", "gamma"])
        out1 = c1.generate_bundle(_spec(), 3, source=src, deterministic=True)
        out2 = c2.generate_bundle(_spec(), 3, source=src, deterministic=True)
        assert out1["first"].tolist() == out2["first"].tolist()
        assert out1["last"].tolist() == out2["last"].tolist()

    def test_custom_same_source_same_value_per_slot(self):
        """Two source rows with the SAME value produce the SAME bundle."""
        bundle = [
            {"column": "first", "provider": "person_first_name"},
            {"column": "last", "provider": "person_last_name"},
        ]
        c = CompositeCustom(coherent_namespace="cust", bundle=bundle, pool_size=200)
        src = pd.Series(["alpha", "alpha", "beta"])
        out = c.generate_bundle(_spec(), 3, source=src, deterministic=True)
        # Rows 0 + 1 both have source "alpha" -> identical bundle.
        assert out["first"].iloc[0] == out["first"].iloc[1]
        assert out["last"].iloc[0] == out["last"].iloc[1]


# ── null handling ────────────────────────────────────────────────────


class TestNullHandling:
    def test_custom_null_source_handling(self):
        bundle = [
            {"column": "first", "provider": "person_first_name"},
            {"column": "last", "provider": "person_last_name"},
        ]
        c = CompositeCustom(coherent_namespace="cust", bundle=bundle, pool_size=200)
        src = pd.Series(["alpha", None, "beta"])
        out = c.generate_bundle(_spec(), 3, source=src, deterministic=True)
        assert pd.isna(out["first"].iloc[1])
        assert pd.isna(out["last"].iloc[1])
        assert out["first"].iloc[0] is not None
        assert out["last"].iloc[2] is not None


# ── non-deterministic path ────────────────────────────────────────────


class TestNonDeterministicPath:
    def test_custom_non_deterministic_produces_count_rows(self):
        bundle = [
            {"column": "first", "provider": "person_first_name"},
            {"column": "last", "provider": "person_last_name"},
        ]
        c = CompositeCustom(coherent_namespace="cust", bundle=bundle, pool_size=200)
        out = c.generate_bundle(_spec(deterministic=False), 5, deterministic=False)
        # PoolSampler.sample_bundle returns one Series per output column.
        assert set(out.keys()) >= {"first", "last"}
        for k in ("first", "last"):
            assert len(out[k]) == 5


# ── output_columns shape ─────────────────────────────────────────────


class TestOutputColumnsShape:
    def test_output_columns_sorted_for_wiring_check(self):
        c = CompositeCustom(
            coherent_namespace="cust",
            bundle=[
                {"column": "z", "provider": "person_first_name"},
                {"column": "a", "provider": "person_last_name"},
                {"column": "m", "provider": "person_email"},
            ],
        )
        assert c.output_columns == ("a", "m", "z")
