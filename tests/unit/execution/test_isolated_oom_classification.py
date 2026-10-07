"""Isolated-run memory-failure classification (docs/plans/2026-10-07-oom-classification.md).

The guarantee: a running job that exhausts its memory cap is named `oom_killed`, never an
opaque `crashed`. These tests drive the worker in-process (no subprocess) so each failure
shape is injected deterministically, plus the driver-side classifier as a parity table.
"""

from __future__ import annotations

import errno
import json
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _isolated_worker
from decoy_engine.execution._adapter import pandas_column_to_kernel_input
from decoy_engine.execution._isolated_common import (
    classify_abnormal_exit,
    is_memory_failure,
    scrub_error_text,
)
from decoy_engine.execution._strategies._redact import RedactHandler

_WRAPPING = "Unknown error: Wrapping {} failed"


def _payload(tmp_path: Path) -> dict[str, Any]:
    return {
        "config": {},
        "kwargs": {},
        "sources": {},
        "staging_output_dir": str(tmp_path / "staging"),
    }


def _fake_result() -> SimpleNamespace:
    return SimpleNamespace(
        outputs={}, quality_metrics={}, table_kinds={}, row_errors=(SimpleNamespace(),)
    )


def _raising(exc: BaseException):
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise exc

    return _raise


# --- 1. fallbacks preserved ---------------------------------------------------------------


class TestFallbacksPreserved:
    def test_redact_falls_back_after_arrow_memory_error(self, monkeypatch):
        monkeypatch.setattr(
            "decoy_engine.execution._strategies._redact.redact_array",
            _raising(pa.lib.ArrowMemoryError("injected")),
        )
        df = pd.DataFrame({"c": ["a", None, "b"]})
        plan = SimpleNamespace(provider_config=(("redact_with", "XX"),))
        out, warnings = RedactHandler().run(df, "c", plan, None)  # type: ignore[arg-type]
        assert out["c"].tolist()[0] == "XX"
        assert pd.isna(out["c"].tolist()[1])
        assert out["c"].tolist()[2] == "XX"
        assert warnings == []

    def test_kernel_input_falls_back_to_a_list_after_arrow_memory_error(self, monkeypatch):
        monkeypatch.setattr(pa, "array", _raising(pa.lib.ArrowMemoryError("injected")))
        out = pandas_column_to_kernel_input(pd.Series(["a", None, "b"]))
        assert out == ["a", None, "b"]


# --- 2. post-run steps are classified -----------------------------------------------------


class TestPostRunStepsClassified:
    def test_memory_error_in_finalize_outputs_is_oom_killed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_isolated_worker, "run_pipeline", lambda *a, **k: _fake_result())
        monkeypatch.setattr(_isolated_worker, "_finalize_outputs", _raising(MemoryError()))
        envelope = _isolated_worker._run(_payload(tmp_path))
        assert envelope["outcome"] == "oom_killed"

    def test_memory_error_in_stage_row_errors_is_oom_killed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_isolated_worker, "run_pipeline", lambda *a, **k: _fake_result())
        monkeypatch.setattr(_isolated_worker, "_finalize_outputs", lambda *a, **k: [])
        monkeypatch.setattr(_isolated_worker, "_stage_row_errors", _raising(MemoryError()))
        envelope = _isolated_worker._run(_payload(tmp_path))
        assert envelope["outcome"] == "oom_killed"

    def test_non_memory_error_in_finalize_outputs_is_crashed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_isolated_worker, "run_pipeline", lambda *a, **k: _fake_result())
        monkeypatch.setattr(_isolated_worker, "_finalize_outputs", _raising(ValueError("bug")))
        envelope = _isolated_worker._run(_payload(tmp_path))
        assert envelope["outcome"] == "crashed"


# --- 3. main()'s outer handler -------------------------------------------------------------


def _read_envelope(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))


class TestMainOuterHandler:
    def test_memory_error_outside_run_is_oom_killed(self, tmp_path, monkeypatch):
        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")
        monkeypatch.setattr(_isolated_worker, "_run", _raising(MemoryError()))
        assert _isolated_worker.main(["worker", str(payload_path)]) == 0
        assert _read_envelope(tmp_path)["outcome"] == "oom_killed"

    def test_malformed_payload_is_crashed(self, tmp_path):
        payload_path = tmp_path / "payload.json"
        payload_path.write_text("{not json", encoding="utf-8")
        assert _isolated_worker.main(["worker", str(payload_path)]) == 0
        assert _read_envelope(tmp_path)["outcome"] == "crashed"


# --- 4. recognition unchanged --------------------------------------------------------------


def _malformed_utf8_parquet(path: Path) -> None:
    data = b"John Smith\xff"
    offsets = pa.py_buffer(np.array([0, len(data)], dtype=np.int32).tobytes())
    arr = pa.Array.from_buffers(pa.string(), 1, [None, offsets, pa.py_buffer(data)])
    pq.write_table(pa.table({"c": arr}), path)


class TestRecognitionUnchanged:
    def test_single_token_wrapping_is_still_a_memory_failure(self):
        assert is_memory_failure(pa.lib.ArrowException(_WRAPPING.format("value")))

    def test_whitespace_wrapping_is_still_not_a_memory_failure(self):
        assert not is_memory_failure(pa.lib.ArrowException(_WRAPPING.format("John Smith")))

    def test_real_malformed_utf8_with_whitespace_is_crashed_and_scrubbed(
        self, tmp_path, monkeypatch
    ):
        src = tmp_path / "bad.parquet"
        _malformed_utf8_parquet(src)

        def _convert(*_a: Any, **_k: Any) -> Any:
            return pq.read_table(src).to_pandas()

        monkeypatch.setattr(_isolated_worker, "run_pipeline", _convert)
        envelope = _isolated_worker._run(_payload(tmp_path))
        assert envelope["outcome"] == "crashed"
        assert "Wrapping <value> failed" in envelope["error"]
        assert "John" not in envelope["error"]
        assert "Smith" not in envelope["error"]


# --- 5. scrub, through both handlers -------------------------------------------------------

_VALUES = {
    "long": "SECRETVAL" * 80,
    "multiline": "SECRET\nVAL\nUE",
    "embedded_failed": "SECRET failed VAL",
    "repeated": "SECRET failed Wrapping VAL",
}


def _leaks(error: str, value: str) -> bool:
    return any(part in error for part in ("SECRET", "VAL")) or value in error


class TestScrub:
    def test_scrub_function_replaces_only_the_value(self):
        assert scrub_error_text(_WRAPPING.format("abc")) == _WRAPPING.format("<value>")

    @pytest.mark.parametrize("name", list(_VALUES))
    def test_scrub_function_removes_the_whole_value(self, name):
        scrubbed = scrub_error_text(_WRAPPING.format(_VALUES[name]))
        assert scrubbed == _WRAPPING.format("<value>")

    def test_non_wrapping_text_is_unchanged(self):
        text = "ValueError: column 'x' not found, failed twice"
        assert scrub_error_text(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "step failed: Wrapping abc",
            "Wrapping abc",
            "Wrapping failed",
        ],
    )
    def test_wrapping_without_a_later_failed_is_unchanged(self, text):
        assert scrub_error_text(text) == text

    @pytest.mark.parametrize("name", list(_VALUES))
    def test_run_handler_scrubs(self, name, tmp_path, monkeypatch):
        exc = pa.lib.ArrowException(_WRAPPING.format(_VALUES[name]))
        monkeypatch.setattr(_isolated_worker, "run_pipeline", _raising(exc))
        error = _isolated_worker._run(_payload(tmp_path))["error"]
        assert not _leaks(error, _VALUES[name])
        assert error == "ArrowException: " + _WRAPPING.format("<value>")

    @pytest.mark.parametrize("name", list(_VALUES))
    def test_main_handler_scrubs(self, name, tmp_path, monkeypatch):
        payload_path = tmp_path / "payload.json"
        payload_path.write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")
        exc = pa.lib.ArrowException(_WRAPPING.format(_VALUES[name]))
        monkeypatch.setattr(_isolated_worker, "_run", _raising(exc))
        _isolated_worker.main(["worker", str(payload_path)])
        error = _read_envelope(tmp_path)["error"]
        assert not _leaks(error, _VALUES[name])
        assert error == "ArrowException: " + _WRAPPING.format("<value>")

    def test_a_long_value_is_scrubbed_before_truncation(self, tmp_path, monkeypatch):
        # Truncating first would cut the terminal " failed" and leave the value in place.
        exc = pa.lib.ArrowException(_WRAPPING.format("SECRET" * 200))
        monkeypatch.setattr(_isolated_worker, "run_pipeline", _raising(exc))
        error = _isolated_worker._run(_payload(tmp_path))["error"]
        assert "SECRET" not in error

    def test_non_wrapping_error_is_stored_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_isolated_worker, "run_pipeline", _raising(ValueError("plain failure")))
        assert _isolated_worker._run(_payload(tmp_path))["error"] == "ValueError: plain failure"


# --- 6. classifier parity -----------------------------------------------------------------


class TestClassifierParity:
    @pytest.mark.parametrize(
        "exc",
        [
            MemoryError(),
            pa.lib.ArrowMemoryError("x"),
            duckdb.OutOfMemoryException("x"),
            OSError(errno.ENOMEM, "x"),
            RuntimeError("not able to copy ctx"),
            RuntimeError("digital envelope routines: x"),
            pa.lib.ArrowException(_WRAPPING.format("value")),
        ],
    )
    def test_memory_shapes_are_memory_failures(self, exc):
        assert is_memory_failure(exc)

    def test_non_memory_exception_is_not(self):
        assert not is_memory_failure(ValueError("bug"))

    @pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGABRT])
    @pytest.mark.parametrize("stderr", ["", "bad_alloc", "some noise"])
    def test_kill_and_abort_are_oom_killed(self, sig, stderr):
        assert classify_abnormal_exit(-sig, stderr) == "oom_killed"

    @pytest.mark.parametrize(
        "stderr",
        [
            "MemoryError",
            "terminate called after throwing std::bad_alloc",
            "not able to copy ctx",
            "digital envelope routines",
            "cannot allocate memory for thread-local data",
        ],
    )
    @pytest.mark.parametrize("returncode", [1, -signal.SIGSEGV])
    def test_markers_in_stderr_are_oom_killed(self, returncode, stderr):
        assert classify_abnormal_exit(returncode, stderr) == "oom_killed"

    @pytest.mark.parametrize("returncode", [1, -signal.SIGSEGV])
    def test_wrapping_message_in_stderr_is_oom_killed(self, returncode):
        stderr = "pyarrow.lib.ArrowException: " + _WRAPPING.format("value")
        assert classify_abnormal_exit(returncode, stderr) == "oom_killed"

    def test_sigsegv_without_marker_is_crashed_deferred_behavior(self):
        assert classify_abnormal_exit(-signal.SIGSEGV, "") == "crashed"

    def test_positive_exit_without_marker_is_crashed(self):
        assert classify_abnormal_exit(1, "Traceback: ValueError") == "crashed"
