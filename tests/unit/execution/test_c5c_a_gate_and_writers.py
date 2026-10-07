"""`when:` gate positions and the earlier-writer rule for nullable-int Faker columns."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.relationships._namespace import NamespaceBinding, NamespaceRegistry
from tests.unit.execution import _c5c_a_support as sup

pytestmark = pytest.mark.filterwarnings("ignore")

_INT = sup.int_registry()


def _plan(when: str | None, provider: str = "address_zip") -> Any:
    return sup.plan_of(
        {
            "n": sup.faker_seed(provider=provider, when=when),
            "f": sup.seed_of("passthrough"),
        }
    )


def _expected_map(
    values: list[int | None], *, registry: Any, provider: str = "address_zip"
) -> dict[int, Any]:
    keys = sorted({v for v in values if v is not None})
    clean = sup.run(
        _plan(None, provider),
        pa.table({"n": pa.array(keys, type=pa.int64()), "f": pa.array([0] * len(keys))}),
        registry=registry,
    )
    return dict(zip(keys, sup.column(clean), strict=True))


_VALUES: list[int | None] = [3, None, 5, 2**53 + 1, None, 7, 5]


def _table_with_exact_ints(
    flags: list[Any], values: list[int | None] = _VALUES, index: Any = None
) -> pa.Table:
    """int64 with nulls in Arrow; an optional non-default index rides in the metadata."""
    df = pd.DataFrame({"f": flags})
    if index is not None:
        df.index = index
    table = pa.Table.from_pandas(df, preserve_index=index is not None)
    return table.append_column("n", pa.array(values, type=pa.int64()))


def _check_numeric(
    flags: list[int], *, index: Any = None, row_offset: int = 0, registry: Any = _INT
) -> None:
    table = _table_with_exact_ints(flags, index=index)
    out = sup.column(sup.run(_plan("f == 1"), table, registry=registry, row_offset=row_offset))
    expected = _expected_map(_VALUES, registry=registry)
    for i, (src, flag, got) in enumerate(zip(_VALUES, flags, out, strict=True)):
        if src is None:
            assert got is None or pd.isna(got), i
        elif flag == 1:
            assert got == expected[src], i
        else:
            assert got == float(src), i


@pytest.mark.parametrize(
    "flags",
    [[1, 0, 1, 0, 0, 1, 0], [1, 1, 1, 1, 1, 1, 1], [0, 1, 0, 1, 1, 0, 1]],
    ids=["some", "all", "nulls_and_big_selected"],
)
def test_numeric_output_under_a_gate_maps_selected_rows_only(flags: list[int]) -> None:
    _check_numeric(flags)


def test_non_default_index_is_positional() -> None:
    _check_numeric([1, 0, 1, 0, 1, 1, 0], index=pd.Index(list("gfedcba")))


def test_integer_index_that_is_not_a_range() -> None:
    _check_numeric([0, 1, 1, 0, 0, 1, 1], index=pd.Index([50, 40, 30, 20, 10, 5, 1]))


def test_duplicate_index_is_positional() -> None:
    _check_numeric([1, 0, 1, 0, 0, 1, 0], index=pd.Index([0, 0, 1, 1, 2, 2, 2]))


def test_nonzero_chunk_offset_does_not_shift_positions() -> None:
    _check_numeric([1, 0, 1, 0, 0, 1, 0], row_offset=1_000_000)


def test_nullable_boolean_predicate_with_na_selects_like_loc() -> None:
    # `flag == 1` over a pandas nullable Int64 yields <NA> for the null flag rows.
    flags = pd.array([1, None, 1, 0, None, 1, 0], dtype="Int64")
    df = pd.DataFrame({"f": flags})
    table = pa.Table.from_pandas(df, preserve_index=False).append_column(
        "n", pa.array(_VALUES, type=pa.int64())
    )
    out = sup.column(sup.run(_plan("f == 1"), table, registry=_INT))
    expected = _expected_map(_VALUES, registry=_INT)
    selected = [0, 2, 5]
    for i, (src, got) in enumerate(zip(_VALUES, out, strict=True)):
        if src is None:
            assert got is None or pd.isna(got)
        elif i in selected:
            assert got == expected[src]
        else:
            assert got == float(src)


# 3b. String output ----------------------------------------------------------------


def test_string_output_partial_gate_keeps_the_mixed_type_rejection() -> None:
    table = _table_with_exact_ints([1, 0, 1, 0, 0, 1, 0])
    with pytest.raises(pa.ArrowTypeError):
        sup.run(_plan("f == 1", "person_email"), table)


def test_string_output_gate_selecting_every_non_null_value_succeeds() -> None:
    flags = [1, 0, 1, 1, 0, 1, 1]  # the null rows (1 and 4) are the excluded ones
    out = sup.column(sup.run(_plan("f == 1", "person_email"), _table_with_exact_ints(flags)))
    assert out[1] is None and out[4] is None
    assert all(isinstance(out[i], str) for i in (0, 2, 3, 5, 6))
    clean = _expected_map(_VALUES, registry=sup.REG, provider="person_email")
    assert [out[i] for i in (0, 2, 3, 5, 6)] == [clean[_VALUES[i]] for i in (0, 2, 3, 5, 6)]


def test_string_output_all_rows_gate_succeeds() -> None:
    out = sup.column(
        sup.run(_plan("f == 1", "person_email"), _table_with_exact_ints([1] * len(_VALUES)))
    )
    assert out[1] is None and isinstance(out[0], str)


def test_zero_row_gate_never_runs_faker() -> None:
    out = sup.column(
        sup.run(_plan("f == 99", "person_email"), _table_with_exact_ints([1] * len(_VALUES)))
    )
    assert out[0] == 3.0 and out[1] is None


# 3c. Unrelated strategy under an NA predicate -------------------------------------


def test_redact_under_a_nullable_boolean_na_predicate_succeeds() -> None:
    flags = pd.array([1, None, 1, 0], dtype="Int64")
    table = pa.Table.from_pandas(
        pd.DataFrame({"f": flags, "s": ["a", "b", "c", "d"]}), preserve_index=False
    )
    plan = sup.plan_of({"s": sup.seed_of("redact", when="f == 1"), "f": sup.seed_of("passthrough")})
    out = sup.run(plan, table).outputs["t"].column("s").to_pylist()
    assert out[0] != "a" and out[2] != "c"
    assert out[1] == "b" and out[3] == "d"


# 5. Earlier writer ----------------------------------------------------------------


def _composite(columns: tuple[str, ...], bundle: list[dict[str, str]]) -> Any:
    return sup.ColumnSeed(
        namespace=None,
        strategy="composite",
        provider="composite_custom",
        backend_type="composite",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(("bundle", bundle),),
        coherent_with=columns,
    )


def _composite_ns(columns: tuple[str, ...]) -> NamespaceRegistry:
    return NamespaceRegistry(
        bindings=(NamespaceBinding(namespace="comp_ns", declared_by=(("t", columns),)),)
    )


def composite_before_faker_job(n_values: list[int | None]) -> Any:
    """Composite ("a","n") sorts before the scalar ("n",): Faker reads the composite's strings."""
    plan = sup.plan_of(
        {
            "a": _composite(
                ("n",),
                [
                    {"column": "a", "provider": "person_first_name"},
                    {"column": "n", "provider": "person_last_name"},
                ],
            ),
            "n": sup.faker_seed(provider="person_last_name"),
        }
    )
    table = pa.table(
        {"a": pa.array(["x"] * len(n_values)), "n": pa.array(n_values, type=pa.int64())}
    )
    return sup.run(plan, table, namespaces=_composite_ns(("a", "n")))


def faker_before_composite_job(n_values: list[int | None]) -> Any:
    """Scalar ("n",) sorts before the composite ("n","z"), which overwrites n afterwards."""
    plan = sup.plan_of(
        {
            "z": _composite(
                ("n",),
                [
                    {"column": "n", "provider": "person_last_name"},
                    {"column": "z", "provider": "person_first_name"},
                ],
            ),
            "n": sup.faker_seed(provider="person_last_name"),
        }
    )
    table = pa.table(
        {"z": pa.array(["x"] * len(n_values)), "n": pa.array(n_values, type=pa.int64())}
    )
    return sup.run(plan, table, namespaces=_composite_ns(("n", "z")))


def test_a_column_written_by_an_earlier_composite_is_not_keyed_from_the_source_ints() -> None:
    values: list[int | None] = [1, None, 3]
    got = sup.column(composite_before_faker_job(values))
    exact_keyed = sup.column(
        sup.run(
            sup.plan_of({"n": sup.faker_seed(provider="person_last_name")}),
            pa.table({"n": pa.array(values, type=pa.int64())}),
        )
    )
    assert got != exact_keyed
    assert all(isinstance(v, str) for v in got)


def test_a_node_that_runs_before_the_composite_is_exact_and_the_composite_wins() -> None:
    nullable = faker_before_composite_job([1, None, 3])
    clean = faker_before_composite_job([1, 2, 3])
    assert sup.column(nullable) == sup.column(clean)
    assert sup.column(nullable, "z") == sup.column(clean, "z")


def test_float_source_under_an_earlier_writer_still_raises_like_main() -> None:
    table = pa.table({"a": pa.array(["x", "y"]), "n": pa.array([1.5, None], type=pa.float64())})
    plan = sup.plan_of({"a": sup.seed_of("passthrough"), "n": sup.faker_seed()})
    with pytest.raises(GenerationError) as exc:
        sup.run(plan, table)
    assert exc.value.code == "float_canonicalization_unsupported"
