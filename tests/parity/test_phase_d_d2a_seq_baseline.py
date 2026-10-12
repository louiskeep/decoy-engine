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

_COLUMNS = [*typed_columns()[0], "c_trunc", "c_other_ns"]
_SIGNATURE_T = (
    "c_utf8:string,c_large:string,c_bool:string,c_i8:string,c_i16:string,c_i32:string,"
    "c_i64:string,c_u8:string,c_u16:string,c_u32:string,c_u64:string,c_trunc:string,"
    "c_other_ns:string,fk:int64,hk:string"
)
_DIGESTS = {
    "up": "367d4298cfd04154d0fb035828c5cb5dec560e361a575c8e31eaeef112040b73",
    "t": "35fd98f3d318aad1952f874cf0c042c1afc9e4cc9909a0d78eddc2aa82c181dc",
    "child": "d7179a5cbc2c8d86dde231a3e1cf3550378f773f88ba6ec150177ff9dc1893c3",
}
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
# Orphan child keys "orph1", "orph2" minted by REMAP through the parent hash (ns_hk).
_REMAP_ORPHANS = (
    "eec76cc0854f45170489845b902c4b25c8a159db8e3703ffe0672d377a6c6cdf",
    "112054fc4071e2e01bace616c8f68ef3c7595d55b0f11633046610a06e1c3aef",
)
# Int parent keys 0..3 (ns_hk); 7 and 300 are orphans under REMAP.
_INT_KEY_TOKENS = (
    "fff6d07feba156f6826ff1d01f734e0c546250ad326e8928f64e6616ba752ccd",
    "2281ce17d19092cdd3131a04ca5674fb4a18e93d6b00b11753d43108a6134f1f",
    "27b0f2ce7c10728f371130bacdafc5cf362ca3eb8966093ca21972b07289e4b2",
    "fb2f5df29035d1a294711f9e858996e775abf22ce35f564c666b5655da592885",
)
_INT_REMAP_7 = "9b6d51b87187710ef3eca19de8bc15979febd256571c13b776fbc88300211ffa"
_INT_REMAP_300 = "fa05044540ef148d161877d3a9510e6dd2aa48a5c3562ee79e5f43f06b5c9ea0"


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


def test_nullable_integer_hash_payload_is_still_rejected() -> None:
    nullable = source_nullable_int()
    with pytest.raises(ExecutionError) as exc:
        run_seq(nullable, (("c_i8", column_seed()),))
    assert exc.value.code == "null_bearing_int_unsupported"


def test_nullable_integer_parent_key_is_still_rejected() -> None:
    srcs = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = srcs["t"]
    srcs["t"] = t.set_column(
        t.schema.get_field_index("hk"), "hk", pa.array([0, None, 2, 3], pa.int64())
    )
    with pytest.raises(ExecutionError) as exc:
        run_seq(None, _A_SEED, sources=srcs)
    assert exc.value.code == "null_bearing_int_unsupported"


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
        "396638be5d87d395d352c03a410a27cfe6531419520e706e69b01fa9f04b1eff",
        "y",
        "7931cf8b1bb2ea09557876229a27206c8449485be75ba2d2dfae80e42fe8269f",
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
        "396638be5d87d395d352c03a410a27cfe6531419520e706e69b01fa9f04b1eff",
        None,
        "7931cf8b1bb2ea09557876229a27206c8449485be75ba2d2dfae80e42fe8269f",
        "v",
    ]


def test_zero_match_when_leaves_the_column_untouched() -> None:
    payload = pa.table({"a": pa.array(["w", "x"], pa.string()), "i": pa.array([1, 2], pa.int32())})
    out = run_seq(payload, (("a", with_when("fk > 100")), ("i", with_when("fk > 100")))).outputs
    assert out["t"].column("a").to_pylist() == ["w", "x"]
    assert out["t"].column("i").to_pylist() == [1, 2]
    assert out["t"].column("i").type == pa.int32()


@pytest.mark.parametrize(
    ("values", "arrow_type"),
    [([1, 2, 3, 4], pa.int32()), ([True, False, True, False], pa.bool_())],
)
def test_partial_when_on_a_non_text_column_fails_on_conversion(
    values: list[object], arrow_type: pa.DataType
) -> None:
    # The handler stitches string tokens into a typed column; the output conversion refuses it.
    # This is today's behavior on every route that stitches a partial gate, frozen as is.
    payload = pa.table({"x": pa.array(values, arrow_type)})
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError)):
        run_seq(payload, (("x", with_when("fk == 1 or fk == 3")),))


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


# Negative witness baseline: today every hash value on SEQ comes from the Python handler.


def _spy_python_hash(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from decoy_engine.execution._strategies import _hash

    rows: list[int] = []
    real = _hash.hash_array

    def spy(values, **kwargs):
        rows.append(len(values))
        return real(values, **kwargs)

    monkeypatch.setattr(_hash, "hash_array", spy)
    return rows


def test_every_payload_and_parent_hash_runs_in_the_python_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _spy_python_hash(monkeypatch)
    run_seq(payload_table(), payload_seeds())
    # 13 payload columns plus the parent key `hk`; the matched child `ck` is inherited and
    # makes no hash call.
    assert rows == [10] * 14


def test_remap_runs_the_parent_hash_handler_once_for_the_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _spy_python_hash(monkeypatch)
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    run_seq(payload_table(), payload_seeds(), policy=OrphanPolicy.REMAP, sources=srcs)
    assert rows == [10] * 14 + [2]
