"""Public validation API for decoy_engine pipeline configs.

`validate_config` lets callers (the CLI, the platform) check whether a
config is well-formed without instantiating a full Masker or
DataGenerator (which has heavy side effects: opening connectors,
touching output paths, etc.).
"""

from pathlib import Path
from typing import Any

import yaml

from decoy_engine.errors import ConfigError, PipelineValidationError


def validate_config(config: str | Path | dict) -> None:
    """Validate a decoy_engine pipeline config.

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
    # S9: _select_validator now performs PipelineConfig validation inline for
    # v2 configs (returns None) and rejects v1 shapes; the historical
    # _run_validator(validator_cls, data) call path was V1-only and is gone.
    _select_validator(data)


def _load_config(config: str | Path | dict) -> dict:
    if isinstance(config, dict):
        return config
    if not isinstance(config, (str, Path)):
        raise ConfigError(f"Config must be a path or dict, got {type(config).__name__}")
    path = Path(config)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a YAML mapping, got {type(data).__name__}")
    return data


def _select_validator(data: dict) -> Any:
    # S9: V1 ``masking_rules`` (mask) + V1 ``tables: dict`` (generate)
    # config shapes are no longer validated by ``validate_config``. The V1
    # validators (``MaskerConfigValidator`` + ``GeneratorConfigValidator``)
    # were deleted with the V1 ``Masker`` + ``DataGenerator`` paths. V2
    # ``PipelineConfig`` configs (``version: 1``) flow through the engine
    # ``PipelineConfig.model_validate`` choke-point instead -- the platform
    # spine at ``api/jobs/v2_runner.py`` calls it directly. Callers that
    # used to reach validate_config for a V1 shape now get a typed reject
    # pointing at the V2 path.
    if data.get("version") == 1:
        from decoy_engine.config import PipelineConfig

        try:
            PipelineConfig.model_validate(data)
        except Exception as exc:  # PipelineConfigError is the typed shape
            raise PipelineValidationError(str(exc)) from exc
        return None
    raise PipelineValidationError(
        "v1 mask + v1 generate config shapes are no longer validated by "
        "validate_config (S9 removal). Use a `version: 1` PipelineConfig "
        "(see decoy_engine.PipelineConfig.model_validate) for v2 mask + "
        "v2 generate configs."
    )
