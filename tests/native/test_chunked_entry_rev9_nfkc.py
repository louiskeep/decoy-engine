"""B1 rev9 gate round 1, finding B1: `DataFrame.eval` parses through Python's `ast`,
which NFKC-normalizes identifiers, so a fullwidth x reads the column `x`. The carried-column
decision must see the same name, or the predicate reads a null placeholder and the masked
column silently comes back unmasked."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from tests.native._chunked_entry_support import make_config, passthrough, redact
from tests.native._rev9_support import run_entry, run_public, same_column

# (predicate, the real column name it reads after NFKC)
_CASES = {
    "fullwidth": ("\uff58 > 4", "x"),
    "ligature": ("\ufb01le > 4", "file"),
    "fullwidth_backtick": ("`\uff58` > 4", "x"),
    "ligature_backtick": ("`\ufb01le` > 4", "file"),
    "fullwidth_in_literal": ("(x > 4) | (x == 99) | (\"\uff58\" == 'q')", "x"),
    "ligature_notnull": ("\ufb01le.notnull()", "file"),
}


@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
@pytest.mark.parametrize("case", _CASES)
def test_nfkc_spelled_predicate_reads_the_column_like_the_oracle(
    case: str, configured: bool
) -> None:
    expr, name = _CASES[case]
    cols: list[dict[str, Any]] = [{**redact("s"), "when": expr}]
    if configured:
        cols.append(passthrough(name))
    chunks = [
        pa.table({"s": ["alice", "bob", "carol"], name: pa.array(v, pa.int64())})
        for v in ([1, 5, 9], [9, 1, 5], [2, 6, 3])
    ]
    expected = run_public(make_config(cols), chunks)
    out, sink, _ = run_entry(make_config(cols), chunks)
    for got, want in zip(out, expected, strict=True):
        assert got.column("s").to_pylist() == want.column("s").to_pylist()
        assert same_column(got.column(name), want.column(name))
    listed = [r.quality_metrics["chunked_route"]["pandas_read_passthrough"] for r in sink]
    assert listed == [[name]] * 3


def test_string_literal_values_are_normalized_too() -> None:
    from decoy_engine.execution._chunked_carry import read_set

    cols = [{**redact("s"), "when": "s == '\ufb01le'"}]
    assert read_set(cols, ["file", "other"]) == frozenset({"file"})


@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "configured"])
def test_duplicate_source_column_names_raise_the_oracle_refusal(configured: bool) -> None:
    """Gate round 1, L1: a carried duplicate name used to raise ArrowInvalid."""
    chunk = pa.Table.from_arrays(
        [pa.array(["a", "b"]), pa.array([1, 2]), pa.array([3, 4])], names=["s", "x", "x"]
    )
    cols = [redact("s")] + ([passthrough("x")] if configured else [])
    with pytest.raises(ValueError, match="duplicate column names") as pub:
        run_public(make_config(cols), [chunk])
    with pytest.raises(ValueError, match="duplicate column names") as ent:
        run_entry(make_config(cols), [chunk])
    assert str(ent.value) == str(pub.value)
