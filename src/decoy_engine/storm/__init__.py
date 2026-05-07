"""decoy_engine.storm — dataset analysis (the "scan" event).

Public entry point:
    run_storm(df, source_label, *, sample_strategy=...) -> StormProfile

A StormProfile is a JSON-serializable summary of a dataset: per-field stats,
PII detector hits, format signals, sentinel-value flags, plus dataset-level
re-identification risk. It is the only thing FORECAST sees; FORECAST never
touches raw data.
"""

from decoy_engine.storm.profiler import run_storm
from decoy_engine.storm.types import (
    StormProfile,
    FieldStats,
    CustomDetectorSpec,
    DetectorMatch,
    DetectionSignal,
    Distribution,
    SentinelFlag,
    TopValue,
)

__all__ = [
    "run_storm",
    "StormProfile",
    "FieldStats",
    "CustomDetectorSpec",
    "DetectorMatch",
    "DetectionSignal",
    "Distribution",
    "SentinelFlag",
    "TopValue",
]
