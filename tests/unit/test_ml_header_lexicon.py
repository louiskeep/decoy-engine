"""Tests for the header-role lexicon (CH-1 acronym/synonym + CH-2 fuzzy).

Gate reference: ML cryptic-header recognition track (roadmap CH-1/CH-2). The
lexicon maps header tokens to a closed set of canonical roles so cryptic /
abbreviated headers (``dx_cd`` -> ``role_icd10``) get a vocabulary-stable header
signal the raw ``hdr_{token}`` features cannot provide.

These tests pin the AUDITABLE contract: exact mappings, no cross-role or
non-PII false positives, determinism, and the featurizer wiring. They do NOT
need the ml extra (pure Python, no model).
"""

from __future__ import annotations

import pytest

from decoy_engine.storm.features.header_lexicon import (
    header_roles,
    roles_from_tokens,
)

pytestmark = pytest.mark.ml  # ml-gate membership; the classifier consumes this


# ── CH-1 exact acronym / synonym mappings ────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # clear headers
        ("ssn", ["ssn"]),
        ("social_security_number", ["ssn"]),
        ("email_address", ["email"]),
        ("card_number", ["pan"]),
        ("credit_card_no", ["pan"]),
        ("iban_code", ["iban"]),
        ("diagnosis_code", ["icd10"]),
        ("service_date", ["iso_date"]),
        ("provider_npi", ["npi"]),
        ("patient_mrn", ["mrn"]),
        ("health_plan_id", ["health_plan_id"]),
        ("cvv2", ["cvv"]),
        # cryptic / abbreviated healthcare headers (the CH-1 mission)
        ("dx_cd", ["icd10"]),
        ("primary_dx", ["icd10"]),
        ("dob", ["iso_date"]),  # birth-date folds into iso_date (no DOB label)
        ("date_of_birth", ["iso_date"]),
        ("mbr_id", ["health_plan_id"]),  # member id -> plan
        ("pt_id", ["mrn"]),
        ("prov_id", ["npi"]),
        ("chart_no", ["mrn"]),
    ],
)
def test_exact_role_mappings(header: str, expected: list[str]) -> None:
    assert header_roles(header) == expected


# ── CH-2 fuzzy typo / morphological fallback ─────────────────────────────────


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("insurnce_id", ["health_plan_id"]),  # 'insurance' typo (Dice 0.80)
        ("diagnossis", ["icd10"]),  # 'diagnosis' typo
    ],
)
def test_fuzzy_role_mappings(header: str, expected: list[str]) -> None:
    assert header_roles(header) == expected


# ── Negative space: structural / non-PII headers map to nothing ──────────────


@pytest.mark.parametrize(
    "header",
    [
        "order_id",
        "product_code",
        "sku",
        "amount",
        "quantity",
        "status_code",
        "serial_number",
        "claim_id",
        "visit_id",  # 'visit' must NOT imply iso_date here
        "encounter_id",
        "reference_no",
        "transaction_id",
        "user_id",
        "employee_id",
        "zip_code",
        "hash_id",
        "index",
        "record_count",  # 'record' must NOT imply mrn
        "first_name",
        "gender",
        "col_a",
        "x1",
        "id",  # bare stopword token
    ],
)
def test_non_pii_headers_map_to_no_role(header: str) -> None:
    assert header_roles(header) == []


# ── Ambiguous tokens are deliberately excluded ───────────────────────────────


@pytest.mark.parametrize(
    "header",
    [
        "security_id",  # ssn|cvv ambiguous -> neither
        "account_no",  # pan|iban ambiguous -> neither
        "tax_ref",  # ssn|npi ambiguous -> neither
    ],
)
def test_ambiguous_tokens_excluded(header: str) -> None:
    assert header_roles(header) == []


# ── Structural guarantees ────────────────────────────────────────────────────


def test_roles_are_sorted_and_deduped() -> None:
    """Multi-token headers return a sorted, de-duplicated role list."""
    # 'card' -> pan and 'cvv' -> cvv; both legitimately present, sorted.
    assert header_roles("card_cvv") == ["cvv", "pan"]


def test_deterministic() -> None:
    """Same input -> identical output across repeated calls."""
    for _ in range(3):
        assert header_roles("patient_mrn_diagnosis_code") == roles_from_tokens(
            ["patient", "mrn", "diagnosis", "code"]
        )


def test_roles_from_tokens_matches_header_roles() -> None:
    """The token entry point and the name wrapper agree."""
    assert roles_from_tokens(["dx", "cd"]) == header_roles("dx_cd")


def test_empty_header() -> None:
    assert header_roles("") == []
    assert roles_from_tokens([]) == []


# ── Every canonical role is reachable (no dead role in the table) ─────────────


def test_all_roles_reachable() -> None:
    """Each declared canonical role is emitted by at least one probe header.

    Guards against a role whose synonyms were all removed (dead feature).
    """
    from decoy_engine.storm.features.header_lexicon import _ROLE_SYNONYMS

    probes = {
        "ssn": "ssn",
        "email": "email",
        "pan": "card_number",
        "iban": "iban",
        "icd10": "dx",
        "iso_date": "service_date",
        "npi": "npi",
        "mrn": "mrn",
        "health_plan_id": "plan_id",
        "cvv": "cvv",
    }
    for role in _ROLE_SYNONYMS:
        assert role in probes, f"probe missing for role {role!r}"
        assert role in header_roles(probes[role]), f"role {role!r} unreachable"


# ── Featurizer wiring: role_{canonical} features are emitted ──────────────────


def test_featurizer_emits_role_features() -> None:
    from decoy_engine.storm.model_pack.featurizer import flatten_features

    flat = flatten_features({"header_tokens": ["dx", "cd"], "row_count": 1})
    assert flat.get("role_icd10") == 1.0
    # No spurious role features for a token that maps to nothing.
    flat2 = flatten_features({"header_tokens": ["order"], "row_count": 1})
    assert not [k for k in flat2 if k.startswith("role_")]
