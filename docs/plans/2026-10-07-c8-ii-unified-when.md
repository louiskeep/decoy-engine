Status: plan (rev 2, BUILD-READY: Codex plan gate GO in round 2)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, observability-and-resilience, code-review.

# C8-ii: `when:` on the unified full-frame route

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Follows C8-i (#214), which opened `when:` on the chunked native route and left the unified route declining every `when` column (`_unified_slice_admission.py:308-314`). Branch `feat/c8-ii-unified-when` off engine main `9bbde63c`. Risk R2: it changes which jobs the default-on unified lane takes, and output must stay byte-identical.

## 1. Goal and scope

A table whose `when:` columns all pass the C8-i column verdict runs on the unified route instead of the pandas oracle, with output, warnings, row errors and metrics identical to the oracle.

In scope:
- Admission: replace the blanket decline with the C8-i per-column verdict.
- The row mask: the oracle's own predicate function, evaluated once per table on the same frame the oracle evaluates.
- Execution: the masked kernel step per batch.
- Reconstruction: replay the oracle's subset write-back with the kernel's values.
- Evidence for a `when` node that selects no rows.

Out of scope:
- More operators (text_redact, date_shift, bucket_perturb, deterministic Faker), numeric targets and the raw-dict cleanup. These are C8-iii.
- Position-keyed operators, group_key, text_mask and code_set with `when`. They keep their rejections.
- The shadow and mixed harnesses. They keep declining `when`.
- Changing how the oracle evaluates `when`.

## 2. Established facts (main `9bbde63c`)

**Oracle** (`execution/_when_gate.py:161-217`, called from `_pandas_adapter.py:405-413`):
- The predicate is evaluated by `_eval_predicate(pdf, expr, strategy, column=)`, which runs `pdf.eval` (numexpr, empty scopes) on the LIVE table frame. Columns masked by earlier work nodes are seen post-mask.
- With zero matches, `df` comes back unchanged.
- Otherwise the handler runs on `sub = df.loc[mask].copy()`. Every admitted handler assigns a Python list to `sub[column]`:
  - hash, truncate and redact assign `masked.to_pylist()` (`_hash.py:59`, `_truncate.py:108`, `_redact.py:38`);
  - categorical assigns its `out` list (`_categorical.py:208`).

  Redact's fallback branch (`_redact.py:39-42`) runs only when `pa.array(col, from_pandas=True)` raises, which an object column of strings and nulls does not. All four return no warnings and record no row errors (`return df, []`), so the subset run contributes nothing but the column values. The gate then writes back `df.loc[mask, column] = sub[column]`, so non-matching rows keep their original values and the frame column keeps its dtype.
- A null in a nullable boolean mask is not selected.
- Error codes: `numexpr_required`, `when_expression_error`, `when_expression_not_boolean`.

**C8-i pieces to reuse:**
- `native/_when_admission.when_native_rejection` gives one verdict per column: strategy in {hash, redact, truncate, deterministic categorical} with its config gate; target source type `string`; predicate inside the closed grammar; every referenced column is the target or one no earlier work node writes.
- `native/_operator_step.run_kernel_step_masked` (`:387-416`): with no selected row it returns the source unchanged and `ran=False`. Otherwise it runs the kernel on every row and `if_else` keeps the source elsewhere.

**Unified route:**
- Admission builds `CheapCandidate.source_frame` (`_unified_slice_admission.py:166-186, 400-406`). This is the ONE conversion of the source, identical to the oracle adapter's `to_pandas_fk_safe` frame.
- The coordinator runs nodes `in_work_order` (`_runner.py:55`) over Arrow batches (`_shadow_coordinator.py:296-389`).
- The driver then calls `reconstruct_source_shaped_output` (`_unified_slice.py:287-293`; `_unified_slice_evidence.py:150-200`). It overlays each masked column's `to_pylist()` onto `source_frame` and converts with `pa.Table.from_pandas`.
- **The gap C8-i named:** a whole-column list overlay infers a fresh dtype. The oracle's `.loc` write-back keeps the frame column's dtype, for example `StringDtype` when the source carries a `b"pandas"` sidecar. The two can differ in dtype and in the output's pandas metadata.
- `assemble_node_evidence` (`_unified_slice_evidence.py:~105-120`) requires positive kernel evidence for hash and Faker nodes. The one exemption is a zero-row positional Faker (C5b-iii).

## 3. Decisions

**3a. Admission.**
- In `_unified_slice_admission.py`, replace the `_has_when_gate` decline with a per-column check. A table with a `when` column is admitted only if, for every such column:
  - `when_native_rejection(...)` returns None (the same function the chunked route uses, called with the same config, registry and source schema);
  - every referenced column is in `source.column_names`;
  - the target's resident type is `pa.string()`.
- A table is also declined when two of its column names map to the same pandas eval resolver key (pandas' `clean_column_name`, e.g. `a b` and `BACKTICK_QUOTED_STRING_a_b`). One column can then shadow another inside `eval`, and rule 4 only tracks physical names (Codex round 1 LOW).
- Any failure declines the whole table to the oracle, as today.
- The decline reason is recorded with the C8-i code where one exists. No new codes, unless the builder finds a case without one; that is listed in the record.

**3b. The mask: once per table, from the oracle's frame.**
- Before the coordinator runs, the unified driver computes, for each admitted `when` node, `mask = _eval_predicate(candidate.source_frame, expr, strategy, column=column)`. It evaluates on the FULL frame, not a projection, so pandas resolves names exactly as the oracle does.
- Each mask is kept in two forms built from the same values, with nulls unselected:
  - a non-null NumPy `bool` array for the pandas selection and write-back (3d);
  - a `pa.bool_()` array for kernel slicing and `filter`.

  Both must have the frame's length. A length mismatch raises `UnifiedSliceInvariantError` (Codex round 1 MEDIUM: an Arrow array cannot index `frame.loc`).
- This is the oracle's function on the oracle's own frame. Rule 4 of the verdict guarantees the referenced columns hold their source values at that node's turn, so the mask equals the oracle's by construction for any reference type, numeric included.
- No per-batch conversion is needed. That is why C8-i's numeric chunk-stability limit does not apply here.
- The masks travel to the coordinator as `when_masks: Mapping[node_id, pa.Array]`.
- **Mask failure declines.** If computing any mask raises, the driver declines the table before ANY node runs, and the oracle executes it from the start. The oracle then decides which error surfaces first, which matters when another node would fail earlier in work order, for example an admitted Faker whose pool build fails ahead of a later `when` column (Codex round 1 HIGH). The decline is logged by code only, with no predicate text. Test 6.

**3c. Execution: fail closed on a missing mask.**
- `ExecutionBinding` gains `when_expression: str | None`, set at bind time from the plan slice for the four admitted strategies.
- The binder does NOT start admitting `when` in shadow or mixed compilation: those callers pass no masks, and 3c's guard declines them.
- In the coordinator, a node whose binding has `when_expression` and no entry in `when_masks` raises `ShadowDifference`. A `when` node can never run unmasked.
- With a mask, each batch passes `mask.slice(row_offset, batch.num_rows)` and `run_operator` calls `run_kernel_step_masked`.
- Evidence counts `rows_selected` (summed per batch) next to `rows_seen`.

**3d. Reconstruction: replay the oracle's write-back.** For a `when` node, `reconstruct_source_shaped_output` does exactly what the gate does, with the kernel's values:

```
if mask_np.any():
    sub = frame.loc[mask_np].copy()
    sub[column] = masked_selected            # list of the kernel outputs at the selected rows
    frame.loc[mask_np, column] = sub[column]
```

- `masked_selected` is `masked_col.filter(mask_arrow).to_pylist()`. For the value-keyed operators over strings it equals the handler's output on the subset, row by row (C8-i design note).
- With no selected row, the frame column is left untouched, exactly as the oracle returns `df` unchanged.
- Non-`when` nodes keep today's overlay.
- Nodes are replayed in work order, so a later node sees the same frame the oracle would.

**3e. Evidence.**
- A `when` node with `rows_selected == 0` is exempt from the positive-kernel check, next to the positional Faker exemption.
- Any node with selected rows and no kernel evidence still raises.
- No new public evidence fields. `rows_selected` lives on the internal `OperatorCallEvidence` only.

**3f. Logs.** No predicate text or data values in any log line (log-hygiene rule; `tests/sentry/test_log_interpolation.py` must pass unchanged).

**3g. Docs.**
- CHANGELOG.
- `docs/strategies.md`: the `when:` section says the fast path covers both native routes.
- The roadmap and shipped log at merge.
- An intended output change is NOT expected. If any testflight fingerprint moves, stop and report it; do not re-record.

## 4. Acceptance tests (written first; no later contributor weakens them)

Every differential test compares the lane-on run against an explicit lane-off run, on:
- output tables byte-equal, including schema and `b"pandas"` metadata;
- quality warnings;
- row errors;
- every non-timing quality metric except the `unified_slice_activation` leaf, which exists only on the lane-on side. No other exclusion is allowed. The leaf is checked separately: node coverage, calls, actual backend, and idle evidence for a zero-selected hash node (Codex round 1 MEDIUM).

Cases are split in two, so a case cannot pass by falling back silently (Codex round 1 MEDIUM):
- **Admitted cases** assert activation, with the oracle fallback poisoned so any reroute fails the test.
- **Decline cases** assert the decline and the lane-off output.

1. **Differential matrix:**
   - the four strategies;
   - selectivity 0, partial and all;
   - target nulls selected (`c != 'x'` selects nulls) and unselected;
   - references, admitted: the target itself; a string sibling; `in` and `not in` lists; a nullable integer sibling carried as `Int64` through `b"pandas"` metadata; a nullable integer reached through a group_key-protected passthrough sibling;
   - references, declined: a float passthrough sibling (outside passthrough's unified resident types) and a plain Arrow int64 sibling with nulls (fails the round-trip check). The test asserts the decline. Widening the resident domain is out of scope;
   - a source with and without a `b"pandas"` StringDtype sidecar;
   - several batches (small `batch_size_rows`, ragged) and an empty table.
2. **Several `when` columns in one table**, including two that reference the same sibling, and a `when` column after a non-`when` column that it does not reference.
3. **Rule 4 counterexample:** an earlier node masks the referenced column. The table declines to the oracle and the output still equals lane-off.
4. **Declines:**
   - a reference not in the source;
   - a predicate outside the closed grammar (raw dict);
   - two column names that collide under pandas' resolver normalization, including a variant where an earlier node writes the aliasing column.
5. **Fail-closed:** a binding with `when_expression` and no mask raises `ShadowDifference`. The shadow and mixed harnesses still decline `when`.
6. **Error parity and order:**
   - a predicate that raises (the builder pins a real case, for example `z < 1` on a string column) declines before any node runs. The job raises the oracle's error with the same code and message;
   - competing failures: an earlier node that fails (for example an admitted Faker whose pool build fails) plus a later failing predicate. The job raises the oracle's FIRST error;
   - the same pair with config order and work order reversed.
7. **Evidence:**
   - a zero-selected hash node completes with idle evidence;
   - a selected hash node without kernel evidence still raises;
   - the positional Faker exemption is unchanged;
   - `rows_selected` sums across batches.
8. **Default-on switch:** an eligible `when` job now carries `unified_slice_activation` with identical output. Non-`when` jobs are unchanged.
9. **Reconstruction dtype pin:** with a StringDtype-sidecar source and partial selectivity, the lane-on output's pandas metadata equals lane-off's. This is the case the old overlay would get wrong.
10. **Sentries:** log interpolation, module size (census entries only at exact LOC), pandas-eval sites (no new `eval` site outside the gate).
11. **Perf record (not a gate):** 1M rows, hash, at 1%, 50% and 100% selectivity, lane-on against lane-off. Recorded in the build record.
12. **Mutation** on the changed units: admission check, mask handoff, fail-closed guard, write-back replay, evidence exemption. Equivalent mutants are argued in the record.

## 5. Failure modes and how each is closed

| Risk | Closed by |
|---|---|
| Dtype or metadata drift from a list overlay | 3d literal write-back replay; test 9 |
| Mask computed on a different frame than the oracle | 3b uses `candidate.source_frame` and the oracle's `_eval_predicate`; test 1 numeric references |
| Predicate reads a column an earlier node masked | Verdict rule 4; test 3 |
| A `when` node running unmasked | 3c fail-closed guard; test 5 |
| Zero-selected node tripping the evidence check | 3e scoped exemption; test 7 |
| Predicate errors differ, or surface in a different order | A mask failure declines before any node runs; test 6 |
| Arrow mask used as a pandas indexer | 3b keeps a NumPy mask for pandas; 3d uses it |
| A column name shadows another inside `eval` | Full-frame evaluation plus the collision decline; test 4 |
| A test passes through silent fallback | Admitted cases poison the fallback; test 1 split |
| Shadow or mixed callers picking up `when` | No masks are supplied, so the guard declines; test 5 |
| Predicate text in logs | 3f; sentry |

Rollback: revert the merge commit.

## 6. Build notes

- Reuse, do not copy: `when_native_rejection`, `_eval_predicate` and `run_kernel_step_masked`.
- Keep `_shadow_coordinator.py` under 600 lines. If the mask handoff pushes it over, move the per-batch mask slicing into a helper module rather than adding a census entry.
- One test process at a time (`~/bin/pytest-one`). Run tests/sentry on 3.10 and 3.11.

## 7. Review log

- **Codex plan gate, round 1: REVISE** (1 HIGH, 3 MEDIUM, 1 LOW). All folded into rev 2:
  - **HIGH (error order):** a mask failure now declines before any node runs, so the oracle decides the first error. Test 6 adds competing failures in both orders.
  - **MEDIUM (mask representation):** a NumPy mask is kept for pandas and an Arrow mask for the kernel, with a length check.
  - **MEDIUM (metric contract):** the activation leaf is the only exclusion, and it is checked separately.
  - **MEDIUM (silent fallback in the matrix):** admitted and decline cases are split, admitted cases poison the fallback, and the numeric references are restated against what admission really accepts.
  - **LOW (resolver aliasing):** full-frame evaluation plus a collision decline.
  - **Fact qualifications noted:** redact's `try` also wraps the kernel and the assignment, and reconstruction uses `to_pandas()` for empty tables (`_unified_slice_evidence.py`). 3d leaves the empty-table branch as it is: an empty table selects no rows.
- **Codex plan gate, round 2: GO.** All five round-1 findings closed; no new findings. 72 read-only redact/truncate probes of oracle gate against the proposed replay passed. Hash and categorical kernel probes need the companion and are build-gate obligations.
