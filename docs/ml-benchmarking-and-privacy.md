# ML field-recognition: benchmarking & privacy standards

**Status:** authoritative standard. GATE-1 prerequisite for the ML depth sprints
(Sprint B = ML2.x pack loader + LightGBM; Sprint C = ML3.x classify API +
provenance + review UI). No ML model pack merges, and no model-driven
classification ships, until it meets every MUST below with committed evidence.

**Scope.** Covers the STORM **field-recognition** capability: a classifier that
labels tabular **columns** with one of the Decoy-owned semantic types (SSN,
email, MRN, ...). It is a detector, not a privacy mechanism. It does not change
masked output; it suggests labels that flow through the existing STORM
review/remap path (ML off by default; on-prem only; no cloud inference).

This standard refines, and is authoritative over, the per-sprint specs it
touches: `phase4-ml/ml0.2-baseline-measurement.md` (uses F1; **superseded by F2
below**), `phase4-ml/ml0.3-confidence-bands.md` (bands must be **calibrated**,
below), `phase4-ml/ml2.3-classifier-evidence.md`, `ml3.2-model-provenance.md`,
and `backlog/ml/ml-model-pack-security-and-deployment.md`.

> Two evaluation regimes, kept distinct:
> - **Column classification (V2, in scope):** multiclass, each column gets one
>   semantic-type label. Evaluated per-class with type-match (§A).
> - **Free-text NER (V3, deferred per ML Lock 3):** span extraction inside text.
>   Evaluated span-level with IoU matching (§A.6). The standard is recorded now
>   so it is ready; the gate applies when/if NER is built. Free-text NER has a
>   distinct privacy cliff (§B.6).

---

## A. Benchmarking standards

### A.1 Primary metric: F2 (recall-weighted), reported per class

A missed PII column is far costlier than a false alarm (a false alarm costs a
human a glance in the remap UI; a miss leaks). Optimize and report **Fβ with
β=2** (recall weighted 2× precision), not F1.

- **MUST** report, per semantic type: precision, recall, **F2**.
- **MUST** report aggregate **macro-F2** (unweighted mean across types, equal
  weight to rare types, which is where detectors fail) **and** weighted-F2
  (corpus-prevalence weighted). Macro is the headline; report both.
- **MUST** report `balanced_accuracy` (macro-average recall) as an
  imbalance sanity check, and an entity-type **confusion matrix**.
- **MUST** emit an enumerated list of every false negative and false positive
  (column identifier + predicted/true label), so errors are inspectable, never
  an aggregate number alone.

Source: Microsoft Presidio explicitly recommends β=2 for PII because recall
matters more than precision (presidio-research `SpanEvaluator`). sklearn §3.4:
`F_β = (1+β²)·tp / ((1+β²)·tp + fp + β²·fn)`, "beta = 2 makes recall twice as
important as precision"; macro "over-emphasizes typically low performance on an
infrequent class"; `balanced_accuracy` "avoids inflated performance estimates on
imbalanced datasets."

### A.2 The baseline-beating gate (the lift lock)

A model pack ships **only if** it beats the deterministic regex baseline
(ML0.2) on **macro recall by ≥ 5 percentage points** on the held-out test set
(the standing V2 lock for the optional MiniLM pack, applied here to every
pack). A pack that does not clear the lift does not ship; the regex baseline
stands. The lift is a CI gate, computed on the frozen test set, not a dev claim.

### A.3 Held-out test set + leakage guard

- **MUST** keep a held-out test split that is **never** read during model
  development or tuning. Tuning happens on train/validation only.
- **MUST** split with **`StratifiedGroupKFold`, group = the unique PII value
  string** (the actual SSN/email/MRN), so the same value cannot appear in both
  train and test. This prevents the model memorizing strings instead of
  learning column-shape patterns, the dominant leakage failure here.
- **MUST** fit all preprocessing/feature scaling on the train fold only.

Source: sklearn §12.2 "Data leakage ... information not available at prediction
time ... overly optimistic estimates"; §3.1.2.4 `GroupKFold` "ensures the same
group is not represented in both testing and training sets";
`StratifiedGroupKFold` preserves class balance and group separation.

### A.4 Calibrated confidence bands

The ML0.3 bands (high / review-needed / low) drive how much a human trusts a
suggestion, so the probabilities behind them **MUST be calibrated**, not raw
`predict_proba`.

- **MUST** report a **reliability curve + Brier score** for the shipped model.
- **MUST** calibrate when probabilities drive triage: `CalibratedClassifierCV`
  (`method='isotonic'` for ≥ ~1000 calibration samples, else sigmoid).
- **MUST** set the high/review/low thresholds on the **calibrated** scores, and
  state them in the model card. A "high confidence ≥ X" band must mean roughly
  X of those predictions are correct (verified on the held-out set).

Source: sklearn §1.16 (reliability diagrams, Brier score as a strictly proper
scoring rule, isotonic ≥ ~1000 samples).

### A.5 Operating threshold from an explicit cost ratio

- **MUST NOT** default the decision threshold to 0.5. Set it from an explicit
  **FN:FP cost ratio** favouring recall, tuned via `TunedThresholdClassifierCV`
  against that cost, and **document the ratio + its justification in the model
  card.** No external authority mandates a specific PII ratio, the choice is
  ours and must be justified (a miss leaks; a false alarm costs a UI glance).

Source: Elkan (2001) "Foundations of Cost-Sensitive Learning," IJCAI, with cost
ratio k = C(FN)/C(FP), the Bayes-optimal threshold is 1/(1+k); sklearn §3.3
cost-sensitive learning + `TunedThresholdClassifierCV`.

### A.6 Free-text NER eval (deferred V3), span-level

When/if free-text NER ships: evaluate at **span level** (not token), counting a
true positive only when char/token IoU with the annotation **≥ 0.9 and the type
matches**; partial overlap counts as both FP and FN. Report per-entity F2 +
global PII F2, with an IoU-tagged error list. Ground truth in Presidio's
`InputSample`/`Span` schema.

Source: Presidio `SpanEvaluator` (default IoU 0.9; F2 default).

### A.7 Determinism & regression

- **MUST** make evaluation deterministic (seed-pinned) and commit a **frozen
  golden baseline** of the metrics, so any regression fails CI (extends the
  ML0.2 regression test). Same corpus + same seed -> identical report bytes.

### A.8 Subgroup breakdown (not aggregate-only)

- **MUST** report metrics broken down by semantic-type subgroup in the model
  card's Quantitative Analyses; a single aggregate number is non-compliant
  (it hides the rare-type failures the macro metric exists to surface).

Source: Model Cards (Mitchell et al., 2019), Factors enumerate subgroups;
Quantitative Analyses report disaggregated metrics.

---

## B. Privacy standards

### B.1 Detection ≠ de-identification (honest claims)

- **MUST** state, in the model card and in `what-we-cannot-prove.md`, that the
  classifier is **necessary but not sufficient** for de-identification: it
  flags candidate PII, it does not guarantee privacy, and a missed column or a
  re-identification vector can remain. **MUST NOT** claim the detector "protects
  privacy" or performs de-identification on its own. No differential-privacy
  claim attaches to the classifier, it is a detector.

Source: NIST IR 8053 (de-identified data can sometimes be re-identified); NIST
SP 800-188 (rejects masking-tool-alone de-identification; calls for measurable
performance + governance).

### B.2 Quantitative thresholds + adversarial/OOD evaluation

- **MUST** gate on the measurable thresholds in §A (quantitative, not
  qualitative sign-off).
- **MUST** include an **out-of-distribution / adversarial held-out** slice
  (cryptic headers, mixed-locale, obfuscated values), the motivated-intruder
  analogue, and report metrics on it separately. Benign-only test data is
  non-compliant.

Source: NIST SP 800-188 (re-identification studies, measurable standards,
motivated-intruder test).

### B.3 Synthetic-only corpora + a datasheet

- **MUST** build train/eval corpora from **synthetic data only**, no real
  customer PII enters the harness (BF2/ML0.1 already holds this).
- **MUST** ship a **Datasheet for Datasets** (all 7 sections: Motivation,
  Composition, Collection, Preprocessing/labeling, Uses, Distribution,
  Maintenance) for any corpus that gates a sprint. The Preprocessing/labeling
  section records the label schema + ground-truth labeling decisions and any
  inter-annotator agreement.

Source: Datasheets for Datasets (Gebru et al., 2018/2021).

### B.4 No raw cell values anywhere

- **MUST** keep features as **aggregate column statistics** (header tokens,
  dtype, null/distinct/unique rates, char-class fractions, entropy, checksum
  pass rates, shape signatures), **never raw cell values** in feature vectors,
  eval reports, model artifacts, or logs. Add a test asserting no raw sampled
  value appears in the serialized report (BF2's security-canary pattern).

### B.5 On-prem only + tenant isolation

- **MUST** run inference on-prem; **no cloud inference** (V2 lock).
- **MUST** keep any customer correction/override signal **tenant-isolated**: it
  never trains a model shared across tenants (V2 Lock 1). Cross-tenant training
  requires an explicit, separately-gated decision.

### B.6 Span-logging policy precedes any NER

- **MUST** decide and document a span-logging / retention policy **before** any
  free-text NER is built (V3). Free-text NER touches raw text spans, the real
  privacy cliff, and is out of scope until that policy exists.

### B.7 Pack provenance + governance gate

- **MUST** ship every model pack with: a signed **provenance** record
  (ML3.2, training corpus datasheet ref, code version, eval report hash) and a
  **Model Card** (all 9 sections: Model Details, Intended Use, Factors, Metrics,
  Evaluation Data, Training Data, Quantitative Analyses, Ethical Considerations,
  Caveats/Recommendations).
- **MUST** pass a **legal + security review** before a pack is published (V2
  lock; see `ml-model-pack-security-and-deployment.md`).

Source: Model Cards (Mitchell et al., 2019), the 9 required sections.

---

## C. The gate, what Sprint B / C must produce

A sprint clears this GATE-1 prerequisite only when all of these exist and are
committed (cite this doc's section in the PR):

- [ ] Per-type precision/recall/**F2** + macro-F2 + weighted-F2 + balanced
      accuracy + confusion matrix + enumerated FP/FN list (§A.1)
- [ ] Held-out test via `StratifiedGroupKFold` grouped by unique value;
      preprocessing fit on train only (§A.3)
- [ ] **≥5 ppt macro-recall lift over the regex baseline** on the held-out set,
      as a CI gate; else the pack does not ship (§A.2)
- [ ] Calibrated confidence bands: reliability curve + Brier score; thresholds
      set on calibrated scores (§A.4)
- [ ] Operating threshold from a documented FN:FP cost ratio (§A.5)
- [ ] Deterministic eval + frozen golden baseline regression test (§A.7)
- [ ] Detection-≠-de-identification statement in the model card +
      `what-we-cannot-prove.md` (§B.1)
- [ ] OOD/adversarial held-out slice reported separately (§B.2)
- [ ] Synthetic-only corpus + completed **Datasheet** (§B.3)
- [ ] No-raw-value test on features/report/logs (§B.4)
- [ ] On-prem-only + tenant-isolation honored (§B.5)
- [ ] Signed provenance + complete **Model Card** + legal/security review (§B.7)

---

## References

- Microsoft Presidio, presidio-research `SpanEvaluator`: https://github.com/microsoft/presidio-research ; https://microsoft.github.io/presidio/evaluation/
- NIST SP 800-188, *De-Identifying Government Datasets* (2023): https://csrc.nist.gov/pubs/sp/800/188/final
- NIST IR 8053, *De-Identification of Personal Information* (2015): https://csrc.nist.gov/pubs/ir/8053/final ; motivated-intruder test: https://csrc.nist.gov/glossary/term/motivated_intruder_test
- Mitchell et al., *Model Cards for Model Reporting* (FAccT 2019): https://arxiv.org/abs/1810.03993
- Gebru et al., *Datasheets for Datasets* (2018; CACM 2021): https://arxiv.org/abs/1803.09010
- scikit-learn: Metrics §3.4 https://scikit-learn.org/stable/modules/model_evaluation.html ; Calibration §1.16 https://scikit-learn.org/stable/modules/calibration.html ; Threshold tuning §3.3 https://scikit-learn.org/stable/modules/classification_threshold.html ; Leakage §12.2 https://scikit-learn.org/stable/common_pitfalls.html ; Grouped CV §3.1.2.4 https://scikit-learn.org/stable/modules/cross_validation.html
- Elkan, *The Foundations of Cost-Sensitive Learning* (IJCAI 2001): https://dblp.org/rec/conf/ijcai/Elkan01.html

> Citation caveats (carried from research): NIST PDFs are not text-extractable
> via fetch; section/page anchors are the CSRC landing pages + glossary, which
> are confirmed live. No external authority fixes a specific PII FN:FP ratio
> (§A.5), that choice is ours to justify; Presidio is the domain anchor for the
> recall-priority (β=2) stance.
