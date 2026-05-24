"""Per-node static output-schema prediction (R2.3).

Some ops let validation know the names of columns they will produce
*without actually reading data*, just from their config. When that's
the case, downstream ops that reference column names (mask, derive,
drop_column, etc.) can be checked at validate time: a name they read
that the upstream cannot produce is a silent-no-op bug today.

This module exposes :func:`predicted_output_columns` which returns:

  - ``list[str]``     - the exact ordered column names the node will
                        produce when statically known.
  - ``"$auto"``       - the node WILL produce columns but their names
                        come from DuckDB / pandas auto-numbering
                        (``column0``, ``column1``, …). Downstream
                        checks should accept ``column<int>`` names and
                        flag everything else.
  - ``None``          - we cannot tell. The downstream check should
                        SKIP rather than fail; we don't want to block
                        a parquet source just because we haven't read
                        its schema.

Only the release-visible source kinds carry a useful implementation
today. Other ops fall through to ``None`` so the cross-node check is
a no-op there until they migrate.
"""

from __future__ import annotations

from typing import Any

ColumnPrediction = list[str] | str | None


def predicted_output_columns(node: dict[str, Any]) -> ColumnPrediction:
    kind = node.get("kind")
    cfg = node.get("config") or {}

    if kind == "source.file":
        return _source_file_predicted(cfg)
    # source.s3 / source.gcs / source.sftp could mirror source.file
    # once their CSV/fixed-width parser configs are unified -- skipped
    # in this initial slice so we don't claim more than we deliver.
    return None


def _source_file_predicted(cfg: dict[str, Any]) -> ColumnPrediction:
    fmt = (cfg.get("format") or "").lower() or None

    # Explicit column_names always wins -- it's the user-authored
    # ordered list and the engine reads from there at run time.
    cn = cfg.get("column_names")
    if isinstance(cn, list) and cn and all(isinstance(x, str) and x for x in cn):
        return list(cn)

    # fixed_width carries its names on fw_columns; the engine reads
    # them and feeds the pandas/duckdb output frame with those names.
    if fmt == "fixed_width" or _is_fixed_width_path(cfg.get("path")):
        fw = cfg.get("fw_columns")
        if isinstance(fw, list):
            out: list[str] = []
            for c in fw:
                if isinstance(c, dict):
                    name = c.get("name")
                    if isinstance(name, str) and name:
                        out.append(name)
            if out:
                return out
        # fw_columns missing - engine will reject earlier with
        # SOURCE_FILE_MISSING_FW_COLUMNS, no need to second-guess
        # the schema here.
        return None

    # CSV with has_header=false and no column_names produces DuckDB
    # auto-names (column0, column1, ...). We don't know how many but
    # we know the SHAPE of the names so downstream checks can accept
    # the column<int> pattern.
    if (fmt == "csv" or fmt is None) and cfg.get("has_header") is False:
        return "$auto"

    # CSV with has_header=true reads names from the file's first row.
    # We can't tell at validate time without reading; return None so
    # downstream checks skip. The platform layer COULD fill in a
    # known schema here when a file_id resolves to a known preview
    # (R2.4 preflight); engine alone stays portable.
    return None


def _is_fixed_width_path(path: Any) -> bool:
    """Best-effort: the engine treats .dat as fixed-width by default."""
    if not isinstance(path, str):
        return False
    return path.lower().endswith(".dat")


def is_auto_name(name: str) -> bool:
    """True iff ``name`` matches DuckDB's auto-numbered column shape
    (``column0``, ``column1``, …). Used by downstream cross-node
    checks when the upstream prediction is the ``"$auto"`` marker."""
    if not isinstance(name, str) or not name.startswith("column"):
        return False
    rest = name[len("column") :]
    return rest.isdigit()
