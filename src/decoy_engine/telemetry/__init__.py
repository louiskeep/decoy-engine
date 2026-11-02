"""Engine span recorder for customer-controlled telemetry (``decoy-telemetry/v1``).

The engine records closed spans and imports no OpenTelemetry package; the Platform and the
CLI own the SDK, the exporter, the endpoint and the flush.
"""

from __future__ import annotations

from decoy_engine.telemetry._recorder import SpanRecorder
from decoy_engine.telemetry._vocab import (
    ATTRIBUTE_KEYS,
    RESOURCE_KEYS,
    SPAN_NAMES,
    TELEMETRY_SCHEMA,
    SpanRecord,
)

__all__ = [
    "ATTRIBUTE_KEYS",
    "RESOURCE_KEYS",
    "SPAN_NAMES",
    "TELEMETRY_SCHEMA",
    "SpanRecord",
    "SpanRecorder",
]
