"""Task 2.4: native passthrough/redact/truncate kernels vs the shipped handlers.

Every case here drives the REAL shipped handler (`PassthroughHandler` /
`RedactHandler` / `TruncateHandler`, through their `run(df, column, plan, ctx)`
entry point) and compares its observable output to the native kernel's Arrow
output on the same logical values. The native kernels never reimplement the
per-value logic; they call the same `kernel/_scalar.py` functions the
handlers already call, so parity here is a proof that the wrapper adds no
Python-round-trip drift, not a second copy of the masking formula.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._passthrough import PassthroughHandler
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._strategies._truncate import TruncateHandler
from decoy_engine.execution.native._kernels_scalar import (
    native_passthrough,
    native_redact,
    native_truncate,
)
from decoy_engine.plan._types import ColumnSeed


def _seed(strategy: str, provider_config: dict | None = None) -> ColumnSeed:
    cfg = provider_config or {}
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(cfg.items())),
    )


def _as_comparable(value):
    """Normalize a handler-observed scalar for equality: every null reads alike."""
    if pd.isna(value):
        return None
    return value


def _oracle_arrow_type(out_df: pd.DataFrame) -> pa.DataType:
    """The Arrow TYPE the shipped handler's output actually becomes on the write path.

    The handler mutates a pandas frame; the sequential/pandas sinks serialize it with
    `pa.Table.from_pandas` (see execution/_pandas_adapter.py, _sequential.py). That
    conversion, not the kernel, decides the on-disk Arrow type, so this is the type the
    native route must equal to be a drop-in. Comparing native `.type` against THIS
    (not just `.to_pylist()` values) is what catches an int-vs-double or string-vs-null
    divergence that Python value-equality hides."""
    return pa.Table.from_pandas(out_df, preserve_index=False).column("col").type


# ---------------------------------------------------------------------------
# passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "make_series"),
    [
        ("strings_with_null", lambda: pd.Series(["a", "b", None], dtype=object)),
        ("integers", lambda: pd.Series([1, 2, 3, None], dtype="Int64")),
        ("empty_strings", lambda: pd.Series(["", "x", ""], dtype=object)),
        ("all_null", lambda: pd.Series([None, None], dtype=object)),
        ("bool_with_null", lambda: pd.Series([True, False, None], dtype="boolean")),
    ],
)
def test_native_passthrough_matches_handler(label: str, make_series) -> None:
    series = make_series()
    df = pd.DataFrame({"col": series})
    out_df, warnings = PassthroughHandler().run(df.copy(), "col", _seed("passthrough"), ctx=None)
    assert warnings == []

    array = pa.array(series, from_pandas=True)
    native_arr = native_passthrough(array)
    shipped_out = [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_arr.to_pylist() == shipped_out, label
    # Type parity, not just value parity: passthrough is identity on the same
    # from_pandas array, so it must match the oracle's on-disk type exactly.
    assert native_arr.type == _oracle_arrow_type(out_df), label


def test_native_passthrough_is_pure_identity_no_state() -> None:
    # Calling twice with the same array yields the same result (no draw, no state).
    array = pa.array([1, 2, 3])
    assert native_passthrough(array).to_pylist() == native_passthrough(array).to_pylist()


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "make_series"),
    [
        ("object_strings", lambda: pd.Series(["alpha", "beta", None], dtype=object)),
        ("extension_int64", lambda: pd.Series([1, 2, None], dtype="Int64")),
        ("extension_string_pyarrow", lambda: pd.Series(["x", "y", None], dtype="string[pyarrow]")),
        ("empty_strings", lambda: pd.Series(["", "nonempty", None], dtype=object)),
    ],
)
def test_native_redact_matches_handler_default(label: str, make_series) -> None:
    series = make_series()
    df = pd.DataFrame({"col": series})
    out_df, warnings = RedactHandler().run(df.copy(), "col", _seed("redact"), ctx=None)
    assert warnings == []

    array = pa.array(series, from_pandas=True)
    native_arr = native_redact(array)
    native_out = native_arr.to_pylist()
    shipped_out = [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_out == shipped_out, label
    # Type parity with the oracle's on-disk type (default redact_with is a string,
    # matching every shipped disguise; a non-string redact_with is out of the admitted
    # contract and characterized separately below).
    assert native_arr.type == _oracle_arrow_type(out_df), label
    # default constant per RedactHandler / kernel.redact_array
    for value, source in zip(native_out, series.tolist(), strict=True):
        if pd.isna(source):
            assert value is None
        else:
            assert value == "REDACTED"


def test_native_redact_with_custom_redact_with_matches_handler() -> None:
    series = pd.Series(["a", "b", None], dtype=object)
    df = pd.DataFrame({"col": series})
    out_df, warnings = RedactHandler().run(
        df.copy(), "col", _seed("redact", {"redact_with": "XXXX"}), ctx=None
    )
    assert warnings == []

    array = pa.array(series, from_pandas=True)
    native_arr = native_redact(array, redact_with="XXXX")
    native_out = native_arr.to_pylist()
    shipped_out = [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_out == shipped_out
    assert native_out == ["XXXX", "XXXX", None]
    assert native_arr.type == _oracle_arrow_type(out_df)


def test_native_redact_extension_dtype_matches_handler_fallback_path() -> None:
    """Guard: a mixed-type Categorical forces the HANDLER onto its pandas
    `.where()` fallback (pa.array() raises ArrowException on mixed python
    types). The native kernel receives the same logical values as a plain
    list (mirroring `pandas_column_to_kernel_input`'s own list fallback for
    exactly this shape) and must match the handler's object-dtype result."""
    series = pd.Series(pd.Categorical(["a", 5, None]))
    # Guard against the coverage going silently vacuous if a future pyarrow accepts
    # this shape: the whole point is that the handler's ArrowException `.where()`
    # fallback is TAKEN, which requires the direct conversion to actually raise.
    with pytest.raises(pa.ArrowException):
        pa.array(series, from_pandas=True)

    df = pd.DataFrame({"col": series})
    out_df, warnings = RedactHandler().run(df.copy(), "col", _seed("redact"), ctx=None)
    assert warnings == []

    raw_values = [None if pd.isna(v) else v for v in series.tolist()]
    native_out = native_redact(raw_values).to_pylist()
    shipped_out = [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_out == shipped_out
    assert native_out == ["REDACTED", "REDACTED", None]


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "cfg", "series"),
    [
        ("head_default", {"length": 3}, pd.Series(["hello", "world", "foo"])),
        (
            "tail_from_end",
            {"length": 2, "from_end": True},
            pd.Series(["hello", "world", "ab"]),
        ),
        ("keep_tail_explicit", {"length": 3, "keep": "tail"}, pd.Series(["hello"])),
        (
            "mask_char_keep_tail",
            {"length": 4, "keep": "tail", "mask_char": "*"},
            pd.Series(["1234567890", "abc", None]),
        ),
        (
            "mask_char_keep_head",
            {"length": 4, "keep": "head", "mask_char": "*"},
            pd.Series(["1234567890"]),
        ),
        ("length_one_boundary", {"length": 1}, pd.Series(["hello", "a"])),
        ("empty_strings", {"length": 3}, pd.Series(["", "hi", None])),
        ("integers_stringified", {"length": 3}, pd.Series([12345, 67, None])),
        (
            "multi_byte_utf8",
            {"length": 2, "keep": "head"},
            pd.Series(["日本語", "\U0001f600\U0001f601\U0001f602", None]),
        ),
        (
            "combining_mark_code_points",
            # 'e' + combining acute (U+0301) is two code points; truncate must
            # count code points the same way `str.__getitem__` does, matching
            # the shared `truncate_array`, not a grapheme-cluster count.
            {"length": 2, "keep": "head"},
            pd.Series(["éclair", "café", None]),
        ),
        (
            "combining_mark_tail_mask",
            {"length": 3, "keep": "tail", "mask_char": "#"},
            pd.Series(["éclair"]),
        ),
    ],
)
def test_native_truncate_matches_handler(label: str, cfg: dict, series: pd.Series) -> None:
    df = pd.DataFrame({"col": series})
    out_df, warnings = TruncateHandler().run(df.copy(), "col", _seed("truncate", cfg), ctx=None)
    assert warnings == []

    length = cfg["length"]
    from_end_legacy = bool(cfg.get("from_end", False))
    # Mirror the handler's own resolution exactly: it maps from_end->keep ONLY when
    # keep is absent/None (TruncateHandler), so an explicit keep (even "") is preserved
    # rather than overridden -- `cfg.get("keep") or ...` would wrongly rewrite keep="".
    keep = cfg.get("keep")
    if keep is None:
        keep = "tail" if from_end_legacy else "head"
    mask_char = cfg.get("mask_char")

    array = pa.array(series, from_pandas=True)
    native_arr = native_truncate(array, length=length, keep=keep, mask_char=mask_char)
    shipped_out = [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_arr.to_pylist() == shipped_out, label
    # Type parity with the oracle's on-disk type. Every case here carries at least one
    # non-null value, so both paths land on string; the all-null degenerate case (where
    # the pandas round-trip infers null-type) is characterized separately below.
    assert native_arr.type == _oracle_arrow_type(out_df), label


def test_native_truncate_invalid_length_raises_same_code_as_handler() -> None:
    df = pd.DataFrame({"col": ["hello", "world"]})
    with pytest.raises(StrategyError) as handler_exc:
        TruncateHandler().run(df.copy(), "col", _seed("truncate", {"length": 0}), ctx=None)
    assert handler_exc.value.code == "truncate_length_invalid"

    array = pa.array(df["col"], from_pandas=True)
    with pytest.raises(StrategyError) as native_exc:
        native_truncate(array, length=0, keep="head", mask_char=None)
    assert native_exc.value.code == "truncate_length_invalid"
    assert native_exc.value.strategy == "truncate"


def test_native_truncate_invalid_length_type_raises_same_code_as_handler() -> None:
    array = pa.array(["hello", "world"])
    with pytest.raises(StrategyError) as native_exc:
        native_truncate(array, length="3", keep="head", mask_char=None)  # type: ignore[arg-type]
    assert native_exc.value.code == "truncate_length_invalid"


def test_native_truncate_invalid_keep_raises_same_code_as_handler() -> None:
    df = pd.DataFrame({"col": ["hello"]})
    with pytest.raises(StrategyError) as handler_exc:
        TruncateHandler().run(
            df.copy(), "col", _seed("truncate", {"length": 3, "keep": "middle"}), ctx=None
        )
    assert handler_exc.value.code == "truncate_keep_invalid"

    array = pa.array(df["col"], from_pandas=True)
    with pytest.raises(StrategyError) as native_exc:
        native_truncate(array, length=3, keep="middle", mask_char=None)
    assert native_exc.value.code == "truncate_keep_invalid"
    assert native_exc.value.strategy == "truncate"


def test_native_truncate_invalid_mask_char_multi_char_raises_same_code_as_handler() -> None:
    df = pd.DataFrame({"col": ["hello"]})
    with pytest.raises(StrategyError) as handler_exc:
        TruncateHandler().run(
            df.copy(),
            "col",
            _seed("truncate", {"length": 2, "keep": "tail", "mask_char": "XY"}),
            ctx=None,
        )
    assert handler_exc.value.code == "truncate_mask_char_invalid"

    array = pa.array(df["col"], from_pandas=True)
    with pytest.raises(StrategyError) as native_exc:
        native_truncate(array, length=2, keep="tail", mask_char="XY")
    assert native_exc.value.code == "truncate_mask_char_invalid"
    assert native_exc.value.strategy == "truncate"


def test_native_truncate_invalid_mask_char_non_string_raises_same_code_as_handler() -> None:
    df = pd.DataFrame({"col": ["hello"]})
    with pytest.raises(StrategyError) as handler_exc:
        TruncateHandler().run(
            df.copy(),
            "col",
            _seed("truncate", {"length": 2, "keep": "tail", "mask_char": 42}),
            ctx=None,
        )
    assert handler_exc.value.code == "truncate_mask_char_invalid"

    array = pa.array(df["col"], from_pandas=True)
    with pytest.raises(StrategyError) as native_exc:
        native_truncate(array, length=2, keep="tail", mask_char=42)  # type: ignore[arg-type]
    assert native_exc.value.code == "truncate_mask_char_invalid"


def test_native_truncate_default_keep_truncates_from_head() -> None:
    # Every other case in this file passes keep= explicitly, so a mutation of the
    # keep="head" DEFAULT (its only untested shape) survived every prior test; this
    # one omits keep entirely and asserts the default resolves to head-truncation,
    # matching TruncateHandler's own default.
    df = pd.DataFrame({"col": ["hello", "world", None]})
    out_df, warnings = TruncateHandler().run(
        df.copy(), "col", _seed("truncate", {"length": 3}), ctx=None
    )
    assert warnings == []

    array = pa.array(df["col"], from_pandas=True)
    native_arr = native_truncate(array, length=3, mask_char=None)
    shipped_out = [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_arr.to_pylist() == shipped_out
    assert native_arr.to_pylist() == ["hel", "wor", None]


def test_native_truncate_is_pure_no_state() -> None:
    array = pa.array(["hello", "world"])
    first = native_truncate(array, length=3, keep="head", mask_char=None).to_pylist()
    second = native_truncate(array, length=3, keep="head", mask_char=None).to_pylist()
    assert first == second


# ---------------------------------------------------------------------------
# Batch-schema stability: the property the out-of-core writer depends on. For the
# admitted string contract, redact and truncate must emit pa.string() for EVERY batch
# shape -- value-bearing, all-null, and empty -- so a column's batches concatenate
# under one Parquet schema. `redact_array` infers null for all-null/empty, so this
# pins native_redact's own stabilization (native_truncate inherits it from truncate_array).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "series"),
    [
        ("value_bearing", pd.Series(["alice", None, "bob"], dtype=object)),
        ("all_null", pd.Series([None, None], dtype=object)),
        ("empty", pd.Series([], dtype=object)),
    ],
)
def test_native_redact_and_truncate_emit_stable_string_type(label: str, series: pd.Series) -> None:
    array = pa.array(series, from_pandas=True)
    assert native_redact(array).type == pa.string(), f"redact {label}"
    assert native_truncate(array, length=3, keep="head", mask_char=None).type == pa.string(), (
        f"truncate {label}"
    )


# ---------------------------------------------------------------------------
# Known output-TYPE divergences from the pandas oracle (characterized, not hidden).
#
# The native route emits each strategy's stable string type (above); the pandas
# oracle's `pa.Table.from_pandas` round-trip infers a DIFFERENT type on three inputs.
# Two are degenerate but realistic streaming batch shapes (native emits the stable
# string a column needs; the oracle's type is the pandas artifact, reconciled in 2.7):
#   1. an all-null column -- pandas infers null-type.
#   2. an empty (zero-row) column -- pandas infers double (its empty-column default).
# The third is genuinely out of the admitted contract (excluded at eligibility):
#   3. a non-string `redact_with` -- no shipped disguise uses one; pandas promotes
#      int+null to double, native keeps the value's own type (int64).
# In every case native emits the stable string a streaming column needs; the oracle's
# type is a pandas inference artifact. Reconciling it (or excluding these at
# eligibility) is a Task 2.6/2.7 responsibility; these tests pin the divergence so it
# is documented, not silent. Values still agree -- a value-only test would miss the
# TYPE difference that reaches Parquet.
# ---------------------------------------------------------------------------


def test_native_redact_non_string_redact_with_type_diverges_from_pandas_oracle() -> None:
    series = pd.Series(["a", "b", None], dtype=object)
    df = pd.DataFrame({"col": series})
    out_df, _ = RedactHandler().run(df.copy(), "col", _seed("redact", {"redact_with": 7}), ctx=None)

    native_arr = native_redact(pa.array(series, from_pandas=True), redact_with=7)
    assert native_arr.to_pylist() == [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_arr.type == pa.int64()
    assert _oracle_arrow_type(out_df) == pa.float64()


def test_native_redact_all_null_column_type_diverges_from_pandas_oracle() -> None:
    series = pd.Series([None, None, None], dtype=object)
    df = pd.DataFrame({"col": series})
    out_df, _ = RedactHandler().run(df.copy(), "col", _seed("redact"), ctx=None)

    native_arr = native_redact(pa.array(series, from_pandas=True))
    assert native_arr.to_pylist() == [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_arr.type == pa.string()
    assert _oracle_arrow_type(out_df) == pa.null()


def test_native_redact_empty_column_type_diverges_from_pandas_oracle() -> None:
    series = pd.Series([], dtype=object)
    df = pd.DataFrame({"col": series})
    out_df, _ = RedactHandler().run(df.copy(), "col", _seed("redact"), ctx=None)

    native_arr = native_redact(pa.array(series, from_pandas=True))
    assert native_arr.to_pylist() == []
    assert native_arr.type == pa.string()
    assert _oracle_arrow_type(out_df) == pa.float64()


def test_native_truncate_all_null_column_type_diverges_from_pandas_oracle() -> None:
    series = pd.Series([None, None], dtype=float)
    df = pd.DataFrame({"col": series})
    out_df, _ = TruncateHandler().run(df.copy(), "col", _seed("truncate", {"length": 3}), ctx=None)

    native_arr = native_truncate(
        pa.array(series, from_pandas=True), length=3, keep="head", mask_char=None
    )
    assert native_arr.to_pylist() == [_as_comparable(v) for v in out_df["col"].tolist()]
    assert native_arr.type == pa.string()
    assert _oracle_arrow_type(out_df) == pa.null()


def test_native_truncate_empty_column_type_diverges_from_pandas_oracle() -> None:
    series = pd.Series([], dtype=object)
    df = pd.DataFrame({"col": series})
    out_df, _ = TruncateHandler().run(df.copy(), "col", _seed("truncate", {"length": 3}), ctx=None)

    native_arr = native_truncate(
        pa.array(series, from_pandas=True), length=3, keep="head", mask_char=None
    )
    assert native_arr.to_pylist() == []
    assert native_arr.type == pa.string()
    assert _oracle_arrow_type(out_df) == pa.float64()
