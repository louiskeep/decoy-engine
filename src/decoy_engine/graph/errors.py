"""Engine-specific exception → user-friendly message translation.

Polars and DuckDB raise different exception shapes than pandas. Without
translation, the canvas user sees `polars.exceptions.ColumnNotFoundError:
column 'foo' not found` which is fine for engineers but useless for the
user. This module maps those shapes to OpError with messages keyed off
the node id and op kind so the canvas can surface them in context.

Polars / duckdb imports are lazy (try/except inside the translator) so
pandas-only installs don't pay the import cost or crash on missing
modules.
"""

from __future__ import annotations

from decoy_engine.graph.ops._base import OpError


def translate(exc: Exception, op_kind: str, node_id: str) -> OpError:
    """Map an engine exception to a user-friendly OpError.

    The translator is best-effort: anything not specifically handled gets
    wrapped with the node + kind context so canvas error messages always
    name *which* node failed, not just "something failed."

    R3.4 typed errors: when the input exception carries stable structured
    metadata (``ValidationError.code`` / ``.path``) we forward it onto
    the returned OpError so the runner can persist it on JobNodeRun and
    the manifest can route by code rather than parsing the free-text
    message. The forwarding is purely additive: bare ``OpError`` carries
    no code today, and ad-hoc Python exceptions stay unmapped.
    """
    if isinstance(exc, OpError):
        # Already user-friendly; just prefix with node context if missing.
        msg = str(exc)
        if not msg.startswith("Node "):
            new = OpError(f"Node {node_id!r} ({op_kind}): {msg}")
        else:
            new = exc
        _forward_structured_metadata(new, exc)
        return new

    # R3.4: ValidationError carries .code (stable per the R2.1 contract)
    # and .path; surface them on the returned OpError so the manifest's
    # nodes[].error gets a structured payload.
    try:
        from decoy_engine.internal.validator import ValidationError
    except ImportError:
        ValidationError = ()  # type: ignore[assignment]
    if ValidationError and isinstance(exc, ValidationError):
        out = OpError(f"Node {node_id!r} ({op_kind}): {exc}")
        # Promote the validator's code + path attributes onto the OpError
        # so downstream callers can read them via the same attribute
        # surface they would on any structured failure.
        if hasattr(exc, "code") and getattr(exc, "code", None):
            out.code = exc.code  # type: ignore[attr-defined]
        if hasattr(exc, "path") and getattr(exc, "path", None):
            out.path = exc.path  # type: ignore[attr-defined]
        return out

    polars_msg = _maybe_translate_polars(exc)
    if polars_msg is not None:
        return OpError(f"Node {node_id!r} ({op_kind}): {polars_msg}")

    duckdb_msg = _maybe_translate_duckdb(exc)
    if duckdb_msg is not None:
        return OpError(f"Node {node_id!r} ({op_kind}): {duckdb_msg}")

    # Default: wrap with node context. Don't lose the original message —
    # advanced users may need it for diagnosis. Forward any structured
    # ``.code`` / ``.path`` attributes from the source exception onto
    # the wrapped OpError so typed exceptions raised by ops (e.g.
    # PKDuplicatesError carries ``code = "pk.duplicates"``) make it
    # through to the manifest's per-node error metadata.
    out = OpError(f"Node {node_id!r} ({op_kind}): {exc}")
    _forward_structured_metadata(out, exc)
    return out


def _maybe_translate_polars(exc: Exception) -> str | None:
    """Return a translated message if `exc` is a polars exception, else None.

    Imports polars lazily so pandas-only installs (which never raise polars
    exceptions) don't pay the import cost."""
    try:
        import polars.exceptions as pe
    except ImportError:
        return None

    if isinstance(exc, pe.ColumnNotFoundError):
        col = _extract_first_quoted(str(exc)) or "?"
        return (
            f"column {col!r} not found in input. "
            f"Did upstream drop it or rename it?"
        )
    if isinstance(exc, pe.SchemaError):
        return (
            f"schema mismatch — {exc}. "
            f"Check column types match between inputs."
        )
    if isinstance(exc, pe.ComputeError):
        return f"compute error — {exc}. Check the expression / predicate."
    if isinstance(exc, pe.SQLInterfaceError):
        return (
            f"SQL interface error — {exc}. "
            f"Polars SQL accepts standard predicates; "
            f"Python-only operators (`is`, `in`) and unquoted identifiers can fail."
        )
    return None


def _maybe_translate_duckdb(exc: Exception) -> str | None:
    """Return a translated message if `exc` is a duckdb exception, else None."""
    try:
        import duckdb
    except ImportError:
        return None

    if isinstance(exc, duckdb.CatalogException):
        return (
            f"table or column missing — {exc}. "
            f"Check the connector's schema / table name."
        )
    if isinstance(exc, duckdb.IOException):
        return f"I/O error reading source — {exc}. Check the file path / DSN."
    if isinstance(exc, duckdb.ParserException):
        return (
            f"SQL parse error — {exc}. "
            f"Check the predicate / expression syntax."
        )
    if isinstance(exc, duckdb.BinderException):
        return (
            f"column binding error — {exc}. "
            f"Did upstream drop a column the predicate references?"
        )
    return None


def _extract_first_quoted(msg: str) -> str | None:
    """Best-effort: pull the first single-quoted token out of an error msg.
    Polars' ColumnNotFoundError formats as `column 'foo' not found` — this
    grabs the `foo` for the user-friendly message."""
    if "'" not in msg:
        return None
    parts = msg.split("'")
    if len(parts) < 3:
        return None
    return parts[1] or None


def _forward_structured_metadata(target: OpError, src: Exception) -> None:
    """Copy ``code`` / ``path`` attributes from src onto target if set.

    Used when translate() returns a NEW OpError instance (vs passing the
    incoming OpError through unchanged) so the structured metadata
    doesn't get dropped. Idempotent + tolerant of missing attrs.
    """
    if hasattr(src, "code"):
        code = getattr(src, "code", None)
        if code:
            target.code = code  # type: ignore[attr-defined]
    if hasattr(src, "path"):
        path = getattr(src, "path", None)
        if path:
            target.path = path  # type: ignore[attr-defined]
