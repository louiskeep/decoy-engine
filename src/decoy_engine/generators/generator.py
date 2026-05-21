# decoy_engine/generator/generator.py
"""
Main generator class for the decoy_engine package.
Handles the creation of synthetic data.
"""

import os
import yaml
import pandas as pd
import random
import time
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, List

from decoy_engine.context import emit_lineage, emit_step
from decoy_engine.expressions import BASE_GLOBALS, safe_eval

class DataGenerator:
    """
    Handles the generation of synthetic data with referential integrity.
    """
    
    def __init__(self, config_path: str, logger=None, ctx=None):
        """
        Initialize the generator with a configuration file

        Args:
            config_path: Path to the YAML configuration file
            logger: Direct logger instance (optional, kept for backwards
                compatibility -- prefer the ctx parameter for new code).
            ctx: Optional decoy_engine.ExecutionContext. If provided and
                ctx.logger is set, it takes precedence over the `logger`
                kwarg. When neither is provided, the engine creates a
                default logger.
        """
        # Load configuration
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        # Pick a logger: ctx.logger > logger > engine default
        if ctx is not None and getattr(ctx, "logger", None) is not None:
            self.logger = ctx.logger
        elif logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger
            self.logger = get_logger()
        
        # Validate configuration
        from decoy_engine.internal.validator import GeneratorConfigValidator
        self.validator = GeneratorConfigValidator(self.logger)
        self.validator.validate(self.config)
        
        # Initialize seed for deterministic generation
        self.seed = self.config.get('generator_settings', {}).get('seed', 42)
        random.seed(self.seed)

        # Thread the pipeline-bound key resolver from the ExecutionContext
        # into ColumnGenerator. When the platform / CLI built `ctx` with a
        # `pipeline_derive_key` (master-key-derived, pipeline-label-scoped),
        # this is what ties generation output to the tenant's master key --
        # same master + same pipeline_label produces the same generated
        # bytes across runs and across instances. Without this thread,
        # generation falls back to the legacy `seed + hash(name)` per-column
        # seed and the "lose the DB, restore the master key" recovery story
        # only holds for mask.
        self.pipeline_derive_key = (
            ctx.pipeline_derive_key if ctx is not None else None
        )

        # Initialize column generator
        from decoy_engine.generators.columns import ColumnGenerator
        instance_locale = (
            getattr(ctx, "instance_default_locale", None) if ctx is not None else None
        )
        self.column_generator = ColumnGenerator(
            self.seed, self.logger, derive_key=self.pipeline_derive_key,
            instance_default_locale=instance_locale,
        )
        
        # Initialize relationship handler
        from decoy_engine.generators.relationships import RelationshipHandler
        self.relationship_handler = RelationshipHandler(self.seed, self.logger)
        
        # Initialize memory monitoring
        from decoy_engine.internal.memory import MemoryMonitor
        MemoryMonitor.monitor_memory_usage(self.logger, "After generator initialization")
        
        # Reference data storage
        self.reference_data = {}
        
        self.logger.info(f"DataGenerator initialized with configuration: {config_path}")
    
    def generate(self):
        """
        Generate synthetic data according to the configuration
        """
        self.logger.info(f"=== Starting data generation process ===")

        # Pre-process configuration to ensure definition files exist
        self._preprocess_configuration()

        # Use tables in the exact order specified in the configuration file
        # Note: Users must ensure tables with dependencies appear after their referenced tables
        tables = self.config.get('tables', [])

        self.logger.info(f"Generating {len(tables)} tables in the order specified in the configuration")

        # Lineage: one output entry per generated table. There's no
        # external "source" for a generate run -- the source is the
        # config itself -- so emit a single virtual source labeled by
        # the config and one output per produced table.
        emit_lineage(self.logger, 'transform', 'synth', 'generate')
        for table_config in tables:
            tname = table_config.get('name', 'unnamed_table')
            out_type = table_config.get('output_type', 'csv').lower()
            emit_lineage(self.logger, 'output', tname, out_type)

        start_time = time.time()

        # Generate tables in the order they appear in the config --
        # emit one step per table so the timeline shows per-table
        # boundaries. ``synth:<table>`` lets the UI group all
        # generation work under a single 'synth' phase while still
        # rendering per-table durations.
        for table_config in tables:
            table_name = table_config.get('name', 'unnamed_table')
            self.logger.info(f"Generating table: {table_name}")
            step_name = f"synth:{table_name}"
            emit_step(self.logger, step_name, status='start')
            self._generate_table(table_config)
            emit_step(
                self.logger, step_name, status='finish',
                rows_out=table_config.get('rows', 1000),
            )

            # Process self-references if any
            self.relationship_handler.process_self_references(table_config, self.config)

        # Process relationships between tables
        self.relationship_handler.process_relationships(self.config, self.reference_data)

        # Process formulas with cross-column `references: [...]` after every
        # other column in every table is generated. Same scope + per-row
        # deterministic seeding as the inline formula path; the only thing
        # that differs is when it runs (post-pass) and that the row context
        # includes the referenced sibling columns.
        self._process_referenced_formulas()
        
        # Calculate total time
        total_time = time.time() - start_time
        
        # Calculate overall processing speed
        table_count = len(tables)
        avg_time_per_table = total_time / table_count if table_count > 0 else 0
        
        self.logger.info(f"Data generation completed in {total_time:.1f} seconds")
        self.logger.info(f"Average time per table: {avg_time_per_table:.1f} seconds")
        self.logger.info("=== Data generation completed successfully ===")
    
    def _generate_table(self, table_config: Dict[str, Any]):
        """
        Generate a single table of data
        
        Args:
            table_config: Configuration for this table
        """
        table_name = table_config.get('name', 'generated_table')
        output_path = table_config.get('output_path')
        
        # If output_path not specified, use default directory
        if not output_path:
            output_dir = self.config.get('generator_settings', {}).get('output_directory', 'data/generated/')
            output_path = os.path.join(output_dir, f"{table_name}.csv")
        
        num_rows = table_config.get('rows', 1000)
        
        self.logger.info(f"Generating table: {table_name} with {num_rows} rows")
        
        # Create empty DataFrame with specified columns
        df = pd.DataFrame()
        
        # Get table start time
        table_start_time = time.time()
        
        # Generate data for each column
        column_configs = table_config.get('columns', [])
        
        # Create progress logger
        from decoy_engine.internal.logging import ProgressLogger
        progress = ProgressLogger(self.logger, len(column_configs), f"Generating columns for {table_name}")
        progress.start()
        
        for column_config in column_configs:
            column_name = column_config.get('name')
            data_type = column_config.get('type')
            
            if column_name and data_type:
                self.logger.debug(f"Generating data for column: {column_name} of type: {data_type}")
                df[column_name] = self.column_generator.generate_column(num_rows, column_config, table_name, self.reference_data)
                progress.update(1)
            else:
                self.logger.warning(f"Skipping column with missing name or type: {column_config}")
        
        progress.finish()
        
        # Store reference data for other tables to use
        self.reference_data[table_name] = df
        
        # Create output directory if it doesn't exist
        from decoy_engine.internal.helpers import create_directory_for_file
        create_directory_for_file(output_path)
        
        # Determine output format and save accordingly
        output_type = table_config.get('output_type', 'csv').lower()
        
        if output_type == 'csv':
            # Write generated data to CSV (existing behavior)
            self.logger.info(f"Saving table {table_name} as CSV")
            df.to_csv(output_path, index=False)
        elif output_type == 'fixed_width':
            # Write generated data to fixed-width format using IOHandler
            self.logger.info(f"Saving table {table_name} as fixed-width")
            
            # Get fixed-width options
            fixed_width_options = table_config.get('fixed_width_options', {})
            definition_path = fixed_width_options.get('definition_path')
            
            if not definition_path:
                self.logger.error("No definition_path specified for fixed-width output")
                raise ValueError("Fixed-width output requires a definition_path in fixed_width_options")
            
            # Ensure the definition file exists
            if not os.path.exists(definition_path):
                self.logger.error(f"Definition file not found: {definition_path}")
                raise FileNotFoundError(f"Definition file not found: {definition_path}")
            
            # Create input and output config for the IOHandler
            input_config = {
                'type': 'fixed_width',
                'path': 'dummy.txt',  # Not used for output, but needs to be set
                'fixed_width_options': fixed_width_options
            }
            
            output_config = {
                'type': 'fixed_width',
                'path': output_path,
                'fixed_width_options': fixed_width_options
            }
            
            # Create and use IO handler with column configurations
            from decoy_engine.connectors.factory import create_io_handler
            io_handler = create_io_handler(input_config, output_config, self.config, self.logger)
            
            # Set column configurations on the handler for padding purposes
            io_handler.set_column_configurations(column_configs)
            io_handler.save_data(df)
        else:
            self.logger.warning(f"Unsupported output type: {output_type}, defaulting to CSV")
            df.to_csv(output_path, index=False)
        
        # Calculate table generation time
        table_time = time.time() - table_start_time
        
        # Log table generation stats
        self.logger.info(f"Generated table {table_name} in {table_time:.1f} seconds")
        self.logger.info(f"Generated data saved to {output_path}")
    
    def _process_referenced_formulas(self):
        """
        Process formula columns that read sibling columns via
        ``references: [col_a, col_b, ...]``. Runs AFTER every column in
        every table has been generated and written, so the formula sees
        finalized values. Same Python-expression evaluator + same per-row
        deterministic seeding as the inline formula path.

        Trigger condition: ``type == 'formula'`` AND ``references`` is a
        non-empty list. The previous separate ``formula_type: composite``
        flag is gone -- having references is what flips the column into
        post-pass mode.
        """
        self.logger.info("Processing formula columns with cross-column references")

        # Iterate through all tables
        tables = self.config.get('tables', [])
        for table_config in tables:
            table_name = table_config.get('name')
            output_path = table_config.get('output_path')

            # If output_path not specified, use default directory
            if not output_path:
                output_dir = self.config.get('generator_settings', {}).get('output_directory', 'data/generated/')
                output_path = os.path.join(output_dir, f"{table_name}.csv")

            # Pick out columns that defer to the post-pass: formula type +
            # non-empty references. Empty/missing references means the
            # formula was already evaluated inline.
            has_referenced = False
            referenced_configs = []

            for col_config in table_config.get('columns', []):
                if (col_config.get('type') == 'formula' and
                    col_config.get('references')):
                    has_referenced = True
                    referenced_configs.append(col_config)

            if not has_referenced or not os.path.exists(output_path):
                continue

            # Keep `composite_configs` alias for the rest of the function so
            # the existing CSV / fixed-width branches don't need a sweep --
            # the rename is local to this dispatcher.
            composite_configs = referenced_configs
            self.logger.info(f"Processing referenced formulas for table: {table_name}")
            
            try:
                # Determine the file type
                output_type = table_config.get('output_type', 'csv').lower()
                
                if output_type == 'fixed_width':
                    # For fixed-width files, we need to do special processing
                    self.logger.debug(f"Processing fixed-width file for composite formulas: {output_path}")
                    
                    # Get the definition file path
                    fixed_width_options = table_config.get('fixed_width_options', {})
                    definition_path = fixed_width_options.get('definition_path')
                    
                    if not definition_path or not os.path.exists(definition_path):
                        self.logger.error(f"Definition file not found for fixed-width table: {definition_path}")
                        continue
                    
                    # Parse the field definitions to understand column positions
                    field_definitions = self._parse_fixed_width_definition(definition_path, fixed_width_options)
                    if not field_definitions:
                        self.logger.error("Failed to parse field definitions")
                        continue
                    
                    # Create a mapping of field names to their positions
                    field_map = {field['name']: (field['start'], field['end']) for field in field_definitions}
                    self.logger.debug(f"Field map: {field_map}")
                    
                    # Create a list of field names in order for creating a DataFrame
                    field_names = [field['name'] for field in field_definitions]
                    
                    # Read the fixed-width file and process it line by line
                    encoding = fixed_width_options.get('encoding', 'utf-8')
                    
                    # Read all data into memory
                    with open(output_path, 'r', encoding=encoding) as f:
                        lines = f.readlines()
                    
                    self.logger.debug(f"Read {len(lines)} lines from {output_path}")
                    
                    # Process composite formulas for each line
                    for i, line in enumerate(lines):
                        for composite_config in composite_configs:
                            column_name = composite_config.get('name')
                            references = composite_config.get('references', [])
                            original_formula = composite_config.get('formula', '')
                            
                            # Skip if any referenced column doesn't exist in our field map
                            missing_refs = [ref for ref in references if ref not in field_map]
                            if missing_refs:
                                self.logger.warning(f"Missing referenced columns: {', '.join(missing_refs)}")
                                self.logger.debug(f"Available columns: {list(field_map.keys())}")
                                break  # Skip this composite formula
                                
                            # Build row context for composite formula evaluation
                            row_context = {}
                            for ref in references:
                                start, end = field_map[ref]
                                value = line[start:end].strip()
                                row_context[ref] = value
                            try:
                                result = self._evaluate_composite_formula(original_formula, row_context, i, column_name)
                            except Exception as e:
                                self.logger.warning(f"Failed to evaluate composite formula for line {i}, column {column_name}: {e}")
                                self.logger.debug(f"Formula: {original_formula}")
                                result = ""
                            
                            # Replace the value in the fixed-width line
                            col_start, col_end = field_map[column_name]
                            width = col_end - col_start
                            
                            # Format the result to fit in the field width
                            if len(result) > width:
                                # Truncate if too long
                                formatted_result = result[:width]
                                self.logger.warning(f"Truncated result for {column_name}: '{result}' to '{formatted_result}'")
                            else:
                                # Pad with spaces to maintain fixed width
                                formatted_result = result.ljust(width)
                                
                            # Replace the section in the line
                            line = line[:col_start] + formatted_result + line[col_end:]
                            
                        # Update the line in the lines array
                        lines[i] = line
                    
                    # Write all lines back to the file
                    with open(output_path, 'w', encoding=encoding) as f:
                        f.writelines(lines)
                        
                    self.logger.info(f"Saved fixed-width file with processed composite formulas: {output_path}")
                    
                else:
                    # For CSV files, use the standard pandas approach
                    self.logger.debug(f"Processing CSV file for composite formulas: {output_path}")
                    df = pd.read_csv(output_path)
                    
                    # Process each composite formula
                    for col_config in composite_configs:
                        column_name = col_config.get('name')
                        references = col_config.get('references', [])
                        original_formula = col_config.get('formula', '')
                        
                        self.logger.debug(f"Processing composite formula for column: {column_name}")
                        
                        # Skip if referenced columns don't exist
                        missing_refs = [ref for ref in references if ref not in df.columns]
                        if missing_refs:
                            self.logger.warning(f"Missing referenced columns: {', '.join(missing_refs)}")
                            self.logger.debug(f"Available columns: {df.columns.tolist()}")
                            continue
                        
                        # Ensure the composite column can hold string values
                        if column_name not in df:
                            df[column_name] = pd.Series([None] * len(df), dtype=object)
                        elif df[column_name].dtype != object:
                            df[column_name] = df[column_name].astype(object)

                        # Process each row
                        for i in range(len(df)):
                            # Build row context for composite formula evaluation
                            row_context = {}
                            for ref in references:
                                value = df.at[i, ref]
                                if value is None or pd.isna(value):
                                    value = ""
                                row_context[ref] = value
                            try:
                                result = self._evaluate_composite_formula(original_formula, row_context, i, column_name)
                            except Exception as e:
                                self.logger.warning(f"Failed to evaluate composite formula for row {i}, column {column_name}: {e}")
                                self.logger.debug(f"Formula: {original_formula}")
                                result = ""
                            df.at[i, column_name] = result
                    
                    # Save the updated CSV
                    df.to_csv(output_path, index=False)
                    self.logger.info(f"Saved CSV with processed composite formulas: {output_path}")
                    
            except Exception as e:
                self.logger.error(f"Failed to process composite formulas for {table_name}: {str(e)}")
                import traceback
                self.logger.error(f"Traceback: {traceback.format_exc()}")
        
    def _evaluate_composite_formula(
        self, formula: str, context: Dict[str, Any], row_index: int,
        column_name: str = 'unnamed_column',
    ) -> str:
        """Evaluate a referenced formula as a Python expression. Same scope
        as the inline formula path (delegated to ColumnGenerator) PLUS the
        sibling-column values from ``context``.

        Per-row determinism: ``column_seed + row_index`` reseeds ``random``
        and the Faker instance before each row's eval, so RNG calls inside
        the formula are stable across runs given the same key. Pre-F0
        ``_evaluate_composite_formula`` skipped this -- composite formulas
        with ``random()``/``randint()`` were silently non-deterministic.

        F0 also drops the previous ``f'''...'''`` wrapping. Formulas are
        Python expressions; users who want template-style ``"Hello {x}""
        substitution write ``f"Hello {x}"`` themselves.

        All eval() calls route through
        :func:`decoy_engine.expressions.safe_eval`.
        """
        # Reseed per row so RNG calls inside the formula are deterministic.
        # Mirrors what `_eval_formula_inline` does for the inline path.
        column_seed = self.column_generator._column_seed(column_name)
        local_seed = column_seed + row_index
        random.seed(local_seed)
        self.column_generator.faker.seed_instance(local_seed)

        scope = self.column_generator._formula_scope(local_seed)
        scope['i'] = row_index
        scope['index'] = row_index
        scope.update(context)

        result = safe_eval(formula, BASE_GLOBALS, scope)
        return "" if result is None else str(result)

    def _parse_fixed_width_definition(self, definition_path: str, fixed_width_options: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parse a fixed width definition file to get field information
        
        Args:
            definition_path: Path to the definition file
            fixed_width_options: Options for fixed width processing
            
        Returns:
            List of field dictionaries with name, start, and end positions
        """
        fields = []
        
        # Get delimiter from config or default to comma
        delimiter = fixed_width_options.get('definition_delimiter', ',')
        encoding = fixed_width_options.get('encoding', 'utf-8')
        
        self.logger.debug(f"Parsing definition file: {definition_path}")
        
        try:
            with open(definition_path, 'r', encoding=encoding) as f:
                # Skip header line and determine column positions
                header = next(f).strip().split(delimiter)
                header = [h.strip() for h in header]  # Clean whitespace
                
                # Find column indices
                name_idx = header.index('FIELD') if 'FIELD' in header else 0
                start_idx = header.index('START') if 'START' in header else 1
                end_idx = header.index('FINISH') if 'FINISH' in header else 2
                
                self.logger.debug(f"Column positions: FIELD={name_idx}, START={start_idx}, FINISH={end_idx}")
                
                line_count = 0
                for line in f:
                    line_count += 1
                    # Skip empty lines
                    if not line.strip():
                        continue
                        
                    # Clean the line and split by delimiter
                    parts = [p.strip() for p in line.strip().split(delimiter)]
                    if len(parts) < 3:
                        self.logger.warning(f"Skipping line {line_count}: too few fields ({len(parts)})")
                        continue
                        
                    try:
                        # Extract field name and positions
                        name = parts[name_idx]
                        start = int(parts[start_idx]) - 1  # Convert to 0-based indexing
                        end = int(parts[end_idx])          # End position is exclusive
                        
                        fields.append({
                            'name': name,
                            'start': start,
                            'end': end
                        })
                    except (ValueError, IndexError) as e:
                        self.logger.warning(f"Error parsing line {line_count}: {str(e)}")
            
            self.logger.debug(f"Successfully parsed {len(fields)} fields from definition file")
            return fields
            
        except Exception as e:
            self.logger.error(f"Failed to parse definition file: {definition_path}")
            self.logger.error(f"Error details: {str(e)}")
            return []

    def _preprocess_configuration(self):
        """
        Preprocess configuration before generation to ensure all required files exist
        """
        self.logger.debug("Preprocessing configuration")
        
        # Check each table configuration
        for table_config in self.config.get('tables', []):
            table_name = table_config.get('name', 'unknown')
            
            # Check if fixed-width output is specified
            if table_config.get('output_type') == 'fixed_width':
                fixed_width_options = table_config.get('fixed_width_options', {})
                definition_path = fixed_width_options.get('definition_path')
                
                if not definition_path:
                    error_msg = f"Table '{table_name}': No definition_path specified for fixed-width output"
                    self.logger.error(error_msg)
                    raise ValueError(error_msg)
                    
                if not os.path.exists(definition_path):
                    error_msg = f"Table '{table_name}': Definition file not found: {definition_path}"
                    self.logger.error(error_msg)
                    raise FileNotFoundError(error_msg + 
                        ". The definition file must be created manually before generation.")
        
        self.logger.debug("Configuration preprocessing completed")
