"""Test 0's cell matrix and recorder (plan Acceptance test 0).

A cell is one source shape: a column `x` of a given Arrow type in a given role
(configured passthrough, unconfigured, or the source of a string-output
strategy or native Faker) with a given null pattern, beside the two always
masked string columns `h` (hash) and `r` (redact). `record_cell` runs today's
lane, the forced full frame and the dispatcher lane (B1 called as the plan's
Design 4 calls it) on both B1 routes and returns a JSON-safe description of
each output. The recorded fixture is checked in; the test compares the live
record against it and checks every lane difference against guarantee 3.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from tests.unit.execution import _auto_chunk_support as support

N = support.ROWS
CHUNK = support.CHUNK
DAY_MS = 86_400_000

# role -> column config for x (None = unconfigured).
ROLES: dict[str, dict[str, Any] | None] = {
    "pass": support.pass_col("x"),
    "uncfg": None,
    "hash": support.hash_col("x"),
    "truncate": support.truncate_col("x"),
    "redact": support.redact_col("x"),
    "faker": support.faker_col("x"),
}
STRING_OUTPUT_ROLES = {"hash", "truncate", "redact"}
MASKED_ROLES = STRING_OUTPUT_ROLES | {"faker"}


def _ints(dtype: Any) -> pa.Array:
    return pa.array(np.arange(N, dtype=dtype))


def _typed(values: list[int], typ: pa.DataType) -> pa.Array:
    return pa.array(values, typ)


def _builders() -> dict[str, Any]:
    b: dict[str, Any] = {}
    for name in ("int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64"):
        b[name] = lambda n=name: _ints(getattr(np, n))
    b["string"] = lambda: pa.array([f"k{i}" for i in range(N)])
    b["large_string"] = lambda: pa.array([f"k{i}" for i in range(N)], pa.large_string())
    b["float16"] = lambda: pa.array(np.arange(N, dtype=np.float16))
    b["float32"] = lambda: pa.array(np.arange(N, dtype=np.float32) / 4)
    b["float64"] = lambda: pa.array(np.arange(N, dtype=np.float64) / 4)
    b["bool"] = lambda: pa.array([i % 3 == 0 for i in range(N)])
    b["date32"] = lambda: _typed(list(range(N)), pa.date32())
    b["date64_whole"] = lambda: _typed([i * DAY_MS for i in range(N)], pa.date64())
    b["date64_subday"] = lambda: _typed([i * DAY_MS + 1234 for i in range(N)], pa.date64())
    for unit in ("s", "ms", "us", "ns"):
        b[f"timestamp_{unit}"] = lambda u=unit: _typed(
            [i * 1_000_000 for i in range(N)], pa.timestamp(u)
        )
        b[f"timestamp_{unit}_utc"] = lambda u=unit: _typed(
            [i * 1_000_000 for i in range(N)], pa.timestamp(u, "UTC")
        )
        b[f"duration_{unit}"] = lambda u=unit: _typed(list(range(N)), pa.duration(u))
    for unit in ("s", "ms"):
        b[f"time32_{unit}"] = lambda u=unit: _typed(list(range(N)), pa.time32(u))
    b["time64_us"] = lambda: _typed([i * 1000 for i in range(N)], pa.time64("us"))
    b["time64_ns_aligned"] = lambda: _typed([i * 1000 for i in range(N)], pa.time64("ns"))
    b["time64_ns_nonaligned"] = lambda: _typed([i * 1000 + 7 for i in range(N)], pa.time64("ns"))
    b["null"] = lambda: pa.nulls(N)
    return b


BUILDERS = _builders()
NULLABLE_TYPES = {k for k in BUILDERS if not k.startswith(("int", "uint"))}
NULL_PATTERNS = ("none", "mixed", "all")


def with_nulls(arr: pa.Array, pattern: str) -> pa.Array:
    """`mixed`: chunk 0 entirely null and a few nulls later, so a later chunk is the
    first with data. `all`: every chunk entirely null (typed null column)."""
    if pattern == "none" or arr.type == pa.null():
        return arr
    mask = np.zeros(N, dtype=bool)
    if pattern == "all":
        mask[:] = True
    else:
        mask[:CHUNK] = True
        mask[CHUNK + 3] = True
        mask[N - 1] = True
    return pc.if_else(pa.array(mask), pa.scalar(None, arr.type), arr)


def cell_ids() -> list[str]:
    ids: list[str] = []
    for typ in BUILDERS:
        for role in ROLES:
            for nulls in NULL_PATTERNS if typ in NULLABLE_TYPES else ("none",):
                ids.append(f"{typ}|{role}|{nulls}")
    ids += [
        "string_nonnullable_field|pass|none",
        "string_nonnullable_field|uncfg|none",
        "string_field_metadata|pass|none",
        "string_field_metadata|uncfg|none",
        "uint64_max|pass|none",
    ]
    return ids


def build_source(cell: str, path: str) -> tuple[dict[str, Any], pa.Table]:
    typ, role, nulls = cell.split("|")
    fields: dict[str, pa.Field] = {}
    if typ == "string_nonnullable_field":
        arr = BUILDERS["string"]()
        fields["x"] = pa.field("x", pa.string(), nullable=False)
    elif typ == "string_field_metadata":
        arr = BUILDERS["string"]()
        fields["x"] = pa.field("x", pa.string(), metadata={b"owner": b"b2"})
    elif typ == "uint64_max":
        arr = pa.array([2**64 - 1 - i for i in range(N)], pa.uint64())
    else:
        arr = with_nulls(BUILDERS[typ](), nulls)
    cols = {**support.string_source(), "x": arr}
    base = [support.hash_col("h"), support.redact_col("r")]
    xcfg = ROLES[role]
    cfg = support.make_cfg(base + ([xcfg] if xcfg else []), path=path)
    table = support.table_of(cols, fields)
    pq.write_table(table, path)
    return cfg, table


def describe(table: pa.Table) -> dict[str, Any]:
    return {
        "columns": [[f.name, str(f.type), f.nullable, bool(f.metadata)] for f in table.schema],
        "schema_metadata": sorted(k.decode() for k in (table.schema.metadata or {})),
    }


LIVE_PREFIX = "disp_live_"


def run_lane(fn: Any) -> tuple[dict[str, Any], pa.Table | None]:
    """(record, output table). A failure is recorded as its exception type and code."""
    try:
        result = fn()
    except Exception as exc:
        return support.exc_record(exc), None
    out = result.outputs[support.TABLE]
    rec = describe(out)
    rec["mode"] = result.quality_metrics.get("auto_chunk", {}).get("mode")
    return rec, out


def run_cell(
    cell: str, monkeypatch: pytest.MonkeyPatch, path: str
) -> tuple[pa.Table, dict[str, tuple[dict[str, Any], pa.Table | None]]]:
    """Source table plus lane -> (record, table). `disp_native` is absent when the
    compiled companion is not installed."""
    cfg, src = build_source(cell, path)
    lanes = {
        "legacy": run_lane(lambda: support.run_legacy(cfg, src)),
        "full": run_lane(lambda: support.run_full_frame(cfg, src)),
    }
    for route in ("native", "oracle"):
        if route == "native" and not support.COMPANION_PRESENT:
            continue
        with monkeypatch.context() as mp:
            if route == "oracle":
                support.remove_companion(mp)
            with support.b1_as_the_lane(mp):
                lanes[f"disp_{route}"] = run_lane(lambda: support.run_default(cfg, src))
            # The shipped lane, no patch: test 0's fixture lane must not hide a
            # difference between B1's entry as the plan calls it and what ships.
            lanes[f"{LIVE_PREFIX}{route}"] = run_lane(lambda: support.run_default(cfg, src))
    return src, lanes


def record_cell(cell: str, monkeypatch: pytest.MonkeyPatch, path: str) -> dict[str, Any]:
    _, lanes = run_cell(cell, monkeypatch, path)
    return {lane: rec for lane, (rec, _table) in lanes.items() if not lane.startswith(LIVE_PREFIX)}
