"""Unit tests for graph.events: node lifecycle event helpers."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from decoy_engine.graph.events import (
    _REDACT_KEYS,
    _summarize_node_config,
    node_error,
    node_export_error,
    node_ok,
    node_start,
)


class TestNodeStart(unittest.TestCase):
    def test_logs_running_node(self):
        log = MagicMock()
        with patch("decoy_engine.graph.events.emit_step"):
            node_start(log, "n1", "'src' [id=n1]", "pandas", 100, "source.file", {"path": "x"})
        log.info.assert_any_call(
            "graph: running node %s (engine=%s)", "'src' [id=n1]", "pandas"
        )

    def test_emit_step_start_called(self):
        log = MagicMock()
        with patch("decoy_engine.graph.events.emit_step") as mock_step:
            node_start(log, "n1", "desc", "pandas", 80, "mask", {"a": 1})
        mock_step.assert_called_once_with(log, "n1", status="start", rows_in=80)

    def test_rows_in_zero_becomes_none(self):
        log = MagicMock()
        with patch("decoy_engine.graph.events.emit_step") as mock_step:
            node_start(log, "n1", "desc", "hybrid", 0, "mask", {})
        mock_step.assert_called_once_with(log, "n1", status="start", rows_in=None)

    def test_config_summary_logged_when_cfg_nonempty(self):
        log = MagicMock()
        with patch("decoy_engine.graph.events.emit_step"):
            node_start(log, "n1", "desc", "pandas", 5, "mask", {"mode": "hash"})
        summary_calls = [c for c in log.info.call_args_list if "config:" in str(c)]
        assert len(summary_calls) == 1

    def test_config_summary_skipped_when_cfg_empty(self):
        log = MagicMock()
        with patch("decoy_engine.graph.events.emit_step"):
            node_start(log, "n1", "desc", "pandas", 5, "mask", {})
        assert log.info.call_count == 1

    def test_no_log_no_crash(self):
        with patch("decoy_engine.graph.events.emit_step"):
            node_start(None, "n1", "desc", "pandas", 10, "mask", {"a": 1})


class TestNodeOk(unittest.TestCase):
    def _call(self, rows_out=50, split=False, elapsed_ms=200, rows_in=100):
        log = MagicMock()
        with (
            patch("decoy_engine.graph.events.emit_step") as mock_step,
            patch("decoy_engine.graph.events.emit_throughput_sample") as mock_tp,
        ):
            rec = node_ok(
                log, "n1", "desc", "n1", "mask",
                {"x": 1}, elapsed_ms, rows_in, rows_out, split=split,
            )
        return log, mock_step, mock_tp, rec

    def test_returns_ok_status(self):
        _, _, _, rec = self._call()
        assert rec["status"] == "ok"

    def test_row_count_matches(self):
        _, _, _, rec = self._call(rows_out=30)
        assert rec["row_count"] == 30

    def test_error_field_is_none(self):
        _, _, _, rec = self._call()
        assert rec["error"] is None

    def test_emit_step_finish_called(self):
        log, mock_step, _, _ = self._call(rows_out=30, rows_in=100)
        mock_step.assert_called_once_with(
            log, "n1", status="finish", rows_in=100, rows_out=30,
        )

    def test_throughput_sample_emitted(self):
        _, _, mock_tp, _ = self._call(rows_out=50, elapsed_ms=200)
        mock_tp.assert_called_once()
        log_arg, sample_arg = mock_tp.call_args[0]
        assert abs(sample_arg - 50 * 1000 / 200) < 0.01

    def test_split_label_appended(self):
        log, _, _, _ = self._call(split=True)
        args = log.info.call_args[0]
        assert args[-1] == " (split)"

    def test_no_split_label_by_default(self):
        log, _, _, _ = self._call(split=False)
        args = log.info.call_args[0]
        assert args[-1] == ""

    def test_no_throughput_on_zero_rows(self):
        log = MagicMock()
        with (
            patch("decoy_engine.graph.events.emit_step"),
            patch("decoy_engine.graph.events.emit_throughput_sample") as mock_tp,
        ):
            node_ok(log, "n1", "desc", "n1", "mask", None, 200, 0, 0)
        mock_tp.assert_not_called()

    def test_no_throughput_on_zero_elapsed(self):
        log = MagicMock()
        with (
            patch("decoy_engine.graph.events.emit_step"),
            patch("decoy_engine.graph.events.emit_throughput_sample") as mock_tp,
        ):
            node_ok(log, "n1", "desc", "n1", "mask", None, 0, 0, 50)
        mock_tp.assert_not_called()

    def test_no_log_no_crash(self):
        with (
            patch("decoy_engine.graph.events.emit_step"),
            patch("decoy_engine.graph.events.emit_throughput_sample"),
        ):
            rec = node_ok(None, "n1", "desc", "n1", "mask", None, 10, 0, 5)
        assert rec["status"] == "ok"


class TestNodeError(unittest.TestCase):
    def _make_exc(self, code="E001", path="config.col"):
        raw = ValueError("raw")
        translated = RuntimeError("translated msg")
        if code is not None:
            translated.code = code
        if path is not None:
            translated.path = path
        return raw, translated

    def _call(self, traceback_str=None, code="E001", path="config.col"):
        log = MagicMock()
        raw, translated = self._make_exc(code=code, path=path)
        with patch("decoy_engine.graph.events.emit_step") as mock_step:
            rec = node_error(
                log, "n1", "desc", "n1", "mask",
                {"x": 1}, 150, 80, raw, translated,
                traceback_str=traceback_str,
            )
        return log, mock_step, rec

    def test_returns_error_status(self):
        _, _, rec = self._call()
        assert rec["status"] == "error"

    def test_row_count_is_none(self):
        _, _, rec = self._call()
        assert rec["row_count"] is None

    def test_error_message_is_translated(self):
        _, _, rec = self._call()
        assert rec["error"] == "translated msg"

    def test_error_code_forwarded(self):
        _, _, rec = self._call(code="E001")
        assert rec["error_code"] == "E001"

    def test_error_path_forwarded(self):
        _, _, rec = self._call(path="config.col")
        assert rec["error_path"] == "config.col"

    def test_emit_step_error_called(self):
        log, mock_step, _ = self._call()
        mock_step.assert_called_once_with(
            log, "n1", status="error",
            rows_in=80,
            error_class="ValueError",
            error_msg="translated msg",
            node_id="n1",
        )

    def test_traceback_logged_when_provided(self):
        log, _, _ = self._call(traceback_str="Traceback...\n  line 1")
        assert log.error.call_count == 2

    def test_no_traceback_log_without_str(self):
        log, _, _ = self._call(traceback_str=None)
        assert log.error.call_count == 1

    def test_error_code_none_when_absent(self):
        _, _, rec = self._call(code=None, path=None)
        assert rec["error_code"] is None
        assert rec["error_path"] is None

    def test_no_log_no_crash(self):
        raw, translated = self._make_exc()
        with patch("decoy_engine.graph.events.emit_step"):
            rec = node_error(None, "n1", "desc", "n1", "mask", None, 0, 0, raw, translated)
        assert rec["status"] == "error"

    def test_rows_in_zero_becomes_none_in_emit_step(self):
        log = MagicMock()
        raw, translated = self._make_exc()
        with patch("decoy_engine.graph.events.emit_step") as mock_step:
            node_error(log, "n1", "desc", "n1", "mask", None, 50, 0, raw, translated)
        call_kwargs = mock_step.call_args[1]
        assert call_kwargs["rows_in"] is None


class TestNodeExportError(unittest.TestCase):
    def test_returns_error_status(self):
        rec = node_export_error(None, "desc", "n1", "mask", RuntimeError("bad ref"))
        assert rec["status"] == "error"

    def test_elapsed_ms_is_zero(self):
        rec = node_export_error(None, "desc", "n1", "mask", RuntimeError("x"))
        assert rec["elapsed_ms"] == 0

    def test_exports_is_none(self):
        rec = node_export_error(None, "desc", "n1", "mask", RuntimeError("x"))
        assert rec["exports"] is None

    def test_error_message_set(self):
        rec = node_export_error(None, "desc", "n1", "mask", RuntimeError("bad ref"))
        assert rec["error"] == "bad ref"

    def test_logs_when_log_present(self):
        log = MagicMock()
        node_export_error(log, "desc", "n1", "mask", RuntimeError("x"))
        log.error.assert_called_once()

    def test_no_log_no_crash(self):
        node_export_error(None, "desc", "n1", "mask", ValueError("x"))

    def test_node_id_in_record(self):
        rec = node_export_error(None, "desc", "node42", "mask", ValueError("x"))
        assert rec["node_id"] == "node42"


class TestSummarizeNodeConfig(unittest.TestCase):
    def test_empty_cfg_message(self):
        assert _summarize_node_config("mask", {}) == "config: (no config)"

    def test_none_cfg_message(self):
        assert _summarize_node_config("mask", None) == "config: (no config)"

    def test_simple_string_value(self):
        result = _summarize_node_config("mask", {"mode": "hash"})
        assert "mode='hash'" in result

    def test_redacts_password(self):
        result = _summarize_node_config("target.db", {"password": "s3cr3t"})
        assert "s3cr3t" not in result
        assert "***" in result

    def test_redacts_token_in_key_name(self):
        result = _summarize_node_config("source.s3", {"api_token": "tok"})
        assert "tok" not in result

    def test_skips_private_keys(self):
        result = _summarize_node_config("mask", {"__engine": "pandas", "mode": "hash"})
        assert "__engine" not in result
        assert "mode" in result

    def test_dict_value_shows_key_count(self):
        result = _summarize_node_config("mask", {"mapping": {"a": 1, "b": 2}})
        assert "2 keys" in result

    def test_list_value_shows_item_count(self):
        result = _summarize_node_config("mask", {"columns": ["a", "b", "c"]})
        assert "3 items" in result

    def test_long_string_truncated(self):
        long_str = "x" * 100
        result = _summarize_node_config("mask", {"path": long_str})
        assert "..." in result
        assert "x" * 100 not in result

    def test_caps_at_six_items(self):
        cfg = {f"k{i}": i for i in range(10)}
        result = _summarize_node_config("mask", cfg)
        assert "..." in result

    def test_returns_string_prefixed_config(self):
        result = _summarize_node_config("mask", {"mode": "hash"})
        assert result.startswith("config: ")

    def test_redact_keys_set_contains_expected(self):
        assert "password" in _REDACT_KEYS
        assert "token" in _REDACT_KEYS
        assert "api_key" in _REDACT_KEYS


if __name__ == "__main__":
    unittest.main()
