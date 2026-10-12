# Phase D / D2c-i: generalize the SEQ-native seam + wire `fpe`

Status: plan (rev 1, pre-gate). First slice of D2c's ENGINE half (remaining SEQ payload
operators native). D2c's platform-sequential-activation half is PARKED (needs A3 + Cam go).
Branches off origin/main (f9885434, includes D2a #241 + D2b #242) on
feat/phase-d-d2c-seq-operators (worktree phase-d-d2c).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md, autonomous-operation.md.
Engine CLAUDE.md (established-methodology, module caps, internal boundary), compatibility-contract.md.

Risk: R2. Moves payload token computation for one more strategy onto a compiled kernel on the
SEQ route, with byte-parity + the accepted NFC divergence. Plan + independent review + Codex
plan gate + final gate before merge, per the standing Rust rule.

## FRAME

**Problem.** On the sequential (SEQ) FK route, only `hash` runs on a compiled kernel today
(D2a #241 + D2b #242). Every other payload strategy that HAS a compiled kernel on the
out-of-core / native-chunked routes - `fpe`, `categorical`, `bucket_perturb`, `date_shift`,
`text_mask`, `text_redact` - still runs in Python on SEQ (`_strategies/<op>.py` with no
`seq_native` block; `_sequential_native.py:202` admits `strategy == "hash"` ONLY). So a bounded
SEQ job that masks, say, an `fpe` payload column gets the pandas handler, not the Rust kernel.

**Why a framework first.** `SeqNativeRun` (`_sequential_native.py`) is hash-specific end to end:
`_decide` hard-codes hash's `decline_reason`, `hash_column`/`_remap_tokens` call
`native_hash_array`, the disposition key is the literal `sequential_hash`, and `prepare_table`
filters to `strategy == "hash"`. Adding five more operators by copying that module five times
would be five near-duplicate kernel-dispatch paths to keep in parity-lockstep. Instead this
slice generalizes the per-column decision + scalar-kernel seam to a SMALL strategy-keyed
dispatch (a table of {strategy -> native-op descriptor}), then wires the FIRST new operator
(`fpe`) through it. hash is migrated onto the same table unchanged (its tokens stay
byte-identical). The remaining operators (D2c-ii/iii) then plug into the table, not copy the
module.

**Why `fpe` first.** It is the lowest-friction real-kernel operator: a compiled crypto-family
kernel (`native/_fpe_ext.py`, same positive-kernel-evidence shape as hash), PAYLOAD-ONLY (never
a parent key in the FK graph, so it needs only the SCALAR execution path - NOT the REMAP
orphan-minting path, which stays hash-only this slice), and already proven byte-parity on the
chunked route (`tests/native/` fpe parity). It exercises the generalized scalar seam with a
compiled kernel without the REMAP or pool-plumbing complexity.

**Definition of done.** On the SEQ route, an `fpe` payload column whose Arrow input type the
`fpe` admission predicate accepts computes its tokens on the SAME compiled `fpe` kernel the
chunked route uses, byte-identical to the Python `fpe` handler except the already-accepted
native-vs-Python NFC divergence (Unicode-version-dependent, documented for hash/categorical;
`fpe` operates on configured-radix strings so it carries the same envelope). A non-admitted
`fpe` input type, a missing companion, or a kernel fault declines-with-reason to the Python
handler (no silent fallback; a fault aborts like hash). hash behavior, disposition, and all
D2a/D2b parity are UNCHANGED. Strategies with no registered native op (everything except hash
+ fpe this slice) keep running in Python exactly as today.

**Boundaries / this slice.** SEQ route only. Generalize the seam + wire `fpe` ONLY. NO REMAP
generalization (fpe is payload-only; REMAP stays hash-only, untouched). NO index-kernel ops
(categorical/bucket_perturb/date_shift = D2c-ii), NO text ops (D2c-iii), NO pure-Arrow trio, NO
faker/group_key/code_set/nested/etc. (deferred, see Non-goals). NO platform activation (A3 +
Cam go). Full-frame, OOC, chunked routes UNCHANGED. The generalization must not alter any hash
token, disposition entry, or decline reason.

## Established solutions

- D2a/D2b already established the pattern this reuses UNCHANGED: the compiled kernel is called
  at a per-column scalar seam from the strategy handler via `getattr(ctx, "seq_native")`
  (`_strategies/_hash.py:52-65`); the run-scoped `SeqNativeRun` pins each column's Arrow source
  type at `prepare_table` and rebuilds the pandas column to that pinned type before the kernel
  (`_sequential_native.py:_pinned_array`); there is no fallback (fault aborts); reason codes
  carry strategy + column + Arrow type only, never a value.
- The `fpe` compiled kernel + its admission already exist and are proven on the chunked route:
  `native/_fpe_ext.py` + `native/_fpe_route.py` + the operator-registry spec
  (`_operator_registry.py:260-267`, `required_kernel = fpe`). This slice REUSES that kernel and
  its decline predicate; it does NOT re-implement FPE (established-methodology rule - FPE is a
  cryptographic primitive, we do not roll our own).
- The generalized dispatch is a SMALL table, not a plugin framework (engineering-pillars: no
  machinery beyond the need). Each native-capable SEQ strategy registers a descriptor:
  `(load_kernel, decline_reason, apply_to_pinned_array, disposition_key)`. `_decide` and the
  scalar seam look the descriptor up by `seed.strategy`; absent => Python (today's behavior).

## Design (strategy-keyed scalar seam, SEQ)

1. **Native-op descriptor table.** Introduce a per-strategy descriptor (a frozen mapping in
   `_sequential_native.py`, or a sibling `_sequential_native_ops.py` if the module nears its
   cap): `strategy -> SeqNativeOp(load_kernel, decline_reason, apply, disposition_key)` where
   `apply(bound_kernel, pinned_array, seed_params) -> pa.Array`. hash's entry wraps the existing
   `load_compiled_crypto_kernel` + `decline_reason` + `native_hash_array`; fpe's entry wraps the
   compiled fpe kernel loader + the fpe decline predicate + the fpe apply. hash's descriptor
   MUST reproduce today's tokens/paths exactly (regression-pinned).
2. **`prepare_table` generalization.** Replace the `seed.strategy != "hash"` filter
   (`_sequential_native.py:202`) with "look up `seed.strategy` in the descriptor table; skip if
   absent". A column gets an execution decision from its strategy's descriptor
   (`decline_reason` + that strategy's kernel load). REMAP decisions stay hash-only this slice
   (fpe is never a parent key; if a future key-capable op is added, REMAP generalizes then -
   explicitly out of scope here). The per-column decision stays run-scoped and pinned-type as
   today.
3. **Scalar seam generalization.** `hash_column` becomes a strategy-agnostic
   `native_column(ctx, df, column, seed_params)` that: looks up the column's decision; if native,
   rebuilds the pinned array and calls the descriptor's `apply`; returns the positional list or
   None. hash keeps its exact call signature behavior (its `apply` passes namespace/mask_key/
   truncate). fpe's `apply` passes fpe's configured params (radix/alphabet/tweak etc. from the
   seed). The `remap_scope` / `_remap_tokens` path is UNTOUCHED (hash-only).
4. **Handler wiring.** `_strategies/_fpe.py` gains the same `getattr(ctx, "seq_native")` block
   `_hash.py:52-65` has: ask `seq_native.native_column(...)`; if it returns tokens, assign
   positionally and return; else fall through to the existing Python fpe handler. No other
   handler changes.
5. **Disposition.** The disposition key becomes per-strategy (fpe entries under a `sequential_fpe`
   key or a unified `sequential_native` list tagged by strategy - pick one and pin it). hash's
   existing `sequential_hash` fragment MUST be preserved byte-for-byte (surface-pin test).

## Pinned guarantee and non-goals

**Guarantee.** An admitted SEQ `fpe` payload column masks byte-identically to the Python `fpe`
handler except the accepted NFC divergence; a non-admitted input type / missing companion /
fault declines-with-reason or aborts exactly as hash does; the computation runs on the compiled
`fpe` kernel (proven by the SEQ marking-witness). hash tokens, hash disposition, hash REMAP, and
every D2a/D2b/full-frame/OOC/chunked behavior are UNCHANGED (regression-pinned). Strategies with
no descriptor run in Python as today.

**Non-goals.** NO REMAP generalization (hash-only this slice; fpe is payload-only). NO
categorical/bucket_perturb/date_shift (D2c-ii). NO text_mask/text_redact (D2c-iii). NO
pure-Arrow trio (redact/truncate/passthrough). NO faker (pool plumbing), group_key (cross-row),
code_set/bucketize/nested/joint_mask/geo_generalize/formula/derived/composite (no compiled
kernel). NO platform sequential activation (A3 + Cam go). NO new FPE implementation. NO change to
`resolve_fk_keys`, routing, or the full-frame/OOC/chunked paths.

## Acceptance tests (authored before impl)

- **hash regression (no observable change):** the full D2a/D2b SEQ suites
  (`test_phase_d_d2a_seq_*`, `test_phase_d_d2b_seq_*`) pass unchanged; the `sequential_hash`
  disposition fragment is byte-identical (surface-pin); hash tokens/REMAP/inherited entries
  unchanged. This is the "generalization changed nothing for hash" proof.
- **fpe parity:** a SEQ job with an `fpe` payload column (payload + across an FK edge where the
  fpe column is NOT a key) masks byte-identical to the Python fpe handler on strict dimensions
  (`_strict_compare.py`: schema/metadata/width/order/values/null-vs-NaN), for every admitted
  input type; RI unaffected (fpe is payload, keys are hash). Oracle = the Python fpe handler on
  the same job.
- **fpe native witness:** reuse the SEQ marking approach (`tests/parity/_seq_witness.py` pattern)
  with an fpe marking stand-in to PROVE the fpe tokens came from the kernel (SEQ rebuilds buffers
  via `from_pandas`, so buffer-identity can't be used - use the marking kernel, as D2a did).
- **fpe decline (fail-closed, coded, no value in message):** a non-admitted fpe input type
  declines-with-reason to Python; companion-absent declines (REASON_UNAVAILABLE path); a kernel
  fault aborts the run (no silent fallback, no part-native/part-Python column). Reason codes
  carry strategy + column + Arrow type only.
- **NFC envelope:** an fpe value with a post-running-Python-Unicode-version codepoint carries the
  same accepted native-vs-Python divergence as hash (assert documented, version-guarded), OR -
  if fpe's radix/alphabet makes it NFC-agnostic - assert byte-identical and record that fpe does
  NOT carry the divergence (verify which during build; document in SEMANTIC_DIFFERENCES.md).
- **Descriptor-absent = Python:** a strategy with no descriptor (e.g. categorical) still runs in
  Python on SEQ unchanged (guards against the generalization accidentally admitting an unwired
  strategy).
- **Perf gate:** a sized SEQ fpe workload runs native faster than the Python handler, within the
  declared memory ceiling, byte-identical digest (predeclared perf test mirroring
  `test_d2a_seq_hash_native_perf.py`).
- **Module caps + boundaries:** `_sequential_native.py` (and any new `_sequential_native_ops.py`)
  under 700 (census entry if >600); `tests/sentry/test_module_size.py`,
  `test_public_import_boundary.py`, `test_physical_seam_disconnection.py` pass; the
  surface/run-call pin tier passes (per the D1c lesson: run the full sentry/surface-pin tier
  locally for native/adapter-touching slices).

## Known context for the gate

- The generalization is the architecturally significant part; fpe is the first tenant. Keeping
  hash byte-identical through the refactor is the central regression risk - pin it hard.
- `fpe` is payload-only in the FK graph, so the REMAP path genuinely does not apply; do not
  generalize REMAP speculatively (YAGNI / no machinery beyond need). When a key-capable native
  op is added later (the pure-Arrow trio), REMAP generalizes in that slice.
- Reuse the compiled `fpe` kernel + decline predicate from `native/_fpe_ext.py` /
  `_fpe_route.py` as-is; do not re-derive FPE. Verify the chunked route's fpe admission predicate
  is the right one to reuse for SEQ (same pinned-type conversion contract as hash).
- Carries the standing accepted NFC divergence family; confirm fpe's exact status (carries vs
  NFC-agnostic) during build and document it.
- Test env: companion/native tests need the abi-3 venv; confirm the import path resolves to THIS
  worktree first; run one heavy test process at a time.

## Docs to update

`docs/compatibility-contract.md` (SEQ `fpe` now native, same NFC envelope family), `docs/strategies.md`
(fpe native on SEQ), `tests/parity/SEMANTIC_DIFFERENCES.md` (fpe NFC status), CODEMAP (the
generalized SEQ-native seam / any new module), and the platform ROADMAP D row (D2c-i done) at
DOCUMENT. Follow-on operators D2c-ii (categorical/bucket_perturb/date_shift) and D2c-iii
(text_mask/text_redact) tracked for subsequent slices.
