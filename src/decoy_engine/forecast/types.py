"""Dataclasses FORECAST emits.

Pure dataclasses (no Pydantic) for engine-side leanness. The platform layer
wraps these in Pydantic at the API edge for FastAPI response_model support.
All types here are JSON-serializable via dataclasses.asdict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class FieldRecommendation:
    """One column's recommended Mask."""

    field_name: str
    recommended_mask: str  # transform id: "faker", "hash", "date_shift", ...
    mask_params: dict[str, Any]  # the params dict that goes into the masking_rules entry
    confidence: float  # 0.0 - 1.0; mirrors the strongest detector's match_rate
    why: str  # short user-facing explanation
    matched_detector: str  # the detector_id that drove this pick


@dataclass
class DisguiseRecommendation:
    """One ranked Disguise with everything the UI needs to apply it."""

    disguise_id: str
    name: str
    summary: str
    regulation: str | None
    match_score: float  # 0.0 - 1.0
    matched_fields: list[str]  # column names this Disguise's rules cover
    reasoning: str  # one-sentence "why" for the user
    apply_payload: dict[
        str, Any
    ]  # the structured payload UI sends back when "Apply Disguise" is clicked
    # apply_payload shape:
    # {
    #   "disguise_id": "hipaa",
    #   "field_masks": [
    #     {"column": "ssn", "type": "hash", "algorithm": "sha256"},
    #     {"column": "first_name", "type": "faker", "faker_type": "name"},
    #     ...
    #   ]
    # }


@dataclass
class RiskFlag:
    """A profile-level concern surfaced from STORM's sentinels and outliers,
    plus FORECAST-suggested fixes the user can act on."""

    field_name: str
    kind: str  # mirrors SentinelFlag.kind: "date_sentinel", "string_sentinel", ...
    value: str
    note: str
    fix_options: list[str] = field(default_factory=list)


@dataclass
class ForecastReport:
    """The artifact FORECAST produces. JSON-serializable."""

    profile_source: str
    disguise_recommendations: list[DisguiseRecommendation] = field(default_factory=list)
    field_recommendations: list[FieldRecommendation] = field(default_factory=list)
    risk_flags: list[RiskFlag] = field(default_factory=list)
    proposed_pipeline_yaml: str = ""  # ready-to-edit pipeline config string
    engine_version: str = "0.1.0"
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
