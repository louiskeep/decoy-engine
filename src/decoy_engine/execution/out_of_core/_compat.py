"""Compatibility gate for the out-of-core FK route.

The gate lives beside the polars adapter instead of inside the future execution
operator so routing has one decision surface: polars-native, out-of-core, or
oracle/rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge


_CROSS_ROW_STRATEGIES = frozenset(
    {
        "grouped_series",
        "windowed_date",
        "derived_aggregate",
        "group_key",
        "joint_mask",
    }
)
_INITIAL_SUPPORTED_STRATEGIES = frozenset({"hash", "redact", "truncate", "passthrough"})
# SC3 Group (b): per-value / source-conditioned strategies ported onto the
# out-of-core kernel with proven byte-parity (see `_mask_group_b.py`). Admitted
# for masked (payload) columns only; the PARENT-KEY surface (`_check_edge`)
# stays `_INITIAL_SUPPORTED_STRATEGIES` because none of these are ported for the
# join/remap key path, so an FK edge keyed on one is a fail-closed MISS.
_GROUP_B_SUPPORTED_STRATEGIES = frozenset({"fpe", "text_redact", "categorical"})
# SC4 Group (c): `text_mask` ports unconditionally (per-value HMAC span mask, no
# cross-row state). `code_set` and `bucket_perturb` are admitted only for the
# config shapes the out-of-core kernel reproduces byte-for-byte -- code_set in
# mask mode without chapter_preserve, bucket_perturb with an explicit date_format
# (`_group_c_conditional_rejection` below; see `_mask_group_c.py`). Admitted for
# masked (payload) columns only; the PARENT-KEY surface (`_check_edge`) stays
# `_INITIAL_SUPPORTED_STRATEGIES`, so a Group (c) FK key is a fail-closed MISS.
_GROUP_C_ALWAYS_SUPPORTED = frozenset({"text_mask"})
_GROUP_C_CONDITIONAL = frozenset({"code_set", "bucket_perturb"})
_GROUP_C_SUPPORTED_STRATEGIES = _GROUP_C_ALWAYS_SUPPORTED | _GROUP_C_CONDITIONAL
_SUPPORTED_WORK_STRATEGIES = (
    _INITIAL_SUPPORTED_STRATEGIES | _GROUP_B_SUPPORTED_STRATEGIES | _GROUP_C_SUPPORTED_STRATEGIES
)
# SC3 deferred Group (b): investigated and NOT ported, each for a concrete
# reason that would otherwise cause a route-dependent divergence. A MISS here
# means the job falls back to sequential/full-frame (which handle them), never a
# wrong output. `faker` needs a registry-backed ValuePool + a cross-batch pool
# cache the registry-free out-of-core kernel has no channel for. `bucketize` and
# `date_shift` record per-value format errors (uncoercible numeric / unparseable
# date) that the full-frame path removes via the D8 quarantine pass, but the
# out-of-core route returns before that pass and has no row-error channel;
# `date_shift` additionally needs whole-column format detection that does not
# chunk. See docs/plans/2026-07-07-next-up-roadmap.md (SC3) + `_mask_group_b.py`.
_DEFERRED_GROUP_B: dict[str, tuple[str, str]] = {
    "faker": (
        "out_of_core_faker_pool_unsupported",
        "faker needs a registry-backed value pool + cross-batch pool cache the "
        "out-of-core kernel has no channel for; falls back to sequential/full-frame.",
    ),
    "bucketize": (
        "out_of_core_row_error_strategy_unsupported",
        "bucketize records per-value format errors that full-frame quarantines "
        "(D8) but the out-of-core route has no row-error/quarantine channel; "
        "falls back to sequential/full-frame.",
    ),
    "date_shift": (
        "out_of_core_row_error_strategy_unsupported",
        "date_shift records per-value format errors (D8-quarantined full-frame) "
        "and needs whole-column format detection that does not chunk; the "
        "out-of-core route has neither, so it falls back to sequential/full-frame.",
    ),
}

# SC4 deferred Group (c): investigated and NOT ported, each for a concrete reason
# that would otherwise cause a route-dependent divergence. A MISS here means the
# job falls back to sequential/full-frame (which handle them), never a wrong
# output. `geo_generalize`'s k-anonymity cascade thresholds each row on WHOLE-
# DATASET counts, which a chunk cannot see. `formula` and `derived` emit a value
# whose Arrow type is not analytically determinable from the plan (the expression
# can change the column type), which the fixed-output-schema route cannot satisfy;
# `formula` additionally carries an order-dependent RNG channel and `derived`
# additionally needs same-row sibling-column context the per-column kernel does
# not receive. `nested` reuses the full pandas child-strategy dispatch plus a
# per-cell JSON round-trip, an architectural port beyond dispatch-widening (like
# SC3's faker). See docs/plans/2026-07-07-next-up-roadmap.md (SC4) + `_mask_group_c.py`.
_DEFERRED_GROUP_C: dict[str, tuple[str, str]] = {
    "geo_generalize": (
        "out_of_core_whole_column_aggregation_unsupported",
        "geo_generalize thresholds each row's cascade level on whole-dataset "
        "k-anonymity counts (ZIP5/ZIP3/state or H3 resolutions), which a chunk "
        "cannot see; not batch-local. Falls back to sequential/full-frame.",
    ),
    "formula": (
        "out_of_core_dynamic_output_type_unsupported",
        "formula emits a value whose Arrow type is not determinable from the plan "
        "(the expression can change the column type) and carries an order-dependent "
        "RNG channel; neither fits the fixed-schema streaming route. Falls back to "
        "sequential/full-frame.",
    ),
    "derived": (
        "out_of_core_dynamic_output_type_unsupported",
        "derived emits a value whose Arrow type is not determinable from the plan "
        "and needs same-row sibling-column context the per-column out-of-core mask "
        "kernel does not receive. Falls back to sequential/full-frame.",
    ),
    "nested": (
        "out_of_core_child_dispatch_unsupported",
        "nested reuses the full pandas child-strategy dispatch (SCALAR_HANDLERS) "
        "plus a per-cell JSON round-trip; porting needs the pandas handler stack "
        "per batch with the child strategy statically bounded to the batch-local "
        "set, beyond dispatch-widening. Falls back to sequential/full-frame.",
    ),
}

# Public alias (SC5, decoy-platform cross-repo query surface): the PAYLOAD
# (non-key) strategy set this gate currently admits, re-exported at
# `decoy_engine.execution` so an external caller (e.g. a platform-side coarse
# eligibility proxy that cannot afford to compile a real Plan pre-read) can
# consult the CURRENT admitted set instead of hardcoding a copy that would
# drift the moment SC3/SC4 widen it -- tracks `_SUPPORTED_WORK_STRATEGIES`,
# not `_INITIAL_SUPPORTED_STRATEGIES` alone, so it does not understate
# admission after a widening sprint.
#
# Two things this constant deliberately does NOT capture, by design: (1) the
# FK PARENT-KEY surface is narrower and independently gated by `_check_edge`
# against `_INITIAL_SUPPORTED_STRATEGIES` directly (never this alias) -- a
# strategy in this set may still be a fail-closed MISS as a join/remap key;
# (2) `code_set`/`bucket_perturb` are members here but only admitted for the
# config shapes `_group_c_conditional_rejection` allows, and each deferred
# strategy (`_DEFERRED_GROUP_B`/`_DEFERRED_GROUP_C`) plus the when-predicate/
# composite-fk/cycle checks above still apply per-job. This is a coarse,
# necessary-but-not-sufficient membership check; only
# `check_out_of_core_compatibility` is the authoritative decision.
SUPPORTED_STRATEGIES = _SUPPORTED_WORK_STRATEGIES


@dataclass(frozen=True)
class OutOfCoreRejection:
    code: str
    message: str


@dataclass(frozen=True)
class OutOfCoreCompatibility:
    accepted: bool
    rejections: tuple[OutOfCoreRejection, ...] = ()

    @property
    def primary_code(self) -> str | None:
        return self.rejections[0].code if self.rejections else None

    def message(self) -> str:
        return "; ".join(r.message for r in self.rejections)


def check_out_of_core_compatibility(
    plan: Plan,
    work: list[WorkNode],
    relationship_graph: RelationshipGraph,
) -> OutOfCoreCompatibility:
    """Return the current Option 4 compatibility decision.

    Initial acceptance covers kernelized per-value parent key strategies and no
    multiple-parent resolution for the same child FK tuple yet.
    """
    rejections: list[OutOfCoreRejection] = []
    if not relationship_graph.edges:
        return OutOfCoreCompatibility(
            accepted=False,
            rejections=(
                OutOfCoreRejection(
                    "out_of_core_no_relationships",
                    "out-of-core FK route requires at least one relationship edge.",
                ),
            ),
        )

    child_targets: dict[tuple[str, tuple[str, ...]], int] = {}
    composite_fk_child_columns: dict[str, set[str]] = {}
    for edge in relationship_graph.edges:
        key = (edge.child_table, edge.child_columns)
        child_targets[key] = child_targets.get(key, 0) + 1
        if len(edge.child_columns) > 1:
            composite_fk_child_columns.setdefault(edge.child_table, set()).update(
                edge.child_columns
            )
    if any(count > 1 for count in child_targets.values()):
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_multi_parent_child_unsupported",
                "out-of-core route does not yet support multiple parents for one child FK.",
            )
        )

    for edge in relationship_graph.edges:
        _check_edge(edge, plan, rejections)

    if _table_graph_has_cycle(relationship_graph):
        # `_table_order` (out_of_core/_route_policy.py) sequences whole TABLES, so any
        # table-level FK cycle across two or more tables (A.id->B.x, B.id->A.y)
        # makes `_table_order` raise `out_of_core_relationship_cycle` at run
        # time -- even when the COLUMN-level dependency the full-frame oracle
        # orders on is acyclic and the oracle succeeds. Reject fail-closed here
        # (the multi-table generalization of the self-referential edge rule
        # above) so the route never runs on a config it would crash on; the job
        # falls back to full-frame, which handles it natively.
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_relationship_cycle_unsupported",
                "out-of-core route requires an acyclic table-level FK graph; a "
                "multi-table FK cycle cannot be expressed by table-level dependency "
                "ordering (even when column-level ordering is acyclic). Falls back "
                "to full-frame.",
            )
        )

    for node in work:
        # GroupSeed (composite_fk_group) has no `when` field; getattr handles
        # both plan_slice shapes without a kind-based isinstance branch.
        if getattr(node.plan_slice, "when", None) is not None:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_when_predicate_unsupported",
                    f"{node.table}.{node.columns} carries a `when` predicate; the "
                    "out-of-core route masks every non-null row unconditionally and "
                    "has no row-gating, so admitting it would silently over-mask.",
                )
            )
            continue
        if node.kind == "composite_fk_group":
            if not _composite_group_join_covered(node, relationship_graph):
                rejections.append(
                    OutOfCoreRejection(
                        "out_of_core_composite_group_uncovered",
                        f"composite_fk_group {node.table}.{node.columns} has a column "
                        "not covered as a child FK column by any relationship edge; "
                        "join coverage can't be proven safe out-of-core.",
                    )
                )
            continue
        if node.kind != "scalar":
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_non_scalar_work_unsupported",
                    f"out-of-core route does not yet support {node.kind} work nodes.",
                )
            )
            continue
        if any(col in composite_fk_child_columns.get(node.table, ()) for col in node.columns):
            # A COMPOSITE FK child column masked as an independent scalar strategy
            # (rather than as one composite_fk_group over the whole key) is the
            # one composite shape that diverges from the pandas oracle. The oracle
            # masks each such column with its own strategy BEFORE resolving the FK
            # (FK children resolve last), so a PRESERVE/WARN orphan keeps the
            # scalar-MASKED value; the out-of-core route joins on and preserves the
            # RAW source value for the same orphan -- a raw-value leak (and, for a
            # partial-null key, a null-vs-masked divergence). The canonical
            # composite_fk_group shape (a single GroupSeed over the FK columns) is
            # oracle-parity across orphans, partial-nulls, and every policy, and
            # stays admitted; single-column scalar FK children stay admitted too
            # (they are FK-resolution-owned, not double-masked). Reject fail-closed
            # so the job falls back to full-frame instead of leaking. See CF2 in
            # docs/plans/2026-07-07-next-up-roadmap.md and
            # tests/parity/SEMANTIC_DIFFERENCES.md.
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_composite_fk_scalar_child_unsupported",
                    f"{node.table}.{node.columns} is a child column of a composite "
                    "FK edge but is masked as an independent scalar strategy; the "
                    "out-of-core route preserves the RAW source value for orphans "
                    "here while the pandas oracle preserves the scalar-masked value "
                    "(a raw-value leak divergence). Use a composite_fk_group over "
                    "the FK columns, which is oracle-parity; falls back to full-frame.",
                )
            )
            continue
        if node.strategy in _CROSS_ROW_STRATEGIES:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_cross_row_strategy_unsupported",
                    f"strategy {node.strategy!r} needs a bounded relational lowering.",
                )
            )
        elif node.strategy in _DEFERRED_GROUP_B:
            code, reason = _DEFERRED_GROUP_B[node.strategy]
            rejections.append(OutOfCoreRejection(code, f"{node.table}.{node.columns}: {reason}"))
        elif node.strategy in _DEFERRED_GROUP_C:
            code, reason = _DEFERRED_GROUP_C[node.strategy]
            rejections.append(OutOfCoreRejection(code, f"{node.table}.{node.columns}: {reason}"))
        elif node.strategy in _GROUP_C_CONDITIONAL:
            # code_set / bucket_perturb are ported only for the config shapes the
            # out-of-core kernel reproduces byte-for-byte; other shapes MISS.
            conditional = _group_c_conditional_rejection(node)
            if conditional is not None:
                rejections.append(conditional)
        elif node.strategy == "categorical" and not _is_deterministic(node.plan_slice):
            # Non-deterministic categorical draws from an unseeded RNG, so it has
            # no cross-run/cross-route parity; only the source-conditioned
            # deterministic path is ported. Reject fail-closed -> full-frame.
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_categorical_nondeterministic_unsupported",
                    f"{node.table}.{node.columns}: non-deterministic categorical has no "
                    "cross-route parity (unseeded RNG); only deterministic categorical "
                    "is out-of-core-supported. Falls back to full-frame.",
                )
            )
        elif node.strategy not in _SUPPORTED_WORK_STRATEGIES:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_strategy_unsupported",
                    f"strategy {node.strategy!r} is not in the out-of-core strategy surface.",
                )
            )

    return OutOfCoreCompatibility(accepted=not rejections, rejections=tuple(rejections))


def _group_c_conditional_rejection(node: WorkNode) -> OutOfCoreRejection | None:
    """Reject a conditionally-supported Group (c) node whose config shape the
    out-of-core kernel does not reproduce byte-for-byte; None admits it.

    code_set: mask mode without chapter_preserve only (gen mode threads a global
    row index the streaming kernel has no offset for; chapter_preserve records
    per-value errors the route has no quarantine channel to remove). bucket_perturb:
    an explicit date_format only (whole-column format auto-detection does not chunk).
    """
    cfg = dict(getattr(node.plan_slice, "provider_config", ()) or ())
    if node.strategy == "code_set":
        mode = str(cfg.get("mode", "mask"))
        if mode != "mask" or cfg.get("chapter_preserve"):
            return OutOfCoreRejection(
                "out_of_core_code_set_shape_unsupported",
                f"{node.table}.{node.columns}: out-of-core code_set supports mask mode "
                "without chapter_preserve only (gen mode needs a global row index the "
                "streaming kernel lacks; chapter_preserve records per-value errors the "
                "route cannot quarantine). Falls back to full-frame.",
            )
    elif node.strategy == "bucket_perturb" and not cfg.get("date_format"):
        return OutOfCoreRejection(
            "out_of_core_bucket_perturb_autodetect_unsupported",
            f"{node.table}.{node.columns}: out-of-core bucket_perturb requires an "
            "explicit date_format; whole-column format auto-detection does not chunk. "
            "Falls back to full-frame.",
        )
    return None


def _is_deterministic(plan_slice: object) -> bool:
    """True if a scalar work node's seed is in deterministic mode.

    Only scalar (ColumnSeed) nodes carry `deterministic`; a categorical node is
    always scalar (a group node's strategy is `<group>`, never `categorical`),
    so the getattr default is a safety net, not a live path.
    """
    return bool(getattr(plan_slice, "deterministic", False))


def _table_graph_has_cycle(relationship_graph: RelationshipGraph) -> bool:
    """True if the child->parent TABLE dependency graph has a multi-table cycle.

    Mirrors `_table_order`'s Kahn topological sort (out_of_core/_route_policy.py) at
    gate time, so a cycle the runner would crash on is rejected before it runs.
    Self-edges (parent_table == child_table) are excluded: those are reported
    separately by `_check_edge`'s self-referential rejection, and a self-loop is
    not the multi-table shape this check owns.
    """
    tables: set[str] = set()
    deps: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    for edge in relationship_graph.edges:
        tables.add(edge.parent_table)
        tables.add(edge.child_table)
        deps.setdefault(edge.parent_table, set())
        deps.setdefault(edge.child_table, set())
        children.setdefault(edge.parent_table, set())
        children.setdefault(edge.child_table, set())
        if edge.parent_table == edge.child_table:
            continue
        deps[edge.child_table].add(edge.parent_table)
        children[edge.parent_table].add(edge.child_table)
    ready = [table for table in tables if not deps[table]]
    ordered = 0
    while ready:
        table = ready.pop()
        ordered += 1
        for child in children[table]:
            deps[child].discard(table)
            if not deps[child]:
                ready.append(child)
    return ordered != len(tables)


def _composite_group_join_covered(node: WorkNode, relationship_graph: RelationshipGraph) -> bool:
    """True if every column of a composite_fk_group node is a child FK column
    of some edge on that table (the join the out-of-core route relies on to
    resolve the group actually exists, rather than being assumed)."""
    covered: set[str] = set()
    for edge in relationship_graph.edges:
        if edge.child_table == node.table:
            covered.update(edge.child_columns)
    return all(column in covered for column in node.columns)


def _check_edge(
    edge: RelationshipEdge,
    plan: Plan,
    rejections: list[OutOfCoreRejection],
) -> None:
    if len(edge.parent_columns) != len(edge.child_columns):
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_fk_arity_mismatch",
                "parent and child FK column counts must match.",
            )
        )
    if edge.parent_table == edge.child_table:
        # `_table_order` (out_of_core/_route_policy.py) sequences whole TABLES, so a
        # self-referential edge makes a table depend on itself and always
        # raises `out_of_core_relationship_cycle` -- even though the column-
        # level dependency (parent column masked before the FK column resolves
        # against it) has no cycle and the full-frame oracle handles it fine.
        # Reject fail-closed here instead of letting the route crash on an
        # admitted config; the job falls back to full-frame, which supports
        # this shape natively.
        rejections.append(
            OutOfCoreRejection(
                "out_of_core_self_referential_fk_unsupported",
                f"{edge.parent_table}: self-referential FK edges are not supported "
                "out-of-core (table-level dependency ordering cannot express a "
                "table depending on itself); falls back to full-frame.",
            )
        )
    for parent_column in edge.parent_columns:
        parent_seed = _column_seed(plan, edge.parent_table, parent_column)
        if parent_seed is None:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_parent_seed_missing",
                    f"parent key {edge.parent_table}.{parent_column} is not in the plan.",
                )
            )
        elif parent_seed.strategy not in _INITIAL_SUPPORTED_STRATEGIES:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_parent_strategy_unsupported",
                    f"out-of-core route does not support parent FK key strategy "
                    f"{parent_seed.strategy!r}.",
                )
            )
        elif parent_seed.strategy == "hash" and parent_seed.namespace is None:
            rejections.append(
                OutOfCoreRejection(
                    "out_of_core_parent_namespace_missing",
                    "hash parent FK key must carry a namespace.",
                )
            )


def _column_seed(plan: Plan, table: str, column: str) -> ColumnSeed | None:
    for table_name, table_seed in plan.seed_envelope.per_table:
        if table_name != table:
            continue
        for col_name, col_seed in table_seed.per_column:
            if col_name == column:
                return col_seed
    return None
