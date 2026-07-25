# OOC-B Final Architecture: Unordered DuckDB Join + Bounded External row_nr Reorder

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Provenance.** This is the FINAL OOC-B architecture, decided by a cross-model architecture consult (Codex gpt-5.6-sol, 2026-07-22) after the GCP 200M@8GB run proved a DuckDB-ordered streaming join cannot reach never-OOM (peak RSS ~21 GB, indistinguishable from the unfixed regression; the dominant unbounded structure is the global `ORDER BY __decoy_row_nr` external sort, which `memory_limit` does not bound). PR #108 reverts main to the bounded O(parent) `_batch_join.py` path as the interim; the single-source-read scaffolding this plan rebuilds from is preserved on `origin/fix/ooc-b-memory-streaming-join`. Do not redesign the mechanism; it is fixed by the consult.

**Goal:** An out-of-core FK join that completes at ARBITRARY row count with resident memory independent of both parent and child row counts (spill disk scales with data), preserving single-source-read and byte-parity to the pandas oracle.

**Architecture:** Per FK edge, DuckDB performs ONE unordered, spillable, forced-parent-build external hash join (child on the left as a streamed Arrow IPC scan, deduplicated parent on the right as a `read_parquet` view, `build_side_probe_side` and `join_order` optimizers disabled, threads=1, NO `ORDER BY`). Decoy then restores `__decoy_row_nr` order with its OWN explicitly byte-capped external sorter: byte-capped sorted runs spilled to Arrow IPC, capped-fan-in k-way merge passes that **collapse each edge all the way down to a SINGLE ordered run during phase 2**, and a final streaming read of that one run that fail-closed asserts the output is exactly the contiguous sequence 0..N-1 before feeding the existing `JoinRowCursor`. The collapse-to-one-run is load-bearing for the memory envelope: the expensive fan-in-way merges (fan_in open readers) run to completion, one edge at a time, and are torn down inside phase 2, so phase 3 holds exactly ONE run reader plus one batch PER EDGE, never `edges x fan_in` readers. Incoming FK edges execute SEQUENTIALLY so DuckDB blocking operators from different edges are never co-resident, and parent-relation last-write-wins dedup is split into single-blocking-operator queries (stage, winners, join-back). Everything else (SpillChildKeys, SpillPayloadStore, raw-parent-key IPC staging, global monotonic `__decoy_row_nr`, `resolve_batch` + REMAP semantics, payload-batch-aligned resolution) is retained from the preserved scaffolding.

**Honest customer guarantee (what "done" delivers, to be documented verbatim):** "The architecture holds no O(parent) or O(child) resident structure. Working memory is O(DuckDB budget + sort-run budget + merge fan-in buffers + Arrow batches), independent of row count. Parent, child keys, payload, join state, and ordered runs all spill to disk. Completes at any N for which sufficient spill disk exists and individual values stay within supported Arrow/DuckDB limits. This is a tested process-memory envelope for the pinned DuckDB version, not a raw memory_limit promise."

**Tech Stack:** DuckDB 1.5.4 (public core API only), PyArrow (IPC streams, `pyarrow.compute` sort/take), the preserved `out_of_core/` single-read modules, pytest + Hypothesis parity harness.

## Global Constraints

- DuckDB PUBLIC CORE API ONLY. No source customization, no community/third-party extensions, no internals/monkeypatching. Annotate any dependence on documented-but-subtle behavior.
- Do NOT rely on DuckDB probe-side output order for correctness. It is NOT guaranteed and can change across versions/spill; the external sorter is what guarantees order. Unit-test the sorter with deliberately SHUFFLED join output so correctness cannot accidentally depend on DuckDB order.
- Pin/certify DuckDB 1.5.4 for this path (external hash join integrated since 1.2.0; do not claim the same for the >=0.10.0 floor without separate tests). If `build_side_probe_side` is absent from `duckdb_optimizers()`, fail closed or route to the O(parent) path. Never silently no-op (that silently restores an O(child) build).
- Byte-parity to `tests/parity/test_out_of_core_fk_parity.py` is the hard gate; honor the accepted-divergence contract (`tests/parity/SEMANTIC_DIFFERENCES.md`) for nested/struct/map keys + zero-row empty types. Do NOT touch `_join.py::mask_child_fk` (resident oracle path). No em-dashes in prose.
- Per-edge DuckDB connection config (Codex, copy exactly): `memory_limit` = 50-60% of the process RAM ceiling (headroom for Arrow/sorter/Python/allocators; DuckDB's `memory_limit` covers its buffer manager, not all allocations); `threads=1`; `preserve_insertion_order=false`; `max_temp_directory_size` set; `disabled_optimizers='join_order,build_side_probe_side'` (public, documented). Child on LEFT, deduplicated parent on RIGHT. CRUCIALLY: NO `ORDER BY`.
- Single source read is invariant: the raw child is read exactly once in phase 1; every later pass reads Decoy spills, never the source. The FAIL orphan precount rereads the child-key spill via a FRESH IPC reader.
- Resolution stays at payload-store batch boundaries: `cursor.take(payload_batch_rows, expected_row_nr)` feeding `resolve_batch` is unchanged, preserving per-batch REMAP minting and the value-derived inference boundary.
- Final merge MUST assert fail-closed contiguity: exactly N rows; first/last row_nr are 0 and N-1; every adjacent row_nr differs by 1; no duplicate or missing row_nr.
- Allocator env for every capped/scale measurement: `ARROW_DEFAULT_MEMORY_POOL=system`, `MALLOC_ARENA_MAX=2`, exported BEFORE process start (the probe's `--mem-cap-mb` path does not auto-set them; a missing export spuriously OOMs on address-space reservation).
- Pre-GA, fix-forward on a branch. Repo comment rule: explain why, not what; no references to the current task/PR/author.

## File Structure

```
src/decoy_engine/execution/out_of_core/
  _external_sort.py       NEW   ExternalRowNrSorter: byte-capped runs, k-way merge,
                                fail-closed contiguity assert (Task 2)
  _stream_join.py         MOD   drop ORDER BY; lazy per-edge connection; fail-closed
                                optimizer/version guard; run_ordered_join() wiring the
                                sorter (Tasks 3, 4, 6)
  _stream_driver.py       MOD   sequential per-edge phase 2 (join + reorder one edge
                                at a time) (Task 7)
  _relation.py            MOD   last-write-wins dedup split into two single-blocking-
                                operator queries with an on-disk winners file (Task 5)
  _duckdb.py              MOD   threads / max_temp_directory_size connection params
                                (Task 3)
  _memory_estimate.py     MOD   ReorderBudgets resolver (Task 7); split the
                                O(parent) preflight into resident-only floor +
                                stream bounded-envelope check (Task 8)
src/decoy_engine/execution/
  _pipeline_route_exec.py   MOD   route seam: pick stream vs resident path, call
                                  the route-matched preflight, stop dividing the
                                  resident cap by incoming_edges+1 (Task 8)

tests/unit/execution/
  test_external_sort.py                 NEW   shuffled-input sorter unit tests (Task 2)
  test_out_of_core_stream_join.py       MOD   unordered-join + guard + ordered-pipeline
                                              tests (Tasks 3, 4, 6)
  test_out_of_core_relation.py          MOD   split-dedup equivalence tests (Task 5)
  test_out_of_core_runner_streaming.py  MOD   sequential-edge lifecycle expectations
                                              (Task 7)
  test_pipeline_route_exec.py           MOD   stream-vs-resident route selection +
                                              preflight split (Task 8)
tests/perf/
  test_out_of_core_memory_sentinel.py   MOD   dual-dimension plateau + spill-evidence
                                              gates (Task 9)
scripts/
  ooc_child_key_plateau_probe.py        MOD   --grow child|parent axis (Task 9)
  ooc_join_order_probe.py               NEW   diagnostic order recorder, never an
                                              order assert (Task 9)
```

Responsibility boundaries: `_external_sort.py` knows nothing about FK semantics (it sorts any batch stream carrying a dense unique `__decoy_row_nr` column); `_stream_join.py` owns the join SQL, connection lifecycle, and the sorter hookup; `_stream_driver.py` owns edge sequencing; `_relation.py` owns dedup; budgets live in `_memory_estimate.py` so every byte number has one home.

---

## Task 1: Restore the single-read scaffolding on a fresh branch

The scaffolding to rebuild from lives on `origin/fix/ooc-b-memory-streaming-join` (SpillChildKeys, SpillPayloadStore, RawParentKeySpill, StreamFkJoiner, JoinRowCursor, the three-phase driver, and their tests). Main is being reverted to the O(parent) `_batch_join.py` path by PR #108, so a plain branch off main would NOT contain these files.

**Files:**
- Branch only; no code edits in this task.

- [ ] **Step 1: Branch from the preserved fix branch**

```bash
cd /home/cam/vscode/decoy-engine
git fetch origin
git checkout -b feat/ooc-b-external-reorder origin/fix/ooc-b-memory-streaming-join
```

- [ ] **Step 2: Merge current main (post PR #108) and resolve in favor of the scaffolding**

```bash
git merge origin/main
```

If the merge conflicts (the #108 revert deletes/rewrites files this branch keeps), resolve with this rule: under `src/decoy_engine/execution/out_of_core/`, `tests/unit/execution/test_out_of_core_*`, `tests/perf/test_out_of_core_memory_sentinel.py`, and `scripts/ooc_child_key_plateau_probe.py`, take THIS branch's side (`git checkout --ours -- <path>`); everywhere else take main's side. If `_batch_join.py` reappears from main alongside `_stream_join.py`, keep both for now; `_runner.py` must import `_stream_driver.stream_table` (this branch's wiring), and Task 9 removes any dead remainder. If PR #108 is not yet merged when you start, skip this step and rebase before the PR instead.

- [ ] **Step 3: Verify the baseline is green**

```bash
uv run pytest tests/parity/test_out_of_core_fk_parity.py tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_runner_streaming.py tests/unit/execution/test_out_of_core_child_key_spill.py -q
```

Expected: all pass. (This branch's known defect is the memory plateau at scale, not correctness; every parity/permutation gate was green at 9c9f44b.)

- [ ] **Step 4: Define the shared edge fixtures up front (so every later task commits green)**

Every later task's tests reference the same handful of fixtures. Define them ONCE now, in a shared helper module, with a trivial smoke test, and commit them green. This is deliberate: it removes forward references between tasks and lets each subsequent task be a single green commit (no red-until-a-later-task, no `xfail` markers to remove). The fixtures wrap the restored scaffolding plus the resident `_join.py::mask_child_fk` oracle; nothing here depends on Tasks 2-8.

Create `tests/unit/execution/_ooc_fixtures.py` exposing pytest fixtures (registered via `tests/unit/execution/conftest.py`):

- `simple_edge_fixture`: one parent + one child (no orphans, `OrphanPolicy.ERROR`-free), `.make_joiner(tmp_path) -> StreamFkJoiner`, `.child_batches`, `.n_child_rows`, `.oracle_resolved` (resolved batches from the resident `mask_child_fk` path on the same inputs and seed).
- `remap_edge_fixture`: parent/child with orphans under `OrphanPolicy.REMAP`; adds `.edge`, `.payload_batch_sizes` (deliberately straddling the reader batch size), `.oracle_resolved_per_payload_batch`.
- `orphan_edge_fixture`: parent/child with a known orphan count; adds `.n_orphans`, `.corrupt_source()` (mutates the fixture's on-disk source to prove the precount reads the spill, not the source).
- `fail_policy_route_fixture`: a full single-table route under `OrphanPolicy.FAIL` with orphans present; `.run()` drives `stream_table` to completion, `.sink_batches_written` counts batches handed to the sink.
- `two_edge_route`: a two-incoming-edge route; `.run()` drives `stream_table`.

Each fixture's `make_joiner`/`run` builds on the restored `StreamFkJoiner` and `_stream_driver.stream_table` exactly as the pre-existing tests already construct them (lift those builders, do not invent new ones). Add one smoke test asserting each fixture constructs and its oracle is non-empty:

```python
def test_fixtures_construct(simple_edge_fixture, remap_edge_fixture, orphan_edge_fixture, tmp_path):
    assert simple_edge_fixture.n_child_rows > 0
    assert simple_edge_fixture.oracle_resolved  # resident-oracle golden is populated
    assert orphan_edge_fixture.n_orphans > 0
    assert remap_edge_fixture.payload_batch_sizes  # misaligned sizes defined
```

Run: `uv run pytest tests/unit/execution/test_out_of_core_fixtures_smoke.py -q` (put the smoke test there). Expected: green.

- [ ] **Step 5: Commit the merge and fixtures**

```bash
git add src/decoy_engine/execution/out_of_core/ tests/unit/execution/ tests/perf/test_out_of_core_memory_sentinel.py scripts/ooc_child_key_plateau_probe.py
git commit -m "chore: restore single-read OOC scaffolding and shared edge fixtures onto post-revert main"
```

(Explicit paths, not `git add -A`: the merge touches only the OOC subtree and its tests/probes; an `-A` here would also stage any unrelated working-tree drift. If the merge produced conflicts outside these paths, resolve and add them by name.)

---

## Task 2: `ExternalRowNrSorter` with shuffled-input unit tests

The Decoy-owned bounded external sorter: accumulate unordered join-output batches to a byte cap, sort each run by `__decoy_row_nr`, spill runs to Arrow IPC, merge with capped fan-in, stream the final merge with a fail-closed contiguity assert.

**Files:**
- Create: `src/decoy_engine/execution/out_of_core/_external_sort.py`
- Test: `tests/unit/execution/test_external_sort.py`

**Interfaces:**
- Consumes: any `pa.RecordBatch` stream whose schema contains an int64 `__decoy_row_nr` column holding a dense permutation of `0..N-1`.
- Produces: `ExternalRowNrSorter(temp_dir: Path, run_bytes_cap: int, merge_fan_in: int = 16, batch_rows: int = 65_536)` with `.write(batch)`, `.finish()`, `.iter_ordered(expected_rows: int) -> Iterator[pa.RecordBatch]`, `.close()`. Task 4's `StreamFkJoiner.run_ordered_join` is the production caller.

- [ ] **Step 1: Write the failing tests (shuffled input is mandatory)**

```python
"""Unit tests for the bounded external row_nr reorder.

Every test feeds DELIBERATELY SHUFFLED input: correctness must come from the
sorter, never from any accidental ordering of the producer (the production
producer is a DuckDB join whose output order is NOT a contract).
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._external_sort import ExternalRowNrSorter


def _shuffled_batches(n_rows: int, batch_rows: int, seed: int) -> list[pa.RecordBatch]:
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n_rows)
    batches = []
    for start in range(0, n_rows, batch_rows):
        chunk = perm[start : start + batch_rows]
        batches.append(
            pa.record_batch(
                [
                    pa.array(chunk, type=pa.int64()),
                    # Value derived from row_nr so misordering is detectable per row.
                    pa.array(chunk * 3, type=pa.int64()),
                ],
                names=["__decoy_row_nr", "payload"],
            )
        )
    return batches


def _drain(sorter: ExternalRowNrSorter, n_rows: int) -> pa.Table:
    return pa.Table.from_batches(list(sorter.iter_ordered(expected_rows=n_rows)))


def test_shuffled_input_comes_back_in_row_nr_order(tmp_path):
    n = 10_000
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=32 * 1024, batch_rows=512)
    for batch in _shuffled_batches(n, 512, seed=7):
        sorter.write(batch)
    sorter.finish()
    out = _drain(sorter, n)
    assert out.column("__decoy_row_nr").to_pylist() == list(range(n))
    assert out.column("payload").to_pylist() == [i * 3 for i in range(n)]


def test_multiple_runs_collapse_to_single_run(tmp_path):
    # Tiny cap + fan_in=2 forces many runs and >1 merge pass; finish() must
    # collapse ALL the way to ONE run so phase-3 reads a single reader per edge.
    n = 4_096
    sorter = ExternalRowNrSorter(
        temp_dir=tmp_path, run_bytes_cap=4 * 1024, merge_fan_in=2, batch_rows=128
    )
    for batch in _shuffled_batches(n, 128, seed=11):
        sorter.write(batch)
    sorter.finish()
    assert sorter.run_count_after_finish == 1  # collapsed to a single run
    out = _drain(sorter, n)
    assert out.column("__decoy_row_nr").to_pylist() == list(range(n))


def test_wide_batch_is_byte_sliced_and_run_bytes_bounded(tmp_path):
    # A single wide input batch (much larger than the cap) must be sliced by
    # bytes before buffering; no run file exceeds ~run_bytes_cap on disk.
    cap = 64 * 1024
    n = 2_000
    rng = np.random.default_rng(5)
    perm = rng.permutation(n)
    wide = pa.record_batch(
        [
            pa.array(perm, type=pa.int64()),
            pa.array([("x" * 512) for _ in range(n)]),  # ~512B/row payload
        ],
        names=["__decoy_row_nr", "payload"],
    )
    assert wide.nbytes > cap  # precondition: one batch exceeds the cap
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=cap, batch_rows=256)
    sorter.write(wide)
    sorter.finish()
    for run in tmp_path.glob("run_*.arrow"):
        # Each spilled run was built from at most one cap's worth of buffered
        # input; allow 2x slack for Arrow framing/overhead, but not unbounded.
        assert run.stat().st_size <= 2 * cap
    assert _drain(sorter, n).column("__decoy_row_nr").to_pylist() == list(range(n))


def test_single_row_wider_than_cap_fails_closed(tmp_path):
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=64, batch_rows=8)
    fat = pa.record_batch(
        [pa.array([0], type=pa.int64()), pa.array(["z" * 4096])],
        names=["__decoy_row_nr", "payload"],
    )
    with pytest.raises(ExecutionError, match="out_of_core_fk_reorder_contiguity"):
        sorter.write(fat)


def test_output_batches_are_bounded(tmp_path):
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=8 * 1024, batch_rows=100)
    for batch in _shuffled_batches(1_000, 100, seed=3):
        sorter.write(batch)
    sorter.finish()
    assert all(b.num_rows <= 100 for b in sorter.iter_ordered(expected_rows=1_000))


def test_duplicate_row_nr_fails_closed(tmp_path):
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=1 << 20, batch_rows=64)
    rows = [0, 1, 2, 2]  # duplicate
    sorter.write(
        pa.record_batch(
            [pa.array(rows, type=pa.int64()), pa.array(rows, type=pa.int64())],
            names=["__decoy_row_nr", "payload"],
        )
    )
    sorter.finish()
    with pytest.raises(ExecutionError, match="out_of_core_fk_reorder_contiguity"):
        _drain(sorter, 4)


def test_missing_row_nr_fails_closed(tmp_path):
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=1 << 20, batch_rows=64)
    rows = [0, 1, 3]  # 2 missing
    sorter.write(
        pa.record_batch(
            [pa.array(rows, type=pa.int64()), pa.array(rows, type=pa.int64())],
            names=["__decoy_row_nr", "payload"],
        )
    )
    sorter.finish()
    with pytest.raises(ExecutionError, match="out_of_core_fk_reorder_contiguity"):
        _drain(sorter, 3)


def test_short_stream_fails_closed_on_expected_rows(tmp_path):
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=1 << 20, batch_rows=64)
    sorter.write(
        pa.record_batch(
            [pa.array([0, 1], type=pa.int64()), pa.array([0, 3], type=pa.int64())],
            names=["__decoy_row_nr", "payload"],
        )
    )
    sorter.finish()
    with pytest.raises(ExecutionError, match="out_of_core_fk_reorder_contiguity"):
        _drain(sorter, 5)


def test_zero_rows_yields_nothing(tmp_path):
    sorter = ExternalRowNrSorter(temp_dir=tmp_path, run_bytes_cap=1 << 20, batch_rows=64)
    sorter.finish()
    assert list(sorter.iter_ordered(expected_rows=0)) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/execution/test_external_sort.py -q`
Expected: FAIL with `ModuleNotFoundError: ... _external_sort`.

- [ ] **Step 3: Implement `_external_sort.py`**

```python
"""Bounded external row_nr reorder for the out-of-core stream FK join.

DuckDB's JOIN does not guarantee input order (order-preservation contract:
https://duckdb.org/docs/lts/sql/dialect/order_preservation), and a global
ORDER BY over the join output is a blocking external sort whose operator
state `memory_limit` does not bound (measured 2026-07-22: 200M rows at an
8GB cap OOMs at ~21 GB RSS, while the same join WITHOUT the sort plateaus
flat). This module restores `__decoy_row_nr` order OUTSIDE DuckDB with the
classic external merge sort (byte-capped sorted runs spilled to Arrow IPC,
then capped-fan-in k-way merge passes; Knuth TAOCP vol. 3 sec. 5.4, the
same shape DuckDB and Polars implement internally), with every buffer
explicitly capped by THIS module. Resident memory for the reorder is
O(run_bytes_cap + merge_fan_in x batch), independent of row count.

`__decoy_row_nr` is a dense permutation of 0..N-1: globally unique, one per
child row, and the parent relation is deduplicated to at most one row per
join key, so the LEFT JOIN emits exactly one output row per child row. The
final merged stream must therefore be exactly 0,1,...,N-1. `iter_ordered`
asserts that fail-closed (each batch equals its expected arange, total
equals expected_rows), which catches duplicated, missing, and misordered
rows, including a parent-dedup failure upstream, in one guard.

Byte accounting (the reorder's resident peak, per edge, one edge at a time):
  - Drain+sort phase (DuckDB connection LIVE): duckdb_memory_limit (buffer
    manager) + run_bytes_cap (the buffer being filled) + run_bytes_cap (the
    sort `take()` transient: input + output co-resident briefly). With the
    resolve_reorder_budgets policy (duckdb=50%, run=15% of the budget) this is
    ~50% + 2 x 15% = ~80%, leaving ~20% for Arrow/Python/allocator slack.
  - Merge phase (DuckDB connection CLOSED, its ~50% freed): fan_in resident
    heads, each capped at run_bytes_cap/fan_in bytes, so ~run_bytes_cap total,
    plus a merge-window `take()` transient of ~run_bytes_cap: ~2 x 15% = ~30%.
Peak is the drain+sort phase (~80%), NOT the sum, because the phases do not
overlap (the connection is closed before the merge). `run_bytes_cap` counts
`RecordBatch.nbytes` of buffered input; `_memory_estimate.resolve_reorder_budgets`
owns the fraction policy and must keep 2 x run_fraction + duckdb_fraction below
1.0 with headroom.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_ROW_NR = "__decoy_row_nr"


def _contiguity_error(detail: str) -> ExecutionError:
    return ExecutionError(
        code="out_of_core_fk_reorder_contiguity",
        message=(
            "out-of-core FK reorder produced a non-contiguous row_nr stream: "
            f"{detail}. The merged join output must be exactly 0..N-1; this is "
            "a fail-closed internal guard, never silent truncation, duplication, "
            "or misordering."
        ),
    )


class ExternalRowNrSorter:
    """Byte-capped external sort of a batch stream by its `__decoy_row_nr`."""

    def __init__(
        self,
        *,
        temp_dir: Path,
        run_bytes_cap: int,
        merge_fan_in: int = 16,
        batch_rows: int = 65_536,
    ) -> None:
        if run_bytes_cap <= 0 or merge_fan_in < 2 or batch_rows < 1:
            raise AssertionError("sorter budgets must be positive (fan_in >= 2)")
        self._temp_dir = temp_dir
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._run_bytes_cap = run_bytes_cap
        self._merge_fan_in = merge_fan_in
        self._batch_rows = batch_rows
        # Batches WRITTEN to runs (hence the merge's resident heads) are capped
        # at run_bytes_cap / fan_in bytes, so a fan_in-way merge holds at most
        # ~run_bytes_cap of head data at once. Without this, a wide-row head
        # would be run_bytes_cap each and fan_in heads would blow the budget by
        # fan_in x (the byte-accounting fix for the reorder resident peak).
        self._head_bytes_cap = max(1, run_bytes_cap // merge_fan_in)
        self._buffer: list[pa.RecordBatch] = []
        self._buffer_bytes = 0
        self._runs: list[Path] = []
        self._run_seq = 0
        self._finished = False

    @property
    def run_count_after_finish(self) -> int:
        """Surviving run files after merge passes (test observability only)."""
        if not self._finished:
            raise AssertionError("finish() has not run")
        return len(self._runs)

    def write(self, batch: pa.RecordBatch) -> None:
        if self._finished:
            raise AssertionError("write after finish")
        if batch.num_rows == 0:
            return
        # Byte-cap the INPUT before it is buffered: a single wide batch must not
        # blow run_bytes_cap. Slice by rows into byte-bounded units, flushing
        # whenever the buffer reaches the cap. A single row wider than the cap
        # cannot be bounded, so fail closed rather than silently OOM.
        per_row = max(1, batch.nbytes // batch.num_rows)
        if per_row > self._run_bytes_cap:
            raise _contiguity_error(
                f"a single row is {per_row} bytes, exceeding the sort run cap "
                f"{self._run_bytes_cap}; raise the reorder budget for this schema"
            )
        rows_per_unit = max(1, self._run_bytes_cap // per_row)
        for start in range(0, batch.num_rows, rows_per_unit):
            unit = batch.slice(start, min(rows_per_unit, batch.num_rows - start))
            self._buffer.append(unit)
            self._buffer_bytes += unit.nbytes
            if self._buffer_bytes >= self._run_bytes_cap:
                self._flush_run()

    def finish(self) -> None:
        """Flush the residual run, then merge ALL THE WAY down to a SINGLE run.

        Every intermediate AND final merge pass happens HERE (eagerly), one
        edge at a time, so by the time `iter_ordered` streams there is exactly
        ONE surviving run and reading it opens exactly ONE reader. This is what
        keeps phase-3 residency at one reader plus one batch PER EDGE instead of
        `edges x fan_in`: the fan_in-way merges (fan_in open readers each) are
        confined to this edge's phase-2 window and torn down before the next
        edge opens. A zero-row sort produces zero runs.
        """
        if self._finished:
            return
        if self._buffer:
            self._flush_run()
        self._finished = True
        while len(self._runs) > 1:
            self._runs = self._merge_pass(self._runs)

    def iter_ordered(self, *, expected_rows: int) -> Iterator[pa.RecordBatch]:
        """Stream the single collapsed run, asserting fail-closed 0..N-1.

        finish() collapsed to one run, so this opens ONE reader (no k-way merge
        at read time). The arange-equality check per batch covers first-row,
        last-row, adjacent-difference-1, duplicate, missing, and null row_nr in
        one vectorized comparison; the trailing count check catches truncation.
        """
        if not self._finished:
            raise AssertionError("finish() must run before iter_ordered")
        if len(self._runs) > 1:  # defensive: finish() guarantees <= 1
            raise AssertionError("iter_ordered requires a single collapsed run")
        emitted = 0
        if self._runs:
            reader = pa.ipc.open_stream(str(self._runs[0]))
            try:
                for batch in reader:
                    row_nr = batch.column(_ROW_NR)
                    expected = pa.array(
                        range(emitted, emitted + batch.num_rows), type=pa.int64()
                    )
                    if pc.all(pc.equal(row_nr, expected)).as_py() is not True:
                        raise _contiguity_error(
                            f"batch starting at emitted={emitted} is not the "
                            f"contiguous range [{emitted}..{emitted + batch.num_rows - 1}]"
                        )
                    emitted += batch.num_rows
                    yield batch
            finally:
                reader.close()
        if emitted != expected_rows:
            raise _contiguity_error(
                f"merged stream carried {emitted} rows, expected exactly {expected_rows}"
            )

    def close(self) -> None:
        """Best-effort removal of surviving run files; safe to call twice."""
        for path in self._runs:
            path.unlink(missing_ok=True)
        self._runs = []
        self._buffer = []
        self._buffer_bytes = 0

    def _byte_bounded_rows(self, table: pa.Table) -> int:
        """Rows per emitted batch so no batch exceeds head_bytes_cap bytes.

        Bounds every batch WRITTEN to a run or YIELDED from a merge, so read-
        side head batches are byte-bounded (not just row-bounded); this keeps
        the k-way merge's resident heads inside `fan_in x head_bytes_cap <=
        run_bytes_cap` for wide rows (the merge-resident term of the budget
        accounting).
        """
        if table.num_rows == 0:
            return self._batch_rows
        per_row = max(1, table.nbytes // table.num_rows)
        return max(1, min(self._batch_rows, self._head_bytes_cap // per_row))

    def _flush_run(self) -> None:
        # buffer is already <= run_bytes_cap (write() byte-slices input), so the
        # sort's take() briefly holds ~2x cap; that 2x is folded into the run-cap
        # policy (resolve_reorder_budgets sizes the cap at ~15% of the budget).
        table = pa.Table.from_batches(self._buffer)
        self._buffer = []
        self._buffer_bytes = 0
        indices = pc.sort_indices(table.column(_ROW_NR))
        ordered = table.take(indices).combine_chunks()
        del table, indices
        step = self._byte_bounded_rows(ordered)
        path = self._next_run_path()
        with pa.ipc.new_stream(str(path), ordered.schema) as writer:
            for start in range(0, ordered.num_rows, step):
                length = min(step, ordered.num_rows - start)
                writer.write_batch(ordered.slice(start, length).to_batches()[0])
        self._runs.append(path)

    def _merge_pass(self, runs: list[Path]) -> list[Path]:
        next_runs: list[Path] = []
        for start in range(0, len(runs), self._merge_fan_in):
            group = runs[start : start + self._merge_fan_in]
            if len(group) == 1:
                next_runs.append(group[0])
                continue
            path = self._next_run_path()
            writer: pa.ipc.RecordBatchStreamWriter | None = None
            for batch in self._merge_stream(group):
                if writer is None:
                    writer = pa.ipc.new_stream(str(path), batch.schema)
                writer.write_batch(batch)
            if writer is not None:
                writer.close()
            for spent in group:
                spent.unlink(missing_ok=True)
            next_runs.append(path)
        return next_runs

    def _merge_stream(self, paths: list[Path]) -> Iterator[pa.RecordBatch]:
        """K-way merge of sorted runs, batch-at-a-time, vectorized.

        Safe-emission frontier: each run is sorted, so any row_nr <= the
        minimum of the runs' buffered-head maxima is guaranteed already
        buffered; those rows are gathered from every head, sorted once
        (bounded by fan_in x batch rows), and emitted. At least one head is
        fully consumed per iteration, so progress is guaranteed with no
        per-row Python loop.
        """
        readers = [pa.ipc.open_stream(str(p)) for p in paths]
        try:
            heads: list[pa.RecordBatch | None] = [_next_batch(r) for r in readers]
            while True:
                live = [h for h in heads if h is not None]
                if not live:
                    return
                frontier = min(
                    h.column(_ROW_NR)[h.num_rows - 1].as_py() for h in live
                )
                parts: list[pa.RecordBatch] = []
                for idx, head in enumerate(heads):
                    if head is None:
                        continue
                    keep = pc.sum(
                        pc.less_equal(head.column(_ROW_NR), frontier)
                    ).as_py() or 0
                    if keep:
                        parts.append(head.slice(0, keep))
                    if keep == head.num_rows:
                        heads[idx] = _next_batch(readers[idx])
                    else:
                        heads[idx] = head.slice(keep)
                # `parts` is bounded: at most fan_in heads, each already byte-
                # bounded by _byte_bounded_rows on write, so the gather + sort +
                # combine_chunks here stay inside ~fan_in x run-cap-per-head.
                merged = pa.Table.from_batches(parts)
                indices = pc.sort_indices(merged.column(_ROW_NR))
                ordered = merged.take(indices).combine_chunks()
                step = self._byte_bounded_rows(ordered)
                for start in range(0, ordered.num_rows, step):
                    length = min(step, ordered.num_rows - start)
                    yield ordered.slice(start, length).to_batches()[0]
        finally:
            for reader in readers:
                reader.close()

    def _next_run_path(self) -> Path:
        path = self._temp_dir / f"run_{self._run_seq:06d}.arrow"
        self._run_seq += 1
        return path


def _next_batch(reader: pa.RecordBatchReader) -> pa.RecordBatch | None:
    try:
        return reader.read_next_batch()
    except StopIteration:
        return None


__all__ = ["ExternalRowNrSorter"]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/execution/test_external_sort.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_external_sort.py tests/unit/execution/test_external_sort.py
git commit -m "feat: bounded external row_nr sorter with fail-closed contiguity"
```

---

## Task 3: `StreamFkJoiner` unordered join, pragma set, fail-closed guard

Drop the `ORDER BY`, force the written join order (child LEFT, deduplicated parent RIGHT, parent as build side), make the guard fail closed instead of silently no-op, and move the connection to lazy per-edge open so Task 7 can sequence edges. `iter_join_rows` becomes private and explicitly UNORDERED.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_duckdb.py` (new `threads` / `max_temp_directory_size` params)
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py` (extend), `tests/unit/execution/test_out_of_core_duckdb.py` (extend)

**Interfaces:**
- Consumes: `connect_duckdb(temp_dir=..., memory_limit=..., threads=..., max_temp_directory_size=...)`.
- Produces (for Task 4): `StreamFkJoiner` with the SAME constructor signature plus keyword `max_temp_directory_size: str | None = None`; no connection is opened at construction; `_ensure_conn()` opens/configures on demand; a NEW private `_iter_unordered_join_rows(batch_rows)` (no ORDER BY). The existing public `iter_join_rows` (with its ORDER BY) is KEPT as a transient shim so the driver stays green through Tasks 3-6; Task 7 switches the driver to `run_ordered_join` and deletes `iter_join_rows` + its ORDER BY in the same commit. Do NOT remove it here. `total_orphans()` unchanged in signature; `close()` closes the connection if open. Module constant `_BUILD_SIDE_SWAP_OPTIMIZER = "build_side_probe_side"` and `_MIN_STREAM_JOIN_DUCKDB = (1, 2, 0)` for the structural tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/execution/test_out_of_core_stream_join.py` (reuse that file's existing fixture helpers for building an edge, a parent relation, and child batches; they exist on the restored branch):

```python
def test_build_side_optimizer_absent_fails_closed(monkeypatch, tmp_path, simple_edge_fixture):
    """A missing/renamed build_side_probe_side must raise, never no-op:
    a silent no-op restores an O(child) hash build (the 21 GB regression)."""
    from decoy_engine.execution.out_of_core import _stream_join

    joiner = simple_edge_fixture.make_joiner(tmp_path)
    monkeypatch.setattr(
        _stream_join, "_optimizer_names", lambda conn: {"join_order"}
    )
    joiner.stage_keys(simple_edge_fixture.child_batches)
    with pytest.raises(ExecutionError, match="out_of_core_duckdb_join_unsupported"):
        joiner.total_orphans()


def test_join_plan_builds_on_parent_and_has_no_order_by(tmp_path, simple_edge_fixture):
    """EXPLAIN (FORMAT JSON) must show a HASH_JOIN whose BUILD side is the
    deduplicated parent and NO ORDER_BY operator anywhere; the reorder is the
    sorter's job (Task 2), never DuckDB's. Walk the JSON operator tree, not the
    rendered text: the tree is far more stable across DuckDB point releases."""

    def _walk(node):
        yield node
        for child in node.get("children", []):
            yield from _walk(child)

    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    plan = joiner.explain_join()  # parsed dict (Task 3 step 4d)
    nodes = list(_walk(plan))
    names = {str(n.get("name", "")).upper() for n in nodes}
    assert not any("ORDER_BY" in name or name == "ORDER BY" for name in names)
    hash_joins = [n for n in nodes if "HASH_JOIN" in str(n.get("name", "")).upper()]
    assert hash_joins, "expected a HASH_JOIN operator in the plan"
    # DuckDB's JSON plan renders the build (right) input as the SECOND child of
    # the hash join. The build subtree must reference parent_keys (the
    # deduplicated parent read_parquet view), never the child arrow scan.
    build_subtree = list(_walk(hash_joins[0]["children"][1]))
    build_text = " ".join(str(n.get("extra_info", "")) + str(n.get("name", "")) for n in build_subtree)
    assert "parent_keys" in build_text
    assert "child_keys" not in build_text


def test_connection_is_lazy_and_configured(tmp_path, simple_edge_fixture):
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    assert joiner._conn is None  # nothing opened at construction
    conn = joiner._ensure_conn()
    disabled = conn.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    assert "build_side_probe_side" in disabled
    assert "join_order" in disabled
    assert conn.execute("SELECT current_setting('threads')").fetchone()[0] in (1, "1")
    joiner.close()
    assert joiner._conn is None
```

And to `tests/unit/execution/test_out_of_core_duckdb.py`:

```python
def test_pinned_duckdb_exposes_build_side_probe_side(tmp_path):
    """Structural pin: the locked DuckDB (1.5.4) must expose the optimizer this
    route disables. A rename/removal on an upgrade must fail HERE, loudly,
    before any scale run can silently regress."""
    conn = connect_duckdb(temp_dir=tmp_path)
    names = {row[0] for row in conn.execute("SELECT name FROM duckdb_optimizers()").fetchall()}
    assert "build_side_probe_side" in names
    assert "join_order" in names


def test_connect_duckdb_threads_and_temp_size(tmp_path):
    conn = connect_duckdb(
        temp_dir=tmp_path, memory_limit="128MB", threads=1, max_temp_directory_size="1024MB"
    )
    assert conn.execute("SELECT current_setting('threads')").fetchone()[0] in (1, "1")
    assert "1024" in str(
        conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_duckdb.py -q`
Expected: new tests FAIL (`connect_duckdb` rejects `threads=`, joiner opens eagerly, ORDER BY present, guard no-ops).

- [ ] **Step 3: Implement `_duckdb.py` changes**

Extend `connect_duckdb` (keep the existing docstring reasoning; add the two params):

```python
def connect_duckdb(
    *,
    temp_dir: Path,
    memory_limit: str | None = None,
    threads: int | None = None,
    max_temp_directory_size: str | None = None,
):
    ...
    config["preserve_insertion_order"] = False
    if memory_limit is not None:
        config["memory_limit"] = memory_limit
        if threads is None:
            threads = _threads_for_memory_limit(memory_limit)
    if threads is not None:
        # threads=1 for the stream-join path: one blocking query at a time is
        # part of the tested memory envelope (Codex final mechanism); other
        # callers keep the memory-derived default.
        config["threads"] = threads
    if max_temp_directory_size is not None:
        config["max_temp_directory_size"] = max_temp_directory_size
    return duckdb.connect(database=":memory:", config=config)
```

- [ ] **Step 4: Implement `_stream_join.py` changes**

4a. Replace `_disable_build_side_swap` with a fail-closed guard plus a mockable name fetch:

```python
_BUILD_SIDE_SWAP_OPTIMIZER = "build_side_probe_side"
_JOIN_ORDER_OPTIMIZER = "join_order"
# External (larger-than-memory) hash join is integrated since DuckDB 1.2.0;
# this path is certified on 1.5.4 and must not silently run on the library's
# older >=0.10.0 floor.
_MIN_STREAM_JOIN_DUCKDB = (1, 2, 0)


def _optimizer_names(conn: object) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM duckdb_optimizers()").fetchall()}


def _require_parent_build(conn: object) -> None:
    """Pin the written join order (child probe, parent build), fail closed.

    A registered Arrow RecordBatchReader has no known row count, so DuckDB
    estimates ~1 row and swaps the build side onto the actual multi-million-
    row child: an O(child) resident hash build `memory_limit` does not fully
    cover. Public API only: `duckdb_optimizers()` + `SET disabled_optimizers`
    (https://duckdb.org/docs/current/guides/performance/join_operations).
    Absence of the optimizer name MUST raise: a silent no-op silently
    restores the O(child) build (the 200M@8GB OOM).
    """
    import duckdb

    version = tuple(int(p) for p in duckdb.__version__.split(".")[:3])
    names = _optimizer_names(conn)
    # BOTH optimizers are REQUIRED, not best-effort. Disabling only
    # build_side_probe_side while join_order still runs lets the optimizer
    # reorder the join and re-pick the build side by estimate, silently
    # restoring the O(child) build. If either name is missing (a rename or
    # removal on an upgrade), or the version predates external hash join, fail
    # closed and let the caller route to the resident/O(parent) path.
    required = {_BUILD_SIDE_SWAP_OPTIMIZER, _JOIN_ORDER_OPTIMIZER}
    if version < _MIN_STREAM_JOIN_DUCKDB or not required.issubset(names):
        raise ExecutionError(
            code="out_of_core_duckdb_join_unsupported",
            message=(
                "the out-of-core stream FK join requires DuckDB >= "
                f"{'.'.join(map(str, _MIN_STREAM_JOIN_DUCKDB))} (certified on "
                f"1.5.4; found {duckdb.__version__}) with both "
                f"{sorted(required)} optimizers available to disable. Refusing "
                "to run with an uncontrolled hash-join build side; use the "
                "resident/O(parent) route instead."
            ),
        )
    existing = conn.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    disabled = {name for name in existing.split(",") if name} | required
    conn.execute(f"SET disabled_optimizers = '{','.join(sorted(disabled))}'")
```

4b. Lazy connection. In `__init__`: delete the whole `connect_duckdb ... CREATE TEMP VIEW` block (keep everything before it, including `_resolve_output_types`); set `self._conn = None` and store `self._max_temp_directory_size = max_temp_directory_size` (new keyword param, default None). Add:

```python
    def _ensure_conn(self):
        """Open and configure this edge's connection on first blocking use.

        Lazy so the driver can hold many staged joiners while only ONE edge's
        DuckDB instance (and its blocking operators) exists at a time; the
        connection closes in `close()` before the next edge opens (sequential
        per-edge execution, part of the tested memory envelope).
        """
        if self._conn is not None:
            return self._conn
        conn = connect_duckdb(
            temp_dir=self._temp_dir / "duckdb",
            memory_limit=self._memory_limit,
            threads=1,
            max_temp_directory_size=self._max_temp_directory_size,
        )
        try:
            _require_parent_build(conn)
            conn.execute(
                "CREATE TEMP VIEW parent_keys AS SELECT * FROM "
                f"read_parquet({_sql_string(str(self._relation.path))})"
            )
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return conn
```

Store `self._memory_limit = memory_limit` in `__init__` (it currently passes it straight to `connect_duckdb`). `total_orphans` starts with `conn = self._ensure_conn()` and uses `conn` (its SQL is unchanged: whole-child anti-join count over a fresh `open_reader()`).

4c. ADD a new private `_iter_unordered_join_rows` (copy `iter_join_rows`, drop the `ORDER BY`) and LEAVE the existing `iter_join_rows` untouched (it keeps its `ORDER BY`; the driver still calls it until Task 7). The new method's query:

```python
        query = f"""
            SELECT {", ".join(select_list)}
            FROM child_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(join_key)}
        """
```

Its docstring: batches are UNORDERED (DuckDB gives no order contract; `preserve_insertion_order=false` and spill can reorder freely); the ONLY consumer is `run_ordered_join` (Task 4), which owns restoring order. Body otherwise mirrors `iter_join_rows` (fresh reader, register/unregister, `to_arrow_reader(batch_rows)`). Keeping both methods live means every commit in Tasks 3-6 stays green; Task 7 deletes `iter_join_rows` when the driver stops calling it.

4d. Add `explain_join()` for the plan test: run `EXPLAIN (FORMAT JSON) <the UNORDERED SELECT>` (register a fresh reader like `_iter_unordered_join_rows`), parse the returned string with `json.loads`, and return the parsed plan tree (a dict). Do NOT return raw text: the structural assertions must walk the JSON operator tree (operator names, build-side child), which is far more stable across DuckDB point releases than substring matching on the rendered text. Update `close()` to also set `self._conn = None` after closing (already does) and rewrite the module docstring's join description: replace every "ORDER BY __decoy_row_nr" / "DuckDB owns the sort" claim with the unordered-join + Decoy-owned external reorder story, citing the DuckDB order-preservation page and the join-order guidance page, and stating the honest guarantee sentence from the plan header. (The docstring may describe both methods during the Tasks 3-6 window; Task 7 removes the ordered one.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_duckdb.py -q`
Expected: the three new joiner tests and two duckdb tests pass, AND every pre-existing test stays green. Because `iter_join_rows` (with its ORDER BY) is untouched and the driver still calls it, the existing ordered-output tests, the driver, and the parity suite are unaffected by this commit. The new work is purely additive (the unordered method, the guard, lazy connection, `explain_join`). Run the full OOC suite to confirm nothing regressed: `uv run pytest tests/unit/execution -q -k out_of_core && uv run pytest tests/parity/test_out_of_core_fk_parity.py -q`. All green.

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_duckdb.py src/decoy_engine/execution/out_of_core/_stream_join.py tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_duckdb.py
git commit -m "feat: unordered forced-parent-build stream join with fail-closed guard"
```

---

## Task 4: `run_ordered_join`: unordered join into the sorter, out to `JoinRowCursor`

Wire the pipeline: drain the unordered join into `ExternalRowNrSorter` while the DuckDB connection is live, close the connection BEFORE merge passes (so join state and merge buffers are never co-resident), then stream the contiguity-asserted ordered merge into the existing `JoinRowCursor`. `resolve_batch`, REMAP minting, and payload-boundary alignment are untouched.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py` (extend)

**Interfaces:**
- Consumes: `ExternalRowNrSorter` (Task 2), `_iter_unordered_join_rows` (Task 3).
- Produces (for Task 7): `StreamFkJoiner.run_ordered_join(batch_rows: int, *, run_bytes_cap: int, merge_fan_in: int = 16) -> Iterator[pa.RecordBatch]`. Blocking work (join drain, run sorts, intermediate merges) executes eagerly inside the call; the returned iterator only streams the final merge. The joiner's DuckDB connection is CLOSED by the time the call returns. `JoinRowCursor` consumes the iterator unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_ordered_join_matches_oracle_order_and_values(tmp_path, simple_edge_fixture):
    """End-to-end: unordered join -> sorter -> contiguous 0..N-1 stream whose
    resolved output equals the resident oracle path for the same seed."""
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    batches = list(joiner.run_ordered_join(1_000, run_bytes_cap=64 * 1024))
    assert joiner._conn is None  # connection released before streaming
    row_nrs = [n for b in batches for n in b.column("__decoy_row_nr").to_pylist()]
    assert row_nrs == list(range(simple_edge_fixture.n_child_rows))
    resolved = [joiner.resolve_batch(b) for b in batches]
    assert resolved == simple_edge_fixture.oracle_resolved  # fixture golden


def test_pipeline_does_not_depend_on_duckdb_order(monkeypatch, tmp_path, simple_edge_fixture):
    """Force a worst-case producer: reverse every unordered join batch AND the
    batch sequence. The sorter must still deliver 0..N-1; any hidden reliance
    on DuckDB's incidental probe order fails here."""
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    real = joiner._iter_unordered_join_rows

    def scrambled(batch_rows):
        collected = list(real(batch_rows))
        for batch in reversed(collected):
            table = pa.Table.from_batches([batch])
            yield table.take(list(range(batch.num_rows - 1, -1, -1))).to_batches()[0]

    monkeypatch.setattr(joiner, "_iter_unordered_join_rows", scrambled)
    batches = list(joiner.run_ordered_join(1_000, run_bytes_cap=16 * 1024))
    row_nrs = [n for b in batches for n in b.column("__decoy_row_nr").to_pylist()]
    assert row_nrs == list(range(simple_edge_fixture.n_child_rows))


def test_remap_and_payload_misalignment_survive_reorder(tmp_path, remap_edge_fixture):
    """REMAP minting stays per payload-aligned slice: take() slices the ordered
    stream at payload boundaries that deliberately straddle sorter batch
    boundaries, and the minted values equal the whole-child oracle mint."""
    joiner = remap_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(remap_edge_fixture.child_batches)
    cursor = JoinRowCursor(
        joiner.run_ordered_join(97, run_bytes_cap=8 * 1024),  # prime-sized reader batches
        remap_edge_fixture.edge.child_columns,
    )
    out = []
    row_nr = 0
    for size in remap_edge_fixture.payload_batch_sizes:  # misaligned, e.g. [100, 3, 250, ...]
        out.append(joiner.resolve_batch(cursor.take(size, row_nr)))
        row_nr += size
    cursor.assert_exhausted()
    assert out == remap_edge_fixture.oracle_resolved_per_payload_batch
```

def test_close_releases_connection_and_reorder_runs(tmp_path, simple_edge_fixture):
    """Teardown is tested, not assumed: after draining and close(), the DuckDB
    connection is gone AND no sorter run files survive under the edge's reorder
    dir. This pins the close-ownership contract (close() owns run-file cleanup)."""
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    list(joiner.run_ordered_join(1_000, run_bytes_cap=16 * 1024))  # drain fully
    assert joiner._conn is None
    joiner.close()
    reorder_dir = joiner._temp_dir / "reorder"
    assert not any(reorder_dir.glob("run_*.arrow"))  # close() removed the runs
    joiner.close()  # idempotent


(`remap_edge_fixture`, `simple_edge_fixture`, and `orphan_edge_fixture` were defined in Task 1 Step 4; do not redefine them. `remap_edge_fixture` is a parent/child pair with orphans under `OrphanPolicy.REMAP`, payload sizes chosen to straddle the 97-row reader batches, and oracle values from the resident `_join.py::mask_child_fk` path on the same inputs.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py -q -k "run_ordered or reorder"`
Expected: FAIL with `AttributeError: ... run_ordered_join`.

- [ ] **Step 3: Implement `run_ordered_join`**

```python
    def run_ordered_join(
        self,
        batch_rows: int,
        *,
        run_bytes_cap: int,
        merge_fan_in: int = 16,
    ) -> Iterator[pa.RecordBatch]:
        """One edge's ordered join stream: unordered DuckDB join, Decoy reorder.

        All blocking work runs EAGERLY here, in strict sequence, so at most
        one blocking operator's state is resident at a time: (1) the forced-
        parent-build hash join drains into the sorter's byte-capped runs
        (peak: DuckDB budget + one run buffer); (2) the connection CLOSES,
        releasing every DuckDB allocation; (3) the sorter's intermediate
        merge passes run (peak: fan_in head batches + one merge window). The
        returned iterator only streams the final merge, whose contiguity
        assert guarantees exactly 0..N-1 (N = staged child rows; the parent
        side is deduplicated, so the LEFT JOIN is row-preserving).
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before run_ordered_join")
        sorter = ExternalRowNrSorter(
            temp_dir=self._temp_dir / "reorder",
            run_bytes_cap=run_bytes_cap,
            merge_fan_in=merge_fan_in,
            batch_rows=batch_rows,
        )
        # Own the sorter so close() can delete its run files deterministically
        # (the driver calls joiner.close() in _release_joiners). Closing it here
        # via self.close() would be wrong: it must survive until the caller has
        # drained iter_ordered().
        self._sorter = sorter
        try:
            for batch in self._iter_unordered_join_rows(batch_rows):
                sorter.write(batch)
        finally:
            # Free ONLY the DuckDB buffer manager before the merge passes run, so
            # join state and merge buffers are never co-resident. This must NOT
            # touch the sorter (its runs are still needed): connection teardown
            # and sorter cleanup are separate lifecycles.
            self._close_conn()
        sorter.finish()
        return sorter.iter_ordered(expected_rows=self._staged_rows)
```

Import `ExternalRowNrSorter` at module top. Initialize `self._sorter = None` in `__init__`. Split the two lifecycles explicitly:

```python
    def _close_conn(self) -> None:
        """Close only the DuckDB connection (used mid-run before merge passes)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def close(self) -> None:
        """Release the connection AND the sorter's run files.

        The child-key spill file stays on disk (the driver's temp_dir rmtree
        owns that), so resolve_batch (pure Python/Arrow) still works after
        close(); only DuckDB state and the reorder run files are freed here.
        Idempotent and safe after a fully drained iter_ordered().
        """
        self._close_conn()
        if self._sorter is not None:
            self._sorter.close()
            self._sorter = None
```

This gives the sorter's run files a single deterministic owner (`close()`, called by the driver's `_release_joiners`), closing HIGH-severity ambiguity about who deletes them; the driver's `shutil.rmtree` in `finally` remains the backstop for a crash between `finish()` and drain.

- [ ] **Step 4: Run the joiner suite and parity**

Run: `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py -q`
Expected: new tests pass. Do NOT rewrite the older `iter_join_rows` tests here: that method is still live (the shim), so its tests stay green as-is; they are removed alongside the method in Task 7. Then:
Run: `uv run pytest tests/parity/test_out_of_core_fk_parity.py -q`
Expected: green. The driver still calls the ordered `iter_join_rows` shim, so parity is unchanged by this additive commit; `run_ordered_join` is exercised only by the new unit tests until Task 7 rewires the driver.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_stream_join.py tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "feat: run_ordered_join wires unordered join through external reorder"
```

---

## Task 5: Spillable parent-relation construction (stage, winners, join-back as separate plans)

The restored `_relation.py::_build_relation` already stages the registered stream to Parquet, but runs the `max(row_nr) GROUP BY` and the join-back inside ONE query: two blocking operators (hash aggregate + hash join) co-resident in one plan, which DuckDB documents can still OOM even though each operator individually supports larger-than-memory execution (https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads). Split them: compute winners to an on-disk file in one query, join back in a second.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_relation.py` (`_build_relation` only)
- Test: `tests/unit/execution/test_out_of_core_relation.py` (extend)

**Interfaces:**
- Consumes/produces: `_build_relation`'s signature and the returned `ParentKeyRelation` are unchanged; only the internal query plan shape changes. Output Parquet must be value-identical to the single-query form.

- [ ] **Step 1: Write the failing tests**

```python
def test_dedup_runs_as_two_single_blocking_operator_queries(tmp_path, monkeypatch):
    """Each dedup query must contain at most one blocking operator: the
    aggregate lands winners to disk, the join-back reads them from disk."""
    from decoy_engine.execution.out_of_core import _relation

    executed: list[str] = []
    real_connect = _relation.connect_duckdb

    class _SpyConn:
        # DuckDBPyConnection.execute is a read-only builtin method and cannot be
        # reassigned, so wrap the connection in a delegating proxy: intercept
        # execute(), forward everything else via __getattr__.
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kw):
            executed.append(sql if isinstance(sql, str) else str(sql))
            return self._inner.execute(sql, *args, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def spying_connect(**kwargs):
        return _SpyConn(real_connect(**kwargs))

    monkeypatch.setattr(_relation, "connect_duckdb", spying_connect)
    build_small_relation(tmp_path)  # existing helper in this test module
    dedup_queries = [q for q in executed if "GROUP BY" in q or "__decoy_win_row_nr" in q]
    assert len(dedup_queries) == 2
    grouped, joined = dedup_queries
    assert "GROUP BY" in grouped and "JOIN" not in grouped
    assert "JOIN" in joined and "GROUP BY" not in joined
    assert "read_parquet" in joined  # winners come from disk, not a subquery


def test_split_dedup_is_value_identical_and_cleans_scratch(tmp_path):
    relation = build_small_relation(tmp_path)  # duplicate keys, last-write-wins fixture
    table = pq.read_table(relation.path)
    assert table.to_pydict() == EXPECTED_LAST_WRITE_WINS  # existing golden in this module
    leftovers = [p for p in tmp_path.rglob("*_key_staged.parquet")] + [
        p for p in tmp_path.rglob("*_key_winners.parquet")
    ]
    assert leftovers == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/execution/test_out_of_core_relation.py -q -k "dedup"`
Expected: first test FAILS (one combined query today).

- [ ] **Step 3: Implement the split**

In `_build_relation`, replace the single `COPY (SELECT ... JOIN (SELECT max ...) ...)` statement with:

```python
    winners_path = duckdb_dir / (
        f"{edge.parent_table}_{_column_tuple_slug(edge.parent_columns)}_key_winners.parquet"
    )
    conn = connect_duckdb(
        temp_dir=duckdb_dir, memory_limit=memory_limit, threads=1
    )
    try:
        conn.register("parent_keys", reader)
        # Plan 1 of 3: land the single-pass stream (no blocking operator).
        conn.execute(f"COPY parent_keys TO {_sql_string(str(staged_path))} (FORMAT PARQUET)")
        staged_sql = f"read_parquet({_sql_string(str(staged_path))})"
        # Plan 2 of 3: ONE blocking operator (external hash aggregate).
        # max-over-int per group is fixed-size spillable state; the winners
        # land on disk so the join below never co-resides with this aggregate
        # (DuckDB documents that stacking blocking operators in one plan can
        # OOM even when each spills individually).
        conn.execute(
            f"""
            COPY (
                SELECT __decoy_fk_join_key,
                       max(__decoy_row_nr) AS __decoy_win_row_nr
                FROM {staged_sql}
                GROUP BY __decoy_fk_join_key
            ) TO {_sql_string(str(winners_path))} (FORMAT PARQUET)
            """
        )
        winners_sql = f"read_parquet({_sql_string(str(winners_path))})"
        masked_select = ", ".join(f"s.{col} AS {col}" for col in masked_columns)
        # Plan 3 of 3: ONE blocking operator (external hash join). row_nr is
        # globally unique, so exactly one staged row survives per key, all
        # masked columns from the SAME winning row.
        conn.execute(
            f"""
            COPY (
                SELECT s.__decoy_fk_join_key, {masked_select}
                FROM {staged_sql} s
                JOIN {winners_sql} w
                  ON s.__decoy_fk_join_key = w.__decoy_fk_join_key
                 AND s.__decoy_row_nr = w.__decoy_win_row_nr
            ) TO ? (FORMAT PARQUET)
            """,
            [str(out_path)],
        )
    finally:
        try:
            conn.close()
        finally:
            staged_path.unlink(missing_ok=True)
            winners_path.unlink(missing_ok=True)
```

Preserve the existing long comment block explaining WHY stage/winners/join-back beats `arg_max`/window forms (it is measured and load-bearing); extend it with one sentence on the split ("winners land on disk so the aggregate and the join are never co-resident blocking operators", with the DuckDB workload-tuning citation).

- [ ] **Step 4: Run relation + parity tests**

Run: `uv run pytest tests/unit/execution/test_out_of_core_relation.py tests/unit/execution/test_out_of_core_relation_streamed.py tests/unit/execution/test_out_of_core_relation_chunked.py -q`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_relation.py tests/unit/execution/test_out_of_core_relation.py
git commit -m "feat: split parent dedup into single-blocking-operator plans"
```

---

## Task 6: FAIL anti-join precount pinned under the new lifecycle

`total_orphans()` (whole-child anti-join count, FRESH IPC reader over the child-key spill, never a source reread) already exists; this task pins it against the new lazy-connection, unordered-join lifecycle so a later refactor cannot silently degrade it to a source reread or a stale shared reader.

**Files:**
- Test: `tests/unit/execution/test_out_of_core_stream_join.py` (extend). Implementation changes only if a test exposes a lifecycle break.

**Interfaces:**
- Consumes: `total_orphans()` (Task 3), `run_ordered_join` (Task 4).
- Produces: guarantees for Task 7: precount-then-join on one edge's own connection works back-to-back; FAIL raises before any ordered output exists.

- [ ] **Step 1: Write the (possibly already passing; keep them regardless) tests**

```python
def test_fail_precount_then_join_share_one_edge_connection(tmp_path, orphan_edge_fixture):
    """Scan 1 (anti-join precount) and scan 2 (the join) each open a FRESH
    single-pass reader over the SAME spill; a shared reader would return zero
    rows on scan 2."""
    joiner = orphan_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(orphan_edge_fixture.child_batches)
    assert joiner.total_orphans() == orphan_edge_fixture.n_orphans
    batches = list(joiner.run_ordered_join(500, run_bytes_cap=32 * 1024))
    assert sum(b.num_rows for b in batches) == orphan_edge_fixture.n_child_rows


def test_precount_reads_the_spill_not_the_source(tmp_path, orphan_edge_fixture, monkeypatch):
    """Single-source-read invariant: after staging, corrupting the SOURCE must
    not change the precount (it reads the Decoy spill, an artifact of the one
    phase-1 read)."""
    joiner = orphan_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(orphan_edge_fixture.child_batches)
    orphan_edge_fixture.corrupt_source()  # helper: mutate the fixture's source in place
    assert joiner.total_orphans() == orphan_edge_fixture.n_orphans
```

The FULL-route FAIL assertion (raise before any output, `sink_batches_written == 0`) belongs to Task 7, where the driver's per-edge FAIL sequencing actually exists; committing it here would mean committing a red or `xfail` test, which this plan does not do (every commit is green). It is written in Task 7 Step 1.

- [ ] **Step 2: Run**

Run: `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py -q -k "precount"`
Expected: both pass off Tasks 3-4 (the joiner-level precount is fully functional once `total_orphans` and `run_ordered_join` exist; no driver change is needed for these two).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "test: pin FAIL anti-join precount under lazy per-edge lifecycle"
```

---

## Task 7: Sequential per-edge execution in the driver + reorder budgets

Rewire `stream_table`: phase 1 (stage + mask + payload store) is unchanged; phase 2 becomes a strict per-edge sequence (FAIL precount, then `run_ordered_join`, one edge at a time, each edge's DuckDB connection closed before the next opens); phase 3 (payload-aligned resolution) is unchanged. Add the budget resolver that turns the run budget into the DuckDB share, sort-run cap, and merge fan-in.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_memory_estimate.py` (add `ReorderBudgets` + `resolve_reorder_budgets`)
- Modify: `src/decoy_engine/execution/out_of_core/_stream_driver.py`
- Test: `tests/unit/execution/test_out_of_core_memory_estimate.py` (extend), `tests/unit/execution/test_out_of_core_runner_streaming.py` (extend)

**Interfaces:**
- Consumes: `run_ordered_join` (Task 4), `total_orphans` (Tasks 3/6).
- Produces: `resolve_reorder_budgets(budget_bytes: int | None, memory_limit: str | None) -> ReorderBudgets` where `ReorderBudgets(duckdb_memory_limit: str | None, run_bytes_cap: int, merge_fan_in: int)`. `stream_table`'s signature is unchanged.

- [ ] **Step 1: Write the failing tests**

In `test_out_of_core_memory_estimate.py`:

```python
def test_reorder_budgets_from_run_budget():
    b = resolve_reorder_budgets(8 * 1024**3, None)
    # DuckDB gets 50% (Codex: 50-60% of the ceiling; headroom for Arrow,
    # sorter, Python, allocators, which memory_limit does not cover).
    assert b.duckdb_memory_limit == f"{4 * 1024}MB"
    assert b.run_bytes_cap == int(8 * 1024**3 * 0.15)
    assert b.merge_fan_in == 16


def test_reorder_budgets_from_flat_memory_limit():
    b = resolve_reorder_budgets(None, "1024MB")
    assert b.duckdb_memory_limit == "1024MB"
    assert b.run_bytes_cap == max(int(1024 * 1024 * 1024 * 0.25), 64 * 1024 * 1024)


def test_reorder_budgets_unbudgeted_fails_closed():
    # No budget of either kind must NOT silently run uncapped: it raises so the
    # route seam sends the job to the resident/O(parent) path instead.
    with pytest.raises(ExecutionError, match="out_of_core_reorder_unbudgeted"):
        resolve_reorder_budgets(None, None)
```

In `test_out_of_core_runner_streaming.py` (uses that module's existing two-edge route fixture):

```python
def test_edges_execute_sequentially_never_two_connections(monkeypatch, two_edge_route):
    """No two joiner DuckDB connections may be live at once: blocking
    operators from different edges must never be co-resident."""
    from decoy_engine.execution.out_of_core import _stream_join

    live = {"count": 0, "max": 0}
    real_ensure = _stream_join.StreamFkJoiner._ensure_conn
    real_close = _stream_join.StreamFkJoiner.close

    def counting_ensure(self):
        opened = self._conn is None
        conn = real_ensure(self)
        if opened:
            live["count"] += 1
            live["max"] = max(live["max"], live["count"])
        return conn

    def counting_close(self):
        if self._conn is not None:
            live["count"] -= 1
        real_close(self)

    monkeypatch.setattr(_stream_join.StreamFkJoiner, "_ensure_conn", counting_ensure)
    monkeypatch.setattr(_stream_join.StreamFkJoiner, "close", counting_close)
    two_edge_route.run()
    assert live["max"] == 1


def test_fail_policy_raises_before_any_output(tmp_path, fail_policy_route_fixture):
    """Full route: FAIL with orphans raises orphan_fk and the sink stages ZERO
    output batches. Per-edge precount (each edge before its own join) still
    guarantees output-before-error semantics: phase 3 has not started when any
    edge's precount raises, so no batch can have reached the sink."""
    with pytest.raises(ExecutionError, match="orphan_fk"):
        fail_policy_route_fixture.run()
    assert fail_policy_route_fixture.sink_batches_written == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/execution/test_out_of_core_memory_estimate.py tests/unit/execution/test_out_of_core_runner_streaming.py -q -k "reorder_budgets or sequentially"`
Expected: FAIL (`resolve_reorder_budgets` undefined; driver still opens per-edge connections at construction / streams via the removed public `iter_join_rows`).

- [ ] **Step 3: Implement `resolve_reorder_budgets`**

In `_memory_estimate.py`:

```python
_DUCKDB_BUDGET_FRACTION = 0.5      # of the undivided run budget
_SORT_RUN_FRACTION = 0.15          # of the undivided run budget
_SORT_RUN_FRACTION_OF_LIMIT = 0.25 # when only a flat DuckDB limit is given
_RUN_BYTES_FLOOR = 64 * 1024 * 1024
_RUN_BYTES_DEFAULT = 256 * 1024 * 1024
_MERGE_FAN_IN = 16


@dataclass(frozen=True)
class ReorderBudgets:
    """Per-edge budgets for the unordered-join + external-reorder pipeline.

    The split is byte-accounted, not tuned by trial: the two non-overlapping
    phases (see `_external_sort` module docstring) have resident peaks
      drain+sort = duckdb_fraction + 2 x run_fraction   (DuckDB live)
      merge      = 2 x run_fraction                      (DuckDB closed)
    of the process budget. With duckdb_fraction=0.50 and run_fraction=0.15 the
    binding peak is the drain+sort phase at ~0.80, leaving ~0.20 for Arrow,
    Python, and allocator slack. The invariant the fractions must satisfy is
    `duckdb_fraction + 2 x run_fraction + headroom <= 1.0`; the merge phase is
    strictly smaller because DuckDB's share is freed before it runs, and the
    merge's fan_in heads are individually capped at run_bytes_cap/merge_fan_in
    so they sum to <= run_bytes_cap regardless of row width. DuckDB's
    memory_limit covers only its buffer manager, not all allocations
    (https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads),
    which is why its fraction is well under 1.0. Edges run sequentially, so
    these are NOT divided by edge count.
    """

    duckdb_memory_limit: str | None
    run_bytes_cap: int
    merge_fan_in: int


def resolve_reorder_budgets(
    budget_bytes: int | None, memory_limit: str | None
) -> ReorderBudgets:
    """Fail-closed budget resolver for the bounded stream-join path.

    The arbitrary-size guarantee holds ONLY when DuckDB is capped well below
    the process RAM ceiling; an uncapped DuckDB defaults to ~80% of physical
    RAM (https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads)
    and reintroduces the OOM this architecture exists to prevent. So a request
    to run this path with NEITHER a run budget NOR a flat memory_limit is a
    configuration error, not a "use defaults" case: the route seam
    (_pipeline_route_exec) must send such jobs to the resident/O(parent) path
    instead of ever calling this with (None, None). This raise is the
    defense-in-depth backstop if that seam is bypassed.
    """
    if budget_bytes is not None:
        duckdb_mib = int(budget_bytes * _DUCKDB_BUDGET_FRACTION) // (1024 * 1024)
        run_cap = max(int(budget_bytes * _SORT_RUN_FRACTION), _RUN_BYTES_FLOOR)
        return ReorderBudgets(f"{duckdb_mib}MB", run_cap, _MERGE_FAN_IN)
    if memory_limit is not None:
        parsed = _parse_memory_limit_bytes(memory_limit)  # reuse/extract the "<n>MB" parser
        run_cap = (
            max(int(parsed * _SORT_RUN_FRACTION_OF_LIMIT), _RUN_BYTES_FLOOR)
            if parsed is not None
            else _RUN_BYTES_DEFAULT
        )
        return ReorderBudgets(memory_limit, run_cap, _MERGE_FAN_IN)
    raise ExecutionError(
        code="out_of_core_reorder_unbudgeted",
        message=(
            "the bounded out-of-core stream-join path requires an explicit "
            "process memory budget (budget_bytes) or a DuckDB memory_limit; "
            "neither was supplied. Running it uncapped would let DuckDB default "
            "to ~80% of physical RAM and defeat the arbitrary-size memory "
            "guarantee. Route to the resident/O(parent) path when no budget "
            "is configured."
        ),
    )
```

Extract the existing `"<n>MB"` regex from `_duckdb.py` into a shared `_parse_memory_limit_bytes` (import it from `_duckdb.py` or move the regex to `_memory_estimate.py`; keep one definition). Import `ExecutionError` into `_memory_estimate.py` if it is not already there. `_RUN_BYTES_DEFAULT` remains (used by the flat-limit-unparsed arm); the `(None, None)` default return is gone. Keep `resolve_phase_memory_limits` for the relation-build caps, but change its SINK joiner arm to stop dividing by `incoming_edges` (edges are sequential now; each joiner gets the full share). Update its docstring accordingly.

The driver must ALSO require a disk budget for this path: in Task 8's route-seam work, admission to the stream path is gated on BOTH a memory budget (so `resolve_reorder_budgets` never hits the raise) AND a concrete `max_temp_directory_size` (so DuckDB temp and the sorter runs cannot grow the disk without bound). Absent either, the route falls back to the resident/O(parent) path.

- [ ] **Step 4: Rewire `stream_table`**

Replace the FAIL loop + phase-2 cursor block (the code between the `finalize_staging` loop and the `rewritten()` definition) with:

```python
        fk_components = _fk_component_map(incoming_edges, joiners)
        fixed_schema = _fixed_output_schema(plan, table_name, source_schema, fk_components)
        warnings.extend(
            enforce_output_projection(
                table_name,
                fixed_schema.names,
                plan,
                unconfigured_column_policy,
                extra_known=frozenset(fk_components),
            )
        )

        # --- Phase 2: SEQUENTIAL per-edge join + external reorder. One edge's
        # blocking operators (forced-parent-build hash join, run sorts, merge
        # passes) run to completion, and its DuckDB connection closes, before
        # the next edge opens: blocking state from two edges is never
        # co-resident (part of the tested memory envelope). FAIL's whole-child
        # precount runs on the same short-lived connection, before that edge's
        # join and before ANY output exists (phase 3 has not started). ---
        reorder = resolve_reorder_budgets(budget_bytes, memory_limit)
        cursors: list[JoinRowCursor] = []
        for edge, joiner in zip(incoming_edges, joiners, strict=True):
            if edge.orphan_policy is OrphanPolicy.FAIL:
                total = joiner.total_orphans()
                if total:
                    raise orphan_fk_error(edge, total)
            cursors.append(
                JoinRowCursor(
                    joiner.run_ordered_join(
                        batch_rows,
                        run_bytes_cap=reorder.run_bytes_cap,
                        merge_fan_in=reorder.merge_fan_in,
                    ),
                    edge.child_columns,
                )
            )
```

Then:
- `_open_joiner` passes `memory_limit=reorder.duckdb_memory_limit` and `max_temp_directory_size` derived from the threaded disk budget: `f"{temp_disk_budget_bytes // (1024 * 1024)}MB" if temp_disk_budget_bytes is not None else None`. Compute `reorder = resolve_reorder_budgets(budget_bytes, memory_limit)` BEFORE the joiner-open loop and delete the old `resolve_phase_memory_limits` joiner-cap plumbing (`sink_joiner`/`resident_joiner`); keep the build caps (`sink_build_memory_limit`, `resident_build_memory_limit`) exactly as they are.
- `_release_joiners` stays (it now also frees the sorter run files: extend `StreamFkJoiner.close()` in Task 4's module to call `sorter.close()` if you kept a reference; if not, the driver's `shutil.rmtree(temp_dir, ...)` in the `finally` already removes `edge_*/reorder/`; prefer the rmtree and note it).
- Phase 3 (`rewritten()`), `emit_to_sink`, the resident branch, WARN aggregation, and the `finally` guard are UNCHANGED.
- In THIS commit (not before), delete the now-dead `iter_join_rows` (the ordered shim) and its `ORDER BY` from `_stream_join.py`, plus the older unit tests that exercised it directly; the driver no longer calls it. `grep -rn "iter_join_rows" src/ tests/` must return nothing after this step. This is the single commit where the ordered path disappears, so the tree goes straight from "driver on ordered shim (green)" to "driver on `run_ordered_join` (green)" with no red in between.
- Update the module docstring's phase description: phase 2 is now "sequential per-edge join + bounded external reorder (Decoy-owned; DuckDB output order is never relied upon)".

- [ ] **Step 5: Run the full local gate**

```bash
uv run pytest tests/unit/execution/test_out_of_core_memory_estimate.py \
  tests/unit/execution/test_out_of_core_runner_streaming.py \
  tests/unit/execution/test_out_of_core_stream_join.py \
  tests/unit/execution/test_external_sort.py -q
uv run pytest tests/parity/test_out_of_core_fk_parity.py -q
uv run pytest tests/unit/execution -q -k "out_of_core"
```

Expected: ALL green, including the byte-parity suite (payload-boundary misalignment, REMAP, overlapping edges, duplicate-parent last-write-wins, and the accepted nested/struct/map + zero-row divergences all run inside it).

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_memory_estimate.py src/decoy_engine/execution/out_of_core/_stream_driver.py tests/unit/execution/test_out_of_core_memory_estimate.py tests/unit/execution/test_out_of_core_runner_streaming.py tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "feat: sequential per-edge execution with explicit reorder budgets"
```

---

## Task 8: Retire the O(parent) hard preflight so the arbitrary-parent path is admitted

The preserved scaffolding's route admission still calls `enforce_ooc_memory_preflight()` (`_memory_estimate.py`, exported), whose model rejects a job when the predicted O(parent) hash-build floor (`predict_ooc_build_floor_bytes`, using `_BUILD_FLOOR_BYTES_PER_ROW = 190.0`) exceeds the budget. That floor described the OLD non-spillable parent build. Under this architecture the parent build is spillable (Task 5) and its resident cost is bounded by `duckdb_memory_limit`, not by parent row count, so the floor is obsolete for the stream path: left in place, it refuses exactly the large-parent jobs this program exists to serve, BEFORE Task 5's spillable dedup ever runs. The route seam also divides the resident cap by `(incoming_edges + 1)` (`_pipeline_route_exec.py`), which made sense when edges were concurrent; edges are sequential now, so that division wrongly shrinks the budget.

Split the preflight by route: keep the O(parent) floor ONLY for the resident/O(parent) path (where the parent genuinely must fit in memory and the floor is the correct guard), and give the stream path a bounded-envelope preflight that checks the preconditions the guarantee actually needs (a memory budget and a disk budget are configured), with NO row-count floor.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_memory_estimate.py` (split `enforce_ooc_memory_preflight`; keep `predict_ooc_build_floor_bytes` / `_BUILD_FLOOR_BYTES_PER_ROW` for the resident arm only)
- Modify: `src/decoy_engine/execution/_pipeline_route_exec.py` (call the route-matched preflight; stop dividing the resident cap by `incoming_edges + 1` for the stream path)
- Test: `tests/unit/execution/test_out_of_core_memory_estimate.py` (extend), `tests/unit/execution/test_pipeline_route_exec.py` (extend, or the module's existing route-selection test file)

**Interfaces:**
- Produces: `enforce_stream_reorder_preflight(*, memory_limit: str | None, budget_bytes: int | None, temp_disk_budget_bytes: int | None) -> None` (raises `out_of_core_reorder_unbudgeted` if neither memory budget is set, `out_of_core_reorder_undisked` if no disk budget). `enforce_ooc_memory_preflight` is retained UNCHANGED for the resident path (still uses the 190 B/row floor). The stream path never calls the floor-based preflight.

- [ ] **Step 1: Write the failing tests**

In `test_out_of_core_memory_estimate.py`:

```python
def test_stream_preflight_admits_large_parent_when_budgeted(tmp_path):
    """The stream path must NOT reject on parent row count: a huge parent with a
    real memory + disk budget is admitted (the build spills; it does not need to
    fit). The old O(parent) 190 B/row floor must not gate this path."""
    # No raise == admitted.
    enforce_stream_reorder_preflight(
        memory_limit="4096MB",
        budget_bytes=8 * 1024**3,
        temp_disk_budget_bytes=500 * 1024**3,
    )


def test_stream_preflight_fails_closed_without_disk_budget():
    with pytest.raises(ExecutionError, match="out_of_core_reorder_undisked"):
        enforce_stream_reorder_preflight(
            memory_limit="4096MB", budget_bytes=8 * 1024**3, temp_disk_budget_bytes=None
        )


def test_stream_preflight_fails_closed_without_memory_budget():
    with pytest.raises(ExecutionError, match="out_of_core_reorder_unbudgeted"):
        enforce_stream_reorder_preflight(
            memory_limit=None, budget_bytes=None, temp_disk_budget_bytes=500 * 1024**3
        )


def test_resident_preflight_still_enforces_parent_floor(tmp_path):
    """The resident/O(parent) path KEEPS the floor: a parent too large for the
    budget is still rejected there (that path really does hold the parent)."""
    with pytest.raises(ExecutionError):
        enforce_ooc_memory_preflight(
            parent_rows=10**12, budget_bytes=64 * 1024 * 1024  # match the real signature
        )
```

Add a route-selection test asserting that when a memory budget and disk budget are present the stream path is chosen and its preflight (not the floor one) runs, and that the resident cap is NOT divided by `incoming_edges + 1` on the stream path. Model it on the route module's existing selection tests (reuse their fixtures; do not invent a new route harness). If the route decision is not currently unit-observable, assert on the effective `memory_limit` passed into the joiner (full budget share, not `/(edges + 1)`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/execution/test_out_of_core_memory_estimate.py -q -k "preflight"`
Expected: FAIL (`enforce_stream_reorder_preflight` undefined).

- [ ] **Step 3: Implement the split**

In `_memory_estimate.py`, add the stream-path preflight (leave `enforce_ooc_memory_preflight`, `predict_ooc_build_floor_bytes`, and `_BUILD_FLOOR_BYTES_PER_ROW` exactly as-is for the resident arm):

```python
def enforce_stream_reorder_preflight(
    *,
    memory_limit: str | None,
    budget_bytes: int | None,
    temp_disk_budget_bytes: int | None,
) -> None:
    """Admission check for the bounded stream-join + external-reorder path.

    Deliberately NO O(parent) or O(child) row-count floor: this path's parent
    build and row reorder both spill, so resident cost is bounded by the
    configured memory budget, not by row count. What it DOES require is that
    those bounds are actually set. A missing memory budget would let DuckDB
    default to ~80% of RAM; a missing disk budget would let temp files grow
    without limit. Either makes the arbitrary-size guarantee false, so both are
    fail-closed here (the caller routes such jobs to the resident path).
    """
    if budget_bytes is None and memory_limit is None:
        raise ExecutionError(
            code="out_of_core_reorder_unbudgeted",
            message=(
                "the bounded out-of-core stream-join path requires a memory "
                "budget; none configured. Route to the resident path instead."
            ),
        )
    if temp_disk_budget_bytes is None:
        raise ExecutionError(
            code="out_of_core_reorder_undisked",
            message=(
                "the bounded out-of-core stream-join path requires a temp disk "
                "budget (max_temp_directory_size) so DuckDB spill and sorter "
                "runs stay bounded; none configured. Route to the resident path."
            ),
        )
```

- [ ] **Step 4: Rewire the route seam in `_pipeline_route_exec.py`**

- At the stream-route branch, call `enforce_stream_reorder_preflight(memory_limit=..., budget_bytes=..., temp_disk_budget_bytes=...)` INSTEAD of `enforce_ooc_memory_preflight(...)`. Keep `enforce_ooc_memory_preflight` on the resident/O(parent) branch.
- Route selection: choose the stream path when a memory budget AND a disk budget are configured; otherwise fall through to the resident/O(parent) path (whose floor preflight then legitimately decides whether the parent fits). This is the single admission point that guarantees `resolve_reorder_budgets` (Task 7) is never called with `(None, None)` and the stream path always has a disk cap.
- Remove the `/(incoming_edges + 1)` division of the resident memory cap on the stream path: edges run sequentially (Task 7), so each edge's joiner gets the full configured share, not a per-edge fraction. Update the comment that justified the division.
- Do NOT change the resident path's arithmetic; this task only stops the obsolete floor and the obsolete division from gating the NEW path.

- [ ] **Step 5: Run**

```bash
uv run pytest tests/unit/execution/test_out_of_core_memory_estimate.py tests/unit/execution/test_pipeline_route_exec.py -q
uv run pytest tests/unit/execution -q -k "out_of_core or route" && uv run pytest tests/parity/test_out_of_core_fk_parity.py -q
```

Expected: green, including a large-parent route case that previously would have been rejected at admission.

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_memory_estimate.py src/decoy_engine/execution/_pipeline_route_exec.py tests/unit/execution/test_out_of_core_memory_estimate.py tests/unit/execution/test_pipeline_route_exec.py
git commit -m "feat: admit arbitrary-parent stream path; retire obsolete O(parent) preflight floor"
```

---

## Task 9: Verification harness: dual-dimension plateaus, order probe, docs

Wire the release-gate measurements: plateau tests in BOTH dimensions with spill evidence, the no-order diagnostic probe, and the honest-guarantee documentation. The 200M@8GB GCP run is the final gate (commands below), not a pytest.

**Files:**
- Modify: `scripts/ooc_child_key_plateau_probe.py` (add `--grow child|parent`)
- Create: `scripts/ooc_join_order_probe.py`
- Modify: `tests/perf/test_out_of_core_memory_sentinel.py`
- Modify: module docstrings (`_stream_join.py`, `_external_sort.py` already carry theirs); `docs/relationships-memory-scaling.md` gains the honest-guarantee paragraph. Delete `_batch_join.py` if Task 1's merge left it dead (grep for imports first).

- [ ] **Step 1: Extend the plateau probe**

`scripts/ooc_child_key_plateau_probe.py` currently sweeps child sizes against a fixed parent. Add `--grow {child,parent}` (default `child`): `--grow parent` fixes the child row count and sweeps parent cardinality. Extend its JSON output contract per size to account for the TWO spill sinks SEPARATELY: `{"rows": n, "peak_rss_mb": ..., "duckdb_temp_disk_mb": ..., "reorder_temp_disk_mb": ..., "parity_ok": true}`. Measure each by walking its subtree high-water before cleanup: `duckdb_temp_disk_mb` over the DuckDB temp dir (`.../duckdb`), `reorder_temp_disk_mb` over the sorter run dirs (`.../edge_*/reorder`). Separate accounting matters because the dimensions exercise different sinks: growing the CHILD drives the external SORTER to spill (more rows to reorder), while growing the PARENT drives the DuckDB hash-join BUILD to spill. A single merged number can stay nonzero from one sink while the other silently never spills, hiding a false plateau in exactly the dimension meant to stress it. (This program already fell into a zero-spill false plateau once.)

- [ ] **Step 2: Write the sentinel gates**

Append to `tests/perf/test_out_of_core_memory_sentinel.py` (both `perf`-marked; subprocess with `VmHWM`; env pinned):

```python
_PLATEAU_ENV = {**os.environ, "ARROW_DEFAULT_MEMORY_POOL": "system", "MALLOC_ARENA_MAX": "2"}
# Bounded-slope bound, MB per +1M rows on the LAST growth segment. The
# unordered-join probe measured 187/190/196 MB at 1M/3M/5M child rows
# (slope ~2-5 MB/1M); 20 leaves allocator-jitter headroom while still
# failing the pre-fix O(child) slopes (41-126 MB/1M) by an order of
# magnitude.
_MAX_SLOPE_MB_PER_M = 20.0


def _plateau(grow: str, sizes: list[int]) -> list[dict]:
    out = subprocess.run(
        [sys.executable, _PLATEAU_PROBE, "--grow", grow, "--sizes",
         ",".join(map(str, sizes)), "--json"],
        env=_PLATEAU_ENV, capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def _assert_bounded(records: list[dict], *, must_spill: str) -> None:
    assert all(r["parity_ok"] for r in records)
    # Nonzero spill evidence in the sink THIS dimension is meant to stress: a
    # plateau without spill in that sink means the data fit in memory and the
    # test proves nothing (the earlier false plateau). `must_spill` is the JSON
    # key for the dimension-relevant sink: growing child -> reorder sorter;
    # growing parent -> DuckDB build.
    assert all(r[must_spill] > 0 for r in records), f"no spill in {must_spill}"
    a, b = records[-2], records[-1]
    slope = (b["peak_rss_mb"] - a["peak_rss_mb"]) / ((b["rows"] - a["rows"]) / 1e6)
    assert slope <= _MAX_SLOPE_MB_PER_M, f"RSS slope {slope:.1f} MB/1M exceeds bound"


@pytest.mark.perf
def test_plateau_fixed_parent_growing_child():
    # Growing child stresses the external sorter: reorder spill must be nonzero.
    _assert_bounded(
        _plateau("child", [1_000_000, 3_000_000, 5_000_000]),
        must_spill="reorder_temp_disk_mb",
    )


@pytest.mark.perf
def test_plateau_fixed_child_growing_parent():
    # Growing parent stresses the DuckDB hash-join build: DuckDB spill nonzero.
    _assert_bounded(
        _plateau("parent", [1_000_000, 3_000_000, 5_000_000]),
        must_spill="duckdb_temp_disk_mb",
    )
```

Also add a high fan-in case if the probe supports multiple incoming edges (`--edges 4`); if it does not, extend it (same JSON contract) and add `test_plateau_high_fan_in` calling `_plateau("child", ..., edges=4)` and asserting via `_assert_bounded(..., must_spill="reorder_temp_disk_mb")` (many edges each drive the sorter). Thread `--edges` through `_plateau` as an extra arg.

- [ ] **Step 3: Write the order probe (diagnostic, never an assert on order)**

`scripts/ooc_join_order_probe.py`: run the UNORDERED join across a config matrix: `threads` 1 and N, `memory_limit` large (no spill) and tiny (forced spill), skewed keys, null keys, orphans, composite keys, `batch_rows` in {97, 1024, 65_536}; for each config, record (to stdout JSON) whether the observed output happened to be row_nr-ordered and the first divergence offset.

Threads seam: `StreamFkJoiner._ensure_conn` hardcodes `threads=1` (correct for production; part of the tested envelope), so the probe MUST NOT drive the join through `StreamFkJoiner` when it wants threads=N. Instead the probe opens its OWN `connect_duckdb(temp_dir=..., memory_limit=..., threads=<matrix value>, max_temp_directory_size=...)` connection, calls `_require_parent_build(conn)` (the same guard, so the probe exercises the real forced-build), registers the same child-keys reader + parent_keys view, and runs the SAME unordered SELECT that `_iter_unordered_join_rows` builds (import the SQL builder helper from `_stream_join`, or duplicate the one SELECT with a comment pointing at the source of truth so they cannot drift). This is a diagnostic harness deliberately parameterizing what production pins; it RECORDS order, never asserts it. Its purpose is the release-gate evidence that correctness cannot rest on incidental order (the sorter's shuffled-input tests are the correctness gate); running threads=N specifically is where DuckDB is MOST likely to reorder, so it is the highest-value row in the matrix. Wire a fast smoke test that the script runs and emits valid JSON on one tiny config.

- [ ] **Step 4: Documentation + cleanup**

- Add the honest customer guarantee paragraph (verbatim from this plan's header) to `docs/relationships-memory-scaling.md` under a new "Final OOC-B memory guarantee" section, with the operating conditions list: memory_limit substantially below the hard RAM ceiling; one DuckDB blocking query at a time; threads=1; explicit Arrow/sorter budgets; max_temp_directory_size plus Decoy's mid-operation disk accounting; transactional abort on DuckDB OOM or disk exhaustion.
- Confirm `uv.lock` pins duckdb 1.5.4; state in the same doc section that the path is certified on 1.5.4 and refuses (fail-closed, `out_of_core_duckdb_join_unsupported`) below 1.2.0 or without `build_side_probe_side`.
- `grep -rn "_batch_join" src/ tests/` and delete the module plus dead imports if nothing references it (pre-GA = hard delete).

- [ ] **Step 5: Run everything local**

```bash
uv run pytest tests/unit/execution -q -k "out_of_core or external_sort"
uv run pytest tests/parity/test_out_of_core_fk_parity.py -q
uv run pytest tests/perf/test_out_of_core_memory_sentinel.py -q -m "not benchmark"
ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 uv run pytest tests/perf/test_out_of_core_memory_sentinel.py -q -m perf -k plateau
uv run ruff format --check . && uv run ruff check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/ tests/perf/test_out_of_core_memory_sentinel.py docs/relationships-memory-scaling.md
git commit -m "test: dual-dimension plateau gates, order probe, memory-guarantee docs"
```

---

## Verification Gates (release requirements, from the architecture consult)

All of these are required before merge/ship; a lower-scale pass is NOT sufficient after the earlier false plateau.

1. External-sorter unit tests with SHUFFLED input pass (order-independence proven at the unit level). (Task 2)
2. DuckDB pin certified: locked 1.5.4; runtime fail-closed below 1.2.0 or when `build_side_probe_side` is absent from `duckdb_optimizers()`; never a silent no-op. Structural test green. (Task 3)
3. No-order join probed across threads=1/N (via the probe's own connection, since production pins threads=1), spill/no-spill, skew, nulls, orphans, composite keys, varying Arrow batch sizes; order RECORDED but never relied upon. (Task 9)
4. Plateau tests in BOTH dimensions (fixed parent/growing child AND fixed child/growing parent) plus high fan-in: nonzero temp-spill evidence in the sink each dimension stresses (reorder for child-growth, DuckDB for parent-growth) AND bounded RSS slope at 3+ growing sizes. (Task 9)
5. Full pandas byte-parity suite green, including payload-boundary misalignment, REMAP, overlapping edges, duplicate-parent last-write-wins, and the accepted nested/struct/map + zero-row divergences per `tests/parity/SEMANTIC_DIFFERENCES.md`. (Tasks 4, 7)
6. SCALE PROOF (the final quantitative gate, run BEFORE the human/model review so they review scale-proven code): 200M rows at an 8GB cap on GCP completes with bounded peak RSS and full parity, allocator env pinned (`ARROW_DEFAULT_MEMORY_POOL=system`, `MALLOC_ARENA_MAX=2` exported before process start). Run BOTH stressed shapes, not one: (a) a large CHILD (200M child rows) against a moderate parent, which drives the external sorter; and (b) a large PARENT (200M distinct parent keys) against a moderate child, which drives the DuckDB hash-join build. Both must show nonzero spill in the sink they stress (per the dual-dimension accounting) and a flat RSS plateau. Set `max_temp_directory_size` to a concrete disk budget and confirm the run stays within it. Use the decoy-platform `scripts/gcp-bench` harness; count each run against the 50-run budget and deliver results to Slack. If either shape OOMs, PARK and reconsult; do not iterate blind (this program's third strike already fired once).
7. Golden gate: `scripts/test_flight.py` green; zero reproducible P0/P1.
8. FINAL REVIEW (gates the merge, after 1-7 are all green): dennis (Opus) adversarial review, then the Codex cross-model pre-merge gate. Reviewing last means they see the scale evidence and the final code together, not a plan or a partial build.

## Self-Review

Checked against the consult's "Final mechanism", "Honest memory guarantee", and "Version and verification gates" sections plus the preserved scaffolding:

1. **Spec coverage.** Retained scaffolding (SpillChildKeys, SpillPayloadStore, raw-parent IPC, global row_nr): Task 1. Shared edge fixtures defined up front: Task 1 Step 4. Sequential edges: Task 7. Per-edge connection config (memory_limit share, threads=1, preserve_insertion_order=false, max_temp_directory_size, disabled_optimizers, child-left/parent-right, no ORDER BY): Tasks 3 and 7 (config), `_duckdb.py` already sets `preserve_insertion_order=false` for all OOC connections. External sorter steps 1-6 incl. `cursor.take` alignment: Tasks 2 and 4. Contiguity asserts (exactly N, 0 and N-1, adjacent diff 1, no dup/missing): Task 2 (`iter_ordered` arange-equality + final count). Byte-capped input slicing + single-run collapse + merge-head byte cap (the reorder resident accounting): Task 2. FAIL fresh-reader precount (joiner-level): Task 6; full-route zero-output assertion: Task 7. Parent-relation stage/winners/join-back split: Task 5. Fail-closed optimizer/version guard (BOTH optimizers required): Task 3. Budgets fail-closed when unbudgeted: Task 7 (`resolve_reorder_budgets` raises). Obsolete O(parent) preflight floor retired for the stream path + route seam stops dividing by incoming_edges+1: Task 8. Verification gates incl. both plateau dimensions, per-sink spill evidence, order probe, dual-shape 200M@8GB: Task 9 + gates list. Honest guarantee documented: header + Task 9 step 4. No gaps found.
2. **Placeholder scan.** No TBD/TODO/"handle edge cases" steps; every code step carries code. Two deliberate delegations remain and are named as such: probe-flag plumbing in Task 9 step 1 (contract specified, implementation trivial against the existing script) and the shared edge fixtures defined in Task 1 Step 4 (construction recipe stated inline, lifted from the restored branch's existing builders).
3. **Type consistency.** `run_ordered_join(batch_rows, *, run_bytes_cap, merge_fan_in=16)` is identical in Task 4 (definition), Tasks 6/7 (tests), Task 7 (driver call). `ReorderBudgets` fields match between Task 7's resolver and driver usage. `_iter_unordered_join_rows` naming matches between Tasks 3 and 4; the transient `iter_join_rows` shim is named consistently in Tasks 3, 4, 7. `ExternalRowNrSorter` constructor matches between Tasks 2 and 4. `enforce_stream_reorder_preflight` signature matches between Task 8 definition and its route-seam call. Error codes used in tests (`out_of_core_fk_reorder_contiguity`, `out_of_core_duckdb_join_unsupported`, `out_of_core_reorder_unbudgeted`, `out_of_core_reorder_undisked`) match their definitions.
4. **Judgment calls (resolved after the Codex plan review, 2026-07-22).** (a) The 50%/15%/16 split is now byte-accounted, not "tuned later": the two non-overlapping phases have resident peaks `duckdb + 2 x run` (drain+sort) and `2 x run` (merge, DuckDB freed), the merge's fan-in heads are individually capped at `run_bytes_cap/fan_in` so they sum to <= `run_bytes_cap`, and the fraction invariant `duckdb_fraction + 2 x run_fraction + headroom <= 1.0` is documented on `ReorderBudgets` and `_external_sort`. The plateau gates now VALIDATE the accounting rather than being the mechanism that discovers it. (b) FAIL precount ordering "per edge before its own join" is ACCEPTED (Codex ruling): output-before-error is preserved (no output exists until phase 3), only wasted work on a later-edge FAIL differs, and the full-route test asserting `sink_batches_written == 0` is retained (moved to Task 7). (c) The plan test now walks the `EXPLAIN (FORMAT JSON)` operator tree, NOT rendered text (Codex ruling): `explain_join()` returns a parsed dict and the test inspects operator names + the build subtree, which is stable across DuckDB point releases. All three Codex plan-review blockers on this section are cleared.
