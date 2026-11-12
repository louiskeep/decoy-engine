"""Header pools and fixture helpers for the expanded ML corpus.

Moved verbatim from ``corpus.py`` (H-1 Wave B E3), which builds the fixtures
and imports what it needs from here.
"""

from __future__ import annotations

import string

import pandas as pd

from decoy_engine.profiling.eval.fixtures import LabeledFixture

# ===========================================================================
# Extended corpus -- ML2 LightGBM training (ml-benchmarking-and-privacy.md §B.3)
# Phase B rebaseline: ~2960 columns, diversified value formats + header styles.
# ===========================================================================
# Kept for provenance/documentation; corpus generation below is purely
# deterministic via col_idx-derived offsets (no RNG), so calling any of the
# build_* functions twice yields byte-identical output with no seed needed.
_EXT_SEED = 20260627
_EXT_ROWS = 60  # rows per extended-corpus column


def _make_fixture(name: str, col_name: str, values: list[str], label: str) -> LabeledFixture:
    """Build a single-column LabeledFixture."""
    return LabeledFixture(name, pd.DataFrame({col_name: values}), {col_name: label})


# ── header pools: clear canonical names / realistic abbreviations / cryptic ──
# Each type gets 3 short hand-written pools (clear, abbrev) plus a
# programmatically generated cryptic pool (_cryptic_headers) -- opaque
# single/double-char and "col_x"/"fNN" headers don't need hand-listing,
# they're a small closed pattern space. _pick_header rotates a column's
# header through the 3 style bands across its type's column range so the
# model sees clear AND cryptic headers for every type during training.


def _cryptic_headers(tag: str, count: int) -> list[str]:
    """Deterministically generate ``count`` opaque column-header strings.

    Cheap synthetic stand-ins for real-world opaque exports (col_a, f07,
    single/double-char headers) -- generated, not hand-listed, so scaling
    the corpus doesn't require ever-longer literal lists.
    """
    letters = string.ascii_lowercase
    out: list[str] = []
    i = 0
    while len(out) < count:
        bucket = i % 4
        if bucket == 0:
            out.append(f"{letters[i % 26]}{(i // 26) % 10}")
        elif bucket == 1:
            out.append(f"col_{letters[i % 26]}")
        elif bucket == 2:
            out.append(f"f{i:02d}")
        else:
            out.append(f"{tag}{i}")
        i += 1
    return out


def _pick_header(
    clear: list[str], abbrev: list[str], cryptic: list[str], col_idx: int, total: int
) -> str:
    """Rotate a column's header through 3 style bands across ``[0, total)``.

    Roughly the first third of a type's columns get clear canonical names,
    the next third realistic abbreviations, the last third cryptic/opaque
    headers.
    """
    band = (col_idx * 3) // max(total, 1)
    pool = (clear, abbrev, cryptic)[min(band, 2)]
    return pool[col_idx % len(pool)]


_SSN_CLEAR = [
    "ssn",
    "social_security_no",
    "social_security_number",
    "ss_number",
    "ssn_number",
    "tax_id",
    "taxpayer_id",
    "social_sec",
]
_SSN_ABBREV = ["ss_no", "ssn_num", "taxid", "soc_sec", "ss_id", "natl_id", "fed_id", "tin"]
_SSN_CRYPTIC = _cryptic_headers("s", 14)

_EMAIL_CLEAR = [
    "email",
    "email_address",
    "contact_email",
    "user_email",
    "primary_email",
    "work_email",
    "home_email",
    "e_mail",
]
_EMAIL_ABBREV = [
    "em_addr",
    "eml",
    "usr_email",
    "cust_email",
    "addr_email",
    "email_id",
    "mail_addr",
    "em",
]
_EMAIL_CRYPTIC = _cryptic_headers("e", 14)

_PAN_CLEAR = [
    "pan",
    "card_number",
    "credit_card_no",
    "cc_number",
    "card_no",
    "debit_card_no",
    "payment_card_number",
    "card_account_no",
]
_PAN_ABBREV = [
    "cc_no",
    "card_num",
    "pmt_card",
    "cardnbr",
    "acct_pan",
    "card_ref",
    "cc_num",
    "pay_card",
]
_PAN_CRYPTIC = _cryptic_headers("p", 14)

_IBAN_CLEAR = [
    "iban",
    "iban_number",
    "bank_account_no",
    "account_iban",
    "international_account",
    "bank_iban",
    "sepa_iban",
    "wire_iban",
]
_IBAN_ABBREV = [
    "iban_no",
    "bank_acct",
    "acct_no_intl",
    "bban",
    "routing_iban",
    "acct_iban",
    "iban_ref",
    "bnk_iban",
]
_IBAN_CRYPTIC = _cryptic_headers("i", 14)

_ICD10_CLEAR = [
    "icd10",
    "icd10_code",
    "diagnosis_code",
    "dx_code",
    "primary_dx",
    "icd_code",
    "diag_code",
    "clinical_code",
]
_ICD10_ABBREV = [
    "dx",
    "dx_cd",
    "icd_cd",
    "primary_diag",
    "diag",
    "icd10cm",
    "condition_cd",
    "cpt_icd",
]
_ICD10_CRYPTIC = _cryptic_headers("d", 14)

_ISO_DATE_CLEAR = [
    "service_date",
    "event_date",
    "record_date",
    "entry_date",
    "visit_date",
    "discharge_date",
    "admit_date",
    "encounter_date",
]
_ISO_DATE_ABBREV = ["svc_dt", "evt_dt", "rec_dt", "ent_dt", "vis_dt", "dis_dt", "adm_dt", "enc_dt"]
_ISO_DATE_CRYPTIC = _cryptic_headers("t", 14)

_NPI_CLEAR = [
    "npi",
    "npi_number",
    "provider_npi",
    "physician_npi",
    "billing_npi",
    "rendering_npi",
    "clinician_npi",
    "npi_code",
]
_NPI_ABBREV = [
    "npi_no",
    "prov_npi",
    "phys_npi",
    "bill_npi",
    "rend_npi",
    "clin_npi",
    "npi_num",
    "prv_npi",
]
_NPI_CRYPTIC = _cryptic_headers("n", 14)

_CVV_CLEAR = [
    "cvv",
    "cvc",
    "cvv_code",
    "security_code",
    "card_cvv",
    "cvv2",
    "cvc2",
    "card_verification",
]
_CVV_ABBREV = ["cvv_no", "sec_cd", "cvn", "cv_code", "cvc_no", "verif_cd", "cvv_num", "sc"]
_CVV_CRYPTIC = _cryptic_headers("v", 8)

_MRN_CLEAR = [
    "mrn",
    "patient_mrn",
    "medical_record_no",
    "med_rec_num",
    "mrn_number",
    "hospital_mrn",
    "chart_number",
    "record_number",
]
_MRN_ABBREV = ["pt_mrn", "mr_no", "med_rec", "rec_no", "pt_no", "chart_no", "emr_no", "hsp_mrn"]
_MRN_CRYPTIC = _cryptic_headers("m", 18)

_HP_CLEAR = [
    "health_plan_id",
    "plan_id",
    "insurance_id",
    "payer_id",
    "hmo_id",
    "carrier_id",
    "plan_number",
    "insurance_number",
]
_HP_ABBREV = [
    "hp_id",
    "ins_no",
    "plan_no",
    "mbr_plan",
    "payer_no",
    "cov_id",
    "hplan_id",
    "ins_plan",
]
_HP_CRYPTIC = _cryptic_headers("h", 18)
