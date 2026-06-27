"""Model-pack manifest and provenance types (ML2.1).

The ``decoy-model-pack/v1`` manifest format tracks:
  - identity (id, version, format_version)
  - integrity (sha256 of the serialised model weights)
  - compatibility (feature_schema_version: must match ML1 feature builder)
  - governance (signing_key_ref, license_note)

Source: ml2.1-model-pack-loading.md; ml-benchmarking-and-privacy.md §B.7.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: Frozen feature-schema version this pack was trained against.
#: Must stay in sync with decoy_engine.storm.features.builder (ML1).
#: Bump this if the ColumnFeatures dict shape changes in a breaking way.
FEATURE_SCHEMA_VERSION = "ml1-v1"

#: Pack format identifier embedded in every manifest.
PACK_FORMAT = "decoy-model-pack/v1"


@dataclass
class ModelPackManifest:
    """Manifest for a ``decoy-model-pack/v1`` model artifact.

    All fields are plain JSON-serializable types so the manifest can be
    written as ``manifest.json`` alongside the model weights.

    Fields
    ------
    pack_id:
        Unique stable identifier for this pack (e.g. ``lgbm-v1``).
    version:
        Semantic version string of the trained artifact (e.g. ``0.1.0``).
    format_version:
        Always ``"decoy-model-pack/v1"``; the loader rejects any other value.
    feature_schema_version:
        The ``FEATURE_SCHEMA_VERSION`` the pack was trained against.  The
        loader rejects packs whose value does not match the constant above.
    sha256:
        Hex SHA-256 of the ``model.joblib`` weights blob.  Verified on load;
        mismatch -> ``ModelPackLoadError``.
    signing_key_ref:
        Reference to the key used to sign the pack (ML3.2, provenance sprint).
        ``"unsigned"`` for development packs not yet through the signing gate.
    license_note:
        Human-readable note about training data provenance and licence.
    training_seed:
        Integer random seed used for the train/test split and the model
        (determinism requirement, ml-benchmarking-and-privacy.md §A.7).
    calibration_method:
        Either ``"isotonic"`` (>= ~1000 calibration samples) or ``"sigmoid"``.
    operating_threshold:
        Calibrated probability below which the top predicted class is suppressed
        and the column is left unlabelled (set from the FN:FP cost ratio).
    eval_report_hash:
        SHA-256 of the canonical ``lightgbm-report.json`` produced at training
        time.  Ties the weights to the frozen metric evidence (§B.7).
    extra:
        Arbitrary key-value pairs for forward-compatibility.
    """

    pack_id: str
    version: str
    format_version: str = PACK_FORMAT
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    sha256: str = ""
    signing_key_ref: str = "unsigned"
    license_note: str = (
        "Trained on fully synthetic data (no real PII). "
        "Detection only -- not a de-identification tool. "
        "See corpus-datasheet.md for provenance."
    )
    training_seed: int = 42
    calibration_method: str = "sigmoid"
    operating_threshold: float = 0.167
    eval_report_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    #: HMAC-SHA256 of the canonical manifest JSON (all fields except this one).
    #: Set by sign_manifest(); verified by the loader when DECOY_PACK_SIGNING_KEY
    #: is configured (ML3.2).  Empty string = unsigned.
    manifest_hmac: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelPackManifest:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        base = {k: v for k, v in d.items() if k in known}
        return cls(**base)
