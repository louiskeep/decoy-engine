# Phase D / D2c-iii: `bucket_perturb` native on the SEQ route

Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing

Slice owner: Opus (author) -> Codex plan-gate -> Sonnet (build) -> dennis -> Codex final.
Branch: `feat/phase-d-d2c-iii-seq-date-kernels` (worktree `.claude/worktrees/phase-d-d2c-iii`), cut from main `fc6fe86e` (D2c-ii merged).

Revision: rev 2 (addresses Codex plan-gate NO-GO on rev 1: 3 HIGH + 1 MED). Changes from rev 1
are called out inline as `[rev2]`.

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
This slice adds the first DATE operator to that framework and builds the shared seam work
(`preserve_values` threading, a shared date-range predicate, derive-call evidence) that
`date_shift` (D2c-iv) then reuses.

### Scope (this slice)
- `bucket_perturb` NON-KEY payload columns native on SEQ via the index kernel.
- Extract the date-range check into a shared PREDICATE both routes call, keeping each
  route's ACTION its own: OOC keeps aborting (it streams and cannot re-buffer); SEQ
  DECLINES the whole column to Python (it can cleanly fall back). `[rev2]` This closes the
  prior NO-GO ("guard in wrapper not raw kernel") without the false "SEQ aborts like the
  oracle" claim that rev 1 made.
- Thread D1c `preserve_values` through the SEQ seam with the OOC ordering: compute the
  mask first, exclude preserved rows, range-check and mask only the remainder, record the
  count exactly once. `[rev2]` Closes the prior NO-GO ("preserve omitted") AND the rev-1
  guard-before-preserve ordering bug.
- SEQ-scoped, value-free coded decline reasons; derive-call-based execution evidence that
  reports `executed_backend="none"` for a zero-call column. `[rev2]`

### Non-scope (explicit)
- **`date_shift` is deferred to D2c-iv.** Its native kernel (`native/_date_shift_ext.py:124`)
  returns a third survive-and-report row-error channel (`format_error_positions`,
  `:136`) that the current SEQ `NativeResult` (values + fail-closed `errors`) does not
  model. D2c-iii ships `bucket_perturb` only (no `NativeResult` change); the preserve mask
  + derive-call evidence built here are reusable by D2c-iv without that channel. Codex
  plan-gate confirmed this split is sound.
- No FK-key native path: native REMAP is hash-only, so a `bucket_perturb` column used as
  an FK parent/child key stays Python (inherited from the non-hash `prepare_table`
  boundary at `_sequential_native.py:260`; no new code).
- No chunked-route change. The chunked raw-kernel call (`_operator_step.py:443`) has no
  range guard and Codex found no range guarantee there; this is tracked as a SEPARATE
  correctness follow-up (§6.1), NOT fixed here and NOT accepted as safe. `[rev2]`

### Reuse (established methodology)
- Kernel: `native_bucket_perturb` (`native/_bucket_perturb_ext.py:180`), unchanged.
- Loader: `load_compiled_index_kernel` wrapped as `BoundIndexKernel` (see §2.1).
- Admission: the route-neutral `bucket_perturb_config_rejection` resolver
  (`native/_operator_config_rejections.py:114`).
- preserve: `transforms/date_preserve.py` `PreserveSet.mask`, `StrategyContext.record_date_preserved`.
- Range predicate + evidence: lifted from OOC (`out_of_core/_bucket_native.py`), see §2.2/§2.5.

## 2. DESIGN

### 2.1 Descriptor registration `[rev2 — loader + payload + apply contract corrected]`
Add to `_sequential_native_ops.py`:
- `BUCKET_PERTURB = "bucket_perturb"` and `SEQ_BUCKET_DISPOSITION_KEY = "sequential_bucket_perturb"`.
- `BucketCall` frozen payload carrying EVERYTHING the apply + evidence need:
  `namespace: str`, `mask_key: bytes`, `bucket: str`, `date_format: str`, `column: str`,
  `preserved: np.ndarray | None` (the mask the seam precomputed), and
  `derive_calls: list[int]` (an invocation-local accumulator the kernel appends to, which
  the seam reads back for evidence). The preserved count is NOT returned from apply; it is
  computed in the seam from the mask. `[rev2 — fixes HIGH #3]`
- `bucket_perturb_op(load)`:
  ```
  NativeOp(BUCKET_PERTURB, SEQ_BUCKET_DISPOSITION_KEY, load, _bucket_admit, _bucket_apply)
  ```
  `fail_closed=None` (a kernel fault raises and aborts, matching categorical); `warn=None`.
- `_bucket_admit(column, input_type, seed)`: thin adapter over the route-neutral
  `bucket_perturb_config_rejection`, exactly as `_categorical_admit`. Returns a value-free
  coded reason or `None`. An invalid `preserve_values` config declines here
  (`..._preserve_values_invalid`) so the handler raises its coded `StrategyError` on the
  Python path (mirrors categorical's unbuildable-CDF precedent).
- `_bucket_apply(bound, values, call) -> NativeResult`: a PURE apply (no guard, no tuple).
  ```
  out = native_bucket_perturb(
      values, bucket=call.bucket, date_format=call.date_format,
      mask_key=call.mask_key, namespace=call.namespace,
      index_kernel=bound.kernel, native_threads=bound.native_threads,
      derive_calls=call.derive_calls, preserved=call.preserved,
  )
  return NativeResult(out)
  ```
  `[rev2 — fixes HIGH #3: apply returns exactly NativeResult; the guard + preserve-count + evidence move to the seam §2.4]`

Loader (`_sequential_native.py`, beside `_load_categorical`), mirroring it EXACTLY so the
`_kernel` thread-budget dispatch (`:224`) stays valid: `[rev2 — fixes HIGH #3]`
```
def _load_bucket(native_threads: int | None) -> BoundIndexKernel:
    return BoundIndexKernel(load_compiled_index_kernel(), native_threads)
```
Register `BUCKET_PERTURB: bucket_perturb_op(_load_bucket)` in `_OPS` (`:195`). bucket keeps
its own `_bound["bucket_perturb"]` entry (keyed by strategy) even though the underlying
loader is the same index kernel categorical uses.

### 2.2 Shared date-range PREDICATE (closes NO-GO #1, fixes rev-1 HIGH #1) `[rev2]`
rev 1 wrongly claimed SEQ should "abort iff the oracle aborts." Codex proved this false:
the Python transform (`transforms/bucket_perturb.py:126`) does bucket arithmetic with
`datetime.date`, which represents dates OUTSIDE the pandas Timestamp range, so `run_full`
SUCCEEDS on near-boundary dates the native guard refuses (verified: month-bucket
`1677-09-22`/`2262-04-10` map fine under Python; the OOC guard rejects them;
`test_phase_d_d1b1_bucket_baseline.py:128` pins the Python success). The native range limit
is a NATIVE-ARITHMETIC-ELIGIBILITY limit, not an oracle-parity limit.

Fix: separate the PREDICATE from the ACTION.
- Extract the near-boundary test from `_guard_native_range` into a shared, value-free
  predicate beside the raw kernel, e.g. in `native/_bucket_perturb_ext.py`:
  `native_range_exceeded(values, bucket, date_format) -> bool` (parse with the date_format,
  coerce, compare min/max against `pd.Timestamp.min/max +/- _BUCKET_REACH_DAYS[bucket]`;
  all-NaT returns False). `_BUCKET_REACH_DAYS` moves beside it.
- OOC keeps its ACTION unchanged: `_guard_native_range` becomes a thin wrapper that calls
  the predicate and RAISES `ExecutionError(code=REASON_OUT_OF_NATIVE_RANGE, ...)` with the
  EXACT existing code/message. OOC behavior is byte-for-byte preserved (regression-pinned).
- SEQ ACTION: the seam calls the predicate on the NON-PRESERVED rows (§2.4); if True, it
  DECLINES the whole column to Python (records a data-aware Python decline, returns None so
  the handler masks the column in Python). No abort, no per-row mixing, no retry.

This makes SEQ a strict superset of the Python result (declines to the exact Python path
when native cannot safely compute), so there is no new failing-job regression when the
companion is installed.

### 2.3 Handler seq_native block (`_strategies/_bucket_perturb.py`)
After `bucket, date_format, preserve = _validated_config(plan)` and the namespace check,
before the Python `apply_excluding_preserved` block, add (mirrors categorical):
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
`out is None` => fall through to the existing Python path unchanged (this is the path a
config-decline, a data-aware range decline, or an absent companion all take). No warnings.

### 2.4 `bucket_column` seam (`_sequential_native.py`) — orchestration `[rev2 — ordering + evidence]`
A dedicated method (not generic `native_column`), because it owns the preserve mask, the
data-aware decline, the preserved count, and the derive-call evidence. Order (mirrors OOC
`native_bucket_perturb_array`, which Codex cited as the correct contract):
1. Preconditions identical to `native_column` (return `None` -> Python when
   nested_outer_column / in_remap / no decision / strategy mismatch / not native /
   rows==0 / bound is None).
2. Compute `preserved = preserve.mask(df[column])` from the ORIGINAL selected pandas column
   (before Arrow reconstruction), if `preserve` is configured, else None. `[rev2 — HIGH #2: mask first]`
3. `array = self._pinned_array(df[column], decision)` (raises
   `sequential_bucket_perturb_input_type_drift` on conversion failure).
4. Range check on the NON-PRESERVED rows only: build `rest = array.filter(~preserved)` (or
   `array` when no preserve), then `if native_range_exceeded(rest, bucket, date_format):`
   record a data-aware Python decline on the decision
   (`backend=python`, `reason="sequential_bucket_perturb_date_out_of_native_range"`),
   and `return None` WITHOUT recording any preserved count (the Python handler owns all
   recording for a declined column, so the count is never double-counted). `[rev2 — HIGH #1 + HIGH #2]`
5. Otherwise build `BucketCall(namespace, mask_key, bucket, date_format, column,
   preserved=preserved, derive_calls=[])` and call `op.apply(bound, array, call)`; on
   `result.errors` raise `fail_closed` (None here => AssertionError guard, as native_column).
6. Evidence from the ACTUAL kernel behavior (not inferred): `native_calls = sum(call.derive_calls)`.
   Record `native_calls` and (`counts_preserved=True`, `preserved_rows=int(preserved.sum())`)
   when preserve is configured, via `ctx.record_date_preserved(column, preserved_rows)`
   exactly once (including zero). `[rev2 — HIGH #3: count in the seam]`
7. Return `result.values.to_pylist()`.

### 2.5 Execution evidence — mirror the OOC `BucketColumnDecision` `[rev2 — fixes MED #4]`
The SEQ bucket decision mirrors OOC `BucketColumnDecision` (`out_of_core/_bucket_native.py:76-113`):
fields `native_calls`, `counts_preserved`, `preserved_rows`; `entry()` computes
`ran = native_calls > 0` for a native-pinned column and emits:
- `native_calls > 0` => `executed_backend="rust_companion"`.
- `native_calls == 0` (all-null / all-unparseable / all-preserved) => `executed_backend="none"`,
  `reason="no_native_bucket_calls"`. A zero-call column NEVER claims Rust execution. `[rev2]`
- data-aware range decline (§2.4 step 4) => `executed_backend="python"`,
  `reason="sequential_bucket_perturb_date_out_of_native_range"`.
- config decline at admit => `executed_backend="python"`, `reason=<admit reason>`.
`preserved_rows` appears in the entry only when `counts_preserved`. The hash/fpe/categorical
serialization is untouched; `metrics()` already emits a new op's entries under its own key.
NOTE: if a Python-declined column still exposes a `preserved_rows` count, it must come from
the actual Python handler execution, not a default zero (Codex MED #4 tail).

### 2.6 FK boundary + group_by (inherited; group_by claim corrected) `[rev2]`
`bucket_perturb` is non-hash, so `prepare_table` (`:260`) gives it a native decision only
off the FK-key boundary (no native REMAP). A preserve-bearing column can only be a payload
or a parent key (compile guards reject preserve on child side / unsupported FK shapes),
consistent with this boundary. `[rev2]` Correction: rev 1 cited a bucket `group_by`
admission rule; the `group_by` compile rejection in `plan/_checks_date_shift.py` is
`date_shift`-specific. bucket_perturb has no `group_by` native carve-out to assert here;
drop that claim.

### 2.7 Module size
`_sequential_native_ops.py` 251 LOC + op/admit/apply/payload; `_sequential_native.py` 470 LOC
+ `bucket_column` + the decision evidence. Codex flags the real additions likely exceed the
rev-1 estimate but stay < 600. If either module crosses 600 the builder STOPS and escalates
(no census entry to force it through). The predicate extraction REMOVES lines from
`out_of_core/_bucket_native.py`.

## 3. ACCEPTANCE TESTS (behavior before impl) `[rev2 — split consumption vs parity; preserve cases hardened]`

Companion-INDEPENDENT discipline (the D2c-ii CI-miss lesson): the `*_seq.py` file MUST pass
with NO companion (stand-in kernel via monkeypatch) and MUST be run in the no-companion
`~/.cache/decoy-ci-mirror-venv` AND the abi-3 venv before the PR (Done-when). `[rev2]` Codex
note: a marking kernel proves CONSUMPTION but not oracle PARITY; oracle-parity cases need a
REFERENCE-EQUIVALENT stand-in (one that reproduces the real floor-to-bucket math), or they
belong in the real-kernel suite. Keep the two concerns in separate tests.

New helpers `tests/parity/_d2c_bucket_data.py`: `BucketMarkingKernel` (a `derive_index_batch`
stand-in with a DISTINGUISHABLE offset so consumption is provable), optionally a
`ReferenceBucketKernel` (reproduces real offsets for parity cases), `install_bucket`/
`hide_bucket` (monkeypatch `_sequential_native.load_compiled_index_kernel`), `bucket_seed`,
`bucket_payload_shape`, `IndexCall`.

`tests/parity/test_phase_d_d2c_iii_bucket_seq.py` (stand-in, no companion):
1. **Native witness (consumption).** An admitted payload column carries the distinguishable
   marked values in the emitted table (value-equality alone can't prove it; SEQ rebuilds
   buffers).
2. **Discarded-native-result fails the proof.** Dropping the native result yields the Python
   pick, not the marker.
3. **Config-aware decline, value-free coded reasons** (non-namespaced / non-string-source /
   unsupported-bucket / missing-date-format / tz-directive / special-date-format / invalid
   preserve) decline to Python, `kernel.calls == []`, reason carries strategy+column+type only.
4. **Data-aware range decline (HIGH #1).** A payload with a parseable NON-preserved
   near-boundary date (e.g. month-bucket `2262-04-10`) DECLINES the whole column to Python
   (not abort): the emitted column equals the Python oracle, the disposition records
   `executed_backend="python"` + `sequential_bucket_perturb_date_out_of_native_range`, and
   `kernel.calls == []` for that column. A companion-present run of the SAME job still
   succeeds (no new failure). `[rev2 — replaces the false abort-parity case]`
5. **preserve_values parity (HIGH #2).** (a) a preserved near-boundary sentinel
   (`2262-04-10` IN the preserve list) SURVIVES and does NOT trigger the range decline (the
   range check sees only non-preserved rows); (b) `9999-12-31` survives; (c) an ALL-preserved
   column; (d) a gapped `when` selection; each asserts the emitted column equals the oracle,
   `ctx.record_date_preserved` count equals the oracle's exactly (including zero when none
   preserved), and the kernel inputs EXCLUDE preserved rows. `[rev2 — adds parseable
   near-boundary sentinel + all-preserved + gapped-when; 9999-12-31 alone is insufficient
   since the guard coerces it to NaT]`
6. **Kernel-fault abort + sink-qualified no-partial-output.** A kernel fault raises and
   aborts; transactional sink commits nothing; callable sink keeps already-delivered tables.
7. **FK-key boundary.** A bucket key column gets no native decision, matches the oracle, and
   does not disturb a native hash payload beside it.
8. **Evidence (MED #4).** all-null / all-unparseable / all-preserved => `executed_backend="none"`
   + `no_native_bucket_calls`; a 1-parseable-row column => `rust_companion`; empty selection
   => skip entry. Assert via the kernel's `derive_calls`, not inferred parseability.
9. **when-subset selectivity.** A `when:` subset masks only selected rows natively, oracle-identical.
10. **Cross-strategy isolation + byte-identical fragments.** hash+fpe+categorical+bucket in
    varied column orders each mask correctly; the hash/fpe/categorical disposition fragments
    are BYTE-IDENTICAL with and without bucket present.
11. **Descriptor registry.** `set(_OPS) == {"hash","fpe","categorical","bucket_perturb"}`;
    `date_shift` still NOT in `_OPS` (assert explicitly); update
    `test_the_generalized_metrics_iterate_the_registered_descriptors`.
12. **No bucket => no `sequential_bucket_perturb` key.**
13. **Companion absence.** With `hide_bucket`, the column runs Python with the
    index-unavailable reason. `[rev2]`

`tests/parity/test_phase_d_d2c_iii_bucket_real.py` (real kernel, skip-if-no-companion):
real-kernel byte-identical parity vs the Python handler across week/month/quarter for
uniform payload columns, INCLUDING a decomposed-Unicode probe. `[rev2]` Codex note: "dates
are ASCII" is FALSE — the date_format can contain Unicode literals, and preserved/unparseable
strings can be non-ASCII; the Python derivation NFC-normalizes via `_canonicalize_source` and
a 28-row decomposed-Unicode probe matched the real kernel byte-for-byte. So assert STRICT
byte-identity with a decomposed-Unicode format-literal + non-ASCII preserved/unparseable
rows; NO NFC exemption.

Regression (must stay green): OOC bucket native unchanged (predicate extraction is
behavior-preserving; `out_of_core_bucket_perturb_date_out_of_native_range` code + message
unchanged, pinned by assertion); chunked/full-frame/acyclic-SEQ unchanged; public-import-
boundary + module-size sentries.

## 4. FAILURE MODES
- Kernel fault (companion raises) => run aborts, no Python retry, no partial output.
- Near-boundary NON-preserved date => data-aware DECLINE of the whole column to Python
  (NOT abort), coded `sequential_bucket_perturb_date_out_of_native_range`. `[rev2]`
- Near-boundary PRESERVED sentinel => survives via preserve, no decline. `[rev2]`
- Input-type drift at `_pinned_array` => `sequential_bucket_perturb_input_type_drift` raises.
- Invalid `preserve_values` config => admission declines, handler raises its coded `StrategyError`.
- Companion absent => admission declines (index-unavailable); whole column runs Python.

## 5. GATE / DONE-WHEN
- Codex PLAN gate GO (this rev) BEFORE build.
- Build (Sonnet) from the GO plan; SELF-CHECK; VERIFY (Opus): coverage 100% line+branch on
  the changed units + mutation on the added logic; both `*_seq.py` + `*_real.py` suites PASS
  in the no-companion ci-mirror venv AND the companion abi-3 venv.
- dennis REVIEW green; remediate findings. Codex FINAL gate GO.
- Merge only on full green gate chain + Slack flag before merge-to-main; wait for PR CI
  green; confirm main CI green after merge.
- DOCUMENT: platform ROADMAP (D2c-iii DONE, NEXT=D2c-iv) + RECENTLY-SHIPPED; refresh the
  engine graphify graph against the new main; plain-English Slack recap.

## 6. FOLLOW-UPS / TRACKED (not this slice)
1. **Chunked range guard gap.** The chunked raw-kernel call (`_operator_step.py:443`) has no
   range guard and no proven range guarantee (Codex). Now that the predicate is shared, file
   a correctness follow-up to wire it on the chunked path (decline or guard as appropriate).
   Do NOT change chunked behavior in this slice. `[rev2 — Codex qualification]`
2. D2c-iv: `date_shift` native on SEQ — needs the survive-and-report row-error channel on
   the SEQ seam; reuses this slice's preserve mask + derive-call evidence.
