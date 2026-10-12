# Phase D / D2c-ii: wire `categorical` into the SEQ-native framework + generalize the disposition

Status: plan (rev 2, GATE GO - ready to build once D2c-i main CI is green; round-2 Codex plan-gate GO,
all 4 rev-1 findings resolved). Second slice of D2c's ENGINE half. Builds on D2c-i (engine
#243, the `_sequential_native_ops` descriptor framework + `fpe`). Branches off origin/main (b52fe688,
includes D2c-i) on feat/phase-d-d2c-ii-seq-index-kernels (worktree phase-d-d2c-ii).

rev-2 RESCOPE after the rev-1 Codex plan-gate NO-GO (2 HIGH + 2 MED; descriptor DIRECTION endorsed,
FK boundary confirmed sufficient, NFC confirmed). rev 1 bundled `categorical` + `bucket_perturb`; the
gate showed bucket_perturb carries date-operator obligations the raw-kernel call would skip - its
date-range guard lives in a WRAPPER (`out_of_core/_bucket_native.py:218`), not `native_bucket_perturb`
(HIGH-1), and D1c's `preserve_values` sentinel-preserve + preserved-row evidence must be honored
(HIGH-2). Those belong with `date_shift` (which shares `preserve_values`), so rev 2 narrows this slice
to `categorical` ONLY and moves bucket_perturb + date_shift to D2c-iii (date operators). rev 2 also
folds the two framework MEDs that any second-descriptor-beyond-fpe hits: `metrics()` is hardcoded to
hash+fpe and must be generalized to emit per-descriptor disposition (MED-1), and the no-partial-output
guarantee must be sink-qualified (MED-2).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md, architecture.md,
reuse-first.md, testing.md, documentation.md, autonomous-operation.md. Engine CLAUDE.md,
compatibility-contract.md.

Risk: R2. Moves categorical payload token computation onto the compiled index kernel on the SEQ route,
with byte-parity + the accepted NFC divergence, and generalizes the SEQ-native disposition. Plan +
independent review + Codex plan gate + final gate before merge.

## FRAME

**Problem.** D2c-i wired `fpe` through the SEQ-native descriptor framework but left `metrics()`
hardcoded to the hash + fpe fragments. `categorical` already runs native on the OOC/chunked routes
(D1b-ii #238) via the compiled INDEX kernel but still runs in Python on the bounded sequential (SEQ)
FK route. Register it, AND generalize the disposition so a descriptor-driven operator actually reports
its backend (the framework gap every operator past fpe hits).

**Why categorical alone.** It is the clean index-kernel operator: payload-only, deterministic keyed
index from a prebuilt CDF, the kernel RAISES on a malformed index (no per-cell error channel, so
`fail_closed=None`), no residual-warning phase (`warn=None`), no `preserve_values` or date-range guard
(those are date-operator concerns). It carries the accepted NFC divergence (the index derives from the
NFC-normalized source value), like hash. So categorical is a near-pure descriptor registration, which
makes it the right operator to carry the one genuine framework change this slice needs - the
`metrics()` generalization - with no operator-specific complexity confounding it. `bucket_perturb` and
`date_shift` (D2c-iii) both need the date-range guard and/or the D1c `preserve_values` pass, so they
are a separate, cohesive date-operator slice.

**Definition of done.** On the SEQ route, a `categorical` NON-KEY payload column whose resolved
configuration (deterministic + namespaced + string-category config, carrying the resolved categories
AND CDF - profile-derived weights, with an unbuildable-CDF decline per `out_of_core/_categorical_native.py:57`)
AND Arrow input type (exactly `pa.string()`) its admission predicate accepts computes its tokens on the
SAME compiled index kernel the chunked route uses, byte-identical to the Python handler except the
accepted NFC divergence. A non-admitted config/type, a missing companion, or a kernel fault
declines-with-reason to Python or aborts. A `categorical` FK key column stays Python (framework
non-hash-key boundary). `SeqNativeRun.metrics()` now emits a `sequential_categorical` fragment (and is
generalized so future descriptors emit theirs) while the `sequential_hash` + `sequential_fpe` fragments
stay byte-identical. hash + fpe behavior and all D2c-i/D2a/D2b parity are UNCHANGED.

**Boundaries / this slice.** SEQ route only. Register `categorical` ONLY + generalize `metrics()`. NO
`bucket_perturb`/`date_shift` (D2c-iii), NO text ops (D2c-iv), NO pure-Arrow trio, NO faker/group_key/
etc. NO REMAP generalization (payload-only; REMAP stays hash-only). NO platform activation. Full-frame,
OOC, chunked UNCHANGED.

## Established solutions

- D2c-i's descriptor framework is reused: `categorical` registers a `NativeOp` (`load_kernel` =
  categorical index-kernel loader; `admit(column, input_type, resolved_config)` = the EXISTING
  OOC/chunked categorical admission via a thin schema adapter, carrying resolved categories + CDF;
  `apply` = call `native_categorical` (deterministic only; the positional kernel is not used because
  non-deterministic configs decline), return `NativeResult(values, errors=())`; `fail_closed=None`;
  `warn=None`; `disposition_key="sequential_categorical"`).
- The compiled kernel + admission exist and are proven on OOC/chunked (D1b-ii; `native/_categorical_ext.py`,
  `out_of_core/_categorical_native.py`). REUSE them; do not re-implement (established-methodology).
- The `metrics()` generalization is the one framework change: today `SeqNativeRun.metrics()`
  (`_sequential_native.py:405`) emits hash + fpe explicitly; generalize it to iterate the registered
  descriptors' `disposition_key`s and emit each operator's recorded entries, PRESERVING hash's
  scalar/REMAP/inherited assembly and the exact existing hash + fpe fragments (pin them).

## Design

1. **categorical descriptor.** Add a `categorical_op` factory to `_sequential_native_ops.py` returning
   the `NativeOp` above. The admit is a thin adapter over the existing OOC/chunked categorical
   admission (construct a one-field schema from `(column, input_type)` and reuse the OOC predicate,
   `out_of_core/_categorical_native.py:57`), taking the resolved seed config and declining (coded) a
   non-deterministic / non-namespaced / non-string-category / non-string-source / unbuildable-CDF
   column (deterministic subset only - a non-deterministic config DECLINES, so the positional kernel
   path is not used; only `native_categorical`). NOTE `NativeOp.admit` returns `str | None` (a reason),
   so it does NOT carry the categories/CDF out; the resolved categories + CDF + namespace + mask_key
   reach `apply` through the PREPARED STATE / call payload (`_sequential_native_ops.py:68`), not admit's
   return. `from_profile` is already resolved to categories + weights before execution
   (`_strategies/_categorical.py:23`). `apply` rebuilds the pinned `pa.string()` array and calls
   `native_categorical`; a malformed index raises before assignment (`native/_categorical_ext.py:99`)
   and aborts (no error channel). Register in `_OPS`.
2. **Handler wiring.** `_strategies/_categorical.py` gains the `getattr(ctx,"seq_native")` block (as
   `_hash.py` has): assign native tokens on a non-None return, else fall through to the Python handler.
   categorical has no residual-warning phase (verify), so the simple early-return is correct.
3. **metrics() generalization (MED-1).** Generalize `SeqNativeRun.metrics()` (`_sequential_native.py:412`)
   to build the fragment set from the registered descriptors (each `disposition_key` -> its recorded
   entries): filter scalar decisions per descriptor, append REMAP + inherited entries ONLY for hash,
   preserve entry order, omit empty fragments. hash's REMAP + inherited assembly and the exact
   `sequential_hash` / `sequential_fpe` fragment contents MUST be byte-identical (surface-pin), proven
   by the pins PLUS the unchanged D2a/D2b/D2c-i suites + mixed-order tests (fragment pins ALONE do not
   prove token parity). Per-strategy loader isolation already exists (`_sequential_native.py:207`).
   Evidence must reflect ACTUAL kernel calls: empty SEQ input already skips application
   (`_sequential_native.py:333`) and records a no-call entry; but an ALL-NULL column does NOT auto-skip
   `native_categorical` (its raw helper calls the kernel unconditionally, `native/_categorical_ext.py:71`;
   only the OOC path shortcuts it, `out_of_core/_categorical_native.py:211`). So either mirror the OOC
   all-null shortcut on SEQ (then record a no-call entry) OR let all-null call the kernel and record it
   as a native call - pick the one that keeps parity simplest, and make the evidence + test assert what
   ACTUALLY happens, not an assumed skip.
4. **Key boundary (reuse, confirm) + sink-qualified guarantee (MED-2).** categorical is payload-only,
   so a categorical FK key stays Python via the framework boundary (test it). The no-partial-output
   guarantee is SINK-QUALIFIED: a kernel fault aborts before the table is published; a transactional
   sink commits nothing, a callable sink retains tables already delivered before the failing one
   (existing `_sequential.py:14` semantics, unchanged). State this precisely; test a later-table fault.

## Pinned guarantee and non-goals

**Guarantee.** An admitted SEQ `categorical` NON-KEY payload column masks byte-identically to the
Python handler except the accepted NFC divergence; a non-admitted config/type / missing companion /
kernel fault declines-with-reason or aborts; the computation runs on the compiled index kernel (proven
by the SEQ marking-witness). A categorical FK key stays Python. `metrics()` emits `sequential_categorical`
and is descriptor-generalized, with hash + fpe fragments byte-identical. On a fault, a transactional
sink publishes nothing for the failed/later tables; a callable sink keeps tables already delivered. hash
+ fpe + every D2c-i/D2a/D2b/full-frame/OOC/chunked behavior UNCHANGED.

**Non-goals.** NO bucket_perturb / date_shift (D2c-iii: date-range guard + `preserve_values`). NO text
ops, pure-Arrow trio, faker/group_key/etc. NO REMAP generalization. NO new kernel/admission impl. NO
platform activation. NO change to `resolve_fk_keys`, routing, or full-frame/OOC/chunked.

## Acceptance tests (authored before impl)

- **hash + fpe regression (byte-identical):** full D2a/D2b/D2c-i SEQ suites pass unchanged; the
  `sequential_hash` AND `sequential_fpe` disposition fragments are byte-identical through the metrics()
  generalization (surface-pin); a mixed hash+fpe+categorical table in varied column orders masks all
  correctly.
- **categorical parity:** a SEQ job with a `categorical` payload column (payload + across an FK edge
  where it is NOT a key) masks byte-identical to the Python handler on strict dimensions
  (`_strict_compare.py`), for every admitted config (incl profile-weighted CDF); oracle = the Python
  handler. Real compiled-kernel parity suite (abi-3), as D2c-i did.
- **categorical native witness:** a SEQ marking stand-in proves the tokens came from the kernel.
- **config-aware decline (coded, no value):** non-deterministic / non-namespaced / non-string-category
  / non-string-source / unbuildable-CDF declines to Python; companion-absent declines; a kernel fault
  aborts. Reason codes carry strategy + column + Arrow type only.
- **categorical key stays Python:** a categorical FK key gets no native decision; matched/orphan runs
  Python; no `sequential_hash`/`sequential_fpe`/`sequential_categorical` cross-contamination.
- **NFC envelope:** a category source value with a post-running-Python-Unicode-version codepoint carries
  the same accepted native-vs-Python divergence as hash (documented, version-guarded).
- **metrics() generalization (MED-1):** `sequential_categorical` appears with the right entries; a
  categorical column that records a decision but makes NO kernel call (empty/all-null selection) still
  produces an accurate evidence entry; `sequential_hash`/`sequential_fpe` fragments byte-identical;
  hash REMAP/inherited assembly unchanged.
- **sink-qualified no-partial-output (MED-2):** a categorical kernel fault in a LATER table leaves a
  transactional sink with nothing committed; a callable sink retains the earlier delivered tables
  (existing semantics). Assert both.
- **descriptor-absent = Python:** an un-wired strategy (bucket_perturb, date_shift, text_mask) still
  runs in Python on SEQ unchanged.
- **perf gate:** a sized SEQ categorical workload runs native faster than the Python handler within the
  memory ceiling, byte-identical digest (mirror the D2a/D2c-i perf tests).
- **module caps + boundaries:** `_sequential_native_ops.py` + `_sequential_native.py` under caps
  (census if >600); `tests/sentry/*` pass; run the FULL sentry/surface-pin tier locally.

## Known context for the gate

- The `metrics()` generalization is the one real framework change (rev-1 called the slice "additive";
  the gate corrected that - MED-1). Keep it minimal: iterate descriptors, pin hash + fpe fragments
  byte-identical, accurate no-kernel-call evidence.
- Reuse the EXISTING categorical admission + kernel (D1b-ii). The admit must carry resolved categories
  AND the CDF (profile-derived weights), declining an unbuildable CDF (`out_of_core/_categorical_native.py:57`).
- categorical carries the accepted NFC divergence (assert divergence, not byte-identity, on post-version
  codepoints).
- The no-partial-output guarantee is sink-qualified (transactional aborts; callable keeps delivered) -
  do not overstate it (MED-2).
- Test env: companion/native tests need the abi-3 venv; confirm the import path resolves to THIS
  worktree first; one heavy test process at a time.

## Docs to update

`docs/compatibility-contract.md` (SEQ `categorical` native + the generalized disposition),
`docs/strategies.md` (categorical compute-path note), `tests/parity/SEMANTIC_DIFFERENCES.md`, CODEMAP,
and the platform ROADMAP D row (D2c-ii done, NEXT = D2c-iii bucket_perturb + date_shift) at DOCUMENT.
