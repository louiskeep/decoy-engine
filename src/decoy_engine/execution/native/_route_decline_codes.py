"""Structured, leak-safe route-decline records for the chunked native route (OBS-2b).

`NativeRouteEvidence.reroute_reason` is a composite free-text string that embeds column,
strategy and type text, so a consumer cannot attribute a decline to a column without
parsing identifiers that may contain the delimiter. This module defines the closed code
vocabulary and the one record shape that rides next to it: a code, the table, the column
(when the cause is inherently per-column) and `table_wide` (the CAUSAL scope of the cause,
independent of the routing consequence that every decline sends the whole table to the
oracle).

Records are emitted at each decision source, where the column is known, never at the
generic `_downgrade_to_oracle` helper, which erases per-column attribution. The record
holds an enum code and schema identifiers only: no provider, expression, exception or
type text.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class RouteDeclineCode(str, enum.Enum):
    """One member per distinct decline cause. The string values are the serialized form and
    are stable: rename a member, never its value. `OTHER` is for genuinely unknown input only."""

    # Table-wide causes.
    FK_RELATIONSHIP = "fk_relationship"
    NO_MASK_NODES = "no_mask_nodes"
    NON_SCALAR_NODE = "non_scalar_node"
    ADAPTER_REQUESTED = "adapter_requested"
    UNCOVERED_OR_MISSING_COLUMNS = "uncovered_or_missing_columns"
    CRYPTO_EXT_UNAVAILABLE = "crypto_ext_unavailable"
    FPE_EXT_UNAVAILABLE = "fpe_ext_unavailable"
    INDEX_EXT_UNAVAILABLE = "index_ext_unavailable"
    RAW_HEX_EXT_UNAVAILABLE = "raw_hex_ext_unavailable"
    UNCONFIGURED_SET_MISMATCH = "unconfigured_set_mismatch"
    # Defined for completeness but never surfaced publicly: an empty source reaches no
    # per-chunk builder, and its reroute reason is the unchanged `empty_input` string.
    EMPTY_INPUT = "empty_input"
    # Column-attributable causes.
    STRATEGY_VETOED_CHUNKED = "strategy_vetoed_chunked"
    GROUP_KEY_SELF_ANCHOR = "group_key_self_anchor"
    GROUP_KEY_MASKED_SIBLING = "group_key_masked_sibling"
    FALLBACK_POLICY_NOT_NATIVE = "fallback_policy_not_native"
    NO_NATIVE_KERNEL_OR_POOL = "no_native_kernel_or_pool"
    WHEN_PREDICATE_NOT_NATIVE = "when_predicate_not_native"
    WHEN_OUTSIDE_SUBSET = "when_outside_subset"
    WHEN_READS_MASKED_COLUMN = "when_reads_masked_column"
    FAKER_SOURCE_NOT_STRING = "faker_source_not_string"
    FAKER_SCHEMA_NOT_GUARANTEED = "faker_schema_not_guaranteed"
    FAKER_METADATA_NOT_ALLOWLISTED = "faker_metadata_not_allowlisted"
    FPE_CHECKSUM_NOT_NATIVE = "fpe_checksum_not_native"
    HASH_CONFIG_REJECTED = "hash_config_rejected"
    FAKER_PROVIDER_NOT_NATIVE = "faker_provider_not_native"
    SOURCE_TYPE_NOT_STRING = "source_type_not_string"
    GROUP_KEY_SIBLING_MISSING = "group_key_sibling_missing"
    GROUP_KEY_SIBLING_TYPE_NOT_NATIVE = "group_key_sibling_type_not_native"
    FAKER_PROVIDER_OUTPUT_NOT_STRING = "faker_provider_output_not_string"
    OTHER = "other"


@dataclass(frozen=True)
class RouteDeclineRecord:
    """One decline cause. `column is None` exactly when `table_wide`."""

    code: RouteDeclineCode
    table: str
    column: str | None
    table_wide: bool

    def __post_init__(self) -> None:
        if (self.column is None) != self.table_wide:
            raise ValueError("RouteDeclineRecord: column is None if and only if table_wide")

    def to_payload(self) -> dict[str, object]:
        """The JSON-safe public form: exactly code, table, column, table_wide."""
        return {
            "code": self.code.value,
            "table": self.table,
            "column": self.column,
            "table_wide": self.table_wide,
        }


@dataclass(frozen=True)
class CodedDecline:
    """A decision source's verdict: the legacy reason string (unchanged) plus its record."""

    reason: str
    record: RouteDeclineRecord


def table_decline(code: RouteDeclineCode, table: str) -> RouteDeclineRecord:
    return RouteDeclineRecord(code, table, None, True)


def column_decline(code: RouteDeclineCode, table: str, column: str) -> RouteDeclineRecord:
    return RouteDeclineRecord(code, table, column, False)


def coded(
    reason: str, code: RouteDeclineCode, table: str, column: str | None = None
) -> CodedDecline:
    """Pair a legacy reason with its record; a `None` column is a table-wide cause."""
    record = table_decline(code, table) if column is None else column_decline(code, table, column)
    return CodedDecline(reason, record)


def decline_payload(records: tuple[RouteDeclineRecord, ...]) -> list[dict[str, object]]:
    return [r.to_payload() for r in records]


__all__ = [
    "CodedDecline",
    "RouteDeclineCode",
    "RouteDeclineRecord",
    "coded",
    "column_decline",
    "decline_payload",
    "table_decline",
]
