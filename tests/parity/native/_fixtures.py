"""Pinned-oracle parity harness + live-registry golden-matrix fixtures.

Task 0.6 (engine-efficiency program, Phase 0). Reused by every later phase to
prove a native result is LOGICALLY identical to today's pandas result.

The oracle is PINNED (`run_oracle`): `substrate="pandas"`,
`execution_mode="full_frame"`, `auto_chunk=False`. It is the single source of
truth every later phase compares against; it must not drift with routing
changes.

`assert_logical_parity` compares values, row order, nulls, diagnostics
(warnings AND row errors), and the LOGICAL schema EXACTLY, allowing ONLY the
enumerated physical differences passed in. Each allowed difference is specific
(a strategy/backend + the exact normalization) and requires an explicit
decision to add. The default allow-list is the ONE null-typed normalization
`concat_masked_chunks` already performs; it is NOT a generic Arrow-widening
escape hatch. See `tests/parity/SEMANTIC_DIFFERENCES.md`.

`STRATEGY_MATRIX` / `PROVIDER_MATRIX` are generated from the LIVE registries
(`SCALAR_HANDLERS`, `ProviderRegistry.known_providers()`), so a newly added
strategy or provider appears as a case rather than being silently omitted.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.native._provider_class import classify_provider
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.providers_v2 import get_default_registry

_ENGINE_VERSION = "native-parity-harness"

# The native substrate the efficiency program's Phases 1+ build. It is NOT a
# valid substrate yet (`VALID_SUBSTRATES == ("pandas", "polars")`), so routing
# a candidate through it raises `invalid_substrate` today. That is the flip
# switch: when a phase wires native execution for a strategy and it reaches
# logical parity, that strategy's matrix cases start passing. Later phases
# repoint this single constant if the wired token differs.
_NATIVE_SUBSTRATE = "native"


# ---------------------------------------------------------------------------
# LogicalResult: the comparison-ready snapshot of an ExecutionResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogicalResult:
    """What `assert_logical_parity` compares: masked outputs plus diagnostics.

    Constructed from a real run via `from_execution_result`, or directly (the
    harness self-test builds these by hand to inject each divergence kind).
    """

    outputs: dict[str, pa.Table]
    warnings: tuple[QualityWarning, ...] = ()
    row_errors: tuple[RowErrorRecord, ...] = ()

    @classmethod
    def from_execution_result(cls, result: ExecutionResult) -> LogicalResult:
        return cls(
            outputs=dict(result.outputs),
            warnings=tuple(result.warnings),
            row_errors=tuple(result.row_errors),
        )


# ---------------------------------------------------------------------------
# Enumerated physical differences (the allow-list)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhysicalDiff:
    """One explicitly-decided, specific physical difference the comparison
    tolerates. `predicate(oracle_type, candidate_type)` returns True only for
    the exact type transition this entry names; `decision` records why it is
    lossless and its `SEMANTIC_DIFFERENCES.md` grounding.
    """

    name: str
    decision: str
    predicate: Callable[[pa.DataType, pa.DataType], bool]

    def allows(self, oracle_type: pa.DataType, candidate_type: pa.DataType) -> bool:
        return self.predicate(oracle_type, candidate_type)


def _is_null_typed_normalization(oracle_type: pa.DataType, candidate_type: pa.DataType) -> bool:
    # Exactly one side is the Arrow `null` type; the other is a concrete type.
    # (If BOTH were null-typed the types would be equal and the gate is never
    # reached; the value check has already proven every value is null.)
    return pa.types.is_null(oracle_type) != pa.types.is_null(candidate_type)


NULL_TYPED_NORMALIZATION = PhysicalDiff(
    name="null_typed_normalization",
    decision=(
        "concat_masked_chunks (execution/_chunked.py): a chunk whose column is "
        "entirely null returns from pandas as Arrow `null` type, while the full "
        "frame infers the concrete type its non-null chunks agree on. Casting "
        "the null-typed column to that concrete type is lossless (every value is "
        "null) and lands exactly where whole-frame inference does. Grounded in "
        "SEMANTIC_DIFFERENCES.md rows v1/v2 (Arrow type width is representational, "
        "not logical); this is the narrowest instance: a null-typed vs a "
        "concrete-typed all-null column. Not a generic Arrow-widening allowance."
    ),
    predicate=_is_null_typed_normalization,
)

# The DEFAULT allow-list is exactly this one normalization. A width drift
# (string -> large_string, etc.) is NOT admitted by default; a phase that needs
# one must add an explicit PhysicalDiff by decision.
DEFAULT_ALLOWED_PHYSICAL_DIFFS: tuple[PhysicalDiff, ...] = (NULL_TYPED_NORMALIZATION,)


# ---------------------------------------------------------------------------
# The pinned oracle and the native candidate
# ---------------------------------------------------------------------------


def run_oracle(config: dict[str, Any], sources: Mapping[str, pa.Table]) -> LogicalResult:
    """Run `config` on the PINNED pandas oracle: `substrate="pandas"`,
    `execution_mode="full_frame"`, `auto_chunk=False`. The single ground truth
    every later phase diffs against; it must not drift with routing changes.
    """
    result = run_pipeline(
        config,
        dict(sources),
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
    )
    return LogicalResult.from_execution_result(result)


def run_candidate(config: dict[str, Any], sources: Mapping[str, pa.Table]) -> LogicalResult:
    """Run `config` on the NATIVE substrate, pinned like the oracle in every
    other dimension so only the substrate differs. Raises `invalid_substrate`
    until a phase wires native execution; that is the intended flip switch.
    """
    result = run_pipeline(
        config,
        dict(sources),
        engine_version=_ENGINE_VERSION,
        substrate=_NATIVE_SUBSTRATE,
        execution_mode="full_frame",
        auto_chunk=False,
    )
    return LogicalResult.from_execution_result(result)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def _warning_key(warning: QualityWarning) -> tuple[Any, ...]:
    return (
        "warning",
        warning.code,
        warning.provider,
        warning.column,
        json.dumps(warning.detail, sort_keys=True, default=repr),
    )


def _row_error_key(record: RowErrorRecord) -> tuple[Any, ...]:
    return (
        "row_error",
        record.table,
        record.column,
        record.row_index,
        record.trigger,
        record.reason,
    )


def _assert_diagnostics_equal(
    candidate_keys: list[tuple[Any, ...]], oracle_keys: list[tuple[Any, ...]], label: str
) -> None:
    # Order-independent multiset equality catches a missing OR an extra
    # diagnostic (both change the multiset), without pinning emission order.
    got, want = Counter(candidate_keys), Counter(oracle_keys)
    if got != want:
        missing = list((want - got).elements())
        extra = list((got - want).elements())
        raise AssertionError(
            f"{label} differ: missing (in oracle, not candidate)={missing} "
            f"extra (in candidate, not oracle)={extra}"
        )


def _assert_table_parity(
    candidate: pa.Table,
    oracle: pa.Table,
    table: str,
    allowed_physical_diffs: tuple[PhysicalDiff, ...],
) -> None:
    # LOGICAL schema: column names in EXACT order.
    if candidate.column_names != oracle.column_names:
        raise AssertionError(
            f"{table}: logical schema differs (column names/order): "
            f"candidate={candidate.column_names} oracle={oracle.column_names}"
        )
    oracle_values = oracle.to_pydict()
    candidate_values = candidate.to_pydict()
    for name in oracle.column_names:
        # Values + null positions + row order (positional Python-scalar
        # equality; None marks a null). Catches a differing value, a reordered
        # row, and a null-vs-value swap.
        if candidate_values[name] != oracle_values[name]:
            raise AssertionError(
                f"{table}.{name}: values/nulls/row-order diverge\n"
                f"  oracle={oracle_values[name]}\n  candidate={candidate_values[name]}"
            )
        # Physical Arrow type: EXACT, unless an enumerated diff allows it.
        oracle_type = oracle.schema.field(name).type
        candidate_type = candidate.schema.field(name).type
        if not oracle_type.equals(candidate_type):
            if not any(d.allows(oracle_type, candidate_type) for d in allowed_physical_diffs):
                allowed = ", ".join(d.name for d in allowed_physical_diffs) or "(none)"
                raise AssertionError(
                    f"{table}.{name}: un-enumerated Arrow type difference "
                    f"oracle={oracle_type} candidate={candidate_type}; enumerated "
                    f"allowances={allowed}. Add an explicit PhysicalDiff by "
                    "decision to admit it, or fix the divergence."
                )


def assert_logical_parity(
    candidate: LogicalResult,
    oracle: LogicalResult,
    *,
    allowed_physical_diffs: tuple[PhysicalDiff, ...] = DEFAULT_ALLOWED_PHYSICAL_DIFFS,
) -> None:
    """Assert `candidate` is LOGICALLY identical to the pinned `oracle`.

    Compares EXACTLY: output-table set, per-column values, null positions, row
    order, diagnostics (warnings AND row errors), and the logical schema
    (column names + order). Physical Arrow-type differences are rejected unless
    named in `allowed_physical_diffs`. Raises `AssertionError` on any
    divergence.
    """
    candidate_tables, oracle_tables = set(candidate.outputs), set(oracle.outputs)
    if candidate_tables != oracle_tables:
        raise AssertionError(
            f"output tables differ: candidate={sorted(candidate_tables)} "
            f"oracle={sorted(oracle_tables)}"
        )
    _assert_diagnostics_equal(
        [_warning_key(w) for w in candidate.warnings],
        [_warning_key(w) for w in oracle.warnings],
        "warnings",
    )
    _assert_diagnostics_equal(
        [_row_error_key(r) for r in candidate.row_errors],
        [_row_error_key(r) for r in oracle.row_errors],
        "row errors",
    )
    for table in sorted(oracle_tables):
        _assert_table_parity(
            candidate.outputs[table], oracle.outputs[table], table, allowed_physical_diffs
        )


# ---------------------------------------------------------------------------
# The golden matrix, generated from the LIVE registries
# ---------------------------------------------------------------------------

# One module-scoped temp dir holds the materialized source files. The oracle
# profiles the DECLARED source path (not the in-memory table), so each case's
# source is written to disk once and the same table is also passed in-memory.
_SOURCE_DIR = tempfile.mkdtemp(prefix="native-parity-src-")


@dataclass(frozen=True)
class MatrixCase:
    """One enumerated parity case: a validated config + its in-memory source.

    `oracle_runnable` marks the cases the pinned oracle can execute today (a
    minimal valid single-column config exists). Cases without one are still
    enumerated so the registry stays fully covered; they carry a config that
    will run once their strategy/provider has a native binding.
    """

    case_id: str
    table: str
    strategy: str
    provider: str | None
    variant: str
    config: dict[str, Any]
    sources: dict[str, pa.Table]
    oracle_runnable: bool
    allowed_physical_diffs: tuple[PhysicalDiff, ...] = DEFAULT_ALLOWED_PHYSICAL_DIFFS


def _materialize(key: str, source: pa.Table) -> str:
    # A UNIQUE path per case: every case declares table "t", so a shared
    # filename would let cases clobber each other's source on disk (the oracle
    # profiles the DECLARED path, so the last writer would win for all).
    path = f"{_SOURCE_DIR}/{key}.parquet"
    pq.write_table(source, path)
    return path


def _build_config(table: str, key: str, column: dict[str, Any], source: pa.Table) -> dict[str, Any]:
    path = _materialize(key, source)
    raw = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {table: {"type": "file", "format": "parquet", "path": path}},
        "targets": {table: {"type": "file", "format": "parquet", "path": f"{path}.out"}},
        "tables": [{"name": table, "columns": [column]}],
    }
    return PipelineConfig.model_validate(raw).model_dump()


# Per-strategy minimal specs. Each entry: (column-config extras, source values,
# arrow type, variant tag). Absent strategies are enumerated below as
# non-runnable cases (whole-column / composite shapes with no single-column
# minimal fixture; they are native-migration-deferred regardless).
_STR = pa.string()
_INT = pa.int64()

_STRATEGY_SPECS: dict[str, list[tuple[dict[str, Any], list[object], pa.DataType, str]]] = {
    "passthrough": [({}, ["a", None, "b"], _STR, "null")],
    "redact": [({}, ["a", None, "b"], _STR, "null")],
    "truncate": [({"provider_config": {"length": 3}}, ["12345", "6789", None], _STR, "null")],
    "hash": [
        ({"namespace": "h_ns"}, ["a", None, "b"], _STR, "null"),
        ({"namespace": "h_ns"}, [1, 22, 333], _INT, "dtype_int"),
    ],
    "fpe": [
        (
            {
                "deterministic": True,
                "namespace": "fpe_ns",
                "provider_config": {"charset": "digits"},
            },
            ["12345", "67890", "00001"],
            _STR,
            "seed_deterministic",
        )
    ],
    "date_shift": [
        (
            {"namespace": "ds_ns", "provider_config": {"min_days": -30, "max_days": 30}},
            ["2020-01-15", None, "2019-06-30"],
            _STR,
            "null",
        )
    ],
    "text_redact": [({}, ["email a@b.com here", None, "no pii"], _STR, "null")],
    "bucketize": [({"provider_config": {"width": 10}}, [3, 17, 42], _INT, "dtype_int")],
    "shuffle": [
        (
            {"namespace": "s_ns", "deterministic": True},
            ["a", "b", "c", "d"],
            _STR,
            "seed_deterministic",
        )
    ],
    "categorical": [
        (
            {
                "deterministic": True,
                "namespace": "cat_ns",
                "provider_config": {"categories": ["A", "B", "C"]},
            },
            ["a", None, "b"],
            _STR,
            "seed_deterministic",
        )
    ],
    "formula": [
        ({"provider_config": {"formula": "value.upper()"}}, ["alpha", "beta"], _STR, "no_null")
    ],
    "faker": [
        (
            {
                "provider": "person_email",
                "deterministic": True,
                "namespace": "fk_ns",
                "provider_config": {"pool_size": 16},
            },
            ["a", None, "b"],
            _STR,
            "seed_deterministic",
        )
    ],
}


def _strategy_matrix() -> list[MatrixCase]:
    cases: list[MatrixCase] = []
    for strategy in sorted(SCALAR_HANDLERS):
        specs = _STRATEGY_SPECS.get(strategy)
        if not specs:
            # Enumerated but no minimal single-column fixture (whole-column or
            # composite shapes): keep the registry fully covered. Marked
            # non-runnable so the oracle smoke skips it.
            source = pa.table({"c": pa.array(["a", "b"], type=_STR)})
            cases.append(
                MatrixCase(
                    case_id=f"strategy/{strategy}/enumerated-only",
                    table="t",
                    strategy=strategy,
                    provider=None,
                    variant="enumerated_only",
                    config={"__no_minimal_fixture__": True, "tables": [{"name": "t"}]},
                    sources={"t": source},
                    oracle_runnable=False,
                )
            )
            continue
        for extras, values, arrow_type, variant in specs:
            column = {"name": "c", "strategy": strategy, **extras}
            source = pa.table({"c": pa.array(values, type=arrow_type)})
            config = _build_config("t", f"strat_{strategy}_{variant}", column, source)
            cases.append(
                MatrixCase(
                    case_id=f"strategy/{strategy}/{variant}",
                    table="t",
                    strategy=strategy,
                    provider=extras.get("provider"),
                    variant=variant,
                    config=config,
                    sources={"t": source},
                    oracle_runnable=True,
                )
            )
    return cases


def _provider_matrix() -> list[MatrixCase]:
    registry = get_default_registry()
    cases: list[MatrixCase] = []
    for provider in sorted(registry.known_providers()):
        caps = registry.get_capabilities(provider)
        provider_class = classify_provider(provider, {})
        # A POOLABLE faker-backed provider runs through the `faker` strategy
        # today, so the oracle can execute it. Non-poolable faker providers
        # (uuid, lorem_text, ...) reject at plan-compile under `faker`, and
        # non-faker bindings (decoy_native identifiers, composites) invoke via
        # other strategies; both are enumerated (so a new provider appears) but
        # not oracle-run here.
        if caps.backend_type == "faker" and caps.poolable:
            column = {
                "name": "c",
                "strategy": "faker",
                "provider": provider,
                "deterministic": True,
                "namespace": f"pv_{provider}",
                "provider_config": {"pool_size": 16},
            }
            source = pa.table({"c": pa.array(["a", None, "b"], type=_STR)})
            config = _build_config("t", f"prov_{provider}", column, source)
            runnable = True
            sources = {"t": source}
        else:
            column = {"name": "c", "strategy": "faker", "provider": provider}
            source = pa.table({"c": pa.array(["a", "b"], type=_STR)})
            config = {"__no_minimal_fixture__": True, "tables": [{"name": "t"}]}
            runnable = False
            sources = {"t": source}
        cases.append(
            MatrixCase(
                case_id=f"provider/{provider}/{caps.backend_type}/{provider_class}",
                table="t",
                strategy="faker",
                provider=provider,
                variant=f"backend_{caps.backend_type}",
                config=config,
                sources=sources,
                oracle_runnable=runnable,
            )
        )
    return cases


STRATEGY_MATRIX: list[MatrixCase] = _strategy_matrix()
PROVIDER_MATRIX: list[MatrixCase] = _provider_matrix()
