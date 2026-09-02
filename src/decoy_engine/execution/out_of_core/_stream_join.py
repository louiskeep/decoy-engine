"""Single-streaming-join FK joiner for the batch-streaming sink path (OOC-B).

This replaces the removed per-child-batch `ChildFkBatchJoiner`'s join against a
MATERIALIZED parent TEMP TABLE. That table held one row per distinct parent
key and, being a DuckDB temp table, could not fully evict its
buffer-manager/control state, so on a large parent it pinned an
O(distinct-parent-key) resident structure for the whole child stream -- the
floor that made out-of-core peak memory rise with parent row count.

fix#1b (this module): the FIRST fix (above) made the child side a DuckDB
`child_keys` TEMP TABLE fed by per-batch `INSERT` -- itself an O(child)
resident structure for the SAME reason (a DuckDB temp table cannot fully evict
its buffer-manager/control state), so peak memory rose with CHILD row count
instead. The child side is now symmetric with the parent: staged once into a
`SpillChildKeys` Arrow-IPC file (`_payload_store.py`, mirroring
`RawParentKeySpill`) during phase 1, then each of this joiner's two scans
(`total_orphans`'s FAIL precount, `iter_join_rows`'s join) opens a FRESH
`pa.ipc.open_stream` reader and `conn.register()`s it for that scan alone. A
DuckDB-registered `pyarrow.RecordBatchReader` is SINGLE-PASS (rows on the
first query against it, zero on a second -- observed on DuckDB 1.5.4's Python
Arrow integration), which is exactly why each scan needs its own reader
rather than one shared registration.

This joiner uses the SAME shape the whole-child resident path
(`_join.py::mask_child_fk`) already proves: the parent relation is a
`read_parquet` VIEW (never materialized), the child keys are a file-backed
streaming scan (never a resident structure), and ONE `LEFT JOIN child_keys x
parent_keys ORDER BY __decoy_row_nr` per edge is read back through
`to_arrow_reader`. The join build, hash table, and external sort are DuckDB's
established larger-than-memory (grace / hybrid hash join + external merge
sort) operations under `memory_limit`, spilling to `temp_directory`; we
delegate all memory-management, spill, and ordering to DuckDB and do not roll
our own. The one difference from `_join.py` is that this joiner EMITS
incrementally (an ordered FK-output reader the runner zips to its mask
stream) rather than setting columns into a whole resident `pa.Table`.

Two invariants carried over UNCHANGED from `ChildFkBatchJoiner`, because the
sink path writes one Parquet file under one schema fixed before the first
batch:

- Fixed output schema. `output_types` is resolved from schemas alone at
  construction (`_batch_join._resolve_output_types`), fail-closing on any mix
  that cannot be typed byte-identically to whole-column inference, and
  `observed_types` records the pre-cast chunk types so `_emit.py` can replay
  the whole-column narrowing on the resident/relation sides. Neither the
  fixed-schema typing nor the documented divergences it carries change here.
- Per-(output-)batch REMAP minting. Orphan REMAP values are minted from each
  join-output batch's own `__decoy_src_i` keys (the raw child values, which
  ride through the join unchanged, so every edge still keys off the RAW child),
  never precomputed over the whole child -- so no kernel call is sized by total
  child cardinality. Because the streamed join numbers rows GLOBALLY (for the
  `ORDER BY`), each output batch's `__decoy_row_nr` is re-based to positional
  0..n before `_append_output_batch`, which uses row_nr solely as the REMAP
  index; the values are identical to the whole-child mint because the kernels
  are per-value deterministic.

See `_batch_join.py`'s module docstring for the full inventory of documented
typing divergences (all preserved), and `tests/parity/` for the byte-parity
gate against the pandas oracle.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_key_value
from decoy_engine.execution.out_of_core._batch_join import (
    _cast_chunks,
    _resolve_output_types,
)
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter
from decoy_engine.execution.out_of_core._join import (
    _append_output_batch,
    _child_key_batches,
    _q,
    _sql_string,
)
from decoy_engine.execution.out_of_core._mask import mask_column
from decoy_engine.execution.out_of_core._payload_store import SpillChildKeys
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.plan._types import ColumnSeed
    from decoy_engine.relationships._graph import RelationshipEdge


# DuckDB's optimizers, by name in `duckdb_optimizers()`, pinned off so the
# unordered join keeps the WRITTEN join order (child probe, deduplicated
# parent build) rather than DuckDB's own cardinality-estimate-driven choice:
# `build_side_probe_side` can swap which side builds the hash table, and
# `join_order` can reorder multi-relation joins. Kept as a module constant so
# the structural plan-pin test can assert on the exact same names this module
# pragmas against.
_PINNED_DISABLED_OPTIMIZERS = ("build_side_probe_side", "join_order")

# Physical-plan operator names (DuckDB 1.5.4's `EXPLAIN (FORMAT JSON)`) that
# perform a GLOBAL sort/order. The whole point of `run_ordered_join`'s
# unordered-join + bounded-sorter split is that NEITHER side re-injects one of
# these; the plan guard below fails closed if it ever sees one here.
_GLOBAL_SORT_OPERATOR_NAMES = frozenset({"ORDER_BY", "TOP_N"})


def _disable_join_optimizers(conn: Any) -> None:
    """Pin the joiner's own connection to the written join order (parent-build).

    Public DuckDB API only (`duckdb_optimizers()` + `SET disabled_optimizers`),
    not a library customization. Appends to any `disabled_optimizers` value
    `connect_duckdb` may already carry rather than overwriting it, since this
    connection is per-joiner and not shared with the resident/relation-build
    connections that never register an unsized Arrow stream as a join side.
    """
    names = {row[0] for row in conn.execute("SELECT name FROM duckdb_optimizers()").fetchall()}
    existing = conn.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    disabled = {name for name in existing.split(",") if name}
    for optimizer in _PINNED_DISABLED_OPTIMIZERS:
        if optimizer in names:
            disabled.add(optimizer)
        # else: an older/renamed DuckDB without this optimizer name never
        # swaps/reorders this way; a missing name must degrade to a no-op,
        # never an error.
    if disabled:
        conn.execute(f"SET disabled_optimizers = '{','.join(sorted(disabled))}'")


def _iter_plan_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Depth-first walk of a parsed `EXPLAIN (FORMAT JSON)` operator tree.

    Fails closed on any node that is not a `{"name": ..., "children": [...]}`
    shape: a malformed plan must never be silently skipped over, since a
    verification that quietly ignores what it cannot parse is no verification
    at all.
    """
    if not isinstance(node, dict) or "name" not in node:
        raise ExecutionError(
            code="out_of_core_fk_join_plan_unverified",
            message=(
                "the unordered join's EXPLAIN (FORMAT JSON) plan had an unexpected "
                f"node shape (expected a dict with a 'name' key, got {node!r}); "
                "refusing to run an unverified plan."
            ),
        )
    yield node
    children = node.get("children", [])
    if not isinstance(children, list):
        raise ExecutionError(
            code="out_of_core_fk_join_plan_unverified",
            message=(
                f"the unordered join's EXPLAIN plan node {node.get('name')!r} had a "
                "non-list 'children'; refusing to run an unverified plan."
            ),
        )
    for child in children:
        yield from _iter_plan_nodes(child)


def _verify_unordered_plan_or_raise(plan: dict[str, Any]) -> None:
    """Fail-closed structural guard (Codex plan-gate MEDIUM 4).

    The unordered join's physical plan must carry NO global sort operator (the
    whole reason it is unordered is that order is restored separately, by the
    bounded sorter), and its single `HASH_JOIN` must keep the WRITTEN join
    order: the child (a registered Arrow reader, `ARROW_SCAN`) as the streamed
    probe side, and the deduplicated parent (a `read_parquet` view,
    `READ_PARQUET`) as the build side. This is a structural check on operator
    TYPES, not the query's view aliases: a future optimizer change that
    reintroduces a global sort, or flips the build side onto the (unsized,
    unbounded) child stream, would make the join hold an O(child) resident
    hash table -- exactly the regression the pinned optimizers exist to
    prevent -- so it must fail closed here, not silently regress.
    """
    nodes = list(_iter_plan_nodes(plan))
    for node in nodes:
        if node["name"] in _GLOBAL_SORT_OPERATOR_NAMES:
            raise ExecutionError(
                code="out_of_core_fk_join_plan_unverified",
                message=(
                    f"the unordered join's plan regained a global sort operator "
                    f"({node['name']!r}); the reorder path requires an UNORDERED "
                    "join -- order is restored separately by the bounded sorter."
                ),
            )
    joins = [node for node in nodes if node["name"] == "HASH_JOIN"]
    if len(joins) != 1:
        raise ExecutionError(
            code="out_of_core_fk_join_plan_unverified",
            message=(
                "expected exactly one HASH_JOIN operator in the unordered join's "
                f"plan, found {len(joins)}; refusing to run an unverified plan."
            ),
        )
    join = joins[0]
    children = join.get("children", [])
    if len(children) != 2:
        raise ExecutionError(
            code="out_of_core_fk_join_plan_unverified",
            message=(
                f"the HASH_JOIN operator had {len(children)} children, expected "
                "exactly 2 (probe, build); refusing to run an unverified plan."
            ),
        )
    probe, build = children
    join_type = join.get("extra_info", {}).get("Join Type")
    probe_name = str(probe.get("name", "")).upper()
    build_name = str(build.get("name", "")).upper()
    if join_type != "LEFT" or "ARROW" not in probe_name or "PARQUET" not in build_name:
        raise ExecutionError(
            code="out_of_core_fk_join_plan_unverified",
            message=(
                "the unordered join's physical plan did not keep the written join "
                f"order (child probe / deduplicated-parent build): Join Type="
                f"{join_type!r}, probe operator={probe.get('name')!r}, build "
                f"operator={build.get('name')!r}. A regained global sort or a "
                "flipped build side would make the join hold an O(child) resident "
                "hash table, breaking the never-OOM memory model; refusing to run "
                "an unverified plan."
            ),
        )


def _contiguity_error(detail: str) -> ExecutionError:
    return ExecutionError(
        code="out_of_core_fk_reorder_contiguity",
        message=(
            "the order-restored FK join output is not exactly the child's own "
            f"0..N-1 row_nr domain: {detail}. N is taken from the INDEPENDENT "
            "child-stage count (the SpillChildKeys row count), never inferred "
            "from the join output itself, so a lost suffix fails closed instead "
            "of silently self-validating as a shorter dense range. This is an "
            "all-or-nothing failure: no partial result from this iterator is a "
            "valid FK output."
        ),
    )


class StreamFkJoiner:
    """One streamed FK join per edge, emitting an ordered FK-output reader.

    Lifecycle, one LAZILY-opened DuckDB connection per edge (spillable, no
    materialized parent):

    1. Construct: resolve the fixed `output_types` from schemas (may raise
       fail-closed before any connection is opened). NO connection is opened
       here (P4-A.3 Task 2) -- `_ensure_conn()` opens and configures one
       (single-threaded, pinned-optimizer pragmas, `parent_keys` as a
       `read_parquet` VIEW) on first use, so a joiner that never joins never
       leaks one.
    2. `begin_staging()` then `stage_batch(source_batch)` per raw child batch
       (or `stage_keys(iter)` for the whole child at once): append the
       `(row_nr, join_key, src)` keys, GLOBALLY numbered, to a `SpillChildKeys`
       Arrow-IPC file -- never a resident structure, so the child's own row
       count no longer sets a memory floor. `self._staged_rows` (the count of
       everything staged) is this edge's INDEPENDENT child-row-count witness,
       later used as `run_ordered_join`'s contiguity target.
    3. `finalize_staging()`: close the spill's writer so its stream carries
       its end-of-stream marker; idempotent. Must run before any scan below
       opens a reader over it (the driver calls this once, at the
       phase-1/phase-2 boundary; `stage_keys` calls it automatically for a
       single-shot caller).
    4. `total_orphans()`: the FAIL-policy anti-join precount over the whole
       child (the runner raises before any output if it is non-zero). Opens
       its OWN fresh reader over the spill.
    5. EITHER `iter_join_rows(batch_rows)` (the pre-existing ORDERED shim: one
       `LEFT JOIN ... ORDER BY __decoy_row_nr`, DuckDB's own global sort,
       staying live only until Task 6 removes it) OR the new bounded-reorder
       path: `run_ordered_join(batch_rows, run_bytes_cap=..., merge_fan_in=...)`
       drains the UNORDERED join (`_iter_unordered_join_rows`, plan-verified
       via `explain_join`'s structural guard) into a `BoundedExternalSorter`
       and returns an owning, order-restored, contiguity-guarded iterator.
       Either way, each RAW join-result batch carries `__decoy_row_nr`,
       `__decoy_fk_join_key`, one `__decoy_src_i` per child column,
       `__decoy_parent_match`, and one `__decoy_parent_masked_i` per child
       column -- exactly the columns `resolve_batch` needs.
    6. `resolve_batch(join_rows)`: resolve one payload-aligned slice of those
       raw join rows into FK output arrays, accumulating `orphan_total` and
       `observed_types`. Called by the driver once per payload-store batch
       (the same source-chunk granularity `main` resolves at), never once per
       reader batch -- those boundaries can differ, and a reader batch that
       coalesces a matched-bool run beside an orphan-int run cannot always be
       resolved as a single unit (Codex HIGH finding).
    7. `close()`.
    """

    def __init__(
        self,
        *,
        edge: RelationshipEdge,
        parent_relation: ParentKeyRelation,
        child_key_types: tuple[pa.DataType, ...],
        temp_dir: Path,
        memory_limit: str | None = None,
        remap_seeds: tuple[ColumnSeed, ...] | None = None,
        job_seed: bytes | None = None,
    ) -> None:
        if len(child_key_types) != len(edge.child_columns):
            raise ExecutionError(
                code="out_of_core_child_key_types_mismatch",
                message="one child key type is required per FK child column.",
            )
        if edge.orphan_policy is OrphanPolicy.REMAP:
            if remap_seeds is None or job_seed is None:
                raise ExecutionError(
                    code="out_of_core_remap_seeds_missing",
                    message="orphan_policy='remap' requires the parent key seeds and job seed.",
                )
            for parent_col, seed in zip(edge.parent_columns, remap_seeds, strict=True):
                if seed is None:
                    raise ExecutionError(
                        code="out_of_core_parent_seed_missing",
                        message=f"parent key {edge.parent_table}.{parent_col} is not in the plan.",
                    )
        self._edge = edge
        self._relation = parent_relation
        self._remap_seeds = remap_seeds
        self._job_seed = job_seed
        self._child_columns = edge.child_columns
        # The key schema mirrors `_join.py::_child_key_schema` exactly (row_nr,
        # join_key, then one raw src column per child key column), built from the
        # source key types the runner passes so the empty child_keys table can be
        # created before any batch is staged.
        self._key_schema = pa.schema(
            [
                pa.field("__decoy_row_nr", pa.int64()),
                pa.field("__decoy_fk_join_key", pa.string()),
            ]
            + [
                pa.field(f"__decoy_src_{idx}", child_key_types[idx])
                for idx in range(len(edge.child_columns))
            ]
        )
        # The relation's Parquet footer is the authoritative masked-key type
        # (already through the DuckDB COPY round trip the join result takes).
        relation_schema = pq.read_metadata(parent_relation.path).schema.to_arrow_schema()
        masked_types = tuple(
            relation_schema.field(name).type for name in parent_relation.masked_key_columns
        )
        self._output_types = _resolve_output_types(
            edge=edge,
            masked_types=masked_types,
            child_key_types=child_key_types,
            remap_seeds=remap_seeds,
        )
        self._observed_types: tuple[set[pa.DataType], ...] = tuple(
            set() for _ in edge.child_columns
        )
        self._orphan_total = 0
        self._staged_rows = 0
        self._staged = False
        self._temp_dir = temp_dir
        self._memory_limit = memory_limit
        self._child_keys: SpillChildKeys | None = None
        # No connection is opened here (Codex plan-gate LOW 7 / P4-A.3 Task 2):
        # a joiner that only ever stages keys (or fails before any join runs)
        # never needs one, so `_ensure_conn()` opens and configures it lazily,
        # on first use, rather than unconditionally at construction.
        self._conn: Any = None

    @property
    def output_types(self) -> tuple[pa.DataType, ...]:
        """The fixed Arrow type of each FK output column, in edge order."""
        return self._output_types

    @property
    def observed_types(self) -> tuple[set[pa.DataType], ...]:
        """Pre-cast chunk types seen so far, per FK component.

        `_emit.py` replays the whole-column permissive merge over these to
        recover the value-derived narrowing the fixed schema cannot know up
        front (the documented divergence), on the resident and relation sides.
        """
        return self._observed_types

    @property
    def orphan_total(self) -> int:
        """Running orphan total over the output emitted so far.

        Fully populated once every `resolve_batch` call is made; the runner reads it for
        the WARN aggregation, matching whole-table reporting.
        """
        return self._orphan_total

    def begin_staging(self) -> None:
        """Open the empty, typed `SpillChildKeys` file-backed store.

        Separate from `stage_batch` so the runner can open one store per edge
        and feed every incoming edge from a SINGLE raw source pass (phase 1),
        rather than re-reading the source once per edge.
        """
        if self._staged:
            raise AssertionError("child_keys already staged")
        self._child_keys = SpillChildKeys(self._temp_dir / "child_keys.arrow", self._key_schema)
        self._staged = True

    def stage_batch(self, source_batch: pa.RecordBatch) -> None:
        """Append one raw child batch's keys to the spill with global row_nr.

        The `(row_nr, join_key, src_i)` encoding is `_join.py::_child_key_batches`
        verbatim (reused, not reimplemented); only the row numbers are shifted
        by the running global offset so the whole child is numbered positionally
        across batches, exactly as a single whole-child stage would.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before stage_batch")
        self._check_child_columns(source_batch.schema)
        length = source_batch.num_rows
        if length == 0:
            return
        source_table = pa.Table.from_batches([source_batch])
        for key_batch in _child_key_batches(source_table, self._child_columns, length):
            columns = list(key_batch.columns)
            # pc.* funcs are dynamically generated; stubs miss them.
            columns[0] = pc.add(columns[0], self._staged_rows)  # type: ignore[attr-defined, unused-ignore]
            shifted = pa.record_batch(columns, schema=self._key_schema)
            assert self._child_keys is not None  # noqa: S101 -- begin_staging guarantees this
            self._child_keys.append(shifted)
        self._staged_rows += length

    def finalize_staging(self) -> None:
        """Close the child-key spill's writer so its stream carries its
        end-of-stream marker before either scan below opens a reader over it.

        Idempotent, and safe to call even when `begin_staging` never ran (the
        driver's cleanup guard calls this unconditionally on every joiner it
        opened, mirroring `RawParentKeySpill.finalize()`'s own guard use).
        """
        if self._child_keys is not None:
            self._child_keys.finalize()

    def stage_keys(self, source_batches: Iterable[pa.RecordBatch]) -> None:
        """Stage a whole child (convenience for a single-edge caller/tests)."""
        self.begin_staging()
        for batch in source_batches:
            self.stage_batch(batch)
        self.finalize_staging()

    def total_orphans(self) -> int:
        """Whole-child anti-join orphan count (the FAIL-policy precount).

        Mirrors `_join.py::mask_child_fk`'s FAIL count (`_join.py:129-138`): a
        null child key is never an orphan; a non-null key with no matching
        parent row is. Only the count is resident. Opens its OWN fresh reader
        over the child-key spill (scan 1 of the joiner's two child scans; see
        `iter_join_rows` for scan 2 and why each needs its own reader).
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before total_orphans")
        assert self._child_keys is not None  # noqa: S101 -- the staged-check above guarantees this
        conn = self._ensure_conn()
        join_key = self._relation.join_key_column
        reader = self._child_keys.open_reader()
        try:
            conn.register("child_keys", reader)
            try:
                count = conn.execute(
                    f"""
                    SELECT count(*)
                    FROM child_keys c
                    LEFT JOIN parent_keys p
                      ON c.__decoy_fk_join_key = p.{_q(join_key)}
                    WHERE c.__decoy_fk_join_key IS NOT NULL
                      AND p.{_q(join_key)} IS NULL
                    """
                ).fetchone()[0]
            finally:
                conn.unregister("child_keys")
        finally:
            reader.close()
        return int(count)

    def iter_join_rows(self, batch_rows: int) -> Iterator[pa.RecordBatch]:
        """Yield ordered RAW join-result batches: no resolution, no casting.

        Opens its OWN fresh reader over the child-key spill (scan 2 of the
        joiner's two child scans) and registers it as `child_keys` for this
        query alone -- a DuckDB-registered `RecordBatchReader` is single-pass,
        so reusing `total_orphans`'s reader here would silently return zero
        rows. Runs the single `LEFT JOIN child_keys x parent_keys ORDER BY
        __decoy_row_nr` and reads the ordered result back through
        `to_arrow_reader(batch_rows)` (DuckDB owns the sort + spill; Python sees
        one result batch at a time). Each batch carries `__decoy_row_nr`,
        `__decoy_fk_join_key`, one `__decoy_src_i` per child column,
        `__decoy_parent_match`, and one `__decoy_parent_masked_i` per child
        column -- exactly the columns `resolve_batch` needs. This reader's own
        batch boundaries are DuckDB's, not the source's; the driver is
        responsible for re-batching via `JoinRowCursor.take` before resolving.
        The registration and the child-key reader stay open for as long as
        this generator is (the `finally` below runs on normal exhaustion AND
        on early abandonment, since Python closes a live generator on GC).

        This ORDER BY makes DuckDB run a global sort over the full join
        output -- the never-OOM gap this slice's sorter-backed
        `run_ordered_join` (Task 3) replaces. It stays live here, unchanged,
        as the shim `total_orphans`/tests exercise until that consumer lands;
        removing it is deferred Task 6.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before iter_join_rows")
        assert self._child_keys is not None  # noqa: S101 -- the staged-check above guarantees this
        conn = self._ensure_conn()
        edge = self._edge
        n_components = len(edge.child_columns)
        join_key = self._relation.join_key_column
        select_list = [f"c.{_q('__decoy_row_nr')}", f"c.{_q('__decoy_fk_join_key')}"]
        select_list += [f"c.{_q(f'__decoy_src_{idx}')}" for idx in range(n_components)]
        # Explicit LEFT JOIN match indicator (mirrors _join.py): parent_keys only
        # ever holds non-null join keys, so p's join-key column is NULL iff no
        # parent row matched -- using the masked value's nullness would
        # misclassify a matched-but-null-masked parent as an orphan.
        select_list.append(f"p.{_q(join_key)} AS {_q('__decoy_parent_match')}")
        for idx, masked_column in enumerate(self._relation.masked_key_columns):
            select_list.append(f"p.{_q(masked_column)} AS {_q(f'__decoy_parent_masked_{idx}')}")
        query = f"""
            SELECT {", ".join(select_list)}
            FROM child_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(join_key)}
            ORDER BY c.__decoy_row_nr
        """
        reader = self._child_keys.open_reader()
        conn.register("child_keys", reader)
        try:
            yield from conn.execute(query).to_arrow_reader(batch_rows)
        finally:
            # An abandoned (never fully drained) generator's cleanup can run
            # AFTER `close()` has already torn down the connection -- e.g. a
            # sibling edge's fail-closed error unwinds `stream_table` while
            # THIS edge's cursor is mid-batch, and Python finalizes this
            # generator (GeneratorExit) whenever the `JoinRowCursor` holding
            # it is garbage-collected, which is not ordered against
            # `stream_table`'s own `finally` block closing every joiner.
            # `unregister` is meaningless on an already-closed connection, so
            # skip it rather than raise past a GeneratorExit.
            if self._conn is not None:
                self._conn.unregister("child_keys")
            reader.close()

    def _unordered_join_query(self) -> str:
        """The Task 2 join query, identical to `iter_join_rows`'s SELECT/JOIN
        but with the `ORDER BY` dropped -- shared by `_iter_unordered_join_rows`
        and `explain_join()` so the plan a test inspects is exactly the plan
        the drain actually runs."""
        edge = self._edge
        n_components = len(edge.child_columns)
        join_key = self._relation.join_key_column
        select_list = [f"c.{_q('__decoy_row_nr')}", f"c.{_q('__decoy_fk_join_key')}"]
        select_list += [f"c.{_q(f'__decoy_src_{idx}')}" for idx in range(n_components)]
        select_list.append(f"p.{_q(join_key)} AS {_q('__decoy_parent_match')}")
        for idx, masked_column in enumerate(self._relation.masked_key_columns):
            select_list.append(f"p.{_q(masked_column)} AS {_q(f'__decoy_parent_masked_{idx}')}")
        return f"""
            SELECT {", ".join(select_list)}
            FROM child_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(join_key)}
        """

    def _run_explain_json(self, query: str) -> dict[str, Any]:
        """Parsed `EXPLAIN (FORMAT JSON)` physical plan for `query`.

        Assumes `child_keys` is already registered on this joiner's connection
        (the caller's job, since a fresh reader must back it and EXPLAIN never
        consumes that reader -- it plans without executing). Fails closed
        (`out_of_core_fk_join_plan_unverified`) on any malformed, missing, or
        unparseable EXPLAIN output rather than proceeding on an unverified plan.
        """
        conn = self._ensure_conn()
        rows = conn.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchall()
        raw = next((value for key, value in rows if key == "physical_plan"), None)
        if raw is None:
            raise ExecutionError(
                code="out_of_core_fk_join_plan_unverified",
                message="EXPLAIN (FORMAT JSON) returned no 'physical_plan' row.",
            )
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ExecutionError(
                code="out_of_core_fk_join_plan_unverified",
                message="EXPLAIN (FORMAT JSON) output was not valid JSON.",
            ) from exc
        if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise ExecutionError(
                code="out_of_core_fk_join_plan_unverified",
                message=(
                    f"EXPLAIN (FORMAT JSON) plan had an unexpected top-level shape: {parsed!r}."
                ),
            )
        return parsed[0]

    def _disabled_optimizers(self) -> frozenset[str]:
        conn = self._ensure_conn()
        value = conn.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
        return frozenset(name for name in value.split(",") if name)

    def explain_join(self) -> dict[str, Any]:
        """Structural proof of the unordered join's physical plan.

        Returns `{"plan": <parsed EXPLAIN (FORMAT JSON) root operator>,
        "disabled_optimizers": <frozenset of names currently disabled on this
        connection>}` so a caller (test #5) can assert on operator TYPES and
        pragma state without depending on the query's view aliases. Registers
        its OWN fresh child-key reader for the duration of the EXPLAIN call
        (EXPLAIN never executes the query, so the reader is never consumed)
        and unregisters it before returning, exactly like `total_orphans` and
        `iter_join_rows`'s own single-purpose scans.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before explain_join")
        assert self._child_keys is not None  # noqa: S101 -- the staged-check above guarantees this
        conn = self._ensure_conn()
        reader = self._child_keys.open_reader()
        conn.register("child_keys", reader)
        try:
            plan = self._run_explain_json(self._unordered_join_query())
            disabled = self._disabled_optimizers()
        finally:
            conn.unregister("child_keys")
            reader.close()
        return {"plan": plan, "disabled_optimizers": disabled}

    def _iter_unordered_join_rows(self, batch_rows: int) -> Iterator[pa.RecordBatch]:
        """Yield UNORDERED raw join-result batches: no `ORDER BY`, no resolution.

        A copy of `iter_join_rows` with the `ORDER BY` dropped: the join
        itself no longer asks DuckDB to sort the whole output, so the never-
        OOM claim shifts onto this method's own build-side pin plus
        `run_ordered_join`'s bounded sorter, instead of DuckDB's unbounded
        global sort. Batches here are UNORDERED (whatever order DuckDB's
        hash-join probe happens to emit them in); the ONLY intended consumer
        is `run_ordered_join`, which drains this into the bounded sorter and
        restores `__decoy_row_nr` order afterward. Runs the SAME fail-closed
        plan verification `explain_join()`'s test exercises, but on every
        real drain (not just the dedicated plan test): dropping the `ORDER BY`
        means this query's build-side pin is the only thing standing between
        a never-OOM parent-build join and an accidental child-build
        regression, so it must never run unverified.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before _iter_unordered_join_rows")
        assert self._child_keys is not None  # noqa: S101 -- the staged-check above guarantees this
        conn = self._ensure_conn()
        query = self._unordered_join_query()
        reader = self._child_keys.open_reader()
        conn.register("child_keys", reader)
        try:
            plan = self._run_explain_json(query)
            _verify_unordered_plan_or_raise(plan)
            yield from conn.execute(query).to_arrow_reader(batch_rows)
        finally:
            # See iter_join_rows's own finally: an abandoned generator's
            # cleanup can run after close() already tore down the connection.
            if self._conn is not None:
                self._conn.unregister("child_keys")
            reader.close()

    def run_ordered_join(
        self,
        batch_rows: int,
        *,
        run_bytes_cap: int,
        merge_fan_in: int = 16,
    ) -> _OrderedJoinRows:
        """Restore `__decoy_row_nr` order over this edge's unordered join output.

        Two distinct lifecycle mechanisms (Codex plan-gate MEDIUM 6 + round-2
        MEDIUM), because a bare generator's `try/finally` does not reliably run
        when a caller abandons the returned iterator before its first `next()`:

        1. EAGER blocking phase, right here, inside `try/finally`: every
           unordered join-row batch is drained into a fresh
           `BoundedExternalSorter` (constructed with an EXPLICIT
           `sort_key_column="__decoy_row_nr"`, never the default -- Codex
           plan-gate LOW 7) while this joiner's DuckDB connection is live, the
           connection is then CLOSED, and `sorter.finish()` runs the bounded
           merge -- so DuckDB's join buffers and the sorter's merge buffers
           are never co-resident. Any failure anywhere in this phase (drain
           error, sorter failure, disk exhaustion, a malformed/unverified
           plan) closes the connection AND the sorter (unlinking every spill
           file) before propagating.
        2. The returned `_OrderedJoinRows` is an OWNING, closeable iterator,
           not a bare generator: its `close()` (idempotent) unlinks the
           sorter's spill and is safe before the first `next()`, after partial
           consumption, or called twice. It also wraps `sorter.iter_ordered()`
           in a fail-closed 0..N-1 contiguity guard against `N`, the
           INDEPENDENT child-stage row count (`self._staged_rows`, the
           `SpillChildKeys` count) -- never inferred from the join output --
           so a lost suffix fails closed instead of silently self-validating
           as a shorter dense range.

        By the time this method RETURNS (successfully), the connection is
        already closed; the only resource the result still owns is the
        sorter's final ordered run on disk.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before run_ordered_join")
        expected_row_count = self._staged_rows
        sorter = BoundedExternalSorter(
            spill_dir=self._temp_dir / "reorder",
            run_bytes_cap=run_bytes_cap,
            merge_fan_in=merge_fan_in,
            sort_key_column="__decoy_row_nr",
        )
        try:
            for batch in self._iter_unordered_join_rows(batch_rows):
                sorter.write(batch)
            # The connection is closed BEFORE finish()'s merge runs, so the
            # DuckDB join's buffers and the sorter's merge buffers are never
            # co-resident (the memory contract this consumer exists to prove).
            self.close()
            sorter.finish()
        except BaseException:
            self.close()
            sorter.close()
            raise
        return _OrderedJoinRows(sorter, expected_row_count)

    def _ensure_conn(self) -> Any:
        """Open and configure this edge's DuckDB connection on first use.

        Lazy (Codex plan-gate LOW 7 / P4-A.3 Task 2): a joiner that never
        needs a connection never opens one to leak. Idempotent -- the pinned
        optimizers and the `parent_keys` view are only ever set up once per
        connection.
        """
        if self._conn is not None:
            return self._conn
        conn = connect_duckdb(temp_dir=self._temp_dir / "duckdb", memory_limit=self._memory_limit)
        try:
            # Single-threaded so the unordered join's physical plan (verified
            # structurally by `_verify_unordered_plan_or_raise`) is deterministic
            # run to run, and so DuckDB's own join+buffer memory stays inside
            # the budget this joiner's caller sized for one thread.
            conn.execute("SET threads = 1")
            # `child_keys` is a registered Arrow RecordBatchReader with no known
            # row count, so DuckDB's cardinality estimator treats it as ~1 row and
            # can swap the LEFT JOIN's build side onto it -- building the hash
            # table on the O(child) side instead of the bounded `parent_keys`
            # VIEW. Disabling `build_side_probe_side` (+ `join_order`, DuckDB
            # 1.5.4, public `duckdb_optimizers()` pragma) forces the planner to
            # keep the written join order, i.e. the bounded parent, as the build
            # side; guarded so an absent/renamed optimizer name on a future
            # DuckDB version is a no-op, not an error.
            _disable_join_optimizers(conn)
            # Parent as a VIEW, never a materialized TEMP TABLE: this joiner runs
            # ONE join against it per edge (unlike the removed per-batch joiner
            # that ran hundreds), so DuckDB reads the relation parquet once as a
            # spillable grace-hash build side -- the same shape `_join.py` uses.
            conn.execute(
                "CREATE TEMP VIEW parent_keys AS SELECT * FROM "
                f"read_parquet({_sql_string(str(self._relation.path))})"
            )
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return conn

    def resolve_batch(self, join_rows: pa.RecordBatch) -> tuple[pa.Array, ...]:
        """Resolve one payload-aligned slice of raw join rows into FK output.

        `join_rows` must be a single, internally consistent slice (typically a
        `JoinRowCursor.take` result sized to one payload-store batch, the same
        source-chunk granularity `main` resolved at). FK columns are produced
        by `_append_output_batch` (the shared orphan-policy code) and cast to
        the fixed `output_types`, with per-slice REMAP minting; `orphan_total`
        and `observed_types` accumulate across every call.
        """
        edge = self._edge
        n_components = len(edge.child_columns)
        remap_values = self._batch_remap_values(join_rows)
        # REMAP indexes remap_values by row_nr; re-base to positional 0..n so
        # the per-slice mint aligns (row_nr is used ONLY as the REMAP index).
        source = self._with_positional_row_nr(join_rows) if remap_values is not None else join_rows
        output_chunks: list[list[pa.Array]] = [[] for _ in range(n_components)]
        self._orphan_total += _append_output_batch(
            source,
            edge=edge,
            remap_values=remap_values,
            output_chunks=output_chunks,
        )
        for idx in range(n_components):
            for chunk in output_chunks[idx]:
                self._observed_types[idx].add(chunk.type)
        return tuple(
            _cast_chunks(output_chunks[idx], self._output_types[idx]) for idx in range(n_components)
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> StreamFkJoiner:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _check_child_columns(self, schema: pa.Schema) -> None:
        for child_col in self._child_columns:
            if schema.get_field_index(child_col) < 0:
                raise ExecutionError(
                    code="out_of_core_child_column_missing",
                    message=f"child source table has no column {child_col!r}.",
                )

    def _batch_remap_values(self, result: pa.RecordBatch) -> tuple[pa.Array, ...] | None:
        if self._edge.orphan_policy is not OrphanPolicy.REMAP:
            return None
        if self._remap_seeds is None or self._job_seed is None:
            raise AssertionError("remap seeds checked at construction")
        # Mint over ALL of the batch's keys (the kernels are per-value
        # deterministic, so orphan positions carry the same values either way),
        # from the raw src columns that rode through the join unchanged. Bounded
        # by the result batch size, never child cardinality.
        remapped = []
        for idx, seed in enumerate(self._remap_seeds):
            normalized = [
                None if fk_key_value(value) is NULL_FK_KEY else fk_key_value(value)
                for value in result.column(f"__decoy_src_{idx}").to_pylist()
            ]
            remapped.append(
                mask_column(pa.array(normalized, from_pandas=True), seed, self._job_seed)
            )
        return tuple(remapped)

    def _with_positional_row_nr(self, result: pa.RecordBatch) -> pa.RecordBatch:
        columns = list(result.columns)
        idx = result.schema.get_field_index("__decoy_row_nr")
        columns[idx] = pa.array(range(result.num_rows), type=pa.int64())
        return pa.record_batch(columns, schema=result.schema)


class _OrderedJoinRows:
    """Owning, closeable iterator over one edge's order-restored join rows.

    Returned by `StreamFkJoiner.run_ordered_join`, which has ALREADY drained
    the unordered join into `sorter` and closed the DuckDB connection by the
    time this object exists -- the only resource it owns is the sorter's
    final ordered run on disk. Implements `__iter__`/`__next__`, the context-
    manager protocol, and an idempotent `close()` (Codex plan-gate MEDIUM 6):
    a bare generator's `try/finally` does not run reliably when a caller
    abandons it before the first `next()` (cleanup would wait for
    nondeterministic GC), so the consumer contract is `with
    joiner.run_ordered_join(...) as rows:` or an explicit `close()` -- the
    only reliable cleanup for abandonment before first use, after partial
    consumption, or on error. The iterator also self-closes on normal
    exhaustion AND on any exception (including its own contiguity failure),
    so no exit path leaks the sorter's spill.

    Wraps `sorter.iter_ordered()` in the fail-closed 0..N-1 contiguity guard:
    `N` (`expected_row_count`) is the INDEPENDENT child-stage row count (the
    `SpillChildKeys` count `run_ordered_join` captured BEFORE the join ran),
    never inferred from the join output itself, so a lost suffix cannot
    silently self-validate as a shorter dense range. A gap is detected only
    when the batch AFTER it arrives (or, for a lost suffix, only once the
    whole stream is exhausted) -- earlier batches have already been yielded
    to the caller by then, so this is an ALL-OR-NOTHING contract: the
    consumer must not commit any batch to durable output until this iterator
    is fully drained without raising.
    """

    def __init__(self, sorter: BoundedExternalSorter, expected_row_count: int) -> None:
        self._sorter = sorter
        self._expected_row_count = expected_row_count
        self._iter = self._guarded_iter()
        self._closed = False

    def __iter__(self) -> _OrderedJoinRows:
        return self

    def __next__(self) -> pa.RecordBatch:
        try:
            return next(self._iter)
        except BaseException:
            # StopIteration (normal exhaustion) and any raised failure
            # (contiguity violation, or anything unexpected) both end this
            # iterator's life -- close it either way, exactly once.
            self.close()
            raise

    def __enter__(self) -> _OrderedJoinRows:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Unlink the sorter's spill. Idempotent; safe before the first
        `next()`, after partial consumption, or called more than once."""
        if self._closed:
            return
        self._closed = True
        self._sorter.close()

    def _guarded_iter(self) -> Iterator[pa.RecordBatch]:
        expected_next = 0
        seen = 0
        for batch in self._sorter.iter_ordered():
            n = batch.num_rows
            if n == 0:
                continue
            row_nr = batch.column("__decoy_row_nr")
            first = row_nr[0].as_py()
            if first != expected_next:
                raise _contiguity_error(
                    f"a batch started at row_nr {first}, expected {expected_next}"
                )
            if n > 1:
                # pc.* funcs are dynamically generated; stubs miss them.
                diffs = pc.subtract(  # type: ignore[attr-defined, unused-ignore]
                    row_nr.slice(1, n - 1), row_nr.slice(0, n - 1)
                )
                if not pc.all(pc.equal(diffs, 1)).as_py():  # type: ignore[attr-defined, unused-ignore]
                    raise _contiguity_error(
                        f"row_nr [{first}..{row_nr[n - 1].as_py()}] is not a "
                        "contiguous ascending run (duplicate or missing value)"
                    )
            expected_next = first + n
            seen += n
            yield batch
        if seen != self._expected_row_count or expected_next != self._expected_row_count:
            raise _contiguity_error(
                f"expected exactly {self._expected_row_count} contiguous rows (the "
                f"independent child-stage count) but the ordered stream produced "
                f"{seen} row(s), ending at row_nr {expected_next - 1}"
            )


class JoinRowCursor:
    """Forward-only cursor over one edge's ordered raw join-row reader.

    Wraps `StreamFkJoiner.iter_join_rows`, whose own batch boundaries are
    DuckDB's (not the payload store's). `take(n, expected_row_nr_start)`
    returns exactly `n` raw join rows as one `pa.RecordBatch`, slicing across
    reader batch boundaries, holding at most one reader batch plus a slice
    offset, so memory stays bounded regardless of alignment. The result is fed
    straight to `StreamFkJoiner.resolve_batch`.

    Correctness backbone: the join output covers every child row exactly once,
    contiguously numbered 0..N-1 in `ORDER BY` order. Unlike the removed
    `FkOutputCursor` (which only checked that the join reader was contiguous
    with ITS OWN running count -- a check that a two-read design's second pass
    could satisfy even while silently misaligned to the first), `take` also
    asserts the join reader's current position equals `expected_row_nr_start`,
    the row_nr the DRIVER read off the payload store -- an artifact of the
    SAME single source read as the join keys. That makes this an identity
    check between two artifacts of one read, not a hope that two reads agree.
    """

    def __init__(self, reader: Iterator[pa.RecordBatch], join_columns: tuple[str, ...]) -> None:
        self._reader = reader
        self._join_columns = join_columns
        self._batch: pa.RecordBatch | None = None
        self._offset = 0
        self._emitted = 0

    def take(self, n: int, expected_row_nr_start: int) -> pa.RecordBatch:
        """Return exactly `n` raw join rows starting at `expected_row_nr_start`."""
        if self._emitted != expected_row_nr_start:
            raise _row_alignment_error(
                f"the join reader is positioned at row_nr {self._emitted}, but the "
                f"payload store's next batch starts at row_nr {expected_row_nr_start}"
            )
        if n == 0:
            return self._schema_probe().slice(0, 0)
        collected: list[pa.RecordBatch] = []
        remaining = n
        while remaining > 0:
            batch = self._current()
            available = batch.num_rows - self._offset
            take_k = min(available, remaining)
            collected.append(batch.slice(self._offset, take_k))
            self._offset += take_k
            self._emitted += take_k
            remaining -= take_k
        return collected[0] if len(collected) == 1 else _concat_join_row_batches(collected)

    def assert_exhausted(self) -> None:
        """Fail closed unless every raw join row has been consumed.

        The other half of the alignment guard: the payload store must not be
        SHORTER than the join output either, or the tail join rows would be
        silently dropped.
        """
        if self._batch is not None and self._offset < self._batch.num_rows:
            raise _row_alignment_error(
                f"{self._batch.num_rows - self._offset} join row(s) left unconsumed"
            )
        try:
            next(self._reader)
        except StopIteration:
            return
        raise _row_alignment_error("join row reader has rows the payload store never consumed")

    def _schema_probe(self) -> pa.RecordBatch:
        # A zero-row take still needs a batch of the right schema; reuse the
        # last-seen reader batch if there is one, otherwise pull the next one
        # (its rows are left untouched, offset stays where it was).
        return self._batch if self._batch is not None else self._current()

    def _current(self) -> pa.RecordBatch:
        batch = self._batch
        if batch is None or self._offset >= batch.num_rows:
            batch = self._advance()
            self._batch = batch
            self._offset = 0
        return batch

    def _advance(self) -> pa.RecordBatch:
        try:
            batch = next(self._reader)
        except StopIteration:
            raise _row_alignment_error(
                "join row reader exhausted before the payload store did "
                "(join output shorter than the masked payload)"
            ) from None
        row_nr = batch.column("__decoy_row_nr")
        first = row_nr[0].as_py()
        last = row_nr[len(row_nr) - 1].as_py()
        # Each ordered batch must start exactly where the running count left off
        # and be a contiguous ascending run, so positional slicing equals
        # row_nr slicing. This is the join reader's OWN internal contiguity,
        # independent of (and in addition to) the cross-artifact check in take().
        if first != self._emitted or last != self._emitted + len(row_nr) - 1:
            raise _row_alignment_error(
                f"join row_nr [{first}..{last}] does not match the expected "
                f"contiguous range starting at {self._emitted}"
            )
        return batch


def _concat_join_row_batches(batches: list[pa.RecordBatch]) -> pa.RecordBatch:
    table = pa.Table.from_batches(batches).combine_chunks()
    return table.to_batches()[0]


def _row_alignment_error(detail: str) -> ExecutionError:
    return ExecutionError(
        code="out_of_core_fk_row_alignment",
        message=(
            "out-of-core FK join output could not be row-aligned to the masked "
            f"payload: {detail}. The join row_nr must match the payload row_nr "
            "captured in the single source read; this is a fail-closed internal "
            "guard, never silent truncation or misalignment."
        ),
    )


__all__ = ["JoinRowCursor", "StreamFkJoiner"]
