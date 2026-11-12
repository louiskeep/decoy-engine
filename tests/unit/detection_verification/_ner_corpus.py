"""The NER calibration corpus and the held-out negative corpus (seeded, no network).

Frozen by `test_ner_corpus_frozen.py`, which pins the SHA-256 of each corpus's cells. Neither
corpus is edited after a measurement: a change would move the digest, and the threshold would
no longer be what the corpus measured. The calibration corpus sets `NER_MIN_RATE`; the held-out
corpus has its own seed and templates and is never used in the threshold.

Every column is free text (`is_free_text`) and a known negative: it holds no person name. A
column that names a city or street is a negative for `person_name` only, because
`NER_ENTITY_MAP` sends GPE, LOC and FAC to `location`.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from decoy_engine.detection.ner import NER_ENTITY_MAP

BOTH = frozenset(NER_ENTITY_MAP.values())
PERSON_ONLY = frozenset({"person_name"})
N = 2_000
SEED = 20261107
HELD_OUT_N = 1_000
HELD_OUT_SEED = 20261108
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


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


def person_pool() -> frozenset[str]:
    """Lowercase tokens Faker knows as given or family names, the pool prose cells are cleaned with.

    `_name_pool` also holds place words ("of", "the", "and" appear in country and state names),
    which would drop every English sentence; prose only needs person names kept out.
    """
    from faker.providers.person.en_US import Provider as Person

    return frozenset(
        t.lower() for entry in (*Person.first_names, *Person.last_names) for t in str(entry).split()
    )


def _clean(cells: list[str], pool: frozenset[str]) -> list[str]:
    """Drop any cell with a token that is a known person or place name: a negative holds none."""
    keep = [c for c in cells if not ({t.strip(".,;:!?()[]").lower() for t in c.split()} & pool)]
    return keep


def _faker_text(kind: str, pool: frozenset[str], seed: int, count: int, nb_words: int) -> list[str]:
    from faker import Faker

    fake = Faker("en_US")
    fake.seed_instance(seed)
    make: dict[str, Callable[[], str]] = {
        "sentence": lambda: fake.sentence(nb_words=nb_words),
        "slogan": fake.catch_phrase,
        "bs": fake.bs,
    }
    out: list[str] = []
    while len(out) < count:
        out += _clean([make[kind]() for _ in range(count)], pool)
    return out[:count]


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


_COMPANY_FIRST = [
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
_COMPANY_SECOND = [
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
_COMPANY_SUFFIX = ["Inc", "LLC", "Ltd", "Corp", "Group", "Co", "GmbH", "PLC"]
_PRODUCT_KIND = [
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
_PRODUCT_GRADE = ["Pro", "Max", "Lite", "Ultra", "Plus", "Mini", "Air", "Edge", "Prime", "Core"]
_PRODUCT_LINE = [
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


def _company(rng: random.Random) -> str:
    return (
        f"{rng.choice(_COMPANY_FIRST)} {rng.choice(_COMPANY_SECOND)} {rng.choice(_COMPANY_SUFFIX)}"
    )


def _product(rng: random.Random) -> str:
    return (
        f"{rng.choice(_PRODUCT_LINE)} {rng.choice(_PRODUCT_KIND)} "
        f"{rng.choice(_PRODUCT_GRADE)} {rng.randint(100, 9999)}"
    )


def _address(rng: random.Random) -> str:
    return f"{rng.randint(1, 9999)} {rng.choice(_STREETS)} {rng.choice(_SUFFIX)}"


# Sentence templates over the curated lists. A template names companies, products, cities or
# streets but never a person. The `{k}` slots are filled by `_fill`.
_COMPANY_PRODUCT_TEMPLATES = [
    "Shipped the {product} to {company} on {day}.",
    "{company} reordered the {product} after the trial ended.",
    "The invoice from {company} lists one {product} and a service plan.",
    "Support closed the ticket about the {product} from {company} last {day}.",
    "We compared the {product} with the {product2} before signing with {company}.",
    "Please confirm that {company} received the {product} on {day}.",
    "The warranty on the {product} expires before the {company} renewal.",
    "Procurement asked {company} for a discount on the {product} and the {product2}.",
]
_CITY_TEMPLATES = [
    "Shipped the {product} to the {city} warehouse on {day}.",
    "The {company} office in {city} reopened on {day}.",
    "Our team flew from {city} to {city2} for the {product} launch.",
    "The {product} is back in stock at the {city} store.",
    "{company} moved its returns desk from {city} to {city2} last {day}.",
    "A pallet of {product} units left the {city} depot on {day}.",
]
_STREET_TEMPLATES = [
    "Deliver the {product} to {address} before noon on {day}.",
    "The {company} depot at {address} closes early on {day}.",
    "Return the {product} to {address} within ten days.",
    "The pickup point at {address} holds the {product} until {day}.",
    "{company} listed {address} as the loading dock for the {product}.",
]
# Held-out phrasing: different sentence shapes from the calibration templates above.
_HELD_OUT_COMPANY_PRODUCT_TEMPLATES = [
    "On {day} the vendor {company} quoted a lead time for the {product}.",
    "Did the {product} pass inspection, or does {company} owe us a replacement?",
    "Stock of the {product} and the {product2} is low, so {company} will restock.",
    "A refund for the {product} was approved by {company} after review.",
    "Every {product} in the batch from {company} shipped with the new manual.",
    "Quality flagged the {product} again; {company} has been notified.",
]
_HELD_OUT_PLACE_TEMPLATES = [
    "The {product} arrives at the {city} hub on {day}, then goes to {address}.",
    "Drivers leaving {city} will pass {address} on the way to {city2}.",
    "{company} opened a new branch in {city} next to {address}.",
    "Stock for {address} and for the {city} store ships on {day}.",
    "The audit visited {city} first and {address} the day after.",
    "Customers near {city} can collect the {product} at {address}.",
]


def _fill(rng: random.Random, template: str) -> str:
    return template.format(
        product=_product(rng),
        product2=_product(rng),
        company=_company(rng),
        city=rng.choice(_CITIES),
        city2=rng.choice(_CITIES),
        address=_address(rng),
        day=rng.choice(DAYS),
    )


def _prose(seed: int, templates: Sequence[str], pool: frozenset[str], count: int) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = []
    while len(out) < count:
        out += _clean([_fill(rng, rng.choice(templates)) for _ in range(count)], pool)
    return out[:count]


def corpus() -> list[Negative]:
    """The calibration corpus: free-text negatives only."""
    pool = _name_pool()
    names = person_pool()
    both = [
        ("redacted_notes", _redacted_notes()),
        ("faker_sentences", _faker_text("sentence", pool, SEED, N, 9)),
        ("faker_catch_phrases", _faker_text("slogan", pool, SEED, N, 9)),
        ("faker_bs", _faker_text("bs", pool, SEED, N, 9)),
        ("prose_company_product", _prose(SEED + 5, _COMPANY_PRODUCT_TEMPLATES, names, N)),
    ]
    person_only = [
        ("prose_cities", _prose(SEED + 6, _CITY_TEMPLATES, names, N)),
        ("prose_streets", _prose(SEED + 7, _STREET_TEMPLATES, names, N)),
    ]
    return [Negative(n, BOTH, tuple(c)) for n, c in both] + [
        Negative(n, PERSON_ONLY, tuple(c)) for n, c in person_only
    ]


def held_out_corpus() -> list[Negative]:
    """Held-out free-text negatives: own seed and templates, never used in the threshold."""
    pool = _name_pool()
    names = person_pool()
    return [
        Negative(
            "held_out_faker_sentences",
            BOTH,
            tuple(_faker_text("sentence", pool, HELD_OUT_SEED, HELD_OUT_N, 14)),
        ),
        Negative(
            "held_out_company_product",
            BOTH,
            tuple(
                _prose(HELD_OUT_SEED + 1, _HELD_OUT_COMPANY_PRODUCT_TEMPLATES, names, HELD_OUT_N)
            ),
        ),
        Negative(
            "held_out_places",
            PERSON_ONLY,
            tuple(_prose(HELD_OUT_SEED + 2, _HELD_OUT_PLACE_TEMPLATES, names, HELD_OUT_N)),
        ),
    ]


def digest(columns: Sequence[Negative]) -> str:
    """SHA-256 over every column's name, negative ids and cells, in order."""
    payload = [[c.name, sorted(c.negative_for), list(c.cells)] for c in columns]
    blob = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("ascii")).hexdigest()
