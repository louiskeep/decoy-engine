"""Tagged-JSON value codec for protected-quarantine rows, split out of ``_quarantine_local``.

A raw cell is stored as JSON with a one-key tag for each non-JSON type, so a reopened row has
the same Python types it was captured with (the extended-JSON tagging pattern of MongoDB's
Extended JSON ``$date`` / ``$numberDecimal`` wrappers).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any


def _encode(value: Any) -> Any:
    """A raw cell as tagged JSON that reopens as the same Python type.

    Only the types a framed Arrow row can hold are accepted; anything else is refused rather than
    stringified, so a reopened row is never a lossy approximation of what was captured.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"$hex": bytes(value).hex()}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, datetime):  # before date: datetime is a date subclass
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, dt_time):
        return {"$time": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$td": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, (list, tuple)):
        tag = "$tuple" if isinstance(value, tuple) else "$list"
        return {tag: [_encode(v) for v in value]}
    if isinstance(value, dict):
        return {"$map": [[_encode(k), _encode(v)] for k, v in value.items()]}
    raise TypeError("unsupported quarantined value type")


def _decode(value: Any) -> Any:
    if not isinstance(value, dict) or len(value) != 1:
        return value
    ((tag, body),) = value.items()
    if tag == "$hex":
        return bytes.fromhex(body)
    if tag == "$dec":
        return Decimal(body)
    if tag == "$datetime":
        return datetime.fromisoformat(body)
    if tag == "$date":
        return date.fromisoformat(body)
    if tag == "$time":
        return dt_time.fromisoformat(body)
    if tag == "$td":
        return timedelta(days=body[0], seconds=body[1], microseconds=body[2])
    if tag == "$list":
        return [_decode(v) for v in body]
    if tag == "$tuple":
        return tuple(_decode(v) for v in body)
    if tag == "$map":
        return {_decode(k): _decode(v) for k, v in body}
    raise ValueError("unknown value tag")
