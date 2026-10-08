"""TQ sweep: oracle cells for the `when:` predicate gate (pandas).

These pin the machine-observable behavior of `_when_gate` that the broader
regression suite left unguarded: the subset-relative row-error remap (B1),
the unconditional `preflight` call and its argument order, the no-gate and
gated dispatch argument threading, the strategy attribution on every typed
error, the `numexpr_required` import-failure path, and the numexpr scope
clamp (empty local/global dicts) that blocks `@var` walks into the module's
own locals and globals.

Assertions target machine fields only (`.code`, `.strategy`, `RowError`
positional fields), never error prose. Expected values are hardcoded, not
recomputed from the module.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._when_gate import run_with_when_gate
from decoy_engine.plan._types import ColumnSeed


def _seed(*, when: str | None = None, strategy: str = "redact") -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=(),
        when=when,
    )


class _Ctx:
    """Minimal stand-in for StrategyContext's mutable row_errors sink."""

    def __init__(self) -> None:
        self.row_errors: list[RowError] = []


# ── row-error remap: subset-relative -> full-table (B1) ───────────────


class _PandasAppendingHandler:
    """Appends one RowError at a subset-relative row_index, no-op otherwise."""

    name = "appending"

    def __init__(self, sub_row_index: int) -> None:
        self._sub = sub_row_index

    def run(self, df, column, plan, ctx):
        ctx.row_errors.append(
            RowError(
                column=column,
                row_index=self._sub,
                trigger="mask_error",
                reason="boom",
            )
        )
        return df, []


def test_pandas_gate_remaps_new_error_and_leaves_prior_untouched():
    # flag matches full positions 1,2,3; subset-relative 1 -> full 2.
    df = pd.DataFrame({"v": ["a", "b", "c", "d"], "flag": [0, 1, 1, 1]})
    ctx = _Ctx()
    # A pre-existing error from an earlier node must NOT be remapped.
    ctx.row_errors.append(
        RowError(column="other", row_index=0, trigger="format_error", reason="pre")
    )
    run_with_when_gate(
        _PandasAppendingHandler(sub_row_index=1),
        df,
        "v",
        _seed(when="flag == 1"),
        ctx,
    )
    prior, new = ctx.row_errors
    # Prior error preserved verbatim (guards err_start bound + full-range mut).
    assert prior.column == "other"
    assert prior.row_index == 0
    assert prior.trigger == "format_error"
    assert prior.reason == "pre"
    # New error remapped to full-table position 2, other fields carried.
    assert new.column == "v"
    assert new.row_index == 2
    assert new.trigger == "mask_error"
    assert new.reason == "boom"


# ── preflight: called unconditionally, correct arg order ───────────────


class _PandasPreflightHandler:
    name = "pf"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def preflight(self, plan, ctx):
        self.calls.append((plan, ctx))

    def run(self, df, column, plan, ctx):
        return df, []


def test_pandas_preflight_called_before_zero_match_shortcircuit():
    df = pd.DataFrame({"v": ["a", "b"], "flag": [0, 0]})
    seed = _seed(when="flag == 1")  # matches zero rows
    ctx = _Ctx()
    h = _PandasPreflightHandler()
    run_with_when_gate(h, df, "v", seed, ctx)
    assert len(h.calls) == 1
    got_plan, got_ctx = h.calls[0]
    assert got_plan is seed
    assert got_ctx is ctx


# ── dispatch arg threading: no-gate + gated paths ─────────────────────


class _RecordingPandasHandler:
    name = "rec"

    def __init__(self) -> None:
        self.got: tuple | None = None

    def run(self, df, column, plan, ctx):
        self.got = (df, column, plan, ctx)
        return df, []


def test_pandas_no_gate_passthrough_threads_ctx():
    df = pd.DataFrame({"v": ["a", "b"]})
    seed = _seed(when=None)
    ctx = _Ctx()
    h = _RecordingPandasHandler()
    run_with_when_gate(h, df, "v", seed, ctx)
    assert h.got is not None
    assert h.got[3] is ctx


def test_pandas_gated_subset_threads_ctx():
    df = pd.DataFrame({"v": ["a", "b", "c"], "flag": [0, 1, 1]})
    seed = _seed(when="flag == 1")
    ctx = _Ctx()
    h = _RecordingPandasHandler()
    run_with_when_gate(h, df, "v", seed, ctx)
    assert h.got is not None
    assert h.got[3] is ctx


# ── strategy attribution on every typed error ─────────────────────────


def test_pandas_expression_error_attributes_strategy():
    df = pd.DataFrame({"v": ["a", "b"]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="absent_col == 'x'"), _Ctx())
    assert exc.value.code == "when_expression_error"
    assert exc.value.strategy == "redact"


def test_pandas_not_boolean_attributes_strategy(monkeypatch):
    # An accepted predicate whose evaluation yields a non-boolean Series: the grammar cannot
    # produce one, so the branch is reached by injecting the result.
    df = pd.DataFrame({"v": ["a", "b"], "n": [1, 2]})
    monkeypatch.setattr(pd.DataFrame, "eval", lambda *a, **k: pd.Series([2, 3]))
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="n > 0"), _Ctx())
    assert exc.value.code == "when_expression_not_boolean"
    assert exc.value.strategy == "redact"


def test_pandas_outside_grammar_attributes_strategy():
    df = pd.DataFrame({"v": ["a", "b"], "n": [1, 2]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="n + 1"), _Ctx())
    assert exc.value.code == "when_outside_closed_grammar"
    assert exc.value.strategy == "redact"


# ── numexpr_required import-failure path ──────────────────────────────


def test_missing_numexpr_raises_numexpr_required(monkeypatch):
    def _raise_import_error(*args, **kwargs):
        raise ImportError("numexpr not installed")

    monkeypatch.setattr(pd.DataFrame, "eval", _raise_import_error)
    df = pd.DataFrame({"v": ["a", "b"], "flag": [1, 0]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="flag == 1"), _Ctx())
    assert exc.value.code == "numexpr_required"
    assert exc.value.strategy == "redact"


# ── numexpr scope clamp: empty local/global dicts block @var walks ────


def _eval_kwargs(monkeypatch):
    """The keyword arguments `DataFrame.eval` receives from the gate, recorded by a spy."""
    seen = []
    real = pd.DataFrame.eval

    def spy(self, expr, **kwargs):
        seen.append(kwargs)
        return real(self, expr, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "eval", spy)
    return seen


def test_local_dict_clamp_is_pinned_on_an_accepted_predicate(monkeypatch):
    """`@strategy` names a local of `_eval_predicate`. The empty local_dict is what keeps such a
    name undefined; a dropped clamp (local_dict=None) would let it resolve. The `@` form itself
    is now rejected by the grammar before pandas, so the clamp is asserted on the call."""
    seen = _eval_kwargs(monkeypatch)
    df = pd.DataFrame({"v": ["a", "b"]})
    run_with_when_gate(RedactHandler(), df, "v", _seed(when="v == 'a'"), _Ctx())
    assert seen and all(kw["local_dict"] == {} for kw in seen)
    assert all(kw["local_dict"] is not None for kw in seen)


def test_global_dict_clamp_is_pinned_on_an_accepted_predicate(monkeypatch):
    """`@TYPE_CHECKING` names a module global of `_when_gate`. The empty global_dict keeps it
    undefined; a dropped clamp (global_dict=None) would resolve it."""
    seen = _eval_kwargs(monkeypatch)
    df = pd.DataFrame({"v": ["a", "b"]})
    run_with_when_gate(RedactHandler(), df, "v", _seed(when="v == 'a'"), _Ctx())
    assert seen and all(kw["global_dict"] == {} for kw in seen)
    assert all(kw["global_dict"] is not None for kw in seen)
    assert all(kw["engine"] == "numexpr" for kw in seen)


@pytest.mark.parametrize("hostile", ["@strategy == 'redact'", "@TYPE_CHECKING"])
def test_at_references_are_rejected_before_pandas_evaluates_them(hostile, monkeypatch):
    calls = []
    monkeypatch.setattr(pd.DataFrame, "eval", lambda *a, **k: calls.append(a))
    df = pd.DataFrame({"v": ["a", "b"]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when=hostile), _Ctx())
    assert exc.value.code == "when_outside_closed_grammar"
    assert calls == []


# ── mask/subset selection by predicate ────────────────────────────────


def test_pandas_gate_selects_predicate_rows():
    pdf = pd.DataFrame({"v": ["a", "b", "c", "d"], "age": [10, 20, 30, 15]})
    pandas_out, _ = run_with_when_gate(RedactHandler(), pdf, "v", _seed(when="age >= 20"), _Ctx())
    # Hardcoded: rows with age>=20 (positions 1,2) redacted; 0,3 untouched.
    expected = ["a", "REDACTED", "REDACTED", "d"]
    assert pandas_out["v"].tolist() == expected


# ── gate positions for exact-int Faker columns (real StrategyContext) ──


def _real_ctx(**kw):
    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._graph import RelationshipGraph
    from decoy_engine.relationships._namespace import NamespaceRegistry

    kw.setdefault("current_table", "t")
    return StrategyContext(
        registry=get_default_registry(),
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=b"\x01" * 8,
        **kw,
    )


def _exact_sources():
    import pyarrow as pa

    return {("t", "v"): pa.chunked_array([pa.array([1, 2, 3, 4], type=pa.int64())])}


def test_gate_hands_selected_positions_to_a_column_with_exact_values():
    df = pd.DataFrame({"v": [1.0, 2.0, 3.0, 4.0], "flag": [0, 1, 0, 1]})
    ctx = _real_ctx(exact_int_sources=_exact_sources())
    h = _RecordingPandasHandler()
    run_with_when_gate(h, df, "v", _seed(when="flag == 1"), ctx)
    got = h.got[3]
    assert got is not ctx
    assert got.gate_positions.tolist() == [1, 3]
    # The sinks stay shared so the adapter's identity-based drains still work.
    assert got.row_errors is ctx.row_errors
    assert got.code_set_corpora is ctx.code_set_corpora
    assert got.exact_int_sources is ctx.exact_int_sources
    # The caller's own context is not modified.
    assert ctx.gate_positions is None


def test_gate_keeps_the_callers_context_for_other_columns():
    df = pd.DataFrame({"v": [1.0, 2.0, 3.0, 4.0], "flag": [0, 1, 0, 1]})
    ctx = _real_ctx(exact_int_sources=_exact_sources())
    h = _RecordingPandasHandler()
    run_with_when_gate(h, df, "flag", _seed(when="flag == 1"), ctx)
    assert h.got[3] is ctx


def test_gate_positions_ignore_a_missing_value_in_a_nullable_boolean_mask():
    df = pd.DataFrame({"v": [1.0, 2.0, 3.0, 4.0], "flag": pd.array([1, None, 1, 0], dtype="Int64")})
    ctx = _real_ctx(exact_int_sources=_exact_sources())
    h = _RecordingPandasHandler()
    run_with_when_gate(h, df, "v", _seed(when="flag == 1"), ctx)
    assert h.got[3].gate_positions.tolist() == [0, 2]


def test_gate_remaps_row_errors_through_a_nullable_boolean_mask():
    # `<NA>` selects nothing, so rows 0, 2 and 3 are the subset and its row 1 is full row 2.
    df = pd.DataFrame({"v": ["a", "b", "c", "d"], "flag": pd.array([1, None, 1, 1], dtype="Int64")})
    ctx = _real_ctx()
    run_with_when_gate(
        _PandasAppendingHandler(sub_row_index=1), df, "v", _seed(when="flag == 1"), ctx
    )
    assert [e.row_index for e in ctx.row_errors] == [2]


def test_no_gate_never_sets_positions():
    df = pd.DataFrame({"v": [1.0, 2.0, 3.0, 4.0]})
    ctx = _real_ctx(exact_int_sources=_exact_sources())
    h = _RecordingPandasHandler()
    run_with_when_gate(h, df, "v", _seed(when=None), ctx)
    assert h.got[3] is ctx
    assert ctx.gate_positions is None
