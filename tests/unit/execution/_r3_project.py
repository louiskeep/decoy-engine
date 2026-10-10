"""The explicit snapshot projection for the R3 within-route baseline.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 1.
Everything a route produces is compared EXACTLY except the enumerated volatile
fields, whose VALUES differ between identical runs; for those the projection keeps
structure only (key present, value type). Nothing volatile is dropped silently:
`VOLATILE_KEYS` and `VOLATILE_RESULT_FIELDS` name every one.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pyarrow as pa

# `ExecutionResult` fields that are wall-clock measurements.
VOLATILE_RESULT_FIELDS: tuple[str, ...] = ("timings", "boundary_conversion_ms")

# Keys anywhere inside `quality_metrics` whose value is a wall-time / RSS / memory
# measurement, plus any key ending in `_ms` (every timing in the engine is named so).
# Structure (key present + value type) is asserted; the value is not.
VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "boundary_conversion_ms",
        "timings",
        "peak_rss_bytes",
        "rss_bytes",
        "peak_memory_delta_kb",
    }
)
VOLATILE_SUFFIXES: tuple[str, ...] = ("_ms",)

# Hashes of the compiled physical plan fold in the source file path, which is a fresh temp
# directory per run, so the value moves between identical runs. Structure only.
PATH_DEPENDENT_KEYS: frozenset[str] = frozenset({"activation_hash", "plan_hash"})

# The five emitted members of `quality_metrics["execution"]` (`execution_telemetry`).
EXECUTION_TELEMETRY_KEYS: tuple[str, ...] = (
    "execution_mode",
    "route_reason",
    "eviction",
    "outputs_streamed",
    "loaded_fully_in_memory",
)

# Library version stamps pandas writes into Arrow schema metadata.
_LIBRARY_VERSION_KEYS = ("creator", "pandas_version")


def canon(value: Any) -> Any:
    """A JSON-safe, lossless-enough canonical form (NaN, bytes, dates, decimals tagged)."""
    if isinstance(value, float):
        if math.isnan(value):
            return {"__float__": "nan"}
        if math.isinf(value):
            return {"__float__": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, (datetime, date, time)):
        return {"__time__": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, dict):
        return {str(k): canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canon(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return {"__repr__": repr(value)}


def project_table(table: pa.Table) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key, raw in (table.schema.metadata or {}).items():
        name, text = key.decode(), raw.decode()
        if name == "pandas":
            meta = json.loads(text)
            for stamp in _LIBRARY_VERSION_KEYS:
                meta.pop(stamp, None)
            metadata[name] = meta
        else:
            metadata[name] = text
    return {
        "schema": [(f.name, str(f.type), f.nullable) for f in table.schema],
        "metadata": metadata,
        "rows": canon(table.to_pydict()),
    }


def _is_volatile(key: Any) -> bool:
    key = str(key)
    return key in VOLATILE_KEYS or key in PATH_DEPENDENT_KEYS or key.endswith(VOLATILE_SUFFIXES)


def scrub(value: Any) -> Any:
    """Replace each volatile key's value by its type name; recurse everywhere else."""
    if isinstance(value, dict):
        return {
            str(k): ({"__volatile__": type(v).__name__} if _is_volatile(k) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return canon(value)


def project(result: Any) -> dict[str, Any]:
    """The comparison surface of an `ExecutionResult`."""
    metrics = result.quality_metrics
    return {
        "tables": {name: project_table(tbl) for name, tbl in sorted(result.outputs.items())},
        "warnings": [repr(w) for w in result.warnings],
        "row_errors": [repr(r) for r in result.row_errors],
        "table_kinds": dict(result.table_kinds),
        "execution": {k: metrics.get("execution", {}).get(k) for k in EXECUTION_TELEMETRY_KEYS},
        "execution_extra_keys": sorted(
            set(metrics.get("execution", {})) - set(EXECUTION_TELEMETRY_KEYS)
        ),
        "execution_plan": scrub(metrics.get("execution_plan")),
        "fidelity_reports": scrub(metrics.get("fidelity_reports")),
        "quality_metrics": scrub(metrics),
        "volatile_structure": {
            "timings": type(result.timings).__name__,
            "boundary_conversion_ms": type(result.boundary_conversion_ms).__name__,
            "timing_count_positive": len(result.timings) > 0,
        },
    }
