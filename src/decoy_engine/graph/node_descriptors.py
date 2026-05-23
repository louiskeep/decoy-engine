"""Small log-formatting helpers for graph node lifecycle messages.

Extracted from decoy_engine/graph/runner.py per the Code Organization
Migration Plan §Section 6. Move-only.

These four pure-utility helpers shape the strings the runner writes
into its narrative log lines:

  - ``_node_descriptor(node)``: ``'name' [id=X, kind=Y]`` for log lines.
  - ``_summarize_node_config(kind, cfg)``: bounded one-line summary
    of a node's config block, with credential-like keys redacted.
  - ``_jsonable(v)``: NaN/NaT -> None coercion for sample-row tuples.
  - ``_REDACT_KEYS``: the set of substring matches that trigger the
    redaction in ``_summarize_node_config``.

No engine state. No I/O. Reimported into runner.py so existing
callers (and tests that reach for them) keep working unchanged.
"""

from __future__ import annotations

from typing import Any

_REDACT_KEYS = {"password", "secret", "token", "api_key", "apikey", "auth"}


def _node_descriptor(node: dict) -> str:
    nid = node.get("id", "?")
    kind = node.get("kind", "?")
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name!r} [id={nid}, kind={kind}]"
    return f"[id={nid}, kind={kind}]"


def _summarize_node_config(kind: str, cfg: dict) -> str:
    if not isinstance(cfg, dict) or not cfg:
        return "config: (no config)"
    parts: list[str] = []
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        key_l = str(k).lower()
        if any(rk in key_l for rk in _REDACT_KEYS):
            parts.append(f"{k}=***")
            continue
        if isinstance(v, (dict, list)):
            kind_word = "keys" if isinstance(v, dict) else "items"
            parts.append(f"{k}=<{len(v)} {kind_word}>")
        elif isinstance(v, str) and len(v) > 80:
            parts.append(f"{k}={v[:77]!r}...")
        else:
            parts.append(f"{k}={v!r}")
        if len(parts) >= 6:
            parts.append("...")
            break
    return "config: " + ", ".join(parts)


def _jsonable(v: Any) -> Any:
    """Replace NaN/NaT/etc. with None so the row tuples serialize cleanly."""
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v
