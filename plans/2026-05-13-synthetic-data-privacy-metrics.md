# Block 10 — synthetic-data privacy metrics (engine side)

> **Status:** plan — part of the 2026-05-13 audit-fix series.
> **Standalone, heavier.** Recommend shipping after Block 7 because it shares the `quality/` package.
> **Pairs with:** `decoy-platform/plans/2026-05-13-synthetic-privacy-ui.md`.

## Why

`DataGenerator` produces synthetic rows; nothing scores whether the output leaks the training data. The reference's §7 minimum metric set: NewRowSynthesis, DCROverfittingProtection, plus at least one attack-based metric (Anonymeter's inference risk is the lowest-friction strong choice).

Important caveat from the reference: Yao et al. 2025 ("The DCR Delusion") showed DCR systematically underestimates risk versus membership inference. The UI copy (platform side) must surface DCR as a sanity check, not a guarantee.

## Code paths

- New `src/decoy_engine/quality/privacy.py` — DCR + NewRowSynthesis + Anonymeter wrappers.
- New `src/decoy_engine/quality/utility.py` — TSTR / TRTR / pMSE.
- `pyproject.toml` extras: `anonymeter`, `scikit-learn` (gated; engine core stays slim).
- `src/decoy_engine/generators/generator.py` — accept `compare_to_training=True` kwarg; return `SynthReport` alongside the output.

## Engine changes

### 1. `quality/privacy.py`

```python
def new_row_synthesis(synth: pd.DataFrame, train: pd.DataFrame) -> float:
    """Fraction of synth rows that are NOT exact copies of any train row."""
    # Hash each row's tuple; set membership over the train hashes.
    train_set = set(map(tuple, train.itertuples(index=False, name=None)))
    synth_tuples = [tuple(r) for r in synth.itertuples(index=False, name=None)]
    return 1.0 - sum(1 for t in synth_tuples if t in train_set) / len(synth_tuples)


def dcr_overfitting(synth: pd.DataFrame, train: pd.DataFrame, holdout: pd.DataFrame) -> float:
    """Gower DCR ratio: synth↔train vs synth↔holdout.

    Returns the share of synth rows whose nearest neighbor is in train
    rather than holdout. 0.5 = balanced (good); → 1.0 = overfit / leaky.
    """
    # Use a vectorized Gower implementation; for >10k rows sample down.
    ...


def anonymeter_risks(synth: pd.DataFrame, train: pd.DataFrame, holdout: pd.DataFrame):
    """Wrap the Anonymeter package's singling-out, linkability, inference
    evaluators. Returns a dict with each risk + 95% CI from Anonymeter's
    own bootstrap.
    """
    from anonymeter.evaluators import (
        SinglingOutEvaluator, LinkabilityEvaluator, InferenceEvaluator,
    )
    # 3 evaluator runs; each returns risk + CI.
    ...
```

All three metrics are optional (the `anonymeter` import is gated; the function returns `None` with a `notes=["anonymeter not installed"]` if absent — the engine core stays slim).

### 2. `quality/utility.py`

```python
def tstr(synth_train: pd.DataFrame, real_test: pd.DataFrame, target: str) -> float:
    """Train Synthetic, Test Real: fit a classifier on synth, score on real."""
    ...


def trtr(real_train: pd.DataFrame, real_test: pd.DataFrame, target: str) -> float:
    """Baseline."""
    ...


def pmse(synth: pd.DataFrame, real: pd.DataFrame) -> float:
    """Propensity MSE — train CART to distinguish synth vs real, measure deviation."""
    ...
```

Fix the model (single sklearn `GradientBoostingClassifier` for TSTR/TRTR, `DecisionTreeClassifier(max_depth=4)` for pMSE). One model class per metric — predictable wall-clock, comparable across runs.

### 3. `SynthReport` dataclass

```python
# quality/report.py — extend
@dataclass
class SynthReport(QualityReport):           # composes Block 7's QualityReport
    new_row_synthesis: float
    dcr_overfitting: Optional[float] = None
    anonymeter: Optional[dict] = None        # {"singling_out": …, "linkability": …, "inference": …}
    tstr_minus_trtr: Optional[float] = None
    pmse: Optional[float] = None
```

Returned from `DataGenerator.run(...)` when `compare_to_training` and a holdout slice are provided.

### 4. Caveat handling

DCR result alone is not a verdict. The engine emits a `notes` entry:

> "DCR is a sanity check, not a privacy guarantee. Yao et al. 2025 show DCR underestimates risk vs membership inference; cross-check with Anonymeter's inference risk above."

The platform/UI copy must render this verbatim — see the pairing platform doc.

## Tests to add

`tests/unit/test_synth_privacy.py`:
- **Leakage:** synth is a copy of train → `new_row_synthesis == 0.0`; `dcr_overfitting → 1.0`.
- **Independent:** synth drawn from same distribution but different seed → `dcr_overfitting ≈ 0.5`.
- **Anonymeter inference risk:** gate on `anonymeter` being importable; skip otherwise.

`tests/unit/test_synth_utility.py`:
- TSTR with leak-copy synth ≈ TRTR.
- TSTR with random-noise synth « TRTR.

## Verification

1. `pytest tests/unit/test_synth_privacy.py -v` — green (skip Anonymeter tests if extras not installed).
2. `pytest tests/unit/test_synth_utility.py -v` — green.
3. Manual: run `DataGenerator` on a tiny fixture, confirm `SynthReport.anonymeter.inference < 0.5` for a properly-randomized output.

## Open questions

- Holdout sourcing: today `DataGenerator` doesn't carry the training data. For Block 10's evaluations we need a train + holdout split. Options:
  1. Make it the caller's responsibility (`generate(train_df=…, holdout_df=…)`).
  2. Engine takes a single training df and does the 80/20 split itself.
  Option 1 is more flexible; the CLI / platform layer wraps option 2 as a default. Go with 1.

- Sample-down for large datasets: cap each evaluator at 50k rows (`synth.sample(50_000, random_state=0)`); document.
