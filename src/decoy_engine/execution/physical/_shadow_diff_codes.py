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
    "FULL_FRAME_DISPATCH_MISSING_DEPENDENCY",
    "FULL_FRAME_DRIVER_UNSUPPORTED",
    "FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED",
    "FULL_FRAME_SUBSTRATE_UNSUPPORTED",
    "GENERATION_SHAPE_UNSUPPORTED",
    "GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED",
    "GLOBAL_STRATEGY_UNSUPPORTED",
    "MIXED_DRIVER_UNSUPPORTED",
    "MIXED_FK_CROSS_GENERATE_UNSUPPORTED",
    "MIXED_FK_TOPOLOGY_UNSUPPORTED",
    "MIXED_FK_UNADMITTED_CHILD",
    "NATIVE_COMPANION_UNAVAILABLE",
    "NULL_MASK_DIFF",
    "OOC_DISPATCH_MISSING_DEPENDENCY",
    "OOC_FK_PARITY_DIFF",
    "OPERATOR_INVARIANT_VIOLATION",
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
# Task 4.6 slice 3 (widened by slice 5b-i): a compiled plan whose mask-table
# driver set is not exactly `{OUT_OF_CORE}` but still contains it -- mixed
# with another masking driver, or (slice 5b-i) present at all on an
# otherwise-admitted generate+mask dispatch. OOC-mask + generation is not a
# future capability this deferral waits on: a mixed job always routes
# full_frame/pandas in production (`_pipeline_routing.py`'s `generate_plus_
# mask` never selects out_of_core), so this shape can never legitimately
# arise from a real compiled plan -- the check exists as a defensive total
# guard, matching the same shape's own hand-forced test coverage. The
# coordinator refuses to dispatch part of a plan through the OOC adapter and
# mask the rest scalar, or to dispatch a mixed job whose mask half needs OOC,
# rather than silently picking one. `detail` names the driver set only
# (never a table/column value).
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
# Task 4.6 slice 5a (widened by slice 5b-i): a generate-bearing plan's shape
# falls outside the admitted domain for one of the coordinator's two
# synthesis-owning gates -- `_shadow_generation.require_pure_generation_
# shadowable` (no mask tables at all) or `_shadow_mixed.require_independent_
# mixed_shadowable` (mask tables present, independent of the generate half).
# Both share the same identity/column-shape core (`require_generation_
# shape`): a missing/malformed/mismatched `ShadowContext.plan`, an
# unsupported generate-column type, or `determinism: fresh`. The pure gate
# additionally requires an empty sources/relationships/namespaces/subset/
# transforms/validators/quarantine/run_storm/mask_secret_ref and every
# runtime carrier at its admitted value; the mixed gate instead requires no
# job-level validators/quarantine/vault-writer/fidelity-reporting/mask_
# secret_ref, no sink/source_loader, and (via `ShadowContext.relationship_
# graph`) that the graph itself is present. `detail` names the failed
# structural check + a table/field LOCATION only, never a leaf knob's value
# (neither gate inspects one).
GENERATION_SHAPE_UNSUPPORTED: Final = "generation-shape-unsupported"
# Task 4.6 slice 5b-i (widened by slice 5b-ii): a generate-parent ->
# mask-child relationship edge exists but this slice's FK-coupling admission
# rule cannot resolve it -- a COMPOSITE key (either side's column tuple has
# more than one column), or a single-column key whose Arrow type is not
# admitted (int/string only). `detail` names the parent/child table.column
# LOCATION and, for a type miss, the Arrow type name -- never a row value.
# The reverse direction (a mask-parent referenced by a generate child) is
# already rejected upstream, at generation-config validation, so it never
# reaches this gate.
MIXED_FK_CROSS_GENERATE_UNSUPPORTED: Final = "mixed-fk-cross-generate-unsupported"
# Task 4.6 slice 5b-ii: a generate-parent -> mask-child edge's KEY SHAPE is
# admitted (single-column int/string), but the surrounding relationship
# GRAPH is not -- more than one crossing generate->mask edge, another
# incoming edge to the admitted child, an outgoing edge from the child or
# the generated parent that reaches a mask table, or any other relationship
# edge touching a mask table anywhere in this mixed run. The admission gate
# declines the WHOLE coupling rather than guess which edge should win;
# multi-parent/multi-level FK-through-generate stays a tracked deferral, not
# a silent partial resolution. `detail` names the LOCATION only.
MIXED_FK_TOPOLOGY_UNSUPPORTED: Final = "mixed-fk-topology-unsupported"
# Task 4.6 slice 5b-ii: the coordinator's per-node mask loop found an FK-
# child node (`relationship_graph.parents_of` is non-empty for it) that is
# NOT the one edge the admission gate allowlisted for this run. The
# admission gate's own complete-graph rule already guarantees this can never
# happen for a properly-admitted plan, so this is a defensive total guard,
# not an expected runtime path: masking an unadmitted FK child scalar
# (ignoring its parent) would silently diverge from the oracle, so it
# declines coded instead. `detail` names the table LOCATION only.
MIXED_FK_UNADMITTED_CHILD: Final = "mixed-fk-unadmitted-child"
# Task 4.6 slice 6: the FULL_FRAME dispatch's own catalog -- see
# `_shadow_full_frame.py`'s module docstring for the full admission gate
# each code guards. `detail` stays to the same location-only discipline as
# every code above: a count, a strategy name, a table/column location, or a
# field name that was absent -- never a source value.
FULL_FRAME_DRIVER_UNSUPPORTED: Final = "full-frame-driver-unsupported"
FULL_FRAME_SUBSTRATE_UNSUPPORTED: Final = "full-frame-substrate-unsupported"
FULL_FRAME_DISPATCH_MISSING_DEPENDENCY: Final = "full-frame-dispatch-missing-dependency"
GLOBAL_STRATEGY_UNSUPPORTED: Final = "global-strategy-unsupported"
GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED: Final = "global-shuffle-determinism-unsupported"
FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED: Final = "full-frame-runtime-feature-unsupported"
# ^ also covers item 5's FK half: a relationship edge naming the one
# admitted table (sink/source_loader/vault/fidelity/validators/quarantine
# share this code too -- see _shadow_full_frame.py's module docstring).
# A bound operator broke its own contract: a compiled index kernel returned a
# wrong-typed/sized/null/out-of-range result (the `index_batch_*`
# `GenerationError`s), or run_operator's dispatch preconditions failed. Either
# means the native path itself is broken, so it must surface loudly; silently
# rerouting to the oracle would hide a defective kernel behind the slow path.
# Input-domain failures (unparseable or out-of-range data) are NOT this code.
# `detail` names the operator and the error code/class only.
OPERATOR_INVARIANT_VIOLATION: Final = "operator-invariant-violation"

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
        MIXED_FK_CROSS_GENERATE_UNSUPPORTED,
        MIXED_FK_TOPOLOGY_UNSUPPORTED,
        MIXED_FK_UNADMITTED_CHILD,
        FULL_FRAME_DRIVER_UNSUPPORTED,
        FULL_FRAME_SUBSTRATE_UNSUPPORTED,
        FULL_FRAME_DISPATCH_MISSING_DEPENDENCY,
        GLOBAL_STRATEGY_UNSUPPORTED,
        GLOBAL_SHUFFLE_DETERMINISM_UNSUPPORTED,
        FULL_FRAME_RUNTIME_FEATURE_UNSUPPORTED,
        OPERATOR_INVARIANT_VIOLATION,
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
