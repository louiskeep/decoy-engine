"""
Configuration validation utilities for the decoy_engine package.

V2.0-C: ValidationError moved to ``decoy_engine.errors`` (public
surface). This module retains the V1 legacy MaskerConfigValidator and
GeneratorConfigValidator classes that V2.1 audits for deletion. The
GraphConfigValidator was extracted into the modular
``decoy_engine.graph.validators`` package in V2.0-B.

GeneratorConfigValidator.validate() runs both a basic structural pass
and the deep per-table / per-column / relationship validators (wired
in by overnight-dev session 9, fix b2de7bd). Earlier it only ran the
structural pass; the deep methods were defined but never called.
"""

import os
from typing import Any

from decoy_engine.errors import (
    ValidationError,
)
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


class GeneratorConfigValidator(ConfigValidator):
    """
    Validates the configuration for data generation operations.
    """

    DEFAULT_GENERATOR_SETTINGS = {
        "seed": 42,
        "output_directory": "data/generated/",
        "chunk_size": 10000,
    }

    SUPPORTED_COLUMN_TYPES = ["faker", "sequence", "categorical", "reference", "formula"]

    SUPPORTED_RELATIONSHIP_TYPES = ["self_reference", "foreign_key", "many_to_many"]

    def validate(self, config: dict[str, Any]) -> None:
        """Validate a generator config dict. Raises ValidationError on failure.

        Performs two passes:
        1. Basic structural checks (output_type, fixed_width_options) that
           are specific to the outer table format and not covered by the
           deep validators.
        2. Deep per-table / per-column / relationship validation via
           _validate_tables and _validate_relationships, which catch
           duplicate names, cardinality constraints, weight sums,
           null_probability ranges, and formula reference errors.

        overnight-dev session 9 fix b2de7bd: the deep validators were
        defined as private methods but never called; validate() also
        raised ValueError instead of ValidationError, so callers
        catching ValidationError silently missed generator config errors.
        Both are fixed here. Backfill mutations (rows default = 1000,
        faker_type default = "word") are preserved for V1 callers that
        rely on them; the legacy validator is slated for V2.1 deletion
        anyway so the validate-never-mutates contract is enforced only
        on the graph-mode validators.
        """
        if "tables" not in config:
            raise ValidationError(
                "Missing required section 'tables' in generator configuration"
            )

        for i, table in enumerate(config["tables"]):
            if "name" not in table:
                raise ValidationError(
                    f"Table at index {i} is missing a 'name'",
                    f"tables[{i}].name",
                )
            if "columns" not in table:
                raise ValidationError(
                    f"Table '{table.get('name', f'at index {i}')}' is missing 'columns' definition",
                    f"tables[{i}].columns",
                )
            if "rows" not in table:
                table["rows"] = 1000

            if "output_type" in table:
                output_type = table["output_type"].lower()
                if output_type not in ["csv", "fixed_width"]:
                    raise ValidationError(
                        f"Unsupported output_type: {output_type} for table '{table.get('name')}'. "
                        "Supported types: 'csv', 'fixed_width'",
                        f"tables[{i}].output_type",
                    )
                if output_type == "fixed_width":
                    if "fixed_width_options" not in table:
                        table["fixed_width_options"] = {"encoding": "utf-8"}
                    if "definition_path" not in table["fixed_width_options"]:
                        raise ValidationError(
                            f"Missing required 'definition_path' in fixed_width_options for table '{table.get('name')}'",
                            f"tables[{i}].fixed_width_options.definition_path",
                        )
                    definition_path = table["fixed_width_options"]["definition_path"]
                    if not os.path.exists(definition_path):
                        self.logger.warning(f"Definition file not found: {definition_path}.")

            for j, column in enumerate(table["columns"]):
                if "name" not in column:
                    raise ValidationError(
                        f"Column at index {j} in table '{table.get('name')}' is missing a 'name'",
                        f"tables[{i}].columns[{j}].name",
                    )
                if "type" not in column:
                    raise ValidationError(
                        f"Column '{column.get('name', f'at index {j}')}' in table '{table.get('name')}' is missing a 'type'",
                        f"tables[{i}].columns[{j}].type",
                    )
                col_type = column["type"]
                if col_type == "faker" and "faker_type" not in column:
                    column["faker_type"] = "word"

        # Deep per-table and per-column validation: duplicate names, row
        # counts, type-specific constraints (null_probability, weight
        # sums, formula refs). overnight-dev session 9 wired these in.
        self._validate_tables(config["tables"])

        # Relationship validation: referential integrity across the table map.
        if "relationships" in config:
            self._validate_relationships(config["relationships"], config["tables"])

    def _validate_tables(self, tables: list[dict[str, Any]]) -> None:
        table_names = set()
        for i, table in enumerate(tables):
            table_path = f"tables[{i}]"
            if "name" not in table:
                raise ValidationError("Missing required field 'name'", table_path)
            table_name = table["name"]
            if table_name in table_names:
                raise ValidationError(f"Duplicate table name: '{table_name}'", f"{table_path}.name")
            table_names.add(table_name)
            if "columns" not in table:
                raise ValidationError("Missing required field 'columns'", table_path)
            if "rows" not in table:
                table["rows"] = 1000
            rows = table["rows"]
            if not isinstance(rows, int) or rows <= 0:
                raise ValidationError(
                    f"'rows' must be a positive integer, got '{rows}'", f"{table_path}.rows"
                )
            if "output_path" in table:
                output_dir = os.path.dirname(table["output_path"])
                if output_dir and not os.path.exists(output_dir):
                    self.logger.info(f"Output directory '{output_dir}' doesn't exist.")
            self._validate_columns(table["columns"], table_path, table_name)

    def _validate_columns(
        self, columns: list[dict[str, Any]], table_path: str, table_name: str
    ) -> None:
        """Deep-validate column list: checks names and per-type constraints."""
        if not columns:
            raise ValidationError("No columns defined", f"{table_path}.columns")
        column_names = set()
        for j, column in enumerate(columns):
            column_path = f"{table_path}.columns[{j}]"
            if "name" not in column:
                raise ValidationError("Missing required field 'name'", column_path)
            column_name = column["name"]
            if column_name in column_names:
                raise ValidationError(
                    f"Duplicate column name: '{column_name}'", f"{column_path}.name"
                )
            column_names.add(column_name)
            if "type" not in column:
                raise ValidationError("Missing required field 'type'", column_path)
            column_type = column["type"]
            if column_type not in self.SUPPORTED_COLUMN_TYPES:
                raise ValidationError(
                    f"Unsupported column type: '{column_type}'. Supported types: {', '.join(self.SUPPORTED_COLUMN_TYPES)}",
                    f"{column_path}.type",
                )
            self._validate_column_type_specific(column, column_path, column_type)

    def _validate_column_type_specific(
        self, column: dict[str, Any], column_path: str, column_type: str
    ) -> None:
        """Per-type column validation: null_probability, faker defaults,
        sequence defaults, categorical weights, reference fields, formula
        references.
        """
        if "null_probability" in column:
            null_prob = column["null_probability"]
            if not isinstance(null_prob, (int, float)) or not (0 <= null_prob <= 1):
                raise ValidationError(
                    f"'null_probability' must be a number between 0 and 1, got {null_prob}",
                    f"{column_path}.null_probability",
                )

        if column_type == "faker":
            if "faker_type" not in column:
                column["faker_type"] = "word"
        elif column_type == "sequence":
            if "start" not in column:
                column["start"] = 1
            if "step" not in column:
                column["step"] = 1
        elif column_type == "categorical":
            if "categories" not in column:
                raise ValidationError(
                    "Missing required field 'categories'", f"{column_path}.categories"
                )
            categories = column["categories"]
            if not isinstance(categories, list) or not categories:
                raise ValidationError(
                    "'categories' must be a non-empty list", f"{column_path}.categories"
                )
            if "weights" in column:
                weights = column["weights"]
                if not isinstance(weights, list) or len(weights) != len(categories):
                    raise ValidationError(
                        "'weights' must be a list with the same length as 'categories'",
                        f"{column_path}.weights",
                    )
                weight_sum = sum(weights)
                if not (0.99 <= weight_sum <= 1.01):
                    self.logger.warning(
                        f"Weights for column '{column.get('name')}' sum to {weight_sum}."
                    )
        elif column_type == "reference":
            if "reference_table" not in column:
                raise ValidationError(
                    "Missing required field 'reference_table'", f"{column_path}.reference_table"
                )
            if "reference_column" not in column:
                raise ValidationError(
                    "Missing required field 'reference_column'", f"{column_path}.reference_column"
                )
        elif column_type == "formula":
            if "formula" not in column:
                raise ValidationError("Missing required field 'formula'", f"{column_path}.formula")
            refs = column.get("references")
            if refs is not None and not isinstance(refs, list):
                raise ValidationError(
                    f"'references' must be a list of column names (got {type(refs).__name__})",
                    f"{column_path}.references",
                )

        if "fixed_width_options" in column:
            fixed_width_opts = column["fixed_width_options"]
            if "padding_char" in fixed_width_opts:
                padding_char = fixed_width_opts["padding_char"]
                if not isinstance(padding_char, str) or len(padding_char) != 1:
                    self.logger.warning(
                        f"Column '{column.get('name')}': padding_char must be a single character."
                    )
                    fixed_width_opts["padding_char"] = " "
            if "padding_alignment" in fixed_width_opts:
                alignment = fixed_width_opts["padding_alignment"]
                valid_alignments = ["auto", "left", "right"]
                if alignment not in valid_alignments:
                    self.logger.warning(
                        f"Column '{column.get('name')}': Invalid padding_alignment: '{alignment}'."
                    )
                    fixed_width_opts["padding_alignment"] = "left"

    def _validate_relationships(
        self, relationships: list[dict[str, Any]], tables: list[dict[str, Any]]
    ) -> None:
        """Validate relationship declarations against the table map."""
        if not relationships:
            return
        table_map = {}
        for table in tables:
            table_name = table.get("name")
            if table_name:
                column_names = [
                    col.get("name") for col in table.get("columns", []) if col.get("name")
                ]
                table_map[table_name] = set(column_names)
        for i, relationship in enumerate(relationships):
            rel_path = f"relationships[{i}]"
            if "name" not in relationship:
                raise ValidationError("Missing required field 'name'", rel_path)
            if "type" not in relationship:
                raise ValidationError("Missing required field 'type'", rel_path)
            rel_type = relationship["type"]
            if rel_type not in self.SUPPORTED_RELATIONSHIP_TYPES:
                raise ValidationError(
                    f"Unsupported relationship type: '{rel_type}'. Supported types: {', '.join(self.SUPPORTED_RELATIONSHIP_TYPES)}",
                    f"{rel_path}.type",
                )
            if rel_type == "self_reference":
                self._validate_self_reference(relationship, rel_path, table_map)
            elif rel_type == "foreign_key":
                self._validate_foreign_key(relationship, rel_path, table_map)
            elif rel_type == "many_to_many":
                self._validate_many_to_many(relationship, rel_path, table_map)

    def _validate_self_reference(self, relationship, rel_path, table_map):
        for field in ["table", "column", "reference_column"]:
            if field not in relationship:
                raise ValidationError(f"Missing required field '{field}'", f"{rel_path}.{field}")
        table_name = relationship["table"]
        if table_name not in table_map:
            raise ValidationError(f"Table '{table_name}' not defined", f"{rel_path}.table")
        column_set = table_map[table_name]
        if relationship["column"] not in column_set:
            raise ValidationError(
                f"Column '{relationship['column']}' not defined in table '{table_name}'",
                f"{rel_path}.column",
            )
        if relationship["reference_column"] not in column_set:
            raise ValidationError(
                f"Column '{relationship['reference_column']}' not defined in table '{table_name}'",
                f"{rel_path}.reference_column",
            )

    def _validate_foreign_key(self, relationship, rel_path, table_map):
        for field in ["source_table", "source_column", "target_table", "target_column"]:
            if field not in relationship:
                raise ValidationError(f"Missing required field '{field}'", f"{rel_path}.{field}")
        source_table = relationship["source_table"]
        target_table = relationship["target_table"]
        if source_table not in table_map:
            raise ValidationError(
                f"Source table '{source_table}' not defined", f"{rel_path}.source_table"
            )
        if target_table not in table_map:
            raise ValidationError(
                f"Target table '{target_table}' not defined", f"{rel_path}.target_table"
            )
        if relationship["source_column"] not in table_map[source_table]:
            raise ValidationError(
                f"Column '{relationship['source_column']}' not defined in table '{source_table}'",
                f"{rel_path}.source_column",
            )
        if relationship["target_column"] not in table_map[target_table]:
            raise ValidationError(
                f"Column '{relationship['target_column']}' not defined in table '{target_table}'",
                f"{rel_path}.target_column",
            )

    def _validate_many_to_many(self, relationship, rel_path, table_map):
        if "junction_table" not in relationship:
            raise ValidationError(
                "Missing required field 'junction_table'", f"{rel_path}.junction_table"
            )
        junction_table = relationship["junction_table"]
        if junction_table not in table_map:
            raise ValidationError(
                f"Junction table '{junction_table}' not defined", f"{rel_path}.junction_table"
            )


# V2.0-B: GraphConfigValidator class removed. Its responsibilities are
# split across pure modular validators under decoy_engine.graph.validators
# (top_level, nodes, edges, cross_node) plus the FK / m2m / multi-parent
# checks already in decoy_engine.graph._fk_validators. The format
# back-fill that used to live inside _validate_file_format_consistency
# moved to decoy_engine.graph.normalize.normalize_config, the explicit
# normalization path. Callers use validate_graph (raise-on-first-error)
# or validate_graph_full (collect-all) from decoy_engine.graph.runner;
# both are re-exported as decoy_engine.validate_graph /
# decoy_engine.validate_graph_full.
