"""Graph node lifecycle event helpers.

Emits structured log events (emit_step, emit_throughput_sample) and builds
NodeRunRecord dicts for each lifecycle stage: start, ok, error, export_error.
Extracted from runner.py so the runner's hot loop stays focused on data flow.
"""
from __future__ import annotations

from typing import Any

from decoy_engine.context import emit_step, emit_throughput_sample
from decoy_engine.graph.types import NodeRunRecord

_REDACT_KEYS = {"password", "secret", "token", "api_key", "apikey", "auth"}


def node_start(
    log: Any,
    step_name: str,
    descriptor: str,
    engine: str,
    rows_in: int,
    kind: str,
    node_cfg: dict,
) -> None:
    if log is not None:
        log.info("graph: running node %s (engine=%s)", descriptor, engine)
    emit_step(log, step_name, status="start", rows_in=rows_in or None)
    if log is not None and node_cfg:
        log.info(_summarize_node_config(kind, node_cfg))


def node_ok(
    log: Any,
    step_name: str,
    descriptor: str,
    nid: str,
    kind: str,
    exports: dict | None,
    elapsed_ms: int,
    rows_in: int,
    rows_out: int,
    split: bool = False,
) -> NodeRunRecord:
    label = " (split)" if split else ""
    if log is not None:
        log.info(
            "graph: node %s ok rows=%d elapsed=%dms%s",
            descriptor, rows_out, elapsed_ms, label,
        )
    emit_step(
        log, step_name, status="finish",
        rows_in=rows_in or None, rows_out=rows_out,
    )
    if elapsed_ms > 0 and rows_out > 0:
        emit_throughput_sample(log, rows_out * 1000 / elapsed_ms)
    return {
        "node_id": nid,
        "kind": kind,
        "status": "ok",
        "row_count": rows_out,
        "elapsed_ms": elapsed_ms,
        "error": None,
        "exports": exports,
    }


def node_error(
    log: Any,
    step_name: str,
    descriptor: str,
    nid: str,
    kind: str,
    exports: dict | None,
    elapsed_ms: int,
    rows_in: int,
    exc: BaseException,
    translated: BaseException,
    traceback_str: str | None = None,
) -> NodeRunRecord:
    if log is not None:
        log.error("graph: node %s failed: %s", descriptor, translated)
        if traceback_str:
            log.error(traceback_str)
    emit_step(
        log, step_name, status="error",
        rows_in=rows_in or None,
        error_class=type(exc).__name__,
        error_msg=str(translated),
        node_id=nid,
    )
    return {
        "node_id": nid,
        "kind": kind,
        "status": "error",
        "row_count": None,
        "elapsed_ms": elapsed_ms,
        "error": str(translated),
        "error_code": getattr(translated, "code", None),
        "error_path": getattr(translated, "path", None),
        "exports": exports,
    }


def node_export_error(
    log: Any,
    descriptor: str,
    nid: str,
    kind: str,
    exc: BaseException,
) -> NodeRunRecord:
    if log is not None:
        log.error("graph: node %s failed: %s", descriptor, exc)
    return {
        "node_id": nid,
        "kind": kind,
        "status": "error",
        "row_count": None,
        "elapsed_ms": 0,
        "error": str(exc),
        "exports": None,
    }


def _summarize_node_config(kind: str, cfg: dict) -> str:
    """Return a short per-node config summary for the narrative log."""
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
