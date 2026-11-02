"""Phase D / D2a stage 1: the frozen strict-parity contract for the SEQ route (test-only).

`run_sequential` masks an FK job one table at a time in pandas. This module pins what it
emits today, at the strict level of the D0 comparator (schema incl. field metadata and
nullability, row and column order, null versus NaN), BEFORE the hash handler gains a native
path. It runs unchanged afterwards: native output must reproduce every literal here.

Every token, schema signature and digest below is a hardcoded literal. The token literals
come from the pure-Python `hash_array` (D1a's `EXPECTED_TOKENS`, with the SEQ-specific
integer null fill documented in `_d2a_seq_data`). The edge-case and REMAP literals were
captured from the engine at de1cb852, before any D2a source change. None is computed from the
output under test.

Re-pinned under F13 because job_seed keying was removed: the digests come from the full-frame pandas
oracle and the token literals from `hash_array`, all keyed on `THROWAWAY_KEY_PROVIDER`.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import EXPECTED_TOKENS, chain_sources, column_seed
from tests.parity._d2a_seq_data import (
    RecordingSink,
    callable_sink,
    payload_seeds,
    payload_table,
    run_full,
    run_plain,
    run_seq,
    sources_with_orphans,
    table_digest,
    typed_columns,
    with_when,
)
from tests.parity._seq_witness import hide_companion, spy_python_hash

_COLUMNS = [*typed_columns()[0], "c_trunc", "c_other_ns"]
_SIGNATURE_T = (
    "c_utf8:string,c_large:string,c_bool:string,c_i8:string,c_i16:string,c_i32:string,"
    "c_i64:string,c_u8:string,c_u16:string,c_u32:string,c_u64:string,c_trunc:string,"
    "c_other_ns:string,fk:int64,hk:string"
)
_DIGESTS = {
    "up": "367d4298cfd04154d0fb035828c5cb5dec560e361a575c8e31eaeef112040b73",
    "t": "aa930a2f0de7d243e62e3026d3108d9ec18b1ed6f69f8127d22955f0aa0b214b",
    "child": "8f3f86de6504353cc1fe7ceb1049a0386a2d4e736a1b54180b0626a065bf279b",
}
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
# Orphan child keys "orph1", "orph2" minted by REMAP through the parent hash (ns_hk).
_REMAP_ORPHANS = (
    "5d9cd6fdcf3c2ba555c7b3adaf62b52eaa9f8be0ca5c9f26157cd72251769871",
    "be1d93bbc6cfb23bd57442dc7e1a59a9f8be4feac5bc2ec6b19d1d3aedd3c48c",
)
# Int parent keys 0..3 (ns_hk); 7 and 300 are orphans under REMAP.
_INT_KEY_TOKENS = (
    "9b93a019d45609396afb0d9ab6264d04f69533480a5fbe8faf9d91e581573e9e",
    "df9df74e34b59ef619077d6e75abbf88bb53d07da1359d729541b21894c10ac0",
    "01c587d046c8fb2fce3a9862864a32c7864ca3a7d4a2ee4f3dc2616570227195",
    "7b0bc128750c49c8e23722c4a149e29dd2acb35a867a4f6536cf4b2d57b8f640",
)
_INT_REMAP_7 = "d704217d0e02b26d381cf82df7f3cd684f05a024f1ef299e5de097a0cd6a08aa"
_INT_REMAP_300 = "9d4d5ceef7e9a962dbcda218bd3a557d9cb42682b41f1cb224368da775add1d5"


def _int_key_sources(
    child_keys: list[int | None], parent_type: pa.DataType | None = None
) -> dict[str, pa.Table]:
    srcs = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = srcs["t"]
    srcs["t"] = t.set_column(
        t.schema.get_field_index("hk"), "hk", pa.array([0, 1, 2, 3], parent_type or pa.int64())
    )
    srcs["child"] = pa.table({"ck": pa.array(child_keys, pa.int64())})
    return srcs


_A_SEED = (("a", column_seed()),)


def test_seq_chain_matches_the_frozen_tokens() -> None:
    _, expected = typed_columns()
    out = run_seq(payload_table(), payload_seeds()).outputs
    for name in _COLUMNS:
        assert out["t"].column(name).type == pa.string(), name
        assert out["t"].column(name).to_pylist() == expected[name], name
    assert out["t"].column("hk").to_pylist() == EXPECTED_TOKENS["hk"]
    # Matched children take the parent's masked key unchanged.
    assert out["child"].column("ck").to_pylist() == EXPECTED_TOKENS["hk"]


def test_seq_chain_schema_and_digest_are_frozen() -> None:
    out = run_seq(payload_table(), payload_seeds()).outputs
    assert sc.schema_signature(out["t"]) == _SIGNATURE_T
    assert sc.schema_signature(out["child"]) == "ck:string"
    assert sc.schema_signature(out["up"]) == "pk:int64"
    for name, digest in _DIGESTS.items():
        assert table_digest(out[name]) == digest, name
        assert sc.metadata_keys(out[name]) == [b"pandas"], name
        assert out[name].schema.field(0).nullable is True


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN])
def test_seq_agrees_with_full_frame_on_every_dimension(policy: OrphanPolicy) -> None:
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    seq = run_seq(payload_table(), payload_seeds(), policy=policy, sources=srcs).outputs
    full = run_full(payload_table(), payload_seeds(), policy=policy, sources=srcs).outputs
    assert set(seq) == set(full)
    for name in full:
        assert sc.compare_strict(full[name], seq[name]) == _ALL_TRUE, name


def test_seq_remap_mints_orphans_through_the_parent_hash() -> None:
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    out = run_seq(payload_table(), payload_seeds(), policy=OrphanPolicy.REMAP, sources=srcs)
    ck = out.outputs["child"].column("ck").to_pylist()
    assert ck[:-2] == EXPECTED_TOKENS["hk"]
    assert tuple(ck[-2:]) == _REMAP_ORPHANS


def test_seq_preserve_keeps_orphans_raw() -> None:
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    out = run_seq(payload_table(), payload_seeds(), sources=srcs)
    assert out.outputs["child"].column("ck").to_pylist()[-2:] == ["orph1", "orph2"]


def test_seq_int_parent_key_remap_is_frozen_including_a_value_the_parent_width_cannot_hold() -> (
    None
):
    for parent_type in (pa.int64(), pa.int8()):
        srcs = _int_key_sources([0, None, 2, 7, 7, 300], parent_type)
        out = run_seq(None, _A_SEED, sources=srcs, policy=OrphanPolicy.REMAP).outputs
        assert out["child"].column("ck").to_pylist() == [
            _INT_KEY_TOKENS[0],
            None,
            _INT_KEY_TOKENS[2],
            _INT_REMAP_7,
            _INT_REMAP_7,
            _INT_REMAP_300,
        ], parent_type
        assert out["t"].column("hk").to_pylist() == list(_INT_KEY_TOKENS)


def test_nullable_child_inherits_the_parent_token_and_keeps_its_null() -> None:
    srcs = _int_key_sources([0, None, 2, 3])
    for policy in (OrphanPolicy.PRESERVE, OrphanPolicy.REMAP):
        seq = run_seq(None, _A_SEED, sources=srcs, policy=policy).outputs
        full = run_full(None, _A_SEED, sources=srcs, policy=policy).outputs
        assert seq["child"].column("ck").to_pylist() == [
            _INT_KEY_TOKENS[0],
            None,
            _INT_KEY_TOKENS[2],
            _INT_KEY_TOKENS[3],
        ]
        assert sc.compare_strict(full["child"], seq["child"]) == _ALL_TRUE


def test_nullable_integer_hash_payload_hashes_the_exact_integers_on_both_routes() -> None:
    # Int8 with a null: pandas widens the frame column to float64, but the hash keys from the
    # recorded Arrow values, so the tokens are D1a's frozen literals and the null stays null.
    tokens = EXPECTED_TOKENS["c_i8"]
    expected = [tokens[0], tokens[1], tokens[2], None, tokens[4]]
    nullable = source_nullable_int()
    seq = run_seq(nullable, (("c_i8", column_seed()),)).outputs
    full = run_full(nullable, (("c_i8", column_seed()),)).outputs
    assert seq["t"].column("c_i8").to_pylist() == expected
    assert sc.compare_strict(full["t"], seq["t"]) == _ALL_TRUE


def test_nullable_integer_parent_key_hashes_and_keeps_its_null_on_both_routes() -> None:
    srcs = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = srcs["t"]
    srcs["t"] = t.set_column(
        t.schema.get_field_index("hk"), "hk", pa.array([0, None, 2, 3], pa.int64())
    )
    expected = [_INT_KEY_TOKENS[0], None, _INT_KEY_TOKENS[2], _INT_KEY_TOKENS[3]]
    seq = run_seq(None, _A_SEED, sources=srcs).outputs
    full = run_full(None, _A_SEED, sources=srcs).outputs
    assert seq["t"].column("hk").to_pylist() == expected
    assert sc.compare_strict(full["t"], seq["t"]) == _ALL_TRUE


def source_nullable_int() -> pa.Table:
    return pa.table({"c_i8": pa.array([-128, -1, 0, None, 127], pa.int8())})


# Conversion edge cases. Each is the SEQ output today, so the native path inherits it.


def test_empty_table_keeps_the_double_columns_the_handler_leaves_today() -> None:
    empty = pa.table(
        {
            "a": pa.array([], pa.string()),
            "b": pa.array([], pa.large_string()),
            "c": pa.array([], pa.bool_()),
            "i": pa.array([], pa.int64()),
        }
    )
    seeds = tuple((n, column_seed()) for n in ("a", "b", "c", "i"))
    out = run_seq(empty, seeds).outputs
    assert sc.schema_signature(out["t"]) == "a:double,b:double,c:double,i:double,fk:int64,hk:double"
    assert sc.schema_signature(out["child"]) == "ck:null"
    assert out["t"].num_rows == 0
    full = run_full(empty, seeds).outputs
    assert sc.compare_strict(full["t"], out["t"]) == _ALL_TRUE


def test_all_null_string_and_bool_columns_come_out_as_null_typed() -> None:
    nulls = pa.table(
        {"a": pa.array([None] * 3, pa.string()), "c": pa.array([None] * 3, pa.bool_())}
    )
    seeds = (("a", column_seed()), ("c", column_seed()))
    out = run_seq(nulls, seeds).outputs["t"]
    assert sc.schema_signature(out) == "a:null,c:null,fk:int64,hk:string"
    assert out.column("a").to_pylist() == [None, None, None]
    assert out.column("c").to_pylist() == [None, None, None]


def test_noncontiguous_when_selection_hashes_in_place() -> None:
    payload = pa.table({"a": pa.array(["w", "x", "y", "z"], pa.string())})
    seeds = (("a", with_when("fk == 1 or fk == 3")),)
    out = run_seq(payload, seeds).outputs["t"]
    assert out.column("a").to_pylist() == [
        "w",
        "d299af0182676631ef59b1cd52de0fa402946c1cd7ea96e34ea231675a0323a6",
        "y",
        "b8eb27966956d259b9aa89927b3efa0902a931928b267a540b470f4313102156",
    ]
    assert out.column("a").type == pa.string()
    full = run_full(payload, seeds).outputs["t"]
    assert sc.compare_strict(full, out) == _ALL_TRUE


def test_contiguous_when_selection_on_large_string_with_nulls() -> None:
    payload = pa.table({"a": pa.array(["w", "x", None, "z", "v"], pa.large_string())})
    out = run_seq(payload, (("a", with_when("fk >= 1 and fk <= 3")),)).outputs["t"]
    assert out.column("a").type == pa.string()
    assert out.column("a").to_pylist() == [
        "w",
        "d299af0182676631ef59b1cd52de0fa402946c1cd7ea96e34ea231675a0323a6",
        None,
        "b8eb27966956d259b9aa89927b3efa0902a931928b267a540b470f4313102156",
        "v",
    ]


def test_zero_match_when_leaves_the_column_untouched() -> None:
    payload = pa.table({"a": pa.array(["w", "x"], pa.string())})
    out = run_seq(payload, (("a", with_when("fk > 100")),)).outputs
    assert out["t"].column("a").to_pylist() == ["w", "x"]


def test_gated_hash_on_an_integer_column_is_rejected_even_when_no_row_matches() -> None:
    # Conditional hashing of an integer column is deferred: the rejection keys on the Arrow
    # schema, not on how many rows the predicate selects.
    payload = pa.table({"i": pa.array([1, 2], pa.int32())})
    with pytest.raises(ExecutionError) as exc:
        run_seq(payload, (("i", with_when("fk > 100")),))
    assert exc.value.code == "gated_hash_on_integer_unsupported"


def test_partial_when_on_a_bool_column_fails_on_conversion() -> None:
    # The handler stitches string tokens into a typed column; the output conversion refuses it.
    # This is today's behavior on every route that stitches a partial gate, frozen as is.
    payload = pa.table({"x": pa.array([True, False, True, False], pa.bool_())})
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError)):
        run_seq(payload, (("x", with_when("fk == 1 or fk == 3")),))


def test_partial_when_on_an_integer_column_is_rejected_before_masking() -> None:
    payload = pa.table({"x": pa.array([1, 2, 3, 4], pa.int32())})
    with pytest.raises(ExecutionError) as exc:
        run_seq(payload, (("x", with_when("fk == 1 or fk == 3")),))
    assert exc.value.code == "gated_hash_on_integer_unsupported"


# Sink behavior: the atomicity claim is scoped to the sink.


def test_transactional_sink_commits_once_with_every_table() -> None:
    sink = RecordingSink()
    run_seq(payload_table(), payload_seeds(), sink=sink)
    assert (sink.commits, sink.aborts) == (1, 0)
    assert set(sink.committed) == {"up", "t", "child"}
    assert table_digest(sink.committed["t"]) == _DIGESTS["t"]


def test_transactional_sink_aborts_with_nothing_committed_after_earlier_staged_writes() -> None:
    sink = RecordingSink(fail_on_write="child")
    with pytest.raises(RuntimeError, match="injected sink fault"):
        run_seq(payload_table(), payload_seeds(), sink=sink)
    assert (sink.commits, sink.aborts) == (0, 1)
    assert sink.committed == {}
    assert sink.staged == {}


def test_transactional_sink_aborts_on_an_orphan_fail_after_staged_writes() -> None:
    sink = RecordingSink()
    srcs = sources_with_orphans(payload_table(), ["orph1"])
    with pytest.raises(ExecutionError) as exc:
        run_seq(payload_table(), payload_seeds(), policy=OrphanPolicy.FAIL, sources=srcs, sink=sink)
    assert exc.value.code == "orphan_fk_violation"
    assert (sink.commits, sink.aborts) == (0, 1)
    assert sink.committed == {}


def test_callable_sink_keeps_the_tables_delivered_before_a_later_failure() -> None:
    delivered: dict[str, pa.Table] = {}
    srcs = sources_with_orphans(payload_table(), ["orph1"])
    with pytest.raises(ExecutionError):
        run_seq(
            payload_table(),
            payload_seeds(),
            policy=OrphanPolicy.FAIL,
            sources=srcs,
            sink=callable_sink(delivered),
        )
    # No commit or abort exists for a callable: the parent tables already left the engine.
    assert set(delivered) == {"up", "t"}
    assert table_digest(delivered["t"]) == _DIGESTS["t"]


# A run with no hash column is untouched.


def test_a_run_without_a_hash_column_is_frozen_by_digest() -> None:
    out = run_plain().outputs["t"]
    assert sc.schema_signature(out) == "a:string,b:string"
    assert out.to_pydict() == {"a": ["x", "y", None], "b": ["abc", "gh", None]}
    assert table_digest(out) == "041806437378b258a638fe775abbe26bd613fefe3ee90f7d51f6066dac148905"
    assert run_plain().quality_metrics == {}


# Per-dimension controls on the SEQ output itself: the contract bites on each dimension.


def _t() -> pa.Table:
    return run_seq(payload_table(), payload_seeds()).outputs["t"]


def _mutations() -> dict[str, tuple[pa.Table, dict[str, bool]]]:
    base = _t()
    swapped = base.select(list(reversed(base.column_names)))
    nonnull = base.cast(
        pa.schema(
            [pa.field(f.name, f.type, nullable=(f.name != "hk")) for f in base.schema],
            metadata=base.schema.metadata,
        )
    )
    reordered = base.take(list(reversed(range(base.num_rows))))
    changed = base.set_column(
        base.schema.get_field_index("c_utf8"),
        "c_utf8",
        pa.array(["x"] * base.num_rows, pa.string()),
    )
    wide = base.set_column(
        base.schema.get_field_index("c_utf8"),
        "c_utf8",
        base.column("c_utf8").cast(pa.large_string()),
    )
    return {
        "schema_structure": (nonnull, {**_ALL_TRUE, "schema_structure": False}),
        "field_metadata": (
            base.replace_schema_metadata({b"pandas": b"{}"}),
            {**_ALL_TRUE, "field_metadata": False},
        ),
        "type_width": (
            wide,
            {**_ALL_TRUE, "schema_structure": False, "type_width": False},
        ),
        "column_order": (
            swapped,
            {**_ALL_TRUE, "schema_structure": False, "column_order": False},
        ),
        "row_count": (
            base.slice(0, base.num_rows - 1),
            {
                **_ALL_TRUE,
                "row_count": False,
                "null_vs_nan": False,
                "row_set": False,
                "row_order": False,
                "cellwise_values": False,
            },
        ),
        "row_order": (
            reordered,
            {**_ALL_TRUE, "row_order": False, "cellwise_values": False},
        ),
        "cellwise_values": (
            changed,
            {**_ALL_TRUE, "row_set": False, "row_order": False, "cellwise_values": False},
        ),
    }


@pytest.mark.parametrize("dimension", list(_mutations()))
def test_each_dimension_of_the_comparator_bites_on_a_seq_output(dimension: str) -> None:
    mutated, expected = _mutations()[dimension]
    assert sc.compare_strict(_t(), mutated) == expected
    assert sc.compare_strict(_t(), _t()) == _ALL_TRUE


# Python-only control: with the companion hidden, every hash value on SEQ comes from the Python
# handler. Stage 1 pinned this before the native route existed; it now needs the explicit hide.


def test_every_payload_and_parent_hash_runs_in_the_python_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_companion(monkeypatch)
    rows = spy_python_hash(monkeypatch)
    run_seq(payload_table(), payload_seeds())
    # 13 payload columns plus the parent key `hk`; the matched child `ck` is inherited and
    # makes no hash call.
    assert rows == [10] * 14


def test_remap_runs_the_parent_hash_handler_once_for_the_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_companion(monkeypatch)
    rows = spy_python_hash(monkeypatch)
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    run_seq(payload_table(), payload_seeds(), policy=OrphanPolicy.REMAP, sources=srcs)
    assert rows == [10] * 14 + [2]
