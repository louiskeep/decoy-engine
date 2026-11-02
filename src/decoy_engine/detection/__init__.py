"""decoy_engine.detection - PII/format span detection (the base layer).

Detection is the base layer shared by the Pre-scan (profiling) and Post-scan
(verification) phases: both depend on it, never the reverse.
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
