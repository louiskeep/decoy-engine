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
- Resolve `wanted` once, identically to `iter_ner_spans` (`ner.py:149`).
- Guard each element with the SAME predicate `iter_ner_spans` uses
  (`isinstance(text, str) and text` -> otherwise `[]`, `ner.py:147`). Build the
  index list of elements that pass the guard and the parallel list of their
  texts. Elements that fail the guard get `[]` without ever reaching the
  pipeline (matches `ner.py:147` and avoids feeding empty/non-str docs into the
  pipe).
- Run `_pipeline(model).pipe(passing_texts, batch_size=batch_size, n_process=1)`
  and iterate the yielded `Doc`s in order (spaCy guarantees input order). For
  each `Doc`, reproduce the per-doc body of `iter_ner_spans` verbatim
  (`ner.py:151-156`): map `ent.label_` through `NER_ENTITY_MAP`, drop `None`/not
  in `wanted`, append `Span(detector_id, ent.start_char, ent.end_char, ent.text)`.
- Scatter the per-doc results back to their original indices; guarded-out
  indices keep `[]`.
- One `Doc` per input text only. Never concatenate cells (char offsets are
  per-cell; concatenation would corrupt them).
- Let any per-doc exception (e.g. `max_length` exceeded) propagate; spaCy raises
  it while consuming the generator, matching the single-cell raise contract.
  Because the guard already excluded empties, a raising cell is a real oversized
  cell, same as today.

`_NER_PIPE_BATCH_SIZE` default: a module constant (start at 256; it changes only
throughput, never output, and the differential gate proves that).

Keep `iter_ner_spans` as-is (or as a thin delegate to a shared per-doc extractor)
so every existing caller and test is unaffected.

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

## Acceptance tests (define behavior before build; no later contributor weakens)

1. **Differential span identity (the primary gate)** -- `@needs_ner`, in
   `tests/unit/storm/test_ner_spans.py`: for a corpus mixing person/location/no-
   entity/empty/whitespace/non-string/duplicate/oversized-adjacent texts,
   `iter_ner_spans_batch(corpus) == [iter_ner_spans(t) for t in corpus]`,
   element for element. Include an `entities=` filtered variant and an
   all-empty batch.
2. **Order + scatter** -- a batch where only some elements are guarded-out
   returns `[]` at exactly those indices and correct spans elsewhere.
3. **batch_size invariance** -- the same corpus under batch_size in {1, 3, 256}
   yields identical results (proves batching never changes spans).
4. **max_length raise parity** -- a cell exceeding the model max raises the same
   error class from the batch helper as from `iter_ner_spans` (do not truncate).
5. **Caller byte-parity, unchanged** -- the existing suites must stay green
   after the switch: `test_text_mask_ner.py`, `test_text_redact.py`,
   `test_text_mask_chunked.py` (cross-chunk byte-identity, sizes [1,7,500]),
   and `tests/parity/test_out_of_core_group_b_parity.py` /
   `_group_c_parity.py` (OOC-vs-oracle byte parity). No assertion is relaxed.
6. **Arg-forwarding** -- `test_text_mask_ner.py::TestArgForwarding` equivalent:
   the batch helper receives the same `model`/`entities` the per-cell call did.
7. **spaCy-absent skip** -- inference tests gate on `needs_ner`; the pure
   collect/scatter logic (guard, order) is covered by a spaCy-free unit test
   that monkeypatches the pipeline, so coverage holds where spaCy is absent.

## Steps

1. Add `iter_ner_spans_batch` + `_NER_PIPE_BATCH_SIZE`; factor the per-doc span
   extraction so single and batch share one code path (no drift between them).
2. Add the differential + batch-size-invariance + scatter + max_length tests.
3. Switch the four callers to collect-batch-zip-back; keep guards/coercion/order.
4. Run the caller byte-parity suites; confirm zero diffs.
5. Verify: ruff + format + mypy; mutation grade on the new helper's logic
   (the guard/scatter/filter branches) on the changed unit.

## Risks

- Batch composition shifting spans (mitigated: per-element guard, one doc per
  text, differential + batch-size-invariance gates).
- `nlp.pipe` ordering (spaCy guarantees input order; the gate proves it).
- Version drift: unchanged; the existing stamp/guard runs before the helper.
- Empty/whitespace: guarded per element, never enters the pipe.
