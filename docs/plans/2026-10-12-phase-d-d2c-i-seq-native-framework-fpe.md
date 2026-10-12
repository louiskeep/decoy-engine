# Phase D / D2c-i: generalize the SEQ-native seam + wire `fpe`

Status: plan (rev 2, pre-gate re-gate). First slice of D2c's ENGINE half (remaining SEQ payload
operators native). D2c's platform-sequential-activation half is PARKED (needs A3 + Cam go).
Branches off origin/main (f9885434, includes D2a #241 + D2b #242) on
feat/phase-d-d2c-seq-operators (worktree phase-d-d2c).
rev-2 folds the rev-1 Codex plan-gate NO-GO (3 HIGH + 1 MED; descriptor-table DIRECTION
ENDORSED): HIGH-1 fpe is NOT payload-only (can be an FK parent key) -> enforce payload-only as an
admission boundary (fpe KEY column stays Python) + guard non-hash REMAP from touching hash state;
HIGH-2 the fpe kernel returns FpeBatchResult (values + per-cell errors, does not raise) -> the
seam must inspect errors + raise fpe_fail_closed_error, no output for the failed table; HIGH-3
admission must be CONFIG-aware (checksum rejected before type) -> descriptor admit takes resolved
config + full FpeParams/FpeConfig vocabulary; MED fpe security warnings (partial-plaintext/
join-group) must be preserved (handler warning phase, not dropped by an early return). Plus:
isolate kernel state per strategy; fpe is NFC-AGNOSTIC (byte-identical, no hash divergence); fpe
admits exactly pa.string().

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

**Why `fpe` first.** It is the lowest-friction real-kernel operator for the SCALAR payload path:
a compiled crypto-family kernel (`native/_fpe_ext.py`, same positive-kernel-evidence shape as
hash), already proven byte-parity on the chunked route (`tests/native/` fpe parity). CORRECTION
(rev-2, plan-gate HIGH-1): `fpe` is NOT payload-only in the FK graph - SEQ eligibility
(`_pipeline_routing.py:214`) imposes no parent-strategy restriction, so an `fpe` column CAN be
an FK parent key and the orphan-REMAP path (`_orphan.py:129`) re-runs the parent handler inside
`seq_native.remap_scope()`. This slice therefore ENFORCES payload-only as an admission BOUNDARY:
native `fpe` applies ONLY to an `fpe` column that is NOT an FK key (parent or child); an `fpe`
KEY column stays on Python, and the REMAP path stays hash-only. That keeps this slice to the
scalar path without native fpe REMAP (deferred), but it must be an enforced guard, not an
assumption.

**Definition of done.** On the SEQ route, an `fpe` NON-KEY payload column whose resolved
CONFIGURATION AND Arrow input type the `fpe` admission predicate accepts computes its tokens on
the SAME compiled `fpe` kernel the chunked route uses, byte-identical to the Python `fpe`
handler (fpe is NFC-agnostic - plan-gate confirmed both impls preserve out-of-charset chars
verbatim and do not normalize - so NO NFC divergence; byte-identical, unlike hash). The fpe
kernel returns an `FpeBatchResult` of values PLUS per-cell errors (it does not raise): the
adapter MUST inspect `result.errors` and raise `fpe_fail_closed_error` before assigning any
value, publishing NO output for the failed table (plan-gate HIGH-2). The handler's SECURITY
WARNING phase (`fpe_partial_plaintext_disclosure`, `fpe_join_group_active`) is PRESERVED - the
kernel does not supply it, so the native path still runs the handler's residual-warning pass on
the original values at handler scope, incl `when` subsets (plan-gate MED). A non-admitted config
(e.g. `checksum="luhn"`) or input type (fpe admits exactly `pa.string()`; `large_string`
declines), a missing companion, or a kernel fault declines-with-reason to Python or aborts. hash
behavior, disposition, REMAP, and all D2a/D2b parity are UNCHANGED. Strategies with no
descriptor run in Python as today.

**Boundaries / this slice.** SEQ route only. Generalize the scalar seam + wire `fpe` ONLY. NO
native fpe REMAP (an fpe KEY column declines to Python; REMAP stays hash-only, and non-hash
REMAP calls return to Python BEFORE touching hash state - plan-gate HIGH-1). NO index-kernel ops
(categorical/bucket_perturb/date_shift = D2c-ii), NO text ops (D2c-iii), NO pure-Arrow trio, NO
faker/group_key/code_set/nested/etc. (deferred, see Non-goals). NO platform activation (A3 +
Cam go). Full-frame, OOC, chunked routes UNCHANGED. The generalization must not alter any hash
token, disposition entry, or decline reason, and must ISOLATE kernel state per strategy (the
current single `_loaded`/`_bound` fields `_sequential_native.py:163` are hash-specific; fpe's
kernel load must not collide with hash's).

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
  machinery beyond the need; plan-gate endorsed the sizing). Each native-capable SEQ strategy
  registers a descriptor whose contract is shaped by what fpe actually needs (and by what
  categorical/date_shift will need too): `(load_kernel, admit, apply, warn, disposition_key)`
  where `admit(column, input_type, resolved_config) -> reason|None` is CONFIG-AWARE (not
  type-only - plan-gate HIGH-3), `apply(bound, pinned_array, params) -> result-with-errors`
  surfaces the kernel's error channel (plan-gate HIGH-2), and `warn` is the handler-side
  residual-warning pass (plan-gate MED). hash's descriptor: `admit` = hash `decline_reason`
  (config-trivial), `apply` = `native_hash_array` (no error channel), `warn` = none. `_decide`
  and the scalar seam look the descriptor up by `seed.strategy`; absent => Python (today's
  behavior, fail-safe). Each strategy's kernel state is ISOLATED (per-strategy loader, not the
  shared hash `_loaded`/`_bound`).

## Design (strategy-keyed scalar seam, SEQ)

1. **Native-op descriptor table.** A per-strategy descriptor (frozen mapping in
   `_sequential_native.py`, or a sibling `_sequential_native_ops.py` if the module nears its cap)
   with the config-aware + error-channel + warning contract above. hash's entry is trivial
   (type-only admit, no error channel, no warnings) and MUST reproduce today's tokens/paths/
   disposition exactly (regression-pinned). fpe's entry wraps the compiled fpe kernel loader, a
   CONFIG-AWARE admit that mirrors chunked admission (`_real_type_admission.py:148` rejects
   `checksum="luhn"` and other unsupported configs BEFORE source type; admits exactly
   `pa.string()`), an `apply` that returns the `FpeBatchResult`, and the handler's residual-warning
   pass. The fpe params the kernel needs flow through `FpeParams`/`FpeConfig`
   (`_operator_params.py:148`): mask_key, namespace, target-column tweak identity, charset,
   separator flag, validate_luhn, checksum, join group, thread budget (radix/preserved-width
   derive from charset + input, not independent settings).
2. **`prepare_table` generalization + key boundary.** Replace the `seed.strategy != "hash"`
   filter (`_sequential_native.py:202`) with "look up `seed.strategy` in the descriptor table;
   skip if absent". A column gets a native execution decision from its descriptor's config-aware
   `admit` + kernel load ONLY when it is NOT an FK key (parent or child) UNLESS its strategy is
   hash (hash keeps its existing key/REMAP behavior). So an `fpe` column that is an FK key gets
   NO native decision and stays Python (plan-gate HIGH-1). The per-column decision stays
   run-scoped and pinned-type as today.
3. **Scalar seam generalization + fail-closed error channel.** `hash_column` becomes a
   strategy-agnostic `native_column(ctx, df, column, params)` that looks up the column's
   decision; if native, rebuilds the pinned array, calls the descriptor's `apply`, and - before
   returning any value - inspects the result's error channel: if the descriptor declares errors
   (fpe) and `result.errors` is non-empty, raise the strategy's fail-closed code
   (`fpe_fail_closed_error`, pinning the first-failing-row code; covers subminimum domain,
   out-of-charset, excessive length) so NO output publishes for the failed table, exactly as
   chunked execution does (`native/_chunk_masking.py:230`). Only on a clean result does it return
   the positional list. hash's path has no error channel and is unchanged.
4. **REMAP stays hash-only + non-hash guard.** `remap_scope`/`_remap_tokens` are UNTOUCHED and
   hash-specific. Because an fpe key column gets no native decision (item 2), an fpe parent's
   orphan REMAP already runs the Python handler - but add an explicit guard so a non-hash
   strategy entering `_in_remap` returns to Python BEFORE touching any hash decision/disposition
   state (plan-gate HIGH-1: no contamination of `sequential_hash` accounting).
5. **Handler wiring + preserved warnings.** `_strategies/_fpe.py` gains a `getattr(ctx,
   "seq_native")` block like `_hash.py:52-65`, BUT unlike hash it does NOT early-return on native
   tokens before the warning phase: the handler still runs its residual-warning pass
   (`fpe_residual_warnings` `native/_fpe_route.py:80`) on the ORIGINAL values at handler scope
   (incl `when` subsets) so `fpe_partial_plaintext_disclosure` / `fpe_join_group_active` still
   emit (plan-gate MED); the native tokens replace only the computed values. Fall through to the
   full Python handler when `native_column` returns None. No other handler changes.
6. **Disposition + per-strategy kernel state.** The disposition key is per-strategy (fpe under a
   `sequential_fpe` key or a unified `sequential_native` list tagged by strategy - pick one, pin
   it); hash's `sequential_hash` fragment is preserved byte-for-byte (surface-pin). Kernel state
   is isolated per strategy (fpe's loader separate from hash's `_loaded`/`_bound`), so a
   mixed hash+fpe table in either column order and an independent per-strategy loader failure
   behave correctly (plan-gate test ask).

## Pinned guarantee and non-goals

**Guarantee.** An admitted SEQ `fpe` NON-KEY payload column (config-aware + type admit) masks
byte-identically to the Python `fpe` handler (NFC-agnostic, no divergence); its per-cell errors
fail the run closed (`fpe_fail_closed_error`, no output for the failed table); its security
warnings (`fpe_partial_plaintext_disclosure`, `fpe_join_group_active`) still emit from the
preserved handler warning phase; a non-admitted config (e.g. checksum) / non-string type /
missing companion / fault declines-with-reason to Python or aborts; the computation runs on the
compiled `fpe` kernel (proven by the SEQ marking-witness). An fpe KEY column stays Python. hash
tokens, hash disposition, hash REMAP, and every D2a/D2b/full-frame/OOC/chunked behavior are
UNCHANGED (regression-pinned); kernel state is isolated per strategy. Strategies with no
descriptor run in Python as today.

**Non-goals.** NO native fpe REMAP (an fpe key column stays Python; REMAP stays hash-only). NO
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
- **fpe config-aware decline (plan-gate HIGH-3):** an fpe column with `checksum="luhn"` (and
  other unsupported configs) DECLINES to Python at admission BEFORE type check, matching chunked
  admission (`_real_type_admission.py:148`); never admitted-then-crashed. A `large_string` fpe
  column declines (fpe admits exactly `pa.string()`). Config-parity test: the native path carries
  mask_key/namespace/tweak/charset/separator/validate_luhn/checksum/join_group so ciphertext
  matches the Python handler exactly.
- **fpe row-error fail-closed (plan-gate HIGH-2):** encrypting a batch with an out-of-charset /
  subminimum-domain / excessive-length cell makes the fpe `apply` return an `FpeBatchResult` with
  errors; the seam RAISES `fpe_fail_closed_error` (pinning the first-failing-row code) and
  publishes NO output for that table - it does NOT emit nulls. Assert each error class + that the
  transactional sink commits nothing.
- **fpe key column stays Python (plan-gate HIGH-1):** an fpe column used as an FK PARENT key gets
  no native decision; its matched children, orphan REMAP minting, and chain behavior run the
  Python handler UNCHANGED, and the `sequential_hash` disposition is not contaminated by the
  fpe parent. Test matched + orphan + chain with an fpe parent key.
- **fpe security warnings preserved (plan-gate MED):** a partial-plaintext fpe value (e.g.
  `M100000`) still emits `fpe_partial_plaintext_disclosure`, and a join-group fpe column emits
  `fpe_join_group_active`, on the NATIVE path (warning phase runs on original values at handler
  scope incl `when` subsets). Assert warning contents, counts, and order match the Python path.
- **fpe decline (companion/fault):** companion-absent declines (REASON_UNAVAILABLE path); a kernel
  fault (as opposed to a per-cell error) aborts the run. Reason codes carry strategy + column +
  Arrow type only, no value.
- **NFC-agnostic (plan-gate confirmed):** an fpe value with out-of-charset / high codepoints is
  byte-IDENTICAL native vs Python (fpe does not normalize; both preserve verbatim). Assert
  byte-identical and record in SEMANTIC_DIFFERENCES.md that fpe does NOT carry hash's NFC
  divergence.
- **mixed hash + fpe + kernel-state isolation (plan-gate test ask):** a table with both a hash and
  an fpe native column, in BOTH column orders, masks correctly; an independent per-strategy loader
  failure (fpe companion absent but hash present, and vice versa) degrades only that strategy.
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
