"""Reference table loader for decoy_engine (SP-06 / P5.INFRA.3).

Pattern: pyarrow Parquet I/O (apache/arrow, Apache-2.0).
See: https://arrow.apache.org/docs/python/parquet.html

Reference tables are static Parquet data bundled with the engine or
supplied by the operator at a configured path. They power the
``code_set`` and ``joint_mask`` strategies (P5.S.code_set,
P5.S.joint_mask).

Schema convention
-----------------
Every reference table MUST have:

- ``id`` column (int64, stable across versions) -- canonical row
  identifier for HMAC-keyed access.
- Domain columns specific to the table (e.g. ``zip``, ``city``,
  ``state`` for the US ZIP table).

Packaging metadata
------------------
Shipped tables carry Parquet file-level metadata under the key
``b"decoy_table_version"`` with a semantic-version string. A table
without this key is treated as unversioned (no warning).

Swap-in hook
------------
Replace a shipped table with a fuller dataset by dropping a Parquet at
the ``path`` argument of :func:`load_table`. The file MUST follow the
same schema convention (``id`` column + matching domain columns).

Public API
----------
:func:`load_table` -- load by name (shipped or customer-provided path).
:class:`ReferenceTable` -- in-memory table with row and keyed access.
"""

from __future__ import annotations

from decoy_engine.reference_tables._loader import load_table
from decoy_engine.reference_tables._types import ReferenceTable

__all__ = ["load_table", "ReferenceTable"]
