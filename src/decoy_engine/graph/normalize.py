"""Explicit graph-config normalization (V2.0-B).

`normalize_config(config)` returns a deep-copied, normalized version of
a graph config with engine-inferred defaults applied. The function is
the single named path that applies such defaults; validators no longer
do so as a side effect.

What gets normalized today:

  - target.file nodes that omit `format` get it back-filled from the
    inferred source format (path extension or explicit source.format).
    Cloud targets (target.s3 / target.gcs / target.sftp) resolve
    format via their own validate_config / extension inference paths
    and are NOT back-filled here.

The function NEVER raises. It assumes the caller has already validated
the config (validate_graph_full does this); a malformed config produces
a normalized copy that is still malformed in the same way.

Contract:
  - Returns a new dict; the input is never mutated.
  - Idempotent: normalize_config(normalize_config(c)) == normalize_config(c).
  - Does not validate. Callers compose: validate then normalize.

Why this lives separately from validation:

  Pre-V2.0-B, `_validate_file_format_consistency` wrote into the
  passed-in dict (`tgt_cfg["format"] = tgt_fmt`). That tied validation
  to mutation and made it impossible to call validate() without
  surprising downstream code. The roadmap's V2.0-B Done state demands
  "Mutating validation is impossible by contract"; the only way to
  satisfy that contract is to move the writes out of the validator.
"""

from __future__ import annotations

import copy
from typing import Any

from decoy_engine.graph.validators._shared import (
    FILE_SOURCE_KINDS,
    FILE_TARGET_KINDS,
)


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized deep copy of `config` with engine defaults applied."""
    out = copy.deepcopy(config)
    _backfill_target_file_formats(out)
    return out


def _backfill_target_file_formats(config: dict[str, Any]) -> None:
    """In-place on the normalized copy: when a target.file node omits
    `format`, infer it from path/output_filename or from the connected
    source's format and write it back.

    This mirrors the logic that
    `decoy_engine.graph.validators.cross_node.validate_file_format_consistency`
    uses to PROBE the format, except here we actually write it. The
    write is safe because `config` is the normalized copy, not the
    caller's dict.

    Lenient-mode only: in strict mode the validator REJECTS targets
    without explicit formats, so no back-fill happens (and is not
    needed because the caller never proceeds).
    """
    from decoy_engine.graph.ops._cloud_io import infer_format as _infer_fmt

    nodes = config.get("nodes")
    edges = config.get("edges") or []
    if not isinstance(nodes, list):
        return

    node_by_id: dict[str, dict[str, Any]] = {
        n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n
    }

    # Adjacency list: node_id -> list of direct downstream node_ids.
    # Strip the ".port" suffix that split ops use so the BFS stays simple.
    adj: dict[str, list[str]] = {n["id"]: [] for n in node_by_id.values()}
    for e in edges:
        if not isinstance(e, dict):
            continue
        raw_src = e.get("from")
        dst = e.get("to")
        if not isinstance(raw_src, str) or not isinstance(dst, str):
            continue
        src = raw_src.split(".", 1)[0]
        if src in adj:
            adj[src].append(dst)

    for node in nodes:
        if not isinstance(node, dict) or node.get("kind") not in FILE_SOURCE_KINDS:
            continue

        src_id = node.get("id")
        if not isinstance(src_id, str):
            continue
        src_cfg = node.get("config") or {}
        src_fmt = src_cfg.get("format") or _infer_fmt(src_cfg.get("path", ""))
        if not src_fmt:
            continue

        # BFS for downstream file-targets that don't sit behind a
        # convert.file_type node. The convert node performs the format
        # change at runtime, so the target should keep its declared
        # format (or its own inference); only un-converted reaches
        # benefit from source-driven back-fill.
        visited: set[tuple[str, bool]] = set()
        queue: list[tuple[str, bool]] = [(src_id, False)]
        while queue:
            cur_id, has_convert = queue.pop(0)
            state = (cur_id, has_convert)
            if state in visited:
                continue
            visited.add(state)

            cur_node = node_by_id.get(cur_id)
            if cur_node is None:
                continue
            cur_kind = cur_node.get("kind")
            if cur_id != src_id and cur_kind in FILE_TARGET_KINDS:
                if cur_kind == "target.file" and not has_convert:
                    tgt_cfg = cur_node.setdefault("config", {})
                    if "format" not in tgt_cfg:
                        tgt_fmt = (
                            _infer_fmt(tgt_cfg.get("output_filename", ""))
                            or _infer_fmt(tgt_cfg.get("path", ""))
                            or src_fmt
                        )
                        if tgt_fmt:
                            tgt_cfg["format"] = tgt_fmt
                # Targets stop the walk; nothing flows past a sink.
                continue

            next_has_convert = has_convert or (cur_kind == "convert.file_type")
            for nxt_id in adj.get(cur_id, []):
                queue.append((nxt_id, next_has_convert))
