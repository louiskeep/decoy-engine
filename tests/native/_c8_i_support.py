"""Shared builders for the C8-i (`when:` on the chunked native route) tests.

Every comparison is against the pandas oracle, so no value here needs a hand-written
expected output. The generated-predicate strategy is shared by the grammar test (every
accepted string evaluates under the oracle's eval) and the mask-parity test.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pyarrow as pa
from hypothesis import strategies as st

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution.native._dispatch import NativeRouteEvidence
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    categorical,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    truncate,
)

S_VALUES = ["red", "green", None, "blue", "red", "green", "blue", None, "red", "blue", "green"]
P_VALUES = ["x", "y", None, "x", "y", "x", None, "y", "x", "x", "y"]
U_VALUES = ["q", "r", "q", None, "r", "q", "r", "q", None, "q", "r"]
N_VALUES = [1, 5, None, 3, 2, 8, None, 4, 9, 1, 6]


def source_table() -> pa.Table:
    return pa.table(
        {
            "s": pa.array(S_VALUES, pa.string()),
            "p": pa.array(P_VALUES, pa.string()),
            "u": pa.array(U_VALUES, pa.string()),
            "n": pa.array(N_VALUES, pa.int64()),
        }
    )


# Chunk sizes; a zero is a zero-row chunk with the full schema.
CHUNKINGS: dict[str, list[int]] = {
    "even": [4, 4, 3],
    "ragged": [1, 0, 4, 2, 0, 3, 1],
    "single_row": [1] * 11,
    "whole": [11],
}


def chunk_by_sizes(table: pa.Table, sizes: list[int]) -> list[pa.Table]:
    out, at = [], 0
    for size in sizes:
        out.append(table.slice(at, size))
        at += size
    assert at == table.num_rows
    return out


PREDICATES: dict[str, str] = {
    "target_eq": "s == 'red'",
    "target_in": "s in ['red', 'blue']",
    "target_ne": "s != 'red'",
    "target_not": "not (s == 'red')",
    "passthrough_eq": "p == 'x'",
    "passthrough_in": "p in ['x', 'y']",
    "passthrough_ne": "p != 'x'",
    "unconfigured_eq": "u == 'q'",
    "compound_and": "s == 'red' and p != 'y'",
    "compound_or": "p == 'y' or u == 'r'",
    "zero_match": "p == 'zzz'",
    "all_match": "p != 'zzz'",
    "numeric_reference": "n > 3",
}


def operator_column(kind: str) -> dict[str, Any]:
    return {
        "redact": redact("s"),
        "truncate": truncate("s", 3),
        "hash": hash_col("s"),
        "categorical": categorical("s"),
    }[kind]


def when_config(
    kind: str, predicate: str, *, extra: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The operator on `s` gated by `predicate`; `p` is configured passthrough, `u` and `n`
    are unconfigured."""
    return make_config(
        [{**operator_column(kind), "when": predicate}, passthrough("p"), *(extra or [])]
    )


@contextmanager
def companion_missing(monkeypatch: Any) -> Iterator[None]:
    """Force the oracle leg with the stock adapter: a hash column then has no kernel."""
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
    yield


def run_native(
    config: dict[str, Any],
    chunks: list[pa.Table],
    *,
    sink: list[Any] | None = None,
) -> tuple[list[pa.Table], NativeRouteEvidence]:
    evidence: list[NativeRouteEvidence] = []
    out = list(
        run_mask_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=evidence,
            chunk_result_sink=sink,
        )
    )
    return out, evidence[0]


def run_oracle_leg(
    config: dict[str, Any], chunks: list[pa.Table], monkeypatch: Any
) -> tuple[list[pa.Table], NativeRouteEvidence]:
    """The oracle leg of `run_mask_chunked`, forced by an unadmittable sibling column: the
    config gains a hash column `h` with no companion. Its output is stripped of `h`."""
    config = make_config(
        [
            *[{k: v for k, v in c.items()} for c in _columns(config)],
            hash_col("h"),
        ]
    )
    chunks = [t.append_column("h", pa.array([f"h{i}" for i in range(t.num_rows)])) for t in chunks]
    with companion_missing(monkeypatch):
        out, ev = run_native(config, chunks)
    assert ev.native_admitted is False, ev
    return [t.drop_columns(["h"]) for t in out], ev


def _columns(config: dict[str, Any]) -> list[dict[str, Any]]:
    return config["tables"][0]["columns"]


def run_public_oracle(config: dict[str, Any], chunks: list[pa.Table]) -> list[pa.Table]:
    return list(
        run_mask_pipeline_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )


# ---------------------------------------------------------------------------
# Generated predicates (grammar test 1b, mask-parity test 3).
# ---------------------------------------------------------------------------

_RESERVED_FOR_GENERATION = frozenset(
    {"and", "or", "not", "in", "is", "if", "as", "True", "False", "None"}
)

_text_alphabet = st.characters(
    blacklist_categories=("Cc", "Cs", "Zl", "Zp"), blacklist_characters="'\"\\"
)


def identifiers() -> st.SearchStrategy[str]:
    from decoy_engine.expressions._when_parser import RESERVED_NAMES

    return (
        st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,8}", fullmatch=True)
        .filter(
            lambda n: n not in RESERVED_NAMES and not n.startswith("__") and not n.endswith("__")
        )
        .filter(lambda n: n not in _RESERVED_FOR_GENERATION)
    )


_int_literal = st.integers(min_value=-(2**40), max_value=2**40)
_float_literal = st.floats(allow_nan=False, allow_infinity=False, width=32)
_str_literal = st.text(alphabet=_text_alphabet, max_size=6)


def _spell_number(value: int | float) -> str:
    return repr(value)


def _spell_string(value: str, quote: str) -> str:
    return f"{quote}{value}{quote}"


_CMP = st.sampled_from(["==", "!=", "<", "<=", ">", ">="])
_EQ = st.sampled_from(["==", "!="])


@st.composite
def _atom(draw: Any, columns: dict[str, str]) -> str:
    name = draw(st.sampled_from(sorted(columns)))
    kind = columns[name]
    quote = draw(st.sampled_from(["'", '"']))
    if kind == "string":
        lit = lambda: _spell_string(draw(_str_literal), quote)
        ops = _CMP
    elif kind == "bool":
        lit = lambda: draw(st.sampled_from(["True", "False"]))
        ops = _EQ
    else:
        lit = lambda: _spell_number(draw(st.one_of(_int_literal, _float_literal)))
        ops = _CMP
    if draw(st.booleans()):
        items = ", ".join(lit() for _ in range(draw(st.integers(min_value=1, max_value=3))))
        negated = "not in" if draw(st.booleans()) else "in"
        return f"{name} {negated} [{items}]"
    op = draw(ops)
    if draw(st.booleans()):
        return f"{name} {op} {lit()}"
    return f"{lit()} {op} {name}"


def predicates(columns: dict[str, str]) -> st.SearchStrategy[str]:
    """Grammar-valid predicates over `columns` (name -> "string" | "number" | "bool"), each
    atom compares a column with literals of a compatible type."""
    return st.recursive(
        _atom(columns),
        lambda inner: st.one_of(
            st.builds(lambda e: f"not ({e})", inner),
            st.builds(lambda a, b: f"({a}) and ({b})", inner, inner),
            st.builds(lambda a, b: f"({a}) or ({b})", inner, inner),
        ),
        max_leaves=4,
    )
