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

from decoy_engine.internal.helpers import (
    deterministic_hash,
    get_faker_providers,
    make_faker,
)


class ColumnGenerator:
    """
    Generates data for columns based on configuration.
    Supports various column types and ensures consistent generation.
    """
    
    def __init__(self, seed: int = 42, logger=None, derive_key=None):
        """
        Initialize with a seed for deterministic behavior

        Args:
            seed: Random seed for deterministic generation when no key is set
            logger: Logger instance (optional)
            derive_key: Optional callable ``(info: str) -> bytes`` returning at
                least 4 bytes of HKDF-derived material. When provided, per-
                column seeds come from ``derive_key("col:<name>")`` instead of
                ``seed + hash(name)`` — same key + same column always yields
                the same bytes across runs and across instances. When None,
                generation is reproducible by ``seed`` alone but ignores any
                pipeline / instance master key (i.e. random-by-policy).
        """
        self.seed = seed
        self.derive_key = derive_key
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
        
        self.logger.debug(
            f"Initialized ColumnGenerator with seed: {seed}, "
            f"keyed: {self.derive_key is not None}"
        )

    def _column_seed(self, column_name: str) -> int:
        """Per-column base seed used by every row-level seeding site.

        When ``derive_key`` is set, take the first 4 bytes of
        ``derive_key("col:<name>")`` and decode as a 32-bit int — same key +
        same column always yields the same seed bytes, so faker / random
        output is bitwise stable across runs. Otherwise fall back to the
        legacy ``seed + hash(name)`` formula so behavior is unchanged for
        callers that don't pass a key.
        """
        if self.derive_key is not None:
            try:
                key_bytes = self.derive_key(f"col:{column_name}")
                return int.from_bytes(key_bytes[:4], "big", signed=False)
            except Exception:
                # Fall through to seed-based path on any resolver hiccup;
                # better to produce *some* output than crash a generation run.
                pass
        return (self.seed + hash(column_name)) & 0x7FFFFFFF
    
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

            # Per-row seeding off the column-seed (HKDF-derived when keyed,
            # `seed + hash(name)` otherwise). Same column + same row →
            # same null/non-null decision across runs.
            column_seed = self._column_seed(column_name)
            for i in range(num_rows):
                random.seed(column_seed + i)
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
        locale = column_config.get('locale')

        self.logger.debug(
            f"Generating faker column with type: {faker_type}, locale: {locale!r}"
        )

        # Use the shared seeded Faker for the common (no-locale) path; build
        # a fresh instance when the column overrides locale so en_GB / de_DE
        # / etc. produce locale-correct addresses, names, phone numbers.
        # Provider list is rebuilt off the active instance because some
        # providers (e.g. `state_abbr`) raise on locales that don't define
        # them — falling back to the default-locale provider would silently
        # leak en_US output.
        if locale:
            faker_inst = make_faker(locale)
            providers = get_faker_providers(faker_inst)
        else:
            faker_inst = self.faker
            providers = self.faker_providers

        if faker_type in providers:
            provider_func = providers[faker_type]
        else:
            self.logger.warning(f"Unknown faker_type '{faker_type}', using 'word' instead")
            provider_func = providers['word']

        # Generate values for all rows. When `derive_key` is set, the
        # column-seed is HKDF-derived from the pipeline key, so the same
        # key + same column always yields the same bytes across runs.
        column_name = column_config.get('name', 'unnamed_column')
        column_seed = self._column_seed(column_name)
        values = []
        for i in range(num_rows):
            row_seed = column_seed + i
            random.seed(row_seed)
            faker_inst.seed_instance(row_seed)
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

        # Reseed from the column-specific seed so the choices are stable
        # across runs when a key is provided, and stable per-column even
        # without one (otherwise output depends on the order of column
        # generation calls — order-dependence is a footgun).
        column_name = column_config.get('name', 'unnamed_column')
        random.seed(self._column_seed(column_name))
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
        Generate data based on a formula.

        Single inline path: every formula is a Python expression (write
        ``f"..."`` yourself if you want template-like substitution). Drops
        the previous three-way dispatch (basic / template / composite).

        When ``references: [...]`` is set on the column config, this method
        emits a None-filled placeholder series — the column's actual values
        are filled by ``DataGenerator._process_referenced_formulas`` AFTER
        every other column has been generated, so the formula can read its
        siblings. When ``references`` is empty/missing, the formula is
        evaluated inline per row with deterministic seeding.

        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables

        Returns:
            pandas.Series with generated data (or None placeholders when
            the column has cross-column references — filled in post-pass).
        """
        formula = column_config.get('formula', '')
        column_name = column_config.get('name', 'unnamed_column')
        references = column_config.get('references', []) or []

        if not formula:
            self.logger.warning("No formula provided in configuration")
            return pd.Series([None] * num_rows)

        if references:
            # Defer to the post-pass: this column reads sibling columns,
            # which haven't been generated yet during the per-column loop.
            self.logger.debug(
                f"Formula column '{column_name}' references {references} — "
                f"deferring to post-pass."
            )
            return pd.Series([None] * num_rows, dtype=object)

        return self._eval_formula_inline(num_rows, formula, column_name)

    def _eval_formula_inline(
        self, num_rows: int, formula: str, column_name: str = 'unnamed_column',
    ) -> pd.Series:
        """Per-row eval of a Python expression. Same deterministic seeding
        as the legacy ``basic`` path: ``column_seed + row_index`` reseeds
        ``random`` and the Faker instance per row.

        Scope per row:
          - ``i`` / ``index`` — row number
          - ``random`` / ``randint`` / ``choice`` — RNG (deterministic per row)
          - ``hash`` — short deterministic hash
          - ``str`` / ``int`` / ``float`` / ``round`` / ``min`` / ``max`` / ``len``
          - Faker date helpers + arithmetic (``today``, ``days_from_now``, …)

        Cross-column refs aren't reachable here — that's the post-pass."""
        column_seed = self._column_seed(column_name)
        values = []
        for i in range(num_rows):
            local_seed = column_seed + i
            random.seed(local_seed)
            self.faker.seed_instance(local_seed)

            scope = self._formula_scope(local_seed)
            scope['i'] = i
            scope['index'] = i

            try:
                result = eval(formula, {"__builtins__": {}}, scope)
                values.append(result)
            except Exception as e:
                error_msg = str(e)
                if "not defined" in error_msg:
                    self.logger.warning(
                        f"Name not available in formula for row {i}: {error_msg}"
                    )
                    self.logger.info(
                        f"Available names: {sorted(list(scope.keys()))}"
                    )
                else:
                    self.logger.warning(
                        f"Error evaluating formula for row {i}: {error_msg}"
                    )
                self.logger.debug(f"Formula: {formula}")
                values.append(None)

        return pd.Series(values)

    def _formula_scope(self, local_seed: int) -> Dict[str, Any]:
        """Build the names available inside a formula eval. Shared between
        the inline path here and the post-pass in
        ``DataGenerator._process_referenced_formulas`` so users get the
        same vocabulary regardless of whether their formula reads other
        columns. Per-row seed is captured into the closure so RNG calls
        within the eval stay deterministic."""
        return {
            # RNG (already reseeded by the caller before each row)
            'random': random.random,
            'randint': lambda a, b: random.randint(a, b),
            'choice': lambda lst: random.choice(lst),
            # Numeric / string utilities
            'round': round,
            'min': min,
            'max': max,
            'len': len,
            'str': str,
            'int': int,
            'float': float,
            'hash': lambda x: deterministic_hash(str(x), local_seed)[:8],
            # Faker date helpers
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
            'days_from_now': lambda days: (pd.Timestamp.now() + pd.Timedelta(days=days)).strftime('%Y-%m-%d'),
            'months_from_now': lambda months: (pd.Timestamp.now() + pd.DateOffset(months=months)).strftime('%Y-%m-%d'),
            'years_from_now': lambda years: (pd.Timestamp.now() + pd.DateOffset(years=years)).strftime('%Y-%m-%d'),
            'format_date': lambda date_obj, fmt='%Y-%m-%d': date_obj.strftime(fmt) if hasattr(date_obj, 'strftime') else str(date_obj),
        }