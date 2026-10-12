# Phase D / D2c-ii: wire `categorical` + `bucket_perturb` into the SEQ-native framework

Status: plan (rev 1, pre-gate). Second slice of D2c's ENGINE half. Builds on D2c-i (engine #243,
the strategy-keyed `_sequential_native_ops` descriptor framework + `fpe`). Branches off origin/main
(b52fe688, includes D2c-i) on feat/phase-d-d2c-ii-seq-index-kernels (worktree phase-d-d2c-ii).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md, architecture.md,
reuse-first.md, testing.md, documentation.md, autonomous-operation.md. Engine CLAUDE.md
(established-methodology, module caps, internal boundary), compatibility-contract.md.

Risk: R2. Moves payload token computation for two more strategies onto compiled kernels on the SEQ
route, with byte-parity + the accepted NFC divergence. Plan + independent review + Codex plan gate +
final gate before merge, per the standing Rust rule.

## FRAME

**Problem.** D2c-i generalized the SEQ-native seam into a strategy-keyed descriptor table and wired
`fpe`. `categorical` and `bucket_perturb` already run native on the out-of-core and native-chunked
routes (D1b-ii #238, D1b-i #239) via compiled INDEX kernels, but still run in Python on the bounded
sequential (SEQ) FK route - they have no descriptor registered. Register them so a SEQ job masks a
`categorical` or `bucket_perturb` payload column on the same compiled kernel the chunked route uses.

**Why these two together.** Both are payload-only INDEX-kernel operators with the same shape: a
deterministic keyed index selects from a prebuilt structure (`native_categorical` /
`native_bucket_perturb`), the kernel RAISES on an invalid input (GenerationError / AssertionError)
rather than returning a per-cell error channel, and both carry the same accepted native-vs-Python NFC
divergence as hash (the keyed index derives from the NFC-normalized source value). They are strictly
MORE MECHANICAL than fpe: no `fail_closed` error channel (a raise aborts the run like a kernel
fault), no handler warning pass. Each is one descriptor registration + its config-aware admission.
`date_shift` (the third index kernel) is a SEPARATE later slice because it carries the OOC-deferred
whole-column format-detect + per-value format-error row-error semantics that need their own parity
check; the text operators are later still.

**Definition of done.** On the SEQ route, a `categorical` or `bucket_perturb` NON-KEY payload column
whose resolved configuration AND Arrow input type its admission predicate accepts computes its tokens
on the SAME compiled index kernel the chunked route uses, byte-identical to the Python handler except
the accepted NFC divergence (both operators carry it). A non-admitted config/type, a missing
companion, or a kernel fault declines-with-reason to Python or aborts (no silent fallback). The
operator stays PAYLOAD-only: a `categorical`/`bucket_perturb` column used as an FK key gets no native
decision and stays Python (the framework's non-hash-key boundary already enforces this; confirm). hash
and fpe behavior, their disposition, and all D2c-i/D2a/D2b parity are UNCHANGED.

**Boundaries / this slice.** SEQ route only. Register `categorical` + `bucket_perturb` descriptors
ONLY. NO `date_shift` (D2c-iii), NO text ops (D2c-iv), NO pure-Arrow trio, NO faker/group_key/etc.
(deferred). NO framework changes beyond adding two `_OPS` entries and whatever per-descriptor hook the
two operators need (if the descriptor contract needs no change, do not change it). NO REMAP
generalization (both are payload-only; REMAP stays hash-only). NO platform activation (A3 + Cam go).
Full-frame, OOC, chunked routes UNCHANGED.

## Established solutions

- D2c-i's descriptor framework (`_sequential_native_ops.py`: `NativeOp` = load_kernel + config-aware
  `admit(column, input_type, resolved_config)` + `apply` + optional `fail_closed` + optional `warn` +
  disposition_key; per-strategy kernel state; `prepare_table` non-hash-key boundary; `native_column`
  scalar seam) is REUSED UNCHANGED where possible. categorical/bucket_perturb register a descriptor
  each: `fail_closed=None` (kernel raises -> abort), `warn=None` (no handler warnings - VERIFY in the
  handlers `_categorical.py`/`_bucket_perturb.py`).
- The compiled kernels + admission already exist and are proven on OOC/chunked: `categorical` via
  `native/_categorical_ext.py` (`native_categorical` / `native_categorical_positional`),
  `bucket_perturb` via `native/_bucket_perturb_ext.py` (`native_bucket_perturb`), both index-kernel
  (`derive_index_batch`). REUSE each operator's existing config-aware admission predicate (the one the
  chunked/OOC route uses - likely in `native/_real_type_admission.py` or the operator resolver; the
  builder locates and reuses it, NOT a new predicate). This slice does NOT re-implement either kernel
  or its admission (established-methodology).
- The accepted NFC divergence is already documented for hash + categorical; bucket_perturb carries it
  too (D1b-i). No NEW semantic difference is introduced.

## Design

1. **Descriptor registration (per operator).** Add a `categorical_op` and a `bucket_perturb_op`
   factory to `_sequential_native_ops.py`, each returning a `NativeOp`:
   - `load_kernel`: the operator's compiled index-kernel loader (reused).
   - `admit`: the operator's existing config-aware admission (categorical: deterministic + namespaced +
     string-category config AND a string source; bucket_perturb: explicit `date_format`, no tz
     directive, admitted date range) - reused from the chunked/OOC path, taking the resolved seed
     config, declining everything else with its coded reason.
   - `apply`: rebuild the pinned array, call `native_categorical`/`native_bucket_perturb`, return
     `NativeResult(values, errors=())`. A kernel raise (GenerationError/AssertionError, e.g.
     bucket_perturb's date-out-of-native-range) propagates and aborts the run - no error channel.
   - `fail_closed=None`, `warn=None`, `disposition_key` = `sequential_categorical` /
     `sequential_bucket_perturb`.
   Add each to `_OPS`. If the two operators need a param object the way fpe needed `FpeConfig`, build
   the minimal analogue from the seed; do not over-generalize.
2. **Handler wiring.** `_strategies/_categorical.py` and `_strategies/_bucket_perturb.py` each gain the
   `getattr(ctx, "seq_native")` block (as `_hash.py`/`_fpe.py` have): ask `native_column`; assign the
   tokens on a non-None return; else fall through to the existing Python handler. Since neither has a
   handler warning phase (verify), a simple early-return-on-native is correct (unlike fpe).
3. **Key boundary (reuse, confirm).** The framework already gives a non-hash strategy a native decision
   only when the column is not an FK parent/child key. categorical/bucket_perturb are payload-only, so
   a key column of either strategy stays Python with no new code. Add a test proving it.
4. **Per-strategy kernel state + disposition.** Each operator loads its kernel at most once (the
   framework's per-strategy `_bound`/`_load_attempted`); disposition goes under its own key. hash + fpe
   fragments unchanged.

## Pinned guarantee and non-goals

**Guarantee.** An admitted SEQ `categorical`/`bucket_perturb` NON-KEY payload column masks
byte-identically to its Python handler except the accepted NFC divergence; a non-admitted config/type
/ missing companion / kernel fault declines-with-reason or aborts; the computation runs on the
operator's compiled index kernel (proven by a SEQ marking-witness). A key column of either strategy
stays Python. hash + fpe tokens, their disposition, REMAP, and every D2c-i/D2a/D2b/full-frame/OOC/
chunked behavior are UNCHANGED. Strategies with no descriptor run in Python as today.

**Non-goals.** NO `date_shift` (D2c-iii - its format-detect + row-error semantics need their own
slice). NO text ops, pure-Arrow trio, faker/group_key/code_set/etc. NO REMAP generalization. NO
framework redesign. NO new kernel or admission implementation. NO platform activation. NO change to
`resolve_fk_keys`, routing, or full-frame/OOC/chunked.

## Acceptance tests (authored before impl)

- **hash + fpe regression (no observable change):** the full D2a/D2b/D2c-i SEQ suites pass unchanged;
  the `sequential_hash` and `sequential_fpe` disposition fragments are byte-identical; a mixed
  hash+fpe+categorical+bucket_perturb table in varied column orders masks all correctly.
- **parity (per operator):** a SEQ job with a `categorical` (and a `bucket_perturb`) payload column,
  payload and across an FK edge where the column is NOT a key, masks byte-identical to the Python
  handler on strict dimensions (`_strict_compare.py`), for every admitted config/type; oracle = the
  Python handler on the same job. Real compiled-kernel parity suite (abi-3 companion), as D2c-i did.
- **native witness (per operator):** a SEQ marking stand-in proves the tokens came from the kernel
  (reuse the `_seq_witness.py` pattern; SEQ rebuilds buffers via `from_pandas`, so buffer-identity is
  unusable - mark, as D2a/D2c-i did).
- **config-aware decline (per operator, fail-closed, coded, no value):** categorical - a
  non-deterministic / non-namespaced / non-string-category / non-string-source column declines to
  Python; bucket_perturb - no `date_format` / a tz directive / an out-of-native-range date declines or
  fails closed exactly as the oracle does. Companion-absent declines. Reason codes carry strategy +
  column + Arrow type only.
- **kernel fault aborts:** a kernel raise (e.g. bucket_perturb date-out-of-native-range on an admitted
  config) aborts the run with no partial output (no error channel; raise propagates), matching the
  Python handler's behavior for the same input.
- **key column stays Python (per operator):** a `categorical`/`bucket_perturb` column used as an FK
  key gets no native decision; matched/orphan behavior runs Python; no `sequential_hash`/`sequential_fpe`
  contamination.
- **NFC envelope (per operator):** a source value with a post-running-Python-Unicode-version codepoint
  carries the same accepted native-vs-Python divergence as hash (documented, version-guarded) - assert
  BOTH operators carry it (bucket_perturb on its string source, categorical on its category source).
- **descriptor-absent = Python:** an un-wired strategy (e.g. date_shift, text_mask) still runs in
  Python on SEQ unchanged.
- **perf gate (per operator):** a sized SEQ workload runs native faster than the Python handler within
  the memory ceiling, byte-identical digest (mirror `test_d2a_seq_hash_native_perf.py` /
  `test_d2c_seq_fpe_native_perf.py`).
- **module caps + boundaries:** `_sequential_native_ops.py` (now growing with two more ops) under 700,
  census if >600; the SEQ-native + handler modules under caps; `tests/sentry/*` pass; run the FULL
  sentry/surface-pin tier locally (the native/adapter-touching-slice rule).

## Known context for the gate

- The descriptor framework is built + gated (D2c-i); this slice is additive. If the two operators need
  NO change to the `NativeOp` contract, do not change it - register two entries + two handler blocks.
  If they DO need a change (e.g. an index-kernel apply signature that differs from fpe's), make the
  minimal one and note it.
- Verify each operator's handler has no warning phase (so the simple early-return wiring is correct,
  unlike fpe's warning-preserving wiring). If one DOES emit warnings, wire it like fpe.
- Reuse each operator's EXISTING chunked/OOC admission predicate; do not author a new one. categorical
  and bucket_perturb both already have proven admission (D1b-ii, D1b-i) + real-kernel parity suites to
  mirror for SEQ.
- Both carry the accepted NFC divergence (unlike fpe, which is NFC-agnostic) - the acceptance test
  asserts divergence, not byte-identity, on post-version codepoints.
- Test env: companion/native tests need the abi-3 venv; confirm the import path resolves to THIS
  worktree first; run one heavy test process at a time.

## Docs to update

`docs/compatibility-contract.md` (SEQ `categorical` + `bucket_perturb` native, same NFC envelope),
`docs/strategies.md` (compute-path note for each), `tests/parity/SEMANTIC_DIFFERENCES.md`, CODEMAP,
and the platform ROADMAP D row (D2c-ii done, NEXT = D2c-iii date_shift) at DOCUMENT.
