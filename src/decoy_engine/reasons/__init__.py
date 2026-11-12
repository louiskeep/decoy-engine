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
    platform,
    post_scan,
    pre_scan,
    provider,
    quality,
    route,
    runtime,
    safe_output,
    subset,
    verification,
    warning,
)
from decoy_engine.reasons._identity import (
    REASONS,
    REGISTRY_SCHEMA,
    ReasonRef,
    UnknownReasonError,
    get_reason,
    require_known,
)
from decoy_engine.reasons._record import (
    COUNT_KEYS,
    MAX_COUNTS,
    REGISTRY_VERSION_RE,
    CleanupState,
    Operation,
    PublicationState,
    ReasonContext,
    ReasonRecord,
    approve_name,
    approve_ref,
)
from decoy_engine.reasons._record import _is_count as is_count
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
    pending_summary_edits,
    specs,
)
from decoy_engine.reasons._types import (
    UNRELEASED,
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
    "COUNT_KEYS",
    "EVENT_SCHEMA_VERSION",
    "MAX_COUNTS",
    "REASONS",
    "REASON_SCHEMA_VERSION",
    "REGISTRY_SCHEMA",
    "REGISTRY_VERSION",
    "REGISTRY_VERSION_RE",
    "UNRELEASED",
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
    "ReasonRef",
    "ReasonSpec",
    "RetryClass",
    "Severity",
    "UnknownReasonError",
    "approve_name",
    "approve_ref",
    "check_integrity",
    "config",
    "connector",
    "decoy",
    "find",
    "get",
    "get_reason",
    "is_count",
    "is_registered",
    "lookup",
    "manifest",
    "namespaces",
    "pending_summary_edits",
    "physical",
    "plan",
    "platform",
    "post_scan",
    "pre_scan",
    "provider",
    "quality",
    "require_known",
    "route",
    "runtime",
    "safe_output",
    "specs",
    "subset",
    "verification",
    "warning",
]
