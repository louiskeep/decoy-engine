"""Canonical dtype labels across pandas majors (audit M5, 2026-06-12).

The profile and the distribution snapshot both emit dtype labels into
persisted payloads whose digests users hold as baselines. pandas 3
changed two default inference labels (str for strings, datetime64[us]
for datetimes); these cells pin the normalization that keeps pandas-2
era digests valid.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.internal.pandas_compat import canonical_dtype_label


class TestCanonicalDtypeLabel:
    def test_default_inferred_string_normalizes_to_object(self):
        assert canonical_dtype_label(pd.Series(["a", "b"]).dtype) == "object"

    def test_explicit_object_passes_through(self):
        assert canonical_dtype_label(pd.Series(["a"], dtype=object).dtype) == "object"

    def test_explicit_extension_string_dtypes_pass_through(self):
        # Explicit extension dtypes are NOT normalized -- whatever label
        # this pandas version gives them passes through verbatim (only
        # the default-inference labels are pinned).
        for req in ("string", "string[pyarrow]"):
            dtype = pd.Series(["a"], dtype=req).dtype
            assert canonical_dtype_label(dtype) == str(dtype)
            assert canonical_dtype_label(dtype) != "object"

    def test_datetime_resolution_normalizes_to_ns(self):
        inferred = pd.to_datetime(pd.Series(["2026-01-01", "2026-06-12"]))
        assert canonical_dtype_label(inferred.dtype) == "datetime64[ns]"

    def test_datetime_tz_suffix_preserved(self):
        tz = pd.to_datetime(pd.Series(["2026-01-01"])).dt.tz_localize("UTC")
        assert canonical_dtype_label(tz.dtype) == "datetime64[ns, UTC]"

    def test_non_string_dtypes_pass_through(self):
        assert canonical_dtype_label(pd.Series([1, 2]).dtype) == "int64"
        assert canonical_dtype_label(pd.Series([1.5]).dtype) == "float64"
        assert canonical_dtype_label(pd.Series([1], dtype="Int64").dtype) == "Int64"
