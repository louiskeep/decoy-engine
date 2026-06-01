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

from decoy_engine.expressions import BASE_GLOBALS, safe_eval
from decoy_engine.generators.derivation import (
    synthetic_column_seed,
)
from decoy_engine.internal.crypto import (
    deterministic_hash,
)
from decoy_engine.internal.faker_setup import (
    get_faker_providers,
    make_faker,
)


class ColumnGenerator:
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
        self._reference_date = reference_date if reference_date is not None else pd.Timestamp.utcnow()

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

    def _column_seed(
        self,
        column_name: str,
        column_config: dict[str, Any] | None = None,
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
        if column_name and "name" not in cfg:
            cfg = {**cfg, "name": column_name}
        return synthetic_column_seed(
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
            self.logger.debug(
                f"Applying null probability {null_probability} to column '{column_name}'"
            )

            # Per-row seeding off the column-seed (HKDF-derived when keyed,
            # `seed + hash(name)` otherwise). Same column + same row ->
            # same null/non-null decision across runs.
            # QA-1 H6 + M17 (2026-06-01): pass column_config through so
            # two columns with different configs do not share a null
            # mask; use a fresh Random instance per call to avoid
            # mutating self._rng's state (which other generator calls
            # may depend on).
            column_seed = self._column_seed(column_name, column_config)
            null_rng = random.Random()
            for i in range(num_rows):
                null_rng.seed(column_seed + i)
                if null_rng.random() < null_probability:
                    result.iloc[i] = None

        # Log generation time
        generation_time = time.time() - start_time
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
            faker_inst = make_faker(locale)
            providers = get_faker_providers(faker_inst)
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
        column_seed = self._column_seed(column_name, column_config)
        values = []
        # QA-1 H6 (2026-06-01): use a column-scoped Random instance so the
        # per-row re-seed does not pollute self._rng or module-global.
        # Faker.seed_instance still mutates module-level random.seed
        # internally (Faker library limitation; QA-7 F1 added the
        # cross-thread lock for that). Within a single ColumnGenerator
        # call we accept the within-call serialization.
        row_rng = random.Random()
        for i in range(num_rows):
            row_seed = column_seed + i
            row_rng.seed(row_seed)
            faker_inst.seed_instance(row_seed)
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
        cat_rng = random.Random(self._column_seed(column_name, column_config))
        values = cat_rng.choices(categories, weights=weights, k=num_rows)
        return pd.Series(values)

    def _generate_distribution_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
        """V2 Phase 3 D6: generate rows whose distribution matches a
        source snapshot.

        Consumes a `snapshot` dict whose shape matches what
        `decoy_engine.quality.snapshot.compute_distribution_snapshot`
        emits per column (D1a). Dispatches on `snapshot.kind` so a
        single strategy covers numeric, categorical, and datetime
        source columns; freetext is deferred (V2 scope cut).

        Operator config:

            columns:
              age:
                strategy: distribution
                snapshot:
                  kind: numeric
                  bin_edges: [18, 30, 45, 60, 80]
                  bin_counts: [120, 340, 280, 190, 70]
                  # min / max / mean / std / quantiles are present
                  # in real snapshots but unused by the sampler.

        Determinism follows the same pattern as the other generators:
        per-column seed via `_column_seed`, so same key + same
        snapshot dict -> same output bytes.

        Verification: a column generated from a snapshot should score
        ~1.0 on the D5b `compute_shape_fidelity` against the source's
        snapshot (tests pin this contract).
        """
        column_name = column_config.get("name", "unnamed_column")
        snapshot = column_config.get("snapshot")
        if not isinstance(snapshot, dict):
            self.logger.warning(
                f"distribution column '{column_name}' has no snapshot dict; "
                "emitting nulls",
            )
            return pd.Series([None] * num_rows)

        kind = str(snapshot.get("kind") or "").lower()
        seed = self._column_seed(column_name, column_config)

        # D6a: numeric. Numpy RNG for vectorized sampling + bitwise
        # determinism across machines/python versions when the
        # column_seed is stable.
        if kind == "numeric":
            return self._generate_distribution_numeric(num_rows, snapshot, seed)

        # D6b: categorical. Weighted sampling from the snapshot's
        # top_values head, with a synthetic "<other>" bucket carrying
        # the collapsed tail weight from `other_count`.
        if kind == "categorical":
            return self._generate_distribution_categorical(num_rows, snapshot, seed)

        # D6c: datetime. Weighted year choice from year_bins, then
        # uniform timestamp within that year clipped to [min, max].
        if kind == "datetime":
            return self._generate_distribution_datetime(num_rows, snapshot, seed)

        self.logger.warning(
            f"distribution column '{column_name}' has unsupported kind {kind!r}; "
            "emitting nulls",
        )
        return pd.Series([None] * num_rows)

    def _generate_distribution_numeric(
        self,
        num_rows: int,
        snapshot: dict[str, Any],
        seed: int,
    ) -> pd.Series:
        """D6a numeric sampler.

        Inverse-CDF sampling on the snapshot's histogram:
          1. Build per-bin probabilities from `bin_counts`.
          2. For each output row, pick a bin index proportional to that.
          3. Within the picked bin, sample uniformly between
             `bin_edges[i]` and `bin_edges[i+1]`.

        Degenerate cases:
          - No bins / empty arrays -> all nulls (logged).
          - Single bin with zero range (lo == hi, the
            constant-column case D1a explicitly handles) -> emit
            the constant value `num_rows` times. No randomness needed.
          - Total bin count zero -> all nulls (snapshot was empty
            on the source; nothing to sample from).

        Returns a Series of Python floats. Integer-valued source
        columns will land as floats; D6 doesn't try to infer int
        vs float from the snapshot (the snapshot doesn't carry a
        dtype hint). A downstream cast op or a `dtype: int` config
        can pin the output dtype if needed; deferred to a D6
        follow-up.
        """
        # D1a snapshot shape: per-column dict has `kind` at top level
        # and the kind-specific fields (bin_edges, bin_counts, min,
        # max, ...) nested under `stats`. Read from `stats` so the
        # snapshot can be passed in verbatim from compute_distribution_snapshot
        # without flattening. Fall back to top-level for callers
        # constructing simplified snapshots in tests / examples.
        stats = snapshot.get("stats") or {}
        bin_edges = stats.get("bin_edges") or snapshot.get("bin_edges") or []
        bin_counts = stats.get("bin_counts") or snapshot.get("bin_counts") or []
        if not bin_edges or not bin_counts:
            self.logger.warning(
                "distribution numeric snapshot missing bin_edges / bin_counts; "
                "emitting nulls",
            )
            return pd.Series([None] * num_rows)
        # bin_edges has len(bin_counts) + 1 entries by D1a convention.
        if len(bin_edges) != len(bin_counts) + 1:
            self.logger.warning(
                f"distribution numeric snapshot shape mismatch "
                f"(edges={len(bin_edges)}, counts={len(bin_counts)}); "
                "emitting nulls",
            )
            return pd.Series([None] * num_rows)

        rng = np.random.default_rng(seed)
        edges = np.asarray(bin_edges, dtype=float)
        counts = np.asarray(bin_counts, dtype=float)
        total = float(counts.sum())
        if total <= 0:
            self.logger.warning(
                "distribution numeric snapshot has zero total count; "
                "emitting nulls",
            )
            return pd.Series([None] * num_rows)

        # Constant-column case from D1a: lo == hi -> single bin with
        # edges [lo, hi] both equal. Just emit the constant.
        if len(counts) == 1 and edges[0] == edges[1]:
            return pd.Series([float(edges[0])] * num_rows)

        # Normal path: weighted bin choice + uniform within bin.
        probs = counts / total
        chosen_bins = rng.choice(len(counts), size=num_rows, p=probs)
        bin_lo = edges[chosen_bins]
        bin_hi = edges[chosen_bins + 1]
        # rng.uniform(low, high) is half-open [low, high); fine for
        # sampling — the snapshot's upper bound was inclusive of the
        # source max but np.histogram's right-most bin is closed, so
        # a tiny chance of sampling exactly at the open boundary is
        # not a real distribution loss.
        values = rng.uniform(bin_lo, bin_hi)
        return pd.Series(values)

    def _generate_distribution_categorical(
        self,
        num_rows: int,
        snapshot: dict[str, Any],
        seed: int,
    ) -> pd.Series:
        """D6b categorical sampler.

        Weighted sampling from the snapshot's `top_values` head. The
        tail values (everything past `top_k` distinct values at
        snapshot time) collapse into a single `other_count` weight in
        D1a's snapshot shape. We re-introduce that mass as a synthetic
        `<other>` bucket so the value-frequency distribution shape
        scores well on D5b without inventing fake category names.

        Snapshot shape (per D1a):

          stats:
            top_values:
              - {value: "Texas", count: 4200}
              - {value: "California", count: 3900}
              ...
            other_count: 850   # mass collapsed from tail

        Operators who want literal-only output (no `<other>`
        placeholder) can drop the placeholder downstream or set
        `other_label` on the config; see below.

        Determinism: per-column seed feeds numpy default_rng.
        rng.choice with explicit p= is bitwise stable across runs.

        Degenerate cases:
          - missing / empty top_values + zero other_count -> all nulls
          - non-dict top_values entry -> skipped (defensive)
          - zero total weight (top_values present but counts all 0) ->
            all nulls
        """
        # D1a snapshot shape: top_values + other_count nested under
        # `stats`. Fall back to top-level for simplified inline test
        # snapshots (matches D6a's two-layer read).
        stats = snapshot.get("stats") or {}
        top_values = stats.get("top_values") or snapshot.get("top_values") or []
        other_count = stats.get("other_count")
        if other_count is None:
            other_count = snapshot.get("other_count", 0)

        # Allow operator override of the tail placeholder. Default
        # picked to be visibly synthetic so it doesn't collide with
        # real category values; an empty string also works if the
        # downstream consumer prefers to filter.
        other_label = snapshot.get("other_label", "<other>")

        # Normalize the head into (value, weight) tuples. Skip malformed
        # entries silently rather than failing the whole column on a
        # single bad row in the snapshot.
        pairs: list[tuple[str, float]] = []
        for entry in top_values:
            if not isinstance(entry, dict):
                continue
            val = entry.get("value")
            cnt = entry.get("count", 0)
            if val is None:
                continue
            try:
                w = float(cnt)
            except (TypeError, ValueError):
                continue
            if w <= 0:
                continue
            pairs.append((str(val), w))

        # Synthetic tail bucket. Only add when there's actual collapsed
        # mass; emitting a 0-weight `<other>` would pollute the value
        # vocabulary for no benefit.
        try:
            tail_weight = float(other_count)
        except (TypeError, ValueError):
            tail_weight = 0.0
        if tail_weight > 0:
            pairs.append((other_label, tail_weight))

        if not pairs:
            self.logger.warning(
                "distribution categorical snapshot has no usable top_values "
                "or other_count; emitting nulls",
            )
            return pd.Series([None] * num_rows)

        values = [v for v, _ in pairs]
        weights = np.asarray([w for _, w in pairs], dtype=float)
        total = float(weights.sum())
        if total <= 0:
            self.logger.warning(
                "distribution categorical snapshot has zero total weight; "
                "emitting nulls",
            )
            return pd.Series([None] * num_rows)

        rng = np.random.default_rng(seed)
        probs = weights / total
        # rng.choice on a Python list of strings preserves dtype well
        # (object dtype Series); numpy's str-dtype default would
        # truncate to the longest seen string and break downstream
        # mask/compare ops that expect open-ended strings.
        chosen_idx = rng.choice(len(values), size=num_rows, p=probs)
        out = [values[i] for i in chosen_idx]
        return pd.Series(out, dtype=object)

    def _generate_distribution_datetime(
        self,
        num_rows: int,
        snapshot: dict[str, Any],
        seed: int,
    ) -> pd.Series:
        """D6c datetime sampler.

        Two-step inverse-CDF on D1a's datetime snapshot shape:
          1. Pick a year via weighted choice from `year_bins`
             (each entry is `{year, count}`).
          2. Sample a uniform timestamp within that year, clipped to
             [snapshot.min, snapshot.max] so a partial source year
             (data starts 2020-06-01) doesn't generate January 2020
             timestamps.

        Snapshot shape (per D1a `_datetime_stats`):

          stats:
            min: "2020-06-01T00:00:00"
            max: "2024-03-15T18:30:00"
            year_bins:
              - {year: 2020, count: 1200}
              - {year: 2021, count: 2100}
              ...

        Determinism: numpy default_rng seeded from the per-column
        seed; year choice + within-year uniform are both vectorized.
        Output dtype is pandas datetime64[ns] (matches how the source
        snapshot is keyed).

        Degenerate cases:
          - missing year_bins / empty -> nulls + warning
          - missing min or max -> nulls + warning (can't bound the
            uniform draw safely)
          - zero total bin count -> nulls
          - all bins in a single year that equals the only year in
            min/max -> uniform across [min, max] (the chosen year
            collapses to the full span)
        """
        stats = snapshot.get("stats") or {}
        year_bins = stats.get("year_bins") or snapshot.get("year_bins") or []
        min_iso = stats.get("min") or snapshot.get("min")
        max_iso = stats.get("max") or snapshot.get("max")

        if not year_bins:
            self.logger.warning(
                "distribution datetime snapshot missing year_bins; "
                "emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")
        if not min_iso or not max_iso:
            self.logger.warning(
                "distribution datetime snapshot missing min/max; "
                "emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")

        try:
            ts_min = pd.Timestamp(min_iso)
            ts_max = pd.Timestamp(max_iso)
        except (TypeError, ValueError):
            self.logger.warning(
                f"distribution datetime snapshot has unparseable min/max "
                f"({min_iso!r}, {max_iso!r}); emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")

        # Normalize tz-aware bounds to UTC-naive so the in-year
        # uniform math doesn't trip on mixed-tz arithmetic. D1a
        # already strips tz before isoformat, so this is defensive.
        if ts_min.tzinfo is not None:
            ts_min = ts_min.tz_convert("UTC").tz_localize(None)
        if ts_max.tzinfo is not None:
            ts_max = ts_max.tz_convert("UTC").tz_localize(None)
        if ts_max < ts_min:
            self.logger.warning(
                "distribution datetime snapshot has max < min; "
                "emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")

        # Build year -> weight, filter malformed entries.
        years: list[int] = []
        weights_list: list[float] = []
        for entry in year_bins:
            if not isinstance(entry, dict):
                continue
            y = entry.get("year")
            c = entry.get("count", 0)
            try:
                yi = int(y)
                wi = float(c)
            except (TypeError, ValueError):
                continue
            if wi <= 0:
                continue
            years.append(yi)
            weights_list.append(wi)
        if not years:
            self.logger.warning(
                "distribution datetime snapshot has no usable year_bins; "
                "emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")

        weights = np.asarray(weights_list, dtype=float)
        total = float(weights.sum())
        if total <= 0:
            self.logger.warning(
                "distribution datetime snapshot has zero total year count; "
                "emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")

        rng = np.random.default_rng(seed)
        probs = weights / total
        chosen_idx = rng.choice(len(years), size=num_rows, p=probs)

        # For each row, compute the in-year window clipped to
        # [ts_min, ts_max]. Vectorized via nanosecond integer math
        # so the uniform draw is one numpy call.
        years_arr = np.asarray([years[i] for i in chosen_idx], dtype=np.int64)
        # Build per-row year-start + year-end timestamps.
        year_starts = pd.to_datetime([f"{y}-01-01" for y in years_arr])
        year_ends = pd.to_datetime([f"{y + 1}-01-01" for y in years_arr])  # exclusive
        # Clip to the snapshot's actual min/max.
        lo_ns = np.maximum(year_starts.view("int64"), ts_min.value)
        hi_ns = np.minimum(year_ends.view("int64"), ts_max.value)
        # A clipped window can be inverted when the chosen year sits
        # entirely outside [min, max] (snapshot integrity bug, but
        # be defensive: clamp to year-start so we still emit a
        # plausible value rather than nulls or an out-of-range draw).
        bad = hi_ns <= lo_ns
        if bad.any():
            hi_ns = np.where(bad, lo_ns + 1, hi_ns)
        # Random nanosecond offsets within each row's window.
        rand_floats = rng.random(size=num_rows)
        offsets = (rand_floats * (hi_ns - lo_ns)).astype(np.int64)
        sample_ns = lo_ns + offsets
        return pd.Series(pd.to_datetime(sample_ns, unit="ns"))

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

        # Get reference values
        ref_values = ref_df[reference_column].dropna().unique().tolist()

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
        ref_rng = random.Random(self._column_seed(column_name, column_config))

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
                f"fully satisfied — would need {len(queue)} injections, only "
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

    def _generate_formula_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
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
        formula = column_config.get("formula", "")
        column_name = column_config.get("name", "unnamed_column")
        references = column_config.get("references", []) or []

        if not formula:
            self.logger.warning("No formula provided in configuration")
            return pd.Series([None] * num_rows)

        if references:
            # Defer to the post-pass: this column reads sibling columns,
            # which haven't been generated yet during the per-column loop.
            self.logger.debug(
                f"Formula column '{column_name}' references {references} -- deferring to post-pass."
            )
            return pd.Series([None] * num_rows, dtype=object)

        return self._eval_formula_inline(
            num_rows,
            formula,
            column_name,
            column_config,
        )

    def fill_referenced_formula_column(
        self,
        col_name: str,
        formula: str,
        references: list[str],
        out: "pd.DataFrame",
    ) -> "pd.Series":
        """Evaluate a formula column whose expression reads sibling columns.

        Called by generate_op after pass 1 has produced all non-formula
        columns, so ``out`` contains finalized values for every referenced
        column. Uses the same per-row deterministic seeding and safe_eval
        scope as ``_eval_formula_inline``.

        Mirrors DataGenerator._evaluate_composite_formula but operates
        on the in-memory DataFrame instead of a CSV file.
        """
        missing = [r for r in references if r not in out.columns]
        if missing:
            self.logger.warning(
                f"Formula column {col_name!r} references missing columns "
                f"{missing!r} -- emitting None"
            )
            return pd.Series([None] * len(out), dtype=object)

        column_seed = self._column_seed(col_name)
        values: list = []
        # QA-1 H6 + M21 (2026-06-01): per-row Random instance feeds
        # _formula_scope; module-global random no longer used by formula
        # eval. faker.seed_instance still serializes module-level state
        # internally (Faker library limitation; see synthesize.py
        # _FAKER_CALL_LOCK).
        row_rng = random.Random()
        for i in range(len(out)):
            local_seed = column_seed + i
            row_rng.seed(local_seed)
            self.faker.seed_instance(local_seed)

            scope = self._formula_scope(local_seed, rng=row_rng)
            scope["i"] = i
            scope["index"] = i
            for ref in references:
                val = out.at[i, ref]
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    val = ""
                scope[ref] = val

            try:
                result = safe_eval(formula, BASE_GLOBALS, scope)
                values.append(result)
            except Exception as exc:
                self.logger.warning(
                    f"Formula column {col_name!r} row {i} eval error: {exc}"
                )
                values.append(None)

        return pd.Series(values, dtype=object)

    def _eval_formula_inline(
        self,
        num_rows: int,
        formula: str,
        column_name: str = "unnamed_column",
        column_config: dict[str, Any] | None = None,
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
        # QA-1 H6 + M21 (2026-06-01): per-row Random instance feeds
        # _formula_scope. See _evaluate_referenced_formula_column for
        # the rationale.
        row_rng = random.Random()
        for i in range(num_rows):
            local_seed = column_seed + i
            row_rng.seed(local_seed)
            self.faker.seed_instance(local_seed)

            scope = self._formula_scope(local_seed, rng=row_rng)
            scope["i"] = i
            scope["index"] = i

            try:
                result = safe_eval(formula, BASE_GLOBALS, scope)
                values.append(result)
            except Exception as e:
                error_msg = str(e)
                if "not defined" in error_msg:
                    self.logger.warning(f"Name not available in formula for row {i}: {error_msg}")
                    self.logger.info(f"Available names: {sorted(list(scope.keys()))}")
                else:
                    self.logger.warning(f"Error evaluating formula for row {i}: {error_msg}")
                self.logger.debug(f"Formula: {formula}")
                values.append(None)

        return pd.Series(values)

    def _formula_scope(
        self, local_seed: int, rng: random.Random | None = None
    ) -> dict[str, Any]:
        """Build the names available inside a formula eval. Shared between
        the inline path here and the post-pass in
        ``DataGenerator._process_referenced_formulas`` so users get the
        same vocabulary regardless of whether their formula reads other
        columns. Per-row seed is captured into the closure so RNG calls
        within the eval stay deterministic.

        QA-1 M21 (2026-06-01): ``random``/``randint``/``choice`` bind to
        the passed-in ``rng`` instance instead of module-level
        ``random``. Pre-fix two formula columns in the same job shared
        module-global random state and column B's output depended on
        column A's execution order. With a per-row rng, column B's
        sequence is a pure function of (column_seed, row_index).
        Backwards-compatible: when ``rng`` is None, falls back to the
        module-global pattern for any caller that hasn't migrated yet.

        QA-1 H7 (2026-06-01): ``now``/``today``/``days_from_now``/
        ``months_from_now``/``years_from_now`` now read
        ``self._reference_date`` (snapshotted at construction time)
        instead of ``pd.Timestamp.now()`` per call. The same formula
        run on two different calendar days against the same generator
        returns identical output.
        """
        _rng = rng if rng is not None else random
        ref_date = self._reference_date
        return {
            # RNG bound to the per-row instance (M21).
            "random": _rng.random,
            "randint": lambda a, b: _rng.randint(a, b),
            "choice": lambda lst: _rng.choice(lst),
            # Numeric / string utilities
            "round": round,
            "min": min,
            "max": max,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "hash": lambda x: deterministic_hash(str(x), local_seed)[:8],
            # Faker date helpers
            "date_between": self.faker.date_between,
            "date_this_decade": self.faker.date_this_decade,
            "date_this_year": self.faker.date_this_year,
            "date_this_month": self.faker.date_this_month,
            "future_date": self.faker.future_date,
            "past_date": self.faker.past_date,
            "date_of_birth": self.faker.date_of_birth,
            "time": lambda: self.faker.time(),
            # Wall-clock helpers bound to reference_date (H7).
            "now": lambda fmt="%Y-%m-%d": ref_date.strftime(fmt),
            "today": lambda fmt="%Y-%m-%d": ref_date.strftime(fmt),
            "days_from_now": lambda days: (ref_date + pd.Timedelta(days=days)).strftime(
                "%Y-%m-%d"
            ),
            "months_from_now": lambda months: (
                ref_date + pd.DateOffset(months=months)
            ).strftime("%Y-%m-%d"),
            "years_from_now": lambda years: (
                ref_date + pd.DateOffset(years=years)
            ).strftime("%Y-%m-%d"),
            "format_date": lambda date_obj, fmt="%Y-%m-%d": (
                date_obj.strftime(fmt) if hasattr(date_obj, "strftime") else str(date_obj)
            ),
        }
