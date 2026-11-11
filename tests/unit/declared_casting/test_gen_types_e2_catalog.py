"""GEN-TYPES E2: the `boolean/text/v1` descriptor, `round_to_scale` and `arrow_type_for_spec`.

Mutant named in the plan: `round_to_scale` rounding half up (the half-even vectors go red).
"""

from __future__ import annotations

import decimal
from decimal import Decimal

import pyarrow as pa
import pytest

from decoy_engine import declared_casting as dc

BOOL = {"id": "boolean/text/v1"}


def test_boolean_text_is_registered_in_both_directions_with_vectors() -> None:
    parse = dc.descriptor_for("boolean/text/v1", "parse")
    out = dc.descriptor_for("boolean/text/v1", "output")
    assert parse.logical == out.logical == "boolean"
    assert parse.inverse == out.inverse == "boolean/text/v1"
    assert parse.vectors and out.vectors


@pytest.mark.parametrize(
    ("raw", "want"),
    [("true", True), ("false", False), ("TRUE", True), ("False", False), ("1", True), ("0", False)],
)
def test_boolean_parse_accepts_true_false_one_zero_any_case(raw: str, want: bool) -> None:
    out = dc.parse_value(BOOL, raw)
    assert out.ok and out.value is want


@pytest.mark.parametrize("raw", ["", "yes", "t", "2", " true", "true ", None, 1, True])
def test_boolean_parse_refuses_everything_else(raw: object) -> None:
    assert not dc.parse_value(BOOL, raw).ok


def test_boolean_format_is_lowercase_text_and_refuses_non_bool() -> None:
    assert dc.format_value(BOOL, True).value == "true"
    assert dc.format_value(BOOL, False).value == "false"
    assert not dc.format_value(BOOL, 1).ok


# -- round_to_scale ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "scale", "want"),
    [
        ("12.345", 2, "12.34"),  # half even: 4 is even
        ("12.355", 2, "12.36"),  # half even: 5 is odd, rounds up
        ("0.5", 0, "0"),
        ("1.5", 0, "2"),
        ("2.5", 0, "2"),
        ("-2.5", 0, "-2"),
        ("1", 2, "1.00"),
        ("12.3", 2, "12.30"),
        ("7.004", 2, "7.00"),
    ],
)
def test_round_to_scale_rounds_half_even_on_the_decimal_value(
    text: str, scale: int, want: str
) -> None:
    out = dc.round_to_scale(Decimal(text), scale)
    assert out.ok
    assert str(out.value) == want


def test_round_to_scale_ignores_the_ambient_decimal_context() -> None:
    ctx = decimal.Context(prec=3, rounding=decimal.ROUND_UP, traps=[decimal.Inexact])
    with decimal.localcontext(ctx):
        out = dc.round_to_scale(Decimal("12345.675"), 2)
    assert out.ok and str(out.value) == "12345.68"  # half even on 7: rounds to 8 (odd up)
    with decimal.localcontext(ctx):
        out = dc.round_to_scale(Decimal("12345.665"), 2)
    assert out.ok and str(out.value) == "12345.66"


@pytest.mark.parametrize(
    "bad", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_round_to_scale_refuses_nan_and_infinity(bad: Decimal) -> None:
    assert not dc.round_to_scale(bad, 2).ok


def test_round_to_scale_refuses_a_value_too_large_to_quantize() -> None:
    assert not dc.round_to_scale(Decimal("1E+150"), 2).ok


def test_round_to_scale_refuses_a_non_decimal() -> None:
    assert not dc.round_to_scale(1.5, 2).ok  # type: ignore[arg-type]


# -- arrow_type_for_spec -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ({"id": "string/exact/v1"}, pa.string()),
        ({"id": "integer/ascii/v1", "signed": True, "bit_width": 64}, pa.int64()),
        ({"id": "integer/ascii/v1", "signed": False, "bit_width": 16}, pa.uint16()),
        ({"id": "decimal/plain/v1", "precision": 38, "scale": 2}, pa.decimal128(38, 2)),
        ({"id": "date/iso8601/v1"}, pa.date32()),
        (BOOL, pa.bool_()),
    ],
)
def test_arrow_type_for_spec(spec: dict[str, object], want: pa.DataType) -> None:
    assert dc.arrow_type_for_spec(dc.validate_format_spec(spec)) == want


def test_arrow_type_for_a_contract_is_the_same_mapping() -> None:
    from decoy_engine.contracts.declared_casting import build_contract

    for parse, logical in (
        ({"id": "integer/ascii/v1", "signed": True, "bit_width": 32}, "integer"),
        ({"id": "decimal/plain/v1", "precision": 9, "scale": 3}, "decimal"),
        ({"id": "date/iso8601/v1"}, "date"),
        ({"id": "string/exact/v1"}, "string"),
    ):
        model = build_contract({"logical_type": logical, "parse_format": parse})
        assert dc.arrow_type(model) == dc.arrow_type_for_spec(model.parse_format)


@pytest.mark.parametrize(
    ("kind", "scale", "arrow"),
    [
        ("int", None, pa.int64()),
        ("decimal", 0, pa.decimal128(38, 0)),
        ("decimal", 18, pa.decimal128(38, 18)),
        ("date", None, pa.date32()),
        ("bool", None, pa.bool_()),
    ],
)
def test_generated_value_spec_compiles_to_a_catalog_descriptor(
    kind: str, scale: int | None, arrow: pa.DataType
) -> None:
    spec = dc.generated_value_spec(kind, scale)
    assert dc.descriptor_for(spec["id"], "parse")
    assert dc.arrow_type_for_spec(dc.validate_format_spec(spec)) == arrow


def test_boolean_text_is_generation_only_and_absent_from_the_declared_surface() -> None:
    from decoy_engine.contracts.declared_casting import DeclaredCastContractError, build_contract

    assert not dc.descriptor_for("boolean/text/v1", "parse").declarable
    assert not dc.descriptor_for("boolean/text/v1", "output").declarable
    assert all(d.declarable for d in dc.descriptors() if d.format_id != "boolean/text/v1")
    assert "boolean/text/v1" not in {f["id"] for f in dc.capability_manifest()["formats"]}
    with pytest.raises(DeclaredCastContractError):
        build_contract({"logical_type": "boolean", "parse_format": BOOL})
