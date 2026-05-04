"""Public validation API for forge_engine pipeline configs.

`validate_config` lets callers (the CLI, the platform) check whether a
config is well-formed without instantiating a full Masker or
DataGenerator (which has heavy side effects: opening connectors,
creating mappings dirs, etc.).
"""

import logging
from pathlib import Path
from typing import Any, Union

import yaml

from forge_engine.exceptions import ConfigError, PipelineValidationError


def validate_config(config: Union[str, Path, dict]) -> None:
    """Validate a forge_engine pipeline config.

    Accepts a YAML file path or an already-loaded config dict. Detects
    whether the config targets masking or generation by inspecting the
    top-level keys (`masking_rules` for masking, `tables` for generation)
    and runs the appropriate validator.

    Raises:
        ConfigError: if the config cannot be loaded or parsed.
        PipelineValidationError: if validation fails or the config type
            cannot be determined.
    """
    data = _load_config(config)
    validator_cls = _select_validator(data)
    _run_validator(validator_cls, data)


def _load_config(config: Union[str, Path, dict]) -> dict:
    if isinstance(config, dict):
        return config
    if not isinstance(config, (str, Path)):
        raise ConfigError(
            f"Config must be a path or dict, got {type(config).__name__}"
        )
    path = Path(config)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(
            f"Config root must be a YAML mapping, got {type(data).__name__}"
        )
    return data


def _select_validator(data: dict) -> Any:
    from forge_engine.internal.validator import (
        GeneratorConfigValidator,
        MaskerConfigValidator,
    )

    if "tables" in data:
        return GeneratorConfigValidator
    if "masking_rules" in data:
        return MaskerConfigValidator
    raise PipelineValidationError(
        "Cannot determine config type: expected 'masking_rules' (for mask) "
        "or 'tables' (for generate) at top level."
    )


def _run_validator(validator_cls: Any, data: dict) -> None:
    from forge_engine.internal.validator import ValidationError

    quiet_logger = logging.getLogger("forge_engine.validate")
    if not quiet_logger.handlers:
        quiet_logger.addHandler(logging.NullHandler())

    try:
        validator_cls(quiet_logger).validate(data)
    except ValidationError as e:
        raise PipelineValidationError(str(e)) from e
