"""Cross-file PK/FK inference from STORM profiles.

Walks today operates over a single ``SchemaSnapshot`` produced by the
platform's DB snapshotter. File-based STORM scans never produce a
snapshot — there's no live schema with declared constraints to read.
But once you have a group of STORM profiles (members of one
``storm_run_group_id``), you can synthesize a multi-table snapshot
from the per-column ``FieldStats`` they already carry and run PK/FK
inference on it.

Two complementary inference rules apply:

  - **Engine's existing :func:`decoy_engine.walks.inference.infer_edges`** handles the SQL-style
    ``<x>_id`` :math:`\\rightarrow` ``<x>.id`` pattern (works for any
    table that happens to have a literal ``id`` PK column).
  - **:func:`infer_cross_file_edges`** in this module handles the
    file-style pattern: an FK column has the **same name** as the PK
    column it references. So ``orders.customer_id`` (non-PK) links to
    ``customers.customer_id`` (PK).

Most file-based datasets follow the file-style pattern, so that's the
primary contributor. The SQL-style rule still runs in case a profile
mixes conventions.

The security-blind boundary holds: this module only consumes
``StormProfile`` dataclasses (which never carry raw row data), so it
can run anywhere in the engine without elevating data access.
"""

from __future__ import annotations

from dataclasses import dataclass

from decoy_engine.storm.types import StormProfile
from decoy_engine.walks.inference import infer_edges
from decoy_engine.walks.types import Column, Edge, SchemaSnapshot, Table


@dataclass(frozen=True)
class CrossFileWalkResult:
    """Output of :func:`run_cross_file_walk`.

    ``edges`` carries both inference rules merged + deduplicated.
    ``snapshot_summary`` is a small dict the platform can log or render
    without re-deriving counts.
    """

    snapshot_summary: dict
    edges: tuple[Edge, ...]


def run_cross_file_walk(profiles: list[StormProfile]) -> CrossFileWalkResult:
    """Infer cross-file foreign-key edges from a group of STORM profiles.

    Returns deterministic, sorted edges. Empty result is valid (no
    inferable relationships) — callers should not treat it as an error.
    """
    snapshot = storm_profiles_to_snapshot(profiles)
    file_style = infer_cross_file_edges(snapshot)
    sql_style = infer_edges(snapshot)

    merged: dict[tuple[str, str, str, str], Edge] = {}
    for e in (*file_style, *sql_style):
        key = (e.source_table, e.source_column, e.target_table, e.target_column)
        # Prefer the first occurrence (file-style runs first, which is the
        # more common pattern for file-based datasets).
        merged.setdefault(key, e)

    edges = tuple(
        sorted(
            merged.values(),
            key=lambda e: (e.source_table, e.source_column, e.target_table),
        )
    )
    return CrossFileWalkResult(
        snapshot_summary={
            "table_count": len(snapshot.tables),
            "column_count": sum(len(t.columns) for t in snapshot.tables),
            "edge_count": len(edges),
        },
        edges=edges,
    )


def storm_profiles_to_snapshot(profiles: list[StormProfile]) -> SchemaSnapshot:
    """Build a multi-table ``SchemaSnapshot`` from STORM profiles.

    Each profile becomes one ``Table`` named after the profile's
    ``source_label`` (file extension stripped). Per-column attributes
    are derived from ``FieldStats``:

      - ``name`` :math:`\\leftarrow` ``FieldStats.name``
      - ``data_type`` :math:`\\leftarrow` ``FieldStats.inferred_type``
      - ``nullable`` :math:`\\leftarrow` ``null_rate > 0`` (best guess from
        the sampled rows)
      - ``is_primary_key`` :math:`\\leftarrow` ``is_likely_unique``
        (heuristic — STORM flags columns where ``unique_rate > 0.9``
        which captures both surrogate keys and natural keys)

    ``declared_edges`` is always empty for file-based snapshots — there
    are no schema-level constraints to read.
    """
    tables: list[Table] = []
    for profile in profiles:
        table_name = _table_name_from_source_label(profile.source_label)
        columns = tuple(
            Column(
                name=fs.name,
                data_type=fs.inferred_type,
                nullable=fs.null_rate > 0,
                is_primary_key=fs.is_likely_unique,
            )
            for fs in profile.fields
        )
        tables.append(Table(name=table_name, schema="", columns=columns))

    return SchemaSnapshot(
        db_kind="file",
        schema_name="",
        tables=tuple(tables),
        declared_edges=(),
        connector_id=None,
    )


def infer_cross_file_edges(snapshot: SchemaSnapshot) -> tuple[Edge, ...]:
    """Emit FK edges where a non-PK column in one table shares a name
    with a PK column in another table.

    The file-style convention: ``customers.customer_id`` is the PK,
    ``orders.customer_id`` is the FK, and the column name is identical
    on both sides. The engine's existing
    :func:`decoy_engine.walks.inference.infer_edges` doesn't
    catch this because it strips ``_id`` and looks for a literal ``id``
    PK in the target table — that rule is for the SQL convention where
    the PK is always named ``id``.

    **PK ambiguity tie-break.** STORM flags any column with
    ``is_likely_unique=True`` as a PK candidate, but with 1:1:1
    referential integrity (every customer has exactly one order, every
    order exactly one orderline) the FK column lands at 100% unique
    too, so the same column name is flagged PK in multiple tables.
    Resolve the tie by name affinity: a column named ``<stem>_id``
    "really belongs to" the table whose name shares ``<stem>``
    (singular or plural). All other tables' instances of the column
    get demoted to FK candidates for the purpose of edge inference.

    Self-loops are skipped. Edges between two non-PK occurrences of
    the same column name are skipped — without a PK anchor we don't
    know which table is the "parent".
    """
    # Map column name -> tables where that column is flagged PK.
    pk_tables_by_col: dict[str, list[str]] = {}
    for table in snapshot.tables:
        for col in table.columns:
            if col.is_primary_key:
                pk_tables_by_col.setdefault(col.name, []).append(table.name)

    # Tie-break: when a column is PK in multiple tables, prefer the
    # table whose name stems match the column's *_id stem.
    table_names = {t.name for t in snapshot.tables}
    canonical_pk_table: dict[str, str | None] = {}
    for col_name, tables in pk_tables_by_col.items():
        if len(tables) == 1:
            canonical_pk_table[col_name] = tables[0]
            continue
        match = _pk_table_for_id_column(col_name, table_names)
        # If exactly one of the PK-flagged tables matches the stem,
        # promote it as canonical. Otherwise leave the column ambiguous
        # — emit no edges rather than guess wrong.
        if match in tables:
            canonical_pk_table[col_name] = match
        else:
            canonical_pk_table[col_name] = None

    edges: list[Edge] = []
    for table in snapshot.tables:
        for col in table.columns:
            pk_owner = canonical_pk_table.get(col.name)
            if pk_owner is None:
                continue
            if table.name == pk_owner:
                continue  # this table is the PK; don't emit edge from PK to itself
            edges.append(
                Edge(
                    source_table=table.name,
                    source_column=col.name,
                    target_table=pk_owner,
                    target_column=col.name,
                    declared=False,
                )
            )

    edges.sort(key=lambda e: (e.source_table, e.source_column, e.target_table))
    return tuple(edges)


def _pk_table_for_id_column(column_name: str, table_names: set[str]) -> str | None:
    """For a column named ``<stem>_id``, return the table whose name
    matches ``<stem>`` (singular or plural) — that table is the
    canonical owner of this PK. Falls back to suffix-match for table
    names that prefix or suffix the stem (e.g. ``acme_csv_customers``
    matches the ``customer_id`` stem).

    Returns ``None`` when the column name doesn't follow the
    ``<stem>_id`` convention or no table name matches.
    """
    name = column_name.lower()
    if not name.endswith("_id"):
        return None
    stem = name[:-3]
    if not stem:
        return None
    candidates = {stem, stem + "s"}

    # QA walks/generators F2 (2026-06-01, CRITICAL determinism):
    # iterate in sorted order. `table_names` arrives as a set[str],
    # whose iteration order depends on PYTHONHASHSEED (re-randomised on
    # every process start unless pinned). When two tables both match
    # the stem (e.g. `customers` + `customer_archive`), whichever the
    # set yielded first won; that varied across processes + restarts
    # and corrupted the `run_cross_file_walk` result + hazard UI.
    # Centralising the sort here means callers can pass any iterable.
    sorted_tables = sorted(table_names)

    # Exact match first.
    for t in sorted_tables:
        if t.lower() in candidates:
            return t
    # Suffix match: a table named like ``acme_csv_customers`` carries the
    # ``customers`` (or ``customer``) stem on its trailing segment.
    for t in sorted_tables:
        lower = t.lower()
        if any(lower.endswith(c) or lower.endswith("_" + c) for c in candidates):
            return t
    return None


def _table_name_from_source_label(label: str) -> str:
    """Derive a table name from a STORM ``source_label``.

    For file sources the label is the filename (e.g.
    ``acme_csv_customers.csv``); strip any directory prefix and the
    final extension. For connector sources the label is already a
    table identifier (``schema.table``) and survives unchanged.
    Empty / unparseable inputs fall back to the raw label.
    """
    name = label.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        # Strip only the final extension so ``foo.bar.csv`` -> ``foo.bar``.
        stem, ext = name.rsplit(".", 1)
        # Don't strip schema-qualified table names (no extension-like suffix).
        # Heuristic: skip the strip when the suffix is longer than 5 chars,
        # which is true for all real file extensions but false for normal
        # table-name fragments.
        if 1 <= len(ext) <= 5 and ext.isalnum():
            name = stem
    return name or label
