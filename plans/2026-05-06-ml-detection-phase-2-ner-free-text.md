---
Status: planning
Branch: claude/ml-masked-data-evaluation-9FbpV
References: 2026-05-06-ml-detection-phase-1-column-classifier.md, ../decoy-platform/plans/2026-05-06-ml-sensitive-data-detection.md, ../decoy-platform/STORM_GUIDE.md
---

# Phase 2 — ML detection: cell-level NER for free-text columns

Adds Presidio + spaCy `en_core_web_lg` as stage 3 of the detector pipeline.
Only runs on columns the Phase 1 classifier flagged as `free_text`.

Depends on Phase 1.

## Scope

- Wire Presidio's `AnalyzerEngine` into `storm.ml` as a new stage.
- Run NER on a sampled set of cells per free-text column.
- Aggregate cell-level entity hits into column-level `DetectorMatch` records
  plus a per-column entity histogram for the platform UI.
- Reuse the Phase 1 lazy-pull machinery for the spaCy model and Presidio
  recognizers.

Out of scope: review queue UI (Phase 3), generic-language NER beyond English
(deferred), domain-specific entity types (Phase 5).

## Files

```
src/decoy_engine/storm/ml/
├── ner.py                                     ← NEW: PresidioNER (load → analyze_column)
├── artifacts.py                               ← MODIFIED: register spaCy + Presidio assets
└── pipeline.py                                ← MODIFIED: stage 3 = NER on free_text columns
src/decoy_engine/storm/types.py                ← MODIFIED: add InlineEntityHistogram (per-column)
tests/unit/storm/ml/
└── test_ner.py
tests/fixtures/ml_detection/synth/
└── mixed_text_pii.parquet                     ← already present from Phase 0; ground truth gets used here
```

## Reuse / do not reinvent

- `DetectorMatch` — NER stage emits these for column-level detections (e.g.
  "this column is a notes field that contains EMAIL_ADDRESS at 4% rate").
- `artifacts.py` lazy-pull — extend its asset list, don't fork it.
- Phase 0 harness — extends naturally to NER metrics by reading
  `inline_entities` from the manifest.
- Existing profiler sampling — NER reuses the sample the profiler already
  takes. Cap at 500 cells per free-text column (RFC §4); make this a config
  knob, not a hardcode.

## Aggregator behavior

- Stage 3 runs only when stage 2 produced `free_text` (or when the column is
  declared text and stage 2 was inconclusive).
- Per-cell NER hits roll up to:
  1. A `DetectorMatch` per entity type at column level, with `match_rate` =
     fraction of sampled cells containing at least one hit.
  2. An `InlineEntityHistogram` keyed by entity type with counts and a
     sample of matched cells.
- The aggregator dedupes against regex (RFC §5.2). When Presidio's
  `EMAIL_ADDRESS` overlaps with the regex `email` detector, regex wins.

## Customer opt-in

Same `ml_detection_enabled` flag from Phase 1. NER does not get its own
flag — if a customer turns ML on, they get classifier + NER together. If
they turn it off, neither artifact is pulled. (We can split into two
sub-flags later if a customer asks; do not build that pre-emptively.)

NER artifacts are heavier than the classifier (~600MB for `en_core_web_lg`
plus Presidio recognizers). The lazy-pull progress is surfaced through the
existing structured logger so the platform can render a one-time "preparing
ML detection" state on first scan.

## STORM_GUIDE alignment

The "Inline entities found" row in the column drill-down (already documented
in `decoy-platform/STORM_GUIDE.md` §4.4) is fed by `InlineEntityHistogram`.
Wiring the platform-side renderer is a separate small plan in
`decoy-platform/plans/`; this engine plan just exposes the data.

## Verification

1. Harness: recall ≥ 0.90 for PERSON / EMAIL_ADDRESS / PHONE_NUMBER on
   `mixed_text_pii.parquet`; precision ≥ 0.85.
2. With ML off: no NER artifacts on disk, profiler unchanged.
3. With ML on but the column is not classified as free-text: NER does not
   run (cost guarantee).
4. Cell sample cap is honored: a 1M-row free-text column does not run NER
   over more than 500 cells.

## Risks

- spaCy model download is the single biggest cold-start cost in the whole
  ML layer. Phase 1 framing matters — if first-scan UX is bad, this is
  where it shows up.
- Presidio's default recognizers can over-fire on `LOCATION` and
  `DATE_TIME`. Decide whether to exclude those from the active recognizer
  set in `ner.py`. Suggest: yes, exclude both initially — date detection
  is already covered by regex, and `LOCATION` is too noisy for the recall
  win.
