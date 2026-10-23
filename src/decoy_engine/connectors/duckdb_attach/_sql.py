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
