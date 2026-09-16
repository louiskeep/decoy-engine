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
    "GENERATION_SHAPE_UNSUPPORTED",
    "MIXED_DRIVER_UNSUPPORTED",
    "NATIVE_COMPANION_UNAVAILABLE",
    "NULL_MASK_DIFF",
    "OOC_DISPATCH_MISSING_DEPENDENCY",
    "OOC_FK_PARITY_DIFF",
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
# Task 4.6 slice 3: a compiled plan whose mask-table driver set is not exactly
# `{OUT_OF_CORE}` but still contains it (mixed with another masking driver, or
# paired with a synthesis stage) -- the coordinator refuses to dispatch part of
# a plan through the OOC adapter and mask the rest scalar, rather than silently
# picking one. `detail` names the driver set only (never a table/column value).
MIXED_DRIVER_UNSUPPORTED: Final = "mixed-driver-unsupported"
# The runtime OOC carriers (`ShadowContext.plan`/`.relationship_graph`, or the
# coordinator's `registry`) are declared `| None` for back-compat with the
# scalar/chunked callers that never set them, but a `{OUT_OF_CORE}` dispatch
# genuinely requires all three. This code names which carrier was absent
# instead of the dispatch failing on a bare, uncoded `None` downstream.
OOC_DISPATCH_MISSING_DEPENDENCY: Final = "ooc-dispatch-missing-dependency"
# The OOC-specific comparator's value-equal check against the full_frame
# oracle (distinct from the five FULL_FRAME/CHUNKED diff codes above, which a
# reused comparator cannot express for OOC's documented normalizations).
# `detail` carries a column name + a count, never a cell value.
OOC_FK_PARITY_DIFF: Final = "ooc-fk-parity-diff"
# Task 4.6 slice 5a: a pure-generate plan's shape falls outside the admitted
# domain for the coordinator's synthesis dispatch (`_shadow_coordinator.
# _require_generation_shadowable`) -- a synthesis+mask mix, a missing/
# malformed/mismatched `ShadowContext.plan`, an unsupported generate-column
# type, `determinism: fresh`, a non-empty sources/relationships/namespaces/
# subset/transforms/validators/quarantine/run_storm/mask_secret_ref, or a
# non-admitted runtime carrier (derive_key/instance_default_locale/
# key_provider/sink/source_loader/vault_writer/fidelity_report). `detail`
# names the failed structural check + a table/field LOCATION only, never a
# leaf knob's value (the gate never inspects one).
GENERATION_SHAPE_UNSUPPORTED: Final = "generation-shape-unsupported"

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
        MIXED_DRIVER_UNSUPPORTED,
        OOC_DISPATCH_MISSING_DEPENDENCY,
        OOC_FK_PARITY_DIFF,
        GENERATION_SHAPE_UNSUPPORTED,
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
