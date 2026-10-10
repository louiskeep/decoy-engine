"""Shared corpus and builders for the C6b-i (text_mask as an ARROW_PYTHON operator) tests.

text_mask is KEYED and handler-rich, so unlike text_redact these configs exercise the fpe, faker
and date_shift span branches and the sub-floor warning / fail-closed paths. Every comparison is
against the shipped `TextMaskHandler` (the pandas oracle), so a value here never needs a
hand-written expected output.

Two corpora: `CORPUS` holds no value that drives an fpe span below the FF1 domain floor, so the
default config (no `sub_floor_span`) masks it without failing; `SUB_FLOOR_TEXTS` carries 5-digit
us_zip values (domain 10**5, below the floor) for the warning- and failure-parity tests, which set
an explicit policy.
"""

from __future__ import annotations

from typing import Any

# No 5-digit us_zip, no pan/npi/iban: every default-fpe detector here (ssn, us_phone) clears the
# FF1 million-domain floor, so the default no-policy config never fails closed. Repeated SSNs span
# cells so the faker-override branch's cross-cell consistency is observable.
TEXTS: list[str] = [
    "Patient John, SSN 123-45-6789, seen today.",
    "email a@b.com or call (555) 123-4567 tomorrow",
    "ip 10.0.0.1 and 192.168.1.1 logged",
    "see https://example.org/path?q=1 now",
    "no pii here at all",
    "",
    "123-45-6789",
    "123-45-6789 and 123-45-6789",
    "naïve café 日本語 SSN 123-45-6789 ünï x@y.io 😀",
    "contact 987-65-4321 or (800) 555-0199",
    "😀😀 a@b.com",
    "ssn 123-45-6789 again here",
]

CORPUS: list[str | None] = [t if i % 5 != 3 else None for i, t in enumerate(TEXTS * 2)] + [None]

# A 5-digit us_zip is below the FF1 floor; us_zip defaults to the fpe span strategy, so these drive
# the sub-floor path (warning when a policy is set, fail-closed when it is not).
SUB_FLOOR_TEXTS: list[str | None] = [
    "home zip 12345 on file",
    "zip 12345 and zip 67890 listed",
    None,
    "no zip here",
    "zip 90210 recorded",
    "",
]

SHAPES: dict[str, list[str | None]] = {
    "corpus": CORPUS,
    "empty": [],
    "all_null": [None] * 6,
    "single_row": ["ssn 123-45-6789"],
    "empty_strings": ["", "", None, ""],
    "ragged": [None, "a@b.com", "", None, "x 123-45-6789 y", None],
}

# Admitted configurations: the plain text_mask the plan lifts onto both native routes. All are inert
# over `CORPUS` with respect to the sub-floor path (no sub-floor value), so the default (no policy)
# config masks it without failing; the keyed span branches (fpe, faker, date_shift) still run.
ADMITTED_CONFIGS: dict[str, dict[str, Any]] = {
    "default": {},
    "detectors_null": {"detectors": None},
    "detectors_one": {"detectors": ["ssn"]},
    "detectors_several": {"detectors": ["ssn", "email", "ipv4"]},
    "detectors_empty_means_all": {"detectors": []},
    "detectors_unknown_noop": {"detectors": ["no_such_detector"]},
    "detectors_tuple": {"detectors": ("ssn", "email")},
    "token_custom": {"token": "<X>"},
    "token_empty": {"token": ""},
    "unmatched_passthrough": {"unmatched_span_policy": "passthrough"},
    "unmatched_replace": {"unmatched_span_policy": "replace_with_token"},
    "per_detector_ssn_faker": {"per_detector_strategy": {"ssn": "faker"}},
    "per_detector_ssn_redact": {"per_detector_strategy": {"ssn": "redact"}},
    "per_detector_ssn_date_shift": {
        "per_detector_strategy": {"ssn": "date_shift"},
        "min_days": -10,
        "max_days": 10,
    },
    "per_detector_phone_faker": {"per_detector_strategy": {"us_phone": "faker"}},
    "sub_floor_redact_inert": {"sub_floor_span": "redact"},
    "sub_floor_synthetic_inert": {"sub_floor_span": "synthetic"},
}

# Configurations that stay on the oracle, with the code each reports on the eligibility report.
# `ner` is exercised through the requirement resolver because spaCy is optional. Unlike text_redact,
# a non-string token and a malformed `detectors` do NOT decline (text_mask normalizes both as the
# oracle does), so they are not here.
EXCLUDED_CONFIGS: dict[str, tuple[dict[str, Any], str]] = {
    "ner_true": ({"ner": True}, "text_mask_ner_not_native"),
    "ner_dict": ({"ner": {"model": "en_core_web_sm"}}, "text_mask_ner_not_native"),
}


def tm_col(name: str = "s", **provider_config: Any) -> dict[str, Any]:
    """A text_mask column. Keyword arguments become its `provider_config` verbatim."""
    col: dict[str, Any] = {"name": name, "strategy": "text_mask"}
    if provider_config:
        col["provider_config"] = provider_config
    return col
