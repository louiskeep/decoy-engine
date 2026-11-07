"""Value identity shared by the anonymity code: which cells count as the same value.

Two cells are one value when they are equal and of the same type family, so
``1``, ``1.0`` and ``True`` are three values while ``1`` and ``numpy.int64(1)``
are one. Plain equality (what ``pandas.factorize`` uses on an object column)
would merge the first three into whichever appears first, making any grouping
depend on row order.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import numpy as np


def type_family(cell: object) -> str:
    """Canonical type family; equal cells of one family are one value."""
    # Temporal checks come first: np.timedelta64 subclasses np.integer.
    if isinstance(cell, (datetime.timedelta, np.timedelta64)):
        return "timedelta"
    if isinstance(cell, (datetime.datetime, np.datetime64)):
        return "datetime"
    if isinstance(cell, (bool, np.bool_)):
        return "bool"
    if isinstance(cell, (int, np.integer)):
        return "int"
    if isinstance(cell, (float, np.floating)):
        return "float"
    if isinstance(cell, (complex, np.complexfloating)):
        return "complex"
    if isinstance(cell, Decimal):
        return "decimal"
    if isinstance(cell, str):
        return "str"
    if isinstance(cell, (bytes, np.bytes_)):
        return "bytes"
    if isinstance(cell, datetime.date):
        return "date"
    if isinstance(cell, datetime.time):
        return "time"
    kind = type(cell)
    return f"{kind.__module__}.{kind.__qualname__}"
