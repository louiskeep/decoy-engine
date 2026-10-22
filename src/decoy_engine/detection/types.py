"""Detection-layer dataclasses (PII/format detector output).

Split out of the former `storm/types.py` by the STORM taxonomy rename: these
are the detection-concern types (detector specs, matches, reasoning signals).
The profiling-concern types (`FieldStats`, `ColumnProfile`, ...) live in
`decoy_engine.profiling.types` and import `DetectorMatch` / `DetectionSignal`
from here (one-directional: detection is the base layer).

Kept as pure dataclasses (no Pydantic) so the engine stays lean. The platform
layer wraps these in Pydantic models at the API edge for FastAPI responses.
Everything here must be JSON-serializable via dataclasses.asdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CustomDetectorSpec:
    """Caller-supplied detector definition that runs alongside the built-ins.

    PR6 of the storm port plan. The platform persists detectors in
    custom_detectors and hands a list of these specs to profile_columns so
    users can add organization-specific PII patterns (e.g. UK NHS numbers, SBR
    routing numbers) without engine changes.

    `id` is the detector_id that appears in DetectorMatch.detector_id -
    callers should namespace it (e.g. "custom__uk_nhs_number") to avoid
    colliding with built-in ids. `name_hints` is a list of substrings;
    the engine compiles a case-insensitive regex matching any of them
    against the column name to drop the match-rate threshold.
    `threshold` is the default-fire match rate (0.0-1.0) when no name
    hint matches; the name-hint floor is fixed at 0.4 mirroring the
    built-in detectors.
    """

    id: str
    pattern: str
    name_hints: list[str] = field(default_factory=list)
    threshold: float = 0.7


@dataclass
class DetectorMatch:
    """A single PII/format detector matched against a column."""

    detector_id: str  # "ssn", "email", "us_phone", "us_zip", "iso_date", ...
    match_rate: float  # fraction of non-null values that matched (0.0 - 1.0)
    sample_misses: list[str] = field(default_factory=list)  # up to 3 values that didn't match
    # Item 65 - surface which sub-pattern variant actually fired so the
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
    # Gap-closure item 1: the detector's semantic domain (Identity/Financial/
    # Health/Contact/Location/Other), derived from detector_id at construction.
    # Defaulted so older persisted profiles deserialize; OTHER for custom ids.
    domain: str = "OTHER"


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
    confidence: float | None = None  # 0.0 - 100.0; None when skipped
    winner: bool = False
    ml: bool = False
    skipped: bool = False  # signal was considered but not run (e.g. ML disabled)
