"""Synthea-shaped FK dataset: schema, column strategies, and a chunked writer.

This backs the RW-A / A-2 "100M Synthea scale characterization" harness. It is a
healthcare-shaped specialization of the D4 generic chain in
`scripts/fk-scale/_chain_layout.py`: same established pattern (positional
row-index keys, one bounded chunk resident at a time, one Parquet row group per
chunk so a parity window reads without a full scan), but with the three tables a
Synthea export produces (patients -> encounters -> observations) and realistic
PHI columns instead of opaque payloads.

One spec drives all three consumers so a column's shape lives in one place:
  - the fixture writer below turns `gen` into values,
  - `_synthea_config.build_config` turns `strategy`/`namespace`/`provider*` into
    the mask rule a hospital customer would write,
  - `_synthea_parity` reads the key and FK columns.

Layout, positional so no table needs its parent resident to build a valid FK
(the formula lives only here):
  patients      id        = P{i}                         i in [0, n_patients)
  encounters    id        = E{i}                         i in [0, n_encounters)
                patient   = P{i % n_patients}
  observations  encounter = E{i % n_encounters}
                patient   = P{(i % n_encounters) % n_patients}  (same patient as
                            the row's encounter, so the denormalized FK is coherent)
Referential integrity holds by construction, and a head/middle/tail window of any
FK column resolves inside a bounded window of its parent, which keeps the parity
check O(sample) at any row count.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

TABLE_NAMES = ("patients", "encounters", "observations")

# child table -> (fk column, parent table, parent key column). The chain is a
# single path patients -> encounters -> observations; `observations.patient` is a
# denormalized, coherently-masked reference, not a declared relationship edge.
FK_EDGE = {
    "encounters": ("patient", "patients", "id"),
    "observations": ("encounter", "encounters", "id"),
}
KEY_COLUMN = {"patients": "id", "encounters": "id"}
KEY_PREFIX = {"patients": "P", "encounters": "E"}

DEFAULT_CHUNK_ROWS = 50_000
_POOL = 4096  # distinct values per generated pool; same order as the D4 chain filler
_SEED_OFFSET = {"patients": 0, "encounters": 1, "observations": 2}
_EPOCH = date(1950, 1, 1)
_DATE_SPAN_DAYS = 27_000  # ~1950..2024, covers birth through recent encounters


@dataclass(frozen=True)
class ColumnSpec:
    """A column's generation family and its mask rule, in one place.

    `gen` is the value family the writer produces; `strategy` (+ namespace /
    provider / provider_config) is exactly what the mask config emits. `role`
    marks the key and FK columns the FK wiring and parity read.
    """

    name: str
    gen: str
    strategy: str
    role: str = "payload"  # "key" | "fk" | "payload"
    namespace: str | None = None
    provider: str | None = None
    provider_config: dict[str, Any] = field(default_factory=dict)


# Namespaces: the two FK edges each get one shared namespace so the parent key and
# the child FK mask to the SAME value (joinability preserved). `observations.patient`
# reuses the patient namespace so a denormalized patient join still holds.
NS_PATIENT = "synthea_patient_id"
NS_ENCOUNTER = "synthea_encounter_id"


def _fpe(charset: str) -> dict[str, Any]:
    return {"charset": charset}


# Synthetic replacement pools for the deterministic `categorical` remap of the
# direct-identifier name/address columns. These are the MASKED output values (the
# source names are drawn from the separate `_FIRST` / `_LAST` / `_STREET` / `_CITY`
# generation pools below). Kept DISJOINT from those source pools on purpose: the
# masked addresses carry a house number and the names/cities share no entry with
# the source sets, so a masked cell is always a different string from its source --
# which is the property the "names/addresses are genuinely transformed" acceptance
# test (tests/unit/scripts/test_synthea_scale_infra.py) asserts on the out-of-core
# route. The remap is deterministic (same source value -> same synthetic value
# within the column namespace), so it is consistent pseudonymization, not a reshuffle.
_FIRST_POOL = [
    "Olivia", "Liam", "Emma", "Noah", "Ava", "Ethan", "Sophia", "Mason",
    "Isabella", "Logan", "Mia", "Lucas", "Amelia", "Jackson", "Harper", "Aiden",
]  # fmt: skip
_LAST_POOL = [
    "Bennett", "Coleman", "Fletcher", "Hayes", "Ingram", "Keller", "Mercer", "Novak",
    "Osborne", "Pratt", "Reeves", "Sutton", "Vance", "Whitaker", "Yates", "Abbott",
]  # fmt: skip
# Every entry carries a house number, so none can collide with a bare source street
# name ("Main St", ...) -- the masked value is always distinct from the source.
_ADDRESS_POOL = [
    "128 Birch Ter", "47 Willow Ct", "953 Sycamore Way", "12 Juniper Pl",
    "604 Aspen Loop", "8 Magnolia Row", "275 Dogwood Path", "1120 Spruce Walk",
]  # fmt: skip
_CITY_POOL = [
    "Austin", "Denver", "Seattle", "Portland", "Nashville", "Raleigh", "Madison",
    "Boulder", "Tucson", "Fresno", "Dayton", "Akron",
]  # fmt: skip


PATIENTS: tuple[ColumnSpec, ...] = (
    ColumnSpec("id", "key", "hash", role="key", namespace=NS_PATIENT),
    ColumnSpec("ssn", "ssn", "fpe", namespace="synthea_ssn", provider_config=_fpe("digits")),
    ColumnSpec(
        "drivers", "license", "fpe", namespace="synthea_dl", provider_config=_fpe("ALPHANUM")
    ),
    ColumnSpec(
        "passport", "license", "fpe", namespace="synthea_pp", provider_config=_fpe("ALPHANUM")
    ),
    # Direct-identifier name cells are replaced by a deterministic `categorical`
    # remap onto a synthetic name pool: the same source name maps to the same
    # synthetic name within the column's namespace (consistent pseudonymization),
    # and the compiled native categorical kernel runs it on the bounded out-of-core
    # route. `text_redact` is a free-text SPAN pass (it removes detected PII spans
    # inside a cell); on a single-token name cell with no NER model configured it
    # detects nothing and leaves the value UNCHANGED, so it is the wrong tool for a
    # structured identifier column. Synthetic replacement via `faker` would also fit
    # but is not out-of-core-supported, so it would force the unbounded sequential
    # route (see docs/records and _compat.py); deterministic `categorical` is the
    # only native, name-shaped strategy on the out-of-core route.
    ColumnSpec("first", "first_name", "categorical", provider_config={"categories": _FIRST_POOL}),
    ColumnSpec("last", "last_name", "categorical", provider_config={"categories": _LAST_POOL}),
    # Dates generalized to the month via keyed bucket-perturb (a Safe-Harbor style
    # date reduction, and out-of-core-supported, unlike per-value date_shift).
    ColumnSpec(
        "birthdate",
        "date",
        "bucket_perturb",
        namespace="synthea_dob",
        provider_config={"bucket": "month", "date_format": "%Y-%m-%d"},
    ),
    ColumnSpec(
        "deathdate",
        "date",
        "bucket_perturb",
        namespace="synthea_dod",
        provider_config={"bucket": "month", "date_format": "%Y-%m-%d"},
    ),
    ColumnSpec(
        "marital",
        "cat_marital",
        "categorical",
        provider_config={"categories": ["M", "S", "D", "W"]},
    ),
    ColumnSpec(
        "race",
        "cat_race",
        "categorical",
        provider_config={"categories": ["white", "black", "asian", "hispanic", "native", "other"]},
    ),
    ColumnSpec("gender", "cat_gender", "categorical", provider_config={"categories": ["M", "F"]}),
    # Street address and city: same deterministic `categorical` remap onto a
    # synthetic pool as the name columns above (native on the out-of-core route).
    ColumnSpec("address", "address", "categorical", provider_config={"categories": _ADDRESS_POOL}),
    ColumnSpec("city", "city", "categorical", provider_config={"categories": _CITY_POOL}),
    ColumnSpec(
        "state",
        "cat_state",
        "categorical",
        provider_config={"categories": ["MA", "NY", "CA", "TX", "FL", "IL", "PA", "OH"]},
    ),
    # ZIP masked by HIPAA Safe-Harbor truncation to the 3-digit prefix (FPE needs a
    # >=6-digit domain, which a 5-digit ZIP cannot meet; truncation is the standard
    # de-identification for ZIP anyway).
    ColumnSpec("zip", "zip", "truncate", provider_config={"length": 3}),
)

ENCOUNTERS: tuple[ColumnSpec, ...] = (
    ColumnSpec("id", "key", "hash", role="key", namespace=NS_ENCOUNTER),
    ColumnSpec("patient", "fk", "hash", role="fk", namespace=NS_PATIENT),
    ColumnSpec(
        "start",
        "date",
        "bucket_perturb",
        namespace="synthea_enc_start",
        provider_config={"bucket": "month", "date_format": "%Y-%m-%d"},
    ),
    ColumnSpec(
        "stop",
        "date",
        "bucket_perturb",
        namespace="synthea_enc_stop",
        provider_config={"bucket": "month", "date_format": "%Y-%m-%d"},
    ),
    ColumnSpec("organization", "uuid", "hash", namespace="synthea_org"),
    ColumnSpec("provider", "uuid", "hash", namespace="synthea_prov"),
    ColumnSpec("payer", "uuid", "hash", namespace="synthea_payer"),
    ColumnSpec(
        "encounterclass",
        "cat_encclass",
        "categorical",
        provider_config={
            "categories": [
                "ambulatory",
                "emergency",
                "inpatient",
                "outpatient",
                "wellness",
                "urgentcare",
            ]
        },
    ),
    ColumnSpec("code", "code", "hash", namespace="synthea_enc_code"),
    ColumnSpec("description", "text", "text_redact"),
    ColumnSpec(
        "base_cost", "cost", "fpe", namespace="synthea_base_cost", provider_config=_fpe("digits")
    ),
    ColumnSpec(
        "total_claim_cost",
        "cost",
        "fpe",
        namespace="synthea_claim_cost",
        provider_config=_fpe("digits"),
    ),
    ColumnSpec(
        "payer_coverage",
        "cost",
        "fpe",
        namespace="synthea_coverage",
        provider_config=_fpe("digits"),
    ),
    ColumnSpec("reasoncode", "code", "hash", namespace="synthea_reason_code"),
    ColumnSpec("reasondescription", "text", "text_redact"),
)

OBSERVATIONS: tuple[ColumnSpec, ...] = (
    ColumnSpec("encounter", "fk", "hash", role="fk", namespace=NS_ENCOUNTER),
    ColumnSpec("patient", "fk_patient", "hash", namespace=NS_PATIENT),
    ColumnSpec(
        "date",
        "date",
        "bucket_perturb",
        namespace="synthea_obs_date",
        provider_config={"bucket": "month", "date_format": "%Y-%m-%d"},
    ),
    ColumnSpec(
        "category",
        "cat_obscat",
        "categorical",
        provider_config={"categories": ["vital-signs", "laboratory", "survey", "imaging"]},
    ),
    ColumnSpec("code", "code", "hash", namespace="synthea_obs_code"),
    ColumnSpec("description", "text", "text_redact"),
    ColumnSpec(
        "value", "value", "fpe", namespace="synthea_obs_value", provider_config=_fpe("digits")
    ),
    ColumnSpec(
        "units",
        "cat_units",
        "categorical",
        provider_config={"categories": ["mg/dL", "mmHg", "kg", "cm", "/min", "%"]},
    ),
    ColumnSpec(
        "type", "cat_obstype", "categorical", provider_config={"categories": ["numeric", "text"]}
    ),
    ColumnSpec("provider", "uuid", "hash", namespace="synthea_prov"),
)

COLUMNS: dict[str, tuple[ColumnSpec, ...]] = {
    "patients": PATIENTS,
    "encounters": ENCOUNTERS,
    "observations": OBSERVATIONS,
}


def column_count(table: str) -> int:
    return len(COLUMNS[table])


def total_columns() -> int:
    return sum(column_count(t) for t in TABLE_NAMES)


def table_schema(table: str) -> pa.Schema:
    return pa.schema([pa.field(c.name, pa.string()) for c in COLUMNS[table]])


# ------------------------------------------------------------------ value families


def _keys(prefix: str, idx: np.ndarray) -> np.ndarray:
    return (prefix + idx.astype("U")).astype(object)


def _pool_strings(rng: np.random.Generator, n: int, width: int) -> np.ndarray:
    alphabet = np.frombuffer(
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", dtype="S1"
    )
    picks = rng.integers(0, len(alphabet), size=(n, width))
    return np.array([b"".join(row).decode("ascii") for row in alphabet[picks]], dtype=object)


def _pool_digits(rng: np.random.Generator, n: int, width: int) -> np.ndarray:
    picks = rng.integers(0, 10, size=(n, width))
    return np.array(["".join(map(str, row)) for row in picks], dtype=object)


def _pool_dates(rng: np.random.Generator, n: int) -> np.ndarray:
    offs = rng.integers(0, _DATE_SPAN_DAYS, size=n)
    return np.array([(_EPOCH + timedelta(days=int(o))).isoformat() for o in offs], dtype=object)


_FIRST = [
    "James",
    "Mary",
    "John",
    "Patricia",
    "Robert",
    "Jennifer",
    "Michael",
    "Linda",
    "William",
    "Elizabeth",
    "David",
    "Barbara",
    "Maria",
    "Jose",
    "Wei",
    "Mohammed",
]
_LAST = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Lee",
]
_STREET = ["Main St", "Oak Ave", "Maple Dr", "Elm St", "Cedar Ln", "Pine Rd", "Washington Blvd"]
_CITY = ["Boston", "Springfield", "Worcester", "Cambridge", "Lowell", "Quincy", "Newton"]
_WORDS = [
    "Encounter for check up",
    "Chronic condition review",
    "Acute symptom visit",
    "Routine laboratory panel",
    "Follow up assessment",
    "Medication reconciliation",
]


def _pool_from(
    rng: np.random.Generator, choices: list[str], n: int, prefix: str = ""
) -> np.ndarray:
    base = np.array(choices, dtype=object)
    picks = base[rng.integers(0, len(base), size=n)]
    if prefix:
        nums = rng.integers(0, 10_000, size=n)
        picks = np.array(
            [f"{prefix}{p} {k}" for p, k in zip(picks, nums, strict=True)], dtype=object
        )
    return picks


def _build_pools(rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Fixed-size pools generated once per table; chunk values index into them.

    Pools keep generation fast at tens of millions of rows (the cost is one pool,
    not one draw per row) while staying varied enough to be a realistic workload.
    """
    return {
        "ssn": _pool_digits(rng, _POOL, 9),
        "license": _pool_strings(rng, _POOL, 9),
        "zip": _pool_digits(rng, _POOL, 5),
        "cost": _pool_digits(rng, _POOL, 6),
        # 6 digits so the value column meets the FF1 >=6-digit digit domain floor.
        "value": _pool_digits(rng, _POOL, 6),
        "uuid": _pool_strings(rng, _POOL, 16),
        "code": _pool_from(rng, [str(c) for c in range(10000, 10000 + 512)], _POOL),
        "date": _pool_dates(rng, _POOL),
        "first_name": np.array(_FIRST, dtype=object),
        "last_name": np.array(_LAST, dtype=object),
        "address": _pool_from(rng, _STREET, _POOL, prefix=""),
        "city": np.array(_CITY, dtype=object),
        "text": np.array(_WORDS, dtype=object),
    }


def _categorical_values(spec: ColumnSpec, n: int, rng: np.random.Generator) -> np.ndarray:
    base = np.array(spec.provider_config["categories"], dtype=object)
    return base[rng.integers(0, len(base), size=n)]


def _column_values(
    table: str,
    spec: ColumnSpec,
    start: int,
    length: int,
    counts: Mapping[str, int],
    rng: np.random.Generator,
    pools: dict[str, np.ndarray],
) -> np.ndarray:
    idx = np.arange(start, start + length)
    gen = spec.gen
    if gen == "key":
        return _keys(KEY_PREFIX[table], idx)
    if gen == "fk":
        if table == "encounters":
            return _keys("P", idx % counts["patients"])
        return _keys("E", idx % counts["encounters"])  # observations.encounter
    if gen == "fk_patient":
        enc_idx = idx % counts["encounters"]
        return _keys("P", enc_idx % counts["patients"])
    if gen.startswith("cat_"):
        return _categorical_values(spec, length, rng)
    pool = pools[gen]
    return pool[rng.integers(0, len(pool), size=length)]


def _chunk(
    table: str,
    start: int,
    length: int,
    counts: Mapping[str, int],
    rng: np.random.Generator,
    pools: dict[str, np.ndarray],
) -> pa.Table:
    cols = {
        spec.name: _column_values(table, spec, start, length, counts, rng, pools)
        for spec in COLUMNS[table]
    }
    return pa.table(cols, schema=table_schema(table))


def write_table_chunked(
    table: str,
    path: Path,
    counts: Mapping[str, int],
    *,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    seed: int,
) -> None:
    """Write one table `chunk_rows` rows at a time; never more than one chunk resident.

    One Parquet row group per chunk, which the parity reader relies on to read a
    row window without scanning the file.
    """
    rng = np.random.default_rng(seed + _SEED_OFFSET[table])
    pools = _build_pools(rng)
    writer = pq.ParquetWriter(path, table_schema(table))
    try:
        for start in range(0, counts[table], chunk_rows):
            length = min(chunk_rows, counts[table] - start)
            writer.write_table(_chunk(table, start, length, counts, rng, pools))
    finally:
        writer.close()


def read_window(path: Path, column: str, start: int, n: int) -> list[str]:
    """`n` values of one column from row `start` (wrapping past the end), reading only
    the row groups that overlap the window. Mirrors `_chain_layout.read_window`."""
    pf = pq.ParquetFile(path)
    total = pf.metadata.num_rows
    out: list[str] = []
    for lo, hi in _segments(start % total, min(n, total), total):
        out.extend(_read_segment(pf, column, lo, hi))
    return out


def _segments(start: int, n: int, total: int) -> list[tuple[int, int]]:
    end = start + n
    if end <= total:
        return [(start, end)]
    return [(start, total), (0, end - total)]


def _read_segment(pf: pq.ParquetFile, column: str, lo: int, hi: int) -> list[str]:
    values: list[str] = []
    offset = 0
    for rg in range(pf.metadata.num_row_groups):
        rg_rows = pf.metadata.row_group(rg).num_rows
        if offset + rg_rows > lo and offset < hi:
            col = pf.read_row_group(rg, columns=[column]).column(0).to_pylist()
            values.extend(col[max(lo - offset, 0) : hi - offset])
        offset += rg_rows
        if offset >= hi:
            break
    return values
