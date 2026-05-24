"""Tests verifying that Masker and DataGenerator honor ExecutionContext.

When a caller (CLI or platform) passes an ExecutionContext with a
custom logger, the engine MUST use that logger instead of building
its own from the config's 'logging' section. This is the contract
the platform's StructuredLogger and the CLI's RichLogger depend on.
"""

import logging
from pathlib import Path

import pytest
import yaml

from decoy_engine import DataGenerator, ExecutionContext, Masker


class CapturingLogger:
    """Implements decoy_engine.context.Logger via duck typing."""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def debug(self, msg, *args, **kwargs):
        self.messages.append(("debug", str(msg)))

    def info(self, msg, *args, **kwargs):
        self.messages.append(("info", str(msg)))

    def warning(self, msg, *args, **kwargs):
        self.messages.append(("warning", str(msg)))

    def error(self, msg, *args, **kwargs):
        self.messages.append(("error", str(msg)))


@pytest.fixture
def mask_config(tmp_path: Path) -> Path:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("name,id\nAlice,1\nBob,2\n")
    cfg = {
        "global_settings": {"seed": 42},
        "input": {
            "type": "csv",
            "path": str(csv_path),
            "csv_options": {"delimiter": ",", "encoding": "utf-8"},
        },
        "output": {
            "type": "csv",
            "path": str(tmp_path / "out.csv"),
            "csv_options": {"delimiter": ",", "encoding": "utf-8"},
        },
        "masking_rules": [
            {"column": "id", "type": "passthrough"},
            {"column": "name", "type": "faker", "faker_type": "first_name"},
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg))
    return path


@pytest.fixture
def generate_config(tmp_path: Path) -> Path:
    cfg = {
        "generator_settings": {
            "seed": 42,
            "output_directory": str(tmp_path) + "/",
            "chunk_size": 10,
        },
        "tables": [
            {
                "name": "people",
                "row_count": 5,
                "columns": [
                    {"name": "id", "type": "sequence", "start": 1},
                    {"name": "name", "type": "faker", "faker_type": "name"},
                ],
            }
        ],
    }
    path = tmp_path / "gen.yaml"
    path.write_text(yaml.dump(cfg))
    return path


class TestMaskerContextInjection:
    def test_ctx_logger_is_used_when_provided(self, mask_config: Path):
        custom = CapturingLogger()
        masker = Masker(str(mask_config), ctx=ExecutionContext(logger=custom))
        assert masker.logger is custom

    def test_default_logger_used_when_ctx_omitted(self, mask_config: Path):
        masker = Masker(str(mask_config))
        # Default is a stdlib logging.Logger
        assert isinstance(masker.logger, logging.Logger)

    def test_default_logger_used_when_ctx_has_no_logger(self, mask_config: Path):
        masker = Masker(str(mask_config), ctx=ExecutionContext())
        assert isinstance(masker.logger, logging.Logger)

    def test_ctx_logger_receives_run_messages(self, mask_config: Path):
        custom = CapturingLogger()
        Masker(str(mask_config), ctx=ExecutionContext(logger=custom)).mask()
        # The engine should have sent at least one info message to our logger
        assert any(level == "info" for level, _ in custom.messages)
        assert not (mask_config.parent / "mappings").exists()


class TestDataGeneratorContextInjection:
    def test_ctx_logger_is_used_when_provided(self, generate_config: Path):
        custom = CapturingLogger()
        gen = DataGenerator(str(generate_config), ctx=ExecutionContext(logger=custom))
        assert gen.logger is custom

    def test_ctx_takes_precedence_over_legacy_logger_kwarg(self, generate_config: Path):
        legacy = logging.getLogger("legacy")
        ctx_logger = CapturingLogger()
        gen = DataGenerator(
            str(generate_config),
            logger=legacy,
            ctx=ExecutionContext(logger=ctx_logger),
        )
        assert gen.logger is ctx_logger

    def test_legacy_logger_kwarg_still_works(self, generate_config: Path):
        legacy = logging.getLogger("legacy")
        gen = DataGenerator(str(generate_config), logger=legacy)
        assert gen.logger is legacy

    def test_default_logger_used_when_neither_provided(self, generate_config: Path):
        gen = DataGenerator(str(generate_config))
        assert isinstance(gen.logger, logging.Logger)
