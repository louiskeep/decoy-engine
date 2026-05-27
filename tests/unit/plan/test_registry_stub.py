"""S1_STUB_REGISTRY pin test.

The stub registry is the S1 contract for unknown_provider checks. S4
replaces this with the real backend registry; the swap should be a
discrete event in the diff. This test pins the exact contents so S4's
PR has to update this list explicitly.
"""

from __future__ import annotations

from decoy_engine.plan import S1_STUB_REGISTRY


def test_registry_contains_exactly_documented_names() -> None:
    expected = frozenset(
        {
            "synthetic_ssn",
            "synthetic_ein",
            "synthetic_account_number",
            "person_name",
            "person_first_name",
            "person_last_name",
            "person_email",
            "person_phone",
            "person_dob",
            "address_street",
            "address_city",
            "address_state",
            "address_zip",
            "address_full",
            "synthetic_member_id",
            "synthetic_plan_id",
            "lorem_text",
            "uuid",
            "random_int_range",
            "random_choice",
        }
    )
    assert expected == S1_STUB_REGISTRY


def test_registry_is_frozenset() -> None:
    assert isinstance(S1_STUB_REGISTRY, frozenset)
