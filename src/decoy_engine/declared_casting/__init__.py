"""Declared parsing, casting and protected quarantine (F06).

One engine-owned boundary applies a column's versioned contract (``decoy_engine.contracts.
declared_casting``) before masking and renders it after. Public names are re-exported here; this
package imports no execution module at import time (route owners import it lazily), so it stays
a leaf of the dependency graph.
"""

from __future__ import annotations

from decoy_engine._lexeme import lexeme_columns_by_table, profile_descriptors
from decoy_engine.declared_casting._boundary import DeclaredCastRun, arrow_type
from decoy_engine.declared_casting._catalog import (
    FormatDescriptor,
    capability_manifest,
    compile_formatter,
    compile_parser,
    descriptor_for,
    descriptors,
    format_value,
    match_sentinel,
    max_rendered_width,
    output_width,
    parse_value,
    validate_format_spec,
    validate_sentinel_spec,
)
from decoy_engine.declared_casting._counts import CastIssue, CastIssueLedger
from decoy_engine.declared_casting._eligibility import (
    ROUTES,
    CastRouteDecision,
    RuntimeCapabilities,
    declared_route_guard,
    evaluate_declared_casting,
    preflight_declared_casting,
)
from decoy_engine.declared_casting._quarantine_local import LocalProtectedQuarantineSink
from decoy_engine.declared_casting._quarantine_protocol import (
    OpaqueRetrievalToken,
    ProtectedQuarantineRuntime,
    ProtectedQuarantineSink,
    PurgeResult,
    QuarantineAccessContext,
    QuarantineCapability,
    QuarantineEnvelope,
    QuarantinePolicy,
    QuarantineReceipt,
    QuarantineRecord,
    QuarantineRun,
    QuarantineSession,
)
from decoy_engine.declared_casting._types import CastOutcome

__all__ = [
    "ROUTES",
    "CastIssue",
    "CastIssueLedger",
    "CastOutcome",
    "CastRouteDecision",
    "DeclaredCastRun",
    "FormatDescriptor",
    "LocalProtectedQuarantineSink",
    "OpaqueRetrievalToken",
    "ProtectedQuarantineRuntime",
    "ProtectedQuarantineSink",
    "PurgeResult",
    "QuarantineAccessContext",
    "QuarantineCapability",
    "QuarantineEnvelope",
    "QuarantinePolicy",
    "QuarantineReceipt",
    "QuarantineRecord",
    "QuarantineRun",
    "QuarantineSession",
    "RuntimeCapabilities",
    "arrow_type",
    "capability_manifest",
    "compile_formatter",
    "compile_parser",
    "declared_route_guard",
    "descriptor_for",
    "descriptors",
    "evaluate_declared_casting",
    "format_value",
    "lexeme_columns_by_table",
    "match_sentinel",
    "max_rendered_width",
    "output_width",
    "parse_value",
    "preflight_declared_casting",
    "profile_descriptors",
    "validate_format_spec",
    "validate_sentinel_spec",
]
