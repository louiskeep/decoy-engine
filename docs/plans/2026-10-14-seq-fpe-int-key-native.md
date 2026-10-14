Status: plan (Phase D / audit GAP8. DRAFT rev 1, pre Codex plan-gate. Admit integer-typed fpe columns to the compiled FF1 kernel on the SEQ route, byte-identical to the existing Python path.)

# fpe on an integer source (incl. FK keys) native on the sequential route

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md`. Grounding: the 2026-10-14 bottleneck audit (GAP8: an fpe int64 FK key runs the per-cell Python oracle, ~1756s/10M, 2.9x over the 600s bar) + the shipped SEQ fpe-native slice (#243 D2c-i payload, #251 fpe-as-FK-key) + the FF1 compiled kernel (#232 C6a). Base: engine `origin/main` @ `c4dd75a9`.

## 0. Why + scope

On the SEQ (sequential FK) route, `fpe` masks natively only when the source arrow type is exactly `pa.string()`. An integer-typed column (the common surrogate PK/FK shape) declines to the Python handler with `fpe_source_type_not_string:<col>:<type>` and masks per cell (`_strategies/_fpe.py` `str(v)` + per-cell `fpe_encrypt_value`), ~1756s/10M.

**This is an admission-gate choice, not a kernel or crypto limit, and it changes no output.** Confirmed in code:
- The current Python path for an integer fpe column already emits a **string** column: `_strategies/_fpe.py:196` builds `raw_non_na = [str(v) for v in ...]`, `:242-247` assembles an object list and assigns `df[column] = out`; the reference returns `pa.array(out, type=pa.string())` (`native/_crypto_reference.py:177`). FF1 is numeral-based (`transforms/_ff1.py`), and the Python oracle ciphers `str(value)` over the configured charset. So an int column is already maskable and already yields ciphered strings today.
- The string requirement lives only on the native path: the admit gate `_fpe_admit` (`_sequential_native_ops.py:229-230`) and its REMAP twin `_fpe_remap_input_reason:236-237`, plus the kernel's string-in/string-out wiring (`native/_fpe_ext.py`, and `_fpe_apply:252` asserts `out.type == pa.string()`).

So this slice makes the native path accept integer sources by casting int -> decimal string at the boundary with semantics identical to `str(value)`, then running the exact kernel the string path already uses. **Output is byte-identical to the current Python path** (a ciphered string column); the only change is speed. The residual-risk warning path is already integer-safe (`native/_fpe_route.py` `_non_missing_strings` `str()`s each value before counting).

**In scope:** fpe PAYLOAD columns and fpe FK KEY columns (parent, inherited child, orphan-REMAP mint) whose source arrow type is a fixed-width **integer** (`int8/16/32/64`, `uint8/16/32/64`) take the compiled kernel on the SEQ route.

**Out of scope / still DECLINE to Python (byte-identical), unchanged reasons:** `large_string` source (the existing "exact string" decline stays); `float`/`double`/`decimal` sources (decimal/float `str()` formatting - `"1.5"`, `"nan"`, scientific notation - is a separate parity question, not this gap); a truthy `checksum`; a missing namespace; a handler-rejectable config. No change to any other strategy, route, or to the full-frame / chunked / OOC fpe paths.

## 1. Risk

R2 (privacy-relevant masking; FK keys touched). Output must be byte-identical to the current Python path, which itself is the full-frame oracle for the SEQ parity contract. Same compiled kernel the string fpe path already proves; the only new surface is the integer -> string boundary cast and the broadened admit. Acceptance compares SEQ-native (integer source) to the full-frame oracle on the strict per-executor parity contract (exact output dtype = string/object, values, nulls, row/col order) plus a consumed-output witness and FK referential-integrity checks.

## 2. The code change (minimal, mirrors the string fpe wiring)

All in `src/decoy_engine/execution/_sequential_native_ops.py` unless noted. Keep every module within the 600 goal / 700 max.

- **An admitted-integer-type predicate** (one small module-level helper, e.g. `_FPE_NATIVE_INT_TYPES = frozenset({pa.int8(), pa.int16(), pa.int32(), pa.int64(), pa.uint8(), pa.uint16(), pa.uint32(), pa.uint64()})`, or `pa.types.is_integer(t)`). Used by both admit sites so they stay in lockstep.
- **`_fpe_admit` (`:213`)**: after the checksum / namespace / config gates (unchanged), replace the exact-string source check (`:229-230`) with "admit `pa.string()` OR an admitted integer type; otherwise decline with the existing `fpe_source_type_not_string:<col>:<type>` reason." `large_string`, float, decimal still hit the decline. The checksum/namespace/config order is preserved exactly (a checksum int still declines `fpe_checksum_not_native` first, matching the string path and the handler raise-order).
- **`_fpe_remap_input_reason` (`:234`)**: same broadening for the orphan-key mint path (the parent config was already admitted at table prep; only the orphan keys' own type is open here).
- **`_fpe_apply` (`:241`)**: before `bound.kernel.run(...)`, if `values.type` is an admitted integer type, `values = pc.cast(values, pa.string())`. The cast's decimal output must equal `str(value)` for the integer domain (proven by T2 below); nulls are preserved as nulls; integers never produce `""`, so the empty-string missing policy is a no-op for this source. Output stays `pa.string()`; the existing result-shape guard (`:252`) is unchanged.
- **No change** to `_fpe_warn` / `fpe_residual_warnings` (`_non_missing_strings` already `str()`s), to `_strategies/_fpe.py` (stays the reference + declined-fallback for the out-of-scope types), to the kernel, to `_KEY_STRATEGIES` (fpe is already key-eligible; this only widens its admitted source types), or to the drivers.

## 3. Acceptance matrix (strict parity contract; SEQ-native integer source vs full-frame oracle)

Each cell builds the same job twice (SEQ-native under the companion; full-frame oracle) and asserts byte-identical output on the strict comparator (exact dtype, values, null-vs-NaN, row/col order), plus the consumed-output witness (patch-where-looked-up on the kernel binding, native-result-consumed, native_calls-gated, with a negative control that discards the native result and fails) so the test is not vacuous.

- **T1 payload byte-parity**: an int64 fpe PAYLOAD column, byte-identical to the oracle, across row counts and the empty / all-null / nulls-interspersed cases. Output dtype is string/object (matching today). Witness non-vacuous.
- **T2 int->string cast == `str()` (the parity anchor)**: prove `pc.cast(arr, pa.string())` equals `[str(v) ...]` for the integer domain: `0`, positive, **negative**, `int64` min/max, each admitted width (`int8..uint64`), and nulls. This pins the one new assumption. A dedicated unit test, independent of the cipher.
- **T3 FK key RI**: an int64 fpe **FK key** (parent + matched child inherit + orphan-REMAP mint), byte-identical to the oracle AND referential integrity preserved (every masked child key resolves to a masked parent key; orphans minted identically). The key column becomes string on both sides (as today); join holds on the string ciphertext. Spy that the REMAP mint path admitted the integer orphan keys (native_calls > 0 on the mint).
- **T4 fail-closed parity (sub-floor + out-of-charset)**: a short integer whose in-charset domain is below the FF1 floor (`radix**len < 100`, e.g. a 1-2 digit id over a digit charset) fails closed identically native vs oracle (same `StrategyError` code, job killed, no partial output). A **negative** integer over a digit charset (the `"-"` is out of charset) fails closed or preserves-separator identically on both paths. Same error code, same sink abort.
- **T5 declines unchanged (route to Python, byte-identical, coded reason)**: `large_string`, `float64`, `decimal` sources still decline with `fpe_source_type_not_string`; a checksum int declines `fpe_checksum_not_native` first; a missing-namespace int declines `fpe_requires_namespace_stays_python`. Assert the disposition reports the Python backend + reason, `native_calls == 0`, output byte-identical to the oracle.
- **T6 charset variation**: int source over (a) the default digit charset and (b) a non-digit charset (str-of-int digits out of charset -> fail-closed or preserve-sep), byte-identical to the oracle on both. Confirms the cast + kernel honor the configured charset exactly as the handler does.
- **T7 mixed job isolation**: an int64 fpe column beside a string fpe column and a hash column on one SEQ FK job; each reports its own disposition, no cross-contamination; a declined fpe (float) column in the same job must not move the admitted int one.

## 4. Failure modes (reproduce, do not smooth)

- `pc.cast(int, string)` diverging from `str()` for any integer (negatives, int64 min) -> T2 (the one new assumption; if it ever diverges, the boundary must build the string array from `str()` explicitly rather than cast).
- A negative or sub-floor integer masking silently instead of failing closed -> T4 (must match the handler's fail-closed exactly; never emit cleartext or a non-round-trip value).
- An FK key int source losing RI because parent and child cast/cipher differently -> T3.
- `large_string` / float silently going native -> T5 (must still decline).

## 5. Verification + gates

- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python` (companion); ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python` (no companion, exercises the decline/Python path); one test process via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- T1-T7 pass in abi-3 (witness non-vacuous); declines + companion-absent in ci-mirror. The full existing SEQ fpe suite (payload #243 + FK-key #251) and the string-source cases stay green (the string path is untouched). The full-frame / chunked / OOC fpe suites stay green.
- Coverage + mutation on every changed production line (the broadened admit branches, the REMAP broadening, the boundary cast); prove surviving mutants equivalent by injection (e.g. a mutant that drops the integer admit must be killed by T1/T3; a mutant that casts the wrong way must be killed by T2).
- Module size: the edits are small (one type-set constant + two admit-branch edits + one cast line); confirm `_sequential_native_ops.py` stays under the 600 goal / 700 max post-build (census entry if it crosses 600).
- Perf measurement (merge condition, pin it): isolate the SEQ fpe step on an int64 column (realistic id-width integers, a digit charset), measured native-kernel vs the Python handler at 1M rows (warm once, median of >=4 native / >=2 python reps), extrapolated to 10M vs the 600s bar; record native vs Python wall + the speedup. Memory: confirm the boundary cast adds no unbounded resident state (one transient string array per batch, same envelope as the string fpe path).
- Gate chain: Opus VERIFY (diff + both venvs + equivalence proofs + the isolated perf number) -> dennis -> Codex final. Merge under the standing Rust rule (dennis GO + Codex GO + CI green + fpe parity contract + faster on the fpe-int step + within memory ceiling). Flag Slack before merge.

## 6. Done when

- [ ] T1-T7 green (payload + FK-key RI byte-parity + consumed-output witness + string-output pin; the int->string == `str()` anchor; fail-closed parity for sub-floor + negative + out-of-charset; declines unchanged incl. large_string/float/decimal/checksum/namespace; charset variation; mixed-job isolation); full existing SEQ (payload + FK-key) + full-frame/chunked/OOC fpe suites green both venvs.
- [ ] Changed production lines coverage + mutation-proven; module within size policy; sentries green.
- [ ] Perf: native faster than Python on the isolated fpe-int step, 10M extrapolation recorded vs 600s.
- [ ] ROADMAP + RECENTLY-SHIPPED updated (GAP8 closed); graphify refresh (subject to the keyless-refresh blocker - note if deferred); Slack recap in plain language.

cam
