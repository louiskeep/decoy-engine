"""classify_fields: public ML3.1 column-type classification function.

Loads the model pack via ``ModelPackLoader``, builds ML1 aggregate features
per column, and returns per-column classification results.

Privacy invariant (ml-benchmarking-and-privacy.md §B.4):
    Output contains ONLY: label, calibrated_confidence, band, and model
    provenance metadata.  No raw cell values are ever included in the
    returned dict.  The feature pipeline (``build_column_features`` +
    ``flatten_features``) already enforces this at the featurizer boundary;
    this function adds no new surface.

Off-by-default (ml-benchmarking-and-privacy.md §B.5):
    The function returns ``None`` when ML is disabled (``DECOY_ML_DISABLED=1``)
    or when the pack cannot be loaded (missing, corrupt, wrong schema).
    Callers MUST treat ``None`` as "use the deterministic baseline" --
    a failed pack load must NEVER crash the host process.

Determinism:
    Given the same ``DataFrame`` + same pack, ``classify_fields`` always
    returns the same result.  The feature builder is deterministic (no
    random sampling), and the model uses a fixed seed (``TRAIN_SEED=42``).
    No external state is written.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas  # classify_fields signature only; imported lazily at call-time

_log = logging.getLogger(__name__)

#: Pack id of the committed default artifact.
_DEFAULT_PACK_ID = "lgbm-v1"

#: Source-of-truth location of the committed pack in the repo tree. Used for
#: source checkouts and editable installs, where the wheel force-include (which
#: copies the pack under ``decoy_engine/model_packs/``) has not run.
_SOURCE_PACK = Path(__file__).parents[4] / "docs" / "v2" / "ml" / "packs" / _DEFAULT_PACK_ID


def _default_pack_dir() -> Path:
    """Resolve the default lgbm-v1 pack directory across install shapes.

    A built wheel force-includes the pack under the installed package at
    ``decoy_engine/model_packs/lgbm-v1`` (see pyproject ``force-include``); that
    copy is resolved here via ``importlib.resources`` so ``classify_fields``
    works from an installed wheel. Before this (DE-07) a clean wheel omitted the
    pack entirely and the classifier silently returned ``None``.

    Source checkouts and editable installs have no packaged copy, so fall back
    to the repo-tree source-of-truth under ``docs/v2/ml/packs`` -- the same
    bytes the golden gate and provenance tests pin.
    """
    try:
        from importlib.resources import files

        packaged = files("decoy_engine") / "model_packs" / _DEFAULT_PACK_ID
        packaged_path = Path(str(packaged))
        if packaged_path.is_dir():
            return packaged_path
    except Exception:  # resolver must never crash the host (see below)
        # This resolver runs BEFORE ModelPackLoader, outside load_with_fallback's
        # never-crash guard, so any failure here (zip import, namespace-package
        # MultiplexedPath raising FileNotFoundError, an unreadable site-packages
        # ancestor) must fall through to the source tree rather than propagate.
        # The §B.5 invariant is that a pack-resolution failure degrades to the
        # deterministic baseline, never crashes the caller.
        pass
    return _SOURCE_PACK


def classify_fields(
    df: pandas.DataFrame,
    *,
    pack_dir: Path | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Classify each column in *df* using the trained LightGBM model pack.

    This is the engine library entry point for ML field recognition (ML3.1).
    The HTTP classify-fields endpoint and the platform review UI consume this
    function; they live in the platform repo (ML3.3, frontend lane).

    Parameters
    ----------
    df:
        A pandas ``DataFrame``.  Each column is classified independently.
        The raw cell values are NEVER included in the output (§B.4).
    pack_dir:
        Path to the ``decoy-model-pack/v1`` directory.  Defaults to the
        committed ``docs/v2/ml/packs/lgbm-v1`` artifact.

    Returns
    -------
    dict[str, dict] | None
        Per-column classification results, keyed by column name.  Each value
        is a dict with keys:

          ``label`` (str | None)
            Predicted field type (e.g. ``"ssn"``, ``"pan"``), or ``None``
            when the calibrated confidence is below the operating threshold.
          ``calibrated_confidence`` (float)
            Calibrated ``predict_proba`` maximum score, in [0, 1].
          ``band`` (str)
            ``"high"`` (>= 0.95), ``"review"`` (0.70-0.95), or ``"low"``.
            NOTE: for lgbm-v1 the ``"high"`` band is not triggered at this
            corpus scale (see ``eval/bands.py`` PROVISIONAL measurements).
          ``model_pack_id`` (str)
            Pack identifier from the manifest (e.g. ``"lgbm-v1"``).
          ``model_pack_version`` (str)
            Semantic version from the manifest (e.g. ``"0.1.0"``).
          ``feature_schema_version`` (str)
            Feature schema the pack was trained against (e.g. ``"ml1-v1"``).

        Returns ``None`` when ML is disabled (``DECOY_ML_DISABLED=1``) or
        when the pack cannot be loaded (missing, corrupt, schema mismatch).
        Callers MUST treat ``None`` as "fall back to the deterministic regex
        baseline" and MUST NOT raise from ``None``.

    Notes
    -----
    - Empty ``DataFrame`` (0 columns) returns an empty dict (not ``None``).
    - Columns with all-null values are classified from their header tokens
      and dtype alone; the feature builder handles nulls gracefully.
    """
    import numpy as np

    from decoy_engine.storm.eval.bands import classify_band
    from decoy_engine.storm.eval.fixtures import NO_DETECTOR
    from decoy_engine.storm.features.builder import build_column_features
    from decoy_engine.storm.model_pack.featurizer import flatten_features
    from decoy_engine.storm.model_pack.loader import ModelPackLoader

    # DE-04 (Option C): the resolved default pack is first-party (shipped in the
    # wheel, SHA-256 verified) and is trusted; a caller-supplied pack_dir crosses
    # an untrusted boundary and must carry a verifiable signature (the loader
    # fail-closes it before joblib.load runs).
    if pack_dir is None:
        resolved_dir = _default_pack_dir()
        trusted = True
    else:
        resolved_dir = pack_dir
        trusted = False

    loader = ModelPackLoader(resolved_dir, trusted=trusted)
    pack = loader.load_with_fallback()
    if pack is None:
        # ML disabled or pack unavailable; caller uses the regex baseline.
        _log.debug("classify_fields: pack unavailable; returning None.")
        return None

    vec = pack["vec"]
    clf = pack["clf"]
    manifest = pack["manifest"]
    threshold: float = manifest.operating_threshold

    results: dict[str, dict[str, Any]] = {}

    for col_name in df.columns:
        series = df[col_name]
        feats = build_column_features(series, str(col_name))
        flat = flatten_features(feats.to_dict())

        X_mat = vec.transform([flat])
        proba: np.ndarray = clf.predict_proba(X_mat)[0]
        max_idx = int(np.argmax(proba))
        max_prob = float(proba[max_idx])
        predicted_cls: str = clf.classes_[max_idx]

        if max_prob < threshold:
            label = None
            band = "low"
        else:
            label = predicted_cls if predicted_cls != NO_DETECTOR else None
            band = classify_band(max_prob)

        # §B.4: output contains metadata and statistics only -- NO raw cell values.
        results[str(col_name)] = {
            "label": label,
            "calibrated_confidence": round(max_prob, 4),
            "band": band,
            "model_pack_id": manifest.pack_id,
            "model_pack_version": manifest.version,
            "feature_schema_version": manifest.feature_schema_version,
        }

    return results
