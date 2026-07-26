"""TQ substrate sweep: oracle cells for the `when:` predicate gate.

These pin the machine-observable behavior of `_when_gate` that the broader
regression suite left unguarded: the subset-relative row-error remap (B1),
the unconditional `preflight` call and its argument order, the no-gate and
gated dispatch argument threading on both substrates, the strategy
attribution on every typed error, the `numexpr_required` import-failure
path, and the numexpr scope clamp (empty local/global dicts) that blocks
`@var` walks into the module's own locals and globals.

Assertions target machine fields only (`.code`, `.strategy`, `RowError`
positional fields), never error prose. Expected values are hardcoded, not
recomputed from the module.
"""

from __future__ import annotations

import pandas as pd
import polars as pl
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._when_gate import (
    run_with_when_gate,
    run_with_when_gate_polars,
)
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


class _PolarsAppendingHandler:
    name = "appending_pl"

    def __init__(self, sub_row_index: int) -> None:
        self._sub = sub_row_index

    def run(self, frame, column, plan, ctx):
        ctx.row_errors.append(
            RowError(
                column=column,
                row_index=self._sub,
                trigger="mask_error",
                reason="boom",
            )
        )
        return frame, []


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


def test_polars_gate_remaps_new_error_and_leaves_prior_untouched():
    frame = pl.DataFrame({"v": ["a", "b", "c", "d"], "flag": [0, 1, 1, 1]})
    ctx = _Ctx()
    ctx.row_errors.append(
        RowError(column="other", row_index=0, trigger="format_error", reason="pre")
    )
    run_with_when_gate_polars(
        _PolarsAppendingHandler(sub_row_index=1),
        frame,
        "v",
        _seed(when="flag == 1"),
        ctx,
    )
    prior, new = ctx.row_errors
    assert prior.column == "other"
    assert prior.row_index == 0
    assert prior.trigger == "format_error"
    assert prior.reason == "pre"
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


class _PolarsPreflightHandler(_PandasPreflightHandler):
    name = "pf_pl"

    def run(self, frame, column, plan, ctx):
        return frame, []


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


def test_polars_preflight_called_before_zero_match_shortcircuit():
    frame = pl.DataFrame({"v": ["a", "b"], "flag": [0, 0]})
    seed = _seed(when="flag == 1")
    ctx = _Ctx()
    h = _PolarsPreflightHandler()
    run_with_when_gate_polars(h, frame, "v", seed, ctx)
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


class _RecordingPolarsHandler:
    name = "rec_pl"

    def __init__(self) -> None:
        self.got: tuple | None = None

    def run(self, frame, column, plan, ctx):
        self.got = (frame, column, plan, ctx)
        return frame, []


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


def test_polars_no_gate_passthrough_threads_all_args():
    frame = pl.DataFrame({"v": ["a", "b"]})
    seed = _seed(when=None)
    ctx = _Ctx()
    h = _RecordingPolarsHandler()
    run_with_when_gate_polars(h, frame, "v", seed, ctx)
    assert h.got is not None
    got_frame, got_col, got_plan, got_ctx = h.got
    assert got_frame is frame
    assert got_col == "v"
    assert got_plan is seed
    assert got_ctx is ctx


def test_polars_gated_subset_threads_plan_and_ctx():
    frame = pl.DataFrame({"v": ["a", "b", "c"], "flag": [0, 1, 1]})
    seed = _seed(when="flag == 1")
    ctx = _Ctx()
    h = _RecordingPolarsHandler()
    run_with_when_gate_polars(h, frame, "v", seed, ctx)
    assert h.got is not None
    _, _, got_plan, got_ctx = h.got
    assert got_plan is seed
    assert got_ctx is ctx


# ── strategy attribution on every typed error ─────────────────────────


def test_pandas_expression_error_attributes_strategy():
    df = pd.DataFrame({"v": ["a", "b"]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="absent_col == 'x'"), _Ctx())
    assert exc.value.code == "when_expression_error"
    assert exc.value.strategy == "redact"


def test_pandas_not_boolean_attributes_strategy():
    df = pd.DataFrame({"v": ["a", "b"], "n": [1, 2]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="n + 1"), _Ctx())
    assert exc.value.code == "when_expression_not_boolean"
    assert exc.value.strategy == "redact"


def test_polars_expression_error_attributes_strategy():
    frame = pl.DataFrame({"v": ["a", "b"]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate_polars(
            RedactHandler(), frame, "v", _seed(when="absent_col == 'x'"), _Ctx()
        )
    assert exc.value.code == "when_expression_error"
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


def test_local_dict_clamp_blocks_at_local_scope_walk():
    """`@strategy` names a local of `_eval_predicate`; the empty local_dict
    must keep it undefined (expression error), never resolve it. If the
    clamp were dropped (local_dict=None) it would resolve to the strategy
    string and surface as a not-boolean scalar instead."""
    df = pd.DataFrame({"v": ["a", "b"]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="@strategy == 'redact'"), _Ctx())
    assert exc.value.code == "when_expression_error"


def test_global_dict_clamp_blocks_at_global_scope_walk():
    """`@TYPE_CHECKING` names a module global of `_when_gate`; the empty
    global_dict must keep it undefined. Dropping the clamp (global_dict=None)
    would resolve it to a bool scalar (not-boolean code instead)."""
    df = pd.DataFrame({"v": ["a", "b"]})
    with pytest.raises(StrategyError) as exc:
        run_with_when_gate(RedactHandler(), df, "v", _seed(when="@TYPE_CHECKING"), _Ctx())
    assert exc.value.code == "when_expression_error"


# ── cross-substrate parity of the mask/subset selection ───────────────


class _PolarsRedact:
    name = "redact_pl"

    def run(self, frame, column, plan, ctx):
        return frame.with_columns(pl.lit("REDACTED").alias(column)), []


def test_pandas_and_polars_gate_select_identical_rows():
    pdf = pd.DataFrame({"v": ["a", "b", "c", "d"], "age": [10, 20, 30, 15]})
    frame = pl.DataFrame({"v": ["a", "b", "c", "d"], "age": [10, 20, 30, 15]})
    pandas_out, _ = run_with_when_gate(RedactHandler(), pdf, "v", _seed(when="age >= 20"), _Ctx())
    polars_out, _ = run_with_when_gate_polars(
        _PolarsRedact(), frame, "v", _seed(when="age >= 20"), _Ctx()
    )
    # Hardcoded: rows with age>=20 (positions 1,2) redacted; 0,3 untouched.
    expected = ["a", "REDACTED", "REDACTED", "d"]
    assert pandas_out["v"].tolist() == expected
    assert polars_out["v"].to_list() == expected
