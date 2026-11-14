"""Smart-default mask strategy and starter choice per detector.

Single source of truth that FORECAST and the platform's column-override
endpoint both consume. When Pre-scan (or a manual override) attaches a
detector_id to a column, this table answers "what's the right default
mask strategy + params?" so the user gets a sensible starting point
without re-deriving it in three places.

The user can always edit after the default is applied - the values here
are starting points, not locks. Detection sprint (V1) chose conservative
defaults that prioritize safety (format-preserving where it's well-
defined, redact otherwise) over ergonomics.

One descriptor per detector id (`DETECTOR_DESCRIPTORS`) carries both the platform default and
the starter choice `decoy scan` writes. `DEFAULT_STRATEGY_BY_DETECTOR` is derived from it, and
the starter decision algorithm in `_starter_inference.py` reads it; neither keeps a second
detector table.

Cross-references:
  - decoy-platform/docs/product/format-preservation.md publishes the
    user-visible coverage table sourced from this module.
  - decoy-platform/docs/backlog/v2/plans/decoy-platform/2026-05-17-detection-and-fpe-gaps.md
    tracks the redact-default detectors that V2 will upgrade to
    semantic-aware FPE (icd10, url, license_num, ...).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# (strategy_name, params_dict) - strategy_name is the canonical mask kind
# (faker.first_name / fpe / date_shift / redact / ...). The platform
# routes these through the mask registry without further translation.
StrategyDefault = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class StarterChoice:
    """What `decoy scan` writes for a column a detector flagged.

    `strategy` and `provider` name the mask kind and the faker provider (None unless the
    strategy is faker). `provider_config` is the strategy's starter settings. `review` is the
    fixed copy that goes above the column; it names detectors and strategies, never a value.
    """

    strategy: str
    provider: str | None = None
    provider_config: Mapping[str, Any] = MappingProxyType({})
    review: str = ""


@dataclass(frozen=True)
class DetectorDescriptor:
    """Everything the engine decides for one detector id, in one place.

    `default` is the platform's smart default (`DEFAULT_STRATEGY_BY_DETECTOR`); `starter` is
    the choice `decoy scan` writes into a starter pipeline. A detector may carry either or both.
    """

    detector_id: str
    default: StrategyDefault | None = None
    starter: StarterChoice | None = None


# NOTE: param names here MUST match the engine strategy classes:
#   faker        -> faker_type, locale, ...    (transforms/faker_based.py)
#   fpe          -> charset, preserve_separators, validate_luhn
#                                              (transforms/fpe.py)
#   date_shift   -> jitter_days                (transforms/date_shift.py)
#   redact       -> redact_with                (transforms/redact.py)
# The override endpoint surfaces these directly to the mask
# inspector, so a mismatch here silently corrupts the user's
# configured pipeline at build time.
#
# FPE preserves the input length naturally; there's no "length"
# param. The `preserve_separators: true` default keeps non-charset
# characters (dashes, spaces, dots, slashes) at their original
# positions, so "123-45-6789" stays dash-formatted after masking.

_DESCRIPTORS: tuple[DetectorDescriptor, ...] = (
    # ── personal identifiers (names + contact) ──────────────────────────
    DetectorDescriptor(
        detector_id="first_name",
        default=("faker", {"faker_type": "first_name", "locale": "en_US"}),
    ),
    DetectorDescriptor(
        detector_id="last_name",
        default=("faker", {"faker_type": "last_name", "locale": "en_US"}),
    ),
    DetectorDescriptor(
        detector_id="person_name",
        default=("faker", {"faker_type": "name", "locale": "en_US"}),
        starter=StarterChoice(
            strategy="faker",
            provider="person_full_name",
            review=(
                "The scan detected person names. Faker replaces with synthetic names. If "
                "this column is a first OR last name only, swap the provider to "
                "person_first_name or person_last_name."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="email",
        default=("faker", {"faker_type": "email"}),
        starter=StarterChoice(
            strategy="faker",
            provider="person_email",
            review=("The scan detected email addresses. Faker replaces with synthetic emails."),
        ),
    ),
    DetectorDescriptor(
        detector_id="us_phone",
        default=("faker", {"faker_type": "phone_number", "locale": "en_US"}),
        starter=StarterChoice(
            strategy="faker",
            provider="person_phone",
            review=("The scan detected US phone numbers. Faker replaces with synthetic phone."),
        ),
    ),
    DetectorDescriptor(
        detector_id="fax_number",
        default=("faker", {"faker_type": "phone_number", "locale": "en_US"}),
    ),
    DetectorDescriptor(
        detector_id="address",
        default=("faker", {"faker_type": "address", "locale": "en_US"}),
    ),
    # ── government / national identifiers ───────────────────────────────
    DetectorDescriptor(
        detector_id="ssn",
        default=("fpe", {"charset": "digits"}),
        starter=StarterChoice(
            strategy="hash",
            review=(
                "The scan detected SSNs. Hash preserves joinability without exposing the "
                "value. If you need real-shape output (333-22-4444), use provider: "
                "synthetic_ssn instead."
            ),
        ),
    ),
    # ── dates ───────────────────────────────────────────────────────────
    DetectorDescriptor(
        detector_id="iso_date",
        default=("date_shift", {"jitter_days": 15}),
        starter=StarterChoice(
            strategy="date_shift",
            provider_config={"min_days": -365, "max_days": 365},
            review=(
                "The scan detected ISO-format dates. date_shift shifts each date by an "
                "independent per-value offset in [min_days, max_days] (same source "
                "value always gets the same shift, but shifts are NOT globally order-"
                "preserving across rows). Defaults are min_days: -365 / max_days: 365; "
                "tune via provider_config."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="us_date",
        default=("date_shift", {"jitter_days": 15}),
        starter=StarterChoice(
            strategy="date_shift",
            provider_config={"min_days": -365, "max_days": 365},
            review=(
                "The scan detected US-format dates. date_shift shifts each date by an "
                "independent per-value offset in [min_days, max_days] (not order-"
                "preserving across rows). Defaults are -365/+365 days; tune via "
                "provider_config."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="eu_date",
        default=("date_shift", {"jitter_days": 15}),
    ),
    # ── geolocation ─────────────────────────────────────────────────────
    DetectorDescriptor(
        detector_id="us_zip",
        default=("fpe", {"charset": "digits"}),
        starter=StarterChoice(
            strategy="truncate",
            provider_config={"length": 3},
            review=(
                "The scan detected US ZIP codes. truncate to 3 digits matches HIPAA Safe "
                "Harbor; if your downstream needs full ZIP, use faker + provider: "
                "address_zip instead."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="ipv4",
        default=("faker", {"faker_type": "ipv4"}),
        starter=StarterChoice(
            strategy="redact",
            review=(
                "The scan detected IPv4 addresses. redact removes the value; if you need "
                "realistic IP-shaped output for analytics, swap to faker + provider: "
                "random_choice over a known IP range."
            ),
        ),
    ),
    # ── healthcare identifiers ──────────────────────────────────────────
    DetectorDescriptor(
        detector_id="mrn",
        default=("fpe", {"charset": "alphanum"}),
        starter=StarterChoice(
            strategy="hash",
            review=(
                "The scan detected medical record numbers. Hash preserves joinability. If "
                "you need real-shape output, use provider: synthetic_mrn."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="npi",
        default=("fpe", {"charset": "digits"}),
        starter=StarterChoice(
            strategy="hash",
            review=(
                "The scan detected NPI numbers. Hash preserves joinability. If you need "
                "real-shape output, use provider: synthetic_npi."
            ),
        ),
    ),
    # ── financial identifiers ───────────────────────────────────────────
    DetectorDescriptor(
        detector_id="pan",
        default=("fpe", {"charset": "digits", "validate_luhn": True}),
        starter=StarterChoice(
            strategy="fpe",
            provider_config={"charset": "digits", "validate_luhn": True},
            review=(
                "The scan detected payment card numbers (PAN). fpe (format-preserving "
                "encryption) with charset: digits + validate_luhn: true keeps the "
                "masked output Luhn-valid, matching the bundled PCI template. The FPE "
                "key itself is derived automatically per job/namespace -- no key config"
                " needed -- but a namespace IS required (see below)."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="cvv",
        default=("redact", {"redact_with": "XXX"}),
        starter=StarterChoice(
            strategy="redact",
            review=(
                "The scan detected CVV. PCI DSS 3.3 requires CVV not be stored post-"
                "authorization; redact removes the value entirely."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="iban",
        default=("fpe", {"charset": "ALPHANUM"}),
        starter=StarterChoice(
            strategy="hash",
            review=(
                "The scan detected IBANs. Hash preserves joinability. If you need real-"
                "shape output, use provider: synthetic_iban."
            ),
        ),
    ),
    # ── vehicle ─────────────────────────────────────────────────────────
    DetectorDescriptor(
        detector_id="vehicle_id",
        default=("fpe", {"charset": "ALPHANUM"}),
    ),
    # ── redact-default group ────────────────────────────────────────────
    # Semantic / structural format preservation is V2 (see gap doc).
    # Until then, redact is the safe default - preserving "shape" without
    # semantics would be misleading (e.g. a fake ICD-10 that happens to
    # mean a different disease).
    DetectorDescriptor(
        detector_id="icd10",
        default=("redact", {"redact_with": "REDACTED"}),
        starter=StarterChoice(
            strategy="truncate",
            provider_config={"length": 3},
            review=(
                "The scan detected ICD-10 codes. truncate to 3 chars (category) matches "
                "HIPAA Safe Harbor."
            ),
        ),
    ),
    DetectorDescriptor(
        detector_id="url",
        default=("redact", {"redact_with": "REDACTED"}),
    ),
    DetectorDescriptor(
        detector_id="license_num",
        default=("redact", {"redact_with": "REDACTED"}),
    ),
    DetectorDescriptor(
        detector_id="health_plan_id",
        default=("redact", {"redact_with": "REDACTED"}),
    ),
    DetectorDescriptor(
        detector_id="device_id",
        default=("redact", {"redact_with": "REDACTED"}),
    ),
    DetectorDescriptor(
        detector_id="biometric_id",
        default=("redact", {"redact_with": "REDACTED"}),
    ),
    # ── starter-only detectors (no platform default) ────────────────────
    # `decoy scan` suggests a strategy for these; the platform override
    # endpoint has no default for them, so they stay out of the derived view.
    DetectorDescriptor(
        detector_id="uuid",
        starter=StarterChoice(
            strategy="faker",
            provider="uuid",
            review=("The scan detected UUIDs. Faker generates fresh synthetic UUIDs."),
        ),
    ),
    DetectorDescriptor(
        detector_id="address_street",
        starter=StarterChoice(
            strategy="faker",
            provider="address_street",
            review=("The scan detected street addresses. Faker replaces with synthetic addresses."),
        ),
    ),
    DetectorDescriptor(
        detector_id="address_city",
        starter=StarterChoice(
            strategy="faker",
            provider="address_city",
            review=("The scan detected city names. Faker replaces with synthetic cities."),
        ),
    ),
    DetectorDescriptor(
        detector_id="ndc",
        starter=StarterChoice(
            strategy="hash",
            review=(
                "The scan detected NDC codes. Hash preserves joinability. If you need "
                "real-shape output, use provider: synthetic_ndc."
            ),
        ),
    ),
)

DETECTOR_DESCRIPTORS: Mapping[str, DetectorDescriptor] = MappingProxyType(
    {descriptor.detector_id: descriptor for descriptor in _DESCRIPTORS}
)

# The platform defaults, derived from the descriptors. Kept as the public name the platform and
# the override endpoint already import; its content is unchanged (pinned by
# tests/fixtures/scan_multi/default_strategy_by_detector_base.json).
DEFAULT_STRATEGY_BY_DETECTOR: dict[str, StrategyDefault] = {
    descriptor.detector_id: (descriptor.default[0], dict(descriptor.default[1]))
    for descriptor in _DESCRIPTORS
    if descriptor.default is not None
}


def starter_choice(detector_id: str) -> StarterChoice | None:
    """The starter choice for a detector id, or None when the detector has none."""
    descriptor = DETECTOR_DESCRIPTORS.get(detector_id)
    return None if descriptor is None else descriptor.starter


# Valid strategy names accepted across the mask layer. Used by the
# get_default_strategy validator + the test suite to detect typos in the
# lookup above. Adding a new strategy here without wiring it in the mask
# registry is harmless - it just widens the validator. Removing one
# without updating the lookup will break the regression test.
VALID_STRATEGIES: frozenset[str] = frozenset(
    {
        "faker",
        "fpe",
        "date_shift",
        "redact",
    }
)


def get_default_strategy(detector_id: str) -> StrategyDefault | None:
    """Return the default (strategy_name, params) for a detector_id.

    Returns None when the detector_id isn't in the table - callers should
    treat that as "no smart default; leave the column to manual config".
    Custom detector ids (e.g. "custom__uk_nhs_number") will always miss
    this table by design.
    """
    return DEFAULT_STRATEGY_BY_DETECTOR.get(detector_id)


def known_detector_ids() -> frozenset[str]:
    """The set of built-in detector_ids that have a smart-default strategy.

    Used by the override UI to populate the "Set field type" dropdown
    without re-listing detectors in the platform layer.
    """
    return frozenset(DEFAULT_STRATEGY_BY_DETECTOR.keys())
