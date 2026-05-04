# decoy_engine/generator/columns.py
"""
Column data generators for the decoy_engine package.
Provides various strategies for generating synthetic column data.
"""

import pandas as pd
import random
import hashlib
import time
from faker import Faker
from typing import Dict, Any, Optional, List, Callable

from decoy_engine.internal.helpers import deterministic_hash, get_faker_providers


class ColumnGenerator:
    """
    Generates data for columns based on configuration.
    Supports various column types and ensures consistent generation.
    """
    
    def __init__(self, seed: int = 42, logger=None):
        """
        Initialize with a seed for deterministic behavior
        
        Args:
            seed: Random seed for deterministic generation
            logger: Logger instance (optional)
        """
        self.seed = seed
        random.seed(self.seed)
        
        # Initialize faker
        self.faker = Faker()
        self.faker.seed_instance(self.seed)
        
        # Get all available faker providers
        self.faker_providers = get_faker_providers(self.faker)
        
        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger
            self.logger = get_logger()
        
        # Initialize generator functions
        self.generators = {
            'faker': self._generate_faker_column,
            'sequence': self._generate_sequence_column,
            'categorical': self._generate_categorical_column,
            'reference': self._generate_reference_column,
            'formula': self._generate_formula_column
        }
        
        self.logger.debug(f"Initialized ColumnGenerator with seed: {seed}")
    
    def generate_column(self, num_rows: int, column_config: Dict[str, Any], 
                    table_name: str, reference_data: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Generate data for a column based on its configuration
        
        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables
            
        Returns:
            pandas.Series with generated data
        """
        column_name = column_config.get('name', 'unnamed_column')
        data_type = column_config.get('type', 'faker')
        null_probability = column_config.get('null_probability', 0.0)
        
        start_time = time.time()
        
        # First, generate the base data without nulls
        if data_type in self.generators:
            generator_func = self.generators[data_type]
            result = generator_func(num_rows, column_config, table_name, reference_data)
        else:
            self.logger.warning(f"Unsupported column type: {data_type}, defaulting to faker 'word'")
            # Default to faker word generator
            result = pd.Series([self.faker.word() for _ in range(num_rows)])
        
        # Apply null probability if specified
        if null_probability > 0:
            self.logger.debug(f"Applying null probability {null_probability} to column '{column_name}'")
            
            # Create a mask for which values should be null
            # Use the same seed-based approach for deterministic behavior
            for i in range(num_rows):
                # Set a row-specific seed for reproducibility
                row_seed = self.seed + i + hash(column_name)
                random.seed(row_seed)
                
                # Apply null probability
                if random.random() < null_probability:
                    result.iloc[i] = None
        
        # Log generation time
        generation_time = time.time() - start_time
        self.logger.debug(f"Generated column '{column_name}' of type '{data_type}' in {generation_time:.2f} seconds")
        
        # Log null statistics if null_probability was applied
        if null_probability > 0:
            null_count = result.isna().sum()
            null_percentage = (null_count / num_rows) * 100
            self.logger.debug(f"Applied null probability: {null_count}/{num_rows} values are null ({null_percentage:.1f}%)")
        
        return result
    
    def _generate_faker_column(self, num_rows: int, column_config: Dict[str, Any], 
                              table_name: str, reference_data: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Generate data using Faker
        
        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables
            
        Returns:
            pandas.Series with generated data
        """
        faker_type = column_config.get('faker_type', 'word')
        
        self.logger.debug(f"Generating faker column with type: {faker_type}")
        
        # Create function to generate single value
        if faker_type in self.faker_providers:
            provider_func = self.faker_providers[faker_type]
        else:
            self.logger.warning(f"Unknown faker_type '{faker_type}', using 'word' instead")
            provider_func = self.faker_providers['word']
        
        # Generate values for all rows
        values = []
        for i in range(num_rows):
            # Set a row-specific seed for reproducibility
            row_seed = self.seed + i
            random.seed(row_seed)
            self.faker.seed_instance(row_seed)
            
            # Generate the value
            values.append(provider_func())
        
        return pd.Series(values)
    
    def _generate_sequence_column(self, num_rows: int, column_config: Dict[str, Any], 
                                 table_name: str, reference_data: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Generate sequential data (e.g., IDs)
        
        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables
            
        Returns:
            pandas.Series with generated data
        """
        start = column_config.get('start', 1)
        step = column_config.get('step', 1)
        prefix = column_config.get('prefix', '')
        suffix = column_config.get('suffix', '')
        pad_length = column_config.get('pad_length', 0)
        
        self.logger.debug(f"Generating sequence column with start={start}, step={step}")
        
        values = []
        for i in range(num_rows):
            value = start + (i * step)
            
            # Apply padding if specified
            if pad_length > 0:
                value_str = str(value).zfill(pad_length)
            else:
                value_str = str(value)
                
            # Apply prefix and suffix
            formatted_value = f"{prefix}{value_str}{suffix}"
            values.append(formatted_value)
            
        return pd.Series(values)
    
    def _generate_categorical_column(self, num_rows: int, column_config: Dict[str, Any], 
                                    table_name: str, reference_data: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Generate data from a set of categories with specified probabilities
        
        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables
            
        Returns:
            pandas.Series with generated data
        """
        categories = column_config.get('categories', ['Category A', 'Category B'])
        weights = column_config.get('weights', None)  # Optional probability weights
        
        self.logger.debug(f"Generating categorical column with {len(categories)} categories")
        
        # Generate values with weighted random choices
        values = random.choices(categories, weights=weights, k=num_rows)
        return pd.Series(values)
    
    def _generate_reference_column(self, num_rows: int, column_config: Dict[str, Any], 
                              table_name: str, reference_data: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Generate data that references values from another table or column
        
        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables
            
        Returns:
            pandas.Series with generated data
        """
        reference_table = column_config.get('reference_table')
        reference_column = column_config.get('reference_column')
        distribution = column_config.get('distribution', 'random')  # random, sequential, weighted
        # Note: null_probability is now handled at the column level, not here
        
        self.logger.debug(f"Generating reference column referencing {reference_table}.{reference_column}")
        
        # Check if reference table exists
        if reference_table not in reference_data:
            self.logger.warning(f"Reference table '{reference_table}' not found. Returning placeholder values.")
            return pd.Series([f"REF_TABLE_NOT_FOUND_{i}" for i in range(num_rows)])
        
        # Get reference DataFrame
        ref_df = reference_data[reference_table]
        
        # Check if reference column exists
        if reference_column not in ref_df.columns:
            self.logger.warning(f"Reference column '{reference_column}' not found in table '{reference_table}'. Returning placeholder values.")
            return pd.Series([f"REF_COLUMN_NOT_FOUND_{i}" for i in range(num_rows)])
        
        # Get reference values
        ref_values = ref_df[reference_column].dropna().unique().tolist()
        
        if not ref_values:
            self.logger.warning(f"No reference values found in {reference_table}.{reference_column}. Returning NULL values.")
            return pd.Series([None] * num_rows)
        
        # Generate references based on distribution type
        values = []
        for i in range(num_rows):
            # Note: null_probability is now handled at the column level
            if distribution == 'random':
                # Random selection with replacement
                values.append(random.choice(ref_values))
                
            elif distribution == 'sequential':
                # Cycle through values sequentially
                values.append(ref_values[i % len(ref_values)])
                
            elif distribution == 'weighted':
                # If weights are provided, use them
                weights = column_config.get('weights')
                if not weights or len(weights) != len(ref_values):
                    # Default to equal weights
                    weights = None
                values.append(random.choices(ref_values, weights=weights, k=1)[0])
                
            else:
                self.logger.warning(f"Unknown distribution type: {distribution}, using random")
                values.append(random.choice(ref_values))
                
        return pd.Series(values)
    
    def _generate_formula_column(self, num_rows: int, column_config: Dict[str, Any], 
                               table_name: str, reference_data: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Generate data based on a formula
        
        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables
            
        Returns:
            pandas.Series with generated data
        """
        formula_type = column_config.get('formula_type', 'basic')
        formula = column_config.get('formula', '')
        
        self.logger.debug(f"Generating formula column with type: {formula_type}")
        
        # Check if formula is provided
        if not formula:
            self.logger.warning("No formula provided in configuration")
            return pd.Series([None] * num_rows)
        
        try:
            if formula_type == 'basic':
                # Basic arithmetic or string operations using eval
                return self._generate_basic_formula(num_rows, formula)
                
            elif formula_type == 'template':
                # String template with Faker placeholders
                return self._generate_template_formula(num_rows, formula)
                
            elif formula_type == 'composite':
                # Composite formulas are processed after row generation because they
                # may depend on other columns in the same row.
                return pd.Series([None] * num_rows, dtype=object)
                
            else:
                self.logger.warning(f"Unsupported formula type: {formula_type}")
                return pd.Series([f"UNSUPPORTED_FORMULA_TYPE_{formula_type}"] * num_rows)
                
        except Exception as e:
            self.logger.error(f"Error in formula column generation: {str(e)}")
            self.logger.error(f"Formula: {formula}")
            self.logger.error(f"Formula type: {formula_type}")
            return pd.Series([f"FORMULA_ERROR"] * num_rows)
    
    def _generate_basic_formula(self, num_rows: int, formula: str) -> pd.Series:
        """
        Generate data using a basic arithmetic or string formula
        
        Args:
            num_rows: Number of rows to generate
            formula: Formula to evaluate
            
        Returns:
            pandas.Series with generated data
        """
        # Basic arithmetic or string operations
        values = []
        for i in range(num_rows):
            # Replace index placeholder if present
            formula_with_index = formula.replace('{i}', str(i))
            formula_with_index = formula_with_index.replace('{index}', str(i))
            
            # Add random seed if needed for reproducibility 
            # but with variation per row
            local_seed = self.seed + i
            random.seed(local_seed)
            self.faker.seed_instance(local_seed)
            
            # Define safe functions for formula evaluation
            safe_globals = {
                # Basic utility functions
                'random': random.random,
                'randint': lambda a, b: random.randint(a, b),
                'choice': lambda lst: random.choice(lst),
                'round': round,
                'min': min,
                'max': max,
                'str': str,
                'int': int,
                'float': float,
                'hash': lambda x: deterministic_hash(str(x), local_seed)[:8],  # Short hash
                
                # Date and time functions
                'date_between': self.faker.date_between,
                'date_this_decade': self.faker.date_this_decade,
                'date_this_year': self.faker.date_this_year,
                'date_this_month': self.faker.date_this_month,
                'future_date': self.faker.future_date,
                'past_date': self.faker.past_date,
                'date_of_birth': self.faker.date_of_birth,
                'time': lambda: self.faker.time(),
                'now': lambda fmt='%Y-%m-%d': pd.Timestamp.now().strftime(fmt),
                'today': lambda fmt='%Y-%m-%d': pd.Timestamp.today().strftime(fmt),
                
                # Date arithmetic helpers
                'days_from_now': lambda days: (pd.Timestamp.now() + pd.Timedelta(days=days)).strftime('%Y-%m-%d'),
                'months_from_now': lambda months: (pd.Timestamp.now() + pd.DateOffset(months=months)).strftime('%Y-%m-%d'),
                'years_from_now': lambda years: (pd.Timestamp.now() + pd.DateOffset(years=years)).strftime('%Y-%m-%d'),
                
                # Format helpers
                'format_date': lambda date_obj, fmt='%Y-%m-%d': date_obj.strftime(fmt) if hasattr(date_obj, 'strftime') else str(date_obj)
            }
            
            try:
                # Safely evaluate the formula
                result = eval(formula_with_index, {"__builtins__": {}}, safe_globals)
                values.append(result)
            except Exception as e:
                error_msg = str(e)
                if "not defined" in error_msg:
                    # Provide more helpful error message for undefined functions
                    self.logger.warning(f"Function not available in formula for row {i}: {error_msg}")
                    self.logger.info(f"Available functions: {sorted(list(safe_globals.keys()))}")
                else:
                    self.logger.warning(f"Error evaluating formula for row {i}: {error_msg}")
                    
                self.logger.debug(f"Formula: {formula_with_index}")
                values.append(None)
                    
        return pd.Series(values)
    
    def _generate_template_formula(self, num_rows: int, formula: str) -> pd.Series:
        """
        Generate data using a string template with Faker placeholders
        
        Args:
            num_rows: Number of rows to generate
            formula: Template formula with placeholders
            
        Returns:
            pandas.Series with generated data
        """
        # String template with placeholders
        faker_providers = self.faker_providers
        values = []
        
        for i in range(num_rows):
            # Set seed for this specific row for reproducibility
            row_seed = self.seed + i
            random.seed(row_seed)
            self.faker.seed_instance(row_seed)
            
            # Create a context with base utility values
            context = {
                'i': i,
                'index': i,
                'random': random.random(),  # Call the function for a value
                'randint': lambda a, b: random.randint(a, b),  # Keep as lambda
                'hash': lambda x: deterministic_hash(str(x), row_seed)[:8],
            }
            
            # Pre-populate the context with actual faker values
            for key, provider_func in faker_providers.items():
                try:
                    context[key] = provider_func()
                except Exception as e:
                    self.logger.debug(f"Error generating faker value for {key}: {str(e)}")
                    context[key] = f"ERROR_{key}"
            
            try:
                # Use f-string evaluation to support expressions like {first_name.lower()}
                safe_locals = {
                    'str': str,
                    'int': int,
                    'float': float,
                    'round': round,
                    'min': min,
                    'max': max,
                    'len': len,
                    'hash': lambda x: deterministic_hash(str(x), row_seed)[:8],
                    'random': random.random,
                    'randint': lambda a, b: random.randint(a, b),
                }
                safe_locals.update(context)
                result = eval(f"f'''{formula}'''", {"__builtins__": None}, safe_locals)
                values.append(result)
            except NameError as e:
                missing_key = str(e).split("'")[1] if "'" in str(e) else str(e)
                self.logger.warning(f"Template key '{missing_key}' not found in row {i}")
                self.logger.debug(f"Available keys: {list(context.keys())}")
                values.append(f"Missing key: {missing_key}")
            except Exception as e:
                self.logger.warning(f"Error formatting template for row {i}: {str(e)}")
                self.logger.debug(f"Template: {formula}")
                values.append("TEMPLATE_ERROR")
                
        return pd.Series(values)