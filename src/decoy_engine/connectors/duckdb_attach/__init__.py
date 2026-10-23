"""DuckDB-attach connectors: the dialect-independent core shared by every
database source Decoy reads through a DuckDB read-only ATTACH (D0).

A source database is a ``Dialect`` (``_dialect.Dialect``) plus the generic
``run_snapshot`` orchestration here. Postgres is the first adapter
(``decoy_engine.connectors.postgres_snapshot``); SQLite/MySQL/etc. add a
``Dialect`` and a fidelity matrix, reusing everything in this package.
"""

from decoy_engine.connectors.duckdb_attach._core import run_snapshot
from decoy_engine.connectors.duckdb_attach._dialect import Dialect
from decoy_engine.connectors.duckdb_attach.catalog import (
    CatalogListing,
    ColumnInfo,
    TableInfo,
    browse,
)
from decoy_engine.connectors.duckdb_attach._errors import SnapshotError
from decoy_engine.connectors.duckdb_attach._models import (
    _IDENT_RE,
    RelationSelection,
    SnapshotConfig,
    _validate_ident,
)
from decoy_engine.connectors.duckdb_attach._sql import _lit, _order_clause, _q

__all__ = [
    "CatalogListing",
    "ColumnInfo",
    "Dialect",
    "RelationSelection",
    "SnapshotConfig",
    "SnapshotError",
    "TableInfo",
    "browse",
    "run_snapshot",
    "_IDENT_RE",
    "_lit",
    "_order_clause",
    "_q",
    "_validate_ident",
]
