"""decoy_engine.storm - dataset analysis (the "scan" event).

Public entry point:
    run_storm(df, source_label, *, sample_strategy=...) -> StormProfile

A StormProfile is a JSON-serializable summary of a dataset: per-field stats,
PII detector hits, format signals, sentinel-value flags, plus dataset-level
re-identification risk. It is the only thing FORECAST sees; FORECAST never
touches raw data.
"""

from decoy_engine.storm.domains import DOMAIN_BY_DETECTOR, Domain, domain_for
from decoy_engine.storm.profiler import run_storm
from decoy_engine.storm.types import (
    CustomDetectorSpec,
    DetectionSignal,
    DetectorMatch,
    Distribution,
    FieldStats,
    SentinelFlag,
    StormProfile,
    TopValue,
)

__all__ = [
    "DOMAIN_BY_DETECTOR",
    "CustomDetectorSpec",
    "DetectionSignal",
    "DetectorMatch",
    "Distribution",
    "Domain",
    "FieldStats",
    "SentinelFlag",
    "StormProfile",
    "TopValue",
    "domain_for",
    "run_storm",
]
