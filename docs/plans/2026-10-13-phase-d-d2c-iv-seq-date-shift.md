# Phase D / D2c-iv: `date_shift` native on the SEQ route

Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing

Slice owner: Opus (author) -> Codex plan-gate -> Sonnet (build) -> dennis -> Codex final.
Branch: `feat/phase-d-d2c-iv-seq-date-shift` (worktree `.claude/worktrees/phase-d-d2c-iv`), cut from main `91fefec7` (D2c-iii merged).

## 1. FRAME

### What
Register `date_shift` as the fifth tenant of the SEQ-native descriptor framework (after
`hash`, `fpe`, `categorical`, `bucket_perturb`). Non-key `date_shift` payload columns mask
on the compiled index kernel on the bounded sequential (SEQ) FK route, byte-identical to
the Python handler, reusing the D1c `preserve_values` contract and the D2c-iii derive-call
evidence. This is the last of the date operators and the first SEQ descriptor to use a
SURVIVE-AND-REPORT row-error channel.

### Why
`date_shift` (shift a date by a keyed per-row offset within a bounded window) runs native
on the chunked / full-frame routes already; D2c-iii added `bucket_perturb` to the SEQ
descriptor framework and built the shared preserve + evidence foundation this slice reuses.
`date_shift` was split out of D2c-iii specifically because its kernel returns a third output
channel the SEQ seam had not modelled: `format_error_positions` (non-null values that failed
to parse), which SURVIVE (pass through unchanged) and are REPORTED as row errors, unlike the
`fpe` fail-closed channel that kills the run.

### Headline (de-risks the slice)
The SEQ route (`run_sequential`) ALREADY collects `ctx.row_errors`: the Python `date_shift`
handler appends `RowError`s to `ctx.row_errors`, and `run_sequential` drains them per node
(`_sequential.py:445 drain_row_errors(ctx.row_errors, table=table)`) and classifies them
fail-loud (`:462-467 RowErrorsFailedError` on uncovered errors), exactly as the chunked /
full-frame routes do. So the native seam only has to append the SAME `RowError`s the Python
handler appends, and they reach `ExecutionResult.row_errors` identically. There is NO new
plumbing; the channel is "new" only in that no SEQ descriptor has used `ctx.row_errors` yet.

### Scope (this slice)
- `date_shift` NON-KEY payload columns native on SEQ via the index kernel.
- A dedicated `date_shift_column` seam that appends format-error `RowError`s to
  `ctx.row_errors`, byte-identical to the Python handler (`_date_shift.py:201-209`).
- Thread D1c `preserve_values` (mask first, recorded once) and the D2c-iii derive-call
  evidence (generalize `ColumnDecision.entry()`'s `calls_decide` to include `date_shift`).
- SEQ-scoped value-free coded decline reasons via `date_shift_config_rejection`.

### Non-scope (explicit)
- `group_by` date_shift is NOT native (admission declines it; the seam is gated on a falsy
  `group_by`). The snapshot-anchor machinery stays Python.
- No FK-key native path (native REMAP is hash-only; a `date_shift` FK key stays Python via
  the non-hash `prepare_table` boundary; no new code).
- NO runtime range guard. Unlike `bucket_perturb` (which has a data-aware near-boundary
  decline), `date_shift`'s only range limit is the shift MAGNITUDE, bounded at ADMISSION by
  `MAX_ABS_SHIFT_DAYS = 106_751` (`native/_date_shift_ext.py:56-67`). So `date_shift_column`
  needs no mid-run decline. (The plan asserts no all-calendar overflow survives admission:
  `abs(min_days)` and `abs(max_days)` are both `<= 106_751`, keeping `offset + min_days`
  within int64 and the pandas Timedelta range.)
- No chunked/full-frame/OOC change. (`date_shift` is not an OOC-native operator.)

### Reuse (established methodology)
- Kernel: `native_date_shift` (`native/_date_shift_ext.py:124`), unchanged. It returns
  `(out: pa.Array, format_error_positions: tuple[int,...])`; positions are 0-based indices
  into the FULL input array (preserve-rebased internally), matching the Python handler's
  `error_rows` on the SEQ route (one table = one frame, no chunk offset).
- Loader: `load_compiled_index_kernel` (date_shift uses the SAME `derive_index_batch` index
  kernel as bucket/categorical — confirmed `_date_shift_ext.py:90-96`).
- Admission: `date_shift_config_rejection` (`native/_operator_config_rejections.py:150-200`).
- preserve: `transforms/date_preserve.py` `PreserveSet.mask` + `ctx.record_date_preserved`.
- RowError literal: `FORMAT_ERROR_REASON` (`native/_date_shift_ext.py`), the single source
  the Python handler's inline reason already equals.
- Seam precedent: `bucket_column` / `BucketCall` / the `ColumnDecision` evidence fields
  (`_sequential_native.py:376-421`, `:133-160`).

## 2. DESIGN

### 2.1 Descriptor registration (`_sequential_native_ops.py`)
- `DATE_SHIFT = "date_shift"`, `SEQ_DATE_SHIFT_DISPOSITION_KEY = "sequential_date_shift"`.
- `_date_shift_admit(column, input_type, seed) -> str | None`: the admit adapter. It bridges
  the SEQ `(column, input_type, seed)` admit signature to `date_shift_config_rejection`'s
  `(name, table, profile, *, namespace, provider_config, resident_sources)` signature,
  reproducing the resolver's value-free decline codes. It assembles: `name=column`,
  `table=column` (stub), a one-field `profile`/`resident_sources` carrying `input_type` so
  the `source_not_string` check fires, and `namespace` + `provider_config` extracted from the
  seed exactly as the Python handler reads them from the plan config
  (`_date_shift.py` cfg usage). Returns the coded reason or `None`. This is the one real
  signature-bridge in the slice; the builder confirms the seed -> (namespace, provider_config)
  mapping against `_date_shift.py` and the resolver, and the decline tests (§3 case 3) pin
  every code. (`date_shift` is not an OOC operator, so there is no existing `decline_reason`
  wrapper to reuse as `_bucket_admit` reused the OOC one; this adapter is new but thin.)
- `date_shift_op(load)`: `NativeOp(DATE_SHIFT, SEQ_DATE_SHIFT_DISPOSITION_KEY, load,
  _date_shift_admit, <apply unused>)` with `fail_closed=None` (format errors survive, they do
  NOT kill) and `warn=None`. NOTE: unlike hash/fpe/categorical, `date_shift` does NOT route
  through `NativeOp.apply`/`NativeResult` — its kernel returns a tuple (values + positions),
  so the dedicated seam (§2.2) calls `native_date_shift` directly. The `apply` slot is either
  a trivial unused stub or omitted via an Optional; the builder picks the cleaner of (a) make
  `NativeOp.apply` Optional and leave it None for date_shift, or (b) a stub that raises
  `AssertionError("date_shift uses date_shift_column, not apply")`. Prefer (a) if it does not
  disturb the other tenants' typing; else (b). The fpe fail-closed kill-channel
  (`NativeResult.errors` + `fail_closed`) is UNTOUCHED.
- Loader (`_sequential_native.py`): `_load_date_shift(native_threads) ->
  BoundIndexKernel(load_compiled_index_kernel(), native_threads)` (identical to
  `_load_bucket`). Register `DATE_SHIFT: date_shift_op(_load_date_shift)` in `_OPS`.

### 2.2 The dedicated `date_shift_column` seam (the novelty) — `_sequential_native.py`
Mirrors `bucket_column`, minus the range guard, plus the row-error append. Order:
1. Preconditions: `ctx.nested_outer_column or self._in_remap` -> None; decision missing or
   `decision.strategy != DATE_SHIFT` -> None; `decision.rows += rows`; `not decision.native or
   rows == 0 or bound is None` -> None.
2. `preserved = preserve.mask(df[column])` from the ORIGINAL pandas column (if preserve set).
3. `array = self._pinned_array(df[column], decision)` (raises
   `sequential_date_shift_input_type_drift` on conversion failure).
4. `derive_calls: list[int] = []`; call the kernel DIRECTLY:
   `out, positions = native_date_shift(array, min_days=..., max_days=..., date_format=...,
   mask_key=ctx.mask_key, namespace=..., index_kernel=bound.kernel,
   native_threads=bound.native_threads, derive_calls=derive_calls, preserved=preserved)`.
5. `decision.native_calls += sum(derive_calls)`.
6. SURVIVE-AND-REPORT: for each `pos in positions`, append
   `RowError(column=column, row_index=int(pos), trigger="format_error",
   reason=FORMAT_ERROR_REASON)` to `ctx.row_errors` — byte-identical to `_date_shift.py:201-209`.
   These do NOT raise; the run continues (the per-node drain + fail-loud classification in
   `run_sequential` handle them exactly as for the Python handler).
7. preserve: if `preserved is not None`, `count = int(preserved.sum())`,
   `decision.counts_preserved = True`, `decision.preserved_rows += count`,
   `ctx.record_date_preserved(column, count)` (once, including zero).
8. Return `out.to_pylist()`.
A `DateShiftCall` payload dataclass (parallel to `BucketCall`, adding `min_days`, `max_days`)
MAY carry the kwargs, or they pass as method kwargs since the seam calls the kernel directly;
the builder picks the clearer. No `op.apply`, no `NativeResult` for date_shift.

### 2.3 Handler seq_native block (`_strategies/_date_shift.py`)
Add to `DateShiftStrategyHandler.run`, after namespace/cfg/min-max/preserve/format resolution
and gated on a FALSY `group_by` (and `anchor_col is None`), before the Python
`preserved`/`_shift_rows` block (~:177):
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
`out is None` falls through to the Python path unchanged (config decline, companion absent,
FK-key boundary, or group_by). The native path having appended its own RowErrors, the Python
path does NOT also run for that column (no double-report). `fmt` here must be the resolved
EXPLICIT `date_format`; admission declines a missing/autodetect format, so the native path
never calls `_detect_format`.

### 2.4 Evidence (generalize the D2c-iii rule)
`ColumnDecision.entry()` `calls_decide` (`_sequential_native.py:147`) currently special-cases
`self.strategy == BUCKET_PERTURB`. Generalize to `self.strategy in (BUCKET_PERTURB, DATE_SHIFT)`
so a native `date_shift` column reports from actual `derive_calls`:
- `native_calls > 0` -> `executed_backend="rust_companion"`.
- `native_calls == 0` with rows (all-null / all-unparseable) -> `executed_backend="none"` +
  `reason="no_native_date_shift_calls"` (parallel to `no_native_bucket_calls`).
- empty selection (rows == 0) -> the existing `none` + `no_rows_hashed` skip.
IMPORTANT evidence subtlety to pin: an ALL-UNPARSEABLE non-null column makes ZERO derive
calls (kernel skips the derive when nothing is usable) YET appends format-error RowErrors.
So `executed_backend="none"` AND `ctx.row_errors` non-empty is a valid, correct combination;
the row-error report does not depend on a kernel call. (`preserved_rows` appears only when
`counts_preserved`.)

### 2.5 FK boundary (inherited, no new code)
`date_shift` is non-hash, so `prepare_table` gives it a native decision only off the FK-key
boundary. A `date_shift` FK-key column stays Python. preserve + group_by on an FK shape is
already compile-rejected; group_by is non-native regardless.

### 2.6 Module size (WATCH)
`_sequential_native.py` is 539 LOC; `date_shift_column` (~40-45) + the `calls_decide`
generalization (~2) projects ~585 — UNDER 600 but tight. `_sequential_native_ops.py` 308 +
the admit adapter + op + constants (~35) projects ~343. If `_sequential_native.py` would
cross 600 the builder STOPS and escalates (no census entry to force it; consider whether the
admit adapter or a `DateShiftCall` helper belongs in the ops module to keep the driver lean).

## 3. ACCEPTANCE TESTS (behavior before impl)

Companion-INDEPENDENT discipline: the `*_seq.py` + any `*_gap.py` file MUST pass with NO
companion (stand-in index kernel via monkeypatch) and MUST be run in the no-companion
`~/.cache/decoy-ci-mirror-venv` AND the abi-3 venv before the PR (Done-when). New helpers
`tests/parity/_d2c_date_shift_data.py` parallel `_d2c_bucket_data.py`: a `DateShiftMarkingKernel`
(returns `pool_size - 1` so offset == `max_days`, distinguishable), a `ReferenceDateShiftKernel`
(reproduces the keyed draw for parity), `install_date_shift`/`hide_date_shift` (patch
`_sequential_native.load_compiled_index_kernel`), shapes + an `expect_shift` helper.

`tests/parity/test_phase_d_d2c_iv_date_shift_seq.py` (stand-in, no companion):
1. **Native witness.** An admitted payload column carries the distinguishable shifted values
   (the kernel offset) in the emitted table; value-equality alone cannot prove it.
2. **Discarded-native-result fails the proof.** Dropping the native result yields the Python pick.
3. **Config-aware decline, value-free coded reasons** — one case each:
   `date_shift_requires_namespace`, `date_shift_group_by_not_native`,
   `date_shift_requires_date_format` (missing/empty/autodetect), `date_shift_special_date_format`,
   `date_shift_timezone_directive`, `date_shift_preserve_values_invalid`,
   `date_shift_min_days_not_int` / `date_shift_max_days_not_int`,
   `date_shift_min_days_out_of_range` / `date_shift_max_days_out_of_range` (abs > 106_751),
   `date_shift_source_not_string:<col>:<type>`. Each declines to Python, `kernel.calls == []`,
   reason carries name/type only (no cell value).
4. **ROW-ERROR PARITY (the slice novelty).** A column mixing parseable, non-null UNPARSEABLE,
   and null rows: (a) the emitted output equals the Python oracle (`run_full`) byte-for-byte
   (unparseable + null rows pass through unchanged, parseable rows shifted); (b) the run does
   NOT fail-close (survives); (c) `ExecutionResult.row_errors` equals the oracle's exactly —
   same `column`, `row_index`, `trigger="format_error"`, `reason=FORMAT_ERROR_REASON`, same
   count and order; (d) null rows are NOT reported as errors. Assert against `run_full`.
5. **preserve + row-error rebasing.** With a `preserve_values` sentinel list where an
   unparseable row sits AFTER a preserved row: the preserved sentinel survives, the
   `record_date_preserved` count equals the oracle, and the format-error `row_index` is the
   correct FULL-FRAME position (preserve-rebased), matching the oracle. Include an
   all-preserved column and a gapped `when`.
6. **Kernel-fault abort (distinct from format errors).** A KERNEL fault (companion raises)
   aborts the run (no Python retry, no partial; transactional sink commits nothing, callable
   sink keeps delivered tables) — this is the fail-closed path, SEPARATE from the surviving
   format errors of case 4.
7. **FK-key boundary.** A `date_shift` key column gets no native decision, matches the oracle,
   and does not disturb a native hash payload beside it.
8. **Evidence.** all-null -> `none` + `no_native_date_shift_calls`, no row errors;
   ALL-UNPARSEABLE (non-null) -> `none` + `no_native_date_shift_calls` AND row_errors non-empty
   (zero kernel calls, errors still reported — pin this combination); 1-parseable-row ->
   `rust_companion`; empty selection -> skip.
9. **when-subset selectivity** (only selected rows shifted natively; row-error positions are
   within the selected frame as the oracle reports them).
10. **Cross-strategy isolation.** hash + fpe + categorical + bucket + date_shift in varied
    column orders each mask correctly; the hash/fpe/categorical/bucket disposition fragments
    stay BYTE-IDENTICAL with date_shift present.
11. **Registry.** Update `test_an_unwired_strategy_has_no_descriptor` (cat_seq) so
    `set(_OPS) == {hash,fpe,categorical,bucket_perturb,date_shift}` and the unwired
    parametrize is `["text_mask"]` only; update
    `test_the_generalized_metrics_iterate_the_registered_descriptors` if it enumerates keys.
12. **No date_shift -> no `sequential_date_shift` key.**
13. **Companion absence** (`hide_date_shift`) -> column runs Python with the
    `crypto_extension_unavailable` reason.
14. **group_by column stays Python** (seam not engaged; the snapshot-anchor path runs).
15. **native_threads spy** reaches the kernel (D2c-iii precedent).
16. **The usable/unusable predicate matches the oracle:** native `unusable & ~source_null`
    (`_date_shift_ext.py:216`) == oracle `_shift_rows` (`_date_shift.py:289`) — a column whose
    error set is non-trivial (some null, some unparseable, some parseable) yields the identical
    error_rows on both paths.

`tests/parity/test_phase_d_d2c_iv_date_shift_real.py` (real kernel, skip-if-no-companion):
real-kernel byte-identical parity vs the Python handler, INCLUDING a decomposed-Unicode
format literal + non-ASCII unparseable rows (strict byte-identity; dates NFC-normalize via
`_canonicalize_source` as in bucket, so no NFC exemption), and a real row-error parity case.

Regression (stay green): full-frame/chunked/OOC/acyclic-SEQ unchanged; the d1b1/d2c bucket +
categorical + hash SEQ suites; public-import-boundary + module-size + log + physical-seam
sentries. Specifically confirm the Python date_shift handler path is UNCHANGED for group_by /
declined / companion-absent columns.

## 4. FAILURE MODES
- Non-null unparseable date -> SURVIVES (passes through unchanged) + a `format_error` RowError
  appended to `ctx.row_errors` (NOT a kill). Null rows: pass through, no error.
- Kernel fault (companion raises) -> run ABORTS (fail-closed; no retry/partial).
- Config decline (namespace/group_by/format/tz/bounds/preserve/non-string) -> Python, coded
  value-free reason.
- Companion absent -> Python (`crypto_extension_unavailable`).
- Input-type drift at `_pinned_array` -> `sequential_date_shift_input_type_drift` raises.
- NO runtime range decline (admission bounds the shift magnitude at +/-106_751 days).

## 5. GATE / DONE-WHEN
- Codex PLAN gate GO (this doc) BEFORE build.
- Build (Sonnet) from the GO plan; SELF-CHECK; VERIFY (Opus): coverage 100% line+branch on the
  changed units + mutation on the added logic (pay special attention to the row-error append
  loop, the usable/unusable predicate, and the preserve-rebased positions — the D2c-iii lesson
  was a mis-judged "equivalent" boundary mutant, so verify equivalence claims empirically);
  both `*_seq` + `*_real` suites PASS in the no-companion ci-mirror venv AND the abi-3 venv.
- dennis REVIEW green; remediate. Codex FINAL gate GO.
- Merge only on full green gate chain + Slack flag before merge-to-main; PR CI green; main CI
  green after merge.
- DOCUMENT: platform ROADMAP (D2c-iv DONE, NEXT = D2c-v text_mask/text_redact) + RECENTLY-SHIPPED;
  refresh the engine graphify graph against the new main; plain-English Slack recap.

## 6. FOLLOW-UPS / TRACKED (not this slice)
1. Pre-GA operator-neutral empty-selection disposition reason (now covers `no_native_bucket_calls`
   + `no_native_date_shift_calls` + the shared `no_rows_hashed`): a single operator-neutral code,
   deferred because it would churn the byte-identical hash/fpe/categorical fragments.
2. The chunked-route date-range guard gap (`_operator_step.py:443`, carried from D2c-iii).
3. D2c-v = text_mask / text_redact on the SEQ route; then the pure-Arrow trio
   (redact/truncate/passthrough) + REMAP generalization; then platform sequential activation
   (gated after A3 + Cam go).
