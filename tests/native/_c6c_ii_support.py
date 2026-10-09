"""Shared corpus, oracle and companion gate for the C6c-ii (text_redact Rust span kernel) tests.

The oracle is the literal C6c-i Python path (`iter_spans` + `_splice` with C6c-i's stringification),
so every comparison is native-vs-oracle and no expected output is hand-written. The corpus mixes
eligible (ASCII-safe) and ineligible (non-ASCII, a 0x1c-0x1f separator) cells, every built-in
detector, overlapping/adjacent/boundary spans, the two named tie counterexamples, empty, null and
non-string cells. A lone surrogate cannot live in a `pa.string()` array, so it is exercised at the
kernel surface directly (`test_c6c_ii_text_redact_rust.py`), not through this corpus.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

from decoy_engine.execution._strategies._text_redact import _DEFAULT_TOKEN, _splice
from decoy_engine.execution.native._text_redact_kernel import load_text_redact_kernel
from decoy_engine.storm.detectors import _SPAN_DETECTORS, iter_spans

# The eight Rust-supported detectors and the three that always stay Python (plan 2).
RUST_DETECTORS = ("email", "us_phone", "pan", "iban", "ipv4", "icd10", "npi", "url")
PYTHON_DETECTORS = ("ssn", "us_zip", "street_address")

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_TR_KERNEL = load_text_redact_kernel()

# "Companion present" for C6c-ii means the NEW span kernel loads (symbol present, catalog agrees),
# not merely that the package is importable -- an older companion skips these, the companion-present
# CI job runs them.
NEEDS_TR_KERNEL = pytest.mark.skipif(
    _TR_KERNEL is None,
    reason="decoy-engine-native text_redact span kernel not available (old or absent companion)",
)

ELIGIBLE_TEXTS: list[str] = [
    "Patient John, SSN 123-45-6789, seen today.",
    "email a@b.com or call (555) 123-4567 tomorrow",
    "ip 10.0.0.1 and 192.168.1.1 logged",
    "card 4111 1111 1111 1111 expired",
    "zip 12345 npi 1234567893 icd E11.9 noted",
    "see https://example.org/path?q=1 now",
    "12 Main Street, Springfield",
    "iban GB82WEST12345698765432 on file",
    "no pii here at all",
    "",
    "123-45-6789",
    "123-45-6789 and 123-45-6789",
    "a@b.comc@d.org",
    "overlap 12345-6789 4111111111111111 1234567893",
    "contact DR.SMITH@CLINIC.ORG re: MRN and icd a01.9 lower-case",
    "2234567891",  # named tie: npi vs us_phone both span (0,10)
    "12345",  # named tie: us_zip (python) vs would-be location NER at (0,5)
    "phones 212-555-1234 and (212) 555-1234 x",
    "IBAN DE89370400440532013000 wired, pan 5555555555554444 done",
    "url https://a.co/x?y=1#f and mail x@y.io end",
]

# Ineligible cells: each carries a non-ASCII code point or a 0x1c-0x1f separator, so the kernel
# returns None for it and the Python path handles it byte-identically.
INELIGIBLE_TEXTS: list[str] = [
    "naïve café 日本語 SSN 123-45-6789 ünï 4111 1111 1111 1111 \U0001f600 x@y.io",
    "😀😀 a@b.com",
    "separator 212\x1c555\x1c1234 inside",  # 0x1c between phone groups (round-2 divergence case)
    "unit\x1fseparator a@b.com and 10.0.0.1",
    "émail josé@exÿ.com with 1234567893",
]

CORPUS: list[str | None] = []
_merged = ELIGIBLE_TEXTS + INELIGIBLE_TEXTS
for i, t in enumerate(_merged * 2):
    CORPUS.append(None if i % 7 == 3 else t)
CORPUS.append(None)

SHAPES: dict[str, list[str | None]] = {
    "corpus": CORPUS,
    "eligible_only": list(ELIGIBLE_TEXTS),
    "ineligible_only": list(INELIGIBLE_TEXTS),
    "empty": [],
    "all_null": [None] * 5,
    "ragged": [None, "a@b.com", "", None, "x 123-45-6789 y", "café", None],
}

# Detector-selection permutations: None (all), supported-only, python-only, mixed, and the two
# tie-sensitive orderings over npi/us_phone (both Rust-run).
DETECTOR_SELECTIONS: dict[str, tuple[str, ...] | None] = {
    "all": None,
    "email_only": ("email",),
    "rust_subset": ("email", "ipv4", "pan"),
    "python_only": ("ssn", "us_zip", "street_address"),
    "mixed": ("ssn", "email", "us_zip", "ipv4"),
    "tie_npi_then_phone": ("npi", "us_phone"),
    "tie_phone_then_npi": ("us_phone", "npi"),
    "dup_email": ("email", "ssn", "email"),
    "unknown_id": ("no_such_detector", "email"),
}


def oracle_text_redact(
    values: list[Any],
    *,
    detectors: tuple[str, ...] | None,
    token: str = _DEFAULT_TOKEN,
    label_token: bool = False,
) -> list[str | None]:
    """The C6c-i Python path: stringify non-null non-strings, then `iter_spans` + `_splice`."""
    detector_ids = list(detectors) if detectors is not None else None
    out: list[str | None] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        text = v if isinstance(v, str) else str(v)
        spans = iter_spans(text, detector_ids, extra_spans=None)
        out.append(text if not spans else _splice(text, spans, token, label_token))
    return out


def python_raw_candidates(text: str, det_id: str) -> list[tuple[int, int]]:
    """One detector's raw ordered (start, end) candidates via Python `finditer` + validator,
    exactly as `iter_spans` collects them before the overlap sweep."""
    regex, validator = _SPAN_DETECTORS[det_id]
    out: list[tuple[int, int]] = []
    for m in regex.finditer(text):
        matched = m.group(0)
        if validator is not None and not validator(matched):
            continue
        out.append((m.start(), m.end()))
    return out
