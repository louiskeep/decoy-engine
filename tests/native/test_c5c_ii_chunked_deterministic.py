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
from decoy_engine.execution._faker_degenerate_pin import pin_degenerate_to_string
from decoy_engine.execution.native import _chunked_entry
from decoy_engine.providers_v2 import get_default_registry
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
@pytest.mark.parametrize(
    ("values", "size", "degenerate_idx"),
    [
        ([None, None, 1, 2, 3, 4], 2, 0),
        ([1, 2, None, None, 3, 4], 2, 1),
        ([1, 2, 3, 4, None, None], 2, 2),
    ],
    ids=["leading", "interior", "trailing"],
)
def test_degenerate_all_null_chunk_by_position_pins_to_string(
    values: list[Any], size: int, degenerate_idx: int
) -> None:
    source = int_table(pa.int64(), pa.array(values, pa.int64()))
    cols = [faker_col("f"), passthrough("p")]
    with poisoned_chunked_oracle():
        native = run_trusted(make_config(cols, global_settings=GS), source, size)
    forced = forced_oracle(cols, source, size)
    assert native.ev[0].native_admitted is True
    # The targeted chunk really is entirely null; the pin still applies, and the other chunks
    # carry values. Assert the actual chunk shape, not just that the run stayed native.
    degenerate = native.out[degenerate_idx]
    assert degenerate.num_rows == size
    assert degenerate.column("f").null_count == degenerate.num_rows
    assert all(o.schema.field("f").type == pa.string() for o in native.out)
    assert_same_as_oracle(native, forced)
    # ROUTE-OUTPUT-CONTRACT: the chunked leg carries no `b"pandas"` schema metadata; the full-frame
    # pin stamps the pandas string shape. The two route schemas must NOT be equal metadata-inclusive.
    assert b"pandas" not in (degenerate.schema.metadata or {})
    full_frame = pin_degenerate_to_string(pandas_table(pa.int64(), [None] * size), frozenset({"f"}))
    assert full_frame.schema.metadata is not None and b"pandas" in full_frame.schema.metadata
    assert not degenerate.schema.equals(full_frame.schema, check_metadata=True)


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


# ---------------------------------------------------------------------------
# An allowlisted provider name overridden to return non-string pool values fails closed on
# every chunked path (Codex final gate HIGH-1). The degenerate pin casts the column to `string`
# by the provider-name allowlist, so without a pool guard a non-string override would silently
# stringify value-bearing output on the chunked/oracle leg and for an ordinary iterable, diverging
# from the whole-frame run, which keeps the natural type. `reject_nonstring_deterministic_pools`
# resolves the pin-eligible pools eagerly on both legs and fails closed before any write.
# ---------------------------------------------------------------------------

DET_POOL_CODE = "chunked_faker_deterministic_pool_not_string"
_INT_SOURCE = int_table(pa.int64(), pa.array([1, 2, 3, 4, 5, 6], pa.int64()))


class _IntAdapter:
    """A poolable adapter whose pool values are integers, not strings."""

    backend_type = "test_int"
    backend_version = "1"

    def __init__(self, provider: str) -> None:
        self._provider = provider

    def generate(self, provider: str, *, spec: Any, source_value: bytes | None = None) -> Any:
        return 7

    def generate_batch(self, provider: str, *, spec: Any, count: int) -> list[int]:
        return list(range(100, 100 + count))

    def capability_matrix(self, provider: str) -> Any:
        return get_default_registry().get_capabilities(self._provider)


def _int_override() -> Any:
    default = get_default_registry()
    return default.override(
        "person_first_name",
        _IntAdapter("person_first_name"),
        default.get_capabilities("person_first_name"),
    )


def _assert_det_pool_error(exc: BaseException) -> None:
    assert getattr(exc, "code", None) == DET_POOL_CODE, exc
    message = str(exc)
    assert "f" in message and "person_first_name" in message


def _run_int_override(chunks: Any, columns: list[dict[str, Any]] | None = None) -> BaseException:
    cols = columns if columns is not None else [faker_col("f"), passthrough("p")]
    writes: list[Any] = []
    with pytest.raises(Exception) as info:
        # Consume chunk-by-chunk: a chunk yielded BEFORE the guard raises would be captured here,
        # so the "no write" assertion actually tests fail-before-output, not just the final list.
        for chunk in run_mask_chunked(
            make_config(cols, global_settings=GS),
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            registry=_int_override(),
        ):
            writes.append(chunk)
    assert not writes, "no chunk may be written before the guard fails closed"
    return info.value


def test_det_override_trusted_producer_fails_closed() -> None:
    # The admit path (a trusted resident-slice producer) must fail closed, not stringify.
    _assert_det_pool_error(_run_int_override(fixed_schema_chunks_from_resident(_INT_SOURCE, 2)))


def test_det_override_ordinary_iterable_fails_closed() -> None:
    # The pin set is config-derived, so an ordinary list (which would otherwise decline to the
    # oracle) still reaches the guard and fails closed rather than silently stringifying.
    _assert_det_pool_error(_run_int_override(split(_INT_SOURCE, 2)))


def test_det_override_fails_closed_even_when_a_sibling_downgrades_the_table() -> None:
    # A force-oracle sibling would downgrade the whole table, and a trusted producer is the admit
    # path; the guard still fails closed before that path is taken, writing nothing.
    producer = fixed_schema_chunks_from_resident(with_force(_INT_SOURCE), 3)
    cols = [faker_col("f"), passthrough("p"), force_oracle(FORCE)]
    _assert_det_pool_error(_run_int_override(producer, cols))
