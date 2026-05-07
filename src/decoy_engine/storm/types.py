"""Dataclasses for STORM output.

Kept as pure dataclasses (no Pydantic) so the engine stays lean. The platform
layer wraps these in Pydantic models at the API edge for FastAPI responses.

Everything here must be JSON-serializable via dataclasses.asdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional


@dataclass
class TopValue:
    value: str
    count: int
    pct: float


@dataclass
class DetectorMatch:
    """A single PII/format detector matched against a column."""
    detector_id: str           # "ssn", "email", "us_phone", "us_zip", "iso_date", ...
    match_rate: float          # fraction of non-null values that matched (0.0 – 1.0)
    sample_misses: list[str] = field(default_factory=list)  # up to 3 values that didn't match


@dataclass
class Distribution:
    """Per-column value distribution for the Profile/Drill UI.

    Five shapes the renderer supports — driven by `kind`:
      - "numeric"      : 10 quantile bins, data = bucket counts
      - "date"         : decade bins, data = bucket counts, labels = decade ranges
      - "categorical"  : top 10 + "other", data = pct of column, labels = values
      - "pattern"      : 3 buckets (matches / no-match / null) for detector-fired columns
      - "freetext"     : 4 length buckets (<20, 20-50, 50-100, >100)

    `data` and `labels` are parallel arrays. min/max/mean only meaningful for
    numeric or date kinds; left as None otherwise.
    """
    kind: str                                  # "numeric" | "date" | "categorical" | "pattern" | "freetext"
    data: list[float] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    min: Optional[str] = None                  # stringified for JSON parity (numeric or ISO date)
    max: Optional[str] = None
    mean: Optional[float] = None               # numeric only


@dataclass
class DetectionSignal:
    """One row in a column's detection-reasoning trail.

    Each signal records ONE piece of evidence for the winning detector:
      - regex match score
      - column-name hint match
      - (future) ML column classifier score
      - (future) ML cell-level NER score

    `winner=True` marks the signal that drove the firing decision. `ml=False`
    today; ML detection phases (Roadmap Item 8) will append `ml=True` rows
    without disturbing the existing schema.
    """
    signal: str                                # "regex · ssn_pattern", "name-hint · col=\"ssn\"", ...
    confidence: Optional[float] = None         # 0.0 – 100.0; None when skipped
    winner: bool = False
    ml: bool = False
    skipped: bool = False                      # signal was considered but not run (e.g. ML disabled)


@dataclass
class SentinelFlag:
    """A value (or pattern) that parsed structurally but is suspicious."""
    kind: str                  # "date_outlier", "numeric_sentinel", "string_sentinel", "future_date"
    value: str
    count: int
    note: str                  # human-readable explanation for FORECAST + UI


@dataclass
class FieldStats:
    """Everything STORM computed about one column."""
    name: str
    inferred_type: str         # "integer", "float", "string", "date", "boolean", "mixed"
    dtype_raw: str             # the underlying pandas dtype, for debugging
    row_count: int
    null_count: int
    null_rate: float
    distinct_count: int
    unique_rate: float
    is_likely_unique: bool

    # Numeric
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    mean_value: Optional[str] = None

    # String
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    avg_length: Optional[float] = None

    # Date (sniffed or native)
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    date_format: Optional[str] = None      # "iso_date", "us_date", "eu_date", "datetime", or None
    invalid_count: Optional[int] = None
    sample_invalid: list[str] = field(default_factory=list)

    # Top values for distribution awareness
    top_values: list[TopValue] = field(default_factory=list)

    # Detector hits — what FORECAST will key on
    detector_matches: list[DetectorMatch] = field(default_factory=list)

    # Outliers / sentinels
    sentinels: list[SentinelFlag] = field(default_factory=list)

    # Heuristic 0.0 – 1.0 likelihood this column contains PII
    pii_score: float = 0.0

    # Value distribution for the Profile/Drill UI. Optional so old persisted
    # profiles (pre-PR3) deserialize cleanly via dict-spreading at the
    # platform edge.
    distribution: Optional[Distribution] = None

    # Per-column detection-reasoning trail. Empty when no detector fires.
    # ML rows (column classifier, cell-level NER) append in Roadmap Item 8.
    detection_trail: list[DetectionSignal] = field(default_factory=list)


@dataclass
class StormProfile:
    """The artifact STORM produces. Input to FORECAST. JSON-serializable."""
    source_label: str          # "users.csv" / "public.orders" / etc.
    row_count: int             # rows actually scanned
    sample_strategy: str       # "full", "head", "random", "stratified"
    sample_row_cap: Optional[int] = None
    fields: list[FieldStats] = field(default_factory=list)

    # Dataset-level
    reid_risk_columns: list[str] = field(default_factory=list)
    reid_risk_score: float = 0.0
    quasi_identifier_groups: list[list[str]] = field(default_factory=list)
    # ^^ e.g. [["dob", "zip", "gender"]] when those co-occur — FORECAST flags these
    # as HIPAA-style quasi-identifiers.

    # Run metadata
    engine_version: str = "0.1.0"
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict. Use this when persisting or sending over the wire."""
        return asdict(self)
