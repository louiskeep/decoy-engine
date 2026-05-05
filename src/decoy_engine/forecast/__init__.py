"""decoy_engine.forecast — recommendations over a StormProfile.

Public entry point:
    recommend(profile: StormProfile) -> ForecastReport

CRITICAL invariant: `recommend` accepts ONLY a StormProfile. It must not
take a connector, DataFrame, file path, or any raw-data argument. This is
the security boundary we promise buyers — FORECAST never sees customer
data, only the JSON statistics STORM produced. A unit test introspects
this signature and fails CI if it ever changes.
"""

from decoy_engine.forecast.recommender import recommend
from decoy_engine.forecast.types import (
    DisguiseRecommendation,
    FieldRecommendation,
    ForecastReport,
    RiskFlag,
)

__all__ = [
    "recommend",
    "ForecastReport",
    "DisguiseRecommendation",
    "FieldRecommendation",
    "RiskFlag",
]
