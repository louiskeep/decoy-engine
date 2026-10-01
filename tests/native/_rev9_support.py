"""Shared builders for the B1 revision 9 tests (carried passthrough columns).

`SHAPES` lists the passthrough values of acceptance test 15: each one is a value
the pandas round trip refuses or alters, and `run_mask_chunked` must return it
exactly. `bad` is a three-row array (row 1 null) holding the value; `good` is a
valid array of the same Arrow type, used for the other chunks of a stream.

`public` pins what the unchanged public oracle `run_mask_pipeline_chunked`
does with the shape: the class name of the exception it raises, `"altered"` for
the `-2^63` shapes (row 0 yielded as null), or `"exact"` when it returns the
value unchanged. The time-zone shapes are `"exact"`: the plan's guarantee 1
lists them as refused, but neither pandas 2.3.3 nor pyarrow 24.0.0 and 25.0.1
refuses them (probe `tz3.py` in the build scratchpad), so they still guard
route equality and exactness without being an exception to the oracle. Pinned under
pandas 2.3.3 with pyarrow 24.0.0 (project venv) and 25.0.1 (companion venv); an
upgrade that changes a pin fails the test and is re-pinned by review.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
)

I64MIN = -(2**63)
_FAR_SECONDS = 253402300799 + 86400  # one day past 9999-12-31T23:59:59Z
_UNIT_MULT = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}


@dataclass(frozen=True)
class Shape:
    name: str
    bad: pa.Array
    good: pa.Array
    public: str


def _dict(indices: list[Any], values: list[Any], index_type: Any = None) -> pa.Array:
    index_type = index_type or pa.int32()
    return pa.DictionaryArray.from_arrays(pa.array(indices, index_type), pa.array(values))


def _tz_shapes() -> list[Shape]:
    out: list[Shape] = []
    for unit in ("s", "ms", "us"):
        for tz in ("America/New_York", "+05:30", "Europe/London"):
            t = pa.timestamp(unit, tz=tz)
            out.append(
                Shape(
                    f"ts_{unit}_{tz}_far",
                    pa.array([_FAR_SECONDS * _UNIT_MULT[unit], None, 0], t),
                    pa.array([1, None, 2], t),
                    "exact",
                )
            )
    # pandas' nanosecond bounds: the local time of the extreme value leaves 1677..2262.
    for tz, value in (
        ("America/New_York", -(2**63) + 1),
        ("+05:30", 2**63 - 1),
        ("Europe/London", -(2**63) + 1),
    ):
        t = pa.timestamp("ns", tz=tz)
        out.append(
            Shape(
                f"ts_ns_{tz}_edge",
                pa.array([value, None, 0], t),
                pa.array([1, None, 2], t),
                "exact",
            )
        )
    return out


def _min_shapes() -> list[Shape]:
    out: list[Shape] = []
    for kind in ("timestamp", "duration"):
        for unit in ("s", "ms", "us", "ns"):
            t = pa.timestamp(unit) if kind == "timestamp" else pa.duration(unit)
            out.append(
                Shape(
                    f"{kind}_{unit}_min",
                    pa.array([I64MIN, None, 7], t),
                    pa.array([1, None, 2], t),
                    "altered",
                )
            )
    return out


def _build_shapes() -> list[Shape]:
    shapes = [
        Shape(
            "time64ns_unaligned",
            pa.array([1000, None, 3001], pa.time64("ns")),
            pa.array([1000, None, 3000], pa.time64("ns")),
            "ArrowInvalid",
        ),
        Shape(
            "date32_far",
            pa.array([2**31 - 1, None, 0], pa.date32()),
            pa.array([0, None, 1], pa.date32()),
            "ValueError",
        ),
        Shape(
            "date64_far",
            pa.array([2**62, None, 0], pa.date64()),
            pa.array([0, None, 86400000], pa.date64()),
            "ValueError",
        ),
        Shape(
            "time32s_out_of_day",
            pa.array([90000, None, -1], pa.time32("s")),
            pa.array([0, None, 3600], pa.time32("s")),
            "ValueError",
        ),
        Shape(
            "time32ms_out_of_day",
            pa.array([90000 * 1000, None, -1], pa.time32("ms")),
            pa.array([0, None, 3600], pa.time32("ms")),
            "ValueError",
        ),
        Shape(
            "time64us_out_of_day",
            pa.array([90000 * 10**6, None, -1], pa.time64("us")),
            pa.array([0, None, 3600], pa.time64("us")),
            "ValueError",
        ),
        Shape(
            "time64ns_out_of_day",
            pa.array([90000 * 10**9, None, -1], pa.time64("ns")),
            pa.array([0, None, 3_600_000], pa.time64("ns")),
            "ValueError",
        ),
        Shape(
            "dict_null_referenced",
            _dict([0, 1, None], ["a", None]),
            _dict([0, None, 1], ["a", "b"]),
            "ValueError",
        ),
        Shape(
            "dict_null_unreferenced",
            _dict([0, None, 1], ["a", "b", None]),
            _dict([0, None, 1], ["a", "b"]),
            "ValueError",
        ),
        Shape(
            "dict_duplicate",
            _dict([0, 1, None], ["a", "a"]),
            _dict([0, None, 1], ["a", "b"]),
            "ValueError",
        ),
        Shape(
            "dict_nan",
            _dict([0, 1, None], [1.0, float("nan")]),
            _dict([0, None, 1], [1.0, 2.0]),
            "ValueError",
        ),
        Shape(
            "dict_uint64_indices",
            _dict([0, 1, None], ["a", "b"], pa.uint64()),
            _dict([0, None, 1], ["a", "b"], pa.uint64()),
            "ArrowTypeError",
        ),
        *_tz_shapes(),
        *_min_shapes(),
        Shape(
            "list_int",
            pa.array([[1], None, [2, 3]], pa.list_(pa.int64())),
            pa.array([[4], None, [5]], pa.list_(pa.int64())),
            "TypeError",
        ),
        Shape(
            "struct",
            pa.array([{"a": 1}, None, {"a": 2}], pa.struct([("a", pa.int64())])),
            pa.array([{"a": 3}, None, {"a": 4}], pa.struct([("a", pa.int64())])),
            "TypeError",
        ),
        Shape(
            "map",
            pa.array([[("k", 1)], None, [("j", 2)]], pa.map_(pa.string(), pa.int64())),
            pa.array([[("m", 3)], None, [("n", 4)]], pa.map_(pa.string(), pa.int64())),
            "TypeError",
        ),
    ]
    return shapes


SHAPES: list[Shape] = _build_shapes()
SHAPE_IDS = [s.name for s in SHAPES]
BY_NAME = {s.name: s for s in SHAPES}


def stream(shape: Shape, pos: int, *, with_hash: bool = False, name: str = "x") -> list[pa.Table]:
    """Three chunks of `s` (and `name`); chunk `pos` holds the shape's bad value."""
    chunks = []
    for i in range(3):
        cols: dict[str, Any] = {
            "s": pa.array([f"s{i}a", f"s{i}b", f"s{i}c"]),
            name: shape.bad if i == pos else shape.good,
        }
        if with_hash:
            cols["h"] = pa.array([f"h{i}a", f"h{i}b", f"h{i}c"])
        chunks.append(pa.table(cols))
    return chunks


def config_for(*, configured: bool, with_hash: bool = False, name: str = "x") -> dict[str, Any]:
    cols = [redact("s")]
    if with_hash:
        cols.append(hash_col("h"))
    if configured:
        cols.append(passthrough(name))
    return make_config(cols)


@contextmanager
def companion_missing(monkeypatch: Any) -> Iterator[None]:
    """Force the oracle route with the stock adapter: a hash column then has no kernel."""
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
    yield


def _valid_values(column: Any) -> Any:
    arr = column if isinstance(column, pa.ChunkedArray) else pa.chunked_array([column])
    return arr.filter(pc.is_valid(arr))


def ipc_bytes(column: Any, name: str = "x") -> bytes:
    """Exact serialized form of one column's valid values (NaN and out-of-range values compare
    bitwise; the bytes under a null slot are not data and are left out)."""
    table = pa.table({name: _valid_values(column)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def same_column(a: Any, b: Any) -> bool:
    """Same type, same null positions, same valid values."""
    if a.type != b.type or len(a) != len(b):
        return False
    nulls_a = pc.is_null(a).to_pylist()
    return nulls_a == pc.is_null(b).to_pylist() and ipc_bytes(a) == ipc_bytes(b)


def run_entry(
    config: dict[str, Any], chunks: list[pa.Table], **kw: Any
) -> tuple[list[pa.Table], list[Any], list[Any]]:
    from decoy_engine import run_mask_chunked

    sink: list[Any] = []
    ev: list[Any] = []
    out = list(
        run_mask_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            chunk_result_sink=sink,
            route_evidence_sink=ev,
            **kw,
        )
    )
    return out, sink, ev


def run_public(config: dict[str, Any], chunks: list[pa.Table], **kw: Any) -> list[pa.Table]:
    from decoy_engine import run_mask_pipeline_chunked

    return list(
        run_mask_pipeline_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            **kw,
        )
    )
