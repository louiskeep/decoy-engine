"""Held-out split utility for the ML field-recognition harness (ML0 / §A.3).

Implements the leakage guard from ml-benchmarking-and-privacy.md §A.3:
split with ``StratifiedGroupKFold``, group = the unique PII value string,
so the same value cannot appear in both train and test. This prevents a
future model memorising strings instead of learning column-shape patterns,
the dominant leakage failure mode for tabular-column classifiers.

This is scaffolding for ML2.2: the regex baseline has no training phase and
does not use this utility. It is defined here so the interface is reviewed
and tested before the model is built.

Requires: scikit-learn (``[ml]`` optional extra):
    pip install -e '.[ml]'

Source: sklearn §3.1.2.4 ``StratifiedGroupKFold``:
    "ensures the same group is not represented in both testing and training
    sets; StratifiedGroupKFold preserves class balance and group separation."
sklearn §12.2 data-leakage warning: "any preprocessing steps must be
    carried out using the information from training data only."
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pandas as pd


def make_group_key(series: pd.Series) -> str:
    """Return the group key for one fixture column.

    Group key = lexicographically sorted unique non-null values joined by
    ``|``.  Two columns that share any value get the same group; the split
    then keeps them on the same side of the train/test boundary so the model
    cannot memorise the values.

    For the regex baseline the key is informational (the baseline has no
    training phase).  At ML2 it becomes the ``groups`` argument to
    ``StratifiedGroupKFold``.
    """
    unique_vals = sorted(str(v) for v in series.dropna().unique())
    return "|".join(unique_vals)


def make_split_inputs(
    fixtures: list[Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Convert labeled fixtures to (X, y, groups) for StratifiedGroupKFold.

    Parameters
    ----------
    fixtures:
        A list of ``LabeledFixture`` objects (``decoy_engine.storm.eval``).

    Returns
    -------
    X : list of dicts
        One feature dict per column, from ``build_column_features``.
        Features are aggregate column statistics -- no raw cell values
        (ml-benchmarking-and-privacy.md §B.4).
    y : list of str
        Ground-truth semantic-type label per column (the truth_label from
        the fixture).
    groups : list of str
        Group key per column -- the sorted unique PII values in that column
        (via ``make_group_key``).  ``StratifiedGroupKFold`` uses this to
        keep the same value on one side of the train/test boundary.

    Notes
    -----
    Fit all preprocessing/feature-scaling on the train fold only (§A.3).
    The ``X`` dicts here are raw features; the caller is responsible for
    fitting any scaler on the train split before transforming the test split.
    """
    from decoy_engine.storm.features import build_column_features

    X: list[dict[str, Any]] = []
    y: list[str] = []
    groups: list[str] = []

    for fx in fixtures:
        for col, label in fx.labels.items():
            feats = build_column_features(fx.df[col], col)
            X.append(feats.to_dict())
            y.append(label)
            groups.append(make_group_key(fx.df[col]))

    return X, y, groups


def held_out_split(
    X: list[Any],
    y: list[str],
    groups: list[str],
    n_splits: int = 5,
    random_state: int = 42,
) -> Iterator[tuple[list[int], list[int]]]:
    """Yield (train_indices, test_indices) from StratifiedGroupKFold.

    Uses ``sklearn.model_selection.StratifiedGroupKFold`` so the same PII
    value string cannot appear in both train and test (§A.3 leakage guard).

    Parameters
    ----------
    X:
        Feature inputs (length N).
    y:
        Labels (length N).
    groups:
        Group keys (length N), one per column.
    n_splits:
        Number of CV folds (default 5).
    random_state:
        Random seed for reproducibility (default 42).

    Yields
    ------
    (train_indices, test_indices): tuple of lists of ints

    Raises
    ------
    ImportError
        If scikit-learn is not installed.  Install via ``pip install -e '.[ml]'``.

    Notes
    -----
    - This is **scaffolding**: the regex baseline does not call this function.
      ML2.2 (LightGBM classifier) will call it during model training/eval.
    - Calibration (reliability curve + Brier score, ``CalibratedClassifierCV``
      isotonic) is REQUIRED at ML2 when a probabilistic model exists
      (ml-benchmarking-and-privacy.md §A.4).  The regex baseline has no
      ``predict_proba``, so calibration does not apply here.
    """
    try:
        from sklearn.model_selection import StratifiedGroupKFold  # type: ignore[import]
    except ImportError as e:
        raise ImportError(
            "scikit-learn is required for held_out_split. Install it via: pip install -e '.[ml]'"
        ) from e

    splitter = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=random_state)
    for train_idx, test_idx in splitter.split(X, y, groups):
        yield list(train_idx), list(test_idx)
