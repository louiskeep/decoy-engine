"""Snapshot harness for STORM detector name-hint coverage.

Per engineering-best-practices §1.1 (snapshot before extraction): the
``_NAME_HINTS`` dict in ``decoy_engine/storm/detectors.py`` is being
moved from a hard-coded Python dict into versioned YAML files under
``decoy_engine/storm/name_hints/v1/``. This snapshot captures the
``hits_name_hint(detector_id, col_name)`` matrix BEFORE the refactor;
the post-refactor run must produce the same digest.

What's hashed:
  - For every detector_id in the registry, a dict of {column_name: bool}
    for every header in CORPUS.
  - Result is JSON-canonicalized (sort_keys=True) and SHA-256 hashed.
  - The full matrix is also written to the golden directory so a
    regression's diff shows exactly which (detector, header) pair
    flipped, not just "the hash changed".

CORPUS is built to cover:
  - Obvious matches (``email``, ``ssn``, ``customer_id``).
  - Abbreviated / enterprise forms (``EMP_FN``, ``MBR_DOB``, ``PROV_NPI``).
  - False-positive traps (``email_count``, ``email_opt_in``,
    ``phone_book_id``, ``zip_file_path``).
  - Mixed-case + suffix/prefix permutations (``addr1``, ``LASTNAME``,
    ``cust_fn``, ``birth_dt``).
  - Headers that should NEVER match anything (``id``, ``value``,
    ``timestamp``, ``status``, ``count``).

Adding a fixture:
  1. Append to CORPUS below.
  2. Run: UPDATE_SNAPSHOTS=1 pytest tests/snapshots/test_name_hints_baseline.py
  3. Inspect the generated golden file and commit it.

Updating an existing fixture's expected output:
  Only do this when name-hint coverage has intentionally changed
  (e.g. a YAML edit adds or removes a pattern). The commit MUST
  explain why the snapshot drift is legitimate.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from decoy_engine.storm.detectors import _NAME_HINTS, hits_name_hint

GOLDEN = Path(__file__).parent / "golden" / "name_hints"


# ── corpus ────────────────────────────────────────────────────────────────

# Curated representative header set. Order matters only for snapshot
# stability (sort_keys handles canonical hashing). New entries should
# group with their kind.
CORPUS: list[str] = [
    # Obvious matches across the common detector types.
    "email", "email_address", "EMAIL", "user.email", "contact_email",
    "ssn", "social_security_number", "SSN", "ss_num",
    "phone", "phone_number", "Cell_Phone", "work_phone", "primary_phone",
    "zip", "zip_code", "postal_code", "Zipcode",
    "first_name", "firstname", "FN", "f_name", "given_name",
    "last_name", "lastname", "LN", "surname",
    "name", "full_name", "customer_name", "middle_name",
    "date", "dob", "birth_date", "date_of_birth", "DT",
    "address", "addr", "addr1", "street", "street_address",
    "pan", "credit_card", "card_number", "CC_NUM",
    "cvv", "card_security_code",
    "iban", "bank_account",
    "ip", "ipv4", "ip_address", "client_ip",
    "icd", "icd10", "diagnosis_code",
    "npi", "provider_npi", "physician_id",
    "mrn", "medical_record", "patient_id", "customer_id", "employee_id",
    "url", "uri", "website",
    "fax", "fax_number", "facsimile",
    "beneficiary", "member_id", "subscriber_id",
    "license", "drivers_license", "DL_NUM",
    "vin", "license_plate",
    "device_id", "serial_number", "UDI",
    "fingerprint", "biometric_id",
    # Abbreviated / enterprise forms (the DTGEB / MBRDOB family).
    "EMP_FN", "EMP_LN", "EMP_ID",
    "CUST_FN", "CUST_LN", "CUST_ID",
    "PT_FN", "PT_LN", "PT_ID",
    "PAT_FN", "PAT_LN", "PAT_ID",
    "MBR_DOB", "MBR_FN", "MBR_LN",
    "PROV_NPI", "PROV_LOC_PMP_PRFL_DIM_SK",
    "RGN_GEO_DIM_SK", "SAK_RE_PMP_ASSIGN",
    # False-positive traps: contain a hint word but mean something else.
    "email_count", "email_opt_in",
    "phone_book_id", "phone_record_id",
    "zip_file_path", "zip_file_size",
    "date_created", "date_updated",  # SHOULD match date hints (these are dates)
    "address_count", "address_id",
    "card_count", "card_image",
    "ip_count", "ip_version",
    # Generic columns that should never match anything in particular.
    "id", "value", "amount", "timestamp", "status", "count",
    "label", "type", "kind", "level", "score", "rate",
    "name_count",  # tricky: contains "name" but is a count metric
    "code",  # too generic to match
    # Case-variant repeats to catch case-insensitivity regressions.
    "DOB", "Dob", "dob",
    "EMAIL", "Email", "email",
    "Phone_Number", "PHONE_NUMBER", "phone_number",
]


# ── snapshot driver ──────────────────────────────────────────────────────


def _build_matrix() -> dict[str, dict[str, bool]]:
    """For each detector_id, a dict of {header: hits_name_hint(...)}.

    Sorting both axes for canonical-form output regardless of
    insertion order in _NAME_HINTS or CORPUS.
    """
    detector_ids = sorted(_NAME_HINTS.keys())
    headers = sorted(set(CORPUS))
    return {
        det: {hdr: hits_name_hint(det, hdr) for hdr in headers}
        for det in detector_ids
    }


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True)


def _digest(obj: object) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def _golden_paths() -> tuple[Path, Path]:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    return GOLDEN / "matrix.json", GOLDEN / "matrix.sha256"


def test_name_hints_baseline_matrix() -> None:
    """The full (detector_id, header) -> hits matrix is unchanged.

    Drift is the signal we want: a YAML edit that drops a pattern OR
    a loader change that picks up the wrong file will flip at least
    one cell in the matrix and break this assertion with a clear
    line-by-line diff.
    """
    matrix = _build_matrix()
    digest = _digest(matrix)
    matrix_path, digest_path = _golden_paths()

    if os.environ.get("UPDATE_SNAPSHOTS"):
        matrix_path.write_text(_canonical_json(matrix) + "\n", encoding="utf-8")
        digest_path.write_text(digest + "\n", encoding="utf-8")
        pytest.skip("UPDATE_SNAPSHOTS=1 -- baseline rewritten")

    if not matrix_path.exists() or not digest_path.exists():
        pytest.fail(
            f"No baseline at {matrix_path} or {digest_path}. "
            "Generate with: UPDATE_SNAPSHOTS=1 pytest "
            "tests/snapshots/test_name_hints_baseline.py"
        )

    expected_matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    expected_digest = digest_path.read_text(encoding="utf-8").strip()

    # Hash first (cheap failure). If it differs, fall through to
    # the structural assert which surfaces the offending cells.
    assert digest == expected_digest, (
        f"name-hint matrix digest drift: expected={expected_digest} got={digest}. "
        "Compare matrix.json to find the changed (detector_id, header) cells."
    )
    assert matrix == expected_matrix, (
        "name-hint matrix structural drift; digest matched but cells differ "
        "(unexpected -- file a bug)."
    )
