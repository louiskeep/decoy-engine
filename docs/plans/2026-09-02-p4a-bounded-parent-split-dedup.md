# P4-A (slice 1): bounded-parent split-dedup

Status: plan APPROVED for build (authored 2026-09-02, Opus; Codex PLAN-gate rounds 1-4: NO-GO x3 -> GO round 4). Build next (Sonnet); then VERIFY + dennis + Codex final gate. Held target
branch `feat/native-phase3`; Phase 4 merges once at the end.

> First of three ordered, gate-able slices toward a LIVE never-OOM FK reorder
> route (Cam decision 2026-09-02): **this slice = Task 4 bounded-parent
> split-dedup** -> then Task 6 sequential per-edge driver -> then Task 7 route
> seam. This slice is independently valuable: the parent dedup it bounds is
> shared by the CURRENT resident `_batch_join.py` route, so it removes that
> route's O(parent) build floor too, not only the future reorder route's.
> Prior art: the shelved 8-task plan's "Task 4"
> (`docs/plans/2026-07-22-ooc-b-external-reorder-implementation.md` @
> `feat/ooc-b-external-reorder`).

## 1. Why this slice exists

`_relation.py::_build_relation` derives the deduplicated parent-key relation
(one row per join key, last-write-wins by `max(__decoy_row_nr)`). It already
stages the registered parent-key stream to a parquet file, then runs the dedup.
But the dedup is ONE query: the winners aggregate
(`max(__decoy_row_nr) GROUP BY __decoy_fk_join_key`) is an inline SUBQUERY of the
join-back, so DuckDB holds TWO blocking operators (the hash aggregate and the
hash join) co-resident. The hash aggregate's per-group state (one int per
distinct key) is then pinned in memory alongside the join build -- an O(distinct
parent key) resident floor (measured elsewhere as ~190 bytes/row). That floor is
exactly what `enforce_ooc_memory_preflight` prices and rejects large parents
against, and it is the parent-side never-OOM gap the reorder route inherits.

Splitting the winners aggregate into its OWN `COPY`-to-disk, then a SEPARATE
join-back that reads the winners from disk, makes the aggregate and the join two
distinct single-blocking-operator plans. DuckDB runs a `max`-over-int external
`GROUP BY` fully spillable (measured: 20M groups complete at a 600 MB limit), and
an external hash join spills cleanly, so neither pins O(parent) state.

## 2. Scope

IN: `_build_relation` in `src/decoy_engine/execution/out_of_core/_relation.py`
(that function ONLY). Replace the single combined `COPY (... JOIN (SELECT max ...
GROUP BY ...) ...)` with three statements on the one connection:
1. `COPY parent_keys TO <staged.parquet>` (already present -- unchanged).
2. NEW: `COPY (SELECT __decoy_fk_join_key, max(__decoy_row_nr) AS
   __decoy_win_row_nr FROM read_parquet(<staged>) GROUP BY __decoy_fk_join_key)
   TO <winners.parquet>`.
3. NEW: the join-back reads the winners from `read_parquet(<winners>)` instead of
   the inline subquery: `COPY (SELECT s.__decoy_fk_join_key, <masked cols> FROM
   read_parquet(<staged>) s JOIN read_parquet(<winners>) w ON
   s.__decoy_fk_join_key = w.__decoy_fk_join_key AND s.__decoy_row_nr =
   w.__decoy_win_row_nr) TO <out>`.
The `finally` unlinks BOTH the staged and the new winners scratch parquet
INDEPENDENTLY (each guarded so one failing does not skip the other, and neither
skipped if `conn.close()` raises), and removes a partially written output parquet
on a mid-build failure.

OUT (explicit non-goals): Task 6 (driver), Task 7 (route seam / process_ceiling
/ require_disk), and any change to `_build_relation`'s signature, the returned
`ParentKeyRelation`, the dedup SEMANTICS, or any caller. This slice only changes
the internal query SHAPE.

## 3. Behavior contract (what "correct" means)

- **Row-value-equivalent** (not "byte-identical" -- COPY output order and Parquet
  layout are not guaranteed stable). The output relation, compared as a
  key->row mapping with exact typed values and row order IGNORED, is identical to
  the current single-query form: exactly one row per `__decoy_fk_join_key`, every
  masked column taken from the SAME winning row (max `__decoy_row_nr`).
  `__decoy_row_nr` is globally unique, so the join yields exactly one winner per
  key; the split does not change which row wins or how a composite key's columns
  stay row-consistent (no `arg_max` NULL-member hazard, same as today).
- **Non-NULL join key (invariant, verified).** `__decoy_fk_join_key` is a
  `pa.string()` built via `_fk_keys.fk_key_value`, which folds a null source FK
  to the `NULL_FK_KEY` sentinel string. It is therefore NEVER a SQL NULL, so the
  `=` join is TOTAL (no NULL-key row is silently dropped by `NULL = NULL`). The
  split uses the SAME `=` join as the combined form, so even if a SQL NULL key
  ever arose, both forms would treat it identically. A null-source-FK row is
  tested (#2) to confirm its sentinel key round-trips identically.
- **Two separate physical plans.** The winners statement's physical plan contains
  an aggregate and NO join; the join-back statement's contains a join and NO
  aggregate. Structural proof that the two blocking operators are never
  co-resident (not merely two SQL strings DuckDB might re-fuse).
- **Memory-bounded (bounded, not just "fits once").** The parent dedup holds only
  fixed-size spillable state (an int-per-group `max` aggregate that spills to
  disk, then an external hash join that spills), so peak resident memory does NOT
  grow with distinct-key count -- proved by a plateau across increasing
  cardinalities (#5), not a single capped case.
- **Row order not part of the contract (audited).** Both relation consumers
  (`_batch_join.py:162` TEMP TABLE + `_stream_join.py` VIEW) read the relation and
  `LEFT JOIN ... ON __decoy_fk_join_key`; neither reads it positionally, so the
  COPY output row order is irrelevant.
- **Scratch cleanup (independent, fail-closed).** The staged AND the new winners
  scratch parquet are each unlinked on every exit path, INDEPENDENTLY (one
  unlink failing, or `conn.close()` failing, must not skip the other), and a
  partially written output parquet is removed on a mid-build failure.
- **Peak scratch disk rises (narrowed guarantee, Codex plan-gate HIGH).** The
  split trades resident memory for disk: the new winners parquet ((key,
  max_row_nr) pairs = O(distinct keys), 2 columns, so strictly smaller than the
  staged file) coexists with the staged input AND the output parquet during
  join-back. Peak scratch disk therefore rises by up to the winners-file size
  versus the combined form. This slice does NOT add a disk RESERVATION for that
  (the per-edge `require_disk` ledger is Task 7); its guarantee is the MEMORY
  bound, at a bounded (<= staged) extra peak-disk cost. Disk exhaustion during
  either `COPY` fails CLOSED: DuckDB raises, the exception propagates, and the
  `finally` unlinks every scratch file + any partial output -- never a silent
  or corrupt result. The winners/staged scratch lands in the same job temp area
  the staged file already uses, under the route's existing disk budget.

## 4. Acceptance tests (authored before impl; no later contributor weakens them)

1. **`test_dedup_runs_as_two_separate_physical_plans`** -- spy the connection's
   `execute` (wrap, do not reassign the read-only builtin), capture
   `EXPLAIN (FORMAT JSON)` for the winners and join-back `COPY` statements, and
   assert: winners plan has an AGGREGATE/GROUP operator and NO JOIN; join-back
   plan has a JOIN and NO AGGREGATE/GROUP. Structural, on operator types.
2. **`test_split_dedup_is_value_identical`** -- the PRIMARY oracle is the actual
   pre-split DuckDB combined query (the test runs the current combined SQL and the
   new split on the SAME input in the SAME DuckDB, comparing outputs), NOT pandas
   -- DuckDB and pandas can differ on NULL/type/decimal/timestamp handling.
   Compare as a `key -> typed-row` mapping, row order ignored, values exact.
   Parametrized over edge cases (Codex plan-gate MEDIUM): (a) a plain
   last-write-wins fixture (a key in several rows, ascending `__decoy_row_nr`);
   (b) EMPTY input (zero parent rows -> empty relation, no crash); (c) a
   null-source-FK row (its `NULL_FK_KEY` sentinel key is present and identical in
   both forms); (d) a COMPOSITE join key; (e) multiple masked columns, some
   values NULL (the winning row's NULLs ride through the join unchanged in both
   forms). A pandas last-write-wins recomputation is a SECONDARY independent
   sanity check only.
3. **`test_split_dedup_cleans_scratch`** -- after a successful build, no
   `*_staged.parquet` / `*_winners.parquet` scratch remains under the temp dir
   (only the relation's own output parquet).
4. **`test_split_dedup_cleanup_is_fail_closed`** -- inject a failure (i) during
   the winners step and (ii) during the join-back step; assert BOTH scratch files
   are gone and no partial output parquet remains. Then (iii) inject the FIRST
   scratch `unlink` itself to raise: assert the OTHER scratch file is still
   unlinked and no partial output remains (independent guards -- the point is
   that one cleanup failing never SKIPS the others; the injected-to-fail file
   itself is expected to remain, since the test forced its unlink to fail, and
   the injected error must not mask the build's own outcome).
5. **`tests/perf/test_out_of_core_relation_dedup_memory.py`** (perf-marked,
   subprocess RSS) -- boundedness proof with FIXED tolerances and a NEGATIVE
   CONTROL (Codex plan-gate HIGH), no escape hatch:
   - Pick a small DuckDB `memory_limit` L (e.g. 256 MiB) and a distinct-key count
     K chosen so the old combined plan's O(distinct-key) aggregate state
     (~190 B/key) is a MULTIPLE of L (e.g. K = 3M -> ~570 MB state >> 256 MiB), i.e.
     K genuinely crosses the regime where the unsplit form cannot stay bounded.
   - PLATEAU + ENVELOPE on BOTH runs (Codex plan-gate round-3 HIGH): build the
     SPLIT form at K and at 2K under L; assert `peak_rss(K) <= ENVELOPE_FACTOR * L`
     AND `peak_rss(2K) <= ENVELOPE_FACTOR * L` (BOTH within the fixed envelope,
     e.g. 1.6) AND `peak_rss(2K) <= 1.15 * peak_rss(K)` (flat, not O(K) growth).
     Bounding only the 2K run would let a K-run above the envelope slip through.
   - NEGATIVE CONTROL (Codex plan-gate round-3 MEDIUM): build the pre-split
     COMBINED query at K under L; assert EITHER its peak RSS is materially higher
     (`>= 1.5 * peak_rss(split at K)`) OR it fails with a RECOGNIZED
     memory/resource-limit error (DuckDB's out-of-memory / resource exception
     class specifically, matched on type/code -- NOT any exception). Any other
     failure (SQL error, fixture/I-O error, subprocess crash) FAILS the test, so a
     broken control can never masquerade as the expected OOM.
   - If this box cannot allocate the K/2K fixture on disk, the test SKIPS as
     INCONCLUSIVE (`pytest.skip`), never passes on an undersized run. Concrete K,
     L, tolerances, and the disk floor are pinned in the test module.

Regression: the full parent-relation unit suite
(`test_out_of_core_relation.py`, `test_out_of_core_relation_streamed.py`,
`test_out_of_core_relation_chunked.py`) and the byte-parity FK suite
(`tests/parity/test_out_of_core_fk_parity.py`, which builds relations through
this path) stay green -- the strongest value-identity guarantee.

VERIFY bar: coverage + mutation on the CHANGED unit (`_build_relation`), 0
unresolved correctness-critical logic; ruff/format/mypy(3.12) clean; perf RSS
green.

## 5. Failure modes (each fails closed / cleans up)

| Condition | Behavior | Test |
|---|---|---|
| Winners or join-back query raises | propagate; `finally` unlinks staged + winners INDEPENDENTLY + removes partial output | #4 |
| First cleanup unlink or `conn.close()` raises | every OTHER scratch is still unlinked (independent guards) | #4(iii) |
| Disk exhaustion (ENOSPC) during winners/join-back COPY | fail closed: COPY raises, propagate, `finally` unlinks all scratch + partial output | #4 (same path) |
| Empty input | empty relation, no crash | #2(b) |
| Null-source-FK key | `NULL_FK_KEY` sentinel key present, identical both forms (non-NULL invariant) | #2(c) |
| Output not one-row-per-key or wrong winner | caught by value-identity vs pre-split DuckDB + parity | #2, regression |

## 6. Tasks

- [ ] A. Land the failing acceptance tests (#1-#4) + the perf RSS test (#5).
- [ ] B. Split the combined query into winners-to-disk + join-back-from-disk in
  `_build_relation`; extend the existing WHY comment with the split rationale
  (winners land on disk so the aggregate and join are never co-resident); unlink
  the winners scratch in `finally`.
- [ ] C. VERIFY (relation suite + parity + perf RSS green; coverage+mutation on
  `_build_relation`; ruff/format/mypy) -> dennis REVIEW -> Codex FINAL gate.
  HELD, no merge.

## 7. Risks (resolved at plan-gate)

- Row ORDER may differ between combined and split forms (COPY has no ORDER BY).
  RESOLVED: audited both consumers -- `_batch_join.py:162` (TEMP TABLE) and
  `_stream_join.py` (VIEW) both `LEFT JOIN ... ON __decoy_fk_join_key`, never
  positional. Row order is not part of the relation's contract; the value test
  compares order-insensitively.
- NULL join key: RESOLVED -- `__decoy_fk_join_key` is non-NULL by construction
  (null FK -> `NULL_FK_KEY` sentinel string), and the split reuses the same `=`
  join as the combined form, so identical semantics either way (tested #2c).
- `read_parquet(<winners>)` adds one more scratch file, so peak scratch disk
  rises (see §3's narrowed guarantee): the winners file (O(distinct keys), 2
  cols, < staged) coexists with staged + output during join-back. The disk
  RESERVATION for this is Task 7's `require_disk`; this slice narrows its
  guarantee to the memory bound and makes disk exhaustion fail closed rather than
  silently reserving. Task 7 MUST add the winners-file term to the disk ledger.
