"""MG-4 Step 4 (2026-05-31): composite_address regression cells.

Locks the 4-output (street/city/state/zip) coherent bundle:
- All 4 outputs present + same length.
- city/state/zip triple is membership-by-construction (a verbatim
  row of the US locality table; never an invalid pairing).
- Same source -> same 4-tuple across runs.
- Null source -> all-null row.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.generation.composite._address import CompositeAddress
from decoy_engine.generation.composite._city_state_zip import load_locality_table
from decoy_engine.providers_v2._adapter import ProviderSpec

_SEED = (0x0123456789).to_bytes(8, "big")


def _spec() -> ProviderSpec:
    return ProviderSpec(
        locale="en_US",
        deterministic=True,
        namespace="a",
        seed=_SEED,
        extra={},
    )


def _make() -> CompositeAddress:
    return CompositeAddress(coherent_namespace="a", pool_size=200)


class TestOutputs:
    def test_address_4_outputs_match_output_columns(self):
        c = _make()
        out = c.generate_bundle(_spec(), 3, source=pd.Series(["a", "b", "c"]), deterministic=True)
        assert set(out.keys()) == {"street_address", "city", "state", "zip"}
        for k in out:
            assert len(out[k]) == 3

    def test_address_state_zip_consistency_via_locality_table(self):
        """Every (city, state, zip) triple in the output must be a verbatim
        row of the locality table -- no Texas city with a Chicago ZIP."""
        c = _make()
        out = c.generate_bundle(
            _spec(), 10, source=pd.Series([f"v{i}" for i in range(10)]), deterministic=True
        )
        locality = set(load_locality_table())
        for i in range(10):
            triple = (out["city"].iloc[i], out["state"].iloc[i], out["zip"].iloc[i])
            assert triple in locality, f"row {i}: {triple} not in locality table"


class TestIdentityStability:
    def test_address_identity_stable_across_runs(self):
        c1 = _make()
        c2 = _make()
        src = pd.Series(["alpha", "beta"])
        out1 = c1.generate_bundle(_spec(), 2, source=src, deterministic=True)
        out2 = c2.generate_bundle(_spec(), 2, source=src, deterministic=True)
        for k in ("street_address", "city", "state", "zip"):
            assert out1[k].tolist() == out2[k].tolist()


class TestNullHandling:
    def test_address_null_source_handling(self):
        c = _make()
        src = pd.Series(["alpha", None, "beta"])
        out = c.generate_bundle(_spec(), 3, source=src, deterministic=True)
        for k in ("street_address", "city", "state", "zip"):
            assert pd.isna(out[k].iloc[1])
        assert not pd.isna(out["city"].iloc[0])
