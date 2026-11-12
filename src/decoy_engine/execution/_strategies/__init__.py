"""Strategy handlers for the pandas execution adapter (engine-v2 S9).

`SCALAR_HANDLERS` maps a strategy name to a handler instance. Slice 2a ships the
three no-backend strategies (passthrough, redact, truncate); later slices add
the keyed/backend strategies (faker, hash, date_shift, bucketize, categorical,
shuffle, formula, fpe) re-keyed onto S3's `derive`/`derive_index` + S5's
`PoolSampler`. SP-10 adds `derived` (closed-grammar row-context expression).
SP-08b adds `bucket_perturb` (coarse time-bucket datetime generalization).
SP-10b adds `derived_aggregate` (intra-table scalar aggregate fill).
SP-10c adds `grouped_series` (per-group series), `windowed_date` (bounded date
offset from an anchor column), and `group_key` (HKDF-keyed per-group identifier).
HC-3b adds `top_code` (numeric tail generalization, e.g. HIPAA Safe Harbor age>89).

`strategy_catalog()` is the public, enumerable description of those handlers. Each handler
declares its own catalog facts as class attributes; the function only reads them.
"""

from __future__ import annotations

from decoy_engine.execution._adapter import StrategyHandler, StrategyInfo, StrategyOption
from decoy_engine.execution._strategies._bucket_perturb import BucketPerturbStrategyHandler
from decoy_engine.execution._strategies._bucketize import BucketizeStrategyHandler
from decoy_engine.execution._strategies._categorical import CategoricalStrategyHandler
from decoy_engine.execution._strategies._code_set import CodeSetHandler
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.execution._strategies._derived import DerivedStrategyHandler
from decoy_engine.execution._strategies._derived_aggregate import DerivedAggregateStrategyHandler
from decoy_engine.execution._strategies._faker import FakerStrategyHandler
from decoy_engine.execution._strategies._formula import FormulaStrategyHandler
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._strategies._geo_generalize import GeoGeneralizeHandler
from decoy_engine.execution._strategies._group_key import GroupKeyStrategyHandler
from decoy_engine.execution._strategies._grouped_series import GroupedSeriesStrategyHandler
from decoy_engine.execution._strategies._hash import HashStrategyHandler
from decoy_engine.execution._strategies._joint_mask import JointMaskHandler
from decoy_engine.execution._strategies._nested import NestedStrategyHandler
from decoy_engine.execution._strategies._passthrough import PassthroughHandler
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler
from decoy_engine.execution._strategies._text_mask import TextMaskHandler
from decoy_engine.execution._strategies._text_redact import TextRedactHandler
from decoy_engine.execution._strategies._tokenize import TokenizeStrategyHandler
from decoy_engine.execution._strategies._top_code import TopCodeStrategyHandler
from decoy_engine.execution._strategies._truncate import TruncateHandler
from decoy_engine.execution._strategies._windowed_date import WindowedDateStrategyHandler

SCALAR_HANDLERS: dict[str, StrategyHandler] = {
    handler.name: handler
    for handler in (
        PassthroughHandler(),
        RedactHandler(),
        TruncateHandler(),
        FakerStrategyHandler(),
        HashStrategyHandler(),
        BucketizeStrategyHandler(),
        TopCodeStrategyHandler(),
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
        DerivedAggregateStrategyHandler(),
        GroupedSeriesStrategyHandler(),
        WindowedDateStrategyHandler(),
        GroupKeyStrategyHandler(),
        TokenizeStrategyHandler(),
    )
}

# Read from each class's own `__dict__`, never by attribute lookup: an inherited default would
# let a new handler satisfy the catalog without anyone deciding its facts.
_CATALOG_FIELDS: dict[str, type | tuple[type, ...]] = {
    "namespace_required": bool,
    "join_safe": bool,
    "wizard_options": tuple,
    "offer_in_wizard": bool,
}


def _catalog_row(handler: StrategyHandler) -> StrategyInfo:
    declared = vars(type(handler))
    for field, expected in _CATALOG_FIELDS.items():
        if field not in declared:
            raise TypeError(f"strategy handler {handler.name!r} does not declare {field!r}")
        if not isinstance(declared[field], expected):
            raise TypeError(f"strategy handler {handler.name!r}: {field!r} has the wrong type")
    options = declared["wizard_options"]
    if not all(isinstance(o, StrategyOption) for o in options):
        raise TypeError(f"strategy handler {handler.name!r}: wizard_options must hold options")
    names = [o.name for o in options]
    if len(set(names)) != len(names):
        raise TypeError(f"strategy handler {handler.name!r}: duplicate option names")
    return StrategyInfo(
        name=handler.name,
        namespace_required=declared["namespace_required"],
        join_safe=declared["join_safe"],
        options=options,
        offer_in_wizard=declared["offer_in_wizard"],
    )


def strategy_catalog() -> tuple[StrategyInfo, ...]:
    """One row per scalar strategy handler, sorted by name.

    A registry of descriptors, the shape of argparse and click option metadata and of the
    connector spec catalogs Airbyte and Singer publish: each handler declares what an operator
    may set on it, so a front end can prompt, validate and test from this data instead of
    keeping a parallel table.
    """
    return tuple(sorted((_catalog_row(h) for h in SCALAR_HANDLERS.values()), key=lambda r: r.name))


__all__ = [
    "SCALAR_HANDLERS",
    "BucketPerturbStrategyHandler",
    "BucketizeStrategyHandler",
    "CategoricalStrategyHandler",
    "CodeSetHandler",
    "DateShiftStrategyHandler",
    "DerivedAggregateStrategyHandler",
    "DerivedStrategyHandler",
    "FakerStrategyHandler",
    "FormulaStrategyHandler",
    "FpeStrategyHandler",
    "GeoGeneralizeHandler",
    "GroupKeyStrategyHandler",
    "GroupedSeriesStrategyHandler",
    "HashStrategyHandler",
    "JointMaskHandler",
    "NestedStrategyHandler",
    "PassthroughHandler",
    "RedactHandler",
    "ShuffleStrategyHandler",
    "StrategyInfo",
    "StrategyOption",
    "TextMaskHandler",
    "TextRedactHandler",
    "TokenizeStrategyHandler",
    "TopCodeStrategyHandler",
    "TruncateHandler",
    "WindowedDateStrategyHandler",
    "strategy_catalog",
]
