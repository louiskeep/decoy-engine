"""DPS-CODEC pinned golden vectors for the number and text carriers (guide
sections 3.2/3.5).

Phase 5 built these as a hard regression guard comparing the carrier path
against `dp_normalize._normalize_numeric`/`_normalize_categorical`, the module
the carrier layer was designed to reproduce byte-for-byte on the values in
their shared domain. Phase 7 retired `dp_normalize.py` once the carrier suite
(`test_carriers.py`) was shown to subsume its whole test matrix, so this file
no longer compares against that oracle. What it still protects is unchanged:
these are the exact expected vectors the phase-5 comparison established,
kept as standalone pinned values so the carrier path's release for a
reboxing-heavy input stays fixed even as the surrounding code moves. If a
comparison here fails, the carrier path's number/text release has changed --
STOP and root-cause, do not paper over it.

"Byte-identical" for a numeric release is under OpenDP's ATOMIC f64 equality
(`==`), which is the equality the histogram binning consumes: `-0.0 == 0.0`,
so `decode_number`'s signed-zero normalization does not diverge from these
pinned vectors even though intermediate float reprs can differ.

The `flag` carrier has no equivalent here: it is a bool-domain mechanism with
no counterpart in the values pinned below, and `test_text_carrier_is_strict_
about_non_strings_by_design` pins its own deliberate divergence from the
retired categorical path (a `text` carrier drops a non-string cell where the
old path stringified it) separately.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.quality.carrier_adapter import dataframe_to_carrier_table
from decoy_engine.quality.carriers import released_values


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


class TestNumberCarrierGoldenVectors:
    def test_reboxing_heavy_object_column_projects_to_the_pinned_vector(self):
        # One object column exercising every reboxing the codec must be
        # invariant under: int/float widening, None, out-of-domain clamp,
        # +/-inf clamp, zero-imag complex (keep real part), nonzero-imag complex
        # (drop), a float-parseable string (parsed, matching bare `float(raw)`),
        # and a plain 0.
        raw = [1, 2.5, None, 200.0, -5.0, float("inf"), float("-inf"), 3 + 0j, 1 + 2j, "7", 0]
        series = pd.Series(raw, dtype=object)
        assert _carrier_number(series, lower=0.0, upper=100.0) == [
            1.0,
            2.5,
            100.0,
            0.0,
            100.0,
            0.0,
            3.0,
            7.0,
            0.0,
        ]

    def test_numpy_backed_widths_project_to_the_pinned_vector(self):
        for dtype in ("int64", "float64", "float32", "int8", "uint64"):
            series = pd.Series([0, 1, 5, 9], dtype=dtype)
            assert _carrier_number(series, lower=0.0, upper=10.0) == [0.0, 1.0, 5.0, 9.0]

    def test_boxing_invariance_matches_the_pinned_vector_across_a_null_upcast(self):
        # The bug class the redesign kills: adding one null upcasts int64 ->
        # float64, reboxing every existing cell. Both the pre- and post-upcast
        # frames must project the shared rows to the identical vector.
        int_backed = pd.Series([1, 2, 3], dtype="int64")
        upcast = pd.Series([1, 2, 3, None])  # float64 with a trailing NaN
        assert _carrier_number(int_backed, lower=0.0, upper=10.0) == [1.0, 2.0, 3.0]
        assert _carrier_number(upcast, lower=0.0, upper=10.0) == [1.0, 2.0, 3.0]


class TestTextCarrierGoldenVectors:
    def test_string_column_with_nulls_nul_and_surrogate_projects_to_the_pinned_vector(self):
        # Plain strings pass through verbatim, nulls drop, a NUL-bearing label
        # drops (OpenDP truncates at NUL), a lone surrogate drops (not UTF-8
        # encodable). Numbers/bools are deliberately EXCLUDED here (see the
        # by-design test below).
        raw = ["CA", None, "NY", "a\x00b", "\ud800", "CA", "TX", "über"]
        series = pd.Series(raw, dtype=object)
        assert _carrier_text(series) == ["CA", "NY", "CA", "TX", "über"]

    def test_numpy_str_backed_column_projects_to_the_pinned_vector(self):
        series = pd.Series(np.array(["a", "b", "a", "c"], dtype=object))
        assert _carrier_text(series) == ["a", "b", "a", "c"]


def test_text_carrier_is_strict_about_non_strings_by_design():
    """Pin the DELIBERATE divergence from the retired `dp_normalize` categorical
    path (guide section 3.2): that path stringified a numeric/bool cell, the
    `text` carrier drops it (never `str()`)."""
    series = pd.Series([1, True, "CA", 2.5], dtype=object)
    assert _carrier_text(series) == ["CA"]
