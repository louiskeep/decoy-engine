# P4-A.2: the bounded external sorter (port + key-generalize `BoundedExternalSorter`)

Status: plan

> Part 2 Phase 4, slice P4-A.2. Cam chose this as the next piece (2026-09-01)
> and chose to **build it standalone now** (a foundational primitive with its own
> parity + memory-cap tests, no route wiring). Design doc:
> `docs/plans/2026-08-31-part2-phase4-plan.md` §2b. Prior art recovered from the
> shelved branch `feat/ooc-b-external-reorder`. Ledger: auto-memory
> `decoy-engine-efficiency-plan.md`. Phase 4 merges once at the end after all
> testing; this slice is built and held, not merged.

## What this slice is, and what it is NOT (READ FIRST)

A memory-capped external merge sort (Knuth TAOCP v3 §5.4: run generation +
k-way merge, resident memory O(M) independent of N) that reorders a stream of
Arrow batches by a key **without changing any value, only the memory envelope**.
It is Decoy's own bounded sorter because DuckDB `ORDER BY` / `ROW_NUMBER()` are
not reliably process-memory-bounded on the pinned 1.5.4 (measured 21 GB @ 8 GB at
200M rows) — see `2026-08-31-part2-phase4-plan.md` §3.

The primitive already exists, built and tested, on `feat/ooc-b-external-reorder`
as `ExternalRowNrSorter` (commit `389939c7`; the `OSFile`-not-`memory_map` RSS
fix `903a52e4`). It is NOT on `feat/native-phase3`. This slice **ports it and
generalizes its key** from the hard-wired int `__decoy_row_nr` to an arbitrary
orderable key column.

**Explicitly NOT in this slice:**

- **No route wiring.** Nothing in the OOC route calls the sorter after this slice.
  The recon corrected the master plan's premise: the *live* OOC route's row-order
  restoration (`_batch_join.py:258`, `ORDER BY __decoy_row_nr` inside
  `ChildFkBatchJoiner.join_batch`) is **per-batch bounded** (bounded by batch
  size, not child cardinality) and does not OOM at scale. The 21 GB@8 GB figure
  came from the **reverted** single-streaming-join architecture (#107 merged /
  #108 reverted). Re-landing a whole-stream external reorder is **P4-A.3**
  (`2026-09-01-p4a1-fk4a-join-free-out-of-core.md:317-320`), a deep
  DuckDB-version-pinned effort out of scope here.
- **No shuffle.** The sorter does not rescue `mask.shuffle`; that stays
  oracle-deferred (an exact NumPy-PCG64 reproduction is O(n)-disk and separate).
- **No multi-column tuple key (deferred).** See "The generalization" — this slice
  ships the single-arbitrary-typed-key form; the composite `(group, order,
  tiebreak)` key that `grouped_series` (P4-E) will need is deferred to its first
  real consumer, so the fail-closed merge-contiguity invariant is re-proven
  against a real key shape rather than speculatively.

This is a **foundational primitive ahead of its consumers** (order-restore
reland A.3; `grouped_series` P4-E, proof-gated). Cam accepted that trade with the
"build standalone now" choice. Its byte-parity target is verifiable today against
an in-memory-sort oracle, and its never-OOM proof is a subprocess RSS test — no
route needed.

## The port (recover verbatim, then generalize)

Port these four files from `feat/ooc-b-external-reorder` onto `feat/native-phase3`
(`git show feat/ooc-b-external-reorder:<path>`), preserving their structure and
tests, then apply the generalization below:

- `src/decoy_engine/execution/out_of_core/_external_sort.py` (428 LOC) — the sorter.
- `src/decoy_engine/execution/out_of_core/_reorder_budget.py` (171 LOC) —
  `resolve_reorder_budgets` (`run_bytes_cap` / `merge_fan_in` sized from the
  process budget as `F_SORT * M`); the perf proof imports it.
- `tests/unit/execution/test_ooc_external_sort.py` (192 LOC).
- `tests/unit/execution/test_ooc_reorder_budget.py`.
- `tests/perf/test_ooc_external_sort_memory.py` (283 LOC) — the subprocess VmHWM
  proof that caught the `memory_map` bug.

The existing design (verified against the branch source):
`ExternalRowNrSorter(spill_dir, run_bytes_cap, merge_fan_in, row_nr_column)`;
streaming lifecycle `write(batch)*` → `finish()` → `iter_ordered()` → `close()`.
Run generation flushes the buffer to a sorted on-disk IPC run *before* a row
would exceed `run_bytes_cap` (`_flush`, `table.sort_by(col)`, `_external_sort.py:293`);
a single over-cap row fails closed (`out_of_core_sort_row_too_wide`). `finish`
does a capped-fan-in k-way merge collapsing to one run. Fail-closed contiguity
(`_merge_heads_into`): emit only the safe prefix at/below the min "last value"
across non-final heads (`max_value()` at `:372`, `pc.less_equal(col, cutoff)` at
`:386`). Spill is pyarrow IPC read through `pa.OSFile` (never `pa.memory_map` —
the RSS fix). Residency is bounded by flush-before-overflow, one stored batch per
`_RunHead`, and `_per_head_cap_bytes = run_bytes_cap // (2 * merge_fan_in)` (the
halving absorbs the ~2x `concat_tables(...).sort_by(...)` merge transient).

## The generalization (row_nr int → arbitrary orderable key column)

The machinery is already key-agnostic: `_iter_bounded_views`, `_materialize`,
`_bounded_batches`, the IPC run format, the merge tree, and residency accounting
all move opaque Arrow batches. The int-`row_nr`-specific surface is narrow and
already routed through **type-generic Arrow kernels**:

1. `_RunHead.__init__(path, row_nr_column)` and `ExternalRowNrSorter.__init__(...,
   row_nr_column="__decoy_row_nr")` — rename the parameter to `sort_key_column`.
2. `_flush`: `table.sort_by(self._row_nr_column)` — sort by `sort_key_column`
   (unchanged behavior; already a column name).
3. `_RunHead.max_value(self) -> int`: `pc.max(pending[col]).as_py()` — the **only**
   int-typed surface. `pc.max` already works on any orderable Arrow type; change
   the return annotation to `Any` (the value is only ever compared, never used as
   an int).
4. The merge cutoff `pc.less_equal(combined[col], cutoff)` (`:386`) — `pc.less_equal`
   and `pc.min` are type-generic; no change beyond the column name.

Rename the class `ExternalRowNrSorter` → `BoundedExternalSorter` (keep a module
alias `ExternalRowNrSorter = BoundedExternalSorter` only if any ported test
references the old name; prefer updating the tests). This is a **minimal,
low-risk** generalization: it makes the sorter accept a single orderable key
column of any type (int, string, binary, date, …), which serves the near-term
consumers — order-restore's int `__decoy_row_nr` directly, and `grouped_series`
via a **pre-encoded single monotone sort-key column** (the consumer bakes its
`(group, order, tiebreak)` into one order-preserving column; that encoding, and
any native multi-column `sort_keys` tuple-cutoff, is deferred to P4-E when its
real key shape can test the contiguity invariant).

### Determinism + key contract (the caller's responsibility, documented)

- **Uniqueness / stability.** The sorter does NOT rely on Arrow `sort_by` being
  stable across the multi-pass merge (a run collapse reshuffles rows). For a
  deterministic output the **sort key must be total (unique per row)** — the
  caller bakes any needed tiebreak into the key column. `__decoy_row_nr` is
  already unique. `grouped_series` must append a stable tiebreak (e.g. the source
  row number) into its encoded key. The plan states this: "A stable tiebreak is
  MANDATORY for a deterministic ordinal" (`2026-08-31-part2-phase4-plan.md:114`).
  Document this precondition on `BoundedExternalSorter`.
- **Partition-invariance.** The emitted order is independent of `run_bytes_cap`
  and `merge_fan_in`: those change how many runs/passes occur, not the result.
  The fail-closed cutoff guarantees a globally correct merge for any fan-in. This
  is the property that makes the sorter a drop-in for an in-memory sort.

## Byte-parity + memory-cap target (verifiable now, no route)

- **Parity oracle:** an in-memory `table.sort_by(sort_key_column)` (equivalently
  `sorted(...)` on a unique key). A shuffled input stream through the sorter
  (`write*` → `finish` → `iter_ordered`) must reproduce it **byte-for-byte**,
  for int AND at least one non-int key type (string/binary) — the added coverage
  the generalization owes. `test_ooc_external_sort.py::test_shuffled_input_is_
  sorted_by_row_nr` is the template.
- **Memory-cap proofs (ported):** `peak_pre_sort_buffer_bytes <= run_bytes_cap`;
  `peak_buffered_bytes <= run_bytes_cap * SORT_OVERHEAD_FACTOR`;
  `peak_merge_resident_bytes <= run_bytes_cap` across a forced multi-pass merge
  (`> merge_fan_in` runs); every emitted batch `nbytes <= run_bytes_cap`.
- **Partition-invariance proof:** the same input at a huge cap and a tiny cap
  (forcing many runs/passes) yields the identical ordered output — extend the
  existing tiny-cap test to assert equality against the huge-cap run, not only
  `list(range(n))`.
- **Fail-closed proofs (ported):** an over-cap single row raises
  `out_of_core_sort_row_too_wide`; `run_bytes_cap <= 0` / `merge_fan_in < 2` raise
  `out_of_core_reorder_budget_too_small`; `close()` removes all run files
  idempotently.
- **Subprocess RSS proof (ported):** a fresh allocator-pinned subprocess streams
  ~4,000,000 variable-width rows through a fixed ceiling and asserts real VmHWM
  stays within ~1.35x — the never-OOM proof. Keep it in `tests/perf/` (mark it so
  it runs where perf tests run; do not make it a default-suite gate if perf tests
  are opt-in — confirm the repo's perf-test invocation).

## Tasks

- [ ] **Task 1: Port the four files verbatim.** `git show` each from
  `feat/ooc-b-external-reorder`, land on `feat/native-phase3` unchanged, confirm
  imports resolve on this branch (the `_errors`/`ExecutionError` codes,
  `_reorder_budget` deps). Run the ported unit tests green as-is (int key) before
  touching anything.
- [ ] **Task 2: Key-generalize.** Rename `ExternalRowNrSorter` →
  `BoundedExternalSorter`, `row_nr_column` → `sort_key_column`; make
  `_RunHead.max_value` type-generic (`-> Any`); confirm `_flush` /
  merge-cutoff use the renamed column and no other int assumption remains. Update
  the ported tests to the new names. Document the total-key (unique/tiebreak)
  precondition and partition-invariance on the class docstring; cite Knuth §5.4
  and the OOC-B provenance.
- [ ] **Task 3: Generalization tests.** Add byte-parity for a non-int key
  (string and/or binary) against the in-memory `sort_by` oracle; add the explicit
  partition-invariance test (tiny-cap output == huge-cap output). Keep every
  ported proof green under the new names.
- [ ] **Task 4: Lint + mutation + sentry.** ruff check + format on the diff;
  mypy on the diff where runnable (numpy-2.5.0 aborts locally on py3.13 — CI py3.10
  covers it). Mutation-grade the sorter's LOGIC (the flush-before-overflow guard,
  the fail-closed cutoff / contiguity, the budget validation) to 0 unresolved-logic
  survivors (message prose adjudicated per the ledger policy); write the mutation
  ledger. Module-size sentry green (`_external_sort.py` ~428 LOC + the small
  generalization stays under the 600 cap; confirm).

## Non-goals (explicitly deferred)

- Route wiring / whole-stream order-restore reland (P4-A.3).
- Multi-column `sort_keys` tuple-cutoff (deferred to its first real consumer,
  `grouped_series` P4-E, so the fail-closed contiguity invariant is proven against
  a real key shape).
- `mask.shuffle` (oracle-deferred; a separate O(n)-disk exact-permutation slice).
- Any change to the live `ChildFkBatchJoiner` per-batch ordering (already bounded).

## Acceptance

- `BoundedExternalSorter` exists on `feat/native-phase3`, sorts a batch stream by
  an arbitrary orderable key column byte-identically to an in-memory `sort_by`
  (int + at least one non-int key type), and never alters a value.
- All ported memory-cap / fail-closed / RSS proofs pass under the new names;
  partition-invariance (tiny-cap == huge-cap output) is proven.
- The total-key (unique/tiebreak) precondition and partition-invariance are
  documented on the class; the multi-column form and route wiring are recorded as
  deferred (A.3 / P4-E).
- ruff + mypy(diff, where runnable) clean; module-size sentry green; 0
  unresolved-logic mutation survivors on the sorter's guards/cutoff/budget; ledger
  written.
- Held on `feat/native-phase3`; no merge.

## Risks the plan-gate should weigh

1. **Is the single-key generalization the right scope, or should A.2 ship the
   multi-column tuple-cutoff now?** This plan defers multi-column to its first
   consumer to avoid proving the tuple contiguity invariant speculatively. The
   gate should confirm the near-term consumers (order-restore int key;
   grouped_series via pre-encoded key) are genuinely served by a single-key
   sorter, or push back if pre-encoding is unreasonable for grouped_series.
2. **`max_value`/cutoff type-genericity.** Confirm `pc.max` / `pc.min` /
   `pc.less_equal` behave identically for the non-int key types the tests add
   (nulls in the key column? the key should be non-null; assert/validate).
3. **Perf-test placement.** The subprocess RSS proof must run where perf tests
   run and not silently no-op; confirm the repo's perf invocation and that it is
   not a flaky default-suite gate.
