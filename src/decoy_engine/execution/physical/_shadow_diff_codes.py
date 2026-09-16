"""Task 4.4 C4: the frozen, privacy-safe shadow difference/failure-code
catalog.

Each code records LOCATION + TYPE only -- never a source value, key, or
derived material. `ShadowDifference` is the exception the coordinator and
the comparison harness raise for a coded difference; `detail` must stay to
that same privacy discipline. Closure is asserted by
`tests/physical/test_shadow_diff_catalog.py`.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "CELL_VALUE_DIFF",
    "DIAGNOSTICS_DIFF",
    "DIFFERENCE_CODES",
    "DUPLICATE_NODE_DECLARATION",
    "FAKER_POOL_NON_STRING_OUTPUT",
    "NATIVE_COMPANION_UNAVAILABLE",
    "NULL_MASK_DIFF",
    "OPERATOR_NOT_EXECUTED",
    "PLANNED_VS_ACTUAL_ROUTE_DIFF",
    "PUBLICATION_ATTEMPT",
    "RESOURCE_LIMIT_BREACH",
    "ROW_COUNT_DIFF",
    "ROW_ORDER_DIFF",
    "SCHEMA_DIFF",
    "SNAPSHOT_IDENTITY_DIFF",
    "ShadowDifference",
]

SCHEMA_DIFF: Final = "schema-diff"
ROW_COUNT_DIFF: Final = "row-count-diff"
ROW_ORDER_DIFF: Final = "row-order-diff"
CELL_VALUE_DIFF: Final = "cell-value-diff"
NULL_MASK_DIFF: Final = "null-mask-diff"
DIAGNOSTICS_DIFF: Final = "diagnostics-diff"
PLANNED_VS_ACTUAL_ROUTE_DIFF: Final = "planned-vs-actual-route-diff"
OPERATOR_NOT_EXECUTED: Final = "operator-not-executed"
SNAPSHOT_IDENTITY_DIFF: Final = "snapshot-identity-diff"
RESOURCE_LIMIT_BREACH: Final = "resource-limit-breach"
PUBLICATION_ATTEMPT: Final = "publication-attempt"
# A plan whose nodes carry a duplicate `node_id` (a config declaring the same
# column + strategy twice; config accepts it). Two such nodes would collapse
# into one `route_evidence` record and one output column, hiding a node. The
# bounded slice treats a duplicate declaration as malformed and surfaces it
# with this code rather than silently masking the collision.
DUPLICATE_NODE_DECLARATION: Final = "duplicate-node-declaration"
# The coded shadow FAILURE for a missing/ABI-incompatible compiled hash
# companion (C2): the coordinator translates `CryptoExtensionUnavailableError`
# (`native/_crypto_ext.py:110`) into this code and never falls back to the
# oracle. Underscored (not hyphenated) to match the live exception's own
# code-naming convention, since it is a direct translation of it.
NATIVE_COMPANION_UNAVAILABLE: Final = "native_companion_unavailable"
# The shadow faker operator (`sample_faker_array`) always emits `pa.string()`;
# admission only checks the provider NAME + poolability + string SOURCE type,
# never the pool's actual value type. A custom `ProviderRegistry.override()`
# can rebind an allowlisted name to a poolable adapter that yields non-string
# values, which would otherwise crash the Arrow cast instead of diverging
# through a coded, privacy-safe difference.
FAKER_POOL_NON_STRING_OUTPUT: Final = "faker-pool-non-string-output"

DIFFERENCE_CODES: Final[frozenset[str]] = frozenset(
    {
        SCHEMA_DIFF,
        ROW_COUNT_DIFF,
        ROW_ORDER_DIFF,
        CELL_VALUE_DIFF,
        NULL_MASK_DIFF,
        DIAGNOSTICS_DIFF,
        PLANNED_VS_ACTUAL_ROUTE_DIFF,
        OPERATOR_NOT_EXECUTED,
        SNAPSHOT_IDENTITY_DIFF,
        RESOURCE_LIMIT_BREACH,
        PUBLICATION_ATTEMPT,
        NATIVE_COMPANION_UNAVAILABLE,
        DUPLICATE_NODE_DECLARATION,
        FAKER_POOL_NON_STRING_OUTPUT,
    }
)


class ShadowDifference(Exception):  # noqa: N818 -- plan-named (TASK-4.4-PLAN.md C4), not an *Error type.
    """One coded shadow difference or failure.

    `detail` is free text for a human reading a test failure, but it must
    never carry a source value, key, or derived material -- only location
    (table/node/column) and difference type, per the design doc's
    diagnostics-privacy rule (section 7).
    """

    def __init__(self, code: str, detail: str) -> None:
        if code not in DIFFERENCE_CODES:
            raise ValueError(f"{code!r} is not in the frozen difference-code catalog")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")
