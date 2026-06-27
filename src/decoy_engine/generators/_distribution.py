"""Distribution-snapshot column samplers, split out of columns.py (F11a).

These methods are a mixin folded into ``ColumnGenerator``. They read
``self.logger`` and are otherwise pure functions of (num_rows, snapshot,
seed). They live here only to keep the generator orchestration module under
the size cap; behavior is byte-identical to the in-class versions.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


class _DistributionMixin:
    """Distribution-snapshot samplers for :class:`ColumnGenerator`."""

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
                f"distribution column '{column_name}' has no snapshot dict; emitting nulls",
            )
            return pd.Series([None] * num_rows)

        kind = str(snapshot.get("kind") or "").lower()
        seed = self._column_ctx(column_name, column_config).base_int("np")

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
            f"distribution column '{column_name}' has unsupported kind {kind!r}; emitting nulls",
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
                "distribution numeric snapshot missing bin_edges / bin_counts; emitting nulls",
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
                "distribution numeric snapshot has zero total count; emitting nulls",
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
        # sampling - the snapshot's upper bound was inclusive of the
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
                "distribution categorical snapshot has zero total weight; emitting nulls",
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
                "distribution datetime snapshot missing year_bins; emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")
        if not min_iso or not max_iso:
            self.logger.warning(
                "distribution datetime snapshot missing min/max; emitting nulls",
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
                "distribution datetime snapshot has max < min; emitting nulls",
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
                "distribution datetime snapshot has no usable year_bins; emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")

        weights = np.asarray(weights_list, dtype=float)
        total = float(weights.sum())
        if total <= 0:
            self.logger.warning(
                "distribution datetime snapshot has zero total year count; emitting nulls",
            )
            return pd.Series([pd.NaT] * num_rows, dtype="datetime64[ns]")

        rng = np.random.default_rng(seed)
        probs = weights / total
        chosen_idx = rng.choice(len(years), size=num_rows, p=probs)

        # For each row, compute the in-year window clipped to
        # [ts_min, ts_max]. Vectorized via nanosecond integer math
        # so the uniform draw is one numpy call.
        years_arr = np.asarray([years[i] for i in chosen_idx], dtype=np.int64)
        # QA walks/generators F7 (2026-06-01, MEDIUM correctness): cap
        # years_arr to ts_max.year so neither year_starts nor year_ends
        # construction trips OutOfBoundsDatetime at the active
        # pd.to_datetime precision (nanosecond by default; max year
        # 2262). Pre-fix any year_bins entry beyond the active
        # precision crashed the entire distribution sampler. The
        # nanosecond clip on hi_ns/lo_ns below already bounds the
        # per-row output to [ts_min, ts_max], so capping the
        # intermediate year is safe: every row that picked a beyond-
        # max bin now lands at ts_max.year (which the downstream clip
        # would have produced anyway). The audit's documented endpoint
        # cap at 9999 still applies for snapshots that use the wider
        # datetime64[s] precision where ts_max can reach year 9999.
        ts_max_year = int(ts_max.year)
        years_arr = np.minimum(years_arr, ts_max_year)
        # Build per-row year-start + year-end timestamps.
        year_starts = pd.to_datetime([f"{y}-01-01" for y in years_arr])
        year_ends = pd.to_datetime(
            ["9999-12-31" if y >= 9999 else f"{y + 1}-01-01" for y in years_arr]
        )  # exclusive
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
