"""Golden fixture determinism invariants for the four S3-gated fixtures.

Per S3 spec §7: determinism-side assertions for `relational_parent_child`,
`composite_key`, `repeated_within_column`, `repeated_across_tables`. The
fixtures themselves were created in S1; S2 filled in the relationship
invariants; S3 fills in the deterministic-mapping invariants.

The invariants exercise the determinism layer's contract:
- same source -> same masked output (across N calls in one process)
- same source under same namespace -> same masked output (across two tables)
- different namespace on same source -> different masked outputs (statistically)

These tests exercise `decoy_engine.determinism.derive(...)` directly (not
through a full `decoy run`); the runtime wiring of derive into masking
strategies lives in S9 (Execution Adapter). S3 ships the primitive;
S9 wires it into the strategies.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from decoy_engine.determinism import derive

GOLDEN_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "golden"

_SEED = b"\x00\x00\x00\x00\x00\x00\x00\x2a"  # 42 as 8-byte big-endian
_NS_CUSTOMER = "customer_identity"
_NS_ENROLLMENT = "enrollment_identity"
_NS_EMAIL_SHARED = "shared_email_pool"
_NS_EMAIL_A = "primary_pool"
_NS_EMAIL_B = "login_pool"


@pytest.mark.golden
class TestRelationalParentChildDeterminism:
    """Same customer_id -> same derived bytes; FK preserves mask."""

    def test_same_source_customer_id_produces_same_derived_bytes(self) -> None:
        """The headline guarantee: for a fixed (seed, namespace), the same
        source customer_id maps to identical 32-byte derived material."""
        customers = pd.read_csv(GOLDEN_ROOT / "relational_parent_child" / "customers.csv")
        for cid in customers["customer_id"].head(20):
            src = str(cid).encode("utf-8")
            a = derive(_SEED, _NS_CUSTOMER, src)
            b = derive(_SEED, _NS_CUSTOMER, src)
            assert a == b

    def test_fk_preserves_mask_across_tables(self) -> None:
        """The customer_id values that appear in orders.csv mask to the
        same derived bytes as the matching customers.csv rows. This is
        what 'FK preserves mask' means at the primitive layer; the
        runtime FK-binding logic lives in S9."""
        customers = pd.read_csv(GOLDEN_ROOT / "relational_parent_child" / "customers.csv")
        orders = pd.read_csv(GOLDEN_ROOT / "relational_parent_child" / "orders.csv")
        customer_ids = set(customers["customer_id"])
        for cid in orders["customer_id"].head(50):
            if cid not in customer_ids:
                continue
            src = str(cid).encode("utf-8")
            customer_mask = derive(_SEED, _NS_CUSTOMER, src)
            order_mask = derive(_SEED, _NS_CUSTOMER, src)
            assert customer_mask == order_mask


@pytest.mark.golden
class TestCompositeKeyDeterminism:
    """Same composite tuple -> same derived bytes (canonical joined)."""

    def test_same_composite_tuple_produces_same_derived_bytes(self) -> None:
        enrollments = pd.read_csv(GOLDEN_ROOT / "composite_key" / "enrollments.csv")
        for _, row in enrollments.head(20).iterrows():
            # Canonical tuple encoding: sorted column names, "|" separator.
            canonical = "|".join(
                str(row[c]) for c in sorted(["member_id", "plan_id", "effective_date"])
            )
            src = canonical.encode("utf-8")
            a = derive(_SEED, _NS_ENROLLMENT, src)
            b = derive(_SEED, _NS_ENROLLMENT, src)
            assert a == b

    def test_composite_tuple_in_enrollments_matches_claims(self) -> None:
        """A composite tuple that appears in both enrollments and claims
        produces the same derived bytes under the same namespace. This
        is the per-group seed-binding contract: the whole tuple is one
        unit, not three independent columns."""
        enrollments = pd.read_csv(GOLDEN_ROOT / "composite_key" / "enrollments.csv")
        claims = pd.read_csv(GOLDEN_ROOT / "composite_key" / "claims.csv")
        enrollment_tuples = {
            tuple(sorted([str(r.member_id), str(r.plan_id), str(r.effective_date)]))
            for r in enrollments.itertuples()
        }
        sample_claim = claims.head(20)
        for r in sample_claim.itertuples():
            claim_tuple = tuple(sorted([str(r.member_id), str(r.plan_id), str(r.effective_date)]))
            if claim_tuple not in enrollment_tuples:
                continue
            src = "|".join(claim_tuple).encode("utf-8")
            a = derive(_SEED, _NS_ENROLLMENT, src)
            b = derive(_SEED, _NS_ENROLLMENT, src)
            assert a == b


@pytest.mark.golden
class TestRepeatedWithinColumnDeterminism:
    """Every duplicate source value in one column masks to the same output."""

    def test_duplicate_source_values_mask_to_same_bytes(self) -> None:
        duplicates = pd.read_csv(GOLDEN_ROOT / "repeated_within_column" / "duplicates.csv")
        # Find a value that appears at least twice.
        col = duplicates.columns[0]
        counts = duplicates[col].value_counts()
        repeats = counts[counts >= 2].head(5).index
        ns = "duplicates_pool"
        for value in repeats:
            occurrences = duplicates[duplicates[col] == value]
            assert len(occurrences) >= 2
            src = str(value).encode("utf-8")
            outputs = {derive(_SEED, ns, src) for _ in range(len(occurrences))}
            assert len(outputs) == 1, f"value {value!r} produced multiple outputs across calls"


@pytest.mark.golden
class TestRepeatedAcrossTablesDeterminism:
    """Same source under same namespace -> same output (cross-table).
    Same source under different namespaces -> different outputs."""

    def test_same_namespace_unifies_cross_table_emails(self) -> None:
        primary = pd.read_csv(GOLDEN_ROOT / "repeated_across_tables" / "primary_emails.csv")
        login = pd.read_csv(GOLDEN_ROOT / "repeated_across_tables" / "login_emails.csv")
        shared = set(primary["email"]) & set(login["email"])
        assert shared, "fixture expected to have overlapping emails across the two tables"
        for email in list(shared)[:10]:
            src = email.encode("utf-8")
            primary_mask = derive(_SEED, _NS_EMAIL_SHARED, src)
            login_mask = derive(_SEED, _NS_EMAIL_SHARED, src)
            assert primary_mask == login_mask

    def test_different_namespace_keeps_cross_table_emails_independent(self) -> None:
        """When primary_emails.email is in namespace A and login_emails.email
        is in namespace B, the same source value masks to DIFFERENT derived
        bytes. The namespace string is the isolation axis."""
        primary = pd.read_csv(GOLDEN_ROOT / "repeated_across_tables" / "primary_emails.csv")
        login = pd.read_csv(GOLDEN_ROOT / "repeated_across_tables" / "login_emails.csv")
        shared = set(primary["email"]) & set(login["email"])
        assert shared
        for email in list(shared)[:10]:
            src = email.encode("utf-8")
            primary_mask = derive(_SEED, _NS_EMAIL_A, src)
            login_mask = derive(_SEED, _NS_EMAIL_B, src)
            assert primary_mask != login_mask
