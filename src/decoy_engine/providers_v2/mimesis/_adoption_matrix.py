"""Mimesis adoption matrix (engine-v2 S7).

Pure data: which providers route through Mimesis at S7 close. This module
deliberately imports neither `mimesis` nor any adapter, so the registry can
read `ADOPTED_MIMESIS_PROVIDERS` to decide bindings without forcing the
optional dependency.

Default at S7 close: EMPTY. No provider is adopted until the 7-check parity
suite (`_parity.run_parity_suite`) produces benchmark evidence that a
provider is both behavior-equivalent to Faker and >=5x faster. Zero adopted
providers is an acceptable, spec-sanctioned outcome (S7 spec §4 / Risks).

The candidate set is the poolable PII-shaped subset of the real 24-provider
catalog (S7 spec §2 candidate table), enumerated against `_real_registry.py`.
The 5 DecoyNative identifiers (S6 owns regulated IDs) and the 3 synthetic
Faker IDs (format-shaped, no clean Mimesis analog) are intentionally excluded.
"""

from __future__ import annotations

# The 11 poolable PII providers eligible for Mimesis adoption (S7 spec §2).
MIMESIS_CANDIDATES: frozenset[str] = frozenset(
    {
        "person_name",
        "person_first_name",
        "person_last_name",
        "person_full_name",
        "person_email",
        "person_phone",
        "person_dob",
        "address_street",
        "address_city",
        "address_state",
        "address_zip",
    }
)

# Providers actually bound to MimesisAdapter in the default registry build.
# Empty at S7 close: the parity suite + benchmarks populate this only when a
# candidate clears the adoption gate (items 1-6 pass AND ratio < 0.20). Until
# then the default registry stays 24 providers (19 Faker + 5 DecoyNative).
ADOPTED_MIMESIS_PROVIDERS: frozenset[str] = frozenset()
