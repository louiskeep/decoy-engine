"""Profiling-layer dataclasses (column profile output).

Split out of the former `storm/types.py` by the STORM taxonomy rename: these
are the profiling-concern types. `StormProfile` is renamed to `ColumnProfile`
(the public artifact profile_columns produces); the detection-concern types it
embeds (`DetectorMatch`, `DetectionSignal`) are imported from
`decoy_engine.detection.types` (one-directional: detection is the base layer).

Kept as pure dataclasses (no Pydantic) so the engine stays lean. The platform
layer wraps these in Pydantic models at the API edge for FastAPI responses.
Everything here must be JSON-serializable via dataclasses.asdict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from decoy_engine.detection.types import DetectionSignal, DetectorMatch


@dataclass
class TopValue:
    value: str
    count: int
    pct: float


@dataclass
class Distribution:
    """Per-column value distribution for the Profile/Drill UI.

    Five shapes the renderer supports - driven by `kind`:
      - "numeric"      : 10 quantile bins, data = bucket counts
      - "date"         : decade bins, data = bucket counts, labels = decade ranges
      - "categorical"  : top 10 + "other", data = pct of column, labels = values
      - "pattern"      : 3 buckets (matches / no-match / null) for detector-fired columns
      - "freetext"     : 4 length buckets (<20, 20-50, 50-100, >100)

    `data` and `labels` are parallel arrays. min/max/mean only meaningful for
    numeric or date kinds; left as None otherwise.
    """

    kind: str  # "numeric" | "date" | "categorical" | "pattern" | "freetext"
    data: list[float] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    min: str | None = None  # stringified for JSON parity (numeric or ISO date)
    max: str | None = None
    mean: float | None = None  # numeric only


@dataclass
class SentinelFlag:
    """A value (or pattern) that parsed structurally but is suspicious."""

    kind: str  # "date_sentinel", "date_out_of_range", "numeric_sentinel", "string_sentinel"
    value: str
    count: int
    note: str  # human-readable explanation for FORECAST + UI


@dataclass
class FieldStats:
    """Everything the column profiler computed about one column."""

    name: str
    inferred_type: str  # "integer", "float", "string", "date", "boolean", "mixed"
    dtype_raw: str  # the underlying pandas dtype, for debugging
    row_count: int
    null_count: int
    null_rate: float
    distinct_count: int
    unique_rate: float
    is_likely_unique: bool

    # Numeric
    min_value: str | None = None
    max_value: str | None = None
    mean_value: str | None = None

    # String
    min_length: int | None = None
    max_length: int | None = None
    avg_length: float | None = None

    # Date (sniffed or native)
    date_min: str | None = None
    date_max: str | None = None
    date_format: str | None = None  # "iso_date", "us_date", "eu_date", "datetime", or None
    invalid_count: int | None = None
    sample_invalid: list[str] = field(default_factory=list)

    # Item 65 - surface-format hints consumed by the masking-strategy
    # post-pass so masked output preserves the source's shape.
    #   casing_pattern : 'upper' | 'lower' | 'title' | 'mixed' | 'digits_only' | None
    #   format_pattern : the dominant variant detected by a detector
    #                    (e.g. r'\\d{3}-\\d{2}-\\d{4}' for dashed SSN,
    #                    '%Y-%m-%d' strptime for ISO dates). None when
    #                    no detector with a variant fired.
    casing_pattern: str | None = None
    format_pattern: str | None = None

    # Plan B-2 - column-shape signals FORECAST's per-detector choosers
    # consume to pick mask params instead of using hardcoded defaults.
    # All four are Optional / sensibly-defaulted so persisted profiles
    # from before Plan B-2 deserialize cleanly via dict-spreading at the
    # platform edge.
    #
    #   alphabet            : 'digits' | 'alpha' | 'alphanum' | 'mixed' | None
    #                          Drives hash.truncate length + FPE radix.
    #                          None for non-string / empty columns.
    #   value_set_size_class: 'unique' | 'high' | 'medium' | 'low' |
    #                          'binary' | 'constant' | None.
    #                          Coarse cardinality bucket the chooser can
    #                          branch on without re-deriving from
    #                          unique_rate.
    #   numeric_range_class : 'small_int' | 'big_int' | 'decimal_money' |
    #                          'decimal_other' | None. Drives FPE vs
    #                          hash for numeric IDs; None for non-numeric.
    #   mode_value          : most common non-null value as a string.
    #                          Detects "default value pollution" alongside
    #                          mode_freq.
    #   mode_freq           : frequency of mode_value in 0.0..1.0.
    alphabet: str | None = None
    value_set_size_class: str | None = None
    numeric_range_class: str | None = None
    mode_value: str | None = None
    mode_freq: float = 0.0

    # Top values for distribution awareness
    top_values: list[TopValue] = field(default_factory=list)

    # Detector hits - what FORECAST will key on
    detector_matches: list[DetectorMatch] = field(default_factory=list)

    # Outliers / sentinels
    sentinels: list[SentinelFlag] = field(default_factory=list)

    # Heuristic 0.0 - 1.0 likelihood this column contains PII
    pii_score: float = 0.0

    # Value distribution for the Profile/Drill UI. Optional so old persisted
    # profiles (pre-PR3) deserialize cleanly via dict-spreading at the
    # platform edge.
    distribution: Distribution | None = None

    # Per-column detection-reasoning trail. Empty when no detector fires.
    # ML rows (column classifier, cell-level NER) append in Roadmap Item 8.
    detection_trail: list[DetectionSignal] = field(default_factory=list)

    # Gap-closure item 1: the winning detector's semantic domain, for
    # convenience grouping/filtering at the platform/web edge. None when no
    # detector fired. Derived from detector_matches[0]; never persisted as a
    # source of truth.
    domain: str | None = None


@dataclass
class ColumnProfile:
    """The artifact profile_columns produces. Input to FORECAST. JSON-serializable.

    Formerly `StormProfile`; renamed by the STORM taxonomy rename (the value is
    a per-source column profile). Wire/config identifiers are unchanged.
    """

    source_label: str  # "users.csv" / "public.orders" / etc.
    row_count: int  # rows actually scanned
    sample_strategy: str  # "full", "head", "random", "stratified"
    sample_row_cap: int | None = None
    fields: list[FieldStats] = field(default_factory=list)

    # Dataset-level
    reid_risk_columns: list[str] = field(default_factory=list)
    reid_risk_score: float = 0.0
    quasi_identifier_groups: list[list[str]] = field(default_factory=list)
    # ^^ data-driven (Plan B-1): the column combos that achieve
    # the minimum k-anonymity value k below. Multiple combos can
    # tie at the same k; all winning combos are listed. Empty
    # when the dataset has no low-cardinality categoricals
    # (re-id risk via quasi-id linkage isn't a concern).

    # k-anonymity (Plan B-1). Minimum group size across 2- and
    # 3-column combos of low-cardinality categorical candidates.
    # k=1 means at least one combo uniquely identifies a row
    # (re-id risk via linkage is high). k=None means no
    # candidates were eligible (either dataset is too small or
    # every column is either unique or constant).
    k_anonymity: int | None = None

    # Run metadata
    engine_version: str = "0.1.0"
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict. Use this when persisting or sending over the wire."""
        return asdict(self)
