"""Engine-owned retention contract (F26): ``retention-policy/v1`` and ``retention-report/v1``.

Pure types, closed reason and exit-code registries and one canonical serializer. No locator, no
backend SDK and no deletion lives here: the platform owns lifecycle state and adapters, the CLI
owns local manifest handling, and both construct typed inputs and serialize only through this
package so the vocabularies cannot drift.

Methodology: dry-run-then-force confirmation follows git-clean; the content-addressed inventory
digest binds an execution to the exact reviewed set.
"""

from __future__ import annotations

from decoy_engine.retention._canonical import canonical_json, check_opaque_id, is_opaque_id
from decoy_engine.retention._policy import (
    DEFAULT_POLICY,
    MAX_WINDOW_HOURS,
    MIN_WINDOW_HOURS,
    POLICY_SCHEMA,
    ClassPolicy,
    PolicyKey,
    PolicyMode,
    RetentionPolicy,
)
from decoy_engine.retention._report import (
    ALLOWED_COUNT_KEYS,
    ALLOWED_ITEM_KEYS,
    ALLOWED_LIMIT_KEYS,
    ALLOWED_REPORT_KEYS,
    REPORT_SCHEMA,
    Counts,
    Limits,
    ReportItem,
    RetentionReport,
    counts_from_items,
    derive_outcome,
    inventory_digest,
    render_text,
)
from decoy_engine.retention._vocab import (
    REASON_SUMMARY,
    TARGET_CLASSES,
    ArtifactClass,
    Backend,
    DataClass,
    Decision,
    ExitCode,
    ReasonCode,
    ReportKind,
    RunOutcome,
    exit_code_for,
)

__all__ = [
    "ALLOWED_COUNT_KEYS",
    "ALLOWED_ITEM_KEYS",
    "ALLOWED_LIMIT_KEYS",
    "ALLOWED_REPORT_KEYS",
    "DEFAULT_POLICY",
    "MAX_WINDOW_HOURS",
    "MIN_WINDOW_HOURS",
    "POLICY_SCHEMA",
    "REASON_SUMMARY",
    "REPORT_SCHEMA",
    "TARGET_CLASSES",
    "ArtifactClass",
    "Backend",
    "ClassPolicy",
    "Counts",
    "DataClass",
    "Decision",
    "ExitCode",
    "Limits",
    "PolicyKey",
    "PolicyMode",
    "ReasonCode",
    "ReportItem",
    "ReportKind",
    "RetentionPolicy",
    "RetentionReport",
    "RunOutcome",
    "canonical_json",
    "check_opaque_id",
    "counts_from_items",
    "derive_outcome",
    "exit_code_for",
    "inventory_digest",
    "is_opaque_id",
    "render_text",
]
