"""decoy_engine.storm.features - deterministic column feature builder (BF2 / ML1).

Standalone, JSON-serializable feature vectors for downstream field
recognition. Namespaced to the storm subpackage on purpose: this is not
on the public run path and is intentionally NOT re-exported from
``decoy_engine.__init__`` while it is pre-model groundwork.
"""

from decoy_engine.storm.features.builder import build_column_features, tokenize_header
from decoy_engine.storm.features.types import ColumnFeatures, ShapeSignature

__all__ = [
    "ColumnFeatures",
    "ShapeSignature",
    "build_column_features",
    "tokenize_header",
]
