"""decoy_engine.storm.model_pack - LightGBM field-recognition pack (ML2).

Provides the model-pack format, loader, and featurizer for the optional
LightGBM column-type classifier (ml-benchmarking-and-privacy.md, Sprint B).

Not on the public run path: this is gated tooling (ML off by default;
on-prem only; no cloud inference). NOT re-exported from decoy_engine.__init__.

Requires the ``[ml]`` optional extras:
    pip install -e '.[ml]'
"""

from decoy_engine.storm.model_pack.classify import classify_fields
from decoy_engine.storm.model_pack.featurizer import flatten_features
from decoy_engine.storm.model_pack.loader import (
    ModelPackLoader,
    ModelPackLoadError,
)
from decoy_engine.storm.model_pack.provenance import (
    derive_pack_signing_key,
    sign_manifest,
    sign_pack,
    verify_manifest,
)
from decoy_engine.storm.model_pack.types import ModelPackManifest

__all__ = [
    "ModelPackLoadError",
    "ModelPackLoader",
    "ModelPackManifest",
    "classify_fields",
    "derive_pack_signing_key",
    "flatten_features",
    "sign_manifest",
    "sign_pack",
    "verify_manifest",
]
