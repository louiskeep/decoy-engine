"""Shared builders for the B8 tests (native admission of unconfigured passthrough columns).

`run_pair` runs one table twice through `run_mask_chunked`: as configured (the route
under test) and with the oracle route forced by a `force_oracle("cat_force")` column
(`group_key`), which the dispatcher still vetoes and which is dropped before the two are compared.
`assert_same_as_oracle` is the comparison of acceptance test 2: values, Arrow types,
field nullability and metadata, warnings, timing columns, vault entries, sink lengths
and the route each side took.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine import run_mask_chunked
from decoy_engine.vault import VaultWriter
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    FORCE_ORACLE_VALUE,
    TABLE,
    force_oracle,
    key_provider,
    make_config,
    vault_key,
)

FORCE = "cat_force"
FORCE_STRATEGY = "group_key"


@dataclass
class Run:
    out: list[pa.Table]
    sink: list[Any]
    ev: list[Any]
    vault: frozenset[Any]


def run_one(
    config: dict[str, Any],
    chunks: list[pa.Table],
    *,
    vault: bool = False,
    route_evidence_sink: list[Any] | None = None,
    **kw: Any,
) -> Run:
    """`route_evidence_sink`, when given, is the caller's list: it keeps the decision even
    when the run raises, so an expected-error leg can still prove which route it took."""
    sink: list[Any] = []
    ev: list[Any] = route_evidence_sink if route_evidence_sink is not None else []
    writer = VaultWriter(vault_key()) if vault else None
    out = list(
        run_mask_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            chunk_result_sink=sink,
            route_evidence_sink=ev,
            vault_writer=writer,
            **kw,
        )
    )
    return Run(out, sink, ev, frozenset(writer._entries) if writer is not None else frozenset())


def policy_settings(policy: str | None) -> dict[str, Any] | None:
    return {"unconfigured_column_policy": policy} if policy else None


def with_force(chunk: pa.Table) -> pa.Table:
    return chunk.append_column(FORCE, pa.array([FORCE_ORACLE_VALUE] * chunk.num_rows, pa.string()))


def run_pair(
    columns: list[dict[str, Any]],
    chunks: list[pa.Table],
    *,
    policy: str | None = "warn",
    vault: bool = False,
    **kw: Any,
) -> tuple[Run, Run]:
    """(native-route run, forced-oracle run) of the same columns and chunks."""
    gs = policy_settings(policy)
    native = run_one(make_config(columns, global_settings=gs), chunks, vault=vault, **kw)
    forced = run_one(
        make_config([*columns, force_oracle(FORCE)], global_settings=gs),
        [with_force(c) for c in chunks],
        vault=vault,
        **kw,
    )
    # run_pair always forces the oracle leg with a group_key column named FORCE, so by
    # construction that leg must stay on the oracle with the exact column-qualified reason.
    # Asserting it here protects every run_pair consumer (even ones that never call
    # assert_same_as_oracle) from silently degrading into a native-vs-native comparison.
    assert forced.ev[0].native_admitted is False, forced.ev[0]
    assert f"{FORCE_STRATEGY}_not_native_chunked_route:{FORCE}" in (
        forced.ev[0].reroute_reason or ""
    ), forced.ev[0]
    return native, forced


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def identical(a: pa.Table, b: pa.Table) -> bool:
    """`Table.equals(check_metadata=True)`. A column holding NaN is never `equals` to
    itself in Arrow, so when that fails the schemas must still be equal with metadata and
    the serialized streams byte-identical; nothing looser is accepted."""
    if a.equals(b, check_metadata=True):
        return True
    return bool(a.schema.equals(b.schema, check_metadata=True) and _ipc(a) == _ipc(b))


def same_field(a: pa.Field, b: pa.Field) -> bool:
    return bool(a.equals(b, check_metadata=True))


def assert_same_as_oracle(native: Run, forced: Run, *, expect_native: bool = True) -> None:
    assert native.ev[0].native_admitted is expect_native, native.ev[0]
    if expect_native:
        assert native.ev[0].reroute_reason is None
    assert forced.ev[0].native_admitted is False, forced.ev[0]
    assert f"{FORCE_STRATEGY}_not_native_chunked_route:{FORCE}" in (
        forced.ev[0].reroute_reason or ""
    )
    assert len(native.out) == len(forced.out) == len(native.sink) == len(forced.sink)
    for i, (got, want) in enumerate(zip(native.out, forced.out, strict=True)):
        assert identical(got, want.drop_columns([FORCE])), i
    for got, want in zip(native.sink, forced.sink, strict=True):
        assert got.warnings == want.warnings
        assert {r.column for r in got.timings} == {r.column for r in want.timings} - {FORCE}
        assert got.quality_metrics["chunked_route"]["pandas_read_passthrough"] == []
        assert want.quality_metrics["chunked_route"]["pandas_read_passthrough"] == []
    assert native.vault == forced.vault


def strings(n: int, tag: str, chunk: int = 0) -> pa.Array:
    return pa.array(
        [None if j % 5 == 2 else f"{tag}{chunk}-{j:03d}" for j in range(n)], pa.string()
    )
