"""Warehouse-export fixtures for the Pattern-E fidelity reuse proof (S0).

Single source of truth for the warehouse-export fidelity matrix. Each entry
reproduces the physical Arrow/Parquet encoding a cloud warehouse emits when it
runs its native ``UNLOAD`` / ``COPY INTO ... FORMAT PARQUET`` export, which is
the Pattern-E ingestion path (warehouse exports Parquet to a bucket -> Decoy
reads it through the existing S3 Parquet input, unchanged masking seam).

The matrix doc ``docs/records/2026-10-23-warehouse-export-fidelity-matrix.md``
is generated from / kept consistent with ``WAREHOUSE_TYPES`` here (one place per
fact). Tests in ``test_warehouse_export_fidelity.py`` iterate these entries.

Export-encoding sources (cited per the use-established-methodology rule; the
exact physical encoding must still be confirmed against a real export in S2, so
rows carry an honest ``confidence`` flag):

* Redshift ``UNLOAD ... FORMAT PARQUET``:
  https://docs.aws.amazon.com/redshift/latest/dg/r_UNLOAD.html -- Redshift maps
  SMALLINT/INTEGER/BIGINT to Parquet INT 16/32/64, DECIMAL to Parquet decimal,
  REAL/DOUBLE to float/double, BOOLEAN to bool, CHAR/VARCHAR to byte array
  (UTF-8), DATE to date, TIMESTAMP to microsecond timestamp, TIMESTAMPTZ to a
  UTC-normalized timestamp, and SUPER to a JSON/utf-8 string.
* Snowflake ``COPY INTO <stage> ... FILE_FORMAT=(TYPE=PARQUET)``:
  https://docs.snowflake.com/en/sql-reference/sql/copy-into-location -- NUMBER
  to Parquet decimal (default NUMBER(38,0)), FLOAT to double, VARCHAR to string,
  BOOLEAN to bool, DATE to date, TIMESTAMP_NTZ to a tz-naive timestamp,
  TIMESTAMP_TZ/LTZ to a UTC timestamp, and VARIANT/OBJECT/ARRAY serialized to a
  JSON string.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext

import pyarrow as pa

_ROWS = 6  # small, fixed; every column is this length with one null row (index 3)
_NULL_ROW = 3


@dataclass(frozen=True)
class WarehouseType:
    """One (warehouse source type -> Parquet export encoding) fidelity case.

    ``disposition`` is the EXPECTED engine disposition, asserted by the tests
    (observed 2026-10-23, not assumed):

    * ``"faithful"``       -- the passthrough output column is byte-identical
      (exact Arrow type AND values) to the input; the Pattern-E reuse win.
    * ``"faithful-value"`` -- values (and, for decimals, scale) are preserved,
      but the logical type metadata is value-derived through the masking seam:
      decimal precision narrows to the minimum that fits the data (e.g. a
      DECIMAL(18,2) column of small values emits decimal128(6,2)). No value is
      lost; the output schema's decimal precision is data-derived, which S1 must
      document so a downstream load is not surprised by a tighter precision.
    * ``"gap"``            -- does not round-trip losslessly through the engine;
      pinned with a coded reason, rejected at platform admission
      (preserve-or-reject). None observed among the representative passthrough
      set -- the reuse win holds broadly.

    ``confidence`` is honest about the fixture itself: ``"encoding-confirmed"``
    when the Arrow/Parquet encoding is the one the warehouse provably emits, or
    ``"encoding-assumed"`` when it must still be confirmed against a real
    warehouse export in S2.
    """

    key: str
    warehouse: str  # "redshift" | "snowflake"
    source_type: str  # the warehouse's own type name
    encoding: str  # the Arrow/Parquet encoding it exports as
    builder: Callable[[], pa.Array]
    disposition: str  # "faithful" | "gap"
    confidence: str
    note: str = ""


def _with_null(values: list, pa_type: pa.DataType) -> pa.Array:
    """Build an array of length _ROWS with a null at _NULL_ROW."""
    assert len(values) == _ROWS
    masked = [None if i == _NULL_ROW else v for i, v in enumerate(values)]
    return pa.array(masked, type=pa_type)


def _ints(width: pa.DataType, base: int = 1) -> Callable[[], pa.Array]:
    return lambda: _with_null([base + i for i in range(_ROWS)], width)


def _full_width_decimal(precision: int, scale: int) -> Decimal:
    """The max-magnitude value at (precision, scale), built as a literal so no
    Decimal arithmetic rounds it against the default 28-digit context."""
    int_digits = precision - scale
    text = "9" * int_digits if int_digits else "0"
    if scale:
        text += "." + "9" * scale
    return Decimal(text)


def _dec_builder(precision: int, scale: int, pa_type: pa.DataType) -> Callable[[], pa.Array]:
    def build() -> pa.Array:
        with localcontext() as ctx:
            # Raise precision so arithmetic on large magnitudes never renormalizes
            # to the 28-digit default (which would push 10^38-1 up to 10^38).
            ctx.prec = precision + 5
            unit = Decimal(1).scaleb(-scale)
            vals = [Decimal(i + 1) * unit for i in range(_ROWS)]
            vals[-1] = _full_width_decimal(precision, scale)  # exercise full width
        return _with_null(vals, pa_type)

    return build


def _decimal(precision: int, scale: int) -> Callable[[], pa.Array]:
    return _dec_builder(precision, scale, pa.decimal128(precision, scale))


def _decimal256(precision: int, scale: int) -> Callable[[], pa.Array]:
    return _dec_builder(precision, scale, pa.decimal256(precision, scale))


def _floats(width: pa.DataType) -> Callable[[], pa.Array]:
    return lambda: _with_null([0.5 + i for i in range(_ROWS)], width)


def _bools() -> Callable[[], pa.Array]:
    return lambda: _with_null([i % 2 == 0 for i in range(_ROWS)], pa.bool_())


def _strings(prefix: str) -> Callable[[], pa.Array]:
    # includes unicode + empty string to exercise encoding fidelity
    def build() -> pa.Array:
        vals = [f"{prefix}-{i}" for i in range(_ROWS)]
        vals[0] = ""  # empty string is distinct from null
        vals[1] = "café-é中文"  # unicode
        return _with_null(vals, pa.string())

    return build


def _dates() -> Callable[[], pa.Array]:
    return lambda: _with_null([dt.date(2024, 1, 1 + i) for i in range(_ROWS)], pa.date32())


def _timestamps(unit: str, tz: str | None) -> Callable[[], pa.Array]:
    return lambda: _with_null(list(range(1, _ROWS + 1)), pa.timestamp(unit, tz))


def _json_strings() -> Callable[[], pa.Array]:
    """Semi-structured (SUPER / VARIANT / OBJECT / ARRAY) exported as a JSON string."""

    def build() -> pa.Array:
        vals = [
            json.dumps({"id": i, "tags": [f"t{i}", f"t{i + 1}"], "nested": {"k": i}})
            for i in range(_ROWS)
        ]
        return _with_null(vals, pa.string())

    return build


def _binary() -> Callable[[], pa.Array]:
    return lambda: _with_null([bytes([i, i + 1, i + 2]) for i in range(_ROWS)], pa.binary())


# The matrix. "faithful" rows are the Pattern-E reuse claim; "gap" rows are the
# preserve-or-reject boundary the tests pin observationally.
WAREHOUSE_TYPES: tuple[WarehouseType, ...] = (
    # ---- Redshift UNLOAD FORMAT PARQUET -----------------------------------
    # These fixtures CONTAIN a null (row 3), so they exercise the pandas
    # int+null -> float64 widening (values preserved below 2**53). A null-FREE
    # integer column keeps its exact type (test_null_free_integer_keeps_exact_type),
    # and a null-bearing integer >= 2**53 loses precision -- the gap pinned by
    # test_null_bearing_large_integer_loses_precision_is_a_gap.
    WarehouseType(
        "rs_smallint",
        "redshift",
        "SMALLINT",
        "int16",
        _ints(pa.int16()),
        "faithful-value",
        "encoding-confirmed",
        "null-bearing integer widens to float64 (values preserved below 2**53).",
    ),
    WarehouseType(
        "rs_integer",
        "redshift",
        "INTEGER",
        "int32",
        _ints(pa.int32()),
        "faithful-value",
        "encoding-confirmed",
        "null-bearing integer widens to float64 (values preserved below 2**53).",
    ),
    WarehouseType(
        "rs_bigint",
        "redshift",
        "BIGINT",
        "int64",
        _ints(pa.int64()),
        "faithful-value",
        "encoding-confirmed",
        "null-bearing integer widens to float64; magnitudes >= 2**53 lose "
        "precision (gap); null-free stays int64.",
    ),
    WarehouseType(
        "rs_decimal_18_2",
        "redshift",
        "DECIMAL(18,2)",
        "decimal128(18,2)",
        _decimal(18, 2),
        "faithful-value",
        "encoding-confirmed",
        "decimal precision is value-derived through the seam (scale kept).",
    ),
    WarehouseType(
        "rs_decimal_38_0",
        "redshift",
        "DECIMAL(38,0)",
        "decimal128(38,0)",
        _decimal(38, 0),
        "faithful-value",
        "encoding-confirmed",
        "decimal precision is value-derived through the seam (scale kept).",
    ),
    WarehouseType(
        "rs_real",
        "redshift",
        "REAL",
        "float32",
        _floats(pa.float32()),
        "faithful",
        "encoding-confirmed",
    ),
    WarehouseType(
        "rs_double",
        "redshift",
        "DOUBLE PRECISION",
        "float64",
        _floats(pa.float64()),
        "faithful",
        "encoding-confirmed",
    ),
    WarehouseType(
        "rs_boolean", "redshift", "BOOLEAN", "bool", _bools(), "faithful", "encoding-confirmed"
    ),
    WarehouseType(
        "rs_varchar",
        "redshift",
        "VARCHAR",
        "string(utf8)",
        _strings("rs"),
        "faithful",
        "encoding-confirmed",
    ),
    WarehouseType(
        "rs_date", "redshift", "DATE", "date32", _dates(), "faithful", "encoding-confirmed"
    ),
    WarehouseType(
        "rs_timestamp",
        "redshift",
        "TIMESTAMP",
        "timestamp(us, naive)",
        _timestamps("us", None),
        "faithful",
        "encoding-confirmed",
    ),
    WarehouseType(
        "rs_timestamptz",
        "redshift",
        "TIMESTAMPTZ",
        "timestamp(us, UTC)",
        _timestamps("us", "UTC"),
        "faithful",
        "encoding-confirmed",
    ),
    WarehouseType(
        "rs_super",
        "redshift",
        "SUPER",
        "string(json)",
        _json_strings(),
        "faithful",
        "encoding-assumed",
        "SUPER exports as a JSON string; masking INSIDE the JSON is a "
        "separate capability (text_mask/nested), out of S0 scope.",
    ),
    WarehouseType(
        "rs_varbyte", "redshift", "VARBYTE", "binary", _binary(), "faithful", "encoding-assumed"
    ),
    # ---- Snowflake COPY INTO FILE_FORMAT=(TYPE=PARQUET) -------------------
    WarehouseType(
        "sf_number_38_0",
        "snowflake",
        "NUMBER(38,0)",
        "decimal128(38,0)",
        _decimal(38, 0),
        "faithful-value",
        "encoding-confirmed",
        "decimal precision is value-derived through the seam (scale kept).",
    ),
    WarehouseType(
        "sf_number_18_4",
        "snowflake",
        "NUMBER(18,4)",
        "decimal128(18,4)",
        _decimal(18, 4),
        "faithful-value",
        "encoding-confirmed",
        "decimal precision is value-derived through the seam (scale kept).",
    ),
    WarehouseType(
        "sf_float",
        "snowflake",
        "FLOAT",
        "float64",
        _floats(pa.float64()),
        "faithful",
        "encoding-confirmed",
    ),
    WarehouseType(
        "sf_varchar",
        "snowflake",
        "VARCHAR",
        "string(utf8)",
        _strings("sf"),
        "faithful",
        "encoding-confirmed",
    ),
    WarehouseType(
        "sf_boolean", "snowflake", "BOOLEAN", "bool", _bools(), "faithful", "encoding-confirmed"
    ),
    WarehouseType(
        "sf_date", "snowflake", "DATE", "date32", _dates(), "faithful", "encoding-confirmed"
    ),
    WarehouseType(
        "sf_timestamp_ntz",
        "snowflake",
        "TIMESTAMP_NTZ",
        "timestamp(ns, naive)",
        _timestamps("ns", None),
        "faithful",
        "encoding-assumed",
        "Snowflake TIMESTAMP_NTZ exports at up to ns precision.",
    ),
    WarehouseType(
        "sf_timestamp_tz",
        "snowflake",
        "TIMESTAMP_TZ",
        "timestamp(us, UTC)",
        _timestamps("us", "UTC"),
        "faithful",
        "encoding-assumed",
    ),
    WarehouseType(
        "sf_variant",
        "snowflake",
        "VARIANT",
        "string(json)",
        _json_strings(),
        "faithful",
        "encoding-assumed",
        "VARIANT/OBJECT/ARRAY serialize to a JSON string on Parquet export.",
    ),
    WarehouseType(
        "sf_binary", "snowflake", "BINARY", "binary", _binary(), "faithful", "encoding-assumed"
    ),
    # ---- Boundary case: precision > 38 (decimal256) -----------------------
    # Observed 2026-10-23: PASSTHROUGH of a decimal256(40,0) column round-trips
    # faithfully (exact type + values) through run_pipeline -- it is NOT a
    # passthrough gap. The >38-precision ceiling the postgres-input primitive
    # pins (postgres_snapshot._NUMERIC_MAX_PRECISION=38) is about LOSSLESS
    # NUMERIC MASKING / DuckDB decimal handling, not about carrying the column
    # through untouched. Whether a numeric MASKING strategy on a >38-precision
    # column is supported or must reject is a separate question for S2.
    WarehouseType(
        "boundary_decimal256_40_0",
        "snowflake",
        "NUMBER(40,0)",
        "decimal256(40,0)",
        _decimal256(40, 0),
        "faithful-value",
        "encoding-confirmed",
        "passthrough preserves values (decimal256 may narrow to decimal128 "
        "to fit the data); numeric MASKING of >38 precision to be qualified "
        "in S2 (postgres-input pins a 38-digit ceiling).",
    ),
)


def warehouse_type(key: str) -> WarehouseType:
    for wt in WAREHOUSE_TYPES:
        if wt.key == key:
            return wt
    raise KeyError(key)


# --- A relational (FK) warehouse-shaped fixture ----------------------------
# Snowflake-style export: NUMBER ids, VARIANT-as-JSON payload, tz timestamps.


def warehouse_fk_tables() -> tuple[pa.Table, pa.Table]:
    """A parent/child pair shaped like a Snowflake Parquet export.

    customers(customer_id NUMBER PK, profile VARIANT, created_at TIMESTAMP_TZ)
    orders(order_id NUMBER PK, customer_id NUMBER FK, amount NUMBER(18,2),
           order_date DATE)
    FK orders.customer_id -> customers.customer_id, no orphans.
    """
    n_cust, n_ord = 8, 20
    customers = pa.table(
        {
            "customer_id": pa.array([1000 + i for i in range(n_cust)], pa.decimal128(38, 0)),
            "profile": pa.array(
                [json.dumps({"tier": i % 3, "flags": [f"f{i}"]}) for i in range(n_cust)],
                pa.string(),
            ),
            "created_at": pa.array(list(range(1, n_cust + 1)), pa.timestamp("us", "UTC")),
        }
    )
    orders = pa.table(
        {
            "order_id": pa.array([5000 + i for i in range(n_ord)], pa.decimal128(38, 0)),
            "customer_id": pa.array(
                [Decimal(1000 + (i % n_cust)) for i in range(n_ord)], pa.decimal128(38, 0)
            ),
            "amount": pa.array(
                [Decimal(i + 1) * Decimal("0.01") * Decimal(997) for i in range(n_ord)],
                pa.decimal128(18, 2),
            ),
            "order_date": pa.array(
                [dt.date(2024, 6, 1 + (i % 28)) for i in range(n_ord)], pa.date32()
            ),
        }
    )
    return customers, orders
