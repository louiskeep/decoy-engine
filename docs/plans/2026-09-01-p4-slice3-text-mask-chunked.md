---
Status: plan
---

# Phase 4 Slice 3: text_mask on the chunked route

Part of Part 2 Phase 4 (docs/plans/2026-08-31-part2-phase4-plan.md). Third small chunked-route slice
after windowed_date (slice 1) and group_key (slice 2). Admits the `text_mask` masking strategy onto
the bounded pandas chunked route so a large single-table text_mask job streams in O(chunk) memory
instead of running full-frame. Byte-identical to the pinned pandas oracle, or it stays on the oracle
(Cam's hard rule: each strategy exactly as its current Python code).

## 1. Why this slice, what it changes

`text_mask` (`_strategies/_text_mask.TextMaskHandler.run` -> `transforms/text_mask.mask_cell`) detects
PII spans in each text cell and masks each matched span with a per-detector keyed sub-strategy. The
REAL span dispatch branches are `fpe`, `faker`, `date_shift`, `passthrough`, `redact`, and an
unknown-detector-strategy `redact` FALLBACK (`replace_with_token` is NOT a span sub-strategy -- it is
an `unmatched_span_policy` for non-PII text). Span keys are `HMAC(mask_key, matched_text)`, so each
span's mask is keyed on `ctx.mask_key` and the span's own value. It is
per-cell / per-span deterministic: no whole-column state, no cross-row dependency. Every cell's output
is a pure function of (that cell's text, config, mask_key), so for any chunking of the rows the
concatenated output equals the full-frame output. It is exactly the value-keyed contract the chunked
route already guarantees for `text_redact` (which is in `CHUNK_SAFE_STRATEGIES` and has the same
shape), except text_mask carries a richer per-detector dispatch.

The design table (part2 plan, line 159) classifies text_mask as `OOC / PORT` ("streams in OOC today;
native port"). This slice takes the same pragmatic path slices 1-2 took: the pandas CHUNKED route
(not the native Rust route), because that route already exists, already bounds memory, and runs the
real `mask_cell` per chunk, giving byte-parity by construction. The native-route port (P4-C) is a
later, separate slice.

What this slice changes (additive; no existing admitted strategy changes behaviour):

1. Add `"text_mask"` to `CHUNK_SAFE_STRATEGIES` (`_chunked_fk.py`). This admits it on both the manual
   entry and the auto-chunk planner (which reuses `check_chunked_compatibility`), AND makes it
   eligible as an FK-self-mask key (correct: it is value-own-keyed, see Trap A). One line.

The contract it must preserve, unchanged:

- Byte-identity to the pinned pandas oracle (`run_pipeline` full-frame) for values, order, nulls, and
  warnings, on the real `run_pipeline(auto_chunk=True)` route (mode == "chunked").
- FK referential integrity when a text_mask column is an FK-self-mask key.
- Cross-substrate value parity (polars adapter), per tests/parity/SEMANTIC_DIFFERENCES.md.

## 1a. The correctness traps (each a gate item; verify by reproduction, not assertion)

- **Trap A - FK self-mask (RI).** Adding text_mask to `CHUNK_SAFE_STRATEGIES` makes it an eligible
  FK-self-mask key (`_chunked_fk.gate_fk_child_edges` uses that set). Unlike group_key (slice 2,
  sibling-keyed, correctly EXCLUDED), text_mask IS value-keyed on the column's OWN value, so parent
  and child compute identical masked bytes from the same raw key -> RI is preserved, and text_mask
  BELONGS in CHUNK_SAFE. text_mask is NOT in `NAMESPACE_REQUIRING_STRATEGIES` ({hash, fpe, date_shift})
  because it keys on `ctx.mask_key` globally, not a per-column namespace. The FK gate admits a
  self-mask on conditions (b) same top-level strategy, (e) identical provider_config, and (f)
  compatible declared dtype family; the namespace sub-check applies only to
  `NAMESPACE_REQUIRING_STRATEGIES` (hash/fpe/date_shift), so it is correctly SKIPPED for a text_mask
  edge. Confirm a text_mask FK-self-mask (child declares text_mask + identical config) is admitted and
  produces matching parent/child keys ACROSS chunk boundaries, for EVERY real span branch (Trap A
  matrix in the tests). The construction + cross-table equality of `ctx.mask_key` is proven by the
  real parent/child route tests, not assumed. Verify: an FK job keyed on a text_mask column, chunked
  vs full-frame, has identical child->parent linkage.

- **Trap B - output dtype chunk-invariance.** text_mask writes an object column (per-cell masked
  strings), and its output dtype is a function of CONFIG (which sub-strategies + unmatched_span_policy
  run), fixed across chunks, NOT of chunk content -- so it is chunk-invariant, exactly like
  `text_redact` (already CHUNK_SAFE). Confirm there is no content-dependent dtype fallthrough (the
  bucketize hazard): null cells are skipped and preserved (`_text_mask` mirrors `_text_redact`'s null
  handling), non-null cells are stringified. Verify `concat_masked_chunks` never raises a schema
  mismatch across chunkings, including an all-null chunk vs a masked chunk.

- **Trap C - per-span date format is per-cell (chunk-safe), unlike standalone date_shift.** A
  date-typed span is shifted by `_shift_date_span`, which detects the format from the SPAN'S OWN text
  (`transforms/text_mask._detect_date_format(matched_text)`), not from whole-column samples, and
  preserves it; an unparseable span keeps its original text (no error). So text_mask does NOT carry
  standalone date_shift's whole-column-format hazard and needs NO `date_format`-style planner gate.
  Verify: a date-span-bearing column chunked at a boundary that splits same-format and mixed-format
  cells is byte-identical to full-frame.

- **Trap D - NER is off by default; version-mismatch is fail-closed (no chunk variance).** `ner:
  true/{...}` is opt-in; the `ner_model_version_mismatch` guard RAISES (identically on both routes ->
  a job error, never a divergent output) ONLY when the installed model version DIFFERS from the
  stamped `plan.ner_model_version`; when `installed_model_version(...) is None` the guard does NOT
  fire. NER span detection is per-cell (same complete cell on every route), so even with NER on the
  output is chunk-invariant. NER off (default) -> no NER path. Verify a default (no-ner) job chunks
  byte-identically; a stamped-version-mismatch job raises on both routes. Do NOT require a real spaCy
  model in CI; gate/skip the real-NER path as the text_mask handler tests already do.

- **Trap E - `when` predicate: REJECT (fail-closed).** The string-only-source premise is FALSE: the
  handler `str(value)`-converts every non-null non-string cell (`_text_mask.py:124`), so a text_mask
  source CAN be numeric/mixed. A when-gated text_mask leaves non-matching rows at their ORIGINAL
  (possibly numeric) value while matching rows become masked object/strings -> a chunk-of-all-non-
  matching keeps the source dtype while a chunk with matches becomes object -> chunk-boundary-dependent
  output dtype (`concat_masked_chunks` mismatch), exactly the group_key+when hazard. So this slice
  REJECTS `group_key`-style: add `reject_text_mask_when` (mirroring `_chunked_dgrn.reject_windowed_
  date_when`) raising `chunked_text_mask_when_not_supported` at the manual `run_mask_pipeline_chunked`
  entry; the auto planner already blanket-rejects `when`. Do NOT try to prove a narrow safe case -- the
  fail-closed reject (job runs full-frame on the oracle) is the correct default here, and proving byte
  + dtype uniformity across predicate-true/false x numeric/string/null/mixed chunks is not worth the
  risk for a rare shape.

## 2. Tasks (ordered; each keeps the tree green)

1. **Admit text_mask.** Add `"text_mask"` to `CHUNK_SAFE_STRATEGIES` in `_chunked_fk.py`, with a
   one-line comment (value-own-keyed, per-cell deterministic; NOT namespace-requiring). `_chunked_fk.py`
   is allowlisted at 651; a one-line addition stays under its ceiling (confirm).

2. **Reject `text_mask` + `when` (Trap E).** Add `reject_text_mask_when(table_cfg, table)` mirroring
   `_chunked_dgrn.reject_windowed_date_when`, raising
   `PlanCompileError(code="chunked_text_mask_when_not_supported", ...)` when any `text_mask` column
   carries a `when` predicate, called from `check_chunked_compatibility` (the auto planner already
   blanket-rejects `when`). Put it wherever keeps `_chunked.py` at/under its 663 ceiling (a small
   `_chunked_text_mask.py` sibling, or beside `reject_windowed_date_when` in `_chunked_dgrn.py` if that
   file has room -- builder's call, documented). Fail-closed -> a `when`-bearing text_mask job runs
   full-frame on the oracle.

## 3. Tests (the parity + mutation bar)

Parity asserted on the REAL `run_pipeline(auto_chunk=True)` route with `result.mode == "chunked"`
(never a hand-rolled chunk loop), against the same config run full-frame. New file
`tests/unit/execution/test_text_mask_chunked.py`:

Detector note: built-in span detectors do NOT emit the date/faker-routed IDs without a real spaCy
model (those are Tier 2, supplied-span detectors). So route-level tests that must reach the
`date_shift`/`faker` span branches inject a deterministic detector (a stub `iter_spans` / a supplied
span set via the handler's config) so the branch runs WITHOUT a real spaCy dependency; a plain
`mask_cell` unit test alone does not prove chunk-route parity. Keep any real-spaCy path gated/skipped
(the handler tests' precedent).

1. **Byte-identity across chunkings, REAL branches.** A text column whose spans route through each
   REAL branch -- `fpe`, `faker`, `date_shift`, `passthrough`, `redact`, and an unknown-detector-
   strategy (redact FALLBACK) -- plus each `unmatched_span_policy` (redact / passthrough /
   replace_with_token), at chunk sizes 1 / 7 / 500 vs full-frame: identical output + dtype. ASSERT the
   BRANCH SEMANTICS (e.g. a date span is date-shifted, an fpe span is format-preserved), not only
   full-vs-chunked equality -- otherwise an accidental redact-fallback would pass on both routes.
2. **Date span at a chunk boundary (Trap C).** Same-format and mixed-format date spans (via the
   injected detector) split across chunks: byte-identical, proving per-span (not whole-column) format.
3. **FK self-mask RI, per branch (Trap A).** For EACH branch a text_mask FK key can route to
   (`fpe`/`date_shift`/`passthrough`/`redact` on a keyed span), a single-column FK edge keyed on a
   text_mask column (child declares text_mask + IDENTICAL provider_config + matching declared dtype,
   orphan_policy remap): chunked == full-frame AND the child's masked key EQUALS the parent's for the
   same raw value, with EXACT output-byte assertions, across chunk boundaries. The text_mask FK edge
   is ADMITTED (contrast group_key, which is rejected).
4. **FK negative + namespace independence (Trap A).** (a) A text_mask FK edge whose child config
   DIFFERS from the parent is REJECTED (`chunked_fk_child_strategy_mismatch` / config mismatch code).
   (b) Two text_mask columns with ABSENT or DIFFERING column namespaces produce IDENTICAL output for
   the same input (text_mask keys on `ctx.mask_key`, not the namespace) -- proving it correctly stays
   out of `NAMESPACE_REQUIRING_STRATEGIES`.
5. **Output dtype invariance + null breadth (Trap B).** All-null chunk vs masked chunk;
   unmatched_span_policy = redact / passthrough / replace_with_token; null shapes = `None` / `NaN` /
   `pd.NA` / empty string / extension-array input: `concat_masked_chunks` raises no schema mismatch;
   the output is an object column on every chunk.
6. **NER default + version-mismatch (Trap D).** A DEFAULT (no-ner) job chunks byte-identically. A job
   with `ner` on AND a STAMPED plan version that DIFFERS from the installed version raises
   `ner_model_version_mismatch` on both routes; when `installed_model_version(...) is None` the guard
   does NOT fire (narrow the assertion accordingly). Keep the real-spaCy path gated/skipped.
7. **`when` REJECTED (Trap E).** A `when`-bearing text_mask column: the manual
   `run_mask_pipeline_chunked` raises `chunked_text_mask_when_not_supported`; the auto route rejects it
   via the existing planner `when` gate (mode != chunked / oracle fallback). Direct unit test of
   `reject_text_mask_when` (raises on a text_mask+when column, returns on a text_mask-without-when
   column).
8. **Admission surfaces, explicit.** Separate tests: (a) the MANUAL chunked entry admits a text_mask
   job (`result.mode == "chunked"`); (b) `run_pipeline(auto_chunk=True)` routes a text_mask job to
   `chunked` (no other planner gate silently rejects it); (c) cross-substrate polars traverses this
   admission surface and is value-equal to the pandas oracle (Arrow schema differences allowed).
9. **Warnings vs logging.** The handler ALWAYS returns `[]` QualityWarnings; assert that. The
   unmatched-passthrough LOG (once per processed non-empty cell) is captured via `caplog`, kept
   separate from the (empty) warnings contract, and confirmed identical between chunked and full-frame.

Mutation bar: the NEW production logic is the CHUNK_SAFE membership + `reject_text_mask_when`.
Full-grade `reject_text_mask_when` to zero unadjudicated survivors (direct raise/return tests). For
the one-line CHUNK_SAFE addition, the FK-admission + parity tests are the grading surface (a mutant
removing text_mask from the set reddens the admission + FK tests). Follow slice 2's adjudication
discipline for message-string / equivalent survivors, and the documented mutmut fast-selection
approach (direct unit tests over pandas-pipeline tests to avoid the false-timeout).

## 4. Acceptance

- BYTE-IDENTICAL output + warnings to the pinned pandas oracle on the real chunked route, across
  chunkings, per-detector sub-strategies, date spans, FK-self-mask, and null shapes. Exact, or
  text_mask stays on the oracle.
- FK RI preserved when a text_mask column is an FK-self-mask key; the text_mask FK edge is admitted.
- The intended chunked route provably ran (`result.mode == "chunked"`); oracle completion or silent
  fallback on an admitted job is a gate failure.
- ruff + format + mypy (CI py3.10) clean; module-size sentry green (no ceiling raised without a
  documented justification -- a one-line CHUNK_SAFE addition should not raise any ceiling).
- dennis + Codex per-slice gate green.

## 5. Risks (explicit review gates)

- **Trap E (`when` + non-string source)** is the one genuinely open question; resolve by reproduction
  at build, not assumption (the slice-2 lesson: pin every dtype/passthrough condition).
- **The per-detector dispatch is richer than text_redact**; the FK-self-mask parity (Trap A) must be
  verified for EACH REAL branch a text_mask span can invoke (fpe / faker / date_shift / passthrough /
  redact / unknown-detector redact fallback), because an FK key that routes through date_shift or fpe
  spans must still match parent/child across chunks. `faker`-routed spans are non-deterministic unless
  seeded from the span key -- verify a faker span used as an FK key still matches parent/child.
- **NER**: keep the real-spaCy path gated/skipped in CI (handler tests' precedent); the version-
  mismatch raise is the only NER behaviour this slice asserts.

## 6. Sequencing

Slice 3 of Phase 4, stacked on feat/native-phase3 (slices 1 + 2), HELD -- Phase 4 merges ONCE at the
end after all testing (Cam, 2026-08-31). A sequencing question is open with Cam: whether to keep
adding small chunked-route ports (this slice, then code_set / bucket_perturb with their config
conditions) or pivot to the design's native-route ports (P4-C, which needs the `_dispatch.py`
decomposition first) or the PREPASS slices (P4-D: top_code percentile, derived_aggregate). The BIG
slices (FK streaming P4-A, OOC-B external sorter, grouped_series P4-E) stay deferred for Cam.
