# decoy_engine/generator/columns.py
"""
Column data generators for the decoy_engine package.
Provides various strategies for generating synthetic column data.
"""

import os
import pandas as pd
import random
import hashlib
import time
from faker import Faker
from typing import Dict, Any, Optional, List, Callable

from decoy_engine.expressions import BASE_GLOBALS, safe_eval
from decoy_engine.internal.helpers import (
    deterministic_hash,
    get_faker_providers,
    make_faker,
)
from decoy_engine.generators.derivation import (
    strategy_config_fingerprint,
    synthetic_column_seed,
)


class ColumnGenerator:
    """
    Generates data for columns based on configuration.
    Supports various column types and ensures consistent generation.
    """
    
    def __init__(self, seed: int = 42, logger=None, derive_key=None,
                 instance_default_locale: str | None = None):
        """
        Initialize with a seed for deterministic behavior

        Args:
            seed: Random seed for deterministic generation when no key is set
            logger: Logger instance (optional)
            derive_key: Optional callable ``(info: str) -> bytes`` returning at
                least 4 bytes of HKDF-derived material. When provided, per-
                column seeds come from ``derive_key("col:<name>")`` instead of
                ``seed + hash(name)`` -- same key + same column always yields
                the same bytes across runs and across instances. When None,
                generation is reproducible by ``seed`` alone but ignores any
                pipeline / instance master key (i.e. random-by-policy).
            instance_default_locale: Optional locale code (e.g. ``en_GB``).
                When a column doesn't set its own ``locale``, generated Faker
                values come from this locale instead of the library default
                (en_US). Platform passes the operator's chosen value from
                AppSettings.default_faker_locale here.
        """
        self.seed = seed
        self.derive_key = derive_key
        self.instance_default_locale = instance_default_locale
        random.seed(self.seed)

        # Initialize faker. When an instance-wide default locale is set,
        # bind the shared Faker to that locale so the "no column-level
        # override" path produces locale-correct output without each
        # column generation rebuilding a Faker.
        if instance_default_locale:
            self.faker = make_faker(instance_default_locale)
        else:
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

    def _column_seed(
        self,
        column_name: str,
        column_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Per-column base seed used by every row-level seeding site.

        R3.10 contract: the seed is derived from the resolved generation
        key plus a canonical fingerprint of strategy/config, NOT the
        display column name. Same key + same strategy/config produces
        the same seed regardless of what the column is called or which
        column it lives in.

        Direct-YAML pipelines that need the pre-R3.10 column-name path
        can opt in by setting ``_legacy_column_name_seed: true`` on the
        column config. The Web UI never sets this; it is compat-only
        surface.

        Honors ``determinism: fresh`` for the admin-allowed roll-per-run
        path. Fresh is gated upstream by
        ``allow_per_pipeline_random_generation``; the engine accepts
        whatever the caller passed.

        Falls through to a seed-based (key-less) path when no resolver
        is configured. The fallback uses the fingerprint, not the
        column name, so renames are stable in unkeyed runs too.
        """
        cfg = column_config or {}
        if column_name and 'name' not in cfg:
            cfg = {**cfg, 'name': column_name}
        return synthetic_column_seed(
            derive_key=self.derive_key,
            column_config=cfg,
            fallback_seed=self.seed,
        )
    
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
            # `seed + hash(name)` otherwise). Same column + same row ->
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
        # them -- falling back to the default-locale provider would silently
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

        # Per-provider kwargs (representation, minimum_age, nb_sentences,
        # etc.) flow through from YAML's ``faker_kwargs:`` block. Invalid
        # entries are dropped silently by the provider lambda so a stale
        # config doesn't crash generation.
        faker_kwargs = column_config.get('faker_kwargs') or {}
        if not isinstance(faker_kwargs, dict):
            self.logger.warning(
                f"generate: faker_kwargs for {column_config.get('name')!r} must "
                f"be a mapping, got {type(faker_kwargs).__name__}; ignoring"
            )
            faker_kwargs = {}

        # Generate values for all rows. When `derive_key` is set, the
        # column-seed is HKDF-derived from the pipeline key, so the same
        # key + same column always yields the same bytes across runs.
        # When `column_config["determinism"] == "fresh"`, the column-seed
        # comes from os.urandom instead -- the column's output rolls per
        # run while staying internally consistent within the run.
        column_name = column_config.get('name', 'unnamed_column')
        column_seed = self._column_seed(column_name, column_config)
        values = []
        for i in range(num_rows):
            row_seed = column_seed + i
            random.seed(row_seed)
            faker_inst.seed_instance(row_seed)
            values.append(provider_func(**faker_kwargs))

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
        # generation calls -- order-dependence is a footgun). Honors
        # `determinism: fresh` for columns the user wants rolling per run.
        column_name = column_config.get('name', 'unnamed_column')
        random.seed(self._column_seed(column_name, column_config))
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
        # Cardinality bounds. min_per_parent: every parent value must
        # appear at least this many times in the child column.
        # max_per_parent: no parent value can appear more than this
        # many times. 0 means "no bound" (matches the YAML helper which
        # omits zero values to keep entries minimal).
        min_per_parent = int(column_config.get('min_per_parent') or 0)
        max_per_parent = int(column_config.get('max_per_parent') or 0)
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

        # Reseed so the choice sequence + any repair shuffles are stable
        # across runs when a key is provided + stable per-column even
        # without one. Mirrors the categorical generator's pattern at
        # the top of _generate_categorical_column. Otherwise output
        # depends on the order of column generation calls.
        column_name = column_config.get('name', 'unnamed_column')
        random.seed(self._column_seed(column_name, column_config))

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

        # Cardinality repair. When bounds are set, post-process the
        # value list to satisfy per-parent min + max. Note that this
        # phase reorders / shuffles, so sequential distribution + bounds
        # do not compose (the sequence is broken by the repair). Bounds
        # are inherently a global constraint; combine them with random
        # or weighted when ordering doesn't matter.
        if min_per_parent > 0 or max_per_parent > 0:
            values = self._apply_cardinality_bounds(
                values, ref_values, min_per_parent, max_per_parent,
            )

        return pd.Series(values)

    def _apply_cardinality_bounds(
        self,
        values: list,
        ref_values: list,
        min_per_parent: int,
        max_per_parent: int,
    ) -> list:
        """Repair a generated value list to honor per-parent cardinality bounds.

        Repair algorithm:
          1. Free over-max slots: for any parent value above max, mark
             the excess slot positions for replacement.
          2. Compute under-min deficits per parent value.
          3. If the deficit exceeds the over-max free slots, pull donor
             slots from over-min values (values that have more than
             min). Each donor can supply at most ``count - min`` slots
             without violating its own min.
          4. Build a replacement queue: under-min injections first, any
             remaining slots filled randomly from eligible parent values
             (those not yet at max).
          5. Shuffle + apply to the freed slots.

        Best-effort on impossible constraints (warn, never raise):
          - ``min * |pool| > num_rows``: cannot satisfy min for every
            parent; partially satisfy and warn.
          - All values at max while slots remain: over-fill the pool
            uniformly and warn (the constraint is unsatisfiable but the
            caller wanted num_rows back).
        """
        from collections import Counter
        n = len(values)
        max_eff = max_per_parent if max_per_parent > 0 else n + 1
        counts = Counter(values)
        free_slots: list[int] = []

        # 1. Mandatory: free over-max excess slots.
        for pv in ref_values:
            if counts[pv] > max_eff:
                excess = counts[pv] - max_eff
                indices = [i for i, v in enumerate(values) if v == pv]
                random.shuffle(indices)
                free_slots.extend(indices[:excess])
                counts[pv] = max_eff

        # 2. Compute under-min deficits using post-truncation counts.
        deficits = {pv: max(0, min_per_parent - counts[pv]) for pv in ref_values}
        total_deficit = sum(deficits.values())

        # 3. Optional: pull donor slots from over-min values when the
        #    deficit exceeds what step 1 freed. Each donor pv can give
        #    up to (counts[pv] - min_per_parent) slots without dropping
        #    below its own min.
        if total_deficit > len(free_slots):
            needed = total_deficit - len(free_slots)
            already_free = set(free_slots)
            donor_indices: list[int] = []
            for pv in ref_values:
                surplus = counts[pv] - min_per_parent
                if surplus <= 0:
                    continue
                candidates = [
                    i for i, v in enumerate(values)
                    if v == pv and i not in already_free
                ]
                random.shuffle(candidates)
                donor_indices.extend(candidates[:surplus])
            random.shuffle(donor_indices)
            taken = donor_indices[:needed]
            for i in taken:
                counts[values[i]] -= 1
            free_slots.extend(taken)

        # 4. Build replacement queue: deficits first, then eligible fills.
        queue: list = []
        for pv in ref_values:
            if deficits[pv] > 0:
                queue.extend([pv] * deficits[pv])

        if len(queue) > len(free_slots):
            self.logger.warning(
                f"Cardinality: min_per_parent={min_per_parent} cannot be "
                f"fully satisfied — would need {len(queue)} injections, only "
                f"{len(free_slots)} slots available without violating other "
                f"min bounds. Partial satisfaction."
            )
            queue = queue[:len(free_slots)]

        remaining = len(free_slots) - len(queue)
        if remaining > 0:
            tally = Counter(counts)
            for q in queue:
                tally[q] += 1
            eligible = [v for v in ref_values if tally[v] < max_eff]
            if not eligible:
                self.logger.warning(
                    f"Cardinality: all values at max_per_parent={max_per_parent}; "
                    f"over-filling proportionally for {remaining} rows."
                )
                eligible = ref_values
            queue.extend(random.choices(eligible, k=remaining))

        # 5. Apply replacements in shuffled order so the repair doesn't
        #    bias position.
        random.shuffle(queue)
        for slot, replacement in zip(free_slots, queue):
            values[slot] = replacement

        return values
    
    def _generate_formula_column(self, num_rows: int, column_config: Dict[str, Any],
                               table_name: str, reference_data: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Generate data based on a formula.

        Single inline path: every formula is a Python expression (write
        ``f"..."`` yourself if you want template-like substitution). Drops
        the previous three-way dispatch (basic / template / composite).

        When ``references: [...]`` is set on the column config, this method
        emits a None-filled placeholder series -- the column's actual values
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
            the column has cross-column references -- filled in post-pass).
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
                f"Formula column '{column_name}' references {references} -- "
                f"deferring to post-pass."
            )
            return pd.Series([None] * num_rows, dtype=object)

        return self._eval_formula_inline(
            num_rows, formula, column_name, column_config,
        )

    def _eval_formula_inline(
        self,
        num_rows: int,
        formula: str,
        column_name: str = 'unnamed_column',
        column_config: Optional[Dict[str, Any]] = None,
    ) -> pd.Series:
        """Per-row eval of a Python expression. Same deterministic seeding
        as the legacy ``basic`` path: ``column_seed + row_index`` reseeds
        ``random`` and the Faker instance per row. When the column's
        config has ``determinism: fresh``, the column-seed comes from
        os.urandom -- internal consistency holds within a run, but the
        column rolls per run.

        Scope per row (via :func:`decoy_engine.expressions.safe_eval` +
        :data:`decoy_engine.expressions.BASE_GLOBALS`):
          - ``i`` / ``index`` -- row number
          - ``random`` / ``randint`` / ``choice`` -- RNG (deterministic per row)
          - ``hash`` -- short deterministic hash
          - ``str`` / ``int`` / ``float`` / ``round`` / ``min`` / ``max`` / ``len``
          - Faker date helpers + arithmetic (``today``, ``days_from_now``, ...)

        Cross-column refs aren't reachable here -- that's the post-pass."""
        column_seed = self._column_seed(column_name, column_config)
        values = []
        for i in range(num_rows):
            local_seed = column_seed + i
            random.seed(local_seed)
            self.faker.seed_instance(local_seed)

            scope = self._formula_scope(local_seed)
            scope['i'] = i
            scope['index'] = i

            try:
                result = safe_eval(formula, BASE_GLOBALS, scope)
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
