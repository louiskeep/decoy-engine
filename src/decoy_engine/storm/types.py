"""Dataclasses for STORM output.

Kept as pure dataclasses (no Pydantic) so the engine stays lean. The platform
layer wraps these in Pydantic models at the API edge for FastAPI responses.

Everything here must be JSON-serializable via dataclasses.asdict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class CustomDetectorSpec:
    """Caller-supplied detector definition that runs alongside the built-ins.

    PR6 of the storm port plan. The platform persists detectors in
    custom_detectors and hands a list of these specs to run_storm so users
    can add organization-specific PII patterns (e.g. UK NHS numbers, SBR
    routing numbers) without engine changes.

    `id` is the detector_id that appears in DetectorMatch.detector_id —
    callers should namespace it (e.g. "custom__uk_nhs_number") to avoid
    colliding with built-in ids. `name_hints` is a list of substrings;
    the engine compiles a case-insensitive regex matching any of them
    against the column name to drop the match-rate threshold.
    `threshold` is the default-fire match rate (0.0–1.0) when no name
    hint matches; the name-hint floor is fixed at 0.4 mirroring the
    built-in detectors.
    """

    id: str
    pattern: str
    name_hints: list[str] = field(default_factory=list)
    threshold: float = 0.7


@dataclass
class TopValue:
    value: str
    count: int
    pct: float


@dataclass
class DetectorMatch:
    """A single PII/format detector matched against a column."""

    detector_id: str  # "ssn", "email", "us_phone", "us_zip", "iso_date", ...
    match_rate: float  # fraction of non-null values that matched (0.0 – 1.0)
    sample_misses: list[str] = field(default_factory=list)  # up to 3 values that didn't match
    # Item 65 — surface which sub-pattern variant actually fired so the
    # mask post-pass can splice separators back at the right positions.
    # Detectors with no variants (email, name, etc.) leave this None.
    # Detectors with variants (SSN dash/no-dash, phone separator styles,
    # date strptime, ZIP 5/9) write the winning variant's label here.
    format_pattern: str | None = None
    # Detection sprint (V1): three-bucket confidence so the UI can render
    # "detected" / "needs review" / "low signal" without re-deriving from
    # match_rate. 'high' is the safe-to-auto-apply bucket; 'medium' asks
    # the user to confirm; 'low' surfaces only when the user explicitly
    # asks to see weak signals.
    #
    # Thresholds (set by _evaluate in detectors.py):
    #   high:   name hint + content >= 45%, OR content alone >= 75%
    #   medium: content alone >= 50% with no hint
    #   low:    content alone >= 30% with no hint
    #
    # Defaults to 'high' so every existing firing site that didn't set
    # the field gets the old behavior.
    confidence: str = "high"


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

    kind: str  # "numeric" | "date" | "categorical" | "pattern" | "freetext"
    data: list[float] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    min: str | None = None  # stringified for JSON parity (numeric or ISO date)
    max: str | None = None
    mean: float | None = None  # numeric only


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

    signal: str  # "regex · ssn_pattern", "name-hint · col=\"ssn\"", ...
    confidence: float | None = None  # 0.0 – 100.0; None when skipped
    winner: bool = False
    ml: bool = False
    skipped: bool = False  # signal was considered but not run (e.g. ML disabled)


@dataclass
class SentinelFlag:
    """A value (or pattern) that parsed structurally but is suspicious."""

    kind: str  # "date_sentinel", "date_out_of_range", "numeric_sentinel", "string_sentinel"
    value: str
    count: int
    note: str  # human-readable explanation for FORECAST + UI


@dataclass
class FieldStats:
    """Everything STORM computed about one column."""

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

    # Item 65 — surface-format hints consumed by the masking-strategy
    # post-pass so masked output preserves the source's shape.
    #   casing_pattern : 'upper' | 'lower' | 'title' | 'mixed' | 'digits_only' | None
    #   format_pattern : the dominant variant detected by a detector
    #                    (e.g. r'\\d{3}-\\d{2}-\\d{4}' for dashed SSN,
    #                    '%Y-%m-%d' strptime for ISO dates). None when
    #                    no detector with a variant fired.
    casing_pattern: str | None = None
    format_pattern: str | None = None

    # Plan B-2 — column-shape signals FORECAST's per-detector choosers
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

    # Detector hits — what FORECAST will key on
    detector_matches: list[DetectorMatch] = field(default_factory=list)

    # Outliers / sentinels
    sentinels: list[SentinelFlag] = field(default_factory=list)

    # Heuristic 0.0 – 1.0 likelihood this column contains PII
    pii_score: float = 0.0

    # Value distribution for the Profile/Drill UI. Optional so old persisted
    # profiles (pre-PR3) deserialize cleanly via dict-spreading at the
    # platform edge.
    distribution: Distribution | None = None

    # Per-column detection-reasoning trail. Empty when no detector fires.
    # ML rows (column classifier, cell-level NER) append in Roadmap Item 8.
    detection_trail: list[DetectionSignal] = field(default_factory=list)


@dataclass
class StormProfile:
    """The artifact STORM produces. Input to FORECAST. JSON-serializable."""

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
