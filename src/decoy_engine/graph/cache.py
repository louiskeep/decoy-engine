"""Graph Arrow cache with engine conversion and consumer-count eviction.

GraphCache owns the in-flight pyarrow.Table store for a single run or
preview. It tracks downstream consumer counts pre-computed by the planner
and evicts entries as soon as their last consumer reads them, keeping peak
memory bounded by the working set rather than the full run history.

Engine conversion happens at consume time so each op receives data in its
declared NATIVE_ENGINE substrate rather than raw Arrow.
"""
from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.graph.conversion import (
    arrow_row_count,
    arrow_to_engine,
    engine_to_arrow,
)


class GraphCache:
    """In-run Arrow table store with consumer-count eviction.

    Parameters
    ----------
    consumer_counts:
        Per-node (or per split-port) downstream consumer counts as returned
        by the planner. The cache takes a mutable copy.
    keep_set:
        Node ids whose entries must survive until :meth:`snapshot` is called
        regardless of consumer-count reaching zero. Typically the ids in
        the ``keep_nodes`` argument of ``execute_graph_capture``.
    """

    def __init__(
        self,
        consumer_counts: dict[str, int],
        keep_set: set[str] | None = None,
    ) -> None:
        self._tables: dict[str, pa.Table | None] = {}
        self._remaining: dict[str, int] = dict(consumer_counts)
        self._keep_set: set[str] = keep_set or set()

    # ── write ───────────────────────────────────────────────────────────────────

    def store_from_op(
        self,
        key: str,
        result: Any,
        engine: str,
        row_limit: int | None = None,
    ) -> pa.Table | None:
        """Convert op output to Arrow, optionally cap rows, store, and return.

        Returns the stored Arrow table so callers can read row counts from
        the local reference even if the entry is immediately evicted (e.g.
        a sink with no downstream consumers).
        """
        table = engine_to_arrow(result, engine) if result is not None else None
        if row_limit is not None and table is not None and table.num_rows > row_limit:
            table = table.slice(0, row_limit)
        self._tables[key] = table
        self._maybe_evict(key)
        return table

    # ── read ───────────────────────────────────────────────────────────────────

    def consume(
        self,
        key: str,
        engine: str,
        hold: str | None = None,
    ) -> Any:
        """Decrement consumer count, evict at zero, return engine-native value.

        `hold` pins a specific key so preview can serialise the target
        node's output after the full ancestor walk completes.
        """
        table = self._tables.get(key)
        if table is None:
            return None
        if key in self._remaining:
            self._remaining[key] -= 1
            if self._remaining[key] <= 0 and key != hold and key not in self._keep_set:
                del self._tables[key]
        return arrow_to_engine(table, engine)  # type: ignore[arg-type]

    def get_arrow(self, key: str) -> pa.Table | None:
        """Return the stored Arrow table without touching consumer counts."""
        return self._tables.get(key)

    def row_count(self, key: str) -> int:
        """Return row count of a stored table, or 0 if absent."""
        return arrow_row_count(self._tables.get(key))

    # ── lifecycle ───────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, pa.Table]:
        """Return pinned keep-set entries still in the cache."""
        return {k: self._tables[k] for k in self._keep_set if k in self._tables}

    # ── internals ───────────────────────────────────────────────────────────────

    def _maybe_evict(self, key: str) -> None:
        if self._remaining.get(key, 0) == 0 and key not in self._keep_set:
            self._tables.pop(key, None)
