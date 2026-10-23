"""S0 reuse proof: warehouse Parquet exports mask faithfully through the
unchanged engine seam (Pattern-E).

Plan: ``docs/plans/2026-10-23-warehouse-s0-pattern-e-fidelity-reuse-proof.md``.
Fidelity matrix (kept consistent with ``_warehouse_fixtures.WAREHOUSE_TYPES``):
``docs/records/2026-10-23-warehouse-export-fidelity-matrix.md``.

The Pattern-E premise: a warehouse's native ``UNLOAD`` / ``COPY INTO`` Parquet
export, read through Decoy's existing S3/file Parquet input, masks correctly
through the unchanged seam -- so warehouse support is platform binding/UX, not
new engine code. These tests make that premise evidence-backed, on fixtures
shaped like real Redshift/Snowflake exports, and pin the observed fidelity of
every representative export type. Tests-only; no src change.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.keyprovider import SecretKeyProvider
from tests.integration._warehouse_fixtures import (
    WAREHOUSE_TYPES,
    WarehouseType,
    warehouse_fk_tables,
)

ENGINE_VERSION = "warehouse-s0-fidelity"
_KEY = bytes(range(32))


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_KEY, key_version="v1")


def _validated(raw: dict[str, Any]) -> dict[str, Any]:
    return PipelineConfig.model_validate(raw).model_dump()


def _run(config: dict[str, Any], sources: dict[str, Any], **kwargs: Any) -> ExecutionResult:
    return run_pipeline(
        config, dict(sources), engine_version=ENGINE_VERSION, key_provider=_kp(), **kwargs
    )


def _write(tmp_path: Path, table: pa.Table, name: str = "in") -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _single_col_table(wt: WarehouseType) -> pa.Table:
    """A table with a string column to mask and the warehouse-typed column to pass through."""
    col = wt.builder()
    return pa.table({"s": pa.array([f"user{i}@example.com" for i in range(len(col))]), "c": col})


def _single_table_config(tmp_path: Path, source: Path) -> dict[str, Any]:
    return _validated(
        {
            "version": 1,
            "global_settings": {"seed": 5},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(source)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out")}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "s", "strategy": "hash", "namespace": "ns"},
                        {"name": "c", "strategy": "passthrough"},
                    ],
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# 1. Reader leg: every export encoding round-trips through the Parquet reader.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wt", WAREHOUSE_TYPES, ids=lambda wt: wt.key)
def test_warehouse_export_roundtrips_through_parquet_reader(
    tmp_path: Path, wt: WarehouseType
) -> None:
    """The ingestion reader preserves the exact Arrow type + values of the
    warehouse export (write -> read), before any masking. This isolates the
    read leg of Pattern-E from the masking seam."""
    col = wt.builder()
    table = pa.table({"c": col})
    read_back = pq.read_table(_write(tmp_path, table, wt.key))
    assert read_back.column("c").combine_chunks().equals(col), (
        f"{wt.key}: Parquet read did not preserve the warehouse export column"
    )


# ---------------------------------------------------------------------------
# 2. Masking seam: passthrough of a warehouse-typed column is faithful, and the
#    route decision (byte-estimate on/off) does not change the output.
# ---------------------------------------------------------------------------


def _assert_passthrough_fidelity(wt: WarehouseType, src_col: pa.Array, out_col: pa.Array) -> None:
    src = src_col.combine_chunks()
    out = out_col.combine_chunks()
    if wt.disposition == "faithful":
        assert out.equals(src), f"{wt.key}: passthrough output not byte-identical to input"
    elif wt.disposition == "faithful-value":
        # Values (and scale, for decimals) preserved; logical precision may narrow.
        assert out.to_pylist() == src.to_pylist(), f"{wt.key}: passthrough changed values"
        if pa.types.is_decimal(src.type):
            assert out.type.scale == src.type.scale, f"{wt.key}: decimal scale not preserved"
            assert out.type.precision <= src.type.precision, (
                f"{wt.key}: precision should narrow-or-equal, not widen"
            )
    else:  # pragma: no cover - no gap rows today; guards against a silent reclassification
        pytest.fail(f"{wt.key}: unexpected disposition {wt.disposition!r}")


@pytest.mark.parametrize("wt", WAREHOUSE_TYPES, ids=lambda wt: wt.key)
def test_warehouse_typed_column_passthrough_is_faithful(tmp_path: Path, wt: WarehouseType) -> None:
    table = _single_col_table(wt)
    config = _single_table_config(tmp_path, _write(tmp_path, table, wt.key))
    result = _run(config, {"t": table})
    _assert_passthrough_fidelity(wt, table.column("c"), result.outputs["t"].column("c"))


@pytest.mark.parametrize("wt", WAREHOUSE_TYPES, ids=lambda wt: wt.key)
def test_route_decision_does_not_change_warehouse_passthrough(
    tmp_path: Path, wt: WarehouseType
) -> None:
    """On vs off byte-estimate routing produce identical output for every
    warehouse-typed passthrough column (the house on/off rigor)."""
    table = _single_col_table(wt)
    config = _single_table_config(tmp_path, _write(tmp_path, table, wt.key))
    on = _run(config, {"t": table})
    off = _run(config, {"t": table}, use_byte_estimate_routing=False)
    assert (
        on.outputs["t"]
        .column("c")
        .combine_chunks()
        .equals(off.outputs["t"].column("c").combine_chunks())
    ), f"{wt.key}: route decision changed the passthrough column"


# ---------------------------------------------------------------------------
# 3. The decimal precision-narrowing caveat, pinned explicitly (observed
#    2026-10-23): a DECIMAL(18,2) of small values emits decimal128(<=18, 2).
#    Values and scale are preserved; precision is value-derived. S1 must
#    document this so a downstream load is not surprised by a tighter precision.
# ---------------------------------------------------------------------------


def test_decimal_precision_is_value_derived_scale_preserved(tmp_path: Path) -> None:
    col = pa.array([Decimal("12.34"), Decimal("9.99"), None, Decimal("0.01")], pa.decimal128(18, 2))
    table = pa.table({"s": pa.array(["a", "b", "c", "d"]), "c": col})
    config = _single_table_config(tmp_path, _write(tmp_path, table, "dec"))
    out = _run(config, {"t": table}).outputs["t"].column("c").combine_chunks()
    assert out.to_pylist() == col.to_pylist()  # no value lost
    assert out.type.scale == 2  # scale preserved
    assert out.type.precision <= 18  # precision narrowed to fit the data (value-derived)


# ---------------------------------------------------------------------------
# 3b. Integer + null behavior (observed 2026-10-23): a warehouse INTEGER/BIGINT
#     column is routinely nullable. The pandas seam widens a null-bearing integer
#     to float64. Below 2**53 values are preserved; at/above 2**53 they are
#     silently corrupted -- the one real preserve-or-reject GAP S0 surfaces.
# ---------------------------------------------------------------------------


def _int_passthrough(tmp_path: Path, col: pa.Array) -> pa.Array:
    table = pa.table({"s": pa.array([f"u{i}" for i in range(len(col))]), "c": col})
    config = _single_table_config(tmp_path, _write(tmp_path, table, "int"))
    return _run(config, {"t": table}).outputs["t"].column("c").combine_chunks()


def test_null_free_integer_keeps_exact_type(tmp_path: Path) -> None:
    col = pa.array([1, 2, 3, 2_000_000_000], pa.int32())
    out = _int_passthrough(tmp_path, col)
    assert out.equals(col)  # exact int32, no widening


def test_null_bearing_integer_widens_to_float64_values_preserved(tmp_path: Path) -> None:
    col = pa.array([1, 2, None, 5], pa.int32())
    out = _int_passthrough(tmp_path, col)
    assert pa.types.is_floating(out.type)  # widened by the pandas seam
    assert out.to_pylist() == col.to_pylist()  # values preserved below 2**53 (1 == 1.0)


def test_null_bearing_large_integer_loses_precision_is_a_gap(tmp_path: Path) -> None:
    """Pinned GAP: a null-bearing BIGINT with magnitude >= 2**53 widens to
    float64 and loses precision. S1 must reject (or warn on) a nullable
    large-integer warehouse column; this is the preserve-or-reject boundary.
    The assertion documents the CURRENT lossy behavior; if the engine later
    preserves it (e.g. an Int64 nullable path), this test flips to equality."""
    big = 2**53 + 1
    col = pa.array([big, big + 2, None], pa.int64())
    out = _int_passthrough(tmp_path, col)
    assert pa.types.is_floating(out.type)
    assert out.to_pylist() != col.to_pylist(), (
        "expected float64 precision loss at >= 2**53; if this now holds, the "
        "engine gained a lossless nullable-int path -- update the matrix to faithful"
    )


# ---------------------------------------------------------------------------
# 4. Relational (FK) warehouse export: referential integrity preserved and
#    warehouse-typed passthrough columns survive masking.
# ---------------------------------------------------------------------------


def _fk_config(tmp_path: Path, customers: pa.Table, orders: pa.Table) -> dict[str, Any]:
    paths = {
        "customers": _write(tmp_path, customers, "customers"),
        "orders": _write(tmp_path, orders, "orders"),
    }
    return _validated(
        {
            "version": 1,
            "global_settings": {"seed": 7},
            "sources": {
                k: {"type": "file", "format": "parquet", "path": str(v)} for k, v in paths.items()
            },
            "targets": {
                k: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{k}.out")}
                for k in paths
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "customer_id",
                            "strategy": "faker",
                            "provider": "person_email",
                            "deterministic": True,
                            "namespace": "cust",
                        },
                        {"name": "profile", "strategy": "passthrough"},
                        {"name": "created_at", "strategy": "passthrough"},
                    ],
                },
                {
                    "name": "orders",
                    "columns": [
                        {
                            "name": "customer_id",
                            "strategy": "faker",
                            "provider": "person_email",
                            "deterministic": True,
                            "namespace": "cust",
                        },
                        {"name": "amount", "strategy": "passthrough"},
                        {"name": "order_date", "strategy": "passthrough"},
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "fail",
                    "namespace": "cust",
                }
            ],
        }
    )


def test_warehouse_fk_export_preserves_referential_integrity(tmp_path: Path) -> None:
    customers, orders = warehouse_fk_tables()
    res = _run(_fk_config(tmp_path, customers, orders), {"customers": customers, "orders": orders})
    cust_ids = set(res.outputs["customers"].column("customer_id").to_pylist())
    masked_order_fks = res.outputs["orders"].column("customer_id").to_pylist()
    assert cust_ids, "masked parent keys should be non-empty"
    for fk in masked_order_fks:
        assert fk in cust_ids, "masked orders.customer_id must resolve to a masked customer"
    # the key was actually masked (not passed through)
    assert (
        res.outputs["customers"].column("customer_id").to_pylist()
        != customers.column("customer_id").to_pylist()
    )


def test_warehouse_fk_export_passthrough_columns_survive(tmp_path: Path) -> None:
    customers, orders = warehouse_fk_tables()
    res = _run(_fk_config(tmp_path, customers, orders), {"customers": customers, "orders": orders})
    # JSON VARIANT string + tz timestamp: byte-faithful.
    assert (
        res.outputs["customers"]
        .column("profile")
        .combine_chunks()
        .equals(customers.column("profile").combine_chunks())
    )
    assert (
        res.outputs["customers"]
        .column("created_at")
        .combine_chunks()
        .equals(customers.column("created_at").combine_chunks())
    )
    # date32: byte-faithful; NUMBER(18,2) amount: values + scale preserved.
    assert (
        res.outputs["orders"]
        .column("order_date")
        .combine_chunks()
        .equals(orders.column("order_date").combine_chunks())
    )
    amt_out = res.outputs["orders"].column("amount").combine_chunks()
    assert amt_out.to_pylist() == orders.column("amount").to_pylist()
    assert amt_out.type.scale == 2


def test_warehouse_fk_export_byte_identical_across_runs(tmp_path: Path) -> None:
    customers, orders = warehouse_fk_tables()
    config = _fk_config(tmp_path, customers, orders)
    a = _run(config, {"customers": customers, "orders": orders})
    b = _run(config, {"customers": customers, "orders": orders})
    for name in ("customers", "orders"):
        assert a.outputs[name].equals(b.outputs[name]), f"{name}: not reproducible across runs"


# ---------------------------------------------------------------------------
# 5. Property: decimal passthrough through the ENGINE seam preserves values and
#    scale for any (precision, scale, values) in the decimal128 range (invariant
#    per testing.md: round-trip claims get a property test, not only examples).
#    This is the generative form of the "faithful-value" decimal claim.
# ---------------------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(data=st.data())
def test_decimal_passthrough_preserves_values_and_scale(
    tmp_path_factory: Any, data: st.DataObject
) -> None:
    precision = data.draw(st.integers(min_value=1, max_value=38))
    scale = data.draw(st.integers(min_value=0, max_value=precision))
    n = data.draw(st.integers(min_value=1, max_value=6))
    max_unscaled = 10**precision - 1
    unscaled = data.draw(
        st.lists(
            st.integers(min_value=-max_unscaled, max_value=max_unscaled), min_size=n, max_size=n
        )
    )
    with localcontext() as ctx:
        ctx.prec = precision + 5  # never renormalize against the 28-digit default
        unit = Decimal(1).scaleb(-scale)
        values = [Decimal(u) * unit for u in unscaled]
    col = pa.array(values, pa.decimal128(precision, scale))
    table = pa.table({"s": pa.array([f"u{i}" for i in range(n)]), "c": col})
    tmp = tmp_path_factory.mktemp("dp")
    config = _single_table_config(tmp, _write(tmp, table, "d"))
    out = _run(config, {"t": table}).outputs["t"].column("c").combine_chunks()
    assert out.to_pylist() == col.to_pylist()  # no value lost
    assert pa.types.is_decimal(out.type) and out.type.scale == scale
    assert out.type.precision <= precision  # precision is value-derived (narrow-or-equal)
