# OOC-B Milestone 1: external row_nr sorter + reorder budget ledger

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this task-by-task. Steps use `- [ ]` checkboxes.

**Goal:** Build and *measure* the two hardest, riskiest primitives of the OOC-B external-reorder
architecture in isolation, before wiring them into the join path. These are (1) a Decoy-owned external
`row_nr` sorter whose *resident* memory is provably byte-capped regardless of row width, and (2) a
reorder budget ledger that fails closed and derives every allocation from a single process ceiling. This
milestone exists because two Codex plan-review rounds showed these two pieces cannot be pinned down on
paper - they need real code and real memory measurement.

**Architecture (why this shape):** Within one FK edge the phases run *sequentially*, so the co-resident
peak is not a sum of every phase. The lifecycle is: **join+buffer** (DuckDB streams the unordered join
out; the sorter buffers batches until it flushes a run) - here DuckDB and the sort buffer ARE
co-resident; then **merge** (DuckDB connection closed; only the sorter's merge heads are resident); then
**drain** (DuckDB closed; one reader + one output batch). Therefore one process memory ceiling `M` is
split into fractions that must fit *together during the join+buffer phase*, plus a reserve for Arrow
batches in flight, Python, allocator overhead, and DuckDB overshooting its own `memory_limit`. The "2x"
that a k-way merge costs is a *disk* cost (old runs plus the new run coexist on disk), not a memory cost.

**Tech stack:** Python 3.13, pyarrow (Arrow IPC for spilled runs), DuckDB 1.5.4 (only referenced by the
budget model, not exercised in M1), pytest.

## Global constraints (copied verbatim; apply to every task)
- DuckDB / pyarrow PUBLIC API only. No internals, no monkeypatching library internals, no extensions.
- No em-dashes in prose or comments.
- Every commit is green (failing test -> implement -> passing test -> commit). No committed red or xfail.
- Fail closed: any missing/undersized budget raises a typed error; never silently proceed unbounded.
- Do not modify `_join.py::mask_child_fk`, `tests/parity/*`, or `SEMANTIC_DIFFERENCES.md` in this milestone.
- New modules live in `src/decoy_engine/execution/out_of_core/`. Build on a fresh branch off `main`
  (`feat/ooc-b-external-reorder`). M1 is standalone: it does not import `_stream_join.py`,
  `_stream_driver.py`, `_batch_join.py`, or `_runner.py`.

---

## Route-seam design decision (RECORD ONLY here; implemented in Milestone 2, not M1)

This resolves Codex round-2 blocker 3 (the fallback path did not exist). Decision, so M2 builds to it:

1. **`_batch_join.py` (the O(parent) resident path) is KEPT as the fallback. It is NOT deleted.** The
   earlier plan's "delete `_batch_join.py`" step is cancelled.
2. The runner dispatches on `(memory_budget, disk_budget, sink)`:
   - **bounded-stream path** (the new external-reorder path) is used only when a memory budget AND a
     disk budget AND a sink are ALL present. This is the only path that carries the arbitrary-size
     guarantee.
   - **resident path** (`_batch_join.py`, O(parent)) is used otherwise. It keeps its existing contract
     (bounded by the parent mapping fitting memory).
3. **The arbitrary-size guarantee REQUIRES a sink.** A no-sink request (caller wants an in-memory
   result) can never be arbitrary-size; it uses the resident path and keeps the resident payload store.
   The bounded-stream path must never be entered without a sink.
4. **Stream intent with a missing budget FAILS CLOSED.** If a sink is present (large-job intent) but a
   memory or disk budget is absent, raise `out_of_core_reorder_unbudgeted` with an actionable message
   asking the operator to set the budget. Do NOT silently fall back to the resident path, because a
   large job on the resident path is exactly the OOM this architecture exists to remove.

M1 delivers the budget model that this dispatch will call (`resolve_reorder_budgets`) and the sorter it
will drive; the dispatch wiring itself is M2.

---

## File structure

- Create `src/decoy_engine/execution/out_of_core/_reorder_budget.py` - `ReorderBudgets` dataclass +
  `resolve_reorder_budgets(...)`, the single source of every allocation and the fraction/disk invariants.
- Create `src/decoy_engine/execution/out_of_core/_external_sort.py` - `ExternalRowNrSorter`.
- Create `src/decoy_engine/execution/out_of_core/_errors.py` additions OR reuse the existing errors
  module for the three typed errors (check which module the OOC package already uses for typed errors and
  put them there; name them exactly `out_of_core_reorder_unbudgeted`, `out_of_core_reorder_budget_too_small`,
  `out_of_core_sort_row_too_wide`).
- Test: `tests/unit/execution/test_ooc_reorder_budget.py`
- Test: `tests/unit/execution/test_ooc_external_sort.py`
- Test (measured, perf-marked): `tests/perf/test_ooc_external_sort_memory.py`

---

## Task 1: reorder budget ledger

**Files:** Create `_reorder_budget.py`; Test `tests/unit/execution/test_ooc_reorder_budget.py`.

**Interfaces produced (M2 consumes these):**
```python
@dataclass(frozen=True)
class ReorderBudgets:
    process_ceiling_bytes: int      # M, the one ceiling everything derives from
    duckdb_memory_limit_bytes: int  # f_duckdb * M  (the string form is what DuckDB SET memory_limit uses)
    run_bytes_cap: int              # f_sort * M    (sorter in-memory buffer + per-merge resident cap)
    merge_fan_in: int               # k for the k-way merge
    remaining_disk_bytes: int       # disk ledger ceiling for this edge

def resolve_reorder_budgets(
    process_ceiling_bytes: int | None,
    remaining_disk_bytes: int | None,
    *,
    merge_fan_in: int = 16,
) -> ReorderBudgets: ...

def require_disk(budgets: ReorderBudgets, estimated_output_bytes: int) -> None:
    # raises out_of_core_reorder_budget_too_small if the disk ledger cannot cover
    # mandatory staging + duckdb temp + sorter runs + 2x merge amplification.
```

**Design constants (encode as module-level, documented):**
- `F_DUCKDB = 0.55`, `F_SORT = 0.15`. Invariant enforced in code: `F_DUCKDB + F_SORT <= 0.70`, leaving a
  >= 0.30 reserve for Arrow batches in flight, phase-3 drain residency (reader head + cursor concat +
  payload batch + resolved output are all O(batch), comfortably inside the reserve), Python, allocator
  slop, and DuckDB overshooting its own `memory_limit` (documented behavior; cite the DuckDB OOM guide in
  the docstring). Assert `F_DUCKDB + F_SORT <= 0.70` at import so a future edit that breaks it fails fast.
- Minimum viable ceiling: `run_bytes_cap` must be at least `MIN_RUN_BYTES = 8 * 1024 * 1024` (8 MiB) for
  sorting to be feasible. If `F_SORT * M < MIN_RUN_BYTES`, raise `out_of_core_reorder_budget_too_small`.
- Disk ledger (in `require_disk`): required disk =
  `mandatory_staging (caller passes, = child-keys + payload + parent-stage bytes)` +
  `duckdb_temp (<= estimated_output_bytes, the join can spill up to its output)` +
  `sorter_runs (~= estimated_output_bytes)` + `merge_amplification (= estimated_output_bytes, the extra
  copy while old runs and the merged run coexist)`. If that sum > `remaining_disk_bytes`, raise
  `out_of_core_reorder_budget_too_small`. (Keep `mandatory_staging` and `estimated_output_bytes` as
  explicit args to `require_disk` so the caller supplies real measured sizes.)

- [ ] **Step 1 - failing tests.** Write `test_ooc_reorder_budget.py` with:
  - `test_none_memory_budget_fails_closed`: `resolve_reorder_budgets(None, 10**12)` raises
    `out_of_core_reorder_unbudgeted`.
  - `test_none_disk_budget_fails_closed`: `resolve_reorder_budgets(10**10, None)` raises
    `out_of_core_reorder_unbudgeted`.
  - `test_fraction_invariant_holds`: for a valid ceiling, `duckdb_memory_limit_bytes + run_bytes_cap
    <= 0.70 * process_ceiling_bytes` exactly, and `duckdb == round(0.55*M)`, `run == round(0.15*M)`.
  - `test_undersized_ceiling_rejected`: a ceiling so small that `0.15*M < 8 MiB` raises
    `out_of_core_reorder_budget_too_small`.
  - `test_disk_ledger_rejects_when_insufficient`: `require_disk` with `remaining_disk_bytes` less than
    `staging + 3*estimated_output_bytes` raises `out_of_core_reorder_budget_too_small`; with enough, it
    returns None.
  - `test_disk_ledger_accounts_two_x_merge`: construct a case where disk covers `staging + 2*output` but
    NOT `staging + 3*output`, assert it is rejected (proves the merge-amplification term is present).
  - Run: `pytest tests/unit/execution/test_ooc_reorder_budget.py -v` -> FAIL (module missing).
- [ ] **Step 2 - implement `_reorder_budget.py`** to pass exactly those tests. Put the fraction and disk
  formulas in code (not comments); the docstring cites the DuckDB OOM guidance for the reserve.
- [ ] **Step 3 - run tests -> PASS.**
- [ ] **Step 4 - commit.** `git add src/decoy_engine/execution/out_of_core/_reorder_budget.py
  tests/unit/execution/test_ooc_reorder_budget.py <errors-module-if-touched>` then commit.

---

## Task 2: `ExternalRowNrSorter` (byte-capped resident memory)

**Files:** Create `_external_sort.py`; Test `tests/unit/execution/test_ooc_external_sort.py`.

**Interface produced:**
```python
class ExternalRowNrSorter:
    def __init__(self, spill_dir: Path, run_bytes_cap: int, merge_fan_in: int,
                 row_nr_column: str = "__decoy_row_nr") -> None: ...
    def write(self, batch: pa.RecordBatch) -> None: ...   # copies + byte-slices; flushes runs
    def finish(self) -> None: ...                          # k-way merge runs into ONE ordered run
    def iter_ordered(self) -> Iterator[pa.RecordBatch]: ... # one reader over the final run
    def close(self) -> None: ...                           # remove all run files
    @property
    def peak_buffered_bytes(self) -> int: ...              # instrumentation for tests
```

**Correctness + memory contract (each bullet answers a Codex round-2 blocker-1/2 point):**
- `write(batch)`: iterate the batch in row slices; for each slice, use `slice(...).combine_chunks()` on
  the SLICE only and copy it so the retained buffer does not pin the whole incoming batch (zero-copy
  slices would pin the parent). Accumulate copied slices; BEFORE appending a slice that would push the
  buffered byte total over `run_bytes_cap`, FLUSH the current buffer to a sorted run first, THEN append.
  This guarantees the buffer never exceeds `run_bytes_cap` (not `2x`).
- A single row whose own byte size exceeds `run_bytes_cap` raises `out_of_core_sort_row_too_wide`
  (measured on the one-row slice's `nbytes`, so isolated wide rows cannot slip past an average).
- Flush = sort the buffered rows by `row_nr` (`pa.Table.from_batches(...).sort_by(row_nr)`, then write to
  Arrow IPC), then release the buffer. Track `peak_buffered_bytes` as the max buffered byte total ever
  held (including the transient sort copy: a sort materializes an indices array + a taken table, so the
  peak during flush is buffer + sort overhead; measure and expose the true peak).
- `finish()`: k-way merge the sorted runs with fan-in `merge_fan_in`. Each open run contributes at most
  one *head batch*, and each head batch is read in chunks capped at `run_bytes_cap // merge_fan_in`, so
  the total resident across a fan-in-way merge is <= `run_bytes_cap`. Emit merged output in bounded
  batches; do NOT `combine_chunks()` the whole merged result. If there are more runs than `merge_fan_in`,
  do multiple merge passes (each pass fan-in-bounded); the final result is ONE ordered run on disk.
  The final run FILE on disk may be large (that is disk, allowed); the RESIDENT bytes stay <= `run_bytes_cap`.
- `iter_ordered()`: open ONE Arrow IPC reader over the final run; yield its batches. Resident = one head
  batch. Assumes `finish()` was called.
- `close()`: delete every run file created; idempotent.

- [ ] **Step 1 - failing correctness tests.** In `test_ooc_external_sort.py`:
  - `test_shuffled_input_is_sorted_by_row_nr`: feed several batches whose combined `row_nr` values are a
    shuffled `range(N)`; after finish, `iter_ordered` yields rows with `row_nr == range(N)` exactly.
  - `test_row_wider_than_cap_fails_closed`: a batch with one very wide binary cell and a tiny
    `run_bytes_cap` raises `out_of_core_sort_row_too_wide` on `write`.
  - `test_close_removes_run_files`: after `close()`, the spill dir has no run files.
  - Run -> FAIL (module missing).
- [ ] **Step 2 - implement `_external_sort.py`** to pass Step 1.
- [ ] **Step 3 - run -> PASS. Commit.**
- [ ] **Step 4 - failing byte-cap tests** (the core proof; add to the same file):
  - `test_buffer_never_exceeds_cap_wide_variable_rows`: feed many batches of highly variable-width rows
    (mix of tiny and large binary cells) sized so total >> `run_bytes_cap`; assert
    `sorter.peak_buffered_bytes <= run_bytes_cap * SORT_OVERHEAD_FACTOR` where `SORT_OVERHEAD_FACTOR` is a
    small documented constant (e.g. 2.2) accounting for the transient sort indices+take copy, and DOCUMENT
    why (the flush sort materializes one extra copy of the buffer). The assertion must hold with the buffer
    itself never over `run_bytes_cap`; expose both "pre-sort buffer peak" and "with-sort peak" if that
    makes the bound clean.
  - `test_merge_resident_within_cap`: force many runs (tiny `run_bytes_cap`, large N) so `finish()` does a
    real multi-run, multi-pass merge; assert the peak resident during merge (instrument the merge head
    buffers) stays `<= run_bytes_cap`.
  - `test_emitted_batches_bounded`: assert every batch yielded by `iter_ordered` has
    `nbytes <= run_bytes_cap`.
  - Run -> FAIL.
- [ ] **Step 5 - implement the byte-slicing, flush-before-cap, capped merge heads, and instrumentation**
  to pass Step 4. Run -> PASS. Commit.

---

## Task 3: measured (real-RSS) proof

**Files:** Test `tests/perf/test_ooc_external_sort_memory.py` (mark `@pytest.mark.perf`).

This is the empirical proof that paper accounting cannot give. It runs the sorter in a FRESH subprocess
(allocator env pinned) and reads the OS high-water mark.

- [ ] **Step 1 - failing test.** A perf test that, in a fresh subprocess with
  `ARROW_DEFAULT_MEMORY_POOL=system` and `MALLOC_ARENA_MAX=2`:
  - builds `run_bytes_cap` from `resolve_reorder_budgets(process_ceiling, big_disk)` for a chosen small
    ceiling (e.g. `M = 512 MiB`);
  - streams a large shuffled dataset (e.g. 20 million rows across many variable-width batches, total on
    the order of several GiB so it FAR exceeds `M`) through `write`, then `finish`, then drains
    `iter_ordered`;
  - asserts the subprocess VmHWM stays `<= process_ceiling * ENVELOPE_FACTOR` (document `ENVELOPE_FACTOR`,
    e.g. 1.3, as the tested process envelope, NOT a raw `memory_limit` promise);
  - asserts the output is fully sorted (spot-check first/last/contiguity of `row_nr`);
  - asserts real spill happened (run files were created on disk).
  Run -> FAIL if the sorter is not actually bounded.
- [ ] **Step 2** - the test should PASS once Task 2 is correct. If it does not, the byte accounting in
  Task 2 is wrong: fix Task 2 (do NOT loosen `ENVELOPE_FACTOR` to force a pass). Commit when green.
- [ ] **Step 3 - record the measured numbers** (peak RSS vs ceiling, run count, wall time) in a short
  `## Milestone 1 measured results` block appended to THIS plan file, and commit. These real numbers feed
  the Milestone 2 revision (the join/driver/preflight wiring) so its budget accounting rests on measured
  behavior instead of paper arithmetic.

---

## Verification for Milestone 1 (all must hold before M2)
- `ruff check` / `ruff format --check` clean; `mypy` clean on the new modules.
- `pytest tests/unit/execution/test_ooc_reorder_budget.py tests/unit/execution/test_ooc_external_sort.py -q`
  green.
- `pytest tests/perf/test_ooc_external_sort_memory.py -q` green (the measured proof), with the numbers
  recorded in this file.
- The three typed errors exist and fire on: no memory budget, no disk budget, undersized ceiling,
  insufficient disk, and a single over-cap row.

## Self-review checklist (run before handing back)
- [ ] Budget model derives EVERY allocation from the single `process_ceiling_bytes`; the fraction
  invariant is enforced in code and asserted at import.
- [ ] `resolve_reorder_budgets(None, ...)` and `(..., None)` both raise; undersized ceiling and
  insufficient disk raise; the disk ledger includes the 2x merge-amplification term (proven by a test).
- [ ] Sorter resident (buffer AND merge heads) is byte-capped and PROVEN by an asserted peak, including a
  wide-variable-row case and an isolated over-cap-row rejection; no unbounded `combine_chunks`.
- [ ] The measured subprocess test proves real RSS stays within the tested envelope while data far
  exceeds the ceiling, with real spill.
- [ ] The route-seam decision above is recorded for M2; `_batch_join.py` is NOT touched in M1.
