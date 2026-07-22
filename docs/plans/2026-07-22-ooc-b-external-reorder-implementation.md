# OOC-B Final Architecture: Unordered DuckDB Join + Bounded External row_nr Reorder

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Provenance.** This is the FINAL OOC-B architecture, decided by a cross-model architecture consult (Codex gpt-5.6-sol, 2026-07-22) after the GCP 200M@8GB run proved a DuckDB-ordered streaming join cannot reach never-OOM (peak RSS ~21 GB; the dominant unbounded structure is the global `ORDER BY __decoy_row_nr` external sort, which `memory_limit` does not bound). PR #108 reverts main to the bounded O(parent) `_batch_join.py` path as the interim; the single-source-read scaffolding this plan rebuilds from is preserved on `origin/fix/ooc-b-memory-streaming-join`. Do not redesign the mechanism; it is fixed by the consult.
>
> **Build sequencing (Cam "Option B", 2026-07-22).** The two risky primitives were built and MEASURED first, as Milestone 1, so this wiring milestone stands on real numbers rather than paper accounting. **Milestone 1 is DONE** on this branch (`feat/ooc-b-external-reorder`): the byte-capped external sorter (`_external_sort.py`) and the reorder budget ledger (`_reorder_budget.py`), each with a measured subprocess RSS proof. This document is **Milestone 2: the wiring**, revised against M1's measured signatures and deviations. Every task below CONSUMES the M1 modules by their real signatures; none rebuilds them.

**Goal:** An out-of-core FK join that completes at ARBITRARY row count with resident memory independent of both parent and child row counts (spill disk scales with data), preserving single-source-read and byte-parity to the pandas oracle.

**Architecture:** Per FK edge, DuckDB performs ONE unordered, spillable, forced-parent-build external hash join (child on the left as a streamed Arrow IPC scan, deduplicated parent on the right as a `read_parquet` view, `build_side_probe_side` and `join_order` optimizers disabled, threads=1, NO `ORDER BY`). Decoy then restores `__decoy_row_nr` order with the Milestone-1 `ExternalRowNrSorter` (byte-capped sorted runs spilled to Arrow IPC, capped-fan-in k-way merge collapsing to a SINGLE ordered run), and the FK consumer wraps that ordered stream in a fail-closed 0..N-1 contiguity guard before feeding the existing `JoinRowCursor`. Incoming FK edges execute SEQUENTIALLY so DuckDB blocking operators from different edges are never co-resident, and parent-relation last-write-wins dedup is split into single-blocking-operator queries (stage, winners, join-back). Everything else (SpillChildKeys, SpillPayloadStore, raw-parent-key IPC staging, global monotonic `__decoy_row_nr`, `resolve_batch` + REMAP semantics, payload-batch-aligned resolution) is retained from the preserved scaffolding.

**Honest customer guarantee (what "done" delivers, to be documented verbatim):** "The architecture holds no O(parent) or O(child) resident structure. Working memory is O(DuckDB budget + sort-run budget + merge fan-in buffers + Arrow batches), independent of row count. Parent, child keys, payload, join state, and ordered runs all spill to disk. Completes at any N for which sufficient spill disk exists and individual values stay within supported Arrow/DuckDB limits. This is a tested process-memory envelope for the pinned DuckDB version, not a raw memory_limit promise."

**Tech Stack:** DuckDB 1.5.4 (public core API only), PyArrow (IPC streams, `pyarrow.compute` sort/take), the preserved `out_of_core/` single-read modules, the M1 `_external_sort` + `_reorder_budget` modules, pytest + Hypothesis parity harness.

---

## Milestone 1 (DONE): sorter + budget ledger

Built and measured on `feat/ooc-b-external-reorder` (commits `cb308ba` budget, `389939c` sorter, `903a52e` OSFile fix + measured proof, `15863a9` measured-results doc). Guide: `docs/plans/2026-07-22-ooc-b-milestone-1-sorter-budget.md`. **Do not rebuild these.** Milestone 2 imports and calls them by the signatures below.

### `_reorder_budget.py` (built)

```python
F_DUCKDB = 0.55      # process-ceiling share for the DuckDB memory_limit (join+buffer phase)
F_SORT   = 0.15      # process-ceiling share for the sorter run_bytes_cap
MIN_RUN_BYTES = 8 * 1024 * 1024
# assert round(F_DUCKDB + F_SORT, 9) <= 0.70  # import-time invariant; >=0.30 reserve

@dataclass(frozen=True)
class ReorderBudgets:
    process_ceiling_bytes: int
    duckdb_memory_limit_bytes: int     # BYTES, not an "<n>MB" string
    run_bytes_cap: int
    merge_fan_in: int
    remaining_disk_bytes: int

def resolve_reorder_budgets(
    process_ceiling_bytes: int | None,
    remaining_disk_bytes: int | None,
    *, merge_fan_in: int = 16,
) -> ReorderBudgets: ...
# Raises ExecutionError(code="out_of_core_reorder_unbudgeted") if EITHER arg is None.
# Raises ExecutionError(code="out_of_core_reorder_budget_too_small") if F_SORT*ceiling < MIN_RUN_BYTES.

def require_disk(
    budgets: ReorderBudgets, *,
    mandatory_staging_bytes: int,
    estimated_output_bytes: int,
) -> None: ...
# Raises out_of_core_reorder_budget_too_small unless
#   mandatory_staging_bytes + 3 * estimated_output_bytes <= budgets.remaining_disk_bytes.
# The 3x = duckdb temp (<= output) + sorter runs (~= output) + k-way merge amplification (~= output,
# old runs + newly-merged run coexist).
```

Deviations from the original M2 assumptions that this milestone locks in, and which M2 MUST honor:
- The budget takes `(process_ceiling_bytes, remaining_disk_bytes)`, NOT `(budget_bytes, memory_limit)`. There is no `"<n>MB"` string arm; the DuckDB limit is returned in BYTES; the disk budget is CARRIED on the dataclass. Callers format `f"{budgets.duckdb_memory_limit_bytes // (1024*1024)}MB"` for `connect_duckdb`.
- `F_DUCKDB = 0.55` (not the paper 0.50). The reserve is `1 - 0.55 - 0.15 = 0.30`, asserted at import.
- Fail-closed is on either-None, with a distinct `out_of_core_reorder_budget_too_small` for a ceiling too small to clear `MIN_RUN_BYTES`.
- `require_disk` IS the disk ledger. M2 must actually CALL it before every blocking phase (parent dedup and each edge join), not merely set `max_temp_directory_size`.

### `_external_sort.py` (built)

```python
class ExternalRowNrSorter:
    def __init__(self, spill_dir: Path, run_bytes_cap: int, merge_fan_in: int,
                 row_nr_column: str = "__decoy_row_nr") -> None: ...
    def write(self, batch: pa.RecordBatch) -> None: ...      # byte-caps + materializes; flush-before-cap
    def finish(self) -> None: ...                            # merge tree -> ONE final run
    def iter_ordered(self) -> Iterator[pa.RecordBatch]: ...  # NO expected_rows arg
    def close(self) -> None: ...                             # remove run files; idempotent
    # observability: .peak_buffered_bytes, .peak_pre_sort_buffer_bytes, .peak_merge_resident_bytes
# Error codes it raises: out_of_core_sort_row_too_wide (single row > run_bytes_cap),
#   out_of_core_sort_invalid_state (write-after-finish / iter-before-finish),
#   out_of_core_reorder_budget_too_small (non-positive cap / fan_in < 2).
```

Measured facts M2 depends on (from the M1 results doc):
- **Peak RSS ~= 1.35 x the run_bytes_cap ceiling** while streaming ~10x the cap through 11-12 real spilled runs, single final merge, allocator pinned (`ARROW_DEFAULT_MEMORY_POOL=system`, `MALLOC_ARENA_MAX=2`). `ENVELOPE_FACTOR = 1.35` is the number the scale gate (Task 8) and the route's `process_ceiling` derivation (Task 7) use.
- Run reads use `pa.OSFile`, NOT `pa.memory_map` (memory_map left whole-file touched pages resident, invisible to pyarrow's pool). M2 code that reads runs must do the same.
- The per-merge-head cap is `run_bytes_cap // (2 * merge_fan_in)` (halved), covering the merge round's transient concat+sort copy. Internal to the sorter; M2 does not re-derive it.
- **The sorter is FK-agnostic: `iter_ordered()` sorts by `row_nr_column` and does NOT assert a dense 0..N-1 range.** The FK route's fail-closed "exactly 0..N-1" contiguity guard therefore lives in the CONSUMER (`run_ordered_join`, Task 3), not in `_external_sort.py`. This is a deliberate separation; M2 adds the FK-semantic guard on top.

---

## Milestone 2: wiring (8 tasks)

## Global Constraints

- DuckDB PUBLIC CORE API ONLY. No source customization, no community/third-party extensions, no internals/monkeypatching. Annotate any dependence on documented-but-subtle behavior.
- Do NOT rely on DuckDB probe-side output order for correctness. The external sorter is what guarantees order; the sorter's own shuffled-input unit tests (M1) are the order-independence correctness gate.
- Pin/certify DuckDB 1.5.4. If `build_side_probe_side` OR `join_order` is absent from `duckdb_optimizers()`, or the version predates external hash join (< 1.2.0), fail closed (`out_of_core_duckdb_join_unsupported`) and route to the O(parent) path. Never silently no-op.
- Byte-parity to `tests/parity/test_out_of_core_fk_parity.py` is the hard gate; honor the accepted-divergence contract (`tests/parity/SEMANTIC_DIFFERENCES.md`) for nested/struct/map keys + zero-row empty types. Do NOT touch `_join.py::mask_child_fk` (resident oracle path). No em-dashes in prose.
- Per-edge DuckDB connection config (Codex, copy exactly): `memory_limit` from `budgets.duckdb_memory_limit_bytes` (0.55 of the process ceiling); `threads=1`; `preserve_insertion_order=false`; `max_temp_directory_size` set from the disk ledger; `disabled_optimizers='join_order,build_side_probe_side'`. Child on LEFT, deduplicated parent on RIGHT. NO `ORDER BY`.
- Single source read is invariant: the raw child is read exactly once in phase 1; every later pass reads Decoy spills. The FAIL orphan precount rereads the child-key spill via a FRESH IPC reader.
- Resolution stays at payload-store batch boundaries: `cursor.take(payload_batch_rows, expected_row_nr)` feeding `resolve_batch` is unchanged.
- The FK consumer MUST assert fail-closed contiguity on the ordered stream: exactly N rows; first/last row_nr are 0 and N-1; every adjacent row_nr differs by 1; no duplicate or missing row_nr. This guard is `run_ordered_join`'s (Task 3), wrapping the M1 sorter's `iter_ordered()`.
- **Route derives `process_ceiling_bytes = int(container_RAM_bytes / ENVELOPE_FACTOR)`** with `ENVELOPE_FACTOR = 1.35` (M1-measured). The scale-gate RSS pass criterion is peak RSS <= `container_RAM_bytes`.
- Allocator env for every capped/scale measurement: `ARROW_DEFAULT_MEMORY_POOL=system`, `MALLOC_ARENA_MAX=2`, exported BEFORE process start.
- Pre-GA, fix-forward on a branch. Repo comment rule: explain why, not what; no references to the current task/PR/author.

## File Structure

```
src/decoy_engine/execution/out_of_core/
  _external_sort.py       DONE (M1)  ExternalRowNrSorter (consumed by Task 3)
  _reorder_budget.py      DONE (M1)  resolve_reorder_budgets + require_disk (consumed by Tasks 6, 7)
  _stream_join.py         MOD   drop ORDER BY; lazy per-edge connection; fail-closed guard;
                                run_ordered_join wiring the M1 sorter + contiguity guard (Tasks 2, 3)
  _stream_driver.py       MOD   sequential per-edge phase 2; disk ledger via require_disk (Task 6)
  _relation.py            MOD   dedup split into stage/winners/join-back single-blocking plans (Task 4)
  _duckdb.py              MOD   threads / max_temp_directory_size connection params (Task 2)
  _memory_estimate.py     MOD   split O(parent) preflight into resident floor + stream envelope check (Task 7)
src/decoy_engine/execution/
  _pipeline_route_exec.py   MOD   route seam: dispatch on (memory, disk, sink); derive process_ceiling
                                  from RAM/ENVELOPE_FACTOR; call the route-matched preflight;
                                  keep _batch_join.py as the resident fallback (Task 7)

tests/unit/execution/
  _ooc_fixtures.py                      NEW   shared edge fixtures incl. a COMPLETE four-edge route (Task 1)
  test_out_of_core_fixtures_smoke.py    NEW   fixture smoke test (Task 1)
  test_out_of_core_stream_join.py       MOD   unordered-join + guard + plan test + ordered pipeline +
                                              contiguity + teardown (Tasks 2, 3, 5)
  test_out_of_core_relation.py          MOD   split-dedup dual-EXPLAIN plan test (Task 4)
  test_out_of_core_runner_streaming.py  MOD   sequential-edge lifecycle + 4-edge byte-lifetime (Task 6)
  test_out_of_core_duckdb.py            MOD   pinned-optimizer structural test + threads/temp params (Task 2)
  test_pipeline_route_exec.py           MOD   dispatch across (memory,disk,sink) combos (Task 7)
tests/perf/
  test_out_of_core_memory_sentinel.py   MOD   dual-dimension plateau + spill-evidence gates (Task 8)
scripts/
  ooc_child_key_plateau_probe.py        MOD   --grow child|parent; separate duckdb/reorder temp accounting (Task 8)
  ooc_join_order_probe.py               NEW   diagnostic order recorder, never an order assert (Task 8)
```

Responsibility boundaries: `_external_sort.py` (M1) knows nothing about FK semantics; `_stream_join.py` owns the join SQL, connection lifecycle, the sorter hookup, and the FK contiguity guard; `_stream_driver.py` owns edge sequencing and the disk ledger; `_relation.py` owns dedup; budgets live in `_reorder_budget.py` (M1).

---

## Task 1: Restore the single-read scaffolding + shared fixtures onto this branch

The M1 primitives are already on `feat/ooc-b-external-reorder`, but the single-read streaming scaffolding (`_stream_join.py`, `_stream_driver.py`, `_payload_store.py`, `_runner.py` wiring, `JoinRowCursor`, and their tests) is NOT: this branch was cut from post-revert main, which has `_batch_join.py` (O(parent)) only. Bring the scaffolding over WITHOUT clobbering the two M1 modules, and define the shared fixtures (including the complete four-edge route) up front so every later task commits green.

**Files:**
- Merge only (no hand-written source edits) for the scaffolding; then:
- Create: `tests/unit/execution/_ooc_fixtures.py`, `tests/unit/execution/conftest.py` (register fixtures), `tests/unit/execution/test_out_of_core_fixtures_smoke.py`

- [ ] **Step 1: Merge the preserved scaffolding, keeping M1 modules**

```bash
cd /home/cam/vscode/decoy-engine/.claude/worktrees/ooc-b-reorder
git fetch origin
git merge --no-ff origin/fix/ooc-b-memory-streaming-join
```

Conflict resolution rule: under `src/decoy_engine/execution/out_of_core/`, take the fix branch's side for the streaming scaffolding files it owns (`_stream_join.py`, `_stream_driver.py`, `_payload_store.py`, `_runner.py`, `_relation.py`, and their `test_out_of_core_*` tests), take THIS branch's side (`git checkout --ours -- <path>`) for `_external_sort.py` and `_reorder_budget.py` (M1, must survive untouched), and for `_memory_estimate.py`/`_duckdb.py` keep BOTH sides' additions (merge by hand). If `_batch_join.py` is present on both, KEEP it (it is the resident fallback; do not delete). `_runner.py` must import `_stream_driver.stream_table`.

- [ ] **Step 2: Verify the M1 modules are intact and the baseline is green**

```bash
uv run pytest tests/unit/execution/test_ooc_external_sort.py tests/unit/execution/test_ooc_reorder_budget.py -q   # M1 suites still pass
uv run pytest tests/parity/test_out_of_core_fk_parity.py tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_runner_streaming.py -q
```

Expected: all pass. If an M1 test file name differs, `grep -rln ExternalRowNrSorter tests/` to locate it. The M1 modules must be byte-identical to their committed form after the merge (`git diff 15863a9 -- src/decoy_engine/execution/out_of_core/_external_sort.py src/decoy_engine/execution/out_of_core/_reorder_budget.py` empty).

- [ ] **Step 3: Define the shared edge fixtures (all of them, including the four-edge route)**

Create `tests/unit/execution/_ooc_fixtures.py` exposing pytest fixtures (registered via `conftest.py`):

- `simple_edge_fixture`: one parent + one child (no orphans), `.make_joiner(tmp_path) -> StreamFkJoiner`, `.child_batches`, `.n_child_rows`, `.oracle_resolved` (resolved batches from the resident `mask_child_fk` path on the same inputs and seed).
- `remap_edge_fixture`: parent/child with orphans under `OrphanPolicy.REMAP`; `.edge`, `.payload_batch_sizes` (deliberately straddling reader batch size), `.oracle_resolved_per_payload_batch`.
- `orphan_edge_fixture`: parent/child with a known orphan count; `.n_orphans`, `.corrupt_source()` (mutates the on-disk source to prove the precount reads the spill, not the source).
- `fail_policy_route_fixture`: a full single-table route under `OrphanPolicy.FAIL` with orphans present; `.run()` drives `stream_table`; `.sink_batches_written`.
- `four_edge_route`: a route with EXACTLY four incoming FK edges, each with distinct cardinality/orphan mix, all present unconditionally (not a `if support: ...` variant). `.run()` drives `stream_table`; `.edge_reorder_dirs` returns the four `edge_*/reorder` dirs; `.oracle_resolved` from the resident path. This is the fixture Task 6's byte-lifetime test and Task 8's high-fan-in plateau reuse; it must exist and be exercised by the smoke test now so it cannot silently rot.

Each fixture's `make_joiner`/`run` reuses the restored `StreamFkJoiner`/`stream_table` builders (lift them, do not invent). Smoke test in `test_out_of_core_fixtures_smoke.py`:

```python
def test_fixtures_construct(simple_edge_fixture, remap_edge_fixture, orphan_edge_fixture, four_edge_route):
    assert simple_edge_fixture.n_child_rows > 0
    assert simple_edge_fixture.oracle_resolved
    assert orphan_edge_fixture.n_orphans > 0
    assert remap_edge_fixture.payload_batch_sizes
    assert len(four_edge_route.edge_reorder_dirs) == 4  # complete, unconditional
```

Run: `uv run pytest tests/unit/execution/test_out_of_core_fixtures_smoke.py -q`. Expected: green.

- [ ] **Step 4: Commit (explicit paths)**

```bash
git add src/decoy_engine/execution/out_of_core/ tests/unit/execution/_ooc_fixtures.py tests/unit/execution/conftest.py tests/unit/execution/test_out_of_core_fixtures_smoke.py
git commit -m "chore: restore single-read OOC scaffolding and shared edge fixtures (incl four-edge route)"
```

No `git add -A`: the merge touches only the OOC subtree and its tests. If the merge produced conflicts outside these paths, resolve and add them by name.

---

## Task 2: `StreamFkJoiner` unordered join, pragma set, fail-closed guard, JSON plan test

Drop the `ORDER BY`, force the written join order (child LEFT probe, deduplicated parent RIGHT build), make the guard fail closed instead of silently no-op, and move the connection to lazy per-edge open. `iter_join_rows` stays as a transient ordered shim (removed in Task 6). The plan test walks `EXPLAIN (FORMAT JSON)` and asserts operator TYPES, not the view alias.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_duckdb.py` (new `threads` / `max_temp_directory_size` params)
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py`, `tests/unit/execution/test_out_of_core_duckdb.py`

**Interfaces:**
- Consumes: `connect_duckdb(temp_dir=..., memory_limit=..., threads=..., max_temp_directory_size=...)`.
- Produces (for Task 3): `StreamFkJoiner` with the SAME constructor plus keyword `max_temp_directory_size: str | None = None`; no connection opened at construction; `_ensure_conn()` opens/configures on demand; a NEW private `_iter_unordered_join_rows(batch_rows)`; `explain_join() -> dict` (parsed JSON plan). Module constants `_BUILD_SIDE_SWAP_OPTIMIZER = "build_side_probe_side"`, `_JOIN_ORDER_OPTIMIZER = "join_order"`, `_MIN_STREAM_JOIN_DUCKDB = (1, 2, 0)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/execution/test_out_of_core_stream_join.py`:

```python
def test_build_side_optimizer_absent_fails_closed(monkeypatch, tmp_path, simple_edge_fixture):
    """A missing/renamed build_side_probe_side (or join_order) must raise, never
    no-op: a silent no-op restores an O(child) hash build (the 21 GB regression)."""
    from decoy_engine.execution.out_of_core import _stream_join

    joiner = simple_edge_fixture.make_joiner(tmp_path)
    monkeypatch.setattr(_stream_join, "_optimizer_names", lambda conn: {"join_order"})  # build_side missing
    joiner.stage_keys(simple_edge_fixture.child_batches)
    with pytest.raises(ExecutionError, match="out_of_core_duckdb_join_unsupported"):
        joiner.total_orphans()


def test_join_plan_builds_on_parent_and_has_no_order_by(tmp_path, simple_edge_fixture):
    """EXPLAIN (FORMAT JSON): the physical plan's root HASH_JOIN must have the
    child ARROW_SCAN as children[0] (probe) and the parent PARQUET_SCAN as
    children[1] (build), and NO ORDER_BY anywhere. Assert on operator TYPE names,
    NOT the view alias 'parent_keys' (which does not appear in the 1.5.4 physical
    plan) and NOT rendered text."""

    def _walk(node):
        yield node
        for child in node.get("children", []):
            yield from _walk(child)

    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    plan = joiner.explain_join()  # parsed dict; explain_join unwraps the EXPLAIN root list
    nodes = list(_walk(plan))
    names = {str(n.get("name", "")).upper() for n in nodes}
    assert not any("ORDER_BY" in nm for nm in names)
    hash_joins = [n for n in nodes if "HASH_JOIN" in str(n.get("name", "")).upper()]
    assert hash_joins, "expected a HASH_JOIN operator in the physical plan"
    root = hash_joins[0]
    probe_types = {str(n.get("name", "")).upper() for n in _walk(root["children"][0])}
    build_types = {str(n.get("name", "")).upper() for n in _walk(root["children"][1])}
    assert any("ARROW_SCAN" in t for t in probe_types), f"probe side not ARROW_SCAN: {probe_types}"
    assert any("PARQUET_SCAN" in t for t in build_types), f"build side not PARQUET_SCAN: {build_types}"


def test_connection_is_lazy_and_configured(tmp_path, simple_edge_fixture):
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    assert joiner._conn is None
    conn = joiner._ensure_conn()
    disabled = conn.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    assert "build_side_probe_side" in disabled and "join_order" in disabled
    assert conn.execute("SELECT current_setting('threads')").fetchone()[0] in (1, "1")
    joiner.close()
    assert joiner._conn is None
```

Append to `tests/unit/execution/test_out_of_core_duckdb.py`:

```python
def test_pinned_duckdb_exposes_required_optimizers(tmp_path):
    """Structural pin on the locked DuckDB (1.5.4): BOTH optimizers this route
    disables must exist. A rename/removal on upgrade fails HERE, loudly, before
    any scale run can silently regress the build side."""
    conn = connect_duckdb(temp_dir=tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM duckdb_optimizers()").fetchall()}
    assert "build_side_probe_side" in names
    assert "join_order" in names


def test_connect_duckdb_threads_and_temp_size(tmp_path):
    conn = connect_duckdb(temp_dir=tmp_path, memory_limit="128MB", threads=1,
                          max_temp_directory_size="1024MB")
    assert conn.execute("SELECT current_setting('threads')").fetchone()[0] in (1, "1")
    assert "1024" in str(conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0])
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_duckdb.py -q`. Expected: new tests FAIL.

- [ ] **Step 3: Implement `_duckdb.py` changes** — extend `connect_duckdb` with `threads: int | None = None` and `max_temp_directory_size: str | None = None`, setting `config["threads"]`/`config["max_temp_directory_size"]` when provided; keep `preserve_insertion_order=False` for all OOC connections and the memory-derived thread default for other callers.

- [ ] **Step 4: Implement `_stream_join.py` changes**

4a. Replace the old build-side guard with a fail-closed `_require_parent_build(conn)` that REQUIRES BOTH optimizers and version >= (1,2,0), raising `ExecutionError(code="out_of_core_duckdb_join_unsupported")` otherwise, then `SET disabled_optimizers` to the union of existing + both names. Add a mockable `_optimizer_names(conn)` helper.

4b. Lazy connection: `__init__` sets `self._conn = None`, stores `self._memory_limit`, `self._max_temp_directory_size`. `_ensure_conn()` opens `connect_duckdb(temp_dir=self._temp_dir/"duckdb", memory_limit=self._memory_limit, threads=1, max_temp_directory_size=self._max_temp_directory_size)`, calls `_require_parent_build(conn)`, then `CREATE TEMP VIEW parent_keys AS SELECT * FROM read_parquet(<relation.path>)`, closing the connection on any exception. `total_orphans` starts with `conn = self._ensure_conn()`.

4c. Add private `_iter_unordered_join_rows(batch_rows)` (copy `iter_join_rows`, drop the `ORDER BY`; child LEFT JOIN parent on the join key; fresh reader, register/unregister, `to_arrow_reader(batch_rows)`). Docstring: batches are UNORDERED; the only consumer is `run_ordered_join` (Task 3). Leave the existing `iter_join_rows` (with its ORDER BY) live as a shim until Task 6.

4d. Add `explain_join() -> dict`: run `EXPLAIN (FORMAT JSON) <the UNORDERED SELECT>` (register a fresh reader as in `_iter_unordered_join_rows`), `json.loads` the returned string, and UNWRAP the EXPLAIN root (DuckDB returns a single-element list whose element carries the plan tree; return that element dict so `_walk` starts at the physical root). Rewrite the module docstring's join description to the unordered-join + Decoy-owned reorder story.

- [ ] **Step 5: Run** — `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_duckdb.py -q` then `uv run pytest tests/unit/execution -q -k out_of_core && uv run pytest tests/parity/test_out_of_core_fk_parity.py -q`. All green (the ordered shim keeps the driver + parity unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_duckdb.py src/decoy_engine/execution/out_of_core/_stream_join.py tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_duckdb.py
git commit -m "feat: unordered forced-parent-build stream join with fail-closed guard and JSON plan"
```

---

## Task 3: `run_ordered_join`: unordered join into the M1 sorter, contiguity guard, out to `JoinRowCursor`

Drain the unordered join into the M1 `ExternalRowNrSorter` while the DuckDB connection is live, close the connection BEFORE `finish()` (so join state and merge buffers are never co-resident), then wrap the sorter's `iter_ordered()` in the fail-closed 0..N-1 contiguity guard and hand that to `JoinRowCursor`. `resolve_batch`, REMAP, and payload-boundary alignment are untouched.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py`

**Interfaces:**
- Consumes: `ExternalRowNrSorter(spill_dir, run_bytes_cap, merge_fan_in)` (M1), `_iter_unordered_join_rows` (Task 2).
- Produces (for Task 6): `StreamFkJoiner.run_ordered_join(batch_rows: int, *, run_bytes_cap: int, merge_fan_in: int = 16) -> Iterator[pa.RecordBatch]`. Blocking work (join drain, run sorts, merges) runs eagerly inside the call; the returned iterator streams the contiguity-guarded final merge. The DuckDB connection is CLOSED by the time the call returns. New error code: `out_of_core_fk_reorder_contiguity` (FK-consumer layer, NOT the sorter).

- [ ] **Step 1: Write the failing tests**

```python
def test_run_ordered_join_matches_oracle_order_and_values(tmp_path, simple_edge_fixture):
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    batches = list(joiner.run_ordered_join(1_000, run_bytes_cap=8 * 1024 * 1024))
    assert joiner._conn is None  # connection released before streaming
    row_nrs = [n for b in batches for n in b.column("__decoy_row_nr").to_pylist()]
    assert row_nrs == list(range(simple_edge_fixture.n_child_rows))
    assert [joiner.resolve_batch(b) for b in batches] == simple_edge_fixture.oracle_resolved


def test_pipeline_does_not_depend_on_duckdb_order(monkeypatch, tmp_path, simple_edge_fixture):
    """Reverse every unordered batch AND the sequence; the sorter+guard must
    still deliver 0..N-1."""
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    real = joiner._iter_unordered_join_rows

    def scrambled(batch_rows):
        collected = list(real(batch_rows))
        for batch in reversed(collected):
            t = pa.Table.from_batches([batch])
            yield t.take(list(range(batch.num_rows - 1, -1, -1))).to_batches()[0]

    monkeypatch.setattr(joiner, "_iter_unordered_join_rows", scrambled)
    batches = list(joiner.run_ordered_join(1_000, run_bytes_cap=8 * 1024 * 1024))
    assert [n for b in batches for n in b.column("__decoy_row_nr").to_pylist()] == \
        list(range(simple_edge_fixture.n_child_rows))


def test_contiguity_guard_fails_closed_on_gap(monkeypatch, tmp_path, simple_edge_fixture):
    """If the ordered stream is missing a row_nr (e.g. an upstream parent-dedup
    bug drops a child row), run_ordered_join raises out_of_core_fk_reorder_contiguity,
    never silently emits a short/duplicated stream. Drop one batch mid-stream."""
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    real = joiner._iter_unordered_join_rows
    monkeypatch.setattr(joiner, "_iter_unordered_join_rows",
                        lambda br: (b for i, b in enumerate(real(br)) if i != 0))  # drop first batch
    with pytest.raises(ExecutionError, match="out_of_core_fk_reorder_contiguity"):
        list(joiner.run_ordered_join(1_000, run_bytes_cap=8 * 1024 * 1024))


def test_remap_and_payload_misalignment_survive_reorder(tmp_path, remap_edge_fixture):
    joiner = remap_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(remap_edge_fixture.child_batches)
    cursor = JoinRowCursor(
        joiner.run_ordered_join(97, run_bytes_cap=8 * 1024 * 1024),
        remap_edge_fixture.edge.child_columns,
    )
    out, row_nr = [], 0
    for size in remap_edge_fixture.payload_batch_sizes:
        out.append(joiner.resolve_batch(cursor.take(size, row_nr)))
        row_nr += size
    cursor.assert_exhausted()
    assert out == remap_edge_fixture.oracle_resolved_per_payload_batch


def test_close_before_finish_and_runs_cleaned(monkeypatch, tmp_path, simple_edge_fixture):
    """Teardown ordering is tested, not assumed: the DuckDB connection is closed
    during the eager phase (before iteration starts, i.e. before finish's merges
    stream out), and after close() no run files survive."""
    joiner = simple_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(simple_edge_fixture.child_batches)
    events = []
    real_close_conn = joiner._close_conn
    monkeypatch.setattr(joiner, "_close_conn",
                        lambda: (events.append("close_conn"), real_close_conn())[1])
    it = joiner.run_ordered_join(1_000, run_bytes_cap=8 * 1024 * 1024)
    assert joiner._conn is None and "close_conn" in events  # closed during the eager phase
    list(it)
    joiner.close()
    reorder_dir = joiner._temp_dir / "reorder"
    assert not any(reorder_dir.glob("*.arrow"))
    joiner.close()  # idempotent
```

(`simple_edge_fixture`, `remap_edge_fixture` from Task 1.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py -q -k "run_ordered or reorder or contiguity or close_before"`. Expected: FAIL (`run_ordered_join` undefined).

- [ ] **Step 3: Implement `run_ordered_join` + the contiguity guard**

```python
    def run_ordered_join(
        self, batch_rows: int, *, run_bytes_cap: int, merge_fan_in: int = 16,
    ) -> Iterator[pa.RecordBatch]:
        """One edge's ordered join stream: unordered DuckDB join, Decoy reorder.

        Blocking work runs EAGERLY here, in strict sequence, so at most one
        blocking operator's state is resident at a time: (1) the forced-parent-
        build hash join drains into the M1 sorter's byte-capped runs; (2) the
        connection CLOSES, releasing every DuckDB allocation; (3) the sorter's
        merge passes collapse to one run. The returned iterator streams the final
        run, wrapped in a fail-closed 0..N-1 contiguity guard. N is the staged
        child row count; the parent side is deduplicated, so the LEFT JOIN is
        row-preserving and the merged stream MUST be exactly 0..N-1. The sorter
        itself is FK-agnostic (it only sorts by row_nr); this contiguity check is
        the FK-semantic guard, and it also catches an upstream parent-dedup
        failure that would drop or duplicate a child row.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before run_ordered_join")
        sorter = ExternalRowNrSorter(
            spill_dir=self._temp_dir / "reorder",
            run_bytes_cap=run_bytes_cap,
            merge_fan_in=merge_fan_in,
        )
        self._sorter = sorter  # close() owns run-file cleanup
        try:
            for batch in self._iter_unordered_join_rows(batch_rows):
                sorter.write(batch)
        finally:
            self._close_conn()  # free DuckDB before merges; must NOT touch the sorter
        sorter.finish()
        return self._iter_contiguous(sorter.iter_ordered(), expected_rows=self._staged_rows)

    def _iter_contiguous(
        self, ordered: Iterator[pa.RecordBatch], *, expected_rows: int
    ) -> Iterator[pa.RecordBatch]:
        emitted = 0
        for batch in ordered:
            expected = pa.array(range(emitted, emitted + batch.num_rows), type=pa.int64())
            if pc.all(pc.equal(batch.column("__decoy_row_nr"), expected)).as_py() is not True:
                raise ExecutionError(
                    code="out_of_core_fk_reorder_contiguity",
                    message=(
                        f"reordered FK join output is not contiguous at emitted={emitted}: "
                        "the merged stream must be exactly 0..N-1 (one row per child row, "
                        "parent deduplicated). Fail-closed guard, never silent truncation, "
                        "duplication, or misordering."
                    ),
                )
            emitted += batch.num_rows
            yield batch
        if emitted != expected_rows:
            raise ExecutionError(
                code="out_of_core_fk_reorder_contiguity",
                message=f"reordered stream carried {emitted} rows, expected exactly {expected_rows}.",
            )
```

Split the two lifecycles explicitly (mirrors the M1 sorter's own close/finish separation):

```python
    def _close_conn(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def close(self) -> None:
        self._close_conn()
        if self._sorter is not None:
            self._sorter.close()   # M1 sorter.close(): removes run files, idempotent
            self._sorter = None
```

Import `ExternalRowNrSorter` and `pyarrow.compute as pc` at module top; init `self._sorter = None`. The driver's `shutil.rmtree` in `finally` remains the crash backstop.

- [ ] **Step 4: Run** — `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py -q` then `uv run pytest tests/parity/test_out_of_core_fk_parity.py -q`. Green; the driver still uses the ordered shim, so parity is unchanged by this additive commit.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_stream_join.py tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "feat: run_ordered_join drives the M1 sorter with a fail-closed contiguity guard"
```

---

## Task 4: Spillable parent-relation dedup (stage, winners, join-back as separate plans) + dual-EXPLAIN test

`_relation.py::_build_relation` runs the `max(row_nr) GROUP BY` and the join-back inside ONE query: two blocking operators co-resident. Split into three single-blocking-operator plans (stage, winners to disk, join-back reading winners from disk). The plan test parses `EXPLAIN (FORMAT JSON)` for BOTH the winners and join-back statements and asserts they are SEPARATE physical plans.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_relation.py` (`_build_relation` only)
- Test: `tests/unit/execution/test_out_of_core_relation.py`

**Interfaces:** `_build_relation`'s signature and returned `ParentKeyRelation` are unchanged; only the internal plan shape changes. Output Parquet must be value-identical to the single-query form.

- [ ] **Step 1: Write the failing tests**

```python
def test_dedup_runs_as_two_separate_physical_plans(tmp_path, monkeypatch):
    """Parse EXPLAIN (FORMAT JSON) for the two dedup statements: the winners plan
    has an aggregate WITHOUT a join; the join-back plan has a join WITHOUT an
    aggregate. Two separate physical plans, not just two SQL strings."""
    from decoy_engine.execution.out_of_core import _relation

    explains: dict[str, dict] = {}
    real_connect = _relation.connect_duckdb

    class _SpyConn:
        # DuckDBPyConnection.execute is a read-only builtin; wrap, do not reassign.
        def __init__(self, inner): self._inner = inner
        def execute(self, sql, *a, **k):
            res = self._inner.execute(sql, *a, **k)
            if isinstance(sql, str) and sql.strip().upper().startswith("COPY (") and (
                "GROUP BY" in sql or "JOIN" in sql
            ):
                key = "winners" if "GROUP BY" in sql else "joinback"
                inner = "SELECT" + sql.split("SELECT", 1)[1].rsplit(") TO", 1)[0]
                plan = self._inner.execute(f"EXPLAIN (FORMAT JSON) {inner}").fetchone()[1]
                explains[key] = json.loads(plan)
            return res
        def __getattr__(self, name): return getattr(self._inner, name)

    monkeypatch.setattr(_relation, "connect_duckdb", lambda **kw: _SpyConn(real_connect(**kw)))
    build_small_relation(tmp_path)  # existing helper

    def names(plan):
        out, stack = set(), [plan[0] if isinstance(plan, list) else plan]
        while stack:
            n = stack.pop(); out.add(str(n.get("name", "")).upper()); stack += n.get("children", [])
        return out

    win, jb = names(explains["winners"]), names(explains["joinback"])
    assert any("AGGREGATE" in n or "GROUP" in n for n in win) and not any("JOIN" in n for n in win)
    assert any("JOIN" in n for n in jb) and not any("AGGREGATE" in n or "GROUP" in n for n in jb)


def test_split_dedup_is_value_identical_and_cleans_scratch(tmp_path):
    relation = build_small_relation(tmp_path)  # last-write-wins fixture
    assert pq.read_table(relation.path).to_pydict() == EXPECTED_LAST_WRITE_WINS
    leftovers = list(tmp_path.rglob("*_key_staged.parquet")) + list(tmp_path.rglob("*_key_winners.parquet"))
    assert leftovers == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/execution/test_out_of_core_relation.py -q -k dedup`. Expected: FAIL.

- [ ] **Step 3: Implement the split** — replace the single combined `COPY (... JOIN (SELECT max ...) ...)` with three statements on one connection: (1) `COPY parent_keys TO <staged.parquet>`; (2) `COPY (SELECT __decoy_fk_join_key, max(__decoy_row_nr) AS __decoy_win_row_nr FROM read_parquet(<staged>) GROUP BY __decoy_fk_join_key) TO <winners.parquet>`; (3) `COPY (SELECT s.__decoy_fk_join_key, <masked cols> FROM read_parquet(<staged>) s JOIN read_parquet(<winners>) w ON s.__decoy_fk_join_key = w.__decoy_fk_join_key AND s.__decoy_row_nr = w.__decoy_win_row_nr) TO <out>`. `finally`: close conn, unlink staged + winners. Extend the existing WHY comment with one sentence on the split (winners land on disk so aggregate and join are never co-resident blocking operators; cite the DuckDB workload-tuning page).

- [ ] **Step 4: Run** — `uv run pytest tests/unit/execution/test_out_of_core_relation.py tests/unit/execution/test_out_of_core_relation_streamed.py tests/unit/execution/test_out_of_core_relation_chunked.py -q`. Green.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_relation.py tests/unit/execution/test_out_of_core_relation.py
git commit -m "feat: split parent dedup into single-blocking-operator plans with dual-EXPLAIN test"
```

---

## Task 5: FAIL anti-join precount pinned under the new lifecycle

`total_orphans()` (whole-child anti-join count, FRESH IPC reader over the child-key spill, never a source reread) already exists; pin it against the new lazy-connection, unordered-join lifecycle.

**Files:** Test: `tests/unit/execution/test_out_of_core_stream_join.py`. Implementation changes only if a test exposes a lifecycle break.

**Interfaces:** Consumes `total_orphans()` (Task 2), `run_ordered_join` (Task 3). Produces the guarantee Task 6 relies on: precount-then-join on one edge's connection works back-to-back; FAIL raises before any ordered output exists.

- [ ] **Step 1: Write the tests**

```python
def test_fail_precount_then_join_share_one_edge_connection(tmp_path, orphan_edge_fixture):
    joiner = orphan_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(orphan_edge_fixture.child_batches)
    assert joiner.total_orphans() == orphan_edge_fixture.n_orphans
    batches = list(joiner.run_ordered_join(500, run_bytes_cap=8 * 1024 * 1024))
    assert sum(b.num_rows for b in batches) == orphan_edge_fixture.n_child_rows


def test_precount_reads_the_spill_not_the_source(tmp_path, orphan_edge_fixture):
    joiner = orphan_edge_fixture.make_joiner(tmp_path)
    joiner.stage_keys(orphan_edge_fixture.child_batches)
    orphan_edge_fixture.corrupt_source()
    assert joiner.total_orphans() == orphan_edge_fixture.n_orphans
```

The FULL-route FAIL assertion (raise before any output, `sink_batches_written == 0`) is Task 6 Step 1 (that is where the driver's per-edge FAIL sequencing exists; committing it here would be a red/xfail test, which this plan forbids).

- [ ] **Step 2: Run** — `uv run pytest tests/unit/execution/test_out_of_core_stream_join.py -q -k precount`. Both pass off Tasks 2-3.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "test: pin FAIL anti-join precount under lazy per-edge lifecycle"
```

---

## Task 6: Sequential per-edge driver + M1 budgets + disk ledger in operation

Rewire `stream_table`: phase 1 unchanged; phase 2 becomes a strict per-edge sequence (FAIL precount, then `run_ordered_join`, one edge at a time, each edge's connection closed before the next opens); phase 3 unchanged. Consume `resolve_reorder_budgets` and CALL `require_disk` before every blocking phase, threading a single remaining-disk ledger. This is where round-2 blocker 4 (disk ledger fail-open) is actually closed.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_driver.py`
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py` (add `configure_connection`; delete the ordered `iter_join_rows` shim here)
- Modify: `src/decoy_engine/execution/out_of_core/_relation.py` (add the parent-dedup `require_disk` call)
- Test: `tests/unit/execution/test_out_of_core_runner_streaming.py`

**Interfaces:** Consumes `resolve_reorder_budgets(process_ceiling_bytes, remaining_disk_bytes, *, merge_fan_in=16) -> ReorderBudgets` and `require_disk(budgets, *, mandatory_staging_bytes, estimated_output_bytes)` (both M1), `run_ordered_join` (Task 3), `total_orphans` (Tasks 2/5). `stream_table`'s signature gains `process_ceiling_bytes: int | None` and `remaining_disk_bytes: int | None` (supplied by the route, Task 7).

- [ ] **Step 1: Write the failing tests**

```python
def test_edges_execute_sequentially_never_two_connections(monkeypatch, four_edge_route):
    """No two joiner DuckDB connections live at once across all four edges."""
    from decoy_engine.execution.out_of_core import _stream_join
    live = {"n": 0, "max": 0}
    real_ensure, real_close = _stream_join.StreamFkJoiner._ensure_conn, _stream_join.StreamFkJoiner.close

    def ce(self):
        opened = self._conn is None
        c = real_ensure(self)
        if opened: live["n"] += 1; live["max"] = max(live["max"], live["n"])
        return c
    def cc(self):
        if self._conn is not None: live["n"] -= 1
        real_close(self)
    monkeypatch.setattr(_stream_join.StreamFkJoiner, "_ensure_conn", ce)
    monkeypatch.setattr(_stream_join.StreamFkJoiner, "close", cc)
    four_edge_route.run()
    assert live["max"] == 1


def test_byte_peaks_and_reorder_dirs_reset_between_edges(four_edge_route):
    """Complete four-edge lifecycle: no edge's observed sorter peak_buffered_bytes
    exceeds its run_bytes_cap ceiling, and every reorder dir is torn down (no
    leaked runs) after the run."""
    peaks = four_edge_route.run(collect_edge_sorter_peaks=True)  # [(peak_buffered, run_cap), ...] per edge
    for peak_buffered, run_cap in peaks:
        assert peak_buffered <= run_cap
    for d in four_edge_route.edge_reorder_dirs:
        assert not any(d.glob("*.arrow"))


def test_disk_ledger_fails_closed_when_budget_too_small(four_edge_route):
    """require_disk is actually WIRED: shrink remaining_disk_bytes below one
    edge's staging + 3x output and the route raises budget_too_small BEFORE that
    edge's blocking join, not after an OOM."""
    with pytest.raises(ExecutionError, match="out_of_core_reorder_budget_too_small"):
        four_edge_route.run(remaining_disk_bytes=1 * 1024 * 1024)  # 1 MiB: cannot cover any edge


def test_fail_policy_raises_before_any_output(fail_policy_route_fixture):
    with pytest.raises(ExecutionError, match="orphan_fk"):
        fail_policy_route_fixture.run()
    assert fail_policy_route_fixture.sink_batches_written == 0
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/execution/test_out_of_core_runner_streaming.py -q -k "sequentially or byte_peaks or disk_ledger or fail_policy"`. Expected: FAIL (driver still opens per-edge connections eagerly / streams via the removed public `iter_join_rows` / never calls `require_disk`).

- [ ] **Step 3: Rewire `stream_table` phase 2 with the disk ledger**

```python
        # One budget for the whole route; edges run sequentially so temp is freed
        # between them (the ledger checks each edge's transient headroom, it does
        # not decrement across edges). Persistent staging (child-key spill, payload
        # store, raw-parent IPC) is alive through phase 3, so it is charged to
        # every per-edge require_disk call via mandatory_staging_bytes.
        budgets = resolve_reorder_budgets(process_ceiling_bytes, remaining_disk_bytes)
        duckdb_limit = f"{budgets.duckdb_memory_limit_bytes // (1024 * 1024)}MB"
        temp_dir_size = f"{budgets.remaining_disk_bytes // (1024 * 1024)}MB"
        persistent_staging = _measure_persistent_staging_bytes(...)  # child keys + payload + raw-parent IPC

        cursors: list[JoinRowCursor] = []
        for edge, joiner in zip(incoming_edges, joiners, strict=True):
            est_output = _estimate_edge_output_bytes(edge, joiner)  # staged child rows x mean masked-row width
            require_disk(  # fail-closed BEFORE this edge's blocking join
                budgets,
                mandatory_staging_bytes=persistent_staging + _staged_parent_bytes(edge),
                estimated_output_bytes=est_output,
            )
            joiner.configure_connection(memory_limit=duckdb_limit, max_temp_directory_size=temp_dir_size)
            if edge.orphan_policy is OrphanPolicy.FAIL:
                total = joiner.total_orphans()
                if total:
                    raise orphan_fk_error(edge, total)
            cursors.append(
                JoinRowCursor(
                    joiner.run_ordered_join(
                        batch_rows,
                        run_bytes_cap=budgets.run_bytes_cap,
                        merge_fan_in=budgets.merge_fan_in,
                    ),
                    edge.child_columns,
                )
            )
```

Parent-relation build (Task 4's `_build_relation`) ALSO calls `require_disk(budgets, mandatory_staging_bytes=<staged parent bytes>, estimated_output_bytes=<winners + relation parquet>)` before its blocking aggregate/join, so BOTH blocking phases are ledgered (round-2 blocker 4). Thread `budgets` into `_build_relation` (or pass `remaining_disk_bytes` and let it build its own guard call). Add `StreamFkJoiner.configure_connection(memory_limit, max_temp_directory_size)` (stores the two so `_ensure_conn` uses them) OR pass them into `_open_joiner`; either way the joiner's DuckDB `memory_limit` comes from `budgets.duckdb_memory_limit_bytes`, not the old `resolve_phase_memory_limits` joiner cap. Delete the old `sink_joiner`/`resident_joiner` cap plumbing; keep the build caps (`sink_build_memory_limit`, `resident_build_memory_limit`).

In THIS commit, delete the now-dead ordered `iter_join_rows` shim and its `ORDER BY` from `_stream_join.py` and the older tests that exercised it directly; `grep -rn "iter_join_rows" src/ tests/` must return nothing after this step. This is the single commit where the ordered path disappears (green driver on shim -> green driver on `run_ordered_join`, no red between). Update the driver docstring: phase 2 is "sequential per-edge join + bounded external reorder (Decoy-owned; DuckDB output order is never relied upon); disk ledgered per blocking phase".

- [ ] **Step 4: Run the full local gate**

```bash
uv run pytest tests/unit/execution/test_out_of_core_runner_streaming.py \
  tests/unit/execution/test_out_of_core_stream_join.py \
  tests/unit/execution/test_ooc_external_sort.py tests/unit/execution/test_ooc_reorder_budget.py -q
uv run pytest tests/parity/test_out_of_core_fk_parity.py -q
uv run pytest tests/unit/execution -q -k out_of_core
```

Expected: ALL green, including byte-parity (payload-boundary misalignment, REMAP, overlapping edges, duplicate-parent last-write-wins, accepted nested/struct/map + zero-row divergences).

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_stream_driver.py src/decoy_engine/execution/out_of_core/_stream_join.py src/decoy_engine/execution/out_of_core/_relation.py tests/unit/execution/test_out_of_core_runner_streaming.py tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "feat: sequential per-edge driver with M1 budgets and in-operation disk ledger"
```

---

## Task 7: Route seam: dispatch on (memory, disk, sink); retire the O(parent) preflight floor

Split the preflight by route and implement the recorded ROUTE-SEAM DECISION. Keep `_batch_join.py` as the resident/O(parent) fallback (do NOT delete it). The bounded-stream path is taken ONLY when a memory budget AND a disk budget AND a sink are all present; otherwise resident. Arbitrary-size REQUIRES a sink. Stream-intent with a missing budget FAILS CLOSED, never a silent resident fallback. The route derives `process_ceiling_bytes = RAM / ENVELOPE_FACTOR`.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_memory_estimate.py` (add `enforce_stream_reorder_preflight` + `ENVELOPE_FACTOR`; keep `enforce_ooc_memory_preflight` / `predict_ooc_build_floor_bytes` / `_BUILD_FLOOR_BYTES_PER_ROW` for the resident arm only)
- Modify: `src/decoy_engine/execution/_pipeline_route_exec.py` (dispatch; derive process_ceiling; call the route-matched preflight; stop dividing the resident cap by `incoming_edges + 1` on the stream path; keep `_batch_join` on the resident branch)
- Test: `tests/unit/execution/test_out_of_core_memory_estimate.py`, `tests/unit/execution/test_pipeline_route_exec.py`

**Interfaces:** Produces `enforce_stream_reorder_preflight(*, process_ceiling_bytes: int | None, remaining_disk_bytes: int | None, has_sink: bool) -> None` (raises `out_of_core_reorder_unbudgeted` if no memory budget, `out_of_core_reorder_undisked` if no disk budget, `out_of_core_reorder_unsinked` if `has_sink` is False). Module constant `ENVELOPE_FACTOR = 1.35` (cite the M1 measured-results doc). `enforce_ooc_memory_preflight` retained UNCHANGED for the resident path.

- [ ] **Step 1: Write the failing tests**

In `test_out_of_core_memory_estimate.py`:

```python
def test_stream_preflight_admits_large_parent_when_fully_budgeted():
    # No O(parent) row-count floor: a huge parent with memory+disk+sink is admitted.
    enforce_stream_reorder_preflight(process_ceiling_bytes=6 * 1024**3,
                                     remaining_disk_bytes=500 * 1024**3, has_sink=True)

def test_stream_preflight_fails_closed_without_disk():
    with pytest.raises(ExecutionError, match="out_of_core_reorder_undisked"):
        enforce_stream_reorder_preflight(process_ceiling_bytes=6 * 1024**3,
                                         remaining_disk_bytes=None, has_sink=True)

def test_stream_preflight_fails_closed_without_memory():
    with pytest.raises(ExecutionError, match="out_of_core_reorder_unbudgeted"):
        enforce_stream_reorder_preflight(process_ceiling_bytes=None,
                                         remaining_disk_bytes=500 * 1024**3, has_sink=True)

def test_stream_preflight_fails_closed_without_sink():
    with pytest.raises(ExecutionError, match="out_of_core_reorder_unsinked"):
        enforce_stream_reorder_preflight(process_ceiling_bytes=6 * 1024**3,
                                         remaining_disk_bytes=500 * 1024**3, has_sink=False)

def test_resident_preflight_still_enforces_parent_floor():
    with pytest.raises(ExecutionError):
        enforce_ooc_memory_preflight(parent_rows=10**12, budget_bytes=64 * 1024 * 1024)
```

In `test_pipeline_route_exec.py`, a dispatch matrix over `(memory?, disk?, sink?)` asserting: all-three-present -> stream path chosen, `enforce_stream_reorder_preflight` runs (not the floor), `process_ceiling_bytes` passed to `stream_table` equals `int(configured_ram / 1.35)`, and the resident cap is NOT divided by `incoming_edges + 1`; any-missing under stream intent -> the matching fail-closed error; no-stream-intent (memory-fitting job) -> resident path with `_batch_join` and the floor preflight. Explicitly assert the no-sink path NEVER constructs a `StreamFkJoiner`/sorter (spy the constructor). Reuse the route module's existing selection fixtures; if the decision is not directly unit-observable, assert on the effective values passed into `stream_table`/the joiner.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/execution/test_out_of_core_memory_estimate.py tests/unit/execution/test_pipeline_route_exec.py -q -k "preflight or dispatch"`. Expected: FAIL.

- [ ] **Step 3: Implement `enforce_stream_reorder_preflight`** (leave the resident floor as-is):

```python
ENVELOPE_FACTOR = 1.35  # M1-measured: peak RSS <= 1.35 x the sorter run_bytes_cap ceiling.

def enforce_stream_reorder_preflight(
    *, process_ceiling_bytes: int | None, remaining_disk_bytes: int | None, has_sink: bool,
) -> None:
    """Admission check for the bounded stream-join + external-reorder path.

    NO O(parent)/O(child) row-count floor: parent build and reorder both spill,
    so resident cost is bounded by the memory budget, not row count. What it
    requires is that the bounds are actually set and a sink exists: a missing
    memory budget lets DuckDB default to ~80% of RAM; a missing disk budget lets
    temp grow without limit; without a sink there is nowhere to stream an
    arbitrary-size result. All three fail closed here. The caller routes
    memory-fitting jobs to the resident path; it does NOT silently downgrade a
    stream-intent job whose budget is missing (that surfaces as an error).
    """
    if process_ceiling_bytes is None:
        raise ExecutionError(code="out_of_core_reorder_unbudgeted", message=(
            "the bounded stream-join path requires a process memory ceiling; none configured."))
    if remaining_disk_bytes is None:
        raise ExecutionError(code="out_of_core_reorder_undisked", message=(
            "the bounded stream-join path requires a temp disk budget so DuckDB spill and "
            "sorter runs stay bounded; none configured."))
    if not has_sink:
        raise ExecutionError(code="out_of_core_reorder_unsinked", message=(
            "the bounded stream-join path requires a sink; an arbitrary-size result cannot be "
            "held resident. Provide a sink or route a memory-fitting job to the resident path."))
```

- [ ] **Step 4: Rewire `_pipeline_route_exec.py`**
- Compute `process_ceiling_bytes = int(configured_ram_bytes / ENVELOPE_FACTOR)` where `configured_ram_bytes` is the job's configured memory ceiling (the same budget the old preflight consumed); document that the route never reads physical RAM directly (cgroup-invisible), it uses the configured budget. Compute `remaining_disk_bytes` from the configured disk budget (free spill-filesystem bytes minus a safety margin).
- Dispatch: choose the stream path when `configured_ram_bytes is not None AND disk budget is not None AND a sink exists`; call `enforce_stream_reorder_preflight(process_ceiling_bytes=..., remaining_disk_bytes=..., has_sink=True)`, then drive `stream_table(..., process_ceiling_bytes=..., remaining_disk_bytes=...)`. If stream intent was explicitly requested but a budget/disk/sink is missing, call `enforce_stream_reorder_preflight` with the missing arg so it raises (fail closed). Otherwise fall to the resident/O(parent) path: `enforce_ooc_memory_preflight(...)` (floor) then `_batch_join`.
- Remove the `/(incoming_edges + 1)` division of the resident cap on the stream path (edges are sequential now); keep the resident path's arithmetic untouched.
- Do NOT delete `_batch_join.py`; it is the resident fallback (the "cancel the delete" from the route-seam decision).

- [ ] **Step 5: Run** — `uv run pytest tests/unit/execution/test_out_of_core_memory_estimate.py tests/unit/execution/test_pipeline_route_exec.py -q` then `uv run pytest tests/unit/execution -q -k "out_of_core or route" && uv run pytest tests/parity/test_out_of_core_fk_parity.py -q`. Green, including a large-parent stream case that the floor would previously have rejected.

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_memory_estimate.py src/decoy_engine/execution/_pipeline_route_exec.py tests/unit/execution/test_out_of_core_memory_estimate.py tests/unit/execution/test_pipeline_route_exec.py
git commit -m "feat: route seam dispatches on memory+disk+sink; retire obsolete O(parent) floor for stream path"
```

---

## Task 8: Verification harness: dual-dimension plateaus, four-edge lifecycle, order probe, scale gate, docs

Wire the release-gate measurements: plateau tests in BOTH dimensions with per-sink spill evidence, a multi-run-merge requirement that does NOT rest on the sorter's always-present final run, the no-order diagnostic probe, and the honest-guarantee docs. The 200M@8GB GCP run is the final executable gate.

**Files:**
- Modify: `scripts/ooc_child_key_plateau_probe.py` (add `--grow child|parent`, `--edges N`; separate duckdb/reorder temp accounting; peak file-count sampler)
- Create: `scripts/ooc_join_order_probe.py`
- Modify: `tests/perf/test_out_of_core_memory_sentinel.py`
- Modify: `docs/relationships-memory-scaling.md` (honest-guarantee section)

- [ ] **Step 1: Extend the plateau probe**

Add `--grow {child,parent}` (default `child`) and `--edges N` (default 1). Extend the per-size JSON contract to account for the TWO spill sinks SEPARATELY plus a spill-multiplicity signal: `{"rows": n, "peak_rss_mb": ..., "duckdb_temp_disk_mb": ..., "reorder_temp_disk_mb": ..., "reorder_peak_run_files": k, "parity_ok": true}`. Measure `duckdb_temp_disk_mb` as the high-water of the `.../duckdb` subtree and `reorder_temp_disk_mb` as the high-water of the `.../edge_*/reorder` subtrees, sampled by a monitor thread DURING the run (not at end, since the sorter always leaves exactly one final run). `reorder_peak_run_files` = the max count of `*.arrow` files seen in the reorder subtree at any sample; it must exceed 1 to prove a real multi-run merge (a single final run at end would read as `1` and hide a false plateau). Separate accounting matters because growing the CHILD drives the sorter to spill while growing the PARENT drives the DuckDB hash-join build to spill; a merged number stays nonzero from one sink while the other silently never spills.

- [ ] **Step 2: Write the sentinel gates**

```python
_PLATEAU_ENV = {**os.environ, "ARROW_DEFAULT_MEMORY_POOL": "system", "MALLOC_ARENA_MAX": "2"}
_MAX_SLOPE_MB_PER_M = 20.0   # MB per +1M rows; pre-fix O(child) slopes were 41-126 MB/1M.

def _plateau(grow, sizes, edges=1):
    out = subprocess.run([sys.executable, _PLATEAU_PROBE, "--grow", grow, "--edges", str(edges),
                          "--sizes", ",".join(map(str, sizes)), "--json"],
                         env=_PLATEAU_ENV, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)

def _assert_bounded(records, *, must_spill):
    assert all(r["parity_ok"] for r in records)
    assert all(r[must_spill] > 0 for r in records), f"no spill in {must_spill}"
    if must_spill == "reorder_temp_disk_mb":
        assert all(r["reorder_peak_run_files"] > 1 for r in records), "no multi-run merge occurred"
    def slope(a, b): return (b["peak_rss_mb"] - a["peak_rss_mb"]) / ((b["rows"] - a["rows"]) / 1e6)
    for a, b in zip(records, records[1:]):
        assert slope(a, b) <= _MAX_SLOPE_MB_PER_M, f"segment slope {slope(a,b):.1f} exceeds bound"
    assert slope(records[0], records[-1]) <= _MAX_SLOPE_MB_PER_M, "overall slope exceeds bound"

@pytest.mark.perf
def test_plateau_fixed_parent_growing_child():
    _assert_bounded(_plateau("child", [1_000_000, 3_000_000, 5_000_000]),
                    must_spill="reorder_temp_disk_mb")

@pytest.mark.perf
def test_plateau_fixed_child_growing_parent():
    _assert_bounded(_plateau("parent", [1_000_000, 3_000_000, 5_000_000]),
                    must_spill="duckdb_temp_disk_mb")

@pytest.mark.perf
def test_plateau_high_fan_in_four_edges():
    _assert_bounded(_plateau("child", [1_000_000, 3_000_000, 5_000_000], edges=4),
                    must_spill="reorder_temp_disk_mb")
```

- [ ] **Step 3: Write the order probe** — `scripts/ooc_join_order_probe.py`: run the UNORDERED join across a matrix (`threads` 1 and N, `memory_limit` large/tiny, skewed keys, null keys, orphans, composite keys, `batch_rows` in {97, 1024, 65_536}); record to stdout JSON whether the observed output happened to be row_nr-ordered and the first divergence offset. Because production pins `threads=1` inside `_ensure_conn`, the probe opens its OWN `connect_duckdb(..., threads=<matrix>)`, calls `_require_parent_build(conn)`, registers the same child reader + parent view, and runs the SAME unordered SELECT (import the SQL builder from `_stream_join`, or duplicate the one SELECT with a comment pointing at the source of truth). Diagnostic only; RECORDS order, never asserts it. Add a fast smoke test that it runs and emits valid JSON on one tiny config.

- [ ] **Step 4: Documentation** — add the honest-guarantee paragraph (verbatim from the header) to `docs/relationships-memory-scaling.md` under "Final OOC-B memory guarantee", with the operating conditions: `process_ceiling = RAM / 1.35`; memory_limit at 0.55 of that ceiling; one DuckDB blocking query at a time; threads=1; explicit sorter + disk budgets via `require_disk`; `max_temp_directory_size` plus Decoy's mid-operation disk ledger; transactional abort on DuckDB OOM or disk exhaustion. State the path is certified on DuckDB 1.5.4 and refuses (fail-closed, `out_of_core_duckdb_join_unsupported`) below 1.2.0 or without `build_side_probe_side`/`join_order`. Confirm `uv.lock` pins duckdb 1.5.4. Do NOT delete `_batch_join.py` (it is the resident fallback the route still selects).

- [ ] **Step 5: Run everything local**

```bash
uv run pytest tests/unit/execution -q -k "out_of_core or external_sort or reorder"
uv run pytest tests/parity/test_out_of_core_fk_parity.py -q
uv run pytest tests/perf/test_out_of_core_memory_sentinel.py -q -m "not benchmark"
ARROW_DEFAULT_MEMORY_POOL=system MALLOC_ARENA_MAX=2 uv run pytest tests/perf/test_out_of_core_memory_sentinel.py -q -m perf -k plateau
uv run ruff format --check . && uv run ruff check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/ooc_child_key_plateau_probe.py scripts/ooc_join_order_probe.py tests/perf/test_out_of_core_memory_sentinel.py docs/relationships-memory-scaling.md
git commit -m "test: dual-dimension plateau + four-edge fan-in gates, order probe, memory-guarantee docs"
```

---

## Verification Gates (release requirements)

All required before merge/ship; a lower-scale pass is NOT sufficient after the earlier false plateau. Run the executable gates in THIS order (cheap/local first, scale last, review last).

1. **Sorter order-independence (M1, done):** `ExternalRowNrSorter` shuffled-input unit tests pass. (M1)
2. **DuckDB pin certified:** locked 1.5.4; runtime fail-closed below 1.2.0 or when `build_side_probe_side`/`join_order` is absent; never a silent no-op. Structural test green. (Task 2)
3. **Contiguity guard:** `run_ordered_join` raises `out_of_core_fk_reorder_contiguity` on any gap/dup/short stream; the sorter stays FK-agnostic. (Task 3)
4. **No-order join probed** across threads=1/N, spill/no-spill, skew, nulls, orphans, composite keys, varying Arrow batch sizes; order RECORDED, never relied upon. (Task 8)
5. **Full pandas byte-parity** green, including payload-boundary misalignment, REMAP, overlapping edges, duplicate-parent last-write-wins, and the accepted nested/struct/map + zero-row divergences per `SEMANTIC_DIFFERENCES.md`. (Tasks 3, 6)
6. **Disk ledger wired:** `require_disk` runs before every blocking phase (parent dedup + each edge join); an undersized disk budget fails closed BEFORE the join. (Tasks 6, 7)
7. **Plateau in BOTH dimensions plus four-edge fan-in:** nonzero temp-spill in the sink each dimension stresses (reorder for child-growth with `reorder_peak_run_files > 1`, DuckDB for parent-growth), and bounded RSS slope on BOTH every adjacent segment AND the overall start-to-end slope at 3+ sizes. (Task 8)
8. **SCALE PROOF (final quantitative gate, run BEFORE human/model review):** 200M rows at an 8GB cap on GCP, BOTH stressed shapes, allocator env pinned (`ARROW_DEFAULT_MEMORY_POOL=system`, `MALLOC_ARENA_MAX=2` exported before process start). The route sets `process_ceiling_bytes = int(8 * 1024**3 / 1.35)` from the 8GB container ceiling. Exact invocations, via the decoy-platform `scripts/gcp-bench` harness (count each against the 50-run budget; deliver results to Slack):
   - **(a) large CHILD:** 200M child rows against a moderate parent (~1-5M distinct keys). Drives the external SORTER. Pass: completes; peak RSS <= 8 GB (i.e. <= `process_ceiling x 1.35`); `reorder_temp_disk_mb` nonzero with a real multi-run merge; full byte-parity vs the pandas oracle on a sampled slice; run stays within `max_temp_directory_size`.
   - **(b) large PARENT:** 200M distinct parent keys against a moderate child (~1-5M rows). Drives the DuckDB hash-join BUILD. Same pass criteria with `duckdb_temp_disk_mb` nonzero.
   - Byte-parity method: run the SAME inputs through the resident `_join.py::mask_child_fk` oracle on a bounded sample (or a smaller full run) and assert byte-identical masked output for the sampled rows; the accepted-divergence contract still applies.
   - If either shape OOMs or breaks parity, PARK and reconsult; do not iterate blind (third strike already fired once).
9. **Golden gate:** `scripts/test_flight.py` green; zero reproducible P0/P1.
10. **FINAL REVIEW (gates the merge, after 1-9 green):** dennis (Opus) adversarial review, then the Codex cross-model pre-merge gate, seeing the scale evidence and final code together.

## Self-Review

Checked against the M1 measured signatures, the recorded route-seam decision, and each REMAINING Codex round-2 finding (the M1-handled ones - byte-cap accounting, budget fraction math, disk-ledger primitive - are built and measured; this milestone WIRES them):

1. **Spec coverage.** M1 consumed by real signature, not rebuilt: sorter in Task 3, budget + `require_disk` in Tasks 4/6/7. Contiguity guard relocated to the FK consumer (M1 sorter is FK-agnostic): Task 3. Route seam dispatch on (memory, disk, sink), `_batch_join` kept, fail-closed on stream-intent-missing-budget, no-sink never builds the sorter: Task 7. Disk ledger in OPERATION (require_disk before every blocking phase, staging + 3x output, single ledger): Tasks 4/6/7. Join guard requires BOTH optimizers, version pin as a route gate, JSON plan test on operator TYPES (ARROW_SCAN probe / PARQUET_SCAN build, no ORDER_BY, not the `parent_keys` alias): Task 2. Parent-dedup dual-EXPLAIN test (aggregate-without-join, join-without-aggregate, separate physical plans): Task 4. Green commits + shared fixtures (incl. complete four-edge route) defined up front, ordered shim kept until Task 6, no red/xfail: Tasks 1-6. Teardown (conn closed before finish, close ownership, run cleanup, idempotent close): Task 3 + Task 6. Four-edge byte-lifetime (peaks within cap, reorder dirs torn down) + plateau with per-sink spill and multi-run-merge evidence (not the always-present final run) bounding every segment AND the overall slope: Tasks 6/8. Scale gate: exact large-child + large-parent invocations, byte-parity method, numeric RSS criterion from the 1.35 envelope, 200M last executable gate then review: Task 8 + gates. Manifests: every task lists exact files, explicit `git add` paths, no `-A`. No gaps found.
2. **Signature fidelity.** `ExternalRowNrSorter(spill_dir, run_bytes_cap, merge_fan_in)` and `iter_ordered()` with NO `expected_rows` (Task 3 wraps it for contiguity). `resolve_reorder_budgets(process_ceiling_bytes, remaining_disk_bytes, *, merge_fan_in=16)` returning `duckdb_memory_limit_bytes` (BYTES) + carrying `remaining_disk_bytes`; driver formats `"<n>MB"` (Task 6). `require_disk(budgets, *, mandatory_staging_bytes, estimated_output_bytes)` called per blocking phase. Error codes match M1 exactly (`out_of_core_sort_row_too_wide`, `out_of_core_sort_invalid_state`, `out_of_core_reorder_unbudgeted`, `out_of_core_reorder_budget_too_small`) plus the new FK-layer `out_of_core_fk_reorder_contiguity` (Task 3) and route codes `out_of_core_reorder_undisked` / `out_of_core_reorder_unsinked` (Task 7).
3. **No weakened invariant.** Byte-parity, `JoinRowCursor`/`resolve_batch`/REMAP, `SEMANTIC_DIFFERENCES` divergences, `_join.py::mask_child_fk` all untouched. `_batch_join.py` is retained by decision.
4. **For a re-reviewer to double-check.** (a) The disk ledger's non-decrementing model: I charge persistent staging to every per-edge `require_disk` and rely on sequential per-edge teardown to free transient temp, so the ledger is a per-phase admission check, not a running counter; this holds because `close()` tears each edge's reorder dir down before the next opens (asserted in Task 6). Confirm this matches the intended semantics. (b) The `explain_join()` JSON shape on DuckDB 1.5.4: the test assumes the root HASH_JOIN's `children[0]` is the probe (ARROW_SCAN) and `children[1]` the build (PARQUET_SCAN); verify against a live 1.5.4 plan during Task 2 and swap the index if DuckDB orders build-first. (c) `process_ceiling = RAM / 1.35` is deliberately conservative (the 0.30 budget reserve already absorbs overhead), so it double-counts the envelope; confirm this is acceptable margin rather than wasted headroom at 8GB.
