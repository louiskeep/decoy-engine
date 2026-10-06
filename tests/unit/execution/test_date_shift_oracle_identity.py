"""R0 item 4: the vectorised date_shift oracle is byte- and dtype-identical.

``_ReferenceDateShift`` is a frozen copy of the pre-R0 handler body (per-row
``derive`` loop and the list comprehension that assembles the output). The new
handler is compared with it on the pandas FRAME (dtype included) and cell by
cell, because pandas infers the output dtype from the exact scalar objects in
the assigned list and a vectorised assembly can silently change it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.determinism import derive
from decoy_engine.determinism._derive import DeriveContext
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.transforms.date_shift import _detect_format

_SEED = (0x42).to_bytes(8, "big")
_NS = "ds_ns"


class _ReferenceDateShift:
    """Pre-R0 implementation, group_by snapshot checks omitted (the tests that
    use group_by always supply an aligned snapshot)."""

    def run(self, df: pd.DataFrame, column: str, plan: ColumnSeed, ctx: Any) -> pd.DataFrame:
        cfg = provider_config_to_dict(plan.provider_config)
        min_days = int(cfg.get("min_days", -365))
        max_days = int(cfg.get("max_days", 365))
        if min_days > max_days:
            min_days, max_days = max_days, min_days
        range_size = max_days - min_days + 1
        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        fmt = cfg.get("date_format") or _detect_format(col)
        group_by = cfg.get("group_by")
        anchor_col = None
        if group_by:
            anchor_col = ctx.group_anchor_snapshots[(ctx.current_table, group_by)]
            if pd.api.types.is_extension_array_dtype(anchor_col.dtype):
                anchor_col = anchor_col.astype(object)
        parsed = pd.to_datetime(col, format=fmt, errors="coerce")
        unusable = parsed.isna().to_numpy()
        source_null = col.isna().to_numpy()
        shifts: list[int] = []
        for i, value in enumerate(col):
            if unusable[i]:
                shifts.append(0)
                continue
            if anchor_col is not None:
                group_value = anchor_col.iloc[i]
                if pd.isna(group_value):
                    anchor = value
                else:
                    anchor = group_value.item() if hasattr(group_value, "item") else group_value
            else:
                anchor = value
            digest = derive(ctx.mask_key, plan.namespace, _canonicalize_source(anchor))
            shifts.append(min_days + (int.from_bytes(digest[:8], "big") % range_size))
        shifted = parsed + pd.to_timedelta(shifts, unit="D")
        formatted = shifted.dt.strftime(fmt) if fmt else shifted.astype(str)
        out = [col.iloc[i] if unusable[i] else formatted.iloc[i] for i in range(len(col))]
        for i in range(len(col)):
            if unusable[i] and not source_null[i]:
                ctx.row_errors.append(
                    RowError(
                        column=column,
                        row_index=i,
                        trigger="format_error",
                        reason="value is not a parseable date under date_shift",
                    )
                )
        df[column] = out
        return df


def _plan(**cfg: Any) -> ColumnSeed:
    cfg.setdefault("date_format", "%Y-%m-%d")
    cfg.setdefault("min_days", -30)
    cfg.setdefault("max_days", 30)
    return ColumnSeed(
        namespace=_NS,
        strategy="date_shift",
        provider="date_shift",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(cfg.items()),
        coherent_with=(),
        when=None,
    )


def _ctx(snapshot: pd.Series | None = None) -> StrategyContext:
    ctx = StrategyContext(
        registry=None,  # type: ignore[arg-type]
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=_SEED,
    )
    object.__setattr__(ctx, "current_table", "t")
    if snapshot is not None:
        object.__setattr__(ctx, "group_anchor_snapshots", {("t", "pid"): snapshot})
    return ctx


def _same_cell(a: object, b: object) -> bool:
    if type(a) is not type(b):
        return False
    if a is None or a is pd.NA or a is pd.NaT:
        return a is b
    if isinstance(a, float) and np.isnan(a):
        return bool(np.isnan(b))  # type: ignore[arg-type]
    return bool(a == b)


def _compare(df: pd.DataFrame, plan: ColumnSeed, snapshot: pd.Series | None = None) -> None:
    ref_ctx, new_ctx = _ctx(snapshot), _ctx(snapshot)
    ref = _ReferenceDateShift().run(df.copy(), "d", plan, ref_ctx)
    new, warnings = DateShiftStrategyHandler().run(df.copy(), "d", plan, new_ctx)
    assert warnings == []
    pd.testing.assert_frame_equal(ref, new, check_dtype=True)
    assert ref["d"].dtype == new["d"].dtype
    assert list(ref["d"].index) == list(new["d"].index)
    for a, b in zip(ref["d"].tolist(), new["d"].tolist(), strict=True):
        assert _same_cell(a, b), (a, b)
    # Same row errors, same order (RowError equality is by value).
    assert new_ctx.row_errors == ref_ctx.row_errors


def _frame(values: list[Any], dtype: Any = object, index: Any = None) -> pd.DataFrame:
    return pd.DataFrame({"d": pd.Series(values, dtype=dtype, index=index)})


GOOD = ["2020-01-15", "2020-06-30", "1999-12-31", "2020-01-15"]

_CASES: dict[str, pd.DataFrame] = {
    "empty_object": _frame([]),
    "empty_float": _frame([], dtype="float64"),
    "nan_only_float64": _frame([np.nan, np.nan], dtype="float64"),
    "nan_only_object": _frame([np.nan, np.nan], dtype=object),
    "none_only": _frame([None, None], dtype=object),
    "nat_only": _frame([pd.NaT, pd.NaT], dtype="datetime64[ns]"),
    "string_na_only": _frame([pd.NA, pd.NA], dtype="string"),
    "all_null_float16": _frame([np.nan, np.nan], dtype="float16"),
    "all_null_float32": _frame([np.nan, np.nan], dtype="float32"),
    "all_null_float64": _frame([np.nan, np.nan], dtype="float64"),
    "float16_with_values": _frame([1.5, np.nan, 2.5], dtype="float16"),
    "float32_with_values": _frame([1.5, np.nan, 2.5], dtype="float32"),
    "float64_with_values": _frame([1.5, np.nan, 2.5], dtype="float64"),
    "mixed": _frame(["2020-01-15", None, "garbage", np.nan, "2021-03-04", "", "2020-13-45"]),
    "mixed_string_dtype": _frame(["2020-01-15", pd.NA, "garbage", "2021-03-04"], dtype="string"),
    "all_valid": _frame(GOOD),
    "all_unparseable": _frame(["x", "y", "z"]),
    "duplicate_index": _frame(GOOD, index=[0, 0, 1, 1]),
    "non_default_index": _frame(GOOD, index=[40, 30, 20, 10]),
    "string_index": _frame(GOOD, index=["a", "b", "c", "d"]),
    "multiple_row_errors": _frame(["bad1", "2020-01-01", "bad2", None, "bad3", "bad4"]),
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_frame_identity_with_pre_r0_implementation(name: str) -> None:
    plan = _plan(date_format="%Y-%m-%d")
    _compare(_CASES[name], plan)


@pytest.mark.parametrize("name", ["all_valid", "mixed", "all_unparseable", "empty_object"])
def test_frame_identity_with_auto_detected_format(name: str) -> None:
    _compare(_CASES[name], _plan(date_format=None))


def test_frame_identity_when_format_is_undetectable() -> None:
    # fmt is None -> the `astype(str)` assembly branch.
    df = _frame(["15 Jan 2020 weird", "also weird"])
    _compare(df, _plan(date_format=None))


def test_frame_identity_astype_str_branch_with_parseable_values() -> None:
    # No date_format and a format outside _COMMON_FORMATS cannot parse, so use
    # a column pandas parses without a format but _detect_format rejects.
    df = _frame(["2020-01-15 10:30:00", "2020-02-01 00:00:00"])
    _compare(df, _plan(date_format=None))


def test_large_integer_and_wide_range() -> None:
    _compare(_frame(GOOD), _plan(min_days=-50000, max_days=50000))
    _compare(_frame(GOOD), _plan(min_days=5, max_days=-5))


def _anchor_frame() -> pd.DataFrame:
    return _frame(["2020-01-01", "2020-02-01", None, "bad", "2020-05-05", "2020-06-06"])


@pytest.mark.parametrize(
    "anchor",
    [
        pd.Series([1, 1, 2, 2, 3, 3]),  # numpy int64
        pd.Series([True, True, False, False, True, True]),  # numpy bool
        pd.Series([1, 1, None, 2, 3, 3], dtype="Int64"),  # nullable int, one null
        pd.Series(["p1", "p1", "p2", "p2", "p3", "p3"]),
        pd.Series(["p1", None, "p2", np.nan, "p3", "p3"]),  # null anchors fall back
        pd.Series([2**62, 2**62, 2**63 - 1, 2**63 - 1, 7, 7]),  # large integers
        pd.Series([None] * 6, dtype=object),  # every anchor null
    ],
)
def test_group_by_anchor_identity(anchor: pd.Series) -> None:
    _compare(_anchor_frame(), _plan(group_by="pid"), snapshot=anchor)


def test_group_by_with_non_default_index() -> None:
    df = _frame(GOOD, index=[9, 8, 7, 6])
    anchor = pd.Series(["a", "a", "b", None], index=[9, 8, 7, 6])
    _compare(df, _plan(group_by="pid"), snapshot=anchor)


def test_row_errors_order_and_content() -> None:
    df = _frame(["bad1", "2020-01-01", "bad2", None, "bad3"])
    ctx = _ctx()
    DateShiftStrategyHandler().run(df.copy(), "d", _plan(), ctx)
    assert [e.row_index for e in ctx.row_errors] == [0, 2, 4]
    assert all(e.trigger == "format_error" for e in ctx.row_errors)


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.for_column_calls: list[tuple[bytes, str]] = []
        self.derive_sources_calls: list[tuple[str, list[bytes]]] = []
        real_for_column = DeriveContext.for_column.__func__  # type: ignore[attr-defined]
        real_derive_sources = DeriveContext.derive_sources
        spy = self

        def for_column(cls: type, seed: bytes, namespace: str) -> DeriveContext:
            spy.for_column_calls.append((seed, namespace))
            return real_for_column(cls, seed, namespace)

        def derive_sources(self_: DeriveContext, namespace: str, sources: Any) -> Any:
            materialized = list(sources)
            spy.derive_sources_calls.append((namespace, materialized))
            return real_derive_sources(self_, namespace, materialized)

        monkeypatch.setattr(DeriveContext, "for_column", classmethod(for_column))
        monkeypatch.setattr(DeriveContext, "derive_sources", derive_sources)


def test_one_batched_derivation_per_column(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    df = _frame(["2020-01-15", None, "bad", "2021-03-04", "2020-01-15"])
    DateShiftStrategyHandler().run(df, "d", _plan(), _ctx())
    assert spy.for_column_calls == [(_SEED, _NS)]
    assert len(spy.derive_sources_calls) == 1
    namespace, sources = spy.derive_sources_calls[0]
    assert namespace == _NS
    # Only usable rows, in row order, canonicalised exactly as the oracle does.
    assert sources == [_canonicalize_source(v) for v in ("2020-01-15", "2021-03-04", "2020-01-15")]


def test_batched_sources_use_normalised_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    df = _frame(["2020-01-01", "2020-02-01", None])
    anchor = pd.Series([np.bool_(True), np.bool_(False), np.bool_(True)])
    DateShiftStrategyHandler().run(df, "d", _plan(group_by="pid"), _ctx(anchor))
    _ns, sources = spy.derive_sources_calls[0]
    assert sources == [_canonicalize_source(True), _canonicalize_source(False)]


def test_no_derivation_when_no_usable_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(monkeypatch)
    DateShiftStrategyHandler().run(_frame([None, "bad"]), "d", _plan(), _ctx())
    assert spy.derive_sources_calls in ([], [(_NS, [])])


def test_tz_naive_datetime_source_fails_identically() -> None:
    # A datetime64 column canonicalises to a tz-naive Timestamp, which the
    # canonicaliser rejects; the new batched path must raise the same error.
    df = _frame(pd.to_datetime(["2020-01-15", "2021-01-01"]).tolist(), "datetime64[ns]")
    with pytest.raises(GenerationError) as ref:
        _ReferenceDateShift().run(df.copy(), "d", _plan(date_format=None), _ctx())
    with pytest.raises(GenerationError) as new:
        DateShiftStrategyHandler().run(df.copy(), "d", _plan(date_format=None), _ctx())
    assert new.value.code == ref.value.code
