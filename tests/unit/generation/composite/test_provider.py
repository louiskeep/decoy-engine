"""MG-4 Step 5 (2026-05-31): composite_provider regression cells.

Locks the 3-output (npi/provider_name/practice_address) coherent bundle:
- All 3 outputs present + same length.
- npi passes the CMS Luhn NPI validator.
- practice_address is the city/state/zip flat string.
- Same source -> same 3-tuple across runs.
- Null source -> all-null row.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.generation.composite._provider import CompositeProvider
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.storm.detectors import _npi_valid

_SEED = (0x0123456789).to_bytes(8, "big")


def _spec() -> ProviderSpec:
    return ProviderSpec(
        locale="en_US",
        deterministic=True,
        namespace="v",
        seed=_SEED,
        extra={},
    )


def _make() -> CompositeProvider:
    return CompositeProvider(coherent_namespace="v", pool_size=200)


class TestOutputs:
    def test_provider_3_outputs_match_output_columns(self):
        c = _make()
        out = c.generate_bundle(
            _spec(), 3, source=pd.Series(["a", "b", "c"]), deterministic=True
        )
        assert set(out.keys()) == {"npi", "provider_name", "practice_address"}
        for k in out:
            assert len(out[k]) == 3

    def test_provider_npi_passes_npi_validator(self):
        c = _make()
        out = c.generate_bundle(
            _spec(),
            5,
            source=pd.Series([f"src{i}" for i in range(5)]),
            deterministic=True,
        )
        for i in range(5):
            npi = out["npi"].iloc[i]
            assert _npi_valid(npi), f"row {i}: NPI {npi!r} fails CMS Luhn"

    def test_provider_practice_address_is_flat_string(self):
        c = _make()
        out = c.generate_bundle(
            _spec(), 3, source=pd.Series(["a", "b", "c"]), deterministic=True
        )
        for addr in out["practice_address"]:
            assert isinstance(addr, str)
            # Shape: "<city>, <ST> <zip>"
            assert ", " in addr


class TestIdentityStability:
    def test_provider_identity_stable_across_runs(self):
        c1 = _make()
        c2 = _make()
        src = pd.Series(["alpha", "beta"])
        out1 = c1.generate_bundle(_spec(), 2, source=src, deterministic=True)
        out2 = c2.generate_bundle(_spec(), 2, source=src, deterministic=True)
        for k in ("npi", "provider_name", "practice_address"):
            assert out1[k].tolist() == out2[k].tolist()


class TestNullHandling:
    def test_provider_null_source_handling(self):
        c = _make()
        src = pd.Series(["alpha", None, "beta"])
        out = c.generate_bundle(_spec(), 3, source=src, deterministic=True)
        for k in ("npi", "provider_name", "practice_address"):
            assert pd.isna(out[k].iloc[1])
        assert not pd.isna(out["npi"].iloc[0])
