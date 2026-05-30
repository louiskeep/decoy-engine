"""Tests for the public validate_config() function.

S9: V1 mask + V1 generate config shapes are no longer validated by
``validate_config``. The V1 validators (``MaskerConfigValidator`` +
``GeneratorConfigValidator``) were deleted with the V1 Masker / DataGenerator
paths. ``validate_config`` now only accepts ``version: 1`` PipelineConfig
shapes (the v2 substrate); V1 shapes raise PipelineValidationError pointing
at the v2 path. Cells that previously asserted V1 dicts validated successfully
were removed; the loading-error cells (file-not-found, malformed YAML, etc.)
remain because they're shape-agnostic.
"""

from pathlib import Path

import pytest

from decoy_engine import ConfigError, PipelineValidationError, validate_config


def _valid_v2_pipeline_config() -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {
            "customers": {"type": "file", "format": "csv", "path": "uploads/customers.csv"},
        },
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "person_email",
                        "namespace": "cust_ns",
                        "deterministic": True,
                    }
                ],
            }
        ],
        "targets": {
            "customers": {"type": "file", "format": "csv", "path": "out/customers.csv"},
        },
        "namespaces": {"cust_ns": {"declared_by": ["customers.email"]}},
    }


class TestValidConfigs:
    def test_valid_v2_pipeline_dict_passes(self):
        validate_config(_valid_v2_pipeline_config())


class TestInvalidConfigs:
    def test_v1_mask_shape_rejected_with_typed_message(self):
        v1_mask = {
            "global_settings": {"seed": 42},
            "input": {"type": "csv", "path": "in.csv"},
            "output": {"type": "csv", "path": "out.csv"},
            "masking_rules": [{"column": "name", "type": "passthrough"}],
        }
        with pytest.raises(PipelineValidationError, match="no longer validated"):
            validate_config(v1_mask)

    def test_v1_generate_shape_rejected_with_typed_message(self):
        v1_generate = {
            "generator_settings": {"seed": 42, "output_directory": "out/"},
            "tables": [{"name": "customers", "row_count": 100, "columns": []}],
        }
        with pytest.raises(PipelineValidationError, match="no longer validated"):
            validate_config(v1_generate)


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
