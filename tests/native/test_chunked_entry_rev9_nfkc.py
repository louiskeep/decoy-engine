"""B1 rev9 gate round 1, finding B1: `DataFrame.eval` parses through Python's `ast`,
which NFKC-normalizes identifiers, so a fullwidth x reads the column `x`. The carried-column
decision must see the same name, or the predicate reads a null placeholder and the masked
column silently comes back unmasked."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_entry_support import make_config, passthrough, redact
from tests.native._rev9_support import (
    run_entry_below_gate,
    run_public_below_gate,
    same_column,
)

# (predicate, the real column name it reads after NFKC)
_CASES = {
    "fullwidth": ("\uff58 > 4", "x"),
    "ligature": ("\ufb01le > 4", "file"),
    "fullwidth_backtick": ("`\uff58` > 4", "x"),
    "ligature_backtick": ("`\ufb01le` > 4", "file"),
    "ligature_notnull": ("\ufb01le.notnull()", "file"),
}


@pytest.mark.parametrize("case", _CASES)
def test_nfkc_spelled_predicate_resolves_to_the_real_column_name(case: str) -> None:
    """Helper level: the read-set scan sees the name pandas resolves, so a spelled predicate
    that reached the oracle without compile could never leave its column unmasked."""
    from decoy_engine.execution._chunked_carry import read_set
    from decoy_engine.execution._column_access import predicate_names

    expr, name = _CASES[case]
    assert name in (predicate_names(expr) or set())
    cols = [{**redact("s"), "when": expr}]
    assert read_set(cols, [name, "other"], get_default_registry()) == frozenset({name})


@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
@pytest.mark.parametrize("case", _CASES)
def test_nfkc_spelled_predicate_is_rejected_at_compile(case: str, configured: bool) -> None:
    """The closed grammar's identifiers are ASCII, so no NFKC spelling reaches evaluation."""
    from decoy_engine.plan._errors import PlanCompileError

    expr, name = _CASES[case]
    cols: list[dict[str, Any]] = [{**redact("s"), "when": expr}]
    if configured:
        cols.append(passthrough(name))
    chunks = [pa.table({"s": ["alice", "bob"], name: pa.array([1, 5], pa.int64())})]
    for call in (
        lambda: run_entry_below_gate(make_config(cols), chunks),
        lambda: run_public_below_gate(make_config(cols), chunks),
    ):
        with pytest.raises(PlanCompileError) as info:
            call()
        assert info.value.code == "when_outside_closed_grammar"
        assert info.value.path == "tables.t.columns.s.when"


@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
def test_nfkc_literal_does_not_read_the_column(configured: bool) -> None:
    cols: list[dict[str, Any]] = [{**redact("s"), "when": 's == "\uff58"'}]
    if configured:
        cols.append(passthrough("x"))
    chunks = [
        pa.table({"s": ["alice", "bob", "carol"], "x": pa.array(v, pa.int64())})
        for v in ([1, 5, 9], [9, 1, 5], [2, 6, 3])
    ]
    expected = run_public_below_gate(make_config(cols), chunks)
    out, sink, _ = run_entry_below_gate(make_config(cols), chunks)
    listed = [r.quality_metrics["chunked_route"]["pandas_read_passthrough"] for r in sink]
    assert listed == [[]] * 3
    for got, src, want in zip(out, chunks, expected, strict=True):
        assert same_column(got.column("x"), src.column("x"))
        assert got.column("s").to_pylist() == want.column("s").to_pylist()


def test_string_literal_values_are_not_read_normalized_or_not() -> None:
    from decoy_engine.execution._chunked_carry import read_set

    cols = [{**redact("s"), "when": "s == '\ufb01le'"}]
    assert read_set(cols, ["file", "other"], get_default_registry()) == frozenset()
    bare = [{**redact("s"), "when": "\ufb01le == 'x'"}]
    assert read_set(bare, ["file", "other"], get_default_registry()) == frozenset({"file"})


@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
def test_duplicate_source_column_names_raise_the_oracle_refusal(configured: bool) -> None:
    """A carried duplicate name is refused with the registered code on both chunked entries."""
    from decoy_engine.execution._transforms import TransformError

    chunk = pa.Table.from_arrays(
        [pa.array(["a", "b"]), pa.array([1, 2]), pa.array([3, 4])], names=["s", "x", "x"]
    )
    cols = [redact("s")] + ([passthrough("x")] if configured else [])
    with pytest.raises(TransformError) as pub:
        run_public_below_gate(make_config(cols), [chunk])
    with pytest.raises(TransformError) as ent:
        run_entry_below_gate(make_config(cols), [chunk])
    # Only the config-key table name and a count: no field name, no cell value, nothing chained.
    expected = "table 't' has 1 duplicated field name(s)"
    for err in (pub.value, ent.value):
        assert type(err) is TransformError and err.code == "duplicate_source_field_names"
        assert str(err) == expected and err.args == (expected,)
        assert getattr(err, "__notes__", []) == []
        assert err.__cause__ is None and err.__context__ is None
