"""Dialect-independent SQL string helpers for the DuckDB-attach connectors.

Extracted verbatim from ``postgres_snapshot`` (D0). Identifier quoting, string
literals, and the total-order clause are DuckDB SQL, the same across every
attached dialect.
"""

from __future__ import annotations

from decoy_engine.connectors.duckdb_attach._models import RelationSelection


def _q(ident: str) -> str:
    """Quote a validated identifier. Validation already excludes ``"``."""
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    """SQL string literal. Used only for the DSN and generated paths."""
    return "'" + text.replace("'", "''") + "'"


def _order_clause(sel: RelationSelection) -> str:
    # Key columns first, then every other projected column as tiebreaker, so
    # keyless relations and non-unique keys still export in one total order.
    ordered = list(sel.order_by) + [c for c in sel.columns if c not in sel.order_by]
    return ", ".join(f"{_q(c)} ASC NULLS LAST" for c in ordered)


def export_relation(conn, sel: RelationSelection, target, row_group_size: int, alias: str) -> None:
    """Deterministic ordered COPY of one selected relation to Parquet.

    The dialect-independent export the Postgres adapter also uses (it keeps its
    own module-level ``_export_relation`` only so the integration suite can
    monkeypatch it). Reads from ``alias.schema.table``; identifiers are already
    validated + quoted.
    """
    cols = ", ".join(_q(c) for c in sel.columns)
    src = f"{_q(alias)}.{_q(sel.schema_name)}.{_q(sel.table)}"
    conn.execute(
        f"COPY (SELECT {cols} FROM {src} ORDER BY {_order_clause(sel)}) "
        f"TO {_lit(str(target))} (FORMAT parquet, ROW_GROUP_SIZE {int(row_group_size)})"
    )
