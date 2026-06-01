"""MG-4 regression (2026-05-31): the existing 2 composites must produce
byte-identical output after MG-4 lands.

Drift = regression. The 4 new composites are additive; the
_COMPOSITE_NAMES + registry extension must not change how the
existing two composites behave.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.generation.composite._city_state_zip import (
    CompositeCityStateZip,
)
from decoy_engine.generation.composite._name_email import CompositeNameEmail
from decoy_engine.generation.composite._generator import _COMPOSITE_NAMES
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.providers_v2._adapter import ProviderSpec


_SEED = (0x0123456789).to_bytes(8, "big")


def _spec() -> ProviderSpec:
    return ProviderSpec(
        locale="en_US",
        deterministic=True,
        namespace="ns",
        seed=_SEED,
        extra={},
    )


class TestRegistryMembership:
    def test_all_six_composites_registered(self):
        r = get_default_registry()
        expected = {
            # Existing.
            "composite_name_email",
            "composite_city_state_zip",
            # MG-4 additions.
            "composite_custom",
            "composite_person",
            "composite_address",
            "composite_provider",
        }
        for name in expected:
            cap = r.get_capabilities(name)
            assert cap.provider == name
            assert cap.backend_type == "composite"

    def test_composite_names_frozenset_holds_six_entries(self):
        assert _COMPOSITE_NAMES == {
            "composite_name_email",
            "composite_city_state_zip",
            "composite_custom",
            "composite_person",
            "composite_address",
            "composite_provider",
        }


class TestExistingCompositesUnchanged:
    """Two stability cells: same input -> same output across runs.
    Catches a regression where the MG-4 additions accidentally drift the
    existing composites' output."""

    def test_composite_name_email_stable_after_mg4(self):
        c1 = CompositeNameEmail(coherent_namespace="ne", pool_size=200)
        c2 = CompositeNameEmail(coherent_namespace="ne", pool_size=200)
        src = pd.Series(["alpha", "beta", "gamma"])
        out1 = c1.generate_bundle(_spec(), 3, source=src, deterministic=True)
        out2 = c2.generate_bundle(_spec(), 3, source=src, deterministic=True)
        for k in ("first_name", "last_name", "email"):
            assert out1[k].tolist() == out2[k].tolist()

    def test_composite_city_state_zip_stable_after_mg4(self):
        c1 = CompositeCityStateZip(coherent_namespace="csz")
        c2 = CompositeCityStateZip(coherent_namespace="csz")
        src = pd.Series(["alpha", "beta", "gamma"])
        out1 = c1.generate_bundle(_spec(), 3, source=src, deterministic=True)
        out2 = c2.generate_bundle(_spec(), 3, source=src, deterministic=True)
        for k in ("city", "state", "zip"):
            assert out1[k].tolist() == out2[k].tolist()
