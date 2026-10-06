"""C8-i acceptance tests 2 and 4: the oracle's own behavior, recorded before the change.

These characterize what the pandas oracle does today, so the native `when:` route is built
against observed behavior and not an assumption. They pass on the base commit by design
(green-before). Test 2 records how the oracle's predicate selects rows for each column
representation, nulls included. Test 4 records the Arrow type the oracle gives a `when:`
column in the degenerate and the ordinary cases, which is what the output-type pin
(plan 3f) has to reconcile.
"""

from __future__ import annotations

import copy
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._when_gate import _eval_predicate
from tests.native._c8_i_support import (
    chunk_by_sizes,
    run_public_oracle,
    source_table,
    when_config,
)
from tests.native._chunked_entry_support import ENGINE_VERSION, TABLE, key_provider

# ---------------------------------------------------------------------------
# 2. Oracle selection, by column representation.
# ---------------------------------------------------------------------------

_STRINGS = ["a", None, "b", "a"]
_INTS = [1, None, 3, 1]


def _representation(kind: str) -> pd.DataFrame:
    if kind == "object_strings":
        return to_pandas_fk_safe(pa.table({"v": pa.array(_STRINGS)}), set())
    if kind == "string_dtype_metadata":
        pdf = pd.DataFrame({"v": pd.array(_STRINGS, dtype="string")})
        return to_pandas_fk_safe(pa.Table.from_pandas(pdf, preserve_index=False), set())
    if kind == "int64_with_nulls":
        return to_pandas_fk_safe(pa.table({"v": pa.array(_INTS)}), set())
    assert kind == "int64_protected"
    return to_pandas_fk_safe(pa.table({"v": pa.array(_INTS)}), {"v"})


# (dtype of the mask, mask values with None for NA, positions `df.loc[mask]` selects)
_EXPECTED: dict[str, dict[str, tuple[str, list[bool | None], list[int]]]] = {
    "object_strings": {
        "v == 'a'": ("bool", [True, False, False, True], [0, 3]),
        "v != 'a'": ("bool", [False, True, True, False], [1, 2]),
        "v < 'b'": ("bool", [True, False, False, True], [0, 3]),
        "v in ['a', 'b']": ("bool", [True, False, True, True], [0, 2, 3]),
        "v not in ['a']": ("bool", [False, True, True, False], [1, 2]),
        "not (v == 'a')": ("bool", [False, True, True, False], [1, 2]),
    },
    "string_dtype_metadata": {
        "v == 'a'": ("bool", [True, False, False, True], [0, 3]),
        "v != 'a'": ("bool", [False, True, True, False], [1, 2]),
        "v < 'b'": ("boolean", [True, None, False, True], [0, 3]),
        "v in ['a', 'b']": ("bool", [True, False, True, True], [0, 2, 3]),
        "v not in ['a']": ("bool", [False, True, True, False], [1, 2]),
        "not (v == 'a')": ("bool", [False, True, True, False], [1, 2]),
    },
    "int64_with_nulls": {
        "v == 1": ("bool", [True, False, False, True], [0, 3]),
        "v != 1": ("bool", [False, True, True, False], [1, 2]),
        "v < 3": ("bool", [True, False, False, True], [0, 3]),
        "v in [1, 3]": ("bool", [True, False, True, True], [0, 2, 3]),
        "v not in [1]": ("bool", [False, True, True, False], [1, 2]),
        "not (v == 1)": ("bool", [False, True, True, False], [1, 2]),
    },
    "int64_protected": {
        "v == 1": ("boolean", [True, None, False, True], [0, 3]),
        "v != 1": ("boolean", [False, None, True, False], [2]),
        "v < 3": ("boolean", [True, None, False, True], [0, 3]),
        "v in [1, 3]": ("boolean", [True, False, True, True], [0, 2, 3]),
        "v not in [1]": ("boolean", [False, True, True, False], [1, 2]),
        "not (v == 1)": ("boolean", [False, None, True, False], [2]),
    },
}


@pytest.mark.parametrize(
    ("kind", "expr"), [(k, e) for k, cases in _EXPECTED.items() for e in cases]
)
def test_the_oracle_predicate_and_its_selection_for_each_representation(
    kind: str, expr: str
) -> None:
    dtype, values, selected = _EXPECTED[kind][expr]
    frame = _representation(kind)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mask = _eval_predicate(frame, expr, "redact")
    assert str(mask.dtype) == dtype
    assert [None if pd.isna(v) else bool(v) for v in mask] == values
    # `df.loc[mask]` is the oracle's selection; a nullable NA is excluded, never an error.
    assert list(frame.loc[mask].index) == selected
    assert list(frame.index[mask.fillna(False).to_numpy(dtype=bool)]) == selected


def test_a_null_in_a_nullable_mask_is_excluded_from_the_write_back_too() -> None:
    frame = to_pandas_fk_safe(pa.table({"v": pa.array(_INTS), "s": pa.array(list("abcd"))}), {"v"})
    mask = _eval_predicate(frame, "v == 1", "redact")
    assert str(mask.dtype) == "boolean" and mask.isna().tolist() == [False, True, False, False]
    frame.loc[mask, "s"] = "X"
    assert frame["s"].tolist() == ["X", "b", "c", "X"]


# ---------------------------------------------------------------------------
# 4. Oracle output types for a `when:` column.
# ---------------------------------------------------------------------------

_OPERATORS = ["redact", "truncate", "hash", "categorical"]


def _scenario(name: str) -> tuple[pa.Table, str]:
    base = source_table()
    if name == "zero_row":
        return base.slice(0, 0), "p == 'x'"
    if name == "all_null":
        return base.set_column(0, "s", pa.array([None] * base.num_rows, pa.string())), "p == 'x'"
    return base, {"zero_match": "p == 'zzz'", "all_match": "p != 'zzz'", "partial": "p == 'x'"}[
        name
    ]


_DEGENERATE = {"zero_row", "all_null"}


def _whole_frame(config: dict[str, Any], source: pa.Table, tmp_path: Path) -> pa.Table:
    path = str(tmp_path / "source.parquet")
    pq.write_table(source, path)
    config = copy.deepcopy(config)
    config["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    config["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    result = run_pipeline(
        config,
        {TABLE: source},
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=key_provider(),
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    return result.outputs[TABLE]


@pytest.mark.parametrize("scenario", ["zero_row", "all_null", "zero_match", "all_match", "partial"])
@pytest.mark.parametrize("kind", _OPERATORS)
def test_the_oracle_types_a_when_column_by_chunk_contents(
    kind: str, scenario: str, tmp_path: Path
) -> None:
    """A degenerate column (no rows, or only nulls) comes back Arrow `null` from both the
    whole-frame run and the chunked oracle; any column holding a value comes back `string`.
    That chunk-content dependence is why the chunked output type is pinned (plan 3f)."""
    source, predicate = _scenario(scenario)
    config = when_config(kind, predicate)
    chunks = chunk_by_sizes(source, [5, 6]) if source.num_rows else [source]
    expected = pa.null() if scenario in _DEGENERATE else pa.string()
    chunked = run_public_oracle(config, chunks)
    assert [t.schema.field("s").type for t in chunked] == [expected] * len(chunks)
    assert _whole_frame(config, source, tmp_path).schema.field("s").type == expected
