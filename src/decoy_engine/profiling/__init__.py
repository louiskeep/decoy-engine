"""decoy_engine.profiling - column classification + profile recommendations.

Public entry point:
    profile_columns(df, source_label, *, sample_strategy=...) -> ColumnProfile

A ColumnProfile is a JSON-serializable summary of a dataset: per-field stats,
PII detector hits, format signals, sentinel-value flags, plus dataset-level
re-identification risk. It is the only thing FORECAST sees; FORECAST never
touches raw data.

Formerly part of the `storm` package; split by concern in the STORM taxonomy
rename (the profiling entrypoint is now `profile_columns`, returning a
`ColumnProfile`). Depends on `decoy_engine.detection`; `storm` survives only as
a product label on the user-facing / wire surfaces (incl. the `run_storm`
config field).
"""

from decoy_engine.profiling.profiler import profile_columns
from decoy_engine.profiling.recommendations import (
    DEFAULT_STRATEGY_BY_DETECTOR,
    StrategyDefault,
    get_default_strategy,
    known_detector_ids,
)
from decoy_engine.profiling.types import (
    ColumnProfile,
    Distribution,
    FieldStats,
    SentinelFlag,
    TopValue,
)

__all__ = [
    "DEFAULT_STRATEGY_BY_DETECTOR",
    "ColumnProfile",
    "Distribution",
    "FieldStats",
    "SentinelFlag",
    "StrategyDefault",
    "TopValue",
    "get_default_strategy",
    "known_detector_ids",
    "profile_columns",
]
