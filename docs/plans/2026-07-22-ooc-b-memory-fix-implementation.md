# OOC-B Memory Fix (fix#1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Revised 2026-07-22 against Codex plan-review** (conditional GO). Corrections folded in: pinned DuckDB ingestion API (no community extension), reopen-per-scan readers, a decisive isolated spike under an explicit `memory_limit`, Task 5 rewritten (drop the wrong estimator retune; add mid-table disk enforcement), TQ-0 test lands before production commits, and the existing sink/permutation gates are named.

**Goal:** Make the out-of-core FK path's peak process memory bounded (independent of child row count) at the 200M-row / 8GB-cap tier, closing the regression PR #107 (OOC-B fix#1) introduced, while preserving every correctness invariant the single-source-read design established.

**Architecture:** The regression is in `StreamFkJoiner` (`_stream_join.py`): the child side of the per-edge join is a materialized DuckDB `child_keys` TEMP TABLE (built by per-batch `INSERT`), holding O(child) rows of `(row_nr int64, Python-string join key, raw FK components)`. Phase-1 attribution proved this is the dominant NATIVE, O(child) resident structure (peak RSS +517 MB per +1M rows/table, tracemalloc flat at 24.6 MB; payload store spills correctly and is not the driver). The **parent** side is already a spillable `read_parquet` VIEW (`_stream_join.py:182`). The fix makes the child side symmetric: stage child keys once to an on-disk Arrow-IPC file during phase 1 (the `RawParentKeySpill` pattern, `_payload_store.py:125`), then feed it to DuckDB per scan as a streaming `RecordBatchReader` so DuckDB's own external hash-join + merge-sort do the spilling, and no O(child) structure is ever resident in the process.

**Tech Stack:** DuckDB (public CORE API only — see Global Constraints), PyArrow IPC, existing `_join.py` single-join shape.

## Global Constraints

- **No library customization, and no community extensions (Cam, 2026-07-22; Codex plan-review).** Use only DuckDB's public CORE API shipped in the pinned version (DuckDB 1.5.4, `uv.lock:766`; runtime floor `duckdb>=0.10.0`, `pyproject.toml:83`): `read_parquet`, `conn.register()` / `conn.from_arrow()` over a PyArrow object, `memory_limit`, `temp_directory`, standard SQL, `to_arrow_reader`. **Do NOT use `read_arrow` / the DuckDB `arrow` community extension** — Codex verified it is not core in 1.5.4 and would add a provisioned, version-tracked dependency to maintain, exactly what Cam ruled out. Do NOT patch, vendor, or monkey-patch DuckDB or pyarrow. Annotate any dependence on documented DuckDB behavior (external hash-join + merge-sort spilling under `memory_limit`; `to_arrow_reader` batched export) in the module docstring with a one-line citation and the observed version. Keep the whole change inside our own `out_of_core/` modules.
- **Pinned ingestion mechanism (do not vary):** stage child keys to an Arrow-IPC file, and for each DuckDB scan open a FRESH `pa.ipc.open_stream(path)` → `RecordBatchReader` and `conn.register(name, reader)` (or `conn.from_arrow(reader)`). A registered `RecordBatchReader` is SINGLE-PASS (Codex verified: rows on first query, zero on second). `StreamFkJoiner` scans the child twice (`total_orphans()` FAIL precount, `_stream_join.py:275`; `iter_join_rows()`, `_stream_join.py:314`), so each scan MUST reopen + re-register the file. A persistent VIEW over one reader is INVALID.
- **Byte-parity is the hard gate.** `tests/parity/test_out_of_core_fk_parity.py` (byte-identical to the pandas oracle across every admitted type) must stay green, unchanged. Non-negotiable, gates every task.
- **IPC, never Parquet, for keys.** Child-key raw components may carry Arrow types Parquet cannot encode (`month_day_nano_interval`, run-end-encoded, etc.) that the route admits and the oracle masks. Use Arrow IPC for the child-key spill exactly as `RawParentKeySpill` does, and cite the same reason. Tests must exercise the chosen DuckDB ingestion path across admitted key types, not merely a PyArrow IPC round-trip.
- **Single source read is invariant.** Child keys are captured during the ONE phase-1 raw pass; phase 3 resolves from artifacts of that same read. No second source read.
- **Resolution stays at payload-batch boundaries.** Phase 3 resolves FK output per payload-store batch via `JoinRowCursor.take`. `_append_output_batch` does value-derived per-batch Arrow inference; re-batching resolution can resurrect the prior type/parity failure. Do not change resolution granularity.
- **Order + alignment guards stay.** Global row order, contiguous-row_nr assertions (`_stream_join.py:427-499`), FAIL's whole-child orphan precount before any output, WARN totals, overlapping-edge last-write — all unchanged. `JoinRowCursor` works with global OR windowed join output provided concatenated output is exactly `0..N-1`.
- **Do NOT touch `_join.py::mask_child_fk`** (Codex). It is the resident oracle path; its child is a resident `pa.Table` that deliberately materializes for two scans (`_join.py:109-125`), and its call sites are tests/oracle comparisons, not the production streaming driver. Changing it broadens scope and weakens the independent oracle. The fix is confined to the SINK/streaming path via `StreamFkJoiner` + `_stream_driver.py`.
- **Pre-GA, fix-forward on a branch** (`fix/ooc-b-memory-streaming-join`). Revert of PR #107 is the fallback ONLY if the corrected spike (Task 1) shows no bounded public-core-API path, or if the windowed fallback (Task 6) proves operationally unacceptable.

---

## Task 1: SPIKE — prove a memory PLATEAU under an explicit limit (decisive go/no-go before build)

**Why first:** The fix's premise is that DuckDB, fed both join inputs as file-backed streaming readers under an explicit `memory_limit`, keeps process RSS bounded as child rows grow. Codex's caveats: (a) a PyArrow reader streams bounded batches but does NOT itself spill — DuckDB's JOIN + ORDER BY are the blocking operators expected to spill, and DuckDB warns multiple blocking operators in one query can still OOM; (b) `memory_limit` governs mainly the buffer manager, not every vector/result allocation; (c) the 3-table probe scales parent AND child cardinality together, so the separate relation-build floor (`_memory_estimate.py:335-367`) can mask the child-side win. So the spike must ISOLATE the joiner, FIX parent cardinality, vary child, and assert a PLATEAU under an explicit limit. If RSS merely drops but keeps a child-row slope, the ceiling only moved → Task 6 (windowed) becomes mandatory.

**Files:** throwaway spike script under scratchpad (NOT committed to `src/`).

- [ ] **Step 1: Prototype the swap.** Throwaway edit of `StreamFkJoiner`: in `stage_batch`, additionally write each shifted `(row_nr, join_key, src_i...)` key batch to an Arrow-IPC file (mirror `RawParentKeySpill`). In `total_orphans` and `iter_join_rows`, replace `FROM child_keys c` with a FRESH `pa.ipc.open_stream(path)` reader registered via `conn.register("child_keys", reader)` — reopened + re-registered per scan (single-pass). Keep `parent_keys` as the existing `read_parquet` VIEW and the `LEFT JOIN ... ORDER BY __decoy_row_nr` identical. Open every DuckDB connection with an EXPLICIT `memory_limit` (e.g. 512MB), not uncapped.

- [ ] **Step 2: Isolated joiner measurement — FIXED parent, ≥3 growing child sizes, both widths, explicit limit.** A harness that builds one parent relation of FIXED cardinality, then stages child sizes at (e.g.) 2M, 4M, 8M rows for width 0 AND width 16, each in a fresh subprocess reading `VmHWM` (with `ARROW_DEFAULT_MEMORY_POOL=system`, `MALLOC_ARENA_MAX=2`), DuckDB `memory_limit=512MB`. Expected (GO): `VmHWM` PLATEAUS across the three child sizes (bounded increment), parity ok. RED baseline to beat: the current TEMP-TABLE path's +517 MB per +1M rows/table slope.

- [ ] **Step 3: Reopen/re-register correctness for FAIL.** Assert `total_orphans()` (scan 1) then `iter_join_rows()` (scan 2) both return the correct rows from the same on-disk file — proving the single-pass reader is correctly reopened per scan (the failure mode Codex flagged: second scan returning zero rows).

- [ ] **Step 4: Full route under a process cap.** Run `scripts/fk_memory_probe.py --mode out_of_core --rows 2000000 --width 16 --orphan-policy preserve --mem-cap-mb 2048 --rlimit-kind data --json`. Expected (GO): completed=true where the RED code OOMs. NOTE: 2M authorizes proceeding with the build; it does NOT establish the 200M invariant — that is proven only by the GCP run at VERIFY.

- [ ] **Step 5: Record the decision.** Append numbers + GO/NO-GO to this plan and `scratchpad/oocb-investigation.md`. GO → Task 2. NO-GO (RSS keeps a child-row slope; the global sort/result is the residual) → Task 6 becomes the PRIMARY fix (re-plan), or recommend reverting PR #107 if no bounded public-core path exists.

### Task 1 RESULT (2026-07-22): GO
- **A (isolated joiner, fixed 1M parent, width 0):** VmHWM plateaus in a ~1.1–1.4 GB band across child 3M→18M (6×); per-1M slope decays 189→83→61→33 MB/1M (RED = constant +517 MB/1M). Parity ok at every size. The child side is no longer O(child) resident.
- **B:** two-scan reopen PASS (`total_orphans` scan 1 → `iter_join_rows` scan 2, both correct from the same file; the "scan 2 returns zero" failure mode did not occur).
- **C:** full route 2M/width16 under a 2 GB `RLIMIT_DATA` cap: completed=true, peak_rss 1587 MB, parity ok, temp_disk 2.49 GB.
- **Residual:** ~30 MB/1M creep + ~140 MB allocator jitter at 15–18M — a bounded external-sort floor, NOT an O(child) slope, so Task 6 stays contingency. The 200M GCP run at VERIFY remains the real ceiling test.

### CRITICAL harness note (fold into every capped run, incl. TQ-0 + GCP verify)
The probe's single-tier `--mem-cap-mb` path does NOT auto-set the allocator-pinning env; only its `--capability` path does. Any process-capped run MUST export `ARROW_DEFAULT_MEMORY_POOL=system` and `MALLOC_ARENA_MAX=2` in the environment BEFORE process start, or it spuriously OOMs on address-space reservation (peak_vms ~3.9 GB blows a 2 GB `RLIMIT_DATA` even though peak_rss is ~1 GB). Bake this into the TQ-0 sentinel and the GCP verify invocation so the guard can't be bitten by it.

---

## Task 2: `SpillChildKeys` — disk-backed, RE-OPENABLE child-key store

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_payload_store.py` (add `SpillChildKeys` beside `RawParentKeySpill`; new `_child_key_spill.py` if `_payload_store.py` nears its LOC ceiling — check first).
- Test: `tests/execution/out_of_core/test_child_key_spill.py` (new).

**Interfaces:**
- Consumes: the `(row_nr, join_key, src_i...)` key schema `StreamFkJoiner._key_schema` builds (`_stream_join.py:146`).
- Produces: `SpillChildKeys(path, key_schema)` with `.append(record_batch)`, `.finalize()` (idempotent), and `.open_reader() -> pa.RecordBatchReader` that returns a FRESH single-pass reader each call (each scan reopens). Explicit close / context-manager scoping, tolerant of an early consumer abort (partial read then reopen). Mirror `RawParentKeySpill`'s eager-writer + idempotent-finalize + re-openable-read shape.

- [ ] **Step 1: Write the failing test** — append 3 batches (incl. one admitted non-Parquet type, e.g. `month_day_nano_interval`), finalize, then call `open_reader()` TWICE and assert BOTH reads return the full row set + schema + row_nr contiguity (the two-scan / single-pass-reopen requirement). Assert an early-aborted read followed by a fresh `open_reader()` still yields the full set.

- [ ] **Step 2: Run it, verify it fails** (`SpillChildKeys` not defined).

- [ ] **Step 3: Implement `SpillChildKeys`** from `RawParentKeySpill`'s structure; `open_reader()` does `pa.ipc.open_stream(path)` fresh each call. Docstring cites the IPC-not-Parquet reason, the "symmetric with the parent read-view" rationale, and the single-pass-reopen requirement.

- [ ] **Step 4: Run the test, verify it passes.**

- [ ] **Step 5: Commit.**

## Task 3: `StreamFkJoiner` — replace the `child_keys` TEMP TABLE with the spill + per-scan registered reader

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_stream_join.py` (`begin_staging`, `stage_batch`, add `finalize_staging`, `total_orphans`, `iter_join_rows`, `close`; module + method docstrings).
- Test: existing joiner unit tests (find + extend).

**Interfaces:**
- Consumes: `SpillChildKeys` (Task 2). Same `child_key_types`, `parent_relation` VIEW, `_child_key_batches` encoding.
- Produces: identical public surface and byte-identical join SQL / ORDER BY / result columns. Adds `finalize_staging()`.

- [ ] **Step 1: Write/extend the failing test** — whole-child stage + `total_orphans()` + `iter_join_rows()` over a small fixture; assert identical row order, orphan match indicator, and resolved output as the current TEMP-TABLE path (golden compare on the same seed). Include an admitted non-Parquet key type so the DuckDB INGESTION path (not just IPC round-trip) is exercised.

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement.** `begin_staging` opens a `SpillChildKeys` at `temp_dir/child_keys.arrow` instead of `CREATE TEMP TABLE`. `stage_batch` appends the shifted key batch instead of `INSERT`. `finalize_staging()` closes the writer (idempotent) so the file carries its EOS before any scan. `total_orphans` and `iter_join_rows` EACH call `SpillChildKeys.open_reader()`, `conn.register("child_keys", reader)`, run the query, then unregister — a fresh reader per scan. `parent_keys` stays the existing `read_parquet` VIEW; the `ORDER BY __decoy_row_nr` and every selected column stay identical. Open the connection with the caller-supplied `memory_limit`. Update the module docstring: the child side is now a spillable file-backed streaming scan symmetric with the parent, with the DuckDB-behavior annotation; DELETE the now-false "child keys are staged once into a spillable `child_keys` TEMP TABLE" sentence.

- [ ] **Step 4: Run joiner tests + `tests/parity/test_out_of_core_fk_parity.py`, verify green.**

- [ ] **Step 5: Commit.**

## Task 4: Driver wiring — finalize the child-key spill at the phase-1/phase-2 boundary

**Files:** Modify `src/decoy_engine/execution/out_of_core/_stream_driver.py`.

- [ ] **Step 1: Write the failing test** — a mid-phase-1 exception still cleans up the child-key writer (no leaked handle), mirroring the `raw_parent_spill` guard.

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement** — call each joiner's `finalize_staging()` after the phase-1 loop (before `total_orphans`), mirroring `raw_parent_spill.finalize()` at `_stream_driver.py:278`; add the idempotent call to the `finally` guard (`:390`). No other phase-1/3 change.

- [ ] **Step 4: Run driver + parity tests, verify green.**

- [ ] **Step 5: Commit.**

## Task 5: Disk accounting — mid-table enforcement + correct the stale estimator docstring

**Rewritten per Codex.** Do NOT retune `predict_ooc_build_floor_bytes` — it prices the SEPARATE outgoing relation-build phase and is unchanged by this fix (`_memory_estimate.py:365-367`); recalibrate it only with new relation-build measurements. The real gap: runtime disk enforcement currently fires only AFTER each table finishes (`_runner.py:167-171`), so `child_keys.arrow + raw_parent_keys.arrow + payload.arrow` can exhaust disk mid-table before that boundary.

**Files:** Modify `_runner.py` (add a mid-phase-1/batch disk checkpoint), `_spill_estimate.py` (the `:56` docstring is false since OOC-B: payload columns no longer "stream straight to the sink"; the three IPC spills are transient on-disk state), and early-delete each spill file after its last read.

- [ ] **Step 1: Write the failing test** — a phase-1 disk checkpoint fires (fail-closed) when the combined spill footprint would exceed the disk budget mid-table, not only at the table boundary.

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement** the mid-table checkpoint + early deletion of each spill after its last read; correct the `:56` docstring to describe the child-key + payload + raw-parent-key IPC spills. Leave `predict_ooc_build_floor_bytes` alone.

- [ ] **Step 4: Run tests, verify green.**

- [ ] **Step 5: Commit.**

## Task 6 (CONTINGENCY — only if Task 1 is NO-GO): windowed bounded join, no global sort

**Trigger:** Task 1 shows RSS keeps a child-row slope under the explicit limit (the global `ORDER BY` external sort or `to_arrow_reader` result is itself the residual O(child) structure).

**Approach:** Child keys are staged in row_nr order (contiguous). Process the child-key IPC file in bounded windows by SEQUENTIAL batch consumption of the stream (NOT `WHERE row_nr BETWEEN ...` predicates, which rescan the whole file per window — Codex). Register only the current window, LEFT JOIN it against the parent VIEW, sort within the window (bounded), yield, release. Bounds the join working set to W rows.

**Design risks to resolve first (Codex):** joining each window separately risks rebuilding/rescanning the parent relation once per window → catastrophic `windows × parent` cost. Add an I/O/runtime gate; either keep the parent hash build resident across windows (bounded by distinct parent keys, the pre-OOC-B floor we accept) or measure the rebuild cost. Preserves the phase-3 contract (`iter_join_rows` yields contiguous row_nr batches; `JoinRowCursor` unchanged). Larger change → own byte-parity re-run.

---

## TQ-0: permanent memory-invariant regression test — LANDS BEFORE PRODUCTION COMMITS

Per Codex: the sentinel is the TQ-0 gate, so it lands test-first (its red state IS the bug) BEFORE Task 3's production commit, not after. Extends `tests/perf/test_out_of_core_memory_sentinel.py` (the existing process-isolated sentinel uses only 5000 rows with 2x headroom, `:57` — why it missed this).

**Design (Codex robustness guidance):**
- Fresh Linux subprocess; read `VmHWM` (native growth, not tracemalloc).
- `ARROW_DEFAULT_MEMORY_POOL=system` + `MALLOC_ARENA_MAX=2` before process start (match `fk_memory_probe.py:207`).
- ≥3 child sizes beyond the DuckDB spill threshold, EXPLICIT DuckDB `memory_limit`, FIXED parent cardinality; assert a PLATEAU / bounded slope, NOT an absolute peak.
- Separate key-width from payload-width (width-0 reacts to FK-key cardinality; payload case to payload width).
- Keep the meaningful-scale case `perf`-marked; pair with a fast structural test asserting `child_keys.arrow` exists and the join reopens it per scan.

## Verify list + gate order (per development-loop.md)

1. Task 1 spike GO (plateau under explicit limit).
2. TQ-0 sentinel green (was red pre-fix).
3. Full regression incl. the existing sink + permutation gates: `tests/parity/test_out_of_core_fk_parity.py`; `tests/unit/execution/test_out_of_core_runner_streaming.py:69`, `:1047`, `:1141`.
4. dennis (Opus) full adversarial review.
5. Codex final cross-model gate (exit second opinion).
6. VERIFY: golden gate (`scripts/test_flight.py`) + one GCP run at 200M@8GB proving completion + bounded peak RSS + parity (GCP 4/50 used).
7. GATE: dennis MERGE + Codex SOUND + acceptance green. PARK if any fails.

## Codex plan-review disposition (2026-07-22)
- Verdict: conditional GO, fix-forward confirmed. All required corrections folded in above: pinned core ingestion API (no `read_arrow` community extension); reopen-per-scan single-pass readers; decisive isolated spike under explicit `memory_limit` with fixed parent + ≥3 child sizes; Task 5 rewritten (dropped the wrong `predict_ooc_build_floor_bytes` retune, added mid-table disk enforcement); `_join.py::mask_child_fk` explicitly untouched; TQ-0 lands before production commits; sink/permutation gates named. Task 6 stays contingency (ORDER BY is not inherently O(child)) with its `windows × parent` risk called out.
