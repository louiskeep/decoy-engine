"""A8: the ``"typed"`` golden of ``canary-span-normalization/v1``.

The expected texts were produced by an independent reference (Python ``datetime`` with floor
``divmod``, float ``repr``), not by the canonicalizer, and any edit to a row or a canonical rule
needs ``canary-span-normalization/v2``. This golden is the anchor a V2 native matcher reproduces.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.verification.canary._canonical import CellState, canonical_cells
from tests.unit.verification.canary.test_canonical_text import unvalidated

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "f34"
    / "canary-span-normalization-v1-golden.json"
)
UNITS = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}
DATE_MIN, DATE_MAX = -719_162, 2_932_896
DAY_MS = 86_400_000

_FIXED = {n: getattr(pa, n)() for n in ("int8", "int16", "int32", "int64", "uint8", "uint16")}
_FIXED |= {n: getattr(pa, n)() for n in ("uint32", "uint64", "float32", "float64", "null")}
_FIXED["bool"] = pa.bool_()
_VARIABLE = {n: getattr(pa, n)() for n in ("string", "large_string", "binary", "large_binary")}


def _rows() -> list[dict]:
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(doc) <= {"typed", "unicode"}
    assert doc["typed"]["contract"] == "canary-span-normalization/v1"
    return doc["typed"]["rows"]


def _bytes(v: dict | None) -> bytes | None:
    return None if v is None else bytes.fromhex(v["hex"])


def _float(v: dict | None) -> float | None:
    h = None if v is None else v["float_hex"]
    return h if h is None else float(h) if h in ("nan", "inf", "-inf") else float.fromhex(h)


def _temporal(typ: str, values: list) -> pa.Array | None:
    m = re.fullmatch(r"(timestamp|time32|time64)\[(s|ms|us|ns)(?:,(UTC))?\]", typ)
    if typ in ("date32", "date64"):
        storage, target = (
            (pa.int32(), pa.date32()) if typ == "date32" else (pa.int64(), pa.date64())
        )
    elif m is None:
        return None
    elif m[1] == "timestamp":
        storage, target = pa.int64(), pa.timestamp(m[2], tz=m[3])
    else:
        storage = pa.int32() if m[1] == "time32" else pa.int64()
        target = pa.time32(m[2]) if m[1] == "time32" else pa.time64(m[2])
    return pa.array(values, storage).view(target)


def build(typ: str, values) -> pa.Array:
    temporal = _temporal(typ, values)
    if temporal is not None:
        return temporal
    if typ in _VARIABLE:
        return unvalidated(_VARIABLE[typ], [_bytes(v) for v in values])
    if typ == "string_view":
        return unvalidated(pa.string(), [_bytes(v) for v in values]).cast(pa.string_view())
    if typ == "fixed_size_binary[2]":
        return pa.array([_bytes(v) for v in values], pa.binary(2))
    if typ == "dictionary<int8,string>":
        dictionary = unvalidated(pa.string(), [_bytes(v) for v in values["values"]])
        return pa.DictionaryArray.from_arrays(pa.array(values["indices"], pa.int8()), dictionary)
    if typ.startswith("float"):
        return pa.array([_float(v) for v in values], pa.float64()).cast(_FIXED[typ])
    m = re.fullmatch(r"decimal(128|256)\((\d+),(-?\d+)\)", typ)
    if m:
        make = pa.decimal128 if m[1] == "128" else pa.decimal256
        dec = [None if v is None else decimal.Decimal(v) for v in values]
        return pa.array(dec, make(int(m[2]), int(m[3])))
    return pa.array(values, _FIXED[typ])


def _expected(item: dict) -> str | CellState:
    if "text" in item:
        return item["text"]
    return {"null_like": CellState.NULL_LIKE, "uncompared": CellState.UNCOMPARED}[item["state"]]


def _mismatches(rows: list[dict]) -> list[str]:
    bad = []
    for row in rows:
        got = canonical_cells(build(row["type"], row["values"]))
        want = [_expected(e) for e in row["expected"]]
        bad += [
            f"{row['id']}[{i}]" for i, (g, w) in enumerate(zip(got, want, strict=True)) if g != w
        ]
    return bad


def _at(row_id: str, value: int) -> dict:
    row = next(r for r in _rows() if r["id"] == row_id)
    return row["expected"][row["values"].index(value)]


def test_golden_v1() -> None:
    rows = _rows()
    assert len(rows) >= 30
    assert _mismatches(rows) == []
    assert _at("date32", -3653) == {"text": "1960-01-01"}
    assert _at("timestamp_s", -1) == {"text": "1969-12-31T23:59:59"}
    assert _at("timestamp_ns", -1) == {"text": "1969-12-31T23:59:59.999999999"}


def test_golden_checker_catches_a_str_cell_row() -> None:
    # Seeded violation: a row whose expected text came from ``str(cell)`` must not pass.
    rows = [r for r in _rows() if r["id"] in ("float64", "timestamp_ns", "bool", "timestamp_s")]
    seeded = []
    for row in rows:
        keep = [v for v, e in zip(row["values"], row["expected"], strict=True) if "text" in e]
        as_str = [{"text": str(s)} for s in build(row["type"], keep).to_pylist()]
        seeded.append({**row, "values": keep, "expected": as_str})
    bad = _mismatches(seeded)
    assert {b.split("[")[0] for b in bad} == {"float64", "timestamp_ns", "bool", "timestamp_s"}


def _truncating(unit: str, v: int) -> str:
    """The rule the golden must reject: ``int(v / n)`` rounds toward zero before 1970."""
    per_second = UNITS[unit]
    day = int(v / (86_400 * per_second))
    within = abs(v - day * 86_400 * per_second)
    clock = dt.timedelta(seconds=within // per_second)
    return f"{dt.date(1970, 1, 1) + dt.timedelta(days=day)}T{clock}.{within % per_second}"


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_pre_1970_rows_fail_under_truncation(unit: str) -> None:
    expected = _at(f"timestamp_{unit}", -1)["text"]
    assert expected.startswith("1969-12-31T23:59:59")
    assert not _truncating(unit, -1).startswith("1969-12-31")
    assert canonical_cells(build(f"timestamp[{unit}]", [-1])) == [expected]


def _quad(rid: str, typ: str, lo: int, hi: int, step: int = 1) -> list[tuple]:
    pairs = ((lo, True), (hi, True), (lo - step, False), (hi + step, False))
    return [(rid, typ, v, ok) for v, ok in pairs]


def _edges() -> list[tuple]:
    """(row id, type, value, in range) at each range edge and one beyond, from the plan table."""
    out = _quad("date32", "date32", DATE_MIN, DATE_MAX)
    out += _quad("date64", "date64", DATE_MIN * DAY_MS, DATE_MAX * DAY_MS, DAY_MS)
    out += [("timestamp_ns", "timestamp[ns]", v, True) for v in (-(2**63), 2**63 - 1)]
    for unit in ("s", "ms", "us"):
        day = 86_400 * UNITS[unit]
        out += _quad(
            f"timestamp_{unit}", f"timestamp[{unit}]", DATE_MIN * day, (DATE_MAX + 1) * day - 1
        )
    for typ in ("time32[s]", "time32[ms]", "time64[us]", "time64[ns]"):
        rid, day = re.sub(r"\[(\w+)\]", r"_\1", typ), 86_400 * UNITS[typ[7:-1]]
        out += _quad(rid, typ, 0, day - 1)
    return out


@pytest.mark.parametrize(("row_id", "typ", "value", "in_range"), _edges())
def test_temporal_range_edges(row_id: str, typ: str, value: int, in_range: bool) -> None:
    assert next(r for r in _rows() if r["id"] == row_id)["type"] == typ
    expected = _at(row_id, value)
    assert ("text" in expected) is in_range
    if not in_range:
        assert expected == {"state": "uncompared"}
    assert canonical_cells(build(typ, [value])) == [_expected(expected)]
