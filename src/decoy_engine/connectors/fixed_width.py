# decoy_engine/io/fixed_width.py
"""
Fixed-width file I/O functionality for the decoy_engine package.
"""

import os
from typing import Any

import pandas as pd

from decoy_engine.connectors.base import IOHandler
from decoy_engine.internal.helpers import create_directory_for_file


class FixedWidthHandler(IOHandler):
    """
    I/O handler for fixed-width files.
    Handles loading and saving data from fixed-width format.
    """
    def __init__(self, input_config: dict[str, Any], output_config: dict[str, Any], config_or_logger=None, logger=None):
        """
        Initialize with input and output configurations
        
        Args:
            input_config: Dictionary with input configuration
            output_config: Dictionary with output configuration
            config_or_logger: Full configuration dictionary or logger instance
            logger: Logger instance (optional)
        """
        # Handle the case where config_or_logger is actually a logger
        if hasattr(config_or_logger, 'debug') and callable(config_or_logger.debug):
            # config_or_logger is a logger
            super().__init__(input_config, output_config, config_or_logger)
            self.config = {}  # Empty config
        else:
            # config_or_logger is a config dict or None
            super().__init__(input_config, output_config, logger)
            self.config = config_or_logger or {}  # Store full config for access to masking rules
    
    def load_data(self) -> pd.DataFrame:
        """
        Load data from a fixed-width file using field definitions
        
        Returns:
            pandas.DataFrame: The loaded data with column headers
        """
        input_path = self.input_config['path']
        definition_path = self.input_config.get('definition_path')
        encoding = self.input_config.get('fixed_width_options', {}).get('encoding', 'utf-8')
        
        self.logger.info(f"Loading fixed-width data from: {input_path}")
        self.logger.debug(f"Using definition file: {definition_path}")
        self.logger.debug(f"Encoding: {encoding}")
        
        # Parse field definitions
        self.logger.debug("Parsing field definition file")
        fields = self._parse_definition_file(definition_path)
        column_names = [field['name'] for field in fields]  # Include all fields
        self.logger.debug(f"Found {len(fields)} field definitions: {', '.join(column_names)}")
        
        try:
            # Read fixed-width file into DataFrame
            df = pd.DataFrame(columns=column_names)
            
            # For large files, we'll process line by line
            self.logger.debug("Reading fixed-width file line by line")
            with open(input_path, encoding=encoding) as f:
                rows = []
                line_count = 0
                for line in f:
                    line_count += 1
                    # Extract all fields
                    row = {}
                    for field in fields:
                        value = line[field['start']:field['end']].strip()
                        row[field['name']] = value
                    rows.append(row)
                    
                    # Process in chunks to avoid memory issues
                    if len(rows) >= 10000:
                        self.logger.debug(f"Processed {line_count} lines so far")
                        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
                        rows = []
                
                # Add any remaining rows
                if rows:
                    df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
            
            self.logger.info(f"Successfully loaded {len(df)} rows with {len(df.columns)} columns")
            return df
            
        except Exception as e:
            self.logger.error(f"Failed to load fixed-width file: {input_path}")
            self.logger.error(f"Error details: {e!s}")
            raise ValueError(f"Failed to load fixed-width file: {e}")
    
    def save_data(self, df: pd.DataFrame) -> None:
        """
        Save the masked data to the output format
        
        Args:
            df: The pandas DataFrame to save
        """
        output_type = self.output_config.get('type', 'csv')
        self.logger.info(f"Saving masked data as {output_type}")
        
        if output_type == 'csv':
            # Reuse CSVHandler's save_data functionality
            self.logger.debug("Using CSVHandler to save as CSV")
            from decoy_engine.connectors.csv_connector import CSVHandler
            csv_handler = CSVHandler(None, self.output_config, self.logger)
            csv_handler.save_data(df)
        elif output_type == 'fixed_width':
            # Save as fixed-width format
            self.logger.debug("Saving as fixed-width format")
            self._save_as_fixed_width(df)
        else:
            error_msg = f"Unsupported output type for fixed-width handler: {output_type}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
    
    def _parse_definition_file(self, definition_path: str) -> list[dict[str, Any]]:
        """
        Parse the fixed width definition file.
        
        Args:
            definition_path: Path to the definition file with field definitions
                
        Returns:
            List of field dictionaries with name, start, and end positions
        """
        fields = []
        
        # Get delimiter from config or default to comma
        delimiter = self.input_config.get('fixed_width_options', {}).get('definition_delimiter', ',')
        encoding = self.input_config.get('fixed_width_options', {}).get('encoding', 'utf-8')
        
        self.logger.debug(f"Parsing definition file: {definition_path}")
        self.logger.debug(f"Definition delimiter: '{delimiter}', encoding: '{encoding}'")
        
        try:
            with open(definition_path, encoding=encoding) as f:
                # Skip header line and determine column positions
                header = next(f).strip().split(delimiter)
                header = [h.strip() for h in header]  # Clean whitespace
                self.logger.debug(f"Definition file header: {header}")
                
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
                        start = int(parts[start_idx]) - 1  # 0-based indexing
                        end = int(parts[end_idx])          # End position exclusive
                        
                        fields.append({
                            'name': name,
                            'start': start,
                            'end': end
                        })
                        self.logger.debug(f"Added field: {name} (positions {start+1}-{end})")
                    except (ValueError, IndexError) as e:
                        self.logger.warning(f"Error parsing line {line_count}: {e!s}")
                        self.logger.warning(f"Line content: {line.strip()}")
                
            self.logger.info(f"Successfully parsed {len(fields)} fields from definition file")
            return fields
            
        except Exception as e:
            self.logger.error(f"Failed to parse definition file: {definition_path}")
            self.logger.error(f"Error details: {e!s}")
            raise
    
    def _save_as_fixed_width(self, df: pd.DataFrame) -> None:
        """
        Save the data to a fixed-width file
        
        Args:
            df: The pandas DataFrame to save
        """
        output_path = self.output_config['path']
        self.logger.info(f"Saving data as fixed-width to: {output_path}")
        
        # Create directory if it doesn't exist
        create_directory_for_file(output_path)
        self.logger.debug(f"Ensured output directory exists: {os.path.dirname(output_path)}")
        
        # Get output options
        encoding = self.output_config.get('fixed_width_options', {}).get('encoding', 'utf-8')
        
        # Determine field definitions
        fields = self._get_output_field_definitions()
        if not fields:
            self.logger.error("No field definitions available for fixed-width output")
            raise ValueError("No field definitions available for fixed-width output")
        
        # Write definition file if specified
        output_def_path = self.output_config.get('definition_path')
        if output_def_path:
            self._write_definition_file(fields, output_def_path, encoding)
        
        # Format each row according to field definitions
        self.logger.debug("Formatting data as fixed-width")
        with open(output_path, 'w', encoding=encoding) as f:
            for _, row in df.iterrows():
                line = self._format_fixed_width_line(row, fields)
                f.write(line + '\n')
        
        self.logger.info(f"Successfully saved {len(df)} rows to {output_path}")
    
    def _get_output_field_definitions(self) -> list[dict[str, Any]]:
        """
        Get field definitions for output, either from input or specified output definition
        
        Returns:
            List of field dictionaries with name, start, and end positions
        """
        # First check if definition_path is at the top level of output_config
        if 'definition_path' in self.output_config:
            output_def_path = self.output_config['definition_path']
            if os.path.exists(output_def_path):
                self.logger.debug(f"Using specified output definition file: {output_def_path}")
                return self._parse_definition_file(output_def_path)
        
        # Then check if definition_path is inside fixed_width_options
        if 'fixed_width_options' in self.output_config and 'definition_path' in self.output_config['fixed_width_options']:
            output_def_path = self.output_config['fixed_width_options']['definition_path']
            if os.path.exists(output_def_path):
                self.logger.debug(f"Using specified output definition file from fixed_width_options: {output_def_path}")
                return self._parse_definition_file(output_def_path)
        
        # Otherwise, check input_config at top level
        if 'definition_path' in self.input_config:
            input_def_path = self.input_config['definition_path']
            if os.path.exists(input_def_path):
                self.logger.debug(f"Using input definition file for output: {input_def_path}")
                return self._parse_definition_file(input_def_path)
        
        # And finally check inside fixed_width_options in input_config
        if 'fixed_width_options' in self.input_config and 'definition_path' in self.input_config['fixed_width_options']:
            input_def_path = self.input_config['fixed_width_options']['definition_path']
            if os.path.exists(input_def_path):
                self.logger.debug(f"Using input definition file from fixed_width_options for output: {input_def_path}")
                return self._parse_definition_file(input_def_path)
        
        self.logger.warning("No definition file found for fixed-width output")
        return None
    
    def _format_fixed_width_line(self, row: pd.Series, fields: list[dict[str, Any]]) -> str:
        """
        Format a single data row as a fixed-width line with custom padding
        
        Args:
            row: pandas Series (single row of data)
            fields: List of field dictionaries with name, start, and end positions
                
        Returns:
            Formatted fixed-width line
        """
        # Create an empty line filled with spaces
        max_position = max(field['end'] for field in fields)
        line = ' ' * max_position
        
        # Get global padding settings
        global_padding_char = self.output_config.get('fixed_width_options', {}).get('padding_char', ' ')
        global_alignment = self.output_config.get('fixed_width_options', {}).get('padding_alignment', 'auto')
        
        # Fill in each field
        for field in fields:
            name = field['name']
            start = field['start']
            end = field['end']
            width = end - start
            
            # Get field-specific padding settings from masking rules if available
            field_padding_char = global_padding_char
            field_alignment = global_alignment
            
            # Look for field-specific settings in masking rules
            if 'masking_rules' in self.config:
                for rule in self.config['masking_rules']:
                    if rule.get('column') == name and 'fixed_width_options' in rule:
                        field_options = rule['fixed_width_options']
                        if 'padding_char' in field_options:
                            field_padding_char = field_options['padding_char']
                        if 'padding_alignment' in field_options:
                            field_alignment = field_options['padding_alignment']
                        break
            
            # Get value from row, convert to string
            value = row.get(name, '')
            if value is None:
                value = ''
            value_str = str(value)
            
            # Truncate if the value exceeds field width
            if len(value_str) > width:
                self.logger.warning(f"Value for field '{name}' exceeds defined width. Truncating: '{value_str}'")
                formatted_value = value_str[:width]
            else:
                # Determine alignment
                if field_alignment == 'auto':
                    # Auto alignment based on value type (right for numeric, left for others)
                    try:
                        # Check if the value can be converted to a number
                        float(value)
                        formatted_value = value_str.rjust(width, field_padding_char)
                    except (ValueError, TypeError):
                        # Non-numeric value, left-justify
                        formatted_value = value_str.ljust(width, field_padding_char)
                elif field_alignment == 'right':
                    formatted_value = value_str.rjust(width, field_padding_char)
                else:  # 'left' alignment
                    formatted_value = value_str.ljust(width, field_padding_char)
            
            # Insert into the line
            line = line[:start] + formatted_value + line[end:]
        
        return line
    
    def _write_definition_file(self, fields: list[dict[str, Any]], output_def_path: str, encoding: str = 'utf-8') -> None:
        """
        Write field definitions to a definition file
        
        Args:
            fields: List of field dictionaries
            output_def_path: Path to write the definition file
            encoding: File encoding to use
        """
        self.logger.info(f"Writing fixed-width definition file to: {output_def_path}")
        
        # Create directory if it doesn't exist
        create_directory_for_file(output_def_path)
        
        # Get delimiter from config or default to comma
        delimiter = self.output_config.get('fixed_width_options', {}).get('definition_delimiter', ',')
        
        with open(output_def_path, 'w', encoding=encoding) as f:
            # Write header
            f.write(f"FIELD{delimiter}START{delimiter}FINISH\n")
            
            # Write each field definition
            for field in fields:
                # Convert to 1-based indexing for human readability
                start_pos = field['start'] + 1
                end_pos = field['end']
                f.write(f"{field['name']}{delimiter}{start_pos}{delimiter}{end_pos}\n")
        
        self.logger.info(f"Successfully wrote definition file with {len(fields)} fields")
    
    def load_sample(self, sample_rows: int = 5) -> pd.DataFrame:
        """
        Load a sample of rows from the fixed-width file to get schema
        
        Args:
            sample_rows: Number of rows to load
            
        Returns:
            pandas.DataFrame with the sample rows
        """
        input_path = self.input_config['path']
        definition_path = self.input_config.get('definition_path')
        encoding = self.input_config.get('fixed_width_options', {}).get('encoding', 'utf-8')
        
        self.logger.debug(f"Loading {sample_rows} sample rows from: {input_path}")
        
        # Parse field definitions
        fields = self._parse_definition_file(definition_path)
        column_names = [field['name'] for field in fields]
        
        try:
            # Create empty DataFrame with correct columns
            df_sample = pd.DataFrame(columns=column_names)
            
            # Read sample rows
            with open(input_path, encoding=encoding) as f:
                for i, line in enumerate(f):
                    if i >= sample_rows:
                        break
                        
                    # Extract fields
                    row = {}
                    for field in fields:
                        value = line[field['start']:field['end']].strip()
                        row[field['name']] = value
                        
                    # Add to DataFrame
                    df_sample = pd.concat([df_sample, pd.DataFrame([row])], ignore_index=True)
            
            self.logger.debug(f"Loaded sample with {len(df_sample)} rows and {len(df_sample.columns)} columns")
            return df_sample
            
        except Exception as e:
            self.logger.error(f"Failed to load sample from fixed-width file: {input_path}")
            self.logger.error(f"Error details: {e!s}")
            raise ValueError(f"Failed to load sample from fixed-width file: {e}")
        
    def set_column_configurations(self, column_configs: list[dict[str, Any]]) -> None:
        """
        Set column configurations for padding and formatting purposes
        
        Args:
            column_configs: List of column configuration dictionaries
        """
        self.column_configs = column_configs
        self.logger.debug(f"Set column configurations for {len(column_configs)} columns")

    def _get_column_padding_settings(self, column_name: str) -> dict[str, str]:
        """
        Get padding settings for a specific column, checking multiple sources
        
        Args:
            column_name: Name of the column
            
        Returns:
            Dictionary with padding_char and padding_alignment
        """
        # Default settings
        default_padding_char = ' '
        default_alignment = 'left'
        
        # Get global settings from output config
        global_options = self.output_config.get('fixed_width_options', {})
        global_padding_char = global_options.get('padding_char', default_padding_char)
        global_alignment = global_options.get('padding_alignment', default_alignment)
        
        # Check for column-specific settings in generator column configurations
        if hasattr(self, 'column_configs') and self.column_configs:
            for col_config in self.column_configs:
                if col_config.get('name') == column_name:
                    col_options = col_config.get('fixed_width_options', {})
                    return {
                        'padding_char': col_options.get('padding_char', global_padding_char),
                        'padding_alignment': col_options.get('padding_alignment', global_alignment)
                    }
        
        # Check for settings in masking rules (for masking operations)
        if 'masking_rules' in self.config:
            for rule in self.config['masking_rules']:
                if rule.get('column') == column_name and 'fixed_width_options' in rule:
                    rule_options = rule['fixed_width_options']
                    return {
                        'padding_char': rule_options.get('padding_char', global_padding_char),
                        'padding_alignment': rule_options.get('padding_alignment', global_alignment)
                    }
        
        # Return global settings
        return {
            'padding_char': global_padding_char,
            'padding_alignment': global_alignment
        }

    def _format_fixed_width_line(self, row: pd.Series, fields: list[dict[str, Any]]) -> str:
        """
        Format a single data row as a fixed-width line with custom padding
        
        Args:
            row: pandas Series (single row of data)
            fields: List of field dictionaries with name, start, and end positions
                
        Returns:
            Formatted fixed-width line
        """
        # Create an empty line filled with spaces
        max_position = max(field['end'] for field in fields)
        line = ' ' * max_position
        
        # Fill in each field
        for field in fields:
            name = field['name']
            start = field['start']
            end = field['end']
            width = end - start
            
            # Get column-specific padding settings
            padding_settings = self._get_column_padding_settings(name)
            field_padding_char = padding_settings['padding_char']
            field_alignment = padding_settings['padding_alignment']
            
            # Get value from row, convert to string
            value = row.get(name, '')
            if value is None:
                value = ''
            value_str = str(value)
            
            # Truncate if the value exceeds field width
            if len(value_str) > width:
                self.logger.warning(f"Value for field '{name}' exceeds defined width. Truncating: '{value_str}'")
                formatted_value = value_str[:width]
            else:
                # Determine alignment
                if field_alignment == 'auto':
                    # Auto alignment based on value type (right for numeric, left for others)
                    try:
                        # Check if the value can be converted to a number
                        float(value)
                        formatted_value = value_str.rjust(width, field_padding_char)
                    except (ValueError, TypeError):
                        # Non-numeric value, left-justify
                        formatted_value = value_str.ljust(width, field_padding_char)
                elif field_alignment == 'right':
                    formatted_value = value_str.rjust(width, field_padding_char)
                else:  # 'left' alignment
                    formatted_value = value_str.ljust(width, field_padding_char)
            
            # Insert into the line
            line = line[:start] + formatted_value + line[end:]
        
        return line
