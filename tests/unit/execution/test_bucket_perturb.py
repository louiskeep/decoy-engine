"""SP-08b datetime tests: bucket_perturb strategy + date_shift determinism (TDD: tests first).

Tests cover:
  D5.1 - bucket_perturb: week bucket snaps dates to the same ISO week.
  D5.2 - bucket_perturb: month bucket snaps dates to the same calendar month.
  D5.3 - bucket_perturb: quarter bucket snaps dates to the same calendar quarter.
  D5.4 - bucket_perturb: deterministic - same value -> same output across runs.
  D5.5 - bucket_perturb: null values preserved.
  D5.6 - date_shift: same (entity, value) -> same shifted date within a namespace.
  D5.7 - Integration through the real plan/run path (STRATEGY-WIRING GUARD).

Methodology: Date-range perturbation based on ISO calendar periods (ISO 8601).
  bucket_perturb truncates dates to the start of a time bucket, then adds a
  deterministic per-value offset derived from derive(job_seed, namespace, value).
  ISO 8601 week/month/quarter boundaries are the established partitioning scheme.
  See: https://www.iso.org/iso-8601-date-and-time-format.html
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.transforms.bucket_perturb import (
    _bucket_start_and_size,
    _perturb_date,
    apply_bucket_perturb,
    validate_bucket_perturb_config,
)

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = b"\xb0\x0c\xfe\x00\x12\x34\x56\x78"  # 8 bytes (derive() contract)


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(col_name: str, seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=((col_name, seed),), per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> list:
    result = PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    return result.output.column(next(iter(table.schema.names))).to_pylist()


# ── D5.1: Week bucket ─────────────────────────────────────────────────────────


class TestBucketPerturbWeek:
    """bucket_perturb with bucket=week: output dates land in the same ISO week."""

    def test_week_bucket_output_in_same_iso_week(self):
        """Dates within the same ISO week must produce output in the same ISO week."""
        # 2024-01-08 and 2024-01-09 are in the same ISO week (week 2, 2024).
        dates = ["2024-01-08", "2024-01-09"]
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "week"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        # Each output must be a valid date string in the same ISO week as input.
        out_dates = [datetime.date.fromisoformat(v) for v in out]
        in_dates = [datetime.date.fromisoformat(d) for d in dates]
        for out_d, in_d in zip(out_dates, in_dates, strict=True):
            assert out_d.isocalendar()[:2] == in_d.isocalendar()[:2], (
                f"Input {in_d} (ISO {in_d.isocalendar()}) -> output {out_d} "
                f"(ISO {out_d.isocalendar()}) should share the same ISO year+week."
            )

    def test_week_bucket_changes_day_within_week(self):
        """bucket_perturb with week should shift the day within the ISO week."""
        dates = ["2024-01-08"] * 5  # Monday
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "week"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        # All outputs should be the same (deterministic per value).
        assert len(set(out)) == 1, "Same input value must produce same output (deterministic)."


# ── D5.2: Month bucket ────────────────────────────────────────────────────────


class TestBucketPerturbMonth:
    """bucket_perturb with bucket=month: output dates land in the same calendar month."""

    def test_month_bucket_output_in_same_month(self):
        """Dates in the same month must produce output dates also in that month."""
        dates = ["2024-03-05", "2024-03-20"]
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        out_dates = [datetime.date.fromisoformat(v) for v in out]
        in_dates = [datetime.date.fromisoformat(d) for d in dates]
        for out_d, in_d in zip(out_dates, in_dates, strict=True):
            assert (out_d.year, out_d.month) == (in_d.year, in_d.month), (
                f"Input {in_d} -> output {out_d} must be in same year-month."
            )

    def test_month_bucket_output_is_valid_date_in_month(self):
        """Output day must be a valid calendar day for the input month."""
        dates = ["2024-02-10"]  # February (28 or 29 days)
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        out_d = datetime.date.fromisoformat(out[0])
        assert out_d.month == 2, f"Expected February output, got {out_d}"
        assert 1 <= out_d.day <= 29, f"Day {out_d.day} is out of range for February."


# ── D5.3: Quarter bucket ──────────────────────────────────────────────────────


class TestBucketPerturbQuarter:
    """bucket_perturb with bucket=quarter: output dates land in the same quarter."""

    def test_quarter_bucket_output_in_same_quarter(self):
        """Dates in Q1 must produce output dates also in Q1."""
        dates = ["2024-01-15", "2024-03-20"]  # Both in Q1
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "quarter"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        out_dates = [datetime.date.fromisoformat(v) for v in out]
        in_dates = [datetime.date.fromisoformat(d) for d in dates]
        for out_d, in_d in zip(out_dates, in_dates, strict=True):
            in_q = (in_d.month - 1) // 3
            out_q = (out_d.month - 1) // 3
            assert (out_d.year, out_q) == (in_d.year, in_q), (
                f"Input {in_d} (Q{in_q + 1}) -> output {out_d} (Q{out_q + 1}) must share quarter."
            )


# ── D5.4: Determinism ─────────────────────────────────────────────────────────


class TestBucketPerturbDeterminism:
    """bucket_perturb must produce the same output for the same (seed, namespace, value)."""

    def test_deterministic_across_runs(self):
        """Two identical runs produce identical outputs."""
        dates = ["2024-06-15", "2024-06-20", "2024-06-15"]
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        )
        plan = _plan("d", seed)
        out1 = _run(plan, src)
        out2 = _run(plan, src)
        assert out1 == out2, "bucket_perturb must be deterministic across runs."

    def test_same_value_same_output_across_positions(self):
        """Same input value at different row positions must produce same output."""
        dates = ["2024-06-15", "2024-06-20", "2024-06-15"]
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        assert out[0] == out[2], (
            f"Same input value at positions 0 and 2 must produce same output: {out[0]!r} vs {out[2]!r}"
        )


# ── D5.5: Null preservation ───────────────────────────────────────────────────


class TestBucketPerturbNullPreservation:
    def test_null_values_preserved(self):
        """Null input values must produce null output (not crash or fill)."""
        src = pa.table({"d": ["2024-06-15", None, "2024-06-20"]})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        assert out[1] is None, f"Null input must produce null output, got {out[1]!r}"
        assert out[0] is not None, "Non-null inputs must still be processed."


# ── D5.6: date_shift determinism (existing strategy, integration test) ─────────


class TestDateShiftDeterminism:
    """date_shift: same (entity, namespace, value) -> same shifted date.

    This verifies the S9 wiring: derive(job_seed, namespace, value) -> offset.
    Longitudinal order survives because the shift is per-value, not per-row-position.
    """

    def test_same_value_same_shift_within_namespace(self):
        """Same source date value -> same shifted date in same namespace."""
        src = pa.table({"d": ["2020-01-15", "2020-06-30", "2020-01-15"]})
        seed = _col(
            "date_shift",
            namespace="medical_dates",
            provider_config=(("min_days", -30), ("max_days", 30), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        assert out[0] == out[2], (
            f"Same input date must produce same shifted date: {out[0]!r} vs {out[2]!r}"
        )

    def test_date_shift_output_within_declared_range(self):
        """Shifted dates must be within the declared [min_days, max_days] window."""
        original = "2020-06-15"
        min_days = -14
        max_days = 14
        src = pa.table({"d": [original] * 10})
        seed = _col(
            "date_shift",
            namespace="dates",
            provider_config=(
                ("min_days", min_days),
                ("max_days", max_days),
                ("date_format", "%Y-%m-%d"),
            ),
        )
        out = _run(_plan("d", seed), src)
        orig_date = datetime.date.fromisoformat(original)
        for shifted_str in out:
            shifted = datetime.date.fromisoformat(shifted_str)
            delta = abs((shifted - orig_date).days)
            assert delta <= max_days, (
                f"Shift of {delta} days exceeds declared max_days={max_days}: "
                f"{original} -> {shifted_str}"
            )

    def test_date_shift_determinism_cross_run(self):
        """Two runs with same seed produce byte-identical output."""
        src = pa.table({"d": ["2020-01-15", "2020-06-30", None]})
        seed = _col(
            "date_shift",
            namespace="dates",
            provider_config=(("min_days", -10), ("max_days", 10), ("date_format", "%Y-%m-%d")),
        )
        plan = _plan("d", seed)
        out1 = _run(plan, src)
        out2 = _run(plan, src)
        assert out1 == out2


# ── D5.7: bucket_perturb integration through plan/run ─────────────────────────


class TestBucketPerturbIntegration:
    """STRATEGY-WIRING GUARD: bucket_perturb through PandasExecutionAdapter."""

    def test_bucket_perturb_wired_through_adapter(self):
        """bucket_perturb strategy registered in SCALAR_HANDLERS and runs end-to-end."""
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        assert "bucket_perturb" in SCALAR_HANDLERS, (
            "bucket_perturb must be registered in SCALAR_HANDLERS."
        )

        dates = ["2024-01-15", "2024-02-20", "2024-03-25"]
        src = pa.table({"d": dates})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        )
        out = _run(_plan("d", seed), src)
        assert len(out) == 3
        for v in out:
            # Should be a valid ISO date string.
            assert v is not None
            datetime.date.fromisoformat(str(v))  # must not raise

    def test_bucket_perturb_requires_namespace(self):
        """bucket_perturb without namespace must raise StrategyError."""
        from decoy_engine.execution._errors import StrategyError

        src = pa.table({"d": ["2024-01-15"]})
        seed = _col(
            "bucket_perturb",
            namespace=None,  # missing namespace
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        )
        plan = _plan("d", seed)
        with pytest.raises(StrategyError, match="namespace") as exc:
            PandasExecutionAdapter().run_single(
                plan,
                src,
                registry=_REG,
                relationship_graph=_GRAPH,
                namespace_registry=_NS,
            )
        # pin the machine-readable fields (match= only checks the message prose)
        assert exc.value.code == "bucket_perturb_requires_namespace"
        assert exc.value.strategy == "bucket_perturb"

    def test_bucket_defaults_to_month_when_absent(self):
        """An absent bucket key resolves to "month" (a valid bucket), so the fit
        succeeds. Pins the `str(cfg.get("bucket", "month"))` default: a mutated
        default (None, "", or a bogus label) would make the validator reject an
        otherwise-valid config."""
        src = pa.table({"d": ["2024-01-15", "2024-01-20"]})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("date_format", "%Y-%m-%d"),),  # no bucket key
        )
        vals = _run(_plan("d", seed), src)
        # month bucket: both inputs are January 2024, so both outputs stay in 2024-01
        assert all(str(v).startswith("2024-01") for v in vals)

    def test_invalid_bucket_raises_strategy_error_not_silent_quarter(self):
        """D5.8 - bucket='garbage' must raise StrategyError, NOT silently return quarter output.

        Fail-closed validator wiring: an unrecognized bucket must be caught before
        apply_bucket_perturb is called. Without the fix, the fallthrough in
        _bucket_start_and_size silently treats any unknown string as 'quarter',
        violating the operator's stated privacy intent.
        """
        from decoy_engine.execution._errors import StrategyError

        src = pa.table({"d": ["2024-06-15"]})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "weekly_typo"), ("date_format", "%Y-%m-%d")),
        )
        plan = _plan("d", seed)
        with pytest.raises(StrategyError, match="weekly_typo") as exc:
            PandasExecutionAdapter().run_single(
                plan,
                src,
                registry=_REG,
                relationship_graph=_GRAPH,
                namespace_registry=_NS,
            )
        assert exc.value.code == "bucket_perturb_invalid_config"
        assert exc.value.strategy == "bucket_perturb"

    def test_configured_date_format_is_honored_over_autodetect(self):
        """An explicit date_format must drive parsing, not auto-detection. For a
        day-first ambiguous date ("05-02-2024" = 5 Feb under %d-%m-%Y), auto-detect
        picks month-first, so a mutant that drops/nulls date_format lands the value
        in a different month. Pins that date_format is actually consulted."""
        import datetime

        src = pa.table({"d": ["05-02-2024"]})
        seed = _col(
            "bucket_perturb",
            namespace="dates",
            provider_config=(("bucket", "month"), ("date_format", "%d-%m-%Y")),
        )
        (out,) = _run(_plan("d", seed), src)
        # parsed under the configured day-first format, the bucketed date is in Feb
        assert datetime.datetime.strptime(str(out), "%d-%m-%Y").month == 2

    def test_valid_buckets_still_work_after_validation_wiring(self):
        """D5.9 - validate_bucket_perturb_config wiring does not break week/month/quarter."""
        for bucket_name in ("week", "month", "quarter"):
            src = pa.table({"d": ["2024-06-15"]})
            seed = _col(
                "bucket_perturb",
                namespace="dates",
                provider_config=(("bucket", bucket_name), ("date_format", "%Y-%m-%d")),
            )
            out = _run(_plan("d", seed), src)
            assert len(out) == 1
            assert out[0] is not None
            datetime.date.fromisoformat(str(out[0]))


# ── TQ mutation-kill layer: direct core-function KATs ─────────────────────────
# These drive transforms/bucket_perturb.py directly (bypassing the handler) so
# the bucket arithmetic, offset derivation, format handling, and passthrough
# branches are pinned to specific input-date -> output-date answers.


class TestBucketStartAndSizeCore:
    """`_bucket_start_and_size` quarter arithmetic (KAT: date -> exact window)."""

    def test_quarter_start_is_first_day_of_the_quarters_first_month(self):
        """Each quarter snaps to (year, {1,4,7,10}, day=1). Apr/Jul/Oct expose a
        wrong `// 4` divisor (they land in the prior quarter) and Jan/Apr expose a
        start day mutated off 1."""
        expected = {
            datetime.date(2024, 2, 10): datetime.date(2024, 1, 1),  # Q1
            datetime.date(2024, 4, 15): datetime.date(2024, 4, 1),  # Q2: // 4 -> Jan
            datetime.date(2024, 7, 15): datetime.date(2024, 7, 1),  # Q3: // 4 -> Apr
            datetime.date(2024, 10, 15): datetime.date(2024, 10, 1),  # Q4: // 4 -> Jul
        }
        for d, want_start in expected.items():
            start, _ = _bucket_start_and_size(d, "quarter")
            assert start == want_start, f"{d} -> {start}, want {want_start}"

    def test_quarter_size_is_the_exact_day_count(self):
        """Q1 2024 (leap) spans Jan 1..Mar 31 = 91 days; off-by-one size mutants
        (`+ 1` -> `- 1` / `+ 2`) would report 89 / 92."""
        _, size = _bucket_start_and_size(datetime.date(2024, 1, 15), "quarter")
        assert size == 91

    def test_month_size_matches_calendar(self):
        """Leap February resolves to 29 days."""
        _, size = _bucket_start_and_size(datetime.date(2024, 2, 15), "month")
        assert size == 29

    def test_unrecognized_bucket_raises_valueerror(self):
        """The defense-in-depth fallthrough raises ValueError naming the bucket.
        A `sorted(None)` mutant would raise TypeError instead; a nulled message
        would drop the offending value from the text."""
        with pytest.raises(ValueError, match="unrecognized bucket 'garbage'"):
            _bucket_start_and_size(datetime.date(2024, 1, 1), "garbage")


class TestPerturbDateOffsetKnownAnswer:
    """`_perturb_date` offset uses digest[:8]; a pinned answer catches slice drift."""

    def test_perturb_date_is_the_pinned_known_answer(self):
        """Fixed seed+namespace+value map to a fixed in-bucket date. Widening the
        digest slice (`[:8]` -> `[:9]`) changes the offset and thus the output."""
        assert _perturb_date(
            datetime.date(2024, 6, 15), "month", _SEED, "dates", "2024-06-15"
        ) == datetime.date(2024, 6, 9)
        assert _perturb_date(
            datetime.date(2024, 6, 15), "quarter", _SEED, "dates", "2024-06-15"
        ) == datetime.date(2024, 6, 3)


class TestApplyBucketPerturbCore:
    """`apply_bucket_perturb` format handling, parse-failure, and null branches."""

    def test_valid_value_is_bucketed_to_its_known_answer(self):
        """A parseable value must be snapped into its bucket, not passed through.
        Pins the exact output so a nulled `fmt`, an inverted `fmt is None` guard,
        or an `&`->`|` parse-mask flip (all of which pass the value through
        unchanged) are caught."""
        out = apply_bucket_perturb(pd.Series(["2024-06-15"]), "month", _SEED, "dates", "%Y-%m-%d")
        assert list(out) == ["2024-06-09"]

    def test_extension_array_dtype_input_is_processed(self):
        """An extension-dtype (pandas `string`) column is materialized and bucketed;
        nulling the series or `astype(None)` in that branch would crash."""
        out = apply_bucket_perturb(
            pd.Series(["2024-06-15"], dtype="string"), "month", _SEED, "dates", "%Y-%m-%d"
        )
        assert list(out) == ["2024-06-09"]

    def test_autodetect_used_when_date_format_is_none(self):
        """With date_format=None the format is detected from the data; passing None
        into detection instead of the series would crash."""
        out = apply_bucket_perturb(pd.Series(["2024-06-15"]), "month", _SEED, "dates", None)
        assert list(out) == ["2024-06-09"]

    def test_unparseable_value_passes_through_unchanged(self):
        """An unparseable cell is preserved verbatim while its neighbours are
        bucketed. Pins `errors="coerce"` (else the parse raises) and the `~null_mask`
        term (else the NaT cell is processed, not skipped)."""
        out = apply_bucket_perturb(
            pd.Series(["2024-06-15", "not-a-date", "2024-08-20"]),
            "month",
            _SEED,
            "dates",
            "%Y-%m-%d",
        )
        assert list(out) == ["2024-06-09", "not-a-date", "2024-08-14"]

    def test_value_after_a_null_is_still_bucketed(self):
        """A null cell is skipped with `continue`, not `break`; the value after it
        must still be processed."""
        out = apply_bucket_perturb(
            pd.Series(["2024-06-15", None, "2024-08-20"]),
            "month",
            _SEED,
            "dates",
            "%Y-%m-%d",
        )
        assert out.iloc[0] == "2024-06-09"
        assert out.iloc[1] is None
        assert out.iloc[2] == "2024-08-14"


class TestValidateConfigCore:
    """`validate_bucket_perturb_config` guard on a missing bucket."""

    def test_missing_bucket_raises_valueerror(self):
        """A falsy/absent bucket is rejected with a ValueError naming the field.
        A `sorted(None)` mutant raises TypeError; a nulled message drops the text."""
        with pytest.raises(ValueError, match="'bucket' is required"):
            validate_bucket_perturb_config({})
