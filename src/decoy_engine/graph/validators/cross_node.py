"""Cross-node semantic validation (V2.0-B stages 6-8).

Three checks that walk relationships between nodes rather than each
node in isolation:

  - validate_file_format_consistency: warn when a file-source feeds
    a file-target with a different format and no convert.file_type
    node sits between them. In strict mode this becomes an error
    when target.file has no explicit format.

    IMPORTANT V2.0-B contract change: this function does NOT mutate
    caller input. The pre-V2.0-B equivalent in GraphConfigValidator
    wrote `tgt_cfg["format"] = tgt_fmt` to back-fill the inferred
    format. That mutation now lives in
    `decoy_engine.graph.normalize.normalize_config`, the explicit
    normalization path.

  - validate_mask_column_reachability: every column a mask op
    references must appear in the schema the upstream source(s)
    produce.

  - validate_nodes_ref_reachability: every ${nodes.<id>.<key>}
    reference must point at a node that exists and is upstream of
    the referrer.

All three run independently of each other (any one can find an error
without short-circuiting the others) so the operator gets every
problem in one validation pass. Each requires the topology stage to
have passed before it runs.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.errors import ValidationError
from decoy_engine.graph.validators._shared import (
    FILE_SOURCE_KINDS,
    FILE_TARGET_KINDS,
)
from decoy_engine.validation_result import CODES


def validate_file_format_consistency(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    strict: bool = False,
    logger: Any = None,
) -> None:
    """Warn on file-source -> file-target format mismatches without a
    convert.file_type intermediary. Strict mode: error when a
    target.file has no explicit `format` field.

    Pure: never writes to nodes/edges. Use
    `decoy_engine.graph.normalize.normalize_config` if you want the
    inferred format back-filled into a normalized copy.
    """
    from decoy_engine.graph.ops._cloud_io import infer_format as _infer_fmt

    node_by_id: dict[str, dict[str, Any]] = {n["id"]: n for n in nodes}
    node_idx: dict[str, int] = {n["id"]: i for i, n in enumerate(nodes) if isinstance(n, dict)}

    # Adjacency list: node_id -> list of direct downstream node_ids.
    # Strip the ".port" suffix that split ops use so the BFS stays simple.
    adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        src = e["from"].split(".", 1)[0]
        adj[src].append(e["to"])

    for node in nodes:
        if node.get("kind") not in FILE_SOURCE_KINDS:
            continue

        src_id = node["id"]
        src_kind = node["kind"]
        src_cfg = node.get("config", {})
        src_fmt = src_cfg.get("format") or _infer_fmt(src_cfg.get("path", ""))
        if not src_fmt:
            continue

        visited_states: set[tuple[str, bool]] = set()
        queue: list[tuple[str, bool]] = [(src_id, False)]
        target_reach: dict[str, set[bool]] = {}

        while queue:
            cur_id, has_convert = queue.pop(0)
            state = (cur_id, has_convert)
            if state in visited_states:
                continue
            visited_states.add(state)

            cur_kind = node_by_id[cur_id].get("kind")
            if cur_id != src_id and cur_kind in FILE_TARGET_KINDS:
                target_reach.setdefault(cur_id, set()).add(has_convert)
                continue

            next_has_convert = has_convert or (cur_kind == "convert.file_type")
            for nxt_id in adj.get(cur_id, []):
                queue.append((nxt_id, next_has_convert))

        for tgt_id, reach_states in target_reach.items():
            if True in reach_states:
                continue

            tgt_node = node_by_id[tgt_id]
            tgt_kind = tgt_node.get("kind")
            tgt_cfg = tgt_node.get("config", {})

            tgt_fmt = (
                tgt_cfg.get("format")
                or _infer_fmt(tgt_cfg.get("output_filename", ""))
                or _infer_fmt(tgt_cfg.get("path", ""))
            )

            if tgt_kind == "target.file" and not tgt_cfg.get("format"):
                if strict:
                    raise ValidationError(
                        f"target.file node {tgt_id!r} has no explicit "
                        f"'format' field; strict mode requires every "
                        f"target to declare its format explicitly "
                        f"(source {src_id!r} uses {src_fmt!r})",
                        f"nodes[{node_idx.get(tgt_id, '?')}].config.format",
                        code=CODES.TARGET_FILE_FORMAT_INFERRED,
                    )
                # Lenient: no mutation here. normalize_config back-fills
                # the inferred format when the caller asks. The mismatch
                # warning still fires below if the inferred fmt differs.

            if tgt_fmt and tgt_fmt != src_fmt and logger is not None:
                # R3.6 demotion: format mismatch is a warning, not an
                # error. Engine logs; platform preflight emits the
                # structured graph.format_mismatch advisory with
                # severity=warning so the R2.5 policy treats it as
                # non-blocking and the target node UI banner picks
                # up the conversion message. The explicit
                # convert.file_type node remains an advanced-tier
                # affordance for users who want the conversion to
                # show up as its own graph node.
                logger.warning(
                    "%s %r produces %s but %s %r expects %s; "
                    "target will auto-convert file type at write time",
                    src_kind,
                    src_id,
                    src_fmt,
                    tgt_kind,
                    tgt_id,
                    tgt_fmt,
                )


def validate_mask_column_reachability(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    """R2.3: a mask node references column names; those names must
    exist in the schema the upstream source produces.
    """
    from decoy_engine.graph.output_schema import (
        is_auto_name,
        predicted_output_columns,
    )

    node_by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}

    upstream: dict[str, list[str]] = {nid: [] for nid in node_by_id}
    for e in edges:
        if not isinstance(e, dict):
            continue
        src = e.get("from")
        dst = e.get("to")
        if isinstance(src, str) and isinstance(dst, str):
            base = src.split(".", 1)[0]
            if dst in upstream:
                upstream[dst].append(base)

    for node in nodes:
        if not isinstance(node, dict) or node.get("kind") != "mask":
            continue
        mask_id = node.get("id", "")
        cols_cfg = (node.get("config") or {}).get("columns")
        if not isinstance(cols_cfg, dict) or not cols_cfg:
            continue

        predicted_union: set[str] = set()
        had_auto = False
        had_unknown = False
        for up_id in upstream.get(mask_id, []):
            up_node = node_by_id.get(up_id)
            if up_node is None:
                continue
            pred = predicted_output_columns(up_node)
            if pred is None:
                had_unknown = True
                continue
            if pred == "$auto":
                had_auto = True
                continue
            if isinstance(pred, list):
                predicted_union.update(pred)

        if had_unknown:
            continue

        for col_name in cols_cfg:
            if had_auto:
                if not is_auto_name(col_name):
                    raise ValidationError(
                        f"mask node {mask_id!r} references column "
                        f"{col_name!r}, but the upstream source has "
                        f"has_header=false with no column_names set "
                        f"- the engine will produce auto-named "
                        f"'column0', 'column1', ... and this rule "
                        f"will silently no-op. Fix: set has_header="
                        f"true on the source, fill in column_names, "
                        f"or load a saved header layout.",
                        f"nodes.{mask_id}.config.columns.{col_name}",
                        code=CODES.MASK_UNKNOWN_COLUMN,
                    )
                continue
            if predicted_union and col_name not in predicted_union:
                available = ", ".join(sorted(predicted_union)) or "(none)"
                raise ValidationError(
                    f"mask node {mask_id!r} references column "
                    f"{col_name!r}, which the upstream source does "
                    f"not produce. Available columns: {available}.",
                    f"nodes.{mask_id}.config.columns.{col_name}",
                    code=CODES.MASK_UNKNOWN_COLUMN,
                )


def validate_nodes_ref_reachability(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    """R2.3: every ``${nodes.<id>.<key>}`` reference must point at
    a node that exists and is UPSTREAM of the referrer.
    """
    import re as _re

    from decoy_engine.graph.topo import upstream_subgraph

    ref_re = _re.compile(r"\$\{nodes\.([a-zA-Z][\w]*)\.([a-zA-Z_][\w.]*)}")
    node_ids = {n["id"] for n in nodes if isinstance(n, dict) and "id" in n}

    def walk(value: Any) -> list[tuple[str, str]]:
        """Yield (target_id, key) for every nodes-ref token under value."""
        found: list[tuple[str, str]] = []
        if isinstance(value, str):
            for m in ref_re.finditer(value):
                found.append((m.group(1), m.group(2)))
        elif isinstance(value, dict):
            for v in value.values():
                found.extend(walk(v))
        elif isinstance(value, list):
            for v in value:
                found.extend(walk(v))
        return found

    for node in nodes:
        if not isinstance(node, dict):
            continue
        referrer_id = node.get("id")
        if not isinstance(referrer_id, str):
            continue
        cfg = node.get("config") or {}
        refs = walk(cfg)
        if not refs:
            continue
        try:
            ordered_ids, _ = upstream_subgraph(nodes, edges, referrer_id)
            upstream_ids = set(ordered_ids)
            upstream_ids.discard(referrer_id)
        except Exception:
            continue
        for target_id, key in refs:
            if target_id not in node_ids:
                raise ValidationError(
                    f"node {referrer_id!r} references "
                    f"${{nodes.{target_id}.{key}}}, but no node "
                    f"with id {target_id!r} exists in this graph.",
                    f"nodes.{referrer_id}.config",
                    code=CODES.NODES_REF_UNKNOWN_ID,
                )
            if target_id not in upstream_ids:
                raise ValidationError(
                    f"node {referrer_id!r} references "
                    f"${{nodes.{target_id}.{key}}}, but {target_id!r} "
                    f"is not upstream of this node - the export will "
                    f"never be set at run time. Add an edge from "
                    f"{target_id!r} (directly or transitively) to "
                    f"{referrer_id!r}, or fix the reference.",
                    f"nodes.{referrer_id}.config",
                    code=CODES.NODES_REF_NOT_UPSTREAM,
                )
