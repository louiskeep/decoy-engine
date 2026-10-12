# Phase D / D2c-iii: `bucket_perturb` native on the SEQ route

Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing

Slice owner: Opus (author) -> Codex plan-gate -> Sonnet (build) -> dennis -> Codex final.
Branch: `feat/phase-d-d2c-iii-seq-date-kernels` (worktree `.claude/worktrees/phase-d-d2c-iii`), cut from main `fc6fe86e` (D2c-ii merged).

## 1. FRAME

### What
Register `bucket_perturb` as the fourth tenant of the SEQ-native descriptor framework
(after `hash`, `fpe`, `categorical`), so `bucket_perturb` payload columns mask on the
compiled index kernel on the bounded sequential (SEQ) FK route, byte-identical to the
Python handler, reusing the D1b-i OOC kernel and the D1c `preserve_values` contract.

### Why
`bucket_perturb` (date coarsening: shift a date to a week/month/quarter bucket) already
runs native on the out-of-core (OOC) and chunked routes (D1b-i, engine #239). The SEQ
route carries the large relational jobs the OOC route declines; D2a/D2b made `hash`
native there, D2c-i/ii added `fpe` and `categorical` through the descriptor framework.
This slice adds the first DATE operator to that framework and, in doing so, builds the
shared seam work (`preserve_values` threading, date-range guard reuse, derive-call
evidence) that `date_shift` (D2c-iv) then reuses.

### Scope (this slice)
- `bucket_perturb` NON-KEY payload columns native on SEQ via the index kernel.
- Lift the date-range guard into a shared helper both OOC and SEQ call (closes the prior
  Codex NO-GO: "bucket date-range guard in wrapper not raw kernel").
- Thread D1c `preserve_values` through the SEQ seam (compute mask, pass `preserved=`,
  record the count). Closes the prior NO-GO: "D1c preserve_values omitted".
- SEQ-scoped, value-free coded decline + abort reasons.
- Generalized `metrics()` already picks up a new op automatically; add the new
  disposition key.

### Non-scope (explicit)
- **`date_shift` is deferred to D2c-iv.** `date_shift` has a native kernel
  (`native_date_shift`, `native/_date_shift_ext.py:124`) but returns a third output
  channel (`format_error_positions`: survive-and-report RowErrors) that the current SEQ
  `NativeResult` (values + fail-closed `errors`) and `native_column` seam do not model.
  Adding that survive-and-report row-error channel is a distinct seam extension. D2c-iii
  deliberately ships `bucket_perturb` only (no `NativeResult` change) and leaves the
  row-error channel to D2c-iv, which reuses this slice's preserve/evidence foundation.
  Rationale recorded for the plan-gate: splitting keeps each gate focused; D2c-ii already
  rescoped these two operators out of itself for the same reasons; smaller slices have
  gated cleanly across D2c.
- No FK-key native path for `bucket_perturb`: native REMAP is hash-only, so a
  `bucket_perturb` column used as an FK parent/child key stays Python (inherited from the
  existing non-hash `prepare_table` boundary; no new code).
- No `group_by` native path (compile-rejected / admission-declined; stays Python).
- No chunked-route change. (The chunked route's own guard omission, `_operator_step.py:443`,
  is noted as a pre-existing question for the planner but is OUT OF SCOPE here; see §6.)

### Reuse (established methodology)
- Kernel: `native_bucket_perturb` (`native/_bucket_perturb_ext.py:180`), unchanged.
- Loader: `load_compiled_index_kernel` (same loader `categorical` uses).
- Admission: the route-neutral `bucket_perturb_config_rejection` resolver
  (`native/_operator_config_rejections.py`).
- preserve: `transforms/date_preserve.py` `PreserveSet.mask` + `StrategyContext.record_date_preserved`.
- Descriptor framework: `_sequential_native_ops.py` / `_sequential_native.py` exactly as
  `categorical` plugged in (D2c-i/ii).

## 2. DESIGN

### 2.1 Descriptor registration
Add to `_sequential_native_ops.py`:
- `BUCKET_PERTURB = "bucket_perturb"` strategy constant and
  `SEQ_BUCKET_DISPOSITION_KEY = "sequential_bucket_perturb"`.
- `BucketCall` frozen payload: `namespace: str`, `mask_key`, `bucket: str`,
  `date_format: str`, `preserve: PreserveSet | None` (carries everything the kernel +
  preserve threading need; parallels `CategoricalCall`).
- `bucket_perturb_op(load)` factory:
  ```
  NativeOp(BUCKET_PERTURB, SEQ_BUCKET_DISPOSITION_KEY, load, _bucket_admit, _bucket_apply)
  ```
  `fail_closed=None` (a kernel fault or the range guard raises directly and aborts the
  run, matching `categorical`); `warn=None` (bucket emits no residual warnings).
- `_bucket_admit(column, input_type, seed)`: thin adapter over the OOC
  `bucket_perturb_config_rejection` using the one-field schema stand-in, exactly as
  `_categorical_admit` does. Returns a value-free coded reason or `None`. An invalid
  `preserve_values` config declines here (`...preserve_values_invalid`) so the handler
  then raises its coded `StrategyError` on the Python path (mirrors categorical's
  unbuildable-CDF precedent).
- `_bucket_apply(bound, values, call)`:
  1. `_guard_native_range(values, call.bucket, call.date_format, <column>)` (the SHARED
     guard, see §2.2) — raises the coded range error, aborting the run, if any parseable
     date is within one bucket of the pandas Timestamp bounds.
  2. compute `preserved = call.preserve.mask(<pandas view>)` if `call.preserve` else None.
  3. `out = native_bucket_perturb(values, bucket=..., date_format=..., mask_key=...,
     namespace=..., index_kernel=bound, preserved=preserved)`.
  4. return `NativeResult(out)` plus the preserved count (see §2.4 for how the count
     reaches `ctx.record_date_preserved` — via the dedicated `bucket_column` seam, not the
     generic `native_column`).

Register `BUCKET_PERTURB: bucket_perturb_op(_load_bucket)` in `_OPS`
(`_sequential_native.py:195`), with `_load_bucket = load_compiled_index_kernel`
(its own per-strategy entry in `_bound`, keyed by strategy, even though the loader
function is shared with categorical).

### 2.2 Shared date-range guard (closes NO-GO #1)
Lift `_guard_native_range` + `_BUCKET_REACH_DAYS` out of `out_of_core/_bucket_native.py`
into a shared location beside the raw kernel (proposed: `native/_bucket_perturb_ext.py`,
a module both routes already import) as a public helper, e.g.
`guard_native_date_range(values, bucket, date_format, column)`. OOC's
`native_bucket_perturb_array` then calls the shared helper (behavior unchanged); the SEQ
`_bucket_apply` calls the same helper. The guard raises a route-neutral coded
`ExecutionError`; SEQ surfaces it as the run-aborting exception (no Python retry, no
partial output). Acceptance: byte-for-byte identical guard behavior on both routes
(a near-boundary date aborts on SEQ exactly as on OOC), proven by a parity test that
asserts SEQ aborts iff `run_full` (the oracle) aborts on the same near-boundary input
(fail-like-the-oracle, the D1b-i contract).

NOTE for the gate: this refactor must be behavior-preserving for OOC. The OOC decline code
`out_of_core_bucket_perturb_date_out_of_native_range` stays the OOC-facing code; the shared
helper raises a route-neutral code that each route maps/surfaces. Pin the OOC code is
unchanged by a regression assertion.

### 2.3 Handler seq_native block (`_strategies/_bucket_perturb.py`)
After `bucket, date_format, preserve = _validated_config(plan)` and the namespace check,
before the Python `apply_excluding_preserved` block, add the seam (mirrors categorical):
```
seq = getattr(ctx, "seq_native", None)
if seq is not None:
    out = seq.bucket_column(
        ctx, df, column,
        namespace=plan.namespace, mask_key=ctx.mask_key,
        bucket=bucket, date_format=date_format, preserve=preserve,
    )
    if out is not None:
        df[column] = out
        return df, []
```
`out is None` => fall through to the existing Python path unchanged. No warnings phase.

### 2.4 Dedicated `bucket_column` seam (`_sequential_native.py`)
Parallels `categorical_column`. Reasons for a dedicated method (not the generic
`native_column`): it must (a) thread the `preserve` mask into the kernel via `BucketCall`,
(b) record the preserved count through `ctx.record_date_preserved(column, count)`, and
(c) record derive-call-based evidence (§2.5). Preconditions identical to `native_column`
(returns `None` -> Python when nested/in_remap/no decision/strategy mismatch/not
native/rows==0/bound None). Uses `_pinned_array` for the pinned Arrow source type (raising
`sequential_bucket_perturb_input_type_drift` on conversion failure, consistent with the
existing `sequential_<strategy>_input_type_drift`).

### 2.5 Evidence semantics (all-null / all-unparseable)
`bucket_perturb` SKIPS the derive call for all-null/all-unparseable batches (kernel
`_grouped_offsets`), so "native but zero kernel calls" is possible — unlike `categorical`,
which always calls the index kernel. The SEQ `ColumnDecision` currently tracks only
`rows`. Extend the bucket decision to record whether the kernel was reached (derive-call
evidence), aligned with the OOC `BucketColumnDecision` (`native_calls` + `preserved_rows`):
- kernel reached for >=1 parseable, non-preserved row => `executed_backend="rust_companion"`.
- zero parseable/non-preserved rows (all-null / all-unparseable / all-preserved) => a
  coded native entry with `reason="no_native_bucket_calls"` (distinct from categorical's
  `no_rows_hashed`), still attributed native (the column was admitted + routed native).
- empty selection (no rows at all) => the existing `"none"` / `no_rows_hashed`-style skip.
Pin each of these in the stand-in suite.

### 2.6 FK boundary (inherited, no new code)
`bucket_perturb` is non-hash, so `prepare_table` (`_sequential_native.py:260-264`) gives it
a native decision only when the column is neither an FK child nor a parent key. A
`bucket_perturb` FK key column stays Python. A preserve-bearing column can only be a
payload or a parent key (compile guards `plan/_checks_date_shift.py` reject preserve on
unsupported FK shapes / child side), consistent with this boundary. Pin: a
`bucket_perturb` key column gets no native decision and matches the Python oracle, and does
not disturb a native hash payload beside it.

### 2.7 Module size
`_sequential_native_ops.py` 251 LOC (+ ~40 for the op/admit/apply/payload = ~291, < 600,
no census). `_sequential_native.py` 470 LOC (+ ~35 for `bucket_column` + evidence = ~505,
< 600, no census). If either crosses 600 the builder STOPS and escalates (do not add a
census entry to force it through). The guard-lift REMOVES lines from
`out_of_core/_bucket_native.py`.

## 3. ACCEPTANCE TESTS (define behavior before impl)

Companion-INDEPENDENT discipline (the D2c-ii CI-miss lesson): the `*_seq.py` parity file
MUST pass with NO companion — install a stand-in index kernel via monkeypatch, never
require the real Rust companion. The `*_real.py` file needs the companion and SKIPS when
absent. Both new files MUST be run in the no-companion `~/.cache/decoy-ci-mirror-venv`
BEFORE opening the PR (Done-when).

New helpers `tests/parity/_d2c_bucket_data.py`:
- `BucketMarkingKernel`: a `derive_index_batch` stand-in returning a DISTINGUISHABLE
  offset (e.g. always 0, i.e. floor-to-bucket with no perturbation, or a fixed marker) so
  a discarded-native-result test can tell kernel output from the Python pick.
- `install_bucket` / `hide_bucket` (monkeypatch `_sequential_native.load_compiled_index_kernel`),
  `bucket_seed`, `bucket_payload_shape`, `IndexCall` record.

`tests/parity/test_phase_d_d2c_iii_bucket_seq.py` (stand-in, no companion) must cover:
1. **Native witness.** An admitted `bucket_perturb` payload column is consumed FROM the
   kernel (the emitted table carries the distinguishable marked values; value-equality
   alone cannot prove it — SEQ rebuilds buffers).
2. **Discarded-native-result fails the proof.** If the native result is dropped, the
   emitted value is the Python pick, not the marker.
3. **Config-aware decline, value-free coded reasons.** non-namespaced / non-string-source
   / unsupported-bucket / missing-date-format / tz-directive / special-date-format /
   invalid-preserve each decline to Python with the coded reason; `kernel.calls == []`.
   Reason text carries strategy + column + Arrow type only (no data values).
4. **Range-guard abort (NO-GO #1).** A date within one bucket of the pandas Timestamp
   bounds aborts the SEQ run with the coded range error, no Python retry, no partial
   output; and SEQ aborts IFF `run_full` aborts on the same input (fail-like-the-oracle).
5. **preserve_values parity (NO-GO #2).** With a `preserve_values` sentinel list: preserved
   rows pass through byte-identical to the oracle; `ctx.record_date_preserved` count equals
   the oracle's; non-preserved rows mask native; a boundary sentinel (e.g. `9999-12-31`)
   survives.
6. **Kernel-fault abort + sink-qualified no-partial-output.** A kernel fault raises and
   aborts; a transactional sink commits nothing; a callable sink keeps already-delivered
   tables.
7. **FK-key boundary.** A `bucket_perturb` key column gets no native decision, matches the
   Python oracle, and does not disturb a native hash payload beside it.
8. **Evidence.** all-null/all-unparseable => native entry with `no_native_bucket_calls`;
   a 1-parseable-row column => `rust_companion`; empty selection => skip entry.
9. **when-subset selectivity.** A `when:` subset runs only the selected rows natively,
   byte-identical to the oracle.
10. **Cross-strategy isolation.** hash + fpe + categorical + bucket_perturb in varied
    column orders each mask correctly; the hash/fpe/categorical disposition fragments stay
    BYTE-IDENTICAL with and without bucket present (the metrics() generalization must not
    perturb the other fragments).
11. **Descriptor registry.** UPDATE `test_an_unwired_strategy_has_no_descriptor` and
    `test_the_generalized_metrics_iterate_the_registered_descriptors`:
    `set(_OPS) == {"hash","fpe","categorical","bucket_perturb"}`; `date_shift` is still NOT
    in `_OPS` (deferred to D2c-iv) — assert that explicitly.
12. **A job without bucket_perturb has no `sequential_bucket_perturb` disposition key.**

`tests/parity/test_phase_d_d2c_iii_bucket_real.py` (real kernel, skip-if-no-companion):
real-kernel byte-identical parity vs the Python handler for uniform payload columns across
week/month/quarter buckets, including the accepted native-vs-Python NFC divergence note
(bucket output is formatted dates, so confirm whether NFC applies — date strings are
ASCII, so likely byte-identical with NO NFC nuance; state the finding).

Regression (must stay green): OOC bucket native unchanged (the guard-lift is
behavior-preserving; `out_of_core_bucket_perturb_date_out_of_native_range` code unchanged);
chunked/full-frame/acyclic-SEQ unchanged; the public-import-boundary + module-size sentries.

## 4. FAILURE MODES
- Kernel fault (companion raises) => run aborts, no Python retry, no partial output.
- Near-boundary date => coded range abort via the shared guard (fail-like-the-oracle).
- Input-type drift at `_pinned_array` => `sequential_bucket_perturb_input_type_drift` raises.
- Invalid `preserve_values` config => admission declines (`...preserve_values_invalid`),
  handler raises its coded `StrategyError` on the Python path.
- Companion absent => admission declines with the index-unavailable reason; the whole
  column runs Python (the `*_seq.py` suite proves this with `hide_bucket`).

## 5. GATE / DONE-WHEN
- Codex PLAN gate GO (this doc) BEFORE build; the scoping decision (bucket-only,
  date_shift -> D2c-iv) is explicitly up for review.
- Build (Sonnet) from the GO plan; SELF-CHECK; VERIFY (Opus): coverage 100% line+branch on
  both changed units + mutation on the added logic; both `*_seq.py` + `*_real.py` suites
  PASS in the no-companion ci-mirror venv AND the companion abi-3 venv.
- dennis REVIEW (fresh-context adversarial) green; remediate findings.
- Codex FINAL gate GO.
- Merge only on full green gate chain + Slack flag before merge-to-main; wait for PR CI
  green; confirm main CI green after merge.
- DOCUMENT: platform ROADMAP (D2c-iii DONE, NEXT=D2c-iv) + RECENTLY-SHIPPED; refresh the
  engine graphify graph against the new main; plain-English Slack recap.

## 6. OPEN QUESTIONS FOR THE PLAN-GATE
1. The chunked route (`_operator_step.py:443`) also skips `_guard_native_range`. Is that a
   known accepted gap (chunked guarantees in-range by another means), or a latent bug? This
   slice does not change chunked, but the guard-lift makes the shared helper available to
   chunked too — flag whether to wire it there now or track separately.
2. Confirm the SEQ bucket evidence reason string (`no_native_bucket_calls`) and whether the
   `ColumnDecision` should grow a `native_calls`/`preserved_rows` pair (OOC parity) or stay
   minimal — the plan proposes the former for accuracy.
3. Confirm date-string output carries no NFC divergence (ASCII dates) so the real-parity
   test asserts strict byte-identity with no NFC carve-out.
