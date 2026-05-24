"""Cross-node file format consistency check (R2.4 + R3.6 + V2.0-B).

`validate_file_format_consistency` raised when a file source produced
one format and a reachable file target expected another with no
convert.file_type node in between. Item 66(a) had this as a
logger.warning; R2.4 promoted it to a hard ValidationError to defeat
the silent-rewrite footgun.

R3.6 demoted it back to a warning (logger.warning at the engine
layer; platform preflight emits a structured advisory with
severity="warning"). Reason: the target writes whatever format the
user picked anyway, so the "conversion" is implicit and is the right
behavior. The R3.5 severity-policy makes warnings non-blocking, and
the target node UI banner discloses the conversion. The explicit
convert.file_type node stays as an advanced-tier affordance.

V2.0-B split: the function now lives in
`decoy_engine.graph.validators.cross_node` and is pure (no mutation).
The format back-fill that used to live inside this function moved to
`decoy_engine.graph.normalize.normalize_config`. Tests below cover
both halves:
  - warning behavior on the pure validator
  - back-fill behavior on normalize_config (returns a new dict)
"""

from __future__ import annotations

import logging

from decoy_engine.graph.normalize import normalize_config
from decoy_engine.graph.validators.cross_node import (
    validate_file_format_consistency,
)


def _check(cfg: dict, caplog) -> None:
    """Run the validator with a caplog-attached logger."""
    log = logging.getLogger("decoy_engine.graph.validate.test")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    with caplog.at_level(logging.WARNING, logger=log.name):
        validate_file_format_consistency(cfg["nodes"], cfg.get("edges") or [], logger=log)


# ---------------------------------------------------------------------------
# Helpers to build minimal valid graph configs
# ---------------------------------------------------------------------------


def _src(path: str, fmt: str | None = None) -> dict:
    cfg: dict = {"path": path}
    if fmt is not None:
        cfg["format"] = fmt
    return {"id": "src", "kind": "source.file", "config": cfg}


def _tgt(filename: str, fmt: str | None = None) -> dict:
    cfg: dict = {"output_filename": filename}
    if fmt is not None:
        cfg["format"] = fmt
    return {"id": "tgt", "kind": "target.file", "config": cfg}


def _direct_graph(src_node, tgt_node) -> dict:
    return {
        "mode": "graph",
        "nodes": [src_node, tgt_node],
        "edges": [{"from": "src", "to": "tgt"}],
    }


# ---------------------------------------------------------------------------
# Happy paths -- no warning expected
# ---------------------------------------------------------------------------


def test_csv_to_csv_no_warning(caplog):
    _check(_direct_graph(_src("data/in.csv"), _tgt("data/out.csv")), caplog)
    assert "auto-convert" not in caplog.text


def test_parquet_to_parquet_no_warning(caplog):
    _check(
        _direct_graph(_src("data/in.parquet"), _tgt("data/out.parquet")),
        caplog,
    )
    assert "auto-convert" not in caplog.text


def test_explicit_matching_formats_no_warning(caplog):
    _check(
        _direct_graph(
            _src("data/in.csv", fmt="csv"),
            _tgt("data/out.csv", fmt="csv"),
        ),
        caplog,
    )
    assert "auto-convert" not in caplog.text


def test_convert_file_type_on_path_suppresses_warning(caplog):
    """source.file -> convert.file_type -> target.file: the explicit
    converter is the user's "make the conversion auditable" affordance.
    No warning needed."""
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {
                "id": "cvt",
                "kind": "convert.file_type",
                "config": {"format": "parquet", "output_filename": "converted.parquet"},
            },
            {"id": "tgt", "kind": "target.file", "config": {"output_filename": "out.parquet"}},
        ],
        "edges": [
            {"from": "src", "to": "cvt"},
            {"from": "cvt", "to": "tgt"},
        ],
    }
    _check(cfg, caplog)
    assert "auto-convert" not in caplog.text


def test_no_target_file_node_no_warning(caplog):
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {"id": "lim", "kind": "limit", "config": {"n": 5}},
        ],
        "edges": [{"from": "src", "to": "lim"}],
    }
    _check(cfg, caplog)
    assert "auto-convert" not in caplog.text


# ---------------------------------------------------------------------------
# Mismatch -- R3.6 logs a warning, does NOT raise
# ---------------------------------------------------------------------------


def test_csv_to_parquet_logs_warning(caplog):
    _check(_direct_graph(_src("data/in.csv"), _tgt("data/out.parquet")), caplog)
    assert "auto-convert" in caplog.text
    assert "src" in caplog.text
    assert "tgt" in caplog.text
    assert "csv" in caplog.text
    assert "parquet" in caplog.text


def test_parquet_to_csv_logs_warning(caplog):
    _check(_direct_graph(_src("data/in.parquet"), _tgt("data/out.csv")), caplog)
    assert "auto-convert" in caplog.text


def test_explicit_format_mismatch_logs_warning(caplog):
    """Extension says csv but explicit format on source says parquet
    -- still a mismatch, still a warning, still non-blocking."""
    _check(
        _direct_graph(
            _src("data/in.csv", fmt="parquet"),
            _tgt("data/out.csv", fmt="csv"),
        ),
        caplog,
    )
    assert "auto-convert" in caplog.text


# ---------------------------------------------------------------------------
# Back-fill: target format is set to source format when absent.
# V2.0-B contract: the back-fill happens in normalize_config, not in
# the validator. The validator no longer mutates caller input.
# ---------------------------------------------------------------------------


def test_target_format_backfilled_from_parquet_source():
    src_tgt_cfg: dict = {"output_filename": "out.parquet"}
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.parquet"}},
            {"id": "tgt", "kind": "target.file", "config": src_tgt_cfg},
        ],
        "edges": [{"from": "src", "to": "tgt"}],
    }
    normalized = normalize_config(cfg)
    # Original caller-held config is untouched (V2.0-B contract).
    assert "format" not in src_tgt_cfg
    # The normalized copy carries the back-filled format.
    norm_tgt_cfg = normalized["nodes"][1]["config"]
    assert norm_tgt_cfg.get("format") == "parquet"


def test_target_format_backfilled_from_csv_source():
    src_tgt_cfg: dict = {"output_filename": "out.csv"}
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {"id": "tgt", "kind": "target.file", "config": src_tgt_cfg},
        ],
        "edges": [{"from": "src", "to": "tgt"}],
    }
    normalized = normalize_config(cfg)
    assert "format" not in src_tgt_cfg
    norm_tgt_cfg = normalized["nodes"][1]["config"]
    assert norm_tgt_cfg.get("format") == "csv"


# ---------------------------------------------------------------------------
# Fork: converter on one branch, direct on another
# ---------------------------------------------------------------------------


def test_forked_graph_warns_only_for_direct_branch(caplog):
    """source.file forks to two targets:
    - one branch goes through convert.file_type (no warning)
    - the other goes direct with a format mismatch (warning logs,
      validate does NOT raise)
    """
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {
                "id": "cvt",
                "kind": "convert.file_type",
                "config": {"format": "parquet", "output_filename": "converted.parquet"},
            },
            {"id": "tgt_ok", "kind": "target.file", "config": {"output_filename": "ok.parquet"}},
            {"id": "tgt_bad", "kind": "target.file", "config": {"output_filename": "bad.parquet"}},
        ],
        "edges": [
            {"from": "src", "to": "cvt"},
            {"from": "cvt", "to": "tgt_ok"},
            {"from": "src", "to": "tgt_bad"},
        ],
    }
    _check(cfg, caplog)
    # The bad branch generates the warning; the converter branch is silent.
    assert "tgt_bad" in caplog.text
    assert "tgt_ok" not in caplog.text
