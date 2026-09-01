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
3. `_RunHead.max_value(self) -> int`: `pc.max(pending[col]).as_py()` (`:173-176`)
   — the **only** int-typed surface. `pc.max` works on any orderable Arrow type;
   change the return annotation to `Any` (the value is only compared, never used
   as an int).
4. The merge cutoff: `cutoff = min(head.max_value() for head in active if not
   head.is_final_batch)` (a **Python `min()`** over the per-head `max_value()`
   scalars, `:372`) then `pc.less_equal(combined[col], cutoff)` (`:386`). Both the
   Python `min` and Arrow `pc.less_equal` are type-generic for a non-null orderable
   key, so the change is only the column name — PROVIDED the key contract below
   holds (a null in the key would make `pc.max`/`pc.less_equal` produce a `None`
   cutoff / null mask that silently drops rows). No `pc.min` is used.

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

### Key contract — validated fail-closed BEFORE spilling (not just documented)

The generic-key form is only correct for a **non-null total order**. The cutoff
math (`pc.max` → Python `min` → `pc.less_equal`) silently misbehaves otherwise: a
null key makes `pc.max`/`pc.less_equal` yield a `None` cutoff or a null filter
mask, and Arrow then **drops rows**. So the sorter validates the key on every
`write(batch)`, before the row is buffered or a run is spilled, and fails closed:

- **Supported key types (allowlist):** signed/unsigned integer, string,
  large_string, binary, date32/64, timestamp. The near-term consumers use int
  (`__decoy_row_nr`) and string; the allowlist is what the tests cover. An
  unsupported key type raises `out_of_core_sort_key_type_unsupported`.
- **Float keys rejected** (`out_of_core_sort_key_type_unsupported`): NaN has no
  total order and no consumer needs a float key. (If one ever does, define NaN
  ordering explicitly then.)
- **Null keys rejected:** any null in the key column raises
  `out_of_core_sort_key_null` before spilling. (A single `pc.any(pc.is_null(col))`
  check per batch — cheap, O(batch).)
- **Key type drift rejected:** the key column's Arrow type must match the first
  batch's on every subsequent batch (schema drift → mixed-type `min`/compare);
  a mismatch raises `out_of_core_sort_key_type_drift`.
- **Missing key column** raises `out_of_core_sort_key_missing`.

All are stable error codes with direct unit tests. Keeping validation at
`write`-time means a bad key is refused before any disk I/O, mirroring the
existing `out_of_core_sort_row_too_wide` fail-closed posture.

- **Uniqueness / stability.** The sorter does NOT rely on Arrow `sort_by` being
  stable across the multi-pass merge (a run collapse reshuffles rows). For a
  deterministic output the **sort key must be total (unique per row)** — the
  caller bakes any needed tiebreak into the key column. `__decoy_row_nr` is
  already unique. `grouped_series` must append a stable tiebreak (e.g. the source
  row number) into its encoded key. The plan states this: "A stable tiebreak is
  MANDATORY for a deterministic ordinal" (`2026-08-31-part2-phase4-plan.md:114`).
  Document this precondition on `BoundedExternalSorter`. (Non-unique keys still
  sort correctly by key; only the tie ORDER among equal keys is unspecified — the
  validation does not reject duplicates, it is the caller's determinism contract.)
- **Partition-invariance.** For a non-null total ascending key the emitted order
  is independent of `run_bytes_cap` and `merge_fan_in`: those change how many
  runs/passes occur, not the result. The fail-closed cutoff is mathematically
  sufficient for a globally correct merge at any fan-in (confirmed at plan-gate).
  The tiny-cap == huge-cap test is regression EVIDENCE of this, not the proof
  itself.

## Byte-parity + memory-cap target (verifiable now, no route)

- **"Byte-parity" defined:** VALUE + SCHEMA parity of the fully reconstructed
  table, NOT batch-for-batch identity — the sorter's output batch boundaries
  legitimately vary with `run_bytes_cap`. The assertion is
  `pa.concat_batches(list(iter_ordered())) == table.sort_by(...)` compared as
  `.to_pydict()` + `.schema` (payload columns included, not just the key).
- **Parity oracle:** an in-memory `table.sort_by(sort_key_column)` on a payload
  table (key column + at least one non-key payload column). The shuffled input
  stream through the sorter (`write*` → `finish` → `iter_ordered`) must reproduce
  it. **One cheap positive parity case per allowlisted key type** — signed int,
  unsigned int, string, large_string, binary, date32/64, timestamp — each a small
  single-run assertion, so the whole promised allowlist is actually exercised, not
  just int/string.
- **The full 2-caps × 2-fan-ins multi-pass matrix uses the string key** (one type
  is enough to exercise the generic multi-pass cutoff; the per-type cases above
  cover breadth cheaply). **This matrix MUST force the generalized multi-pass merge**
  (HIGH from the gate): size the cap so the non-int run generates `> merge_fan_in`
  runs, and run the parity comparison across **at least two caps × two
  `merge_fan_in` values** — the risky generic cutoff logic lives only in the
  multi-pass path, so a single-run non-int test would not exercise it. Include a
  duplicate-key case (equal keys sort correctly by key even if tie order is
  unspecified).
- **Memory-cap proofs (ported):** `peak_pre_sort_buffer_bytes <= run_bytes_cap`;
  `peak_buffered_bytes <= run_bytes_cap * SORT_OVERHEAD_FACTOR`;
  `peak_merge_resident_bytes <= run_bytes_cap` across a forced multi-pass merge
  (`> merge_fan_in` runs); every emitted batch `nbytes <= run_bytes_cap`.
- **Partition-invariance proof:** the same input at a huge cap and a tiny cap
  (forcing many runs/passes) yields the identical ordered output — assert the
  full reconstructed table is equal, not only `list(range(n))`.
- **Key-contract proofs (new):** a null key, a float key, an unsupported key
  type, a key type that drifts between batches, and a missing key column each
  raise their stable error code at `write`-time before any run file is created
  (assert no spill file exists on the failed path).
- **Failure-cleanup proof (MEDIUM, round 2 tightened):** the sorter keeps a
  cleanup registry of EVERY run file it ever creates — the initial spilled runs
  AND every intermediate merge output of every pass. Each merge output path enters
  the registry BEFORE its file is opened and stays registered through success or
  failure (a completed pass may unlink only the *inputs* it has finished merging,
  never drop an un-consumed intermediate from the registry). So `close()` removes
  all remaining files regardless of where `finish()` failed. Do NOT rely on a
  local `unlink-on-exception in _merge_group` alone: it cleans only the failing
  group's output and leaks a prior successful group's intermediate held in local
  state. **Test:** inject a failure in the SECOND merge group after the first
  group has succeeded, then call `close()` and assert no sorter file remains.
- **Fail-closed proofs (ported):** an over-cap single row raises
  `out_of_core_sort_row_too_wide`; `run_bytes_cap <= 0` / `merge_fan_in < 2` raise
  `out_of_core_reorder_budget_too_small`; `close()` removes all run files
  idempotently.
- **Subprocess RSS proof (ported), placement decided:** a fresh allocator-pinned
  subprocess streams variable-width rows through a fixed ceiling and asserts real
  VmHWM stays within ~1.35x — the never-OOM proof. The repo's `perf` marker runs
  in the DEFAULT regression gate by deliberate choice (`pyproject.toml:364-365`),
  and OOC-B marked this test `pytest.mark.perf`; keep that mark so the never-OOM
  proof gates every run. Its cost (~180s, ~1.7 GB peak spill) is accepted as the
  price of the guarantee's proof; the builder MAY reduce the row count if CI
  time/disk demands it, provided the dataset still far exceeds the ceiling and
  forces a multi-pass merge (the envelope ratio, not the exact N, is the proof).
  Do NOT reclassify it to `benchmark` (that marker is excluded from the gate).

## Tasks

- [ ] **Task 1: Port the four files verbatim.** `git show` each from
  `feat/ooc-b-external-reorder`, land on `feat/native-phase3` unchanged, confirm
  imports resolve on this branch (the `_errors`/`ExecutionError` codes,
  `_reorder_budget` deps). Run the ported unit tests green as-is (int key) before
  touching anything.
- [ ] **Task 2: Key-generalize + validate the key contract.** Rename
  `ExternalRowNrSorter` → `BoundedExternalSorter`, `row_nr_column` →
  `sort_key_column`; make `_RunHead.max_value` type-generic (`-> Any`); confirm
  `_flush` / the Python-`min` merge cutoff use the renamed column and no other int
  assumption remains. Add the `write`-time key validation (before buffering/spill):
  supported-type allowlist, reject float, reject null keys, reject key type drift,
  reject missing key column — each a stable error code. Document the total-key
  (unique/tiebreak) precondition and partition-invariance on the class docstring;
  cite Knuth §5.4 and the OOC-B provenance.
- [ ] **Task 3: Generalization + robustness tests.** One cheap positive parity
  case per allowlisted key type (signed int, unsigned int, string, large_string,
  binary, date32/64, timestamp) against the in-memory `sort_by` oracle; the STRING
  key additionally run through the full multi-pass matrix — **forcing
  `> merge_fan_in` runs**, across ≥2 caps × ≥2 fan-ins, comparing the full
  reconstructed table (values + schema, payload columns included); a duplicate-key
  case; the partition-invariance test (tiny-cap == huge-cap full-table equality);
  the five key-contract rejections (assert no spill file created); and the
  failure-cleanup test (fail the second merge group after the first succeeds →
  `close()` leaves no run file). Keep every ported proof green under the new names.
- [ ] **Task 4: Failure-cleanup registry.** The sorter registers EVERY run file
  it creates (initial runs + all intermediate merge outputs of every pass) in a
  cleanup set the moment before the file is opened, and keeps it registered until
  `close()` unlinks it — a completed pass may delete only its consumed inputs,
  never an un-consumed intermediate. So `close()` removes every remaining file no
  matter where `finish()` failed (verified by the Task 3 test that fails the
  second merge group after the first succeeds). A bare unlink-on-exception in
  `_merge_group` is insufficient and is not the mechanism.
- [ ] **Task 5: Lint + mutation + sentry.** ruff check + format on the diff;
  mypy on the diff where runnable (numpy-2.5.0 aborts locally on py3.13 — CI py3.10
  covers it). Mutation-grade the sorter's LOGIC (the flush-before-overflow guard,
  the fail-closed cutoff / contiguity, the key-validation gates, the budget
  validation) to 0 unresolved-logic survivors (message prose adjudicated per the
  ledger policy); write the mutation ledger. Module-size sentry green
  (`_external_sort.py` ~428 LOC + the key validation stays under the 600 cap;
  confirm — if the validation pushes it over, extract the validators to a sibling
  or record a ratchet bump with justification).

## Non-goals (explicitly deferred)

- Route wiring / whole-stream order-restore reland (P4-A.3).
- Multi-column `sort_keys` tuple-cutoff (deferred to its first real consumer,
  `grouped_series` P4-E, so the fail-closed contiguity invariant is proven against
  a real key shape).
- `mask.shuffle` (oracle-deferred; a separate O(n)-disk exact-permutation slice).
- Any change to the live `ChildFkBatchJoiner` per-batch ordering (already bounded).

## Acceptance

- `BoundedExternalSorter` exists on `feat/native-phase3`, sorts a batch stream by
  a single non-null orderable key column to value+schema parity with an in-memory
  `sort_by`, and never alters a value. One positive parity case covers each
  allowlisted key type (int/uint/string/large_string/binary/date/timestamp); the
  string key additionally proves the generic multi-pass merge across ≥2 caps × ≥2
  fan-ins.
- A mid-`finish()` merge failure (second group fails after the first succeeds)
  leaks no run file: `close()` clears the sorter's cleanup registry of every run
  and intermediate it created.
- The key contract is enforced fail-closed at `write`-time (before any spill):
  null key, float key, unsupported type, key type drift, and missing key column
  each raise their stable error code with no run file created.
- All ported memory-cap / fail-closed / RSS proofs pass under the new names;
  partition-invariance (tiny-cap == huge-cap full-table equality) is proven; a
  mid-`finish()` merge failure leaks no run file.
- The total-key (unique/tiebreak) precondition and partition-invariance are
  documented on the class; the multi-column form and route wiring are recorded as
  deferred (A.3 / P4-E).
- The subprocess RSS proof stays `pytest.mark.perf` (runs in the default gate);
  its cost is accepted (N may be right-sized keeping the envelope proof).
- ruff + mypy(diff, where runnable) clean; module-size sentry green; 0
  unresolved-logic mutation survivors on the sorter's guards/cutoff/key-validation/
  budget; ledger written.
- Held on `feat/native-phase3`; no merge.

## Risks resolved at plan-gate (round 1)

The single-key scope is confirmed right-sized (order-restore int key directly;
grouped_series owns its order-preserving encoding / a native tuple extension in
P4-E). The fail-closed cutoff is mathematically sufficient for global ordering +
partition-invariance under a non-null total ascending key. The remaining build
risks are: (1) the key-validation must run BEFORE spilling and cover null/float/
unsupported/drift/missing (HIGH); (2) the non-int parity test must force the
multi-pass merge, not stay single-run (HIGH); (3) failure cleanup must track the
merge output path immediately (MEDIUM); (4) the RSS test stays `perf` in the
default gate (MEDIUM, accepted).
