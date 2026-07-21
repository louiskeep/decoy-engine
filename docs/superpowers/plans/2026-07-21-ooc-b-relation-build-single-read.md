# OOC-B follow-up: single-read the outgoing-relation build

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans. TDD, checkbox steps.

**Goal:** Close the last source re-read in the out-of-core FK path so `raw` is read exactly once end to end, eliminating the same-count-permutation corruption Codex reproduced in the outgoing-relation build.

**Context:** The Codex final gate confirmed OOC-B kills both original findings (mask/FK-resolve double-read and the typing HIGH) and that parity (154) + unit (1530) are green. Its remaining BLOCKER: `build_parent_key_relation_aligned(source_parent=raw, ...)` in `emit_to_sink` (`_emit.py:117,131`) and the resident build (`_stream_driver.py:323`) RE-READ `raw` and pair it positionally with the immutable masked output. On a `customers -> orders -> payments` chain, atomically swapping `orders`' Parquet for an equal-count reordering AFTER phase 1 corrupts `payments` silently (the relation maps the permuted raw keys to the original masked rows). This re-read is pre-existing on main, but OOC-B's single-read goal requires closing it. `_relation.py` does NOT change.

**Architecture:** During the single phase-1 read, capture the raw values of every outgoing-edge parent-key column to a Parquet spill (`temp_dir/"raw_parent_keys.parquet"`), in read order. Feed `LazySource(that spill)` to the relation build as `source_parent` instead of `raw`. The spill is an immutable engine artifact in the same read order as the masked output, so the positional pairing is now between two artifacts of ONE read and no source permutation can occur.

## Global Constraints

- Byte-parity is the hard gate (`tests/parity/test_out_of_core_fk_parity.py` + `test_out_of_core_group_c_parity.py`, 154 cases). No weakening.
- `_relation.py` and `build_parent_key_relation_aligned` are UNCHANGED. Only the driver captures, and `emit_to_sink`'s relation-build input changes.
- Raw parent-key values are only used to derive join keys via `fk_key_value`/`fk_join_key_tuple` (normalized) and are never emitted, so a Parquet round-trip of them is parity-safe (matches main's existing `LazySource(parquet)` relation-build path).
- `.venv` at `/home/cam/vscode/decoy-engine/.venv/bin`. Ruff `check` + `format --check` over `src tests` (the CI mirror) must be clean.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. No em-dashes. Comments say why.
- `_stream_driver.py` stays under 600 LOC.

---

### Task 1: Failing chain-mutation regression test

**Files:** Test: `tests/unit/execution/test_out_of_core_runner_streaming.py`

**Interfaces:** consumes `run_fk_out_of_core` / the existing streaming test scaffolding (`write_large_fk_chain`, `lazy_sources`, `make_graph`, `make_plan`). Reuse the harness the existing `test_single_read_source_mutation_cannot_corrupt` uses, but with a THREE-table chain so the mutated table has an OUTGOING edge.

- [ ] **Step 1: Write the failing test**

Build a `customers -> orders -> payments` chain over `LazySource`s. Capture the unmutated oracle output for `payments`. Then run the job with a hook that, AFTER `orders`' phase-1 payload capture completes, atomically replaces `orders`' Parquet file with an equal-count, equal-schema reordering. Assert published `payments` equals the unmutated oracle (its FK column must still map each payment to the correct customer/order), on BOTH the sink path and the resident (no-sink) path. Model the mutation hook on the existing single-read mutation test; the difference is the mutated table is an intermediate parent, so the corruption (if present) shows up in `payments`, not `orders`.

- [ ] **Step 2: Run to verify it fails**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_runner_streaming.py -k "chain_mutation" -q`
Expected: FAIL (payments corrupted: the relation build re-reads the mutated `orders`).

---

### Task 2: Capture raw parent keys in phase 1; feed the relation build

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_driver.py`
- Modify: `src/decoy_engine/execution/out_of_core/_emit.py`

**Interfaces:**
- `emit_to_sink`: replace the `raw: TableSource` parameter with `raw_parent_source: TableSource` (it is used ONLY as `source_parent=` in the two `build_parent_key_relation_aligned` calls at `_emit.py:117` and `:131`; nothing else in `emit_to_sink` reads `raw`, `source_schema` is separate). Update both call sites to `source_parent=raw_parent_source`.
- Driver: produce `raw_parent_source`.

- [ ] **Step 1: Implement the capture (driver)**

In `stream_table`, compute `outgoing_parent_columns = tuple(sorted({c for edge in outgoing_edges for c in edge.parent_columns}))`. If it is non-empty, before the phase-1 loop open a `pq.ParquetWriter(temp_dir / "raw_parent_keys.parquet", projection_schema)` where `projection_schema = pa.schema([source_schema.field(c) for c in outgoing_parent_columns])`. Inside the phase-1 loop (the existing single `for raw_batch in _iter_source_batches(...)`), after staging/masking, write the projection: `writer.write_batch(pa.record_batch([raw_batch.column(raw_batch.schema.get_field_index(c)) for c in outgoing_parent_columns], schema=projection_schema))` (skip zero-row batches). Close the writer after the loop (in the `finally` too, guarded). Set `raw_parent_source = LazySource(that path) if outgoing_parent_columns else raw` (when there are no outgoing edges the value is never used, so `raw` is a harmless default). The capture MUST live in the same single phase-1 loop, never a second `_iter_source_batches(raw, ...)`.

- [ ] **Step 2: Feed it to both relation builds**

- Sink path: pass `raw_parent_source` where `raw` was passed to `emit_to_sink` (`_stream_driver.py:298`), and rename the `emit_to_sink` parameter as above.
- Resident path (`_stream_driver.py:323`): `build_parent_key_relation_aligned(source_parent=raw_parent_source, masked_parent=table_out, ...)`.
- Confirm no other `raw` read remains after phase 1: `grep -n "source_parent=raw\b\|_iter_source_batches(raw" src/decoy_engine/execution/out_of_core/_stream_driver.py` should show only the phase-1 read and the capture.

- [ ] **Step 3: Run the regression test + streaming suite**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution/test_out_of_core_runner_streaming.py -q`
Expected: PASS (chain-mutation test now green on both paths; existing cases unchanged).

- [ ] **Step 4: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_stream_driver.py src/decoy_engine/execution/out_of_core/_emit.py tests/unit/execution/test_out_of_core_runner_streaming.py
git commit -m "fix(ooc-b): single-read the outgoing-relation build via phase-1 raw-key capture"
```

---

### Task 3: Parity + suite + lint + spec note

- [ ] **Step 1: Byte-parity**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/parity/test_out_of_core_fk_parity.py tests/parity/test_out_of_core_group_c_parity.py -q`
Expected: 154 passed, byte-identical.

- [ ] **Step 2: Full OOC/execution suite + CI ruff mirror**

Run: `/home/cam/vscode/decoy-engine/.venv/bin/python -m pytest tests/unit/execution -q`
Run: `/home/cam/vscode/decoy-engine/.venv/bin/ruff check src tests && /home/cam/vscode/decoy-engine/.venv/bin/ruff format --check src tests`
Expected: all pass.

- [ ] **Step 3: Update the design spec**

In `docs/superpowers/specs/2026-07-21-ooc-b-single-read-redesign.md`, replace the "pre-existing, out of scope" note about the outgoing-relation build with the capture described here (raw parent keys captured in phase 1, relation build reads the immutable spill, source read exactly once end to end).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(ooc-b): relation build now reads a phase-1 raw-key capture, not the source"
```

## Self-Review
- Coverage: capture (Task 2), both call sites (Task 2), chain-mutation regression on sink+resident (Task 1), parity + lint + spec (Task 3).
- Alignment invariant: raw-key spill order == masked-output order == phase-1 read order, so the positional pairing in `_relation_staging_batches` holds and no source permutation can occur.
- No `_relation.py` change; `emit_to_sink` param rename is the only signature change.
