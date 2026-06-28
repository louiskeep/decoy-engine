"""ReferenceTable type for decoy_engine.reference_tables (SP-06 / P5.INFRA.3).

Exposes random access by row index and HMAC-keyed access.
HMAC keying uses decoy_engine.internal.crypto.hmac_hex per the
established-methodology rule (same HMAC-SHA256 primitive used by
transforms/date_shift.py and generators/_formula.py).

Keyed access (keyed_row) is an id-sorted positional selection:
rows are sorted ascending by the 'id' column at construction time,
and the HMAC modular index is applied over that sorted order. This
makes keyed_row deterministic within a table version and independent
of Parquet file row order. Known limitation: adding or removing rows
remaps the modular indices, so keyed_row results are NOT stable across
table versions that differ in row_count. This must be revisited before
joint_mask / code_set strategies (SP-08/09) rely on cross-version
key stability.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

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
        # Sort by 'id' ascending so that keyed_row access is positional
        # over a stable, file-order-independent ordering. All shipped and
        # customer-provided tables are required to have an int64 'id' column
        # (enforced by _loader.py). Direct construction without an 'id' column
        # is allowed for internal use but keyed_row will raise in that case.
        if "id" in table.schema.names:
            sort_indices = pc.sort_indices(  # type: ignore[attr-defined]
                table, sort_keys=[("id", "ascending")]
            )
            table = table.take(sort_indices)
        self._table = table
        self._name = name

    @property
    def row_count(self) -> int:
        """Total number of rows."""
        return int(self._table.num_rows)

    @property
    def column_names(self) -> list[str]:
        """Names of all columns in load order."""
        return list(self._table.schema.names)

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

        Access semantics: id-sorted positional. Rows are pre-sorted by the
        'id' column (ascending) at construction. The HMAC-SHA256 digest is
        reduced modulo row_count to select a position in that sorted order.
        This guarantees determinism within a table version and independence
        from Parquet file row order.

        Known limitation: if row_count changes (rows added or removed), the
        modular mapping shifts and a given key_value will select a different
        row. keyed_row is therefore deterministic WITHIN a table version
        but NOT stable ACROSS table versions with different row counts.
        This constraint must be revisited before joint_mask/code_set
        (SP-08/09) make cross-version stability guarantees.

        Cites: decoy_engine.internal.crypto.hmac_hex (HMAC-SHA256,
        established-methodology per V2.0-C).

        Args:
            key_value: Arbitrary string key (e.g. a masked PK value).

        Returns:
            Row dict for the derived index, from the id-sorted table.
        """
        hex_digest = hmac_hex(_KEYED_ACCESS_SALT, key_value)
        if hex_digest is None:
            raise ValueError("hmac_hex returned None for a non-None key_value")
        # First 8 hex chars -> 32-bit int; modulo row_count for id-sorted index.
        index = int(hex_digest[:8], 16) % self.row_count
        return self.row(index)
