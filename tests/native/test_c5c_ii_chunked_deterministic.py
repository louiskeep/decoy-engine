"""C5c-ii: deterministic Faker over bool/int/uint on the chunked native route.

A deterministic Faker column keys from the SOURCE value, so it admits natively only from a
producer that guarantees the stream schema (a `_FixedSchemaChunks`) AND whose schema metadata is
in the closed allowlist. The admitted case runs the native leg with the chunked oracle poisoned,
so a silent reroute fails, and compares byte-for-byte with a forced-oracle leg over the same
chunks. The same data from an ordinary list declines to the oracle (no producer guarantee).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution._chunked_input import fixed_schema_chunks_from_resident
from decoy_engine.execution.native import _chunked_entry
from tests.native._b8_support import (
    FORCE,
    Run,
    assert_same_as_oracle,
    identical,
    run_one,
    with_force,
)
from tests.native._c5c_i_support import SIGNED, UNSIGNED, int_table, type_id, typed_array
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    column_values,
    faker_col,
    force_oracle,
    key_provider,
    make_config,
    passthrough,
    split,
)

DET_ADMITTED = [*SIGNED, *UNSIGNED, pa.bool_()]
GS = {"unconfigured_column_policy": "warn"}


@contextmanager
def poisoned_chunked_oracle() -> Iterator[None]:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the chunked oracle leg ran on a table that must stay native")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_chunked_entry, "_oracle_route", boom)
        yield


def run_trusted(config: dict[str, Any], source: pa.Table, size: int) -> Run:
    """Run `run_mask_chunked` over a trusted resident-slice producer (the C5c-ii admit path)."""
    sink: list[Any] = []
    ev: list[Any] = []
    out = list(
        run_mask_chunked(
            config,
            fixed_schema_chunks_from_resident(source, size),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            chunk_result_sink=sink,
            route_evidence_sink=ev,
        )
    )
    return Run(out, sink, ev, frozenset())


def forced_oracle(cols: list[dict[str, Any]], source: pa.Table, size: int) -> Run:
    return run_one(
        make_config([*cols, force_oracle(FORCE)], global_settings=GS),
        [with_force(c) for c in split(source, size)],
    )


def pandas_table(typ: pa.DataType, values: list[Any]) -> pa.Table:
    """A resident table carrying the real `b"pandas"` schema metadata (allowlist shape 2)."""
    arrays = [pa.array(values, type=typ), pa.array(list(range(len(values))), pa.int64())]
    table = pa.Table.from_arrays(arrays, names=["f", "p"])
    return pa.Table.from_pandas(table.to_pandas(), preserve_index=False)


# ---------------------------------------------------------------------------
# 1. Every admitted family admits natively and matches the oracle.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("nulls", [False, True], ids=["no_nulls", "nulls"])
@pytest.mark.parametrize("typ", DET_ADMITTED, ids=type_id)
def test_1_admits_and_matches_the_oracle(typ: pa.DataType, nulls: bool) -> None:
    source = int_table(typ, typed_array(typ, nulls=nulls))
    cols = [faker_col("f"), passthrough("p")]
    with poisoned_chunked_oracle():
        native = run_trusted(make_config(cols, global_settings=GS), source, 5)
    forced = forced_oracle(cols, source, 5)
    assert native.ev[0].node_routes[0].route == "native_pool"
    assert_same_as_oracle(native, forced)
    assert {o.schema.field("f").type for o in native.out} == {pa.string()}


@NEEDS_COMPANION
def test_1_pandas_metadata_shape_two_admits() -> None:
    source = pandas_table(pa.int64(), [5, 9, 5, 12, 9, 5])
    cols = [faker_col("f"), passthrough("p")]
    with poisoned_chunked_oracle():
        native = run_trusted(make_config(cols, global_settings=GS), source, 4)
    forced = forced_oracle(cols, source, 4)
    assert native.ev[0].native_admitted is True
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", [pa.int64(), pa.uint64()], ids=type_id)
def test_1_integer_boundaries_above_2_53_and_2_63(typ: pa.DataType) -> None:
    source = int_table(typ, typed_array(typ, nulls=True))
    cols = [faker_col("f"), passthrough("p")]
    with poisoned_chunked_oracle():
        native = run_trusted(make_config(cols, global_settings=GS), source, 5)
    forced = forced_oracle(cols, source, 5)
    # boundary_values includes the type max (>2**53 for int64, >2**63 / uint64 max for uint64).
    assert_same_as_oracle(native, forced)


# ---------------------------------------------------------------------------
# 5. Same data, different provenance: a trusted producer admits, an ordinary list declines.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_5_same_data_different_provenance() -> None:
    source = int_table(pa.int64(), typed_array(pa.int64(), nulls=True))
    cols = [faker_col("f"), passthrough("p")]
    config = make_config(cols, global_settings=GS)
    with poisoned_chunked_oracle():
        native = run_trusted(config, source, 5)
    listed = run_one(config, split(source, 5))
    assert native.ev[0].native_admitted is True
    assert listed.ev[0].native_admitted is False
    assert "faker_conversion_schema_not_guaranteed" in (listed.ev[0].reroute_reason or "")
    # Same draws either way: declining only changes the backend, never the values.
    assert column_values(native.out, "f") == column_values(listed.out, "f")


# ---------------------------------------------------------------------------
# Degenerate output pins to string on both chunked legs (option A).
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", [pa.int64(), pa.bool_()], ids=type_id)
def test_degenerate_all_null_pins_to_string(typ: pa.DataType) -> None:
    source = int_table(typ, pa.nulls(6, typ))
    cols = [faker_col("f"), passthrough("p")]
    with poisoned_chunked_oracle():
        native = run_trusted(make_config(cols, global_settings=GS), source, 3)
    forced = forced_oracle(cols, source, 3)
    assert native.ev[0].native_admitted is True
    assert {o.schema.field("f").type for o in native.out} == {pa.string()}
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
def test_degenerate_empty_leading_and_interior_chunk_pins_to_string() -> None:
    source = int_table(pa.int64(), typed_array(pa.int64(), nulls=True))
    cols = [faker_col("f"), passthrough("p")]
    # A chunk size equal to the row count leaves a single chunk; a size that over-divides makes an
    # empty trailing slice. Place an all-null block in the interior via a null-heavy source.
    with poisoned_chunked_oracle():
        native = run_trusted(make_config(cols, global_settings=GS), source, 4)
    forced = forced_oracle(cols, source, 4)
    assert_same_as_oracle(native, forced)
    assert all(o.schema.field("f").type == pa.string() for o in native.out)


# ---------------------------------------------------------------------------
# 2/3. Metadata shapes outside the allowlist decline to the oracle (even when guaranteed).
# ---------------------------------------------------------------------------


def _declines_to_oracle(source: pa.Table, reason_prefix: str) -> None:
    cols = [faker_col("f"), passthrough("p")]
    native = run_trusted(make_config(cols, global_settings=GS), source, 5)
    forced = forced_oracle(cols, source, 5)
    assert native.ev[0].native_admitted is False
    assert reason_prefix in (native.ev[0].reroute_reason or "")
    for got, want in zip(native.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


@NEEDS_COMPANION
def test_2_numpy_type_only_drift_declines_even_when_guaranteed() -> None:
    base = pandas_table(pa.int64(), [1, 2, 3, 4, 5, 6])
    meta = json.loads(base.schema.metadata[b"pandas"])
    for entry in meta["columns"]:
        if entry["name"] == "f":
            entry["numpy_type"] = "bool[pyarrow]"
    drifted = base.replace_schema_metadata({b"pandas": json.dumps(meta).encode("utf-8")})
    _declines_to_oracle(drifted, "faker_conversion_metadata_not_allowlisted:f")


@NEEDS_COMPANION
def test_3_nullable_extension_metadata_declines_on_the_chunked_route() -> None:
    import pandas as pd

    frame = pd.DataFrame({"f": pd.array([1, 2, 3, 4, 5, 6], dtype="Int64"), "p": list(range(6))})
    source = pa.Table.from_pandas(frame, preserve_index=False)
    _declines_to_oracle(source, "faker_conversion_metadata_not_allowlisted:f")
