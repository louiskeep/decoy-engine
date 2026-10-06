"""C6c-i acceptance: the text_redact kernel contract, resolver, registry entry and config gates.

These tests pin the pieces that the route-level parity tests cannot reach on their own: the
kernel's own cell rules (called directly, with no admission in front of it), the single owner
of the empty-detector-list rule, the coded rejection of each excluded config on both config
dispatchers, and the registry facts.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _operator_registry
from decoy_engine.execution._operator_registry import ARROW_PYTHON, OPERATORS
from decoy_engine.execution._runner import WorkNode
from decoy_engine.execution._strategies import _text_redact
from decoy_engine.execution.native import _kernels_scalar
from decoy_engine.execution.native._operator_config_rejections import text_redact_config_rejection
from decoy_engine.execution.native._operator_params import (
    TextRedactParams,
    resolve_operator_params,
)
from decoy_engine.execution.native._operator_step import run_kernel_step
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.execution.native._requirements import (
    NATIVE_KERNEL_STRATEGIES,
    requirements_for,
)
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.storm import detectors as storm_detectors
from tests.native._c6c_i_support import (
    ADMITTED_CONFIGS,
    EXCLUDED_CONFIGS,
    KNOWN_HIT,
    tr_col,
)

DEFAULT_TOKEN = "[REDACTED]"


def _kernel() -> Any:
    from decoy_engine.execution.native._kernels_scalar import native_text_redact

    return native_text_redact


# ---------------------------------------------------------------------------
# 6a. Direct kernel contract, with no admission in front of it.
# ---------------------------------------------------------------------------


def test_the_kernel_returns_a_string_array_for_every_batch_shape() -> None:
    kernel = _kernel()
    for values in ([KNOWN_HIT], [], [None, None]):
        out = kernel(pa.array(values, pa.string()), detectors=None, token="T", label_token=False)
        assert isinstance(out, pa.Array)
        assert out.type == pa.string()
        assert len(out) == len(values)


def test_the_kernel_redacts_a_known_hit_with_the_token_or_the_detector_label() -> None:
    kernel = _kernel()
    arr = pa.array([KNOWN_HIT], pa.string())
    assert kernel(arr, detectors=None, token="<X>", label_token=False).to_pylist() == [
        "mail <X> now"
    ]
    assert kernel(arr, detectors=None, token="<X>", label_token=True).to_pylist() == [
        "mail [REDACTED:email] now"
    ]


def test_the_kernel_accepts_a_chunked_array_and_keeps_nulls_null() -> None:
    kernel = _kernel()
    chunked = pa.chunked_array([[KNOWN_HIT, None], [None, "plain"]], pa.string())
    out = kernel(chunked, detectors=None, token="T", label_token=False)
    assert out.to_pylist() == ["mail T now", None, None, "plain"]


def test_detectors_none_runs_every_detector_and_an_empty_tuple_runs_none() -> None:
    kernel = _kernel()
    arr = pa.array([KNOWN_HIT, "ssn 123-45-6789"], pa.string())
    every = kernel(arr, detectors=None, token="T", label_token=False).to_pylist()
    assert every == ["mail T now", "ssn T"]
    # Normalization is the resolver's job: an empty tuple reaching the kernel stays empty and
    # runs zero detectors, exactly as `iter_spans([])` does.
    none = kernel(arr, detectors=(), token="T", label_token=False).to_pylist()
    assert none == [KNOWN_HIT, "ssn 123-45-6789"]


def test_a_named_detector_subset_and_an_unknown_id_behave_like_the_oracle() -> None:
    kernel = _kernel()
    arr = pa.array([KNOWN_HIT, "ssn 123-45-6789"], pa.string())
    assert kernel(arr, detectors=("ssn",), token="T", label_token=False).to_pylist() == [
        KNOWN_HIT,
        "ssn T",
    ]
    assert kernel(arr, detectors=("no_such",), token="T", label_token=False).to_pylist() == [
        KNOWN_HIT,
        "ssn 123-45-6789",
    ]


@pytest.mark.parametrize(
    "values, typ, expected",
    [
        ([1234567893, 7, None], pa.int64(), ["T", "7", None]),
        ([2.5, None], pa.float64(), ["2.5", None]),
        ([True, False, None], pa.bool_(), ["True", "False", None]),
    ],
    ids=["int64", "float64", "bool"],
)
def test_the_kernel_stringifies_non_string_cells_and_keeps_nulls_null(
    values: list[Any], typ: pa.DataType, expected: list[Any]
) -> None:
    out = _kernel()(pa.array(values, typ), detectors=None, token="T", label_token=False)
    assert out.type == pa.string()
    assert out.to_pylist() == expected


def test_the_public_empty_detector_list_redacts_the_same_hit_the_empty_tuple_leaves() -> None:
    """Empty means all, and the resolver alone owns that rule."""
    params = resolve_operator_params(
        "text_redact",
        target="s",
        provider_config={"detectors": []},
        namespace=None,
    )
    assert isinstance(params, TextRedactParams)
    assert params.detectors is None
    arr = pa.array([KNOWN_HIT], pa.string())
    assert run_kernel_step(params, arr, mask_key=None, native_threads=None).out.to_pylist() == [
        f"mail {DEFAULT_TOKEN} now"
    ]


def test_the_step_returns_a_non_compiled_result() -> None:
    params = TextRedactParams(detectors=None, token="T", label_token=False)
    result = run_kernel_step(
        params, pa.array([KNOWN_HIT], pa.string()), mask_key=None, native_threads=None
    )
    assert result.ran is None
    assert result.out.to_pylist() == ["mail T now"]


# ---------------------------------------------------------------------------
# 3b. The resolver is the one owner of normalization and defaults.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg, expected",
    [
        ({}, TextRedactParams(None, DEFAULT_TOKEN, False)),
        ({"detectors": None}, TextRedactParams(None, DEFAULT_TOKEN, False)),
        ({"detectors": []}, TextRedactParams(None, DEFAULT_TOKEN, False)),
        ({"detectors": ()}, TextRedactParams(None, DEFAULT_TOKEN, False)),
        ({"detectors": ["email"]}, TextRedactParams(("email",), DEFAULT_TOKEN, False)),
        ({"detectors": ("a", "b")}, TextRedactParams(("a", "b"), DEFAULT_TOKEN, False)),
        ({"detectors": ["ssn", 5]}, TextRedactParams(("ssn", "5"), DEFAULT_TOKEN, False)),
        ({"token": "<X>"}, TextRedactParams(None, "<X>", False)),
        ({"token": ""}, TextRedactParams(None, "", False)),
        ({"label_token": True}, TextRedactParams(None, DEFAULT_TOKEN, True)),
        ({"label_token": 1}, TextRedactParams(None, DEFAULT_TOKEN, True)),
        ({"label_token": 0}, TextRedactParams(None, DEFAULT_TOKEN, False)),
        ({"label_token": None}, TextRedactParams(None, DEFAULT_TOKEN, False)),
    ],
)
def test_the_resolver_applies_the_oracles_normalization_once(
    cfg: dict[str, Any], expected: TextRedactParams
) -> None:
    params = resolve_operator_params("text_redact", target="s", provider_config=cfg, namespace=None)
    assert params == expected


def test_the_resolver_default_token_is_the_oracles_own_constant() -> None:
    assert _text_redact._DEFAULT_TOKEN == DEFAULT_TOKEN
    params = resolve_operator_params("text_redact", target="s", provider_config={}, namespace=None)
    assert isinstance(params, TextRedactParams)
    assert params.token is _text_redact._DEFAULT_TOKEN


def test_text_redact_params_are_frozen() -> None:
    params = TextRedactParams(None, "T", False)
    with pytest.raises(Exception, match="cannot assign"):
        params.token = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. One implementation of the span logic.
# ---------------------------------------------------------------------------


def _kernel_module_tree() -> tuple[ast.Module, ast.FunctionDef]:
    path = Path(inspect.getsourcefile(_kernel()) or "")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "native_text_redact"
    )
    return tree, fn


def test_the_kernel_calls_the_oracles_span_detection_and_splice() -> None:
    _tree, fn = _kernel_module_tree()
    called = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert {"iter_spans", "_splice"} <= called


def test_the_kernel_module_defines_no_regex_of_its_own() -> None:
    tree, _fn = _kernel_module_tree()
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    } | {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "re" not in imported and "regex" not in imported
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"compile", "finditer", "findall", "sub", "search"}


def test_the_kernel_reuses_the_oracles_objects_rather_than_copies() -> None:
    assert _kernels_scalar.iter_spans is storm_detectors.iter_spans
    assert _kernels_scalar._splice is _text_redact._splice


# ---------------------------------------------------------------------------
# 3b. Registry entry.
# ---------------------------------------------------------------------------


def test_the_registry_entry_matches_the_plan() -> None:
    spec = OPERATORS["text_redact"]
    assert spec.operator_id == "native_text_redact"
    assert spec.shape == "kernel"
    assert spec.planned_backend == ARROW_PYTHON
    assert spec.required_kernel is None
    assert spec.positive_kernel_evidence is False
    assert spec.unified_resident_types == frozenset({pa.string()})
    assert spec.full_frame_assembly == "null_on_empty"
    assert spec.routed_diagnostics == frozenset()
    assert spec.provider_allowlist is None
    assert "text_redact" in NATIVE_KERNEL_STRATEGIES
    assert _operator_registry.operator_spec("text_redact") is spec


def test_the_unified_adapter_binds_text_redact_as_an_unkeyed_operator() -> None:
    from decoy_engine.execution.physical._shadow_bindings import _key_binding
    from decoy_engine.execution.physical._shadow_operators import _UNKEYED_PARAMS

    assert _UNKEYED_PARAMS[OPERATORS["text_redact"].operator_id] is TextRedactParams
    assert _key_binding("key_source", TextRedactParams(None, "T", False)) is None


# ---------------------------------------------------------------------------
# 3c. One config predicate, wired into both dispatchers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ADMITTED_CONFIGS))
def test_the_predicate_admits_every_plain_config(name: str) -> None:
    assert text_redact_config_rejection("s", ADMITTED_CONFIGS[name]) is None


@pytest.mark.parametrize(
    "cfg",
    [{"ner": False}, {"ner": None}, {"ner": {}}, {"ner": 0}, {"ner": ""}],
    ids=["false", "none", "empty_dict", "zero", "empty_string"],
)
def test_a_falsy_ner_value_is_not_an_ner_config(cfg: dict[str, Any]) -> None:
    # The oracle's `if ner_cfg:` never loads a model for these, so neither does the gate.
    assert text_redact_config_rejection("s", cfg) is None


@pytest.mark.parametrize("name", sorted(EXCLUDED_CONFIGS))
def test_the_predicate_names_each_excluded_config(name: str) -> None:
    cfg, code = EXCLUDED_CONFIGS[name]
    assert text_redact_config_rejection("col", cfg) == f"{code}:col"


@pytest.mark.parametrize("name", sorted(ADMITTED_CONFIGS))
def test_the_eligibility_report_accepts_every_plain_config(name: str) -> None:
    result = native_route_eligibility(
        {
            "global_settings": {"seed": 1},
            "tables": [{"name": "t", "columns": [tr_col("s", **ADMITTED_CONFIGS[name])]}],
        },
        table="t",
    )
    assert result.accepted is True, result.rejections
    assert result.rejections == ()


@pytest.mark.parametrize("name", sorted(EXCLUDED_CONFIGS))
def test_the_eligibility_report_gives_the_exact_code_for_each_excluded_config(name: str) -> None:
    cfg, code = EXCLUDED_CONFIGS[name]
    result = native_route_eligibility(
        {
            "global_settings": {"seed": 1},
            "tables": [{"name": "t", "columns": [tr_col("s", **cfg)]}],
        },
        table="t",
    )
    assert result.accepted is False
    assert result.rejections == (f"{code}:s",)


def _profile() -> Profile:
    column = ColumnProfile(
        name="s",
        dtype="object",
        row_count=3,
        null_count=0,
        distinct_count=3,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=3, columns=(column,)),),
        relationships=(),
        profiled_at=datetime(2026, 10, 6, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _node(cfg: dict[str, Any]) -> WorkNode:
    seed = ColumnSeed(
        namespace="ns",
        strategy="text_redact",
        provider=None,
        backend_type=None,
        backend_version="",
        cardinality_mode="reuse",
        provider_config=tuple(cfg.items()),
        coherent_with=(),
    )
    return WorkNode(
        table="t",
        columns=("s",),
        kind="scalar",
        strategy="text_redact",
        provider=None,
        plan_slice=seed,
    )


@pytest.mark.parametrize("name", sorted(ADMITTED_CONFIGS))
def test_the_requirement_resolver_sends_every_plain_config_native(name: str) -> None:
    req = requirements_for(_node(ADMITTED_CONFIGS[name]), plan=None, profile=_profile())
    assert req.fallback_policy == "native"


@pytest.mark.parametrize("name", sorted(EXCLUDED_CONFIGS))
def test_the_requirement_resolver_holds_every_excluded_config_on_the_oracle(name: str) -> None:
    cfg, _code = EXCLUDED_CONFIGS[name]
    req = requirements_for(_node(cfg), plan=None, profile=_profile())
    assert req.fallback_policy == "python_only"
