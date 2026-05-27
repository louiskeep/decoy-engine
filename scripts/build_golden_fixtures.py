"""Regenerate the engine-v2 S1 golden fixture suite.

Eight fixtures under `tests/fixtures/golden/` exercise distinct
relational patterns the V2 sprints will gate against. Each fixture
ships:

- One or more CSV files (small; 50-1000 rows each).
- A `manifest.yaml` describing tables, relationships, expected
  invariants, and the sprints that gate on it (validated by
  `_manifest_schema.py`).

Determinism contract: same engine + same seed = byte-identical bytes.
This script uses `faker.Faker(seed)` with a fixed seed PER FIXTURE so
regenerating any single fixture does not perturb the others, and
running the whole script twice produces identical files.

Usage:

    python scripts/build_golden_fixtures.py
    python scripts/build_golden_fixtures.py --fixture relational_parent_child

The script is the authority for fixture contents; `manifest.yaml` is the
inspector-readable description. Both ship to git; the script is the way
to recover from a fixture drift incident (delete, regenerate, commit).
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path
from typing import Any

import yaml
from faker import Faker

# Each fixture gets its own seed so changes to one cannot perturb the
# others. Adding a fixture: append a new constant + a build_<name>()
# function; do not reuse an existing seed.
SEED_RELATIONAL_PARENT_CHILD = 11
SEED_COMPOSITE_KEY = 12
SEED_NULLABLE_FK = 13
SEED_ORPHAN_FK = 14
SEED_REPEATED_ACROSS_TABLES = 15
SEED_REPEATED_WITHIN_COLUMN = 16
SEED_DIRTY_DATA = 17
SEED_COMPOSITE_COHERENCE = 18

# Repo-relative root for the fixture suite. Computed from this file's
# location so the script works regardless of the caller's working dir.
_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent
GOLDEN_ROOT = REPO_ROOT / "tests" / "fixtures" / "golden"


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    """Write a CSV with header + rows. Newlines normalized to LF for
    cross-platform byte stability."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    """Write manifest.yaml with deterministic key ordering."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manifest.yaml").open("w", encoding="utf-8", newline="") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, default_flow_style=False)


def _fixture_dir(name: str) -> Path:
    return GOLDEN_ROOT / name


# ---------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------


def build_relational_parent_child() -> None:
    """100 customers + 500 orders + 300 invoices + 250 addresses, single-column FKs."""
    fake = Faker()
    fake.seed_instance(SEED_RELATIONAL_PARENT_CHILD)
    rng = random.Random(SEED_RELATIONAL_PARENT_CHILD)
    out = _fixture_dir("relational_parent_child")

    customer_ids = [f"C{i:04d}" for i in range(100)]
    customers = [[cid, fake.name(), fake.email()] for cid in customer_ids]
    _write_csv(out / "customers.csv", ["customer_id", "name", "email"], customers)

    orders = []
    for i in range(500):
        cid = rng.choice(customer_ids)
        orders.append(
            [f"O{i:05d}", cid, fake.date_this_decade().isoformat(), round(rng.uniform(10, 5000), 2)]
        )
    _write_csv(out / "orders.csv", ["order_id", "customer_id", "order_date", "amount"], orders)

    invoices = []
    for i in range(300):
        cid = rng.choice(customer_ids)
        invoices.append(
            [
                f"I{i:05d}",
                cid,
                fake.date_this_decade().isoformat(),
                round(rng.uniform(50, 10000), 2),
            ]
        )
    _write_csv(out / "invoices.csv", ["invoice_id", "customer_id", "issued_at", "amount"], invoices)

    addresses = []
    for i in range(250):
        cid = rng.choice(customer_ids)
        addresses.append(
            [
                f"A{i:05d}",
                cid,
                fake.street_address(),
                fake.city(),
                fake.state_abbr(),
                fake.postcode(),
            ]
        )
    _write_csv(
        out / "addresses.csv",
        ["address_id", "customer_id", "street", "city", "state", "zip"],
        addresses,
    )

    _write_manifest(
        out,
        {
            "fixture_name": "relational_parent_child",
            "description": "Parent customers table with three FK-related child tables (orders, invoices, addresses); single-column FK.",
            "files": [
                {"table": "customers", "path": "customers.csv", "format": "csv"},
                {"table": "orders", "path": "orders.csv", "format": "csv"},
                {"table": "invoices", "path": "invoices.csv", "format": "csv"},
                {"table": "addresses", "path": "addresses.csv", "format": "csv"},
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [
                        {"table": "orders", "columns": ["customer_id"]},
                        {"table": "invoices", "columns": ["customer_id"]},
                        {"table": "addresses", "columns": ["customer_id"]},
                    ],
                    "orphan_policy": "fail",
                    "namespace": "customer_identity",
                }
            ],
            "invariants_post_mask": [
                "every unique customer_id in customers maps to exactly one masked value",
                "every order/invoice/address customer_id is in the masked customer_id set",
                "child row counts unchanged",
                "FK join produces same row count before + after mask",
            ],
            "expected_orphans": 0,
            "gating_sprints": ["S2", "S3", "S9", "S10", "S12", "S13"],
        },
    )


def build_composite_key() -> None:
    """200 enrollments + 1000 claims, composite FK (member_id, plan_id, effective_date)."""
    fake = Faker()
    fake.seed_instance(SEED_COMPOSITE_KEY)
    rng = random.Random(SEED_COMPOSITE_KEY)
    out = _fixture_dir("composite_key")

    # Composite PK on enrollments. Generate 200 unique triples.
    seen: set[tuple[str, str, str]] = set()
    enrollments: list[list[Any]] = []
    while len(enrollments) < 200:
        member = f"M{rng.randint(1000, 9999):04d}"
        plan = f"P{rng.randint(1, 50):03d}"
        eff = fake.date_between(start_date="-3y", end_date="today").isoformat()
        key = (member, plan, eff)
        if key in seen:
            continue
        seen.add(key)
        enrollments.append([member, plan, eff, fake.date_this_decade().isoformat()])
    _write_csv(
        out / "enrollments.csv",
        ["member_id", "plan_id", "effective_date", "termination_date"],
        enrollments,
    )

    enrollment_keys = [(e[0], e[1], e[2]) for e in enrollments]
    claims = []
    for i in range(1000):
        m, p, eff = rng.choice(enrollment_keys)
        claims.append(
            [
                f"CL{i:06d}",
                m,
                p,
                eff,
                round(rng.uniform(10, 50000), 2),
                fake.bothify(text="ICD?###"),
            ]
        )
    _write_csv(
        out / "claims.csv",
        ["claim_id", "member_id", "plan_id", "effective_date", "claim_amount", "diagnosis_code"],
        claims,
    )

    _write_manifest(
        out,
        {
            "fixture_name": "composite_key",
            "description": "Healthcare-flavored composite PK (member_id, plan_id, effective_date) on enrollments; claims FK to the whole tuple.",
            "files": [
                {"table": "enrollments", "path": "enrollments.csv", "format": "csv"},
                {"table": "claims", "path": "claims.csv", "format": "csv"},
            ],
            "relationships": [
                {
                    "parent": {
                        "table": "enrollments",
                        "columns": ["member_id", "plan_id", "effective_date"],
                    },
                    "children": [
                        {
                            "table": "claims",
                            "columns": ["member_id", "plan_id", "effective_date"],
                        }
                    ],
                    "orphan_policy": "fail",
                    "namespace": "enrollment_identity",
                }
            ],
            "invariants_post_mask": [
                "every claim's (member_id, plan_id, effective_date) tuple appears in enrollments after mask",
                "composite tuple masks as one unit (no row mixing different member_id between enrollments and claims)",
            ],
            "expected_orphans": 0,
            "gating_sprints": ["S2", "S8", "S9", "S10"],
        },
    )


def build_nullable_fk() -> None:
    """50 employees + 200 reviews; some reviews have null reviewer_id (self-reviews)."""
    fake = Faker()
    fake.seed_instance(SEED_NULLABLE_FK)
    rng = random.Random(SEED_NULLABLE_FK)
    out = _fixture_dir("nullable_fk")

    emp_ids = [f"E{i:04d}" for i in range(50)]
    employees = [[eid, fake.name(), fake.job()] for eid in emp_ids]
    _write_csv(out / "employees.csv", ["employee_id", "name", "title"], employees)

    reviews = []
    for i in range(200):
        target = rng.choice(emp_ids)
        # 20% of reviews are self-reviews with null reviewer_id.
        reviewer: str = "" if rng.random() < 0.20 else rng.choice(emp_ids)
        reviews.append(
            [
                f"R{i:05d}",
                target,
                reviewer,
                fake.date_this_year().isoformat(),
                rng.randint(1, 5),
            ]
        )
    _write_csv(
        out / "reviews.csv",
        ["review_id", "employee_id", "reviewer_id", "reviewed_at", "rating"],
        reviews,
    )

    _write_manifest(
        out,
        {
            "fixture_name": "nullable_fk",
            "description": "Reviews.reviewer_id is a nullable FK to employees.employee_id; ~20% nulls represent self-reviews.",
            "files": [
                {"table": "employees", "path": "employees.csv", "format": "csv"},
                {"table": "reviews", "path": "reviews.csv", "format": "csv"},
            ],
            "relationships": [
                {
                    "parent": {"table": "employees", "columns": ["employee_id"]},
                    "children": [{"table": "reviews", "columns": ["employee_id"]}],
                    "orphan_policy": "fail",
                    "namespace": "employee_identity",
                },
                {
                    "parent": {"table": "employees", "columns": ["employee_id"]},
                    "children": [{"table": "reviews", "columns": ["reviewer_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "employee_identity",
                },
            ],
            "invariants_post_mask": [
                "non-null reviewer_id values resolve to a masked employee",
                "null reviewer_id values remain null after mask (no synthetic reviewer invented)",
            ],
            "expected_orphans": 0,
            "gating_sprints": ["S2", "S9"],
        },
    )


def build_orphan_fk() -> None:
    """50 customers + 100 orders; ~10 orders FK to a deleted customer_id."""
    fake = Faker()
    fake.seed_instance(SEED_ORPHAN_FK)
    rng = random.Random(SEED_ORPHAN_FK)
    out = _fixture_dir("orphan_fk")

    customer_ids = [f"C{i:04d}" for i in range(50)]
    customers = [[cid, fake.name(), fake.email()] for cid in customer_ids]
    _write_csv(out / "customers.csv", ["customer_id", "name", "email"], customers)

    deleted_ids = [f"D{i:04d}" for i in range(10)]  # 10 IDs that no longer exist in customers
    all_for_orders = customer_ids + deleted_ids
    orders = []
    for i in range(100):
        cid = rng.choice(all_for_orders)
        orders.append(
            [f"O{i:05d}", cid, fake.date_this_decade().isoformat(), round(rng.uniform(10, 5000), 2)]
        )
    _write_csv(out / "orders.csv", ["order_id", "customer_id", "order_date", "amount"], orders)

    _write_manifest(
        out,
        {
            "fixture_name": "orphan_fk",
            "description": "Orders.customer_id contains ~10 IDs that no longer exist in customers; exercises orphan_fk_policy_completeness check (S2 row 6).",
            "files": [
                {"table": "customers", "path": "customers.csv", "format": "csv"},
                {"table": "orders", "path": "orders.csv", "format": "csv"},
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "warn",
                    "namespace": "customer_identity",
                }
            ],
            "invariants_post_mask": [
                "non-orphan orders.customer_id values resolve to a masked customer",
                "orphan_policy applies to the ~10 orphan rows per the declared policy",
            ],
            "expected_orphans": 10,
            "gating_sprints": ["S2", "S9"],
        },
    )


def build_repeated_across_tables() -> None:
    """100 primary_emails + 100 login_emails; email values overlap, separate namespaces."""
    fake = Faker()
    fake.seed_instance(SEED_REPEATED_ACROSS_TABLES)
    rng = random.Random(SEED_REPEATED_ACROSS_TABLES)
    out = _fixture_dir("repeated_across_tables")

    primary_emails = []
    email_pool = [fake.email() for _ in range(100)]
    for i in range(100):
        primary_emails.append([f"PE{i:04d}", email_pool[i], "primary"])
    _write_csv(
        out / "primary_emails.csv",
        ["primary_id", "email", "kind"],
        primary_emails,
    )

    # login_emails overlaps with primary_emails by ~50% of values to exercise
    # namespace separation (same email string -> different mask if different namespace).
    login_emails = []
    for i in range(100):
        if rng.random() < 0.5:
            email = email_pool[rng.randint(0, 99)]  # reused from primary
        else:
            email = fake.email()
        login_emails.append([f"LE{i:04d}", email, fake.date_this_year().isoformat()])
    _write_csv(
        out / "login_emails.csv",
        ["login_id", "email", "last_login"],
        login_emails,
    )

    _write_manifest(
        out,
        {
            "fixture_name": "repeated_across_tables",
            "description": "primary_emails.email and login_emails.email share ~50% values; exercises namespace separation (same vs different namespaces).",
            "files": [
                {"table": "primary_emails", "path": "primary_emails.csv", "format": "csv"},
                {"table": "login_emails", "path": "login_emails.csv", "format": "csv"},
            ],
            "relationships": [],
            "invariants_post_mask": [
                "with separate namespaces: primary.email and login.email mask independently (same source value -> different masked values)",
                "with same namespace: primary.email and login.email mask consistently (same source value -> same masked value)",
            ],
            "expected_orphans": 0,
            "gating_sprints": ["S2", "S3", "S6", "S7"],
        },
    )


def build_repeated_within_column() -> None:
    """200 rows with intentional duplication; some values appear 5-10 times."""
    fake = Faker()
    fake.seed_instance(SEED_REPEATED_WITHIN_COLUMN)
    rng = random.Random(SEED_REPEATED_WITHIN_COLUMN)
    out = _fixture_dir("repeated_within_column")

    # Generate ~40 unique values; some used heavily, some used once.
    unique_values = [fake.company() for _ in range(40)]
    rows = []
    for i in range(200):
        # Skew toward low-index unique_values so first ~10 get used 5-10x.
        idx = min(int(rng.expovariate(0.15)), 39)
        rows.append([f"X{i:05d}", unique_values[idx]])
    _write_csv(out / "duplicates.csv", ["row_id", "company_name"], rows)

    _write_manifest(
        out,
        {
            "fixture_name": "repeated_within_column",
            "description": "Single column with intentional within-column repetition; exercises unique vs reuse cardinality divergence.",
            "files": [
                {"table": "duplicates", "path": "duplicates.csv", "format": "csv"},
            ],
            "relationships": [],
            "invariants_post_mask": [
                "with cardinality_mode=reuse: distinct masked values equals distinct source values",
                "with cardinality_mode=unique: every row gets a unique masked value (pool must support 200 unique values)",
            ],
            "expected_orphans": 0,
            "gating_sprints": ["S5", "S6"],
        },
    )


def build_dirty_data() -> None:
    """100 messy rows: mixed casing, whitespace, pre-redacted placeholders, unicode, empty vs null."""
    fake = Faker()
    fake.seed_instance(SEED_DIRTY_DATA)
    rng = random.Random(SEED_DIRTY_DATA)
    out = _fixture_dir("dirty_data")

    rows = []
    for i in range(100):
        # Cycle through dirt patterns so each kind appears at least 10x.
        kind = i % 10
        if kind == 0:
            name = fake.name().upper()  # all uppercase
        elif kind == 1:
            name = fake.name().lower()  # all lowercase
        elif kind == 2:
            name = f"  {fake.name()}  "  # leading + trailing whitespace
        elif kind == 3:
            name = ""  # empty string (not null)
        elif kind == 4:
            name = "XXX-XX-XXXX"  # already-redacted placeholder
        elif kind == 5:
            name = "José"  # decomposed unicode (e vs e + acute)
        elif kind == 6:
            name = f"{fake.name()}\t\n"  # embedded control chars
        elif kind == 7:
            name = "NULL"  # literal "NULL" string
        elif kind == 8:
            name = fake.name() + " " + fake.name()  # double-spaced concat
        else:
            name = fake.name()  # clean baseline

        # ssn column: some valid, some pre-redacted, some null
        if rng.random() < 0.2:
            ssn = "XXX-XX-XXXX"
        elif rng.random() < 0.1:
            ssn = ""
        else:
            ssn = fake.ssn()

        rows.append([f"D{i:04d}", name, ssn])

    _write_csv(out / "messy.csv", ["row_id", "name", "ssn"], rows)

    _write_manifest(
        out,
        {
            "fixture_name": "dirty_data",
            "description": "Single-table fixture with intentional data quality issues: mixed casing, whitespace, pre-redacted placeholders, unicode normalization, empty vs null. Exercises validation + sentinel detection.",
            "files": [
                {"table": "messy", "path": "messy.csv", "format": "csv"},
            ],
            "relationships": [],
            "invariants_post_mask": [
                "STORM sentinel detection flags pre-redacted XXX-XX-XXXX placeholders before masking",
                "empty strings and NULL literals are distinguishable in profiling",
                "unicode normalization does not change content during mask (NFC stays NFC)",
            ],
            "expected_orphans": 0,
            "gating_sprints": ["S10", "S11", "S12"],
        },
    )


def build_composite_coherence() -> None:
    """100 people (first_name, last_name, email matched) + 100 locations (city, state, zip matched US triples)."""
    fake = Faker()
    fake.seed_instance(SEED_COMPOSITE_COHERENCE)
    out = _fixture_dir("composite_coherence")

    people = []
    for i in range(100):
        first = fake.first_name()
        last = fake.last_name()
        # email coherent with first/last
        email = f"{first.lower()}.{last.lower()}@{fake.domain_name()}"
        people.append([f"P{i:04d}", first, last, email])
    _write_csv(
        out / "people.csv",
        ["person_id", "first_name", "last_name", "email"],
        people,
    )

    # Faker's city/state/postcode generate matching US triples per row.
    locations = []
    for i in range(100):
        locations.append([f"L{i:04d}", fake.city(), fake.state_abbr(), fake.postcode()])
    _write_csv(
        out / "locations.csv",
        ["location_id", "city", "state", "zip"],
        locations,
    )

    _write_manifest(
        out,
        {
            "fixture_name": "composite_coherence",
            "description": "Composite coherence: first_name/last_name/email match per row; city/state/zip US triples match per row. Exercises S8 composite generators.",
            "files": [
                {"table": "people", "path": "people.csv", "format": "csv"},
                {"table": "locations", "path": "locations.csv", "format": "csv"},
            ],
            "relationships": [],
            "invariants_post_mask": [
                "masked first_name + last_name + email remain coherent (email derives from masked first.last)",
                "masked city + state + zip remain a valid US triple",
            ],
            "expected_orphans": 0,
            "gating_sprints": ["S8"],
        },
    )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


_BUILDERS = {
    "relational_parent_child": build_relational_parent_child,
    "composite_key": build_composite_key,
    "nullable_fk": build_nullable_fk,
    "orphan_fk": build_orphan_fk,
    "repeated_across_tables": build_repeated_across_tables,
    "repeated_within_column": build_repeated_within_column,
    "dirty_data": build_dirty_data,
    "composite_coherence": build_composite_coherence,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--fixture",
        choices=sorted(_BUILDERS.keys()),
        action="append",
        help="Build only the named fixture(s). May be passed multiple times. Default: build all.",
    )
    args = parser.parse_args(argv)

    names = args.fixture if args.fixture else sorted(_BUILDERS.keys())
    for name in names:
        print(f"Building {name} ...", file=sys.stderr)
        _BUILDERS[name]()
    print(
        f"Done. {len(names)} fixture(s) under {GOLDEN_ROOT.relative_to(REPO_ROOT)}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
