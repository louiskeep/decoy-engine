# Phase 5 slice: batched NER inference helper (`iter_ner_spans_batch`)

Status: plan (Phase 5 exploration, held on `feat/phase5-hard-tail`; nothing merges)
Author: Opus, 2026-09-08.

## Frame

Phase 5 research (`docs/plans/2026-09-01-phase5-hard-tail-research.md`, section `text`/NER)
recommends this as the one "Build" slice and the lowest-risk Phase 5 item. Today
NER runs one cell at a time via `nlp(text)` (`storm/ner.py:152`) at four call
sites (oracle text_mask/text_redact, OOC group-b/group-c). spaCy's own docs say
`Language.pipe` is more efficient than per-call inference. This slice introduces
a shared batch helper around `nlp.pipe` and switches those four sites to it,
**without changing the observable span contract**. It is a performance refactor
plus the shared primitive a future native text handler (a separate slice) will
call.

Risk level: LOW. No new route, no model change, no config-surface change. The
whole slice is protected by a byte-identity differential gate: the per-cell span
list must be identical whether produced by `nlp(text)` or `nlp.pipe([...])`.

This is exploratory: the research doc gates activation on evidence of a large
non-FK NER workload. This slice builds the capability; activation/measurement
stays Cam's call. It does not touch the master efficiency plan's frozen bars.

## Non-goals

- No native non-FK text handler (that is the next slice; native still rejects
  text_mask/text_redact and falls back to oracle/OOC, unchanged here).
- No change to the model, the exclude list, the label->detector map, the entity
  filter, the version-stamp/guard, `mask_cell`, `iter_spans`, or `_splice`.
- No multiprocessing (`n_process=1` only). No cross-cell doc concatenation.
- No change to null/non-string/empty handling as observed by callers.

## Design

### 1. New helper in `storm/ner.py`

```
def iter_ner_spans_batch(
    texts: Sequence[object],
    *,
    model: str = DEFAULT_NER_MODEL,
    entities: Collection[str] | None = None,
    batch_size: int = _NER_PIPE_BATCH_SIZE,
) -> list[list[Span]]:
```

Contract: returns one `list[Span]` per input, in input order, each element
**exactly equal** to `iter_ner_spans(texts[i], model=model, entities=entities)`.

Implementation:
- `batch_size` MUST be a positive int. Reject `<= 0` with a `ValueError` at the
  top of the helper. Rationale (P0, security): `nlp.pipe([text], batch_size=0)`
  yields ZERO documents without raising, which a scatter implementation would
  turn into `[[]]` and silently drop NER-only spans, leaving PII unmasked.
- Resolve `wanted` once, identically to `iter_ner_spans` (`ner.py:149`).
- Guard each element with the SAME predicate `iter_ner_spans` uses:
  `not isinstance(text, str) or not text` -> `[]` (`ner.py:147`). ONLY
  non-strings and the empty string `""` are guarded out. Whitespace-only strings
  (`"   "`) are non-empty and MUST enter the pipe, exactly as `iter_ner_spans`
  sends them to the model today (a builder must not `strip()`; that would change
  the observable raise-on-unavailable-model and oversized-whitespace behavior).
  Build the index list of passing elements and the parallel list of their texts.
- Run `_pipeline(model).pipe(passing_texts, batch_size=batch_size, n_process=1)`.
  VERIFY CARDINALITY: the number of yielded `Doc`s MUST equal
  `len(passing_texts)`. Use `zip(..., strict=True)` or an explicit count assert;
  a mismatch (too few/many docs) MUST raise, never degrade to empty spans (P0
  fail-open guard). Iterate the yielded `Doc`s in input order (spaCy guarantees
  order for `n_process=1`). For each `Doc`, produce the span list with the same
  per-doc logic as `iter_ner_spans` (`ner.py:151-156`): map `ent.label_` through
  `NER_ENTITY_MAP`, drop `None`/not in `wanted`, append
  `Span(detector_id, ent.start_char, ent.end_char, ent.text)`.
- Scatter the per-doc results back to their original indices; guarded-out
  indices keep `[]`.
- One `Doc` per input text only. Never concatenate cells (char offsets are
  per-cell; concatenation would corrupt them).
- Let any per-doc exception (e.g. `max_length` exceeded) propagate; spaCy raises
  it while consuming the generator, matching the single-cell raise contract.
  Because the guard only excludes `""`/non-str, a raising cell is a real
  oversized cell (including oversized whitespace), same as today. No caller
  publishes a partial column on a raise, so the result stays all-or-nothing.

`_NER_PIPE_BATCH_SIZE` default: a module constant (start at 256; it changes only
throughput, never output, and the differential + batch-size-invariance gates
prove that).

**Keep `iter_ner_spans`'s single-cell body UNCHANGED** (do not refactor it into a
shared extractor). It is the independent oracle the differential gate compares
the batch helper against; sharing one extractor would make a mapping/offset/
filter regression common-mode (green differential while both are wrong, P1). The
batch helper reproduces the per-doc logic independently, and a characterization
golden test (below) pins the extraction to exact `Span` lists so the two cannot
silently co-drift.

### 2. Switch the four callers to collect-batch-zip-back

At each site, the null-skip and `str(value)` coercion stay exactly where they are.
Replace the per-cell `iter_ner_spans` call with: build the list of coerced texts
for the non-null cells (recording their positions), call `iter_ner_spans_batch`
once for the column/batch, then consume the returned per-cell span lists in the
existing loop by position. Null cells keep their existing null output. The
version guard stays before the batch call, unchanged.

- `execution/_strategies/_text_mask.py:124-143` (call at 133).
- `execution/_strategies/_text_redact.py:148-162` (call at 157).
- `execution/out_of_core/_mask_group_c.py:163-186` (call at 173).
- `execution/out_of_core/_mask_group_b.py:201-214` (call at 211).

`mask_cell` / `iter_spans` / `_splice` consume `extra_spans` and are untouched:
each cell still receives exactly the span list it would have gotten per-cell.

Coercion happens exactly once per cell: the caller coerces `str(value)` before
collecting, and the SAME coerced text is used both for the NER batch and for
`mask_cell`/`_splice`. Do not coerce a second time.

Memory disposition (P2): the OOC callers (group b/c) already operate one
record batch at a time, so their collect is bounded by `batch_rows`. The oracle
callers are full-frame by design (that route already holds the whole column), so
collecting the column's texts + span lists adds no new order of memory. To keep
peak RSS from rising materially above the per-cell loop on very wide columns, the
oracle callers collect/infer/apply in bounded microbatches of
`_NER_APPLY_WINDOW` rows (a multiple of the pipe batch size) rather than
materializing every span list for the whole column at once. A peak-memory
characterization test asserts the batched path is not materially worse than the
per-cell loop.

## Acceptance tests (define behavior before build; no later contributor weakens)

1. **Differential span identity (primary gate)** -- `@needs_ner`, in
   `tests/unit/storm/test_ner_spans.py`: for a corpus mixing person/location/no-
   entity/empty/whitespace-only/non-string/duplicate/oversized-adjacent texts,
   `iter_ner_spans_batch(corpus) == [iter_ner_spans(t) for t in corpus]`,
   element for element. Include an `entities=` filtered variant and an all-empty
   batch. Unicode cases REQUIRED (they exercise the char-offset contract): an
   astral emoji before an entity, NFC vs NFD combining characters, curly
   apostrophes, and non-ASCII locations.
2. **Characterization golden (P1, breaks common-mode)** -- a spaCy-free test with
   a FAKE pipeline yielding hand-built docs (labels PERSON/PER/GPE/LOC/FAC +
   unmapped ORG/DATE, known char offsets): assert the batch helper's exact
   `Span` lists -- entity order, label->detector mapping, unmapped-label
   exclusion, exact start/end/matched_text, detector-id filtering, `entities=[]`
   -> no spans, `entities=["GPE"]` (a spaCy label, not a detector id) -> no
   spans. Pins the extraction independently of `iter_ner_spans`.
3. **Whitespace enters the pipe (P0.1)** -- spy on `_pipeline(...).pipe`: `"   "`
   reaches `.pipe`; `""` and non-strings do NOT. Oversized whitespace raises the
   same error as a single-cell oversized-whitespace call.
4. **batch_size + cardinality guard (P0.2)** -- `batch_size` in {0, -1} raises
   `ValueError`. A fake pipe yielding FEWER docs than inputs raises (never
   returns `[]`); a fake pipe yielding more also raises. Never fail open.
5. **batch_size invariance** -- the same corpus under batch_size in {1, 3, 256}
   yields identical results.
6. **max_length raise parity** -- a cell exceeding the model max raises the same
   error class from the batch helper as from `iter_ner_spans`; no truncation; no
   partial column published.
7. **Scatter with mixed positions** -- inputs `[None, 42, "", <entity text>,
   pd.NA, <duplicate entity text>]` with distinct fake span results: `[]` at
   exactly the guarded-out indices, correct spans elsewhere, and coercion happens
   exactly once (the same coerced text reaches both NER and masking).
8. **Caller byte-parity, unchanged** -- existing suites stay green after the
   switch: `test_text_mask_ner.py`, `test_text_redact.py`,
   `test_text_mask_chunked.py` (cross-chunk byte-identity [1,7,500]), and
   `tests/parity/test_out_of_core_group_b_parity.py` / `_group_c_parity.py`
   (OOC-vs-oracle byte parity). Parameterize the Group B NER parity over small
   `batch_rows` as Group C already does. No assertion relaxed.
9. **Arg-forwarding** -- the batch helper receives the same `model`/`entities`
   the per-cell call did (mirror `test_text_mask_ner.py::TestArgForwarding`).
10. **Peak-memory characterization (P2)** -- the batched oracle path's peak RSS
    over a wide column is not materially worse than the per-cell loop (bounded
    microbatch window).
11. **spaCy-absent coverage** -- inference tests gate on `needs_ner`; the pure
    collect/scatter/guard/cardinality logic is covered by the fake-pipeline tests
    (2, 4, 7) so coverage holds where spaCy is absent.

## Steps

1. Add `iter_ner_spans_batch` + `_NER_PIPE_BATCH_SIZE` + `_NER_APPLY_WINDOW`.
   Leave `iter_ner_spans`'s single-cell body UNCHANGED (independent oracle). The
   batch helper: positive-batch-size check, per-element guard (`""`/non-str out,
   whitespace in), `.pipe` with strict cardinality, per-doc extraction, scatter.
2. Add the characterization golden (fake pipe) + differential identity (with
   Unicode) + batch-size invariance + whitespace-spy + batch_size/cardinality
   + scatter-mixed-positions + max_length tests.
3. Switch the four callers to collect-batch-zip-back (bounded microbatch on the
   oracle callers); keep null-skip, single coercion, order, version guard.
4. Run the caller byte-parity suites (add Group B `batch_rows` parametrization);
   confirm zero diffs. Add the peak-memory characterization.
5. Verify: ruff + format + mypy; mutation grade on the new helper's logic
   (positivity, guard, cardinality, scatter, filter branches).

## Risks

- Batch composition shifting spans (mitigated: per-element guard, one doc per
  text, differential + batch-size-invariance + Unicode gates).
- `nlp.pipe` ordering (spaCy guarantees input order for `n_process=1`; gated).
- `batch_size <= 0` yielding zero docs -> fail-open (mitigated: positivity check
  + strict cardinality guard that raises, never returns empty).
- Version drift: unchanged; the existing stamp/guard runs before the helper.
- Whitespace-only cells: NON-empty, so they enter the pipe exactly as today; only
  `""` and non-strings are guarded out.
- Peak memory on wide oracle columns (mitigated: bounded microbatch window +
  characterization test).
