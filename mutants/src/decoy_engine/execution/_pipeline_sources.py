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

from collections.abc import Callable, Mapping

import pyarrow as pa

from decoy_engine.profile._readers import LazySource

__all__ = [
    "lazy_source_rejection",
    "materialize_source",
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
