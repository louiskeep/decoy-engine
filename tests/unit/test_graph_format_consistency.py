"""Cross-node file format consistency check (R2.4 + R3.6).

GraphConfigValidator._validate_file_format_consistency originally
raised when a file source produced one format and a reachable file
target expected another with no convert.file_type node in between.
Item 66(a) had this as a logger.warning; R2.4 promoted it to a hard
ValidationError to defeat the silent-rewrite footgun.

R3.6 demoted it back to a warning (logger.warning at the engine
layer; platform preflight emits a structured advisory with
severity="warning"). Reason: the target writes whatever format the
user picked anyway, so the "conversion" is implicit and is the right
behavior. The R3.5 severity-policy makes warnings non-blocking, and
the target node UI banner discloses the conversion. The explicit
convert.file_type node stays as an advanced-tier affordance.

These tests confirm:
  - validate() never raises on a format mismatch (R3.6 behavior)
  - logger.warning fires with the source/target ids in the message
  - the back-fill no longer happens inside the validator (Sprint 2.2);
    validate_graph_full applies it to normalized_config via
    _backfill_target_file_formats after successful validation
"""
from __future__ import annotations

import logging

from decoy_engine.internal.validator import GraphConfigValidator


def _v() -> GraphConfigValidator:
    return GraphConfigValidator()


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
    cfg = _direct_graph(_src("data/in.csv"), _tgt("data/out.csv"))
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "auto-convert" not in caplog.text


def test_parquet_to_parquet_no_warning(caplog):
    cfg = _direct_graph(_src("data/in.parquet"), _tgt("data/out.parquet"))
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "auto-convert" not in caplog.text


def test_explicit_matching_formats_no_warning(caplog):
    cfg = _direct_graph(
        _src("data/in.csv", fmt="csv"),
        _tgt("data/out.csv", fmt="csv"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "auto-convert" not in caplog.text


def test_convert_file_type_on_path_suppresses_warning(caplog):
    """source.file -> convert.file_type -> target.file: the explicit
    converter is the user's "make the conversion auditable" affordance.
    No warning needed."""
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {"id": "cvt", "kind": "convert.file_type",
             "config": {"format": "parquet", "output_filename": "converted.parquet"}},
            {"id": "tgt", "kind": "target.file",
             "config": {"output_filename": "out.parquet"}},
        ],
        "edges": [
            {"from": "src", "to": "cvt"},
            {"from": "cvt", "to": "tgt"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
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
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "auto-convert" not in caplog.text


# ---------------------------------------------------------------------------
# Mismatch -- R3.6 logs a warning, does NOT raise
# ---------------------------------------------------------------------------


def test_csv_to_parquet_logs_warning(caplog):
    cfg = _direct_graph(_src("data/in.csv"), _tgt("data/out.parquet"))
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)  # no raise
    assert "auto-convert" in caplog.text
    assert "src" in caplog.text
    assert "tgt" in caplog.text
    assert "csv" in caplog.text
    assert "parquet" in caplog.text


def test_parquet_to_csv_logs_warning(caplog):
    cfg = _direct_graph(_src("data/in.parquet"), _tgt("data/out.csv"))
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "auto-convert" in caplog.text


def test_explicit_format_mismatch_logs_warning(caplog):
    """Extension says csv but explicit format on source says parquet
    -- still a mismatch, still a warning, still non-blocking."""
    cfg = _direct_graph(
        _src("data/in.csv", fmt="parquet"),
        _tgt("data/out.csv", fmt="csv"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "auto-convert" in caplog.text


# ---------------------------------------------------------------------------
# Back-fill: Sprint 2.2 -- validator is now pure, no in-place mutation
# ---------------------------------------------------------------------------


def test_target_format_not_mutated_by_validator_parquet():
    """Sprint 2.2: validator is now pure — format is no longer back-filled
    in-place. validate_graph_full applies it to normalized_config instead."""
    tgt_cfg: dict = {"output_filename": "out.parquet"}
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.parquet"}},
            {"id": "tgt", "kind": "target.file", "config": tgt_cfg},
        ],
        "edges": [{"from": "src", "to": "tgt"}],
    }
    _v().validate(cfg)
    assert "format" not in tgt_cfg


def test_target_format_not_mutated_by_validator_csv():
    """Sprint 2.2: validator is now pure — format is no longer back-filled
    in-place."""
    tgt_cfg: dict = {"output_filename": "out.csv"}
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {"id": "tgt", "kind": "target.file", "config": tgt_cfg},
        ],
        "edges": [{"from": "src", "to": "tgt"}],
    }
    _v().validate(cfg)
    assert "format" not in tgt_cfg


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
            {"id": "cvt", "kind": "convert.file_type",
             "config": {"format": "parquet", "output_filename": "converted.parquet"}},
            {"id": "tgt_ok", "kind": "target.file",
             "config": {"output_filename": "ok.parquet"}},
            {"id": "tgt_bad", "kind": "target.file",
             "config": {"output_filename": "bad.parquet"}},
        ],
        "edges": [
            {"from": "src", "to": "cvt"},
            {"from": "cvt", "to": "tgt_ok"},
            {"from": "src", "to": "tgt_bad"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    # The bad branch generates the warning; the converter branch is silent.
    assert "tgt_bad" in caplog.text
    assert "tgt_ok" not in caplog.text
