"""Shared corpus and builders for the C6c-i (text_redact as an Arrow operator) tests.

The corpus covers every built-in detector, overlapping candidates, adjacent spans, spans at
the start and end of a cell, multi-byte Unicode before and inside spans, a cell with no span
and the empty string. Every comparison is against the pandas oracle, so a value here never
needs a hand-written expected output.
"""

from __future__ import annotations

from typing import Any

TEXTS: list[str] = [
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
    "naïve café 日本語 SSN 123-45-6789 ünï 4111 1111 1111 1111 😀 x@y.io",
    "overlap 12345-6789 4111111111111111 1234567893",
    "😀😀 a@b.com",
]

CORPUS: list[str | None] = [t if i % 7 != 3 else None for i, t in enumerate(TEXTS * 2)] + [None]

# One text with a known hit, used by the direct kernel contract tests.
KNOWN_HIT = "mail a@b.com now"

SHAPES: dict[str, list[str | None]] = {
    "corpus": CORPUS,
    "empty": [],
    "all_null": [None] * 6,
    "single_row": ["ssn 123-45-6789"],
    "empty_strings": ["", "", None, ""],
    "ragged": [None, "a@b.com", "", None, "x 123-45-6789 y", None],
}

# Admitted configurations: the plain text_redact the plan lifts onto both native routes.
ADMITTED_CONFIGS: dict[str, dict[str, Any]] = {
    "default": {},
    "detectors_null": {"detectors": None},
    "detectors_one": {"detectors": ["email"]},
    "detectors_several": {"detectors": ["email", "ssn", "ipv4"]},
    "detectors_empty_list_means_all": {"detectors": []},
    "detectors_unknown_is_noop": {"detectors": ["no_such_detector"]},
    "detectors_known_and_unknown": {"detectors": ["ssn", "no_such_detector"]},
    "detectors_tuple": {"detectors": ("email", "ssn")},
    "token_custom": {"token": "<X>"},
    "token_regex_metacharacters": {"token": "\\1$&(?i)"},
    "token_empty": {"token": ""},
    "label_token": {"label_token": True},
    "label_token_ignores_token": {"label_token": True, "token": "<X>"},
    "label_token_false_custom_token": {"label_token": False, "token": "T"},
    "label_token_with_detectors": {"label_token": True, "detectors": ["ssn", "email"]},
}

# Configurations that stay on the oracle, with the code each must report on the eligibility
# report. `ner` is exercised through the requirement resolver because spaCy is optional.
EXCLUDED_CONFIGS: dict[str, tuple[dict[str, Any], str]] = {
    "ner_true": ({"ner": True}, "text_redact_ner_not_native"),
    "ner_dict": ({"ner": {"model": "en_core_web_sm"}}, "text_redact_ner_not_native"),
    "token_int": ({"token": 7}, "text_redact_token_not_string"),
    "token_none": ({"token": None}, "text_redact_token_not_string"),
    "detectors_string": ({"detectors": "email"}, "text_redact_detectors_malformed"),
    "detectors_int": ({"detectors": 3}, "text_redact_detectors_malformed"),
}

# The subset that runs end to end without spaCy; each leaves the source column unchanged.
PASS_THROUGH_CONFIGS: dict[str, tuple[dict[str, Any], str]] = {
    name: spec for name, spec in EXCLUDED_CONFIGS.items() if not name.startswith("ner")
}


def tr_col(name: str = "s", **provider_config: Any) -> dict[str, Any]:
    """A text_redact column. Keyword arguments become its `provider_config` verbatim."""
    col: dict[str, Any] = {"name": name, "strategy": "text_redact"}
    if provider_config:
        col["provider_config"] = provider_config
    return col
