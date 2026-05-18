"""Unit tests for graph.events (Sprint 1.3)."""
import pytest

from decoy_engine.graph.events import (
    _config_summary,
    make_error_record,
    make_export_error_record,
    make_ok_record,
    node_descriptor,
)


# ── node_descriptor ───────────────────────────────────────────────────────────

def test_node_descriptor_no_name():
    assert node_descriptor({"id": "n1", "kind": "filter"}) == "[id=n1, kind=filter]"


def test_node_descriptor_with_name():
    r = node_descriptor({"id": "n1", "kind": "filter", "name": "My Filter"})
    assert "My Filter" in r
    assert "n1" in r


def test_node_descriptor_blank_name_falls_back():
    r = node_descriptor({"id": "n1", "kind": "filter", "name": "   "})
    assert r == "[id=n1, kind=filter]"


def test_node_descriptor_missing_fields():
    r = node_descriptor({})
    assert "?" in r


# ── make_ok_record ────────────────────────────────────────────────────────────

def test_make_ok_record_fields():
    rec = make_ok_record("n1", "filter", 42, 100, {"k": 1})
    assert rec["node_id"] == "n1"
    assert rec["kind"] == "filter"
    assert rec["status"] == "ok"
    assert rec["row_count"] == 100
    assert rec["elapsed_ms"] == 42
    assert rec["error"] is None
    assert rec["exports"] == {"k": 1}


def test_make_ok_record_none_exports():
    rec = make_ok_record("n1", "filter", 0, 0, None)
    assert rec["exports"] is None
    assert rec["status"] == "ok"


def test_make_ok_record_zero_rows():
    rec = make_ok_record("sink", "target.file", 10, 0, None)
    assert rec["row_count"] == 0
    assert rec["status"] == "ok"


# ── make_error_record ─────────────────────────────────────────────────────────

class _TaggedError(Exception):
    def __init__(self, msg, code=None, path=None):
        super().__init__(msg)
        self.code = code
        self.path = path


def test_make_error_record_fields():
    exc = _TaggedError("bad stuff", code="E_BAD", path="nodes[0].config")
    rec = make_error_record("n1", "mask", 15, exc, None)
    assert rec["status"] == "error"
    assert rec["row_count"] is None
    assert rec["elapsed_ms"] == 15
    assert "bad stuff" in rec["error"]
    assert rec["error_code"] == "E_BAD"
    assert rec["error_path"] == "nodes[0].config"
    assert rec["exports"] is None


def test_make_error_record_untagged_exception():
    exc = ValueError("boom")
    rec = make_error_record("n1", "filter", 5, exc, None)
    assert rec["error_code"] is None
    assert rec["error_path"] is None
    assert "boom" in rec["error"]


# ── make_export_error_record ───────────────────────────────────────────────────

def test_make_export_error_record_elapsed_is_zero():
    rec = make_export_error_record("n1", "derive", ValueError("no ref"))
    assert rec["elapsed_ms"] == 0
    assert rec["exports"] is None
    assert rec["status"] == "error"
    assert rec["row_count"] is None


# ── _config_summary ─────────────────────────────────────────────────────────────

def test_config_summary_empty():
    assert _config_summary({}) == "config: (no config)"


def test_config_summary_skips_private_keys():
    summary = _config_summary({"__engine": "pandas", "col": "x"})
    assert "__engine" not in summary
    assert "col" in summary


def test_config_summary_redacts_secrets():
    summary = _config_summary({"password": "hunter2", "col": "x"})
    assert "hunter2" not in summary
    assert "***" in summary


def test_config_summary_redacts_token_key():
    summary = _config_summary({"api_token": "abc123"})
    assert "abc123" not in summary


def test_config_summary_truncates_long_strings():
    long_val = "x" * 200
    summary = _config_summary({"path": long_val})
    assert len(summary) < 300
    assert "..." in summary


def test_config_summary_shows_dict_key_count():
    summary = _config_summary({"cols": {"a": 1, "b": 2}})
    assert "2 keys" in summary


def test_config_summary_shows_list_item_count():
    summary = _config_summary({"items": [1, 2, 3]})
    assert "3 items" in summary


def test_config_summary_caps_at_six_fields():
    cfg = {f"k{i}": i for i in range(20)}
    summary = _config_summary(cfg)
    assert "..." in summary
