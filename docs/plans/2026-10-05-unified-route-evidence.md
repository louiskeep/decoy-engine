Status: plan (revision 1, author = Opus). Awaiting Codex plan gate.

# Unified-slice per-column route evidence

Program: `docs/plans/2026-09-30-rust-engine-program.md`. This is the route-evidence obligation (line ~22: every positive fixture asserts, per column, planned backend, executed backend, call count, elapsed time, and zero unintended oracle fallbacks), applied to the full-frame unified slice. C5a's plan carried it as section 3h until Cam moved it out at the review cap on 2026-10-05. C5a's Codex round-3 findings (HIGH 3, MEDIUM 4) are this slice's acceptance inputs.

Branch: `feat/unified-route-evidence` off engine main `98f6c03e` (C5a merged).

## 1. Goal and scope

Every node entry in the unified slice's published evidence (`quality_metrics["unified_slice_activation"]["nodes"]`, built in `_unified_slice.py` ~:286-316) gains four keys:
- `planned_backend`
- `executed_backend`
- `calls`
- `elapsed_ms`

The values come from the same vocabulary and the same idle rule the chunked route already publishes. Existing keys stay. Two related truth fixes are in scope:

- **bucket_perturb and date_shift report compiled work truthfully.** Today the full-frame operator sets `compiled_kernel_executed = True` unconditionally (`physical/_shadow_operators.py` ~:257 and ~:297). Their helpers make zero compiled calls on empty, all-null or all-unparseable input. Both helpers already accept a `derive_calls` spy (`native/_bucket_perturb_ext.py:188,249-252`; `native/_date_shift_ext.py:133,181-182`); C2 and C4 added it for the chunked route.
- **The owed `_unified_slice.py` split.** The census note in `tests/sentry/test_module_size.py` says the next unified-slice change owes moving the completed-evidence validation and the source-shaped output reconstruction out of `_execute_admitted`. This slice rewrites the evidence block anyway, so the move happens here.

Out of scope:
- Any routing or output change. Outputs, warnings, errors and row_errors stay byte-identical.
- The chunked route's evidence, which is already compliant.
- The platform: its pinned reader checks only `activated` (`decoy-platform api/jobs/route_evidence.py:257`).
- The pre-existing finalize row-error reroute ticket (a separate slice).

## 2. Established facts (engine `98f6c03e`)

1. **Vocabulary.** `native/_chunked_evidence.py:32-35` defines `RUST_COMPANION`, `RUST_POOL_SELECT`, `ARROW_PYTHON` and `PANDAS_ORACLE`. The chunked rule is `_executed_backend(plan, native_admitted, idle)`: not admitted → `pandas_oracle`; idle column → `arrow_python`; otherwise the planned backend (~:106).
2. **Operator evidence today** (`physical/_shadow_operators.py`, `OperatorCallEvidence` ~:112-128: `executed`, `compiled_kernel_executed`, `batches_run`):

   | Operator | `compiled_kernel_executed` today | Kernel called on an empty batch? |
   |---|---|---|
   | hash | always True | yes, the crypto kernel |
   | Faker | always True | yes, `sample_faker_array` always calls `derive_index_batch` (Codex C5a round 1) |
   | categorical | always True | yes, `_categorical_ext` calls `derive_index_batch` unconditionally (~:36) |
   | bucket_perturb | always True | NO, the helper short-circuits; this is a lie today |
   | date_shift | always True | NO, the helper short-circuits; this is a lie today |
   | group_key | `sum(derive_calls) > 0` | already truthful (empty is False; all-null hashes `"None"`, so it is NOT idle) |
   | redact, truncate, passthrough | stays False | none; they run as Arrow/Python |

   `batches_run` increments once per operator invocation (~:302).
3. **Timing.** The coordinator wraps each node's whole run in ONE `timed_strategy(node.strategy, ",".join(node.columns))` scope (`_shadow_coordinator.py:317`). That produces one `StrategyTimingRecord(strategy_type, column, elapsed_ms, peak_memory_delta_kb)` per node (`instrumentation/timing.py:66`). Records carry no node id. Admitted unified nodes are scalar, single-column and unique per table, so `(strategy, column)` identifies a node.
4. **D7.** Hash and Faker must show `compiled_kernel_executed=True` (`_unified_slice.py`, positive-kernel set); a miss raises `UnifiedSliceInvariantError`.
5. **Consumers.** No engine code outside tests reads the `nodes` dict. Engine tests read individual fields. The platform reads `activated` only.

## 3. Decisions

**3a. Planned backend per node,** keyed on the admitted binding's operator id:
- `native_keyed_hash`, `native_categorical`, `native_bucket_perturb`, `native_date_shift`, `native_group_key` → `rust_companion`
- `native_faker_select` → `rust_pool_select`
- `native_redact`, `native_truncate`, `native_passthrough` → `arrow_python`

Import the constants from `_chunked_evidence`; do not restate the strings. Put the operator-to-backend map next to the admission tables in `_unified_slice_admission.py`, with a sentry test asserting it covers `ALLOWED_OPERATOR_IDS` exactly. A new admitted operator must then declare its backend.

**3b. Executed backend per node.**
- Companion and pool operators: the planned backend if `compiled_kernel_executed`, otherwise `arrow_python`. This is the chunked idle rule, factored into one shared helper used by both routes, not re-implemented.
- Arrow operators: `arrow_python`.
- Kernel-idle work is NOT an invariant failure, apart from D7 (hash and Faker, which always call their kernel).

**3c. Truthful bucket_perturb and date_shift.** `run_operator` passes a `derive_calls` list to `native_bucket_perturb` / `native_date_shift`, the same way the group_key branch does, and sets `compiled_kernel_executed = sum(derive_calls) > 0`. The kernels, outputs and the chunked route are untouched.

**3d. `calls`** = `OperatorCallEvidence.batches_run`: the operator's batch invocations. The program's "call count" means invocations of the column's operator; compiled-kernel call counts stay internal. This matches the chunked route's per-chunk invocation semantics.

**3e. `elapsed_ms`** comes from joining the collector's records on `(strategy_type, column)`. Every admitted node must have exactly one record, and every record must belong to an admitted node (a bijection). A violation raises `UnifiedSliceInvariantError`, consistent with D7. Admission makes duplicates impossible, so a violation means a bug. `elapsed_ms` is the record's float, rounded to 3 decimals for JSON stability; non-negative.

**3f. One pure evidence-assembly helper** (also the owed split). New sibling module `execution/_unified_slice_evidence.py` holding:

```
assemble_node_evidence(nodes, route_evidence, timing_records) -> dict[str, dict[str, Any]]
```

It is pure, takes no context, and raises `UnifiedSliceInvariantError` on:
- a missing or not-executed node
- an operator mismatch
- a D7 miss
- a timing-bijection miss

It returns the JSON-safe dict. `_execute_admitted` calls it in place of the inline loop. The existing completed-evidence validation (~:286-316) moves into it unchanged in behavior. Unit-testing this helper directly is the seam C5a's round-3 MEDIUM 4 asked for. `ShadowRunResult` carries no timings, so the helper takes the collector's records explicitly.

**3g. Pure-move split of the output reconstruction.** Move the source-shaped output reconstruction block (`_execute_admitted`'s post-run overlay, ~:317-380) into a function in the same new module, or a second sibling if size requires it. This is a separate pure-move commit: byte-identical body apart from parameters and imports. Then lower the `_unified_slice.py` census entry to the new real size and delete the owed note.

## 4. Implementation

1. `native/_chunked_evidence.py`: expose the executed-backend rule as a public helper (it is now private `_executed_backend`), used by both routes.
2. `_unified_slice_admission.py`: the operator-to-backend map (3a) plus the coverage sentry.
3. `physical/_shadow_operators.py`: the `derive_calls` spies for bucket_perturb and date_shift (3c).
4. `_unified_slice_evidence.py` (new): `assemble_node_evidence` (3b/3d/3e/3f) and the moved reconstruction (3g).
5. `_unified_slice.py`: call the helpers; net size shrinks.
6. Sentry: census update; seam-disconnection lists if the new module needs them.
7. Docs: CHANGELOG (evidence keys added; bucket/date compiled-evidence truth fix); build record `docs/records/2026-10-05-unified-route-evidence-build.md`. The compatibility contract only if it lists evidence keys.

## 5. Acceptance tests (written first; red-before recorded)

1. **Exact evidence per operator.** One production-lane table per operator, plus a mixed table holding every admitted operator. Each node's dict equals exactly: `operator`, `executed=True`, `compiled_kernel_executed`, `planned_backend`, `executed_backend`, `calls`, and `elapsed_ms` (asserted as a float ≥ 0; not exact). Expected backends per 3a/3b. Output is unchanged versus main: same bytes as the lane-off oracle, and the oracle is poisoned during lane runs.
2. **Idle rules on the production lane:**
   - empty table: bucket_perturb, date_shift and group_key report `arrow_python` with `compiled_kernel_executed=False`
   - all-null and all-unparseable bucket_perturb / date_shift: `arrow_python`
   - all-null group_key: `rust_companion`
   - empty hash, categorical and Faker: their planned backend, with `compiled_kernel_executed=True`, and no invariant raised
3. **`calls`:** 50,001 rows across the default 50,000-row batch gives `calls == 2` for every node. A 1-row table gives `calls == 1`.
4. **`assemble_node_evidence` unit tests** (pure seam). Each of these raises `UnifiedSliceInvariantError`:
   - a missing node
   - a not-executed node
   - an operator mismatch
   - a hash or Faker D7 miss
   - a duplicate timing record
   - a missing timing record
   - an extra timing record for a non-admitted pair

   Kernel-idle bucket_perturb, date_shift and group_key does NOT raise. The JSON-safety check is `json.dumps` of the result.
5. **Shared rule:** a test proves the unified and chunked routes call the same executed-backend helper (monkeypatch it, and both routes observe the patched rule).
6. **Map coverage sentry:** the operator-to-backend map's keys equal `ALLOWED_OPERATOR_IDS`.
7. **Pure moves:** the reconstruction body is byte-identical (diff check in the build record), and `test_unified_slice_parity.py` stays green unchanged.
8. **Companion-absent clean env:** evidence tests are guarded with `@NEEDS_COMPANION` where native.

Mutation targets (each must be killed):
- map Faker to `rust_companion`
- treat idle as planned
- drop the bucket `derive_calls` spy (set True)
- raise on idle group_key
- report `calls` as a constant 1
- join timings by strategy only
- skip the bijection check
- drop D7

## 6. Risk, rollback, gates

- **Risk: R1-R2.** The change is additive evidence plus truthful flags; outputs are unchanged. The only behavior change is two evidence booleans becoming truthful on idle input. Rollback is a revert.
- **Gates:** Codex plan gate → Sonnet build (tests first) → dennis → Codex final → ci-mirror. Merge under the standing full-green authority.

## 7. Open questions for the plan gate

1. Is `calls` = operator batch invocations the right reading of the program's "call count", or should it count compiled-kernel invocations (where those differ, e.g. bucket_perturb's per-unique-size derives)?
2. Does any test or doc pin `compiled_kernel_executed=True` for an empty or all-null bucket_perturb / date_shift on the full-frame route (which 3c makes False)?
