"""Deterministic per-column feature builder (BF2 / ML1).

``build_column_features(series, col_name)`` turns one pandas column into a
:class:`ColumnFeatures` vector. It is fully deterministic: the same input
produces a byte-identical artifact on every call. There is no model, no
randomness, and no runtime ML dependency - this is the honest first move
that precedes any column classifier (ML2+, gated).

Determinism discipline (mirrors profiler.py):
  - content features sample the column's HEAD (``iloc[:N]``), never a
    random sample, matching ``_classify_alphabet`` / ``_detect_casing``.
  - tie-breaks in the shape vote are resolved by (count, then mask string)
    so the winner is stable across runs and platforms.

Reuse: the regex constants, checksum validators, and the four coarse
profiler classifiers are imported from the sibling modules rather than
re-implemented, so a detector change flows through to the features.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable

import pandas as pd

from decoy_engine.storm.detectors import (
    _CVV_RE,
    _EMAIL_RE,
    _EU_DATE_RE,
    _IBAN_RE,
    _ICD10_RE,
    _IPV4_RE,
    _ISO_DATE_RE,
    _NPI_RE,
    _PAN_RE,
    _PERSON_NAME_RE,
    _SSN_RE,
    _US_DATE_RE,
    _US_PHONE_RE,
    _US_ZIP_RE,
    _iban_valid,
    _icd10_valid,
    _ipv4_valid,
    _luhn_valid,
    _npi_valid,
)
from decoy_engine.storm.features.types import ColumnFeatures, ShapeSignature
from decoy_engine.storm.profiler import (
    _classify_alphabet,
    _classify_numeric_range,
    _classify_value_set_size,
    _detect_casing,
)

# Head-sample cap for content features. Matches profiler._B2_ALPHABET_SAMPLE
# so the feature builder and the profiler agree on which rows they read.
_SAMPLE = 200

# Values longer than this are excluded from the shape vote: a 300-char
# free-text cell has no meaningful fixed mask and would just add noise.
MASK_MAX_LEN = 40

# Regex weak signals. Ordered so the emitted dict is stable. The optional
# second element is the same per-value validator detectors._evaluate runs;
# when present a value counts only if the regex fullmatches AND the
# validator passes (mirrors _evaluate's validator gate).
_REGEX_SIGNALS: tuple[tuple[str, re.Pattern[str], Callable[[str], bool] | None], ...] = (
    ("email", _EMAIL_RE, None),
    ("ssn", _SSN_RE, None),
    ("us_phone", _US_PHONE_RE, None),
    ("us_zip", _US_ZIP_RE, None),
    ("person_name", _PERSON_NAME_RE, None),
    ("iso_date", _ISO_DATE_RE, None),
    ("us_date", _US_DATE_RE, None),
    ("eu_date", _EU_DATE_RE, None),
    ("pan", _PAN_RE, _luhn_valid),
    ("cvv", _CVV_RE, None),
    ("iban", _IBAN_RE, _iban_valid),
    ("ipv4", _IPV4_RE, _ipv4_valid),
    ("icd10", _ICD10_RE, _icd10_valid),
    ("npi", _NPI_RE, _npi_valid),
)

# Standalone checksum validators, applied with no regex gate.
_CHECKSUM_VALIDATORS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("luhn", _luhn_valid),
    ("iban", _iban_valid),
    ("ipv4", _ipv4_valid),
    ("npi", _npi_valid),
    ("icd10", _icd10_valid),
)

# camelCase boundary inserts: split lower|Upper and Upper|UpperLower.
_CAMEL_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_HEADER_SPLIT = re.compile(r"[\s_\-./]+")


def tokenize_header(name: str) -> list[str]:
    """Tokenize a column name on ``_ - . /`` whitespace and camelCase.

    Lowercased, empty tokens dropped. ``patientMRN`` -> ``["patient", "mrn"]``,
    ``order_id`` -> ``["order", "id"]``, opaque headers like ``f07`` /
    ``c1`` stay single tokens (no letter/digit split). Deterministic.
    """
    tokens: list[str] = []
    for part in _HEADER_SPLIT.split(str(name)):
        if not part:
            continue
        spaced = _CAMEL_2.sub(" ", _CAMEL_1.sub(" ", part))
        for tok in spaced.split():
            if tok:
                tokens.append(tok.lower())
    return tokens


def _infer_type(series: pd.Series) -> str:
    """Friendly type label - same mapping the profiler uses inline."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "float"
    if pd.api.types.is_string_dtype(series):
        return "string"
    return "mixed"


def _string_sample(series: pd.Series) -> pd.Series:
    """Non-null, stripped, non-empty string HEAD sample (deterministic)."""
    non_null = series.dropna().astype(str).str.strip()
    non_null = non_null[non_null != ""]
    if len(non_null) > _SAMPLE:
        non_null = non_null.iloc[:_SAMPLE]
    return non_null


def _char_class_fractions(sample: pd.Series) -> dict[str, float]:
    """Fraction of sampled characters in each class; sums to ~1.0."""
    keys = ("digit", "upper", "lower", "whitespace", "other")
    counts = dict.fromkeys(keys, 0)
    total = 0
    for value in sample:
        for ch in value:
            total += 1
            if ch.isdigit():
                counts["digit"] += 1
            elif ch.isalpha() and ch.isupper():
                counts["upper"] += 1
            elif ch.isalpha():
                counts["lower"] += 1
            elif ch.isspace():
                counts["whitespace"] += 1
            else:
                counts["other"] += 1
    if total == 0:
        return {k: 0.0 for k in keys}
    return {k: round(counts[k] / total, 4) for k in keys}


def _shannon_entropy(series: pd.Series) -> tuple[float, float]:
    """(entropy_bits, normalized) over the full non-null value distribution.

    Normalized by log2(distinct); 0.0 when fewer than two distinct values.
    Pure stdlib ``math`` - no numpy/scipy entropy dependency.
    """
    non_null = series.dropna()
    if len(non_null) == 0:
        return 0.0, 0.0
    counts = non_null.value_counts(dropna=True)
    distinct = len(counts)
    if distinct <= 1:
        return 0.0, 0.0
    total = int(counts.sum())
    entropy = 0.0
    for c in counts:
        p = int(c) / total
        entropy -= p * math.log2(p)
    normalized = entropy / math.log2(distinct)
    return round(entropy, 4), round(normalized, 4)


def _regex_signals(sample: pd.Series) -> dict[str, float]:
    """Per-detector regex fullmatch rate, including sub-threshold hits."""
    out: dict[str, float] = {}
    n = len(sample)
    if n == 0:
        return {det_id: 0.0 for det_id, _, _ in _REGEX_SIGNALS}
    for det_id, pattern, validator in _REGEX_SIGNALS:
        matched = sample.str.fullmatch(pattern).fillna(False)
        if validator is not None:
            ok = 0
            for value in sample[matched]:
                try:
                    if validator(value):
                        ok += 1
                except (TypeError, ValueError):
                    continue
            rate = ok / n
        else:
            rate = int(matched.sum()) / n
        out[det_id] = round(rate, 4)
    return out


def _checksum_pass_rates(sample: pd.Series) -> dict[str, float]:
    """Validator pass rate over every sampled value (no regex gate)."""
    n = len(sample)
    if n == 0:
        return {name: 0.0 for name, _ in _CHECKSUM_VALIDATORS}
    out: dict[str, float] = {}
    for name, validator in _CHECKSUM_VALIDATORS:
        ok = 0
        for value in sample:
            try:
                if validator(value):
                    ok += 1
            except (TypeError, ValueError):
                continue
        out[name] = round(ok / n, 4)
    return out


def _mask(value: str) -> str:
    """digit -> d, ASCII letter -> a, everything else verbatim."""
    out = []
    for ch in value:
        if ch.isdigit():
            out.append("d")
        elif ch.isascii() and ch.isalpha():
            out.append("a")
        else:
            out.append(ch)
    return "".join(out)


def _shape_signature(sample: pd.Series) -> ShapeSignature:
    """Dominant value mask + length stats over the sample."""
    if len(sample) == 0:
        return ShapeSignature()
    lengths = [len(v) for v in sample]
    masks = Counter(_mask(v) for v in sample if len(v) <= MASK_MAX_LEN)
    dominant_mask: str | None = None
    dominant_rate = 0.0
    if masks:
        # Deterministic tie-break: highest count, then lexical mask order.
        dominant_mask, top = max(masks.items(), key=lambda kv: (kv[1], kv[0]))
        dominant_rate = round(top / len(sample), 4)
    return ShapeSignature(
        dominant_mask=dominant_mask,
        dominant_mask_rate=dominant_rate,
        min_length=min(lengths),
        max_length=max(lengths),
        mean_length=round(sum(lengths) / len(lengths), 2),
    )


def build_column_features(series: pd.Series, col_name: str) -> ColumnFeatures:
    """Build the deterministic ML1 feature vector for one column.

    ``series`` is the raw pandas column; ``col_name`` is the header used for
    token features (independent of ``series.name`` so callers can override).
    """
    row_count = len(series)
    null_count = int(series.isna().sum())
    non_null_count = row_count - null_count
    distinct_count = int(series.nunique(dropna=True))
    null_rate = round(null_count / row_count, 4) if row_count else 0.0
    distinct_rate = round(distinct_count / row_count, 4) if row_count else 0.0
    unique_rate = round(distinct_count / non_null_count, 4) if non_null_count else 0.0

    sample = _string_sample(series)
    entropy, normalized = _shannon_entropy(series)

    return ColumnFeatures(
        column_name=str(col_name),
        header_tokens=tokenize_header(col_name),
        inferred_type=_infer_type(series),
        dtype_raw=str(series.dtype),
        row_count=row_count,
        non_null_count=non_null_count,
        sample_size=len(sample),
        null_rate=null_rate,
        distinct_count=distinct_count,
        distinct_rate=distinct_rate,
        unique_rate=unique_rate,
        char_class_fractions=_char_class_fractions(sample),
        shannon_entropy=entropy,
        normalized_entropy=normalized,
        regex_signals=_regex_signals(sample),
        checksum_pass_rates=_checksum_pass_rates(sample),
        shape=_shape_signature(sample),
        alphabet=_classify_alphabet(series),
        casing=_detect_casing(series),
        value_set_size_class=_classify_value_set_size(distinct_count, unique_rate),
        numeric_range_class=_classify_numeric_range(series, _infer_type(series)),
    )
