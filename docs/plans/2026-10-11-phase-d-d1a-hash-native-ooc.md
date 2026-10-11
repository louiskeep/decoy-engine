Status: plan (Phase D slice D1a: hash native on the OOC route; sub-slice of the approved Phase D program plan, refined from program-D1 by implementation complexity; rev 3, folds the Codex plan-gate NO-GO of rev 2: #2 CLOSED, #1/#3/#4 HIGH + #5/#6 MEDIUM remained)

# Phase D / D1a: payload hash masking native on the out-of-core route

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md` (incl. the 2026-10-11 graph-current rule), `autonomous-operation.md`, `pre-release-audit.md`; engine `CLAUDE.md` (established-methodology, NEVER reinvent the keyed derivation; module-size; `internal/` boundary; comments explain-why); engine best-practices (validation never mutates; land the contract test first; behavior-preservation under refactor). Grounding: the approved Phase D program plan (`docs/plans/2026-10-10-phase-d-fk-routes-program.md` @ `phase-d-fk` e2401aad, §4 tier matrix + §5 no-legacy transition + §7 D1 row + §8 gate), plus a VERIFIED source survey (every call site, type gate, loader, and evidence surface below was read on engine `origin/main` @ `cc2f1c71`, post-D0 #236, and the rev-2 gate's two counterexamples were reproduced).

Base: engine `origin/main` @ `cc2f1c71` (= abi-3). Branch `feat/phase-d-d1a-hash-categorical-ooc`. Verifies on `ci-mirror` (declined path) + an abi-3 companion-present isolated venv (native path); see §8.

## 1. Why + what
The out-of-core (DuckDB + Arrow) route admits `hash` but computes it in pure Python (`kernel/_scalar.hash_array`, a per-value `derive(...).hex()` loop). The native-chunked route already masks `hash` through the compiled companion kernel (`native/_kernels_keyed.native_keyed_hash` -> `_crypto_ext.load_compiled_crypto_kernel().derive_batch`), graded byte-for-byte against the same Python reference by KAT vectors. D1a routes the OOC **payload** hash compute through that SAME kernel for columns whose Arrow type the kernel admits, so admitted OOC payload hash runs on Rust/Arrow. No new Rust, no ABI change.

D1a is the FIRST slice that makes OOC masking native, so it carries the native-OOC mechanism risk and is scoped to the SIMPLEST hash surface only (see §2 scope boundary). Codex affirmed hash-only-first, the `derive_batch` reuse, preserving the no-companion capability, and that §5 permits "declined + Python retained" (not hard-fail) as long as the disposition is caller-visible and never counted native-admitted.

## 2. Scope
D1a makes the OOC route expose THREE distinct hash-compute entrypoints (verified @ cc2f1c71); the scope boundary is drawn deliberately so the first native-OOC slice stays minimal and every site is dispositioned (program §5: no silent fallback):

**IN (routed native when eligible):**
- **Payload hash via `mask_batch`** in BOTH streaming drivers: `out_of_core/_runner.py:428` (`_stream_table` rewrite pass) and `out_of_core/_stream_driver.py:333` (`stream_table`). Inside `_mask.mask_column`'s `hash` branch, when the per-column decision (§3) is native, compute through the companion `derive_batch` instead of `hash_array`. Whole column takes ONE path, never half-native.

**OUT for D1a, EXPLICITLY DECLINED with a recorded caller-visible reason (Python retained, deferred to a later FK-key-native slice), NOT silently Python:**
- **Parent-key hash**: `out_of_core/_relation.py:~126` calls `hash_array` directly (its own binding) to hash parent-key columns for the relation map.
- **REMAP FK hash**: `out_of_core/_stream_join.py:709` and `out_of_core/_batch_join.py:345` hash via `mask_column(pa.array(normalized, from_pandas=True), ...)`. These build the array from a pandas-normalized REMAP value whose EFFECTIVE Arrow type can differ from the source schema and between batches (null-only vs integer; bool normalization yields integers) (rev-2 finding #3). Routing them native safely needs an effective-type contract that is its own slice.
These three sites each get an explicit decline reason (`fk_key_hash_native_deferred`) on the evidence channel (§4), so the audit sees them declined, not silently pandas/Python. D1a changes NO behavior at these sites (still `hash_array`, byte-identical).

**Also out:** categorical + bucket_perturb native-OOC; SEQ route (D2); any change to the companion crate / ABI / the native-chunked route; any change to OOC admission/routing; any change to `hash_array` itself; the 100M/200M growth proofs (D4c).

## 3. Eligibility, pinning, and the parity-safe native contract (folds #1 + #3)
**Native eligibility is a parity-safe contract, not merely companion admission (#1).** A payload hash column is routed native only when ALL hold, decided ONCE at run/table-prep and immutable for the run:
1. The companion loads and is ABI-compatible (abi-3), established once per run (not per call; see below).
2. The column's up-front Arrow type is in `native/_requirements.is_admitted_native_hash_type` AND is parity-safe (below).

**Parity-safe type set (rev-2 counterexamples reproduced):**
- `utf8/large_utf8/bool/int8..int64/uint8..uint64` are parity-safe: bool is one byte, all integer widths share the length-prefixed signed encoding (confirmed incl. negatives and unsigned > 2**63).
- **Timezone-aware timestamps are DEFERRED wholesale to the declined path** (reason `hash_input_type_native_parity_unsafe:{col}:{type}`). Counterexample: `pa.array([-2**63], type=pa.timestamp("ns", tz="UTC"))` has Arrow `null_count==0` but `to_pylist()` yields `NaT`, so Python `hash_array` returns `None` while the Rust canonicalizer hashes the tick. Admitting tz-aware would break byte-parity. (A future slice may add bounded preprocessing that preserves Python missingness; out of D1a.)
- **Non-admitted types keep Python but are NOT assumed to succeed.** Python `hash_array` does NOT canonicalize every value: a non-null float raises `float_canonicalization_unsupported` and a tz-naive datetime raises `timezone_naive_datetime` (both reproduced). D1a distinguishes (a) types Python masks successfully on the declined path, from (b) types whose shipped behavior is a TYPED ERROR. For (b) D1a preserves the existing error; it never manufactures a successful baseline. It also distinguishes a null-typed array from an all-null array of an admitted type.

**Pinning + call-site threading (#3).** The companion is loaded ONCE per run at table-prep (`native_keyed_hash` reloads on every call at `_kernels_keyed.py:60`, so D1a threads the PRE-LOADED kernel into the per-batch native call rather than calling `native_keyed_hash` unchanged). The per-column decision (native / declined+reason) is computed from the companion-load result + the up-front Arrow type and threaded, immutable, into BOTH drivers' `mask_batch` path. Because hash OUTPUT is always string, the fixed output schema is NOT evidence of input type; the decision is keyed to the INPUT column type captured at prep. Each native batch VALIDATES its incoming Arrow type against the pinned expectation and ABORTS the run on mismatch (fail-closed), never switching to Python mid-column. The LazySource schema/read reopen gap (`profile/_readers.py:211`, documented there) is accounted for so the prep-time type matches the streamed type. A mid-run companion fault ABORTS: the mid-run-fault test proves no Python switch, no returned partial result, and sink abort without commit.

## 4. Caller-visible disposition (folds #4, program §5)
§5 requires each cell to be {native-admitted + parity + compute-witness} OR {declined with a caller-visible reason}; nothing silently falls back. §4 of the program permits declined+rerouted (Codex affirmed D1a need not hard-fail on no-companion). D1a surfaces per-column disposition truthfully:
- `_chunked_evidence` is NOT a shared publication channel (it builds table-level dicts for the chunked route and does not reach the OOC return). So D1a adds a concrete **`ExecutionResult.quality_metrics`** entry for OOC hash disposition: keyed by table + column, carrying the per-column executed-backend and, when declined, the reason code. The OOC runner already returns accumulated quality metrics (`_runner.py:271`) and `run_pipeline` copies them, so no new public result class is needed; thread the entry through BOTH drivers and BOTH the resident and sink return paths.
- **Truthful executed-backend label.** The OOC Python `hash_array` loop is neither the pandas oracle nor an Arrow-Python kernel, so labeling it `PANDAS_ORACLE` (`_operator_registry.py:44`) would misreport execution. D1a adds a truthful token (e.g. `PYTHON_SCALAR = "python_scalar"` in `_operator_registry.py`) for the OOC Python hash loop, distinct from `rust_companion`/`arrow_python`/`pandas_oracle`; native payload hash reports `rust_companion`. A declined cell is never counted native-admitted.
- Reason codes (value-free, log-hygiene safe: strategy, column, Arrow type only; never cell/key/derivation values): `crypto_extension_unavailable`, `hash_input_type_not_native_admitted:{col}:{type}`, `hash_input_type_native_parity_unsafe:{col}:{type}`, `fk_key_hash_native_deferred:{site}`.
- Visibility is asserted THROUGH `run_pipeline` (the caller surface), not only an internal dict.

## 5. Behavior-preservation contract
1. **Byte-identical output:** OOC payload hash output is identical whether native or Python, for every parity-safe admitted cell (KAT-pinned reference equality; asserted on the OOC path).
2. **Oracle parity:** OOC native payload hash is strict-equal (D0 `_strict_compare`, modulo frozen D0 gaps) to the pandas oracle, on an FK-chain payload hash column and a non-FK payload hash column.
3. **Per-batch == whole-column:** streamed per-batch native hash equals the single-pass hash.
4. **Never half-native:** one path per column; decision pinned at prep; mid-run fault aborts.
5. **No silent fallback (§5):** every payload hash column is {native + witness + `rust_companion`} OR {Python + truthful `python_scalar` backend + a caller-visible reason}. Parent-key + REMAP hash sites report `fk_key_hash_native_deferred` and are unchanged. No capability regression on a no-companion install.
6. **Witness:** native payload hash proves native-result-CONSUMED at the insertion boundary (§6); declined reports Python-only. No existing test weakened; `hash_array` untouched.

## 6. Acceptance tests (land the contract test first)
- **Declined, companion absent (ci-mirror):** OOC payload hash succeeds; output byte-identical to the pinned pre-D1a Python baseline; disposition = `python_scalar` + `crypto_extension_unavailable`; witness = Python-only.
- **Declined, parity-unsafe / non-admitted (either env, companion present):** a tz-aware-timestamp column reports `hash_input_type_native_parity_unsafe` and stays Python; a non-admitted type that Python masks successfully (e.g. date) reports `hash_input_type_not_native_admitted` and matches the Python baseline; a float / tz-naive datetime column still raises its existing TYPED ERROR unchanged (no manufactured success). Null-typed vs all-null-admitted distinguished.
- **Native, companion present (abi-3 venv):** payload hash on admitted parity-safe columns strict-equal to oracle AND equal to the Python reference; disposition = `rust_companion`; per-batch == whole-column; covers every admitted integer WIDTH + extrema, bool, utf8 and large-utf8 (NFC), and truncate present/absent; a secret-backed `mask_key` distinct from `job_seed` with namespace binding verified.
- **Both drivers + both outputs:** the native/declined behavior holds in `_runner._stream_table` AND `_stream_driver.stream_table`, and on resident and sink return paths. Parent-key (`_relation`) and REMAP (`_stream_join`/`_batch_join`) sites report `fk_key_hash_native_deferred` and are byte-unchanged.
- **Boundary (#6):** empty/zero-row run emits truthful "no kernel executed" evidence; typed all-null batch FOLLOWED by a populated batch; nulls interleaved (null rows preserved); multi-batch column (multi-call attribution); ABI-incompatible companion load declines with `crypto_extension_unavailable`.
- **Witness hardening (#5):** per-call attribution keyed at the mask-column/batch INSERTION boundary with explicit table/column/call/op context; loader self-test calls excluded from payload attribution; a NEW negative control where native runs but its result is DISCARDED and an equal-valued Python array is emitted MUST FAIL the native-consumed assertion (closes the by-value hole in `_kernel_witness.py:165`); the four D0 provenance controls still pass; the D0 legacy-path-only control re-run under forced unavailability on the OOC path.
- **Performance gate (program §8, predeclared):** isolated-process micro-benchmark of the OOC payload hash column, native vs Python, with a PREDECLARED workload (row count, column type), batch + native-thread settings, round counts (native full rounds; Python minimal with its limited p95 precision disclosed), a numeric absolute peak-memory ceiling, and the acceptance criterion = native is FASTER than Python AND within the declared memory ceiling (not merely "not a regression"). 100M/200M proofs remain D4c.
- **Must pass unchanged:** the full D0 parity suite + `tests/parity/test_out_of_core_fk_parity.py` + the OOC route's existing tests + module-size/import-boundary sentries. No existing test weakened; `hash_array` untouched.

## 7. Build sequencing (ordered green commits)
1. Pin the pre-D1a OOC payload-hash baseline (test-only) for byte-identity.
2. Harden `_kernel_witness` to insertion-boundary per-call attribution + the discard negative control + exclude loader self-test; D0 suite stays green.
3. Add the `python_scalar` backend token + the per-column OOC hash disposition entry on `ExecutionResult.quality_metrics`, threaded through both drivers + resident/sink returns, visible through `run_pipeline`; no compute change yet (every column still Python, now dispositioned with truthful backend + reason).
4. Add the prep-time per-column native decision (companion loaded once + parity-safe eligibility) + the kernel threaded into both drivers' `mask_batch` native path, with incoming-type validation + abort-on-mismatch; wire the native compute; explicit decline for parent-key + REMAP.
5. Add the native + declined + boundary parity/witness cases (§6) and the predeclared perf benchmark.
6. Confirm ci-mirror (declined) + the abi-3 companion-present run (native) + full D0/OOC suites green.
Each commit green; no push/merge beyond the slice.

## 8. Verification
- **Build the abi-3 companion first (Cam-approved):** build an abi-3 `.so` from THIS worktree's crate (`decoy-engine-native/`) into an ISOLATED venv whose engine `src` points at this worktree (NOT the shared `~/.cache/decoy-native-venv`; respects the editable-`.pth` prune hazard, zero blast radius). At verification RECORD: interpreter, engine import path, companion binary path, the companion's `abi_version()` (assert == abi-3, not merely that load succeeded), and the engine revision. Native tests must EXECUTE, not silently skip; a skip is a verification failure for the native legs.
- `ci-mirror` (clean venv, NO companion): lint/type/sentries + the declined-path + parity (native-only cases skip) + full D0/OOC suites. `ci-mirror --rebuild` first (the editable `.pth` was stale at D0).
- abi-3 companion-present isolated venv: native-path parity + witness + per-batch + boundary + perf. Record which legs ran where.

## 9. Done when
- [ ] OOC PAYLOAD hash computes native for parity-safe admitted columns when the abi-3 companion is present (witness proves native-result-CONSUMED at the insertion boundary); Python with a truthful `python_scalar` backend + caller-visible reason when declined (companion absent/ABI-incompatible, type not admitted, or parity-unsafe); parent-key + REMAP hash report `fk_key_hash_native_deferred` and are byte-unchanged; never half-native; decision pinned at prep, kernel loaded once, mid-run fault aborts.
- [ ] Byte-identical to the Python reference + strict-equal to the oracle across every admitted integer width + extrema, bool, utf8/large-utf8, truncate, secret-backed mask_key; tz-aware timestamp + float + tz-naive declined/errored correctly (no manufactured success); per-batch == whole-column; boundary cases covered on BOTH drivers + resident/sink.
- [ ] Disposition visible through `run_pipeline` (not just an internal dict); `_kernel_witness` closed against equal-valued substitution (discard negative control fails as required) + D0 legacy control re-run under forced unavailability; D0 + full OOC suites green; no existing test weakened; `hash_array` untouched.
- [ ] Perf benchmark with predeclared workload/batch/threads/rounds/memory-ceiling shows native FASTER + within ceiling; ci-mirror (declined) + abi-3 companion-present (native) both green and recorded; dennis GO; Codex final GO.

cam
