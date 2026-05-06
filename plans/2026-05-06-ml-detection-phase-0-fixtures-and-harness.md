---
Status: planning
Branch: claude/ml-masked-data-evaluation-9FbpV
References: SHARED_ENGINE_ARCHITECTURE.md, ../decoy-platform/plans/2026-05-06-ml-sensitive-data-detection.md
---

# Phase 0 — ML detection: fixtures and evaluation harness

First implementation chunk of the RFC. **No model code in this phase.** Goal:
get a labeled fixture corpus and a metrics harness in place so every later
phase can be measured against a fixed baseline.

## Why this phase first

The regex layer (`src/decoy_engine/storm/detectors.py`) currently has no
recall/precision number attached to it. Without that, "ML helps" is an
opinion. Phase 0 produces the number, then every later phase has to beat or
match it.

## Deliverables

1. A labeled fixture corpus under `tests/fixtures/ml_detection/`.
2. An evaluation harness that runs any detector pipeline against the corpus
   and emits per-entity recall, precision, and (later) calibration metrics.
3. A pytest entry point + a CLI entry point reusing the harness.
4. CI wiring: harness runs on every PR that touches `storm/`.
5. **License-allowlist CI gate** — fails the build if a new dependency in
   `decoy-engine`'s lockfile declares a license outside the permissive tier
   (Apache-2.0 / MIT / BSD). Captured as a ROADMAP backlog item ("License
   CI gate — should be Phase 0 of ML detection"); promoted here to a
   shipped deliverable so Phase 1 cannot accidentally pull in a
   non-permissive transitive dependency.

Out of scope: ML models, sampling changes, profiler changes, the customer
opt-in flag (Phase 1 introduces it).

## Files

```
src/decoy_engine/storm/
└── eval/                                      ← NEW
    ├── __init__.py
    ├── harness.py                             ← run_harness(detectors, fixtures) → MetricsReport
    ├── metrics.py                             ← per-entity recall/precision; calibration stub
    └── cli.py                                 ← `python -m decoy_engine.storm.eval ...`
ci/
└── check_licenses.py                          ← NEW: license-allowlist gate over the lockfile
tests/fixtures/ml_detection/
├── README.md                                  ← schema + how to regenerate
├── synth/                                     ← Faker-generated, regenerable
│   ├── easy_columns.parquet                   ← email, ssn, phone, zip, dates
│   ├── cryptic_names.parquet                  ← col_07, f3 — same content, scrambled names
│   ├── mixed_text_pii.parquet                 ← free-text with inline PII
│   └── manifest.yaml                          ← per-column ground-truth labels
├── curated/                                   ← hand-labeled, do not regenerate
│   └── (empty in Phase 0; populated as the auditor corpus grows)
└── generators/
    └── synth_gen.py                           ← Faker-driven fixture generator (deterministic seed)
tests/unit/storm/
└── test_eval_harness.py                       ← harness sanity + metric correctness
```

## Reuse / do not reinvent

- `DetectorMatch` (`src/decoy_engine/storm/types.py`) — the harness consumes
  this exact shape. Do not introduce a parallel match type.
- Existing detector callable signature `(series, col_name) → DetectorMatch | None`
  — the harness drives detectors through this interface so regex and ML
  stages plug in identically.
- Faker is already a dev dependency (used by the masking transforms). No new
  top-level dependency for fixture generation.

## Fixture schema

Each fixture is a Parquet file plus an entry in `manifest.yaml`:

```yaml
- file: synth/easy_columns.parquet
  columns:
    - name: email
      ground_truth: email
    - name: ssn
      ground_truth: ssn
    - name: notes_field
      ground_truth: free_text
      inline_entities: [PERSON, EMAIL_ADDRESS]   # NER ground truth, Phase 2 onward
```

The harness reads the manifest, runs the configured detector pipeline against
each column, and compares emitted matches to ground truth.

## Metrics

Per RFC §7. Phase 0 implements:

- **Recall** per entity type (column-level: did we identify the right
  category at all).
- **Precision** per entity type (column-level: when we fired, were we right).
- **Confusion matrix** dump for debugging.

Calibration / ECE is stubbed but not computed (no probability outputs from
the regex layer; lights up in Phase 1).

## CI wiring

Add a job to whatever CI runs the engine tests today: `pytest -m harness`
runs the regex baseline against the synth fixtures and prints the metrics
report. **No threshold gating in Phase 0** — the baseline number is the
first artifact. Threshold gates land in Phase 1 once we have a target.

## Customer opt-in (forward note)

Phase 0 is engine-internal; nothing customer-facing. The opt-in flag arrives
in Phase 1. Phase 0 simply must not assume ML is always on or always off —
the harness is parameterized over an arbitrary detector list, regex-only
included.

## Verification

1. `pytest tests/unit/storm/test_eval_harness.py` — harness produces the
   expected metrics on a tiny in-memory fixture.
2. `python -m decoy_engine.storm.eval --fixtures synth --pipeline regex`
   prints a metrics report against the synth corpus.
3. The reported recall on the regex baseline is a number we write down here
   as the locked-in baseline:
   ```
   regex baseline (Phase 0, date TBD):
     email     recall=___, precision=___
     ssn       recall=___, precision=___
     ...
   ```
   Phase 1 must beat or match each row.

## Open questions

- Do we want a third fixture tier — anonymized real customer data — gated
  behind a separate test marker? Probably yes, but only after we have a
  customer who'll donate one; out of scope here.
