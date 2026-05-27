"""The default ProviderRegistry catalog: 24 semantic names.

Per S4 spec §6: S1's 20 stub names preserved verbatim + 4 additions
(`synthetic_npi`, `synthetic_ndc`, `synthetic_mrn`, `person_full_name`).
(Spec §6 text says "21 S1 stubs + 4 = 25" but the actual S1_STUB_REGISTRY
had 20 names; one-line spec patch flagged for Dennis end-of-sprint review.)
The original draft included a 5th addition (`date_shift_offset`); per
cold-read review M1 it was dropped (date-shift offsets are a strategy
parameter, not a Faker-shaped value source).

Every entry at S4 close declares `supports_deterministic: False` (per
H2 PO call). Deterministic Faker lights up in S5 via PoolAdapter
wrapping FakerAdapter; this catalog declares the intent via
`poolable: True` for the 12 PII-shaped providers.

Source pattern: the V1 `S1_STUB_REGISTRY` frozenset shape carried
forward. CapabilityMatrix per-field rationale lives in the docstring
or in cross-sprint contracts §2.3.
"""

from __future__ import annotations

import faker as faker_module

from decoy_engine.providers_v2._adapter import CapabilityMatrix

_FAKER_VERSION = faker_module.VERSION


def _faker_cap(
    provider: str,
    *,
    poolable: bool = False,
    participates_in_fk_pk: bool = False,
    supported_locales: tuple[str, ...] = ("en_US",),
) -> CapabilityMatrix:
    """Build a CapabilityMatrix entry for a Faker-backed provider with
    the S4-close defaults: non-deterministic, value-reuse OK, cardinality
    not source-preserving, no coherent link, no format regex, no
    blocklist validators, fail-plan-compile on unmet guarantees.
    """
    return CapabilityMatrix(
        provider=provider,
        backend_type="faker",
        backend_version=_FAKER_VERSION,
        supports_deterministic=False,
        supports_uniqueness=True,
        supports_value_reuse=True,
        preserves_source_cardinality=False,
        participates_in_fk_pk=participates_in_fk_pk,
        poolable=poolable,
        supported_locales=supported_locales,
        supports_coherent_link=False,
        format_regex=None,
        blocklist_validators=(),
        fallback_behavior="fail_plan_compile",
    )


# 25 catalog entries. The frozenset of names + the per-name
# CapabilityMatrix are the two public surfaces the registry exposes.
_CATALOG: tuple[CapabilityMatrix, ...] = (
    # Identifiers (8) -- PII-shaped, poolable, participate in FK/PK
    _faker_cap("synthetic_ssn", poolable=True, participates_in_fk_pk=True),
    _faker_cap("synthetic_ein", poolable=True, participates_in_fk_pk=True),
    _faker_cap("synthetic_account_number", poolable=True, participates_in_fk_pk=True),
    _faker_cap("synthetic_npi", poolable=True, participates_in_fk_pk=True),
    _faker_cap("synthetic_ndc", poolable=True, participates_in_fk_pk=True),
    _faker_cap("synthetic_mrn", poolable=True, participates_in_fk_pk=True),
    _faker_cap("synthetic_member_id", poolable=True, participates_in_fk_pk=True),
    _faker_cap("synthetic_plan_id", poolable=True, participates_in_fk_pk=True),
    # Person attributes (7) -- PII-shaped, poolable
    _faker_cap("person_name", poolable=True),
    _faker_cap("person_first_name", poolable=True),
    _faker_cap("person_last_name", poolable=True),
    _faker_cap("person_full_name", poolable=True),
    _faker_cap("person_email", poolable=True),
    _faker_cap("person_phone", poolable=True),
    _faker_cap("person_dob", poolable=True),
    # Address attributes (5) -- PII-shaped (4 poolable + 1 full-address; full not poolable)
    _faker_cap("address_street", poolable=True),
    _faker_cap("address_city", poolable=True),
    _faker_cap("address_state", poolable=True),
    _faker_cap("address_zip", poolable=True),
    _faker_cap("address_full", poolable=False),
    # Generic (4) -- not PII-shaped; not poolable (lorem, uuid, random_*)
    _faker_cap("lorem_text", poolable=False),
    _faker_cap("uuid", poolable=False),
    _faker_cap("random_int_range", poolable=False),
    _faker_cap("random_choice", poolable=False),
    # NOTE: `date_shift_offset` was dropped per cold-read M1.
)


def get_default_catalog() -> tuple[CapabilityMatrix, ...]:
    """Return the 25-entry default catalog (immutable tuple)."""
    return _CATALOG
