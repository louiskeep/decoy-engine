"""Phase D / D3a: job validators on the bounded out-of-core route.

Plan: docs/plans/2026-10-15-d3a-validators-bounded-ooc.md (acceptance matrix T1-T7).

A RAM-fitting pure-mask FK job that declares per-cell validators on route-stable string
columns now runs out-of-core, checking each masked batch and assembling the same
`ValidationReport` the full-frame oracle builds. Every expectation below is compared against an
independent full-frame run or a literal, never against the output under test. The measured
`elapsed_ms` is the one field excluded from report equality.
"""

from __future__ import annotations

import dataclasses
import inspect
import math
import shutil
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError, ValidatorFailedError
from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
from decoy_engine.execution import _pipeline_route_exec as route_exec
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution.out_of_core import _runner, _validate_batch
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.execution.out_of_core._validate_batch import (
    admitted_validators,
    validators_ooc_safe,
)
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.validators import _registry
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER
from tests.parity import _strict_compare as sc

pytestmark = pytest.mark.filterwarnings("ignore")

BUDGET = 1024 * 1024 * 1024
FREE_DISK = 200 * 1024 * 1024 * 1024
_Usage = namedtuple("_Usage", "total used free")
_OOC = ("out_of_core", "byte_estimate_bounded_out_of_core")
N = 30


# ---------------------------------------------------------------------------
# Job construction
# ---------------------------------------------------------------------------


def _luhn_ok(body: str) -> str:
    """`body` plus the check digit that makes it Luhn-valid."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return body + str((10 - total % 10) % 10)


_NPI_OK, _NPI_BAD = "1234567893", "1234567890"
_IBAN_OK, _IBAN_BAD = "GB82WEST12345698765432", "GB00WEST12345698765432"
_VIN_OK, _VIN_BAD = "1M8GDM9AXKP042788", "1M8GDM9A0KP042788"


def _h(name: str, ns: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": ns, **extra}


def _pass(name: str, ns: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough", "namespace": ns, **extra}


def _trunc(name: str, ns: str, length: int = 3) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "truncate",
        "namespace": ns,
        "provider_config": {"length": length},
    }


def _redact(name: str, ns: str, with_: Any = "XXX") -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "redact",
        "namespace": ns,
        "provider_config": {"redact_with": with_},
    }


def _cat(name: str, ns: str, categories: list[Any]) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "categorical",
        "namespace": ns,
        "deterministic": True,
        "provider_config": {"categories": categories},
    }


def job(
    tmp: Path,
    tables: dict[str, pa.Table],
    columns: dict[str, list[dict[str, Any]]],
    validators: list[dict[str, Any]] | None,
    **extra: Any,
) -> dict[str, Any]:
    """parent.id -> child.parent_id (hash namespace `ns`), plus the given columns/validators."""
    src = {}
    for name, table in tables.items():
        path = tmp / f"{name}.parquet"
        pq.write_table(table, path)
        src[name] = {"type": "file", "path": str(path), "format": "parquet"}
    cfg: dict[str, Any] = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": src,
        "targets": {
            n: {"type": "file", "format": "parquet", "path": str(tmp / f"{n}.out.parquet")}
            for n in tables
        },
        "tables": [{"name": n, "columns": cols} for n, cols in columns.items()],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    if validators is not None:
        cfg["validators"] = validators
    cfg.update(extra)
    return cfg


def _tables(n: int = N) -> dict[str, pa.Table]:
    ids = [f"p{i}" for i in range(n)]
    pans = [
        None if i % 7 == 3 else (_luhn_ok(f"4{i:014d}") if i % 3 else f"4{i:014d}9")
        for i in range(n)
    ]
    return {
        "parent": pa.table(
            {
                "id": pa.array(ids, pa.string()),
                "pan": pa.array(pans, pa.string()),
                "npi": pa.array([_NPI_OK if i % 4 else _NPI_BAD for i in range(n)], pa.string()),
                "iban": pa.array([_IBAN_OK if i % 5 else _IBAN_BAD for i in range(n)], pa.string()),
                "vin": pa.array([_VIN_OK if i % 6 else _VIN_BAD for i in range(n)], pa.string()),
                "code": pa.array(
                    [("ab" if i % 5 else "Z9") + f"x{i}" for i in range(n)], pa.string()
                ),
                "region": pa.array([f"r{i % 3}" for i in range(n)], pa.string()),
                "empty": pa.array([None] * n, pa.string()),
            }
        ),
        "child": pa.table(
            {
                "cid": pa.array([f"c{i}" for i in range(n)], pa.string()),
                "parent_id": pa.array(ids, pa.string()),
                "memo": pa.array(
                    [None if i % 11 == 4 else f"m{i % 6}" for i in range(n)], pa.string()
                ),
            }
        ),
    }


def _columns() -> dict[str, list[dict[str, Any]]]:
    return {
        "parent": [
            _h("id", "ns"),
            _pass("pan", "pan_ns"),
            _pass("npi", "npi_ns"),
            _pass("iban", "iban_ns"),
            _pass("vin", "vin_ns"),
            _trunc("code", "code_ns"),
            _redact("region", "region_ns"),
            _pass("empty", "empty_ns"),
        ],
        "child": [_h("cid", "cns"), _h("parent_id", "ns"), _pass("memo", "memo_ns")],
    }


def _v(name: str, columns: dict[str, list[str]], **params: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "columns": columns}
    if params:
        entry["params"] = params
    return entry


def standard_validators() -> list[dict[str, Any]]:
    """Every allowlisted validator; failures in both tables and across batches."""
    return [
        _v("luhn", {"parent": ["pan"]}),
        _v("npi", {"parent": ["npi"]}),
        _v("iban", {"parent": ["iban"]}),
        _v("vin", {"parent": ["vin"]}),
        _v("regex_match", {"parent": ["code"]}, pattern="^[a-z]+$"),
        _v("column_in_set", {"child": ["memo"]}, allowed_values=["m0", "m1", "m2", "m3"]),
    ]


def standard(
    tmp: Path, validators: list[dict[str, Any]] | None = None, **extra: Any
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    tables = _tables()
    vs = standard_validators() if validators is None else validators
    return job(tmp, tables, _columns(), vs, **extra), tables


def run(cfg: dict[str, Any], tables: dict[str, pa.Table], *, full: bool = False, **kw: Any) -> Any:
    kwargs: dict[str, Any] = {"engine_version": "d3a", "key_provider": THROWAWAY_KEY_PROVIDER}
    kwargs.update(
        {"execution_mode": "full_frame"} if full else {"out_of_core_budget_bytes": BUDGET}
    )
    kwargs.update(kw)
    return run_pipeline(cfg, tables, **kwargs)


def mode(result: Any) -> tuple[str, str]:
    ex = result.quality_metrics["execution"]
    return ex["execution_mode"], ex["route_reason"]


def outcome(
    cfg: dict[str, Any], tables: dict[str, pa.Table], *, full: bool = False, **kw: Any
) -> tuple[str, Any]:
    """("ok", validation metrics) or ("failed", report), elapsed_ms zeroed for equality."""
    try:
        result = run(cfg, tables, full=full, **kw)
    except ValidatorFailedError as exc:
        return "failed", dataclasses.replace(exc.report, elapsed_ms=0.0)
    metrics = result.quality_metrics.get("validation")
    if metrics is not None:
        metrics = {"validators": {**metrics["validators"], "elapsed_ms": 0.0}}
    return "ok", metrics


def fixed_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Shutil:
        def disk_usage(self, path: Any) -> Any:
            return _Usage(FREE_DISK * 2, FREE_DISK, FREE_DISK)

        def __getattr__(self, name: str) -> Any:
            return getattr(shutil, name)

    monkeypatch.setattr(route_exec, "shutil", _Shutil())


_WITNESS = SimpleNamespace(calls=0, batch_rows=None)


@pytest.fixture(autouse=True)
def _ooc_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts out-of-core runs so a parity test cannot silently compare full-frame to itself."""
    import decoy_engine.execution.out_of_core as ooc_pkg

    _WITNESS.calls, _WITNESS.batch_rows = 0, None
    real = ooc_pkg.run_fk_out_of_core

    def counted(*a: Any, **k: Any) -> Any:
        _WITNESS.calls += 1
        if _WITNESS.batch_rows is not None:
            k = {**k, "batch_rows": _WITNESS.batch_rows}
        return real(*a, **k)

    monkeypatch.setattr(ooc_pkg, "run_fk_out_of_core", counted)


def small_batches(monkeypatch: pytest.MonkeyPatch, rows: int) -> None:
    _WITNESS.batch_rows = rows


class DriverSpy:
    """Records which per-table driver ran (the batch driver or the reorder/sink driver)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reorder: list[str] = []
        self.batch: list[str] = []
        real_stream, real_batch = _runner.stream_table, _runner._stream_table

        def stream(**kw: Any) -> Any:
            self.reorder.append(kw["table_name"])
            return real_stream(**kw)

        def batch(**kw: Any) -> Any:
            self.batch.append(kw["table_name"])
            return real_batch(**kw)

        monkeypatch.setattr(_runner, "stream_table", stream)
        monkeypatch.setattr(_runner, "_stream_table", batch)
        init = StreamFkJoiner.__init__
        self.reorder_joiners = 0

        def r_init(this: Any, *a: Any, **k: Any) -> None:
            self.reorder_joiners += 1
            init(this, *a, **k)

        monkeypatch.setattr(StreamFkJoiner, "__init__", r_init)


class CountingSink(ParquetTransactionalSink):
    """A real sink that counts the publish/abort calls."""

    def __init__(self, target: Path) -> None:
        super().__init__(target)
        self.commits = 0
        self.aborts = 0
        self.staged: list[str] = []

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.staged.append(table)
        super().write_batches(table, batches, schema=schema)

    def commit(self) -> None:
        self.commits += 1
        super().commit()

    def abort(self) -> None:
        self.aborts += 1
        super().abort()


# ---------------------------------------------------------------------------
# T1: admission and declines
# ---------------------------------------------------------------------------


class _Profile:
    relationships = (object(),)


def _graph() -> RelationshipGraph:
    edge = RelationshipEdge("parent", ("id",), "child", ("parent_id",), "ns", OrphanPolicy.PRESERVE)
    return RelationshipGraph(edges=(edge,), ordering=())


def _cyclic() -> RelationshipGraph:
    e1 = RelationshipEdge("a", ("id",), "b", ("ra",), "na", OrphanPolicy.PRESERVE)
    e2 = RelationshipEdge("b", ("id",), "a", ("rb",), "nb", OrphanPolicy.PRESERVE)
    return RelationshipGraph(edges=(e1, e2), ordering=())


def decide(**kw: Any) -> tuple[str, str]:
    base: dict[str, Any] = {
        "has_generate_table": False,
        "has_mask_table": True,
        "validators": [object()],
        "fidelity_report": False,
        "vault_writer": None,
        "execution_mode": "auto",
        "graph": _graph(),
        "out_of_core_compatible": True,
        "largest_table_rows": 100,
        "validators_ooc_safe": True,
        "full_frame_fits_estimate": True,
    }
    base.update(kw)
    return decide_execution_route(_Profile(), **base)


def test_safe_validator_job_routes_out_of_core_when_it_fits() -> None:
    assert decide() == _OOC


@pytest.mark.parametrize("fits", [False, None])
def test_safe_validator_job_that_does_not_fit_is_rejected(fits: bool | None) -> None:
    # The fit-gate is the coarse RAM envelope: a non-fitting or unpriceable validator job is
    # never admitted to OOC and keeps the existing reject (dropping the gate would admit it).
    with pytest.raises(ExecutionError) as exc:
        decide(full_frame_fits_estimate=fits)
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"


@pytest.mark.parametrize("fits", [False, None])
def test_probe_recovery_routes_full_frame_never_out_of_core(fits: bool | None) -> None:
    assert decide(full_frame_fits_estimate=fits, probe_recovers_full_frame=True) == (
        "full_frame",
        "probe_recovered_full_frame",
    )
    # An inconclusive probe is not a recovery.
    with pytest.raises(ExecutionError):
        decide(full_frame_fits_estimate=fits, probe_recovers_full_frame=False)


def test_probe_flag_off_ignores_a_recovery_signal() -> None:
    with pytest.raises(ExecutionError):
        decide(
            full_frame_fits_estimate=False, probe_recovers_full_frame=True, use_probe_routing=False
        )


def test_unsafe_validator_job_stays_full_frame_when_it_fits() -> None:
    assert decide(validators_ooc_safe=False) == ("full_frame", "byte_estimate_full_frame_fits")


def test_unsafe_validator_job_that_is_too_big_is_still_rejected() -> None:
    with pytest.raises(ExecutionError) as exc:
        decide(validators_ooc_safe=False, full_frame_fits_estimate=False)
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"


def test_the_signal_without_validators_changes_nothing() -> None:
    # No validators: the plain pure-mask job, whatever the stale signal says.
    assert decide(validators=[]) == decide(validators=[], validators_ooc_safe=False)
    with pytest.raises(ExecutionError):
        decide(
            validators=[],
            validators_ooc_safe=True,
            has_mask_table=True,
            fidelity_report=True,
            full_frame_fits_estimate=False,
        )


@pytest.mark.parametrize(
    ("extra", "rejects_when_too_big"),
    [
        ({"out_of_core_compatible": False}, True),  # SEQ-eligible but not OOC-compatible
        ({"fk_ooc_dtype_hazard": True}, True),
        ({"graph": _cyclic()}, True),
        ({"fidelity_report": True}, True),
        ({"post_validation": True}, True),
        ({"resolved_substrate": "polars"}, True),
        ({"vault_writer": object()}, True),
        ({"has_generate_table": True}, False),  # outside the byte-estimate scope entirely
        ({"has_mask_table": False}, False),
    ],
    ids=[
        "ooc-incompatible",
        "fk-dtype-hazard",
        "cyclic",
        "fidelity",
        "post-validation",
        "substrate",
        "vault",
        "generate",
        "no-mask-table",
    ],
)
def test_other_disqualifiers_still_beat_the_validator_admission(
    extra: dict[str, Any], rejects_when_too_big: bool
) -> None:
    # Never out-of-core and never sequential (it has no validator hook): fit -> full-frame.
    assert decide(**extra)[0] == "full_frame"
    if rejects_when_too_big:
        with pytest.raises(ExecutionError):
            decide(full_frame_fits_estimate=False, **extra)


def test_sequential_never_admits_validators() -> None:
    # Neither a fitting nor a too-big validator job may land on the sequential route.
    for fits in (True, False, None):
        try:
            route = decide(validators_ooc_safe=True, full_frame_fits_estimate=fits)[0]
        except ExecutionError:
            continue
        assert route != "sequential"


def test_rollback_flag_off_restores_the_old_validator_routing() -> None:
    assert decide(use_byte_estimate_routing=False) == ("full_frame", "validators_present")
    with pytest.raises(ExecutionError):
        decide(use_byte_estimate_routing=False, largest_table_rows=10**9)


@pytest.mark.parametrize("explicit", ["sequential", "out_of_core"])
def test_explicit_modes_still_refuse_a_validator_job(explicit: str) -> None:
    with pytest.raises(ConfigError):
        decide(execution_mode=explicit)


# --- admission: the validator allowlist and the column allowlist ------------------------


def _seed(strategy: str, cfg: dict[str, Any] | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace="n",
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple((cfg or {}).items()),
        coherent_with=(),
    )


def _plan(seeds: dict[str, ColumnSeed], tables: tuple[str, ...] = ("child",)) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x11" * 8,
            per_table=tuple(
                (t, TableSeed(per_column=tuple(seeds.items()), per_group=())) for t in tables
            ),
        )
    )


_NO_EDGES = RelationshipGraph(edges=(), ordering=())


def _cfg(name: str = "luhn", table: str = "child", *cols: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "columns": {table: list(cols or ("c",))}}
    if name == "regex_match":
        entry["params"] = {"pattern": "x"}
    if name == "column_in_set":
        entry["params"] = {"allowed_values": ["a"]}
    return {"validators": [entry], **extra}


def _admit(
    seed: ColumnSeed,
    arrow_type: pa.DataType | None = None,
    *,
    name: str = "luhn",
    graph: Any = _NO_EDGES,
    **extra: Any,
) -> bool:
    schemas = {"child": pa.schema([("c", arrow_type or pa.string())])}
    return admitted_validators(_cfg(name, **extra), graph, _plan({"c": seed}), schemas) is not None


@pytest.mark.parametrize("name", ["luhn", "npi", "iban", "vin", "regex_match", "column_in_set"])
def test_each_per_cell_validator_is_admitted(name: str) -> None:
    assert _admit(_seed("passthrough"), name=name)


@pytest.mark.parametrize(
    "name",
    [
        "fk_intact",
        "no_orphan_children",
        "leak_check",
        "reconciliation_holds",
        "parent_window_respected",
        "not_a_validator",
        "",
    ],
)
def test_cross_row_deferred_and_unknown_validators_decline(name: str) -> None:
    assert not _admit(_seed("passthrough"), name=name)


def test_a_mixed_job_declines_as_a_whole() -> None:
    cfg = _cfg("luhn")
    cfg["validators"].append({"name": "fk_intact"})
    schemas = {"child": pa.schema([("c", pa.string())])}
    assert admitted_validators(cfg, _NO_EDGES, _plan({"c": _seed("passthrough")}), schemas) is None
    assert not validators_ooc_safe(cfg, _plan({"c": _seed("passthrough")}), _NO_EDGES, {})


@pytest.mark.parametrize("entries", [None, [], "luhn", [None], ["luhn"], [{"columns": {}}]])
def test_malformed_or_absent_validator_blocks_decline(entries: Any) -> None:
    schemas = {"child": pa.schema([("c", pa.string())])}
    cfg = {"validators": entries}
    assert admitted_validators(cfg, _NO_EDGES, _plan({"c": _seed("passthrough")}), schemas) is None


@pytest.mark.parametrize("columns", ["c", ["c"], {"child": "c"}, {"child": [1]}, {1: ["c"]}])
def test_malformed_columns_map_declines(columns: Any) -> None:
    cfg = {"validators": [{"name": "luhn", "columns": columns}]}
    schemas = {"child": pa.schema([("c", pa.string())])}
    assert admitted_validators(cfg, _NO_EDGES, _plan({"c": _seed("passthrough")}), schemas) is None


@pytest.mark.parametrize(
    "seed",
    [
        _seed("truncate", {"length": 3}),
        _seed("redact", {"redact_with": "XXX"}),
        _seed("redact"),
        _seed("passthrough"),
    ],
    ids=["truncate", "redact", "redact-default", "passthrough"],
)
@pytest.mark.parametrize("arrow_type", [pa.string(), pa.large_string()])
def test_route_stable_string_columns_are_admitted(
    seed: ColumnSeed, arrow_type: pa.DataType
) -> None:
    assert _admit(seed, arrow_type)


@pytest.mark.parametrize(
    "seed",
    [
        _seed("hash"),
        _seed("categorical", {"categories": ["a", "b"]}),
        _seed("bucket_perturb"),
        _seed("text_redact"),
        _seed("text_mask"),
        _seed("code_set"),
        _seed("fpe"),
        _seed("faker"),
        _seed("date_shift"),
    ],
    ids=[
        "hash",
        "categorical",
        "bucket_perturb",
        "text_redact",
        "text_mask",
        "code_set",
        "fpe",
        "faker",
        "date_shift",
    ],
)
def test_hash_categorical_and_every_other_strategy_decline(seed: ColumnSeed) -> None:
    # hash/categorical differ natively from the Python oracle under Unicode normalisation, and
    # for categorical that changes WHICH category is selected, so even an ASCII output can differ.
    assert not _admit(seed)


@pytest.mark.parametrize("replacement", [7, 7.0, None, True])
def test_numeric_resolved_output_declines(replacement: Any) -> None:
    assert not _admit(_seed("redact", {"redact_with": replacement}))


@pytest.mark.parametrize(
    "arrow_type",
    [
        pa.int64(),
        pa.float64(),
        pa.bool_(),
        pa.date32(),
        pa.timestamp("us"),
        pa.decimal128(10, 2),
        pa.dictionary(pa.int8(), pa.string()),
        pa.string_view(),
    ],
)
@pytest.mark.parametrize("strategy", ["truncate", "redact", "passthrough"])
def test_non_string_source_declines(strategy: str, arrow_type: pa.DataType) -> None:
    assert not _admit(
        _seed(strategy, {"length": 3} if strategy == "truncate" else None), arrow_type
    )


def test_binary_source_is_admitted_only_when_the_output_is_a_string() -> None:
    assert _admit(_seed("redact"), pa.binary())
    assert not _admit(_seed("passthrough"), pa.binary())  # output stays binary


def test_a_validated_column_without_a_seed_declines() -> None:
    schemas = {"child": pa.schema([("c", pa.string())])}
    assert admitted_validators(_cfg(), _NO_EDGES, _plan({}), schemas) is None


def test_absent_table_or_column_is_a_noop_the_oracle_skips() -> None:
    plan = _plan({"c": _seed("passthrough")})
    assert admitted_validators(_cfg(), _NO_EDGES, plan, {}) is not None
    schemas = {"child": pa.schema([("other", pa.string())])}
    assert admitted_validators(_cfg(), _NO_EDGES, plan, schemas) is not None


def test_a_job_without_validators_is_not_admitted() -> None:
    schemas = {"child": pa.schema([("c", pa.string())])}
    plan = _plan({"c": _seed("passthrough")})
    assert admitted_validators({}, _NO_EDGES, plan, schemas) is None
    assert admitted_validators({"validators": []}, _NO_EDGES, plan, schemas) is None
    assert admitted_validators(None, _NO_EDGES, plan, schemas) is None


# --- incoming child-key columns (single, composite, overlap, multi-parent, self-ref) ------


def _edge(pt: str, pc: tuple[str, ...], ct: str, cc: tuple[str, ...]) -> RelationshipEdge:
    return RelationshipEdge(pt, pc, ct, cc, "ns", OrphanPolicy.PRESERVE)


def _key_admit(validated: str, edges: tuple[RelationshipEdge, ...], table: str = "child") -> bool:
    tabs = ("parent", "child", "mid", "p2")
    cols = {c: _seed("passthrough") for c in ("a", "b", "k")}
    schemas = {t: pa.schema([(c, pa.string()) for c in ("a", "b", "k", "id")]) for t in tabs}
    cfg = {"validators": [{"name": "luhn", "columns": {table: [validated]}}]}
    graph = RelationshipGraph(edges=edges, ordering=())
    return admitted_validators(cfg, graph, _plan(cols, tabs), schemas) is not None


def test_single_child_key_declines_and_the_parent_key_is_admitted() -> None:
    edges = (_edge("parent", ("a",), "child", ("b",)),)
    assert not _key_admit("b", edges, "child")
    assert _key_admit("a", edges, "parent")  # parent keys are final at the mask step
    assert _key_admit("k", edges, "child")


def test_every_component_of_a_composite_child_key_declines() -> None:
    edges = (_edge("parent", ("a", "b"), "child", ("a", "b")),)
    assert not _key_admit("a", edges, "child")
    assert not _key_admit("b", edges, "child")
    assert _key_admit("k", edges, "child")


def test_a_parent_key_that_is_also_a_child_key_declines() -> None:
    edges = (_edge("parent", ("a",), "mid", ("a",)), _edge("mid", ("a",), "child", ("b",)))
    assert not _key_admit("a", edges, "mid")


def test_a_child_key_of_any_of_several_parents_declines() -> None:
    edges = (_edge("parent", ("a",), "child", ("a",)), _edge("p2", ("id",), "child", ("b",)))
    assert not _key_admit("a", edges, "child")
    assert not _key_admit("b", edges, "child")
    assert _key_admit("k", edges, "child")


def test_a_self_referencing_child_key_declines() -> None:
    edges = (_edge("child", ("a",), "child", ("b",)),)
    assert not _key_admit("b", edges, "child")
    assert _key_admit("a", edges, "child")


def test_a_child_key_declines_even_when_absent_from_the_source() -> None:
    edges = (_edge("parent", ("a",), "child", ("b",)),)
    schemas = {"child": pa.schema([("k", pa.string())])}
    cfg = {"validators": [{"name": "luhn", "columns": {"child": ["b"]}}]}
    graph = RelationshipGraph(edges=edges, ordering=())
    assert admitted_validators(cfg, graph, _plan({"k": _seed("passthrough")}), schemas) is None


# --- quarantine coupling ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("quarantine", "admitted"),
    [
        ({"enabled": True, "triggers": ["validation_fail"]}, False),
        ({"enabled": True, "triggers": ["row_error", "validation_fail"]}, False),
        ({"enabled": True, "triggers": ["row_error"]}, True),
        ({"enabled": True, "triggers": []}, True),
        ({"enabled": False, "triggers": ["validation_fail"]}, True),
        ({}, True),
    ],
)
def test_quarantine_coupled_jobs_decline(quarantine: dict[str, Any], admitted: bool) -> None:
    schemas = {"child": pa.schema([("c", pa.string())])}
    plan = _plan({"c": _seed("passthrough")})
    cfg = _cfg(quarantine=quarantine)
    assert (admitted_validators(cfg, _NO_EDGES, plan, schemas) is not None) is admitted


# --- end to end: the real pipeline picks the route -------------------------------------


def test_admitted_job_runs_out_of_core_end_to_end(tmp_path: Path) -> None:
    cfg, tables = standard(
        tmp_path,
        validators=[
            _v(
                "column_in_set",
                {"child": ["memo"]},
                **{"allowed_values": ["m0", "m1", "m2", "m3", "m4", "m5"]},
            )
        ],
    )
    result = run(cfg, tables)
    assert mode(result) == _OOC
    assert result.quality_metrics["validation"]["validators"]["passed"] is True


def _declined_cases(tmp: Path) -> dict[str, tuple[dict[str, Any], dict[str, pa.Table]]]:
    ok = _v(
        "column_in_set", {"child": ["memo"]}, allowed_values=["m0", "m1", "m2", "m3", "m4", "m5"]
    )
    out: dict[str, tuple[dict[str, Any], dict[str, pa.Table]]] = {}

    def build(
        name: str, validators: list[dict[str, Any]], mutate: Any = None, **extra: Any
    ) -> None:
        sub = tmp / name
        sub.mkdir()
        tables, columns = _tables(), _columns()
        if mutate is not None:
            mutate(tables, columns)
        out[name] = (job(sub, tables, columns, validators, **extra), tables)

    build("fk-intact", [{"name": "fk_intact"}])
    build("no-orphan-children", [{"name": "no_orphan_children"}])
    build("mixed", [ok, {"name": "fk_intact"}])
    build(
        "hash-validated",
        [_v("regex_match", {"parent": ["code"]}, pattern=".*")],
        lambda t, c: c["parent"].__setitem__(5, _h("code", "code_ns")),
    )
    build(
        "categorical-validated",
        [_v("column_in_set", {"parent": ["code"]}, allowed_values=["a", "b"])],
        lambda t, c: c["parent"].__setitem__(5, _cat("code", "code_ns", ["a", "b"])),
    )
    build(
        "numeric-redact",
        [_v("regex_match", {"parent": ["region"]}, pattern=".*")],
        lambda t, c: c["parent"].__setitem__(6, _redact("region", "region_ns", 7)),
    )
    build(
        "quarantine",
        [ok],
        quarantine={
            "enabled": True,
            "triggers": ["validation_fail"],
            "output_path": str(tmp / "q.parquet"),
        },
    )

    def child_key(t: dict[str, pa.Table], c: dict[str, list[dict[str, Any]]]) -> None:
        c["parent"][0] = _pass("id", "ns")
        c["child"][1] = _pass("parent_id", "ns")

    build("child-key", [_v("regex_match", {"child": ["parent_id"]}, pattern=".*")], child_key)

    def int_source(t: dict[str, pa.Table], c: dict[str, list[dict[str, Any]]]) -> None:
        t["parent"] = t["parent"].set_column(
            t["parent"].schema.get_field_index("region"), "region", pa.array(range(N), pa.int64())
        )
        c["parent"][6] = _pass("region", "region_ns")

    build("int-source", [_v("regex_match", {"parent": ["region"]}, pattern=".*")], int_source)

    def float_source(t: dict[str, pa.Table], c: dict[str, list[dict[str, Any]]]) -> None:
        vals = [float("nan") if i % 5 == 0 else float(i) for i in range(N)]
        t["parent"] = t["parent"].set_column(
            t["parent"].schema.get_field_index("region"), "region", pa.array(vals, pa.float64())
        )
        c["parent"][6] = _pass("region", "region_ns")

    build(
        "nan-float-source", [_v("regex_match", {"parent": ["region"]}, pattern=".*")], float_source
    )

    def nullable_int(t: dict[str, pa.Table], c: dict[str, list[dict[str, Any]]]) -> None:
        vals = [None if i % 4 == 0 else i for i in range(N)]
        t["parent"] = t["parent"].set_column(
            t["parent"].schema.get_field_index("region"), "region", pa.array(vals, pa.int64())
        )
        c["parent"][6] = _pass("region", "region_ns")

    build(
        "nullable-int-source",
        [_v("regex_match", {"parent": ["region"]}, pattern=".*")],
        nullable_int,
    )
    return out


_DECLINED = [
    "fk-intact",
    "no-orphan-children",
    "mixed",
    "hash-validated",
    "categorical-validated",
    "numeric-redact",
    "quarantine",
    "child-key",
    "int-source",
    "nan-float-source",
    "nullable-int-source",
]


@pytest.mark.parametrize("case", _DECLINED)
def test_declined_jobs_stay_full_frame_and_keep_the_oracle_report(
    case: str, tmp_path: Path
) -> None:
    cfg, tables = _declined_cases(tmp_path)[case]
    got = outcome(cfg, tables)
    assert _WITNESS.calls == 0, f"{case} entered the out-of-core runner"
    assert got == outcome(cfg, tables, full=True)


def test_a_declined_job_that_fails_validation_fails_in_the_full_frame_finalize(
    tmp_path: Path,
) -> None:
    cfg, tables = _declined_cases(tmp_path)["hash-validated"]
    cfg["validators"] = [_v("regex_match", {"parent": ["code"]}, pattern="^zz$")]
    with pytest.raises(ValidatorFailedError):
        run(cfg, tables)
    assert _WITNESS.calls == 0


def test_a_too_big_validator_job_rejects(tmp_path: Path) -> None:
    budget = 64 * 1024 * 1024  # 64 MB floor; 200k rows is over it, so full-frame does not fit.
    rows = 200_000
    ids = [f"p{i}" for i in range(rows)]
    tables = {
        "parent": pa.table({"id": pa.array(ids), "v": pa.array([f"v{i}" for i in range(rows)])}),
        "child": pa.table({"parent_id": pa.array(ids)}),
    }
    columns = {
        "parent": [_h("id", "ns"), _pass("v", "vn")],
        "child": [_h("parent_id", "ns")],
    }
    cfg = job(tmp_path, tables, columns, [_v("regex_match", {"parent": ["v"]}, pattern="v.*")])
    with pytest.raises(ExecutionError) as exc:
        run(cfg, tables, out_of_core_budget_bytes=budget)
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"


def test_the_rollback_flag_runs_an_admitted_job_full_frame(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    ooc = outcome(cfg, tables)
    assert _WITNESS.calls == 1
    rolled_back = outcome(cfg, tables, use_byte_estimate_routing=False)
    assert _WITNESS.calls == 1  # full-frame: the out-of-core runner was not entered
    assert rolled_back == ooc


# ---------------------------------------------------------------------------
# T2: report parity with the full-frame oracle
# ---------------------------------------------------------------------------


def _assert_parity(cfg: dict[str, Any], tables: dict[str, pa.Table], **kw: Any) -> tuple[str, Any]:
    before = _WITNESS.calls
    ooc = outcome(cfg, tables, **kw)
    assert _WITNESS.calls == before + 1, "the job did not take the out-of-core route"
    oracle = outcome(cfg, tables, full=True)
    assert _WITNESS.calls == before + 1, "the oracle run must stay full-frame"
    assert ooc == oracle
    return ooc


@pytest.mark.parametrize("batch_rows", [None, 7, 1])
def test_every_validator_report_equals_the_oracle(
    batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, tables = standard(tmp_path)
    if batch_rows is not None:
        small_batches(monkeypatch, batch_rows)
    kind, report = _assert_parity(cfg, tables)
    assert kind == "failed"
    # Non-trivial: every validator found something, in entry order, with whole-table positions.
    assert [f.validator for f in report.findings] == [
        "luhn",
        "npi",
        "iban",
        "vin",
        "regex_match",
        "column_in_set",
    ]
    assert report.validators_run == ("luhn", "npi", "iban", "vin", "regex_match", "column_in_set")
    code = next(f for f in report.findings if f.validator == "regex_match")
    assert code.failing_row_indices == tuple(range(0, N, 5))
    assert f"{len(code.failing_row_indices)} row(s) in parent.code" in code.detail


def test_the_failing_job_really_ran_out_of_core(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    with pytest.raises(ValidatorFailedError):
        run(cfg, tables)
    assert _WITNESS.calls == 1
    with pytest.raises(ValidatorFailedError):
        run(cfg, tables, full=True)
    assert _WITNESS.calls == 1


def test_failures_spanning_batches_merge_to_whole_table_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_batches(monkeypatch, 7)
    cfg, tables = standard(tmp_path, [_v("regex_match", {"parent": ["code"]}, pattern="^[a-z]+$")])
    kind, report = _assert_parity(cfg, tables)
    (finding,) = report.findings
    assert finding.failing_row_indices == (0, 5, 10, 15, 20, 25)  # four batches of seven
    assert finding.detail.startswith("6 row(s) in parent.code")  # the total, not a batch count


def test_duplicate_entries_with_different_parameters_stay_separate(tmp_path: Path) -> None:
    vs = [
        _v("regex_match", {"parent": ["code"]}, pattern="^[a-z]+$"),
        _v("regex_match", {"parent": ["code"]}, pattern="^ab.$"),
        _v("regex_match", {"parent": ["code"]}, pattern="^[a-z]+$"),
    ]
    cfg, tables = standard(tmp_path, vs)
    kind, report = _assert_parity(cfg, tables)
    assert [f.validator for f in report.findings] == ["regex_match"] * 3
    assert report.validators_run == ("regex_match",)  # names dedup, findings do not
    assert report.findings[0].failing_row_indices == report.findings[2].failing_row_indices
    assert "'^ab.$'" in report.findings[1].detail and "'^ab.$'" not in report.findings[0].detail


def test_a_repeated_column_in_one_entry_is_two_findings(tmp_path: Path) -> None:
    vs = [_v("regex_match", {"parent": ["code", "code"]}, pattern="^[a-z]+$")]
    cfg, tables = standard(tmp_path, vs)
    _, report = _assert_parity(cfg, tables)
    assert len(report.findings) == 2


def test_findings_follow_config_order_not_topological_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The FK topo order is parent -> child; the config lists child first within an entry and
    # across entries, and the report must follow the config.
    vs = [
        _v("column_in_set", {"child": ["memo"], "parent": ["region"]}, allowed_values=["m0", "r0"]),
        _v("luhn", {"child": ["memo"]}),
        _v("luhn", {"parent": ["pan"]}),
    ]
    small_batches(monkeypatch, 4)
    cfg, tables = standard(tmp_path, vs)
    _, report = _assert_parity(cfg, tables)
    order = [(f.validator, f.table, f.column) for f in report.findings]
    assert order[:2] == [
        ("column_in_set", "child", "memo"),
        ("column_in_set", "parent", "region"),
    ]
    assert order[-1] == ("luhn", "parent", "pan")


def test_a_parent_key_column_and_a_payload_column(tmp_path: Path) -> None:
    def keys(c: dict[str, list[dict[str, Any]]]) -> None:
        c["parent"][0] = _pass("id", "ns")  # a passthrough parent key is final at the mask step
        c["child"][1] = _pass("parent_id", "ns")

    tables, columns = _tables(), _columns()
    keys(columns)
    vs = [_v("regex_match", {"parent": ["id", "code"]}, pattern="^p[0-9]$")]
    cfg = job(tmp_path, tables, columns, vs)
    kind, report = _assert_parity(cfg, tables)
    assert {f.column for f in report.findings} == {"id", "code"}
    assert mode(_ok_run(cfg, tables)) == _OOC


def _ok_run(cfg: dict[str, Any], tables: dict[str, pa.Table]) -> Any:
    cfg = {**cfg, "validators": [_v("regex_match", {"parent": ["id"]}, pattern=".*")]}
    return run(cfg, tables)


def test_nulls_are_skipped_and_allow_null_false_fails_them(tmp_path: Path) -> None:
    vs = [
        _v(
            "column_in_set",
            {"child": ["memo"]},
            allowed_values=["m0", "m1", "m2", "m3", "m4", "m5"],
        ),
        _v(
            "column_in_set",
            {"child": ["memo"]},
            allowed_values=["m0", "m1", "m2", "m3", "m4", "m5"],
            allow_null=False,
        ),
    ]
    cfg, tables = standard(tmp_path, vs)
    kind, report = _assert_parity(cfg, tables)
    (finding,) = report.findings  # only the allow_null=false entry fails
    assert finding.failing_row_indices == tuple(i for i in range(N) if i % 11 == 4)
    assert report.validators_run == ("column_in_set",)


def test_an_all_null_column_follows_the_oracle(tmp_path: Path) -> None:
    vs = [
        _v("luhn", {"parent": ["empty"]}),
        _v("column_in_set", {"parent": ["empty"]}, allowed_values=["a"]),
        _v("column_in_set", {"parent": ["empty"]}, allowed_values=["a"], allow_null=False),
    ]
    cfg, tables = standard(tmp_path, vs)
    kind, report = _assert_parity(cfg, tables)
    (finding,) = report.findings
    assert finding.validator == "column_in_set"
    assert finding.failing_row_indices == tuple(range(N))  # not "no findings"


def test_a_passing_job_stamps_the_oracle_validation_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_batches(monkeypatch, 7)
    vs = [
        _v("column_in_set", {"child": ["memo"]}, allowed_values=[f"m{i}" for i in range(6)]),
        _v("regex_match", {"parent": ["code", "region"]}, pattern=".*"),
        _v("regex_match", {"parent": ["code"]}, pattern=".*"),
    ]
    cfg, tables = standard(tmp_path, vs)
    kind, metrics = _assert_parity(cfg, tables)
    assert kind == "ok"
    assert set(metrics) == {"validators"}
    assert metrics["validators"]["passed"] is True
    assert metrics["validators"]["findings"] == ()
    # Both entries are recorded, in first-occurrence order, though neither found anything.
    assert metrics["validators"]["validators_run"] == ("column_in_set", "regex_match")


def test_absent_target_columns_and_tables_are_still_recorded(tmp_path: Path) -> None:
    vs = [
        _v("luhn", {"parent": ["no_such_column"], "ghost": ["x"]}),
        _v("regex_match", {"ghost": ["x"]}, pattern="x"),
    ]
    cfg, tables = standard(tmp_path, vs)
    kind, metrics = _assert_parity(cfg, tables)
    assert kind == "ok"
    assert metrics["validators"]["validators_run"] == ("luhn", "regex_match")
    assert metrics["validators"]["findings"] == ()


def _zero_row_job(tmp: Path, validators: list[dict[str, Any]]) -> tuple[dict[str, Any], Any]:
    tables = {
        "parent": pa.table({"id": pa.array([], pa.string()), "v": pa.array([], pa.string())}),
        "child": pa.table({"parent_id": pa.array([], pa.string())}),
    }
    columns = {"parent": [_h("id", "ns"), _pass("v", "vn")], "child": [_h("parent_id", "ns")]}
    return job(tmp, tables, columns, validators), tables


def test_a_zero_row_table_still_records_the_validator(tmp_path: Path) -> None:
    cfg, tables = _zero_row_job(tmp_path, [_v("regex_match", {"parent": ["v"]}, pattern="x")])
    kind, metrics = _assert_parity(cfg, tables)
    assert kind == "ok" and metrics["validators"]["validators_run"] == ("regex_match",)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (_v("regex_match", {"parent": ["v"]}, pattern="("), "not a valid regex"),
        (_v("regex_match", {"parent": ["v"]}), "requires params.pattern"),
        (_v("column_in_set", {"parent": ["v"]}), "requires params.allowed_values"),
        (
            _v("column_in_set", {"parent": ["v"]}, allowed_values=[]),
            "requires params.allowed_values",
        ),
    ],
)
def test_invalid_parameters_raise_even_with_empty_output(
    entry: dict[str, Any], message: str, tmp_path: Path
) -> None:
    cfg, tables = _zero_row_job(tmp_path, [entry])
    with pytest.raises(ValueError, match=message) as ooc_exc:
        run(cfg, tables)
    with pytest.raises(ValueError, match=message) as oracle_exc:
        run(cfg, tables, full=True)
    assert str(ooc_exc.value) == str(oracle_exc.value)


def test_invalid_parameters_raise_before_any_table_is_streamed(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path, [_v("regex_match", {"parent": ["code"]}, pattern="(")])
    sink = CountingSink(tmp_path / "sink_out")
    with pytest.raises(ValueError, match="not a valid regex"):
        run(cfg, tables, sink=sink)
    assert sink.staged == [] and sink.commits == 0


# ---------------------------------------------------------------------------
# T3: both drivers evaluate (mixed-driver multi-table)
# ---------------------------------------------------------------------------


def test_the_in_memory_driver_evaluates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    spy = DriverSpy(monkeypatch)
    ooc = outcome(cfg, tables)  # no sink: the batch driver for every table
    assert spy.reorder == [] and sorted(spy.batch) == ["child", "parent"]
    monkeypatch.undo()
    assert ooc == outcome(cfg, tables, full=True)
    assert ooc[0] == "failed"


def test_the_mixed_reorder_and_batch_drivers_both_evaluate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    spy = DriverSpy(monkeypatch)
    sink = CountingSink(tmp_path / "sink_out")
    ooc = outcome(cfg, tables, sink=sink, out_of_core_reorder_threshold_rows=1)
    # Witness: the parent ran the batch driver and the child the reorder driver.
    assert spy.reorder == ["child"] and spy.reorder_joiners >= 1
    assert spy.batch == ["parent"]
    monkeypatch.undo()
    oracle = outcome(cfg, tables, full=True)
    assert ooc == oracle
    tables_hit = {f.table for f in ooc[1].findings}
    # A hook on only one driver would drop one of these tables' findings.
    assert tables_hit == {"parent", "child"}


def test_the_reorder_driver_alone_evaluates_a_child_only_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    vs = [_v("column_in_set", {"child": ["memo"]}, allowed_values=["m0"])]
    cfg, tables = standard(tmp_path, vs)
    spy = DriverSpy(monkeypatch)
    ooc = outcome(
        cfg, tables, sink=CountingSink(tmp_path / "s"), out_of_core_reorder_threshold_rows=1
    )
    assert "child" in spy.reorder
    monkeypatch.undo()
    assert ooc == outcome(cfg, tables, full=True) and ooc[0] == "failed"


def test_the_sink_batch_driver_evaluates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    spy = DriverSpy(monkeypatch)
    ooc = outcome(cfg, tables, sink=CountingSink(tmp_path / "sink_out"))
    assert spy.reorder == [] and sorted(spy.batch) == ["child", "parent"]
    monkeypatch.undo()
    assert ooc == outcome(cfg, tables, full=True)


# ---------------------------------------------------------------------------
# T4: fail-closed publishes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reorder", [False, True])
def test_a_validator_failure_publishes_nothing(
    reorder: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    sink = CountingSink(tmp_path / "sink_out")
    kw: dict[str, Any] = {"out_of_core_reorder_threshold_rows": 1} if reorder else {}
    with pytest.raises(ValidatorFailedError) as exc:
        run(cfg, tables, sink=sink, **kw)
    assert not exc.value.report.passed
    # Earlier tables had already staged privately; nothing was published.
    assert "parent" in sink.staged and "child" in sink.staged
    assert sink.commits == 0 and sink.aborts == 1
    assert not list((tmp_path / "sink_out").glob("*.parquet"))


def test_a_late_masking_failure_keeps_its_own_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)  # these validators WOULD fail; masking fails first
    real, calls = _runner.mask_batch, {"n": 0}

    def late_failure(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the child's batch, after the parent staged
            raise ExecutionError(code="injected_late_failure", message="boom")
        return real(*a, **k)

    monkeypatch.setattr(_runner, "mask_batch", late_failure)
    sink = CountingSink(tmp_path / "sink_out")
    with pytest.raises(ExecutionError) as exc:
        run(cfg, tables, sink=sink)
    assert not isinstance(exc.value, ValidatorFailedError)
    assert exc.value.code == "injected_late_failure"
    assert sink.commits == 0 and sink.aborts == 1


def test_a_passing_validated_run_still_commits_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    vs = [_v("column_in_set", {"child": ["memo"]}, allowed_values=[f"m{i}" for i in range(6)])]
    cfg, tables = standard(tmp_path, vs)
    sink = CountingSink(tmp_path / "sink_out")
    result = run(cfg, tables, sink=sink)
    assert mode(result) == _OOC
    assert sink.commits == 1 and sink.aborts == 0
    assert sorted(p.stem for p in (tmp_path / "sink_out").glob("*.parquet")) == ["child", "parent"]


def test_a_direct_caller_with_an_unadmitted_validator_fails_closed(tmp_path: Path) -> None:
    # `run_fk_out_of_core` is public: silently skipping a validator would report success for
    # checks that never ran.
    cfg, _ = standard(tmp_path, [{"name": "fk_intact"}])
    plan = _plan({"c": _seed("passthrough")})
    with pytest.raises(ExecutionError) as exc:
        _validate_batch.build_validator_collector(cfg, plan, _NO_EDGES, {})
    assert exc.value.code == "out_of_core_validators_not_admitted"
    assert _validate_batch.build_validator_collector(None, plan, _NO_EDGES, {}) is None
    assert (
        _validate_batch.build_validator_collector({"validators": []}, plan, _NO_EDGES, {}) is None
    )


# ---------------------------------------------------------------------------
# T5: masked output unchanged
# ---------------------------------------------------------------------------


def _passing_validators() -> list[dict[str, Any]]:
    return [
        _v("column_in_set", {"child": ["memo"]}, allowed_values=[f"m{i}" for i in range(6)]),
        _v("regex_match", {"parent": ["code"]}, pattern=".*"),
    ]


@pytest.mark.parametrize("reorder", [False, True])
def test_the_validator_hook_does_not_perturb_masked_output(
    reorder: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path, _passing_validators())
    kw: dict[str, Any] = {"out_of_core_reorder_threshold_rows": 1} if reorder else {}
    result = run(cfg, tables, sink=ParquetTransactionalSink(tmp_path / "sink_out"), **kw)
    got = {p.stem: pq.read_table(p) for p in sorted((tmp_path / "sink_out").glob("*.parquet"))}
    shutil.rmtree(tmp_path / "sink_out")
    no_validators = {k: v for k, v in cfg.items() if k != "validators"}
    baseline = run_pipeline(
        no_validators,
        tables,
        engine_version="d3a",
        key_provider=THROWAWAY_KEY_PROVIDER,
        execution_mode="out_of_core",
        out_of_core_budget_bytes=BUDGET,
        sink=ParquetTransactionalSink(tmp_path / "sink_out"),
        **kw,
    )
    ref = {p.stem: pq.read_table(p) for p in sorted((tmp_path / "sink_out").glob("*.parquet"))}
    assert mode(result)[0] == mode(baseline)[0] == "out_of_core"
    assert set(got) == set(ref) == {"parent", "child"}
    for name in got:
        report = sc.compare_strict(ref[name], got[name])
        assert all(report.values()), (name, report)


def test_resident_masked_output_is_identical_with_and_without_validators(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path, _passing_validators())
    with_validators = run(cfg, tables)
    without = run_pipeline(
        {k: v for k, v in cfg.items() if k != "validators"},
        tables,
        engine_version="d3a",
        key_provider=THROWAWAY_KEY_PROVIDER,
        execution_mode="out_of_core",
        out_of_core_budget_bytes=BUDGET,
    )
    assert mode(with_validators) == _OOC
    for name in ("parent", "child"):
        report = sc.compare_strict(without.outputs[name], with_validators.outputs[name])
        assert all(report.values()), (name, report)


def test_validated_output_equals_the_oracle_values_it_was_checked_on(tmp_path: Path) -> None:
    # The verdicts are about the value the job emits: the validated columns are byte-equal.
    cfg, tables = standard(tmp_path, _passing_validators())
    ooc, oracle = run(cfg, tables), run(cfg, tables, full=True)
    for table, column in (("parent", "code"), ("parent", "pan"), ("child", "memo")):
        assert ooc.outputs[table].column(column).to_pylist() == (
            oracle.outputs[table].column(column).to_pylist()
        )


# ---------------------------------------------------------------------------
# T6: bounded working memory (narrow, honest)
# ---------------------------------------------------------------------------


def test_validation_is_evaluated_per_batch_never_over_a_whole_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_batches(monkeypatch, 7)
    seen: list[tuple[str, int]] = []
    for name, fn in list(_registry._REGISTRY.items()):

        def spy(outputs: Any, entry: Any, config: Any, *, _fn: Any = fn, **kw: Any) -> Any:
            for table, data in outputs.items():
                seen.append((table, data.num_rows))
            return _fn(outputs, entry, config, **kw)

        monkeypatch.setitem(_registry._REGISTRY, name, spy)
    cfg, tables = standard(tmp_path)
    with pytest.raises(ValidatorFailedError):
        run(cfg, tables)
    assert seen and max(rows for _, rows in seen) <= 7  # never a whole 30-row column
    per_table = {t: sum(r for tab, r in seen if tab == t) for t in ("parent", "child")}
    # parent: 5 occurrences x 30 rows; child: 1 occurrence x 30 rows (each exactly once).
    assert per_table == {"parent": 5 * N, "child": N}
    assert len([1 for t, _ in seen if t == "child"]) == math.ceil(N / 7)


def test_the_validate_module_never_materialises_a_full_column() -> None:
    source = inspect.getsource(_validate_batch)
    assert "to_pylist" not in source and "to_pandas" not in source and "concat_tables" not in source


def test_findings_are_retained_as_compact_index_arrays(tmp_path: Path) -> None:
    from array import array

    cfg, tables = standard(tmp_path)
    collector = _validate_batch.build_validator_collector(
        cfg, _plan_from_cfg(cfg), _graph(), {t: _Src(tab) for t, tab in tables.items()}
    )
    assert collector is not None
    batch = tables["parent"].to_batches()[0]
    collector.add_batch("parent", batch)
    assert collector._failing and all(isinstance(v, array) for v in collector._failing.values())


class _Src:
    def __init__(self, table: pa.Table) -> None:
        self.schema = table.schema


def _plan_from_cfg(cfg: dict[str, Any]) -> Any:
    seeds: dict[str, dict[str, ColumnSeed]] = {}
    for tdef in cfg["tables"]:
        for col in tdef["columns"]:
            strategy = col["strategy"]
            seeds.setdefault(tdef["name"], {})[col["name"]] = _seed(
                strategy, col.get("provider_config")
            )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x11" * 8,
            per_table=tuple(
                (t, TableSeed(per_column=tuple(c.items()), per_group=())) for t, c in seeds.items()
            ),
        )
    )


# ---------------------------------------------------------------------------
# T7: verdict parity under OOC type/normalisation divergence
# ---------------------------------------------------------------------------


def test_an_admitted_string_job_has_identical_verdicts_on_both_routes(tmp_path: Path) -> None:
    sensitive = "a̖ࢗ"  # the post-Python-14 Unicode sequence the native NFC envelope covers
    tables = _tables()
    parent = tables["parent"]
    values = [sensitive if i % 4 == 0 else f"café{i}" for i in range(N)]
    tables["parent"] = parent.set_column(
        parent.schema.get_field_index("code"), "code", pa.array(values, pa.string())
    )
    vs = [
        _v("regex_match", {"parent": ["code"]}, pattern=r"^[^\d]{3}$"),
        _v("column_in_set", {"parent": ["code"]}, allowed_values=[sensitive[:3], "caf"]),
    ]
    cfg = job(tmp_path, tables, _columns(), vs)
    _assert_parity(cfg, tables)


def test_normalisation_sensitive_hash_and_categorical_outputs_decline(tmp_path: Path) -> None:
    sensitive = "a̖ࢗ"
    for strategy in (_h("code", "code_ns"), _cat("code", "code_ns", [sensitive, "x"])):
        sub = tmp_path / strategy["strategy"]
        sub.mkdir()
        tables, columns = _tables(), _columns()
        columns["parent"][5] = strategy
        cfg = job(sub, tables, columns, [_v("regex_match", {"parent": ["code"]}, pattern=".*")])
        assert mode(run(cfg, tables)) == ("full_frame", "byte_estimate_full_frame_fits")
        assert outcome(cfg, tables) == outcome(cfg, tables, full=True)
