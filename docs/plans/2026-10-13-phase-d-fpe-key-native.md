Status: built
Shipped: merge PR #251 (c61fb658)

# Phase D / FK-key-native: fpe on an FK key native; the non-injective-risk operators decline with a reason

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md` (established-methodology, module size, pre-GA hard delete, explain-why comments). Grounding: the Phase D program plan `docs/plans/2026-10-10-phase-d-fk-routes-program.md` (§4 shape x route x operator x tier -- MANDATORY, every admitted cell explicit; §5 no-legacy/compute-witness; §6 strict parity); the 2026-10-13 FK-key-native FRAME survey; Cam directive 2026-10-13 "maximize Rust fast-path coverage" ([[decoy-rust-fast-path-everything]]). Base: engine `origin/main` @ `06348c84` (post the SEQ-native-decisions split).

## 0. Root cause + scope
Only `hash` is native on an FK key today: the gate skips non-hash FK keys (`_sequential_native.py:213-218`); REMAP orphan-minting (`_remap_tokens:461-495`) hardcodes `_bound.get(HASH)` + `native_hash_array`; inherited reporting (`:235`) and metrics routing (`:504-510`) special-case HASH. The FK JOIN is route-neutral and already correct for every strategy (matched child inherits by source key via `resolve_fk_keys`; an orphan is REMAP-minted by re-running the PARENT handler, `_orphan.py:99-156`, dispatching any strategy). So non-hash FK keys already produce CORRECT Python output; the gap is SPEED + the §5 silent-Python observability gap.

Key-validity: a masked FK key must be deterministic (joins) and should stay unique (injective) to remain an unambiguous key.
- **fpe** is bijective on its charset domain -> injective -> always a valid unique joinable key. WIRE IT NATIVE on a key.
- **categorical / bucket_perturb / date_shift** are deterministic but their injectivity is NOT GUARANTEED (categorical maps many sources into a finite pool; bucket_perturb picks a keyed position within a coarse bucket so distinct dates CAN collide; date_shift draws a per-value offset so two dates CAN land together -- though some configs, e.g. a fixed offset, are injective). This slice does NOT accelerate them on a key: it is a conservative exclusion (not a proof that native would be wrong), and it records an explicit decline reason so metrics stop being silent that the key ran Python. (On SEQ "decline" keeps the correct Python output; there is no alternate route.)

This slice closes the FK-key gap: fpe native (the feasible coverage) + the three labelled with a value-free reason. It does NOT change WHETHER a lossy op is allowed on a key (unchanged from today; a separate PO call, tracked in [[decoy-rust-fast-path-everything]]).

## 1. Risk
R2-R3, GATE-TQ0 (FK key correctness, 100% crypto/RI). The REMAP generalization is the crux: a native fpe orphan key MUST be byte-identical to the Python parent handler (same FF1 tweak, key derivation, charset, checksum, fail-closed) or parent and child keys diverge and the join breaks. Codex confirmed the parent-tweak threading path is already correct (`make_remap_fn` sets the parent table and passes `pcol` + the parent seed, `_orphan.py:135`; the handler builds `FpeCall` from that column; the kernel selects join-group or column scope, `native/_fpe_ext.py:127`); the slice must not disturb it, and must prove it.

## 2. Part A -- fpe native on an FK key

### 2a. The code change (one generalization)
Generalize the four hash-special-cases to a key-eligible set `{HASH, FPE}`:
- **Gate (`prepare_table:213-228`):** a key-eligible op's FK parent-key/child-key column gets the same execution + REMAP decision logic hash gets (execution decision unless an FK child; REMAP decision when a parent key; both -> only REMAP; child recorded in `_inherited_columns`). A non-key-eligible native op that is an FK key takes the Part B decline.
- **REMAP minting:** make it strategy-parametric. The fpe handler (`_strategies/_fpe.py:166-180`) gains the `_in_remap` branch `hash_column:278` has, minting orphan keys via the fpe kernel with the PARENT column's `FpeCall` (namespace, mask_key, FpeConfig, tweak column). `_remap_tokens` generalizes to take the op/bound/call (or a sibling `_remap_fpe`), preserving the per-op admission/typing guard (`decline_reason` for the orphan-key type; `REASON_REMAP_PARENT_PYTHON` when the parent ran Python). The native REMAP path MUST check `NativeResult.errors` through the existing fail-closed helper before returning any value, exactly as scalar execution does (`native_column:427-431`), so fpe `fpe_unencryptable_domain` fires on a bad orphan.
- **Inherited reporting (`:235`):** generalize `parent_seed.strategy != "hash"` to the key-eligible set; attribute each inherited entry to the PARENT strategy (so an fpe parent's children land under `sequential_fpe`).
- **Metrics (`:504-510`):** `RemapRecord` and the inherited entries must CARRY their strategy (neither does today -- `_sequential_native_decisions.py:75`, the inherited dict at `:245`); route each op's REMAP + inherited entries under that op's disposition key. hash stays byte-identical.

### 2b. Acceptance matrix (the spec) -- every admitted fpe-key cell explicit (Codex HIGH)
Because the gate admits fpe on EVERY FK shape, §4 requires each as an explicit case, not an A1-A8 subset. Implement by PARAMETRIZING the existing FK-shape harnesses over the key strategy `{hash, fpe}` so every shape+policy already proven for hash is now also proven for an fpe key. Each cell: full-frame oracle vs SEQ-native vs SEQ-python, `_strict_compare.compare_strict == _ALL_TRUE` (output), with warnings/row-errors/native-provenance on their own channels; the witness proves the fpe kernel ran for the key (scalar derivation and/or REMAP) and matched children are `fk_inherited`.
- **Shapes x policies (reuse `tests/parity/_d2b_shapes.py` + `_dcomp_shapes.py`, key strategy = fpe):** single parent key + matched children; chain A->B->C; chain-middle (column is both fpe parent key and fpe child -> only a REMAP decision, NO scalar admission/call for it); diamond / junction / parallel edges; multi-parent (first-hit precedence); self-reference; composite key with an fpe component (incl. partial-null tuple); orphan policies PRESERVE, REMAP, WARN, and FAIL including a GENUINE-ORPHAN FAIL (raises identically on native + python + oracle).
- **Routing dispositions (reuse the pipeline-routing harness, through `decide_execution_route`, not a direct adapter call):** an fpe-key FK job under estimate fit -> full_frame; non-fit -> bounded; unpriceable estimate -> bounded-by-eligibility; flag-off below/at `out_of_core_threshold_rows`; explicit `execution_mode` override. Named (shape, strategy, size tier) -> (route, backend, reason).
- **fpe-specific cells:**
  - **tweak consistency (Codex MED):** a `parent_id -> child_fk` edge with DISTINCT parent/child column names and distinct per-column fpe configs, plus a genuine orphan. Assert the actual REMAP call receives the PARENT namespace/key/config/tweak-column and its ciphertext equals the Python parent handler's; repeat with `fpe_join_group`; include a mutation check that substituting the CHILD column name as the tweak makes the test fail.
  - **fail-closed:** an orphan key with no in-charset character raises `fpe_unencryptable_domain` on the native REMAP identically to Python; the table publishes nothing (witness the failing native REMAP call).
  - **declines (explicit, caller-visible reason, stay Python):** fpe key with a checksum scheme / non-string type / no companion / a parent that ran Python REMAP -> declined with its reason, output still oracle-parity.
- **hash regression (Codex MED, strengthen):** a MIXED hash+fpe-key job asserts hash's `sequential_hash` dispositions (scalar + REMAP + inherited) are byte-identical in VALUE and MULTIPLICITY to pre-slice; fpe entries land only under `sequential_fpe`.

## 3. Part B -- conservative decline for the not-guaranteed-injective key operators
For a `categorical` / `bucket_perturb` / `date_shift` column that is an FK parent/child key, `prepare_table` records a REPORTING-ONLY decline disposition (NOT an execution decision -> stays Python, output unchanged) with a value-free reason code `sequential_fk_key_injectivity_not_guaranteed` (carrying strategy + column + Arrow type). Surfaced in `metrics()` under that op's disposition key.
- **B1** per operator: output still matches the pandas oracle (unchanged), AND `metrics()` carries the explicit decline entry (no longer silent). Assert ZERO key-kernel calls for that column.
- **B2** the same operator as a NON-key payload column is unaffected -- still masks native (D2c-ii/iii/iv), no decline.
- **B3** the reason code is value-free (no cell/key/derived value).

## 4. Failure modes (reproduce, do not smooth)
- A native fpe REMAP orphan key using the wrong tweak (child name instead of parent's) silently breaks the join -- the tweak-consistency cell with distinct names + the child-tweak mutation pins it.
- fpe fail-closed must fire on the native REMAP (errors checked before any value returns).
- A chain-middle fpe column must not be both rehashed-as-child and remapped (only the REMAP decision; assert no scalar call).
- Metrics generalization must not move hash's entries off `sequential_hash` nor change their multiplicity (the mixed-job regression cell).
- Part B must not make a lossy op native on a key, nor touch its payload path.

## 5. Pinning tests to flip/extend
- `tests/parity/test_phase_d_d2c_fpe_seq.py` `test_fpe_key_columns_get_no_native_decision_and_match_the_python_oracle` (~:306-320) + `test_an_fpe_key_does_not_disturb_a_native_hash_payload_beside_it` (~:325): fpe keys now go native -- rewrite to assert the native decision + witness (honest mechanical update).
- `tests/parity/test_phase_d_d2c_iii_bucket_seq.py` / `test_phase_d_d2c_iv_date_shift_seq.py` FK-key boundary pins: keep Python but assert the explicit `injectivity_not_guaranteed` decline reason (Part B), not silent Python.
- New suite `tests/parity/test_phase_d_fpe_key_seq.py` (+ `_real.py`) for the matrix; reuse/parametrize `_d2b_shapes`/`_dcomp_shapes` over the key strategy. Preserve `tests/unit/transforms/test_fpe_remap_orphan_charset.py` + the all-out-of-charset fail-closed path.

## 6. Module size
`_sequential_native.py` is 525. Part A adds the fpe REMAP branch + generalizations (est. +40 to +70 -> ~565-595). 600 is the review/census threshold, 700 the hard ceiling. If it crosses 600, extract the REMAP machinery (`_remap_tokens` + `remap_scope` + the `RemapRecord` wiring) into `_sequential_native_remap.py` as a behavior-preserving extraction FIRST, with one owner; resolve before final verification. Measure post-build.

## 7. Verification + gates
- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python`; ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python`; one test process at a time via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- The matrix passes in abi-3 (native witness non-vacuous); companion-independent parts + Part B in ci-mirror. Full existing SEQ suite stays green.
- Coverage + mutation on every changed production line (gate, REMAP generalization, metrics routing, decline path); prove any surviving mutant equivalent by injection (D2c-iii/iv lesson); the child-tweak mutation must be killed.
- Gate chain: Opus VERIFY (diff + both venvs + equivalence proofs) -> dennis -> Codex final. Merge under the standing Rust rule (dennis GO + Codex GO + CI green + the fpe-key parity contract + faster on the fpe-key step + within memory ceiling).

## 8. Done when
- [ ] Every §2b cell green (all fpe-key shapes x policies incl genuine-orphan FAIL, routing dispositions, tweak consistency + child-tweak mutation killed, fail-closed, declines, mixed-job hash regression with exact multiplicity); B1-B3 green.
- [ ] Pinning tests flipped honestly; no existing test weakened; full SEQ suite green both venvs.
- [ ] Changed production lines coverage+mutation-proven; `_sequential_native.py` < 600 (or REMAP extracted first).
- [ ] ROADMAP + RECENTLY-SHIPPED updated (fold the split-done note); graphify refreshed to the merge SHA (covers split + this slice); Slack recap; the "allow a not-guaranteed-injective op on a key at all?" PO question surfaced.

cam
