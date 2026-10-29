"""decoy_engine.detection - PII/format span detection (the base layer).

Formerly part of the `storm` package; split by concern in the STORM taxonomy
rename. Detection is the base layer: profiling and verification depend on it,
never the reverse. `storm` is retained only as a product label on user-facing
and wire surfaces (the `run_storm` config field, the `"storm.scan"` event, the
`/storm/...` routes); the internal package name is descriptive.
"""

from decoy_engine.detection.detectors import REGISTERED_DETECTORS, Span
from decoy_engine.detection.domains import DOMAIN_BY_DETECTOR, Domain, domain_for
from decoy_engine.detection.name_hints.loader import load_name_hint_terms
from decoy_engine.detection.ner import NerUnavailableError
from decoy_engine.detection.types import CustomDetectorSpec, DetectionSignal, DetectorMatch

__all__ = [
    "DOMAIN_BY_DETECTOR",
    "REGISTERED_DETECTORS",
    "CustomDetectorSpec",
    "DetectionSignal",
    "DetectorMatch",
    "Domain",
    "NerUnavailableError",
    "Span",
    "domain_for",
    "load_name_hint_terms",
]
