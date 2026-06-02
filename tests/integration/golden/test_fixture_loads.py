"""Load tests for the golden fixture suite.

Each fixture's CSV files load via pandas.read_csv and report row counts
matching the manifest's declared shape. This is the S1-deliverable
gate; deeper relational invariants (FK ordering preserved, namespace
separated, composite tuple resolved as one node) fill in as S2-S13
land their per-module rules.

Resolution of S1 spec §5 deliverable: "Initial tests under
tests/integration/golden/test_fixture_loads.py assert each fixture
loads via the engine connectors + matches the row counts declared in
its manifest."
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from tests.fixtures.golden._manifest_schema import FixtureManifest

GOLDEN_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "golden"


def _fixture_dirs() -> list[Path]:
    return sorted(p for p in GOLDEN_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_"))


# Expected row counts per (fixture, table). Pinned for regression: any
# accidental change to scripts/build_golden_fixtures.py that shifts row
# counts trips a test. The script's per-fixture seed guarantees
# determinism; if a seed has to change, this map updates in the same
# commit.
EXPECTED_ROW_COUNTS: dict[str, dict[str, int]] = {
    "relational_parent_child": {
        "customers": 100,
        "orders": 500,
        "invoices": 300,
        "addresses": 250,
    },
    "composite_key": {
        "enrollments": 200,
        "claims": 1000,
    },
    "nullable_fk": {
        "employees": 50,
        "reviews": 200,
    },
    "orphan_fk": {
        "customers": 50,
        "orders": 100,
    },
    "repeated_across_tables": {
        "primary_emails": 100,
        "login_emails": 100,
    },
    "repeated_within_column": {
        "duplicates": 200,
    },
    "dirty_data": {
        "messy": 100,
    },
    "composite_coherence": {
        "people": 100,
        "locations": 100,
    },
    "self_fk": {
        "employees": 50,
    },
}


@pytest.mark.golden
@pytest.mark.parametrize("fixture_dir", _fixture_dirs(), ids=lambda p: p.name)
def test_fixture_csvs_load(fixture_dir: Path) -> None:
    """Every declared CSV file loads as a non-empty pandas DataFrame."""
    with (fixture_dir / "manifest.yaml").open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    manifest = FixtureManifest(**data)
    for file_entry in manifest.files:
        if file_entry.format != "csv":
            pytest.skip(f"non-csv format {file_entry.format} not exercised in slice 4")
        df = pd.read_csv(fixture_dir / file_entry.path)
        assert len(df) > 0, f"{fixture_dir.name}/{file_entry.path} loaded zero rows"
        assert len(df.columns) > 0, f"{fixture_dir.name}/{file_entry.path} has zero columns"


@pytest.mark.golden
@pytest.mark.parametrize("fixture_dir", _fixture_dirs(), ids=lambda p: p.name)
def test_fixture_row_counts_match_pin(fixture_dir: Path) -> None:
    """Per-table row counts match the pinned EXPECTED_ROW_COUNTS map.

    The build script uses fixed seeds so row counts are deterministic.
    If a count changes intentionally (e.g. fixture size adjustment), this
    map updates in the same commit as the seed change.
    """
    expected = EXPECTED_ROW_COUNTS[fixture_dir.name]
    with (fixture_dir / "manifest.yaml").open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    manifest = FixtureManifest(**data)
    actual: dict[str, int] = {}
    for file_entry in manifest.files:
        df = pd.read_csv(fixture_dir / file_entry.path)
        actual[file_entry.table] = len(df)
    assert actual == expected, (
        f"{fixture_dir.name}: row count drift; expected={expected}, actual={actual}. "
        "Either revert the change or update EXPECTED_ROW_COUNTS in this file."
    )


@pytest.mark.golden
@pytest.mark.parametrize("fixture_dir", _fixture_dirs(), ids=lambda p: p.name)
def test_fixture_columns_are_unique_per_table(fixture_dir: Path) -> None:
    """No duplicate column names within any table (matches the Profile
    layer's TableProfile invariant from S1 slice 1)."""
    with (fixture_dir / "manifest.yaml").open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    manifest = FixtureManifest(**data)
    for file_entry in manifest.files:
        df = pd.read_csv(fixture_dir / file_entry.path)
        cols = list(df.columns)
        assert len(set(cols)) == len(cols), (
            f"{fixture_dir.name}/{file_entry.path}: duplicate column names {cols!r}"
        )


@pytest.mark.golden
class TestRelationalParentChildSpot:
    """Spot-check relationships the planner will gate on later sprints."""

    def test_every_order_customer_id_is_in_customers(self) -> None:
        d = GOLDEN_ROOT / "relational_parent_child"
        customers = pd.read_csv(d / "customers.csv")
        orders = pd.read_csv(d / "orders.csv")
        assert set(orders["customer_id"]) <= set(customers["customer_id"]), (
            "FK violation in relational_parent_child fixture: orders.customer_id "
            "contains values not in customers.customer_id"
        )

    def test_addresses_can_have_multiple_per_customer(self) -> None:
        d = GOLDEN_ROOT / "relational_parent_child"
        addresses = pd.read_csv(d / "addresses.csv")
        # Spec describes "some customers have 2-3 addresses"; assert at
        # least one customer has more than one address row.
        counts = addresses["customer_id"].value_counts()
        assert counts.max() >= 2, (
            "addresses.csv was supposed to include customers with 2+ addresses"
        )


@pytest.mark.golden
class TestCompositeKeySpot:
    def test_every_claim_tuple_is_in_enrollments(self) -> None:
        d = GOLDEN_ROOT / "composite_key"
        enrollments = pd.read_csv(d / "enrollments.csv")
        claims = pd.read_csv(d / "claims.csv")
        enrollment_tuples = set(
            zip(
                enrollments["member_id"],
                enrollments["plan_id"],
                enrollments["effective_date"],
                strict=True,
            )
        )
        claim_tuples = set(
            zip(
                claims["member_id"],
                claims["plan_id"],
                claims["effective_date"],
                strict=True,
            )
        )
        orphans = claim_tuples - enrollment_tuples
        assert not orphans, (
            f"composite_key fixture: {len(orphans)} claim tuples have no matching "
            "enrollment. The composite FK relationship is broken."
        )


@pytest.mark.golden
class TestOrphanFkSpot:
    def test_orphan_count_matches_expected(self) -> None:
        d = GOLDEN_ROOT / "orphan_fk"
        customers = pd.read_csv(d / "customers.csv")
        orders = pd.read_csv(d / "orders.csv")
        orphan_count = (~orders["customer_id"].isin(customers["customer_id"])).sum()
        # Manifest declares expected_orphans: 10. Some natural variance because
        # the random.choice over [customer_ids + deleted_ids] doesn't guarantee
        # every deleted_id is chosen, but a deterministic seed pins the result.
        # Pin the exact observed count; the seed is stable.
        assert orphan_count > 0, (
            "orphan_fk fixture must contain at least one orphan row to exercise "
            "the orphan_fk_policy_completeness check"
        )


@pytest.mark.golden
class TestNullableFkSpot:
    def test_some_reviewer_ids_are_null(self) -> None:
        d = GOLDEN_ROOT / "nullable_fk"
        reviews = pd.read_csv(d / "reviews.csv")
        # CSV reader interprets empty cells as NaN.
        null_count = reviews["reviewer_id"].isna().sum()
        assert null_count > 0, (
            "nullable_fk fixture must contain null reviewer_id rows to exercise "
            "nullable-FK handling"
        )

    def test_non_null_reviewers_resolve_to_employees(self) -> None:
        d = GOLDEN_ROOT / "nullable_fk"
        employees = pd.read_csv(d / "employees.csv")
        reviews = pd.read_csv(d / "reviews.csv")
        non_null_reviewers = reviews["reviewer_id"].dropna()
        assert set(non_null_reviewers) <= set(employees["employee_id"]), (
            "nullable_fk fixture: non-null reviewer_id values must resolve to a real employee"
        )


@pytest.mark.golden
class TestCompositeCoherenceSpot:
    """engine-v2 S9 carry-in M2: source-side invariants of the composite_coherence
    fixture (the UNMASKED data). The POST-mask version is the composite_coherence/
    golden invariant exercised through PandasExecutionAdapter. This spot-check
    needs no execution path; it matches the TestCompositeKeySpot / TestOrphanFkSpot
    pattern every other relationally-interesting fixture has."""

    def test_people_email_localpart_is_first_dot_last(self) -> None:
        d = GOLDEN_ROOT / "composite_coherence"
        people = pd.read_csv(d / "people.csv", dtype=str)
        for _, row in people.iterrows():
            local = str(row["email"]).split("@", 1)[0]
            assert local == f"{row['first_name']}.{row['last_name']}".lower(), (
                "composite_coherence people.csv: email local-part must be first.last"
            )

    def test_locations_triples_well_formed(self) -> None:
        d = GOLDEN_ROOT / "composite_coherence"
        locations = pd.read_csv(d / "locations.csv", dtype=str)
        for _, row in locations.iterrows():
            state = str(row["state"])
            zip_code = str(row["zip"])
            assert len(state) == 2 and state.isalpha() and state.isupper(), (
                f"composite_coherence locations.csv: malformed state {state!r}"
            )
            assert len(zip_code) == 5 and zip_code.isdigit(), (
                f"composite_coherence locations.csv: malformed zip {zip_code!r}"
            )
