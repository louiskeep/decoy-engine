Status: plan (Phase D / audit GAP8. rev 2, Codex plan-gate REVISE folded. Admit integer-typed fpe FK KEY columns to the compiled FF1 kernel on the SEQ route, byte-identical to the existing Python path.)

# fpe on an integer FK key native on the sequential route

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md`. Grounding: the 2026-10-14 bottleneck audit (GAP8: an fpe int64 FK key runs the per-cell Python oracle, ~1756s/10M, 2.9x over the 600s bar) + the shipped SEQ fpe-native slices (#243 D2c-i payload, #251 fpe-as-FK-key for string keys) + the FF1 compiled kernel (#232 C6a). Base: engine `origin/main` @ `c4dd75a9`.

## 0. Why + scope

On the SEQ (sequential FK) route, `fpe` masks natively only when the source arrow type is exactly `pa.string()`. An integer-typed FK key (the common surrogate-key shape) declines to the per-cell Python handler with `fpe_source_type_not_string:<col>:<type>` and masks ~1756s/10M.

This slice admits **integer FK KEY columns** (parent key, inherited child, orphan-REMAP mint) to the existing compiled FF1 kernel, byte-identical to the current Python path. It is a pure speed fix: the current Python path for an integer fpe column already emits a ciphered **string** column (`_strategies/_fpe.py:196` `str(v)` + `:242-247` object-list -> `df[column]`; `native/_crypto_reference.py:177` returns `pa.array(out, pa.string())`), and FF1 is numeral-based (`transforms/_ff1.py`), so the string-source requirement is an admission-gate choice, not a crypto limit.

### The Codex-BLOCKER boundary (why KEYS, not payload)
The SEQ route materializes the arrow table to pandas via `to_pandas_fk_safe` (`_fk_keys.py:492`). It **protects FK-relevant integer columns from float widening** (keeps them exact), but an ordinary **nullable integer PAYLOAD** column widens to `float64`. The Python handler then stringifies the float (`str(123456.0)` -> `"123456.0"`, and values beyond 2**53 lose precision), so a native path that stringified the exact integer (`"123456"`) would feed the cipher a different input and emit different ciphertext (reproduced by Codex: `[123456, null]` -> oracle `"164402.4"` vs naive-native `"245737"`; `int64` max -> oracle succeeds, naive-native drifts/aborts). FK keys do not hit this because `to_pandas_fk_safe` keeps them integer-exact, so the handler stringifies the same integer the native path would. Restricting to FK keys makes the native and Python inputs provably identical.

**In scope:** fpe FK KEY columns (parent key, inherited child, orphan-REMAP mint) whose source arrow type is a fixed-width **integer** (`int8/16/32/64`, `uint8/16/32/64`), AND whose materialized representation is confirmed integer-preserving (the drift guard below). The kernel is the one the string fpe key path already uses; output is the ciphered **string** column the Python path already produces.

**Out of scope / still DECLINE to Python (byte-identical), unchanged reasons:**
- **Integer PAYLOAD fpe columns** stay on Python this slice (a documented follow-up): a nullable integer payload is float-widened by `to_pandas_fk_safe`, so native parity needs the payload path to stringify the floated representation, a separate problem. The pure-payload admit stays string-only.
- `large_string` source; `float`/`double`/`decimal` sources; a truthy `checksum`; a missing namespace; a handler-rejectable config. All keep their existing decline reasons.
- No change to the full-frame / chunked / OOC fpe paths, to other strategies, or to `_KEY_STRATEGIES` (fpe is already key-eligible; this only widens its admitted key source types).

## 1. Risk

R2 (privacy-relevant masking; FK keys touched). Output must be byte-identical to the current Python path (the SEQ parity oracle). Same compiled kernel the string fpe key path proves; new surface is the integer->string boundary cast on the key path, the key-only integer admit, and a drift guard. Acceptance compares SEQ-native (integer key) to the full-frame oracle on the strict per-executor parity contract (exact output dtype, values, nulls, row/col order) plus a consumed-output witness and FK referential-integrity checks.

## 2. The code change (minimal, mirrors the string fpe key wiring)

All in `src/decoy_engine/execution/_sequential_native_ops.py` and `_sequential_native.py` unless noted; `_sequential_native_ops.py` is 524 LOC, ample headroom under the 600 goal.

- **An admitted-integer-type predicate** shared by the key admit sites (`pa.types.is_integer(t)` restricted to fixed-width int/uint, i.e. not `decimal`).
- **Key-context integer admit, payload unchanged.** The integer broadening applies ONLY where the column is an FK key, never to a pure payload column:
  - `prepare_table` (`_sequential_native.py:240-263`) already separates the pure-payload decision (`:248`, `column not in fk_children and column not in parent_keys`) from the key decisions (parent-key execution/REMAP at `:259-263`, children inherit). Route the key decisions through a key-aware admit that accepts integer or string; keep the pure-payload decision on the existing string-only `_fpe_admit`. Concretely: add an `is_key` flag (or a `_fpe_key_admit`) so only the parent-key / child-key path admits integers.
  - `_fpe_remap_input_reason` (`_sequential_native_ops.py:234`): broaden to admit integer or string for orphan keys.
  - Preserve the gate ORDER exactly as the string path: checksum -> namespace -> config -> source-type (an integer key with a checksum still declines `fpe_checksum_not_native` first).
- **`_fpe_apply` (`_sequential_native_ops.py:241`) integer boundary + drift guard.** Before `bound.kernel.run(...)`:
  - If `values.type` is an admitted integer type, `values = pc.cast(values, pa.string())`. Codex probed all eight integer widths incl. signed min/max, unsigned max, and nulls: `pc.cast(int, string)` equals `str(value)` with no formatting divergence, so the cast is the fast, exact boundary for integer-preserving input.
  - **Drift guard:** if the runtime `values.type` is NOT the integer type the key decision admitted and NOT string (e.g. an unexpected float widening), do not cast-and-cipher; fail exactly as the oracle would for that column (the existing `sequential_fpe_input_type_drift` abort path), never silently diverge. For an FK key this should not occur (keys are protected), so the guard is a safety assertion, not an expected branch.
  - Output stays `pa.string()`; the existing result-shape guard (`:252`) is unchanged.
- **No change** to `_fpe_warn` / `fpe_residual_warnings` (`_non_missing_strings` already `str()`s, integer-safe), to `_strategies/_fpe.py`, to the kernel, or to the drivers.

## 3. Acceptance matrix (strict parity contract; SEQ-native integer FK key vs full-frame oracle)

Every cell builds the same job twice (SEQ-native under the companion; full-frame oracle) and asserts byte-identical output on the strict comparator (exact dtype, values, null-vs-NaN, row/col order), plus a consumed-output witness (patch-where-looked-up on the kernel binding, native-result-consumed, native_calls-gated, negative control that discards the native result and fails) so the test is non-vacuous. All assertions run on the PRODUCTION path (not an isolated `pc.cast` unit test), under the real companion, non-skipped.

- **T1 FK-key byte-parity + RI**: an int64 fpe FK key (parent masked, matched child inherits, genuine orphan REMAP-minted), byte-identical to the oracle. RI asserted precisely (Codex MED #4): a **matched** child key resolves to a masked parent key; a **genuine orphan** gets the **exact parent-handler ciphertext** (REMAP mints a replacement child key, it does NOT create a parent row, so "every child resolves to a parent" is only asserted for matched children). Witness proves BOTH the parent decision and the REMAP result were consumed. Output dtype matches the oracle.
- **T2 cast == `str()` on the production path**: assert the integer->string boundary inside `_fpe_apply` (not a standalone `pc.cast`) yields the same bytes the oracle stringifies, across generated integers (randomized domain sweep) + `0`, negatives, `int64`/`uint64` min/max, each admitted width, and nulls. A mutant that removes or misdirects the cast in `_fpe_apply` must be killed by this + T1.
- **T3 FF1 floor boundary (Codex HIGH #2)**: using `min_domain_length(radix)` and `FF1_MIN_DOMAIN = 1_000_000` (NOT 100): for a digit charset, a **5-digit** key fails closed and a **6-digit** key is accepted. Assert the exact oracle error code + transactional abort (no committed output) on the reject, and a successful round-trip (decrypt back to the oracle plaintext integer string) on the accept.
- **T4 negative / out-of-charset / luhn fail-closed parity**: a sufficiently-long **negative** key succeeds with `preserve_separators` enabled and fails closed with it disabled (the `"-"` is out of a digit charset); an all-out-of-charset key fails even with preservation; `validate_luhn=True` (encrypted body excludes the check digit) parity. Each byte-identical native vs oracle, same error code, same abort.
- **T5 REMAP orphan fail-closed + precedence (Codex HIGH #2, MED #4)**: an invalid integer orphan (sub-floor / out-of-charset) fails closed after a parent table has already been staged (transactional abort, nothing committed); a mixed valid/invalid orphan batch pins first-error precedence identical to the oracle. Different parent/child column names, inherited intermediate parents, nulls, duplicate child keys, unsigned values above `2**63`, and an orphan value outside the parent's integer width are all covered, using a covering charset where a narrow integer otherwise cannot meet the domain floor.
- **T6 empty / all-null schema parity (Codex MED #3)**: an empty integer key column and an all-null integer key column match the oracle's exact output schema (today an empty payload materializes arrow `double` and all-null materializes arrow `null`; do NOT force unconditional string pinning). These no-work cases are tested separately from the positive witness (they cannot satisfy a native-call witness).
- **T7 declines unchanged (route to Python, byte-identical, coded reason)**: an integer PAYLOAD fpe column (not a key) still declines to Python (string-only payload admit) and is byte-identical to the oracle; `large_string` / `float` / `decimal` keys decline `fpe_source_type_not_string`; a checksum integer key declines `fpe_checksum_not_native` first; a missing-namespace integer key declines `fpe_requires_namespace_stays_python`. The handler-rejectable configs (missing namespace, invalid config) RAISE, so assert the raise, not an output/disposition comparison (Codex MED #5). Assert `native_calls == 0` for the declined-and-masked cases.
- **T8 admission reason-order unit test (Codex MED #5)**: a direct test of the admit order checksum -> namespace -> config -> source-type for integer keys, separate from the fallback-parity tests. (Note: this is the admit's order; the handler itself checks the missing namespace first, so the test asserts the admit order, not a "handler raise order".)
- **T9 mixed job isolation**: an int64 fpe FK key beside a string fpe key and a hash key on one SEQ job; each reports its own disposition, no cross-contamination; a declined fpe (float key) in the same job must not move the admitted integer one.

## 4. Failure modes (reproduce, do not smooth)

- A nullable integer **payload** sneaking onto the native key path and diverging on the floated representation -> prevented by the key-only scope + T7; the drift guard is the backstop.
- `pc.cast(int,string)` diverging from `str()` for any integer -> T2 (Codex found none across all widths; if one ever appears, build the string array from `str()` explicitly).
- A sub-floor / negative / out-of-charset key masking instead of failing closed -> T3/T4/T5 (must match the handler's fail-closed exactly; never emit cleartext or a non-round-trip value).
- A genuine orphan asserted to "resolve to a parent row" -> T1 corrected RI (orphans get parent-handler ciphertext, not a parent row).
- `large_string` / float / payload-int silently going native -> T7.

## 5. Verification + gates

- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python` (companion); ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python` (no companion, exercises the decline/Python path); one test process via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- T1-T9 pass in abi-3 (witness non-vacuous, real companion, non-skipped); declines + companion-absent in ci-mirror. The full existing SEQ fpe suite (payload #243 + string FK-key #251) and the full-frame / chunked / OOC fpe suites stay green (those paths untouched).
- Coverage + mutation on every changed production line (the key-aware integer admit, the REMAP admit broadening, the `_fpe_apply` cast + drift guard). Per-unit branch coverage; prove surviving mutants equivalent by injection (a mutant dropping the integer admit killed by T1; a mutant misdirecting the cast killed by T2; a mutant weakening the drift guard killed by a drift case).
- Module size: edits are small; confirm `_sequential_native_ops.py` + `_sequential_native.py` stay under the 600 goal / 700 max post-build (census entry if either crosses 600).
- Perf measurement (merge condition, enforceable per Codex MED #6): workload = an int64 fpe FK key over an id-width digit charset on a relationship job, `native_threads` pinned (record the value), timing scope INCLUDES the integer->string conversion + warning generation + output materialization. Report median of >=4 native / >=2 python reps + run dispersion + peak RSS, at 1M measured and a 10M measured point if it fits the devbox, else a clearly-labelled projection. Acceptance inequality: the native fpe-key step must be faster than Python AND the 10M figure (measured or projected, labelled) must clear the 600s bar; record the numeric memory ceiling the run stayed under.
- Gate chain: Opus VERIFY (diff + both venvs + equivalence proofs + the pinned perf numbers) -> dennis -> Codex final. Merge under the standing Rust rule (dennis GO + Codex GO + CI green + fpe parity contract + faster on the fpe-key step + within memory ceiling). Flag Slack before merge.

## 6. Done when

- [ ] T1-T9 green (FK-key byte-parity + corrected RI + consumed-output witness; production-path cast==str(); FF1 floor 5-vs-6-digit boundary; negative/out-of-charset/luhn fail-closed parity; REMAP orphan fail-closed + precedence + width/unsigned/duplicate/inherited cases; empty/all-null exact-schema parity; declines unchanged incl. payload-int/large_string/float/checksum/namespace; admit reason-order; mixed-job isolation); full existing SEQ + full-frame/chunked/OOC fpe suites green both venvs.
- [ ] Changed production lines coverage + mutation-proven; modules within size policy; sentries green.
- [ ] Perf: native faster than Python on the isolated fpe-key step, 10M (measured or labelled-projection) clears 600s, peak RSS recorded under the stated ceiling.
- [ ] ROADMAP + RECENTLY-SHIPPED updated (GAP8 closed for FK keys; integer-payload fpe noted as a follow-up); graphify refresh (subject to the keyless-refresh blocker - note if deferred); Slack recap in plain language.

cam
