# OOC-B / fix#1 Single-Streaming-Join Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. This is a correctness-critical restructure of the FK-RI OOC path; code is written TDD against the real functions cited (exact pre-fabricated code is deliberately NOT given for the restructure tasks because it must match the live functions, which each task reads first). Byte-parity to the pandas oracle is the non-negotiable gate.

**Goal:** Remove the per-FK-edge materialized parent TEMP TABLE from the streaming sink path so OOC peak memory stops rising with parent row count, by giving the runner a single streamed join per edge.

**Architecture:** Replace `ChildFkBatchJoiner` (materialize parent TEMP TABLE, join per child batch) with a streaming joiner that stages child keys once and runs one `LEFT JOIN child_keys x parent_keys(read_parquet VIEW) ORDER BY __decoy_row_nr` per edge, reusing `_join.py`'s proven shape and `_append_output_batch`. Restructure `_stream_table` into three phases: key pre-pass, one join per edge, mask pass zipped row_nr-aligned to sink.

**Tech Stack:** Python, PyArrow, DuckDB (out-of-core join + spill), pytest.

## Global Constraints

- Byte-identical output to today: `tests/parity/test_out_of_core_fk_parity.py` and the whole parity/RI suite must pass unchanged. Output bytes, dtypes, orphan counts, and `QualityWarning`s are all frozen.
- Clean replacement, no runtime flag (Cam, 2026-07-21). Pre-GA; a revert is the rollback.
- Keep the sink path's FIXED output schema and `observed_types` accounting; do NOT adopt the resident path's value-derived narrowing.
- Per-batch (batch-local) REMAP minting stays; never precompute REMAP over the whole child (that reintroduces an O(child) resident structure). `_JOIN_BATCH_ROWS = 65_536`.
- Each edge keys off the RAW child values (`key_source=raw_batch` invariant): overlapping edges must not key off an earlier edge's rewrite.
- Orchestration modules cap ~600 LOC; factor the three-phase driver into its own helper if `_runner.py` would exceed it.
- `.venv/bin/python`; ruff repo-wide (src AND tests) before pushing; no em-dashes; commit trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Gate to merge: full parity/RI suite + dennis (Opus) + Codex final + a GCP 200M@8GB flat-floor proof. If parity or any gate fails: PARK and hand back, do not force.

---

### Task 0: Characterize the current mechanism and the memory model (no code change)

**Files (read only):**
- `src/decoy_engine/execution/out_of_core/_batch_join.py` (ChildFkBatchJoiner)
- `src/decoy_engine/execution/out_of_core/_join.py` (mask_child_fk, `_child_key_batches`, `_append_output_batch`)
- `src/decoy_engine/execution/out_of_core/_runner.py:247-433` (`_stream_table`, `rewritten()`, `_release_joiners`)
- `src/decoy_engine/execution/out_of_core/_memory_estimate.py` (`predict_ooc_build_floor_bytes`, `resolve_phase_memory_limits`, `memory_limit_for`, `enforce_ooc_memory_preflight`)
- `src/decoy_engine/execution/out_of_core/_relation.py` (`build_parent_key_relation_aligned`)

- [ ] **Step 1: Resolve the memory-model question.** Determine, from the code, whether the joiner phase (the materialized parent TEMP TABLE fix#1 removes) is priced by `predict_ooc_build_floor_bytes` or by a separate joiner-phase floor, and how `resolve_phase_memory_limits` allocates the sink-joiner / resident-joiner caps. Write the finding (2-4 sentences with line refs) into a scratch note at the top of this task's commit message. This decides Task 6's scope: if the joiner floor is priced off the same per-row model, fix#1 lowers the *joiner-phase* floor and the estimate/preflight must reflect it; if the 190 B/row model is build-phase-only (unchanged by fix#1), Task 6 narrows to the joiner-phase cap only.
- [ ] **Step 2: Confirm the REMAP + typing contracts.** Confirm (a) `ChildFkBatchJoiner._batch_remap_values` mints per input batch and `_join.py` takes precomputed `remap_values` (two different strategies; the sink path keeps per-batch), and (b) `output_types`/`observed_types` is how the sink path fixes schema. Record in the commit note.
- [ ] **Step 3: Commit the note** (docs-only, no code): `git commit --allow-empty -m "ooc-b task0: mechanism + memory-model characterization"` with the findings in the body.

**Deliverable:** a written, code-cited decision on Task 6's scope and the REMAP/typing contracts, so no later task guesses.

---

### Task 1: Land a baseline byte-parity assertion for the streaming FK path

**Files:**
- Test: `tests/parity/test_out_of_core_fk_parity.py` (extend), `tests/unit/execution/test_out_of_core_batch_join.py`

**Interfaces:**
- Consumes: existing parity fixtures and the pandas oracle harness.
- Produces: a named high-fan-in (>=2 incoming edges on one child) parity case + a composite-FK + all-four-orphan-policy case that will be the primary regression signal for the restructure. These must PASS on current `main` before any change.

- [ ] **Step 1:** Add/confirm a high-fan-in child parity case and a composite-FK + orphan-policy matrix case to the oracle suite (if the exact shapes already exist, note their names here instead of duplicating).
- [ ] **Step 2:** Run the full parity suite on the unchanged branch: `.venv/bin/python -m pytest tests/parity/test_out_of_core_fk_parity.py -q`. Expected: PASS (this is the frozen baseline).
- [ ] **Step 3:** Commit.

**Deliverable:** the exact byte-parity cases that gate every subsequent task, green on the pre-change code.

---

### Task 2: New streaming joiner unit (stage keys once, one ordered join)

**Files:**
- Create: `src/decoy_engine/execution/out_of_core/_stream_join.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py`

**Interfaces:**
- Produces a joiner with roughly: `open(edge, parent_relation, source_schema, temp_dir, memory_limit, *, mask_key) -> StreamFkJoiner`; `stage_keys(source_batches: Iterable[pa.RecordBatch]) -> None` (materializes `child_keys` TEMP TABLE from the raw source, extracting `(__decoy_row_nr, __decoy_fk_join_key, __decoy_src_i)` exactly as `_join.py::_child_key_batches` does); `total_orphans() -> int` (FAIL anti-join precount, `_join.py:129-138` shape); `iter_output(batch_rows) -> Iterator[pa.RecordBatch]` yielding the ordered FK output columns per result batch via `_append_output_batch` with per-batch REMAP minting; `output_types` / `observed_types` preserved from `ChildFkBatchJoiner`; `close()`.
- Consumes: `_append_output_batch`, `_batch_remap_values`, `connect_duckdb`, `_resolve_output_types`, the join-key encoders (reuse, do not reimplement).

- [ ] **Step 1:** Write failing unit tests: single edge, matched + null-key + orphan rows, each orphan policy, composite FK, and a REMAP case asserting batch-local minting. Assert output equals what `ChildFkBatchJoiner` produces today for the same input (reuse it as the in-test oracle).
- [ ] **Step 2:** Run, verify fail.
- [ ] **Step 3:** Implement `_stream_join.py` reusing `_join.py`'s SQL (parent as `read_parquet` VIEW, `CREATE TEMP TABLE child_keys`, single `LEFT JOIN ... ORDER BY __decoy_row_nr`, `to_arrow_reader(batch_rows)`), but keep per-batch REMAP minting and the fixed-schema `output_types`/`observed_types` from `ChildFkBatchJoiner`.
- [ ] **Step 4:** Run, verify pass. Keep the module under 600 LOC.
- [ ] **Step 5:** Commit.

**Deliverable:** a spillable single-join FK joiner, output-equivalent to `ChildFkBatchJoiner`, with no materialized parent.

---

### Task 3: Row-aligned forward cursor over the ordered FK output

**Files:**
- Add to: `src/decoy_engine/execution/out_of_core/_stream_join.py` (a small `FkOutputCursor` helper)
- Test: `tests/unit/execution/test_out_of_core_stream_join.py`

**Interfaces:**
- Produces `FkOutputCursor(iter_output_reader)` with `take(n) -> tuple[pa.Array, ...]` returning exactly `n` rows of FK output columns, slicing across reader batch boundaries; raises a fail-closed error if the reader is exhausted early or a row_nr range mismatch is detected.

- [ ] **Step 1:** Failing tests: `take(n)` across misaligned reader batch sizes (e.g. reader yields 65_536-row batches, cursor asked for 40_000 then 90_000); early-exhaustion raises; total rows conserved.
- [ ] **Step 2:** Run, verify fail.
- [ ] **Step 3:** Implement the cursor (holds at most one reader batch + a slice offset).
- [ ] **Step 4:** Run, verify pass.
- [ ] **Step 5:** Commit.

**Deliverable:** the bounded zip primitive that aligns join output to arbitrary mask-batch sizes.

---

### Task 4: Restructure `_stream_table` into the three-phase driver

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_runner.py:247-433`
- Possibly create: `src/decoy_engine/execution/out_of_core/_stream_driver.py` (if `_runner.py` would exceed ~600 LOC)
- Test: the parity suite (Task 1) + `tests/unit/execution/test_out_of_core_batch_join.py` successor

**Interfaces:**
- Consumes: `StreamFkJoiner` (Task 2), `FkOutputCursor` (Task 3), existing `mask_batch`, `_iter_source_batches`, `emit_to_sink`, `_fixed_output_schema`, `_fk_component_map`, `resolve_phase_memory_limits`.
- Produces: a `rewritten()` equivalent driven by phases 1-3; identical `yield`ed batches to today.

- [ ] **Step 1:** Write the phase-structured driver: (phase 1) one pass over `_iter_source_batches(raw, batch_rows)` calling `joiner.stage_keys(...)` for every incoming edge; FAIL precount via `joiner.total_orphans()` before any output; (phase 2) open each edge's `iter_output` reader + wrap in `FkOutputCursor`; (phase 3) second pass over `_iter_source_batches` -> `mask_batch(skip_columns=FK cols)` -> for each edge `cursor.take(out.num_rows)` -> replace FK columns in `out` (reuse `_replace_fk_columns`) -> yield. Preserve the code_set evidence deferred-commit logic and WARN aggregation exactly.
- [ ] **Step 2:** Run the full parity + RI suite: `.venv/bin/python -m pytest tests/parity/ tests/unit/execution/test_orphan_fk.py tests/unit/relationships/test_orphan_fk_policy_dedup.py -q`. Expected: PASS, byte-identical.
- [ ] **Step 3:** Run the DE-10 RI family + chunked-FK oracle. Expected: PASS.
- [ ] **Step 4:** Delete `ChildFkBatchJoiner` and `_open_joiner`/`_release_joiners` once nothing references them; update `test_out_of_core_batch_join.py` to target the new joiner. Confirm no dead imports.
- [ ] **Step 5:** Commit.

**Deliverable:** the streaming sink path runs on the single-streaming-join, byte-parity green, materialized parent TEMP TABLE gone.

---

### Task 5: Two-pass source re-read determinism guard

**Files:**
- Modify: `_runner.py` / `_stream_driver.py`
- Test: `tests/unit/execution/test_out_of_core_stream_join.py`

**Interfaces:** consumes the phase driver; produces an explicit row_nr alignment assertion.

- [ ] **Step 1:** Failing test: a source whose two reads must yield identical row order/count; assert the phase-3 zip's expected row_nr range matches the cursor, and that a (test-injected) reorder trips the fail-closed guard rather than silently misaligning.
- [ ] **Step 2:** Run, verify fail.
- [ ] **Step 3:** Add the guard (compare expected vs actual row_nr span per emitted batch; raise a fail-closed internal error on mismatch).
- [ ] **Step 4:** Run, verify pass.
- [ ] **Step 5:** Commit.

**Deliverable:** the correctness backbone (row_nr alignment across two passes) is asserted, not implicit.

---

### Task 6: Retune the memory floor / preflight to the new mechanism

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_memory_estimate.py`
- Test: `tests/unit/execution/test_*memory*` (the floor/preflight tests)

**Interfaces:** governed entirely by Task 0's finding.

- [ ] **Step 1:** Per Task 0: if the joiner-phase floor was priced by the pinned relation (now removed), lower that estimate to the streaming join's bounded working set, re-deriving from the same first-principles basis the current constants document, and update the affected preflight/routing tests. If the 190 B/row model is build-phase-only and unchanged by fix#1, instead narrow the joiner-phase cap allocation in `resolve_phase_memory_limits` to reflect that a joiner no longer needs a full-relation-sized cap, and document why the build-floor constant is untouched.
- [ ] **Step 2:** Update tests to the new expected floors; keep the over-predict/never-under-predict bias.
- [ ] **Step 3:** Run the memory/preflight suite. Expected: PASS.
- [ ] **Step 4:** Commit.

**Deliverable:** the memory model tracks the mechanism (no stale over-refusal from a floor that priced a structure that no longer exists).

---

### Task 7: Full local gate + docs

**Files:** repo-wide.

- [ ] **Step 1:** `.venv/bin/python -m pytest tests/ -q` (or the OOC + parity + RI + sentry subset if full-suite is too slow), plus `tests/sentry/test_module_size.py`. Expected: PASS.
- [ ] **Step 2:** `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` repo-wide; `.venv/bin/mypy` on the changed modules. Fix all.
- [ ] **Step 3:** Update the OOC narrative/docstrings that referenced the materialized-relation joiner or fix#2's co-residency workaround (barry can do the doc pass); note fix#1 shipped in the OOC capacity guide.
- [ ] **Step 4:** Commit.

**Deliverable:** a merge-ready branch, all local gates green.

---

### Gate chain (post-implementation, not a code task)

1. dennis (Opus) adversarial review of the full diff. Fix real findings.
2. When dennis is green: Codex final gate (`codex exec -m gpt-5.6-sol --sandbox read-only`), defensive-correctness framing. Fix real findings.
3. Open PR (body ends with the Claude Code line).
4. GCP re-run 200M@8GB: prove `completed=true` with a floor that no longer rises vs the 200M@16GB baseline. This is the empirical proof.
5. Merge only after byte-parity + dennis + Codex + GCP proof are ALL green. If any fails: PARK, hand back to Cam. Then ping Cam (he holds DPS instructions for after merge).
