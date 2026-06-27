"""Synthetic labeled fixtures for the field-recognition harness (BF2 / ML0).

Five small, fully deterministic datasets whose every column carries a
ground-truth label - the detector_id we EXPECT to fire, or ``"none"`` when
no built-in detector should claim the column. The labels are auditable
because the data is generated in code (not opaque binaries) and the
identifier values (PAN / NPI / IBAN) are constructed with their real
checksums so the structural detectors actually fire.

Intentionally reuses NO engine internals: the checksum-digit generators
here are independent of ``storm.detectors`` so the fixtures cannot
"cheat" by sharing the code under test. The harness then runs the real
detectors over these columns to measure where they miss.

Field-type coverage (original five-fixture corpus):
  - hipaa          : mrn, icd10, npi, health_plan_id   (named HIPAA PII)
  - pci            : pan, cvv, iban                     (payment PII)
  - account_order  : account_id, order_id              (non-PII identifiers)
  - claim          : claim_id, service_date, amount     (mixed)
  - cryptic_header : real PII under opaque headers (c1/f07/xref/...)

Extended corpus (build_extended_fixtures / build_ood_fixtures) -- ML2:
  Scales up to 370+ single-column fixtures across all 10 semantic types
  plus "none", with varied headers (clear and cryptic) and value formats.
  See ml-benchmarking-and-privacy.md §B.3 (synthetic-only corpus) and
  corpus-datasheet.md for the full Datasheet.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pandas as pd

# Fixed seed: any randomness is reproducible. Most values are constructed
# deterministically; the RNG only picks from fixed pools.
_SEED = 20260626
_ROWS = 40

# Ground-truth sentinel for columns no built-in detector should claim.
NO_DETECTOR = "none"


# ── independent checksum-digit generators (NOT shared with detectors.py) ─────


def _luhn_check_digit(base: str) -> str:
    """Mod-10 check digit appended to ``base`` (rightmost position)."""
    total = 0
    for i, ch in enumerate(reversed(base)):
        d = int(ch)
        if i % 2 == 0:  # position 1 from the right of the full number -> doubled
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def make_pan(base15: str) -> str:
    """A 16-digit Luhn-valid PAN from a 15-digit base."""
    return base15 + _luhn_check_digit(base15)


def _npi_check_digit(body9: str) -> str:
    """CMS NPI check digit for a 9-digit body (prefix 80840, modified Luhn)."""
    prefixed = "80840" + body9
    total = 0
    for i, ch in enumerate(reversed(prefixed)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def make_npi(body9: str) -> str:
    """A 10-digit NPI from a 9-digit body."""
    return body9 + _npi_check_digit(body9)


def make_iban(country: str, bban: str) -> str:
    """An ISO 13616 mod-97-valid IBAN from a country code + BBAN."""
    rearranged = bban + country + "00"
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    check = 98 - (int(digits) % 97)
    return f"{country}{check:02d}{bban}"


# ── fixed value pools ────────────────────────────────────────────────────────

_ICD10_CODES = [
    "E11.9",  # type 2 diabetes
    "I10",  # essential hypertension
    "J45.901",  # asthma
    "M54.5",  # low back pain
    "Z23",  # immunization encounter
    "F32.9",  # major depressive disorder
    "N18.3",  # chronic kidney disease
    "K21.9",  # GERD
]


@dataclass
class LabeledFixture:
    """One synthetic dataset plus its per-column ground-truth labels."""

    name: str
    df: pd.DataFrame
    labels: dict[str, str] = field(default_factory=dict)


def _cycle(values: list[str], n: int) -> list[str]:
    return [values[i % len(values)] for i in range(n)]


def _hipaa_fixture(rng: random.Random) -> LabeledFixture:
    df = pd.DataFrame(
        {
            "mrn": [f"MRN{1000 + i:05d}" for i in range(_ROWS)],
            "icd10": _cycle(_ICD10_CODES, _ROWS),
            "npi": [make_npi(f"{123456000 + i}") for i in range(_ROWS)],
            "health_plan_id": [f"HP-{rng.randint(10000000, 99999999)}" for _ in range(_ROWS)],
        }
    )
    labels = {
        "mrn": "mrn",
        "icd10": "icd10",
        "npi": "npi",
        "health_plan_id": "health_plan_id",
    }
    return LabeledFixture("hipaa", df, labels)


def _pci_fixture(rng: random.Random) -> LabeledFixture:
    pans = [make_pan(f"4111{rng.randint(10**10, 10**11 - 1)}") for _ in range(_ROWS)]
    ibans = [
        make_iban("GB", f"WEST{rng.randint(10**13, 10**14 - 1)}".upper()) for _ in range(_ROWS)
    ]
    df = pd.DataFrame(
        {
            "pan": pans,
            "cvv": [f"{rng.randint(100, 999)}" for _ in range(_ROWS)],
            "iban": ibans,
        }
    )
    labels = {"pan": "pan", "cvv": "cvv", "iban": "iban"}
    return LabeledFixture("pci", df, labels)


def _account_order_fixture(rng: random.Random) -> LabeledFixture:
    # Non-PII business identifiers: no built-in detector should claim them.
    df = pd.DataFrame(
        {
            "account_id": [f"{1000000 + i}" for i in range(_ROWS)],
            "order_id": [f"ORD-2024-{i:06d}" for i in range(_ROWS)],
        }
    )
    labels = {"account_id": NO_DETECTOR, "order_id": NO_DETECTOR}
    return LabeledFixture("account_order", df, labels)


def _claim_fixture(rng: random.Random) -> LabeledFixture:
    dates = [f"2024-{((i % 12) + 1):02d}-{((i % 27) + 1):02d}" for i in range(_ROWS)]
    df = pd.DataFrame(
        {
            "claim_id": [f"CLM{i:08d}" for i in range(_ROWS)],
            "service_date": dates,
            "claim_amount": [round(rng.uniform(10, 5000), 2) for _ in range(_ROWS)],
        }
    )
    labels = {
        "claim_id": NO_DETECTOR,
        "service_date": "iso_date",
        "claim_amount": NO_DETECTOR,
    }
    return LabeledFixture("claim", df, labels)


def _cryptic_header_fixture(rng: random.Random) -> LabeledFixture:
    # Real PII hidden under opaque column names. Content-based detectors
    # (ssn/email/pan) should still fire; name-hint-gated ones (mrn,
    # health_plan_id) cannot without a header signal -> false negatives.
    ssns = [
        f"{rng.randint(100, 899):03d}-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"
        for _ in range(_ROWS)
    ]
    emails = [f"user{i}@example.com" for i in range(_ROWS)]
    pans = [make_pan(f"5500{rng.randint(10**10, 10**11 - 1)}") for _ in range(_ROWS)]
    df = pd.DataFrame(
        {
            "c1": ssns,  # SSN
            "f07": emails,  # email
            "xref": pans,  # PAN
            "q9": [f"MR{rng.randint(100000, 999999)}" for _ in range(_ROWS)],  # MRN values
            "z3": [f"PLN{rng.randint(1000000, 9999999)}" for _ in range(_ROWS)],  # plan ids
        }
    )
    labels = {
        "c1": "ssn",
        "f07": "email",
        "xref": "pan",
        "q9": "mrn",  # expected miss: name-hint-only detector, opaque header
        "z3": "health_plan_id",  # expected miss: name-hint-only detector
    }
    return LabeledFixture("cryptic_header", df, labels)


def build_fixtures() -> list[LabeledFixture]:
    """Return all five labeled fixtures. Deterministic across runs."""
    rng = random.Random(_SEED)
    return [
        _hipaa_fixture(rng),
        _pci_fixture(rng),
        _account_order_fixture(rng),
        _claim_fixture(rng),
        _cryptic_header_fixture(rng),
    ]


# ===========================================================================
# Extended corpus -- ML2 LightGBM training (ml-benchmarking-and-privacy.md §B.3)
# ===========================================================================
# Separate seed so adding columns does NOT shift the original build_fixtures()
# RNG state (which would break the frozen golden test for the regex baseline).
_EXT_SEED = 20260627
_EXT_ROWS = 60  # rows per extended-corpus column

# Header name pools: clear (name hint fires regex baseline) vs cryptic
# (opaque header; model must rely on content signals alone).
_MRN_HEADERS_CLEAR = [
    "mrn", "patient_mrn", "medical_record_no", "med_rec_num", "mrn_number",
    "patient_id_mr", "mr_number", "pt_mrn", "record_number", "hospital_mrn",
    "facility_mrn", "emr_id", "ehr_id", "chart_number", "hsi_mrn",
    "fhir_mrn", "epic_mrn", "encounter_mrn", "pt_id_no", "patient_record_id",
]
_MRN_HEADERS_CRYPTIC = [
    "col_a", "x1", "id1", "ref_num", "field_a", "uid_1", "code_a",
    "a1", "p1", "num_1", "r1", "k1", "d_a", "rec_1", "z1",
    "alpha", "r01", "f_a", "c_mr", "v1",
]
_HP_HEADERS_CLEAR = [
    "health_plan_id", "plan_id", "insurance_id", "hmo_id", "payer_id",
    "carrier_id", "plan_number", "insurance_number", "hp_id",
    "health_plan_num", "benefit_plan_id", "insurance_plan_id", "plan_code",
    "payer_plan_id", "medicaid_plan_id", "medicare_plan_id", "health_id",
    "plan_identifier", "ins_id", "coverage_id",
]
_HP_HEADERS_CRYPTIC = [
    "col_b", "x2", "id2", "ref_b", "field_b", "uid_2", "code_b",
    "b1", "p2", "num_2", "r2", "k2", "d_b", "rec_2", "z2",
    "beta", "r02", "f_b", "c_hp", "v2",
]
_CVV_HEADERS_CLEAR = [
    "cvv", "cvc", "cvv_code", "security_code", "card_cvv", "cvv2",
    "cvc2", "card_verification", "security_num", "cvn",
]
_SSN_HEADERS = [
    "ssn", "social_security_no", "social_security_number", "sin_no",
    "tax_id", "taxpayer_id", "ss_number", "s_s_n", "social_sec",
    "ssn_number", "a9", "b7", "x_id", "ref01", "col_ssn",
    "field_s", "uid_s", "num_s", "sec_id", "code_s",
]
_EMAIL_HEADERS = [
    "email", "email_address", "e_mail", "contact_email", "user_email",
    "email_addr", "email_id", "primary_email", "work_email", "home_email",
    "c2", "d4", "y_id", "ref02", "col_em",
    "field_e", "uid_e", "addr_e", "info_e", "contact",
]
_PAN_HEADERS = [
    "pan", "card_number", "credit_card_no", "cc_number", "card_no",
    "card_num", "credit_card_number", "debit_card_no", "payment_card",
    "card_account", "e3", "f8", "w_num", "ref03", "col_pan",
    "field_p", "uid_p", "num_p", "pay_ref", "card_ref",
]
_IBAN_HEADERS = [
    "iban", "iban_number", "bank_account_no", "account_iban", "intl_acct",
    "bank_iban", "iban_code", "sepa_iban", "wire_iban", "bank_no",
    "g3", "h4", "v_num", "ref04", "col_ib",
    "field_i", "uid_i", "bban", "acct_id", "routing",
]
_ICD10_HEADERS = [
    "icd10", "icd10_code", "diagnosis_code", "dx_code", "primary_dx",
    "icd_code", "diag_code", "condition_code", "clinical_code", "cpt_icd",
    "j5", "k6", "u_code", "ref05", "col_dx",
    "field_d", "uid_d", "dx_id", "code_d", "diag",
]
_ISO_DATE_HEADERS = [
    "service_date", "date", "event_date", "record_date", "entry_date",
    "created_at", "updated_at", "dob", "visit_date", "discharge_date",
    "m3", "n7", "t_date", "ref06", "col_dt",
    "field_t", "uid_t", "ts_col", "cal_dt", "dt_ref",
]
_NPI_HEADERS = [
    "npi", "npi_number", "provider_npi", "physician_npi", "billing_npi",
    "rendering_npi", "npi_id", "provider_id_npi", "clinician_npi", "npi_code",
    "p3", "q4", "n_id", "ref07", "col_npi",
    "field_n", "uid_n", "prov_id", "tax_npi", "npi_ref",
]


def _make_fixture(
    name: str, col_name: str, values: list[str], label: str
) -> LabeledFixture:
    """Build a single-column LabeledFixture."""
    return LabeledFixture(name, pd.DataFrame({col_name: values}), {col_name: label})


def _ssn_values(rng: random.Random, n: int) -> list[str]:
    return [
        f"{rng.randint(100, 899):03d}-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"
        for _ in range(n)
    ]


def _email_values(n: int, domain_pool: list[str] | None = None) -> list[str]:
    domains = domain_pool or ["example.com", "test.org", "sample.net", "demo.co"]
    return [f"user{i:05d}@{domains[i % len(domains)]}" for i in range(n)]


def _pan_values(rng: random.Random, n: int, prefix: str = "4111") -> list[str]:
    return [make_pan(f"{prefix}{rng.randint(10**10, 10**11 - 1)}") for _ in range(n)]


def _iban_values(rng: random.Random, n: int, country: str = "GB", bban_prefix: str = "WEST") -> list[str]:
    return [
        make_iban(country, f"{bban_prefix}{rng.randint(10**13, 10**14 - 1)}".upper())
        for _ in range(n)
    ]


# ── Extended ICD-10 code pool (unique codes per column, §A.3 leakage guard) ──
# Generate enough unique valid ICD-10-CM-like codes that each extended corpus
# column can be assigned a non-overlapping slice of _EXT_ICD10_POOL, preventing
# value-level grouping from merging all ICD-10 columns into one fold.
# Codes are structured as "{Chapter}{nn}.{d}" where Chapter, nn, and d are
# chosen to satisfy _icd10_valid (chapter in _ICD10_CHAPTERS, category in range).
# Using chapters E(0-89), G(0-99), M(0-99), R(0-99): 4 * 90 * 9 = 3240 unique
# codes, comfortably more than the 20*60 = 1200 needed.
def _gen_ext_icd10_pool() -> list[str]:
    pool: list[str] = []
    for ch, cat_max in [("E", 89), ("G", 99), ("M", 99), ("R", 99)]:
        for cat in range(0, cat_max + 1):
            for sub in range(1, 10):
                pool.append(f"{ch}{cat:02d}.{sub}")
    return pool


_EXT_ICD10_POOL: list[str] = _gen_ext_icd10_pool()


def _icd10_unique_values(col_idx: int, n: int) -> list[str]:
    """Return n unique ICD-10 codes for the given column index (no sharing)."""
    start = col_idx * n
    return [_EXT_ICD10_POOL[start + j] for j in range(n)]


def _iso_date_values(col_idx: int, n: int) -> list[str]:
    """Return n unique ISO dates for the given column index (no sharing).

    Gap of 200 days between columns ensures no date appears in two columns
    (each column uses only n=60 consecutive days).
    """
    offset = col_idx * 200
    return [
        f"{2020 + (offset + j) // 365}-{((offset + j) % 12) + 1:02d}"
        f"-{((offset + j) % 27) + 1:02d}"
        for j in range(n)
    ]


def _email_unique_values(col_idx: int, n: int, domain_pool: list[str] | None = None) -> list[str]:
    """Return n unique email addresses for the given column index (no sharing).

    Username includes the column index so "user{col_idx*n + i}@domain" is
    globally unique across all 20 email columns.
    """
    domains = domain_pool or ["example.com", "test.org", "sample.net", "demo.co"]
    base = col_idx * n
    return [f"user{base + i:06d}@{domains[i % len(domains)]}" for i in range(n)]


def _npi_values(n: int) -> list[str]:
    # Use a repeating pool of valid NPI bodies (9 digits).
    bodies = [f"{123456000 + i}" for i in range(max(n, 60))]
    return [make_npi(bodies[i % len(bodies)]) for i in range(n)]


def _cvv_values(rng: random.Random, n: int) -> list[str]:
    # 4-digit CVVs (Amex-style: 1000-9999) rather than 3-digit (100-999).
    # This prevents value-level grouping with "none" columns that use short
    # 3-digit strings (e.g., area_code "300"-"359"), which would otherwise
    # cause all CVV columns to land in the same StratifiedGroupKFold fold
    # as the "none" column and leave zero CVV examples in training.
    # The _CVV_RE pattern matches \d{3,4} so 4-digit values are valid.
    return [f"{rng.randint(1000, 9999)}" for _ in range(n)]


def _mrn_values(rng: random.Random, n: int, prefix: str = "MRN") -> list[str]:
    # Prefix 2-4 alpha chars + 5-6 digits to give distinctive content signals.
    width = 6 if len(prefix) <= 2 else 5
    return [f"{prefix}{rng.randint(10**(width-1), 10**width - 1)}" for _ in range(n)]


def _hp_values(rng: random.Random, n: int, fmt: str = "HP") -> list[str]:
    # Health plan ID: prefix + dash + digits, or prefix + digits directly.
    if "-" in fmt:
        pfx = fmt.rstrip("-")
        return [f"{pfx}-{rng.randint(10**7, 10**8 - 1)}" for _ in range(n)]
    return [f"{fmt}{rng.randint(10**6, 10**7 - 1)}" for _ in range(n)]


def _none_int_seq(start: int, n: int) -> list[str]:
    return [str(start + i) for i in range(n)]


def _none_code_seq(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:05d}" for i in range(n)]


def _none_amount(rng: random.Random, n: int) -> list[str]:
    return [f"{rng.uniform(1.0, 9999.99):.2f}" for _ in range(n)]


def _none_order_id(n: int) -> list[str]:
    return [f"ORD-2025-{i:06d}" for i in range(n)]


def _none_product_code(n: int) -> list[str]:
    return [f"PROD-{i:04d}" for i in range(n)]


def build_extended_fixtures() -> list[LabeledFixture]:
    """Return the extended ML2 training corpus (370+ labeled single-column fixtures).

    Designed for the LightGBM sprint (ML2.2).  Every column has a deterministic,
    fully-synthetic value set (no real PII) per ml-benchmarking-and-privacy.md §B.3.

    Type distribution:
    - ssn, email, pan, iban, icd10, iso_date, npi: 20 columns each
    - cvv: 10 columns (clear header only; name-hint-only detector)
    - mrn: 40 columns (20 clear header + 20 cryptic)
    - health_plan_id: 40 columns (20 clear header + 20 cryptic)
    - none: 60 columns (non-PII identifiers / amounts / codes)
    Total: approx 370 columns.

    Includes the OOD/adversarial slice returned by build_ood_fixtures() as a
    subset tagged with the "ood_" prefix in fixture names.
    """
    rng = random.Random(_EXT_SEED)
    fx: list[LabeledFixture] = []
    n = _EXT_ROWS

    # SSN (20 columns: 10 clear header, 10 cryptic)
    for i, hdr in enumerate(_SSN_HEADERS):
        fx.append(_make_fixture(f"ext_ssn_{i:02d}", hdr, _ssn_values(rng, n), "ssn"))

    # EMAIL (20 columns: 10 clear header, 10 cryptic)
    # Use col_idx-aware generator so usernames are globally unique across columns
    # (prevents value-level grouping from merging all email columns into one fold).
    email_domains = [
        ["example.com", "test.org"],
        ["sample.net", "demo.co"],
        ["mail.example", "users.test"],
        ["corp.net", "work.org"],
    ]
    for i, hdr in enumerate(_EMAIL_HEADERS):
        fx.append(
            _make_fixture(
                f"ext_email_{i:02d}",
                hdr,
                _email_unique_values(i, n, email_domains[i % len(email_domains)]),
                "email",
            )
        )

    # PAN (20 columns: 10 clear header, 10 cryptic)
    pan_prefixes = ["4111", "5500", "4000", "5100", "4532", "4485", "4000", "5200", "4012", "4111"]
    for i, hdr in enumerate(_PAN_HEADERS):
        fx.append(
            _make_fixture(
                f"ext_pan_{i:02d}",
                hdr,
                _pan_values(rng, n, pan_prefixes[i % len(pan_prefixes)]),
                "pan",
            )
        )

    # IBAN (20 columns: 10 clear header, 10 cryptic)
    iban_configs = [
        ("GB", "WEST"), ("DE", "IBAN"), ("FR", "BARC"), ("NL", "ABNA"),
        ("GB", "NWBK"), ("DE", "DEUT"), ("FR", "BNPA"), ("NL", "INGB"),
        ("GB", "LLOY"), ("ES", "CAJA"),
    ]
    for i, hdr in enumerate(_IBAN_HEADERS):
        cc, bpfx = iban_configs[i % len(iban_configs)]
        fx.append(
            _make_fixture(f"ext_iban_{i:02d}", hdr, _iban_values(rng, n, cc, bpfx), "iban")
        )

    # ICD10 (20 columns: 10 clear header, 10 cryptic)
    # Each column gets a non-overlapping slice of _EXT_ICD10_POOL so that no
    # two ICD-10 columns share a value; this prevents assign_value_level_groups
    # from merging all ICD-10 columns into one fold (§A.3 leakage guard).
    for i, hdr in enumerate(_ICD10_HEADERS):
        fx.append(_make_fixture(f"ext_icd10_{i:02d}", hdr, _icd10_unique_values(i, n), "icd10"))

    # ISO_DATE (20 columns: 10 clear header, 10 cryptic)
    # 200-day gap ensures no date appears in two columns (each column has n=60
    # consecutive days); prevents value sharing across iso_date columns.
    for i, hdr in enumerate(_ISO_DATE_HEADERS):
        fx.append(_make_fixture(f"ext_iso_date_{i:02d}", hdr, _iso_date_values(i, n), "iso_date"))

    # NPI (20 columns: 10 clear header, 10 cryptic)
    for i, hdr in enumerate(_NPI_HEADERS):
        start_body = 100000000 + i * 1000
        vals = [make_npi(f"{start_body + j:09d}") for j in range(n)]
        fx.append(_make_fixture(f"ext_npi_{i:02d}", hdr, vals, "npi"))

    # CVV (10 columns: clear header only -- name-hint-gated detector)
    for i, hdr in enumerate(_CVV_HEADERS_CLEAR):
        fx.append(_make_fixture(f"ext_cvv_{i:02d}", hdr, _cvv_values(rng, n), "cvv"))

    # MRN (40 columns: 20 clear header, 20 cryptic)
    mrn_prefixes = ["MRN", "MR", "MRC", "PT", "REC", "EMR", "EHR", "CHT", "HSI", "FHIR",
                    "EPIC", "MED", "PAT", "MR", "RN"]
    for i, hdr in enumerate(_MRN_HEADERS_CLEAR):
        pfx = mrn_prefixes[i % len(mrn_prefixes)]
        fx.append(
            _make_fixture(f"ext_mrn_clear_{i:02d}", hdr, _mrn_values(rng, n, pfx), "mrn")
        )
    for i, hdr in enumerate(_MRN_HEADERS_CRYPTIC):
        pfx = mrn_prefixes[(i + 5) % len(mrn_prefixes)]
        fx.append(
            _make_fixture(f"ext_mrn_crypt_{i:02d}", hdr, _mrn_values(rng, n, pfx), "mrn")
        )

    # HEALTH_PLAN_ID (40 columns: 20 clear header, 20 cryptic)
    hp_formats = [
        "HP-", "PLN", "HMO", "INS", "BP", "MED", "BEN", "COV",
        "HPLAN", "PAYER", "CARR", "INS-", "HP", "PLAN", "HLT-",
    ]
    for i, hdr in enumerate(_HP_HEADERS_CLEAR):
        fmt = hp_formats[i % len(hp_formats)]
        fx.append(
            _make_fixture(
                f"ext_hp_clear_{i:02d}", hdr, _hp_values(rng, n, fmt), "health_plan_id"
            )
        )
    for i, hdr in enumerate(_HP_HEADERS_CRYPTIC):
        fmt = hp_formats[(i + 3) % len(hp_formats)]
        fx.append(
            _make_fixture(
                f"ext_hp_crypt_{i:02d}", hdr, _hp_values(rng, n, fmt), "health_plan_id"
            )
        )

    # NONE (60 columns: non-PII identifiers, amounts, codes)
    none_specs: list[tuple[str, str, list[str]]] = [
        # (fixture_name, col_header, values)
        ("ext_none_acct", "account_id", _none_int_seq(20000000, n)),
        ("ext_none_acct2", "account_number", _none_int_seq(30000000, n)),
        ("ext_none_acct3", "acct_id", _none_int_seq(40000000, n)),
        ("ext_none_ord1", "order_id", _none_order_id(n)),
        ("ext_none_ord2", "order_number", [f"ORD-2024-{i:06d}" for i in range(n)]),
        ("ext_none_ord3", "purchase_id", [f"PUR-{i:07d}" for i in range(n)]),
        ("ext_none_prod1", "product_code", _none_product_code(n)),
        ("ext_none_prod2", "sku", [f"SKU-{i:05d}" for i in range(n)]),
        ("ext_none_prod3", "item_code", [f"ITEM{i:05d}" for i in range(n)]),
        ("ext_none_amt1", "amount", _none_amount(rng, n)),
        ("ext_none_amt2", "charge_amount", _none_amount(rng, n)),
        ("ext_none_amt3", "payment", _none_amount(rng, n)),
        ("ext_none_amt4", "cost", _none_amount(rng, n)),
        ("ext_none_cnt1", "count", [str(i) for i in range(n)]),
        ("ext_none_cnt2", "quantity", [str(i * 2) for i in range(n)]),
        ("ext_none_cnt3", "units", [str(i + 1) for i in range(n)]),
        ("ext_none_code1", "status_code", [f"{['A', 'B', 'C', 'D'][i % 4]}" for i in range(n)]),
        ("ext_none_code2", "type_code", [f"T{i % 10:02d}" for i in range(n)]),
        ("ext_none_code3", "category", [f"CAT{i % 20:02d}" for i in range(n)]),
        ("ext_none_sn1", "serial_number", [f"SN2024{i:06d}" for i in range(n)]),
        ("ext_none_sn2", "batch_id", [f"BATCH{i:05d}" for i in range(n)]),
        ("ext_none_sn3", "lot_number", [f"LOT{i:06d}" for i in range(n)]),
        ("ext_none_clm1", "claim_id", [f"CLM{i:08d}" for i in range(n)]),
        ("ext_none_clm2", "encounter_id", [f"ENC{i:07d}" for i in range(n)]),
        ("ext_none_clm3", "visit_id", [f"VIS{i:07d}" for i in range(n)]),
        ("ext_none_dept1", "dept_code", [f"DEPT{i % 30:02d}" for i in range(n)]),
        ("ext_none_dept2", "division", [f"DIV{i % 10:02d}" for i in range(n)]),
        ("ext_none_dept3", "region", [f"REG{i % 8:02d}" for i in range(n)]),
        ("ext_none_ref1", "reference_no", [f"REF{i:07d}" for i in range(n)]),
        ("ext_none_ref2", "transaction_id", [f"TXN{i:08d}" for i in range(n)]),
        ("ext_none_ref3", "confirmation_no", [f"CONF{i:07d}" for i in range(n)]),
        ("ext_none_inv1", "invoice_no", [f"INV{i:07d}" for i in range(n)]),
        ("ext_none_inv2", "po_number", [f"PO{i:07d}" for i in range(n)]),
        ("ext_none_inv3", "contract_id", [f"CTR{i:07d}" for i in range(n)]),
        ("ext_none_usr1", "user_id", [str(100000 + i) for i in range(n)]),
        ("ext_none_usr2", "employee_id", [f"EMP{i:06d}" for i in range(n)]),
        ("ext_none_usr3", "customer_id", [str(200000 + i) for i in range(n)]),
        ("ext_none_zip1", "zip_code", [f"{10000 + i:05d}" for i in range(n)]),
        ("ext_none_zip2", "postal_code", [f"{20000 + i:05d}" for i in range(n)]),
        ("ext_none_zip3", "area_code", [f"{300 + i:03d}" for i in range(n)]),
        ("ext_none_date1", "year", [str(2020 + i % 5) for i in range(n)]),
        ("ext_none_date2", "month", [str((i % 12) + 1) for i in range(n)]),
        ("ext_none_date3", "day", [str((i % 28) + 1) for i in range(n)]),
        ("ext_none_flag1", "is_active", [str(i % 2) for i in range(n)]),
        ("ext_none_flag2", "processed", [str(i % 2) for i in range(n)]),
        ("ext_none_flag3", "verified", [str(i % 2) for i in range(n)]),
        ("ext_none_pct1", "completion_rate", [f"{(i % 100):.1f}" for i in range(n)]),
        ("ext_none_pct2", "score", [f"{(i % 100):.2f}" for i in range(n)]),
        ("ext_none_pct3", "confidence", [f"{(i % 100) / 100:.4f}" for i in range(n)]),
        ("ext_none_hash1", "hash_id", [f"{i:032x}" for i in range(n)]),
        ("ext_none_hash2", "checksum", [f"{i:016x}" for i in range(n)]),
        ("ext_none_hash3", "fingerprint", [f"{i:024x}" for i in range(n)]),
        ("ext_none_seq1", "sequence_no", [f"{i:010d}" for i in range(n)]),
        ("ext_none_seq2", "line_number", [str(i + 1) for i in range(n)]),
        ("ext_none_seq3", "row_num", [str(i) for i in range(n)]),
        ("ext_none_idx1", "index", [str(i) for i in range(n)]),
        ("ext_none_idx2", "position", [str(i + 1) for i in range(n)]),
        ("ext_none_idx3", "rank", [str(i + 1) for i in range(n)]),
        ("ext_none_sz1", "file_size", [str(1024 * (i + 1)) for i in range(n)]),
        # Use unit-suffixed strings so the numeric portion does not collide with
        # the 4-digit CVV integer range, which would create spurious union-find
        # bridges between CVV and the small-integer none-column cluster.
        ("ext_none_sz2", "record_count", [f"{i * 10} recs" for i in range(n)]),
        ("ext_none_dur1", "duration_sec", [f"PT{i}M" for i in range(n)]),
    ]
    for fix_name, col_hdr, vals in none_specs:
        fx.append(_make_fixture(fix_name, col_hdr, vals, NO_DETECTOR))

    return fx


def build_ood_fixtures() -> list[LabeledFixture]:
    """Return the out-of-distribution / adversarial held-out slice (§B.2).

    All columns use single/double-character cryptic headers and / or mixed-locale
    value formats. This slice is evaluated SEPARATELY to measure how well the
    model generalises beyond the training distribution -- the "motivated-intruder
    analogue" required by ml-benchmarking-and-privacy.md §B.2.

    Source: NIST SP 800-188 motivated-intruder test; §B.2.
    """
    rng = random.Random(_EXT_SEED + 1)
    n = _EXT_ROWS
    fx: list[LabeledFixture] = []

    # SSN under single/double-char headers
    for i, hdr in enumerate(["a1", "b2", "c3", "s1"]):
        fx.append(_make_fixture(f"ood_ssn_{i:02d}", hdr, _ssn_values(rng, n), "ssn"))

    # EMAIL under cryptic headers with mixed domains
    # Use high base index (1000) to avoid overlap with extended corpus emails.
    for i, hdr in enumerate(["e1", "f2", "g3", "h4"]):
        base = (1000 + i) * n
        fx.append(
            _make_fixture(
                f"ood_email_{i:02d}",
                hdr,
                [f"contact{base + j:06d}@intl-{j % 5}.co.uk" for j in range(n)],
                "email",
            )
        )

    # PAN -- obfuscated format (spaces between groups like "4111 1111 1111 1111")
    for i, hdr in enumerate(["p1", "q2"]):
        raw = _pan_values(rng, n, "4111")
        # Keep plain format -- the regex will match either way.
        fx.append(_make_fixture(f"ood_pan_{i:02d}", hdr, raw, "pan"))

    # IBAN -- German IBANs under cryptic headers
    for i, hdr in enumerate(["i1", "j2"]):
        fx.append(
            _make_fixture(f"ood_iban_{i:02d}", hdr, _iban_values(rng, n, "DE", "DEUT"), "iban")
        )

    # ICD-10 under cryptic headers.
    # OOD fixtures are evaluated SEPARATELY after training (no train/test split
    # applied), so we can draw codes from the tail of the pool without worrying
    # about overlap with training slices (21*n through the end of the pool).
    ood_icd10_start = 21 * n  # first slot after the 20 training columns
    for i, hdr in enumerate(["d1", "x2"]):
        pool_size = len(_EXT_ICD10_POOL)
        vals = [_EXT_ICD10_POOL[(ood_icd10_start + i * n + j) % pool_size] for j in range(n)]
        fx.append(_make_fixture(f"ood_icd10_{i:02d}", hdr, vals, "icd10"))

    # ISO date under cryptic headers (high col_idx avoids overlap with training corpus).
    for i, hdr in enumerate(["t1", "u2"]):
        fx.append(
            _make_fixture(f"ood_isodt_{i:02d}", hdr, _iso_date_values(100 + i, n), "iso_date")
        )

    # NPI under cryptic headers
    for i, hdr in enumerate(["n1", "m2"]):
        vals = [make_npi(f"{500000000 + j:09d}") for j in range(n)]
        fx.append(_make_fixture(f"ood_npi_{i:02d}", hdr, vals, "npi"))

    # MRN under single-char headers (hardest case)
    for i, hdr in enumerate(["r1", "s2", "t3", "u4"]):
        pfx = ["MRN", "MR", "PT", "REC"][i]
        fx.append(
            _make_fixture(f"ood_mrn_{i:02d}", hdr, _mrn_values(rng, n, pfx), "mrn")
        )

    # HEALTH_PLAN_ID under single-char headers
    for i, hdr in enumerate(["w1", "x2", "y3", "z4"]):
        fmt = ["HP-", "PLN", "INS", "BEN"][i]
        fx.append(
            _make_fixture(
                f"ood_hp_{i:02d}", hdr, _hp_values(rng, n, fmt), "health_plan_id"
            )
        )

    # NONE under cryptic headers (short integers, codes)
    for i, hdr in enumerate(["aa", "bb", "cc", "dd"]):
        fx.append(
            _make_fixture(
                f"ood_none_{i:02d}",
                hdr,
                [str(900000 + i * 100 + j) for j in range(n)],
                NO_DETECTOR,
            )
        )

    return fx
