"""DPS-CODEC phase 5 hard regression guard: the carrier path must reproduce the
retained `dp_normalize` projection byte-for-byte for the number and text
carriers (guide sections 3.2/3.5, task "HARD REGRESSION GUARD").

The phase-1 codecs (`carriers.decode_number`/`decode_text`) were built to match
`dp_normalize._normalize_numeric`/`_normalize_categorical` exactly on the values
in their domain, so that wiring the fit through the typed-carrier layer changes
the released VECTOR for number/text columns not at all. `dp_normalize.py` is
retained precisely as this oracle (guide section 7 "regression SEEDS"; phase 7,
not yet run, would remove it). If a comparison here fails, the rewrite has
introduced a behaviour change in the number/text release -- STOP and root-cause,
do not paper over it.

"Byte-identical" for a numeric release is under OpenDP's ATOMIC f64 equality
(`==`), which is the equality the histogram binning consumes: `-0.0 == 0.0`, so
`decode_number`'s signed-zero normalization does not diverge from the oracle on
the released vector even though the two intermediate float reprs differ. For a
text release it is exact string identity.

The `flag` carrier is DELIBERATELY not compared to `_normalize_categorical`: a
`text` carrier drops a non-string cell where the old categorical path
STRINGIFIED it (guide section 3.2, "never `str()`"), and a `flag` carrier is a
new bool-domain mechanism the old path had no equivalent for. Those are
intended design changes, not regressions, and are pinned by
`test_text_carrier_is_strict_about_non_strings_by_design` below.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.quality.carrier_adapter import dataframe_to_carrier_table
from decoy_engine.quality.carriers import released_values
from decoy_engine.quality.dp_normalize import _normalize_categorical, _normalize_numeric


def _carrier_number(series: pd.Series, *, lower: float, upper: float) -> list[float]:
    """The numeric vector the carrier path feeds to OpenDP, coerced to plain
    `float` exactly as `quality.dp` does before `release_numeric`."""
    df = pd.DataFrame({"n": series})
    schema = {"n": {"kind": "numeric", "carrier": "number", "bounds": (lower, upper)}}
    released = released_values(dataframe_to_carrier_table(df, schema))
    return [float(v) for v in released["n"]]


def _carrier_text(series: pd.Series) -> list[str]:
    df = pd.DataFrame({"c": series})
    schema = {"c": {"kind": "categorical", "carrier": "text"}}
    released = released_values(dataframe_to_carrier_table(df, schema))
    return list(released["c"])


class TestNumberCarrierReproducesNormalizeNumeric:
    def test_reboxing_heavy_object_column_projects_identically(self):
        # One object column exercising every reboxing the codec must be
        # invariant under: int/float widening, None, out-of-domain clamp,
        # +/-inf clamp, zero-imag complex (keep real part), nonzero-imag complex
        # (drop), a float-parseable string (parsed, matching the oracle's bare
        # `float(raw)`), and a plain 0.
        raw = [1, 2.5, None, 200.0, -5.0, float("inf"), float("-inf"), 3 + 0j, 1 + 2j, "7", 0]
        series = pd.Series(raw, dtype=object)
        oracle = _normalize_numeric(series, lower=0.0, upper=100.0)
        carrier = _carrier_number(series, lower=0.0, upper=100.0)
        assert carrier == oracle
        # A non-empty, non-trivial vector -- guards against a vacuous both-empty
        # pass masking a real divergence.
        assert oracle == [1.0, 2.5, 100.0, 0.0, 100.0, 0.0, 3.0, 7.0, 0.0]

    def test_numpy_backed_widths_project_identically(self):
        for dtype in ("int64", "float64", "float32", "int8", "uint64"):
            series = pd.Series([0, 1, 5, 9], dtype=dtype)
            oracle = _normalize_numeric(series, lower=0.0, upper=10.0)
            assert _carrier_number(series, lower=0.0, upper=10.0) == oracle

    def test_boxing_invariance_matches_the_oracle_across_a_null_upcast(self):
        # The bug class the redesign kills: adding one null upcasts int64 ->
        # float64, reboxing every existing cell. Both the oracle and the carrier
        # path must project the shared rows to the identical vector.
        int_backed = pd.Series([1, 2, 3], dtype="int64")
        upcast = pd.Series([1, 2, 3, None])  # float64 with a trailing NaN
        assert _carrier_number(int_backed, lower=0.0, upper=10.0) == [1.0, 2.0, 3.0]
        assert _carrier_number(upcast, lower=0.0, upper=10.0) == [1.0, 2.0, 3.0]
        assert _carrier_number(int_backed, lower=0.0, upper=10.0) == _normalize_numeric(
            int_backed, lower=0.0, upper=10.0
        )
        assert _carrier_number(upcast, lower=0.0, upper=10.0) == _normalize_numeric(
            upcast, lower=0.0, upper=10.0
        )


class TestTextCarrierReproducesNormalizeCategorical:
    def test_string_column_with_nulls_nul_and_surrogate_projects_identically(self):
        # The agreement set for `text` vs the old categorical path: plain
        # strings pass through verbatim, nulls drop, a NUL-bearing label drops
        # (OpenDP truncates at NUL), a lone surrogate drops (not UTF-8
        # encodable). Numbers/bools are deliberately EXCLUDED here (see the
        # by-design test below).
        raw = ["CA", None, "NY", "a\x00b", "\ud800", "CA", "TX", "über"]
        series = pd.Series(raw, dtype=object)
        oracle = _normalize_categorical(series)
        carrier = _carrier_text(series)
        assert carrier == oracle
        assert oracle == ["CA", "NY", "CA", "TX", "über"]

    def test_numpy_str_backed_column_projects_identically(self):
        series = pd.Series(np.array(["a", "b", "a", "c"], dtype=object))
        assert _carrier_text(series) == _normalize_categorical(series)


def test_text_carrier_is_strict_about_non_strings_by_design():
    """Pin the DELIBERATE divergence (guide section 3.2), so a future reader
    does not mistake it for the regression the guard above protects against: the
    old categorical path stringified a numeric/bool cell, the `text` carrier
    drops it (never `str()`). This is intended, and it is why the byte-identity
    guarantee above is scoped to genuine `str` values."""
    series = pd.Series([1, True, "CA", 2.5], dtype=object)
    oracle = _normalize_categorical(series)  # stringifies the numbers/bool
    carrier = _carrier_text(series)  # drops them
    assert carrier == ["CA"]
    assert oracle != carrier
    # The oracle stringified everything; the carrier kept only the real string.
    assert set(oracle) == {"1", "CA", "2.5"}
