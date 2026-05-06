---
Status: planning (deferred)
Branch: claude/ml-masked-data-evaluation-9FbpV
References: ../decoy-platform/ROADMAP.md, ../decoy-platform/plans/2026-05-06-ml-sensitive-data-detection.md, 2026-05-06-ml-detection-phase-1-column-classifier.md, 2026-05-06-ml-detection-phase-2-ner-free-text.md
---

# Phase 5 — ML detection: domain fine-tune (deferred)

Last phase in the RFC. **Explicitly deferred** in the RFC §10 and ROADMAP
"Deferred" section ("Per-tenant ML fine-tuning"). This plan exists so the
shape of the work is on file when we revisit. Do not start without an
explicit prioritization decision.

## Trigger conditions for un-deferring

All four must be true:

1. The Phase 3 review queue has produced a corpus of **≥ 5,000** labeled
   auditor overrides spanning **≥ 3** customer deployments (numbers nominal
   — tune when we see real growth).
2. A specific customer has asked for a domain entity Presidio doesn't ship
   (e.g. internal account-number formats, customer-specific medical IDs).
3. Phase 0 harness shows the off-the-shelf NER underperforming on that
   entity by a margin large enough to justify training cost.
4. Privacy review has signed off on the per-tenant data flow (see
   Open questions, Risks below).

If any condition fails, stay on the off-the-shelf models from Phases 1–2.

## Scope (when un-deferred)

- Build a fine-tuning pipeline for the spaCy NER head (`en_core_web_lg`-
  compatible token classifier).
- Define a per-tenant override-corpus export pipeline that produces
  training data without leaking PII outside the customer's deployment.
- Define how fine-tuned weights are produced, stored, distributed, and
  versioned.
- Decide between **shared cross-tenant fine-tunes** (one model, all
  customers benefit) and **per-tenant fine-tunes** (each customer's model
  trained only on their data — privacy-safe but operationally heavy).

Out of scope until trigger fires: writing any of the above. This plan is a
shape-only sketch.

## Files (sketch — when implementing)

```
src/decoy_engine/storm/ml/
├── train_ner.py                               ← offline fine-tune script
├── corpus_export.py                           ← in-deployment export of override corpus
└── artifacts.py                               ← MODIFIED: distinguish base vs fine-tuned weights
artifacts/
├── manifest.json                              ← versioning gains a "tenant_id?" or "variant" field
└── (fine-tuned weights stored alongside base)
tests/unit/storm/ml/
└── test_corpus_export_redacts_source.py       ← critical privacy guarantee
```

## Reuse / do not reinvent

- **Phase 0 harness.** Fine-tuned variants run through the same harness;
  metrics gate promotion.
- **Phase 1 `artifacts.py`.** Same lazy-pull, just with one more variant
  per asset. Do not introduce a parallel artifact loader.
- **Phase 3 review-queue tagging** (`origin="ml_review"` on remaps). Already
  marks the rows that should feed training; do not re-design the labeling
  scheme.
- **Existing config flag** `ml_detection_enabled` still gates everything.
  Fine-tuned weights also do not pull when ML is off.

## Decision tree (the substance of this plan)

### Cross-tenant vs per-tenant

| | Cross-tenant fine-tune | Per-tenant fine-tune |
|---|---|---|
| Privacy | Need contractual + technical guarantees that customer data does not become training data without consent | Customer's data stays in customer's deployment; no aggregation |
| Quality | More data → better model (the usual win) | Less data, narrower scope, but exactly fits that customer |
| Operational cost | One model to train, distribute, and version | One model per tenant; multiplicative cost |
| Distribution | Same lazy-pull as Phases 1–2 | Each deployment trains in-place or fetches its own variant |

**Working preference:** start with **opt-in cross-tenant** (default off,
customers must explicitly contribute). This avoids the per-tenant
operational explosion and gives us the data quality win, but only with
explicit consent. Per-tenant becomes the answer if a regulated customer
needs it.

### Where training runs

- Cross-tenant: in our infra, on aggregated and consented corpora. Standard.
- Per-tenant: in the customer's deployment. Hard mode — needs CPU/GPU
  budget, training-time isolation, and a way to ship the fine-tune code
  separately from the runtime engine. **Do not build per-tenant unless
  Trigger #4 forces it.**

### What gets exported from a deployment

`corpus_export.py` reads from the existing remap table where
`origin="ml_review"` (set in Phase 3) and produces:

- The labeled column metadata (column name, inferred type, ground-truth
  type from override).
- Optionally, sampled cell content **only when the customer has explicitly
  opted in to data contribution**, with PII re-masked using the customer's
  own Mask rules before export.

There is no implicit export. Default exports include only column metadata,
never values.

## Verification (when implementing)

1. `corpus_export.py` with default settings produces a file containing
   zero raw cell values. Test asserts every exported record has only
   column-level metadata.
2. Fine-tuned NER beats the off-the-shelf baseline on the targeted entity
   by ≥ 0.05 recall on the held-out fixture.
3. Cross-tenant fine-tune does not regress any existing entity's recall by
   more than 0.01.
4. With ML disabled, fine-tuned weights are not pulled. Same guarantee as
   Phases 1–2.

## Risks / open questions

- **Privacy.** The biggest one. ROADMAP "Deferred" calls per-tenant fine-
  tuning a "privacy + operational minefield." The cross-tenant variant has
  the same risk if data flows aren't audited. Privacy review must precede
  any code.
- **Contamination across customers.** A fine-tune trained on Customer A's
  account-number format might mis-fire on Customer B. Need per-customer
  evaluation before promoting any cross-tenant model.
- **Versioning.** The artifact manifest needs to grow a "variant" field
  cleanly. Settle this in Phase 1's `manifest.json` design so we don't
  retro-fit later.
- **Self-hosted constraint.** Customers running on their own infra cannot
  contribute to a cross-tenant fine-tune without an outbound data path we
  do not currently have. This may dictate per-tenant as the only viable
  option for self-hosted ICP — confirm with sales before un-deferring.

## Promotion checklist (when un-deferring)

- [ ] Trigger conditions 1–4 all met
- [ ] Privacy review signed off
- [ ] Cross-tenant vs per-tenant decision recorded
- [ ] Sales-side confirmation that the targeted customer will accept the
      data-flow shape we're proposing
- [ ] ROADMAP Item 8 split into a separate ROADMAP item for Phase 5
- [ ] This plan re-dated and Status flipped to `planning`
