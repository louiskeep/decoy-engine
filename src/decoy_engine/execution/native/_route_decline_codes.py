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

from decoy_engine import reasons


class RouteDeclineCode(str, enum.Enum):
    """One member per distinct decline cause. The string values are the serialized form and
    are stable: rename a member, never its value. `OTHER` is for genuinely unknown input only."""

    # Table-wide causes.
    FK_RELATIONSHIP = reasons.route.FK_RELATIONSHIP
    NO_MASK_NODES = reasons.route.NO_MASK_NODES
    NON_SCALAR_NODE = reasons.route.NON_SCALAR_NODE
    ADAPTER_REQUESTED = reasons.route.ADAPTER_REQUESTED
    UNCOVERED_OR_MISSING_COLUMNS = reasons.route.UNCOVERED_OR_MISSING_COLUMNS
    CRYPTO_EXT_UNAVAILABLE = reasons.route.CRYPTO_EXT_UNAVAILABLE
    FPE_EXT_UNAVAILABLE = reasons.route.FPE_EXT_UNAVAILABLE
    INDEX_EXT_UNAVAILABLE = reasons.route.INDEX_EXT_UNAVAILABLE
    RAW_HEX_EXT_UNAVAILABLE = reasons.route.RAW_HEX_EXT_UNAVAILABLE
    UNCONFIGURED_SET_MISMATCH = reasons.route.UNCONFIGURED_SET_MISMATCH
    # Defined for completeness but never surfaced publicly: an empty source reaches no
    # per-chunk builder, and its reroute reason is the unchanged `empty_input` string.
    EMPTY_INPUT = reasons.route.EMPTY_INPUT
    # Column-attributable causes.
    STRATEGY_VETOED_CHUNKED = reasons.route.STRATEGY_VETOED_CHUNKED
    GROUP_KEY_SELF_ANCHOR = reasons.route.GROUP_KEY_SELF_ANCHOR
    GROUP_KEY_MASKED_SIBLING = reasons.route.GROUP_KEY_MASKED_SIBLING
    FALLBACK_POLICY_NOT_NATIVE = reasons.route.FALLBACK_POLICY_NOT_NATIVE
    NO_NATIVE_KERNEL_OR_POOL = reasons.route.NO_NATIVE_KERNEL_OR_POOL
    WHEN_PREDICATE_NOT_NATIVE = reasons.route.WHEN_PREDICATE_NOT_NATIVE
    WHEN_OUTSIDE_SUBSET = reasons.route.WHEN_OUTSIDE_SUBSET
    WHEN_READS_MASKED_COLUMN = reasons.route.WHEN_READS_MASKED_COLUMN
    FAKER_SOURCE_NOT_STRING = reasons.route.FAKER_SOURCE_NOT_STRING
    FAKER_SCHEMA_NOT_GUARANTEED = reasons.route.FAKER_SCHEMA_NOT_GUARANTEED
    FAKER_METADATA_NOT_ALLOWLISTED = reasons.route.FAKER_METADATA_NOT_ALLOWLISTED
    FPE_CHECKSUM_NOT_NATIVE = reasons.route.FPE_CHECKSUM_NOT_NATIVE
    HASH_CONFIG_REJECTED = reasons.route.HASH_CONFIG_REJECTED
    FAKER_PROVIDER_NOT_NATIVE = reasons.route.FAKER_PROVIDER_NOT_NATIVE
    SOURCE_TYPE_NOT_STRING = reasons.route.SOURCE_TYPE_NOT_STRING
    GROUP_KEY_SIBLING_MISSING = reasons.route.GROUP_KEY_SIBLING_MISSING
    GROUP_KEY_SIBLING_TYPE_NOT_NATIVE = reasons.route.GROUP_KEY_SIBLING_TYPE_NOT_NATIVE
    FAKER_PROVIDER_OUTPUT_NOT_STRING = reasons.route.FAKER_PROVIDER_OUTPUT_NOT_STRING
    OTHER = reasons.route.OTHER


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
