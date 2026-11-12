"""Per-type value generators and pools for the expanded ML corpus.

Moved verbatim from ``corpus.py`` (H-1 Wave B E3), which builds the fixtures
and imports what it needs from here.
"""

from __future__ import annotations

from datetime import date, timedelta

from decoy_engine.profiling.eval.fixtures import _luhn_check_digit, make_iban, make_npi

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
