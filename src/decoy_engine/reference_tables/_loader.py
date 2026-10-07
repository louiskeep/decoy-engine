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

from decoy_engine import fileaccess
from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessDenied,
    FileHandle,
    FileRequest,
    FileRole,
)
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.reference_tables._registry import SHIPPED_TABLES, shipped_path
from decoy_engine.reference_tables._types import ReferenceTable

_LOG = logging.getLogger(__name__)

# Expected version for each shipped table, from the one shipped-table registry. Files that
# carry a ``decoy_table_version`` metadata key are checked against this map.
_SHIPPED_VERSIONS: dict[str, str] = {t.name: t.version for t in SHIPPED_TABLES.values()}


_CUSTOMER_PREFIX = "customer:"


@fileaccess.entry_point
def load_table(
    name: str, path: Path | None = None, *, file_access: FileAccess | None = None
) -> ReferenceTable:
    """Load a reference table by name.

    When ``path`` is omitted, loads the shipped table of that name; a bare name that is not
    in the shipped-table registry is refused without any file access (it is a lookup key,
    never a path). When ``path`` is provided, loads that file instead (customer-provided
    swap-in hook -- the file must follow the same schema convention: an ``id``
    column plus domain columns), through the file-access policy.

    Customer-provided tables can also be specified by prefixing the ``name``
    with ``"customer:"`` (e.g. ``"customer:/path/to/my_table.parquet"``). This
    is the canonical customer-provided reference table pathway (SP-08b). The
    file path follows the prefix directly without a separator.

    Args:
        name: Table identifier (e.g. ``"us_zip5_city_state"``) or a
            ``"customer:/path/to/file.parquet"`` reference (SP-08b).
        path: Optional override path to a Parquet file. Takes precedence over
            any ``"customer:"`` prefix in ``name``.
        file_access: The policy that decides which files may be read (default: ambient CLI).

    Returns:
        A :class:`~decoy_engine.reference_tables._types.ReferenceTable`.

    Raises:
        FileNotFoundError: No shipped table for ``name`` and no ``path``.
        ValueError: File is not readable as Parquet, or lacks the ``id``
            column.
    """
    # Resolve customer: prefix -> explicit path.
    if path is None and name.startswith(_CUSTOMER_PREFIX):
        customer_path_str = name[len(_CUSTOMER_PREFIX) :]
        path = Path(customer_path_str)
        # Use the basename (without extension) as the logical name for version checking.
        name = path.stem

    handle: FileHandle
    if path is None:
        if name not in SHIPPED_TABLES:
            raise FileNotFoundError(
                f"no shipped reference table {name!r}. "
                f"Provide a path= override, a 'customer:/path/to/file.parquet' reference, "
                f"or check the table name."
            )
        handle = fileaccess.engine_handle(shipped_path(name))
    else:
        handle = fileaccess.current().open_read(
            FileRequest(FileRole.REFERENCE, "read", str(path), "joint_mask.reference")
        )

    try:
        try:
            arrow_table = arrow_adapter.read_table(handle)
        except FileAccessDenied:
            raise
        except Exception as exc:
            raise ValueError(f"failed to read Parquet at {handle.display}: {exc}") from exc
    finally:
        handle.close()
    shown = handle.display

    if "id" not in arrow_table.schema.names:
        raise ValueError(
            f"reference table at {shown} is missing required 'id' column. "
            f"All reference tables must have a stable 'id' column."
        )

    id_type = arrow_table.schema.field("id").type
    if id_type != pa.int64():
        raise ValueError(
            f"reference table at {shown} has 'id' column of type {id_type!r}; "
            f"expected int64. All reference tables must have a stable int64 'id' column "
            f"(schema convention -- see reference_tables/__init__.py)."
        )

    _check_version(name, arrow_table, shown)
    return ReferenceTable(arrow_table, name=name)


def _check_version(name: str, table: Any, path: str) -> None:
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
