"""Mutation-kill tests for `_mem_estimate.py` (TQ isolated-substrate grade).

These pin the machine-observable outputs a `tq_mutate.py` survivor sweep found
un-pinned: the exact byte fields, fit/no-fit verdicts, and boundary comparisons
that the module's arithmetic produces. Each test targets one surviving mutant
by asserting the exact quantity that mutant changes -- not an inequality the
mutant would still satisfy. Companion to `test_mem_estimate.py`, which pins the
behavioral surface; this file closes the numeric gaps that survived it.

Ledger: docs/quality/mutation-ledgers/execution_mem_estimate.md.
"""

from __future__ import annotations

from decoy_engine.execution._mem_estimate import (
    ColumnSizeSpec,
    FkCardinalityInput,
    TableSizeSpec,
    default_fk_key_size_bytes,
    estimate_peak_bytes,
    estimator_basis_bytes,
    fits,
    raw_data_bytes,
)

_GB = 1024 * 1024 * 1024


def _int64_table(name: str, row_count: int) -> TableSizeSpec:
    return TableSizeSpec(
        name=name, row_count=row_count, columns=(ColumnSizeSpec(name="n", dtype="int64"),)
    )


# ---------------------------------------------------------------------------
# raw_data_bytes: the unpriceable branch must `continue`, never `break`
# ---------------------------------------------------------------------------


class TestRawDataBytesContinuesPastUnpriceable:
    def test_priceable_column_after_an_unpriceable_one_is_still_summed(self) -> None:
        """Kills raw_data_bytes mutmut_11 (`continue` -> `break`). With the
        unpriceable column FIRST, a `break` would abandon the rest of the
        table's columns, dropping the trailing int64 from priceable_bytes. The
        invariant: an unpriceable column is recorded but must not stop pricing
        the columns that follow it in the same table."""
        table = TableSizeSpec(
            name="t",
            row_count=1_000,
            columns=(
                ColumnSizeSpec(name="free_text", dtype="object", unpriceable=True),
                ColumnSizeSpec(name="n", dtype="int64"),
            ),
        )
        result = raw_data_bytes((table,))
        # The trailing int64 (1_000 * 8) is priced despite the earlier
        # unpriceable column; a `break` would leave this at 0.
        assert result.priceable_bytes == 1_000 * 8
        assert result.unpriceable_columns == (("t", "free_text"),)


# ---------------------------------------------------------------------------
# default_fk_key_size_bytes: object overhead + hash overhead both ADD
# ---------------------------------------------------------------------------


class TestDefaultFkKeySizeBytesExactSum:
    def test_zero_width_key_costs_object_plus_hash_overhead_exactly(self) -> None:
        """Kills default_fk_key_size_bytes mutmut_2 (`+ _HASH_ENTRY_OVERHEAD`
        -> `- _HASH_ENTRY_OVERHEAD`). The prior test only pinned `> 0`, which a
        subtracted overhead (89 -> 25) still satisfies. The invariant: the
        hash-slot overhead is ADDED on top of the key's object cost, so a
        zero-width key costs 57 (object) + 32 (hash slot) = 89 bytes."""
        assert default_fk_key_size_bytes(0.0) == 89


# ---------------------------------------------------------------------------
# estimator_basis_bytes: sequential FK-dedup term (default 0, and it MULTIPLIES)
# ---------------------------------------------------------------------------


class TestEstimatorBasisSequentialFkTerm:
    def test_sequential_basis_without_fk_is_exactly_the_working_set(self) -> None:
        """Kills estimator_basis_bytes mutmut_33 (`fk_bytes = 0` -> `= 1`). With
        no fk_cardinality the FK-dedup term must contribute nothing, so the
        basis equals the working set alone. A single 1_000-row int64 table has
        raw bytes 8_000, and the two-largest working set of one table is that
        same 8_000 -- a stray `+ 1` would make it 8_001."""
        table = (_int64_table("t", 1_000),)
        basis = estimator_basis_bytes(table, "sequential")
        assert basis.basis_bytes == 8_000

    def test_sequential_fk_term_multiplies_count_by_size(self) -> None:
        """Kills estimator_basis_bytes mutmut_36 (`distinct * size` ->
        `distinct / size`). The FK-dedup working set is count TIMES per-key
        bytes: 10 keys * 64 bytes = 640, added to the 8_000-byte working set =
        8_640. Division would give 8_000 + 0.15625 instead."""
        table = (_int64_table("t", 1_000),)
        fk = FkCardinalityInput(distinct_key_count=10, key_size_bytes=64)
        basis = estimator_basis_bytes(table, "sequential", fk_cardinality=fk)
        assert basis.basis_bytes == 8_000 + 640


# ---------------------------------------------------------------------------
# fits: error-band boundary, fk_cardinality forwarding, and the <-vs-<= margin
# ---------------------------------------------------------------------------


class TestFitsBoundaries:
    def test_error_band_of_zero_is_accepted_not_rejected(self) -> None:
        """Kills fits mutmut_1 (`error_band < 0` -> `<= 0`). Zero is a VALID
        (tightest) error band -- the guard rejects only NEGATIVE bands. The
        mutant would raise ValueError on error_band=0.0; the contract returns a
        verdict. With a budget far above the estimate it must return True."""
        table = (_int64_table("t", 1_000),)
        assert fits(table, "full_frame", 100 * _GB, error_band=0.0) is True

    def test_fk_cardinality_is_forwarded_into_the_estimate(self) -> None:
        """Kills fits mutmut_10 and mutmut_13 (both drop the fk_cardinality
        argument to estimate_peak_bytes, defaulting it to None). On the
        sequential path a large FK dedup set materially raises the estimate;
        dropping it under-estimates. Budget is placed between the two margins so
        the with-fk verdict (does NOT fit) and the fk-ignored verdict (fits)
        diverge -- proving the argument reaches the estimate."""
        table = (_int64_table("t", 1_000),)
        fk = FkCardinalityInput(distinct_key_count=1_000_000, key_size_bytes=64)

        est_with = estimate_peak_bytes(table, "sequential", fk_cardinality=fk).estimated_bytes
        est_without = estimate_peak_bytes(table, "sequential").estimated_bytes
        assert est_with is not None
        assert est_without is not None

        margin_with = est_with * 1.30
        margin_without = est_without * 1.30
        # Strictly between the two margins: the honest (fk-forwarded) call sees
        # margin_with >= budget -> does not fit; a call that ignored fk would
        # see margin_without < budget -> fits. They must not coincide.
        budget = int((margin_with + margin_without) / 2)
        assert margin_without < budget < margin_with

        assert fits(table, "sequential", budget, fk_cardinality=fk) is False

    def test_margin_equal_to_budget_does_not_fit(self) -> None:
        """Kills fits mutmut_26 (`margin < budget` -> `margin <= budget`). At
        the exact boundary the verdict must be "does not fit": a budget equal to
        the required margin leaves zero headroom, and the asymmetric §3.6 rule
        admits only a STRICT under-fit. With error_band=0.0 the margin is the
        estimate itself, so budget == estimate is the boundary."""
        table = (_int64_table("t", 1_000),)
        estimate = estimate_peak_bytes(table, "full_frame").estimated_bytes
        assert estimate is not None
        # margin at error_band=0.0 is exactly the estimate; budget == margin.
        assert fits(table, "full_frame", estimate, error_band=0.0) is False
