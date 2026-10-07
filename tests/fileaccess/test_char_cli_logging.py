"""E1-0 characterization: the default CLI log destination is unchanged.

With no policy the engine logger writes a rotating file at `logs/decoy_engine.log` under the
CWD, an explicit `file: null` opts out, and an unwritable destination degrades to console
logging with a warning rather than killing the process.
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


def test_default_log_file_is_created_under_the_cwd(tmp_path: Path) -> None:
    logger = engine_logging.get_logger({})
    (handler,) = _file_handlers(logger)
    handler.flush()
    path = tmp_path / "logs" / "decoy_engine.log"
    assert path.is_file()
    assert Path(handler.baseFilename) == path
    assert "Logging initialized" in path.read_text()
    assert handler.maxBytes == 10 * 1024 * 1024 and handler.backupCount == 5


def test_explicit_null_file_attaches_no_file_handler(tmp_path: Path) -> None:
    logger = engine_logging.get_logger({"file": None})
    assert _file_handlers(logger) == []
    assert not (tmp_path / "logs").exists()


def test_custom_file_setting_is_honored(tmp_path: Path) -> None:
    logger = engine_logging.get_logger({"file": "x/y/custom.log", "max_size_mb": 2})
    (handler,) = _file_handlers(logger)
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
