"""C8-iii-c acceptance tests 3, 4 and 4a: one test per boundary, no echo, kept defenses.

Plan: docs/plans/2026-10-07-c8-iii-c-rawdict-when.md (rev 3), sections 2c, 2d and 2e.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.errors import ValidationError
from decoy_engine.execution import PandasExecutionAdapter, run_pipeline
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._unified_slice_when import compute_when_masks, when_columns_admitted
from decoy_engine.execution._when_gate import _eval_predicate, run_with_when_gate
from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked
from decoy_engine.execution.native._when_admission import when_native_rejection
from decoy_engine.execution.native._when_mask import WhenSpec, when_mask, when_specs
from decoy_engine.plan import compile_plan, plan_from_yaml, plan_to_yaml, run_config_only_checks
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    key_provider,
    make_config,
    passthrough,
    redact,
)
from tests.unit.execution import _c8_iii_c_support as sup

CODE = "when_outside_closed_grammar"
BAD = "x.notnull()"
S = sup.SENTINEL
LEAKY = f"s == '{S}' and x.notnull()"  # outside the grammar, and its diagnostic could carry S


def _frame(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({"s": [f"v{i}" for i in range(n)], "x": list(range(n))})


def _chunks(n: int = 4) -> list[pa.Table]:
    return [
        pa.table({"s": [f"v{i}" for i in range(n)], "x": pa.array(list(range(n)), pa.int64())})
        for _ in range(2)
    ]


def _chunked_config(predicate: str) -> dict[str, Any]:
    return make_config([{**redact("s"), "when": predicate}, passthrough("x")])


# --- 3(a) public entrypoints raise PlanCompileError --------------------------


def _via_run_pipeline(predicate: str, tmp_path: Path) -> None:
    config, sources = sup.single_table_config(tmp_path)
    run_pipeline(
        sup.with_when(config, "t", "s", predicate), sources=sources, engine_version="c8-iii-c"
    )


def _via_config_only(predicate: str, tmp_path: Path) -> None:
    config, _ = sup.single_table_config(tmp_path)
    run_config_only_checks(sup.with_when(config, "t", "s", predicate))


def _chunked(entry: Any, chunks: list[pa.Table], predicate: str) -> None:
    list(
        entry(
            _chunked_config(predicate),
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )


CHUNKED_ENTRIES = [run_mask_chunked, run_mask_pipeline_chunked, run_native_or_oracle_chunked]
CHUNKED_IDS = ["run_mask_chunked", "run_mask_pipeline_chunked", "run_native_or_oracle_chunked"]


@pytest.mark.parametrize("predicate", [BAD, LEAKY], ids=["method", "leaky"])
def test_run_pipeline_raises_plan_compile_error(tmp_path: Path, predicate: str) -> None:
    with pytest.raises(PlanCompileError) as info:
        _via_run_pipeline(predicate, tmp_path)
    assert info.value.code == CODE


@pytest.mark.parametrize("predicate", [BAD, LEAKY], ids=["method", "leaky"])
def test_run_config_only_checks_raises_plan_compile_error(tmp_path: Path, predicate: str) -> None:
    with pytest.raises(PlanCompileError) as info:
        _via_config_only(predicate, tmp_path)
    assert info.value.code == CODE


@pytest.mark.parametrize("predicate", [BAD, LEAKY], ids=["method", "leaky"])
@pytest.mark.parametrize("entry", CHUNKED_ENTRIES, ids=CHUNKED_IDS)
def test_chunked_config_entrypoints_raise_plan_compile_error(entry: Any, predicate: str) -> None:
    with pytest.raises(PlanCompileError) as info:
        _chunked(entry, _chunks(), predicate)
    assert info.value.code == CODE


@pytest.mark.parametrize("entry", CHUNKED_ENTRIES, ids=CHUNKED_IDS)
def test_chunked_config_entrypoints_reject_before_reading_any_chunk(entry: Any) -> None:
    with pytest.raises(Exception) as info:
        _chunked(entry, [], BAD)
    assert isinstance(info.value, PlanCompileError), repr(info.value)
    assert info.value.code == CODE


# --- 3(b) admission is unchanged: declines return values ---------------------


def _entries(predicate: str) -> list[dict[str, Any]]:
    return [{**redact("s"), "when": predicate}, passthrough("x")]


def test_native_admission_declines_with_the_outside_subset_code() -> None:
    schema = pa.schema([("s", pa.string()), ("x", pa.string())])
    got = when_native_rejection(
        "s", _entries(BAD), get_default_registry(), table="t", schema=schema
    )
    assert got == "when_predicate_outside_native_subset:s"


def test_unified_admission_returns_false() -> None:
    source = pa.table({"s": ["a", "b"], "x": ["c", "d"]})
    assert when_columns_admitted(_entries(BAD), source, get_default_registry(), table="t") is False


def test_when_specs_raises_the_typed_grammar_error() -> None:
    seeds = {"s": SimpleNamespace(strategy="redact", when=BAD)}
    with pytest.raises(ValidationError) as info:
        when_specs(seeds)
    assert info.value.code == CODE


# --- 3(c) direct mask helpers raise StrategyError -----------------------------


@pytest.mark.parametrize("rows", [3, 0], ids=["non_empty", "zero_rows"])
def test_eval_predicate_raises_the_grammar_code(rows: int) -> None:
    with pytest.raises(StrategyError) as info:
        _eval_predicate(_frame(rows), BAD, "redact", column="s")
    assert info.value.code == CODE
    assert info.value.strategy == "redact"


@pytest.mark.parametrize("rows", [3, 0], ids=["non_empty", "zero_rows"])
def test_unified_slice_mask_raises_the_grammar_code(rows: int) -> None:
    node = SimpleNamespace(
        node_id="n1",
        strategy="redact",
        columns=["s"],
        execution=SimpleNamespace(when_expression=BAD),
    )
    with pytest.raises(StrategyError) as info:
        compute_when_masks(_frame(rows), [node])
    assert info.value.code == CODE


@pytest.mark.parametrize("rows", [3, 0], ids=["non_empty", "zero_rows"])
def test_native_mask_raises_the_grammar_code(rows: int) -> None:
    spec = WhenSpec("s", "redact", BAD, ("x",))
    table = pa.table({"x": pa.array(list(range(rows)), pa.int64())})
    with pytest.raises(StrategyError) as info:
        when_mask(spec, table, set())
    assert info.value.code == CODE


def test_the_backstop_stops_before_pandas_eval_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(pd.DataFrame, "eval", lambda *a, **k: calls.append(a))
    with pytest.raises(StrategyError):
        _eval_predicate(_frame(), BAD, "redact", column="s")
    assert calls == []


@pytest.mark.parametrize(
    "value", [5, None, ["x > 0"], b"x > 0"], ids=["int", "none", "list", "bytes"]
)
def test_the_backstop_rejects_a_non_string_predicate(value: Any) -> None:
    with pytest.raises(StrategyError) as info:
        _eval_predicate(_frame(), value, "redact", column="s")
    assert info.value.code == CODE


def test_the_backstop_rejects_a_blank_predicate() -> None:
    with pytest.raises(StrategyError) as info:
        _eval_predicate(_frame(), "   ", "redact", column="s")
    assert info.value.code == CODE


def test_the_backstop_parse_is_cached_per_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.expressions import _when_parser

    seen: list[str] = []
    real = _when_parser.parse_when
    _when_parser.parse_when_cached.cache_clear()
    monkeypatch.setattr(_when_parser, "parse_when", lambda e: (seen.append(e), real(e))[1])
    for _ in range(3):
        _eval_predicate(_frame(), "x > 0", "redact", column="s")
    assert seen == ["x > 0"]


# --- 4 no echo, anywhere it can render ---------------------------------------


def _datetime_frame() -> pd.DataFrame:
    return pd.DataFrame({"s": ["a", "b"], "x": pd.to_datetime(["2020-01-01", "2020-01-02"])})


def _raise_from(call: Any) -> BaseException:
    with pytest.raises(BaseException) as info:
        call()
    return info.value


def test_expression_error_carries_no_predicate_text_in_any_rendering() -> None:
    exc = _raise_from(
        lambda: _eval_predicate(_datetime_frame(), f"x < '{S}'", "redact", column="s")
    )
    assert isinstance(exc, StrategyError) and exc.code == "when_expression_error"
    sup.assert_no_sentinel(exc)


def test_expression_error_through_the_gate_carries_no_predicate_text() -> None:
    from decoy_engine.execution._strategies._redact import RedactHandler

    seed = sup.ColumnSeed(
        namespace=None,
        strategy="redact",
        provider=None,
        backend_type="builtin",  # type: ignore[arg-type]
        backend_version="1",
        cardinality_mode="reuse",  # type: ignore[arg-type]
        when=f"x < '{S}'",
    )
    exc = _raise_from(
        lambda: run_with_when_gate(
            RedactHandler(),
            _datetime_frame(),
            "s",
            seed,
            SimpleNamespace(row_errors=[]),  # type: ignore[arg-type]
        )
    )
    assert isinstance(exc, StrategyError) and exc.code == "when_expression_error"
    sup.assert_no_sentinel(exc)


def test_not_boolean_error_carries_no_predicate_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd.DataFrame, "eval", lambda *a, **k: pd.Series([1, 2]))
    exc = _raise_from(
        lambda: _eval_predicate(_datetime_frame(), f"s == '{S}'", "redact", column="s")
    )
    assert isinstance(exc, StrategyError) and exc.code == "when_expression_not_boolean"
    sup.assert_no_sentinel(exc)


def test_the_backstop_rejection_carries_no_predicate_text() -> None:
    exc = _raise_from(lambda: _eval_predicate(_frame(), LEAKY, "redact", column="s"))
    assert isinstance(exc, StrategyError) and exc.code == CODE
    sup.assert_no_sentinel(exc)


def test_parse_when_chain_carries_no_predicate_text() -> None:
    from decoy_engine.expressions._when_parser import parse_when

    for text in (LEAKY, f"`{S}", f"s == f'{S}'", f"s == b'{S}'"):
        exc = _raise_from(lambda t=text: parse_when(t))
        assert isinstance(exc, ValidationError)
        sup.assert_no_sentinel(exc)


def test_parse_when_names_the_position_and_attaches_no_lark_cause() -> None:
    from decoy_engine.expressions._when_parser import parse_when

    for text in ("x.notnull()", "`oops", "s ==", "x" + " and x > 1" * 40):
        exc = _raise_from(lambda t=text: parse_when(t))
        assert exc.__cause__ is None
        assert exc.__context__ is None


def _validated_config_with_when(predicate: str) -> dict[str, Any]:
    """A schema-shaped raw config whose only fault is the `when`."""
    return {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "targets": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "s", "strategy": "redact", "when": predicate},
                    {"name": "x", "strategy": "passthrough"},
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "boundary",
    [
        "run_pipeline",
        "compile_plan",
        "config_only",
        "run_mask_chunked",
        "run_mask_pipeline_chunked",
        "run_native_or_oracle_chunked",
        "when_specs",
        "validate_plan_when",
        "plan_from_yaml",
        "adapter_run",
        "adapter_run_sequential",
        "eval_predicate",
        "unified_mask",
        "native_mask",
        "pydantic_model_validate",
        "validate_config",
    ],
)
def test_no_raising_boundary_renders_the_predicate(boundary: str, tmp_path: Path) -> None:
    (tmp_path / "job").mkdir()
    (tmp_path / "single").mkdir()
    (tmp_path / "rp").mkdir()
    job = sup.two_table_job(tmp_path / "job")
    config, sources = sup.single_table_config(tmp_path / "single")

    def call() -> None:
        if boundary == "run_pipeline":
            _via_run_pipeline(LEAKY, tmp_path / "rp")
        elif boundary == "compile_plan":
            raw = sup.with_when(config, "t", "s", LEAKY)
            compile_plan(raw, profile_source(config, seed=7), decoy_engine_version="0.1.0")
        elif boundary == "config_only":
            run_config_only_checks(sup.with_when(config, "t", "s", LEAKY))
        elif boundary in {"run_mask_chunked", "run_mask_pipeline_chunked"}:
            entry = {"run_mask_chunked": run_mask_chunked}.get(boundary, run_mask_pipeline_chunked)
            _chunked(entry, _chunks(), LEAKY)
        elif boundary == "run_native_or_oracle_chunked":
            _chunked(run_native_or_oracle_chunked, _chunks(), LEAKY)
        elif boundary == "when_specs":
            when_specs({"s": SimpleNamespace(strategy="redact", when=LEAKY)})
        elif boundary == "validate_plan_when":
            from decoy_engine.expressions._when_parser import validate_plan_when

            validate_plan_when(sup.plan_with_when(job.plan, "a", "s", LEAKY))
        elif boundary == "plan_from_yaml":
            import yaml

            doc = yaml.safe_load(plan_to_yaml(job.plan))
            doc["seed_envelope"]["per_table"]["a"]["per_column"]["s"]["when"] = LEAKY
            plan_from_yaml(yaml.safe_dump(doc, sort_keys=False))
        elif boundary == "adapter_run":
            PandasExecutionAdapter().run(
                sup.plan_with_when(job.plan, "a", "s", LEAKY),
                dict(job.sources),
                registry=job.registry,
                relationship_graph=job.graph,
                namespace_registry=job.namespaces,
            )
        elif boundary == "adapter_run_sequential":
            PandasExecutionAdapter().run_sequential(
                sup.plan_with_when(job.plan, "a", "s", LEAKY),
                lambda name: job.sources[name],
                registry=job.registry,
                relationship_graph=job.graph,
                namespace_registry=job.namespaces,
                sink=lambda name, table: None,
            )
        elif boundary == "eval_predicate":
            _eval_predicate(_frame(), LEAKY, "redact", column="s")
        elif boundary == "unified_mask":
            node = SimpleNamespace(
                node_id="n1",
                strategy="redact",
                columns=["s"],
                execution=SimpleNamespace(when_expression=LEAKY),
            )
            compute_when_masks(_frame(), [node])
        elif boundary == "pydantic_model_validate":
            from decoy_engine.config import PipelineConfig

            PipelineConfig.model_validate(_validated_config_with_when(LEAKY))
        elif boundary == "validate_config":
            from decoy_engine.validation import validate_config

            validate_config(_validated_config_with_when(LEAKY))
        else:
            when_mask(WhenSpec("s", "redact", LEAKY, ("x",)), _chunks()[0], set())

    del sources
    exc = _raise_from(call)
    if boundary not in {"pydantic_model_validate", "validate_config"}:
        assert isinstance(exc, (PlanCompileError, ValidationError, StrategyError)), repr(exc)
    sup.assert_no_sentinel(exc)


# --- 4a kept defenses, tested directly ----------------------------------------


def test_the_eval_scope_clamps_hold_for_an_accepted_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    real = pd.DataFrame.eval

    def spy(self: pd.DataFrame, expr: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(self, expr, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "eval", spy)
    _eval_predicate(_frame(), "x > 1", "redact", column="s")
    assert len(seen) == 1
    assert seen[0]["engine"] == "numexpr"
    assert seen[0]["local_dict"] == {}
    assert seen[0]["global_dict"] == {}


@pytest.mark.parametrize(
    "result", [pd.Series([1, 2, 3]), 7, "text"], ids=["int_series", "scalar", "string"]
)
def test_an_accepted_predicate_with_a_non_boolean_result_is_rejected(
    monkeypatch: pytest.MonkeyPatch, result: Any
) -> None:
    monkeypatch.setattr(pd.DataFrame, "eval", lambda *a, **k: result)
    with pytest.raises(StrategyError) as info:
        _eval_predicate(_frame(), "x > 1", "redact", column="s")
    assert info.value.code == "when_expression_not_boolean"
    assert info.value.strategy == "redact"


def test_a_missing_numexpr_still_raises_the_numexpr_required_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise ImportError("no numexpr")

    monkeypatch.setattr(pd.DataFrame, "eval", boom)
    with pytest.raises(StrategyError) as info:
        _eval_predicate(_frame(), "x > 1", "redact", column="s")
    assert info.value.code == "numexpr_required"


def test_an_undefined_column_is_an_expression_error_with_no_text() -> None:
    with pytest.raises(StrategyError) as info:
        _eval_predicate(_frame(), "absent_col == 'q'", "redact", column="s")
    assert info.value.code == "when_expression_error"
    assert "absent_col" not in str(info.value)


@pytest.mark.parametrize("model", ["ColumnConfig", "TableConfig"])
def test_a_config_submodel_validated_alone_renders_no_predicate(model: str) -> None:
    from decoy_engine.config import _tables

    column = {"name": "s", "strategy": "redact", "when": LEAKY}
    payload = column if model == "ColumnConfig" else {"name": "t", "columns": [column]}
    exc = _raise_from(lambda: getattr(_tables, model).model_validate(payload))
    sup.assert_no_sentinel(exc)


# The CHANGELOG migration table documents these forms for `x.notnull()` and `x.isna()`. The
# negated form selects nothing on a pandas nullable dtype (NOT of NA is NA), so the table says
# there is no `isna` equivalent there. These cases keep that caveat honest.
_NULLABLE = {
    "string": lambda: pd.array(["a", "", None], dtype="string"),
    "string_pyarrow": lambda: pd.array(["a", "", None], dtype="string[pyarrow]"),
    "Int64": lambda: pd.array([1, 0, None], dtype="Int64"),
    "Float64": lambda: pd.array([1.5, 0.0, None], dtype="Float64"),
}
_NOT_NULL_FORM = {"string": "x >= ''", "string_pyarrow": "x >= ''"}
_NUMERIC_NOT_NULL = "x < 0 or x >= 0"


def _selected(values: Any, predicate: str) -> list[bool]:
    mask = _eval_predicate(pd.DataFrame({"x": values}), predicate, "redact", column="x")
    return [bool(v) for v in mask.fillna(False).to_numpy(dtype=bool)]


@pytest.mark.parametrize("dtype", sorted(_NULLABLE))
def test_the_notnull_form_selects_the_non_null_rows_on_nullable_dtypes(dtype: str) -> None:
    predicate = _NOT_NULL_FORM.get(dtype, _NUMERIC_NOT_NULL)
    assert _selected(_NULLABLE[dtype](), predicate) == [True, True, False]


@pytest.mark.parametrize("dtype", sorted(_NULLABLE))
def test_the_negated_form_selects_no_rows_on_nullable_dtypes(dtype: str) -> None:
    predicate = f"not ({_NOT_NULL_FORM.get(dtype, _NUMERIC_NOT_NULL)})"
    assert _selected(_NULLABLE[dtype](), predicate) == [False, False, False]


def test_the_negated_form_selects_the_null_row_on_numpy_dtypes() -> None:
    assert _selected(pd.array([1.5, 0.0, None], dtype="float64"), f"not ({_NUMERIC_NOT_NULL})") == [
        False,
        False,
        True,
    ]
    assert _selected(pd.array(["a", "", None], dtype=object), "not (x >= '')") == [
        False,
        False,
        True,
    ]


# --- no exception context either -------------------------------------------------


def _assert_no_context(exc: BaseException) -> None:
    """`from None` hides the context from tracebacks but keeps `__context__` on the object, so a
    handler that walks it would still reach the pandas or Lark error that quotes the text."""
    assert exc.__cause__ is None, repr(exc.__cause__)
    assert exc.__context__ is None, repr(exc.__context__)


@pytest.mark.parametrize(
    "text", [LEAKY, f"`{S}", f"s == f'{S}'", "x" + " and x > 1" * 40, "index > 1", "s =="]
)
def test_parse_when_leaves_no_exception_context(text: str) -> None:
    from decoy_engine.expressions._when_parser import parse_when

    _assert_no_context(_raise_from(lambda: parse_when(text)))


def test_the_backstop_leaves_no_exception_context() -> None:
    _assert_no_context(_raise_from(lambda: _eval_predicate(_frame(), LEAKY, "redact", column="s")))


def test_the_pandas_boundary_leaves_no_exception_context() -> None:
    exc = _raise_from(
        lambda: _eval_predicate(_datetime_frame(), f"x < '{S}'", "redact", column="s")
    )
    assert isinstance(exc, StrategyError) and exc.code == "when_expression_error"
    _assert_no_context(exc)


def test_the_compile_check_leaves_no_exception_context(tmp_path: Path) -> None:
    exc = _raise_from(lambda: _via_config_only(LEAKY, tmp_path))
    assert isinstance(exc, PlanCompileError)
    _assert_no_context(exc)


def test_plan_level_validation_leaves_no_exception_context(tmp_path: Path) -> None:
    from decoy_engine.expressions._when_parser import validate_plan_when

    job = sup.two_table_job(tmp_path)
    exc = _raise_from(lambda: validate_plan_when(sup.plan_with_when(job.plan, "a", "s", LEAKY)))
    assert isinstance(exc, ValidationError)
    _assert_no_context(exc)
