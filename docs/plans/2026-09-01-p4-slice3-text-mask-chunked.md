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
PII spans in each text cell and masks each span with a per-detector keyed sub-strategy (hash / fpe /
date_shift / redact / replace_with_token), keyed on `ctx.mask_key` and the span's own value. It is
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
  because it keys on `ctx.mask_key` globally, not a per-column namespace; confirm the FK gate's
  condition (c) (namespace match) is therefore correctly SKIPPED for a text_mask FK edge, and that a
  text_mask FK-self-mask (child declares text_mask + same config) is admitted and produces matching
  parent/child keys ACROSS chunk boundaries. Verify: an FK job keyed on a text_mask column, chunked
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

- **Trap D - NER is fail-closed and off by default (no chunk variance).** `ner: true/{...}` is opt-in;
  the `ner_model_version_mismatch` guard RAISES (identically on both routes -> a job error, never a
  divergent output), and NER span detection is per-cell. NER off (default) -> no NER path. Verify a
  default (no-ner) text_mask job chunks byte-identically; a version-mismatch job raises on both
  routes. (Do NOT require a real spaCy model in CI; gate/skip the real-NER path as the text_mask
  handler tests already do.)

- **Trap E - `when` predicate.** text_mask masks its OWN (text) column; a when-gated text_mask leaves
  non-matching rows at their original value. If the source column is a string/text column (text_mask's
  domain), non-matching rows stay string and matching rows become masked strings -> no chunk-boundary
  dtype mix (contrast group_key, whose target could be numeric). VERIFY this: confirm text_mask's
  source is always string-typed (a compile check, or the handler's own coercion), so `when` is
  chunk-safe; if a non-string source is reachable, `when` + text_mask must be rejected the same way
  slice 2 rejected group_key + when (a dtype-mixing hazard). Resolve this at build with a reproduced
  test; do not assume. The auto route rejects ALL `when` already, so this only concerns the manual
  entry.

## 2. Tasks (ordered; each keeps the tree green)

1. **Admit text_mask.** Add `"text_mask"` to `CHUNK_SAFE_STRATEGIES` in `_chunked_fk.py`, with a
   one-line comment (value-own-keyed, per-cell deterministic; NOT namespace-requiring). `_chunked_fk.py`
   is allowlisted at 651; a one-line addition stays under its ceiling (confirm). No other source
   change is expected unless Trap E resolves to "reject when" (then add a small rejection mirroring
   `_chunked_dgrn.reject_windowed_date_when`, and its call in `check_chunked_compatibility`).

2. **Resolve Trap E at build.** Determine whether a text_mask source can be non-string (trace the
   handler's ingestion + any compile check). If it is always string, `when` is chunk-safe (no code);
   if not, add the `when` rejection (task 1 note). Record the finding in the build.

## 3. Tests (the parity + mutation bar)

Parity asserted on the REAL `run_pipeline(auto_chunk=True)` route with `result.mode == "chunked"`
(never a hand-rolled chunk loop), against the same config run full-frame. New file
`tests/unit/execution/test_text_mask_chunked.py`:

1. **Byte-identity across chunkings.** A text column with mixed PII spans (an email, an SSN, a date,
   a name) at chunk sizes 1 / 7 / 500 vs full-frame: identical output, warnings, dtype.
2. **Per-detector sub-strategies.** A config exercising hash / fpe / date_shift / redact /
   replace_with_token spans across a chunk boundary: byte-identical.
3. **Date span at a chunk boundary (Trap C).** Same-format and mixed-format date spans split across
   chunks: byte-identical (proves per-span, not whole-column, format).
4. **FK self-mask (Trap A).** A single-column FK edge keyed on a text_mask column (child declares
   text_mask + same config, orphan_policy remap): chunked == full-frame AND child->parent linkage is
   preserved (the same masked key on both sides), across chunk boundaries. Plus the RI regression: a
   text_mask FK edge is ADMITTED (not rejected), contrasting group_key.
5. **Output dtype invariance (Trap B).** All-null chunk vs masked chunk; unmatched_span_policy =
   redact / passthrough / replace_with_token: `concat_masked_chunks` raises no schema mismatch; dtype
   is chunk-invariant.
6. **NER off default + version-mismatch raises (Trap D).** A default job chunks byte-identically; a
   stamped-version-mismatch job raises the coded error on both routes (real spaCy gated/skipped).
7. **`when` (Trap E).** Per the build resolution: either chunked == full-frame for a when-gated
   text_mask (if string-source-safe), or the manual entry rejects it with a coded error.
8. **Cross-substrate (polars).** The same admitted jobs under the polars adapter: value-equal to the
   pandas oracle (Arrow schema differences allowed), and the intended chunked route ran.

Mutation bar: the only NEW production logic is the set membership (+ possibly a small `when`
rejection). Full-grade any new function to zero unadjudicated survivors; for the one-line CHUNK_SAFE
addition, the FK-admission + parity tests are the grading surface (a mutant removing text_mask from
the set reddens the admission tests). Follow slice 2's adjudication discipline for message-string /
equivalent survivors.

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
  verified for EACH sub-strategy a text_mask span can invoke (hash/fpe/date_shift/redact/token),
  because an FK key that routes through date_shift spans must still match parent/child across chunks.
- **NER**: keep the real-spaCy path gated/skipped in CI (handler tests' precedent); the version-
  mismatch raise is the only NER behaviour this slice asserts.

## 6. Sequencing

Slice 3 of Phase 4, stacked on feat/native-phase3 (slices 1 + 2), HELD -- Phase 4 merges ONCE at the
end after all testing (Cam, 2026-08-31). A sequencing question is open with Cam: whether to keep
adding small chunked-route ports (this slice, then code_set / bucket_perturb with their config
conditions) or pivot to the design's native-route ports (P4-C, which needs the `_dispatch.py`
decomposition first) or the PREPASS slices (P4-D: top_code percentile, derived_aggregate). The BIG
slices (FK streaming P4-A, OOC-B external sorter, grouped_series P4-E) stay deferred for Cam.
