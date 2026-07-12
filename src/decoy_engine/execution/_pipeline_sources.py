"""TB-1 source materialization: resolve a caller-supplied source AT THE POINT OF USE.

`_isolated_worker._load_sources` hands `run_pipeline` every input wrapped as a
`LazySource` instead of eagerly reading it, so the out-of-core route (which
already accepts `pa.Table | LazySource` natively --
`out_of_core._runner.run_fk_out_of_core`) never pays for a resident copy it
does not need. full_frame and sequential are the two routes that DO
legitimately need a whole table resident; each resolver here is called
exactly where its route consumes a source, never before the route decision
runs, so the routing layer never forces residency it will not use.

- `materialize_source`: the base resolver -- a plain `pa.Table` (the
  pre-TB-1 shape, and still what every non-isolated caller passes) is
  returned unchanged; a `LazySource` is read via `.to_table()`.
- `resolve_resident_sources`: the full_frame / auto-chunk continuation's
  need -- every source resident at once, because the adapter, the
  auto-chunk slicer, validators, and the fidelity report all operate on
  whole `pa.Table` frames.
- `resolve_sequential_loader`: `run_sequential_route`'s need -- one table
  at a time, matching `_sequential.py`'s own bounded-memory contract
  (unrelated to this sprint); a caller-supplied `source_loader` always
  wins (it already returns residents), else fall back to resolving from
  `caller_sources` lazily, per table, only when the sequential runner
  actually asks for it.
- `lazy_source_rejection`: the auto-chunk planner's defensive guard --
  see its own docstring for why this is belt-and-suspenders rather than
  a reachable production path.

Established-methodology note: this mirrors the general "resolve lazily, at
the point of consumption" pattern (deferred / lazy evaluation), applied here
so the out-of-core route's whole reason for existing -- bounded memory -- is
never undermined by an eager materialization upstream of the route decision.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.profile._readers import LazySource

__all__ = [
    "lazy_source_from_descriptor",
    "lazy_source_rejection",
    "materialize_source",
    "resolve_out_of_core_sources",
    "resolve_resident_sources",
    "resolve_sequential_loader",
]


def materialize_source(value: pa.Table | LazySource) -> pa.Table:
    """Resolve one source to a resident Arrow table.

    A plain `pa.Table` (the pre-TB-1 shape, and still what every
    non-isolated caller passes) is returned unchanged; a `LazySource` is
    read via `.to_table()`. Callers decide WHEN to call this -- see the
    module docstring for which route needs residency and why.
    """
    if isinstance(value, LazySource):
        return value.to_table()
    return value


def resolve_resident_sources(
    caller_sources: Mapping[str, pa.Table | LazySource],
) -> dict[str, pa.Table]:
    """Materialize every source, for routes that need them all resident at once.

    The full_frame / auto-chunk continuation is the one caller: the
    adapter, the auto-chunk slicer, validators, and the fidelity report
    all operate on whole `pa.Table` frames, so every remaining source is
    resolved once, here, at the point full_frame actually consumes them --
    never before that.
    """
    return {name: materialize_source(src) for name, src in caller_sources.items()}


def resolve_sequential_loader(
    source_loader: Callable[[str], pa.Table] | None,
    caller_sources: Mapping[str, pa.Table | LazySource],
) -> Callable[[str], pa.Table]:
    """Build the per-table loader `run_sequential_route` calls.

    `run_sequential_route` loads one table at a time (its own
    bounded-memory contract, unrelated to this sprint), so a `LazySource`
    entry in `caller_sources` is resolved lazily, per table, only when the
    sequential runner actually asks for it -- never up front. A
    caller-supplied `source_loader` always wins (it already returns
    residents by its own contract); otherwise fall back to resolving from
    `caller_sources`.
    """
    if source_loader is not None:
        return source_loader
    return lambda t: materialize_source(caller_sources[t])


def lazy_source_from_descriptor(descriptor: Any) -> LazySource | None:
    """Build a lazy on-disk handle for a local Parquet file source, else None.

    DE-09: `LazySource` streams a single on-disk Parquet file
    (`iter_batches` + footer metadata), so ONLY a local `file` + `parquet`
    descriptor can be wrapped as one -- exactly the shape
    `_isolated_worker._load_sources` and `ParquetFileSource._lazy` already
    build. A CSV / fixed_width source (Parquet-only `LazySource` cannot read
    it) or a cloud (`s3`/`gcs`) source has no local Parquet path, so this
    returns None and the caller falls back to a resident loader. Constructing
    a `LazySource` is just wrapping the path string -- no I/O -- so this is
    cheap regardless of table size.
    """
    if not isinstance(descriptor, Mapping):
        return None
    if descriptor.get("type") != "file" or descriptor.get("format") != "parquet":
        return None
    path = descriptor.get("path")
    if not isinstance(path, str):
        return None
    return LazySource(Path(path))


def resolve_out_of_core_sources(
    ordered_tables: Iterable[str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    *,
    source_loader: Callable[[str], pa.Table] | None,
    config_sources: Mapping[str, Any],
) -> dict[str, pa.Table | LazySource]:
    """Resolve every plan/graph table the out-of-core runner needs, preferring
    a lazy on-disk handle over an eager resident load (DE-09).

    `run_fk_out_of_core` consumes `pa.Table | LazySource` per table and streams
    a `LazySource` through bounded `iter_batches` (never `.to_table()`), so a
    missing table backed by a local Parquet file is resolved to a `LazySource`
    -- the DuckDB runner then streams it within its own `batch_rows` bound and
    never holds it resident. This replaces the pre-DE-09 branch that eagerly
    called `source_loader(table)` for every missing table and retained the
    resulting resident `pa.Table`s, undoing the whole point of the route.

    Resolution order per table:

    - already in `caller_sources`: kept verbatim (the caller's explicit
      choice -- a resident `pa.Table` or a `LazySource` it built itself, e.g.
      `_isolated_worker`);
    - missing, but its config source is a local Parquet file: a `LazySource`
      (bounded, streamed);
    - missing, no Parquet path (CSV / fixed_width / cloud, or a caller-only
      dynamic loader): the resident `source_loader(table)` fallback, documented
      because those sources have no lazy Parquet handle to stream;
    - missing, neither: left absent, so `run_fk_out_of_core` raises its own
      `out_of_core_source_missing` exactly as before (never a silent skip).
    """
    resolved: dict[str, pa.Table | LazySource] = dict(caller_sources)
    for table in ordered_tables:
        if table in resolved:
            continue
        lazy = lazy_source_from_descriptor(config_sources.get(table))
        if lazy is not None:
            resolved[table] = lazy
        elif source_loader is not None:
            resolved[table] = source_loader(table)
    return resolved


def lazy_source_rejection(src: pa.Table | LazySource, *, table: str) -> str | None:
    """None unless `src` is a `LazySource`; else the chunked-route rejection reason.

    TB-1 defensive guard: a `LazySource` is a lazy on-disk handle
    (`_isolated_worker._load_sources`), not a resident frame -- the
    dtype/null-bearing walk in `_planner._runtime_source_rejections` reads
    actual column DATA (`.column(name).null_count`), which is exactly the
    eager materialization TB-1 exists to avoid. Chunking is already
    rejected upstream for all relationship-bearing jobs
    (`config.get("relationships")`), and ONLY relationship jobs carry a
    `LazySource` by construction, so this guard is unreachable in
    production. It is kept as belt-and-suspenders defense-in-depth against
    any future code path that might hand a `LazySource` to a
    non-relationship job. When this guard fires, conservatively treat it
    like "no loaded source frame": the job stays on the (still correct,
    just not chunk-streamed) full_frame path, where
    `resolve_resident_sources` resolves the `LazySource` at the point
    full_frame actually consumes it.
    """
    if not isinstance(src, LazySource):
        return None
    return (
        f"source for table {table!r} is a lazy (LazySource) handle, not a "
        "resident frame; the chunk-stable-dtype runtime gate needs real "
        "column data, so auto-chunk conservatively declines rather than "
        "force-materializing it just to decide"
    )
