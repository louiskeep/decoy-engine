"""C5b-ii acceptance: non-deterministic REUSE Faker on the native CHUNKED route (parity).

Native chunked == oracle chunked == whole-frame on values, where the draw for the non-null row
at global position `g = base_row_offset + local` is
`pool.values[derive_index(job_seed, selection_namespace, encode_int(g), pool.size)]`
(plan section 5, tests 1, 2, 3, 6b and 9). The oracle leg is the same table with a still-vetoed
numeric-category categorical beside it (`run_pair`), which asserts the exact forced-oracle
reason on every call.

The KATs are FROZEN LITERALS captured once from the scalar primitive, never recomputed by the
code under test. REUSE permits collisions, so no assertion says "always differs" in general:
the discriminating assertions are tied to the frozen fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    column_values,
    key_provider,
    vault_key,
)
from tests.native._chunked_faker_support import (
    KAT_CONFIGURED,
    KAT_DEFAULT,
    KAT_EDGE_CONFIGURED,
    KAT_EDGE_DEFAULT,
    assert_same_as_oracle,
    default_namespace,
    expected_values,
    job_seed_of,
    make_config,
    nd_faker,
    passthrough,
    run_one,
    run_pair,
    source,
    split,
)
from tests.native.test_chunked_entry_values_schema import _full_frame

_REPEATING = [None if i % 6 == 2 else f"v{i % 5:02d}" for i in range(23)]

_SHAPES: dict[str, list[str | None]] = {
    "all_null_non_empty": [None] * 7,
    "single_row": ["only"],
    "ragged": _REPEATING,
    "null_block_then_valued": [None] * 7 + [f"w{i}" for i in range(9)],
}

_NAMESPACES = [None, "", "ns_f"]


def _cols(namespace: str | None, **kw: Any) -> list[dict[str, Any]]:
    return [nd_faker(namespace=namespace, **kw), passthrough("p")]


# ---------------------------------------------------------------------------
# 1. Parity matrix: native chunked == oracle chunked.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("namespace", _NAMESPACES, ids=["none", "empty", "configured"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_native_chunked_equals_oracle_chunked(
    shape: str, namespace: str | None, size: int, threads: int
) -> None:
    native, forced = run_pair(
        _cols(namespace), split(source(_SHAPES[shape]), size), native_threads=threads
    )
    assert_same_as_oracle(native, forced)
    assert native.ev[0].node_routes[0].route == "native_pool"
    for out in native.out:
        assert out.schema.field("f").type == pa.string()


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
def test_a_zero_row_table_is_identical_on_both_legs(namespace: str | None, threads: int) -> None:
    native, forced = run_pair(_cols(namespace), [source([])], native_threads=threads)
    assert_same_as_oracle(native, forced)
    assert native.out[0].num_rows == 0
    assert native.out[0].schema.field("f").type == pa.string()


@NEEDS_COMPANION
@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
def test_a_null_typed_later_chunk_is_identical_on_both_legs(namespace: str | None) -> None:
    valued = source(["a", "b", None, "a"])
    null_typed = pa.table({"f": pa.nulls(3), "p": pa.array([7, 8, 9], pa.int64())})
    native, forced = run_pair(_cols(namespace), [valued, null_typed])
    assert_same_as_oracle(native, forced)
    assert {o.schema.field("f").type for o in native.out} == {pa.string()}


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_a_multi_chunk_fifty_thousand_row_run_is_identical_on_both_legs(threads: int) -> None:
    values = [None if i % 11 == 3 else f"v{i % 997}" for i in range(100_003)]
    native, forced = run_pair(_cols(None), split(source(values), 50_000), native_threads=threads)
    assert len(native.out) == 3
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("namespace", _NAMESPACES, ids=["none", "empty", "configured"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_chunked_native_equals_the_whole_frame_run(
    shape: str, namespace: str | None, tmp_path: Path
) -> None:
    table = source(_SHAPES[shape])
    config = make_config(_cols(namespace))
    run = run_one(config, split(table, 7))
    assert run.ev[0].native_admitted is True
    full = _full_frame(config, table, tmp_path)
    assert column_values(run.out, "f") == full.column("f").to_pylist()


@NEEDS_COMPANION
def test_the_same_source_value_draws_by_global_position_across_chunks() -> None:
    config = make_config(_cols(None))
    run = run_one(config, split(source(["same"] * 40), 7))
    got = column_values(run.out, "f")
    assert run.ev[0].native_admitted is True
    assert len(set(got)) > 1, "a value-keyed draw would give every row the same value"


@NEEDS_COMPANION
def test_default_namespaces_differ_row_wise_between_columns_on_the_frozen_fixture() -> None:
    config = make_config([nd_faker("f"), nd_faker("g"), passthrough("p")])
    table = pa.table(
        {
            "f": pa.array(["x"] * 50),
            "g": pa.array(["x"] * 50),
            "p": pa.array(range(50), pa.int64()),
        }
    )
    run = run_one(config, split(table, 13))
    assert run.ev[0].native_admitted is True
    f, g = column_values(run.out, "f"), column_values(run.out, "g")
    assert f != g
    assert sum(a != b for a, b in zip(f, g, strict=True)) >= 40


@NEEDS_COMPANION
def test_the_same_explicit_namespace_and_pool_give_equal_columns_by_design() -> None:
    config = make_config(
        [nd_faker("f", namespace="shared"), nd_faker("g", namespace="shared"), passthrough("p")]
    )
    table = pa.table(
        {
            "f": pa.array(["x"] * 50),
            "g": pa.array(["y"] * 50),
            "p": pa.array(range(50), pa.int64()),
        }
    )
    run = run_one(config, split(table, 13))
    assert run.ev[0].native_admitted is True
    assert column_values(run.out, "f") == column_values(run.out, "g")


@NEEDS_COMPANION
def test_a_default_namespace_never_reuses_a_sibling_pool_keyed_by_the_same_string(
    tmp_path: Path,
) -> None:
    # `g` (declared first) configures, as its own namespace, exactly the string `f`'s default
    # selection namespace resolves to. Pool identity must stay on each column's CONFIGURED
    # namespace, so `f` builds its own pool rather than reusing `g`'s cached one.
    config = make_config(
        [
            nd_faker("g", namespace=default_namespace("t", "f")),
            nd_faker("f"),
            passthrough("p"),
        ]
    )
    table = pa.table(
        {
            "g": pa.array(["x"] * 40),
            "f": pa.array(["x"] * 40),
            "p": pa.array(range(40), pa.int64()),
        }
    )
    run = run_one(config, split(table, 9))
    assert run.ev[0].native_admitted is True
    full = _full_frame(config, table, tmp_path)
    assert column_values(run.out, "f") == full.column("f").to_pylist()
    assert column_values(run.out, "g") == full.column("g").to_pylist()


# ---------------------------------------------------------------------------
# 2. Global offset and uint64 domain.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
def test_a_nonzero_base_row_offset_draws_at_the_global_positions(namespace: str | None) -> None:
    config = make_config(_cols(namespace))
    values = [None if i % 6 == 2 else f"v{i}" for i in range(23)]
    run = run_one(config, split(source(values), 7), base_row_offset=1000)
    got = column_values(run.out, "f")
    want = expected_values(range(1000, 1023), config=config, namespace=namespace)
    assert run.ev[0].native_admitted is True
    assert got == [None if v is None else w for v, w in zip(values, want, strict=True)]


@NEEDS_COMPANION
@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
@pytest.mark.parametrize("g", [0, 2**63 - 1, 2**63, 2**64 - 1])
def test_frozen_kats_at_the_uint64_edges(namespace: str | None, g: int) -> None:
    config = make_config(_cols(namespace))
    table = source(["only"])
    native, forced = run_pair(_cols(namespace), [table], base_row_offset=g)
    assert_same_as_oracle(native, forced)
    edge = KAT_EDGE_CONFIGURED if namespace else KAT_EDGE_DEFAULT
    first = KAT_CONFIGURED if namespace else KAT_DEFAULT
    assert column_values(native.out, "f") == [first[0] if g == 0 else edge[g]]
    assert expected_values([g], config=config, namespace=namespace) == [
        first[0] if g == 0 else edge[g]
    ]


@pytest.mark.parametrize("native", [True, False], ids=["native-leg", "oracle-leg"])
def test_a_chunk_past_the_uint64_domain_raises_the_existing_public_code(native: bool) -> None:
    columns = _cols(None)
    chunks = [source(["a", "b"])]
    config = make_config(columns)
    if not native:
        from tests.native._b8_support import with_force
        from tests.native._chunked_entry_support import force_oracle

        config = make_config([*columns, force_oracle("cat_force")])
        chunks = [with_force(c) for c in chunks]
    with pytest.raises(ExecutionError) as info:
        run_one(config, chunks, base_row_offset=2**64 - 1)
    assert info.value.code == "chunked_row_offset_out_of_domain"


# ---------------------------------------------------------------------------
# 3. Key and namespace KATs (frozen literals).
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize(
    ("namespace", "frozen"),
    [(None, KAT_DEFAULT), ("ns_f", KAT_CONFIGURED)],
    ids=["default-namespace", "configured-namespace"],
)
def test_the_native_output_equals_the_frozen_kat_and_the_scalar_formula(
    namespace: str | None, frozen: list[str]
) -> None:
    config = make_config(_cols(namespace))
    run = run_one(config, split(source([f"s{i}" for i in range(12)]), 5))
    assert run.ev[0].native_admitted is True
    assert column_values(run.out, "f") == frozen
    assert expected_values(range(12), config=config, namespace=namespace) == frozen


@NEEDS_COMPANION
@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
def test_the_key_is_job_seed_never_the_mask_key(namespace: str | None) -> None:
    config = make_config(_cols(namespace))
    run = run_one(config, split(source([f"s{i}" for i in range(30)]), 7))
    got = column_values(run.out, "f")
    mask_key = vault_key()
    assert mask_key != job_seed_of(config)
    assert key_provider().mask_key() == mask_key
    under_mask_key = expected_values(range(30), config=config, namespace=namespace, key=mask_key)
    assert got == expected_values(range(30), config=config, namespace=namespace)
    assert got != under_mask_key


def test_the_default_selection_namespace_is_the_documented_literal_per_table() -> None:
    from decoy_engine.execution._strategies._faker_positional import faker_selection_namespace

    assert default_namespace("t", "f") == "faker-nd/1:t/1:f"
    assert faker_selection_namespace("t", "f", None) == default_namespace("t", "f")
    assert faker_selection_namespace("t", "f", "") == default_namespace("t", "f")
    assert faker_selection_namespace("u", "f", None) != faker_selection_namespace("t", "f", None)
    assert faker_selection_namespace("t", "f", "ns") == "ns"


@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
def test_the_resolver_carries_the_selection_namespace_of_the_table(namespace: str | None) -> None:
    from decoy_engine.execution.native._operator_params import (
        FakerParams,
        resolve_params_by_column,
    )
    from tests.native._chunked_faker_support import seed_of

    seed = seed_of(namespace)
    params = resolve_params_by_column({"f": seed}, {}, excluded=frozenset(), table="t")["f"]
    other = resolve_params_by_column({"f": seed}, {}, excluded=frozenset(), table="u")["f"]
    assert isinstance(params, FakerParams) and isinstance(other, FakerParams)
    assert params.positional is True
    assert params.namespace == namespace
    assert params.selection_namespace == (namespace or "faker-nd/1:t/1:f")
    assert other.selection_namespace == (namespace or "faker-nd/1:u/1:f")


@pytest.mark.parametrize("mode", ["unique", "match_source_cardinality", "scale_source_cardinality"])
def test_a_whole_column_mode_is_never_resolved_as_position_keyed(mode: str) -> None:
    from decoy_engine.execution.native._operator_params import (
        FakerParams,
        resolve_params_by_column,
    )
    from tests.native._chunked_faker_support import seed_of

    seed = seed_of("ns_f", mode=mode)
    params = resolve_params_by_column({"f": seed}, {}, excluded=frozenset(), table="t")["f"]
    assert params == FakerParams("ns_f")


def test_the_deterministic_resolver_path_is_unchanged() -> None:
    from decoy_engine.execution.native._operator_params import (
        FakerParams,
        resolve_params_by_column,
    )
    from tests.native._chunked_faker_support import seed_of

    seed = seed_of("ns_f", deterministic=True)
    params = resolve_params_by_column({"f": seed}, {}, excluded=frozenset(), table="t")["f"]
    assert params == FakerParams("ns_f")
    assert params.positional is False and params.selection_namespace is None


@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
def test_the_pool_built_natively_equals_the_oracles_pool(namespace: str | None) -> None:
    from decoy_engine.execution.native._chunk_masking import _resolve_faker_pools
    from decoy_engine.generation.pool import PoolCache
    from tests.native._chunked_faker_support import oracle_pool_for, seed_of

    seed = seed_of(namespace)
    job_seed = b"\x00\x00\x00\x00\x01\x35\x28\x89"
    native = _resolve_faker_pools({"f": seed}, job_seed=job_seed, pool_cache=PoolCache())["f"]
    oracle = oracle_pool_for(seed, job_seed)
    assert native.identity == oracle.identity
    assert list(native.values) == list(oracle.values)


# ---------------------------------------------------------------------------
# 6b. The type contract of plan section 3f, literally.
# ---------------------------------------------------------------------------

_TYPE_SHAPES: dict[str, list[str | None]] = {
    "with_values": ["a", None, "b", "c", None, "d", "e"],
    "all_null_beside_valued": [None] * 3 + ["a", "b", "c"] + [None] * 2,
}


@NEEDS_COMPANION
@pytest.mark.parametrize("shape", sorted(_TYPE_SHAPES))
def test_every_chunk_is_string_on_both_legs_and_assembled_equals_whole_frame(
    shape: str, tmp_path: Path
) -> None:
    table = source(_TYPE_SHAPES[shape])
    chunks = split(table, 3)
    native, forced = run_pair(_cols(None), chunks)
    assert_same_as_oracle(native, forced)
    assert {o.schema.field("f").type for o in native.out} == {pa.string()}
    assert {o.schema.field("f").type for o in forced.out} == {pa.string()}
    full = _full_frame(make_config(_cols(None)), table, tmp_path)
    assert full.schema.field("f").type == pa.string()
    assert column_values(native.out, "f") == full.column("f").to_pylist()


@NEEDS_COMPANION
def test_a_zero_row_chunk_is_string_on_both_legs() -> None:
    chunks = [source(["a", "b"]), source([])]
    native, forced = run_pair(_cols(None), chunks)
    assert_same_as_oracle(native, forced)
    assert [o.schema.field("f").type for o in native.out] == [pa.string(), pa.string()]
    assert [o.schema.field("f").type for o in forced.out] == [pa.string(), pa.string()]


@NEEDS_COMPANION
def test_the_documented_exception_whole_column_empty_or_all_null(tmp_path: Path) -> None:
    """Chunked is `string`; whole-frame resolves the type at assembly (pandas inference)."""
    config = make_config(_cols(None))
    (tmp_path / "n").mkdir()
    (tmp_path / "e").mkdir()
    for name, values in (("n", [None] * 5), ("e", [])):
        table = source(values)
        run = run_one(config, [table])
        assert {o.schema.field("f").type for o in run.out} == {pa.string()}
        full = _full_frame(config, table, tmp_path / name)
        assert full.schema.field("f").type != pa.string()


def test_the_streamed_sink_pins_the_same_type(tmp_path: Path) -> None:
    from tests.unit.execution import _auto_chunk_support as support
    from tests.unit.execution import _b6a_support as b6a

    values = [None] * (2 * support.CHUNK) + [f"v{i}" for i in range(16)]
    src = pa.table({"f": pa.array(values, pa.string()), "p": pa.array(range(len(values)))})
    path = support.write_source(src, tmp_path / "s.parquet")
    cfg = support.make_cfg(
        [{**nd_faker("f"), "provider": "person_first_name"}, support.pass_col("p")], path=path
    )
    spill = tmp_path / "spill"
    spill.mkdir()
    sink, _target = b6a.real_sink(spill)
    result = b6a.run_streamed(cfg, src, sink)
    block = result.quality_metrics["auto_chunk"]["output"]
    assert block["held_back_chunks"] == 0
    assert sink.schemas[support.TABLE].field("f").type == pa.string()
    expected = b6a.reference(cfg, src)[support.TABLE]
    assert sink.table(support.TABLE).column("f").to_pylist() == expected.column("f").to_pylist()


# ---------------------------------------------------------------------------
# 9. Evidence.
# ---------------------------------------------------------------------------


def _faker_evidence(run: Any) -> list[dict[str, Any]]:
    return [
        next(c for c in r.quality_metrics["chunked_route"]["columns"] if c["column"] == "f")
        for r in run.sink
    ]


@NEEDS_COMPANION
def test_a_run_reports_pool_select_per_non_empty_chunk() -> None:
    chunks = [source(["a", "b", "c"]), source([]), source([None, None]), source(["d"])]
    run = run_one(make_config(_cols(None)), chunks)
    ev = run.ev[0]
    assert ev.native_admitted is True
    assert ev.pool_select_executed is True
    assert ev.pool_select_calls == 3, "zero-row chunks are idle and uncounted"
    assert ev.node_routes[0].route == "native_pool"
    evidence = _faker_evidence(run)
    assert [e["planned_backend"] for e in evidence] == ["rust_companion"] * 4
    assert [e["executed_backend"] for e in evidence] == [
        "rust_companion",
        "arrow_python",
        "rust_companion",
        "rust_companion",
    ]


@NEEDS_COMPANION
def test_an_all_null_non_empty_chunk_ran_the_kernel() -> None:
    run = run_one(make_config(_cols(None)), [source([None] * 4)])
    assert run.ev[0].pool_select_calls == 1
    assert _faker_evidence(run)[0]["executed_backend"] == "rust_companion"


def test_companion_absent_downgrades_to_the_oracle_with_equal_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution.native import _dispatch
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError

    def _raise() -> Any:
        raise CryptoExtensionUnavailableError("index kernel unavailable for the test")

    chunks = split(source(["a", None, "b", None, None, "c", "d"]), 3)
    config = make_config(_cols(None))
    with monkeypatch.context() as patch:
        patch.setattr(_dispatch, "load_compiled_index_kernel", _raise)
        absent = run_one(config, chunks)
    assert absent.ev[0].native_admitted is False
    assert "index_extension_unavailable" in (absent.ev[0].reroute_reason or "")
    assert absent.ev[0].compiled_kernel_executed is False
    assert {o.schema.field("f").type for o in absent.out} == {pa.string()}
    assert column_values(absent.out, "f") == expected_chunked(chunks, config)


def expected_chunked(chunks: list[pa.Table], config: dict[str, Any]) -> list[Any]:
    values = column_values(chunks, "f")
    want = expected_values(range(len(values)), config=config, namespace=None)
    return [None if v is None else w for v, w in zip(values, want, strict=True)]
