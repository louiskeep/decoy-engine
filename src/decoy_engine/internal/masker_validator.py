"""MaskerConfigValidator: validates legacy masker YAML configs.

V2.0-B split: extracted from internal/validator.py (2026-05-25 session 11).
Legacy masker path (masker/masker.py + IOHandler) is a V2.1 deletion candidate;
the validate-never-mutates contract applies to graph-mode validators only.
Callers that still reach decoy_engine.internal.validator will find a re-export shim.
"""

import os
from typing import Any

from decoy_engine.errors import ValidationError
from decoy_engine.internal.base import ConfigValidator


class MaskerConfigValidator(ConfigValidator):
    """
    Validates configuration for data masking operations.
    Ensures all required fields are present and constraints are met.
    """

    DEFAULT_GLOBAL_SETTINGS = {"seed": 42, "chunk_size": 100000, "large_file_threshold_gb": 1.0}

    DEFAULT_LOGGING = {
        "level": "info",
        "file": "logs/decoy_engine.log",
        "console": False,
        "verbose": False,
        "max_size_mb": 10,
        "backup_count": 5,
    }

    DEFAULT_CSV_OPTIONS = {"delimiter": ",", "encoding": "utf-8", "quoting": "minimal"}

    DEFAULT_FIXED_WIDTH_OPTIONS = {
        "encoding": "utf-8",
        "definition_delimiter": ",",
        "padding_char": " ",
        "padding_alignment": "auto",
    }

    def _validate_fixed_width_options(self, options, path):
        if "padding_char" in options:
            padding_char = options["padding_char"]
            if not isinstance(padding_char, str) or len(padding_char) != 1:
                self.logger.warning(
                    f"padding_char must be a single character. Got '{padding_char}', using default ' '"
                )
                options["padding_char"] = " "

        if "padding_alignment" in options:
            alignment = options["padding_alignment"]
            valid_alignments = ["auto", "left", "right"]
            if alignment not in valid_alignments:
                self.logger.warning(
                    f"Invalid padding_alignment: '{alignment}'. Must be one of {valid_alignments}. Using 'auto'."
                )
                options["padding_alignment"] = "auto"

    SUPPORTED_FILE_TYPES = ["csv", "fixed_width", "database"]

    SUPPORTED_MASKING_STRATEGIES = [
        "faker",
        "hash",
        "redact",
        "categorical",
        "shuffle",
        "passthrough",
        "date_shift",
        "formula",
        "reference",
        "truncate",
        "bucketize",
        "fpe",
    ]

    def validate(self, config: dict[str, Any]) -> None:
        try:
            self._validate_required_sections(config)
            self._validate_input_config(config["input"])
            self._validate_output_config(config["output"])
            if "masking_rules" in config:
                self._validate_masking_rules(config["masking_rules"])
            if "referential_integrity" in config:
                self._validate_referential_integrity(config["referential_integrity"])
            if "global_settings" not in config:
                config["global_settings"] = self.DEFAULT_GLOBAL_SETTINGS.copy()
            if "logging" not in config:
                config["logging"] = self.DEFAULT_LOGGING.copy()
        except ValidationError as e:
            self.logger.error(str(e))
            raise

    def _validate_required_sections(self, config: dict[str, Any]) -> None:
        required_sections = ["input", "output", "masking_rules"]
        for section in required_sections:
            if section not in config:
                raise ValidationError(f"Missing required section '{section}' in configuration")

    def _validate_input_config(self, input_config: dict[str, Any]) -> None:
        if "type" not in input_config:
            raise ValidationError("Missing required field 'type'", "input")

        input_type = input_config["type"]
        if input_type not in self.SUPPORTED_FILE_TYPES:
            raise ValidationError(
                f"Unsupported type: '{input_type}'. Supported types: {', '.join(self.SUPPORTED_FILE_TYPES)}",
                "input.type",
            )

        if input_type == "database":
            if "connector_dsn" not in input_config:
                raise ValidationError("Missing required field 'connector_dsn'", "input")
            if "table" not in input_config:
                raise ValidationError("Missing required field 'table'", "input")
            return

        if "path" not in input_config:
            raise ValidationError("Missing required field 'path'", "input")

        if input_type == "csv":
            if "csv_options" not in input_config:
                input_config["csv_options"] = self.DEFAULT_CSV_OPTIONS.copy()
            if input_config["csv_options"].get("header") is False:
                raise ValidationError(
                    "Input files must have headers. Headerless files are not supported.",
                    "input.csv_options.header",
                )
            input_config["csv_options"]["header"] = True

        elif input_type == "fixed_width":
            if "definition_path" not in input_config:
                raise ValidationError("Missing required field 'definition_path'", "input")
            def_path = input_config["definition_path"]
            if not os.path.exists(def_path):
                self.logger.warning(
                    f"Definition file at '{def_path}' not found. Ensure it exists before processing."
                )
            if "fixed_width_options" not in input_config:
                input_config["fixed_width_options"] = self.DEFAULT_FIXED_WIDTH_OPTIONS.copy()

    def _validate_output_config(self, output_config: dict[str, Any]) -> None:
        if "type" not in output_config:
            raise ValidationError("Missing required field 'type'", "output")

        output_type = output_config["type"]
        if output_type not in self.SUPPORTED_FILE_TYPES:
            raise ValidationError(
                f"Unsupported type: '{output_type}'. Supported types: {', '.join(self.SUPPORTED_FILE_TYPES)}",
                "output.type",
            )

        if output_type == "database":
            if "connector_dsn" not in output_config:
                raise ValidationError("Missing required field 'connector_dsn'", "output")
            if "table" not in output_config:
                raise ValidationError("Missing required field 'table'", "output")
            return

        if "path" not in output_config:
            raise ValidationError("Missing required field 'path'", "output")

        output_path = output_config["path"]
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            self.logger.info(
                f"Output directory '{output_dir}' doesn't exist. It will be created during processing."
            )

        if output_type == "csv":
            if "csv_options" not in output_config:
                output_config["csv_options"] = self.DEFAULT_CSV_OPTIONS.copy()

        elif output_type == "fixed_width":
            if "fixed_width_options" not in output_config:
                output_config["fixed_width_options"] = self.DEFAULT_FIXED_WIDTH_OPTIONS.copy()

    def _validate_masking_rules(self, masking_rules: list[dict[str, Any]]) -> None:
        if not masking_rules:
            raise ValidationError("No masking rules defined", "masking_rules")

        for i, rule in enumerate(masking_rules):
            rule_path = f"masking_rules[{i}]"

            if "column" not in rule:
                raise ValidationError("Missing required field 'column'", rule_path)

            if "type" not in rule:
                raise ValidationError("Missing required field 'type'", rule_path)

            strategy_type = rule["type"]
            if strategy_type not in self.SUPPORTED_MASKING_STRATEGIES:
                raise ValidationError(
                    f"Unsupported masking strategy: '{strategy_type}'. "
                    + f"Supported strategies: {', '.join(self.SUPPORTED_MASKING_STRATEGIES)}",
                    f"{rule_path}.type",
                )

            if strategy_type == "faker":
                if "faker_type" not in rule:
                    rule["faker_type"] = "word"

            elif strategy_type == "redact":
                if "redact_with" not in rule:
                    rule["redact_with"] = "REDACTED"

            elif strategy_type == "categorical":
                categories = rule.get("categories")
                if not isinstance(categories, list) or not categories:
                    raise ValidationError(
                        "Missing required non-empty field 'categories' for categorical strategy",
                        f"{rule_path}.categories",
                    )
                weights = rule.get("weights")
                if weights is not None:
                    if not isinstance(weights, list) or len(weights) != len(categories):
                        raise ValidationError(
                            "'weights' must be a list with the same length as 'categories'",
                            f"{rule_path}.weights",
                        )
                    if any(isinstance(w, bool) for w in weights):
                        raise ValidationError(
                            "'weights' must contain only numeric values",
                            f"{rule_path}.weights",
                        )
                    try:
                        numeric_weights = [float(w) for w in weights]
                    except (TypeError, ValueError):
                        raise ValidationError(
                            "'weights' must contain only numeric values",
                            f"{rule_path}.weights",
                        )
                    if any(w < 0 for w in numeric_weights) or sum(numeric_weights) <= 0:
                        raise ValidationError(
                            "'weights' must be non-negative with at least one positive value",
                            f"{rule_path}.weights",
                        )
                null_probability = rule.get("null_probability")
                if null_probability is not None:
                    if isinstance(null_probability, bool):
                        raise ValidationError(
                            "'null_probability' must be a number between 0 and 1",
                            f"{rule_path}.null_probability",
                        )
                    try:
                        p = float(null_probability)
                    except (TypeError, ValueError):
                        raise ValidationError(
                            "'null_probability' must be a number between 0 and 1",
                            f"{rule_path}.null_probability",
                        )
                    if p < 0 or p > 1:
                        raise ValidationError(
                            "'null_probability' must be between 0 and 1",
                            f"{rule_path}.null_probability",
                        )

            elif strategy_type == "formula":
                if "formula" not in rule:
                    raise ValidationError("Missing required field 'formula'", rule_path)

            elif strategy_type == "reference":
                if "reference" not in rule or not rule["reference"]:
                    raise ValidationError(
                        "Missing required field 'reference' (dataset path) for reference strategy",
                        f"{rule_path}.reference",
                    )

            if "conditions" in rule:
                _VALID_COND_OPS = {
                    "eq",
                    "ne",
                    "gt",
                    "gte",
                    "lt",
                    "lte",
                    "in",
                    "not_in",
                    "contains",
                    "not_contains",
                    "is_null",
                    "is_not_null",
                }
                conditions = rule["conditions"]
                if not isinstance(conditions, list):
                    raise ValidationError("'conditions' must be a list", f"{rule_path}.conditions")
                cond_logic = rule.get("condition_logic", "AND")
                if cond_logic not in ("AND", "OR"):
                    raise ValidationError(
                        "'condition_logic' must be 'AND' or 'OR'", f"{rule_path}.condition_logic"
                    )
                for j, cond in enumerate(conditions):
                    cpath = f"{rule_path}.conditions[{j}]"
                    if "column" not in cond:
                        raise ValidationError("Missing required field 'column'", cpath)
                    if "operator" not in cond:
                        raise ValidationError("Missing required field 'operator'", cpath)
                    if cond["operator"] not in _VALID_COND_OPS:
                        raise ValidationError(
                            f"Invalid operator '{cond['operator']}'. Valid: {sorted(_VALID_COND_OPS)}",
                            f"{cpath}.operator",
                        )
                    if cond["operator"] not in ("is_null", "is_not_null") and "value" not in cond:
                        raise ValidationError("Missing required field 'value'", cpath)

    def _validate_referential_integrity(self, relationships: list[dict[str, Any]]) -> None:
        if not relationships:
            return

        for i, relationship in enumerate(relationships):
            rel_path = f"referential_integrity[{i}]"

            if "name" not in relationship:
                raise ValidationError("Missing required field 'name'", rel_path)

            if "columns" not in relationship:
                raise ValidationError("Missing required field 'columns'", rel_path)

            columns = relationship["columns"]
            if not isinstance(columns, list) or not columns:
                raise ValidationError("'columns' must be a non-empty list", f"{rel_path}.columns")

            for j, column in enumerate(columns):
                if not isinstance(column, str) or "." not in column:
                    raise ValidationError(
                        f"Invalid column format: '{column}'. Should be 'table.column'",
                        f"{rel_path}.columns[{j}]",
                    )
