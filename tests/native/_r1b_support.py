"""Shared harness for the R1b cross-route tests.

Both native routes (the unified `run_operator` and the chunked `_mask_chunk_native`) are driven
from ONE compiled plan, so each example hands the two routes the same config, the same source
and the same keys, and the kernel-argument recorder sees exactly what each route asked the
compiled kernel to do.

Kernel names are patched in every module that may hold the call site (the adapters today, the
shared step once it exists) with `raising=False` semantics, and the recorder keeps ONE list, so
a call is counted once no matter which module it went through.
"""

from __future__ import annotations

import atexit
import contextlib
import importlib
import inspect
import itertools
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution.native import _chunk_masking
from decoy_engine.execution.native._categorical_prepared import prepare_chunked_categoricals
from decoy_engine.execution.native._chunk_masking import _resolve_faker_pools
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import ExecutionBinding
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.generation.pool import PoolCache
from tests.native._chunked_entry_support import MASK_KEY
from tests.physical._shadow_helpers import build_config

CALL_SITE_MODULES = (
    "decoy_engine.execution.physical._shadow_operators",
    "decoy_engine.execution.native._chunk_masking",
    "decoy_engine.execution.native._operator_step",
)
KERNEL_NAMES = (
    "native_passthrough",
    "native_redact",
    "native_truncate",
    "native_keyed_hash",
    "sample_faker_array",
    "native_categorical",
    "native_categorical_positional",
    "native_bucket_perturb",
    "native_group_key",
    "native_date_shift",
)

ENGINE_VERSION = "r1b-cross-route"
TARGET = "c"
SIBLING = "g"
NATIVE_THREADS = 3

# Sentinels stand in for the compiled kernels: the argument tests never run them.
INDEX_KERNEL: Any = object()
RAW_HEX_KERNEL: Any = object()


@dataclass(frozen=True)
class Recorded:
    kernel: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _normalize(value: Any, pool: Any = None) -> Any:
    if value is INDEX_KERNEL:
        return "<index_kernel>"
    if value is RAW_HEX_KERNEL:
        return "<raw_hex_kernel>"
    if isinstance(value, list):
        return "<derive_calls>"
    if pool is not None and value is pool:
        return "<pool>"
    return value


def _stub_result(kernel: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    source = args[0] if args else kwargs["source"]
    rows = source.num_rows if isinstance(source, pa.Table) else len(source)
    out = pa.array(["x"] * rows, pa.string())
    derive_calls = kwargs.get("derive_calls")
    if isinstance(derive_calls, list):
        derive_calls.append(1)
    if kernel == "native_date_shift":
        return out, ()
    return out


@contextlib.contextmanager
def recording_kernels(pool: Any = None) -> Iterator[list[Recorded]]:
    """Replace every kernel name in every call-site module with a recording stub."""
    calls: list[Recorded] = []

    def make(kernel: str) -> Any:
        def stub(*args: Any, **kwargs: Any) -> Any:
            calls.append(
                Recorded(
                    kernel,
                    tuple(_normalize(a, pool) for a in args[1:]),
                    {k: _normalize(v, pool) for k, v in kwargs.items()},
                )
            )
            return _stub_result(kernel, args, kwargs)

        return stub

    with contextlib.ExitStack() as stack:
        for module_name in CALL_SITE_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError:
                continue
            for kernel in KERNEL_NAMES:
                if hasattr(module, kernel):
                    stack.enter_context(mock.patch.object(module, kernel, make(kernel)))
        yield calls


@dataclass(frozen=True)
class Compiled:
    binding: ExecutionBinding
    col_seed_by_name: dict[str, Any]
    job_seed: bytes
    table: pa.Table


def table_for(strategy: str, values: list[Any], sibling: list[Any] | None = None) -> pa.Table:
    """A one-column source, plus the group_by sibling for group_key."""
    data: dict[str, Any] = {TARGET: pa.array(values, pa.string())}
    if strategy == "group_key":
        data[SIBLING] = pa.array(sibling if sibling is not None else values, pa.string())
    return pa.table(data)


def columns_for(column: dict[str, Any]) -> list[dict[str, Any]]:
    if column["strategy"] != "group_key":
        return [column]
    return [{"name": SIBLING, "strategy": "passthrough"}, column]


_WORKDIR = Path(tempfile.mkdtemp(prefix="r1b-support-"))
atexit.register(shutil.rmtree, _WORKDIR, ignore_errors=True)
_COUNTER = itertools.count()


def compile_column(column: dict[str, Any], table: pa.Table) -> Compiled:
    """Compile a one-table plan; profiling reads the source, so it is written to disk first."""
    path = _WORKDIR / f"src{next(_COUNTER)}.parquet"
    pq.write_table(table, path)
    config = build_config(_WORKDIR, "t", path, columns_for(column))
    inputs = capture_physical_plan_inputs(config, {"t": table}, engine_version=ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    node = next(n for tbl in plan.tables for n in tbl.nodes if TARGET in n.columns)
    assert node.execution is not None, f"{column['strategy']} config did not bind: {column}"
    table_seed = next(ts for (name, ts) in inputs.plan.seed_envelope.per_table if name == "t")
    return Compiled(
        binding=node.execution,
        col_seed_by_name=dict(table_seed.per_column),
        job_seed=inputs.plan.seed_envelope.job_seed,
        table=table,
    )


def pools_for(compiled: Compiled) -> dict[str, Any]:
    return _resolve_faker_pools(
        compiled.col_seed_by_name, job_seed=compiled.job_seed, pool_cache=PoolCache()
    )


def run_unified(compiled: Compiled, pool: Any = None, index_kernel: Any = INDEX_KERNEL) -> Any:
    table = compiled.table
    is_group_key = SIBLING in table.schema.names
    array = table.column(SIBLING if is_group_key else TARGET).combine_chunks()
    ctx = SimpleNamespace(mask_key=MASK_KEY, native_threads=NATIVE_THREADS)
    evidence = OperatorCallEvidence(planned_operator=compiled.binding.operator_id)
    out, _ = run_operator(
        array,
        binding=compiled.binding,
        ctx=ctx,  # type: ignore[arg-type]
        evidence=evidence,
        pool=pool,
        index_kernel=index_kernel,
        group_key_sibling=table.select([SIBLING]) if is_group_key else None,
        column=TARGET,
    )
    return out, evidence


def chunk_evidence() -> Any:
    return SimpleNamespace(
        compiled_kernel_executed=False,
        pool_select_executed=False,
        pool_select_calls=0,
        kernel_calls={},
        kernel_elapsed_s={},
    )


def chunked_route_kwargs(compiled: Compiled) -> dict[str, Any]:
    """The route-specific preparation argument, whichever shape `_mask_chunk_native` takes."""
    categoricals = prepare_chunked_categoricals(compiled.col_seed_by_name, compiled.table.schema)
    parameters = inspect.signature(_chunk_masking._mask_chunk_native).parameters
    if "params_by_column" in parameters:
        from decoy_engine.execution.native._operator_params import resolve_params_by_column

        return {
            "params_by_column": resolve_params_by_column(
                compiled.col_seed_by_name,
                prepared_categoricals=categoricals,
                excluded=frozenset(),
            )
        }
    return {"categorical_by_column": categoricals}


def run_chunked(
    compiled: Compiled,
    pools: dict[str, Any] | None = None,
    index_kernel: Any = INDEX_KERNEL,
    raw_hex_kernel: Any = RAW_HEX_KERNEL,
) -> Any:
    table = compiled.table
    evidence = chunk_evidence()
    format_errors: dict[str, tuple[int, ...]] = {}
    kernel_idle: set[str] = set()
    out = _chunk_masking._mask_chunk_native(
        table.select([TARGET]),
        col_seed_by_name=compiled.col_seed_by_name,
        mask_key=MASK_KEY,
        evidence=evidence,
        pool_by_column=pools or {},
        native_threads=NATIVE_THREADS,
        index_kernel=index_kernel,
        kernel_idle=kernel_idle,
        format_errors=format_errors,
        raw_chunk=table,
        raw_hex_kernel=raw_hex_kernel,
        **chunked_route_kwargs(compiled),
    )
    return out, evidence, kernel_idle


@dataclass(frozen=True)
class StepView:
    out: Any
    ran: bool | None
    positions: tuple[int, ...] = ()


def real_kernels() -> tuple[Any, Any]:
    """The compiled index and raw-hex kernels (callers skip when the companion is absent)."""
    from decoy_engine.execution.native._group_key_ext import load_compiled_raw_hex_kernel
    from decoy_engine.execution.native._index_ext import load_compiled_index_kernel

    return load_compiled_index_kernel(), load_compiled_raw_hex_kernel()


def call_step(
    strategy: str,
    *,
    cfg: dict[str, Any],
    namespace: str | None,
    source: pa.Array,
    sibling: pa.Table | None = None,
    index_kernel: Any = INDEX_KERNEL,
    raw_hex_kernel: Any = RAW_HEX_KERNEL,
) -> StepView:
    """One column of one batch through the shared kernel step, bypassing both routes."""
    from decoy_engine.execution.native._categorical_prepared import PreparedCategorical
    from decoy_engine.execution.native._operator_params import resolve_operator_params
    from decoy_engine.execution.native._operator_step import run_kernel_step

    prepared = (
        PreparedCategorical(tuple(cfg["categories"]), None) if strategy == "categorical" else None
    )
    params = resolve_operator_params(
        strategy,
        target=TARGET,
        provider_config=cfg,
        namespace=namespace,
        prepared_categorical=prepared,
    )
    result = run_kernel_step(
        params,
        source,
        mask_key=MASK_KEY,
        native_threads=1,
        index_kernel=index_kernel,
        raw_hex_kernel=raw_hex_kernel,
        sibling=sibling,
    )
    return StepView(result.out, result.ran, result.format_error_positions)
