"""GEN-TYPES E2 review fixes: streamed typing, unique refusal, table agreement, date text, bounds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.parity._fr0_support import run_streamed
from tests.unit.generation.kinds._support import cfg, generate, one, table

SENTINEL_FREE = "output_type_cast_failed"


def _col(name: str, kind: str, output: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": kind, "output_type": output, **extra}


def _padded(col: dict[str, Any]) -> dict[str, Any]:
    """The same column on the whole-table route (a formula sibling declines the stream core)."""
    return cfg(
        [table("t", 5, [col, {"name": "zz", "type": "formula", "formula": "1"}])],
    )


# H1: the generated type, not the emitted type, decides how provider text is parsed. -----------


@pytest.mark.parametrize("provider", ["date_time", "pydecimal", "boolean"])
@pytest.mark.parametrize("route", ["stream-core", "sink", "resident"])
def test_a_faker_int_output_over_non_integer_text_fails_coded_without_the_value(
    provider: str, route: str, tmp_path: Path
) -> None:
    from decoy_engine.execution import ExecutionError

    col = _col("x", "faker", "int", faker_type=provider)
    config = _padded(col) if route == "resident" else one(col, rows=5)
    with pytest.raises(ExecutionError) as info:
        if route == "sink":
            run_streamed(config, tmp_path)
        else:
            generate(config)
    err = info.value
    assert err.code == SENTINEL_FREE, (provider, route, err)
    text = str(err)
    assert "'t'" in text and "'x'" in text and "int" in text
    assert err.__cause__ is None and err.__context__ is None
    # No digit run of a generated value: the message holds only names, the type and a reason code.
    assert "20" not in text.replace("output_type", "")


def test_a_streamed_sequence_keeps_int_values_before_a_decimal_cast(monkeypatch: Any) -> None:
    from decoy_engine.generation import _streamed as site

    seen: list[Any] = []
    real = site.cast_output_values

    def spy(values: Any, *a: Any, **k: Any) -> Any:
        seen.extend(values)
        return real(values, *a, **k)

    monkeypatch.setattr(site, "cast_output_values", spy)
    out = generate(one(_col("x", "sequence", "decimal(2)", start=1), rows=4))["t"]
    assert seen == [1, 2, 3, 4]
    assert all(type(v) is int for v in seen)
    assert [str(v) for v in out.column("x").to_pylist()] == ["1.00", "2.00", "3.00", "4.00"]


# H2: a lossy cast on a unique column would merge values the uniqueness check saw as distinct. ---


def _config_problems(col: dict[str, Any]) -> list[tuple[Any, str]]:
    from decoy_engine.config._generate_unique import column_conflict_problems

    extras = {k: v for k, v in col.items() if k not in ("name", "type")}
    return column_conflict_problems(col["type"], extras)


@pytest.mark.parametrize(
    "col",
    [
        _col("x", "categorical", "int", categories=["1", "01"], unique=True),
        _col("x", "categorical", "bool", categories=["true", "TRUE"], unique=True),
        _col("x", "categorical", "decimal(0)", categories=[1.2, 1.4], unique=True),
        _col("x", "faker", "int", faker_type="pyint", unique=True),
        _col("x", "faker", "date", faker_type="date_time", unique=True),
    ],
    ids=["cat-int", "cat-bool", "cat-decimal", "faker-int", "faker-date"],
)
def test_unique_with_an_output_type_is_a_config_conflict(col: dict[str, Any]) -> None:
    assert _config_problems(col) == [(("output_type",), "generate_unique_conflict")]


@pytest.mark.parametrize(
    "col",
    [
        _col("x", "sequence", "int", start=1, unique=True),
        _col("x", "sequence", "decimal(2)", start=1, step=3, unique=True),
        {"name": "x", "type": "categorical", "categories": ["a", "b"], "unique": True},
        _col("x", "categorical", "int", categories=["1", "01"]),
    ],
    ids=["seq-int", "seq-decimal", "no-output-type", "output-type-not-unique"],
)
def test_unique_stays_allowed_where_the_cast_cannot_merge_values(col: dict[str, Any]) -> None:
    assert _config_problems(col) == []


def test_a_unique_categorical_with_a_lossy_output_type_is_refused_at_validation() -> None:
    from pydantic import ValidationError

    from decoy_engine.config._tables import GenerateColumnConfig

    col = _col("x", "categorical", "int", categories=["1", "01"], unique=True)
    with pytest.raises(ValidationError) as info:
        GenerateColumnConfig(**col)
    assert "generate_unique_conflict" in str(info.value)


# M1: one table says which types take `output_type`. ------------------------------------------


def test_the_option_registry_and_the_kind_table_name_the_same_types() -> None:
    from decoy_engine.config._generate_options import GENERATE_OPTIONS
    from decoy_engine.config._generate_type_checks import OUTPUT_TYPE_KINDS

    with_option = {t for t, o in GENERATE_OPTIONS.items() if "output_type" in o.keys}
    assert with_option == set(OUTPUT_TYPE_KINDS)


# M3: a faker datetime is text before the cast; `date` takes its date part. -------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("2002-12-11 17:38:36.058740", "2002-12-11"),
        ("2002-12-11T17:38:36", "2002-12-11"),
        ("2002-12-11T17:38:36Z", "2002-12-11"),
        ("2002-12-11T17:38:36+02:00", "2002-12-11"),
        ("2002-12-11", "2002-12-11"),
        ("not a date", "not a date"),
        ("2002-12-11 junk", "2002-12-11 junk"),
        ("2002-12-11T23:59:59-23:59", "2002-12-11"),
        ("2002-12-11T24:00:00", "2002-12-11T24:00:00"),
        ("2002-12-11T12:60:00", "2002-12-11T12:60:00"),
        ("2002-12-11T12:00:60", "2002-12-11T12:00:60"),
        ("2002-12-11T12:00:00+24:00", "2002-12-11T12:00:00+24:00"),
        ("2002-12-11T12:00:00+02:60", "2002-12-11T12:00:00+02:60"),
        ("2002-12-11T99:99:99+99:99", "2002-12-11T99:99:99+99:99"),
    ],
)
def test_date_text_takes_the_date_part_of_iso_datetime_text(text: str, want: str) -> None:
    from decoy_engine.generation.kinds._output_type import _date_text

    assert _date_text(text) == want


def test_an_out_of_range_datetime_fails_the_date_cast_without_the_value() -> None:
    from decoy_engine.execution import ExecutionError

    bad = "2002-12-11T99:99:99+99:99"
    col = _col("x", "constant", "date", value=bad)
    with pytest.raises(ExecutionError) as exc:
        generate(one(col, rows=2))
    assert exc.value.code == SENTINEL_FREE
    assert bad not in str(exc.value) and "99" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


@pytest.mark.parametrize("provider", ["date_time", "iso8601"])
def test_a_faker_datetime_provider_can_be_a_date_column(provider: str) -> None:
    import datetime

    col = _col("x", "faker", "date", faker_type=provider)
    for config in (one(col, rows=5), _padded(col)):
        got = generate(config)["t"].column("x").to_pylist()
        assert len(got) == 5 and all(type(v) is datetime.date for v in got)


# M4: the declared type decides pair agreement, and a typed value is priced as an object. -------


def _pair_config(parent_out: str | None, child_out: str | None) -> dict[str, Any]:
    def seq(name: str, out: str | None) -> dict[str, Any]:
        return {"name": name, "type": "sequence", "start": 1, "step": 1} | (
            {"output_type": out} if out else {}
        )

    config = cfg(
        [table("people", 6, [seq("id", parent_out)]), table("accounts", 6, [seq("pid", child_out)])]
    )
    config["relationships"] = [
        {
            "parent": {"table": "people", "columns": ["id"]},
            "children": [{"table": "accounts", "columns": ["pid"]}],
            "orphan_policy": "preserve",
            "namespace": "ns",
        }
    ]
    return config


def test_a_relationship_pair_with_different_emitted_types_is_not_numeric() -> None:
    from decoy_engine.generation._numeric_typing import numeric_plan

    plan = numeric_plan(_pair_config("decimal(2)", None))
    assert ("people", "id") not in plan and ("accounts", "pid") not in plan


@pytest.mark.parametrize("out", [None, "decimal(2)"])
def test_a_relationship_pair_with_the_same_emitted_type_stays_numeric(out: str | None) -> None:
    from decoy_engine.generation._numeric_typing import numeric_plan

    plan = numeric_plan(_pair_config(out, out))
    assert ("people", "id") in plan and ("accounts", "pid") in plan


def test_output_payload_bound_prices_a_typed_value_at_least_64_bytes() -> None:
    from decoy_engine.generation.kinds import output_payload_bound

    assert output_payload_bound(None) is None
    assert output_payload_bound(8) == 64
    assert output_payload_bound(64) == 64
    assert output_payload_bound(200) == 200


@pytest.mark.serial
def test_compute_payload_bounds_applies_it_only_to_a_column_with_an_output_type() -> None:
    from decoy_engine.generation._bounds import compute_payload_bounds

    plain = {"name": "x", "type": "constant", "value": 5}
    typed = plain | {"output_type": "int"}
    open_ended = {"name": "y", "type": "faker", "faker_type": "text", "output_type": "int"}
    long_text = {
        "name": "z",
        "type": "categorical",
        "categories": ["a" * 200],
        "output_type": "bool",
    }
    bounds = compute_payload_bounds(
        one_table([plain, typed | {"name": "w"}, open_ended, long_text])
    )
    assert bounds[("t", "x")] < 64
    assert bounds[("t", "w")] == 64
    assert bounds[("t", "z")] == 200


def one_table(cols: list[dict[str, Any]]) -> dict[str, Any]:
    return cfg([table("t", 4, cols)])
