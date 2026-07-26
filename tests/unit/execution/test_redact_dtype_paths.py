"""`RedactHandler` dual-path (kernel + pandas-fallback) output parity.

`_strategies/_redact.py` tries `redact_array` (the kernel path) first and
falls back to a pandas `.where()` rewrite only when the input column can't
convert to a pyarrow array (`pa.ArrowException`, e.g. an object column mixing
incompatible python types). The two code paths must be observationally
equal: what a caller reads back from the column after `run()` (its only
contract) must not depend on which path executed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.plan._types import ColumnSeed


def _seed() -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=(("redact_with", "X"),),
    )


def _seed_no_redact_with() -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=(),
    )


def test_default_redact_with_is_the_default_constant() -> None:
    # With no `redact_with` config, every non-null value becomes the default
    # constant "REDACTED" (never None, which would blank the column).
    df = pd.DataFrame({"col": pd.Series(["a", "b", None], dtype=object)})
    out_df, warnings = RedactHandler().run(df, "col", _seed_no_redact_with(), ctx=None)
    observed = out_df["col"].tolist()
    assert observed[0] == "REDACTED"
    assert observed[1] == "REDACTED"
    assert pd.isna(observed[2])
    assert warnings == []


def test_extension_dtype_that_forces_fallback_is_dropped_to_object() -> None:
    # A mixed-type Categorical is an extension dtype AND makes the kernel's
    # pa.array() raise, so it hits the fallback branch that must astype(object)
    # before .where() can write the string cleanly.
    df = pd.DataFrame({"col": pd.Series(pd.Categorical(["a", 5, None]))})
    out_df, warnings = RedactHandler().run(df, "col", _seed(), ctx=None)
    observed = out_df["col"].tolist()
    assert observed[0] == "X"
    assert observed[1] == "X"
    assert pd.isna(observed[2])
    assert warnings == []


@pytest.mark.parametrize(
    ("label", "make_series"),
    [
        ("object_strings", lambda: pd.Series(["alpha", "beta", None], dtype=object)),
        ("extension_int64", lambda: pd.Series([1, 2, None], dtype="Int64")),
        ("extension_string_pyarrow", lambda: pd.Series(["x", "y", None], dtype="string[pyarrow]")),
        ("all_null", lambda: pd.Series([None, None, None], dtype=object)),
        # Mixed python types in one object column is what forces the kernel's
        # pa.array() conversion to raise, exercising the pandas fallback path.
        ("mixed_object_forces_fallback", lambda: pd.Series(["alpha", 5, None], dtype=object)),
    ],
)
def test_redact_handler_output_matches_across_dtypes(label: str, make_series) -> None:
    series = make_series()
    df = pd.DataFrame({"col": series})

    out_df, warnings = RedactHandler().run(df, "col", _seed(), ctx=None)

    # The kernel path nulls come back as pyarrow's None; the pandas fallback
    # path nulls come back as whatever `.where()` produces for the column's
    # dtype (float NaN for an object column, pd.NA for an extension dtype).
    # Both read as "missing" through pd.isna, which is the only thing a
    # caller observes post-write, so compare on that rather than identity.
    observed = out_df["col"].tolist()
    original = series.tolist()
    assert len(observed) == len(original), label
    for value, source in zip(observed, original, strict=True):
        if pd.isna(source):
            assert pd.isna(value), label
        else:
            assert value == "X", label
    assert warnings == []
