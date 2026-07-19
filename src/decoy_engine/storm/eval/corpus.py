"""Expanded ML training corpus for the field-recognition harness (MLF-4).

Split out of ``fixtures.py`` (2026-07-19) to satisfy the module-size sentry
(``tests/sentry/test_module_size.py``) -- ``fixtures.py`` had grown past its
allowlisted ceiling as the Phase B rebaseline scaled this corpus up. Holds
the cohesive synthetic-data generators for the three build_* corpora:

Extended corpus (build_extended_fixtures / build_ood_fixtures) -- ML2:
  Scales up to ~2960 single-column fixtures across all 10 semantic types
  plus "none", with varied headers (clear / abbreviated / cryptic) and
  value formats (locale, separator, network, and prefix variants).
  See ml-benchmarking-and-privacy.md §B.3 (synthetic-only corpus) and
  corpus-datasheet.md for the full Datasheet.

Cryptic-header benchmark (build_cryptic_fixtures) -- Phase B / CH-3:
  A held-out slice of real PII under cryptic/abbreviated headers only,
  evaluated separately from the train/test fold. This is the benchmark
  the CH-1/CH-2 header-lexicon lift is measured against.

The leakage guard (§A.3, disjoint value spaces per column) and the
checksum-digit generators / ``LabeledFixture`` dataclass this corpus
builds on live in ``decoy_engine.storm.eval.fixtures`` -- see that
module's docstring for the invariant this corpus must uphold.
"""

from __future__ import annotations

import string
from datetime import date, timedelta

import pandas as pd

from decoy_engine.storm.eval.fixtures import (
    NO_DETECTOR,
    LabeledFixture,
    _luhn_check_digit,
    make_iban,
    make_npi,
)

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


# ── SSN: mixed-radix (area, group, serial) encoding of a global index k ─────
# Injective for any k < 800*90*9000 (~648M), far beyond corpus scale, so no
# two (col_idx, row) pairs can ever collide -- regardless of how many format
# variants (dashed / no-dash / spaced) reuse the same digits.


def _ssn_digits_from_k(k: int) -> tuple[str, str, str]:
    serial = 1000 + (k % 9000)
    rem = k // 9000
    group = 10 + (rem % 90)
    rem2 = rem // 90
    area = 100 + (rem2 % 799)  # dodges the reserved 900+ block
    return f"{area:03d}", f"{group:02d}", f"{serial:04d}"


def _format_ssn(area: str, group: str, serial: str, variant: int) -> str:
    if variant == 0:
        return f"{area}-{group}-{serial}"
    if variant == 1:
        return f"{area}{group}{serial}"
    return f"{area} {group} {serial}"


def _ssn_values_for_col(col_idx: int, n: int) -> list[str]:
    variant = col_idx % 3
    base = col_idx * n
    out = []
    for row in range(n):
        area, group, serial = _ssn_digits_from_k(base + row)
        out.append(_format_ssn(area, group, serial, variant))
    return out


# ── EMAIL: username embeds a global k directly -> trivially unique ──────────

_EMAIL_DOMAIN_POOLS: list[list[str]] = [
    ["example.com", "test.org"],
    ["sample.net", "demo.co"],
    ["mail.example", "users.test"],
    ["corp.net", "work.org"],
    ["example.co.uk", "sample.org.uk"],
    ["firma.de", "beispiel.de"],
    ["exemple.fr", "test.fr"],
    ["example.io", "sample.io"],
    ["agency.edu", "campus.edu"],
    ["dept.gov", "state.gov"],
    ["global.com", "intl.net"],
    ["example.com.au", "sample.co.jp"],
]


def _email_values_for_col(col_idx: int, n: int) -> list[str]:
    domains = _EMAIL_DOMAIN_POOLS[col_idx % len(_EMAIL_DOMAIN_POOLS)]
    style = col_idx % 4
    base = col_idx * n
    out = []
    for row in range(n):
        k = base + row
        domain = domains[row % len(domains)]
        if style == 0:
            local = f"user{k:06d}"
        elif style == 1:
            local = f"person.{k:06d}"
        elif style == 2:
            local = f"contact{k:06d}+acct"
        else:
            local = f"n{k:06d}.doe"
        out.append(f"{local}@{domain}")
    return out


# ── PAN: network-prefixed base + Luhn check digit, k padded into the base ───

_PAN_NETWORKS: list[tuple[str, int]] = [
    ("4111", 16),
    ("4000", 16),
    ("4532", 16),
    ("4716", 16),  # visa
    ("51", 16),
    ("52", 16),
    ("53", 16),
    ("55", 16),  # mastercard (BIN range)
    ("2221", 16),
    ("2720", 16),  # mastercard 2-series
    ("6011", 16),
    ("6500", 16),  # discover
    ("34", 15),
    ("37", 15),  # amex (15-digit)
]


def _pan_from_k(prefix: str, total_len: int, k: int) -> str:
    base_len = total_len - 1 - len(prefix)
    base = f"{prefix}{k:0{base_len}d}"
    return base + _luhn_check_digit(base)


def _format_pan(digits: str, variant: int) -> str:
    if variant == 0:
        return digits
    groups = [digits[i : i + 4] for i in range(0, len(digits), 4)]
    sep = "-" if variant == 1 else " "
    return sep.join(groups)


def _pan_values_for_col(col_idx: int, n: int) -> list[str]:
    prefix, total_len = _PAN_NETWORKS[col_idx % len(_PAN_NETWORKS)]
    variant = col_idx % 3
    base = col_idx * n
    out = []
    for row in range(n):
        digits = _pan_from_k(prefix, total_len, base + row)
        out.append(_format_pan(digits, variant))
    return out


# ── IBAN: country-specific BBAN length, k padded into the BBAN suffix ───────

_IBAN_COUNTRY_CONFIGS: list[tuple[str, int, str]] = [
    # (country, iban_total_len, bank_prefix)
    ("GB", 22, "WEST"),
    ("DE", 22, "DEUT"),
    ("FR", 27, "BNPA"),
    ("NL", 18, "ABNA"),
    ("ES", 24, "CAIX"),
    ("IT", 27, "UNCR"),
    ("BE", 16, "GEBA"),
    ("CH", 21, "UBSW"),
    ("IE", 22, "AIBK"),
    ("PT", 25, "BPIP"),
    ("AT", 20, "GIBA"),
    ("SE", 24, "ESSE"),
    ("NO", 15, "DNBA"),
    ("DK", 18, "DABA"),
    ("FI", 18, "NDEA"),
    ("PL", 28, "PKOP"),
]


def _iban_bban_from_k(country: str, total_len: int, bank_prefix: str, k: int) -> str:
    bban_len = total_len - 4
    suffix_len = bban_len - len(bank_prefix)
    if suffix_len < 5:
        bank_prefix = bank_prefix[: max(bban_len - 5, 1)]
        suffix_len = bban_len - len(bank_prefix)
    return f"{bank_prefix}{k:0{suffix_len}d}"[:bban_len]


def _format_iban(iban: str, variant: int) -> str:
    if variant == 0:
        return iban
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


def _iban_values_for_col(col_idx: int, n: int) -> list[str]:
    country, total_len, bank_prefix = _IBAN_COUNTRY_CONFIGS[col_idx % len(_IBAN_COUNTRY_CONFIGS)]
    variant = col_idx % 2
    base = col_idx * n
    out = []
    for row in range(n):
        bban = _iban_bban_from_k(country, total_len, bank_prefix, base + row)
        out.append(_format_iban(make_iban(country, bban), variant))
    return out


# ── ICD-10 pool: full A-Z chapter-range table (independent of detectors.py,
# same public ICD-10-CM structure) x many subcodes -- comfortably covers
# the training slice (240*60) plus the reserved OOD/cryptic regions below.

_ICD10_EXT_CHAPTER_RANGES: dict[str, tuple[int, int]] = {
    "A": (0, 99),
    "B": (0, 99),
    "C": (0, 99),
    "D": (0, 89),
    "E": (0, 89),
    "F": (1, 99),
    "G": (0, 99),
    "H": (0, 95),
    "I": (0, 99),
    "J": (0, 99),
    "K": (0, 95),
    "L": (0, 99),
    "M": (0, 99),
    "N": (0, 99),
    "O": (0, 99),
    "P": (0, 96),
    "Q": (0, 99),
    "R": (0, 99),
    "S": (0, 99),
    "T": (0, 88),
    "U": (0, 85),
    "V": (0, 99),
    "W": (0, 99),
    "X": (0, 99),
    "Y": (0, 99),
    "Z": (0, 99),
}


def _gen_ext_icd10_pool() -> list[str]:
    pool: list[str] = []
    for ch in sorted(_ICD10_EXT_CHAPTER_RANGES):
        cat_lo, cat_hi = _ICD10_EXT_CHAPTER_RANGES[ch]
        for cat in range(cat_lo, cat_hi + 1):
            for sub in range(1, 26):  # 25 subcodes/category -- ample headroom
                pool.append(f"{ch}{cat:02d}.{sub}")
    return pool


_EXT_ICD10_POOL: list[str] = _gen_ext_icd10_pool()


# ── ISO date: real calendar arithmetic from a base date + global k ──────────
# Distinct bases per corpus slice keep the (base, k-range) windows from ever
# overlapping, while every date stays inside the _iso_date_valid 1900-2100
# bound.

_ISO_DATE_TRAIN_BASE = date(1970, 1, 1)  # k up to 14399 -> ~2009-06
_ISO_DATE_OOD_BASE = date(2012, 1, 1)  # k up to ~120 -> ~2012-04
_ISO_DATE_CRYPTIC_BASE = date(2020, 1, 1)  # k up to ~1320 -> ~2023-08


def _iso_date_values_from_base(base: date, offset_days: int, n: int) -> list[str]:
    return [(base + timedelta(days=offset_days + j)).isoformat() for j in range(n)]


# ── NPI: 9-digit body embeds a global k, Luhn-style check digit appended ────


def _npi_values_for_col(col_idx: int, n: int) -> list[str]:
    base = col_idx * n
    return [make_npi(f"{100000000 + base + row:09d}") for row in range(n)]


# ── CVV: realistic 3-4 digit space (~9900 values) -- fully disjoint up to
# ~120 columns (120*80=9600 < 9800); wraps (and may overlap) beyond that,
# which is the documented exemption from the disjointness invariant.


def _cvv_values_for_col(col_idx: int, n: int, width: int = 80, start: int = 100) -> list[str]:
    lo = start + (col_idx * width) % 9800
    return [str(lo + (row % width)) for row in range(n)]


# ── MRN / health_plan_id: prefix + k-derived numeric suffix, several
# separator styles. The numeric suffix alone is unique per (col_idx, row)
# across the WHOLE label (not just within a prefix), so two columns can
# reuse the same institutional prefix and still never collide.

_MRN_PREFIXES = [
    "MRN",
    "MR",
    "PT",
    "REC",
    "EMR",
    "EHR",
    "CHT",
    "HSI",
    "FHIR",
    "EPIC",
    "MED",
    "PAT",
    "HOSP",
    "CLIN",
    "VISIT",
    "ENC",
    "ADM",
    "DISCH",
    "REGID",
    "CASE",
]
_HP_PREFIXES = [
    "HP",
    "PLN",
    "HMO",
    "INS",
    "BP",
    "MEDPLAN",
    "BEN",
    "COV",
    "HPLAN",
    "PAYER",
    "CARR",
    "MCARE",
    "MCAID",
    "GRP",
    "SUB",
    "POL",
    "CERT",
    "RXBIN",
    "NETWK",
    "TIER",
]


def _institutional_values_for_col(col_idx: int, n: int, prefixes: list[str]) -> list[str]:
    prefix = prefixes[col_idx % len(prefixes)]
    style = col_idx % 3
    base = col_idx * n
    out = []
    for row in range(n):
        k = base + row
        if style == 0:
            out.append(f"{prefix}-{k:07d}")
        elif style == 1:
            out.append(f"{prefix}{k:07d}")
        else:
            out.append(f"{prefix}-{(k // 10000) % 100:02d}-{k % 10000:04d}")
    return out


# ── NONE: the richest label. 22 high-cardinality "families" (k embedded in
# a family-exclusive numeric block -> globally disjoint by construction,
# regardless of prefix reuse) + 16 single-column low-cardinality
# status/flag columns, each with its own mutually-exclusive token alphabet
# so no two ever share a value.

_NONE_HIGH_CARD_FAMILIES: list[tuple[str, list[str], str, str]] = [
    # (family_key, header_pool, prefix, kind)
    ("acct", ["account_id", "account_number", "acct_id", "acct_no"], "ACC", "id"),
    ("cust", ["customer_id", "cust_id", "client_id", "cust_no"], "CUS", "id"),
    ("emp", ["employee_id", "emp_id", "staff_id", "emp_no"], "EMP", "id"),
    ("order", ["order_id", "order_number", "purchase_id", "ord_no"], "ORD", "id"),
    ("invoice", ["invoice_no", "invoice_id", "bill_no", "inv_no"], "INV", "id"),
    ("claim", ["claim_id", "claim_no", "encounter_id", "visit_id"], "CLM", "id"),
    ("product", ["product_code", "sku", "item_code", "prod_id"], "SKU", "id"),
    ("txn", ["transaction_id", "txn_id", "payment_ref", "txn_no"], "TXN", "id"),
    ("conf", ["confirmation_no", "conf_id", "conf_code", "confirm_no"], "CNF", "id"),
    ("po", ["po_number", "purchase_order", "po_id", "contract_id"], "PO", "id"),
    ("ref", ["reference_no", "ref_id", "ref_code", "reference_id"], "REF", "id"),
    ("serial", ["serial_number", "batch_id", "lot_number", "serial_no"], "SN", "id"),
    ("dept", ["dept_code", "division", "region", "cost_center"], "DEPT", "id"),
    ("cat", ["category", "type_code", "category_code", "class_code"], "CAT", "id"),
    ("session", ["session_id", "request_id", "trace_id", "job_id"], "SESS", "id"),
    ("amount", ["amount", "charge_amount", "payment", "cost"], "", "amount"),
    ("balance", ["balance", "total_due", "net_amount", "gross_amount"], "", "amount"),
    ("ratio", ["completion_rate", "score", "confidence", "utilization"], "", "ratio"),
    ("hash", ["hash_id", "checksum", "fingerprint", "content_hash"], "", "hex"),
    ("uuid", ["uuid", "guid", "trace_uuid", "external_uuid"], "", "uuid"),
    ("epoch", ["created_ts", "updated_ts", "event_ts", "log_ts"], "", "epoch"),
    ("zip", ["zip_code", "postal_code", "area_code", "mail_code"], "", "zip"),
]

_NONE_STATUS_ALPHABETS: list[tuple[str, list[str]]] = [
    ("status_code", ["A", "B", "C"]),
    ("state", ["open", "closed", "pending"]),
    ("stage", ["new", "processing", "shipped", "delivered"]),
    ("priority", ["low", "medium", "high"]),
    ("lifecycle", ["draft", "published", "archived"]),
    ("check_result", ["pass", "fail"]),
    ("trend", ["up", "down"]),
    ("category_type", ["male", "female", "other"]),
]
_NONE_FLAG_ALPHABETS: list[tuple[str, list[str]]] = [
    ("is_active", ["0", "1"]),
    ("flag_bool", ["true", "false"]),
    ("verified_flag", ["Y", "N"]),
    ("confirmed_flag", ["yes", "no"]),
    ("bit_flag", ["T", "F"]),
    ("account_state", ["active", "inactive"]),
    ("switch_state", ["on", "off"]),
    ("feature_flag", ["enabled", "disabled"]),
]


def _none_value(kind: str, prefix: str, family_idx: int, local_k: int) -> str:
    # family_idx * 2_000_000 gives every family its own numeric block, so
    # "n" (used by id/amount/uuid) is globally unique across ALL none
    # columns even when two families share a prefix or kind.
    n = family_idx * 2_000_000 + local_k
    if kind == "id":
        return f"{prefix}{n:09d}"
    if kind == "amount":
        return f"{n // 100}.{n % 100:02d}"
    if kind == "ratio":
        return f"{local_k % 10000 / 100:.2f}"
    if kind == "hex":
        return f"{local_k:016x}"
    if kind == "uuid":
        hexn = f"{n:032x}"
        return f"{hexn[0:8]}-{hexn[8:12]}-{hexn[12:16]}-{hexn[16:20]}-{hexn[20:32]}"
    if kind == "epoch":
        return str(1_600_000_000 + local_k)
    if kind == "zip":
        return f"{10000 + local_k:05d}"
    return f"{n:09d}"


def _none_high_card_fixtures(cols_per_family: int = 21) -> list[LabeledFixture]:
    fx: list[LabeledFixture] = []
    for family_idx, (fam_key, headers, prefix, kind) in enumerate(_NONE_HIGH_CARD_FAMILIES):
        for col_local_idx in range(cols_per_family):
            hdr = headers[col_local_idx % len(headers)]
            vals = [
                _none_value(kind, prefix, family_idx, col_local_idx * _EXT_ROWS + row)
                for row in range(_EXT_ROWS)
            ]
            fx.append(
                _make_fixture(f"ext_none_{fam_key}_{col_local_idx:02d}", hdr, vals, NO_DETECTOR)
            )
    return fx


def _none_enum_fixtures() -> list[LabeledFixture]:
    fx: list[LabeledFixture] = []
    for header, alphabet in _NONE_STATUS_ALPHABETS + _NONE_FLAG_ALPHABETS:
        vals = [alphabet[row % len(alphabet)] for row in range(_EXT_ROWS)]
        fx.append(_make_fixture(f"ext_none_enum_{header}", header, vals, NO_DETECTOR))
    return fx


def build_extended_fixtures() -> list[LabeledFixture]:
    """Return the extended ML2 training corpus (~2960 labeled single-column
    fixtures) for the LightGBM sprint (ML2.2) and the Phase B rebaseline.

    Every column has a deterministic, fully-synthetic value set (no real
    PII) per ml-benchmarking-and-privacy.md §B.3. Each label's columns are
    value-disjoint from one another (module docstring "Leakage guard"),
    except ``cvv`` (documented exemption).

    Type distribution (~2960 total, ±15% per label):
    - ssn, email, pan, iban: 260 columns each
    - icd10, iso_date, npi: 240 columns each
    - cvv: 120 columns
    - mrn, health_plan_id: 300 columns each
    - none: 478 columns (22 high-cardinality families x 21 + 16 single
      low-cardinality status/flag columns)
    """
    n = _EXT_ROWS
    fx: list[LabeledFixture] = []

    ssn_total = 260
    for col_idx in range(ssn_total):
        hdr = _pick_header(_SSN_CLEAR, _SSN_ABBREV, _SSN_CRYPTIC, col_idx, ssn_total)
        fx.append(
            _make_fixture(f"ext_ssn_{col_idx:03d}", hdr, _ssn_values_for_col(col_idx, n), "ssn")
        )

    email_total = 260
    for col_idx in range(email_total):
        hdr = _pick_header(_EMAIL_CLEAR, _EMAIL_ABBREV, _EMAIL_CRYPTIC, col_idx, email_total)
        fx.append(
            _make_fixture(
                f"ext_email_{col_idx:03d}", hdr, _email_values_for_col(col_idx, n), "email"
            )
        )

    pan_total = 260
    for col_idx in range(pan_total):
        hdr = _pick_header(_PAN_CLEAR, _PAN_ABBREV, _PAN_CRYPTIC, col_idx, pan_total)
        fx.append(
            _make_fixture(f"ext_pan_{col_idx:03d}", hdr, _pan_values_for_col(col_idx, n), "pan")
        )

    iban_total = 260
    for col_idx in range(iban_total):
        hdr = _pick_header(_IBAN_CLEAR, _IBAN_ABBREV, _IBAN_CRYPTIC, col_idx, iban_total)
        fx.append(
            _make_fixture(f"ext_iban_{col_idx:03d}", hdr, _iban_values_for_col(col_idx, n), "iban")
        )

    icd10_total = 240
    for col_idx in range(icd10_total):
        hdr = _pick_header(_ICD10_CLEAR, _ICD10_ABBREV, _ICD10_CRYPTIC, col_idx, icd10_total)
        vals = _EXT_ICD10_POOL[col_idx * n : col_idx * n + n]
        fx.append(_make_fixture(f"ext_icd10_{col_idx:03d}", hdr, vals, "icd10"))

    iso_total = 240
    for col_idx in range(iso_total):
        hdr = _pick_header(_ISO_DATE_CLEAR, _ISO_DATE_ABBREV, _ISO_DATE_CRYPTIC, col_idx, iso_total)
        vals = _iso_date_values_from_base(_ISO_DATE_TRAIN_BASE, col_idx * n, n)
        fx.append(_make_fixture(f"ext_iso_date_{col_idx:03d}", hdr, vals, "iso_date"))

    npi_total = 240
    for col_idx in range(npi_total):
        hdr = _pick_header(_NPI_CLEAR, _NPI_ABBREV, _NPI_CRYPTIC, col_idx, npi_total)
        fx.append(
            _make_fixture(f"ext_npi_{col_idx:03d}", hdr, _npi_values_for_col(col_idx, n), "npi")
        )

    cvv_total = 120
    for col_idx in range(cvv_total):
        hdr = _pick_header(_CVV_CLEAR, _CVV_ABBREV, _CVV_CRYPTIC, col_idx, cvv_total)
        fx.append(
            _make_fixture(f"ext_cvv_{col_idx:03d}", hdr, _cvv_values_for_col(col_idx, n), "cvv")
        )

    mrn_total = 300
    for col_idx in range(mrn_total):
        hdr = _pick_header(_MRN_CLEAR, _MRN_ABBREV, _MRN_CRYPTIC, col_idx, mrn_total)
        vals = _institutional_values_for_col(col_idx, n, _MRN_PREFIXES)
        fx.append(_make_fixture(f"ext_mrn_{col_idx:03d}", hdr, vals, "mrn"))

    hp_total = 300
    for col_idx in range(hp_total):
        hdr = _pick_header(_HP_CLEAR, _HP_ABBREV, _HP_CRYPTIC, col_idx, hp_total)
        vals = _institutional_values_for_col(col_idx, n, _HP_PREFIXES)
        fx.append(_make_fixture(f"ext_hp_{col_idx:03d}", hdr, vals, "health_plan_id"))

    fx.extend(_none_high_card_fixtures())
    fx.extend(_none_enum_fixtures())

    return fx


# ── OOD (out-of-distribution) slice: cryptic headers + adversarial value
# obfuscation (no-dash SSN, spaced PAN, mixed-domain email). Evaluated
# SEPARATELY from the §A.1/§A.4 held-out test fold (§B.2). A distinct
# col_idx / date-base / ICD-10-pool offset keeps every value here disjoint
# from the training corpus.

_OOD_COL_OFFSET = 10_000


def build_ood_fixtures() -> list[LabeledFixture]:
    """Out-of-distribution / adversarial held-out slice (§B.2).

    Cryptic single/double-char headers PLUS value-obfuscation: spaced PAN,
    no-dash SSN, mixed-domain email. Evaluated SEPARATELY (not in the
    §A.1/§A.4 held-out test fold). Source: NIST SP 800-188 §B.2.
    """
    n = _EXT_ROWS
    fx: list[LabeledFixture] = []

    # SSN: dashed under single/double-char headers, plus no-dash obfuscation.
    for i, hdr in enumerate(["a1", "b2", "c3", "s1", "v1", "w2"]):
        col_idx = _OOD_COL_OFFSET + i
        vals = _ssn_values_for_col(col_idx, n)
        if i >= 4:  # last two: no-dash obfuscation (_SSN_RE uses -?, still matches)
            vals = [v.replace("-", "") for v in vals]
        fx.append(_make_fixture(f"ood_ssn_{i:02d}", hdr, vals, "ssn"))

    # EMAIL under cryptic headers with mixed domains.
    for i, hdr in enumerate(["e1", "f2", "g3", "h4"]):
        col_idx = _OOD_COL_OFFSET + i
        fx.append(
            _make_fixture(f"ood_email_{i:02d}", hdr, _email_values_for_col(col_idx, n), "email")
        )

    # PAN: spaced/dashed format under cryptic headers.
    for i, hdr in enumerate(["p1", "q2"]):
        col_idx = _OOD_COL_OFFSET + i
        fx.append(_make_fixture(f"ood_pan_{i:02d}", hdr, _pan_values_for_col(col_idx, n), "pan"))

    # IBAN under cryptic headers.
    for i, hdr in enumerate(["i1", "j2"]):
        col_idx = _OOD_COL_OFFSET + i
        fx.append(_make_fixture(f"ood_iban_{i:02d}", hdr, _iban_values_for_col(col_idx, n), "iban"))

    # ICD-10 under cryptic headers -- reserved pool region far from training.
    ood_icd10_start = 40_000
    for i, hdr in enumerate(["d1", "x2"]):
        start = ood_icd10_start + i * n
        fx.append(
            _make_fixture(f"ood_icd10_{i:02d}", hdr, _EXT_ICD10_POOL[start : start + n], "icd10")
        )

    # ISO date under cryptic headers -- reserved base date far from training.
    for i, hdr in enumerate(["t1", "u2"]):
        vals = _iso_date_values_from_base(_ISO_DATE_OOD_BASE, i * n, n)
        fx.append(_make_fixture(f"ood_isodt_{i:02d}", hdr, vals, "iso_date"))

    # NPI under cryptic headers.
    for i, hdr in enumerate(["n1", "m2"]):
        col_idx = _OOD_COL_OFFSET + i
        fx.append(_make_fixture(f"ood_npi_{i:02d}", hdr, _npi_values_for_col(col_idx, n), "npi"))

    # MRN under single-char headers (hardest case).
    for i, hdr in enumerate(["r1", "s2", "t3", "u4"]):
        col_idx = _OOD_COL_OFFSET + i
        vals = _institutional_values_for_col(col_idx, n, _MRN_PREFIXES)
        fx.append(_make_fixture(f"ood_mrn_{i:02d}", hdr, vals, "mrn"))

    # HEALTH_PLAN_ID under single-char headers.
    for i, hdr in enumerate(["w1", "x2", "y3", "z4"]):
        col_idx = _OOD_COL_OFFSET + i
        vals = _institutional_values_for_col(col_idx, n, _HP_PREFIXES)
        fx.append(_make_fixture(f"ood_hp_{i:02d}", hdr, vals, "health_plan_id"))

    # NONE under cryptic headers (short integers).
    for i, hdr in enumerate(["aa", "bb", "cc", "dd"]):
        vals = [str(9_000_000 + i * n + j) for j in range(n)]
        fx.append(_make_fixture(f"ood_none_{i:02d}", hdr, vals, NO_DETECTOR))

    return fx


# ── Cryptic-header benchmark slice (Phase B / CH-3 ablation) ────────────────
# Every header is cryptic/abbreviated; values are realistic per PII type.
# Evaluated SEPARATELY from both the train/test fold and the OOD slice --
# this is the benchmark CH-1/CH-2 header-lexicon lift is measured against.
# A distinct col_idx / date-base / ICD-10-pool offset from both the
# training corpus AND the OOD slice keeps every value here out of both.

_CRYPTIC_COL_OFFSET = 20_000

_CRYPTIC_HEADER_POOLS: dict[str, list[str]] = {
    "ssn": ["ssn_x", "tin_no", "s_id", "natid", "c9", "z7", "fld_a", "col7"],
    "email": ["em", "cont_em", "mail_x", "e_addr", "c3", "z9", "fld_b", "col8"],
    "pan": ["card_x", "pan_ref", "cc_x", "paymnt", "c4", "z1", "fld_c", "col9"],
    "iban": ["ib_x", "acct_x", "bank_x", "iban_ref", "c5", "z2", "fld_d", "col10"],
    "icd10": ["dx", "dx_cd", "diag_x", "icdx", "c6", "z3", "fld_e", "col11"],
    "iso_date": ["svc_dt", "dt_x", "evt_x", "d_ref", "c7", "z4", "fld_f", "col12"],
    "npi": ["npi_x", "prov_x", "phy_id", "n_ref", "c8", "z5", "fld_g", "col13"],
    "mrn": ["pt_no", "mr_x", "rec_x", "chart_x", "c1", "z6", "fld_h", "col14"],
    "health_plan_id": ["mbr_id", "plan_x", "hp_x", "cov_x", "c2", "z8", "fld_i", "col15"],
    "cvv": ["cv_x", "sec_x", "vcode", "c0", "z0", "fld_j", "col16"],
}


def _cryptic_hdr(label: str, i: int) -> str:
    pool = _CRYPTIC_HEADER_POOLS[label]
    return pool[i % len(pool)]


def build_cryptic_fixtures() -> list[LabeledFixture]:
    """Held-out cryptic-header benchmark slice (Phase B / CH-3 ablation).

    ~210 columns, every header cryptic/abbreviated, spanning all 10 real
    PII labels with realistic values. Evaluated SEPARATELY from the
    train/test fold (like build_ood_fixtures) -- this is the benchmark the
    CH-1/CH-2 header-lexicon lift is measured against.
    """
    n = _EXT_ROWS
    fx: list[LabeledFixture] = []
    cols_per_type = 22

    for i in range(cols_per_type):
        col_idx = _CRYPTIC_COL_OFFSET + i
        fx.append(
            _make_fixture(
                f"ch_ssn_{i:02d}", _cryptic_hdr("ssn", i), _ssn_values_for_col(col_idx, n), "ssn"
            )
        )

    for i in range(cols_per_type):
        col_idx = _CRYPTIC_COL_OFFSET + i
        vals = _email_values_for_col(col_idx, n)
        fx.append(_make_fixture(f"ch_email_{i:02d}", _cryptic_hdr("email", i), vals, "email"))

    for i in range(cols_per_type):
        col_idx = _CRYPTIC_COL_OFFSET + i
        vals = _pan_values_for_col(col_idx, n)
        fx.append(_make_fixture(f"ch_pan_{i:02d}", _cryptic_hdr("pan", i), vals, "pan"))

    for i in range(cols_per_type):
        col_idx = _CRYPTIC_COL_OFFSET + i
        vals = _iban_values_for_col(col_idx, n)
        fx.append(_make_fixture(f"ch_iban_{i:02d}", _cryptic_hdr("iban", i), vals, "iban"))

    icd10_start = 20_000
    for i in range(cols_per_type):
        start = icd10_start + i * n
        vals = _EXT_ICD10_POOL[start : start + n]
        fx.append(_make_fixture(f"ch_icd10_{i:02d}", _cryptic_hdr("icd10", i), vals, "icd10"))

    for i in range(cols_per_type):
        vals = _iso_date_values_from_base(_ISO_DATE_CRYPTIC_BASE, i * n, n)
        fx.append(_make_fixture(f"ch_isodt_{i:02d}", _cryptic_hdr("iso_date", i), vals, "iso_date"))

    for i in range(cols_per_type):
        col_idx = _CRYPTIC_COL_OFFSET + i
        vals = _npi_values_for_col(col_idx, n)
        fx.append(_make_fixture(f"ch_npi_{i:02d}", _cryptic_hdr("npi", i), vals, "npi"))

    for i in range(cols_per_type):
        col_idx = _CRYPTIC_COL_OFFSET + i
        vals = _institutional_values_for_col(col_idx, n, _MRN_PREFIXES)
        fx.append(_make_fixture(f"ch_mrn_{i:02d}", _cryptic_hdr("mrn", i), vals, "mrn"))

    for i in range(cols_per_type):
        col_idx = _CRYPTIC_COL_OFFSET + i
        vals = _institutional_values_for_col(col_idx, n, _HP_PREFIXES)
        fx.append(
            _make_fixture(
                f"ch_hp_{i:02d}", _cryptic_hdr("health_plan_id", i), vals, "health_plan_id"
            )
        )

    cvv_cols = 12
    for i in range(cvv_cols):
        # Shares cvv's small value space with the training slice; not
        # disjoint-guaranteed (documented exemption, see module docstring).
        col_idx = _CRYPTIC_COL_OFFSET + i
        vals = _cvv_values_for_col(col_idx, n)
        fx.append(_make_fixture(f"ch_cvv_{i:02d}", _cryptic_hdr("cvv", i), vals, "cvv"))

    return fx
