"""Unit tests for decoy_engine.internal.logging.

QA-internal-synth-providers F3 (2026-06-01, HIGH reliability) pins
the read-only-fs fallback contract: when the log directory cannot be
created or the RotatingFileHandler cannot open the file, get_logger
falls back to console-only logging with a warning instead of crashing
the whole process. This matters for Docker/K8s deployments where the
container fs is read-only and the configured log path is not writable.
"""

from __future__ import annotations

import logging as stdlib_logging
import subprocess
import sys
from unittest import mock

import pytest

from decoy_engine.internal.logging import get_logger


@pytest.fixture(autouse=True)
def _reset_logger_state():
    """Each cell starts with a fresh decoy_engine logger.

    get_logger configures the module-level "decoy_engine" logger; if
    we don't reset handlers + the configured flag between cells, the
    second cell inherits the first cell's handler set."""
    logger = stdlib_logging.getLogger("decoy_engine")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    # The package-import NullHandler is the library fallback, not per-cell state.
    logger.handlers = [h for h in saved_handlers if isinstance(h, stdlib_logging.NullHandler)]
    # Some logging implementations cache a `_configured` flag on the
    # module; reset it via the public surface (re-call get_logger with
    # a fresh config in each cell).
    yield
    logger.handlers = saved_handlers
    logger.setLevel(saved_level)


class TestQaInternalF3LoggingReadOnlyFs:
    """F3 (2026-06-01, HIGH reliability): get_logger does NOT crash
    on read-only filesystems. Falls back to console-only logging with
    a warning."""

    def test_get_logger_falls_back_on_oserror_during_mkdir(self, tmp_path):
        """Simulate read-only fs: mkdir raises PermissionError.
        Pre-fix this crashed the process. Post-fix get_logger returns
        a working logger that uses the console fallback."""
        with mock.patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only")):
            logger = get_logger({"file": str(tmp_path / "nope" / "decoy.log")})
        # If we reached here without raising, the fix held.
        assert logger is not None
        # Confirm a console-fallback handler was added (StreamHandler).
        has_stream = any(
            isinstance(h, stdlib_logging.StreamHandler)
            and not isinstance(h, stdlib_logging.handlers.RotatingFileHandler)  # type: ignore[attr-defined]
            for h in logger.handlers
        )
        # Either the existing console handler stayed, or the fallback
        # console handler was injected. Either way: console output
        # works.
        assert has_stream or any(
            isinstance(h, stdlib_logging.StreamHandler) for h in logger.handlers
        )

    def test_get_logger_falls_back_on_rotating_file_handler_error(self, tmp_path):
        """Simulate read-only fs: RotatingFileHandler raises
        PermissionError on open. mkdir might succeed (e.g. tmpfs) but
        the actual file open fails. Same console-fallback contract."""
        with mock.patch(
            "logging.handlers.RotatingFileHandler.__init__",
            side_effect=PermissionError("read-only"),
        ):
            logger = get_logger({"file": str(tmp_path / "decoy.log")})
        assert logger is not None
        # Process did not crash; that is the contract.

    def test_get_logger_with_no_file_config_works(self, tmp_path):
        """An explicit empty `file: ""` config disables file logging
        entirely (no try/except needed). Smoke test the no-file path."""
        logger = get_logger({"file": "", "console": True})
        assert logger is not None
        # Should have a console handler but no file handler.
        file_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, stdlib_logging.handlers.RotatingFileHandler)  # type: ignore[attr-defined]
        ]
        assert len(file_handlers) == 0

    def test_get_logger_writable_fs_uses_file_handler(self, tmp_path):
        """Sanity: on a writable fs (the default tmp_path), the file
        handler is added without falling back. The fix must not break
        the happy path."""
        log_path = tmp_path / "writable" / "decoy.log"
        logger = get_logger({"file": str(log_path)})
        assert logger is not None
        # A RotatingFileHandler should be present on the writable path.
        file_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, stdlib_logging.handlers.RotatingFileHandler)  # type: ignore[attr-defined]
        ]
        assert len(file_handlers) >= 1
        # And the dir was created.
        assert log_path.parent.exists()

    def test_get_logger_no_duplicate_console_handler_on_fallback(self, tmp_path):
        """Dennis D1 (2026-06-01, LOW): when config asks for
        console=True AND the file-handler creation falls back to
        console-only, the fallback must NOT add a second console
        handler. Pre-fix the operator got duplicate output on every
        line in the read-only-container + console=True combo."""
        from logging.handlers import RotatingFileHandler

        with mock.patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only")):
            logger = get_logger(
                {
                    "console": True,
                    "file": str(tmp_path / "nope" / "decoy.log"),
                }
            )

        # Count plain StreamHandlers (excluding RotatingFileHandler
        # which extends StreamHandler).
        console_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, stdlib_logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
        ]
        assert len(console_handlers) == 1, (
            f"Dennis D1: expected exactly one console handler when "
            f"console=True + file-fallback fires. Got {len(console_handlers)}."
        )


def _null_handlers(logger):
    return [h for h in logger.handlers if isinstance(h, stdlib_logging.NullHandler)]


class TestLibraryNullHandler:
    """E-a (CLI-QA1): the package attaches one NullHandler to `decoy_engine` and
    reconfiguration keeps exactly one while replacing only configured handlers."""

    def test_default_config_adds_no_file_handler(self):
        logger = get_logger({})
        assert not any(isinstance(h, stdlib_logging.FileHandler) for h in logger.handlers)

    def test_reconfiguration_keeps_exactly_one_null_handler(self):
        for _ in range(3):
            logger = get_logger({"console": True})
        assert len(_null_handlers(logger)) == 1

    def test_configured_handlers_are_replaced_and_closed(self, tmp_path):
        logger = get_logger({"file": str(tmp_path / "a.log")})
        first = [h for h in logger.handlers if isinstance(h, stdlib_logging.FileHandler)]
        assert len(first) == 1
        logger = get_logger({"file": str(tmp_path / "b.log")})
        files = [h for h in logger.handlers if isinstance(h, stdlib_logging.FileHandler)]
        assert len(files) == 1
        assert files[0] is not first[0]
        assert first[0].stream is None  # closed
        assert len(_null_handlers(logger)) == 1

    def test_null_handler_present_after_package_import(self):
        code = (
            "import logging, decoy_engine;"
            "h=[x for x in logging.getLogger('decoy_engine').handlers"
            " if isinstance(x, logging.NullHandler)];"
            "print(len(h))"
        )
        out = subprocess.run(  # noqa: S603 -- fixed argv, no user input
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "1"

    def test_child_warning_leaves_stderr_empty(self):
        code = (
            "import logging, decoy_engine;"
            "logging.getLogger('decoy_engine.child').warning('capacity advisory')"
        )
        out = subprocess.run(  # noqa: S603 -- fixed argv, no user input
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stderr == ""
