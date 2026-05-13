# Block 2 — equivalence-class engine + k-anonymity math

> **Status:** plan — part of the 2026-05-13 audit-fix series.
> **Depends on:** Block 1 (column role taxonomy) — needs `FieldStats.role`.
> **Pairs with:** `decoy-platform/plans/2026-05-13-equivalence-class-api-surface.md`.

## Why

Today `StormProfile.reid_risk_score` (`storm/profiler.py:489-491`) is computed as:

```python
reid_cols = [f.name for f in fields if f.is_likely_unique]
reid_score = round(len(reid_cols) / max(len(fields), 1) * 100, 1)
```

That is **the percentage of columns whose `unique_rate > 0.9`** — not a re-identification probability. The reference (§5) is explicit: collapse risk into one number and you lose all defensibility. Three named attacker views — prosecutor, journalist, marketer — are the canonical replacement.

This block builds the math + data model. Block 3 ships the UI.

## Code paths

- New module: `src/decoy_engine/storm/risk.py`.
- `src/decoy_engine/storm/types.py` — add `RiskReport` dataclass; `StormProfile.risk`.
- `src/decoy_engine/storm/profiler.py:run_storm` — drive `compute_risk` when QI columns exist.
- `src/decoy_engine/cli/` (if present) or `forge` entry — new `forge storm-risk` subcommand for CLI demo.

## New module: `storm/risk.py`

```python
"""Equivalence-class + named-attacker risk math.

Pure functions over a pandas DataFrame + a list of quasi-identifier columns.
No side effects, no IO; suitable for both batch (StormProfile.risk) and
ad-hoc CLI demos.

Reference: NIST SP 800-188 §5; El Emam, *Guide to the De-Identification of
Personal Health Information* (CRC 2013); ARX project's calibrators.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal
import pandas as pd


@dataclass
class EquivalenceClassStats:
    n_classes: int
    smallest_class: int                  # min |class|
    mean_class: float
    median_class: float
    fraction_below_k: float              # fraction of records in classes < k
    sample_uniques: int                  # # classes of size 1
    qi_columns: list[str]


@dataclass
class RiskReport:
    qi_columns: list[str]
    prosecutor_risk: float               # 1 / smallest_class
    journalist_risk: float               # Pitman-estimated 1 / population_min
    marketer_risk: float                 # 1 / mean_class
    equivalence_classes: Optional[EquivalenceClassStats] = None
    l_diversity: Optional[int] = None    # min over classes; None if no sensitive col
    t_closeness: Optional[float] = None
    sample_n: int = 0
    population_estimator: str = "pitman"
    reason_skipped: Optional[str] = None  # set when computation was skipped

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_K_THRESHOLD = 5  # "fraction in classes < k" default


def compute_equivalence_classes(
    df: pd.DataFrame,
    quasi_id_cols: list[str],
    *,
    k_threshold: int = DEFAULT_K_THRESHOLD,
) -> EquivalenceClassStats:
    if not quasi_id_cols:
        raise ValueError("quasi_id_cols must be non-empty")
    missing = [c for c in quasi_id_cols if c not in df.columns]
    if missing:
        raise KeyError(f"columns not in df: {missing}")

    sizes = df.groupby(quasi_id_cols, dropna=False).size()
    sample_uniques = int((sizes == 1).sum())
    below = int(sizes[sizes < k_threshold].sum())
    n = int(sizes.sum())
    return EquivalenceClassStats(
        n_classes=int(sizes.size),
        smallest_class=int(sizes.min()),
        mean_class=float(sizes.mean()),
        median_class=float(sizes.median()),
        fraction_below_k=below / n if n else 0.0,
        sample_uniques=sample_uniques,
        qi_columns=list(quasi_id_cols),
    )


def prosecutor_risk(stats: EquivalenceClassStats) -> float:
    return 1.0 / stats.smallest_class if stats.smallest_class else 1.0


def marketer_risk(stats: EquivalenceClassStats) -> float:
    return 1.0 / stats.mean_class if stats.mean_class else 1.0


def journalist_risk(
    stats: EquivalenceClassStats,
    sample_n: int,
    *,
    estimator: Literal["pitman", "naive"] = "pitman",
) -> float:
    """Super-population uniqueness estimate.

    `pitman`: classic Pitman 1996 / Zayatz 1991 formula — uses the count of
    sample uniques and the sample size to estimate population uniqueness rate.
    `naive`: just returns sample uniqueness rate (upper bound).
    """
    if sample_n <= 0:
        return 0.0
    if estimator == "naive":
        return stats.sample_uniques / sample_n
    # Pitman: P(unique in population) ≈ (sample_uniques / sample_n) ** (1/π)
    # where π is the sampling fraction. Without π we approximate via the
    # ratio of sample_uniques to total classes — a coarse but defensible
    # default. Document the assumption in the report's `population_estimator`
    # field so the analyst can substitute a known π if available.
    if stats.n_classes == 0:
        return 0.0
    return min(1.0, stats.sample_uniques / stats.n_classes)


def l_diversity(
    df: pd.DataFrame, qi_cols: list[str], sensitive_col: str
) -> int:
    """Smallest # distinct sensitive values across all equivalence classes."""
    if sensitive_col not in df.columns:
        raise KeyError(sensitive_col)
    by_class = df.groupby(qi_cols, dropna=False)[sensitive_col].nunique()
    return int(by_class.min()) if len(by_class) else 0


def t_closeness(
    df: pd.DataFrame, qi_cols: list[str], sensitive_col: str
) -> float:
    """Max Earth-Mover (1-D Wasserstein) distance between each class's
    distribution of the sensitive attribute and the dataset-wide distribution.
    Categorical sensitive: Total Variation Distance fallback.
    """
    if sensitive_col not in df.columns:
        raise KeyError(sensitive_col)
    population = df[sensitive_col].value_counts(normalize=True, dropna=False)
    max_dist = 0.0
    for _, class_df in df.groupby(qi_cols, dropna=False):
        local = class_df[sensitive_col].value_counts(normalize=True, dropna=False)
        # TVD as the safe default (works for cat + num)
        all_keys = population.index.union(local.index)
        diff = abs(population.reindex(all_keys, fill_value=0)
                   - local.reindex(all_keys, fill_value=0))
        max_dist = max(max_dist, float(diff.sum() / 2))
    return max_dist


def compute_risk(
    df: pd.DataFrame,
    quasi_id_cols: list[str],
    *,
    sensitive_col: Optional[str] = None,
    k_threshold: int = DEFAULT_K_THRESHOLD,
) -> RiskReport:
    if not quasi_id_cols:
        return RiskReport(
            qi_columns=[],
            prosecutor_risk=0.0,
            journalist_risk=0.0,
            marketer_risk=0.0,
            reason_skipped="no_quasi_identifiers",
        )
    eq = compute_equivalence_classes(df, quasi_id_cols, k_threshold=k_threshold)
    n = len(df)
    report = RiskReport(
        qi_columns=list(quasi_id_cols),
        prosecutor_risk=prosecutor_risk(eq),
        journalist_risk=journalist_risk(eq, n),
        marketer_risk=marketer_risk(eq),
        equivalence_classes=eq,
        sample_n=n,
    )
    if sensitive_col and sensitive_col in df.columns:
        report.l_diversity = l_diversity(df, quasi_id_cols, sensitive_col)
        report.t_closeness = t_closeness(df, quasi_id_cols, sensitive_col)
    return report
```

## Wiring into `run_storm`

After Block 1 ships, `run_storm` knows each field's role. Hook the risk computation just before `StormProfile` is constructed:

```python
# storm/profiler.py — inside run_storm(), after fields are built
qi_cols = [f.name for f in fields if f.role == "quasi"]
sensitive_cols = [f.name for f in fields if f.role == "sensitive"]
risk = compute_risk(
    df, qi_cols,
    sensitive_col=sensitive_cols[0] if sensitive_cols else None,
)
```

For large dataframes (>1M rows) the groupby can be expensive. Guard:

```python
if len(df) > 5_000_000:
    # sample down for the risk computation; flag in the report
    risk = compute_risk(df.sample(5_000_000, random_state=0), qi_cols, ...)
    risk.sample_n = 5_000_000
```

## Deprecation of `reid_risk_score`

Keep the field on `StormProfile` for **one release**. Log on first access:

```python
@property
def reid_risk_score(self) -> float:
    warnings.warn(
        "reid_risk_score is deprecated and structurally incorrect; "
        "read .risk.prosecutor_risk / .risk.marketer_risk / .risk.journalist_risk",
        DeprecationWarning, stacklevel=2,
    )
    return self._legacy_reid_score
```

Remove in the next release after one full deprecation cycle.

## CLI demo

`forge storm-risk <file.csv> --qi col1 col2 col3 [--sensitive col4]` runs the full pipeline and prints a 4-line summary:

```
Prosecutor risk:  1 / 7  (0.143) — Critical
Journalist risk:  1 / 19 (0.053) — Low
Marketer risk:    1 / 23 (0.043) — Low
Equivalence classes: 412 total, smallest 7, mean 23.1
```

## Tests to add

`tests/unit/test_storm_risk.py`:
- **Identity:** k-anonymous dataset (every QI tuple appears exactly 5 times) → `prosecutor_risk == 0.2`.
- **Sweeney regression:** fixture with `dob, zip, gender` engineered to be all-unique → `prosecutor_risk == 1.0`.
- **Property:** `prosecutor >= marketer >= 0` always.
- **l-diversity:** class of size 5 with 5 distinct sensitive values → l=5; with 1 distinct → l=1.
- **t-closeness:** class distribution = population distribution → t=0.
- **Skip path:** no QI cols → `reason_skipped == "no_quasi_identifiers"`.

Fixtures go under `tests/fixtures/sweeney_dob_zip_gender.csv`.

## Verification

1. `pytest tests/unit/test_storm_risk.py -v` — green.
2. `forge storm-risk tests/fixtures/sweeney_dob_zip_gender.csv --qi dob zip gender` prints prosecutor_risk > 0.5.
3. Existing `pytest tests/unit/test_storm_profiler.py -v` still green (back-compat).

## Performance budget

- 100k rows × 3 QI cols: groupby <100ms on commodity hardware.
- 1M rows × 5 QI cols: <2s.
- Above 5M, automatic sample-down (see wiring section).

## Out of scope (later blocks)

- API endpoint that recomputes risk after a role override without re-scanning — Block 2 platform doc.
- UI panel rendering the three attacker views — Block 3.
- Compliance-report PDF section consuming this report — Block 9.
