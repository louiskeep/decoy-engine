"""Provider-registry pin test (S1 -> S4 migration).

Originally S1's `S1_STUB_REGISTRY` pin (20 names). After S4 the stub
is deleted; this test migrates to pin the V2 default registry's known
names (24; the 20 S1 stubs + 4 S4 additions per spec §6, minus
`date_shift_offset` which was dropped per cold-read review M1).

Per S4 spec §9 (resolution of cold-read M4): the test's behavior
assertion (`expected == known_set`) is the stable contract; the test
signature shape changed (import path + assertion target + pinned-set
size). S1's framing said "without changing the check or its test
signature" but the test signature delta is what M4 forced explicit.
"""

from __future__ import annotations

from decoy_engine.providers_v2 import get_default_registry


def test_registry_contains_exactly_documented_names() -> None:
    """Exact pinned set: 24 names. Any catalog change is a discrete diff."""
    expected = frozenset(
        {
            # S1 stub names (20 preserved verbatim)
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
            # S4 additions (4 per spec §6; date_shift_offset dropped per M1)
            "synthetic_npi",
            "synthetic_ndc",
            "synthetic_mrn",
            "person_full_name",
        }
    )
    assert expected == get_default_registry().known_providers()


def test_registry_is_frozenset() -> None:
    assert isinstance(get_default_registry().known_providers(), frozenset)


def test_date_shift_offset_dropped_per_m1() -> None:
    """M1 of the S4 cold-read review dropped `date_shift_offset` from the
    catalog: it's a strategy parameter, not a Faker-shaped value source."""
    assert "date_shift_offset" not in get_default_registry().known_providers()
