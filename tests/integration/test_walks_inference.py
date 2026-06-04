"""Tests for heuristic PK/FK inference (`infer_edges`).

The heuristic kicks in for warehouses where the DBA didn't declare FK
constraints (common in Snowflake / Redshift / BigQuery deployments)
but column naming follows the `*_id` -> `<table>.id` convention.

Conservative on purpose: only fires on `*_id` columns whose stem
matches a table name (singular or plural) AND that table has an `id`
PK AND no declared FK already exists on the same column.
"""

from __future__ import annotations

from decoy_engine.walks import Column, Edge, SchemaSnapshot, Table, infer_edges


def _col(name: str, *, nullable: bool = True, pk: bool = False, dtype: str = "integer") -> Column:
    return Column(name=name, data_type=dtype, nullable=nullable, is_primary_key=pk)


def _table(name: str, columns: list[Column]) -> Table:
    return Table(name=name, schema="public", columns=tuple(columns))


def test_infers_simple_id_to_id_relationship():
    snap = SchemaSnapshot(
        db_kind="snowflake",  # warehouses are the typical heuristic target
        schema_name="public",
        tables=(
            _table("customers", [_col("id", pk=True, nullable=False)]),
            _table(
                "orders",
                [
                    _col("id", pk=True, nullable=False),
                    _col("customer_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(),
    )
    edges = infer_edges(snap)
    assert len(edges) == 1
    assert edges[0] == Edge(
        source_table="orders",
        source_column="customer_id",
        target_table="customers",
        target_column="id",
        declared=False,
    )


def test_does_not_shadow_a_declared_fk():
    """If the column already has a declared FK, the heuristic skips it
    so we don't duplicate edges."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("customers", [_col("id", pk=True, nullable=False)]),
            _table(
                "orders",
                [
                    _col("id", pk=True, nullable=False),
                    _col("customer_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(Edge("orders", "customer_id", "customers", "id", declared=True),),
    )
    edges = infer_edges(snap)
    assert edges == ()


def test_does_not_fire_when_target_table_lacks_id_pk():
    """Table named `lookup` exists but its key is `code`, not `id`.
    `lookup_id` shouldn't infer an edge because there's no `id` PK
    to connect to."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("lookup", [_col("code", pk=True, nullable=False, dtype="varchar")]),
            _table(
                "orders",
                [
                    _col("id", pk=True, nullable=False),
                    _col("lookup_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(),
    )
    assert infer_edges(snap) == ()


def test_singular_naming_preferred_over_plural():
    """If both `customer` and `customers` exist, prefer the singular
    (more specific match for `customer_id`)."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("customer", [_col("id", pk=True, nullable=False)]),
            _table("customers", [_col("id", pk=True, nullable=False)]),
            _table(
                "orders",
                [
                    _col("id", pk=True, nullable=False),
                    _col("customer_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(),
    )
    edges = infer_edges(snap)
    assert len(edges) == 1
    assert edges[0].target_table == "customer"


def test_plural_naming_when_singular_does_not_exist():
    """The most common case: `users` table, `user_id` column. Plural
    rule kicks in because `user` doesn't exist."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("users", [_col("id", pk=True, nullable=False)]),
            _table(
                "orders",
                [
                    _col("id", pk=True, nullable=False),
                    _col("user_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(),
    )
    edges = infer_edges(snap)
    assert len(edges) == 1
    assert edges[0].target_table == "users"


def test_emits_edges_in_stable_sorted_order():
    """Test assertions across runs need deterministic ordering."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("aaa", [_col("id", pk=True, nullable=False)]),
            _table("bbb", [_col("id", pk=True, nullable=False)]),
            _table(
                "z_table",
                [
                    _col("id", pk=True, nullable=False),
                    _col("bbb_id", nullable=False),
                    _col("aaa_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(),
    )
    edges = infer_edges(snap)
    assert [e.source_column for e in edges] == ["aaa_id", "bbb_id"]


def test_id_only_column_does_not_infer():
    """A column literally named `id` shouldn't try to be a FK to
    itself (or to anything else). The heuristic strips `_id` and the
    stem is empty, so we skip."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table(
                "foo",
                [
                    _col("id", pk=True, nullable=False),
                    _col("name", dtype="varchar"),
                ],
            ),
        ),
        declared_edges=(),
    )
    assert infer_edges(snap) == ()


def test_self_reference_inferable():
    """`teams.parent_team_id` inferring back to `teams.id` is the
    expected outcome: SR detection picks up these edges separately."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table(
                "teams",
                [
                    _col("id", pk=True, nullable=False),
                    _col("parent_team_id", nullable=True),
                ],
            ),
        ),
        declared_edges=(),
    )
    edges = infer_edges(snap)
    assert len(edges) == 1
    assert edges[0].source_table == "teams"
    assert edges[0].target_table == "teams"
