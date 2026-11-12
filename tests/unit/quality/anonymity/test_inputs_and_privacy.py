"""Inputs, null policy, value identity, value-free reports and errors."""

from __future__ import annotations

import ast
import dataclasses
import datetime
import sys
import typing
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import decoy_engine.quality.anonymity as anonymity
from decoy_engine.quality.anonymity import (
    KAnonymityReport,
    measure_k_anonymity,
    measure_l_diversity,
    measure_t_closeness_equal,
    measure_t_closeness_ordered,
)

SENTINEL = "ZZSENTINEL"
SENTINEL_NUMBER = 987654321


def _k(table: Any, qi: list[str], sensitive: str) -> Any:
    return measure_k_anonymity(table, qi)


MEASURES: list[Callable[..., Any]] = [
    _k,
    measure_l_diversity,
    measure_t_closeness_equal,
    measure_t_closeness_ordered,
]


# ---------------------------------------------------------------- null policy


@pytest.mark.parametrize("measure", MEASURES)
def test_null_quasi_identifier_is_its_own_value(measure: Callable[..., Any]) -> None:
    frame = pd.DataFrame({"q": ["a", "a", None], "s": [1.0, 2.0, 3.0]})
    report = measure(frame, ["q"], "s")
    assert report.row_count == 3
    assert report.class_count == 2
    k = measure_k_anonymity(frame, ["q"])
    assert k.k_min == 1
    assert k.rows_with_null_quasi_identifier == 1


def test_all_null_spellings_are_one_value() -> None:
    frame = pd.DataFrame({"q": pd.Series([None, np.nan, pd.NA, pd.NaT, "a"], dtype=object)})
    report = measure_k_anonymity(frame, ["q"])
    assert report.class_size_distribution == ((1, 1), (4, 1))
    arrow = pa.table({"q": pa.array([None, None, "a"])})
    assert measure_k_anonymity(arrow, ["q"]).class_size_distribution == ((1, 1), (2, 1))


def test_null_sensitive_is_excluded_for_t() -> None:
    frame = pd.DataFrame({"q": ["a", "a", "b", "b"], "s": [1.0, None, 1.0, 1.0]})
    for measure in (measure_t_closeness_equal, measure_t_closeness_ordered):
        report = measure(frame, ["q"], "s")
        assert report.rows_with_null_sensitive == 1
        assert report.distinct_sensitive_values == 1
        assert report.max_emd == 0.0


# ------------------------------------------------------------- value identity


def test_arrow_int64_with_nulls_is_lossless() -> None:
    big = 2**53
    table = pa.table({"q": pa.array([big, big + 1, None], pa.int64()), "s": pa.array([1, 2, 3])})
    assert measure_k_anonymity(table, ["q"]).class_count == 3
    as_sensitive = pa.table({"q": ["x", "x", "x"], "s": pa.array([big, big + 1, None], pa.int64())})
    assert measure_l_diversity(as_sensitive, ["q"], "s").distinct_l_min == 2


def test_object_quasi_identifier_is_family_aware() -> None:
    d = datetime.datetime(2020, 1, 1)
    cells = [
        1,
        np.int64(1),
        1.0,
        np.float64(1.0),
        True,
        np.bool_(True),
        "a",
        np.str_("a"),
        pd.Timestamp(d),
        d,
    ]
    frame = pd.DataFrame({"q": pd.Series(cells, dtype=object)})
    report = measure_k_anonymity(frame, ["q"])
    assert report.class_size_distribution == ((2, 5),)
    split = pd.DataFrame({"q": pd.Series([1, 1.0, True, "1"], dtype=object)})
    assert measure_k_anonymity(split, ["q"]).class_count == 4


def test_sensitive_uses_plain_equality() -> None:
    frame = pd.DataFrame({"q": ["x", "x", "x"], "s": pd.Series([1, 1.0, True], dtype=object)})
    report = measure_l_diversity(frame, ["q"], "s")
    assert report.distinct_l_min == 1
    # t keys sensitive values by family instead (see test_t_keys_sensitive_values_by_family).
    assert measure_t_closeness_equal(frame, ["q"], "s").distinct_sensitive_values == 3


def test_native_arrow_nan_is_null() -> None:
    """Built directly, not via from_pandas (which turns NaN into null)."""
    floats = pa.array([1.0, float("nan"), None], pa.float64())
    assert floats.null_count == 1
    table = pa.table({"q": floats, "s": pa.array([1.0, 2.0, 3.0])})
    k = measure_k_anonymity(table, ["q"])
    assert k.class_size_distribution == ((1, 1), (2, 1))
    assert k.rows_with_null_quasi_identifier == 2
    sens = pa.table({"q": pa.array(["x", "x", "x"]), "s": floats})
    l_report = measure_l_diversity(sens, ["q"], "s")
    assert l_report.distinct_l_min == 1
    assert l_report.rows_with_null_sensitive == 2
    for measure in (measure_t_closeness_equal, measure_t_closeness_ordered):
        t = measure(sens, ["q"], "s")
        assert t.distinct_sensitive_values == 1
        assert t.rows_with_null_sensitive == 2


def test_t_keys_sensitive_values_by_family() -> None:
    """1 and 1.0 are two sensitive values for t (one class each): total
    variation 1/2. Merging them would report 0."""
    frame = pd.DataFrame(
        {"q": ["a", "a", "b", "b"], "s": pd.Series([1, 1, 1.0, 1.0], dtype=object)}
    )
    report = measure_t_closeness_equal(frame, ["q"], "s")
    assert report.distinct_sensitive_values == 2
    assert report.max_emd == pytest.approx(0.5, abs=1e-12)
    assert measure_l_diversity(frame, ["q"], "s").distinct_l_min == 1


def test_datetime_family_spans_numpy_and_pandas() -> None:
    d = datetime.datetime(2020, 1, 1)
    cells = [pd.Timestamp(d), np.datetime64("2020-01-01"), d]
    frame = pd.DataFrame({"q": pd.Series(cells, dtype=object)})
    assert measure_k_anonymity(frame, ["q"]).class_size_distribution == ((3, 1),)


def test_date_and_datetime_are_two_families() -> None:
    """Documents behavior; not a guard. pandas infers "date" for this mix and
    the single-family shortcut leaves "date" out, but a date never equals a
    datetime, so the two stay apart on either path."""
    cells = [datetime.date(2020, 1, 1), datetime.datetime(2020, 1, 1)]
    frame = pd.DataFrame({"q": pd.Series(cells, dtype=object)})
    assert measure_k_anonymity(frame, ["q"]).class_count == 2


def test_arrow_dictionary_with_null_or_nan_entries() -> None:
    import pyarrow.compute as pc

    encoded = pc.dictionary_encode(pa.array(["x", None, "x"]), null_encoding="encode")
    assert encoded.null_count == 0
    k = measure_k_anonymity(pa.table({"q": encoded}), ["q"])
    assert k.class_size_distribution == ((1, 1), (2, 1))
    assert k.rows_with_null_quasi_identifier == 1
    nan_dict = pa.DictionaryArray.from_arrays(pa.array([0, 1, 1]), pa.array([1.0, float("nan")]))
    k = measure_k_anonymity(pa.table({"q": nan_dict}), ["q"])
    assert k.class_size_distribution == ((1, 1), (2, 1))
    assert k.rows_with_null_quasi_identifier == 2
    ordered = pa.DictionaryArray.from_arrays(
        pa.array([0, 1, 2, 0]), pa.array(["lo", None, "hi"]), ordered=True
    )
    table = pa.table({"q": ["x", "x", "y", "y"], "s": ordered})
    report = measure_t_closeness_ordered(table, ["q"], "s")
    assert report.rows_with_null_sensitive == 1
    assert report.distinct_sensitive_values == 2
    # x {lo}, y {hi, lo}; Q = (2/3, 1/3); x: cumulative 1 - 2/3 = 1/3 -> 1/3.
    assert report.max_emd == pytest.approx(1 / 3, abs=1e-12)


def test_timedelta_is_its_own_family() -> None:
    """np.timedelta64 subclasses np.integer and hashes equal to an int of the
    same count; it must not join the int family. All timedelta spellings of one
    duration are one value, on the fast path and the per-cell path alike."""
    mixed = [np.timedelta64(1, "ns"), 1]
    frame = pd.DataFrame({"q": pd.Series(mixed, dtype=object)})
    assert measure_k_anonymity(frame, ["q"]).class_count == 2
    spellings = [datetime.timedelta(days=1), np.timedelta64(1, "D"), pd.Timedelta(days=1)]
    fast = pd.DataFrame({"q": pd.Series(spellings, dtype=object)})
    assert measure_k_anonymity(fast, ["q"]).class_size_distribution == ((3, 1),)
    slow = pd.DataFrame({"q": pd.Series([*spellings, 5], dtype=object)})
    assert measure_k_anonymity(slow, ["q"]).class_size_distribution == ((1, 1), (3, 1))


def _ordered_chunks(*parts: tuple[list[int], list[float]]) -> pa.ChunkedArray:
    return pa.chunked_array(
        [
            pa.DictionaryArray.from_arrays(
                pa.array(indices, pa.int32()), pa.array(values), ordered=True
            )
            for indices, values in parts
        ]
    )


def test_chunked_ordered_dictionary_with_conflicting_order_is_refused() -> None:
    """Chunk dictionaries [3, 4] and [1, 3] define no common order (pandas would
    merge them as [3, 4, 1] and keep ordered=True)."""
    column = _ordered_chunks(([0, 1], [3.0, 4.0]), ([0, 1], [1.0, 3.0]))
    table = pa.table({"q": ["x", "x", "y", "y"], "s": column})
    with pytest.raises(TypeError, match="'s'") as info:
        measure_t_closeness_ordered(table, ["q"], "s")
    assert info.value.__cause__ is None and info.value.__context__ is None
    # Same entries, opposite order: also no common order.
    swapped = _ordered_chunks(([0, 1], [3.0, 4.0]), ([0, 1], [4.0, 3.0]))
    with pytest.raises(TypeError, match="'s'"):
        measure_t_closeness_ordered(pa.table({"q": ["x", "x", "y", "y"], "s": swapped}), ["q"], "s")
    # Measures that do not use order still accept it.
    assert measure_t_closeness_equal(table, ["q"], "s").distinct_sensitive_values == 3
    assert measure_k_anonymity(pa.table({"s": column}), ["s"]).class_count == 3


def test_chunked_ordered_dictionary_with_compatible_order() -> None:
    """Chunk dictionaries [1, 3, 4] and [3, 4]: the first holds the second in
    order, so the order is 1 < 3 < 4. Rows x {3, 4}, y {1, 3}: Q = (1/4, 1/2, 1/4);
    x cumulative r = -1/4, 0, 0 -> 1/4 / 2 = 1/8; y: 1/4, 0, 0 -> 1/8. Same as
    the float column below."""
    column = _ordered_chunks(([1, 2], [1.0, 3.0, 4.0]), ([1, 0], [3.0, 4.0]))
    table = pa.table({"q": ["x", "x", "y", "y"], "s": column})
    plain = pd.DataFrame({"q": ["x", "x", "y", "y"], "s": [3.0, 4.0, 4.0, 3.0]})
    report = measure_t_closeness_ordered(table, ["q"], "s")
    assert report == measure_t_closeness_ordered(plain, ["q"], "s")
    # The reviewer's repro, reordered so one chunk holds the full order.
    repro = _ordered_chunks(([1, 2], [1.0, 3.0, 4.0]), ([0, 1], [1.0, 3.0]))
    repro_table = pa.table({"q": ["x", "x", "y", "y"], "s": repro})
    repro_plain = pd.DataFrame({"q": ["x", "x", "y", "y"], "s": [3.0, 4.0, 1.0, 3.0]})
    assert measure_t_closeness_ordered(repro_table, ["q"], "s") == measure_t_closeness_ordered(
        repro_plain, ["q"], "s"
    )


def test_decimal_values_compare_by_number() -> None:
    frame = pd.DataFrame({"q": pd.Series([Decimal("1.0"), Decimal("1.00"), Decimal("2")])})
    assert measure_k_anonymity(frame, ["q"]).class_count == 2
    table = pa.table({"q": pa.array([Decimal("1.0"), Decimal("1.0"), Decimal("2.0")])})
    assert measure_k_anonymity(table, ["q"]).class_count == 2


# --------------------------------------------------------- pandas and pyarrow


def _parity_frames() -> list[tuple[pd.DataFrame, list[str], str]]:
    return [
        (
            pd.DataFrame(
                {
                    "zip": ["1", "1", "2", "2", None, "3"],
                    "age": [30, 30, 40, 41, 50, 50],
                    "s": [1.0, 2.0, 2.0, None, 3.0, 1.0],
                }
            ),
            ["zip", "age"],
            "s",
        ),
        (
            pd.DataFrame({"q": ["a", "b", "a", "b"], "s": [3, 1, 2, 2]}),
            ["q"],
            "s",
        ),
    ]


@pytest.mark.parametrize("case", range(2))
def test_pandas_and_arrow_agree(case: int) -> None:
    frame, qi, sensitive = _parity_frames()[case]
    table = pa.Table.from_pandas(frame, preserve_index=False)
    assert measure_k_anonymity(frame, qi, k_target=2) == measure_k_anonymity(table, qi, k_target=2)
    assert measure_l_diversity(frame, qi, sensitive, l_target=2, recursive_c=2) == (
        measure_l_diversity(table, qi, sensitive, l_target=2, recursive_c=2)
    )
    for measure in (measure_t_closeness_equal, measure_t_closeness_ordered):
        assert measure(frame, qi, sensitive, t_target=0.3) == measure(
            table, qi, sensitive, t_target=0.3
        )


def test_unnamed_unhashable_column_is_not_read() -> None:
    table = pa.table({"q": ["a", "a"], "lists": pa.array([[1], [2]])})
    assert measure_k_anonymity(table, ["q"]).k_min == 2


# ----------------------------------------------------------- value-free reports

_ALLOWED_ANNOTATIONS = {
    int,
    float,
    bool,
    type(None),
    int | None,
    float | None,
    bool | None,
    tuple[tuple[int, int], ...],
}


# Exported dataclasses that are not reports: model descriptors (a column name
# and thresholds) and the integer-array frequency set. Listed, so a new
# exported dataclass is either a report (and walked) or named here on purpose.
_NOT_REPORTS = frozenset(
    {
        "DistinctLDiversity",
        "EntropyLDiversity",
        "EqualTCloseness",
        "FrequencySet",
        "KAnonymity",
        "OrderedTCloseness",
        "RecursiveCLDiversity",
    }
)


def _report_classes() -> list[type]:
    return [
        getattr(anonymity, name)
        for name in anonymity.__all__
        if dataclasses.is_dataclass(getattr(anonymity, name)) and name not in _NOT_REPORTS
    ]


def test_every_report_class_is_found() -> None:
    assert len(_report_classes()) == 3
    exported = {n for n in anonymity.__all__ if dataclasses.is_dataclass(getattr(anonymity, n))}
    assert exported >= _NOT_REPORTS


@pytest.mark.parametrize("cls", _report_classes(), ids=lambda c: c.__name__)
def test_report_fields_are_value_free(cls: type) -> None:
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen
    hints = typing.get_type_hints(cls)
    for field in dataclasses.fields(cls):
        hint = hints[field.name]
        if typing.get_origin(hint) is typing.Literal:
            assert all(isinstance(arg, str) for arg in typing.get_args(hint))
            continue
        assert hint in _ALLOWED_ANNOTATIONS, (cls.__name__, field.name, hint)


def _all_reports() -> list[Any]:
    frame, qi, sensitive = _parity_frames()[0]
    reports = [
        measure_k_anonymity(frame, qi, k_target=2),
        measure_l_diversity(frame, qi, sensitive, l_target=2, recursive_c=2),
        measure_t_closeness_equal(frame, qi, sensitive, t_target=0.3),
        measure_t_closeness_ordered(frame, qi, sensitive, t_target=0.3),
    ]
    table = pa.Table.from_pandas(frame, preserve_index=False)
    reports.append(measure_k_anonymity(table, qi, k_target=2))
    reports.append(measure_t_closeness_ordered(table, qi, sensitive, t_target=0.3))
    return reports


def test_report_values_are_builtin() -> None:
    allowed = {int, float, bool, str, type(None), tuple}
    for report in _all_reports():
        for field in dataclasses.fields(report):
            value = getattr(report, field.name)
            assert type(value) in allowed, (field.name, type(value))
            if type(value) is tuple:
                for pair in value:
                    assert type(pair) is tuple
                    assert all(type(x) is int for x in pair)


def test_report_construction_refuses_non_builtin_values() -> None:
    good = measure_k_anonymity(pd.DataFrame({"q": ["a"]}), ["q"])
    with pytest.raises(TypeError):
        dataclasses.replace(good, row_count=np.int64(1))
    with pytest.raises(TypeError):
        dataclasses.replace(good, k_min=SENTINEL)  # type: ignore[arg-type]
    t_report = measure_t_closeness_equal(pd.DataFrame({"q": ["a"], "s": [1]}), ["q"], "s")
    with pytest.raises(TypeError):
        dataclasses.replace(t_report, ground_distance="hierarchical")  # type: ignore[arg-type]


def test_reports_never_show_cell_values() -> None:
    frame = pd.DataFrame(
        {
            "q": [SENTINEL, SENTINEL, f"{SENTINEL}2"],
            "n": [SENTINEL_NUMBER, SENTINEL_NUMBER, SENTINEL_NUMBER + 1],
            "s": [SENTINEL_NUMBER, 3, 4],
        }
    )
    for report in (
        measure_k_anonymity(frame, ["q", "n"]),
        measure_l_diversity(frame, ["q", "n"], "s", recursive_c=2),
        measure_t_closeness_equal(frame, ["q", "n"], "s"),
        measure_t_closeness_ordered(frame, ["q", "n"], "s"),
    ):
        text = repr(report) + str(report)
        assert SENTINEL not in text
        assert str(SENTINEL_NUMBER) not in text


def test_k_report_is_frozen() -> None:
    report = measure_k_anonymity(pd.DataFrame({"q": ["a"]}), ["q"])
    assert isinstance(report, KAnonymityReport)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.k_min = 5  # type: ignore[misc]


# ----------------------------------------------------------------- errors


def _assert_value_free(exc: BaseException) -> None:
    assert SENTINEL not in str(exc)
    assert str(SENTINEL_NUMBER) not in str(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_unhashable_cells_raise_value_free() -> None:
    frame = pd.DataFrame({"q": pd.Series([[SENTINEL], [SENTINEL]], dtype=object), "s": [1, 2]})
    with pytest.raises(TypeError, match="'q'") as info:
        measure_k_anonymity(frame, ["q"])
    _assert_value_free(info.value)
    sens = pd.DataFrame({"q": ["a", "a"], "s": pd.Series([{SENTINEL: 1}, {}], dtype=object)})
    with pytest.raises(TypeError, match="'s'") as info:
        measure_l_diversity(sens, ["q"], "s")
    _assert_value_free(info.value)


def test_arrow_list_column_raises_value_free() -> None:
    table = pa.table({"q": pa.array([[SENTINEL], [SENTINEL]]), "s": [1, 2]})
    with pytest.raises(TypeError, match="'q'") as info:
        measure_k_anonymity(table, ["q"])
    _assert_value_free(info.value)


def test_conversion_failure_is_value_free(monkeypatch: pytest.MonkeyPatch) -> None:
    table = pa.table({"q": ["a", "b"]})

    def boom(arrow_type: Any) -> Any:
        raise pa.ArrowInvalid(f"Casting {SENTINEL} {SENTINEL_NUMBER} failed")

    # The type mapper runs inside Table.to_pandas, so this fails the real
    # conversion call (pa.Table itself cannot be patched).
    import decoy_engine.quality.anonymity._classes as classes_module

    monkeypatch.setattr(classes_module, "_arrow_type_mapper", boom)
    with pytest.raises(TypeError, match="'q'") as info:
        measure_k_anonymity(table, ["q"])
    _assert_value_free(info.value)


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda f: measure_k_anonymity(f, []), ValueError),
        (lambda f: measure_k_anonymity(f, "q"), TypeError),
        (lambda f: measure_k_anonymity(f, ["missing"]), ValueError),
        (lambda f: measure_k_anonymity(f, ["q", "q"]), ValueError),
        (lambda f: measure_k_anonymity(f, [1]), TypeError),
        (lambda f: measure_l_diversity(f, ["q"], "q"), ValueError),
        (lambda f: measure_l_diversity(f, ["q"], "missing"), ValueError),
        (lambda f: measure_k_anonymity(f.to_dict(), ["q"]), TypeError),
    ],
)
def test_column_errors(call: Callable[[pd.DataFrame], Any], error: type) -> None:
    frame = pd.DataFrame({"q": [SENTINEL], "s": [SENTINEL_NUMBER]})
    with pytest.raises(error) as info:
        call(frame)
    _assert_value_free(info.value)


def test_duplicate_column_in_table_is_refused() -> None:
    frame = pd.DataFrame([[1, 2]], columns=["q", "q"])
    with pytest.raises(ValueError, match="'q'"):
        measure_k_anonymity(frame, ["q"])


_BAD_INT_TARGETS: list[Any] = [True, False, 2.0, "2", 0, -1, np.int64(2)]
_BAD_C: list[Any] = [True, 0, -1.0, float("nan"), float("inf"), "2"]
_BAD_T: list[Any] = [True, -0.1, 1.1, float("nan"), float("inf"), "0.5"]


@pytest.mark.parametrize("bad", _BAD_INT_TARGETS)
def test_k_and_l_targets_are_strict(bad: Any) -> None:
    frame = pd.DataFrame({"q": ["a"], "s": [1]})
    with pytest.raises((TypeError, ValueError)) as info:
        measure_k_anonymity(frame, ["q"], k_target=bad)
    _assert_value_free(info.value)
    with pytest.raises((TypeError, ValueError)):
        measure_l_diversity(frame, ["q"], "s", l_target=bad)


@pytest.mark.parametrize("bad", _BAD_C)
def test_recursive_c_is_strict(bad: Any) -> None:
    frame = pd.DataFrame({"q": ["a"], "s": [1]})
    with pytest.raises((TypeError, ValueError)) as info:
        measure_l_diversity(frame, ["q"], "s", recursive_c=bad)
    _assert_value_free(info.value)


@pytest.mark.parametrize("bad", _BAD_T)
def test_t_target_is_strict(bad: Any) -> None:
    frame = pd.DataFrame({"q": ["a"], "s": [1]})
    for measure in (measure_t_closeness_equal, measure_t_closeness_ordered):
        with pytest.raises((TypeError, ValueError)) as info:
            measure(frame, ["q"], "s", t_target=bad)
        _assert_value_free(info.value)


def test_bool_target_is_a_type_error() -> None:
    frame = pd.DataFrame({"q": ["a"], "s": [1]})
    with pytest.raises(TypeError):
        measure_k_anonymity(frame, ["q"], k_target=True)


# ------------------------------------------------------------------ dark


def test_quality_lazy_exports_do_not_name_anonymity() -> None:
    import decoy_engine.quality as quality

    lazy = quality._LAZY_EXPORTS
    assert not any("anonymity" in module for module in lazy.values())
    assert not any(name in lazy for name in anonymity.__all__)


def _resolve(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


# The one value-identity rule lives at the anonymize package root; that exact
# module is allowed, nothing below it.
_ALLOWED_ENGINE_MODULES = frozenset({"decoy_engine.anonymize"})


def test_package_imports_only_data_libraries() -> None:
    """``import decoy_engine`` already loads the pipeline, so a sys.modules check
    cannot show darkness; the syntax tree of every module in the package can.
    Relative imports are resolved against the package; dynamic import
    machinery is refused so the walk cannot be sidestepped."""
    package = "decoy_engine.quality.anonymity"
    forbidden = {"importlib"}
    allowed_roots = (
        {"numpy", "pandas", "pyarrow", "__future__"} | set(sys.stdlib_module_names)
    ) - forbidden
    package_dir = Path(anonymity.__file__).parent
    modules = sorted(package_dir.glob("*.py"))
    assert len(modules) >= 5
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [_resolve(node, package)]
            elif isinstance(node, ast.Name) and node.id == "__import__":
                raise AssertionError((path.name, "__import__"))
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                inside = name == package or name.startswith(package + ".")
                assert root in allowed_roots or inside or name in _ALLOWED_ENGINE_MODULES, (
                    path.name,
                    name,
                )


def test_only_the_anonymize_root_is_allowed() -> None:
    assert "decoy_engine.anonymize" in _ALLOWED_ENGINE_MODULES
    for name in (
        "decoy_engine.anonymize.hierarchies",
        "decoy_engine.anonymize.search",
        "decoy_engine.anonymize._values",
    ):
        assert name not in _ALLOWED_ENGINE_MODULES


def test_import_walk_sees_relative_and_dynamic_imports() -> None:
    package = "decoy_engine.quality.anonymity"
    outside = ast.parse("from ..report import x").body[0]
    assert isinstance(outside, ast.ImportFrom)
    assert _resolve(outside, package) == "decoy_engine.quality.report"
    inside = ast.parse("from ._classes import y").body[0]
    assert isinstance(inside, ast.ImportFrom)
    assert _resolve(inside, package) == "decoy_engine.quality.anonymity._classes"
    assert "importlib" in sys.stdlib_module_names
