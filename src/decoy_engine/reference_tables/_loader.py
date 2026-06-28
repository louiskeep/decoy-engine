"""Loader for reference tables (SP-06 / P5.INFRA.3).

Pattern: pyarrow Parquet I/O (apache/arrow, Apache-2.0).
See: https://arrow.apache.org/docs/python/parquet.html

Loads shipped tables from the ``data/`` sub-directory or a customer-
provided path. Version mismatch between the expected shipped version and
the file's metadata is logged as a WARNING and the table is still used
(graceful degradation -- the swap-in hook allows custom tables that may
not carry engine version metadata).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.reference_tables._types import ReferenceTable

_LOG = logging.getLogger(__name__)

# Directory holding the shipped Parquet files.
_DATA_DIR = Path(__file__).parent / "data"

# Expected version for each shipped table. Files that carry a
# ``decoy_table_version`` metadata key are checked against this map.
# Increment when the table data or schema changes in a breaking way.
_SHIPPED_VERSIONS: dict[str, str] = {
    "us_zip5_city_state": "1.0",
    "vehicle_make_model_year": "1.0",
    # SP-08: restricted ZIP3 prefix list for geo_generalize Safe Harbor cascade.
    # Source: 45 CFR 164.514(b)(2)(i)(B) HHS HIPAA Safe Harbor guidance.
    "us_zip3_population": "1.0",
}


def load_table(name: str, path: Path | None = None) -> ReferenceTable:
    """Load a reference table by name.

    When ``path`` is omitted, loads the shipped table from the ``data/``
    sub-directory. When provided, loads that file instead (customer-provided
    swap-in hook -- the file must follow the same schema convention: an ``id``
    column plus domain columns).

    Args:
        name: Table identifier (e.g. ``"us_zip5_city_state"``).
        path: Optional override path to a Parquet file.

    Returns:
        A :class:`~decoy_engine.reference_tables._types.ReferenceTable`.

    Raises:
        FileNotFoundError: No shipped table for ``name`` and no ``path``.
        ValueError: File is not readable as Parquet, or lacks the ``id``
            column.
    """
    if path is None:
        path = _DATA_DIR / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(
                f"no shipped reference table {name!r}. "
                f"Provide a path= override or check the table name."
            )

    try:
        arrow_table = pq.read_table(str(path))  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise ValueError(f"failed to read Parquet at {path}: {exc}") from exc

    if "id" not in arrow_table.schema.names:
        raise ValueError(
            f"reference table at {path} is missing required 'id' column. "
            f"All reference tables must have a stable 'id' column."
        )

    id_type = arrow_table.schema.field("id").type
    if id_type != pa.int64():
        raise ValueError(
            f"reference table at {path} has 'id' column of type {id_type!r}; "
            f"expected int64. All reference tables must have a stable int64 'id' column "
            f"(schema convention -- see reference_tables/__init__.py)."
        )

    _check_version(name, arrow_table, path)
    return ReferenceTable(arrow_table, name=name)


def _check_version(name: str, table: Any, path: Path) -> None:
    """Emit a WARNING when file version metadata does not match expected."""
    expected = _SHIPPED_VERSIONS.get(name)
    if expected is None:
        return  # Unknown table name: no version expectation, skip check.

    schema_meta = table.schema.metadata or {}
    actual_bytes = schema_meta.get(b"decoy_table_version")
    if actual_bytes is None:
        _LOG.debug(
            "reference table %r at %s has no decoy_table_version metadata "
            "(expected %r). Continuing with loaded data.",
            name,
            path,
            expected,
        )
        return

    actual = actual_bytes.decode("utf-8", errors="replace")
    if actual != expected:
        _LOG.warning(
            "reference table %r version mismatch: file has %r, engine "
            "expects %r (path: %s). Using the available version. If this "
            "is a customer-provided table, update the table or adjust "
            "_SHIPPED_VERSIONS in _loader.py.",
            name,
            actual,
            expected,
            path,
        )
