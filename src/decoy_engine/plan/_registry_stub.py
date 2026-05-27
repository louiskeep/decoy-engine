"""S1 stub backend registry (resolution of S1 spec review B3).

The `unknown_provider` plan-compile check rejects any strategy whose
`provider` field is not in `S1_STUB_REGISTRY`. S1 ships this literal
hardcoded set; S4 replaces the file with the real backend registry
behind the same name. The unit test
`tests/unit/plan/test_registry_stub.py` pins the exact contents so the
S4 swap shows up as a discrete change in the test diff rather than
silently shifting check semantics.

When S4 lands the real registry, the test assertion stays stable: every
provider name in the new registry is `provider in S4_REAL_REGISTRY`;
this stub becomes redundant and gets deleted in the same PR.

Naming distinction:
- `provider` is a SEMANTIC name (what the user asks for, e.g. "synthetic_ssn").
- `backend_type` is an IMPLEMENTATION source ("faker", "mimesis", "pool",
  "decoy_native"). Stored separately on each `ColumnSeed`; S4's real
  registry binds each `provider` to a specific `backend_type`.
"""

from __future__ import annotations

S1_STUB_REGISTRY: frozenset[str] = frozenset(
    {
        # Identifiers
        "synthetic_ssn",
        "synthetic_ein",
        "synthetic_account_number",
        # Person attributes
        "person_name",
        "person_first_name",
        "person_last_name",
        "person_email",
        "person_phone",
        "person_dob",
        # Address attributes
        "address_street",
        "address_city",
        "address_state",
        "address_zip",
        "address_full",
        # Healthcare (composite-relevant for the composite_key fixture)
        "synthetic_member_id",
        "synthetic_plan_id",
        # Generic
        "lorem_text",
        "uuid",
        "random_int_range",
        "random_choice",
    }
)
