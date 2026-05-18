"""Node lifecycle event helpers for the graph runner.

Centralises NodeRunRecord construction and structured log emission so
runner.py stays an orchestration shell. Platform job logging does not
need to parse runner-specific string patterns to extract node lifecycle
facts. New op authors do not touch runner internals to get normal
event/result behaviour.
"""
from __future__ import annotations

import traceback as _traceback
from typing import Any

from decoy_engine.context import emit_step, emit_throughput_sample


# ── node descriptor ───────────────────────────────────────────────────────────

def node_descriptor(node: dict) -> str:
    """Short human-readable label used in log lines and error messages."""
    nid = node.get("id", "?")
    kind = node.get("kind", "?")
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name!r} [id={nid}, kind={kind}]"
    return f"[id={nid}, kind={kind}]"


# ── NodeRunRecord construction ──────────────────────────────────────────────

def make_ok_record(
    node_id: str,
    kind: str,
    elapsed_ms: int,
    row_count: int,
    exports: dict | None,
) -> dict:
    """Success record shared by split-output and scalar-output nodes."""
    return {
        "node_id": node_id,
        "kind": kind,
        "status": "ok",
        "row_count": row_count,
        "elapsed_ms": elapsed_ms,
        "error": None,
        "exports": exports,
    }


def make_error_record(
    node_id: str,
    kind: str,
    elapsed_ms: int,
    error: Exception,
    exports: dict | None,
) -> dict:
    """Failure record with typed error_code / error_path forwarded from the
    translated exception when present.
    """
    return {
        "node_id": node_id,
        "kind": kind,
        "status": "error",
        "row_count": None,
        "elapsed_ms": elapsed_ms,
        "error": str(error),
        "error_code": getattr(error, "code", None),
        "error_path": getattr(error, "path", None),
        "exports": exports,
    }


def make_export_error_record(node_id: str, kind: str, error: Exception) -> dict:
    """Minimal error record for node-export resolution failures.

    Elapsed is 0 because the failure occurs before the op executes.
    Exports is None because the node never ran.
    """
    return {
        "node_id": node_id,
        "kind": kind,
        "status": "error",
        "row_count": None,
        "elapsed_ms": 0,
        "error": str(error),
        "exports": None,
    }


# ── log + step emission helpers ───────────────────────────────────────────────

def emit_node_start(
    log: Any,
    step_name: str,
    descriptor: str,
    engine: str,
    rows_in: int,
    node_cfg: dict,
) -> None:
    """Log node start, emit the step-start boundary, and log config summary."""
    if log is not None:
        log.info("graph: running node %s (engine=%s)", descriptor, engine)
    emit_step(log, step_name, status="start", rows_in=rows_in or None)
    if log is not None and node_cfg:
        log.info(_config_summary(node_cfg))


def emit_node_ok(
    log: Any,
    step_name: str,
    descriptor: str,
    elapsed_ms: int,
    rows_in: int,
    rows_out: int,
    is_split: bool = False,
) -> None:
    """Log node success, emit step-finish boundary, and add throughput sample."""
    suffix = " (split)" if is_split else ""
    if log is not None:
        log.info(
            "graph: node %s ok rows=%d elapsed=%dms%s",
            descriptor, rows_out, elapsed_ms, suffix,
        )
    emit_step(
        log, step_name, status="finish",
        rows_in=rows_in or None, rows_out=rows_out,
    )
    if elapsed_ms > 0 and rows_out > 0:
        emit_throughput_sample(log, rows_out * 1000 / elapsed_ms)


def emit_node_error(
    log: Any,
    step_name: str,
    descriptor: str,
    elapsed_ms: int,
    rows_in: int,
    error: Exception,
    node_id: str,
    original_exc: Exception,
) -> None:
    """Log node failure and emit step-error boundary.

    Must be called from within an active except block so
    traceback.format_exc() captures the current exception's traceback.
    """
    if log is not None:
        log.error("graph: node %s failed: %s", descriptor, error)
        log.error(_traceback.format_exc())
    emit_step(
        log, step_name, status="error",
        rows_in=rows_in or None,
        error_class=type(original_exc).__name__,
        error_msg=str(error),
        node_id=node_id,
    )


# ── config summary ─────────────────────────────────────────────────────────────

_REDACT_KEYS = {"password", "secret", "token", "api_key", "apikey", "auth"}


def _config_summary(cfg: dict) -> str:
    """Return a short per-node config glance line for the narrative log.

    Secrets are redacted by key name. Private keys (starting with ``_``)
    such as ``__engine`` and ``__preview_row_limit`` are skipped.
    """
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
