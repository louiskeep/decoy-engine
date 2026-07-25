"""
Column data generators for the decoy_engine package.
Provides various strategies for generating synthetic column data.
"""

import random
import time
from typing import Any

import numpy as np
import pandas as pd
from faker import Faker

from decoy_engine.generators._distribution import _DistributionMixin
from decoy_engine.generators._formula import _FormulaMixin
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.internal.faker_setup import (
    get_faker_providers,
    make_faker,
)


class ColumnGenerator(_DistributionMixin, _FormulaMixin):
    """
    Generates data for columns based on configuration.
    Supports various column types and ensures consistent generation.
    """

    def __init__(
        self,
        seed: int = 42,
        logger=None,
        derive_key=None,
        instance_default_locale: str | None = None,
        reference_date: pd.Timestamp | None = None,
    ):
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
            reference_date: Optional pd.Timestamp; if None, snapshots
                ``pd.Timestamp.utcnow()`` once at construction. Bound into
                the formula scope as the source of ``now / today /
                days_from_now / months_from_now / years_from_now``. QA-1 H7
                (2026-06-01) replaces the per-call wall-clock read so the
                same formula returns the same value across runs (mod the
                same reference_date).
        """
        self.seed = seed
        self.derive_key = derive_key
        self.instance_default_locale = instance_default_locale
        # QA-1 H6 (2026-06-01): instance-local RNG replaces module-global
        # `random.seed`. Pre-fix two ColumnGenerators in the same process
        # corrupted each other's state, and any caller of `random.*`
        # outside this class saw side effects from generator construction.
        self._rng = random.Random(self.seed)
        # QA-1 H7 (2026-06-01): snapshot the reference date once so
        # formula helpers (now/today/days_from_now/...) return
        # consistent output across runs of the same column on
        # different calendar days.
        self._reference_date = (
            reference_date if reference_date is not None else pd.Timestamp.utcnow()
        )

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

        # QA walks/generators F8 (2026-06-01, LOW perf): cache per-locale
        # Faker instances + provider dicts. Pre-fix every column with a
        # column-level locale rebuilt a Faker + rescanned providers on
        # every call. A 30-column table all using `locale: en_GB`
        # produced 30 separate Faker objects + 30 provider scans
        # (~1-5ms each per locale). Cache scope is the generator
        # lifetime; single-threaded so no lock needed.
        self._locale_fakers: dict[str, tuple[Faker, dict]] = {}

        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger

            self.logger = get_logger()

        # Initialize generator functions
        self.generators = {
            "faker": self._generate_faker_column,
            "sequence": self._generate_sequence_column,
            "categorical": self._generate_categorical_column,
            "reference": self._generate_reference_column,
            "formula": self._generate_formula_column,
            # V2 Phase 3 D6: distribution-driven generator. Reads a
            # snapshot dict matching the shape `compute_distribution_snapshot`
            # emits and samples rows whose distribution matches the source.
            # Kind dispatch inside the method (numeric -> D6a; categorical
            # -> D6b; datetime -> D6c).
            "distribution": self._generate_distribution_column,
        }

        self.logger.debug(
            f"Initialized ColumnGenerator with seed: {seed}, keyed: {self.derive_key is not None}"
        )

    def _column_ctx(
        self,
        column_name: str,
        column_config: dict[str, Any] | None = None,
    ) -> GenDeriveContext:
        """Per-column generation derivation context (v6, F2/F3).

        The full-width replacement for `_column_seed`: same R3.10 keying
        rules (fingerprint-scoped, rename-stable, fresh + legacy paths),
        but exposes per-family (`base_int`) and per-row (`row_int`)
        derivations that consume all 32 bytes instead of the legacy
        4-byte int. Base-only consumers call `base_int(family)`; per-row
        loops call `row_int(family, i)` in place of `seed + i`.
        """
        cfg = column_config or {}
        if column_name and "name" not in cfg:
            cfg = {**cfg, "name": column_name}
        return GenDeriveContext.for_column(
            derive_key=self.derive_key,
            column_config=cfg,
            fallback_seed=self.seed,
        )

    def generate_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
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
        column_name = column_config.get("name", "unnamed_column")
        data_type = column_config.get("type", "faker")
        null_probability = column_config.get("null_probability", 0.0)

        # QA walks/generators F10 (2026-06-01, NIT): perf_counter is
        # the monotonic clock; time() can move backwards on NTP resync
        # and produces negative durations. We are measuring an interval,
        # not a wall-clock timestamp.
        start_time = time.perf_counter()

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
            self.logger.debug(
                f"Applying null probability {null_probability} to column '{column_name}'"
            )

            # QA walks/generators F3 (2026-06-01, HIGH correctness +
            # perf, Q-F3=b): vectorised null injection via
            # numpy.random.default_rng. Closes two pre-fix issues:
            #
            #   (A) Correctness/dtype: pre-fix `result.iloc[i] = None`
            #   on an int64 Series triggered in-place dtype promotion
            #   to float64 (since int64 cannot hold NaN). Downstream
            #   schema validators + masking strategies expecting int64
            #   then received float64. Post-fix we promote int columns
            #   to pandas nullable Int64 BEFORE applying nulls so the
            #   integer dtype survives.
            #
            #   (B) Perf: pre-fix ran num_rows reseed calls (each a
            #   full Mersenne Twister state reset, ~us each) + num_rows
            #   pandas scalar setters. At 100K rows + p=0.1 that's
            #   ~100ms of pure seeding overhead inside the innermost
            #   generation loop. Post-fix is one RNG construct + one
            #   vectorised draw + one boolean mask write.
            #
            # BREAKING (PO Q-F3=b, 2026-06-01): numpy.default_rng and
            # Python random.Random produce different floats for the
            # same integer seed, so the null PATTERN (which specific
            # rows are nulled) differs from pre-fix. The null FRACTION
            # converges to null_probability either way. This is a
            # controlled determinism bump; SEED_PROTOCOL_VERSION
            # bumped in determinism/_derive.py to 3.
            null_rng = np.random.default_rng(
                self._column_ctx(column_name, column_config).base_int("np")
            )
            null_mask = null_rng.random(num_rows) < null_probability
            if null_mask.any():
                # Promote integer dtypes to pandas nullable Int64 so
                # the integer column survives null assignment without
                # upcasting to float64.
                if pd.api.types.is_integer_dtype(result):
                    result = result.astype("Int64")
                # boolean indexing assignment is vectorised + dtype-aware.
                result = result.mask(null_mask, other=pd.NA)

        # Log generation time
        generation_time = time.perf_counter() - start_time
        self.logger.debug(
            f"Generated column '{column_name}' of type '{data_type}' in {generation_time:.2f} seconds"
        )

        # Log null statistics if null_probability was applied
        if null_probability > 0:
            null_count = result.isna().sum()
            null_percentage = (null_count / num_rows) * 100
            self.logger.debug(
                f"Applied null probability: {null_count}/{num_rows} values are null ({null_percentage:.1f}%)"
            )

        return result

    def _generate_faker_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
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
        faker_type = column_config.get("faker_type", "word")
        locale = column_config.get("locale")

        self.logger.debug(f"Generating faker column with type: {faker_type}, locale: {locale!r}")

        # Use the shared seeded Faker for the common (no-locale) path; build
        # a fresh instance when the column overrides locale so en_GB / de_DE
        # / etc. produce locale-correct addresses, names, phone numbers.
        # Provider list is rebuilt off the active instance because some
        # providers (e.g. `state_abbr`) raise on locales that don't define
        # them -- falling back to the default-locale provider would silently
        # leak en_US output.
        if locale:
            # QA walks/generators F8 (2026-06-01, LOW perf): cache hit.
            # See ColumnGenerator.__init__ for cache lifetime.
            cached = self._locale_fakers.get(locale)
            if cached is None:
                faker_inst = make_faker(locale)
                providers = get_faker_providers(faker_inst)
                self._locale_fakers[locale] = (faker_inst, providers)
            else:
                faker_inst, providers = cached
        else:
            faker_inst = self.faker
            providers = self.faker_providers

        if faker_type in providers:
            provider_func = providers[faker_type]
        else:
            self.logger.warning(f"Unknown faker_type '{faker_type}', using 'word' instead")
            provider_func = providers["word"]

        # Per-provider kwargs (representation, minimum_age, nb_sentences,
        # etc.) flow through from YAML's ``faker_kwargs:`` block. Invalid
        # entries are dropped silently by the provider lambda so a stale
        # config doesn't crash generation.
        faker_kwargs = column_config.get("faker_kwargs") or {}
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
        column_name = column_config.get("name", "unnamed_column")
        gen_ctx = self._column_ctx(column_name, column_config)
        values = []
        # F2/F3 (2026-06-26): per-row Faker seed is a full-width, faker-family
        # derivation (gen_ctx.row_int("faker", i)) instead of column_seed + i,
        # so adjacent columns no longer share row-shifted seeds. Faker.seed_instance
        # still mutates module-level random.seed internally (Faker library
        # limitation; QA-7 F1 added the cross-thread lock for that). Within a
        # single ColumnGenerator call we accept the within-call serialization.
        for i in range(num_rows):
            faker_inst.seed_instance(gen_ctx.row_int("faker", i))
            values.append(provider_func(**faker_kwargs))

        return pd.Series(values)

    def _generate_sequence_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
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
        start = column_config.get("start", 1)
        step = column_config.get("step", 1)
        prefix = column_config.get("prefix", "")
        suffix = column_config.get("suffix", "")
        pad_length = column_config.get("pad_length", 0)

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

    def _generate_categorical_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
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
        categories = column_config.get("categories", ["Category A", "Category B"])
        weights = column_config.get("weights")  # Optional probability weights

        self.logger.debug(f"Generating categorical column with {len(categories)} categories")

        # Reseed from the column-specific seed so the choices are stable
        # across runs when a key is provided, and stable per-column even
        # without one (otherwise output depends on the order of column
        # generation calls -- order-dependence is a footgun). Honors
        # `determinism: fresh` for columns the user wants rolling per run.
        column_name = column_config.get("name", "unnamed_column")
        # QA-1 H6 (2026-06-01): column-scoped Random instance replaces
        # module-global random.seed + random.choices. The fresh instance
        # is seeded byte-identically to the V1 pattern (random.Random(s)
        # produces the same sequence as random.seed(s) followed by
        # module-level draws).
        cat_rng = random.Random(self._column_ctx(column_name, column_config).base_int("py"))
        values = cat_rng.choices(categories, weights=weights, k=num_rows)
        return pd.Series(values)

    def _generate_reference_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
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
        reference_table = column_config.get("reference_table")
        reference_column = column_config.get("reference_column")
        distribution = column_config.get("distribution", "random")  # random, sequential, weighted
        # Cardinality bounds. min_per_parent: every parent value must
        # appear at least this many times in the child column.
        # max_per_parent: no parent value can appear more than this
        # many times. 0 means "no bound" (matches the YAML helper which
        # omits zero values to keep entries minimal).
        min_per_parent = int(column_config.get("min_per_parent") or 0)
        max_per_parent = int(column_config.get("max_per_parent") or 0)
        # Note: null_probability is now handled at the column level, not here

        self.logger.debug(
            f"Generating reference column referencing {reference_table}.{reference_column}"
        )

        # QA-1 M19 (2026-06-01): raise typed errors instead of returning
        # sentinel strings. Pre-fix a missing reference_table produced
        # ["REF_TABLE_NOT_FOUND_0", "REF_TABLE_NOT_FOUND_1", ...] as
        # valid-looking masked output; an operator who didn't check
        # warnings would never notice the misconfiguration.
        if reference_table not in reference_data:
            raise ValueError(
                f"reference_table {reference_table!r} not in reference_data; "
                f"available tables: {sorted(reference_data.keys())!r}"
            )

        # Get reference DataFrame
        ref_df = reference_data[reference_table]

        if reference_column not in ref_df.columns:
            raise ValueError(
                f"reference_column {reference_column!r} not in table "
                f"{reference_table!r}; available columns: "
                f"{sorted(ref_df.columns.tolist())!r}"
            )

        # QA walks/generators F1 (2026-06-01, CRITICAL determinism):
        # sort the pool. pd.Series.unique() returns values in
        # first-occurrence order; DB reads without ORDER BY return rows
        # in undefined order that varies by server restart + page cache
        # state + query plan. The ref_rng below is seeded deterministically
        # from _column_seed, so the SAME RNG seed + a DIFFERENT pool
        # order produced DIFFERENT FK assignments. Sorting the pool here
        # makes FK assignment independent of ref_df row order. Try
        # uniform-type sort first; fall back to str-key for mixed pools.
        # NOTE: .dropna() is load-bearing for the sort path. NaN values
        # break the uniform-type comparator (NaN != NaN, but sort still
        # uses TypeError-free comparison and produces undefined order),
        # and they would also pollute the FK pool with non-key values.
        # Stripping nulls before sort keeps both invariants honest.
        raw_pool = ref_df[reference_column].dropna().unique().tolist()
        try:
            ref_values = sorted(raw_pool)
        except TypeError:
            ref_values = sorted(raw_pool, key=str)

        if not ref_values:
            self.logger.warning(
                f"No reference values found in {reference_table}.{reference_column}. Returning NULL values."
            )
            return pd.Series([None] * num_rows)

        # Reseed so the choice sequence + any repair shuffles are stable
        # across runs when a key is provided + stable per-column even
        # without one. Mirrors the categorical generator's pattern at
        # the top of _generate_categorical_column. Otherwise output
        # depends on the order of column generation calls.
        column_name = column_config.get("name", "unnamed_column")
        # QA-1 H6 (2026-06-01): column-scoped Random instance replaces
        # module-global random.seed. ref_rng below is byte-identical to
        # the V1 module-global pattern.
        ref_rng = random.Random(self._column_ctx(column_name, column_config).base_int("py"))

        # Generate references based on distribution type
        values = []
        for i in range(num_rows):
            # Note: null_probability is now handled at the column level
            if distribution == "random":
                # Random selection with replacement
                values.append(ref_rng.choice(ref_values))

            elif distribution == "sequential":
                # Cycle through values sequentially
                values.append(ref_values[i % len(ref_values)])

            elif distribution == "weighted":
                # If weights are provided, use them
                weights = column_config.get("weights")
                if not weights or len(weights) != len(ref_values):
                    # Default to equal weights
                    weights = None
                values.append(ref_rng.choices(ref_values, weights=weights, k=1)[0])

            else:
                self.logger.warning(f"Unknown distribution type: {distribution}, using random")
                values.append(ref_rng.choice(ref_values))

        # Cardinality repair. When bounds are set, post-process the
        # value list to satisfy per-parent min + max. Note that this
        # phase reorders / shuffles, so sequential distribution + bounds
        # do not compose (the sequence is broken by the repair). Bounds
        # are inherently a global constraint; combine them with random
        # or weighted when ordering doesn't matter.
        if min_per_parent > 0 or max_per_parent > 0:
            # QA-1 H6 (2026-06-01): pass the column-scoped Random
            # instance through so the repair's shuffle / choices are
            # deterministic under self._column_seed without touching
            # module-global random.
            values = self._apply_cardinality_bounds(
                values,
                ref_values,
                min_per_parent,
                max_per_parent,
                rng=ref_rng,
            )

        return pd.Series(values)

    def _apply_cardinality_bounds(
        self,
        values: list,
        ref_values: list,
        min_per_parent: int,
        max_per_parent: int,
        rng: random.Random,
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
                rng.shuffle(indices)
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
                candidates = [i for i, v in enumerate(values) if v == pv and i not in already_free]
                rng.shuffle(candidates)
                donor_indices.extend(candidates[:surplus])
            rng.shuffle(donor_indices)
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
                f"fully satisfied - would need {len(queue)} injections, only "
                f"{len(free_slots)} slots available without violating other "
                f"min bounds. Partial satisfaction."
            )
            queue = queue[: len(free_slots)]

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
            queue.extend(rng.choices(eligible, k=remaining))

        # 5. Apply replacements in shuffled order so the repair doesn't
        #    bias position.
        rng.shuffle(queue)
        for slot, replacement in zip(free_slots, queue, strict=False):
            values[slot] = replacement

        return values
