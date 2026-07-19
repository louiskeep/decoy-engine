# CH-3: cryptic-header recognition -- lexicon ablation and disposition

Status: **DECIDED (2026-07-19).** The CH-1/CH-2 header-role lexicon does NOT
clear the locked >=5pp lift bar on the cryptic-header benchmark and is **excluded
from the shipped model pack**; the lexicon code is **not** carried in the
re-baseline PR. CH-4 (embedded lexicon / MiniLM) stays **parked**. The question
re-opens when real data arrives (HC-1 slice-2 ETL). This document is the record
of the investigation so the negative result is not silently re-discovered later.

## What was tried

CH-1/CH-2 built a header-role lexicon (`storm/features/header_lexicon.py`, added
in phase A of this branch, commit `16064b7`, then removed here): an explicit,
auditable single-token synonym table mapping header tokens to a small closed set
of canonical roles aligned with the label space (`dx_cd` and `diagnosis_code`
both -> `role_icd10`, `mbr_id` -> `role_health_plan_id`, ...), plus a fuzzy
char-bigram (Dice >=0.80) fallback for typos. It was wired into the featurizer as
`role_{canonical}` indicator features next to the existing `hdr_{token}` ones.
The intent: give cryptic/abbreviated headers a vocabulary-stable signal that
`hdr_{token}` (which only fires for training-seen tokens) cannot.

The lexicon itself was correct: 0 cross-role and 0 false positives across all
corpus clear/cryptic headers plus 88 common non-PII probe headers; 54 unit tests.

## The locked decision rule and the measurement

Rule (roadmap, pre-registered): ship the lexicon arm only if it lifts macro
recall by **>=5pp** on the held-out cryptic-header benchmark
(`build_cryptic_fixtures()`, 210 columns, ~26% lexicon-mappable abbreviations +
~74% opaque headers).

Ablation: two arms, **each trained from scratch** on the full 2958-column corpus
with isotonic calibration (fresh `DictVectorizer` + fresh
`CalibratedClassifierCV` per arm; `role_*` stripped from both train and eval
feature dicts for the `fast` arm), evaluated on the cryptic slice. 5 seeds:

| Arm | cryptic macro-recall (mean +/- std) |
|---|---|
| fast (content + `hdr_`, no `role_*`) | 0.9473 +/- 0.0055 |
| fast + lexicon (`role_*`) | 0.9473 +/- 0.0022 |

**Net lift: +0.00pp over 5 seeds** (pooled std ~0.59pp), far below the 5pp bar.

Per-label, the effect looks like two movements that cancel, though each
individual delta is modest relative to its own noise (see caveat below):

| label | fast | fast+lexicon | delta |
|---|---|---|---|
| health_plan_id | 0.691 +/- 0.116 | 0.600 +/- 0.053 | **-0.091** |
| mrn | 0.782 +/- 0.073 | 0.873 +/- 0.045 | **+0.091** |
| (all 8 others) | 1.000 | 1.000 | 0.000 |

## Why (a well-supported hypothesis)

Two findings, entangled. Note the per-class deltas above (health_plan_id
-0.091, mrn +0.091) have a std of ~0.05-0.12 over 5 seeds -- roughly 1-1.7
sigma each -- so read the mechanism below as a directionally-consistent
effect the evidence supports, not a precisely-measured certainty. The
**net +0.00pp lift conclusion**, which is what drives the ship decision, is
solid regardless (it does not depend on the per-class story being exact):

1. **Content saturation.** Content features alone already reach 0.95 macro-recall
   on cryptic headers, so there is almost no headroom for any header method.

2. **Shortcut-reliance.** `role_health_plan_id` fires on 100% of clear hp headers
   in training, so the model learns to lean on it. hp VALUES are low-
   distinctiveness (generic alphanumeric plan IDs), so at inference the cryptic hp
   columns whose headers are opaque (not lexicon-mappable) LOSE recall -- the role
   signal they'd rely on is absent. mrn values are distinctive (MRN-prefixed), so
   its header helps without over-reliance. The two effects cancel. This is a
   distribution-shift fragility, i.e. anti-robustness, not a lexicon bug.

## Disposition (decided with Fable cross-model review, Cam ratified)

- **Exclude `role_*` from the model pack.** Honor the pre-registered rule; do not
  ship features that show zero aggregate lift and introduce a per-class
  regression via shortcut-reliance.
- **Do not couple the lexicon to the pack.** A hand-maintained lexicon feeding
  model features means every table edit changes the input distribution and
  invalidates the trained model + isotonic calibrator -- a re-baseline per
  lexicon edit. Decoupled, the lexicon (if revived) can evolve freely.
- **Remove the lexicon from this PR** (Cam's call), rather than leave unwired code
  that rots. The design + tests are preserved in this branch's history (phase-A
  commit `16064b7`) and summarized here.
- **Park CH-4** (embedded lexicon / MiniLM): it attacks the same ~5pp of headroom
  with more weight and the same 74%-opaque inertness; not justified now.

## Important caveat: this is synthetic saturation, not "header methods don't work"

The 0.95 content ceiling is measured on synthetic data drawn from the same
generator family as training. Leakage guards prevent literal value leakage, not
distributional homogeneity: synthetic SSNs/ICD10s/PANs are cleanly formatted, so
content features look stronger than they will on real data (padding, mixed
formats, sparse/low-row columns). Header signal earns its keep precisely when
content signal is weak -- and hp, the one label with genuinely non-distinctive
values, is exactly where header signal mattered (in both directions). So the
honest conclusion is **"no headroom at synthetic saturation,"** not a general
verdict on header-based recognition.

## Re-evaluation trigger and prescribed fix

- **Trigger:** when HC-1 slice-2 real-data ETL lands, re-run the CH-3 ablation on
  a real (or realistically messy) cryptic-header benchmark. Real data is where a
  header lexicon can beat content features.
- **Prescribed fix if it reopens:** train-time **feature dropout** on the `role_*`
  block (randomly zero it with p~=0.3-0.5 during training). This is the standard
  remedy for over-reliance on an intermittently-available modality -- it forces
  the model to keep content pathways for low-distinctiveness classes (hp) while
  still exploiting the role signal when present. Preferred over an inference-time
  "fire role only when content is weak" gate, which adds a heuristic branch and
  complicates calibration.
- **Benchmark-design note:** a future CH-3 should report **stratified** metrics
  (mappable-subset recall vs opaque-subset recall) rather than one full-slice
  number, since a +5pp bar on a 74%-opaque slice implicitly demands near-perfect
  gains on the ~26% mappable subset. Also check the >=0.95 confidence-band
  composition, not only top-1 recall.

## What ships in the re-baseline instead

MLF-4 (corpus 291 -> 2958, value/format/locale + header-style diversity, leakage-
guarded) and MLF-5 (isotonic calibration: mean calibration error 0.097 -> 0.0536;
the >=0.95 "high" band went from unreachable to 541/592 held-out columns at 100%
accuracy; lift over the regex baseline 19.95pp). Those are the measured wins.
