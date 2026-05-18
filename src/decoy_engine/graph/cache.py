"""Arrow cache for the graph runner.

Owns the in-memory Arrow output cache, consumer-count eviction, and
engine-format conversion at cache boundaries. The runner stores every
op's output as a ``pyarrow.Table``; eviction happens when the last
downstream consumer reads an entry so peak memory stays bounded by the
in-flight working set rather than the lifetime of the run.

Arrow-as-canonical: ``write`` converts op output to Arrow at the store
boundary; ``read`` converts from Arrow to the consuming op's declared
``NATIVE_ENGINE`` at the read boundary. The dual-representation window
is bounded to one op at a time.

Split ops: ops with ``OUTPUT_KIND="split"`` (e.g. ``if``, flag routers)
return a dict of port name to value. ``write_split`` stores each port
under ``"node_id.port"`` keys and evicts per-port independently.
Downstream edges reference ``"node_id.port"`` in their ``from`` field.

Keep-keys: the caller can pin entries (e.g. for ``execute_graph_capture``
outputs or the preview target) by passing their keys to ``keep_keys``.
Pinned entries receive one extra consumer slot so normal eviction cannot
fire; call ``collect_kept`` after the run to retrieve them.
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
    """In-memory Arrow cache with consumer-count eviction.

    Keys are either ``"node_id"`` (single-output ops) or
    ``"node_id.port"`` (split-output ops). Consumer counts come from the
    planner; each ``read`` decrements the count and evicts at zero unless
    the key is pinned.

    Usage::

        gc = GraphCache(plan.consumer_counts, keep_keys={"out_node"})
        gc.write("a", df, "pandas")
        rows = gc.read("a", "duckdb")    # converts + evicts if last consumer
        kept = gc.collect_kept()          # {"out_node": pa.Table}
    """

    def __init__(
        self,
        consumer_counts: dict[str, int],
        keep_keys: set[str] | None = None,
    ) -> None:
        self._data: dict[str, pa.Table | None] = {}
        self._remaining: dict[str, int] = dict(consumer_counts)
        self._keep: frozenset[str] = frozenset(keep_keys or ())
        # Give each pinned key one extra consumer slot so normal eviction
        # logic cannot fire before collect_kept() is called.
        for k in self._keep:
            self._remaining[k] = self._remaining.get(k, 0) + 1

    # ------------------------------------------------------------------
    # Write side

    def write(
        self,
        key: str,
        value: Any,
        engine: str,
        row_limit: int | None = None,
    ) -> int:
        """Store a single-output op result and return the stored row count.

        Converts ``value`` from ``engine`` format to Arrow at the store
        boundary. If ``row_limit`` is given the stored table is sliced to
        at most that many rows (preview mode). Evicts immediately if the
        key has no downstream consumers and is not pinned.
        """
        table = engine_to_arrow(value, engine) if value is not None else None
        if table is not None and row_limit is not None and table.num_rows > row_limit:
            table = table.slice(0, row_limit)
        self._data[key] = table
        if self._remaining.get(key, 0) == 0 and key not in self._keep:
            self._data.pop(key, None)
        return arrow_row_count(table)

    def write_split(
        self,
        node_id: str,
        ports: tuple[str, ...],
        result: dict,
        engine: str,
        row_limit: int | None = None,
    ) -> int:
        """Store all ports of a split-output op and return total row count.

        Each port is stored under ``"node_id.port"`` and evicted
        independently when its consumer count reaches zero.
        """
        total = 0
        for port in ports:
            tbl = result.get(port)
            key = f"{node_id}.{port}"
            table = engine_to_arrow(tbl, engine) if tbl is not None else None
            if table is not None and row_limit is not None and table.num_rows > row_limit:
                table = table.slice(0, row_limit)
            self._data[key] = table
            total += arrow_row_count(table)
            if self._remaining.get(key, 0) == 0 and key not in self._keep:
                self._data.pop(key, None)
        return total

    def store_arrow(self, key: str, table: pa.Table | None) -> None:
        """Store a pre-converted Arrow table directly without conversion.

        Used for preview aliases (mapping a split op's pass port to the
        node's direct key) and for recording None on op error.
        """
        self._data[key] = table

    # ------------------------------------------------------------------
    # Read side

    def read(self, key: str, engine: str) -> Any:
        """Consume an entry: return it in ``engine`` format, decrement
        consumer count, and evict when the count reaches zero unless pinned.

        Returns ``None`` for missing keys (treated as empty input).
        """
        table = self._data.get(key)
        if table is None:
            return None
        if key in self._remaining:
            self._remaining[key] -= 1
            if self._remaining[key] <= 0 and key not in self._keep:
                del self._data[key]
        return arrow_to_engine(table, engine)

    def peek_rows(self, key: str) -> int:
        """Return the row count of a cached entry without consuming it.

        Returns 0 for missing or None entries.
        """
        return arrow_row_count(self._data.get(key))

    def get_arrow(self, key: str) -> pa.Table | None:
        """Return the raw Arrow table for a key without consuming it."""
        return self._data.get(key)

    # ------------------------------------------------------------------
    # Finalisation

    def collect_kept(self) -> dict[str, pa.Table]:
        """Return ``{key: Arrow table}`` for all pinned entries still in cache."""
        return {k: self._data[k] for k in self._keep if k in self._data}
