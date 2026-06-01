"""Tests for cross-file PK/FK inference.

Mirrors the test style in tests/integration/test_walks_inference.py but
exercises the file-style naming convention (FK column shares a name
with the referenced PK column, e.g. orders.customer_id ->
customers.customer_id).
"""

from __future__ import annotations

import json

from decoy_engine.storm.types import FieldStats, StormProfile
from decoy_engine.walks import (
    Edge,
    infer_cross_file_edges,
    run_cross_file_walk,
    storm_profiles_to_snapshot,
)


def _fs(
    name: str,
    *,
    inferred_type: str = "integer",
    null_rate: float = 0.0,
    unique_rate: float = 0.5,
    is_likely_unique: bool = False,
    distinct_count: int = 100,
) -> FieldStats:
    """Minimal FieldStats. STORM normally fills more, but the cross-file
    walk only reads name, inferred_type, null_rate, is_likely_unique."""
    return FieldStats(
        name=name,
        inferred_type=inferred_type,
        dtype_raw=inferred_type,
        row_count=1000,
        null_count=int(null_rate * 1000),
        null_rate=null_rate,
        distinct_count=distinct_count,
        unique_rate=unique_rate,
        is_likely_unique=is_likely_unique,
    )


def _profile(source_label: str, fields: list[FieldStats]) -> StormProfile:
    return StormProfile(
        source_label=source_label,
        row_count=1000,
        sample_strategy="full",
        fields=fields,
    )


# ── storm_profiles_to_snapshot ─────────────────────────────────────────


def test_snapshot_strips_file_extension_from_source_label():
    snap = storm_profiles_to_snapshot(
        [
            _profile("acme_csv_customers.csv", [_fs("customer_id", is_likely_unique=True)]),
        ]
    )
    assert len(snap.tables) == 1
    assert snap.tables[0].name == "acme_csv_customers"


def test_snapshot_keeps_schema_qualified_table_name_intact():
    # Connector-sourced scans use ``schema.table`` as the source_label.
    # The 1-5 char alnum extension heuristic leaves names ending in
    # 6+ char fragments alone, so ``public.orders`` survives intact.
    snap = storm_profiles_to_snapshot(
        [
            _profile("public.orders", [_fs("order_id", is_likely_unique=True)]),
        ]
    )
    assert snap.tables[0].name == "public.orders"


def test_snapshot_marks_unique_columns_as_primary_keys():
    snap = storm_profiles_to_snapshot(
        [
            _profile(
                "customers.csv",
                [
                    _fs("customer_id", is_likely_unique=True, unique_rate=1.0),
                    _fs("status", is_likely_unique=False, unique_rate=0.01, distinct_count=5),
                ],
            ),
        ]
    )
    cols = {c.name: c for c in snap.tables[0].columns}
    assert cols["customer_id"].is_primary_key is True
    assert cols["status"].is_primary_key is False


def test_snapshot_nullable_reflects_null_rate():
    snap = storm_profiles_to_snapshot(
        [
            _profile(
                "t.csv",
                [
                    _fs("with_nulls", null_rate=0.02),
                    _fs("no_nulls", null_rate=0.0),
                ],
            ),
        ]
    )
    cols = {c.name: c for c in snap.tables[0].columns}
    assert cols["with_nulls"].nullable is True
    assert cols["no_nulls"].nullable is False


# ── infer_cross_file_edges ─────────────────────────────────────────────


def test_file_style_edge_when_fk_column_name_matches_pk_column_name():
    """customers.customer_id (PK) <- orders.customer_id (FK)."""
    snap = storm_profiles_to_snapshot(
        [
            _profile("customers.csv", [_fs("customer_id", is_likely_unique=True)]),
            _profile(
                "orders.csv",
                [
                    _fs("order_id", is_likely_unique=True),
                    _fs("customer_id", is_likely_unique=False, unique_rate=0.2),
                ],
            ),
        ]
    )
    edges = infer_cross_file_edges(snap)
    assert edges == (
        Edge(
            source_table="orders",
            source_column="customer_id",
            target_table="customers",
            target_column="customer_id",
            declared=False,
        ),
    )


def test_does_not_emit_self_loops_when_pk_column_name_repeats_in_same_table():
    # A column flagged PK doesn't emit edges from itself even when other
    # columns share the name (rare in practice, but the guard matters).
    snap = storm_profiles_to_snapshot(
        [
            _profile("t.csv", [_fs("id", is_likely_unique=True)]),
        ]
    )
    assert infer_cross_file_edges(snap) == ()


def test_does_not_emit_edge_between_two_non_pk_columns_with_same_name():
    # Without a PK anchor we have no idea which side is the parent.
    snap = storm_profiles_to_snapshot(
        [
            _profile("a.csv", [_fs("customer_id", is_likely_unique=False)]),
            _profile("b.csv", [_fs("customer_id", is_likely_unique=False)]),
        ]
    )
    assert infer_cross_file_edges(snap) == ()


def test_emits_multiple_edges_for_a_three_file_chain():
    # customers (PK customer_id) <- orders (PK order_id, FK customer_id) <- orderlines (FK order_id)
    snap = storm_profiles_to_snapshot(
        [
            _profile("customers.csv", [_fs("customer_id", is_likely_unique=True)]),
            _profile(
                "orders.csv",
                [
                    _fs("order_id", is_likely_unique=True),
                    _fs("customer_id", is_likely_unique=False, unique_rate=0.2),
                ],
            ),
            _profile(
                "orderlines.csv",
                [
                    _fs("orderline_id", is_likely_unique=True),
                    _fs("order_id", is_likely_unique=False, unique_rate=0.2),
                ],
            ),
        ]
    )
    edges = infer_cross_file_edges(snap)
    assert set(edges) == {
        Edge("orders", "customer_id", "customers", "customer_id", False),
        Edge("orderlines", "order_id", "orders", "order_id", False),
    }


def test_tie_break_when_fk_column_is_also_100_percent_unique():
    # 1:1:1 referential integrity: every customer has exactly one order,
    # every order exactly one orderline. STORM flags both customer_id
    # columns (in customers and in orders) as is_likely_unique because
    # both have unique_rate == 1.0. The tie-break should pick the table
    # whose name matches the column's `<stem>_id` stem ("customer" /
    # "customers"), demoting the other to FK so the edge still fires.
    snap = storm_profiles_to_snapshot(
        [
            _profile(
                "customers.csv",
                [
                    _fs("customer_id", is_likely_unique=True, unique_rate=1.0),
                ],
            ),
            _profile(
                "orders.csv",
                [
                    _fs("order_id", is_likely_unique=True, unique_rate=1.0),
                    _fs("customer_id", is_likely_unique=True, unique_rate=1.0),
                ],
            ),
        ]
    )
    edges = infer_cross_file_edges(snap)
    assert edges == (Edge("orders", "customer_id", "customers", "customer_id", False),)


def test_tie_break_matches_table_name_suffix_pattern():
    # File-named tables often have a prefix that doesn't strip into the
    # column stem (e.g. "acme_csv_customers"). The suffix-match path
    # should still resolve customer_id to that table.
    snap = storm_profiles_to_snapshot(
        [
            _profile(
                "acme_csv_customers.csv",
                [
                    _fs("customer_id", is_likely_unique=True, unique_rate=1.0),
                ],
            ),
            _profile(
                "acme_csv_orders.csv",
                [
                    _fs("order_id", is_likely_unique=True, unique_rate=1.0),
                    _fs("customer_id", is_likely_unique=True, unique_rate=1.0),
                ],
            ),
        ]
    )
    edges = infer_cross_file_edges(snap)
    assert edges == (
        Edge("acme_csv_orders", "customer_id", "acme_csv_customers", "customer_id", False),
    )


def test_no_edge_when_pk_ambiguity_cannot_be_resolved():
    # Two tables both flag `id` as PK and neither table name matches a
    # ``<stem>_id`` pattern (the column name is bare "id", no stem).
    # The tie-break can't pick a winner so no edge is emitted.
    snap = storm_profiles_to_snapshot(
        [
            _profile("a.csv", [_fs("id", is_likely_unique=True)]),
            _profile("b.csv", [_fs("id", is_likely_unique=True)]),
        ]
    )
    assert infer_cross_file_edges(snap) == ()


# ── run_cross_file_walk ────────────────────────────────────────────────


def test_run_cross_file_walk_returns_sorted_edges_and_summary():
    result = run_cross_file_walk(
        [
            _profile("customers.csv", [_fs("customer_id", is_likely_unique=True)]),
            _profile(
                "orders.csv",
                [
                    _fs("order_id", is_likely_unique=True),
                    _fs("customer_id", is_likely_unique=False),
                ],
            ),
        ]
    )
    assert result.snapshot_summary == {
        "table_count": 2,
        "column_count": 3,
        "edge_count": 1,
    }
    assert len(result.edges) == 1
    assert result.edges[0].source_table == "orders"
    assert result.edges[0].target_table == "customers"


def test_run_cross_file_walk_empty_when_no_relationships_inferable():
    result = run_cross_file_walk(
        [
            _profile("a.csv", [_fs("name")]),
            _profile("b.csv", [_fs("title")]),
        ]
    )
    assert result.edges == ()
    assert result.snapshot_summary["edge_count"] == 0


def test_run_cross_file_walk_dedupes_against_sql_style_inference():
    # SQL-style: customers has literal `id` PK, orders.customer_id -> customers.id.
    # File-style would NOT fire here because the column name differs between
    # the PK ('id') and the FK ('customer_id'). The merged result should be
    # exactly one edge (the SQL-style one) without duplication.
    snap_profile_customers = StormProfile(
        source_label="customers.csv",
        row_count=10,
        sample_strategy="full",
        fields=[
            FieldStats(
                name="id",
                inferred_type="integer",
                dtype_raw="int64",
                row_count=10,
                null_count=0,
                null_rate=0.0,
                distinct_count=10,
                unique_rate=1.0,
                is_likely_unique=True,
            ),
        ],
    )
    snap_profile_orders = StormProfile(
        source_label="orders.csv",
        row_count=10,
        sample_strategy="full",
        fields=[
            FieldStats(
                name="id",
                inferred_type="integer",
                dtype_raw="int64",
                row_count=10,
                null_count=0,
                null_rate=0.0,
                distinct_count=10,
                unique_rate=1.0,
                is_likely_unique=True,
            ),
            FieldStats(
                name="customer_id",
                inferred_type="integer",
                dtype_raw="int64",
                row_count=10,
                null_count=0,
                null_rate=0.0,
                distinct_count=5,
                unique_rate=0.5,
                is_likely_unique=False,
            ),
        ],
    )
    result = run_cross_file_walk([snap_profile_customers, snap_profile_orders])
    assert result.edges == (Edge("orders", "customer_id", "customers", "id", False),)


# ── round-trip through StormProfile.to_dict() ──────────────────────────


def test_profiles_round_trip_through_dict_unchanged():
    """The platform stores StormProfile as JSON; deserialize must still
    feed the walk correctly. Exercise via to_dict() -> json -> dict ->
    field-by-field reconstruction (mirrors the platform code path)."""
    p = _profile("customers.csv", [_fs("customer_id", is_likely_unique=True)])
    blob = json.dumps(p.to_dict())
    loaded = json.loads(blob)
    # Reconstruct StormProfile from the dict (skip FieldStats sub-fields
    # the loader doesn't care about — same shape as platform does).
    reconstructed = StormProfile(
        source_label=loaded["source_label"],
        row_count=loaded["row_count"],
        sample_strategy=loaded["sample_strategy"],
        fields=[FieldStats(**f) for f in loaded["fields"]],
    )
    result = run_cross_file_walk([reconstructed])
    # One table, zero edges (no FK pairing), summary still computed.
    assert result.snapshot_summary["table_count"] == 1
    assert result.edges == ()


# QA walks/generators F2 (2026-06-01, CRITICAL determinism) ───────────────


def test_walks_gen_f2_pk_tie_break_stable_under_set_iteration_order():
    """F2 contract: when two tables both match the stem heuristic
    (`customers` + `customer_archive`), the tie-break must pick the
    same winner on every process. Pre-fix the loser-vs-winner choice
    came from set[str] iteration, which depends on PYTHONHASHSEED and
    re-randomises on every process start.

    The internal helper sorts its input before iterating; this cell
    pins the public-surface stability by simulating two equally
    qualified PK owners and asserting the resolution is the same
    string on every call.
    """
    snap = storm_profiles_to_snapshot(
        [
            _profile(
                "customers.csv",
                [
                    _fs("customer_id", is_likely_unique=True, unique_rate=1.0),
                ],
            ),
            _profile(
                "customer_archive.csv",
                [
                    _fs("customer_id", is_likely_unique=True, unique_rate=1.0),
                ],
            ),
            _profile(
                "orders.csv",
                [
                    _fs("order_id", is_likely_unique=True, unique_rate=1.0),
                    _fs("customer_id"),
                ],
            ),
        ]
    )
    edges_a = infer_cross_file_edges(snap)
    edges_b = infer_cross_file_edges(snap)
    edges_c = infer_cross_file_edges(snap)
    assert edges_a == edges_b == edges_c, (
        "QA walks/generators F2: tie-break must yield identical edges "
        "across calls within the same process. Drift indicates set "
        "iteration order leaked through."
    )


def test_walks_gen_f2_pk_helper_iterates_in_sorted_order():
    """Direct unit-level pin on the private helper. The function
    accepts a set or any iterable; output must be identical regardless
    of construction order."""
    from decoy_engine.walks.cross_file import _pk_table_for_id_column

    a = _pk_table_for_id_column(
        "customer_id", {"customer_archive", "customers", "zeta"}
    )
    b = _pk_table_for_id_column(
        "customer_id", {"zeta", "customers", "customer_archive"}
    )
    c = _pk_table_for_id_column(
        "customer_id", ["zeta", "customer_archive", "customers"]
    )
    assert a == b == c, (
        f"QA walks/generators F2: _pk_table_for_id_column returned "
        f"{a!r} vs {b!r} vs {c!r}; iteration order leaked through."
    )
    assert a == "customers"
