"""Cross-node file format consistency check (R2.4).

GraphConfigValidator._validate_file_format_consistency raises when a
file source and a reachable file target have mismatched formats and
no convert.file_type node sits between them. Used to be a
logger.warning under Item 66(a); promoted to a hard ValidationError
under R2.4 because the runner would otherwise crash mid-pipeline or
write a wrong-format file.
"""
from __future__ import annotations

import pytest

from decoy_engine.internal.validator import GraphConfigValidator, ValidationError
from decoy_engine.validation_result import CODES


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
    """Single edge source -> target."""
    return {
        "mode": "graph",
        "nodes": [src_node, tgt_node],
        "edges": [{"from": "src", "to": "tgt"}],
    }


# ---------------------------------------------------------------------------
# Happy paths -- no error expected
# ---------------------------------------------------------------------------


def test_csv_to_csv_no_error():
    cfg = _direct_graph(
        _src("data/in.csv"),
        _tgt("data/out.csv"),
    )
    _v().validate(cfg)  # no raise


def test_parquet_to_parquet_no_error():
    cfg = _direct_graph(
        _src("data/in.parquet"),
        _tgt("data/out.parquet"),
    )
    _v().validate(cfg)


def test_explicit_matching_formats_no_error():
    cfg = _direct_graph(
        _src("data/in.csv", fmt="csv"),
        _tgt("data/out.csv", fmt="csv"),
    )
    _v().validate(cfg)


def test_convert_file_type_on_path_suppresses_error():
    """source.file -> convert.file_type -> target.file: user asked for conversion."""
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
    _v().validate(cfg)


def test_no_target_file_node_no_error():
    """Graph with no file target should not trip the cross-node check."""
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
            {"id": "lim", "kind": "limit", "config": {"n": 5}},
        ],
        "edges": [{"from": "src", "to": "lim"}],
    }
    _v().validate(cfg)


# ---------------------------------------------------------------------------
# Mismatch -- ValidationError must fire (R2.4)
# ---------------------------------------------------------------------------


def test_csv_to_parquet_raises():
    cfg = _direct_graph(
        _src("data/in.csv"),
        _tgt("data/out.parquet"),
    )
    with pytest.raises(ValidationError) as exc_info:
        _v().validate(cfg)
    assert exc_info.value.code == CODES.GRAPH_FORMAT_MISMATCH
    assert exc_info.value.path == "nodes.tgt.config"
    assert "csv" in str(exc_info.value)
    assert "parquet" in str(exc_info.value)
    assert "convert.file_type" in str(exc_info.value)


def test_parquet_to_csv_raises():
    cfg = _direct_graph(
        _src("data/in.parquet"),
        _tgt("data/out.csv"),
    )
    with pytest.raises(ValidationError) as exc_info:
        _v().validate(cfg)
    assert exc_info.value.code == CODES.GRAPH_FORMAT_MISMATCH


def test_explicit_format_mismatch_raises():
    """Extension says csv but explicit format says parquet on source."""
    cfg = _direct_graph(
        _src("data/in.csv", fmt="parquet"),
        _tgt("data/out.csv", fmt="csv"),
    )
    with pytest.raises(ValidationError) as exc_info:
        _v().validate(cfg)
    assert exc_info.value.code == CODES.GRAPH_FORMAT_MISMATCH


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


def test_forked_graph_raises_only_for_direct_branch():
    """source.file forks to two targets:
      - one branch goes through convert.file_type -> ok
      - the other goes direct with a format mismatch -> raise

    The validator stops at the first error, so the message must
    identify the bad branch (tgt_bad), not the converted one.
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
    with pytest.raises(ValidationError) as exc_info:
        _v().validate(cfg)
    assert exc_info.value.code == CODES.GRAPH_FORMAT_MISMATCH
    assert "tgt_bad" in str(exc_info.value)
