Status: record

# C8-ii build record: `when:` on the unified full-frame route

Plan: `docs/plans/2026-10-07-c8-ii-unified-when.md` (rev 2, Codex plan gate GO). Branch `feat/c8-ii-unified-when` off engine main `9bbde63c`. Not pushed, not merged.

## What was built

- **Admission** (`execution/_unified_slice_when.py`, called from `cheap_admission`). The blanket `when:` decline is gone. A table with a `when` column is admitted when every such column passes `when_native_rejection` (reused, same config, registry and source schema as the chunked route), reads only columns the source has, and the table has no two names that `pandas.core.computation.parsing.clean_column_name` maps to one key. `cheap_admission` gained an optional `registry` argument, passed from `maybe_run_unified_slice`.
- **Mask** (`compute_when_masks`). For each node whose binding carries a predicate, the oracle's own `_eval_predicate` runs once on `candidate.source_frame` (the full frame). The result is kept as a NumPy `bool` array (pandas write-back) and a `pa.bool_()` array (kernel slicing and filtering) from the same values, nulls unselected. A length mismatch raises `UnifiedSliceInvariantError`. The call sits before the coordinator in `_execute_admitted`, inside the existing `try`, so any exception declines the table before a node runs.
- **Binding** (`ExecutionBinding.when_expression`). Set at bind time from the plan slice for hash, redact, truncate and categorical only.
- **Execution.** `ShadowCoordinator.run(..., when_masks=...)` slices the mask per batch (`batch_when_mask`) and `run_operator` calls `run_kernel_step_masked`. `run_operator` raises `ShadowDifference(operator-invariant-violation)` for a binding with a predicate and no mask, so a `when` node can never run unmasked, whichever caller reaches it. `OperatorCallEvidence.rows_selected` sums the selected rows per batch.
- **Reconstruction** (`_write_back_when`). For a `when` node it replays the gate's write-back literally: `sub = frame.loc[mask].copy(); sub[col] = kernel values at selected rows; frame.loc[mask, col] = sub[col]`, and does nothing when no row is selected.
- **Evidence.** A node with `rows_selected == 0` is exempt from the positive-kernel check. A node that never counted selection (`rows_selected is None`) keeps the strict check.
- **Docs.** CHANGELOG entry and the `when:` section of `docs/strategies.md`.

## Judgment calls

1. **Guard in `run_operator`, not the coordinator loop.** The plan says the coordinator raises. `_shadow_coordinator.py` is at 597 lines, so the check lives one level down where it also protects any other caller. The coordinator still hands the mask in. Code reused: `OPERATOR_INVARIANT_VIOLATION`, so the shadow-difference catalog is unchanged.
2. **Module size without a census entry.** To stay under 600 the coordinator's row-error rebase moved to `rebase_row_errors` in `_shadow_assembly.py` (a pure move, behavior unchanged), and the mask slicing is `batch_when_mask` there. `_unified_slice.py` ends at 599 lines. No census entry added.
3. **`WhenMasks` carries both forms.** One object with `.selected` and `.arrow` keeps the driver change to three lines. The coordinator still receives a `Mapping[node_id, pa.Array]` as planned.
4. **Evidence exemption keyed on `rows_selected`, not on the binding.** Only a masked run ever sets the field, so a node without a mask cannot reach the exemption. Hand-built bindings in existing evidence tests carry no `when_expression`, and this keeps them working.
5. **`compute_when_masks` takes `Iterable[Any]`.** Importing `PhysicalNode` for the annotation would put the new module on the physical-seam import sentry's list. The sentry list was left alone.
6. **One existing test rewritten.** `test_cheap_admission_declines_when_gated_column` pinned the blanket veto this slice removes (a redact `when` over an int64 passthrough sibling). It now asserts admission, and a sibling test keeps a decline pinned for a strategy the verdict rejects (passthrough with `when`). No assertion about any other behavior changed.
7. **Decline reasons.** `cheap_admission` returns `None` without a code, as before, so no code was added. The `when_native_rejection` code is available to a future telemetry pass.
8. **Mask failure logging.** A failing predicate falls into the existing generic reroute log, which records the exception type and table name only. A test asserts no predicate text reaches the log.

## Tests

`tests/physical/test_c8_ii_unified_when.py`, 93 tests written first (73 failed against the old code for the right reason: the oracle ran where the lane was expected). Covers plan tests 1 to 9 and the admission units. Test 10 is the existing sentries. The numeric-reference decline cases assert the decline: a plain Arrow `int64` with nulls (round-trip check) and a float passthrough sibling (resident type domain).

Competing-failure cases (test 6) use a rebound Faker provider that fails pool build, with both work orders and both config orders.

Suite counts on the final code tree (head `0b92ed6d`; the later commits touch only a sentry allowlist entry and docs), Python 3.11 with the Rust companion (`pytest-one`, `-p no:randomly`):

| Suite | Result |
|---|---|
| tests/physical | 1778 passed, 1 skipped |
| tests/native | 5922 passed, 1 skipped, 4 warnings |
| tests/unit/execution | 6386 passed, 4 skipped, 44 warnings |
| tests/parity | 352 passed, 6 skipped, 59 xfailed |
| tests/perf | 15 passed, 10 deselected |
| tests/sentry (3.11) | 2425 passed, 1 skipped |
| tests/sentry (3.10) | 2425 passed, 1 skipped |
| c8-ii file on 3.10 | 57 passed, 36 skipped (no companion: hash and categorical cases skip) |

`ruff check src tests`, `ruff format --check src tests` and `mypy src` (3.10 mirror venv) are clean. The testflight (`scripts/test_flight.py`) passed all 53 checks and `FINGERPRINTS: 5/5 match golden`; no golden was re-recorded.

## Mutation

Hand mutation of the changed units, run against the C8-ii file, the admission tests and the evidence unit tests.

| Mutant | Result |
|---|---|
| admit: drop collision check | killed (test 4 collision) |
| admit: collision key = raw name | killed (test 4 collision) |
| admit: drop `when_native_rejection` | killed (test 3) |
| admit: drop refs-in-source | killed (admission unit; end to end the mask failure declines too) |
| admit: no-`when` table not admitted | killed (test 4 no-collision-effect, test 8) |
| mask: nulls selected | killed (nullable-int cases) |
| mask: arrow mask inverted | killed (matrix) |
| mask: every bound node gets a mask | killed (matrix) |
| mask: drop length check | killed (wrong-length unit) |
| mask: batch slice ignores offset | killed (ragged batches) |
| mask: missing node still sliced | killed (matrix) |
| guard: drop fail-closed check | killed (test 5) |
| guard: binding never carries the predicate | killed (matrix) |
| guard: masked step unused | killed (zero selectivity) |
| write-back: no filter | killed (matrix) |
| write-back: whole-column assign | killed (matrix) |
| write-back: `when` nodes use the old overlay | killed (StringDtype sidecar cases) |
| write-back: missing mask tolerated | killed (missing-mask unit) |
| write-back: drop the empty-selection early return | survived, equivalent (below) |
| evidence: exemption dropped | killed (zero-selected matrix) |
| evidence: exemption for any masked node | killed (test 7) |
| evidence: `rows_selected` not summed | killed (test 7 batches) |
| driver: masks not passed to the coordinator | killed (matrix) |
| driver: selected masks not passed to reconstruction | killed (matrix) |

**Equivalent mutant.** With no row selected, `frame.loc[mask].copy()` is empty, `sub[col] = []` and `frame.loc[mask, col] = sub[col]` write nothing. Checked for `object` and `string` frame dtypes (the only dtypes an Arrow `string` target reads as): the frame is `equals`-identical and keeps its dtype. The early return mirrors the oracle's `if not mask.any()` and costs nothing, so it stays.

## Perf record (not a gate)

1M rows, a hash column with `when: s == 'x'`, `auto_chunk=False` on both arms (above 100k rows the default routes to the chunked lane), best of three, native companion.

| Selectivity | lane-off (pandas) | lane-on (unified) | Ratio |
|---|---|---|---|
| 1% | 0.42 s | 1.49 s | 0.28x (lane slower) |
| 50% | 5.01 s | 2.08 s | 2.40x |
| 100% | 9.62 s | 3.14 s | 3.06x |

At 1% the lane is slower because `run_kernel_step_masked` runs the kernel over every row and keeps the selected ones, while the oracle hashes only the selected subset. The break-even is somewhere below 50%. Output is byte-identical at all three points. A selective-kernel variant (filter before the kernel, scatter after) would fix the low-selectivity case; it is not in this slice.

## Not done

- The shadow and mixed harnesses were not extended. They reach the coordinator without masks, so a `when` node there raises through the fail-closed guard. That is tested through the coordinator directly, not through a mixed generate+mask run.
- Roadmap and shipped-log updates are for merge time, per the plan.
- No new decline code was needed.
- The physical-seam sentry's permitted-exceptions list gained `_unified_slice_when.py` (it lists every changed execution module versus main).
