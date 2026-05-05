"""PII / format detectors used by STORM.

Each detector is a callable `(series, col_name) -> DetectorMatch | None`. The
profiler runs every registered detector against every column and attaches the
resulting matches to the column's FieldStats.

A detector returns:
    - DetectorMatch if (match_rate >= min_match_rate) OR (column name strongly
      hints at the detector). The match_rate field reports the actual fraction
      of non-null values that matched the pattern, regardless of the name hint
      that may have triggered firing.
    - None otherwise.

Conservative thresholds keep the ranked FORECAST output clean: a column with
30% email-like values shouldn't get tagged "email" — that's noise.

MVP detector set: email, ssn, us_phone, us_zip, person_name, plus three date
formats (iso_date, us_date, eu_date). Midrange (ipv4, pan/Luhn, mrn, npi,
icd10) is deferred to a follow-up PR.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

import pandas as pd

from decoy_engine.storm.types import DetectorMatch


# ── thresholds ────────────────────────────────────────────────────────────────

DEFAULT_MIN_MATCH_RATE = 0.7   # 70% of non-null values must match the pattern
NAME_HINT_MIN_RATE     = 0.4   # 40% if the column name strongly hints
SAMPLE_MISS_LIMIT      = 3


# ── helpers ───────────────────────────────────────────────────────────────────

def _series_str(series: pd.Series) -> pd.Series:
    """Drop nulls and coerce to string — every detector needs this prelude."""
    return series.dropna().astype(str).str.strip()


def _evaluate(
    detector_id: str,
    values: pd.Series,
    pattern: re.Pattern[str],
    *,
    name_hint: bool,
    min_rate: float,
) -> Optional[DetectorMatch]:
    """Apply a regex to non-null values and decide whether the detector fires."""
    if len(values) == 0:
        return None
    matches = values.str.fullmatch(pattern)
    rate = float(matches.mean())
    threshold = NAME_HINT_MIN_RATE if name_hint else min_rate
    if rate < threshold:
        return None
    misses = values[~matches].head(SAMPLE_MISS_LIMIT).tolist()
    return DetectorMatch(
        detector_id=detector_id,
        match_rate=round(rate, 4),
        sample_misses=[str(m) for m in misses],
    )


# ── name-hint patterns (case-insensitive, matched against the column name) ──

_NAME_HINTS: dict[str, re.Pattern[str]] = {
    "email":       re.compile(r"(?i)^(.*[_-])?(e?mail|email_address)([_-].*)?$"),
    "ssn":         re.compile(r"(?i)^(.*[_-])?(ssn|social.?security.?(num|number)?)([_-].*)?$"),
    "us_phone":    re.compile(r"(?i)^(.*[_-])?(phone|tel(ephone)?|mobile|cell)([_-].*)?$"),
    "us_zip":      re.compile(r"(?i)^(.*[_-])?(zip|postal|post)([_-]?code)?([_-].*)?$"),
    "person_name": re.compile(r"(?i)^(first|last|full|given|family|sur|user|customer|patient|client|middle|maiden)?[._-]?name$|^name$|^.*_name$"),
    "iso_date":    re.compile(r"(?i)^(.*[_-])?(date|created|updated|modified|dob|birth|start|end|due|effective|expir)([_-].*)?$"),
    "us_date":     re.compile(r"(?i)^(.*[_-])?(date|created|updated|modified|dob|birth|start|end|due|effective|expir)([_-].*)?$"),
    "eu_date":     re.compile(r"(?i)^(.*[_-])?(date|created|updated|modified|dob|birth|start|end|due|effective|expir)([_-].*)?$"),
}


def _hits_name_hint(detector_id: str, col_name: str) -> bool:
    pat = _NAME_HINTS.get(detector_id)
    return bool(pat and pat.fullmatch(col_name or ""))


# ── value patterns ────────────────────────────────────────────────────────────

# Email — RFC 5321ish but not strict; works for the 99% of fields users feed in.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# SSN — ###-##-#### or 9 consecutive digits. Reject 000-/666-/9##- per SSA rules.
_SSN_RE = re.compile(r"(?!000|666|9\d{2})\d{3}-?(?!00)\d{2}-?(?!0000)\d{4}")

# US phone — 10 digits with common separators, optional +1 country code.
_US_PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?[2-9]\d{2}[\s.-]?\d{4}"
)

# US ZIP — 5 digits, optional -#### extension.
_US_ZIP_RE = re.compile(r"\d{5}(?:-\d{4})?")

# Date formats — strict patterns; the profiler also has pandas' to_datetime
# fuzzy parser as a backstop. These are for *format signal* only.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?Z?")
_US_DATE_RE  = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
_EU_DATE_RE  = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}|\d{1,2}-\d{1,2}-\d{4}")

# Person name — 1-3 whitespace-separated tokens, each starts with a letter,
# letters / hyphens / apostrophes / dots only. Length 2-50 total.
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’.\-]{0,29}")
_PERSON_NAME_RE = re.compile(
    rf"{_NAME_TOKEN_RE.pattern}(?:\s+{_NAME_TOKEN_RE.pattern}){{0,2}}"
)


# ── detectors (callables) ─────────────────────────────────────────────────────

def detect_email(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    return _evaluate("email", _series_str(series), _EMAIL_RE,
                     name_hint=_hits_name_hint("email", col_name),
                     min_rate=DEFAULT_MIN_MATCH_RATE)


def detect_ssn(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    return _evaluate("ssn", _series_str(series), _SSN_RE,
                     name_hint=_hits_name_hint("ssn", col_name),
                     min_rate=DEFAULT_MIN_MATCH_RATE)


def detect_us_phone(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    return _evaluate("us_phone", _series_str(series), _US_PHONE_RE,
                     name_hint=_hits_name_hint("us_phone", col_name),
                     min_rate=DEFAULT_MIN_MATCH_RATE)


def detect_us_zip(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    return _evaluate("us_zip", _series_str(series), _US_ZIP_RE,
                     name_hint=_hits_name_hint("us_zip", col_name),
                     min_rate=DEFAULT_MIN_MATCH_RATE)


def detect_person_name(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    """Heuristic: name-hinted column name + values look like proper-cased tokens.

    Without a strong column-name hint, person_name is too noisy — most short
    string columns happen to "look like names" by this regex. Require the hint.
    """
    if not _hits_name_hint("person_name", col_name):
        return None
    return _evaluate("person_name", _series_str(series), _PERSON_NAME_RE,
                     name_hint=True,
                     min_rate=DEFAULT_MIN_MATCH_RATE)


def detect_iso_date(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    return _evaluate("iso_date", _series_str(series), _ISO_DATE_RE,
                     name_hint=_hits_name_hint("iso_date", col_name),
                     min_rate=DEFAULT_MIN_MATCH_RATE)


def detect_us_date(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    return _evaluate("us_date", _series_str(series), _US_DATE_RE,
                     name_hint=_hits_name_hint("us_date", col_name),
                     min_rate=DEFAULT_MIN_MATCH_RATE)


def detect_eu_date(series: pd.Series, col_name: str) -> Optional[DetectorMatch]:
    return _evaluate("eu_date", _series_str(series), _EU_DATE_RE,
                     name_hint=_hits_name_hint("eu_date", col_name),
                     min_rate=DEFAULT_MIN_MATCH_RATE)


# ── registry ──────────────────────────────────────────────────────────────────

DetectorFn = Callable[[pd.Series, str], Optional[DetectorMatch]]

REGISTERED_DETECTORS: list[DetectorFn] = [
    detect_email,
    detect_ssn,
    detect_us_phone,
    detect_us_zip,
    detect_person_name,
    detect_iso_date,
    detect_us_date,
    detect_eu_date,
]


def run_all_detectors(series: pd.Series, col_name: str) -> list[DetectorMatch]:
    """Run every registered detector against a column. Returns matches sorted
    by descending match_rate so the highest-confidence detector is first."""
    matches: list[DetectorMatch] = []
    for fn in REGISTERED_DETECTORS:
        m = fn(series, col_name)
        if m is not None:
            matches.append(m)
    matches.sort(key=lambda m: m.match_rate, reverse=True)
    return matches
