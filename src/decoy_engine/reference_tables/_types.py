"""ReferenceTable type for decoy_engine.reference_tables (SP-06 / P5.INFRA.3).

Exposes random access by row index and HMAC-keyed access.
HMAC keying uses decoy_engine.internal.crypto.hmac_hex per the
established-methodology rule (same HMAC-SHA256 primitive used by
transforms/date_shift.py and generators/_formula.py).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.internal.crypto import hmac_hex

# Stable salt for HMAC-keyed row derivation. Not a secret: its purpose
# is determinism and collision-resistance, not encryption. Same pattern
# as the formula-hash primitive in generators/_formula.py.
_KEYED_ACCESS_SALT = b"decoy.reference_tables.keyed_access.v1"


class ReferenceTable:
    """An in-memory reference table loaded from a Parquet file.

    Immutable after construction. Thread-safe for concurrent reads.

    Attributes:
        row_count: Total number of rows in the table.
        column_names: Names of all columns in load order.
    """

    def __init__(self, table: pa.Table, name: str) -> None:
        self._table = table
        self._name = name

    @property
    def row_count(self) -> int:
        """Total number of rows."""
        return self._table.num_rows

    @property
    def column_names(self) -> list[str]:
        """Names of all columns in load order."""
        return self._table.schema.names

    def row(self, index: int) -> dict[str, Any]:
        """Return the row at ``index`` as a plain dict.

        Args:
            index: Zero-based row index.

        Returns:
            Mapping of column name to Python value.

        Raises:
            IndexError: ``index`` is out of range.
        """
        if index < 0 or index >= self.row_count:
            raise IndexError(
                f"row index {index} out of range for table {self._name!r} "
                f"(row_count={self.row_count})"
            )
        return {col: self._table.column(col)[index].as_py() for col in self.column_names}

    def keyed_row(self, key_value: str) -> dict[str, Any]:
        """Return a deterministic row selected by HMAC-keyed modular index.

        Uses HMAC-SHA256(salt, key_value) to derive a stable row index.
        The same key always maps to the same row across runs and
        deployments, as long as the table does not change.

        Cites: decoy_engine.internal.crypto.hmac_hex (HMAC-SHA256,
        established-methodology per V2.0-C).

        Args:
            key_value: Arbitrary string key (e.g. a masked PK value).

        Returns:
            Row dict for the derived index.
        """
        hex_digest = hmac_hex(_KEYED_ACCESS_SALT, key_value)
        assert hex_digest is not None
        # First 8 hex chars -> 32-bit int; modulo row_count for stable index.
        index = int(hex_digest[:8], 16) % self.row_count
        return self.row(index)
