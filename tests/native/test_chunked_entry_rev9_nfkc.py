"""B1 rev9 gate round 1, finding B1: `DataFrame.eval` parses through Python's `ast`,
which NFKC-normalizes identifiers, so `ｘ > 4` reads the column `x`. The carried-column
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
    "fullwidth": ("ｘ > 4", "x"),
    "ligature": ("ﬁle > 4", "file"),
    "fullwidth_backtick": ("`ｘ` > 4", "x"),
    "ligature_backtick": ("`ﬁle` > 4", "file"),
    "fullwidth_in_literal": ("(x > 4) | (x == 99) | (\"ｘ\" == 'q')", "x"),
    "ligature_notnull": ("ﬁle.notnull()", "file"),
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

    cols = [{**redact("s"), "when": "s == 'ﬁle'"}]
    assert read_set(cols, ["file", "other"]) == frozenset({"file"})
