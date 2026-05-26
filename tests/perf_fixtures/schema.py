"""Shared column spec + tier sizes for the PERF.BASE.2 fixture suite.

Source pattern: Faker is the established standard for synthetic PII
(https://faker.readthedocs.io/). We use Faker for value generation and
numpy for numeric columns; fixed seeds across both keep regeneration
byte-stable run-to-run (see ``scripts/gen_perf_fixtures.py``).

Goals for the column spec:

1. **Apples-to-apples across tiers.** The strategy-tagged columns
   (one per perf band: cheap / medium / expensive) are present in
   every tier so per-strategy timing comparisons survive scale jumps.
2. **Mix of dtypes.** Strings, ints, floats, dates, timestamps,
   categoricals, and one free-text column (medium + large). Strategy
   benchmarking that only saw strings would miss numeric-column
   regressions.
3. **Reproducibility.** Generated values are seeded; the same seed +
   same row count + same engine version must produce a byte-identical
   Parquet file.

The 10 / 30 / 50 column counts the PERF.BASE.2 sprint spec calls for
are interpreted as "approximately"; in practice each tier carries all
strategy-tagged columns plus enough filler to hit the target width.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnSpec:
    """Description of one fixture column.

    ``kind`` is a high-level category understood by the generator script
    (``id_int``, ``ssn``, ``email``, etc.); the script dispatches on it
    to choose the right Faker / numpy call. ``strategy_band`` tags the
    strategy intensity this column is meant to exercise during masking
    benchmarks (see PERF.BASE.2 spec "Strategy intensity mix"). ``None``
    means the column is filler -- present for shape, not for strategy
    timing.
    """

    name: str
    kind: str
    strategy_band: str | None = None  # "cheap" | "medium" | "expensive" | None


# Columns shared by EVERY tier. Each strategy band has at least one
# representative so per-strategy timing tables can compare apples to
# apples across the small / medium / large tiers.
COMMON_COLUMNS: tuple[ColumnSpec, ...] = (
    # Cheap strategies (redact, passthrough, truncate).
    ColumnSpec("customer_id", "id_int", "cheap"),
    ColumnSpec("ssn", "ssn", "cheap"),
    ColumnSpec("account_balance", "amount_float", "cheap"),
    # Medium strategies (faker, date_shift, bucketize).
    ColumnSpec("full_name", "full_name", "medium"),
    ColumnSpec("dob", "date_past", "medium"),
    ColumnSpec("score", "score_int", "medium"),
    # Expensive strategies (fpe, formula, reference).
    ColumnSpec("email", "email", "expensive"),
    ColumnSpec("transaction_amount", "amount_float", "expensive"),
    ColumnSpec("zip", "zip5", "expensive"),
    # Shape variety -- categorical + timestamp present in all tiers
    # because dtype coverage matters more than band coverage here.
    ColumnSpec("status", "category_status"),
    ColumnSpec("created_at", "timestamp_recent"),
)

# Columns added at the medium tier (and inherited by large). Covers the
# remaining PII categories from the sprint spec + the free-text notes
# column for text-mask testing.
MEDIUM_EXTRA_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("phone", "phone"),
    ColumnSpec("street_address", "street_address"),
    ColumnSpec("city", "city"),
    ColumnSpec("state", "state_abbr"),
    ColumnSpec("category", "category_general"),
    ColumnSpec("tier", "category_tier"),
    ColumnSpec("claim_id", "id_int"),
    ColumnSpec("invoice_id", "id_int"),
    ColumnSpec("updated_at", "timestamp_recent"),
    ColumnSpec("processed_at", "timestamp_recent"),
    ColumnSpec("count_metric", "count_int"),
    ColumnSpec("notes", "free_text"),
    # Filler to hit the ~30 column target. Numeric + categorical fillers
    # exercise dtype paths that benchmarks should not gloss over.
    ColumnSpec("filler_int_1", "filler_int"),
    ColumnSpec("filler_int_2", "filler_int"),
    ColumnSpec("filler_int_3", "filler_int"),
    ColumnSpec("filler_float_1", "filler_float"),
    ColumnSpec("filler_float_2", "filler_float"),
    ColumnSpec("filler_cat_1", "filler_cat"),
    ColumnSpec("filler_cat_2", "filler_cat"),
)

# Columns added at the large tier. Extra fillers + a couple more PII /
# numeric columns so the 50-wide table includes plausible variation
# rather than 20 identical filler columns.
LARGE_EXTRA_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("middle_name", "first_name"),
    ColumnSpec("street_address_2", "street_address"),
    ColumnSpec("apt_number", "apt_number"),
    ColumnSpec("zip_plus_four", "zip5"),
    ColumnSpec("alt_phone", "phone"),
    ColumnSpec("alt_email", "email"),
    ColumnSpec("opened_at", "timestamp_recent"),
    ColumnSpec("closed_at", "timestamp_recent"),
    ColumnSpec("renewed_at", "timestamp_recent"),
    ColumnSpec("plan_id", "id_int"),
    ColumnSpec("group_id", "id_int"),
    ColumnSpec("subgroup_id", "id_int"),
    ColumnSpec("ar_balance", "amount_float"),
    ColumnSpec("ap_balance", "amount_float"),
    ColumnSpec("credit_limit", "amount_float"),
    ColumnSpec("usage_units", "count_int"),
    ColumnSpec("flag_a", "filler_cat"),
    ColumnSpec("flag_b", "filler_cat"),
    ColumnSpec("flag_c", "filler_cat"),
    ColumnSpec("filler_float_3", "filler_float"),
)


@dataclass(frozen=True)
class TierSpec:
    """One fixture tier: name, row count, columns, generator seed."""

    name: str
    rows: int
    columns: tuple[ColumnSpec, ...]
    # Approximate Parquet output size in MB. Used by the load-time test
    # as a sanity envelope, NOT as a hard contract -- pyarrow versions
    # and compression-default changes drift it by a few percent.
    expected_size_mb_min: float
    expected_size_mb_max: float
    seed: int
    # Whether the fixture is committed to the repo. Large is regenerated
    # on demand by the gen script + gitignored.
    committed: bool = True


SMALL = TierSpec(
    name="small",
    rows=1_000,
    columns=COMMON_COLUMNS,
    expected_size_mb_min=0.0,
    expected_size_mb_max=2.0,
    seed=20260526,
    committed=True,
)

MEDIUM = TierSpec(
    name="medium",
    rows=100_000,
    columns=COMMON_COLUMNS + MEDIUM_EXTRA_COLUMNS,
    expected_size_mb_min=5.0,
    expected_size_mb_max=80.0,
    seed=20260526,
    committed=True,
)

LARGE = TierSpec(
    name="large",
    rows=10_000_000,
    columns=COMMON_COLUMNS + MEDIUM_EXTRA_COLUMNS + LARGE_EXTRA_COLUMNS,
    expected_size_mb_min=2_000.0,
    expected_size_mb_max=12_000.0,
    seed=20260526,
    committed=False,
)


TIERS: dict[str, TierSpec] = {SMALL.name: SMALL, MEDIUM.name: MEDIUM, LARGE.name: LARGE}


def get_tier(name: str) -> TierSpec:
    """Return the TierSpec for ``name`` or raise KeyError with the valid set."""
    try:
        return TIERS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown tier {name!r}; valid tiers: {sorted(TIERS)}"
        ) from exc
