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
# Populated 2026-06-12 from the first full adoption evaluation (mimesis
# 19.1.0, n=10_000 per provider, en_US): the five person string providers
# cleared the gate with ratios 0.018-0.060 (17-55x faster than Faker) and
# checks 1-6 green. person_first_name failed only advisory check 7, with
# MORE distinct values than Faker (3103 vs 656); adopted per is_adoptable.
# Rejected with evidence (do not re-add without a fresh passing run):
#   address_state  ratio 0.37   (speed)
#   address_zip    ratio 0.82   (speed)
#   person_dob     ratio 0.20-0.25 over 4 runs (speed, stable fail)
#   address_city   length mean 9.2 vs 11.99, distribution 1749 vs 7735
#   address_street length mean 7.3 vs 22.4, distribution 2307 vs 10000
#   person_phone   length mean 13.5 vs 16.2
# Full results table: docs/mimesis-adoption-2026-06-12.md.
ADOPTED_MIMESIS_PROVIDERS: frozenset[str] = frozenset(
    {
        "person_name",
        "person_first_name",
        "person_last_name",
        "person_full_name",
        "person_email",
    }
)
