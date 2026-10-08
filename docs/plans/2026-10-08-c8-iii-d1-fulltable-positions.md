Status: plan (rev 2)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, code-review.

# C8-iii-d-1: positional draws under `when:` keyed on the full-table row (oracle)

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, the C8-iii split (C8-iii-a merged #223; C8-iii-b and C8-iii-c are built). Branch `feat/c8-iii-d1-fulltable-positions` off engine main `4a08f570`.

Owner decision (Cam, 2026-10-07): under `when:`, positional draws key on the FULL-TABLE row number, not on the position inside the selected subset. This is an approved pre-GA output change. Risk R2: a deliberate output change on a default path.

C8-iii-d is split in two:
- **d-1 (this plan):** changes the keying on the oracle, the only route that runs positional strategies under `when:` today.
- **d-2 (later):** lifts the chunked rejections and native declines, and carries positions through `run_kernel_step_masked`. It needs d-1's keying as its parity target.

## 1. Problem

Three strategies key each row's draw on its position:

| Strategy | Key today | Code |
|---|---|---|
| non-deterministic categorical (C1b) | `encode_int(ctx.row_offset + i)` | `execution/_strategies/_categorical.py:187-193` |
| non-deterministic Faker REUSE (C5b) | `positional_pool_indices(n, row_offset=ctx.row_offset, ...)` | `execution/_strategies/_faker.py:98-108`, `execution/_positional_keys.py:19-26` |
| windowed_date | `enumerate(..., start=row_offset)` | `transforms/windowed_date.py:216`, `execution/_strategies/_windowed_date.py:59-66` |

Under `when:`, `run_with_when_gate` (`execution/_when_gate.py:210-213`) passes the handler the selected subset `df.loc[mask]`, so `i` is the match ordinal `0..k-1`. The same row therefore gets a different value depending on which other rows the predicate selects. Changing the predicate, or adding rows to the table, silently changes the output for rows whose own values did not change. It also means no chunked or native route can reproduce the oracle, which is why all three are rejected or declined under `when:` (`execution/_chunked_dgrn.py:30-41`).

The gate already knows the full-table positions: `selected_positions(mask)` (`execution/_exact_int_faker.py:87-92`). `gated_context` passes them on as `ctx.gate_positions`, but only for exact-integer Faker columns (`:95-110`).

## 2. Decision

**2a. One owner for row positions.** Add `row_positions(ctx, n) -> np.ndarray[uint64]` to `execution/_positional_keys.py`:
- it returns `ctx.row_offset + ctx.gate_positions` when the handler runs under a gate (and `len(gate_positions) == n`);
- otherwise it returns `ctx.row_offset + arange(n)`, today's contiguous range;
- it raises the existing domain error when a position leaves the uint64 domain.
- **Scalar conversion (rev 2, Codex round 1 finding 3).** The categorical encoder (`encode_int`) and windowed_date (`i.to_bytes(...)`) need a PYTHON `int`, which `np.uint64` is not (`np.uint64(3).bit_length()` and `.to_bytes(...)` both fail). So the per-row scalar is converted to Python `int` at the point it is encoded or serialized. The Faker path, which already uses a uint64 array via `positional_key_array`, keeps the array form. The byte encoding is unchanged; test 3 pins it against base `4a08f570`.

All three positional handlers key from it. Nothing else computes a position.

**2b. The gate always carries positions, EXCEPT into nested children.**
- `gated_context` sets `gate_positions` for every gated call, not only exact-integer columns.
- `sampling_source` keeps reading it only when an exact source is registered, so its behavior is unchanged.
- **Nested isolation (rev 2, Codex round 1 finding 1).** A nested child's position is a LEAF ordinal, not an outer row. `_strategies/_nested.py` forwards the outer context to the child, so the child must NOT consume the outer `gate_positions`. The nested dispatch clears `gate_positions` (sets it to None) on the child context before calling the child handler, so a child positional strategy keeps its existing `row_offset + leaf_ordinal` keying. Without this, a child would miskey or fail the length check (`n` leaves vs the outer gated count). Changing nested semantics stays deferred; preserving them is required here.
- The sinks stay shared, as today.

**2c. Kernels take positions, not an offset.** These three gain an explicit positions argument used for the key:
- `positional_pool_indices`;
- the categorical non-deterministic loop;
- `apply_windowed_date`.

When there is no gate, positions equal today's contiguous range, so the output is byte-identical for every call without `when:`, including chunked calls with `row_offset`. The existing `row_offset` callers (chunked oracle and native) keep passing the contiguous form.

**2d. The resulting property (narrowed, Codex round 1 finding 2).** For the three TOP-LEVEL positional strategies, with identical handler inputs and config except the target gate: a selected row `r`'s output equals that row's output in the unfiltered run WHEN THAT UNFILTERED RESULT EXISTS. Unselected rows stay raw. Null TARGET values stay covered.
- windowed_date raises on a null or invalid ANCHOR (`NaTType does not support strftime`). A full-table run with such an anchor fails, but a `when:` predicate that excludes those rows succeeds. So the equality fixtures use valid anchors, and a SEPARATE test pins that excluding null/invalid anchors still succeeds while the unfiltered run still fails.
- Nested children and stream strategies are explicitly outside this property.
- This is the parity target d-2 will reuse.

**2e. Out of scope, recorded:**
- nested children: their positional key is a LEAF ordinal, not a row position, and they are unchanged;
- order-dependent stream strategies (shuffle, Faker UNIQUE/MATCH/SCALE, joint_mask non-key draws): they depend on the subset by construction, and are unchanged;
- every chunked rejection and native or unified decline: unchanged in d-1, lifted in d-2.

## 3. Acceptance tests (written first; never weakened)

1. **Full-table invariance (the core property, per 2d).** For each of the three strategies, over a 50-row table with assorted predicates (none selected, all selected, every other row, a contiguous block, a predicate on another column):
   - every selected row's output equals the same row's output in the run without `when:`, WHERE the unfiltered result exists (categorical and Faker use source nulls freely; windowed_date equality fixtures use valid anchors);
   - unselected rows are byte-identical to the source;
   - null target values stay covered.

   Run on the full-frame oracle, sequential, multi-table and generate-plus-mask (the masking side). Also cover a nonzero `row_offset` caller: the adapter `run(..., row_offset=k)`. In that case, positions equal `k` plus the full-table index.
1a. **windowed_date null-anchor split.** A table with a null/invalid anchor: the unfiltered run raises (`NaTType does not support strftime`); a `when:` predicate excluding that row succeeds and the surviving rows satisfy property 1.
1b. **Nested isolation.** A gated nested categorical and a gated nested Faker, with multiple leaves, unmatched paths and null leaves, produce output byte-identical to main (nested keying unchanged).
2. **Predicate independence.** Two predicates that both select row `r` give `r` the same value.
3. **No change without `when:`.** Byte-identical output to main for all three strategies, on full-frame, chunked-oracle and native routes, for representative configs. Testflight fingerprints unchanged (no golden uses `when:` with these strategies; STOP if a fingerprint moves).
4. **Unchanged declines.** Every existing chunked rejection code and native or unified decline for these strategies under `when:` is still raised, with the same codes.
5. **`gate_positions` contract.**
   - `sampling_source` still keys exact-integer Faker correctly under a gate.
   - A non-exact column now receives positions.
   - The existing mutation-kill pin (`tests/unit/execution/test_when_gate_mutation_kills.py:265-306`) is updated only where it asserted `None` for non-exact columns. The record notes the reason.
6. **Old pins, updated with a recorded reason and the new expected values derived from property 1** (never hand-edited numbers):
   - `tests/unit/execution/test_categorical_seeded_nondet.py:347-420`;
   - `tests/unit/execution/test_faker_positional_nondet.py:923-933`;
   - any windowed_date `when` pin the builder finds.

   The builder lists every changed test before implementing.
7. **Domain.** A gated position near the uint64 limit raises the existing domain error code.
8. **Sentries; mutation** on `row_positions`, the gate change, the nested `gate_positions` clear, the scalar int conversion, and each kernel's use of positions.
9. **Docs.** A CHANGELOG entry under "Changed (pre-GA output)": what changes, why, and that values for gated rows now equal the unfiltered run's. Update the `when:` section of `docs/strategies.md` and the C1b/C5b/windowed_date sections.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| Output changes for a job without `when:` | 2c keeps the contiguous form when there is no gate; test 3 and testflight |
| A handler computes its own position and drifts | 2a single owner; mutation on each kernel's use |
| Exact-integer Faker breaks when every gated call carries positions | `sampling_source` unchanged; test 5 |
| d-2 later disagrees with d-1 | Property 2d is the parity target, pinned by test 1 |

Rollback: revert the merge commit.

## 5. Review log

- **Codex plan gate, round 1: REVISE** (3 MEDIUM; no double-count route, split and exact-source safety confirmed; a 50-row probe found 17/20/25 mismatches, so the core test can fail). Rev 2:
  - nested children must not consume outer `gate_positions`; dispatch clears it; test 1b (finding 1);
  - the invariance property holds only where the unfiltered result exists; windowed_date null-anchor split, test 1a (finding 2);
  - positions convert to Python `int` before `encode_int`/`to_bytes`; byte encoding pinned against base (finding 3).
