# decoy_engine/core/validator.py
"""
Configuration validation utilities for the decoy_engine package.
"""

from typing import Dict, Any, List, Set, Optional, Union
from pathlib import Path
import os
from decoy_engine.internal.base import ConfigValidator


class ValidationError(Exception):
    """
    Custom exception for configuration validation errors.
    Provides more context for troubleshooting configuration issues.

    R2.1: carries an optional stable `code` so callers can map the
    failure to UI inspector fields without parsing the message string.
    Codes live in `decoy_engine.validation_result.CODES`. Older
    raises with no code are tolerated -- they surface as
    `CODES.UNTAGGED` when converted to a `ValidationResult`.
    """

    def __init__(
        self,
        message: str,
        path: Optional[str] = None,
        code: Optional[str] = None,
    ):
        self.path = path
        self.code = code
        if path:
            full_message = f"Validation error at '{path}': {message}"
        else:
            full_message = f"Validation error: {message}"
        super().__init__(full_message)
        self._raw_message = message

    @property
    def raw_message(self) -> str:
        """The original message without the `Validation error at '<path>':` wrapper."""
        return self._raw_message


class MaskerConfigValidator(ConfigValidator):
    """
    Validates configuration for data masking operations.
    Ensures all required fields are present and constraints are met.
    """
    
    DEFAULT_GLOBAL_SETTINGS = {
        'seed': 42,
        'chunk_size': 100000,
        'large_file_threshold_gb': 1.0
    }
    
    DEFAULT_LOGGING = {
        'level': 'info',
        'file': 'logs/decoy_engine.log',
        'console': False,
        'verbose': False,
        'max_size_mb': 10,
        'backup_count': 5
    }
    
    DEFAULT_CSV_OPTIONS = {
        'delimiter': ',',
        'encoding': 'utf-8',
        'quoting': 'minimal'
    }
    
    DEFAULT_FIXED_WIDTH_OPTIONS = {
        'encoding': 'utf-8',
        'definition_delimiter': ',',
        'padding_char': ' ',
        'padding_alignment': 'auto'
    }

    def _validate_fixed_width_options(self, options, path):
        if 'padding_char' in options:
            padding_char = options['padding_char']
            if not isinstance(padding_char, str) or len(padding_char) != 1:
                self.logger.warning(f"padding_char must be a single character. Got '{padding_char}', using default ' '")
                options['padding_char'] = ' '
        
        if 'padding_alignment' in options:
            alignment = options['padding_alignment']
            valid_alignments = ['auto', 'left', 'right']
            if alignment not in valid_alignments:
                self.logger.warning(f"Invalid padding_alignment: '{alignment}'. Must be one of {valid_alignments}. Using 'auto'.")
                options['padding_alignment'] = 'auto'
    
    SUPPORTED_FILE_TYPES = ['csv', 'fixed_width', 'database']
    
    SUPPORTED_MASKING_STRATEGIES = [
        'faker', 'hash', 'redact', 'map', 'shuffle', 'passthrough', 'date_shift', 'formula',
        'reference', 'truncate', 'bucketize', 'fpe',
    ]
    
    def validate(self, config: Dict[str, Any]) -> None:
        try:
            self._validate_required_sections(config)
            self._validate_input_config(config['input'])
            self._validate_output_config(config['output'])
            if 'masking_rules' in config:
                self._validate_masking_rules(config['masking_rules'])
            if 'referential_integrity' in config:
                self._validate_referential_integrity(config['referential_integrity'])
            if 'global_settings' not in config:
                config['global_settings'] = self.DEFAULT_GLOBAL_SETTINGS.copy()
            if 'logging' not in config:
                config['logging'] = self.DEFAULT_LOGGING.copy()
        except ValidationError as e:
            self.logger.error(str(e))
            raise
    
    def _validate_required_sections(self, config: Dict[str, Any]) -> None:
        required_sections = ['input', 'output', 'masking_rules']
        for section in required_sections:
            if section not in config:
                raise ValidationError(f"Missing required section '{section}' in configuration")
    
    def _validate_input_config(self, input_config: Dict[str, Any]) -> None:
        if 'type' not in input_config:
            raise ValidationError("Missing required field 'type'", "input")
        
        input_type = input_config['type']
        if input_type not in self.SUPPORTED_FILE_TYPES:
            raise ValidationError(
                f"Unsupported type: '{input_type}'. Supported types: {', '.join(self.SUPPORTED_FILE_TYPES)}",
                "input.type"
            )

        if input_type == 'database':
            if 'connector_dsn' not in input_config:
                raise ValidationError("Missing required field 'connector_dsn'", "input")
            if 'table' not in input_config:
                raise ValidationError("Missing required field 'table'", "input")
            return

        if 'path' not in input_config:
            raise ValidationError("Missing required field 'path'", "input")

        if input_type == 'csv':
            if 'csv_options' not in input_config:
                input_config['csv_options'] = self.DEFAULT_CSV_OPTIONS.copy()
            if input_config['csv_options'].get('header') is False:
                raise ValidationError(
                    "Input files must have headers. Headerless files are not supported.",
                    "input.csv_options.header"
                )
            input_config['csv_options']['header'] = True
                
        elif input_type == 'fixed_width':
            if 'definition_path' not in input_config:
                raise ValidationError("Missing required field 'definition_path'", "input")
            def_path = input_config['definition_path']
            if not os.path.exists(def_path):
                self.logger.warning(f"Definition file at '{def_path}' not found. Ensure it exists before processing.")
            if 'fixed_width_options' not in input_config:
                input_config['fixed_width_options'] = self.DEFAULT_FIXED_WIDTH_OPTIONS.copy()
    
    def _validate_output_config(self, output_config: Dict[str, Any]) -> None:
        if 'type' not in output_config:
            raise ValidationError("Missing required field 'type'", "output")
            
        output_type = output_config['type']
        if output_type not in self.SUPPORTED_FILE_TYPES:
            raise ValidationError(
                f"Unsupported type: '{output_type}'. Supported types: {', '.join(self.SUPPORTED_FILE_TYPES)}",
                "output.type"
            )

        if output_type == 'database':
            if 'connector_dsn' not in output_config:
                raise ValidationError("Missing required field 'connector_dsn'", "output")
            if 'table' not in output_config:
                raise ValidationError("Missing required field 'table'", "output")
            return

        if 'path' not in output_config:
            raise ValidationError("Missing required field 'path'", "output")
        
        output_path = output_config['path']
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            self.logger.info(f"Output directory '{output_dir}' doesn't exist. It will be created during processing.")
            
        if output_type == 'csv':
            if 'csv_options' not in output_config:
                output_config['csv_options'] = self.DEFAULT_CSV_OPTIONS.copy()
                
        elif output_type == 'fixed_width':
            if 'fixed_width_options' not in output_config:
                output_config['fixed_width_options'] = self.DEFAULT_FIXED_WIDTH_OPTIONS.copy()
    
    def _validate_masking_rules(self, masking_rules: List[Dict[str, Any]]) -> None:
        if not masking_rules:
            raise ValidationError("No masking rules defined", "masking_rules")
            
        for i, rule in enumerate(masking_rules):
            rule_path = f"masking_rules[{i}]"
            
            if 'column' not in rule:
                raise ValidationError("Missing required field 'column'", rule_path)
                
            if 'type' not in rule:
                raise ValidationError("Missing required field 'type'", rule_path)
                
            strategy_type = rule['type']
            if strategy_type not in self.SUPPORTED_MASKING_STRATEGIES:
                raise ValidationError(
                    f"Unsupported masking strategy: '{strategy_type}'. " +
                    f"Supported strategies: {', '.join(self.SUPPORTED_MASKING_STRATEGIES)}",
                    f"{rule_path}.type"
                )
                
            if strategy_type == 'faker':
                if 'faker_type' not in rule:
                    rule['faker_type'] = 'word'
                    
            elif strategy_type == 'redact':
                if 'redact_with' not in rule:
                    rule['redact_with'] = 'REDACTED'
                    
            elif strategy_type == 'map':
                if 'map_type' not in rule:
                    rule['map_type'] = 'faker'
                map_type = rule.get('map_type')
                if map_type == 'faker' and 'faker_type' not in rule:
                    rule['faker_type'] = 'word'
                if map_type == 'fixed' and 'fixed_prefix' not in rule:
                    rule['fixed_prefix'] = 'MASKED'

            elif strategy_type == 'formula':
                if 'formula' not in rule:
                    raise ValidationError("Missing required field 'formula'", rule_path)

            elif strategy_type == 'reference':
                if 'reference' not in rule or not rule['reference']:
                    raise ValidationError(
                        "Missing required field 'reference' (dataset path) for reference strategy",
                        f"{rule_path}.reference"
                    )

            if 'conditions' in rule:
                _VALID_COND_OPS = {
                    'eq', 'ne', 'gt', 'gte', 'lt', 'lte',
                    'in', 'not_in', 'contains', 'not_contains',
                    'is_null', 'is_not_null',
                }
                conditions = rule['conditions']
                if not isinstance(conditions, list):
                    raise ValidationError("'conditions' must be a list", f"{rule_path}.conditions")
                cond_logic = rule.get('condition_logic', 'AND')
                if cond_logic not in ('AND', 'OR'):
                    raise ValidationError("'condition_logic' must be 'AND' or 'OR'", f"{rule_path}.condition_logic")
                for j, cond in enumerate(conditions):
                    cpath = f"{rule_path}.conditions[{j}]"
                    if 'column' not in cond:
                        raise ValidationError("Missing required field 'column'", cpath)
                    if 'operator' not in cond:
                        raise ValidationError("Missing required field 'operator'", cpath)
                    if cond['operator'] not in _VALID_COND_OPS:
                        raise ValidationError(
                            f"Invalid operator '{cond['operator']}'. Valid: {sorted(_VALID_COND_OPS)}",
                            f"{cpath}.operator"
                        )
                    if cond['operator'] not in ('is_null', 'is_not_null') and 'value' not in cond:
                        raise ValidationError("Missing required field 'value'", cpath)

    def _validate_referential_integrity(self, relationships: List[Dict[str, Any]]) -> None:
        if not relationships:
            return
            
        for i, relationship in enumerate(relationships):
            rel_path = f"referential_integrity[{i}]"
            
            if 'name' not in relationship:
                raise ValidationError("Missing required field 'name'", rel_path)
                
            if 'columns' not in relationship:
                raise ValidationError("Missing required field 'columns'", rel_path)
                
            columns = relationship['columns']
            if not isinstance(columns, list) or not columns:
                raise ValidationError("'columns' must be a non-empty list", f"{rel_path}.columns")
                
            for j, column in enumerate(columns):
                if not isinstance(column, str) or '.' not in column:
                    raise ValidationError(
                        f"Invalid column format: '{column}'. Should be 'table.column'",
                        f"{rel_path}.columns[{j}]"
                    )


class GeneratorConfigValidator(ConfigValidator):
    """
    Validates the configuration for data generation operations.
    """
    
    DEFAULT_GENERATOR_SETTINGS = {
        'seed': 42,
        'output_directory': 'data/generated/',
        'chunk_size': 10000
    }
    
    SUPPORTED_COLUMN_TYPES = [
        'faker', 'sequence', 'categorical', 'reference', 'formula'
    ]

    SUPPORTED_RELATIONSHIP_TYPES = [
        'self_reference', 'foreign_key', 'many_to_many'
    ]
    
    def validate(self, config: Dict[str, Any]) -> None:
        if 'tables' not in config:
            raise ValueError("Missing required section 'tables' in generator configuration")
        
        for i, table in enumerate(config['tables']):
            if 'name' not in table:
                raise ValueError(f"Table at index {i} is missing a 'name'")
            if 'columns' not in table:
                raise ValueError(f"Table '{table.get('name', f'at index {i}')}' is missing 'columns' definition")
            if 'rows' not in table:
                table['rows'] = 1000
            
            if 'output_type' in table:
                output_type = table['output_type'].lower()
                if output_type not in ['csv', 'fixed_width']:
                    raise ValueError(f"Unsupported output_type: {output_type} for table '{table.get('name')}'. "
                                    "Supported types: 'csv', 'fixed_width'")
                if output_type == 'fixed_width':
                    if 'fixed_width_options' not in table:
                        table['fixed_width_options'] = {'encoding': 'utf-8'}
                    if 'definition_path' not in table['fixed_width_options']:
                        raise ValueError(f"Missing required 'definition_path' in fixed_width_options for table '{table.get('name')}'")
                    definition_path = table['fixed_width_options']['definition_path']
                    if not os.path.exists(definition_path):
                        self.logger.warning(f"Definition file not found: {definition_path}.")
            
            for j, column in enumerate(table['columns']):
                if 'name' not in column:
                    raise ValueError(f"Column at index {j} in table '{table.get('name')}' is missing a 'name'")
                if 'type' not in column:
                    raise ValueError(f"Column '{column.get('name', f'at index {j}')}' in table '{table.get('name')}' is missing a 'type'")
                col_type = column['type']
                if col_type == 'faker' and 'faker_type' not in column:
                    column['faker_type'] = 'word'
    
    def _validate_tables(self, tables: List[Dict[str, Any]]) -> None:
        table_names = set()
        for i, table in enumerate(tables):
            table_path = f"tables[{i}]"
            if 'name' not in table:
                raise ValidationError("Missing required field 'name'", table_path)
            table_name = table['name']
            if table_name in table_names:
                raise ValidationError(f"Duplicate table name: '{table_name}'", f"{table_path}.name")
            table_names.add(table_name)
            if 'columns' not in table:
                raise ValidationError("Missing required field 'columns'", table_path)
            if 'rows' not in table:
                table['rows'] = 1000
            rows = table['rows']
            if not isinstance(rows, int) or rows <= 0:
                raise ValidationError(f"'rows' must be a positive integer, got '{rows}'", f"{table_path}.rows")
            if 'output_path' in table:
                output_dir = os.path.dirname(table['output_path'])
                if output_dir and not os.path.exists(output_dir):
                    self.logger.info(f"Output directory '{output_dir}' doesn't exist.")
            self._validate_columns(table['columns'], table_path, table_name)
    
    def _validate_columns(self, columns: List[Dict[str, Any]], table_path: str, table_name: str) -> None:
        if not columns:
            raise ValidationError("No columns defined", f"{table_path}.columns")
        column_names = set()
        for j, column in enumerate(columns):
            column_path = f"{table_path}.columns[{j}]"
            if 'name' not in column:
                raise ValidationError("Missing required field 'name'", column_path)
            column_name = column['name']
            if column_name in column_names:
                raise ValidationError(f"Duplicate column name: '{column_name}'", f"{column_path}.name")
            column_names.add(column_name)
            if 'type' not in column:
                raise ValidationError("Missing required field 'type'", column_path)
            column_type = column['type']
            if column_type not in self.SUPPORTED_COLUMN_TYPES:
                raise ValidationError(
                    f"Unsupported column type: '{column_type}'. Supported types: {', '.join(self.SUPPORTED_COLUMN_TYPES)}",
                    f"{column_path}.type"
                )
            self._validate_column_type_specific(column, column_path, column_type)
    
    def _validate_column_type_specific(self, column: Dict[str, Any], column_path: str, column_type: str) -> None:
        if 'null_probability' in column:
            null_prob = column['null_probability']
            if not isinstance(null_prob, (int, float)) or not (0 <= null_prob <= 1):
                raise ValidationError(
                    f"'null_probability' must be a number between 0 and 1, got {null_prob}",
                    f"{column_path}.null_probability"
                )
        
        if column_type == 'faker':
            if 'faker_type' not in column:
                column['faker_type'] = 'word'
        elif column_type == 'sequence':
            if 'start' not in column:
                column['start'] = 1
            if 'step' not in column:
                column['step'] = 1
        elif column_type == 'categorical':
            if 'categories' not in column:
                raise ValidationError("Missing required field 'categories'", f"{column_path}.categories")
            categories = column['categories']
            if not isinstance(categories, list) or not categories:
                raise ValidationError("'categories' must be a non-empty list", f"{column_path}.categories")
            if 'weights' in column:
                weights = column['weights']
                if not isinstance(weights, list) or len(weights) != len(categories):
                    raise ValidationError(
                        "'weights' must be a list with the same length as 'categories'",
                        f"{column_path}.weights"
                    )
                weight_sum = sum(weights)
                if not (0.99 <= weight_sum <= 1.01):
                    self.logger.warning(f"Weights for column '{column.get('name')}' sum to {weight_sum}.")
        elif column_type == 'reference':
            if 'reference_table' not in column:
                raise ValidationError("Missing required field 'reference_table'", f"{column_path}.reference_table")
            if 'reference_column' not in column:
                raise ValidationError("Missing required field 'reference_column'", f"{column_path}.reference_column")
        elif column_type == 'formula':
            if 'formula' not in column:
                raise ValidationError("Missing required field 'formula'", f"{column_path}.formula")
            refs = column.get('references')
            if refs is not None and not isinstance(refs, list):
                raise ValidationError(
                    f"'references' must be a list of column names (got {type(refs).__name__})",
                    f"{column_path}.references",
                )

        if 'fixed_width_options' in column:
            fixed_width_opts = column['fixed_width_options']
            if 'padding_char' in fixed_width_opts:
                padding_char = fixed_width_opts['padding_char']
                if not isinstance(padding_char, str) or len(padding_char) != 1:
                    self.logger.warning(f"Column '{column.get('name')}': padding_char must be a single character.")
                    fixed_width_opts['padding_char'] = ' '
            if 'padding_alignment' in fixed_width_opts:
                alignment = fixed_width_opts['padding_alignment']
                valid_alignments = ['auto', 'left', 'right']
                if alignment not in valid_alignments:
                    self.logger.warning(f"Column '{column.get('name')}': Invalid padding_alignment: '{alignment}'.")
                    fixed_width_opts['padding_alignment'] = 'left'
    
    def _validate_relationships(self, relationships: List[Dict[str, Any]], tables: List[Dict[str, Any]]) -> None:
        if not relationships:
            return
        table_map = {}
        for table in tables:
            table_name = table.get('name')
            if table_name:
                column_names = [col.get('name') for col in table.get('columns', []) if col.get('name')]
                table_map[table_name] = set(column_names)
        for i, relationship in enumerate(relationships):
            rel_path = f"relationships[{i}]"
            if 'name' not in relationship:
                raise ValidationError("Missing required field 'name'", rel_path)
            if 'type' not in relationship:
                raise ValidationError("Missing required field 'type'", rel_path)
            rel_type = relationship['type']
            if rel_type not in self.SUPPORTED_RELATIONSHIP_TYPES:
                raise ValidationError(
                    f"Unsupported relationship type: '{rel_type}'. Supported types: {', '.join(self.SUPPORTED_RELATIONSHIP_TYPES)}",
                    f"{rel_path}.type"
                )
            if rel_type == 'self_reference':
                self._validate_self_reference(relationship, rel_path, table_map)
            elif rel_type == 'foreign_key':
                self._validate_foreign_key(relationship, rel_path, table_map)
            elif rel_type == 'many_to_many':
                self._validate_many_to_many(relationship, rel_path, table_map)
    
    def _validate_self_reference(self, relationship, rel_path, table_map):
        for field in ['table', 'column', 'reference_column']:
            if field not in relationship:
                raise ValidationError(f"Missing required field '{field}'", f"{rel_path}.{field}")
        table_name = relationship['table']
        if table_name not in table_map:
            raise ValidationError(f"Table '{table_name}' not defined", f"{rel_path}.table")
        column_set = table_map[table_name]
        if relationship['column'] not in column_set:
            raise ValidationError(f"Column '{relationship['column']}' not defined in table '{table_name}'", f"{rel_path}.column")
        if relationship['reference_column'] not in column_set:
            raise ValidationError(f"Column '{relationship['reference_column']}' not defined in table '{table_name}'", f"{rel_path}.reference_column")
    
    def _validate_foreign_key(self, relationship, rel_path, table_map):
        for field in ['source_table', 'source_column', 'target_table', 'target_column']:
            if field not in relationship:
                raise ValidationError(f"Missing required field '{field}'", f"{rel_path}.{field}")
        source_table = relationship['source_table']
        target_table = relationship['target_table']
        if source_table not in table_map:
            raise ValidationError(f"Source table '{source_table}' not defined", f"{rel_path}.source_table")
        if target_table not in table_map:
            raise ValidationError(f"Target table '{target_table}' not defined", f"{rel_path}.target_table")
        if relationship['source_column'] not in table_map[source_table]:
            raise ValidationError(f"Column '{relationship['source_column']}' not defined in table '{source_table}'", f"{rel_path}.source_column")
        if relationship['target_column'] not in table_map[target_table]:
            raise ValidationError(f"Column '{relationship['target_column']}' not defined in table '{target_table}'", f"{rel_path}.target_column")
    
    def _validate_many_to_many(self, relationship, rel_path, table_map):
        if 'junction_table' not in relationship:
            raise ValidationError("Missing required field 'junction_table'", f"{rel_path}.junction_table")
        junction_table = relationship['junction_table']
        if junction_table not in table_map:
            raise ValidationError(f"Junction table '{junction_table}' not defined", f"{rel_path}.junction_table")


import re

_GRAPH_NODE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")


class GraphConfigValidator(ConfigValidator):
    """Validates `mode: graph` pipeline configs."""

    SUPPORTED_SCHEMA_VERSIONS = {1}

    # Op kinds treated as file-producing sources for format-consistency checks.
    _FILE_SOURCE_KINDS: frozenset = frozenset({
        "source.file",
        "source.s3",
        "source.gcs",
        "source.sftp",
    })

    # Op kinds treated as file-consuming sinks for format-consistency checks.
    _FILE_TARGET_KINDS: frozenset = frozenset({
        "target.file",
        "target.s3",
        "target.gcs",
        "target.sftp",
    })

    def validate(self, config: Dict[str, Any]) -> None:
        try:
            self._validate_top_level(config)
            kinds = self._known_kinds()
            self._validate_nodes(config["nodes"], kinds)
            self._validate_edges(config.get("edges") or [], config["nodes"])
            self._validate_cardinality(config["nodes"], config.get("edges") or [], kinds)
            self._validate_acyclic(config["nodes"], config.get("edges") or [])
            self._validate_file_format_consistency(config["nodes"], config.get("edges") or [])
            self._validate_mask_column_reachability(config["nodes"], config.get("edges") or [])
            self._validate_nodes_ref_reachability(config["nodes"], config.get("edges") or [])
        except ValidationError as e:
            self.logger.error(str(e))
            raise

    def _known_kinds(self) -> Set[str]:
        from decoy_engine.graph.ops import OPS
        return set(OPS.keys())

    def _validate_top_level(self, config: Dict[str, Any]) -> None:
        from decoy_engine.validation_result import CODES

        mode = config.get("mode")
        if mode != "graph":
            raise ValidationError(
                f"top-level 'mode' must be 'graph' (got {mode!r})", "mode",
                code=CODES.TOP_LEVEL_BAD_MODE,
            )
        if not isinstance(config.get("nodes"), list) or not config["nodes"]:
            raise ValidationError(
                "'nodes' must be a non-empty list", "nodes",
                code=CODES.NODES_EMPTY_LIST,
            )
        if "edges" in config and not isinstance(config["edges"], list):
            raise ValidationError(
                "'edges' must be a list", "edges",
                code=CODES.EDGES_BAD_TYPE,
            )

        sv = config.get("schema_version", 1)
        if not isinstance(sv, int) or sv not in self.SUPPORTED_SCHEMA_VERSIONS:
            raise ValidationError(
                f"unsupported schema_version {sv!r} (supported: {sorted(self.SUPPORTED_SCHEMA_VERSIONS)})",
                "schema_version",
                code=CODES.TOP_LEVEL_BAD_SCHEMA_VERSION,
            )

        engine = config.get("engine", "pandas")
        if engine not in ("pandas", "hybrid"):
            raise ValidationError(
                f"'engine' must be 'pandas' or 'hybrid' (got {engine!r})",
                "engine",
                code=CODES.TOP_LEVEL_BAD_ENGINE,
            )

    def _validate_nodes(
        self, nodes: List[Dict[str, Any]], kinds: Set[str]
    ) -> None:
        from decoy_engine.graph.ops import OPS
        from decoy_engine.graph.conversion import VALID_ENGINES
        from decoy_engine.validation_result import CODES

        seen_ids: Set[str] = set()
        for i, node in enumerate(nodes):
            path = f"nodes[{i}]"
            if not isinstance(node, dict):
                raise ValidationError(
                    "node must be a mapping", path,
                    code=CODES.NODE_BAD_TYPE,
                )
            nid = node.get("id")
            if not isinstance(nid, str) or not _GRAPH_NODE_ID_RE.match(nid):
                raise ValidationError(
                    "id must match ^[a-zA-Z][a-zA-Z0-9_]{0,63}$",
                    f"{path}.id",
                    code=CODES.NODE_BAD_ID,
                )
            if nid in seen_ids:
                raise ValidationError(
                    f"duplicate node id {nid!r}", f"{path}.id",
                    code=CODES.NODE_DUPLICATE_ID,
                )
            seen_ids.add(nid)

            kind = node.get("kind")
            if kind not in kinds:
                raise ValidationError(
                    f"unknown kind {kind!r} (supported: {sorted(kinds)})",
                    f"{path}.kind",
                    code=CODES.NODE_UNKNOWN_KIND,
                )

            declared_engine = getattr(OPS[kind], "NATIVE_ENGINE", None)
            if declared_engine is not None and declared_engine not in VALID_ENGINES:
                raise ValidationError(
                    f"op {kind!r} declares invalid NATIVE_ENGINE {declared_engine!r}; "
                    f"supported: {sorted(VALID_ENGINES)}",
                    f"{path}.kind",
                    code=CODES.NODE_BAD_NATIVE_ENGINE,
                )

            name = node.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValidationError(
                    "name must be a non-empty string when set",
                    f"{path}.name",
                    code=CODES.NODE_BAD_NAME,
                )

            cfg = node.get("config", {})
            if not isinstance(cfg, dict):
                raise ValidationError(
                    "config must be a mapping", f"{path}.config",
                    code=CODES.NODE_BAD_CONFIG_TYPE,
                )

            try:
                OPS[kind].validate_config(cfg)
            except ValidationError as e:
                raw_msg = getattr(e, "raw_message", None) or str(e)
                raise ValidationError(
                    raw_msg,
                    f"{path}.{getattr(e, 'path', None) or 'config'}",
                    code=getattr(e, "code", None),
                ) from e

    def _validate_nodes_collecting(
        self, nodes: List[Dict[str, Any]], kinds: Set[str]
    ) -> List[ValidationError]:
        """Like _validate_nodes but collects ALL per-node errors instead of
        raising on the first. Called by validate_graph_full (R2.2) so that
        a graph with multiple bad nodes surfaces all of them in one pass.

        _validate_nodes is unchanged for backward compatibility -- validate()
        and _validate_or_raise still use it via the raise-on-first path.
        """
        from decoy_engine.graph.ops import OPS
        from decoy_engine.graph.conversion import VALID_ENGINES
        from decoy_engine.validation_result import CODES

        errors: List[ValidationError] = []
        seen_ids: Set[str] = set()

        for i, node in enumerate(nodes):
            path = f"nodes[{i}]"

            if not isinstance(node, dict):
                errors.append(ValidationError(
                    "node must be a mapping", path, code=CODES.NODE_BAD_TYPE,
                ))
                continue  # can't inspect sub-fields of a non-dict

            nid = node.get("id")
            if not isinstance(nid, str) or not _GRAPH_NODE_ID_RE.match(nid):
                errors.append(ValidationError(
                    "id must match ^[a-zA-Z][a-zA-Z0-9_]{0,63}$",
                    f"{path}.id",
                    code=CODES.NODE_BAD_ID,
                ))
            else:
                if nid in seen_ids:
                    errors.append(ValidationError(
                        f"duplicate node id {nid!r}", f"{path}.id",
                        code=CODES.NODE_DUPLICATE_ID,
                    ))
                seen_ids.add(nid)

            kind = node.get("kind")
            kind_valid = kind in kinds
            if not kind_valid:
                errors.append(ValidationError(
                    f"unknown kind {kind!r} (supported: {sorted(kinds)})",
                    f"{path}.kind",
                    code=CODES.NODE_UNKNOWN_KIND,
                ))
            else:
                declared_engine = getattr(OPS[kind], "NATIVE_ENGINE", None)
                if declared_engine is not None and declared_engine not in VALID_ENGINES:
                    errors.append(ValidationError(
                        f"op {kind!r} declares invalid NATIVE_ENGINE {declared_engine!r}; "
                        f"supported: {sorted(VALID_ENGINES)}",
                        f"{path}.kind",
                        code=CODES.NODE_BAD_NATIVE_ENGINE,
                    ))

            name = node.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                errors.append(ValidationError(
                    "name must be a non-empty string when set",
                    f"{path}.name",
                    code=CODES.NODE_BAD_NAME,
                ))

            cfg = node.get("config", {})
            if not isinstance(cfg, dict):
                errors.append(ValidationError(
                    "config must be a mapping", f"{path}.config",
                    code=CODES.NODE_BAD_CONFIG_TYPE,
                ))
            elif kind_valid:
                try:
                    OPS[kind].validate_config(cfg)
                except ValidationError as e:
                    raw_msg = getattr(e, "raw_message", None) or str(e)
                    errors.append(ValidationError(
                        raw_msg,
                        f"{path}.{getattr(e, 'path', None) or 'config'}",
                        code=getattr(e, "code", None),
                    ))

        return errors

    def _validate_edges(
        self, edges: List[Dict[str, Any]], nodes: List[Dict[str, Any]]
    ) -> None:
        from decoy_engine.graph.ops import OPS

        node_ids = {n["id"] for n in nodes}
        node_by_id = {n["id"]: n for n in nodes}
        for j, edge in enumerate(edges):
            path = f"edges[{j}]"
            if not isinstance(edge, dict):
                raise ValidationError("edge must be a mapping", path)
            src = edge.get("from")
            dst = edge.get("to")

            # Handle "node_id.port" notation for split ops.
            if isinstance(src, str) and "." in src:
                base_nid, port = src.split(".", 1)
                if base_nid not in node_ids:
                    raise ValidationError(
                        f"'from' references unknown node {base_nid!r}", f"{path}.from"
                    )
                op = OPS[node_by_id[base_nid]["kind"]]
                if getattr(op, "OUTPUT_KIND", "stream") != "split":
                    raise ValidationError(
                        f"node {base_nid!r} is not a split op; port notation not allowed",
                        f"{path}.from",
                    )
                valid_ports = getattr(op, "OUTPUT_PORTS", ())
                if port not in valid_ports:
                    raise ValidationError(
                        f"unknown port {port!r} on split node {base_nid!r} "
                        f"(valid: {valid_ports})",
                        f"{path}.from",
                    )
            else:
                if src not in node_ids:
                    raise ValidationError(
                        f"'from' references unknown node {src!r}", f"{path}.from"
                    )

            if dst not in node_ids:
                raise ValidationError(
                    f"'to' references unknown node {dst!r}", f"{path}.to"
                )

    def _validate_cardinality(
        self,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        kinds: Set[str],
    ) -> None:
        from decoy_engine.graph.ops import OPS

        in_count: Dict[str, int] = {n["id"]: 0 for n in nodes}
        out_count: Dict[str, int] = {n["id"]: 0 for n in nodes}
        for e in edges:
            in_count[e["to"]] += 1
            base_src = e["from"].split(".", 1)[0]  # strip port suffix for split ops
            out_count[base_src] += 1

        for n in nodes:
            kind = n["kind"]
            op = OPS[kind]
            arity = getattr(op, "INPUT_ARITY", (1, 1))
            output_kind = getattr(op, "OUTPUT_KIND", "stream")
            ic = in_count[n["id"]]
            oc = out_count[n["id"]]

            min_in, max_in = arity
            if ic < min_in:
                raise ValidationError(
                    f"node {n['id']!r} ({kind}) needs at least {min_in} incoming edge(s), got {ic}",
                    f"nodes.{n['id']}",
                )
            if max_in is not None and ic > max_in:
                hint = (
                    " -- combine upstream tables with a 'unite' node first"
                    if max_in == 1 and kind != "unite"
                    else ""
                )
                raise ValidationError(
                    f"node {n['id']!r} ({kind}) accepts at most {max_in} incoming edge(s), got {ic}{hint}",
                    f"nodes.{n['id']}",
                )
            if output_kind == "sink" and oc > 0:
                raise ValidationError(
                    f"target node {n['id']!r} must have no outgoing edges (got {oc})",
                    f"nodes.{n['id']}",
                )

    def _validate_acyclic(
        self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]
    ) -> None:
        from decoy_engine.graph.topo import topo_order
        topo_order(nodes, edges)  # raises ValidationError on cycle

    def _validate_file_format_consistency(
        self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
        strict: bool = False,
    ) -> None:
        """Warn when file-source and file-target formats differ without a convert.file_type.

        Covers all file-source kinds (source.file, source.s3, source.gcs,
        source.sftp) and all file-target kinds (target.file, target.s3,
        target.gcs, target.sftp) so cloud-storage pipelines get the same
        mismatch guard as local-file pipelines.

        Also back-fills target.file config.format from the source format when
        the field is absent (cloud targets resolve format via their own
        validate_config / extension inference).
        """
        from decoy_engine.graph.ops._cloud_io import infer_format as _infer_fmt

        node_by_id: Dict[str, Dict[str, Any]] = {n["id"]: n for n in nodes}
        node_idx: Dict[str, int] = {
            n["id"]: i for i, n in enumerate(nodes) if isinstance(n, dict)
        }

        # Adjacency list: node_id -> list of direct downstream node_ids.
        # Strip the ".port" suffix that split ops use so the BFS stays simple.
        adj: Dict[str, List[str]] = {n["id"]: [] for n in nodes}
        for e in edges:
            src = e["from"].split(".", 1)[0]
            adj[src].append(e["to"])

        for node in nodes:
            if node.get("kind") not in self._FILE_SOURCE_KINDS:
                continue

            src_id = node["id"]
            src_kind = node["kind"]
            src_cfg = node.get("config", {})
            src_fmt = src_cfg.get("format") or _infer_fmt(src_cfg.get("path", ""))
            if not src_fmt:
                continue

            visited_states: Set[tuple] = set()
            queue: List[tuple] = [(src_id, False)]
            target_reach: Dict[str, Set[bool]] = {}

            while queue:
                cur_id, has_convert = queue.pop(0)
                state = (cur_id, has_convert)
                if state in visited_states:
                    continue
                visited_states.add(state)

                cur_kind = node_by_id[cur_id].get("kind")
                if cur_id != src_id and cur_kind in self._FILE_TARGET_KINDS:
                    target_reach.setdefault(cur_id, set()).add(has_convert)
                    continue

                next_has_convert = has_convert or (cur_kind == "convert.file_type")
                for nxt_id in adj.get(cur_id, []):
                    queue.append((nxt_id, next_has_convert))

            for tgt_id, reach_states in target_reach.items():
                if True in reach_states:
                    continue

                tgt_node = node_by_id[tgt_id]
                tgt_kind = tgt_node.get("kind")
                tgt_cfg = tgt_node.get("config", {})

                tgt_fmt = (
                    tgt_cfg.get("format")
                    or _infer_fmt(tgt_cfg.get("output_filename", ""))
                    or _infer_fmt(tgt_cfg.get("path", ""))
                )

                if tgt_kind == "target.file" and not tgt_cfg.get("format"):
                    if strict:
                        from decoy_engine.validation_result import CODES as _CODES
                        raise ValidationError(
                            f"target.file node {tgt_id!r} has no explicit 'format' field; "
                            f"strict mode requires every target to declare its format "
                            f"explicitly (source {src_id!r} uses {src_fmt!r})",
                            f"nodes[{node_idx.get(tgt_id, '?')}].config.format",
                            code=_CODES.TARGET_FILE_FORMAT_INFERRED,
                        )
                    tgt_cfg["format"] = tgt_fmt

                if tgt_fmt and tgt_fmt != src_fmt:
                    # R3.6 demotion: a format mismatch used to be a hard
                    # error (forcing the user to insert a convert.file_type
                    # node). UX feedback: the target writes whatever
                    # extension the user picked, so the conversion is
                    # implicit and should "just work" with a visible
                    # disclosure. Engine logs a warning here; platform
                    # preflight (api/pipelines/preflight.py) emits the
                    # structured graph.format_mismatch advisory with
                    # severity=warning so the R2.5 policy treats it as
                    # non-blocking and the target node UI banner picks
                    # up the conversion message. The explicit
                    # convert.file_type node remains as an advanced-tier
                    # affordance for users who want the conversion to
                    # show up as its own graph node.
                    self.logger.warning(
                        "%s %r produces %s but %s %r expects %s; "
                        "target will auto-convert file type at write time",
                        src_kind, src_id, src_fmt, tgt_kind, tgt_id, tgt_fmt,
                    )

    def _validate_mask_column_reachability(
        self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]
    ) -> None:
        """R2.3: a mask node references column names; those names must
        exist in the schema the upstream source produces.
        """
        from decoy_engine.graph.output_schema import (
            is_auto_name,
            predicted_output_columns,
        )
        from decoy_engine.validation_result import CODES

        node_by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}

        upstream: Dict[str, List[str]] = {nid: [] for nid in node_by_id}
        for e in edges:
            if not isinstance(e, dict):
                continue
            src = e.get("from")
            dst = e.get("to")
            if isinstance(src, str) and isinstance(dst, str):
                base = src.split(".", 1)[0]
                if dst in upstream:
                    upstream[dst].append(base)

        for node in nodes:
            if not isinstance(node, dict) or node.get("kind") != "mask":
                continue
            mask_id = node.get("id", "")
            cols_cfg = (node.get("config") or {}).get("columns")
            if not isinstance(cols_cfg, dict) or not cols_cfg:
                continue

            predicted_union: set[str] = set()
            had_auto = False
            had_unknown = False
            for up_id in upstream.get(mask_id, []):
                up_node = node_by_id.get(up_id)
                if up_node is None:
                    continue
                pred = predicted_output_columns(up_node)
                if pred is None:
                    had_unknown = True
                    continue
                if pred == "$auto":
                    had_auto = True
                    continue
                if isinstance(pred, list):
                    predicted_union.update(pred)

            if had_unknown:
                continue

            for col_name in cols_cfg.keys():
                if had_auto:
                    if not is_auto_name(col_name):
                        raise ValidationError(
                            f"mask node {mask_id!r} references column "
                            f"{col_name!r}, but the upstream source has "
                            f"has_header=false with no column_names set "
                            f"- the engine will produce auto-named "
                            f"'column0', 'column1', ... and this rule "
                            f"will silently no-op. Fix: set has_header="
                            f"true on the source, fill in column_names, "
                            f"or load a saved header layout.",
                            f"nodes.{mask_id}.config.columns.{col_name}",
                            code=CODES.MASK_UNKNOWN_COLUMN,
                        )
                    continue
                if predicted_union and col_name not in predicted_union:
                    available = ", ".join(sorted(predicted_union)) or "(none)"
                    raise ValidationError(
                        f"mask node {mask_id!r} references column "
                        f"{col_name!r}, which the upstream source does "
                        f"not produce. Available columns: {available}.",
                        f"nodes.{mask_id}.config.columns.{col_name}",
                        code=CODES.MASK_UNKNOWN_COLUMN,
                    )

    def _validate_nodes_ref_reachability(
        self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]
    ) -> None:
        """R2.3: every ``${nodes.<id>.<key>}`` reference must point at
        a node that exists and is UPSTREAM of the referrer.
        """
        import re as _re
        from decoy_engine.graph.topo import upstream_subgraph
        from decoy_engine.validation_result import CODES

        ref_re = _re.compile(r"\$\{nodes\.([a-zA-Z][\w]*)\.([a-zA-Z_][\w.]*)}")
        node_ids = {n["id"] for n in nodes if isinstance(n, dict) and "id" in n}

        def walk(value: Any) -> List[tuple[str, str]]:
            """Yield (target_id, key) for every nodes-ref token under value."""
            found: List[tuple[str, str]] = []
            if isinstance(value, str):
                for m in ref_re.finditer(value):
                    found.append((m.group(1), m.group(2)))
            elif isinstance(value, dict):
                for v in value.values():
                    found.extend(walk(v))
            elif isinstance(value, list):
                for v in value:
                    found.extend(walk(v))
            return found

        for node in nodes:
            if not isinstance(node, dict):
                continue
            referrer_id = node.get("id")
            if not isinstance(referrer_id, str):
                continue
            cfg = node.get("config") or {}
            refs = walk(cfg)
            if not refs:
                continue
            try:
                ordered_ids, _ = upstream_subgraph(nodes, edges, referrer_id)
                upstream_ids = set(ordered_ids)
                upstream_ids.discard(referrer_id)
            except Exception:
                continue
            for target_id, key in refs:
                if target_id not in node_ids:
                    raise ValidationError(
                        f"node {referrer_id!r} references "
                        f"${{nodes.{target_id}.{key}}}, but no node "
                        f"with id {target_id!r} exists in this graph.",
                        f"nodes.{referrer_id}.config",
                        code=CODES.NODES_REF_UNKNOWN_ID,
                    )
                if target_id not in upstream_ids:
                    raise ValidationError(
                        f"node {referrer_id!r} references "
                        f"${{nodes.{target_id}.{key}}}, but {target_id!r} "
                        f"is not upstream of this node - the export will "
                        f"never be set at run time. Add an edge from "
                        f"{target_id!r} (directly or transitively) to "
                        f"{referrer_id!r}, or fix the reference.",
                        f"nodes.{referrer_id}.config",
                        code=CODES.NODES_REF_NOT_UPSTREAM,
                    )
