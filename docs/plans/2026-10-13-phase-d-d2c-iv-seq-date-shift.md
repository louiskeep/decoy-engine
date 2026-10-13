# Phase D / D2c-iv: `date_shift` native on the SEQ route

Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing

Slice owner: Opus (author) -> Codex plan-gate -> Sonnet (build) -> dennis -> Codex final.
Branch: `feat/phase-d-d2c-iv-seq-date-shift` (worktree `.claude/worktrees/phase-d-d2c-iv`), cut from main `91fefec7` (D2c-iii merged).

Revision: rev 2 (addresses Codex plan-gate NO-GO on rev 1: 1 HIGH + 2 MED). Changes are tagged
`[rev2]`. The core design (dedicated seam, append RowErrors, `fail_closed=None`, admit adapter)
was validated; rev 2 fixes the error-POLICY contract + the acceptance-test boundaries, the
calendar-overflow claim, and the admit/fallback test split, and folds in Codex's verified
implementation recipe.

## 1. FRAME

### What
Register `date_shift` as the fifth tenant of the SEQ-native descriptor framework (after
`hash`, `fpe`, `categorical`, `bucket_perturb`). Non-key `date_shift` payload columns mask on
the compiled index kernel on the bounded sequential (SEQ) FK route, byte-identical to the
Python handler at the HANDLER boundary, reusing the D1c `preserve_values` contract and the
D2c-iii derive-call evidence. First SEQ descriptor to use a survive-and-report row-error channel.

### Why
`date_shift` runs native on the chunked / full-frame routes already; D2c-iii added
`bucket_perturb` to the SEQ descriptor framework and the shared preserve + evidence this slice
reuses. `date_shift` was split from D2c-iii because its kernel returns `format_error_positions`
(non-null values that failed to parse), which at the HANDLER boundary SURVIVE (pass through
unchanged) and are REPORTED as `format_error` RowErrors, unlike the `fpe` fail-closed channel
that kills the run.

### Error-policy contract `[rev2 — the HIGH-1 correction]`
Distinguish two boundaries. rev 1 wrongly promised end-to-end "survival"; the SEQ pipeline
decides what happens to a reported error.
- HANDLER / SEAM boundary: the native `date_shift` seam produces the IDENTICAL `(output frame,
  appended ctx.row_errors)` the Python handler produces for the same input — a non-null
  unparseable cell keeps its original value in the returned frame AND a `format_error` RowError
  is appended. This is the only "survival" the seam owns, and it is byte-identical native vs Python.
- PIPELINE boundary: `run_sequential` (like `run`) owns the D8 fail-loud/quarantine rule
  (`_sequential.py:93-111`). By DEFAULT an uncovered `format_error` raises `RowErrorsFailedError`
  (`:467`); with a quarantine trigger covering `format_error` the covered rows are excluded from
  the main Arrow output (`compute_quarantine`, ~`:505`) and the run completes. A KERNEL fault
  aborts regardless of quarantine, with the existing sink-abort, and never retries Python.
- PARITY statement: because the seam matches the Python handler at the HANDLER boundary, and
  the pipeline policy is route-neutral (it drains the same `ctx.row_errors`), the SEQ run
  behaves IDENTICALLY whether a `date_shift` column ran native or Python. Tests therefore
  compare native-SEQ to PYTHON-SEQ (the same `run_seq` with the companion hidden), NOT to a
  different enforcement boundary such as the handler-level oracle.

### Headline (de-risks the slice)
The SEQ route ALREADY collects `ctx.row_errors` (drain per node `_sequential.py:445`, fail-loud
`:462-467`, `ExecutionResult.row_errors` `:627`), so the seam only has to append the SAME
`RowError`s the Python handler appends. NO new plumbing; the channel is "new" only in that no
SEQ descriptor has used `ctx.row_errors` yet.

### Scope (this slice)
- `date_shift` NON-KEY payload columns native on SEQ via the index kernel.
- A dedicated `date_shift_column` seam that appends format-error `RowError`s to `ctx.row_errors`
  byte-identical to the Python handler (`_date_shift.py:201-209`).
- Thread D1c `preserve_values` (mask first, recorded once) and generalize the D2c-iii
  derive-call evidence to include `date_shift`.
- SEQ-scoped value-free coded decline reasons via `date_shift_config_rejection`.

### Non-scope (explicit)
- `group_by` date_shift is NOT native (admission declines; the seam is gated on falsy
  `group_by`; the snapshot-anchor machinery stays Python).
- No FK-key native path (native REMAP is hash-only; a `date_shift` FK key stays Python via the
  non-hash `prepare_table` boundary at `_sequential_native.py:282`; no new code).
- NO runtime range decline. `[rev2 — MED-2 correction]` rev 1 wrongly claimed admission
  guarantees no calendar overflow. It does not: admission accepts e.g. `min_days=max_days=1`
  with a near-max source date (`2262-04-11`), and the pandas date+offset addition then raises
  `OverflowError` on BOTH paths (`_date_shift_ext.py:209` and `_date_shift.py:273`). The correct
  statement is PARITY: calendar overflow aborts identically on native and Python, so the seam
  needs no runtime range guard and no fallback — the native path fails exactly where Python
  fails. (The shift MAGNITUDE is still bounded at admission by `MAX_ABS_SHIFT_DAYS = 106_751`,
  `_date_shift_ext.py:56-67`; the overflow is a date+offset result-range issue, not a magnitude issue.)
- No chunked / full-frame / OOC change (`date_shift` is not OOC-native).

### Reuse (established methodology)
- Kernel `native_date_shift` (`native/_date_shift_ext.py:124`) unchanged; returns
  `(out, format_error_positions)`; positions index the SUPPLIED array (preserve-rebased
  internally at `:176-180`). Under `when`, the supplied array is the SELECTED frame; the
  existing `_when_gate.py:258` does the final table-position remap, so the seam adds NO offset
  or extra rebasing. `[rev2]`
- Loader `load_compiled_index_kernel` (same index kernel as bucket/categorical).
- Admission `date_shift_config_rejection` (`native/_operator_config_rejections.py:150-200`).
- preserve `transforms/date_preserve.py`; RowError literal `FORMAT_ERROR_REASON`
  (`native/_date_shift_ext.py`); seam precedent `bucket_column` / `ColumnDecision` evidence.

## 2. DESIGN

### 2.1 Descriptor registration (`_sequential_native_ops.py`) `[rev2 — exact admit recipe + raising stub]`
- `DATE_SHIFT = "date_shift"`, `SEQ_DATE_SHIFT_DISPOSITION_KEY = "sequential_date_shift"`.
- `_date_shift_admit(column, input_type, seed) -> str | None`: bridges the SEQ admit signature
  to `date_shift_config_rejection`. Codex verified the exact call (the seed retains the raw
  provider config at `plan/_seed_envelope.py:222`, and the resolver needs a resident `pa.Table`,
  `native/_requirements.py:349`):
  ```
  date_shift_config_rejection(
      column, column, None,                       # name, table, profile=None
      namespace=seed.namespace,
      provider_config=provider_config_to_dict(seed.provider_config),
      resident_sources={column: pa.schema([pa.field(column, input_type)]).empty_table()},
  )
  ```
  Returns the coded reason or `None`. Keep this adapter (and any call carrier) in the OPS
  module, not the driver, to keep `_sequential_native.py` lean (§2.6). `[rev2]`
- `date_shift_op(load)`: `NativeOp(DATE_SHIFT, SEQ_DATE_SHIFT_DISPOSITION_KEY, load,
  _date_shift_admit, apply=<raising stub>)`, `fail_closed=None`, `warn=None`. `date_shift` does
  NOT route through `NativeOp.apply`/`NativeResult` (its kernel returns a tuple), so the seam
  calls `native_date_shift` directly; the `apply` slot is a stub that raises
  `AssertionError("date_shift uses date_shift_column, not apply")`. `[rev2 — Codex: prefer the
  raising stub; making apply Optional would require narrowing both existing call sites.]` The
  fpe fail-closed kill-channel (`NativeResult.errors` + `fail_closed`) is UNTOUCHED.
- Loader `_load_date_shift(native_threads) -> BoundIndexKernel(load_compiled_index_kernel(),
  native_threads)` (identical to `_load_bucket`); register `DATE_SHIFT: date_shift_op(_load_date_shift)`.

### 2.2 The dedicated `date_shift_column` seam (`_sequential_native.py`)
Mirrors `bucket_column`, minus the range guard, plus the row-error append. Order:
1. Preconditions: `ctx.nested_outer_column or self._in_remap` -> None; decision missing or
   `decision.strategy != DATE_SHIFT` -> None; `decision.rows += rows`; `not decision.native or
   rows == 0 or bound is None` -> None.
2. `preserved = preserve.mask(df[column])` from the ORIGINAL column (if preserve set).
3. `array = self._pinned_array(df[column], decision)` (raises
   `sequential_date_shift_input_type_drift` on conversion failure).
4. `derive_calls: list[int] = []`; call the kernel directly:
   `out, positions = native_date_shift(array, min_days=..., max_days=..., date_format=fmt,
   mask_key=ctx.mask_key, namespace=..., index_kernel=bound.kernel,
   native_threads=bound.native_threads, derive_calls=derive_calls, preserved=preserved)`.
5. `decision.native_calls += sum(derive_calls)`.
6. HANDLER-boundary survive-and-report: for each `pos in positions`, append
   `RowError(column=column, row_index=int(pos), trigger="format_error", reason=FORMAT_ERROR_REASON)`
   to `ctx.row_errors` (byte-identical to `_date_shift.py:201-209`; `FORMAT_ERROR_REASON`
   imported from `native._date_shift_ext`). The seam does NOT raise on positions; the pipeline
   (`run_sequential`) applies the error policy. `positions` need NO offset/rebasing (preserve
   rebasing is internal to the kernel; the `when`-gate remaps selected-frame to table position). `[rev2]`
7. preserve: if `preserved is not None`, `count = int(preserved.sum())`,
   `decision.counts_preserved = True`, `decision.preserved_rows += count`,
   `ctx.record_date_preserved(column, count)` once (incl zero).
8. Return `out.to_pylist()`.
A `DateShiftCall` dataclass (in the OPS module, parallel to `BucketCall`, adding `min_days`,
`max_days`) MAY carry the kwargs; since the seam calls the kernel directly, method kwargs are
also fine — builder picks the clearer while keeping the driver lean.

### 2.3 Handler seq_native block (`_strategies/_date_shift.py`)
Add to `DateShiftStrategyHandler.run`, after namespace/cfg/min-max/preserve/format resolution,
gated on falsy `group_by` (the handler initializes `anchor_col=None`; preserve+group_by is
compile-rejected so the native path never sees it), before the Python `_shift_rows` block (~:177):
```
seq = getattr(ctx, "seq_native", None)
if seq is not None and not group_by:
    out = seq.date_shift_column(
        ctx, df, column,
        namespace=plan.namespace, mask_key=ctx.mask_key,
        min_days=min_days, max_days=max_days, date_format=fmt, preserve=preserve,
    )
    if out is not None:
        df[column] = out
        return df, []
```
`out is None` falls through to the Python path (config decline, companion absent, FK-key
boundary). `fmt` is the resolved EXPLICIT `date_format`; admission declines a missing/autodetect
format, so the native path never calls `_detect_format`.

### 2.4 Evidence (generalize the D2c-iii rule)
Generalize `ColumnDecision.entry()` `calls_decide` (`_sequential_native.py:147`) from
`strategy == BUCKET_PERTURB` to `strategy in (BUCKET_PERTURB, DATE_SHIFT)`. A native `date_shift`
column reports from actual `derive_calls`: `native_calls > 0` -> `rust_companion`;
`native_calls == 0` with rows -> `executed_backend="none"` + a date-specific
`reason="no_native_date_shift_calls"` (do NOT change bucket's reason). `[rev2 — Codex verified
all-unparseable produces zero derive calls + nonempty errors.]` PIN this combination: an
ALL-UNPARSEABLE non-null column reports `executed_backend="none"` AND appends format-error
RowErrors (the report does not depend on a kernel call). `preserved_rows` appears only when
`counts_preserved`.

### 2.5 FK boundary (inherited, no new code)
`date_shift` is non-hash; `prepare_table` (`:282`) gives it a native decision only off the
FK-key boundary. A `date_shift` FK-key column stays Python.

### 2.6 Module size (WATCH)
`_sequential_native.py` is 539 LOC; `date_shift_column` + the one-line `calls_decide`
generalization project ~585, but Codex notes that omits import/loader/registration overhead.
Keep `_date_shift_admit` and any `DateShiftCall` carrier in the OPS module. Measure after
`ruff format`; if `_sequential_native.py` would cross 600 the builder STOPS and escalates
(no census entry to force it).

## 3. ACCEPTANCE TESTS (behavior before impl) `[rev2 — reframed around the two boundaries]`

Companion-INDEPENDENT discipline: the `*_seq.py`/`*_gap.py` files MUST pass with NO companion
(stand-in index kernel via monkeypatch) and MUST run in the no-companion ci-mirror venv AND
abi-3 before the PR (Done-when). New helpers `tests/parity/_d2c_date_shift_data.py` parallel
`_d2c_bucket_data.py`: `DateShiftMarkingKernel` (offset == `max_days`, distinguishable),
`ReferenceDateShiftKernel` (reproduces the keyed draw), `install_date_shift`/`hide_date_shift`,
shapes + an `expect_shift` helper. For the QUARANTINE-config setup that cases 4/9 need, use
`tests/integration/test_fk_sequential_row_error_leak.py:150-182` as the template (the D1c files
have row-error assertions but NOT quarantine configuration). `[rev2 — Codex LOW: corrected
template.]` The existing `run_seq` helper (`_d2b_shapes.py:87-98`) does NOT forward a
`quarantine_config`; the new date_shift helper (or a `run_seq` extension) must forward one with
`enabled=True`, `triggers=["format_error"]`, and SEPARATE temporary output paths for the
native-SEQ and Python-SEQ runs. Cross-strategy tests must supply stand-ins for the OTHER
compiled operators too.

Comparison rule: for error-bearing cases, compare native-SEQ (`install_date_shift`) to
PYTHON-SEQ (`hide_date_shift`) under the SAME `run_seq` pipeline — NOT to `run_full`. For
all-parseable output parity, `run_seq` vs `run_full` is still valid.

1. **Native witness** (consumption): an admitted all-parseable payload column carries the
   distinguishable shifted values in the emitted table.
2. **Discarded-native-result fails the proof**: dropping the native result yields the Python pick.
3. **Admission reasons — DIRECT unit tests** `[rev2 — MED-3]`: construct a `ColumnSeed` per code
   and assert `_date_shift_admit` returns exactly: `date_shift_requires_namespace`,
   `date_shift_group_by_not_native`, `date_shift_requires_date_format`,
   `date_shift_special_date_format`, `date_shift_timezone_directive`,
   `date_shift_preserve_values_invalid`, `date_shift_min_days_not_int`/`_max_days_not_int`,
   `date_shift_min_days_out_of_range`/`_max_days_out_of_range`,
   `date_shift_source_not_string:<col>:<type>`. Value-free (name/type only).
3b. **Fallback behavior — SEPARATE** `[rev2 — MED-3]`: for a NON-native-but-valid config
   (e.g. `group_by`), the column runs Python and the run succeeds (seam not engaged / returns
   None). For an INVALID config that the compiler/handler rejects (e.g. malformed preserve
   `_checks_date_shift.py:340`, bad bounds), the SAME compile/handler exception is raised WITH
   and WITHOUT the native seam (the seam does not swallow or change it).
4. **Row-error parity at the HANDLER boundary** `[rev2 — HIGH-1]`: a column mixing parseable,
   non-null UNPARSEABLE, and null rows. (a) native-SEQ and Python-SEQ produce the IDENTICAL
   `ExecutionResult.row_errors` (same column, row_index, trigger="format_error",
   reason=FORMAT_ERROR_REASON, count, order) under a format_error quarantine trigger; (b) under
   quarantine, both COMPLETE, both EXCLUDE the bad rows from the main Arrow output identically,
   and the parseable rows are shifted identically; (c) null rows are NOT reported.
5. **Default pipeline raises** `[rev2 — HIGH-1]`: the same error-bearing column with NO
   quarantine trigger -> `run_seq` raises `RowErrorsFailedError` in BOTH native and Python
   modes (parity on the raise), with the existing sink-abort (transactional sink commits
   nothing).
6. **Kernel fault aborts regardless** `[rev2]`: a KERNEL fault (companion raises) aborts the run
   with no Python retry and no partial output, independent of any quarantine config — distinct
   from the surviving format errors of cases 4-5.
7. **preserve + rebasing**: with a `preserve_values` sentinel where an unparseable row sits AFTER
   a preserved row, the sentinel survives, `record_date_preserved` equals Python-SEQ, and the
   format-error `row_index` is the correct full-frame position; include an all-preserved column.
8. **FK-key boundary**: a `date_shift` key column gets no native decision, matches Python, and
   does not disturb a native hash payload beside it.
9. **Evidence** `[rev2]`: all-null -> `none` + `no_native_date_shift_calls`, no row errors;
   ALL-UNPARSEABLE -> `none` + `no_native_date_shift_calls` AND row_errors non-empty (zero kernel
   calls, errors still reported — under quarantine so the run completes); 1-parseable-row ->
   `rust_companion`; empty selection -> skip.
10. **when-subset** `[rev2]`: a `when:` subset shifts only selected rows natively; the final
    table-position row-error indices match Python-SEQ (the when-gate does the remap; the seam
    adds no offset).
11. **Cross-strategy isolation**: hash+fpe+categorical+bucket+date_shift in varied column orders
    each mask correctly; the hash/fpe/categorical/bucket disposition fragments stay BYTE-IDENTICAL
    with date_shift present (supply stand-ins for all compiled operators).
12. **Registry**: `set(_OPS) == {hash,fpe,categorical,bucket_perturb,date_shift}`; unwired
    parametrize is `["text_mask"]`; update the metrics-iteration test if it enumerates keys.
13. **No date_shift -> no `sequential_date_shift` key.**
14. **Companion absence** (`hide_date_shift`) -> Python, `crypto_extension_unavailable`.
15. **group_by column stays Python** (seam not engaged; snapshot-anchor path runs).
16. **native_threads spy** reaches the kernel (D2c-iii precedent).
17. **usable/unusable predicate matches the oracle**: native `unusable & ~source_null`
    (`_date_shift_ext.py:216`) == oracle `_shift_rows` (`_date_shift.py:289`) error_rows, on a
    column with a non-trivial mix of null/unparseable/parseable.
18. **Calendar-overflow parity** `[rev2 — MED-2]`: a near-pandas-max source date + a positive
    in-range shift (e.g. `min_days=max_days=1`, source `2262-04-11`) raises the SAME error on
    native-SEQ and Python-SEQ (both abort via pandas OverflowError); and a near-min date + a
    negative shift. No fallback, no guard.

`tests/parity/test_phase_d_d2c_iv_date_shift_real.py` (real kernel, skip-if-no-companion):
real-kernel byte-identical parity vs Python (all-parseable, week/month shifts) INCLUDING a
decomposed-Unicode format literal + non-ASCII unparseable rows (strict byte-identity; dates
NFC-normalize via `_canonicalize_source`, no NFC exemption), and a real row-error-parity case
under quarantine.

Regression (stay green): full-frame/chunked/OOC/acyclic-SEQ unchanged; the d1b1/d2c bucket +
categorical + hash SEQ suites; d1c date_shift suites; public-import-boundary + module-size +
log + physical-seam sentries. The Python date_shift path is UNCHANGED for group_by / declined /
companion-absent columns.

## 4. FAILURE MODES
- Non-null unparseable date -> at the handler boundary SURVIVES (original value kept) + a
  `format_error` RowError on `ctx.row_errors`. The PIPELINE then raises `RowErrorsFailedError`
  by default, or (with a covering quarantine trigger) excludes the row from main output and
  completes — identically native vs Python. Null rows: pass through, no error.
- Kernel fault (companion raises) -> run ABORTS (fail-closed; no retry/partial), regardless of quarantine.
- Calendar overflow (near-bound date + shift) -> pandas `OverflowError` aborts on BOTH paths (parity). `[rev2]`
- Config decline -> Python, coded value-free reason. Companion absent -> Python (`crypto_extension_unavailable`).
- Input-type drift at `_pinned_array` -> `sequential_date_shift_input_type_drift` raises.
- NO runtime range decline (parity with Python, not an admission guarantee). `[rev2]`

## 5. GATE / DONE-WHEN
- Codex PLAN gate GO (this rev) BEFORE build.
- Build (Sonnet); SELF-CHECK; VERIFY (Opus): coverage 100% line+branch on the changed units +
  mutation on the added logic (scrutinize the row-error append loop, the usable/unusable
  predicate, preserve rebasing, and the evidence rule; verify every "equivalent" claim
  empirically — the D2c-iii lesson); both suites PASS in ci-mirror AND abi-3; confirm the
  comparison boundary is native-SEQ vs Python-SEQ (not a mismatched enforcement boundary).
- dennis REVIEW green; remediate. Codex FINAL gate GO.
- Merge on full green gate chain + Slack flag before merge-to-main; PR CI green; main CI green after.
- DOCUMENT: platform ROADMAP (D2c-iv DONE, NEXT = D2c-v text_mask/text_redact) + RECENTLY-SHIPPED;
  refresh the engine graphify graph against the new main; plain-English Slack recap.

## 6. FOLLOW-UPS / TRACKED (not this slice)
1. Pre-GA operator-neutral empty-selection disposition reason (now covers
   `no_native_bucket_calls` + `no_native_date_shift_calls` + the shared `no_rows_hashed`).
2. The chunked-route date-range guard gap (`_operator_step.py:443`, carried from D2c-iii).
3. D2c-v = text_mask / text_redact on SEQ; then the pure-Arrow trio (redact/truncate/passthrough)
   + REMAP generalization; then platform sequential activation (gated after A3 + Cam go).
