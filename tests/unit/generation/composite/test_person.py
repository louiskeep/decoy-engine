"""MG-4 Step 3 (2026-05-31): composite_person regression cells.

Locks the 4-output (first/last/email/dob) coherent bundle:
- All 4 outputs present + same length.
- Email matches the {first}.{last}@{domain} default format.
- Email format override works.
- DOB drawn from the faker person_dob pool.
- Same source -> same 4-tuple across runs (identity stability).
- Null source -> all-null row.
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from decoy_engine.generation.composite._person import CompositePerson
from decoy_engine.providers_v2._adapter import ProviderSpec

_SEED = (0x0123456789).to_bytes(8, "big")


def _spec(extra: dict | None = None) -> ProviderSpec:
    return ProviderSpec(
        locale="en_US",
        deterministic=True,
        namespace="p",
        seed=_SEED,
        extra=extra or {},
    )


def _make(pool_size: int = 200) -> CompositePerson:
    return CompositePerson(coherent_namespace="p", pool_size=pool_size)


class TestOutputs:
    def test_person_4_outputs_match_output_columns(self):
        c = _make()
        out = c.generate_bundle(
            _spec(), 3, source=pd.Series(["a", "b", "c"]), deterministic=True
        )
        assert set(out.keys()) == {"first_name", "last_name", "email", "dob"}
        for k in out:
            assert len(out[k]) == 3

    def test_person_email_matches_first_dot_last_at_domain(self):
        c = _make()
        out = c.generate_bundle(
            _spec(), 1, source=pd.Series(["alice"]), deterministic=True
        )
        first = str(out["first_name"].iloc[0]).lower()
        last = str(out["last_name"].iloc[0]).lower()
        email = out["email"].iloc[0]
        assert email.startswith(f"{first}.{last}@")

    def test_person_email_format_override_works(self):
        c = _make()
        out = c.generate_bundle(
            _spec(extra={"email_format": "{last}_{first}@{domain}"}),
            1,
            source=pd.Series(["alice"]),
            deterministic=True,
        )
        first = str(out["first_name"].iloc[0]).lower()
        last = str(out["last_name"].iloc[0]).lower()
        email = out["email"].iloc[0]
        assert email.startswith(f"{last}_{first}@")

    def test_person_dob_drawn_from_faker_date_pool(self):
        c = _make()
        out = c.generate_bundle(
            _spec(), 3, source=pd.Series(["a", "b", "c"]), deterministic=True
        )
        # All non-null dobs are real date objects (faker.date_of_birth -> datetime.date).
        for d in out["dob"]:
            assert isinstance(d, datetime.date)


class TestIdentityStability:
    def test_person_identity_stable_across_runs(self):
        c1 = _make()
        c2 = _make()
        src = pd.Series(["alpha", "beta"])
        out1 = c1.generate_bundle(_spec(), 2, source=src, deterministic=True)
        out2 = c2.generate_bundle(_spec(), 2, source=src, deterministic=True)
        for k in ("first_name", "last_name", "email", "dob"):
            assert out1[k].tolist() == out2[k].tolist()


class TestNullHandling:
    def test_person_null_source_handling(self):
        c = _make()
        src = pd.Series(["alpha", None, "beta"])
        out = c.generate_bundle(_spec(), 3, source=src, deterministic=True)
        for k in ("first_name", "last_name", "email", "dob"):
            assert pd.isna(out[k].iloc[1])
        # Row 0 + 2 are populated.
        assert not pd.isna(out["first_name"].iloc[0])
        assert not pd.isna(out["first_name"].iloc[2])
