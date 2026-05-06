---
Status: planning
Branch: claude/ml-masked-data-evaluation-9FbpV
References: SHARED_ENGINE_ARCHITECTURE.md, 2026-05-06-ml-detection-phase-0-fixtures-and-harness.md, ../decoy-platform/plans/2026-05-06-ml-sensitive-data-detection.md
---

# Phase 1 — ML detection: column classifier

LightGBM column-classifier wired into the profiler behind a feature flag.
Establishes the **customer opt-in mechanism** for the entire ML layer.

Depends on Phase 0 (fixtures + harness) being green.

## Scope

- Train and ship a LightGBM column classifier over engineered features.
- Wire it as stage 2 of the detector pipeline (regex still runs first).
- Introduce the `ml_detection_enabled` config flag and the lazy-pull path.
- Beat the regex-only Phase 0 baseline on the cryptic-column-name fixtures.

Out of scope: free-text NER (Phase 2), review queue UI (Phase 3), default-on
behavior (Phase 4), domain fine-tuning (Phase 5).

## Files

```
src/decoy_engine/storm/
├── ml/                                        ← NEW
│   ├── __init__.py
│   ├── artifacts.py                           ← lazy-pull, version pin, on-disk cache, regex fallback
│   ├── features.py                            ← build_column_features(series, col_name) → np.ndarray
│   ├── embeddings.py                          ← thin wrapper around sentence-transformers MiniLM
│   ├── classifier.py                          ← ColumnClassifier(load → predict_proba)
│   └── train.py                               ← offline training script (not imported at runtime)
├── detectors.py                               ← (unchanged)
├── pipeline.py                                ← NEW: aggregate regex + ML stages into ranked DetectorMatch list
└── profiler.py                                ← MODIFIED: call pipeline.run() instead of detectors directly
src/decoy_engine/config.py                     ← MODIFIED: add ml_detection_enabled (default False)
tests/unit/storm/ml/
├── test_features.py
├── test_classifier.py
└── test_artifacts_lazy_pull.py
artifacts/                                     ← NEW (gitignored beyond manifest)
└── manifest.json                              ← pinned versions + sha256 for every model file
```

## Reuse / do not reinvent

- `DetectorMatch` — classifier emits this shape with `detector_id="ml_column_classifier"`.
- Phase 0 harness — the new pipeline plugs into the same harness; CI runs it.
- Engine config loader — `ml_detection_enabled` is one more bool, not a new
  config subsystem.
- The regex layer is untouched. The new `pipeline.py` orchestrates regex →
  classifier → aggregator; existing detectors keep their callable signature.

## Customer opt-in mechanism

Single flag, three layers:

1. **Engine config:** `PipelineConfig.ml_detection_enabled: bool = False`.
   Defaults off. Engine starts and runs regex-only when false. **No artifacts
   are pulled when false** — that's the "client doesn't want it, don't
   download it" requirement from the user.
2. **Platform surface:** the platform sets the flag from a per-deployment
   admin setting. Implementation in `decoy-platform` is a follow-up issue
   tracked here, not in this plan; for Phase 1 the flag is set via env var
   / `PipelineConfig` arg.
3. **License interaction:** out of scope for Phase 1. Document the seam: if
   we later gate ML behind a license tier, `LicenseVerifier.has_feature("ml_detection")`
   ANDs with the config flag. Do not build the license check yet.

When the flag flips on for the first time, `artifacts.ensure_loaded()` runs
the lazy-pull. If the pull fails, the engine logs a structured warning,
continues with regex-only, and the detection trail records "ML stage skipped
— artifacts unavailable." This matches the contract in
`SHARED_ENGINE_ARCHITECTURE.md` "ML Inference Boundary" §3.

## Model details

- **Algorithm:** LightGBM binary-or-multiclass classifier per the RFC.
- **Features (`features.py`):**
  - 384-dim sentence-transformers `all-MiniLM-L6-v2` embedding of the column
    name.
  - Value-sample summary stats: length distribution (min/max/mean/std),
    charset entropy, fraction-numeric, fraction-alpha, fraction-punct.
  - Regex-feature flags: did the regex layer fire weakly (rate < threshold)
    — feeds back signal that the regex layer alone discarded.
  - Top-k character n-gram hash bucket counts (k=64, mod 256).
  - Declared dtype as a categorical.
- **Output:** distribution over the regex entity set + `none` + `free_text`.
  `free_text` is the trigger for Phase 2 (NER).
- **Confidence band:** `>= 0.85` → fire. `0.55–0.85` → review queue (Phase
  3). `< 0.55` → drop.

## Training (offline, not part of the engine wheel)

`train.py` is a script, not imported at runtime. It:

1. Loads the Phase 0 synth fixtures.
2. Builds features.
3. Fits LightGBM with stratified cross-validation.
4. Writes `classifier.lgbm` + a metrics JSON next to it.
5. Updates `artifacts/manifest.json` with version + sha256.

The trained artifact is uploaded to the artifact host (TBD — same place we'd
host engine releases) and pulled lazily by `artifacts.py`.

## Aggregator behavior

Implemented in `pipeline.py`:

- Run regex stage first.
- If a high-confidence regex hit exists for an entity type, ML cannot
  override it — regex wins on its categories per RFC §5.
- For columns with no regex hit, the classifier's top class fires if it
  clears the threshold.
- Output is the existing `list[DetectorMatch]`, sorted by confidence.

## Verification

1. Phase 0 harness shows ≥ regex baseline on every entity, **plus** non-zero
   recall on `cryptic_names.parquet` where regex scored zero.
2. With `ml_detection_enabled=False`: zero artifacts pulled, profiler output
   bit-identical to pre-Phase-1.
3. With the flag on but artifacts deleted: detection trail records the
   skip; profiler still completes.
4. CI threshold gate added: recall regression on any entity vs. Phase 0
   baseline fails the build.
5. **Calibration regression test in CI** (ROADMAP backlog item, lands here
   because Phase 1 is the first phase with probability outputs). Reliability
   diagram per entity, expected calibration error < 0.05 per RFC §7.
   Calibration regression fails the build.

## Open questions / risks

- Where do trained artifacts get hosted? Object storage we already use for
  releases is the obvious answer; confirm with infra before cutting the PR.
- Do we ship a "tiny" fallback classifier inside the wheel for smoke tests
  / CI even with ML off? Probably not — it's contrary to "don't download
  if you don't want it" — but call it out so we don't accidentally bloat
  the wheel.
