"""Characterization of the engine log destination.

Since project-layout E2 the engine writes no log file unless the caller passes `file`. An
explicit `file: null` stays off, a given path gets a rotating file, and an unwritable
destination degrades to console logging with a warning rather than killing the process.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from decoy_engine.internal import logging as engine_logging


@pytest.fixture(autouse=True)
def _fresh_logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine_logging, "_LOGGER_INSTANCE", None)
    logger = logging.getLogger("decoy_engine")
    saved = list(logger.handlers)
    level = logger.level
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in saved:
        logger.addHandler(handler)
    logger.setLevel(level)


def _file_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def test_default_attaches_no_file_handler_and_creates_no_log_folder(tmp_path: Path) -> None:
    logger = engine_logging.get_logger({})
    assert _file_handlers(logger) == []
    assert list(tmp_path.iterdir()) == []


def test_explicit_null_file_attaches_no_file_handler(tmp_path: Path) -> None:
    logger = engine_logging.get_logger({"file": None})
    assert _file_handlers(logger) == []
    assert not (tmp_path / "logs").exists()


def test_custom_file_setting_is_honored(tmp_path: Path) -> None:
    logger = engine_logging.get_logger({"file": "x/y/custom.log", "max_size_mb": 2})
    (handler,) = _file_handlers(logger)
    handler.flush()
    assert (tmp_path / "x" / "y" / "custom.log").is_file()
    assert "Logging initialized" in (tmp_path / "x" / "y" / "custom.log").read_text()
    assert Path(handler.baseFilename) == tmp_path / "x" / "y" / "custom.log"
    assert handler.maxBytes == 2 * 1024 * 1024


def test_unwritable_destination_falls_back_to_console_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the log directory should be")
    with caplog.at_level(logging.WARNING, logger="decoy_engine"):
        logger = engine_logging.get_logger({"file": "blocked/sub/x.log"})
    assert _file_handlers(logger) == []
    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    assert any("Could not open log file" in r.getMessage() for r in caplog.records)
