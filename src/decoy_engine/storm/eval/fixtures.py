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

The expanded ML training corpus (build_extended_fixtures / build_ood_fixtures
/ build_cryptic_fixtures) lives in ``decoy_engine.storm.eval.corpus`` (split
out of this module, MLF-4, 2026-07-19) -- it reuses the checksum generators
and ``LabeledFixture`` defined here.

Leakage guard (§A.3): every generator below is col_idx-aware -- each
column draws values from a slice of a value space that is EXCLUSIVE to
that column (either via a global index k = col_idx * rows_per_col + row
embedded directly in the formatted value, or via a disjoint slice of a
precomputed pool) -- so assign_value_level_groups() can never merge two
same-label columns into one fold-collapsing group. ``cvv`` is the one
documented exception: its realistic 3-4 digit value space (~9900 values)
is too small to stay disjoint across 100+ columns, so cvv windows wrap
and MAY overlap once the corpus exceeds ~120 columns; every other label
stays exactly disjoint at any corpus scale used here.
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

# fmt: off
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
# fmt: on


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
