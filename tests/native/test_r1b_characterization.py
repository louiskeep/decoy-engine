"""R1b baseline: two route rules that no admitted-config comparison can see.

The namespace rule: categorical, bucket_perturb and date_shift hand the kernel `namespace or ""`,
hash hands it the raw value (so `None` stays `None`). Admitted configs always carry a namespace,
so the cross-route kwargs test never reaches this branch; these cases call the per-operator
kernel call directly with `namespace=None`.

The ran signal: whether a compiled kernel ran is read from each kernel's own `derive_calls`, and
the three kernels that report it disagree on purpose. group_key counts any non-empty sibling,
an all-null one included, and nothing for zero rows; bucket_perturb and date_shift count only
chunks holding a parseable value.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._companion_status import native_companion_status
from tests.native._r1b_support import (
    SIBLING,
    TARGET,
    call_step,
    compile_column,
    real_kernels,
    recording_kernels,
    run_chunked,
    run_unified,
    table_for,
)

NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)

_FMT = "%Y-%m-%d"
_DATES = pa.array(["2024-01-05", None, "2024-03-09"], pa.string())

_NAMESPACE_CASES = [
    ("bucket_perturb", {"bucket": "month", "date_format": _FMT}, "native_bucket_perturb", ""),
    ("date_shift", {"date_format": _FMT}, "native_date_shift", ""),
    ("categorical", {"categories": ("a", "b")}, "native_categorical", ""),
    ("hash", {}, "native_keyed_hash", None),
]


@pytest.mark.parametrize(
    "strategy, cfg, kernel, absent",
    [pytest.param(*case, id=case[0]) for case in _NAMESPACE_CASES],
)
def test_a_missing_namespace_reaches_the_kernel_as_the_documented_value(
    strategy: str, cfg: dict[str, Any], kernel: str, absent: str | None
) -> None:
    with recording_kernels() as calls:
        call_step(strategy, cfg=cfg, namespace=None, source=_DATES)
    assert [c.kernel for c in calls] == [kernel]
    assert calls[0].kwargs["namespace"] == absent


@pytest.mark.parametrize(
    "strategy, cfg, kernel",
    [pytest.param(s, c, k, id=s) for s, c, k, _ in _NAMESPACE_CASES],
)
def test_a_given_namespace_reaches_the_kernel_unchanged(
    strategy: str, cfg: dict[str, Any], kernel: str
) -> None:
    with recording_kernels() as calls:
        call_step(strategy, cfg=cfg, namespace="ns_given", source=_DATES)
    assert [c.kernel for c in calls] == [kernel]
    assert calls[0].kwargs["namespace"] == "ns_given"


@pytest.mark.parametrize(
    "cfg",
    [{}, {"length": None}, {"length": "3"}, {"length": 2.5}],
    ids=["absent", "none", "string", "float"],
)
def test_a_length_that_is_not_an_int_reaches_the_truncate_kernel_as_zero(
    cfg: dict[str, Any],
) -> None:
    """Admission rejects such a config, so no admitted example reaches this coercion; the
    kernel's own validation then fails closed on 0, where a default of 1 would silently
    truncate to one character."""
    with recording_kernels() as calls:
        call_step("truncate", cfg=cfg, namespace=None, source=_DATES)
    assert [c.kernel for c in calls] == ["native_truncate"]
    assert calls[0].kwargs["length"] == 0


_SOURCES: dict[str, list[Any]] = {
    "zero_rows": [],
    "all_null": [None, None],
    "mixed": ["2024-01-05", None, "bad"],
    "all_unparseable": ["bad", "worse"],
    "all_parseable": ["2024-01-05", "2024-02-01"],
    "one_row": ["2024-01-05"],
}

_PARSE_GATED = {
    "zero_rows": False,
    "all_null": False,
    "mixed": True,
    "all_unparseable": False,
    "all_parseable": True,
    "one_row": True,
}
# group_key stringifies its sibling (a null cell included), so only zero rows is idle.
_GROUP_KEY = {**_PARSE_GATED, "all_null": True, "all_unparseable": True}

_RAN: dict[str, dict[str, bool]] = {
    "bucket_perturb": _PARSE_GATED,
    "date_shift": _PARSE_GATED,
    "group_key": _GROUP_KEY,
}
_COLUMNS: dict[str, dict[str, Any]] = {
    "bucket_perturb": {
        "name": TARGET,
        "strategy": "bucket_perturb",
        "namespace": "ns",
        "provider_config": {"date_format": _FMT},
    },
    "date_shift": {
        "name": TARGET,
        "strategy": "date_shift",
        "namespace": "ns",
        "provider_config": {"date_format": _FMT},
    },
    "group_key": {
        "name": TARGET,
        "strategy": "group_key",
        "provider_config": {"group_by": SIBLING},
    },
}
_CASES = [pytest.param(op, source, id=f"{op}-{source}") for op in _COLUMNS for source in _SOURCES]


@NEEDS_COMPANION
@pytest.mark.parametrize("operator, source", _CASES)
def test_ran_signal_matrix(operator: str, source: str) -> None:
    expected = _RAN[operator][source]
    values = _SOURCES[source]
    index_kernel, raw_hex_kernel = real_kernels()
    column = _COLUMNS[operator]
    cfg = dict(column["provider_config"])

    sibling = table_for(operator, values).select([SIBLING]) if operator == "group_key" else None
    step = call_step(
        operator,
        cfg=cfg,
        namespace=column.get("namespace"),
        source=pa.array(values, pa.string()),
        sibling=sibling,
        index_kernel=index_kernel,
        raw_hex_kernel=raw_hex_kernel,
    )
    assert step.ran is expected, "kernel call"

    compiled = compile_column(column, table_for(operator, ["2024-01-05", "x"]))
    compiled = dataclasses.replace(compiled, table=table_for(operator, values))
    _, unified = run_unified(compiled, None, index_kernel)
    assert unified.compiled_kernel_executed is expected, "unified evidence"
    assert unified.executed is True

    _, chunked, idle = run_chunked(compiled, None, index_kernel, raw_hex_kernel)
    assert chunked.compiled_kernel_executed is expected, "chunked evidence"
    assert idle == (set() if expected else {TARGET}), "chunked kernel_idle"
