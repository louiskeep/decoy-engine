"""NER_MIN_RATE is the calibrated threshold, recomputed here from known negatives.

Rule (owner decision Q7, 2026-11-07; Neyman-Pearson style false-positive control, Tong, Feng and
Li 2018; Clopper and Pearson 1934):

    threshold = max(2 * highest observed false-hit rate,
                    highest one-sided 95% Clopper-Pearson upper bound across the negative columns,
                    0.01)

A false-hit rate is the share of sampled cells holding at least one span of one detector id. The
rule works on that cell share, never on a spaCy score (spaCy scores are not calibrated). A column
is a known negative only for the detector ids it does not contain: `NER_ENTITY_MAP` sends GPE, LOC
and FAC to `location`, so city and street columns are negatives for `person_name` only; company
and product names, redacted notes, Faker text and identifier columns are negatives for both.

Rounding: the rule value is rounded UP to the next 0.001, so the constant never sits below the
rule. The test asserts `NER_MIN_RATE == ceil_to_thousandth(threshold)`, so the constant cannot
drift from the corpus or the model, and it is the evidence for the table in `_ner_rate.py`.

Each column is deterministic (seeded, no network) with up to `NER_SCAN_CELLS` cells. The eval
fixtures in `profiling/eval/fixtures.py` hold only 40-row identifier columns (a 40-cell column
alone gives a bound near 0.07), so the identifier columns here come from the extended corpus
generators in `profiling/eval/_corpus_values.py`, which build the same label types at any size.
Set DECOY_NER_RATE_EVIDENCE=<path> to write the measured table as JSON.
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass
from pathlib import Path

import pytest

from decoy_engine.detection import _ner_rate as nr
from decoy_engine.detection.ner import NER_ENTITY_MAP
from decoy_engine.profiling.eval import _corpus_values as cv
from tests._ner import needs_ner

BOTH = frozenset(NER_ENTITY_MAP.values())
PERSON_ONLY = frozenset({"person_name"})
N = nr.NER_SCAN_CELLS
SEED = 20261107


@dataclass(frozen=True)
class Negative:
    name: str
    negative_for: frozenset[str]
    cells: tuple[str, ...]


def _name_pool() -> frozenset[str]:
    """Lowercase tokens Faker knows as person, state, city-part or country names."""
    from faker.providers.address import Provider as Base
    from faker.providers.address.en_US import Provider as Address
    from faker.providers.person.en_US import Provider as Person

    pool: set[str] = set()
    for source in (
        Person.first_names,
        Person.last_names,
        Address.states,
        Address.city_prefixes,
        Address.city_suffixes,
        Base.countries,
    ):
        for entry in source:
            pool.update(t.lower() for t in str(entry).replace("-", " ").split())
    return frozenset(pool)


def _clean(cells: list[str], pool: frozenset[str]) -> list[str]:
    """Drop any cell with a token that is a known person or place name: a negative holds none."""
    keep = [c for c in cells if not ({t.strip(".,;:!?()[]").lower() for t in c.split()} & pool)]
    return keep


def _faker_text(kind: str, pool: frozenset[str]) -> list[str]:
    from faker import Faker

    fake = Faker("en_US")
    fake.seed_instance(SEED)
    make = {
        "sentence": lambda: fake.sentence(nb_words=9),
        "slogan": fake.catch_phrase,
        "bs": fake.bs,
    }[kind]
    out: list[str] = []
    while len(out) < N:
        out += _clean([make() for _ in range(N)], pool)
    return out[:N]


def _redacted_notes() -> list[str]:
    rng = random.Random(SEED)
    openers = [
        "Customer",
        "Caller",
        "Applicant",
        "Member",
        "Claimant",
        "Patient",
        "Tenant",
        "Client",
    ]
    middles = [
        "reported a billing problem",
        "asked to update the file",
        "disputed the last invoice",
        "requested a callback",
        "confirmed the appointment",
        "escalated the ticket",
        "cancelled the renewal",
        "sent a follow-up question",
    ]
    markers = [
        "[REDACTED:person_name]",
        "[REDACTED:email]",
        "[REDACTED:us_phone]",
        "[REDACTED:ssn]",
        "[REDACTED:address]",
        "[REDACTED:location]",
    ]
    return [
        f"{rng.choice(openers)} {rng.choice(markers)} {rng.choice(middles)}; ref {rng.choice(markers)}"
        f" on {rng.randint(1, 28):02d}/{rng.randint(1, 12):02d}."
        for _ in range(N)
    ]


def _company_names(pool: frozenset[str]) -> list[str]:
    rng = random.Random(SEED + 1)
    first = [
        "Vertex",
        "Quantum",
        "Lumen",
        "Apex",
        "Nimbus",
        "Cobalt",
        "Helix",
        "Zenith",
        "Vector",
        "Prism",
        "Summit",
        "Fusion",
        "Catalyst",
        "Pivot",
        "Axiom",
        "Beacon",
        "Cascade",
        "Ember",
        "Flux",
        "Kinetic",
        "Nova",
        "Onyx",
        "Pulse",
        "Radiant",
        "Sterling",
        "Titan",
        "Unity",
        "Vivid",
        "Zephyr",
        "Alloy",
        "Bridgeway",
        "Clarity",
        "Dynamo",
        "Evergreen",
        "Frontier",
    ]
    second = [
        "Dynamics",
        "Logistics",
        "Analytics",
        "Systems",
        "Holdings",
        "Partners",
        "Solutions",
        "Labs",
        "Industries",
        "Networks",
        "Robotics",
        "Materials",
        "Capital",
        "Freight",
        "Software",
        "Foods",
        "Energy",
        "Media",
        "Health",
        "Design",
    ]
    suffix = ["Inc", "LLC", "Ltd", "Corp", "Group", "Co", "GmbH", "PLC"]
    out: list[str] = []
    while len(out) < N:
        out += _clean(
            [f"{rng.choice(first)} {rng.choice(second)} {rng.choice(suffix)}" for _ in range(N)],
            pool,
        )
    return out[:N]


def _product_names(pool: frozenset[str]) -> list[str]:
    rng = random.Random(SEED + 2)
    kind = [
        "Widget",
        "Router",
        "Blender",
        "Backpack",
        "Headset",
        "Keyboard",
        "Monitor",
        "Sensor",
        "Drill",
        "Kettle",
        "Lantern",
        "Tracker",
        "Charger",
        "Speaker",
        "Camera",
        "Thermostat",
    ]
    grade = ["Pro", "Max", "Lite", "Ultra", "Plus", "Mini", "Air", "Edge", "Prime", "Core"]
    line = [
        "UltraGrip",
        "SoftTouch",
        "QuickCharge",
        "AeroFlow",
        "PowerCell",
        "ClearView",
        "SmartLink",
        "FlexFit",
        "ThermoShield",
        "NanoCore",
        "HyperDrive",
        "EcoSeries",
    ]
    out: list[str] = []
    while len(out) < N:
        out += _clean(
            [
                f"{rng.choice(line)} {rng.choice(kind)} {rng.choice(grade)} {rng.randint(100, 9999)}"
                for _ in range(N)
            ],
            pool,
        )
    return out[:N]


_CITIES = [
    "Denver",
    "Seattle",
    "Chicago",
    "Houston",
    "Phoenix",
    "Boston",
    "Atlanta",
    "Miami",
    "Detroit",
    "Memphis",
    "Nashville",
    "Portland",
    "Sacramento",
    "Tucson",
    "Omaha",
    "Tulsa",
    "Cleveland",
    "Pittsburgh",
    "Baltimore",
    "Milwaukee",
    "Albuquerque",
    "Louisville",
    "Richmond",
    "Raleigh",
    "Madison",
    "Dallas",
    "Austin",
    "Orlando",
    "Tampa",
    "Charlotte",
    "Springfield",
    "Columbus",
    "London",
    "Paris",
    "Berlin",
    "Madrid",
    "Toronto",
    "Sydney",
    "Dublin",
    "Lisbon",
    "Vienna",
    "Prague",
    "Osaka",
    "Mumbai",
    "Cairo",
    "Lagos",
    "Lima",
    "Oslo",
    "Helsinki",
    "Warsaw",
]
_STREETS = [
    "Main",
    "Oak",
    "Maple",
    "Cedar",
    "Elm",
    "Pine",
    "Park",
    "Lake",
    "Hill",
    "Sunset",
    "Ridge",
    "Walnut",
    "Spring",
    "Church",
    "Mill",
    "River",
    "Highland",
    "Meadow",
    "Forest",
    "Chestnut",
    "Washington",
    "Lincoln",
    "Jefferson",
    "Franklin",
    "Jackson",
    "Madison",
    "Willow",
    "Birch",
    "Cherry",
    "Dogwood",
    "Hickory",
    "Magnolia",
    "Poplar",
    "Sycamore",
    "Spruce",
    "Aspen",
]
_SUFFIX = [
    "Street",
    "Avenue",
    "Road",
    "Drive",
    "Lane",
    "Court",
    "Boulevard",
    "Way",
    "Place",
    "Terrace",
]


def _city_cells() -> list[str]:
    rng = random.Random(SEED + 3)
    return [f"{rng.choice(_CITIES)}" for _ in range(N)]


def _street_cells() -> list[str]:
    rng = random.Random(SEED + 4)
    return [
        f"{rng.randint(1, 9999)} {rng.choice(_STREETS)} {rng.choice(_SUFFIX)}" for _ in range(N)
    ]


def corpus() -> list[Negative]:
    pool = _name_pool()
    both = [
        ("redacted_notes", _redacted_notes()),
        ("fixture_mrn", cv._institutional_values_for_col(0, N, cv._MRN_PREFIXES)),
        ("fixture_health_plan_id", cv._institutional_values_for_col(1, N, cv._HP_PREFIXES)),
        ("fixture_iso_dates", cv._iso_date_values_from_base(cv._ISO_DATE_TRAIN_BASE, 0, N)),
        ("faker_sentences", _faker_text("sentence", pool)),
        ("faker_catch_phrases", _faker_text("slogan", pool)),
        ("faker_bs", _faker_text("bs", pool)),
        ("company_names", _company_names(pool)),
        ("product_names", _product_names(pool)),
    ]
    person_only = [("city_names", _city_cells()), ("street_addresses", _street_cells())]
    return [Negative(n, BOTH, tuple(c)) for n, c in both] + [
        Negative(n, PERSON_ONLY, tuple(c)) for n, c in person_only
    ]


def clopper_pearson_upper(hits: int, cells: int, confidence: float = 0.95) -> float:
    """One-sided upper bound of the hit rate: scipy's beta quantile, exact closed form at zero hits."""
    if hits >= cells:
        return 1.0
    try:
        from scipy.stats import beta
    except ImportError:
        if hits != 0:
            pytest.fail("scipy is needed for a non-zero Clopper-Pearson bound")
        return 1.0 - (1.0 - confidence) ** (1.0 / cells)
    return float(beta.ppf(confidence, hits + 1, cells - hits))


def ceil_to_thousandth(value: float) -> float:
    return math.ceil(round(value * 1000, 9)) / 1000


@needs_ner
def test_every_negative_column_is_full_size_and_free_of_known_names() -> None:
    for column in corpus():
        assert len(column.cells) == N, column.name
        assert all(isinstance(c, str) and c for c in column.cells), column.name
    pool = _name_pool()
    for column in corpus():
        if column.name.startswith(("faker_", "company_", "product_")):
            for cell in column.cells:
                tokens = {t.strip(".,;:!?()[]").lower() for t in cell.split()}
                assert not tokens & pool, (column.name, "holds a known name")


@pytest.mark.xfail(
    strict=True,
    reason="rule output is 0.557 (> 0.5): NER_MIN_RATE waits for an owner decision",
)
@needs_ner
def test_ner_min_rate_is_the_calibrated_threshold() -> None:
    table = []
    for column in corpus():
        rates = nr.ner_rates(column.cells)
        assert set(rates) == set(NER_ENTITY_MAP.values()), column.name
        for detector_id in sorted(column.negative_for):
            r = rates[detector_id]
            table.append(
                {
                    "column": column.name,
                    "detector_id": detector_id,
                    "hits": r.hit_cells,
                    "cells": r.scanned_cells,
                    "rate": r.rate,
                    "upper_95": clopper_pearson_upper(r.hit_cells, r.scanned_cells),
                }
            )
    highest_rate = max(row["rate"] for row in table)
    highest_bound = max(row["upper_95"] for row in table)
    threshold = max(2 * highest_rate, highest_bound, 0.01)
    expected = ceil_to_thousandth(threshold)
    evidence = {
        "rows": table,
        "2x_highest_rate": 2 * highest_rate,
        "highest_upper_95": highest_bound,
        "threshold": threshold,
        "NER_MIN_RATE": expected,
    }
    path = os.environ.get("DECOY_NER_RATE_EVIDENCE")
    if path:
        Path(path).write_text(json.dumps(evidence, indent=1))
    print(json.dumps(evidence, indent=1))
    assert expected <= 0.5, "a threshold this high makes the detector useless: ask the owner"
    assert expected == nr.NER_MIN_RATE
