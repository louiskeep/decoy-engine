"""B1 rev9 gate round 1, B2 and M1: acceptance test 20 over every pyarrow type factory.

Each type, with and without a null, must either produce an `arrow_column_profile` equal
on every `ColumnProfile` field to the `walk_dataframe` one, or `walk_dataframe` must
refuse it too. `run_mask_chunked` must then complete with the source column exactly for
carried (configured and unconfigured) passthrough, and refuse a read column with the
oracle's own exception as the cause."""

from __future__ import annotations

import dataclasses
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._chunked_profile import arrow_column_profile, walk_one_table
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.profile._types import ColumnProfile
from tests.native._chunked_entry_support import TABLE, make_config, passthrough, redact
from tests.native._rev9_support import run_entry, run_public
from tests.native._rev9_type_catalogue import CATALOGUE

_IDS = [f"{name}-{'null' if null else 'nonull'}" for name in CATALOGUE for null in (False, True)]
_PARAMS = [(name, null) for name in CATALOGUE for null in (False, True)]


def _column(name: str, null: bool) -> pa.Array:
    return CATALOGUE[name](null)


def _pandas_profile(arr: pa.Array) -> tuple[ColumnProfile | None, Exception | None]:
    try:
        return walk_one_table(pa.table({"x": arr}), table_name=TABLE).columns[0], None
    except Exception as exc:
        return None, exc


@pytest.mark.parametrize(("name", "null"), _PARAMS, ids=_IDS)
def test_type_catalogue_profile_matches_or_pandas_refuses(name: str, null: bool) -> None:
    arr = _column(name, null)
    real, refusal = _pandas_profile(arr)
    if real is None:
        assert refusal is not None  # both refuse; the run test below pins the entry point
        return
    ours = arrow_column_profile("x", pa.chunked_array([arr]))
    for field in dataclasses.fields(ColumnProfile):
        assert getattr(ours, field.name) == getattr(real, field.name), (name, field.name)


def _exact(a: Any, b: Any) -> bool:
    """Same type and the same values, null positions and float signs; works for the
    view, run-end-encoded and union types the IPC helper in `_rev9_support` cannot read."""
    return bool(
        a.type == b.type and a.equals(b) and repr(a.to_pylist()) == repr(b.to_pylist())
    )


def _py(column: Any) -> list[Any]:
    """Values with an extension type reduced to its storage (a pandas round trip drops
    the extension type, so a read column differs from the source in type only)."""
    arr = column.combine_chunks()
    return (arr.storage if isinstance(arr.type, pa.BaseExtensionType) else arr).to_pylist()


def _chunks(arr: pa.Array) -> list[pa.Table]:
    return [pa.table({"s": pa.array(["a", "b", "c", "d", "e"]), "x": arr}) for _ in range(2)]


def _public_outcome(config: dict[str, Any], chunks: list[pa.Table]) -> Exception | None:
    try:
        run_public(config, chunks)
    except Exception as exc:
        return exc
    return None


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
@pytest.mark.parametrize(("name", "null"), _PARAMS, ids=_IDS)
def test_type_catalogue_carried_passthrough_is_yielded_exactly(
    name: str, null: bool, configured: bool
) -> None:
    arr = _column(name, null)
    chunks = _chunks(arr)
    cols = [redact("s")] + ([passthrough("x")] if configured else [])
    refusal = _public_outcome(make_config(cols), chunks)
    try:
        out, sink, _ = run_entry(make_config(cols), chunks)
    except Exception as exc:
        # The native route admission may refuse a type pandas refuses too (the unions);
        # it must be the oracle's own refusal, not a new one.
        assert refusal is not None and type(exc) is type(refusal), repr(exc)
        return
    assert len(out) == 2
    assert all(_exact(o.column("x"), chunks[0].column("x")) for o in out)
    assert all(
        r.quality_metrics["chunked_route"]["pandas_read_passthrough"] == [] for r in sink
    )
    if refusal is None:
        expected = run_public(make_config(cols), chunks)
        for got, want in zip(out, expected, strict=True):
            assert got.column("s").to_pylist() == want.column("s").to_pylist()


@pytest.mark.parametrize(("name", "null"), _PARAMS, ids=_IDS)
def test_type_catalogue_read_column_matches_the_oracle(name: str, null: bool) -> None:
    arr = _column(name, null)
    chunks = _chunks(arr)
    cols = [{**redact("s"), "when": "x.notnull()"}]
    refusal = _public_outcome(make_config(cols), chunks)
    if refusal is None:
        out, _, _ = run_entry(make_config(cols), chunks)
        expected = run_public(make_config(cols), chunks)
        for got, want in zip(out, expected, strict=True):
            assert got.column("s").to_pylist() == want.column("s").to_pylist()
            assert _py(got.column("x")) == _py(want.column("x"))
        return
    with pytest.raises(Exception) as info:
        run_entry(make_config(cols), chunks)
    exc = info.value
    if isinstance(exc, ExecutionError) and exc.code == "chunked_passthrough_value_unrepresentable":
        exc = exc.__cause__  # type: ignore[assignment]
    assert type(exc) is type(refusal) and str(exc) == str(refusal)
