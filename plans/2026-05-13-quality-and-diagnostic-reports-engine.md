# Block 7 — Diagnostic + Quality reports (engine side)

> **Status:** plan — part of the 2026-05-13 audit-fix series.
> **Standalone**, but Block 9 (compliance PDF) consumes its output.
> **Pairs with:** `decoy-platform/plans/2026-05-13-quality-reports-api-and-persistence.md`.

## Why

The reference's largest reporting gap. Today the masker emits a file and a log line; nothing measures whether the masked output is still analytically useful or even structurally valid. The reference's two-report model (Diagnostic = validity floor at 100%; Quality = 0–100 fidelity score) is the SDV/MOSTLY AI/Gretel convergence point.

## Code paths

- New package: `src/decoy_engine/quality/`.
  - `quality/diagnostic.py` — validity floor metrics.
  - `quality/fidelity.py` — distribution + correlation similarity.
  - `quality/report.py` — `QualityReport` dataclass + composite score.
- `src/decoy_engine/masker/masker.py` — call `compute_quality(source_df, output_df, …)` when `compare_to_source=True`.
- Optional dep: `scipy` for KS and Wasserstein (already in pyproject if generators use it; otherwise add).

## Engine changes

### 1. Metric set

**Diagnostic (validity floor — must hit 1.0 to release):**
- `boundary_adherence(col_orig, col_masked)` → fraction of masked values within `[min(orig), max(orig)]` (numeric); for dates, within `[min, max]` of parsed range.
- `category_adherence(col_orig, col_masked)` → fraction of masked categorical values present in the original category set (for `redact`/`bucketize` outputs, expected = 0 by design — exclude these strategies from the floor for that column).
- `key_uniqueness(col_orig, col_masked)` → 1.0 if both are unique, 0.0 otherwise.
- `referential_integrity(rel_config, masked_dfs)` → 1.0 if every foreign key still resolves.
- `missing_value_similarity(col_orig, col_masked)` → 1 - |null_rate_orig - null_rate_masked|.

Diagnostic gate logic: every metric on every column ≥ a configurable threshold (default 1.0 = strict). If any metric fails the threshold for a column whose strategy isn't excused, the gate fails. `MaskResult.diagnostic_passed: bool` reflects this.

**Fidelity (Quality score — 0–100):**
- `ks_complement(col_orig, col_masked)` → 1 - KS_D (numeric, dates).
- `tv_complement(col_orig, col_masked)` → 1 - TVD (categorical).
- `js_distance(col_orig, col_masked)` → Jensen-Shannon (categorical, smoothed).
- `correlation_similarity(num_cols_orig, num_cols_masked)` → 1 - mean(|ρ_orig - ρ_masked|) / 2 (per pair).
- `contingency_similarity(cat_pairs_orig, cat_pairs_masked)` → 1 - TVD over joint frequency tables.

Composite formula:

```
marginal_score = mean(per-column ks_complement or tv_complement)
pairwise_score = mean(correlation_similarity for num/num pairs, contingency_similarity for cat/cat pairs)
overall_score  = round((marginal_score + pairwise_score) / 2 * 100)
```

SDV-style. `overall_score >= 90` = Excellent, 80–89 = Good, 60–79 = Moderate, <60 = Poor.

### 2. `QualityReport` dataclass

```python
# quality/report.py
@dataclass
class QualityReport:
    diagnostic: dict[str, dict[str, float]]   # {col -> {metric_name -> value}}
    diagnostic_passed: bool
    per_column_fidelity: dict[str, dict[str, float]]
    pairwise_fidelity: dict[str, float]      # {"corr_avg": …, "contingency_avg": …}
    marginal_score: float                    # 0–1
    pairwise_score: float
    overall_score: int                       # 0–100
    grade: Literal["Excellent", "Good", "Moderate", "Poor"]
    n_rows_compared: int
    generated_at: str

    def to_dict(self) -> dict:
        return asdict(self)
```

### 3. Wire into masker

```python
# masker/masker.py
def run(self, config, *, compare_to_source: bool = True) -> MaskResult:
    # … existing masking work …
    quality = None
    if compare_to_source and self._source_df_for_quality is not None:
        from decoy_engine.quality.report import compute_quality
        quality = compute_quality(self._source_df_for_quality, output_df)
    return MaskResult(output_path=..., disclosure=..., quality=quality)
```

Keep a reference to the source dataframe through the masker pipeline; today it's already held briefly. Memory cost: one extra dataframe-shaped reference for the lifetime of the job — acceptable on the batch path; punt on streaming.

### 4. Performance

KS and TVD per column over 1M rows: <500ms with numpy/scipy. Pairwise correlation on 50 numeric columns: ~50²/2 = 1250 Pearsons, all O(n) — well under a second. Budget the whole report at <10% of the masking job's wall-clock; if it overruns, sample to 100k rows for the fidelity metrics and flag in the report.

## Tests to add

`tests/unit/test_quality_diagnostic.py`:
- Identity mask → every diagnostic metric = 1.0 and `diagnostic_passed=True`.
- Out-of-range numeric injection → `boundary_adherence < 1.0` and gate fails.
- Null-rate mismatch → `missing_value_similarity < 0.95` flagged.

`tests/unit/test_quality_fidelity.py`:
- Identity mask → `overall_score == 100`.
- Pure-noise mask (random replace) → `overall_score < 30`.
- Hash mask of an `id` column → marginal score near 1.0 (distribution preserved), correlation may drop.

`tests/integration/test_masker_with_quality.py`:
- End-to-end masker run with `compare_to_source=True` produces a `QualityReport`.
- `compare_to_source=False` → `result.quality is None`.

## Verification

1. `pytest tests/unit/test_quality_*.py -v` — green.
2. `pytest tests/integration/test_masker_with_quality.py -v` — green.
3. Manual: run the existing HIPAA-disguise example, confirm `overall_score >= 80` on a faker-heavy output (marginal stays high; pairwise drops because hash kills correlation — that's expected).

## Out of scope (later blocks)

- Persisting the report on `Job` — Block 7 platform doc.
- Surfacing in the UI (before/after histograms, correlation heatmap) — Block 7 platform doc + a follow-up UI plan.
- Synth-specific privacy metrics (DCR, MIA, Anonymeter) — Block 10.
