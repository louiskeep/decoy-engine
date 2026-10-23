"""Coded, redacted snapshot error base for the DuckDB-attach connectors.

Dialect-independent base extracted from ``postgres_snapshot`` (D0). Each dialect
subclasses it with its own ``_PREFIX`` so a message reads
``<dialect>_snapshot[code] at <location> cause=<type>`` and never echoes a
value, DSN, or connection string.
"""

from __future__ import annotations

from decoy_engine.sdk import PermanentError


class SnapshotError(PermanentError):
    """Coded, redacted snapshot failure.

    ``code`` is a stable machine-readable token. ``location`` is a relation
    (``schema.table``) or ``schema.table.column``, never a value or DSN. The
    message is built only from those parts plus an exception type name.
    """

    _PREFIX = "duckdb_snapshot"

    def __init__(self, code: str, *, location: str = "", cause_type: str = "") -> None:
        self.code = code
        self.location = location
        self.cause_type = cause_type
        parts = [f"{self._PREFIX}[{code}]"]
        if location:
            parts.append(f"at {location}")
        if cause_type:
            parts.append(f"cause={cause_type}")
        super().__init__(" ".join(parts))
