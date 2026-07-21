# OOC-B Single-Source-Read Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild OOC-B so the child source is read exactly once, eliminating the two-read silent-corruption BLOCKER and the batch-boundary typing HIGH that the Codex final gate found.

**Architecture:** Fuse FK-key staging and non-FK masking into ONE source pass; capture the masked payload (keyed by contiguous `__decoy_row_nr`) into a payload store (in-memory list for the resident path, lossless Arrow-IPC spill for the sink path); resolve FK columns in a later pass from the store, at the same source-chunk granularity `main` used. The FK join stays one spillable DuckDB `LEFT JOIN` per edge.

**Tech Stack:** Python, pyarrow (Arrow IPC record-batch stream for the lossless spill), DuckDB (join + spill + sort, unchanged), existing engine mask/FK-key kernels.

## Global Constraints

- Byte-parity to `tests/parity/test_out_of_core_fk_parity.py` (pandas oracle, 62 cases) is the hard merge gate. No parity weakening.
- Reuse the existing resolution kernels verbatim (`_append_output_batch`, `_cast_chunks`, `_resolve_output_types`, `mask_column`, `fk_key_value`); only relocate WHERE they run.
- DuckDB owns every heavy relational op (join, spill, sort). Do not roll your own memory management. Cite established methodology in new module docstrings (engine `CLAUDE.md` rule).
- No em-dashes in comments/docs. Comments explain WHY, not what. No references to task/PR/author.
- `.venv` at `/home/cam/vscode/decoy-engine/.venv/bin`. Run `ruff format --check` + `ruff check` on changed modules before finishing.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Orchestration modules cap ~600 LOC; keep `_stream_driver.py` under it.
- Design spec: `docs/superpowers/specs/2026-07-21-ooc-b-single-read-redesign.md`.

---

### Task 1: Payload store

**Files:**
- Create: `src/decoy_engine/execution/out_of_core/_payload_store.py`
- Test: `tests/unit/execution/test_out_of_core_payload_store.py`

**Interfaces:**
- Consumes: nothing (leaf module). `pa.RecordBatch`.
- Produces:
  - `class PayloadStore(Protocol)` with `append(self, batch: pa.RecordBatch) -> None`, `iter_batches(self) -> Iterator[tuple[int, pa.RecordBatch]]` (yields `(row_nr_start, batch)`, `row_nr_start` = running sum of prior batch row counts), `close(self) -> None`.
  - `class ResidentPayloadStore` (in-memory list of batches).
  - `class SpillPayloadStore(path: Path)` (Arrow IPC record-batch stream file; write on `append`, sequential read on `iter_batches`; one batch resident at a time; lossless type preservation).

**Why in-band row_nr is NOT stored:** batches are appended in source-read order and row numbering is contiguous from 0, so `row_nr_start` for the k-th batch equals the running sum of prior batch row counts. Each store computes it on read; nothing is added to the batch.

- [ ] **Step 1: Write the failing test**

```python
import pyarrow as pa
import pytest
from decoy_engine.execution.out_of_core._payload_store import (
    ResidentPayloadStore, SpillPayloadStore,
)

def _batches():
    return [
        pa.record_batch({"a": pa.array([1, 2], pa.int64()), "b": pa.array(["x", "y"])}),
        pa.record_batch({"a": pa.array([3], pa.int64()), "b": pa.array(["z"])}),
    ]

@pytest.mark.parametrize("factory", ["resident", "spill"])
def test_store_roundtrip_lossless_with_offsets(tmp_path, factory):
    store = ResidentPayloadStore() if factory == "resident" else SpillPayloadStore(tmp_path / "p.arrow")
    for b in _batches():
        store.append(b)
    got = list(store.iter_batches())
    store.close()
    assert [off for off, _ in got] == [0, 2]                 # running row_nr offsets
    assert got[0][1].schema == _batches()[0].schema          # types preserved exactly
    assert got[0][1].column("a").to_pylist() == [1, 2]
    assert got[1][1].column("b").to_pylist() == ["z"]

@pytest.mark.parametrize("factory", ["resident", "spill"])
def test_empty_store_yields_nothing(tmp_path, factory):
    store = ResidentPayloadStore() if factory == "resident" else SpillPayloadStore(tmp_path / "e.arrow")
    assert list(store.iter_batches()) == []
    store.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_payload_store.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement `_payload_store.py`**

Module docstring cites the staging-spill pattern and Arrow IPC as the lossless columnar stream (established methodology, not bespoke). `SpillPayloadStore.append` lazily opens `pa.ipc.new_stream(path, batch.schema)` on the first non-empty batch (schema known then) and writes each batch; `iter_batches` opens `pa.ipc.open_stream(path)` and yields `(running_offset, batch)`, skipping zero-row batches on write. `ResidentPayloadStore` keeps a `list[pa.RecordBatch]`. Both skip empty batches so offsets stay exact. `close` finalizes the IPC writer / releases the list.

- [ ] **Step 4: Run to verify it passes**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_payload_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_payload_store.py tests/unit/execution/test_out_of_core_payload_store.py
git commit -m "feat(ooc-b): payload store (resident list + lossless Arrow-IPC spill)"
```

---

### Task 2: Split resolution out of `StreamFkJoiner`

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py` (add cases; keep existing joiner coverage green by porting it to the new methods)

**Interfaces:**
- Consumes: `child_keys` TEMP TABLE (staged as today), `parent_keys` VIEW.
- Produces on `StreamFkJoiner`:
  - `iter_join_rows(self, batch_rows: int) -> Iterator[pa.RecordBatch]` — the ordered `LEFT JOIN … ORDER BY __decoy_row_nr` read back via `to_arrow_reader(batch_rows)`, yielding RAW join columns: `__decoy_row_nr`, `__decoy_fk_join_key`, `__decoy_src_i…`, `__decoy_parent_match`, `__decoy_parent_masked_i…`. No resolution, no casting here.
  - `resolve_batch(self, join_rows: pa.RecordBatch) -> tuple[pa.Array, ...]` — the relocated resolution: compute REMAP mint from this batch's `__decoy_src_i` (re-based row_nr 0..n), call `_append_output_batch`, accumulate `observed_types`, `_cast_chunks` to `output_types`, add to `orphan_total`. Returns one resolved FK array per child column.
- `output_types`, `observed_types`, `orphan_total`, staging API: unchanged.
- REMOVE `iter_output`.

**Note:** the SELECT/query and REMAP helpers (`_batch_remap_values`, `_with_positional_row_nr`) already exist; `iter_join_rows` is the current `iter_output` query WITHOUT the per-batch resolution loop, and `resolve_batch` is that loop extracted. Behavior per batch is identical; only the call site moves.

- [ ] **Step 1: Write the failing test**

```python
# resolve_batch must reproduce main's homogeneous-boundary typing when given a
# batch that mixes matched-bool with orphan-int (the Codex HIGH shape), because
# the driver will feed it source-chunk-sized batches. Here we assert the kernel
# itself resolves a mixed join-row batch to int64 [0,0,1,1] rather than failing.
def test_resolve_batch_mixed_bool_int_preserve(bool_preserve_joiner_with_join_rows):
    joiner, join_rows = bool_preserve_joiner_with_join_rows   # fixture: 4 rows, 2 matched bool + 2 orphan
    (fk_array,) = joiner.resolve_batch(join_rows)
    assert fk_array.type == pa.int64()
    assert fk_array.to_pylist() == [0, 0, 1, 1]
```

Build the fixture from the existing stream-join test scaffolding (parent bool `[False,False]`, child src `[False,False,True,True]`, PRESERVE). If the resolver still raises here, Task 4's driver change alone will not fix the HIGH; this test pins the resolver contract. If `resolve_batch` on a genuinely mixed batch cannot type (e.g. float>2**53 beside int), it must still raise the SAME `out_of_core_fk_key_dtype_unsupported` as `main` would for that same batch — add a second case asserting that fail-closed parity.

- [ ] **Step 2: Run to verify it fails**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_stream_join.py -q`
Expected: FAIL (`resolve_batch`/`iter_join_rows` not defined).

- [ ] **Step 3: Implement the split**

Extract the `iter_output` body: `iter_join_rows` keeps the query + `to_arrow_reader(batch_rows)` loop and yields each raw `result` batch unchanged. `resolve_batch` runs the per-batch resolution that was inside that loop (lines ~316-335 of the current file): `_batch_remap_values`, `_with_positional_row_nr`, `_append_output_batch`, `observed_types` update, `_cast_chunks`. Delete `iter_output`.

- [ ] **Step 4: Run to verify it passes**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_stream_join.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_stream_join.py tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "refactor(ooc-b): split StreamFkJoiner into iter_join_rows + resolve_batch"
```

---

### Task 3: `JoinRowCursor` (replace `FkOutputCursor`)

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py`

**Interfaces:**
- Consumes: `iter_join_rows(batch_rows)` reader (Task 2).
- Produces `class JoinRowCursor(reader, join_columns)`:
  - `take(self, n: int, expected_row_nr_start: int) -> pa.RecordBatch` — exactly `n` raw join rows, sliced across reader batches; asserts the run's `__decoy_row_nr` equals `[expected_row_nr_start, expected_row_nr_start + n)` (single-read identity), raising `out_of_core_fk_row_alignment` on any mismatch or early exhaustion. Returns a RecordBatch of the raw join columns (so the driver can pass it to `resolve_batch`).
  - `assert_exhausted(self) -> None` — unchanged intent (payload stream must consume every join row).
- REMOVE `FkOutputCursor`. Update `__all__`.

**Change vs `FkOutputCursor`:** the identity check moves from "join row_nr contiguous with the cursor's own running count" to "join row_nr equals the caller's expected payload offset". Because the payload offset comes from the SAME single read that produced the join keys, this is an identity assertion between two artifacts of one read, and no source permutation can pass it.

- [ ] **Step 1: Write the failing test**

```python
def test_join_row_cursor_slices_across_reader_batches(fake_join_reader):
    # reader yields row_nr batches [0,1] then [2,3,4]; take 3 then 2 with offsets.
    cur = JoinRowCursor(fake_join_reader, JOIN_COLS)
    b0 = cur.take(3, 0); assert b0.column("__decoy_row_nr").to_pylist() == [0, 1, 2]
    b1 = cur.take(2, 3); assert b1.column("__decoy_row_nr").to_pylist() == [3, 4]
    cur.assert_exhausted()

def test_join_row_cursor_identity_mismatch_fails_closed(fake_join_reader):
    cur = JoinRowCursor(fake_join_reader, JOIN_COLS)
    with pytest.raises(ExecutionError) as exc:
        cur.take(3, 5)                       # wrong expected offset
    assert exc.value.code == "out_of_core_fk_row_alignment"
```

- [ ] **Step 2: Run to verify it fails**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_stream_join.py -k join_row_cursor -q`
Expected: FAIL (`JoinRowCursor` not defined).

- [ ] **Step 3: Implement `JoinRowCursor`**

Port `FkOutputCursor`'s bounded cross-batch slicing. `take` collects `n` rows of the raw join columns and returns them as one `pa.RecordBatch`; `_advance` validates `first == expected_row_nr_start + already_taken_in_this_call` and contiguity. Keep the `_row_alignment_error` helper; update its message to reflect single-read identity ("the join row_nr must match the payload row_nr captured in the single source read").

- [ ] **Step 4: Run to verify it passes**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_stream_join.py -k join_row_cursor -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_stream_join.py tests/unit/execution/test_out_of_core_stream_join.py
git commit -m "refactor(ooc-b): JoinRowCursor with single-read row_nr identity guard"
```

---

### Task 4: Rewire `stream_table` to the single read

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_driver.py`
- Test: `tests/unit/execution/test_out_of_core_runner_streaming.py`

**Interfaces:**
- Consumes: `PayloadStore` (Task 1), `iter_join_rows`/`resolve_batch` (Task 2), `JoinRowCursor` (Task 3).
- Produces: unchanged `stream_table` signature and outputs; `emit_to_sink` / `assemble_resident` unchanged.

**Changes:**
1. Phase 1 loop (the ONLY `_iter_source_batches(raw, …)`): after `stage_batch`, also run the `code_set_null_seen` update + `mask_batch` (moved from phase 3) and `store.append(masked_batch)`. Select `store = ResidentPayloadStore()` when `sink is None` else `SpillPayloadStore(temp_dir / "payload.arrow")`.
2. Delete the phase-3 `for raw_batch in _iter_source_batches(raw, …)` loop.
3. New phase 3 `rewritten()`: `for row_nr_start, masked_batch in store.iter_batches():` then per edge `join_rows = cursor.take(masked_batch.num_rows, row_nr_start)`, `fk_arrays = joiner.resolve_batch(join_rows)`, `masked_batch = _replace_fk_columns(masked_batch, edge.child_columns, fk_arrays, joiner.output_types)`; `yield masked_batch`. After the loop, `cursor.assert_exhausted()` per edge.
4. Cursors built from `joiner.iter_join_rows(batch_rows)`.
5. `store.close()` in the `finally` alongside joiner close.
6. Keep the `_release_joiners` hook and the deferred `code_set_corpora` commit (now driven by the phase-1 masking).

- [ ] **Step 1: Write the failing tests (the two Codex reproductions)**

```python
def test_chunked_bool_preserve_matches_oracle(...):
    # Parent customers.id = [False, False]; child orders.id chunked
    # [False,False] + [True,True]; PRESERVE; batch_rows=4. Was the HIGH crash.
    published = run_ooc(...)
    assert published.column("id").to_pylist() == [0, 0, 1, 1]

def test_single_read_source_mutation_cannot_corrupt(tmp_path, ...):
    # LazySource over a parquet. Wrap iter_batches so that AFTER phase 1 finishes,
    # a mutation hook rewrites the file in reversed order. Because phase 3 reads
    # the payload store, output must equal the UNMUTATED oracle (proving no 2nd read).
    published = run_ooc_with_post_phase1_mutation(...)
    assert published.equals(oracle_unmutated)
```

- [ ] **Step 2: Run to verify they fail**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_runner_streaming.py -k "chunked_bool_preserve or source_mutation" -q`
Expected: FAIL (HIGH still crashes / driver not yet single-read).

- [ ] **Step 3: Implement the rewiring**

Apply changes 1-6. Grep for stale references: `grep -rn "iter_output\|FkOutputCursor" src tests` and update all. Keep `_stream_driver.py` under the 600-LOC cap.

- [ ] **Step 4: Run to verify they pass**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_runner_streaming.py -q`
Expected: PASS (including the existing REMAP-across-batches and orphan-policy cases).

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_stream_driver.py tests/unit/execution/test_out_of_core_runner_streaming.py
git commit -m "feat(ooc-b): single source read via payload store + payload-aligned FK resolution"
```

---

### Task 5: Full parity + suite green, docstrings, lint

**Files:**
- Modify: module docstrings in `_stream_driver.py`, `_stream_join.py` (describe the single-read three-phase shape; remove the "two source re-reads" language).
- Verify: `tests/parity/`, OOC/RI unit suites.

- [ ] **Step 1: Update the two module docstrings** to the single-read design (payload store; resolution at source-chunk granularity; no second read). Cite DuckDB (join/spill) and Arrow IPC (lossless spill).

- [ ] **Step 2: Run the parity gate**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/parity/test_out_of_core_fk_parity.py -q`
Expected: PASS (62 cases, byte-identical to the pandas oracle).

- [ ] **Step 3: Run the out-of-core + RI unit suites**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution -q`
Expected: PASS. Fix any stale `iter_output`/`FkOutputCursor` references surfaced.

- [ ] **Step 4: Lint changed modules**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/ruff format --check src/decoy_engine/execution/out_of_core tests/unit/execution && /home/cam/vscode/decoy-engine/.venv/bin/ruff check src/decoy_engine/execution/out_of_core`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs(ooc-b): single-read docstrings; parity + OOC/RI suites green"
```

## Self-Review

- **Spec coverage:** payload store (Task 1), joiner split (Task 2), cursor identity guard (Task 3), single-read rewiring + both Codex repros (Task 4), parity + docs (Task 5). All spec sections mapped.
- **Type consistency:** `iter_join_rows`/`resolve_batch`/`JoinRowCursor.take(n, expected_row_nr_start)`/`PayloadStore.iter_batches()->(int,batch)` used identically across tasks.
- **No placeholders:** every step has concrete code/commands.
