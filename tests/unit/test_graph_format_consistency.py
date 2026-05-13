"""Unit tests for Item 66(a): cross-node file format consistency warning.

GraphConfigValidator._validate_file_format_consistency warns when a
source.file and a reachable target.file have mismatched formats and no
convert.file_type node sits between them.
"""
from __future__ import annotations

import logging

import pytest

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
    """Single edge source → target."""
    return {
        "mode": "graph",
        "nodes": [src_node, tgt_node],
        "edges": [{"from": "src", "to": "tgt"}],
    }


# ---------------------------------------------------------------------------
# Happy paths — no warning expected
# ---------------------------------------------------------------------------


def test_csv_to_csv_no_warning(caplog):
    cfg = _direct_graph(
        _src("data/in.csv"),
        _tgt("data/out.csv"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "convert.file_type" not in caplog.text


def test_parquet_to_parquet_no_warning(caplog):
    cfg = _direct_graph(
        _src("data/in.parquet"),
        _tgt("data/out.parquet"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "convert.file_type" not in caplog.text


def test_explicit_matching_formats_no_warning(caplog):
    cfg = _direct_graph(
        _src("data/in.csv", fmt="csv"),
        _tgt("data/out.csv", fmt="csv"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "convert.file_type" not in caplog.text


def test_convert_file_type_on_path_suppresses_warning(caplog):
    """source.file → convert.file_type → target.file: user asked for conversion."""
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {"id": "cvt", "kind": "convert.file_type", "config": {"format": "parquet"}},
            {"id": "tgt", "kind": "target.file", "config": {"output_filename": "out.parquet"}},
        ],
        "edges": [
            {"from": "src", "to": "cvt"},
            {"from": "cvt", "to": "tgt"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "convert.file_type" not in caplog.text


def test_no_target_file_node_no_warning(caplog):
    """Graph with no target.file should produce no format warning."""
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
    assert "convert.file_type" not in caplog.text


# ---------------------------------------------------------------------------
# Mismatch — warning must fire
# ---------------------------------------------------------------------------


def test_csv_to_parquet_warns(caplog):
    cfg = _direct_graph(
        _src("data/in.csv"),
        _tgt("data/out.parquet"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "convert.file_type" in caplog.text
    assert "src" in caplog.text
    assert "tgt" in caplog.text


def test_parquet_to_csv_warns(caplog):
    cfg = _direct_graph(
        _src("data/in.parquet"),
        _tgt("data/out.csv"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "convert.file_type" in caplog.text


def test_explicit_format_mismatch_warns(caplog):
    """Extension says csv but explicit format says parquet on source."""
    cfg = _direct_graph(
        _src("data/in.csv", fmt="parquet"),
        _tgt("data/out.csv", fmt="csv"),
    )
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "convert.file_type" in caplog.text


# ---------------------------------------------------------------------------
# Back-fill: target format is set to source format when absent
# ---------------------------------------------------------------------------


def test_target_format_backfilled_from_parquet_source():
    """When target has no explicit format and extension matches source, the
    config dict is mutated to include the resolved format string."""
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
    assert tgt_cfg.get("format") == "parquet"


def test_target_format_backfilled_from_csv_source():
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
    assert tgt_cfg.get("format") == "csv"


# ---------------------------------------------------------------------------
# Fork: converter on one branch, direct on another
# ---------------------------------------------------------------------------


def test_forked_graph_warns_only_for_direct_branch(caplog):
    """source.file forks to two targets:
      - one branch goes through convert.file_type → no warning
      - the other goes direct with a format mismatch → warning
    """
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {"id": "cvt", "kind": "convert.file_type", "config": {"format": "parquet"}},
            # Reaches tgt_ok via converter — no warning.
            {"id": "tgt_ok", "kind": "target.file", "config": {"output_filename": "ok.parquet"}},
            # Reaches tgt_bad directly — format mismatch → warning.
            {"id": "tgt_bad", "kind": "target.file", "config": {"output_filename": "bad.parquet"}},
        ],
        "edges": [
            {"from": "src", "to": "cvt"},
            {"from": "cvt", "to": "tgt_ok"},
            {"from": "src", "to": "tgt_bad"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        _v().validate(cfg)
    assert "tgt_bad" in caplog.text
    assert "tgt_ok" not in caplog.text
