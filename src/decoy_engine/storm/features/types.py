"""Dataclasses for the deterministic column feature builder (BF2 / ML1).

Pure dataclasses, mirroring the convention in ``storm/types.py`` (no
Pydantic). Everything here is JSON-serializable via ``dataclasses.asdict``.

This is a SEPARATE artifact from ``StormProfile`` / ``FieldStats`` on
purpose: per the compatibility contract, the persisted profile's
``asdict`` surface is frozen-format governed. Keeping the ML feature
vector standalone means it never crosses that boundary, so the builder
stays additive and reversible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ShapeSignature:
    """The structural "shape" of a column's values.

    ``dominant_mask`` is the most common per-value mask, where each digit
    maps to ``d``, each ASCII letter to ``a``, and every other character
    (separators, whitespace) is kept verbatim. e.g. an SSN column yields
    ``ddd-dd-dddd`` and a 5-digit ZIP yields ``ddddd``. Masks for values
    longer than ``MASK_MAX_LEN`` are excluded from the vote (free-text
    cells have no meaningful fixed shape); ``dominant_mask`` is ``None``
    when no value qualified.
    """

    dominant_mask: str | None = None
    dominant_mask_rate: float = 0.0
    min_length: int | None = None
    max_length: int | None = None
    mean_length: float | None = None


@dataclass
class ColumnFeatures:
    """Deterministic per-column feature vector (ML1).

    A standalone, JSON-serializable summary computed from a single column,
    intended as the input contract for a downstream column classifier
    (ML2+, gated and not built here). No model is involved in producing
    it; every field is a deterministic function of the input series.
    """

    column_name: str
    header_tokens: list[str] = field(default_factory=list)

    inferred_type: str = "mixed"
    dtype_raw: str = ""

    row_count: int = 0
    non_null_count: int = 0
    sample_size: int = 0  # number of values used for content features

    null_rate: float = 0.0
    distinct_count: int = 0
    distinct_rate: float = 0.0  # distinct / row_count
    unique_rate: float = 0.0  # distinct / non_null

    # char-class fractions over the sampled characters; sums to ~1.0 when
    # any character was seen, all-zero for an empty/all-null column.
    char_class_fractions: dict[str, float] = field(default_factory=dict)

    # Shannon entropy (bits) of the full non-null value distribution, plus
    # the same normalized to [0, 1] by log2(distinct_count).
    shannon_entropy: float = 0.0
    normalized_entropy: float = 0.0

    # Per-detector regex match rate INCLUDING sub-threshold hits (the
    # detectors' own firing thresholds are not applied). For detectors with
    # a checksum validator the rate counts only regex-matches that also pass
    # the validator, mirroring detectors._evaluate.
    regex_signals: dict[str, float] = field(default_factory=dict)

    # Standalone checksum pass rates (validator applied to every sampled
    # value, no regex gate) - surfaces structural identifiers hiding under
    # opaque headers.
    checksum_pass_rates: dict[str, float] = field(default_factory=dict)

    shape: ShapeSignature = field(default_factory=ShapeSignature)

    # Reused profiler classifications (coarse enums the chooser already uses).
    alphabet: str | None = None
    casing: str | None = None
    value_set_size_class: str | None = None
    numeric_range_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict. Nested ShapeSignature flattens via asdict."""
        return asdict(self)
