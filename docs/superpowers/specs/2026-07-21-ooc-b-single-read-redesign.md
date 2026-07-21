# OOC-B redesign — single source read (Option A), design spec

**Supersedes** the two-read mechanism in
`2026-07-21-ooc-b-single-streaming-join-design.md`. That design cleared byte-parity and dennis
but was BLOCKED by the Codex final gate for two design-level regressions
(`2026-07-21-ooc-b-codex-block-decision-brief.md`). Cam chose Option A: rebuild on a single
source read. This spec is the mechanism for that rebuild. The memory goal (remove the
O(distinct-parent-key) pinned relation so peak RSS stops rising with rows) is unchanged; the FK
join stays a single spillable DuckDB `LEFT JOIN` per edge.

## The two findings this must resolve

1. **BLOCKER — same-count source permutation.** The two-read driver reads the child source in
   phase 1 (stage FK keys) and AGAIN in phase 3 (mask non-FK columns), then zips by position. The
   alignment guard validates only the join side's `__decoy_row_nr`; the mask side carries no
   identity. A `LazySource` re-read that returns the same row count in a different order (e.g. its
   backing Parquet atomically swapped between reads) silently misaligns FK-to-row. `main` read the
   source once for the mask/FK-resolve, so this exposure is new.

2. **HIGH — batch-boundary typing.** `_append_output_batch` builds `pa.array(component)` by
   value inference per batch. The two-read driver runs it per DuckDB result batch, `main` ran it
   per source chunk. Those boundaries differ, so a batch that coalesces matched-bool beside
   orphan-int (and other irreconcilable dtype mixes) now fails the shared fail-closed guard where
   `main` produced valid output. Byte-parity break, safe failure, exotic trigger.

## Chosen design: single fused read, payload store, payload-aligned resolution

One pass over the source. Capture the masked non-FK payload as it is read, keyed by the same
`__decoy_row_nr` used for the join. Resolve FK columns later from the captured payload, never from
a second read.

### Phases

1. **Phase 1 — fused key-stage + mask (the ONLY source read).** For each raw source batch, at the
   existing `_iter_source_batches(raw, batch_rows)` granularity:
   - Stage each incoming edge's `(row_nr, join_key, src_i)` keys into that edge's `child_keys`
     TEMP TABLE (`StreamFkJoiner.stage_batch`) — unchanged.
   - Mask the non-FK columns (`mask_batch`, skipping FK child columns) and update
     `code_set_null_seen` — moved here from phase 3 (masking still runs exactly once).
   - Append the masked batch, tagged with its contiguous `__decoy_row_nr` range, to the **payload
     store**.
   FAIL-policy anti-join precount runs after staging, as today, before any output exists.

2. **Phase 2 — one streamed ordered join per edge.** Each `StreamFkJoiner` runs its single
   `LEFT JOIN child_keys x parent_keys ORDER BY __decoy_row_nr` and exposes an ordered reader of
   **raw join rows**: `(__decoy_row_nr, __decoy_fk_join_key, __decoy_src_i…, __decoy_parent_match,
   __decoy_parent_masked_i…)`. DuckDB owns the join, spill, and sort (unchanged). The reader does
   NOT resolve FK output; resolution moves to phase 3.

3. **Phase 3 — resolve from the payload store (NO source read).** Iterate the payload store. For
   each masked payload batch of `m` rows at row_nr offset `o`:
   - From each edge's join cursor, `take(m)` raw join rows and assert their `__decoy_row_nr` equals
     the payload batch's own `__decoy_row_nr` range `[o, o+m)` (identity alignment, fail-closed).
   - Run `_append_output_batch` (+ per-batch REMAP mint, `_cast_chunks`, `observed_types`
     accumulation) on those `m` raw join rows to produce the edge's FK output columns.
   - Overwrite the payload batch's FK columns with the resolved arrays; emit to sink / collect for
     resident assembly.

### Why this kills both findings at the root

- **BLOCKER:** the source is read exactly once. The masked payload is captured in that read and
  persisted with its `row_nr`; phase 3 reads the payload store, an immutable engine-owned artifact,
  never the source. No second read exists to disagree, so a same-count source permutation cannot
  occur. The alignment assertion is now an identity check between two artifacts of the SAME read
  (payload row_nr vs join row_nr), not a trust in two reads matching.
- **HIGH:** `_append_output_batch` runs once per payload batch, and payload batches are captured at
  the same `batch_rows` source-chunk granularity `main` masked/resolved at. The value-inference
  boundary is therefore identical to `main`'s, so the fail-closed guard and `observed_types`
  narrowing fire on exactly the same inputs `main` does. Byte-parity becomes structural, not a
  reconciliation patch on coalesced batches.

## Components

### Payload store (new) — `_payload_store.py`

A forward-write / forward-read store of masked payload batches, each carrying `__decoy_row_nr`.
Two implementations behind one small interface (`append(batch, row_nr_start)`,
`iter_batches() -> Iterator[(row_nr_start, pa.RecordBatch)]`, `close()`):

- `ResidentPayloadStore`: an in-memory list. Used when `sink is None` (output already fits in
  memory by definition). No disk, no type round-trip — the masked batches are handed to phase 3
  and to `assemble_resident` exactly as masked, identical to today's resident typing.
- `SpillPayloadStore`: an **Arrow IPC record-batch stream** file under `temp_dir` (write in phase
  1, read in phase 3). Arrow IPC is lossless — it preserves the masked Arrow schema and types
  exactly (a Parquet round-trip would risk type drift and is not used here). Bounded residency:
  one batch resident at a time on read.

This is a standard staging-table / spill pattern; the spill format (Arrow IPC) is an established
lossless columnar stream. Cite in the module docstring per the engine's use-established-methodology
rule; no bespoke serialization.

### `StreamFkJoiner` — split resolution out of `iter_output`

`iter_output(batch_rows)` currently yields RESOLVED FK output. Replace it with
`iter_join_rows(batch_rows)` yielding the RAW join columns listed in Phase 2. Move the resolution
body (`_append_output_batch`, `_batch_remap_values`, `_with_positional_row_nr`, `_cast_chunks`,
`observed_types` accumulation) into a pure method `resolve_batch(join_rows) -> tuple[pa.Array, …]`
that phase 3 calls per payload batch. `orphan_total` accumulates inside `resolve_batch`. The fixed
`output_types`/`observed_types` contracts and the shared `_append_output_batch` guard are reused
verbatim — only WHERE resolution runs changes (per payload/source-chunk batch, matching `main`).

### `FkOutputCursor` → `JoinRowCursor`

Same forward-only, bounded, cross-reader-batch slicing (`take(m)`), but it returns the raw join
columns for a run of `m` rows instead of resolved FK arrays, and its `_advance` identity check
gains the caller-supplied expected row_nr offset so it asserts join row_nr == payload row_nr
(single-read identity), not merely the join's own internal contiguity. `assert_exhausted` is
unchanged in intent (mask/payload stream must consume every join row).

### `stream_table` (`_stream_driver.py`)

- Phase 1 loop gains the mask + payload-store append (the `code_set_null_seen` update and
  `mask_batch` call move here from phase 3). The second `_iter_source_batches(raw, …)` in phase 3
  is DELETED.
- Phase 3 `rewritten()` iterates the payload store, calls `cursor.take(m)` + `joiner.resolve_batch`,
  overwrites FK columns, emits. The row_nr identity assertion lives here.
- Resident vs sink selects the store implementation. `emit_to_sink` and `assemble_resident`
  signatures are unchanged (they still consume a batch iterator).
- Note (pre-existing, out of scope): outgoing-edge relation build still reads `source_parent=raw`.
  That read predates OOC-B and is shared with the resident/relation contract; it is not the
  mask/FK-resolve double read the BLOCKER identified. Called out so the review knows it was
  considered and deliberately left.

## Byte-parity and regression tests

- The full parity oracle (`tests/parity/`, 62 cases) and the OOC/RI suites are the hard gate,
  unchanged. Expectation: byte-identical, because resolution now runs at `main`'s granularity.
- **Add both Codex reproductions as permanent regression tests, asserted to PASS:**
  1. Chunked-bool PRESERVE, `batch_rows=4`, matched chunk `[False,False]` + orphan chunk
     `[True,True]` → output `int64 [0,0,1,1]` (the HIGH repro; must now succeed, not fail-closed).
  2. Same-count second-read permutation is now impossible by construction. Replace the old
     drops-a-row-only test with one that captures the payload in a single read and proves a
     source mutated after phase 1 cannot affect output (there is no second read), plus the
     positive identity-alignment assertion.

## Risks and residuals

- **Spill I/O cost:** the masked payload is now written and re-read on the sink path (the temp
  spill the two-read design was avoiding). This is the accepted cost of Option A; it trades disk
  for the removal of the silent-corruption surface. The FK join spill is unchanged.
- **Resolution relocation:** moving `_append_output_batch` out of `iter_output` is mechanical
  (the body is reused verbatim), but it is byte-parity-critical; the parity oracle + the two
  regression tests pin it.
- **`observed_types` across batches:** accumulation now happens per payload batch in phase 3;
  because payload granularity == source-chunk granularity, the accumulated set matches `main`. The
  parity gate on the resident/relation narrowing side pins this.

## Gate plan

Byte-parity + OOC/RI suites green → dennis (Opus) → Codex (gpt-5.6-sol) final gate,
defensive-correctness framing, both repros as regression tests → GCP run 4/50 (200M @ 8 GB) to
prove the flat floor → merge only when all green. PARK and hand back if any gate fails and cannot
reconcile without weakening byte-parity.
