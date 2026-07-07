"""Unit tests for the OOM classifier in scripts/fk_memory_probe.py.

The capability proof's verdict rests on this classifier being fail-closed:
only a memory-shaped death may count as the expected baseline OOM, so a
genuine bug (bad plan, corrupt input, coded engine error) can never
masquerade as the OOM that proves the capability. These tests pin both
directions: the observed under-cap-only signatures classify as memory
failures, and plain non-memory failures never do.
"""

from __future__ import annotations

import importlib.util
import signal
from pathlib import Path

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fk_memory_probe.py"
_spec = importlib.util.spec_from_file_location("fk_memory_probe", _SCRIPT)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class TestIsMemoryFailure:
    def test_memory_error_subclasses(self) -> None:
        assert probe._is_memory_failure(MemoryError("malloc of size 42 failed"))
        assert probe._is_memory_failure(pa.lib.ArrowMemoryError("malloc of size 42 failed"))

    def test_enomem_oserror(self) -> None:
        import errno

        assert probe._is_memory_failure(OSError(errno.ENOMEM, "Cannot allocate memory"))
        assert not probe._is_memory_failure(OSError(errno.ENOENT, "No such file"))

    def test_openssl_ctx_copy_marker(self) -> None:
        assert probe._is_memory_failure(
            ValueError("[digital envelope routines] not able to copy ctx")
        )

    def test_arrow_value_wrap_failure_is_memory(self) -> None:
        # The under-cap-only arrow_to_pandas shape: a data value in the middle.
        assert probe._is_memory_failure(
            pa.lib.ArrowException("Unknown error: Wrapping c35311 failed")
        )

    def test_generic_unknown_error_is_not_memory(self) -> None:
        # The wrap-failure pattern must not absorb every UnknownError status.
        assert not probe._is_memory_failure(
            pa.lib.ArrowException("Unknown error: something else went wrong")
        )

    def test_non_memory_failures_stay_non_memory(self) -> None:
        assert not probe._is_memory_failure(
            ExecutionError(
                code="out_of_core_batch_rows_invalid", message="batch_rows must be positive"
            )
        )
        assert not probe._is_memory_failure(pa.lib.ArrowInvalid("Parquet magic bytes not found"))
        assert not probe._is_memory_failure(ImportError("No module named 'duckdb'"))
        assert not probe._is_memory_failure(ValueError("orphan_frac must be in [0, 1)"))


class TestClassifyCapabilityOutcome:
    def test_clean_exit_completed(self) -> None:
        rec = {"completed": True}
        assert probe._classify_capability_outcome(0, rec, "") == "completed"

    def test_clean_exit_self_classified_oom(self) -> None:
        rec = {"completed": False, "error": "MemoryError: malloc failed"}
        assert probe._classify_capability_outcome(0, rec, "") == "oom"

    def test_traceback_with_memory_marker_is_oom(self) -> None:
        stderr = "Traceback ...\nMemoryError: Unable to allocate 1.2 GiB"
        assert probe._classify_capability_outcome(1, None, stderr) == "oom"

    def test_traceback_with_arrow_wrap_failure_is_oom(self) -> None:
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "pyarrow/error.pxi", line 92, in pyarrow.lib.check_status\n'
            "pyarrow.lib.ArrowException: Unknown error: Wrapping c35311 failed"
        )
        assert probe._classify_capability_outcome(1, None, stderr) == "oom"

    def test_traceback_with_corrupt_input_is_failed(self) -> None:
        stderr = (
            "Traceback (most recent call last):\n"
            "pyarrow.lib.ArrowInvalid: Parquet magic bytes not found in footer"
        )
        assert probe._classify_capability_outcome(1, None, stderr) == "failed"

    def test_traceback_with_coded_engine_error_is_failed(self) -> None:
        stderr = (
            "Traceback (most recent call last):\n"
            "decoy_engine.execution._errors.ExecutionError: "
            "out_of_core_temp_disk_exceeded: spill footprint over budget"
        )
        assert probe._classify_capability_outcome(1, None, stderr) == "failed"

    def test_traceback_with_generic_unknown_error_is_failed(self) -> None:
        stderr = "pyarrow.lib.ArrowException: Unknown error: something else went wrong"
        assert probe._classify_capability_outcome(1, None, stderr) == "failed"

    def test_sigkill_and_sigabrt_are_oom(self) -> None:
        assert probe._classify_capability_outcome(-signal.SIGKILL, None, "") == "oom"
        assert probe._classify_capability_outcome(-signal.SIGABRT, None, "") == "oom"

    def test_bare_segfault_is_failed(self) -> None:
        assert probe._classify_capability_outcome(-signal.SIGSEGV, None, "") == "failed"
