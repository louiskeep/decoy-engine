"""Per-node validation (V2.0-B stage 2).

Two entry points covering the same ruleset:

  - validate_nodes(nodes, kinds): raise on first failure. Used by the
    raise-on-first-error public API (validate_graph).
  - collect_node_errors(nodes, kinds): collect all per-node errors
    and return them as a list. Used by validate_graph_full so a
    graph with multiple bad nodes surfaces every failure in one pass
    (R2.2).

Both functions validate the same rules: id shape + uniqueness, kind
membership, name optionality, NATIVE_ENGINE validity, config-mapping
shape, and per-op validate_config delegation. The collecting variant
keeps walking after an error so the operator sees every problem; the
raising variant short-circuits.
"""

from __future__ import annotations

from typing import Any, cast

from decoy_engine.graph.validators._shared import NODE_ID_RE
from decoy_engine.internal.validator import ValidationError
from decoy_engine.validation_result import CODES


def _node_config_error(
    nid: str | None, kind: str, cfg: dict[str, Any], path: str
) -> ValidationError | None:
    """Run the per-op validate_config; return a re-pathed ValidationError on
    failure, None on success. Pulled out of the loop body so the raising
    and collecting paths share the same wrapping logic.
    """
    from decoy_engine.graph.ops import OPS

    try:
        OPS[kind].validate_config(cfg)  # type: ignore[attr-defined]
    except ValidationError as e:
        raw_msg = getattr(e, "raw_message", None) or str(e)
        return ValidationError(
            raw_msg,
            f"{path}.{getattr(e, 'path', None) or 'config'}",
            code=getattr(e, "code", None),
        )
    return None


def validate_nodes(nodes: list[dict[str, Any]], kinds: set[str]) -> None:
    """Raise on the first per-node validation failure."""
    from decoy_engine.graph.conversion import VALID_ENGINES
    from decoy_engine.graph.ops import OPS

    seen_ids: set[str] = set()
    for i, node in enumerate(nodes):
        path = f"nodes[{i}]"
        if not isinstance(node, dict):
            raise ValidationError(
                "node must be a mapping",
                path,
                code=CODES.NODE_BAD_TYPE,
            )
        nid = node.get("id")
        if not isinstance(nid, str) or not NODE_ID_RE.match(nid):
            raise ValidationError(
                "id must match ^[a-zA-Z][a-zA-Z0-9_]{0,63}$",
                f"{path}.id",
                code=CODES.NODE_BAD_ID,
            )
        if nid in seen_ids:
            raise ValidationError(
                f"duplicate node id {nid!r}",
                f"{path}.id",
                code=CODES.NODE_DUPLICATE_ID,
            )
        seen_ids.add(nid)

        kind = node.get("kind")
        if kind not in kinds:
            raise ValidationError(
                f"unknown kind {kind!r} (supported: {sorted(kinds)})",
                f"{path}.kind",
                code=CODES.NODE_UNKNOWN_KIND,
            )

        declared_engine = getattr(OPS[kind], "NATIVE_ENGINE", None)
        if declared_engine is not None and declared_engine not in VALID_ENGINES:
            raise ValidationError(
                f"op {kind!r} declares invalid NATIVE_ENGINE {declared_engine!r}; "
                f"supported: {sorted(VALID_ENGINES)}",
                f"{path}.kind",
                code=CODES.NODE_BAD_NATIVE_ENGINE,
            )

        name = node.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValidationError(
                "name must be a non-empty string when set",
                f"{path}.name",
                code=CODES.NODE_BAD_NAME,
            )

        cfg = node.get("config", {})
        if not isinstance(cfg, dict):
            raise ValidationError(
                "config must be a mapping",
                f"{path}.config",
                code=CODES.NODE_BAD_CONFIG_TYPE,
            )

        err = _node_config_error(nid, kind, cfg, path)
        if err is not None:
            raise err


def collect_node_errors(nodes: list[dict[str, Any]], kinds: set[str]) -> list[ValidationError]:
    """Validate every node; return every error found, in order."""
    from decoy_engine.graph.conversion import VALID_ENGINES
    from decoy_engine.graph.ops import OPS

    errors: list[ValidationError] = []
    seen_ids: set[str] = set()

    for i, node in enumerate(nodes):
        path = f"nodes[{i}]"

        if not isinstance(node, dict):
            errors.append(
                ValidationError(
                    "node must be a mapping",
                    path,
                    code=CODES.NODE_BAD_TYPE,
                )
            )
            continue  # can't inspect sub-fields of a non-dict

        nid = node.get("id")
        if not isinstance(nid, str) or not NODE_ID_RE.match(nid):
            errors.append(
                ValidationError(
                    "id must match ^[a-zA-Z][a-zA-Z0-9_]{0,63}$",
                    f"{path}.id",
                    code=CODES.NODE_BAD_ID,
                )
            )
        else:
            if nid in seen_ids:
                errors.append(
                    ValidationError(
                        f"duplicate node id {nid!r}",
                        f"{path}.id",
                        code=CODES.NODE_DUPLICATE_ID,
                    )
                )
            seen_ids.add(nid)

        kind = node.get("kind")
        kind_valid = isinstance(kind, str) and kind in kinds
        if not kind_valid:
            errors.append(
                ValidationError(
                    f"unknown kind {kind!r} (supported: {sorted(kinds)})",
                    f"{path}.kind",
                    code=CODES.NODE_UNKNOWN_KIND,
                )
            )
        else:
            kind = cast(str, kind)  # narrowed by kind_valid
            declared_engine = getattr(OPS[kind], "NATIVE_ENGINE", None)
            if declared_engine is not None and declared_engine not in VALID_ENGINES:
                errors.append(
                    ValidationError(
                        f"op {kind!r} declares invalid NATIVE_ENGINE {declared_engine!r}; "
                        f"supported: {sorted(VALID_ENGINES)}",
                        f"{path}.kind",
                        code=CODES.NODE_BAD_NATIVE_ENGINE,
                    )
                )

        name = node.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            errors.append(
                ValidationError(
                    "name must be a non-empty string when set",
                    f"{path}.name",
                    code=CODES.NODE_BAD_NAME,
                )
            )

        cfg = node.get("config", {})
        if not isinstance(cfg, dict):
            errors.append(
                ValidationError(
                    "config must be a mapping",
                    f"{path}.config",
                    code=CODES.NODE_BAD_CONFIG_TYPE,
                )
            )
        elif kind_valid:
            kind = cast(str, kind)  # narrowed by kind_valid
            err = _node_config_error(nid, kind, cfg, path)
            if err is not None:
                errors.append(err)

    return errors
