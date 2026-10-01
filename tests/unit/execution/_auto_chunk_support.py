"""Shared builders for the B2 auto-chunk dispatcher acceptance tests.

Plan: docs/plans/2026-10-01-dispatcher-auto-chunk.md (revision 5). The tests in
`test_auto_chunk_dispatcher*.py` all need the same config shape, the same way
of running "today's lane" (the kill switch), and the same way of removing the
compiled companion, so they live here once.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import sys
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline

ENGINE_VERSION = "b2-auto-chunk-test"
TABLE = "t"
ROWS = 40
CHUNK = 16
THRESHOLD = 10
COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
NEEDS_COMPANION = pytest.mark.skipif(
    not COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)


def make_cfg(
    columns: list[dict[str, Any]], table: str = TABLE, path: str = "/dev/null"
) -> dict[str, Any]:
    """`path` is the parquet file `profile_source` reads; the execution reads the
    `sources` table the test passes, so the two must hold the same data."""
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {table: {"type": "file", "format": "parquet", "path": path}},
            "tables": [{"name": table, "columns": columns}],
            "targets": {table: {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


def hash_col(name: str, namespace: str | None = None) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace or f"ns_{name}"}


def redact_col(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "redact"}


def truncate_col(name: str, length: int = 3) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "truncate",
        "provider_config": {"length": length, "keep": "head"},
    }


def pass_col(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough"}


def faker_col(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": f"ns_{name}",
        "pool_size": 40,
    }


def kill_switch_supported() -> bool:
    return "chunked_dispatcher_enabled" in inspect.signature(run_pipeline).parameters


def legacy_kwargs() -> dict[str, Any]:
    """Today's lane. Before B2 exists the only lane is today's, so the kwarg is
    omitted; once it exists the kill switch selects it."""
    return {"chunked_dispatcher_enabled": False} if kill_switch_supported() else {}


def run_kwargs(**extra: Any) -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "auto_chunk_threshold_rows": THRESHOLD,
        "chunk_size_rows": CHUNK,
        # Pre-existing defect (plan Known issues): byte-estimate routing crashes on
        # date/time/decimal/binary columns, so cells that need those types turn it off.
        "use_byte_estimate_routing": False,
        **extra,
    }


def run_legacy(cfg: dict[str, Any], table: pa.Table, **extra: Any) -> Any:
    return run_pipeline(cfg, sources={TABLE: table}, **run_kwargs(**legacy_kwargs(), **extra))


def run_full_frame(cfg: dict[str, Any], table: pa.Table, **extra: Any) -> Any:
    return run_pipeline(cfg, sources={TABLE: table}, **run_kwargs(auto_chunk=False, **extra))


def run_default(cfg: dict[str, Any], table: pa.Table, **extra: Any) -> Any:
    """The dispatcher lane: the default knobs, auto-chunk on."""
    return run_pipeline(cfg, sources={TABLE: table}, **run_kwargs(**extra))


def reference_join(chunks: list[pa.Table]) -> pa.Table:
    """Plan Design 3's join, written independently of `join_dispatcher_chunks` so
    test 0 can record the dispatcher lane before that function exists: a field
    every chunk agrees on is kept as is (nullability and field metadata
    included); otherwise the single non-null type the chunks agree on wins and
    `null` is kept only when every chunk is `null`."""
    names = chunks[0].column_names
    if any(c.column_names != names for c in chunks):
        raise ValueError("column names differ across chunks")
    fields: list[pa.Field] = []
    for i, name in enumerate(names):
        per_chunk = [c.schema.field(i) for c in chunks]
        if all(f.equals(per_chunk[0], check_metadata=True) for f in per_chunk):
            fields.append(per_chunk[0])
            continue
        typed = [f for f in per_chunk if not pa.types.is_null(f.type)]
        if any(f.type != typed[0].type for f in typed):
            raise ValueError(f"column {name!r} has disagreeing types")
        fields.append(typed[0] if typed else per_chunk[0])
    schema = pa.schema(fields)
    cast = [c if c.schema.equals(schema, check_metadata=True) else c.cast(schema) for c in chunks]
    return pa.concat_tables(cast).combine_chunks().replace_schema_metadata(None)


@contextlib.contextmanager
def b1_as_the_lane(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route the auto-chunk lane through B1's `run_mask_chunked` exactly as plan
    Design 4 calls it, with a plain table join, so test 0 can record the
    dispatcher lane's output without any B2 code (it passes before and after B2)."""
    from decoy_engine.execution import _chunked
    from decoy_engine.execution import _pipeline_route_exec as rx
    from decoy_engine.execution._chunked_code_set import aggregate_chunk_code_set_corpora
    from decoy_engine.execution.native._chunked_entry import run_mask_chunked

    def b1_lane(
        config: Any,
        source: pa.Table,
        *,
        table: str,
        engine_version: str,
        registry: Any,
        adapter: Any,
        vault_writer: Any,
        chunk_size_rows: int,
        key_provider: Any = None,
        **_ignored: Any,
    ) -> Any:
        results: list[Any] = []
        slices = (
            source.slice(s, chunk_size_rows) for s in range(0, source.num_rows, chunk_size_rows)
        )
        chunks = list(
            run_mask_chunked(
                config,
                slices,
                table=table,
                engine_version=engine_version,
                registry=registry,
                adapter=adapter,
                vault_writer=vault_writer,
                chunk_result_sink=results,
                key_provider=key_provider,
            )
        )
        masked = reference_join(chunks)
        return (
            {table: masked},
            _chunked.aggregate_chunk_timings(results),
            sum(r.boundary_conversion_ms for r in results),
            _chunked.aggregate_chunk_warnings(results),
            aggregate_chunk_code_set_corpora(results),
        )

    monkeypatch.setattr(rx, "run_mask_chunked", b1_lane)
    yield


def remove_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `decoy_engine_native` unimportable, as on a machine without it."""
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)


def string_source(n: int = ROWS) -> dict[str, pa.Array]:
    """The two always-masked columns every cell carries."""
    return {
        "h": pa.array([f"u{i}@x.example" for i in range(n)]),
        "r": pa.array([f"s{i}" for i in range(n)]),
    }


def table_of(columns: dict[str, pa.Array], fields: dict[str, pa.Field] | None = None) -> pa.Table:
    fields = fields or {}
    schema = pa.schema([fields.get(n, pa.field(n, a.type)) for n, a in columns.items()])
    return pa.Table.from_arrays(list(columns.values()), schema=schema)


def exc_record(exc: BaseException) -> dict[str, Any]:
    return {"exc": type(exc).__name__, "code": getattr(exc, "code", None)}


def write_source(table: pa.Table, path: Any) -> str:
    """Write `table` where `profile_source` will read it; returns the path."""
    import pyarrow.parquet as pq

    pq.write_table(table, str(path))
    return str(path)


def check_contract(
    dispatcher: pa.Table,
    legacy: pa.Table,
    full: pa.Table | None,
    source: pa.Table,
    *,
    string_output: frozenset[str] | set[str] = frozenset(),
    masked: frozenset[str] | set[str] = frozenset(),
    faker_native_all_null: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Guarantee 3 of the plan, column by column, for one routed call.

    `string_output` names hash, truncate and string-redact columns (always `string`
    on the dispatcher lane), `masked` any other masked column (its type equals
    today's lane's), `faker_native_all_null` a native Faker column over an
    all-null string source (`string` here, `null` or `string` on today's lane).
    Every other column is a passthrough column and must equal the source.
    """
    # (a) same column names in the same order, same rows.
    assert dispatcher.column_names == legacy.column_names
    assert dispatcher.num_rows == legacy.num_rows
    # (d) no schema metadata.
    assert dispatcher.schema.metadata is None
    all_masked = set(string_output) | set(masked) | set(faker_native_all_null)
    for name in dispatcher.column_names:
        d_field = dispatcher.schema.field(name)
        if name in all_masked:
            # (b) same values as today's lane and as the forced full frame, nullable, no metadata.
            assert dispatcher.column(name).to_pylist() == legacy.column(name).to_pylist(), name
            if full is not None:
                assert dispatcher.column(name).to_pylist() == full.column(name).to_pylist(), name
            assert d_field.nullable, name
            assert d_field.metadata is None, name
            l_type = legacy.schema.field(name).type
            if name in string_output or name in faker_native_all_null:
                assert d_field.type == pa.string(), name
                assert l_type in (pa.string(), pa.null()), name
            else:
                assert d_field.type == l_type, name
        else:
            # (c) a passthrough column equals the source exactly.
            assert dispatcher.column(name).equals(source.column(name)), name
            assert d_field.equals(source.schema.field(name), check_metadata=True), name


def spy_lanes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Count calls into every lane entry point; each wrapper forwards to the real one.

    `auto_chunk_module` only exists once B2 is implemented, so it is patched when present."""
    from decoy_engine.execution import _chunked
    from decoy_engine.execution import _pipeline_route_exec as rx
    from decoy_engine.execution.native import _chunked_entry

    calls: dict[str, list[Any]] = {
        "route_exec.run_mask_chunked": [],
        "entry.run_mask_chunked": [],
        "oracle.run_mask_pipeline_chunked": [],
        "auto_chunk.run_auto_chunk": [],
    }

    def wrap(label: str, real: Any) -> Any:
        def spy(*args: Any, **kwargs: Any) -> Any:
            calls[label].append((args, kwargs))
            return real(*args, **kwargs)

        return spy

    monkeypatch.setattr(
        rx, "run_mask_chunked", wrap("route_exec.run_mask_chunked", rx.run_mask_chunked)
    )
    monkeypatch.setattr(
        _chunked_entry,
        "run_mask_chunked",
        wrap("entry.run_mask_chunked", _chunked_entry.run_mask_chunked),
    )
    monkeypatch.setattr(
        _chunked,
        "run_mask_pipeline_chunked",
        wrap("oracle.run_mask_pipeline_chunked", _chunked.run_mask_pipeline_chunked),
    )
    if importlib.util.find_spec("decoy_engine.execution._pipeline_auto_chunk") is not None:
        from decoy_engine.execution import _pipeline_auto_chunk as ac

        monkeypatch.setattr(
            ac, "run_auto_chunk", wrap("auto_chunk.run_auto_chunk", ac.run_auto_chunk)
        )
    return calls
