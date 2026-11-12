"""decoy_engine.profiling.model_pack - LightGBM field-recognition pack (ML2).

Provides the model-pack format, loader, and featurizer for the optional
LightGBM column-type classifier (ml-benchmarking-and-privacy.md, Sprint B).

ML is off by default, on-prem only, with no cloud inference. Only the status and strict-loading
surface (`classifier_status`, `load_detectors`) is re-exported from `decoy_engine`; the pack
format, loader and featurizer stay package-level.

scikit-learn and lightgbm are base dependencies. lightgbm also needs ``libomp`` on macOS
(``brew install libomp``) and ``libgomp1`` on Linux.
"""

from decoy_engine.profiling.model_pack._detectors import (
    DetectorSet,
    classifier_status,
    load_detectors,
)
from decoy_engine.profiling.model_pack.classify import (
    classify_fields,
    classify_frame,
    default_pack_loader,
)
from decoy_engine.profiling.model_pack.featurizer import flatten_features
from decoy_engine.profiling.model_pack.loader import (
    ModelPackLoader,
    ModelPackLoadError,
)
from decoy_engine.profiling.model_pack.provenance import (
    derive_pack_signing_key,
    sign_manifest,
    sign_pack,
    verify_manifest,
)
from decoy_engine.profiling.model_pack.types import ModelPackManifest

__all__ = [
    "DetectorSet",
    "ModelPackLoadError",
    "ModelPackLoader",
    "ModelPackManifest",
    "classifier_status",
    "classify_fields",
    "classify_frame",
    "default_pack_loader",
    "derive_pack_signing_key",
    "flatten_features",
    "load_detectors",
    "sign_manifest",
    "sign_pack",
    "verify_manifest",
]
