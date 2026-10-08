Status: plan (rev 1)

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
- it returns `ctx.row_offset + ctx.gate_positions` when the handler runs under a gate;
- otherwise it returns `ctx.row_offset + arange(n)`, today's contiguous range;
- it raises the existing domain error when a position leaves the uint64 domain.

All three positional handlers key from it. Nothing else computes a position.

**2b. The gate always carries positions.**
- `gated_context` sets `gate_positions` for every gated call, not only exact-integer columns.
- `sampling_source` keeps reading it only when an exact source is registered, so its behavior is unchanged.
- The sinks stay shared, as today.

**2c. Kernels take positions, not an offset.** These three gain an explicit positions argument used for the key:
- `positional_pool_indices`;
- the categorical non-deterministic loop;
- `apply_windowed_date`.

When there is no gate, positions equal today's contiguous range, so the output is byte-identical for every call without `when:`, including chunked calls with `row_offset`. The existing `row_offset` callers (chunked oracle and native) keep passing the contiguous form.

**2d. The resulting property.** For a selected row `r`, the output equals that row's output in the same job run without `when:`. Unselected rows stay raw. This holds for any predicate, and it is the parity target d-2 will reuse.

**2e. Out of scope, recorded:**
- nested children: their positional key is a LEAF ordinal, not a row position, and they are unchanged;
- order-dependent stream strategies (shuffle, Faker UNIQUE/MATCH/SCALE, joint_mask non-key draws): they depend on the subset by construction, and are unchanged;
- every chunked rejection and native or unified decline: unchanged in d-1, lifted in d-2.

## 3. Acceptance tests (written first; never weakened)

1. **Full-table invariance (the core property).** For each of the three strategies, over a 50-row table with nulls in the source and assorted predicates (none selected, all selected, every other row, a contiguous block, a predicate on another column):
   - every selected row's output equals the same row's output in the run without `when:`;
   - unselected rows are byte-identical to the source.

   Run on the full-frame oracle, sequential, multi-table and generate-plus-mask (the masking side). Also cover a nonzero `row_offset` caller: the adapter `run(..., row_offset=k)`. In that case, positions equal `k` plus the full-table index.
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
8. **Sentries; mutation** on `row_positions`, the gate change and each kernel's use of positions.
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

(none yet)
