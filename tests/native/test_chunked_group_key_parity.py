"""C3 acceptance: native chunked group_key equals the oracle chunked leg.

Values, Arrow types and evidence per chunk and reassembled, over the sibling types the
native leg admits ({string, int64, bool}, with and without nulls), the chunk shapes that
matter (empty, single row, ragged, siblings repeated across a chunk boundary), several chunk
sizes and thread counts. Then the value-keyed known-answer vectors, the prefix
normalization, the later null-typed sibling chunk (the raw-chunk read), and the output-type
pin on both legs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution._chunked import concat_masked_chunks
from decoy_engine.execution.native import _chunk_masking
from decoy_engine.execution.native._chunked_schema_rule import build_schema_rule
from decoy_engine.execution.native._group_key_kernel import native_group_key
from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    key_provider,
)
from tests.native._chunked_group_key_support import (
    FORCE,
    GB,
    MASK_KEY,
    TARGET,
    assert_native_equals_oracle,
    columns,
    expected_key,
    gk_col,
    gk_source,
    make_config,
    passthrough,
    redact,
    run_one,
    run_pair,
    split,
    with_force,
)
from tests.native.test_chunked_entry_values_schema import _full_frame

_SIBLINGS: dict[str, tuple[pa.DataType, list[Any]]] = {
    "string_nulls": (pa.string(), ["h1", "h2", None, "h1", "h3", "h2", "h1", "h4", None, "h3"]),
    "string": (pa.string(), ["h1", "h2", "h1", "h3", "h2", "h1", "h4", "h4", "h3", "h2"]),
    "int64_nulls": (pa.int64(), [1, 2, None, 1, 3, 2, 1, 4, None, 3]),
    "int64": (pa.int64(), [1, 2, 1, 3, 2, 1, 4, 4, 3, 2]),
    "bool_nulls": (pa.bool_(), [True, False, None, True, False, None, True, True, False, None]),
    "bool": (pa.bool_(), [True, False, False, True, False, True, True, True, False, False]),
}


def _shape(values: list[Any], shape: str) -> list[Any]:
    if shape == "empty":
        return []
    if shape == "single_row":
        return values[:1]
    if shape == "repeated_boundary":
        return [v for v in values[:3] if v is not None][:2] * 6
    return values


def _chunks_for(label: str, shape: str, size: int) -> list[pa.Table]:
    typ, values = _SIBLINGS[label]
    table = gk_source(_shape(values, shape), typ)
    return split(table, size) or [table]


def _keys_by_value(out: list[pa.Table], chunks: list[pa.Table]) -> dict[Any, set[str]]:
    seen: dict[Any, set[str]] = {}
    for o, c in zip(out, chunks, strict=True):
        for v, k in zip(c.column(GB).to_pylist(), o.column(TARGET).to_pylist(), strict=True):
            seen.setdefault(v, set()).add(k)
    return seen


# ---------------------------------------------------------------------------
# 1. Parity matrix.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("shape", ["ragged", "single_row", "empty", "repeated_boundary"])
@pytest.mark.parametrize("label", sorted(_SIBLINGS))
def test_native_chunked_equals_oracle_chunked(
    label: str, shape: str, size: int, threads: int
) -> None:
    chunks = _chunks_for(label, shape, size)
    native, forced = run_pair(columns(), chunks, native_threads=threads)
    assert_native_equals_oracle(native, forced)
    assert {o.schema.field(TARGET).type for o in native.out} == {pa.string()}
    assert {o.schema.field(TARGET).type for o in forced.out} == {pa.string()}
    # The same sibling value maps to one key, across chunk boundaries too.
    seen = _keys_by_value(native.out, chunks)
    assert all(len(keys) == 1 for keys in seen.values()), seen
    # Pinned to the formula, so equal-to-the-oracle is not the only anchor.
    # A null sibling stringifies per its pandas dtype, so only valued rows are pinned here.
    for v, keys in seen.items():
        if v is not None:
            assert keys == {expected_key(v)}, v


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_a_multi_chunk_fifty_thousand_row_run_is_identical_on_both_legs(threads: int) -> None:
    values = [None if i % 11 == 3 else f"h{i % 97}" for i in range(100_003)]
    native, forced = run_pair(
        columns(), split(gk_source(values, pa.string()), 50_000), native_threads=threads
    )
    assert len(native.out) == 3
    assert_native_equals_oracle(native, forced)


@NEEDS_COMPANION
def test_empty_chunks_between_valued_chunks_are_identical() -> None:
    chunks = [
        gk_source(["a", "b", "a"], pa.string()),
        gk_source([], pa.string()),
        gk_source(["b", "c"], pa.string()),
        gk_source([], pa.string()),
    ]
    native, forced = run_pair(columns(), chunks)
    assert_native_equals_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("length", [8, 16, 64])
@pytest.mark.parametrize("prefix", ["", "HH-"])
@pytest.mark.parametrize("label", ["string_nulls", "int64_nulls", "bool"])
def test_length_and_prefix_match_the_oracle_and_the_formula(
    label: str, prefix: str, length: int
) -> None:
    typ, values = _SIBLINGS[label]
    native, forced = run_pair(
        columns(gk_col(length=length, prefix=prefix)), split(gk_source(values, typ), 4)
    )
    assert_native_equals_oracle(native, forced)
    for v, k in zip(
        [v for c in split(gk_source(values, typ), 4) for v in c.column(GB).to_pylist()],
        [k for o in native.out for k in o.column(TARGET).to_pylist()],
        strict=True,
    ):
        if v is not None:
            assert k == expected_key(v, length=length, prefix=prefix)


@NEEDS_COMPANION
def test_native_equals_the_full_frame_run(tmp_path: Path) -> None:
    typ, values = _SIBLINGS["int64_nulls"]
    table = gk_source(values, typ)
    native = run_one(make_config(columns()), split(table, 3))
    full = _full_frame(make_config(columns()), table, tmp_path)
    assert [k for o in native.out for k in o.column(TARGET).to_pylist()] == full.column(
        TARGET
    ).to_pylist()


# ---------------------------------------------------------------------------
# 2. Value-keyed known-answer vectors (frozen), differentials and the branch arguments.
# ---------------------------------------------------------------------------

_ALICE = "088e11ec1a338fe6"
_BOB = "75c1be3c3e7c7854"
_NONE = "48c35de5fbcb10f5"
_NA = "c5e7e87aa79ff4ce"
_FORTY_TWO = "e8f22cd51e106615"
_TRUE = "8a87d50e00262dc9"
_FALSE = "f8de89be6b051dcd"


@NEEDS_COMPANION
@pytest.mark.parametrize(
    ("typ", "values", "want"),
    [
        (pa.string(), ["alice", "bob", None, "alice"], [_ALICE, _BOB, _NONE, _ALICE]),
        (pa.int64(), [42, None, 42], [_FORTY_TWO, _NA, _FORTY_TWO]),
        (pa.bool_(), [True, False, True], [_TRUE, _FALSE, _TRUE]),
    ],
    ids=["string", "int64", "bool"],
)
def test_frozen_known_answers(typ: pa.DataType, values: list[Any], want: list[str]) -> None:
    run = run_one(make_config(columns()), split(gk_source(values, typ), 2))
    assert run.ev[0].native_admitted is True
    got = [k for o in run.out for k in o.column(TARGET).to_pylist()]
    assert got == want


@NEEDS_COMPANION
def test_the_frozen_vectors_are_the_scalar_formula() -> None:
    assert expected_key("alice") == _ALICE and expected_key("bob") == _BOB
    assert expected_key("None") == _NONE and expected_key("<NA>") == _NA
    assert expected_key(42) == _FORTY_TWO
    assert expected_key(True) == _TRUE and expected_key(False) == _FALSE


def _run_keys(
    cols: list[dict[str, Any]], table: pa.Table, *, secret: bytes | None = None
) -> list[str]:
    out = list(
        run_mask_chunked(
            make_config(cols),
            split(table, 2),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider() if secret is None else key_provider(secret),
        )
    )
    return [k for o in out for k in o.column(TARGET).to_pylist()]


@NEEDS_COMPANION
def test_a_different_mask_key_changes_every_key_and_matches_the_formula() -> None:
    table = gk_source(["alice", "bob", "alice"], pa.string())
    other = bytes(reversed(range(32)))
    base = _run_keys(columns(), table)
    changed = _run_keys(columns(), table, secret=other)
    assert base == [_ALICE, _BOB, _ALICE]
    assert changed == [
        expected_key(v, mask_key=key_provider(other).mask_key()) for v in ("alice", "bob", "alice")
    ]
    assert all(a != b for a, b in zip(base, changed, strict=True))


@NEEDS_COMPANION
def test_length_prefix_and_target_column_each_change_the_key() -> None:
    table = gk_source(["alice"], pa.string())
    table = table.append_column("k2", pa.array(["x"], pa.string()))
    base = _run_keys(columns(), table)[0]
    assert base == _ALICE
    assert _run_keys(columns(gk_col(length=32)), table)[0] == expected_key("alice", length=32)
    assert _run_keys(columns(gk_col(prefix="HH-")), table)[0] == "HH-" + _ALICE
    cols = [passthrough(GB), passthrough(TARGET), gk_col("k2"), passthrough("p")]
    out = list(
        run_mask_chunked(
            make_config(cols),
            [table],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    other = out[0].column("k2").to_pylist()[0]
    assert other == expected_key("alice", column="k2")
    assert other != base, "the namespace is synthesized from the TARGET column"


@NEEDS_COMPANION
def test_a_plan_namespace_on_the_column_does_not_change_the_synthesized_one() -> None:
    """The oracle handler derives under `group_key/<target>` and ignores the plan namespace."""
    table = gk_source(["alice", "bob"], pa.string())
    cols = columns(gk_col(namespace="ns_custom"))
    native, forced = run_pair(cols, split(table, 2))
    assert_native_equals_oracle(native, forced)
    assert [k for o in native.out for k in o.column(TARGET).to_pylist()] == [_ALICE, _BOB]


@NEEDS_COMPANION
def test_renaming_the_group_by_source_column_leaves_the_key_unchanged() -> None:
    table = gk_source(["alice", "bob"], pa.string()).rename_columns(["household", "k", "p"])
    cols = [passthrough("household"), gk_col(group_by="household"), passthrough("p")]
    assert _run_keys(cols, table) == [_ALICE, _BOB]


@NEEDS_COMPANION
def test_the_branch_receives_the_resolved_mask_key_namespace_and_sibling_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real = native_group_key

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunk_masking, "native_group_key", spy)
    run_one(make_config(columns(gk_col(length=24, prefix="HH-"))), split(gk_source(["a", "b"]), 1))
    assert len(calls) == 2
    for args, kwargs in calls:
        sibling = args[0]
        assert isinstance(sibling, pa.Table) and sibling.column_names == [GB]
        assert kwargs["mask_key"] == key_provider().mask_key() == MASK_KEY
        assert kwargs["namespace"] == f"group_key/{TARGET}"
        assert kwargs["length"] == 24 and kwargs["prefix"] == "HH-"
        assert kwargs["raw_hex_kernel"] is not None, "the preflight kernel is threaded in"


@NEEDS_COMPANION
@pytest.mark.parametrize(
    ("prefix", "text"), [(None, "None"), (False, "False"), (7, "7")], ids=["none", "false", "int"]
)
def test_a_non_string_prefix_is_str_normalized_like_the_oracle(prefix: Any, text: str) -> None:
    table = gk_source(["alice", "bob", "alice"], pa.string())
    native, forced = run_pair(columns(gk_col(prefix=prefix)), split(table, 2))
    assert_native_equals_oracle(native, forced)
    got = [k for o in native.out for k in o.column(TARGET).to_pylist()]
    assert got == [text + _ALICE, text + _BOB, text + _ALICE]


# ---------------------------------------------------------------------------
# 5b. A later null-typed sibling chunk, and a metadata-bearing sibling beside a column that
#     becomes null-typed: the branch reads the RAW chunk, as the oracle does.
# ---------------------------------------------------------------------------


def _null_sibling_chunk(n: int) -> pa.Table:
    return pa.table(
        {
            GB: pa.nulls(n),
            TARGET: pa.array(["x"] * n, pa.string()),
            "p": pa.array(list(range(n)), pa.int64()),
        }
    )


@NEEDS_COMPANION
@pytest.mark.parametrize("typ_values", [(pa.int64(), [1, 2, 1]), (pa.string(), ["a", "b", "a"])])
def test_a_later_null_typed_sibling_chunk_stringifies_none_like_the_oracle(
    typ_values: tuple[pa.DataType, list[Any]],
) -> None:
    typ, values = typ_values
    chunks = [gk_source(values, typ), _null_sibling_chunk(3), gk_source(values[:2], typ)]
    native, forced = run_pair(columns(), [c for c in chunks])
    assert_native_equals_oracle(native, forced)
    assert native.out[1].column(TARGET).to_pylist() == [_NONE] * 3, (
        "a raw null column stringifies as 'None', never the cast table's '<NA>'"
    )


def _pandas_chunk(g: list[Any], u: list[Any], dtype: str) -> pa.Table:
    frame = pd.DataFrame(
        {GB: pd.array(g, dtype=dtype), TARGET: ["x"] * len(g), "u": pd.Series(u, dtype=object)}
    )
    return pa.Table.from_pandas(frame, preserve_index=False)


@NEEDS_COMPANION
@pytest.mark.parametrize("dtype", ["string", "boolean", "Int64"])
def test_a_metadata_bearing_sibling_survives_an_unrelated_null_typed_column(dtype: str) -> None:
    by_dtype: dict[str, list[Any]] = {
        "string": ["a", None, "a"],
        "boolean": [True, None, True],
        "Int64": [1, None, 1],
    }
    g1 = by_dtype[dtype]
    first = _pandas_chunk(g1, [1, 2, 3], dtype)
    second = _pandas_chunk(g1, [None, None, None], dtype)
    assert pa.types.is_null(second.schema.field("u").type)
    assert first.schema.metadata and b"pandas" in first.schema.metadata
    cols = [passthrough(GB), gk_col(), passthrough("u")]
    native = run_one(make_config(cols), [first, second])
    forced = run_one(make_config([*cols, *_force_cols()]), [with_force(first), with_force(second)])
    assert native.ev[0].native_admitted is True
    for got, want in zip(native.out, forced.out, strict=True):
        assert got.column(TARGET).to_pylist() == want.column(TARGET).to_pylist()
    # The metadata-bearing null is "<NA>" on the oracle; a cast table (no metadata) would say "None".
    assert native.out[1].column(TARGET).to_pylist()[1] == expected_key("<NA>")


def _force_cols() -> list[dict[str, Any]]:
    from tests.native._chunked_entry_support import force_oracle

    return [force_oracle(FORCE)]


# ---------------------------------------------------------------------------
# 3. Output type: pinned to `string` on both legs and at every schema-rule site.
# ---------------------------------------------------------------------------


def _types(out: list[pa.Table]) -> list[pa.DataType]:
    return [t.schema.field(TARGET).type for t in out]


def _rule_strings(cols: list[dict[str, Any]], table: pa.Table) -> frozenset[str]:
    """What both construction sites call (the dispatcher entry and the streamed sink): the
    group_key pin must not depend on a per-site argument."""
    rule = build_schema_rule(
        make_config(cols), table=TABLE, first=table, registry=get_default_registry()
    )
    return rule.string_columns


_TYPE_CASES = {
    "zero_row": lambda: [gk_source([], pa.string())],
    "valued": lambda: [gk_source(["a", "b", None], pa.string())],
    "empty_then_valued": lambda: [gk_source([], pa.string()), gk_source(["a", "b"], pa.string())],
    "valued_then_empty": lambda: [gk_source(["a", "b"], pa.string()), gk_source([], pa.string())],
    "all_empty": lambda: [gk_source([], pa.string()), gk_source([], pa.string())],
}


@NEEDS_COMPANION
@pytest.mark.parametrize("target", [pa.string(), pa.int64()], ids=["target_string", "target_int"])
@pytest.mark.parametrize("case", sorted(_TYPE_CASES))
def test_output_type_is_string_on_both_legs_whatever_the_target_source_type(
    case: str, target: pa.DataType
) -> None:
    chunks = [
        c.set_column(
            1,
            TARGET,
            pa.array(
                list(range(c.num_rows)) if target == pa.int64() else ["x"] * c.num_rows, target
            ),
        )
        for c in _TYPE_CASES[case]()
    ]
    native, forced = run_pair(columns(), chunks)
    assert native.ev[0].native_admitted is True
    assert _types(native.out) == [pa.string()] * len(chunks)
    assert _types(forced.out) == [pa.string()] * len(chunks)
    reassembled = concat_masked_chunks(
        [t.drop_columns([FORCE]) if FORCE in t.column_names else t for t in native.out],
        table=TABLE,
    )
    assert reassembled.schema.field(TARGET).type == pa.string()


@pytest.mark.parametrize("target", [pa.string(), pa.int64()], ids=["target_string", "target_int"])
@pytest.mark.parametrize("sibling", [pa.string(), pa.int64(), pa.bool_()])
def test_the_pin_keys_on_the_sibling_type_not_the_target(
    sibling: pa.DataType, target: pa.DataType
) -> None:
    table = pa.table(
        {
            GB: pa.array([], sibling),
            TARGET: pa.array([], target),
            "p": pa.array([], pa.int64()),
        }
    )
    assert TARGET in _rule_strings(columns(), table)


@pytest.mark.parametrize(
    "case",
    [
        "int32_sibling",
        "uint64_sibling",
        "large_string_sibling",
        "masked_sibling",
        "self_anchor",
    ],
)
def test_a_non_admissible_group_key_is_not_pinned(case: str) -> None:
    sibling = {
        "int32_sibling": pa.int32(),
        "uint64_sibling": pa.uint64(),
        "large_string_sibling": pa.large_string(),
    }.get(case, pa.string())
    table = pa.table(
        {
            GB: pa.array([], sibling),
            TARGET: pa.array([], pa.string()),
            "p": pa.array([], pa.int64()),
        }
    )
    cols = columns()
    if case == "masked_sibling":
        cols = [redact(GB), gk_col(), passthrough("p")]
    elif case == "self_anchor":
        cols = [gk_col(group_by=TARGET), passthrough("p")]
    assert TARGET not in _rule_strings(cols, table)


def test_a_when_gated_or_missing_sibling_group_key_is_not_pinned() -> None:
    table = pa.table(
        {
            GB: pa.array([], pa.string()),
            TARGET: pa.array([], pa.string()),
            "p": pa.array([], pa.int64()),
        }
    )
    gated = gk_col()
    gated["when"] = "p > 1"
    assert TARGET not in _rule_strings([passthrough(GB), gated, passthrough("p")], table)
    assert TARGET not in _rule_strings(columns(gk_col(group_by="nope")), table)


@NEEDS_COMPANION
def test_the_empty_chunk_type_diff_against_the_full_frame_route_is_the_recorded_one(
    tmp_path: Path,
) -> None:
    """Chunked pins `string` for an empty chunk; the full-frame route reconciles the empty
    tokenizing column to float64 at assembly. This route-dependent difference is accepted and
    recorded (docs/compatibility-contract.md, ROUTE-OUTPUT-CONTRACT)."""
    table = gk_source([], pa.string())
    chunked = run_one(make_config(columns()), [table])
    assert chunked.ev[0].native_admitted is True
    assert _types(chunked.out) == [pa.string()]
    full = _full_frame(make_config(columns()), table, tmp_path)
    assert full.schema.field(TARGET).type == pa.float64()


def test_a_non_admissible_group_key_keeps_the_type_its_leg_produces() -> None:
    from decoy_engine import run_mask_pipeline_chunked

    cols = [redact(GB), gk_col(), passthrough("p")]
    table = gk_source([], pa.string())
    run = run_one(make_config(cols), [table])
    assert run.ev[0].native_admitted is False
    want = list(
        run_mask_pipeline_chunked(
            make_config(cols),
            [table],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )
    assert _types(run.out) == _types(want)
