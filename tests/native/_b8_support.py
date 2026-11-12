"""Shared builders for the B8 tests (native admission of unconfigured passthrough columns).

`run_pair` runs one table twice through `run_mask_chunked`: as configured (the route
under test) and with the oracle route forced by a `force_oracle("cat_force")` column
(a numeric-categories categorical the native route never admits), dropped before the two
are compared.
`assert_same_as_oracle` is the comparison of acceptance test 2: values, Arrow types,
field nullability and metadata, warnings, timing columns, vault entries, sink lengths
and the route each side took.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.config._subset_run import refuse_unapplied_subset
from decoy_engine.execution.native import _chunked_entry
from decoy_engine.profile import (
    DeclarationRequest,
    PreparedDeclaredSources,
    prepare_declared_sources,
)
from decoy_engine.profile._declared_columns import arrow_columns
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
FORCE_REASON = f"fallback_policy_not_native:{FORCE}:python_only"


def _declare_columns(entry: dict[str, Any], schema: pa.Schema) -> None:
    have = {c["name"] for c in entry.get("columns") or []}
    for name in arrow_columns(schema):
        if name not in have:
            entry.setdefault("columns", []).append({"name": name, "strategy": "passthrough"})


def below_gate(
    config: dict[str, Any], table: str, chunks: Iterable[pa.Table]
) -> tuple[PreparedDeclaredSources, Iterable[pa.Table]]:
    """A real capability over a copy of `config` that declares every column `chunks` carry,
    and the chunks to feed the layer below the gate.

    The B8 tests observe how an *unconfigured* column is carried, which the declaration gate
    now refuses at the public entry. They call the layer below it with this capability; the
    run config keeps the columns unconfigured, so the B8 carry is still what they observe.
    A list and a fixed-schema producer are declared from without being consumed. Any other
    iterable is wrapped so each chunk declares its columns just before it is handed on, which
    keeps "no chunk was pulled" assertions meaningful.
    """
    from decoy_engine.execution._chunked_input import _FixedSchemaChunks

    declared = copy.deepcopy(config)
    entry = next((t for t in declared["tables"] if t.get("name") == table), None)
    if entry is None:
        # An unknown table is the compatibility check's refusal, which runs below the gate.
        entry = {"name": table, "columns": []}
        declared["tables"].append(entry)
    capability = prepare_declared_sources(DeclarationRequest.for_chunks(declared, table, None))
    if isinstance(chunks, _FixedSchemaChunks):
        _declare_columns(entry, chunks.source_schema)
        return capability, chunks
    if isinstance(chunks, list):
        for chunk in chunks:
            _declare_columns(entry, chunk.schema)
        return capability, chunks

    def declaring() -> Iterator[pa.Table]:
        for chunk in chunks:
            _declare_columns(entry, chunk.schema)
            yield chunk

    return capability, declaring()


def run_mask_chunked_below_gate(
    config: dict[str, Any], chunks: Iterable[pa.Table], *, table: str, **kw: Any
) -> Any:
    """`run_mask_chunked` minus the declaration gate: same subset and thread checks, then the
    private body with a `below_gate` capability."""
    refuse_unapplied_subset(config)
    _chunked_entry._validate_native_threads(kw.get("native_threads", 1))
    declared, fed = below_gate(config, table, chunks)
    return _chunked_entry._run_chunked(config, fed, table=table, declared=declared, **kw)


def run_native_or_oracle_chunked_below_gate(
    config: dict[str, Any], chunks: Iterable[pa.Table], *, table: str, **kw: Any
) -> Any:
    """`run_native_or_oracle_chunked` minus the declaration gate (see `below_gate`)."""
    from decoy_engine.execution.native._dispatch import reject_per_table_transforms

    refuse_unapplied_subset(config)
    reject_per_table_transforms(config, table=table, route="native chunked execution")
    declared, fed = below_gate(config, table, chunks)
    return _chunked_entry._run_chunked(
        config,
        fed,
        table=table,
        declared=declared,
        enforce_schema_rule=False,
        native_threads=kw.pop("native_threads", None),
        **kw,
    )


def run_mask_pipeline_chunked_below_gate(
    config: dict[str, Any], chunks: Iterable[pa.Table], *, table: str, **kw: Any
) -> Any:
    """The public oracle entry with its gate answered by a `below_gate` capability.

    `run_mask_pipeline_chunked` has no private body to call, so the gate call it makes at
    entry is replaced for the duration of the (eager) call; the iterator it returns keeps the
    capability it was given.
    """
    from unittest import mock

    from decoy_engine.execution import _chunked

    capability, fed = below_gate(config, table, chunks)
    kw.setdefault("source_schema", pa.schema([]))
    with mock.patch.object(_chunked, "prepare_declared_sources", return_value=capability):
        return _chunked.run_mask_pipeline_chunked(config, fed, table=table, **kw)


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
        run_mask_chunked_below_gate(
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
    # run_pair always forces the oracle leg with a categorical column named FORCE, so by
    # construction that leg must stay on the oracle with the exact column-qualified reason.
    # Asserting it here protects every run_pair consumer (even ones that never call
    # assert_same_as_oracle) from silently degrading into a native-vs-native comparison.
    assert forced.ev[0].native_admitted is False, forced.ev[0]
    assert FORCE_REASON in (forced.ev[0].reroute_reason or ""), forced.ev[0]
    return native, forced


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def identical(a: pa.Table, b: pa.Table) -> bool:
    """Literal serialization identity: equal schema (with metadata) AND byte-identical IPC
    streams. This is stricter than `Table.equals(check_metadata=True)`, which is
    metadata-inclusive value equality, not a byte guarantee. It also handles a NaN column
    (never `equals` to itself in Arrow) correctly, since NaN's IPC bytes still match."""
    return bool(a.schema.equals(b.schema, check_metadata=True) and _ipc(a) == _ipc(b))


def same_field(a: pa.Field, b: pa.Field) -> bool:
    return bool(a.equals(b, check_metadata=True))


def assert_same_as_oracle(native: Run, forced: Run, *, expect_native: bool = True) -> None:
    assert native.ev[0].native_admitted is expect_native, native.ev[0]
    if expect_native:
        assert native.ev[0].reroute_reason is None
    assert forced.ev[0].native_admitted is False, forced.ev[0]
    assert FORCE_REASON in (forced.ev[0].reroute_reason or "")
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
