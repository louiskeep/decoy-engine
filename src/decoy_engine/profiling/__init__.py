"""decoy_engine.profiling - column classification + profile recommendations.

Public entry point:
    profile_columns(df, source_label, *, sample_strategy=...) -> ColumnProfile

A ColumnProfile is a JSON-serializable summary of a dataset: per-field stats,
PII detector hits, format signals, sentinel-value flags, plus dataset-level
re-identification risk. It is the only thing FORECAST sees; FORECAST never
touches raw data.

A ColumnProfile's `to_dict()` is the raw form. Output meant to be saved or shown goes
through `profile_document` (or `redact_profile_dict` for a loaded document), which removes
cell values of value-sensitive columns unless `include_values=True`. A distribution
snapshot meant to be written goes through `distribution_snapshot_document`, which does the
same for the snapshot's stats.

Pre-scan phase: the profiling entrypoint is `profile_columns`, returning a
`ColumnProfile`. Depends on `decoy_engine.detection`.
"""

from decoy_engine.profiling._scan_integrity import (
    KEY_BATCH_ROWS,
    ChildFacts,
    ChildKeyFacts,
    KeyBatches,
    KeyFacts,
    RelationshipFacts,
    column_distinct,
    key_facts,
    keys_comparable,
)
from decoy_engine.profiling._snapshot_values import (
    distribution_snapshot_document,
    snapshot_joint_warnings,
)
from decoy_engine.profiling._value_redaction import (
    MIN_RELEASE_COUNT,
    POLICY_RAW,
    POLICY_REDACTED,
    REDACTED_COLUMNS_KEY,
    VALUE_POLICY_KEY,
    is_value_sensitive,
    profile_document,
    redact_profile_dict,
)
from decoy_engine.profiling.profiler import profile_columns
from decoy_engine.profiling.recommendations import (
    DEFAULT_STRATEGY_BY_DETECTOR,
    StrategyDefault,
    get_default_strategy,
    known_detector_ids,
)
from decoy_engine.profiling.scan_set import (
    SCAN_SET_SCHEMA,
    ScanSet,
    ScanSetCore,
    ScanSetCoreRead,
    ScanSourceError,
    TableScan,
    scan_set_to_dict,
    scan_sources,
)
from decoy_engine.profiling.types import (
    ColumnProfile,
    Distribution,
    FieldStats,
    SentinelFlag,
    TopValue,
)

__all__ = [
    "DEFAULT_STRATEGY_BY_DETECTOR",
    "KEY_BATCH_ROWS",
    "MIN_RELEASE_COUNT",
    "POLICY_RAW",
    "POLICY_REDACTED",
    "REDACTED_COLUMNS_KEY",
    "SCAN_SET_SCHEMA",
    "VALUE_POLICY_KEY",
    "ChildFacts",
    "ChildKeyFacts",
    "ColumnProfile",
    "Distribution",
    "FieldStats",
    "KeyBatches",
    "KeyFacts",
    "RelationshipFacts",
    "ScanSet",
    "ScanSetCore",
    "ScanSetCoreRead",
    "ScanSourceError",
    "SentinelFlag",
    "StrategyDefault",
    "TableScan",
    "TopValue",
    "column_distinct",
    "distribution_snapshot_document",
    "get_default_strategy",
    "is_value_sensitive",
    "key_facts",
    "keys_comparable",
    "known_detector_ids",
    "profile_columns",
    "profile_document",
    "redact_profile_dict",
    "scan_set_to_dict",
    "scan_sources",
    "snapshot_joint_warnings",
]
