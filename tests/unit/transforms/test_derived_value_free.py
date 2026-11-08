"""T5: a per-row derived failure is value-free on every exception surface.

Plan `docs/plans/2026-10-31-slice0-derived-visibility.md` P5: the text carries only the
output column, the row index and a reason word from a closed table; nothing else rides in
`str`, `args` or `__notes__`, and no exception is chained (`__cause__` and `__context__`).
Markers in str, int, list and dict shapes, in mask mode (through the adapter, which wraps
the per-row error as `ExecutionError(derived_evaluation_failed)`) and in generation mode.
Labels per cell: the bad-date marker cells, every exact-text assertion and every
no-chain assertion are FAIL-BEFORE; marker absence in the division, type-mismatch and
missing-reference cells is CHARACTERIZATION (main's text carries no value there).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

STR_MARKER = "CANARY-7F3A-SECRET"
INT_MARKER = 918273645
LIST_MARKER = ["CANARY-LIST-51C2"]
DICT_MARKER = {"k": "CANARY-DICT-88E0"}
_MARKER_TEXTS = (STR_MARKER, str(INT_MARKER), "CANARY-LIST-51C2", "CANARY-DICT-88E0", "CANARY")


class _OutsideTheTableError(Exception):
    """An exception type absent from the reason table; its message carries a marker."""


# (id, expression, a-value, b-value, expected reason word)
_CASES = [
    ("bad-date-str", 'days_between(a, "2020-01-02")', STR_MARKER, 1, "an operand value is invalid"),
    ("bad-date-int", 'days_between(a, "2020-01-02")', INT_MARKER, 1, "an operand value is invalid"),
    (
        "bad-date-list",
        'days_between(a, "2020-01-02")',
        LIST_MARKER,
        1,
        "an operand value is invalid",
    ),
    (
        "bad-date-dict",
        'days_between(a, "2020-01-02")',
        DICT_MARKER,
        1,
        "an operand value is invalid",
    ),
    ("division-int", "a / b", INT_MARKER, 0, "division by zero"),
    ("type-mismatch-str", "a + 1", STR_MARKER, 1, "an operand has an unsupported type"),
    ("type-mismatch-list", "a + 1", LIST_MARKER, 1, "an operand has an unsupported type"),
    ("missing-reference", "a + gone", STR_MARKER, 1, "a referenced column is missing"),
]


def _text(column: str, row: int, word: str) -> str:
    return f"derived expression evaluation failed at column {column!r}, row {row}: {word}"


def _assert_value_free(exc: BaseException, expected: str) -> None:
    assert str(exc) == expected
    assert exc.args == (expected,)
    surfaces = [str(exc), *map(str, exc.args), *map(str, getattr(exc, "__notes__", []))]
    for marker in _MARKER_TEXTS:
        assert all(marker not in s for s in surfaces), marker
    assert "gone" not in str(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def _config(expression: str) -> Any:
    from decoy_engine.transforms.derived import DerivedConfig

    return DerivedConfig.from_dict({"expression": expression})


@pytest.mark.parametrize(
    ("expression", "a", "b", "word"), [c[1:] for c in _CASES], ids=[c[0] for c in _CASES]
)
def test_apply_derived_failure_is_value_free(expression: str, a: Any, b: Any, word: str) -> None:
    from decoy_engine.transforms.derived import apply_derived

    with pytest.raises(ValueError) as exc:
        apply_derived(_config(expression), {"a": a, "b": b}, column="d", row_index=3)
    _assert_value_free(exc.value, _text("d", 3, word))


@pytest.mark.parametrize(
    ("expression", "a", "b", "word"), [c[1:] for c in _CASES], ids=[c[0] for c in _CASES]
)
def test_generation_failure_is_value_free(expression: str, a: Any, b: Any, word: str) -> None:
    from decoy_engine.generation.synthesize import _derived_generate

    generated = {"a": [a], "b": [b]}
    with pytest.raises(ValueError) as exc:
        _derived_generate({"name": "d", "expression": expression}, 1, generated)
    _assert_value_free(exc.value, _text("d", 0, word))


def _mask_cases() -> list[Any]:
    # Arrow needs one type per column: the list and dict shapes are generation-only.
    return [pytest.param(*c[1:], id=c[0]) for c in _CASES if not isinstance(c[2], (list, dict))]


@pytest.mark.parametrize(("expression", "a", "b", "word"), _mask_cases())
def test_mask_failure_is_coded_and_value_free(expression: str, a: Any, b: Any, word: str) -> None:
    from decoy_engine.execution import PandasExecutionAdapter
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._graph import RelationshipGraph
    from decoy_engine.relationships._namespace import NamespaceRegistry

    def seed(strategy: str, config: tuple[tuple[str, Any], ...] = ()) -> ColumnSeed:
        return ColumnSeed(
            namespace=None,
            strategy=strategy,
            provider="x",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=config,
            coherent_with=(),
        )

    table = pa.table({"a": [a, a], "b": [b, b], "d": [0.0, 0.0]})
    per_column = (
        ("a", seed("passthrough")),
        ("b", seed("passthrough")),
        ("d", seed("derived", (("expression", expression),))),
    )
    plan: Any = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x01" * 8, per_table=(("t", TableSeed(per_column=per_column, per_group=())),)
        )
    )
    with pytest.raises(Exception) as exc:
        PandasExecutionAdapter().run_single(
            plan,
            table,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
        )
    if word == "a referenced column is missing":
        # The mask path refuses a missing reference once, before any row (T8).
        assert type(exc.value) is ExecutionError and exc.value.code == "derived_ref_missing"
        return
    assert type(exc.value) is ExecutionError
    assert exc.value.code == "derived_evaluation_failed"
    expected = _text("d", 0, word)
    assert exc.value.message == expected
    surfaces = [
        str(exc.value),
        *map(str, exc.value.args),
        *map(str, getattr(exc.value, "__notes__", [])),
    ]
    for marker in _MARKER_TEXTS:
        assert all(marker not in s for s in surfaces), marker
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_exception_outside_the_table_is_evaluation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.transforms import derived

    def boom(compiled: Any, ctx: Any) -> Any:
        raise _OutsideTheTableError(f"value {STR_MARKER}")

    monkeypatch.setattr(derived, "evaluate", boom)
    with pytest.raises(ValueError) as exc:
        derived.apply_derived(_config("a + 1"), {"a": 1}, column="d", row_index=0)
    _assert_value_free(exc.value, _text("d", 0, "the expression could not be evaluated"))
    assert "_OutsideTheTable" not in str(exc.value)


_LUCKY = [c for c in _CASES if c[0].startswith(("division", "type-mismatch", "missing"))]


@pytest.mark.parametrize(
    ("expression", "a", "b"), [c[1:4] for c in _LUCKY], ids=[c[0] for c in _LUCKY]
)
def test_marker_absent_where_main_was_already_value_free(expression: str, a: Any, b: Any) -> None:
    """CHARACTERIZATION: these exception types never carried the cell value; kept apart from
    the exact-text cells so they are never counted as red evidence."""
    from decoy_engine.transforms.derived import apply_derived

    with pytest.raises(ValueError) as exc:
        apply_derived(_config(expression), {"a": a, "b": b}, column="d", row_index=0)
    surfaces = [str(exc.value), *map(str, exc.value.args)]
    for marker in _MARKER_TEXTS:
        assert all(marker not in s for s in surfaces), marker
