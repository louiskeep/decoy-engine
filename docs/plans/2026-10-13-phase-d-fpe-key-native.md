Status: plan (Phase D / FK-key-native slice. DRAFT rev 1, pre Codex plan-gate. fpe native on an FK key + explicit declines for the lossy key operators, on the SEQ route.)

# Phase D / FK-key-native: fpe on an FK key native; the lossy operators decline with a reason

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md` (established-methodology, module size, pre-GA hard delete, explain-why comments). Grounding: the Phase D program plan `docs/plans/2026-10-10-phase-d-fk-routes-program.md` (§4 shape x route x operator x tier, §5 no-legacy/compute-witness, §6 parity contract); the 2026-10-13 FK-key-native FRAME survey (this session); Cam directive 2026-10-13 "maximize Rust fast-path coverage" ([[decoy-rust-fast-path-everything]]). Base: engine `origin/main` @ `06348c84` (post the SEQ-native-decisions split). Built on the split (`ColumnDecision`/`RemapRecord` now in `_sequential_native_decisions.py`).

## 0. Root cause + scope (from the FRAME)
Today only `hash` is native on an FK key on the SEQ route: `prepare_table` gives a non-hash native operator an execution decision ONLY when the column is no FK key (`_sequential_native.py:213-218`), and the REMAP orphan-minting path (`_remap_tokens:461-495`) and inherited-child reporting (`:235`) and metrics routing (`:506-508`) are hash-specific by design. The FK JOIN itself is route-neutral and already correct for every strategy (matched child inherits the parent's masked value by source key via `resolve_fk_keys`; an orphan is REMAP-minted by re-running the PARENT handler, `_orphan.py:99-156`, which dispatches any strategy). So non-hash FK keys already produce CORRECT output in Python; the gap is SPEED plus the §5 silent-Python observability gap, not correctness.

Determinism makes a masked value join; INJECTIVITY makes it a valid UNIQUE key. Per the FRAME:
- **fpe** is bijective on its charset domain -> injective -> a valid unique joinable key. WIRE IT NATIVE on a key.
- **categorical** (many-to-few pool), **bucket_perturb** (many-to-one coarsening), **date_shift** (keyed offsets can collide; also drags a per-row error/quarantine channel) are deterministic but NON-INJECTIVE as keys. Do NOT accelerate them on a key; instead record an EXPLICIT decline disposition so metrics stop being silent that they run Python. ("Decline" on SEQ keeps the correct Python output -- SEQ has no alternate route -- and only labels it.)

This slice closes the FK-key gap honestly: fpe native (the feasible coverage) + the three labelled. It does NOT change WHETHER a lossy operator is allowed on a key (that stays as today; a separate PO call, tracked).

## 1. Risk
R2-R3 (privacy/RI-critical; FK key correctness is GATE-TQ0, 100% crypto/RI). The REMAP generalization is the crux: a native fpe orphan key MUST be byte-identical to the Python parent handler (same FF1 tweak, key derivation, charset, checksum, fail-closed), or parent and child keys diverge and the join breaks. Acceptance compares native SEQ to the pandas oracle on the strict 9-dimension contract plus the native-execution witness.

## 2. Part A -- fpe native on an FK key
Generalize the hash-specific key machinery to a key-eligible set `{HASH, FPE}`:
- **Gate (`prepare_table:213-228`).** Replace the `op.strategy != HASH` branch with a key-eligibility check: for a key-eligible op (hash, fpe), an FK parent-key/child-key column gets the same execution + REMAP decision logic hash gets today (execution decision unless it is an FK child; REMAP decision when it is a parent key; a column that is both gets only REMAP; `_inherited_columns` recorded for a child). For a NON-key-eligible native op that IS an FK key, see Part B.
- **REMAP minting (`_remap_tokens` + `hash_column:278` + the fpe handler).** Make orphan minting strategy-parametric. The fpe handler (`_strategies/_fpe.py:166-180`) gains the `_in_remap` branch `hash_column` has: inside `remap_scope`, it mints the orphan keys via the fpe kernel using the PARENT column's `FpeCall` (namespace, mask_key, FpeConfig, and the tweak column -- default the parent column name, or `fpe_join_group` when set), recording a `RemapRecord` by backend. `_remap_tokens` generalizes to take the op/bound/call (or a sibling `_remap_fpe` is added) rather than hardcoding `_bound.get(HASH)` + `native_hash_array`; the admission/typing guard (`decline_reason` for the orphan-key type; `REASON_REMAP_PARENT_PYTHON` when the parent ran Python) is preserved per op. fpe fail-closed (`fpe_unencryptable_domain` on an all-out-of-charset orphan, `_orphan.py:22-28`) is preserved on the native REMAP.
- **Inherited reporting (`prepare_table:235`).** Generalize `parent_seed.strategy != "hash"` to the key-eligible set so an fpe parent key records its children as `fk_inherited` with `parent_backend`.
- **Metrics (`metrics:504-510`).** Route each op's REMAP (`_remap`) and inherited (`_inherited`) entries under THAT op's disposition key, not only `SEQ_HASH_DISPOSITION_KEY`; keep `RemapRecord`/`_inherited` carrying their strategy so the fragment lands under `sequential_fpe` for fpe keys and `sequential_hash` for hash. hash output stays byte-identical.

Acceptance (Part A) -- each runs full-frame oracle, SEQ-native, SEQ python-fallback; strict `compare_strict == _ALL_TRUE`; witness proves the fpe kernel ran for the key:
- **A1 single fpe parent key + matched children** -- parent key native fpe; matched child inherits (no kernel call); strict parity; witness shows parent `sequential_fpe` scalar native + child `fk_inherited`.
- **A2 fpe parent key + genuine orphan under REMAP** -- the orphan child key is minted natively via the parent's fpe tweak; byte-identical to the Python oracle's orphan key; `RemapRecord` backend native.
- **A3 tweak consistency** -- default (tweak = column name) AND `fpe_join_group` set: native parent key and native REMAP orphan use the SAME tweak the Python handler uses; two members of a join group encrypt identically on both routes.
- **A4 fpe fail-closed on a REMAP orphan** -- an orphan key with no in-charset character raises `fpe_unencryptable_domain` identically on native and Python REMAP; the table publishes nothing.
- **A5 composite fpe key** -- a composite key with an fpe component (beside a hash component) masks + remaps correctly (cf. the Dcomp composite harness); each component uses its own tweak.
- **A6 chain-middle fpe key** -- a column that is both an fpe parent key and an fpe FK child mints its orphans natively (REMAP decision) without being rehashed as a matched child (the "two decisions, one admission" invariant), byte-identical to the oracle.
- **A7 PRESERVE and WARN** orphan policies on an fpe key, strict parity + the right disposition/warning channel.
- **A8 hash unchanged** -- an existing hash-key shape is byte-identical through the refactor (regression).

## 3. Part B -- explicit decline for the lossy key operators
For a `categorical` / `bucket_perturb` / `date_shift` column that is an FK parent key or child key, `prepare_table` records an explicit DECLINE disposition (no execution decision, so it stays Python and output is unchanged) with a caller-visible reason code (e.g. `sequential_fk_key_not_native_non_injective` carrying the strategy + column + Arrow type, value-free), surfaced in `metrics()` under that op's disposition key. No kernel runs; the Python handler's output is the existing correct output.

Acceptance (Part B):
- **B1** a categorical/bucket_perturb/date_shift FK-key column: output still matches the pandas oracle (unchanged), AND `metrics()` now carries an explicit decline entry naming the reason (no longer silent). One parametrized test per operator.
- **B2** the same operator as a NON-key payload column is unaffected -- still masks native as it does today (D2c-ii/iii/iv), no decline.
- **B3** the reason code is value-free (no cell/key/derived value), per §5 and the reason-code rule.

## 4. Failure modes (reproduce, do not smooth)
- A native fpe REMAP orphan key that uses the wrong tweak (e.g. the child column name instead of the parent's) silently breaks the join -- A3 pins the tweak; the native REMAP must thread the PARENT's FpeCall.
- fpe fail-closed must fire on the native REMAP exactly as in Python (A4).
- The chain-middle fpe column must not be both rehashed-as-child and remapped (A6).
- Generalizing metrics must not move hash's REMAP/inherited entries off `sequential_hash` (A8) nor duplicate them.
- Part B must not accidentally make a lossy op native on a key, and must not touch its payload path (B2).

## 5. Pinning tests to flip/extend
- `tests/parity/test_phase_d_d2c_fpe_seq.py` `test_fpe_key_columns_get_no_native_decision_and_match_the_python_oracle` (~:306-320) and `test_an_fpe_key_does_not_disturb_a_native_hash_payload_beside_it` (~:325): fpe keys now go NATIVE -- rewrite these to assert the native decision + witness (honest mechanical update, not weakening).
- `tests/parity/test_phase_d_d2c_iii_bucket_seq.py` / `test_phase_d_d2c_iv_date_shift_seq.py` FK-key boundary pins: keep Python but now assert the explicit decline reason (Part B), not silent Python.
- New suite `tests/parity/test_phase_d_fpe_key_seq.py` (+ a `_real.py` for the abi-3 kernel) for A1-A8; extend the composite harness for A5.
- Preserve `tests/unit/transforms/test_fpe_remap_orphan_charset.py` and the all-out-of-charset fail-closed path.

## 6. Module size
`_sequential_native.py` is 525 after the split. Part A adds the fpe REMAP branch + the generalizations; estimate +40 to +70 LOC -> ~565-595, under 600. If it crosses 600, extract the REMAP machinery (`_remap_tokens` + `remap_scope` + `RemapRecord` wiring) into a `_sequential_native_remap.py` as a follow-on split FIRST. Measure post-build.

## 7. Verification + gates
- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python`; ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python`; one test process at a time via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- A1-A8 pass in abi-3 (native witness non-vacuous); companion-independent parts + Part B in ci-mirror. The full existing SEQ suite stays green (hash + fpe-payload + categorical/bucket/date_shift payload + Dcomp).
- Coverage + mutation on every changed production line (the REMAP generalization, the gate, the metrics routing, the decline path); prove equivalence of any surviving mutant by injection (the D2c-iii/iv lesson).
- Gate chain: Opus VERIFY (diff + both venvs + equivalence proofs) -> dennis -> Codex final. Merge under the standing Rust rule (dennis GO + Codex GO + CI green + the fpe-key parity contract + faster on the fpe-key step + within memory ceiling).

## 8. Done when
- [ ] A1-A8 green (fpe native on a key, REMAP tweak-consistent + fail-closed, composite + chain-middle, PRESERVE/WARN, hash regression); B1-B3 green (the three decline with a value-free reason, payload unaffected).
- [ ] Pinning tests flipped honestly; no existing test weakened; full SEQ suite green both venvs.
- [ ] Changed production lines coverage+mutation-proven; `_sequential_native.py` < 600 (or the REMAP extracted first).
- [ ] ROADMAP + RECENTLY-SHIPPED updated (fold the split-done note here too); graphify refreshed to the merge SHA (covers the split + this slice); Slack recap in plain language; the "allow lossy op on a key at all?" PO question surfaced.

cam
