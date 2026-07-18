"""Feature flattening for the LightGBM column classifier (ML2.2 / §B.4).

Converts a ``ColumnFeatures.to_dict()`` snapshot into a flat dict that
``sklearn.feature_extraction.DictVectorizer`` can ingest directly.

Privacy invariant (ml-benchmarking-and-privacy.md §B.4):
    The output dict contains ONLY aggregate column statistics, dtype signals,
    shape signatures, and regex/checksum pass rates.  No raw cell values
    appear.  ``column_name`` is intentionally excluded; only the tokenised
    header (``header_tokens``) is used, via binary indicator features for each
    known token.

Header token strategy:
    Each token in ``header_tokens`` becomes an entry
    ``"hdr_{token}" -> 1.0``.  ``DictVectorizer.fit`` on the training set
    determines which tokens become features; unseen tokens in the test set are
    silently ignored (all-zero response), which is the correct behaviour for
    cryptic headers that only appear at inference time.

Established methodology:
    DictVectorizer (sklearn §6.2.3) handles mixed numeric / categorical dicts
    without explicit one-hot encoding: string values become binary indicator
    columns named ``"{key}={value}"``, numeric values pass through directly.
    This is the idiomatic sklearn feature-extraction step for heterogeneous
    tabular dicts.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.storm.features.header_lexicon import roles_from_tokens


def flatten_features(feats: dict[str, Any]) -> dict[str, Any]:
    """Flatten a ``ColumnFeatures.to_dict()`` snapshot to a DictVectorizer-ready dict.

    Parameters
    ----------
    feats:
        Output of ``ColumnFeatures.to_dict()`` (or equivalently,
        ``build_column_features(series, col_name).to_dict()``).

    Returns
    -------
    dict
        Flat dict mapping feature name -> float or string value.  String
        values will be one-hot encoded by ``DictVectorizer``; numeric values
        are passed through as-is.

    Notes
    -----
    - ``column_name`` is EXCLUDED (data-leakage risk; header_tokens captures
      the relevant signal in a generalised form).
    - ``dtype_raw`` is INCLUDED as a categorical string feature.
    - ``shape.dominant_mask`` is included as a categorical string; the
      ``DictVectorizer`` encodes it per-mask-string seen during training.
    - ``None`` values are replaced with safe defaults (0.0 for numerics,
      ``"unknown"`` for categoricals) so no NaN enters the model matrix.
    """
    flat: dict[str, Any] = {}

    # ── Numeric scalar fields ─────────────────────────────────────────────────
    for key in (
        "row_count",
        "non_null_count",
        "sample_size",
        "null_rate",
        "distinct_count",
        "distinct_rate",
        "unique_rate",
        "shannon_entropy",
        "normalized_entropy",
    ):
        flat[key] = float(feats.get(key) or 0.0)

    # ── Categorical / string fields (DictVectorizer creates one-hot) ──────────
    flat["inferred_type"] = str(feats.get("inferred_type") or "unknown")
    flat["dtype_raw"] = str(feats.get("dtype_raw") or "unknown")
    flat["alphabet"] = str(feats.get("alphabet") or "unknown")
    flat["casing"] = str(feats.get("casing") or "unknown")
    flat["value_set_size_class"] = str(feats.get("value_set_size_class") or "unknown")
    flat["numeric_range_class"] = str(feats.get("numeric_range_class") or "unknown")

    # ── Char-class fractions dict (digit / upper / lower / whitespace / other) ─
    for k, v in (feats.get("char_class_fractions") or {}).items():
        flat[f"char_{k}"] = float(v or 0.0)

    # ── Per-detector regex match rates ────────────────────────────────────────
    for k, v in (feats.get("regex_signals") or {}).items():
        flat[f"regex_{k}"] = float(v or 0.0)

    # ── Standalone checksum pass rates ────────────────────────────────────────
    for k, v in (feats.get("checksum_pass_rates") or {}).items():
        flat[f"chk_{k}"] = float(v or 0.0)

    # ── Shape signature ───────────────────────────────────────────────────────
    shape = feats.get("shape") or {}
    # dominant_mask as categorical string; None becomes empty string (rare mask)
    flat["shape_dominant_mask"] = str(shape.get("dominant_mask") or "")
    flat["shape_dominant_mask_rate"] = float(shape.get("dominant_mask_rate") or 0.0)
    flat["shape_min_length"] = float(shape.get("min_length") or 0)
    flat["shape_max_length"] = float(shape.get("max_length") or 0)
    flat["shape_mean_length"] = float(shape.get("mean_length") or 0.0)

    # ── Header token indicator features ──────────────────────────────────────
    # Each token in header_tokens becomes "hdr_{token}" -> 1.0.
    # Unknown tokens at inference time are ignored by DictVectorizer (all-zero).
    header_tokens = feats.get("header_tokens") or []
    for token in header_tokens:
        flat[f"hdr_{token}"] = 1.0

    # ── Header role features (CH-1 lexicon + CH-2 fuzzy) ──────────────────────
    # Canonical roles give a vocabulary-stable header channel that survives
    # cryptic/abbreviated headers: `diagnosis_code` (training) and `dx_cd`
    # (inference) both emit `role_icd10`, so DictVectorizer learns the role at
    # train time and the model can still use the header when the raw token is
    # novel. Unlike hdr_{token}, an unseen role never appears, so no new feature
    # leaks in at inference. Emit nothing when no token maps (content-only).
    for role in roles_from_tokens(header_tokens):
        flat[f"role_{role}"] = 1.0

    return flat
