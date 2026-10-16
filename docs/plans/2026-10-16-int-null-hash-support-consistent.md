Status: plan (Phase D, int+null+hash supported consistently across routes. rev 1 DRAFT. Cam 2026-10-16: "Make both paths consistent" - hashing a whole-number column that contains nulls must work IDENTICALLY on every route (null->null, deterministic, byte-identical). HOLD build until Codex plan-gate GO. Supersedes the HASH portion of 2026-10-15-ooc-null-bearing-int-guard-parity.md; that plan's OOC guard stays for {truncate,categorical}. Flag Slack before merge-to-main.)

Rules consulted: dev-rules 00-universal.md, risk-and-exceptions.md, development-loop.md, autonomous-operation.md, architecture.md; engine CLAUDE.md (fail-closed; validation-never-mutates; module-size ratchet; use established methodology; comments explain why). Base for planning: engine origin/main @ post-GAP9 (a951e205). Build base = branch fix/ooc-null-bearing-int-guard (rebased on post-GAP9 main; already carries the OOC guard + preflight extraction from PR #267).

# int+null+hash supported consistently across all routes

## 0. Why

Hashing an integer column that contains a null is REJECTED on every route today (`null_bearing_int_unsupported`, from `reject_null_bearing_int` keyed on `_INT_NULL_REJECTED_STRATEGIES = {truncate, hash, categorical}`). Cam's decision (2026-10-16): hashing is deterministic, a null stays null, so it must be SUPPORTED - and it must behave the SAME whether the job is small (full-frame) or large (out-of-core), not succeed on one route and reject on another.

The real blocker is substrate-specific, not semantic: the pandas full-frame path converts Arrow `int64+null` to pandas `float64+NaN` (pandas cannot hold a null-bearing integer natively), so the hash kernel then sees `1.0` and raises `float_canonicalization_unsupported` (a separate PO-locked hard error on floats). The Arrow/native fast paths keep `int64+null` and hash it correctly against the FROZEN integer encoding (`_encode_int`), so the native result is already right; the pandas path is the one that loses the integer.

This slice makes the pandas path feed INTEGER values into the (unchanged, frozen) canonicalization, so hash of int+null produces byte-identical output on every route. It does NOT touch the determinism envelope (`_canonicalize_source` / `_encode_int` are unchanged; same frozen bytes). Truncate and categorical stay rejected (Cam's call was hash-specific; the original truncate fail-open closure on OOC is preserved).

## 1. What exists (from the full-frame path map)

- Reject set: `plan/_checks.py:36` `_INT_NULL_REJECTED_STRATEGIES = {truncate, hash, categorical}`. Drives the runtime guard `reject_null_bearing_int` (`execution/_guards.py:66-87`), the compile check `check_null_bearing_int_unsupported` (`plan/_checks.py:197-245`), the shared OOC predicate `null_bearing_int_candidates` (`_guards.py`), and the narrower `_chunked_fk.py:932` variant. Guard call sites: `_pandas_adapter.py:202`, `_sequential.py:377`, `native/_chunked_entry.py:286`, `_unified_slice_admission.py:555`, `out_of_core/_preflight.py:78`.
- Conversion chokepoint (the bug): `execution/_fk_keys.py:533` `df = table.to_pandas()` widens int+null -> float64 for every column NOT in the protected set. Protected columns get lossless nullable Int/UInt via `_exact_fk_types_mapper` (`_fk_keys.py:273-282`; Arrow-int->nullable map `:261-269`), applied per column at `:540-541,554`. The protected set is assembled and passed at `_pandas_adapter.py:221-230` (FK cols | group-anchor | top-code | group-key) and `_sequential.py:384-390`.
- Output round-trip: `_pandas_adapter.py:342` `pa.Table.from_pandas(...)` turns nullable Int64/UInt64 back into plain Arrow int64/uint64 (proven by the FK path).
- Hash full-frame consumption: `_strategies/_hash.py:67` hands the WHOLE column to `hash_array` via `pandas_column_to_kernel_input` (`_adapter.py:331`, `pa.array(col, from_pandas=True)`). Kernel `kernel/_scalar.py:74-90`: null-skip via `_is_missing` (`:14-44`, handles NaN/None/pd.NA identically), non-null -> `_canonicalize_source`. The FROZEN int path `generation/pool/_canonicalize.py:87-88` (`numbers.Integral -> _encode_int`) already produces the correct bytes when fed an integer; the float branch `:89-98` is what raises today.
- Native/Arrow routes already correct: native-chunked (`native/_chunked_entry.py`, masks in Arrow), unified-slice (`_unified_slice.py:258`, Arrow), out-of-core (`out_of_core/_hash_native.py`; Python fallback `kernel/_scalar.hash_array` on raw Arrow). Hash on the eight integer widths is native-admitted (`out_of_core/_hash_native.py:49-55`, `native/_requirements.is_admitted_native_hash_type`). These are blocked ONLY by the guard; remove hash from the reject set and they run int+null+hash correctly with no code change.
- Sequential: WITH the native companion routes hash through `seq_native.hash_column` (Arrow, correct; `_hash.py:53-65`, `_sequential.py:321`). WITHOUT it, falls back to pandas `hash_array` on the widened float -> shares the full-frame bug. Same `to_pandas_fk_safe` chokepoint at `_sequential.py:384-390`.
- Parity target + frozen contract: `docs/native/derive-index-contract.md` (`_encode_int` exact non-minimal sizing, eight integer widths, float rejected). R3 / SEED_PROTOCOL_VERSION - NOT changed by this slice.
- Reuse precedent (not chosen, see §2): `_exact_int_faker.py` snapshots the original Arrow int column onto `ctx.exact_int_sources` for the faker key; its `_widened_int_column` gates on `dtype == np.float64`.

## 2. The change (feed integers to hash on the pandas routes; unblock the Arrow routes)

Two coordinated parts; both must land together.

- **(A) Remove `hash` from the reject set.** `plan/_checks.py:36` -> `_INT_NULL_REJECTED_STRATEGIES = {truncate, categorical}`. This single edit narrows EVERY guard (runtime, compile, OOC `null_bearing_int_candidates`, `_chunked_fk.py` variant) to truncate+categorical in lockstep. The Arrow routes (native-chunked, unified-slice, OOC) then run int+null+hash correctly with no further code change. The OOC guard from PR #267 remains valid (now rejects truncate+categorical only), preserving the original truncate fail-open closure.

- **(B) Make the pandas routes deliver integers for hash-target int columns.** Extend the protected column set passed to `to_pandas_fk_safe` at `_pandas_adapter.py:221-230` AND `_sequential.py:384-390` to include columns whose masking strategy is `hash` and whose source Arrow type is a (null-bearing) integer. Those columns then convert via `_exact_fk_types_mapper` to nullable Int/UInt instead of float64, so `_is_missing` still preserves the nulls and the non-null values reach `_canonicalize_source` as integers -> `_encode_int` -> byte-identical to the native path.
  - Scope narrowly to HASH int columns. Do NOT broadly convert all int+null columns: a passthrough int+null column must keep its current output (float64) to avoid a schema/bytes change for existing passthrough jobs (a separate latent bug, out of scope). Truncate/categorical int+null columns are still guard-rejected before conversion, so they never reach here.
  - Prefer reusing the existing protected-set mechanism (nullable dtype via `_exact_fk_types_mapper`) over the `exact_int_sources` snapshot: the hash column's values are overwritten with string tokens, so the temporary nullable-Int dtype has no downstream consumer and no output effect, and it avoids adding a second snapshot path through `_hash.py`. The builder confirms the chosen seam with a census-exact helper if either module exceeds 600 LOC.
  - The column-selection helper (strategy==hash AND integer source type) is derived from the plan work list + Arrow schema, mirroring how the FK/anchor sets are assembled. It must be computed per table and unioned into the existing protected set (never replacing it).

No change to `_canonicalize_source`, `_encode_int`, the native kernel, the Rust port, or any accepted job's output other than int+null+hash (previously rejected -> now produces the native-parity tokens). Reject policy for truncate/categorical unchanged.

## 3. Risks (reproduce, do not smooth)

- **Byte-parity of the newly-supported cell.** Full-frame int+null+hash MUST equal the native/OOC output byte-for-byte (same `_encode_int`). Pin with a cross-route parity test on the exact D1a config and an independent int-width sweep. If they differ, STOP - the pandas path is not feeding the same integer bytes.
- **Passthrough output drift.** The narrow scoping (hash columns only) must be verified: an int+null column under passthrough still emits float64 (unchanged). A test pins passthrough int+null output is byte-identical to pre-change. If the conversion leaks to passthrough columns, that is a regression.
- **Faker exact-int path.** `_widened_int_column` gates on float64; because only HASH columns convert (faker columns stay float64), the faker exact-int registration is unaffected. A test confirms a faker int+null column still registers exact-int (regression guard).
- **when-gate predicates** on an int+null column that is also hashed: nullable-Int64 vs float64 changes NaN-vs-pd.NA comparison. Targeted test on a `when:` predicate over a hashed int+null column.
- **Guard coordination / route divergence.** Removing hash from the set must take effect on ALL routes at once (it does, via the single set). Regression-gate must show the previously-failing D1a hash tests now pass and no route still rejects int+null+hash. `tests/parity/SEMANTIC_DIFFERENCES.md` updated (int+null+hash is now supported, not a difference; truncate/categorical remain documented rejections).
- **Module size:** the column-selection helper may push `_pandas_adapter.py` / `_sequential.py` / `_fk_keys.py` over 600; census at exact LOC. Physical-seam sentry: any changed `execution/` module must be in `permitted_non_physical` ([[physical-seam-sentry-allowlist]]).
- **Determinism envelope (R3):** this slice must NOT change `_canonicalize_source`/`_encode_int`/SEED_PROTOCOL_VERSION. If a correct fix seems to require changing them, STOP and surface - that is a protocol-version conversation, not this slice.

## 4. Acceptance

- **T1 cross-route byte-parity (the headline):** the D1a witness config (non-FK `n = [1,2,3,None,5]` int64 under hash, plus a string col) run through full-frame produces output byte-identical to the OOC/native route, with the null preserved (null->null). Independent check: an int-width sweep (int8/int16/int32/int64 + unsigned) with interior + boundary values (incl. int min/max) + nulls, full-frame == native for every width. Mutant: feeding the float column (revert part B for hash) -> full-frame raises `float_canonicalization_unsupported` -> T1 fails.
- **T2 all routes accept + agree:** int+null+hash runs (no `null_bearing_int_unsupported`) on full-frame, sequential (companion-absent pandas fallback AND companion-present native), native-chunked, unified-slice, and OOC, and all masked outputs agree under the engine's documented normalizations. The previously-failing D1a hash tests (`test_phase_d_d1a_hash_baseline/pinning/witness`) pass unchanged.
- **T3 truncate/categorical still rejected everywhere:** int+null under truncate and categorical still raises `null_bearing_int_unsupported` on every route (full-frame/seq/native-chunked/unified-slice/OOC). The reworked parity test `test_non_fk_int_payload_reject_parity` now expects: truncate -> both-raised (exact code); hash -> parity (success, byte-equal); categorical -> both-raised.
- **T4 no collateral output change:** passthrough int+null output is byte-identical to pre-change (still float64); a faker int+null column still registers the exact-int path; a non-null int hash column is unchanged; a `when:` predicate over a hashed int+null column behaves correctly.
- **T5 guard/compile/OOC parity:** the compile-time check and the OOC preflight no longer reject hash but still reject truncate+categorical; `reject_null_bearing_int` (full-frame/seq) rejects only truncate+categorical.

## 5. Verification + gates

- abi-3 companion venv `~/.cache/decoy-d1a-abi3-venv/bin/python` for native/OOC/parity; ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python` for pure-python/compile-check/sentry. ALWAYS `PYTHONPATH="$(pwd)/src:$(pwd)" ~/bin/pytest-one <python> <paths> -q`. One test run at a time (devbox memory).
- Full suites that must stay green: the D1a hash suite, OOC + parity suites, the SEQ/native/unified-slice null_bearing_int tests (now asserting truncate/categorical only), the full-frame strategy suites (passthrough/faker/when-gate), module-size + physical-seam sentries. Changed-lines coverage + mutation on the column-selection helper and the set edit (remove-hash and leak-to-passthrough mutants killed).
- ci-mirror clean-env: ruff check + format + mypy on changed modules; reproduce the regression-gate locally before pushing (the full-suite run is where the D1a breakage surfaced).
- Gate chain: Opus VERIFY -> dennis -> Codex final. Build on the fix/ooc-null-bearing-int-guard branch (reworks PR #267). Autonomous merge on FULL green (dennis + Codex + CI), flag Slack before merge-to-main. The Codex FINAL prompt MUST forbid the reviewer from merging/pushing/PR; Opus merges after CI.
- DOCUMENT: roadmap + RECENTLY-SHIPPED updated; SEMANTIC_DIFFERENCES.md reconciled; refresh the decoy-engine graphify graph if stale ([[graphify-standing-ruleset]]). Slack section-complete explainer.

## 6. Done when

- [ ] T1-T5 green; int+null+hash supported + byte-identical on every route; truncate+categorical still rejected everywhere; no collateral output change (passthrough/faker/when-gate/non-null-int).
- [ ] Module sizes within policy; census exact; physical-seam sentry green. Changed-lines coverage + mutation-proven (remove-hash, leak-to-passthrough, feed-float mutants killed).
- [ ] PR #267 reworked; diff + evidence to Slack; PR CI green (regression-gate + substrate pass); merge; main CI green (ci.yml + substrate-matrix). SEMANTIC_DIFFERENCES.md + roadmap + RECENTLY-SHIPPED current. Note: UNBLOCKS the D5 Phase-D ship.

cam
