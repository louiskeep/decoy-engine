"""Catalog of the ``provider`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Outcome, RetryClass

__all__ = [
    "BATCH_DETERMINISTIC_UNSUPPORTED",
    "BLOCKLIST_EXHAUSTED",
    "CAPABILITY_VIOLATION",
    "DETERMINISTIC_REQUIRES_NAMESPACE_AND_SEED",
    "DETERMINISTIC_REQUIRES_SOURCE_VALUE",
    "INVALID_CUSIP_CHAR",
    "INVALID_INPUT_LENGTH",
    "MISSING_NAMESPACE",
    "MISSING_SEED",
    "NAMESPACE_EMPTY",
    "NER_MODEL_NOT_INSTALLED",
    "NER_SPACY_NOT_INSTALLED",
    "SEED_WRONG_LENGTH",
    "UNKNOWN_PROVIDER",
    "UNSUPPORTED_LOCALE",
]

_d = _catalog.provider("AdapterError")

CAPABILITY_VIOLATION = _d(
    "capability_violation",
    "Capability violation.",
    outcome=Outcome.EXECUTION,
    retry=RetryClass.UNKNOWN,
)
UNKNOWN_PROVIDER = _d(
    "unknown_provider", "Unknown provider.", outcome=Outcome.EXECUTION, retry=RetryClass.UNKNOWN
)
UNSUPPORTED_LOCALE = _d(
    "unsupported_locale", "Unsupported locale.", outcome=Outcome.EXECUTION, retry=RetryClass.UNKNOWN
)

_d = _catalog.provider("IdentifierError")

BLOCKLIST_EXHAUSTED = _d("blocklist_exhausted", "Blocklist exhausted.")
INVALID_CUSIP_CHAR = _d("invalid_cusip_char", "Invalid cusip char.")
INVALID_INPUT_LENGTH = _d("invalid_input_length", "Invalid input length.")

_d = _catalog.provider("NerUnavailableError")

NER_MODEL_NOT_INSTALLED = _d("ner_model_not_installed", "NER model not installed.")
NER_SPACY_NOT_INSTALLED = _d("ner_spacy_not_installed", "NER spacy not installed.")

_d = _catalog.provider("ProviderError")

BATCH_DETERMINISTIC_UNSUPPORTED = _d(
    "batch_deterministic_unsupported", "Batch deterministic unsupported."
)
DETERMINISTIC_REQUIRES_NAMESPACE_AND_SEED = _d(
    "deterministic_requires_namespace_and_seed", "Deterministic requires namespace and seed."
)
DETERMINISTIC_REQUIRES_SOURCE_VALUE = _d(
    "deterministic_requires_source_value", "Deterministic requires source value."
)
MISSING_NAMESPACE = _d("missing_namespace", "Missing namespace.")
MISSING_SEED = _d("missing_seed", "Missing seed.")
NAMESPACE_EMPTY = _d("namespace_empty", "Namespace empty.")
SEED_WRONG_LENGTH = _d("seed_wrong_length", "Seed wrong length.")
