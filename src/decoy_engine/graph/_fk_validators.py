"""FK / column-relationship validators (V2.0-A.4 extraction).

Validates the top-level ``column_relationships:`` block of a graph
config. Lifted out of graph/runner.py in V2.0-A.4 because the cluster
(_column_in_node, _validate_column_relationships, _validate_m2m_entry,
_validate_multi_parent_entry, _validate_custom_provider_entry) was
~620 lines of validation logic that had nothing to do with graph
execution. The runner imports back what it needs.

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG with explicit
parent-child topology, materialize parent pool, child samples with
replacement. Several FK_* codes are documented in
validation_result.py and emitted from this module.

Caller contract: pass a non-None `result` (a ValidationResult); the
validators add errors and warnings to it. They never raise on
business-logic problems; they raise only on programmer errors (e.g.
malformed config that GraphConfigValidator should have caught first).

Note on the ``_validate_*_entry`` helper signature: each takes a
``CODES`` parameter even though it could just import CODES directly.
The parameter is a holdover from when the codes were dynamically
resolved per call; keeping the explicit signature lets callers (and
tests) substitute a different code-set if needed. Removable in V2.0-B.
"""
from __future__ import annotations

from decoy_engine.validation_result import CODES


def _column_in_node(node: dict, column: str) -> bool:
    """True if `column` appears in `node`'s config columns mapping.

    For source nodes the schema is the file's actual columns, unknowable
    at validation time, so we treat them as opaque and return True. The
    runtime resolver will catch a true miss at execution time and emit
    fk.unknown_column then.

    Promoted from a nested helper inside _validate_column_relationships
    to module scope 2026-05-23: a sibling validator
    (_validate_custom_provider_entry) was already calling it, which
    raised NameError at runtime on custom-provider FK paths. See
    docs/v2-app-audit-findings.md F-AUDIT-001.
    """
    kind = node.get("kind", "")
    if kind.startswith("source."):
        return True
    cfg = node.get("config") or {}
    cols = cfg.get("columns")
    if not isinstance(cols, dict):
        return True
    return column in cols


def _validate_column_relationships(
    config: dict,
    *,
    strict: bool,
    result,
) -> None:
    """Validate the top-level ``column_relationships:`` block.

    Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
    materialize parent pool; child samples with replacement.

    Per-entry checks (all error-level unless noted):

      - fk.unknown_node        : parent.node or child.node not in graph
      - fk.unknown_column      : node exists but the referenced column
                                 doesn't appear in the node's config or
                                 (for source nodes) the source's
                                 declared schema. Source nodes without
                                 a `columns` config get a carve-out
                                 because their schema is the file's
                                 actual columns, which the engine only
                                 knows at runtime.
      - fk.parent_after_child  : parent appears AFTER child in
                                 plan.order; refuses to run.
      - fk.self_reference      : parent.node == child.node; out of V1
                                 scope (V2 will lift via SDV self-loop
                                 handling).
      - fk.parallel_branches   : no topological path from parent to
                                 child. Advisory in lenient mode
                                 (cache pinning handles parallel-branch
                                 survival); error in strict mode.
      - fk.nondeterministic_mask : a mask op participates in an FK and
                                 uses a non-deterministic strategy
                                 (redact, shuffle, truncate). Advisory
                                 (severity=warning) by default so
                                 operators shipping a one-off scrub
                                 don't hit a hard wall when roundtrip
                                 joinability isn't required. Set the
                                 env var ``DECOY_FK_STRICT_DETERMINISM=1``
                                 (or ``true`` / ``yes``) to restore the
                                 hard reject; that's the right knob for
                                 long-lived analytics pipelines where
                                 cross-run join stability matters.

    Skips silently when no column_relationships block exists.
    """
    import os

    from decoy_engine.graph.planner import build_plan

    rels = config.get("column_relationships")
    if not rels:
        return
    if not isinstance(rels, list):
        result.add_error(
            code=CODES.FK_UNKNOWN_NODE,
            message="column_relationships must be a list",
            path="column_relationships",
        )
        return

    # plan.order gives topo position; needed for parent-after-child check.
    try:
        plan = build_plan(config)
    except Exception:
        # If the graph won't plan, we can't reason about FK ordering.
        # The topology stage already flagged the structural failure.
        return
    pos_in_order = {nid: idx for idx, nid in enumerate(plan.order)}

    nodes_by_id = {n["id"]: n for n in config.get("nodes") or []}

    # Build the reachability cache lazily; only consulted when emitting
    # fk.parallel_branches.
    _reach_cache: dict[tuple[str, str], bool] = {}
    edges_list = config.get("edges") or []

    def _reachable(from_id: str, to_id: str) -> bool:
        key = (from_id, to_id)
        if key in _reach_cache:
            return _reach_cache[key]
        # BFS over edges. Small graphs in practice; no need for fancier
        # structures.
        visited = {from_id}
        stack = [from_id]
        while stack:
            curr = stack.pop()
            if curr == to_id:
                _reach_cache[key] = True
                return True
            for e in edges_list:
                if isinstance(e, dict) and e.get("from") == curr and e.get("to") not in visited:
                    visited.add(e["to"])
                    stack.append(e["to"])
        _reach_cache[key] = False
        return False

    # Mask strategies that preserve referential integrity across runs.
    # Non-members on an FK column emit a warning (or hard error when
    # DECOY_FK_STRICT_DETERMINISM=1 is set).
    DETERMINISTIC_MASK_STRATEGIES = frozenset(
        {"hash", "fpe", "faker", "date_shift", "reference"}
    )

    # _column_in_node was previously a nested function here. Promoted
    # to module scope 2026-05-23 (F-AUDIT-001) because a sibling
    # validator was already trying to call it.

    def _mask_strategy_for_column(node: dict, column: str) -> str | None:
        """If `node` is a mask op and `column` is configured, return its
        strategy. Otherwise None (caller skips the determinism check)."""
        if node.get("kind") != "mask":
            return None
        cfg = node.get("config") or {}
        col_spec = (cfg.get("columns") or {}).get(column)
        if not isinstance(col_spec, dict):
            return None
        return col_spec.get("strategy")

    # Self-reference entries collected to detect column cycles within
    # one node post-loop. Each tuple is (node_id, parent_col, child_col,
    # path) -- we already validated p_col != c_col when appending.
    self_ref_entries: list[tuple[str, str, str, str]] = []

    for i, rel in enumerate(rels):
        path = f"column_relationships[{i}]"
        if not isinstance(rel, dict):
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message="entry must be a mapping",
                path=path,
            )
            continue
        # m2m and multi-parent shapes get their own validators below
        # the main fk loop. Bypass the kind: fk shape check for them.
        kind = rel.get("kind", "fk")
        if kind == "m2m":
            _validate_m2m_entry(rel, path, nodes_by_id, result, CODES)
            continue
        parent = rel.get("parent") or {}
        # Multi-parent (parent: [...] array): defer to dedicated validator.
        if isinstance(parent, list):
            _validate_multi_parent_entry(rel, path, nodes_by_id, result, CODES)
            continue
        child = rel.get("child") or {}
        # Custom-provider parent shape (tier-4 audit): pool sourced
        # from a registered list-backed custom Faker provider instead
        # of a pipeline node's output. Skip topology + column-presence
        # checks (custom providers aren't in the graph). Still verify
        # the provider name is non-empty and the child is well-formed.
        if isinstance(parent, dict) and parent.get("custom_provider"):
            _validate_custom_provider_entry(rel, path, nodes_by_id, result, CODES)
            continue
        p_node = parent.get("node") if isinstance(parent, dict) else None
        p_col = parent.get("column") if isinstance(parent, dict) else None
        c_node = child.get("node") if isinstance(child, dict) else None
        c_col = child.get("column") if isinstance(child, dict) else None

        if not p_node or not c_node or not p_col or not c_col:
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message="entry missing parent.node / parent.column / child.node / child.column",
                path=path,
            )
            continue

        # Self-reference: supported when child.column != parent.column.
        # The engine reads the pool from the in-flight output buffer
        # (out[parent_column]) instead of pool_resolver because the
        # parent column hasn't been cached yet -- it's being produced
        # in the same op invocation. Same-column self-edges (a -> a)
        # would be a cycle; reject as fk.self_cycle.
        if p_node == c_node:
            if p_col == c_col:
                result.add_error(
                    code=CODES.FK_SELF_CYCLE,
                    message=(
                        f"self-edge on the same column is a cycle "
                        f"({c_node}.{c_col} cannot FK to itself)"
                    ),
                    path=path,
                )
                continue
            # Self-reference between two columns within one node:
            # accepted on generate nodes (two-pass within one op via
            # out[parent_column]). Mask + transform + source kinds
            # have no two-pass mechanism (mask is per-cell single-pass;
            # source columns are read-only). Reject rather than silently
            # ignoring at run time. The check uses the node's stored
            # kind; if the node isn't in the graph at all, the unknown-
            # node branch below catches it.
            self_node_obj = nodes_by_id.get(c_node)
            self_kind = (self_node_obj or {}).get("kind")
            if self_node_obj and self_kind != "generate":
                result.add_error(
                    code=CODES.FK_SELF_REF_INERT,
                    message=(
                        f"self-reference on {c_node!r} (kind={self_kind!r}) "
                        f"has no engine effect -- only generate nodes have a "
                        f"two-pass mechanism. Move the self-ref to a generate "
                        f"node downstream, or use a derive node with a formula "
                        f"strategy if the value depends on a sibling column."
                    ),
                    path=path,
                    node_id=c_node,
                )
                continue
            # Skip the topology + parent-after-child check below; the
            # column-order check (handled at apply time via
            # out[parent_column] reads) is the real ordering constraint.
            # Column-cycle detection (a->b, b->a within one node)
            # is captured below after we've collected all entries.
            self_ref_entries.append((c_node, p_col, c_col, path))
            continue

        # Unknown nodes.
        if p_node not in nodes_by_id:
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message=f"parent node {p_node!r} not present in graph",
                path=f"{path}.parent.node",
            )
            continue
        if c_node not in nodes_by_id:
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message=f"child node {c_node!r} not present in graph",
                path=f"{path}.child.node",
            )
            continue

        # Child-kind eligibility + sequential-bounds composition checks.
        # Run before the topology + column checks because they depend only
        # on the entry itself (node kind, distribution config). This ensures
        # the operator sees these higher-priority misconfiguration errors
        # even when the FK also has unrelated topology issues.
        c_kind_early = nodes_by_id[c_node].get("kind", "")
        if c_kind_early and c_kind_early != "mask" and c_kind_early != "generate":
            result.add_error(
                code=CODES.FK_INELIGIBLE_CHILD_KIND,
                message=(
                    f"child node {c_node!r} has kind {c_kind_early!r} -- only "
                    f"mask + generate nodes can carry an FK at run time. "
                    f"source.* nodes are read-only inputs; transforms "
                    f"like drop_column / filter / dedupe don't materialize "
                    f"new column values. Move the FK to a downstream mask "
                    f"or generate node that processes this column."
                ),
                path=f"{path}.child.node",
                node_id=c_node,
            )
            continue
        rel_distribution_early = rel.get("distribution")
        rel_min_early = rel.get("min_per_parent")
        rel_max_early = rel.get("max_per_parent")
        bounds_set_early = (
            (isinstance(rel_min_early, int) and rel_min_early > 0)
            or (isinstance(rel_max_early, int) and rel_max_early > 0)
        )
        if rel_distribution_early == "sequential" and bounds_set_early:
            result.add_warning(
                code=CODES.FK_SEQUENTIAL_BOUNDS_CONFLICT,
                message=(
                    "sequential distribution + cardinality bounds "
                    "(min_per_parent / max_per_parent) don't compose: "
                    "the bounds repair phase shuffles placement, breaking "
                    "the sequence. Pick one or the other -- bounds are "
                    "designed to combine with random / weighted."
                ),
                path=path,
                node_id=c_node,
            )
            # Don't continue -- the FK is still authorable, just the
            # combination is broken; let downstream checks run too.

        # Topology: parent must precede child in plan.order.
        if pos_in_order.get(p_node, 0) >= pos_in_order.get(c_node, 0):
            result.add_error(
                code=CODES.FK_PARENT_AFTER_CHILD,
                message=(
                    f"parent {p_node!r} does not precede child {c_node!r} "
                    "in topological order (parent must produce its column "
                    "before child consumes it)"
                ),
                path=path,
            )
            continue

        # Parallel-branch advisory: parent + child both run, but no
        # topological path connects them. Cache pinning keeps both
        # alive so this is fine in practice; surface as a warning in
        # lenient mode + an error in strict mode.
        if not _reachable(p_node, c_node):
            if strict:
                result.add_error(
                    code=CODES.FK_PARALLEL_BRANCHES,
                    message=(
                        f"no topological path from parent {p_node!r} to child {c_node!r}; "
                        "they run on parallel branches (strict mode rejects this)"
                    ),
                    path=path,
                )
            else:
                result.add_warning(
                    code=CODES.FK_PARALLEL_BRANCHES,
                    message=(
                        f"parent {p_node!r} and child {c_node!r} are on parallel branches; "
                        "cache pinning keeps both alive but the relationship is implicit"
                    ),
                    path=path,
                )

        # Column presence (best-effort; source nodes get a carve-out).
        p_node_obj = nodes_by_id[p_node]
        c_node_obj = nodes_by_id[c_node]
        if not _column_in_node(p_node_obj, p_col):
            result.add_error(
                code=CODES.FK_UNKNOWN_COLUMN,
                message=(
                    f"parent column {p_col!r} not declared in parent {p_node!r} config "
                    f"(kind={p_node_obj.get('kind')})"
                ),
                path=f"{path}.parent.column",
            )
        if not _column_in_node(c_node_obj, c_col):
            result.add_error(
                code=CODES.FK_UNKNOWN_COLUMN,
                message=(
                    f"child column {c_col!r} not declared in child {c_node!r} config "
                    f"(kind={c_node_obj.get('kind')})"
                ),
                path=f"{path}.child.column",
            )


        # Mask determinism: both ends, if they're mask ops, must use a
        # deterministic strategy to preserve the FK. Advisory by
        # default (severity=warning) so a one-off run with redact or
        # shuffle on an FK column doesn't hard-fail; set
        # DECOY_FK_STRICT_DETERMINISM=1 to upgrade back to error. The
        # advisory still records the affected column so the platform
        # manifest assembler can hydrate `fk_preservation.advisories`
        # for downstream auditors.
        strict_determinism = (
            os.environ.get("DECOY_FK_STRICT_DETERMINISM", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        for side, node_obj, col_name in (
            ("parent", p_node_obj, p_col),
            ("child",  c_node_obj, c_col),
        ):
            strategy = _mask_strategy_for_column(node_obj, col_name)
            if strategy is not None and strategy not in DETERMINISTIC_MASK_STRATEGIES:
                msg = (
                    f"{side} mask column {col_name!r} uses strategy {strategy!r} which is "
                    "not deterministic; declared FK requires one of "
                    f"{sorted(DETERMINISTIC_MASK_STRATEGIES)} for cross-run join stability"
                )
                if strict_determinism:
                    result.add_error(
                        code=CODES.FK_NONDETERMINISTIC_MASK,
                        message=msg,
                        path=f"{path}.{side}.column",
                    )
                else:
                    # Advisory path: same code so platform validation routers
                    # (web/src/pipelines/hifi/validation.ts) can still pattern-
                    # match on it. Severity=warning is the only difference.
                    result.add_warning(
                        code=CODES.FK_NONDETERMINISTIC_MASK,
                        message=msg + " (advisory -- set DECOY_FK_STRICT_DETERMINISM=1 to block)",
                        path=f"{path}.{side}.column",
                    )

    # -- Post-loop: detect column cycles within a single node --
    # When a node has both (a -> b) and (b -> a) self-FK entries, the
    # apply-time two-pass approach can't satisfy both -- they form a
    # cycle. Single self-edges (a -> b only) are fine; the cycle case
    # surfaces only when two entries on the same node close the loop.
    if self_ref_entries:
        from collections import defaultdict
        by_node: defaultdict[str, set[tuple[str, str]]] = defaultdict(set)
        for node_id, p_col, c_col, _ in self_ref_entries:
            by_node[node_id].add((p_col, c_col))
        for node_id, pairs in by_node.items():
            # Direct two-edge cycle: (a, b) AND (b, a) both present.
            for (p_col, c_col) in pairs:
                if (c_col, p_col) in pairs:
                    # Find one of the paths to attach the error to.
                    for entry in self_ref_entries:
                        if entry[0] == node_id and entry[1] == p_col and entry[2] == c_col:
                            result.add_error(
                                code=CODES.FK_SELF_CYCLE,
                                message=(
                                    f"column cycle within node {node_id!r}: "
                                    f"{p_col!r} -> {c_col!r} and {c_col!r} -> {p_col!r} "
                                    "both declared; neither can resolve at apply time"
                                ),
                                path=entry[3],
                            )
                            break
                    break


def _validate_m2m_entry(
    rel: dict, path: str, nodes_by_id: dict[str, dict], result, CODES,
) -> None:
    """Validate a `kind: m2m` (many-to-many junction) column_relationships
    entry. Shape:

        - kind: m2m
          junction:    { node: enrollments__gen, columns: [s_id, c_id] }
          left_parent:  { node: students__mask,  column: id }
          right_parent: { node: courses__mask,   column: id }
          pool_strategy: cartesian | sampled | weighted   # default cartesian

    The engine's m2m runtime path (generate_op.py) reads each parent's
    pool, then emits the junction's two columns by sampling
    (left, right) pairs according to pool_strategy.
    """
    junction = rel.get("junction") or {}
    left = rel.get("left_parent") or {}
    right = rel.get("right_parent") or {}
    j_node = junction.get("node") if isinstance(junction, dict) else None
    j_cols = junction.get("columns") if isinstance(junction, dict) else None
    if not j_node or not isinstance(j_cols, list) or len(j_cols) != 2:
        result.add_error(
            code=CODES.FK_M2M_BAD_POOL,
            message="m2m entry needs junction.node + junction.columns (2 columns)",
            path=path,
        )
        return
    for side, side_dict in (("left_parent", left), ("right_parent", right)):
        node = side_dict.get("node") if isinstance(side_dict, dict) else None
        col = side_dict.get("column") if isinstance(side_dict, dict) else None
        if not node or not col:
            result.add_error(
                code=CODES.FK_M2M_UNKNOWN_NODE,
                message=f"m2m {side} needs node + column",
                path=f"{path}.{side}",
            )
            return
        if node not in nodes_by_id:
            result.add_error(
                code=CODES.FK_M2M_UNKNOWN_NODE,
                message=f"m2m {side} node {node!r} not in graph",
                path=f"{path}.{side}.node",
            )
            return
    pool_strategy = rel.get("pool_strategy", "cartesian")
    if pool_strategy not in ("cartesian", "sampled", "weighted"):
        result.add_error(
            code=CODES.FK_M2M_BAD_POOL,
            message=(
                f"m2m pool_strategy {pool_strategy!r} unsupported "
                "(use cartesian | sampled | weighted)"
            ),
            path=f"{path}.pool_strategy",
        )


def _validate_multi_parent_entry(
    rel: dict, path: str, nodes_by_id: dict[str, dict], result, CODES,
) -> None:
    """Validate a multi-parent FK entry -- `parent` is an array of
    parent specs instead of a single object. Each entry contributes to
    a composite-key pool: the child column draws (left_val, right_val,
    ...) tuples from the joint distribution of parents. Shape:

        - kind: fk
          parent:
            - { node: students__mask, column: id }
            - { node: courses__mask,  column: id }
          child: { node: enrollments__gen, column: enrollment_key }
    """
    parents = rel.get("parent") or []
    child = rel.get("child") or {}
    c_node = child.get("node") if isinstance(child, dict) else None
    c_col = child.get("column") if isinstance(child, dict) else None
    if not c_node or not c_col:
        result.add_error(
            code=CODES.FK_MULTI_PARENT_BAD_SHAPE,
            message="multi-parent FK missing child.node or child.column",
            path=path,
        )
        return
    if not isinstance(parents, list) or len(parents) < 2:
        result.add_error(
            code=CODES.FK_MULTI_PARENT_BAD_SHAPE,
            message="multi-parent FK needs parent: [...] with 2+ entries",
            path=f"{path}.parent",
        )
        return
    for i, p in enumerate(parents):
        if not isinstance(p, dict):
            result.add_error(
                code=CODES.FK_MULTI_PARENT_BAD_SHAPE,
                message=f"multi-parent entry [{i}] must be a mapping",
                path=f"{path}.parent[{i}]",
            )
            return
        p_node = p.get("node")
        p_col = p.get("column")
        if not p_node or not p_col:
            result.add_error(
                code=CODES.FK_MULTI_PARENT_BAD_SHAPE,
                message=f"multi-parent entry [{i}] needs node + column",
                path=f"{path}.parent[{i}]",
            )
            return
        if p_node not in nodes_by_id:
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message=f"multi-parent entry [{i}] parent node {p_node!r} not in graph",
                path=f"{path}.parent[{i}].node",
            )
            return


def _validate_custom_provider_entry(
    rel: dict, path: str, nodes_by_id: dict, result, CODES,
) -> None:
    """Validate a column_relationships entry whose parent sources the
    pool from a registered custom Faker provider (parent: {custom_provider:
    <name>}). Skips topology + column-presence checks for the parent
    (custom providers aren't graph nodes). Verifies the child node
    exists, is FK-eligible (mask / generate), and has the named column.
    Provider registration is best-effort: the registry is populated at
    run time, so a missing provider is a warning, not a hard error.
    """
    from decoy_engine.internal.helpers import list_custom_faker_list_providers
    parent = rel.get("parent") or {}
    child = rel.get("child") or {}
    pname = parent.get("custom_provider")
    c_node = child.get("node") if isinstance(child, dict) else None
    c_col = child.get("column") if isinstance(child, dict) else None

    if not pname or not isinstance(pname, str):
        result.add_error(
            code=CODES.FK_UNKNOWN_NODE,
            message="parent.custom_provider must be a non-empty string",
            path=f"{path}.parent.custom_provider",
        )
        return
    if not c_node or not c_col:
        result.add_error(
            code=CODES.FK_UNKNOWN_NODE,
            message="entry missing child.node / child.column",
            path=path,
        )
        return
    if c_node not in nodes_by_id:
        result.add_error(
            code=CODES.FK_UNKNOWN_NODE,
            message=f"child node {c_node!r} not present in graph",
            path=f"{path}.child.node",
        )
        return
    c_node_obj = nodes_by_id[c_node]
    c_kind = c_node_obj.get("kind", "")
    if c_kind and c_kind != "mask" and c_kind != "generate":
        result.add_error(
            code=CODES.FK_INELIGIBLE_CHILD_KIND,
            message=(
                f"child node {c_node!r} has kind {c_kind!r} -- only "
                f"mask + generate nodes can carry an FK at run time."
            ),
            path=f"{path}.child.node",
            node_id=c_node,
        )
        return
    if not _column_in_node(c_node_obj, c_col):
        result.add_error(
            code=CODES.FK_UNKNOWN_COLUMN,
            message=(
                f"child column {c_col!r} not declared in child {c_node!r} config "
                f"(kind={c_kind})"
            ),
            path=f"{path}.child.column",
        )
        return
    # Provider registration check is best-effort at validation time --
    # the engine registers providers from filesystem + DB at run time,
    # so the validator only warns when the provider isn't visible right
    # now. Run-time `EmptyParentPoolError` is the hard backstop.
    registered = set(list_custom_faker_list_providers())
    if pname not in registered:
        result.add_warning(
            code=CODES.FK_INELIGIBLE_CHILD_KIND,  # closest existing code; specific code TODO
            message=(
                f"custom provider {pname!r} not currently registered "
                f"(known: {sorted(registered) or '<none loaded>'}); engine "
                f"will raise empty_parent_pool at run time if it's still "
                f"missing then. Confirm the provider is loaded via "
                f"AppSettings or the custom_providers/ filesystem directory."
            ),
            path=f"{path}.parent.custom_provider",
            node_id=c_node,
        )

