"""R3 acceptance test 2: the public `run_pipeline` signature is frozen.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 2.
`run_pipeline` is public API (`decoy_engine/__init__.py`), governed by the compatibility
contract and called across repos. R3 builds an internal `PipelineRunContext` from these same
arguments; it must not add, remove, rename, reorder or re-default one. The table below was
read off origin/main @ ac6a0e8e. A change to it is an API change that needs its own plan.

This is separate from the context-boundary test (test 4), which covers the internal
`maybe_run_unified_slice` boundary.
"""

from __future__ import annotations

import inspect

import decoy_engine
from decoy_engine.execution import run_pipeline as execution_run_pipeline
from decoy_engine.execution._pipeline import run_pipeline

# (name, kind, default repr, annotation string)
FROZEN_PARAMETERS: tuple[tuple[str, str, str, str], ...] = (
    ("config", "POSITIONAL_OR_KEYWORD", "<required>", "dict[str, Any]"),
    ("sources", "POSITIONAL_OR_KEYWORD", "None", "Mapping[str, pa.Table | LazySource] | None"),
    ("engine_version", "KEYWORD_ONLY", "<required>", "str"),
    ("registry", "KEYWORD_ONLY", "None", "ProviderRegistry | None"),
    ("derive_key", "KEYWORD_ONLY", "None", "Any"),
    ("instance_default_locale", "KEYWORD_ONLY", "None", "str | None"),
    ("vault_writer", "KEYWORD_ONLY", "None", "Any"),
    ("fidelity_report", "KEYWORD_ONLY", "False", "bool"),
    ("post_validation", "KEYWORD_ONLY", "False", "bool"),
    ("post_validation_skip", "KEYWORD_ONLY", "None", "list[str] | None"),
    ("post_validation_sample_size", "KEYWORD_ONLY", "100", "int"),
    ("post_validation_enforce", "KEYWORD_ONLY", "False", "bool"),
    ("now_iso", "KEYWORD_ONLY", "None", "str | None"),
    (
        "execution_mode",
        "KEYWORD_ONLY",
        "'auto'",
        "Literal['auto', 'sequential', 'full_frame', 'out_of_core']",
    ),
    ("sink", "KEYWORD_ONLY", "None", "TransactionalSink | None"),
    ("source_loader", "KEYWORD_ONLY", "None", "Callable[[str], pa.Table] | None"),
    ("substrate", "KEYWORD_ONLY", "'pandas'", "str | None"),
    ("fpe_chunk_count", "KEYWORD_ONLY", "4", "int"),
    ("max_workers", "KEYWORD_ONLY", "4", "int"),
    ("fallback_to_pandas", "KEYWORD_ONLY", "True", "bool"),
    ("explain_plan", "KEYWORD_ONLY", "False", "bool"),
    ("auto_chunk", "KEYWORD_ONLY", "True", "bool"),
    ("chunk_size_rows", "KEYWORD_ONLY", "50000", "int"),
    ("auto_chunk_threshold_rows", "KEYWORD_ONLY", "100000", "int"),
    ("native_threads", "KEYWORD_ONLY", "1", "int"),
    ("chunked_dispatcher_enabled", "KEYWORD_ONLY", "True", "bool"),
    ("stream_chunked_output", "KEYWORD_ONLY", "True", "bool"),
    ("multi_table_dispatch_enabled", "KEYWORD_ONLY", "True", "bool"),
    ("out_of_core_threshold_rows", "KEYWORD_ONLY", "5000000", "int"),
    ("full_frame_reject_rows", "KEYWORD_ONLY", "7500000", "int"),
    ("out_of_core_budget_bytes", "KEYWORD_ONLY", "None", "int | None"),
    ("use_byte_estimate_routing", "KEYWORD_ONLY", "True", "bool"),
    ("use_probe_routing", "KEYWORD_ONLY", "True", "bool"),
    ("key_provider", "KEYWORD_ONLY", "None", "KeyProvider | None"),
    ("out_of_core_reorder_threshold_rows", "KEYWORD_ONLY", "None", "int | None"),
    ("unified_slice_enabled", "KEYWORD_ONLY", "True", "bool"),
    ("_provider_snapshot", "KEYWORD_ONLY", "None", "Mapping[str, Callable[[Faker], Any]] | None"),
)
FROZEN_RETURN = "ExecutionResult"


def _observed() -> tuple[tuple[str, str, str, str], ...]:
    rows = []
    for p in inspect.signature(run_pipeline).parameters.values():
        default = "<required>" if p.default is inspect.Parameter.empty else repr(p.default)
        annotation = "" if p.annotation is inspect.Parameter.empty else str(p.annotation)
        rows.append((p.name, p.kind.name, default, annotation))
    return tuple(rows)


def test_run_pipeline_parameters_are_frozen() -> None:
    assert _observed() == FROZEN_PARAMETERS


def test_run_pipeline_return_annotation_is_frozen() -> None:
    assert str(inspect.signature(run_pipeline).return_annotation) == FROZEN_RETURN


def test_the_frozen_table_covers_every_parameter_once() -> None:
    names = [row[0] for row in FROZEN_PARAMETERS]
    assert len(names) == len(set(names)) == 37
    positional = [r[0] for r in FROZEN_PARAMETERS if r[1] == "POSITIONAL_OR_KEYWORD"]
    assert positional == ["config", "sources"]


def test_the_public_names_are_the_same_function() -> None:
    assert decoy_engine.run_pipeline is run_pipeline
    assert execution_run_pipeline is run_pipeline
