"""C8-i acceptance tests 3 and 5b: the native mask is the oracle's selection.

`when_mask` hands the predicate to the oracle's own `_eval_predicate` over the oracle's own
conversion of the referenced columns, so the selection must equal the oracle's for every
column type and representation, and a value pandas cannot represent must raise the same
coded error with the same attribution.
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._when_gate import _eval_predicate
from decoy_engine.execution.native._when_mask import WhenSpec, when_mask
from decoy_engine.expressions._when_parser import parse_when, when_column_refs
from tests.native._c8_i_support import (
    identifiers,
    predicates,
    run_native,
    run_oracle_leg,
)
from tests.native._chunked_entry_support import make_config, passthrough, redact
from tests.native._rev9_support import BY_NAME

_N = 7


def _spec(expr: str, column: str = "target") -> WhenSpec:
    return WhenSpec(column, "redact", expr, when_column_refs(parse_when(expr)))


def _oracle_selection(
    chunk: pa.Table, expr: str, protected: set[str]
) -> list[bool] | tuple[str, str | None]:
    """The chunked oracle's selection: convert the WHOLE chunk as the adapter does, evaluate,
    and apply `df.loc[mask]`'s rule (a nullable NA is not selected)."""
    frame = to_pandas_fk_safe(chunk, protected)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mask = _eval_predicate(frame, expr, "redact", column="target")
    except StrategyError as exc:
        return ("error", exc.code)
    return [bool(v) for v in mask.fillna(False).to_numpy(dtype=bool)]


def _native_selection(
    chunk: pa.Table, expr: str, protected: set[str]
) -> list[bool] | tuple[str, str | None]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return [bool(v) for v in when_mask(_spec(expr), chunk, protected).to_pylist()]
    except StrategyError as exc:
        return ("error", exc.code)


@st.composite
def _chunk_case(draw: st.DrawFn) -> tuple[str, pa.Table, set[str]]:
    names = draw(st.lists(identifiers(), min_size=1, max_size=4, unique=True))
    kinds = {n: draw(st.sampled_from(["string", "int64", "float64", "bool"])) for n in names}
    expr = draw(
        predicates({n: ("number" if k in ("int64", "float64") else k) for n, k in kinds.items()})
    )
    arrays: dict[str, pa.Array] = {}
    for name, kind in kinds.items():
        if kind == "string":
            values: list[Any] = draw(
                st.lists(st.one_of(st.none(), st.text(max_size=3)), min_size=_N, max_size=_N)
            )
            arrays[name] = pa.array(values, pa.string())
        elif kind == "int64":
            ints = st.one_of(st.none(), st.integers(min_value=-5, max_value=5))
            arrays[name] = pa.array(draw(st.lists(ints, min_size=_N, max_size=_N)), pa.int64())
        elif kind == "float64":
            floats = st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=False, width=32))
            arrays[name] = pa.array(draw(st.lists(floats, min_size=_N, max_size=_N)), pa.float64())
        else:
            bools = st.one_of(st.none(), st.booleans())
            arrays[name] = pa.array(draw(st.lists(bools, min_size=_N, max_size=_N)), pa.bool_())
    int_columns = [n for n, k in kinds.items() if k == "int64"]
    protected = (
        set(draw(st.lists(st.sampled_from(int_columns), unique=True))) if int_columns else set()
    )
    return expr, pa.table(arrays), protected


@settings(
    max_examples=200,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(_chunk_case())
def test_when_mask_equals_the_oracle_selection_over_generated_predicates_and_types(
    case: tuple[str, pa.Table, set[str]],
) -> None:
    expr, chunk, protected = case
    assert _native_selection(chunk, expr, protected) == _oracle_selection(chunk, expr, protected)


def test_a_protected_nullable_integer_reference_excludes_na_like_df_loc() -> None:
    chunk = pa.table({"n": pa.array([1, None, 3, 1], pa.int64())})
    assert _native_selection(chunk, "n == 1", {"n"}) == [True, False, False, True]
    assert _native_selection(chunk, "n != 1", {"n"}) == [False, False, True, False]
    # Unprotected, the column widens to float64 and a null is an ordinary NaN.
    assert _native_selection(chunk, "n != 1", set()) == [False, True, True, False]


def test_an_oracle_error_is_raised_identically() -> None:
    chunk = pa.table({"s": pa.array(["a", "b"])})
    for expr in ("nope == 1", "s < 1"):
        assert _native_selection(chunk, expr, set()) == _oracle_selection(chunk, expr, set())
    with pytest.raises(StrategyError) as info:
        when_mask(_spec("nope == 1"), chunk, set())
    assert info.value.code == "when_expression_error"
    assert info.value.strategy == "redact"


def test_a_chunk_with_no_row_gives_an_empty_mask() -> None:
    chunk = pa.table({"s": pa.array([], pa.string())})
    assert _native_selection(chunk, "s == 'a'", set()) == []


# ---------------------------------------------------------------------------
# 3. Metadata drift between chunks: object in chunk 1, StringDtype in chunk 2.
# ---------------------------------------------------------------------------


def _drift_chunks() -> list[pa.Table]:
    plain = pa.table(
        {
            "target": pa.array(["t0", "t1", "t2", "t3"]),
            "p": pa.array(["x", None, "y", "x"]),
        }
    )
    pdf = pd.DataFrame(
        {
            "target": pd.array(["t4", "t5", "t6", "t7"], dtype="string"),
            "p": pd.array(["x", None, "y", "x"], dtype="string"),
        }
    )
    metadata = pa.Table.from_pandas(pdf, preserve_index=False)
    assert plain.schema.types == metadata.schema.types
    assert metadata.schema.metadata is not None and plain.schema.metadata is None
    return [plain, metadata]


@pytest.mark.parametrize(
    "expr", ["p == 'x'", "p != 'x'", "p < 'y'", "p in ['y']", "not (p == 'x')"]
)
def test_each_chunk_gets_the_selection_its_own_representation_gives_the_oracle(expr: str) -> None:
    for chunk in _drift_chunks():
        assert _native_selection(chunk, expr, set()) == _oracle_selection(chunk, expr, set())


@pytest.mark.parametrize("expr", ["p == 'x'", "p != 'x'", "p < 'y'"])
def test_a_drifting_stream_masks_each_chunk_like_the_chunked_oracle_leg(
    expr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config([{**redact("target"), "when": expr}, passthrough("p")])
    chunks = _drift_chunks()
    native, evidence = run_native(config, chunks)
    assert evidence.native_admitted is True, evidence.reroute_reason
    oracle, _ = run_oracle_leg(config, chunks, monkeypatch)
    assert [t.to_pydict() for t in native] == [t.to_pydict() for t in oracle]


# ---------------------------------------------------------------------------
# 5b. A value pandas cannot represent: same coded error, same attribution.
# ---------------------------------------------------------------------------

_T64 = BY_NAME["time64ns_unaligned"]
_CODE = "chunked_passthrough_value_unrepresentable"


def _drain(gen: Any) -> tuple[list[pa.Table], BaseException | None]:
    emitted: list[pa.Table] = []
    try:
        for chunk in gen:
            emitted.append(chunk)
    except Exception as exc:
        return emitted, exc
    return emitted, None


def _t64_stream() -> list[pa.Table]:
    s = pa.array(["a", "b", "c"])
    return [pa.table({"s": s, "t": _T64.good}), pa.table({"s": s, "t": _T64.bad})]


def _leg_error(config: dict[str, Any], chunks: list[pa.Table], *, forced: bool, mp: Any) -> Any:
    from decoy_engine import run_mask_chunked
    from tests.native._c8_i_support import companion_missing
    from tests.native._chunked_entry_support import ENGINE_VERSION, TABLE, hash_col, key_provider

    if forced:
        config = make_config([*config["tables"][0]["columns"], hash_col("h")])
        chunks = [c.append_column("h", pa.array(["h0", "h1", "h2"])) for c in chunks]
        with companion_missing(mp):
            gen = run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
            return _drain(gen)
    gen = run_mask_chunked(
        config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
    )
    return _drain(gen)


def test_a_conversion_error_in_a_referenced_column_matches_the_oracle_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config([{**redact("s"), "when": "t != 1"}])
    chunks = _t64_stream()
    native_out, native_exc = _leg_error(config, chunks, forced=False, mp=monkeypatch)
    oracle_out, oracle_exc = _leg_error(config, chunks, forced=True, mp=monkeypatch)
    for exc in (native_exc, oracle_exc):
        assert isinstance(exc, ExecutionError) and exc.code == _CODE, repr(exc)
        assert exc.__cause__ is not None
        assert "'t'" in exc.message and "chunk 1" in exc.message
    assert native_exc is not None and oracle_exc is not None
    assert native_exc.message == oracle_exc.message
    assert type(native_exc.__cause__) is type(oracle_exc.__cause__)
    assert str(native_exc.__cause__) == str(oracle_exc.__cause__)
    # Chunk 2 is not emitted on either leg; chunk 1 is.
    assert len(native_out) == len(oracle_out) == 1
    assert native_out[0].column("s").to_pylist() == oracle_out[0].column("s").to_pylist()
    assert np.array_equal(
        native_out[0].column("t").to_numpy(zero_copy_only=False),
        oracle_out[0].column("t").to_numpy(zero_copy_only=False),
    )
