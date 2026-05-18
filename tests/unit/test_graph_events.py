"""Unit tests for graph.events node lifecycle helpers."""
import pytest

from decoy_engine.graph.events import (
    emit_node_error,
    emit_node_ok,
    emit_node_start,
    make_node_error_record,
    make_node_ok_record,
)


class SpyLogger:
    """Minimal logger spy that captures calls for assertions."""

    def __init__(self) -> None:
        self.info_lines: list[str] = []
        self.error_lines: list[str] = []
        self.steps: list[dict] = []
        self.samples: list[float] = []

    def info(self, msg: str, *args, **kw) -> None:
        self.info_lines.append(msg % args if args else msg)

    def warning(self, msg: str, *args, **kw) -> None:
        pass

    def error(self, msg: str, *args, **kw) -> None:
        self.error_lines.append(msg % args if args else msg)

    def step(self, name: str, *, status: str, **kw) -> None:
        self.steps.append({"name": name, "status": status, **kw})

    def throughput_sample(self, rps: float) -> None:
        self.samples.append(rps)


class TestMakeNodeOkRecord:
    def test_basic_shape(self):
        rec = make_node_ok_record("n1", "mask", 100, 42, None)
        assert rec["node_id"] == "n1"
        assert rec["kind"] == "mask"
        assert rec["status"] == "ok"
        assert rec["row_count"] == 100
        assert rec["elapsed_ms"] == 42
        assert rec["error"] is None
        assert rec["exports"] is None

    def test_with_exports(self):
        exports = {"output_path": "/tmp/out.csv"}
        rec = make_node_ok_record("n1", "target.file", 0, 10, exports)
        assert rec["exports"] == exports

    def test_zero_rows(self):
        rec = make_node_ok_record("n1", "mask", 0, 5, None)
        assert rec["row_count"] == 0
        assert rec["status"] == "ok"


class TestMakeNodeErrorRecord:
    def test_basic_shape(self):
        rec = make_node_error_record("n1", "mask", 100, "something failed")
        assert rec["node_id"] == "n1"
        assert rec["kind"] == "mask"
        assert rec["status"] == "error"
        assert rec["row_count"] is None
        assert rec["elapsed_ms"] == 100
        assert rec["error"] == "something failed"
        assert rec["exports"] is None

    def test_no_error_code_or_path_by_default(self):
        rec = make_node_error_record("n1", "mask", 0, "err")
        assert "error_code" not in rec
        assert "error_path" not in rec

    def test_error_code_and_path_included_when_set(self):
        rec = make_node_error_record(
            "n1", "mask", 0, "err",
            error_code="mask.column_missing",
            error_path="nodes.n1.columns",
        )
        assert rec["error_code"] == "mask.column_missing"
        assert rec["error_path"] == "nodes.n1.columns"

    def test_exports_forwarded(self):
        rec = make_node_error_record("n1", "mask", 0, "err", exports={"k": "v"})
        assert rec["exports"] == {"k": "v"}


class TestEmitNodeStart:
    def test_logs_running_node(self):
        log = SpyLogger()
        emit_node_start(log, "n1", "[id=n1, kind=mask]", "pandas", 0)
        assert any("running node" in line for line in log.info_lines)
        assert any("pandas" in line for line in log.info_lines)

    def test_emits_step_start(self):
        log = SpyLogger()
        emit_node_start(log, "n1", "[id=n1, kind=mask]", "pandas", 50)
        assert log.steps == [{"name": "n1", "status": "start", "rows_in": 50}]

    def test_none_rows_in_passed_as_none_to_step(self):
        log = SpyLogger()
        emit_node_start(log, "n1", "[id=n1, kind=source.file]", "duckdb", 0)
        assert log.steps[0]["rows_in"] is None

    def test_no_log_when_logger_is_none(self):
        # Should not raise.
        emit_node_start(None, "n1", "[id=n1]", "pandas", 0)


class TestEmitNodeOk:
    def test_logs_ok_message(self):
        log = SpyLogger()
        emit_node_ok(log, "n1", "[id=n1]", 100, 200, 50)
        assert any("ok" in line for line in log.info_lines)
        assert any("rows=200" in line for line in log.info_lines)

    def test_split_suffix_in_log(self):
        log = SpyLogger()
        emit_node_ok(log, "n1", "[id=n1]", 0, 5, 10, is_split=True)
        assert any("(split)" in line for line in log.info_lines)

    def test_no_split_suffix_by_default(self):
        log = SpyLogger()
        emit_node_ok(log, "n1", "[id=n1]", 0, 5, 10)
        assert not any("(split)" in line for line in log.info_lines)

    def test_emits_finish_step(self):
        log = SpyLogger()
        emit_node_ok(log, "n1", "[id=n1]", 100, 200, 50)
        assert any(s["status"] == "finish" for s in log.steps)

    def test_emits_throughput_sample(self):
        log = SpyLogger()
        emit_node_ok(log, "n1", "[id=n1]", 0, 1000, 100)
        assert len(log.samples) == 1
        assert log.samples[0] == pytest.approx(10_000.0)

    def test_no_throughput_on_zero_elapsed(self):
        log = SpyLogger()
        emit_node_ok(log, "n1", "[id=n1]", 0, 1000, 0)
        assert log.samples == []

    def test_no_throughput_on_zero_rows(self):
        log = SpyLogger()
        emit_node_ok(log, "n1", "[id=n1]", 0, 0, 100)
        assert log.samples == []


class TestEmitNodeError:
    def test_logs_error_message(self):
        log = SpyLogger()
        exc = ValueError("bad")
        try:
            raise exc
        except ValueError:
            emit_node_error(log, "n1", "[id=n1]", 0, exc, exc, "n1", 50)
        assert any("failed" in line for line in log.error_lines)

    def test_emits_error_step(self):
        log = SpyLogger()
        exc = ValueError("boom")
        try:
            raise exc
        except ValueError:
            emit_node_error(log, "n1", "[id=n1]", 50, exc, exc, "n1", 30)
        error_steps = [s for s in log.steps if s["status"] == "error"]
        assert len(error_steps) == 1
        assert error_steps[0]["error_class"] == "ValueError"
        assert error_steps[0]["node_id"] == "n1"

    def test_no_raise_when_logger_is_none(self):
        exc = RuntimeError("x")
        try:
            raise exc
        except RuntimeError:
            emit_node_error(None, "n1", "[id=n1]", 0, exc, exc, "n1", 0)
