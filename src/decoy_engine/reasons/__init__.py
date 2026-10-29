"""The engine-owned, versioned registry of machine-readable reason codes (F29).

One catalog assigns every reason a stable ``(namespace, code)`` identity, a fixed safe summary,
a kind, the phases it may occur in and, per phase, a severity, outcome category and retry class.
Producers reference the symbols in the namespace modules below (``reasons.runtime.X``); the
symbol's value is the legacy wire string, so existing ``.code`` values are unchanged byte for byte.

Codes are append-only. A retired code stays readable as a tombstone with its replacement link.
Nothing here imports execution, planning, the CLI or the platform: it is a leaf that they import.
Consumers (CLI, platform) read the published manifest instead of keeping a second taxonomy.
"""

from __future__ import annotations

from decoy_engine.reasons import (
    config,
    connector,
    decoy,
    physical,
    plan,
    provider,
    quality,
    route,
    runtime,
    subset,
    verification,
    warning,
)
from decoy_engine.reasons._record import (
    CleanupState,
    Operation,
    PublicationState,
    ReasonContext,
    ReasonRecord,
    approve_name,
    approve_ref,
)
from decoy_engine.reasons._registry import (
    EVENT_SCHEMA_VERSION,
    REASON_SCHEMA_VERSION,
    REGISTRY_VERSION,
    check_integrity,
    find,
    get,
    is_registered,
    lookup,
    manifest,
    namespaces,
    specs,
)
from decoy_engine.reasons._types import (
    Kind,
    Outcome,
    Phase,
    PhaseRule,
    ReasonCode,
    ReasonSpec,
    RetryClass,
    Severity,
)

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "REASON_SCHEMA_VERSION",
    "REGISTRY_VERSION",
    "CleanupState",
    "Kind",
    "Operation",
    "Outcome",
    "Phase",
    "PhaseRule",
    "PublicationState",
    "ReasonCode",
    "ReasonContext",
    "ReasonRecord",
    "ReasonSpec",
    "RetryClass",
    "Severity",
    "approve_name",
    "approve_ref",
    "check_integrity",
    "config",
    "connector",
    "decoy",
    "find",
    "get",
    "is_registered",
    "lookup",
    "manifest",
    "namespaces",
    "physical",
    "plan",
    "provider",
    "quality",
    "route",
    "runtime",
    "specs",
    "subset",
    "verification",
    "warning",
]
