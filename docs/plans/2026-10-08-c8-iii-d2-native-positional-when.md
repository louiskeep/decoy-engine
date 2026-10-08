Status: plan (rev 1, DRAFT — not yet gated)

Rules consulted: 00-universal, feature-dev, testing.

# C8-iii-d-2: positional draws under `when:` run natively, byte-identical to the d-1 oracle

Program: `decoy-platform/docs/ROADMAP.md`, Phase C, the C8-iii split. Branch `feat/c8-iii-d2-native-positional-when` off the merge-ready d-1 (`3f562de8`, dennis + Codex GO). d-1 is the parity target.

Owner decision (Cam, 2026-10-07, carried from d-1): under `when:`, a positional draw keys on the FULL-TABLE row number. d-1 made the full-frame **oracle** do this via the gate's `gate_positions`. d-2 lets the **native routes** run the same gated columns without changing a single output byte.

Risk R2: a deliberate route change on a default path. The output contract does not move — every covered column must be BYTE-IDENTICAL to the d-1 oracle; d-2 only changes which engine computes it. The R2 classification is because a mis-wired position would silently corrupt a default-path output.

Parity target (explicit): the d-1 full-frame oracle, i.e. d-1 property 2d — for a top-level positional categorical / REUSE faker under `when:`, a selected row's value equals that row's value in the ungated run, keyed on `_positional_keys.row_positions`.

## 1. Problem

Three positional strategies key each row's draw on its position: non-deterministic categorical (`_strategies/_categorical.py`), non-deterministic REUSE Faker (`_strategies/_faker_positional.py`), and `windowed_date` (`transforms/windowed_date.py`). Under `when:`, d-1 made the full-frame oracle key them on the full-table row. The native routes still send every one of them to the oracle:

- The shared masked kernel step `run_kernel_step_masked` (`native/_operator_step.py:401-436`) filters the selected rows with `pc.filter(plain, mask)` and calls `run_kernel_step` WITHOUT `row_offset`, `job_seed`, `pool`, or `missing_mask`. So it only reproduces the oracle for VALUE-keyed operators (its docstring lists hash/redact/truncate/deterministic-categorical/text_redact/bucket_perturb/date_shift). A positional kernel called this way would key on `0..k-1` (the filtered subset ordinal) — exactly the pre-d-1 bug.
- The native per-column `when:` admission excludes them: `ADMITTED_WHEN_STRATEGIES` (`native/_when_admission.py:38-41`) has no `faker`; a non-deterministic `categorical` is admitted by name but then rejected by `_column_rejection` (because `prepare_categorical` declines a non-deterministic column, `native/_categorical_prepared.py:54-55`). A declined `when:` column downgrades the whole table to the oracle (`native/_dispatch.py:388-390`).
- The unified bindings refuse outright when a predicate is present: `positional_faker_bindable` via `_faker_pool_bindable` (`physical/_shadow_bindings.py:162`) and `positional_categorical_bindable` (`physical/_shadow_bindings.py:190`) both return False when `plan_slice.when` is set.
- The explicit chunked route hard-rejects at compile time: `check_chunked_compatibility` calls `reject_nondeterministic_when` (`_chunked_categorical.py:93`, code `chunked_categorical_nondeterministic_when_not_supported`), `reject_nondeterministic_faker_when` (`_faker_positional_admission.py:92`, code `chunked_faker_nondeterministic_when_not_supported`) and `reject_windowed_date_when` (`_chunked_dgrn.py:128`, code `chunked_windowed_date_when_not_supported`) at `_chunked.py:307-309`.

The core enabler already exists from d-1: `_positional_keys.row_positions(row_offset, n, gate_positions, *, code)` (`_positional_keys.py:21-61`) returns `row_offset + gate_positions` under a gate and `row_offset + arange(n)` without one, and `positional_key_array` forwards `gate_positions`. The oracle faker path already uses it (`_strategies/_faker_positional.py:72-93`). The two NATIVE positional kernels do NOT yet accept gate positions: `native_categorical_positional` (`native/_categorical_ext.py:147-178`) and `sample_faker_array_positional` (`native/_operator_step.py:133-181`) each call `positional_key_array(row_offset, n, ...)` with no `gate_positions`, so they can only produce the contiguous range.

## 2. Decision

### 2a. Scope (the smallest coherent slice that proves native positional-under-`when:`)

COVER, on BOTH native routes (unified full-frame and native chunked), with byte-identical parity to the d-1 oracle:
- non-deterministic categorical under `when:`, STRING source only (the one source type the kernel's string gather and the config-complete positional artifact reproduce);
- non-deterministic REUSE Faker under `when:`, STRING source only.

DEFER, with a clear recommendation and an explicit retained decline:
- **`windowed_date` — deferred.** It has NO native kernel: there is no `WindowedDateParams`, no branch in `run_kernel_step`, and no `native_windowed_date_*`. The chunked route runs it only on the DGRN *oracle* leg (`_chunked_dgrn.py` `CHUNK_DGRN_STRATEGIES`), not through `run_kernel_step`. Inventing a native windowed_date kernel is out of scope for d-2. `reject_windowed_date_when` stays exactly as is. Recommendation: lift windowed_date in a later slice that either wires per-chunk gate positions into the DGRN oracle leg or builds a native kernel.
- **numeric-source positional Faker under `when:` — deferred.** The ungated numeric families already run natively via `POSITIONAL_FAKER_SOURCE_TYPES` and a pandas-derived `missing_mask` (`native/_faker_null_mask.py:37-45`). Under `when:` the missing mask would have to be taken over the FILTERED subset, a second mask-derivation path for a family that d-1's string fixtures do not exercise. Keeping the admission gate's existing "string source" rule (`native/_when_admission.py` rule 2) makes numeric sources fall to the full-frame oracle, which IS the d-1 parity target on the unified route. Recommendation: lift numeric-source faker under `when:` after the string case ships.

Explicitly unchanged (inherited from d-1 scope 2e): nested children (leaf-ordinal keyed), order-dependent stream strategies, and all value-keyed `when:` behavior.

### 2b. Position threading (one owner, reused by both native routes)

The full-table position of a selected row is `chunk_base_row_offset + chunk_local_selected_position`. The masked step already computes the selected positions for its format-error rebase (`np.flatnonzero(mask...)`, `native/_operator_step.py:434`); reuse exactly that array as `gate_positions`.

1. `run_kernel_step_masked` (`native/_operator_step.py:401-436`) gains `pool`, `job_seed`, `row_offset`, `missing_mask` (mirroring `run_kernel_step`). After the empty-selection early return, compute `selected = np.flatnonzero(mask.to_numpy(zero_copy_only=False))` ONCE and use it both for the format-error rebase and as `gate_positions`. It forwards `row_offset`, `job_seed`, `pool`, `gate_positions=selected`, and `missing_mask=pc.filter(missing_mask, mask)` (when `missing_mask is not None`) into `run_kernel_step`.
2. `run_kernel_step` (`native/_operator_step.py:246-398`) gains `gate_positions: np.ndarray | None = None`, forwarded to the two positional kernels only.
3. `native_categorical_positional` and `sample_faker_array_positional` each gain `gate_positions: np.ndarray | None = None` and pass it straight to `positional_key_array(row_offset, n, code=..., gate_positions=gate_positions)` — the already-shipped d-1 signature. No new key math; `row_positions` is the single owner.

Invariant: when `gate_positions is None` (every call with no `when:`, including the chunked positional calls that pass only `row_offset`), the key column is byte-identical to today. When set, the key of selected local row `i` is `row_offset + selected[i]` = that row's full-table number = exactly d-1's `row_positions(ctx, n)` under a gate. For STRING sources, `missing_mask` is `None` and the kernel falls back to the filtered subset's Arrow validity, which equals the d-1 oracle's subset missingness for strings (`native/_faker_null_mask.py:40-43`), so string-source faker needs no missing-mask threading at all.

### 2c. Adapter wiring (both already receive what they need)

- Native chunked `_mask_chunk_native` (`native/_chunk_masking.py:150-159`): in the `when_mask is not None` branch, pass `pool=pool_by_column[name] if isinstance(params, FakerParams) else None`, `row_offset=row_offset`, `job_seed=job_seed`, and `missing_mask=(faker_missing or {}).get(name)` into `run_kernel_step_masked`. All four are already in scope (the non-masked branch already passes them). Evidence counting for a masked positional faker reuses the existing `FakerParams and result.ran` → `pool_select` path; empty-selection stays idle/uncounted (`:189-192`).
- Unified `run_operator` (`physical/_shadow_operators.py:322-330`): in the `when_mask is not None` branch, pass `pool=pool`, `row_offset=row_offset`, `job_seed=job_seed`, and `missing_mask=faker_missing_mask(source_slice, column)` when positional (guarded by the existing `positional_faker and source_slice and column` condition at `:343-347`). The coordinator already passes `pool`, `row_offset`, `source_slice`, `when_mask`, and `ctx.job_seed` (`physical/_shadow_coordinator.py:372-386`); `_bound_params` already asserts a positional faker carries a `job_seed` (`:198-199`).

### 2d. Admission / decline, each gated to reproduce the d-1 oracle exactly

- **Native per-column gate `when_native_rejection` (`native/_when_admission.py`), the single source of truth both native routes read.** Add a positional admission branch: a column is admitted under `when:` when it is the config-complete positional categorical (`prepare_positional_categorical` succeeds, `native/_categorical_prepared.py:79-103`) OR the stage-A positional faker (`positional_faker_config_of_entry` is not None, `native/_faker_positional_admission.py`), AND its source is `string` (keep rule 2), AND the predicate parses in the closed grammar (keep rules 3–4). This branch BYPASSES the value-keyed `_column_rejection`/`ADMITTED_WHEN_STRATEGIES` membership (which legitimately reject a non-deterministic categorical). Everything else keeps its existing code (`when_predicate_not_native`, `when_predicate_outside_native_subset`, `when_predicate_reads_masked_column`).

- **Unified route.** Relax the two `plan_slice.when` refusals: `positional_categorical_bindable` (`physical/_shadow_bindings.py:190`) and `_faker_pool_bindable` (`:162`) no longer refuse solely because a predicate is present; they still require everything else (string resident source, no vault, allowlisted poolable provider, no FK, stage-A config). Admission is still fronted by `when_columns_admitted` → `when_native_rejection` (`_unified_slice_when.py:63-70`), so a column binds natively only when the shared gate admits it. A non-string or otherwise-undecidable positional+`when:` column declines and the whole table runs on the full-frame oracle — which IS the d-1 parity target, so the unified fallback is always safe.

- **Chunked route — the one asymmetry to call out.** The chunked route's fallback is the per-chunk ORACLE leg, which d-1 explicitly did NOT wire to pass full-table gate positions (`_chunked_dgrn.py` docstring point 2). So a positional+`when:` column must NEVER silently fall to that leg. Two coordinated changes, following the bucket_perturb precedent (`_chunked_bucket_perturb.py:197-252`):
  1. Narrow `reject_nondeterministic_when` and `reject_nondeterministic_faker_when` (`_chunked.py:307-309`) so they do NOT reject a config-complete positional column with a closed-grammar predicate (mirroring `_when_column_is_chunk_safe`). They still reject config-incomplete ones under their existing codes.
  2. Add a SCHEMA-AWARE guard on the native chunked route (where `plan_native_route` / `_dispatch.py` sees `first_schema`, alongside the faker source-type check at `:439-452`): a positional categorical/faker `when:` column whose first-chunk source is NOT `string` must HARD-REJECT with its existing code (`chunked_categorical_nondeterministic_when_not_supported` / `chunked_faker_nondeterministic_when_not_supported`) rather than `_downgrade_to_oracle`, because the chunked oracle leg cannot reproduce d-1 for a positional column. String-source positional+`when:` columns pass and run on the native kernel. `reject_windowed_date_when` is untouched.

## 3. Acceptance tests (written first; never weakened; fail-before where meaningful)

1. **Native == d-1 oracle, byte-identical (the core property).** For positional categorical and positional faker, STRING source, over a ≥50-row table with d-1's predicate matrix (none selected, all selected, every-other-row, a contiguous block, a predicate on another column): every selected row's output is byte-identical to the d-1 full-frame oracle's output for the SAME config; unselected rows (nulls included) are byte-identical to source. Assert on the exact Arrow arrays, not values-only. Run on the unified full-frame route AND the native chunked route. Fail-before: these columns run on the oracle today, so the native assertion fails until 2b–2d land.
2. **Chunk boundaries and offsets (chunked route, the thing d-1 could not do).** The property of test 1 holds across: a selection straddling chunk boundaries; a multi-chunk table; a NONZERO `base_row_offset`; a chunk whose predicate selects no row (idle, uncounted). Full-table positions must equal `base_row_offset + flatnonzero(mask within the whole stream)`. Property-based (Hypothesis, fixed seed): native-chunked == d-1 oracle for random row counts, chunk sizes, offsets and predicates; pin any failing example.
3. **Nulls in the subset.** A selected subset containing null targets: covered rows still draw by position, null targets restore to null, and the result equals the d-1 oracle.
4. **Deferred cases keep declining with the same codes.** `windowed_date` + `when:` still raises `chunked_windowed_date_when_not_supported`; a NUMERIC-source positional faker + `when:` still declines (chunked: `chunked_faker_nondeterministic_when_not_supported` via the new schema guard; unified: `when_predicate_not_native` → full-frame oracle, which reproduces d-1). A config-incomplete positional categorical/faker + `when:` keeps its existing code.
5. **No change to value-keyed `when:`.** Byte-identical output and the same route evidence for hash/redact/truncate/deterministic-categorical/text_redact/bucket_perturb/date_shift under `when:`, on both routes, for representative configs.
6. **No change without `when:`.** Byte-identical output for positional categorical and faker on full-frame, chunked and unified routes with no predicate (the `gate_positions is None` path). Testflight fingerprints unchanged — no golden uses `when:` with these strategies; STOP and escalate if any fingerprint moves.
7. **Old pins.** List before implementing any `when:`-decline pins for these strategies that must change from "raises code" to "runs natively" (expected new values derived from test 1, never hand-edited); update the mutation-kill pin (`tests/unit/execution/test_when_gate_mutation_kills.py`) only where it asserted the native decline, recording the reason. Candidate homes for new tests: `tests/unit/execution/test_categorical_seeded_nondet.py`, `test_faker_positional_nondet.py`, `test_when_predicate.py`, `test_multi_table_when.py`.
8. **Sentries + mutation** on the new position threading: `run_kernel_step_masked`'s `selected`/`gate_positions` composition and `missing_mask` filter, the `gate_positions` forward in `run_kernel_step`, each positional kernel's `gate_positions` pass-through, the narrowed chunked config rejects, and the chunked schema-aware hard-reject guard. A surviving mutant on the position composition is build work, not a recorded number.
9. **Docs.** CHANGELOG entry under "Changed": positional `categorical` and REUSE `faker` under `when:` now run on the native/unified/chunked routes with output equal to the oracle; windowed_date and numeric-source faker under `when:` still run on the oracle. Update the d-1 note in `CHANGELOG.md` ("still send these gated columns to the oracle ... C8-iii-d-2") and the `when:` section of `docs/strategies.md`.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| Masked positional kernel keys on the subset ordinal (pre-d-1 bug) | 2b threads `gate_positions = flatnonzero(mask)`; test 1 asserts byte-identity to d-1 |
| A positional+`when:` column silently falls to the chunked oracle leg, which does not reproduce d-1 | 2d chunked schema-aware guard HARD-rejects non-string positional+`when:` instead of downgrading; test 4 |
| Output changes for a job without `when:` | 2b keeps the contiguous key when `gate_positions is None`; tests 6 + testflight (STOP on fingerprint move) |
| A kernel grows its own key math and drifts from the oracle | `row_positions` stays the single owner; mutation on each kernel's pass-through (test 8) |
| Faker subset missingness diverges from the oracle | string-source only; Arrow validity of the filtered subset == oracle (2b); numeric deferred (2a); test 3 |
| Value-keyed `when:` regresses while the masked step grows params | new params default to the no-gate form; test 5 |

Rollback: revert the merge commit. No data migration, no config surface change; the output contract is unchanged, so a revert returns these columns to the oracle with identical bytes.

## 5. Review log

- (to be filled by the plan gate)
