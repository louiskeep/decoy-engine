"""Strategy handlers for the pandas execution adapter (engine-v2 S9).

`SCALAR_HANDLERS` maps a strategy name to a handler instance. Slice 2a ships the
three no-backend strategies (passthrough, redact, truncate); later slices add
the keyed/backend strategies (faker, hash, date_shift, bucketize, categorical,
shuffle, formula, fpe) re-keyed onto S3's `derive`/`derive_index` + S5's
`PoolSampler`. SP-10 adds `derived` (closed-grammar row-context expression).
SP-08b adds `bucket_perturb` (coarse time-bucket datetime generalization).
"""

from __future__ import annotations

from decoy_engine.execution._adapter import StrategyHandler
from decoy_engine.execution._strategies._bucket_perturb import BucketPerturbStrategyHandler
from decoy_engine.execution._strategies._bucketize import BucketizeStrategyHandler
from decoy_engine.execution._strategies._categorical import CategoricalStrategyHandler
from decoy_engine.execution._strategies._code_set import CodeSetHandler
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.execution._strategies._derived import DerivedStrategyHandler
from decoy_engine.execution._strategies._faker import FakerStrategyHandler
from decoy_engine.execution._strategies._formula import FormulaStrategyHandler
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._strategies._geo_generalize import GeoGeneralizeHandler
from decoy_engine.execution._strategies._hash import HashStrategyHandler
from decoy_engine.execution._strategies._joint_mask import JointMaskHandler
from decoy_engine.execution._strategies._nested import NestedStrategyHandler
from decoy_engine.execution._strategies._passthrough import PassthroughHandler
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler
from decoy_engine.execution._strategies._text_mask import TextMaskHandler
from decoy_engine.execution._strategies._text_redact import TextRedactHandler
from decoy_engine.execution._strategies._truncate import TruncateHandler

SCALAR_HANDLERS: dict[str, StrategyHandler] = {
    handler.name: handler
    for handler in (
        PassthroughHandler(),
        RedactHandler(),
        TruncateHandler(),
        FakerStrategyHandler(),
        HashStrategyHandler(),
        BucketizeStrategyHandler(),
        ShuffleStrategyHandler(),
        CategoricalStrategyHandler(),
        DateShiftStrategyHandler(),
        FormulaStrategyHandler(),
        FpeStrategyHandler(),
        TextRedactHandler(),
        TextMaskHandler(),
        NestedStrategyHandler(),
        JointMaskHandler(),
        GeoGeneralizeHandler(),
        CodeSetHandler(),
        DerivedStrategyHandler(),
        BucketPerturbStrategyHandler(),
    )
}

__all__ = [
    "SCALAR_HANDLERS",
    "BucketPerturbStrategyHandler",
    "BucketizeStrategyHandler",
    "CategoricalStrategyHandler",
    "CodeSetHandler",
    "DateShiftStrategyHandler",
    "DerivedStrategyHandler",
    "FakerStrategyHandler",
    "FormulaStrategyHandler",
    "FpeStrategyHandler",
    "GeoGeneralizeHandler",
    "HashStrategyHandler",
    "JointMaskHandler",
    "NestedStrategyHandler",
    "PassthroughHandler",
    "RedactHandler",
    "ShuffleStrategyHandler",
    "TextMaskHandler",
    "TextRedactHandler",
    "TruncateHandler",
]
