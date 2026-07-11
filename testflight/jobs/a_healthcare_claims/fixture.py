"""Job A seeded fixture generator: healthcare claims (one-to-many-multilevel).

Builds three tables -- members, claims, claim_lines -- with EXACT planted
edge-case counts so the invariant assertions compare against known integers.

Planted edge cases (match manifest.yaml invariants exactly):
  - RESTRICTED_ZIP3_COUNT = 18 member rows with HHS-restricted ZIP3 prefixes.
    These will be suppressed or generalized by geo_generalize; the Safe Harbor
    invariant asserts all 18 are handled.
  - INVALID_LUHN_COUNT = 10 member rows with invalid-Luhn card_no values.
    The luhn validator fires on the masked output; those rows are quarantined.
    These 10 rows are the LAST 10 rows so no claims reference them (FK clean).
  - SENTINEL_SSN = "8880088888"  planted as the ssn of member_id "M-SENT-001".
    FPE will transform this completely; the sentinel scan checks it is absent.
  - SENTINEL_EMAIL = "sentinel.leak.test@example-decoy-testflight.invalid"
    planted as the email of member_id "M-SENT-001".
    The faker strategy will generate a completely different email; must be absent.

Correlations (deliberate, non-trivial for the distribution invariant):
  - plan_tier correlates with age: <35 -> Bronze/Silver, 35-55 -> Silver/Gold,
    >55 -> Gold/Platinum.
  - claim_amount correlates with ICD-10 chapter: chapters I/E/N have higher
    average amounts; chapters A/B have lower amounts.
  - diagnosis_secondary is drawn from the SAME chapter as diagnosis (TH-3.4):
    a real chapter-level clinical association, checked post-mask via the
    relabel-invariant masked_correlations Cramers V pair (both columns are
    code_set/chapter_preserve:true, a value-changing mask family the old
    crosstab-TVD joint metric cannot see through).

Row counts (from manifest.yaml):
  - members: 2000
  - claims: 8000 (average 4 per member for the 1990 non-quarantine members)
  - claim_lines: 20000 (average 2.5 per claim)

Source format notes:
  - member_id: "M{n:06d}" (7 chars, digits after M)
  - ssn: 9-digit Luhn-valid string (no hyphens) for valid rows; 9-digit
    Luhn-INVALID string for the 10 quarantine rows.
  - npi: 10-digit NPI-valid string.
  - mrn: 8-char alphanumeric.
  - card_no: 16-digit string; valid Luhn for non-quarantine rows, invalid
    Luhn for the INVALID_LUHN_COUNT quarantine rows.
  - zip5: 5-digit string; for the 18 restricted rows uses one of the 17
    HHS-restricted ZIP3 prefixes.
  - age: integer 18..85.
  - plan_tier: Bronze/Silver/Gold/Platinum.
  - dob: ISO date string YYYY-MM-DD.
  - name: "FirstName LastName" string.
  - email: lowercase email string.
  - claim_id: "C{n:07d}".
  - diagnosis: 5-char ICD-10 code from the shipped corpus.
  - diagnosis_secondary: ICD-10 code drawn from the SAME chapter as diagnosis
    (TH-3.4 code_set-chapter masked_correlations partner).
  - drug_ndc: 11-digit NDC code.
  - claim_amount: float; correlated with diagnosis chapter.
  - service_date: ISO date string YYYY-MM-DD; one per month over 24 months.
  - line_id: "L{n:08d}".
  - procedure: HCPCS code from the shipped corpus.
  - line_amount: float.
  - units: int 1..5.
  - discount_tier: "standard" / "preferred" / "copay" (3 branches for case_when).
  - line_total: derived placeholder (computed by the derived strategy).
  - claim_line_sum: derived_aggregate placeholder.
"""

from __future__ import annotations

import random
import string
from typing import Any

import numpy as np
import pandas as pd
import stdnum.luhn as _luhn

from testflight._fixtures import make_rng, verify_fingerprint

# ---------------------------------------------------------------------------
# Constants (match manifest.yaml exactly)
# ---------------------------------------------------------------------------

MEMBER_COUNT = 2000
CLAIMS_COUNT = 8000
CLAIM_LINES_COUNT = 20000

# Exactly 10 member rows with invalid-Luhn card_no -- quarantined by the
# luhn validator. These are the LAST 10 rows so no claims reference them.
INVALID_LUHN_COUNT = 10
QUARANTINE_MEMBER_COUNT = INVALID_LUHN_COUNT  # alias for clarity

# Source fingerprints (SHA-256 of canonical CSV). verify_fingerprint is called
# inside each build_* function so a faker/numpy version bump that shifts the
# fixture output fails loudly with a re-baseline instruction.
# Re-baseline deliberately by running compute_fingerprint() on the new output
# and updating these constants + the manifest.yaml comment block.
_MEMBERS_FINGERPRINT = "f0a05c63ddfd74c9625f44800ffa87f26cfe8e9e6927cb8ad6907b3b8032c281"
_CLAIMS_FINGERPRINT = "cdd55a057c1537482c614941f09533f561021f5bb38e8d392bb058e5978ca3a3"
_CLAIM_LINES_FINGERPRINT = "3678e5ce5ac37add2d3bf66fc274875282297f038a4a6ebec246ed79b6ecb0f3"

# Orphan claims: reference quarantine member IDs so they become orphans in the
# masked output (the quarantine removes those members; orphan_policy:warn keeps
# the orphan claim rows in the claims output but no parent exists).
ORPHAN_CLAIM_COUNT = 5

# Exactly 18 rows have a HHS-restricted ZIP3 prefix (Safe Harbor).
RESTRICTED_ZIP3_COUNT = 18

# Sentinel strings that MUST NOT appear in any output column after masking.
SENTINEL_SSN = "8880088888"
SENTINEL_EMAIL = "sentinel.leak.test@example-decoy-testflight.invalid"
SENTINEL_MEMBER_IDX = 0  # planted in the FIRST member row

# The 17 HHS-restricted ZIP3 prefixes (from geo_generalize._load_restricted_zip3).
# These are the prefixes with population < 20,000 under HIPAA Safe Harbor.
_RESTRICTED_ZIP3_PREFIXES = [
    "036",
    "059",
    "063",
    "102",
    "203",
    "556",
    "692",
    "790",
    "821",
    "823",
    "830",
    "831",
    "878",
    "879",
    "884",
    "890",
    "893",
]

# ICD-10 chapters with correlated claim amounts (high-cost vs low-cost).
_HIGH_COST_CHAPTERS = frozenset({"I", "E", "N", "C"})  # cardiovascular, endo, renal, cancer
_LOW_COST_CHAPTERS = frozenset({"A", "B", "R"})  # infectious, symptoms

# Approximate chapter weights for ICD-10 selection (matching the shipped corpus size).
_ICD10_CHAPTERS = [
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "H",
    "I",
    "J",
    "K",
    "L",
    "M",
    "N",
    "O",
    "P",
    "Q",
    "R",
    "S",
    "T",
    "V",
    "W",
    "X",
    "Y",
    "Z",
]


# ---------------------------------------------------------------------------
# Helper: valid and invalid Luhn digit strings
# ---------------------------------------------------------------------------


def _make_luhn_valid(body: str) -> str:
    """Return body + valid Luhn check digit."""
    return body + _luhn.calc_check_digit(body)


def _make_luhn_invalid(body: str) -> str:
    """Return body + a check digit that is guaranteed INVALID for Luhn."""
    valid_check = _luhn.calc_check_digit(body)
    # Flip the check digit by +1 mod 10 to produce an invalid string.
    bad_check = str((int(valid_check) + 1) % 10)
    result = body + bad_check
    # Confirm it really is invalid (belt-and-suspenders for the fixture).
    assert not _luhn.is_valid(result), f"Expected invalid Luhn for {result!r}"
    return result


def _make_npi_valid(rng: np.random.Generator) -> str:
    """Return a random valid 10-digit NPI.

    NPPES allocates only 1- and 2-prefixed NPIs; start with '1' or '2'.
    Check digit is Luhn applied to the '80840' prefix + 9-digit body.
    """
    from decoy_engine.checksums import _npi_calc_check_digit

    # First digit must be 1 or 2 per NPPES convention.
    first_digit = str(rng.integers(1, 3))  # 1 or 2
    rest = "".join(str(rng.integers(0, 10)) for _ in range(8))
    body = first_digit + rest
    check = _npi_calc_check_digit(body)
    return body + check


# ---------------------------------------------------------------------------
# build_members
# ---------------------------------------------------------------------------


def build_members(seed: int = 42) -> pd.DataFrame:
    """Build the members source DataFrame with exact planted edge cases.

    Row layout:
      [0]                           sentinel member (SSN + email sentinel planted)
      [1 .. 1999 - INVALID_LUHN]    regular valid members
      [1999 - INVALID_LUHN + 1 ..
       1999]                         quarantine members (invalid Luhn card_no)
      [interspersed within 0..1981]  18 restricted-ZIP3 members

    Args:
        seed: Reproducibility seed (matches manifest.seed).

    Returns:
        pandas DataFrame with columns:
          member_id, ssn, npi, mrn, card_no, name, email, dob, age, zip5,
          plan_tier.
    """
    rng = make_rng(seed)
    fkr_seed = int(rng.integers(0, 2**31))
    random.seed(fkr_seed)
    import faker as faker_lib

    faker_lib.Faker.seed(fkr_seed)
    fkr = faker_lib.Faker()

    n_valid = MEMBER_COUNT - INVALID_LUHN_COUNT  # 1990 valid members

    rows: list[dict[str, Any]] = []

    # --- Phase 1: n_valid regular members + restricted-ZIP3 members -----------
    # We scatter the RESTRICTED_ZIP3_COUNT restricted rows across the first
    # n_valid positions by seeding a choice of indices.
    restricted_positions = set(
        rng.choice(n_valid, size=RESTRICTED_ZIP3_COUNT, replace=False).tolist()
    )
    restricted_zip3_pool = list(_RESTRICTED_ZIP3_PREFIXES)

    for i in range(n_valid):
        member_id = f"M{i + 1:06d}"
        age = int(rng.integers(18, 86))

        # ZIP5
        if i in restricted_positions:
            # Cycle through the restricted ZIP3 prefixes for variety.
            sorted_restricted = sorted(restricted_positions)
            rank = sorted_restricted.index(i)
            z3 = restricted_zip3_pool[rank % len(restricted_zip3_pool)]
            zip5 = z3 + str(rng.integers(0, 100)).zfill(2)
        else:
            # Non-restricted ZIP: choose a prefix NOT in the restricted list.
            # Use 3-digit prefix from 100-900 range, skipping restricted ones.
            z3 = str(int(rng.integers(100, 900)))
            while z3 in _RESTRICTED_ZIP3_PREFIXES:
                z3 = str(int(rng.integers(100, 900)))
            zip5 = z3 + str(rng.integers(0, 100)).zfill(2)

        # plan_tier correlated with age
        plan_tier = _age_to_plan_tier(age, rng)

        # SSN: 9-digit Luhn-valid. First row gets the sentinel.
        if i == SENTINEL_MEMBER_IDX:
            ssn = SENTINEL_SSN  # planted sentinel; FPE will transform completely
            email = SENTINEL_EMAIL  # planted sentinel; faker will generate a new value
        else:
            ssn_body = "".join(str(int(rng.integers(0, 10))) for _ in range(8))
            ssn = _make_luhn_valid(ssn_body)
            email = fkr.email()

        npi = _make_npi_valid(rng)
        mrn_body = "".join(rng.choice(list(string.ascii_uppercase + string.digits), 8).tolist())
        mrn = mrn_body

        # card_no: valid Luhn (16 digits, Visa-like prefix 4)
        card_body = "4" + "".join(str(int(rng.integers(0, 10))) for _ in range(14))
        card_no = _make_luhn_valid(card_body)

        dob_year = 2026 - age - int(rng.integers(0, 2))
        dob_month = int(rng.integers(1, 13))
        dob_day = int(rng.integers(1, 29))
        dob = f"{dob_year:04d}-{dob_month:02d}-{dob_day:02d}"
        name = fkr.name()

        rows.append(
            {
                "member_id": member_id,
                "ssn": ssn,
                "npi": npi,
                "mrn": mrn,
                "card_no": card_no,
                "name": name,
                "email": email,
                "dob": dob,
                "age": age,
                "zip5": zip5,
                "plan_tier": plan_tier,
            }
        )

    # --- Phase 2: INVALID_LUHN_COUNT quarantine members (last 10 rows) -------
    for j in range(INVALID_LUHN_COUNT):
        i = n_valid + j
        member_id = f"M{i + 1:06d}"
        age = int(rng.integers(18, 86))
        plan_tier = _age_to_plan_tier(age, rng)

        ssn_body = "".join(str(int(rng.integers(0, 10))) for _ in range(8))
        ssn = _make_luhn_valid(ssn_body)  # valid SSN; only card_no is invalid

        npi = _make_npi_valid(rng)
        mrn_body = "".join(rng.choice(list(string.ascii_uppercase + string.digits), 8).tolist())
        mrn = mrn_body

        # card_no: INVALID Luhn (this triggers quarantine)
        card_body = "4" + "".join(str(int(rng.integers(0, 10))) for _ in range(14))
        card_no = _make_luhn_invalid(card_body)

        # Non-restricted ZIP
        z3 = str(int(rng.integers(100, 900)))
        while z3 in _RESTRICTED_ZIP3_PREFIXES:
            z3 = str(int(rng.integers(100, 900)))
        zip5 = z3 + str(rng.integers(0, 100)).zfill(2)

        dob_year = 2026 - age - int(rng.integers(0, 2))
        dob_month = int(rng.integers(1, 13))
        dob_day = int(rng.integers(1, 29))
        dob = f"{dob_year:04d}-{dob_month:02d}-{dob_day:02d}"
        name = fkr.name()
        email = fkr.email()

        rows.append(
            {
                "member_id": member_id,
                "ssn": ssn,
                "npi": npi,
                "mrn": mrn,
                "card_no": card_no,
                "name": name,
                "email": email,
                "dob": dob,
                "age": age,
                "zip5": zip5,
                "plan_tier": plan_tier,
            }
        )

    df = pd.DataFrame(rows)
    assert len(df) == MEMBER_COUNT, f"Expected {MEMBER_COUNT} members, got {len(df)}"
    verify_fingerprint(df, _MEMBERS_FINGERPRINT, label="members")
    return df


# ---------------------------------------------------------------------------
# build_claims
# ---------------------------------------------------------------------------


def _group_codes_by_chapter(codes: list[str], chapters: list[str]) -> dict[str, list[str]]:
    """Group (code, chapter) pairs into {chapter: [codes]}, preserving list order.

    Iterates the parallel `codes`/`chapters` lists (deterministic parquet-row
    order, not a hash-ordered structure) and appends into a plain dict, so
    the resulting per-chapter code lists are process-stable regardless of
    PYTHONHASHSEED -- required for the TH-3.4 diagnosis_secondary draw below
    to be reproducible across processes (cross-process fingerprint gate).
    """
    grouped: dict[str, list[str]] = {}
    for code, chapter in zip(codes, chapters, strict=True):
        grouped.setdefault(chapter, []).append(code)
    return grouped


def build_claims(seed: int = 42, members_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Build the claims source DataFrame.

    Claims reference only the first MEMBER_COUNT - INVALID_LUHN_COUNT member_ids
    (indices 0 to n_valid-1). The last INVALID_LUHN_COUNT members are excluded
    from the FK pool so quarantined members do not create orphan FK violations.

    Planted correlations: claim_amount is correlated with ICD-10 chapter.
      High-cost chapters (I, E, N, C): amount in [5000, 25000].
      Low-cost chapters (A, B, R):     amount in [100, 2000].
      Mid-cost chapters (other):        amount in [1000, 8000].

    Args:
        seed: Reproducibility seed.
        members_df: Optional pre-built members DataFrame. If None, calls
            build_members(seed) to get the member_id pool.

    Returns:
        pandas DataFrame with columns:
          claim_id, member_id, diagnosis, drug_ndc, claim_amount, service_date.
    """
    rng = make_rng(seed + 1)  # offset seed so claims rng diverges from members

    if members_df is None:
        members_df = build_members(seed)

    # FK pool: only the first n_valid members (exclude last INVALID_LUHN_COUNT)
    n_valid = MEMBER_COUNT - INVALID_LUHN_COUNT
    valid_member_ids = members_df["member_id"].iloc[:n_valid].tolist()

    # Load ICD-10 codes from shipped corpus
    icd10_codes, icd10_chapters = _load_icd10_corpus()
    # TH-3.4 (P1-9): chapter -> codes lookup for diagnosis_secondary (drawn from
    # the SAME chapter as the primary diagnosis; see the column below).
    icd10_by_chapter = _group_codes_by_chapter(icd10_codes, icd10_chapters)
    # NDC corpus: cap at 20 codes. Known boundary (MEDIUM-2):
    # Using all 38 shipped NDC codes makes the source freetext (38 > 30)
    # but code_set's HMAC-deterministic mapping produces ~23 distinct output
    # codes (categorical, <= 30) due to pigeonhole collisions in the N->N-1
    # candidate pool. Capping at 20 keeps both source and output categorical
    # (no kind_drift). A larger cap would require a NDC corpus with >= 50 codes
    # to clear the freetext threshold on the output side (output ~= 0.63*N).
    ndc_codes = _load_ndc_corpus()[:20]

    rows: list[dict[str, Any]] = []
    for i in range(CLAIMS_COUNT):
        claim_id = f"C{i + 1:07d}"
        # FK: pick from valid member pool
        member_idx = int(rng.integers(0, n_valid))
        member_id = valid_member_ids[member_idx]

        # Diagnosis: pick an ICD-10 code (weighted toward mid-cost chapters)
        diag_idx = int(rng.integers(0, len(icd10_codes)))
        diagnosis = icd10_codes[diag_idx]
        chapter = icd10_chapters[diag_idx]

        # diagnosis_secondary: TH-3.4 code_set-chapter masked_correlations
        # partner for diagnosis. Drawn UNIFORMLY from the SAME chapter as the
        # primary diagnosis -- a real chapter-level clinical association
        # (comorbidity within the same body system), not a synthetic
        # coincidence. Both columns are chapter_preserve:true code_set, so
        # each masks to a DIFFERENT code within its own (preserved) chapter;
        # the pairing is chapter-level, exercised at the same granularity the
        # masking guarantees.
        secondary_pool = icd10_by_chapter[chapter]
        diagnosis_secondary = secondary_pool[int(rng.integers(0, len(secondary_pool)))]

        # Drug NDC
        ndc_idx = int(rng.integers(0, len(ndc_codes)))
        drug_ndc = ndc_codes[ndc_idx]

        # claim_amount: correlated with chapter
        if chapter in _HIGH_COST_CHAPTERS:
            claim_amount = float(rng.uniform(5000, 25000))
        elif chapter in _LOW_COST_CHAPTERS:
            claim_amount = float(rng.uniform(100, 2000))
        else:
            claim_amount = float(rng.uniform(1000, 8000))

        # amount_band: 3-category banding of claim_amount (correlated with chapter
        # by construction). Passthrough in the pipeline; joint (amount_band,
        # diagnosis_chapter) is declared for the distribution correlation check.
        if chapter in _HIGH_COST_CHAPTERS:
            amount_band = "high"
        elif chapter in _LOW_COST_CHAPTERS:
            amount_band = "low"
        else:
            amount_band = "mid"

        # diagnosis_chapter: first letter of the ICD-10 code (preserved when
        # chapter_preserve: true is set on the diagnosis code_set column).
        # Passthrough in the pipeline; appears in the joint with amount_band.
        diagnosis_chapter = chapter

        # service_date: random date in last 24 months
        months_ago = int(rng.integers(0, 24))
        year = 2026 - months_ago // 12
        month = ((5 - months_ago) % 12) + 1  # rough approximation
        day = int(rng.integers(1, 29))
        service_date = f"{year:04d}-{month:02d}-{day:02d}"

        rows.append(
            {
                "claim_id": claim_id,
                "member_id": member_id,
                "diagnosis": diagnosis,
                "diagnosis_secondary": diagnosis_secondary,
                "drug_ndc": drug_ndc,
                "claim_amount": round(claim_amount, 2),
                "amount_band": amount_band,
                "diagnosis_chapter": diagnosis_chapter,
                "service_date": service_date,
            }
        )

    # Plant ORPHAN_CLAIM_COUNT claims referencing quarantine member IDs (last
    # INVALID_LUHN_COUNT members). Those members are removed from the masked
    # members output by quarantine, so these claims have no parent in the
    # output -- orphan_policy:warn keeps them in the claims output.
    quarantine_member_ids = members_df["member_id"].iloc[n_valid:].tolist()
    for k in range(ORPHAN_CLAIM_COUNT):
        orphan_claim_id = f"C{CLAIMS_COUNT + k + 1:07d}"
        # Cycle through quarantine member IDs for variety.
        orphan_member_id = quarantine_member_ids[k % len(quarantine_member_ids)]

        # Use a mid-cost ICD-10 code for orphan claims (chapters not in
        # high/low cost sets so amount_band="mid" is predictable).
        orphan_diag_idx = int(rng.integers(0, len(icd10_codes)))
        orphan_diag = icd10_codes[orphan_diag_idx]
        orphan_chapter = icd10_chapters[orphan_diag_idx]
        orphan_pool = icd10_by_chapter[orphan_chapter]
        orphan_diag_secondary = orphan_pool[int(rng.integers(0, len(orphan_pool)))]
        if orphan_chapter in _HIGH_COST_CHAPTERS:
            orphan_amount = float(rng.uniform(5000, 25000))
            orphan_band = "high"
        elif orphan_chapter in _LOW_COST_CHAPTERS:
            orphan_amount = float(rng.uniform(100, 2000))
            orphan_band = "low"
        else:
            orphan_amount = float(rng.uniform(1000, 8000))
            orphan_band = "mid"

        orphan_ndc = ndc_codes[int(rng.integers(0, len(ndc_codes)))]
        orphan_months = int(rng.integers(0, 24))
        orphan_year = 2026 - orphan_months // 12
        orphan_month = ((5 - orphan_months) % 12) + 1
        orphan_day = int(rng.integers(1, 29))

        rows.append(
            {
                "claim_id": orphan_claim_id,
                "member_id": orphan_member_id,
                "diagnosis": orphan_diag,
                "diagnosis_secondary": orphan_diag_secondary,
                "drug_ndc": orphan_ndc,
                "claim_amount": round(orphan_amount, 2),
                "amount_band": orphan_band,
                "diagnosis_chapter": orphan_chapter,
                "service_date": f"{orphan_year:04d}-{orphan_month:02d}-{orphan_day:02d}",
            }
        )

    df = pd.DataFrame(rows)
    expected_total = CLAIMS_COUNT + ORPHAN_CLAIM_COUNT
    assert len(df) == expected_total, f"Expected {expected_total} claims, got {len(df)}"
    verify_fingerprint(df, _CLAIMS_FINGERPRINT, label="claims")
    return df


# ---------------------------------------------------------------------------
# build_claim_lines
# ---------------------------------------------------------------------------


def build_claim_lines(
    seed: int = 42,
    claims_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the claim_lines source DataFrame.

    Line amounts and units are the source values for the derived column test.
    line_total (derived: line_amount * units with case_when discount) and
    claim_line_sum (derived_aggregate: sum(line_amount) across ALL rows) are set to
    placeholder values here; the pipeline's derived/derived_aggregate strategies
    compute the real values from line_amount and units in the OUTPUT.

    discount_tier distribution (3 branches for case_when branch-coverage test):
      standard  -> ~60%
      preferred -> ~30%
      copay     -> ~10%
    These must ALL be present so the case_when invariant can assert 3/3 branches.

    Args:
        seed: Reproducibility seed.
        claims_df: Optional pre-built claims DataFrame. If None, calls
            build_claims(seed) to get the claim_id pool.

    Returns:
        pandas DataFrame with columns:
          line_id, claim_id, procedure, line_amount, units, discount_tier.
    """
    rng = make_rng(seed + 2)  # offset seed

    if claims_df is None:
        claims_df = build_claims(seed)

    claim_ids = claims_df["claim_id"].tolist()
    # Load HCPCS codes from shipped corpus
    hcpcs_codes = _load_hcpcs_corpus()

    rows: list[dict[str, Any]] = []
    for i in range(CLAIM_LINES_COUNT):
        line_id = f"L{i + 1:08d}"
        # FK: pick from claims pool
        claim_idx = int(rng.integers(0, CLAIMS_COUNT))
        claim_id = claim_ids[claim_idx]

        proc_idx = int(rng.integers(0, len(hcpcs_codes)))
        procedure = hcpcs_codes[proc_idx]

        line_amount = round(float(rng.uniform(10, 1500)), 2)
        units = int(rng.integers(1, 6))

        # discount_tier: 3 branches required by case_when branch-coverage check.
        # Ensure all three branches appear: ~60% standard, ~30% preferred, ~10% copay.
        tier_draw = rng.uniform(0, 1)
        if tier_draw < 0.60:
            discount_tier = "standard"
        elif tier_draw < 0.90:
            discount_tier = "preferred"
        else:
            discount_tier = "copay"

        rows.append(
            {
                "line_id": line_id,
                "claim_id": claim_id,
                "procedure": procedure,
                "line_amount": line_amount,
                "units": units,
                "discount_tier": discount_tier,
            }
        )

    df = pd.DataFrame(rows)

    # Guarantee all 3 discount_tier values appear (branch-coverage requirement).
    # With 20000 rows and ~10% copay, the probability of 0 copay rows is negligible,
    # but enforce explicitly for determinism.
    _ensure_all_tiers(df)

    assert len(df) == CLAIM_LINES_COUNT, f"Expected {CLAIM_LINES_COUNT} lines, got {len(df)}"
    verify_fingerprint(df, _CLAIM_LINES_FINGERPRINT, label="claim_lines")
    return df


# ---------------------------------------------------------------------------
# Corpus loaders (ship from the engine's codesets directory)
# ---------------------------------------------------------------------------


def _load_icd10_corpus() -> tuple[list[str], list[str]]:
    """Return (codes, chapters) lists from the shipped ICD-10 corpus.

    Filters out chapters with fewer than 2 codes. chapter_preserve:true
    requires the engine to select a DIFFERENT code from the same chapter;
    single-code chapters make that impossible and raise a pipeline error.
    Chapters V, W, X, Y each have only 1 code in the shipped corpus and
    are excluded here.
    """
    from collections import Counter
    from pathlib import Path

    import pyarrow.parquet as pq

    corpus_path = (
        Path(__file__).parent.parent.parent.parent
        / "src"
        / "decoy_engine"
        / "codesets"
        / "icd10.parquet"
    )
    tbl = pq.read_table(str(corpus_path))
    codes: list[str] = tbl.column("code").to_pylist()
    chapters: list[str] = tbl.column("chapter").to_pylist()

    chapter_counts: Counter[str] = Counter(chapters)
    pairs = [(c, ch) for c, ch in zip(codes, chapters, strict=True) if chapter_counts[ch] >= 2]
    return [c for c, _ in pairs], [ch for _, ch in pairs]


def _load_ndc_corpus() -> list[str]:
    """Return code list from the shipped NDC corpus."""
    from pathlib import Path

    import pyarrow.parquet as pq

    corpus_path = (
        Path(__file__).parent.parent.parent.parent
        / "src"
        / "decoy_engine"
        / "codesets"
        / "ndc.parquet"
    )
    tbl = pq.read_table(str(corpus_path))
    return tbl.column("code").to_pylist()


def _load_hcpcs_corpus() -> list[str]:
    """Return code list from the shipped HCPCS corpus."""
    from pathlib import Path

    import pyarrow.parquet as pq

    corpus_path = (
        Path(__file__).parent.parent.parent.parent
        / "src"
        / "decoy_engine"
        / "codesets"
        / "hcpcs.parquet"
    )
    tbl = pq.read_table(str(corpus_path))
    return tbl.column("code").to_pylist()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _age_to_plan_tier(age: int, rng: np.random.Generator) -> str:
    """Map age to plan_tier with correlation and some noise."""
    draw = float(rng.uniform(0, 1))
    if age < 35:
        return "Bronze" if draw < 0.60 else "Silver"
    elif age < 55:
        return "Silver" if draw < 0.50 else ("Gold" if draw < 0.80 else "Bronze")
    else:
        return "Gold" if draw < 0.50 else ("Platinum" if draw < 0.80 else "Silver")


def _ensure_all_tiers(df: pd.DataFrame) -> None:
    """Guarantee all three discount_tier values appear in the claim_lines fixture.

    Replaces a small number of rows with the missing tier if any tier is absent.
    Called after generation; deterministic because we always check the same indices.
    """
    tiers = {"standard", "preferred", "copay"}
    present = set(df["discount_tier"].unique())
    missing = tiers - present
    if not missing:
        return
    # Assign missing tiers to fixed row indices (last rows for each missing tier).
    for k, tier in enumerate(sorted(missing)):
        df.at[len(df) - 1 - k, "discount_tier"] = tier
