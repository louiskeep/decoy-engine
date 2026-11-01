"""DuckDB adapter: the only module that creates DuckDB connections.

Pattern: DuckDB's "Securing DuckDB" guidance for untrusted SQL: `enable_external_access =
false`, `lock_configuration = true`, extension auto-install and auto-load off, unsigned
extensions off. Two kinds of connection:

- Untrusted-SQL connections (`untrusted_session`): in-memory, tables registered as Arrow
  datasets over policy-issued handles, then the configuration locked, so no SQL text can read
  a file, write one, attach a database, load an extension or turn any of this back on.
- Engine-internal connections (`connect_internal`): never given caller SQL text; external
  access stays on because the engine reads and writes its own scratch Parquet through them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import duckdb
import pyarrow.dataset as ds

from decoy_engine.fileaccess._errors import guarded
from decoy_engine.fileaccess._types import FileHandle

_UNTRUSTED_CONFIG: dict[str, Any] = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_unsigned_extensions": False,
}


def statement_kinds(sql: str) -> list[str]:
    """The kind (`SELECT`, `COPY`, `ATTACH`, ...) of each statement DuckDB's own parser finds
    in `sql`, parsed on a throwaway connection that touches no file."""
    con = duckdb.connect(":memory:", config=_UNTRUSTED_CONFIG)
    try:
        return [str(s.type).rsplit(".", 1)[-1] for s in con.extract_statements(sql)]
    finally:
        con.close()


@contextmanager
def untrusted_session(handles: Mapping[str, FileHandle]) -> Iterator[duckdb.DuckDBPyConnection]:
    """A locked in-memory connection with each handle registered under its table name.

    The handles must stay open for the whole session: under a strict policy the scan reads
    the handle's descriptor."""
    con = duckdb.connect(":memory:", config=_UNTRUSTED_CONFIG)
    try:
        for name, handle in handles.items():
            strict = bool(getattr(handle, "strict", True))
            dataset = guarded(
                handle.display, strict, ds.dataset, handle.library_path(), format="parquet"
            )
            con.register(name, dataset)
        con.execute("SET lock_configuration = true")
        yield con
    finally:
        con.close()


def connect_internal(config: dict[str, Any]) -> duckdb.DuckDBPyConnection:
    """An in-memory connection for the engine's own out-of-core relations.

    Never pass it caller-supplied SQL text: external access is on."""
    return duckdb.connect(database=":memory:", config=config)
