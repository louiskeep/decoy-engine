"""Inter-node Arrow cache with consumer-count eviction.

GraphCache stores every node output as a pyarrow.Table and tracks how many
downstream consumers are still waiting to read each slot.  When the last
consumer reads a slot the entry is evicted so peak memory stays bounded
to the in-flight working set rather than the full run history.

Split ops store each declared port under a "{nid}.{port}" key.  The runner
passes the per-port edge keys directly to ``consume``; GraphCache handles
eviction per port just as it does for plain node keys.

Preview usage: create with ``keep={node_id}`` (and, for split target nodes,
with the target's port keys) so the target's output survives past the run
loop.  Use ``set_raw`` to alias the split "pass" port onto the direct node
key without triggering conversion or eviction logic.
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
    """Arrow-backed inter-node cache with consumer-count eviction.

    All intermediate results are stored as ``pyarrow.Table`` instances.
    Consumer counts are decremented as each downstream node reads a slot;
    when the count reaches zero the slot is deleted from the store.

    Keys are either plain node ids (``"mask_1"``) or split-port ids
    (``"router.pass"``, ``"router.fail"``).  The caller decides which
    shape to use; GraphCache is agnostic.
    """

    def __init__(
        self,
        consumer_counts: dict[str, int],
        keep: set[str] | None = None,
    ) -> None:
        self._data: dict[str, pa.Table | None] = {}
        self._remaining: dict[str, int] = dict(consumer_counts)
        self._keep: set[str] = set(keep or ())
        # Bump ref counts for explicitly retained keys so eviction skips them.
        for k in self._keep:
            self._remaining[k] = self._remaining.get(k, 0) + 1

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def row_sum(self, keys: list[str]) -> int:
        """Sum row counts for the given cache keys without consuming them."""
        return sum(
            arrow_row_count(self._data.get(k))
            for k in keys
            if self._data.get(k) is not None
        )

    def consume(self, key: str, engine: str, hold: str | None = None) -> Any:
        """Convert ``key``'s Arrow table to ``engine`` format and evict when last consumer reads.

        ``hold`` pins a key past zero consumers.  With the keep-set approach
        callers rarely need ``hold`` directly; it is retained for
        backward-compatibility.
        """
        table = self._data.get(key)
        if table is None:
            return None
        if key in self._remaining:
            self._remaining[key] -= 1
            if self._remaining[key] <= 0 and key != hold:
                del self._data[key]
        return arrow_to_engine(table, engine)

    def get(self, key: str) -> pa.Table | None:
        """Direct Arrow read without eviction (for post-run extraction)."""
        return self._data.get(key)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write_stream(
        self,
        key: str,
        result: Any,
        engine: str,
        row_limit: int | None = None,
    ) -> int:
        """Convert and store a single-output op result.  Returns the row count.

        Evicts immediately when there are no downstream consumers (sink nodes
        whose output is empty by convention, or unused intermediate outputs).
        """
        table = engine_to_arrow(result, engine) if result is not None else None
        if table is not None and row_limit is not None and table.num_rows > row_limit:
            table = table.slice(0, row_limit)
        self._data[key] = table
        row_count = arrow_row_count(table)
        if self._remaining.get(key, 0) <= 0:
            self._data.pop(key, None)
        return row_count

    def write_split(
        self,
        nid: str,
        result: dict[str, Any],
        ports: tuple[str, ...],
        engine: str,
        row_limit: int | None = None,
    ) -> int:
        """Convert and store split-op port outputs.  Returns total row count.

        Each port is stored under ``"{nid}.{port}"``.  Ports with no
        downstream consumers are evicted immediately (same as ``write_stream``).
        """
        total = 0
        for port in ports:
            tbl = result.get(port)
            key = f"{nid}.{port}"
            arrow_tbl = engine_to_arrow(tbl, engine) if tbl is not None else None
            if arrow_tbl is not None and row_limit is not None and arrow_tbl.num_rows > row_limit:
                arrow_tbl = arrow_tbl.slice(0, row_limit)
            self._data[key] = arrow_tbl
            total += arrow_row_count(arrow_tbl)
            if self._remaining.get(key, 0) <= 0:
                self._data.pop(key, None)
        return total

    def set_raw(self, key: str, table: pa.Table | None) -> None:
        """Store ``table`` directly, bypassing conversion and eviction logic.

        Used by preview_graph for two purposes:
        - Recording ``None`` on an op error so downstream preview nodes get
          ``None`` inputs and gracefully produce no output.
        - Aliasing a split "pass" port onto the plain node id so
          ``cache.get(node_id)`` returns the preview output.
        """
        self._data[key] = table

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def kept(self) -> dict[str, pa.Table]:
        """Return entries that were explicitly requested to be retained."""
        return {k: self._data[k] for k in self._keep if k in self._data}
