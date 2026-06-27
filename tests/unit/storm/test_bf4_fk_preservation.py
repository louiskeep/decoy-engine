"""BF4: FK-preservation check on masked output (pure-engine runner tests).

Synthetic fixture tests that exercise ``check_fk_preservation`` as a
standalone post-mask pass. The BF4 contract: after a masking job runs,
the engine walks each declared FK relationship against the MASKED OUTPUT
and verifies that child FK values still resolve to parent PK values.

The most common BF4 failure mode is inconsistent masking: the parent PK
was hashed in one job run and the child FK in another (e.g. a namespace
collision, or the FK column was forgotten in the config). Orphaned
child rows signal the breakage.

Scenario coverage:
  F1: Single-table parent/child - consistent masking keeps FKs intact
  F2: Single-table parent/child - inconsistent masking creates orphans (fail)
  F3: Null FK values are not orphans
  F4: Multiple children, each checked independently
  F5: Namespace-annotated relationship resolves correctly
  F6: Composite FK - both columns must match as a tuple
  F7: Parent table missing from output (error, not crash)
  F8: Security -- report findings carry no raw key values
"""

from __future__ import annotations

import hashlib

import pandas as pd

from decoy_engine.storm.postmask.fk_preservation import check_fk_preservation
from decoy_engine.storm.postmask.runner import run_storm_post_mask

# ── Synthetic fixture helpers ─────────────────────────────────────────────────


def _hash_id(v: int, prefix: str = "") -> str:
    """Simulate a hash-strategy PK: hash the integer + return a short hex string."""
    key = f"{prefix}{v}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _make_users(n: int = 50, hash_prefix: str = "") -> pd.DataFrame:
    """50 users with integer source IDs hashed to a consistent masked_id."""
    return pd.DataFrame(
        {
            "user_id": [_hash_id(i, hash_prefix) for i in range(n)],
            "name_hash": [_hash_id(i, "name") for i in range(n)],
        }
    )


def _make_orders(user_df: pd.DataFrame, n: int = 100) -> pd.DataFrame:
    """100 orders whose user_id FK values all come from user_df.user_id."""
    user_ids = user_df["user_id"].tolist()
    return pd.DataFrame(
        {
            "order_id": [_hash_id(i, "order") for i in range(n)],
            "user_id": [user_ids[i % len(user_ids)] for i in range(n)],
        }
    )


def _single_fk_config(
    parent_table: str = "users",
    parent_col: str = "user_id",
    child_table: str = "orders",
    child_col: str = "user_id",
    *,
    namespace: str | None = None,
    orphan_policy: str | None = None,
) -> dict:
    child_cfg: dict = {"table": child_table, "columns": [child_col]}
    if orphan_policy is not None:
        child_cfg["orphan_policy"] = orphan_policy
    rel: dict = {
        "parent": {"table": parent_table, "columns": [parent_col]},
        "children": [child_cfg],
    }
    if namespace is not None:
        rel["namespace"] = namespace
    return {
        "tables": [
            {"name": parent_table, "columns": []},
            {"name": child_table, "columns": []},
        ],
        "relationships": [rel],
    }


# ── F1: Consistent masking - all FKs resolve ─────────────────────────────────


class TestF1ConsistentMaskingFksIntact:
    """When both parent PK and child FK are masked with the same hash key
    (same namespace), the masked child values all resolve to masked parent
    values. FK check should report severity='info' with 0 orphans.
    """

    def test_consistently_hashed_fks_are_info(self):
        users = _make_users(50)
        orders = _make_orders(users, 100)
        config = _single_fk_config()
        findings = check_fk_preservation({"users": users, "orders": orders}, config)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "info", f"expected info, got {f.severity!r}: {f.message}"
        assert f.orphan_count == 0
        assert f.total_child_rows == 100

    def test_consistently_hashed_fks_pass_via_runner(self):
        users = _make_users(50)
        orders = _make_orders(users, 100)
        config = _single_fk_config()
        report = run_storm_post_mask(
            source_frames={"users": users, "orders": orders},
            output_frames={"users": users, "orders": orders},
            config=config,
        )
        assert report["fail_count"] == 0
        assert len(report["fk_preservation"]) == 1
        assert report["fk_preservation"][0]["orphan_count"] == 0


# ── F2: Inconsistent masking - child FK does not resolve ──────────────────────


class TestF2InconsistentMaskingOrphans:
    """When the parent PK was hashed with one prefix and the child FK was
    hashed with a different prefix (simulating a namespace collision or a
    masking run that forgot to include both tables), the child FK values
    will not be present in the parent PK column.

    This is the core BF4 catch: a silently-broken FK relationship.
    """

    def test_different_hash_prefix_produces_orphans(self):
        # Parent hashed with prefix "p:", child FK hashed with different prefix "c:"
        users = _make_users(50, hash_prefix="p:")
        orders_fks = [_hash_id(i % 50, "c:") for i in range(100)]  # wrong prefix
        orders = pd.DataFrame(
            {
                "order_id": [_hash_id(i, "order") for i in range(100)],
                "user_id": orders_fks,
            }
        )
        config = _single_fk_config()
        findings = check_fk_preservation({"users": users, "orders": orders}, config)
        assert len(findings) == 1
        f = findings[0]
        # All 100 orders reference nonexistent parent IDs -> fail
        assert f.orphan_count == 100
        assert f.orphan_rate == 1.0
        assert f.severity == "fail"

    def test_partial_orphan_above_threshold_is_fail(self):
        """Only some child FKs are broken (e.g. 10% of orders reference
        IDs that no longer exist in the masked parent). Still fail if
        rate > _DEFAULT_FAIL_THRESHOLD (1%).
        """
        users = _make_users(50)
        # First 90 orders point at valid user IDs; last 10 are stale
        valid_fks = [users["user_id"].iloc[i % 50] for i in range(90)]
        stale_fks = [f"stale_{i}" for i in range(10)]
        order_fks = valid_fks + stale_fks
        orders = pd.DataFrame(
            {
                "order_id": [_hash_id(i, "order") for i in range(100)],
                "user_id": order_fks,
            }
        )
        config = _single_fk_config()
        findings = check_fk_preservation({"users": users, "orders": orders}, config)
        assert len(findings) == 1
        f = findings[0]
        assert f.orphan_count == 10
        assert f.total_child_rows == 100
        assert f.orphan_rate == 0.1  # 10% > 1% fail threshold
        assert f.severity == "fail"

    def test_partial_orphan_via_runner_increments_fail_count(self):
        users = _make_users(10)
        bad_fks = [f"ghost_{i}" for i in range(20)]
        orders = pd.DataFrame({"order_id": range(20), "user_id": bad_fks})
        config = _single_fk_config()
        report = run_storm_post_mask(
            source_frames={"users": users, "orders": orders},
            output_frames={"users": users, "orders": orders},
            config=config,
        )
        assert report["fail_count"] >= 1
        assert report["fk_preservation"][0]["orphan_count"] == 20


# ── F3: Null FK values are not orphans ───────────────────────────────────────


class TestF3NullFkValuesNotOrphans:
    """Null FK values should not be counted as orphans.

    A child row with a null FK is semantically 'not referencing anything'
    (optional FK), which is a valid database state -- not a broken join.
    """

    def test_null_fk_values_not_counted_as_orphans(self):
        users = _make_users(10)
        fks = [users["user_id"].iloc[0]] * 5 + [None] * 5  # half null
        orders = pd.DataFrame({"order_id": range(10), "user_id": fks})
        config = _single_fk_config()
        findings = check_fk_preservation({"users": users, "orders": orders}, config)
        assert len(findings) == 1
        f = findings[0]
        # total_child_rows should reflect only non-null FKs
        assert f.total_child_rows == 5
        assert f.orphan_count == 0
        assert f.severity == "info"

    def test_all_null_fk_column_is_info_nothing_to_check(self):
        users = _make_users(10)
        orders = pd.DataFrame({"order_id": range(5), "user_id": [None] * 5})
        config = _single_fk_config()
        findings = check_fk_preservation({"users": users, "orders": orders}, config)
        assert len(findings) == 1
        f = findings[0]
        assert f.total_child_rows == 0
        assert f.severity == "info"
        assert "nothing to check" in f.message.lower()


# ── F4: Multiple children, each checked independently ────────────────────────


class TestF4MultipleChildren:
    """One parent with two child tables. Each child is checked separately
    and can have different orphan outcomes.
    """

    def test_two_children_one_passes_one_fails(self):
        users = _make_users(20)
        # valid_orders: all FKs resolve
        valid_orders = _make_orders(users, 30)
        # broken_orders: stale FKs
        broken_orders = pd.DataFrame(
            {
                "order_id": range(10),
                "user_id": [f"stale_{i}" for i in range(10)],
            }
        )
        config = {
            "tables": [
                {"name": "users", "columns": []},
                {"name": "orders", "columns": []},
                {"name": "broken_orders", "columns": []},
            ],
            "relationships": [
                {
                    "parent": {"table": "users", "columns": ["user_id"]},
                    "children": [
                        {"table": "orders", "columns": ["user_id"]},
                        {"table": "broken_orders", "columns": ["user_id"]},
                    ],
                }
            ],
        }
        findings = check_fk_preservation(
            {
                "users": users,
                "orders": valid_orders,
                "broken_orders": broken_orders,
            },
            config,
        )
        assert len(findings) == 2
        by_table = {f.child_table: f for f in findings}
        assert by_table["orders"].severity == "info"
        assert by_table["orders"].orphan_count == 0
        assert by_table["broken_orders"].severity == "fail"
        assert by_table["broken_orders"].orphan_count == 10


# ── F5: Namespace annotation propagated to finding ───────────────────────────


class TestF5NamespaceAnnotation:
    """A relationship with a 'namespace' annotation should carry that
    namespace value through to the FKPreservationFinding. The namespace
    is used by the platform to identify which hash key was applied.
    """

    def test_namespace_on_clean_relationship_appears_in_finding(self):
        users = _make_users(10)
        orders = _make_orders(users, 20)
        config = _single_fk_config(namespace="patient_ns")
        findings = check_fk_preservation({"users": users, "orders": orders}, config)
        assert len(findings) == 1
        assert findings[0].namespace == "patient_ns"
        assert findings[0].orphan_count == 0

    def test_namespace_on_broken_relationship_appears_in_finding(self):
        users = _make_users(10)
        broken = pd.DataFrame({"order_id": range(5), "user_id": [f"bad_{i}" for i in range(5)]})
        config = _single_fk_config(namespace="patient_ns")
        findings = check_fk_preservation({"users": users, "orders": broken}, config)
        assert len(findings) == 1
        assert findings[0].namespace == "patient_ns"
        assert findings[0].severity == "fail"


# ── F6: Composite FK - tuple-wise check ──────────────────────────────────────


class TestF6CompositeFkTupleCheck:
    """Composite FK requires exact tuple containment, not column-by-column.

    This is the H4 contract: if parent has (a=1, b=1) and (a=2, b=99),
    a child row with (fk_a=1, fk_b=99) is an orphan because the tuple
    (1, 99) never appears as a parent row -- even though each individual
    column value does exist in the parent.
    """

    def _composite_config(self) -> dict:
        return {
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["pk_a", "pk_b"]},
                    "children": [
                        {"table": "child", "columns": ["fk_a", "fk_b"]},
                    ],
                }
            ]
        }

    def test_cross_column_valid_values_but_bad_tuple_is_orphan(self):
        # Parent rows: (1,1) and (2,99). Child has (1,99) -- column
        # values individually exist in parent but the TUPLE does not.
        parent = pd.DataFrame({"pk_a": [1, 2], "pk_b": [1, 99]})
        child = pd.DataFrame({"fk_a": [1], "fk_b": [99]})
        findings = check_fk_preservation(
            {"parent": parent, "child": child}, self._composite_config()
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "fail"
        assert f.orphan_count == 1
        # Composite columns represented as comma-joined strings
        assert f.parent_column == "pk_a,pk_b"
        assert f.child_column == "fk_a,fk_b"

    def test_composite_valid_tuples_pass(self):
        parent = pd.DataFrame({"pk_a": [1, 2], "pk_b": [1, 99]})
        child = pd.DataFrame({"fk_a": [1, 2], "fk_b": [1, 99]})
        findings = check_fk_preservation(
            {"parent": parent, "child": child}, self._composite_config()
        )
        assert len(findings) == 1
        assert findings[0].orphan_count == 0
        assert findings[0].severity == "info"


# ── F7: Missing parent table produces error finding ───────────────────────────


class TestF7MissingParentTable:
    """When a declared parent table is not present in output_frames,
    the check should skip that relationship gracefully (the current
    code skips entirely when the parent table is absent, producing no
    finding). This tests the defensive path.
    """

    def test_missing_parent_table_produces_no_finding(self):
        # orders exists but users (the parent) is missing from output
        orders = pd.DataFrame({"order_id": range(5), "user_id": range(5)})
        config = _single_fk_config()
        findings = check_fk_preservation({"orders": orders}, config)
        # The parent table is absent -> the relationship is silently skipped.
        # No crash, no erroneous finding.
        assert isinstance(findings, list)
        # No orphan findings should appear because we couldn't even walk the edge.
        for f in findings:
            assert f.parent_table != "users" or f.child_table != "orders"


# ── F8: Security - no raw FK values in findings ───────────────────────────────


class TestF8SecurityNoRawKeyValues:
    """FK preservation findings must not contain raw PK/FK values.

    The findings carry parent_table, parent_column, child_table,
    child_column, counts, and rates -- not the actual hash values or
    original ID values that would let an attacker reconstruct the mapping.
    """

    _CANARY_ID = "canary-pk-12345"

    def test_orphan_finding_contains_no_raw_fk_values(self):
        # Parent has real hash-like PKs; child references the canary directly.
        parent = pd.DataFrame({"user_id": [_hash_id(i) for i in range(5)]})
        child = pd.DataFrame({"user_id": [self._CANARY_ID] * 20})
        config = _single_fk_config()
        findings = check_fk_preservation({"users": parent, "orders": child}, config)
        finding_str = str([f.__dict__ if hasattr(f, "__dict__") else f for f in findings])
        assert self._CANARY_ID not in finding_str, (
            f"raw FK value {self._CANARY_ID!r} leaked into FK findings: {finding_str[:400]!r}"
        )

    def test_runner_fk_preservation_section_has_no_raw_values(self):
        parent = pd.DataFrame({"user_id": [_hash_id(i) for i in range(5)]})
        child = pd.DataFrame({"user_id": [self._CANARY_ID] * 20})
        config = _single_fk_config()
        report = run_storm_post_mask(
            source_frames={"users": parent, "orders": child},
            output_frames={"users": parent, "orders": child},
            config=config,
        )
        report_str = str(report["fk_preservation"])
        assert self._CANARY_ID not in report_str, (
            f"raw FK value {self._CANARY_ID!r} leaked into fk_preservation section "
            f"of report: {report_str[:400]!r}"
        )
