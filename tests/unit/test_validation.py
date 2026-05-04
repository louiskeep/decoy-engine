"""Tests for the public validate_config() function."""

from pathlib import Path

import pytest
import yaml

from decoy_engine import validate_config, ConfigError, PipelineValidationError


def _valid_mask_config(input_path: str = "in.csv", output_path: str = "out.csv") -> dict:
    return {
        "global_settings": {"seed": 42},
        "input": {
            "type": "csv",
            "path": input_path,
            "csv_options": {"delimiter": ",", "encoding": "utf-8"},
        },
        "output": {
            "type": "csv",
            "path": output_path,
            "csv_options": {"delimiter": ",", "encoding": "utf-8"},
        },
        "masking_rules": [
            {"column": "name", "type": "faker", "faker_type": "name"},
            {"column": "id", "type": "passthrough"},
        ],
    }


def _valid_generate_config() -> dict:
    return {
        "generator_settings": {"seed": 42, "output_directory": "data/generated/"},
        "tables": [
            {
                "name": "customers",
                "row_count": 100,
                "columns": [
                    {"name": "id", "type": "sequence", "start": 1},
                    {"name": "name", "type": "faker", "faker_type": "name"},
                ],
            }
        ],
    }


class TestValidConfigs:
    def test_valid_mask_dict_passes(self):
        validate_config(_valid_mask_config())

    def test_valid_generate_dict_passes(self):
        validate_config(_valid_generate_config())

    def test_valid_mask_yaml_file_passes(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(_valid_mask_config()))
        validate_config(path)
        validate_config(str(path))


class TestInvalidConfigs:
    def test_missing_input_raises(self):
        config = _valid_mask_config()
        del config["input"]
        with pytest.raises(PipelineValidationError):
            validate_config(config)

    def test_missing_masking_rules_and_tables_raises(self):
        with pytest.raises(PipelineValidationError, match="Cannot determine"):
            validate_config({"input": {}, "output": {}})

    def test_unknown_strategy_raises(self):
        config = _valid_mask_config()
        config["masking_rules"][0]["type"] = "not_a_real_strategy"
        with pytest.raises(PipelineValidationError):
            validate_config(config)


class TestLoadingErrors:
    def test_nonexistent_file_raises_config_error(self):
        with pytest.raises(ConfigError, match="not found"):
            validate_config("/nonexistent/path.yaml")

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path):
        path = tmp_path / "broken.yaml"
        path.write_text("not: valid: yaml: ::: garbage")
        with pytest.raises(ConfigError, match="parse YAML"):
            validate_config(path)

    def test_yaml_root_not_mapping_raises_config_error(self, tmp_path: Path):
        path = tmp_path / "list.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="mapping"):
            validate_config(path)

    def test_wrong_argument_type_raises_config_error(self):
        with pytest.raises(ConfigError, match="path or dict"):
            validate_config(42)  # type: ignore[arg-type]
