"""Phase D / D3b: the token vault on the bounded out-of-core route.

Plan: docs/plans/2026-10-15-d3b-vault-bounded-ooc.md (acceptance matrix T1-T7 and T-neg).

A RAM-fitting pure-mask FK job that writes a token vault used to run full-frame (or be
rejected when too big). D3b lets it run out-of-core, collecting the vault per batch, when
every vaulted column is on the reviewed allowlist, string in and out, and not an incoming
FK child key. Vault side effects must equal the full-frame oracle's except the accepted
native-vs-Python NFC envelope that hash/categorical masked output already carries
(Option A). Every expectation below is compared against an independent full-frame run or a
literal, never against the output under test.
"""

from __future__ import annotations

import math
import shutil
import unicodedata
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
from decoy_engine.execution import _pipeline_route_exec as route_exec
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.out_of_core import _runner
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.execution.out_of_core._vault_collect import vault_ooc_safe
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.unmask import unmask_pipeline
from decoy_engine.vault import VaultWriter, load_vault, vault_writer_for_config
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER
from tests.parity import _strict_compare as sc

pytestmark = pytest.mark.filterwarnings("ignore")

BUDGET = 1024 * 1024 * 1024
FREE_DISK = 200 * 1024 * 1024 * 1024
_Usage = namedtuple("_Usage", "total used free")
SENSITIVE = "a̖ࢗ"  # the post-Python-14 Unicode sequence the NFC envelope covers


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


# ---------------------------------------------------------------------------
# Job construction
# ---------------------------------------------------------------------------


def _h(name: str, ns: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": ns, **extra}


def _cat(name: str, ns: str, categories: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "categorical",
        "namespace": ns,
        "deterministic": True,
        "provider_config": {"categories": categories},
        **extra,
    }


def _trunc(name: str, ns: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "truncate",
        "namespace": ns,
        "provider_config": {"length": 3},
        **extra,
    }


def _pass(name: str, ns: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough", "namespace": ns, **extra}


def _redact(name: str, ns: str, with_: Any = "XXX", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "redact",
        "namespace": ns,
        "provider_config": {"redact_with": with_},
        **extra,
    }


def job(
    tmp: Path,
    tables: dict[str, pa.Table],
    columns: dict[str, list[dict[str, Any]]],
    *,
    orphan: str = "preserve",
) -> dict[str, Any]:
    """parent.id -> child.parent_id (hash namespace `ns`), plus the given per-table columns."""
    src = {}
    for name, table in tables.items():
        path = tmp / f"{name}.parquet"
        pq.write_table(table, path)
        src[name] = {"type": "file", "path": str(path), "format": "parquet"}
    return {
        "version": 1,
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
                "orphan_policy": orphan,
                "namespace": "ns",
            }
        ],
    }


def standard(
    tmp: Path, *, n: int = 30, parent_vault: list[dict[str, Any]] | None = None
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """String parent key, string payloads (repeats, nulls, an all-null column), child memo."""
    ids = [f"p{i}" for i in range(n)]
    tables = {
        "parent": pa.table(
            {
                "id": pa.array(ids, pa.string()),
                "email": pa.array(
                    [None if i % 7 == 3 else f"user{i % 11}@x.test" for i in range(n)], pa.string()
                ),
                "tier": pa.array([f"t{i % 9}" for i in range(n)], pa.string()),
                "note": pa.array([f"note-{i}" for i in range(n)], pa.string()),
                "secret": pa.array([f"s{i % 4}" for i in range(n)], pa.string()),
                "empty": pa.array([None] * n, pa.string()),
            }
        ),
        "child": pa.table(
            {
                "cid": pa.array([f"c{i}" for i in range(n)], pa.string()),
                "parent_id": pa.array(ids, pa.string()),
                "memo": pa.array([f"m{i % 6}" for i in range(n)], pa.string()),
            }
        ),
    }
    columns = {
        "parent": parent_vault
        or [
            _h("id", "ns", vault=True),
            _h("email", "email_ns", vault=True),
            _cat("tier", "tier_ns", ["gold", "silver"], vault=True),
            _trunc("note", "note_ns", vault=True),
            _redact("secret", "sec_ns", vault=True),
            _h("empty", "empty_ns", vault=True),
        ],
        "child": [_h("cid", "cns"), _h("parent_id", "ns"), _h("memo", "memo_ns", vault=True)],
    }
    return job(tmp, tables, columns), tables


def run(
    cfg: dict[str, Any],
    tables: dict[str, pa.Table],
    *,
    full: bool = False,
    writer: Any = None,
    **kw: Any,
) -> tuple[Any, Any]:
    writer = (
        vault_writer_for_config(cfg, key_provider=THROWAWAY_KEY_PROVIDER)
        if writer is None
        else writer
    )
    kwargs: dict[str, Any] = {
        "engine_version": "d3b",
        "vault_writer": writer,
        "key_provider": THROWAWAY_KEY_PROVIDER,
    }
    kwargs.update(
        {"execution_mode": "full_frame"} if full else {"out_of_core_budget_bytes": BUDGET}
    )
    kwargs.update(kw)
    return run_pipeline(cfg, tables, **kwargs), writer


def mode(result: Any) -> tuple[str, str]:
    ex = result.quality_metrics["execution"]
    return ex["execution_mode"], ex["route_reason"]


def vault_of(writer: VaultWriter, tmp: Path, tag: str) -> tuple[dict[Any, Any], int]:
    path = tmp / f"{tag}.vault"
    writer.write(path)
    return load_vault(path, writer._mask_key)


def assert_same_vault(a: VaultWriter, b: VaultWriter, tmp: Path) -> None:
    assert a._entries == b._entries
    assert vault_of(a, tmp, "a") == vault_of(b, tmp, "b")


def fixed_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Shutil:
        def disk_usage(self, path: Any) -> Any:
            return _Usage(FREE_DISK * 2, FREE_DISK, FREE_DISK)

        def __getattr__(self, name: str) -> Any:
            return getattr(shutil, name)

    monkeypatch.setattr(route_exec, "shutil", _Shutil())


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


class CountingWriter(VaultWriter):
    """A real VaultWriter that records every add() batch and every write() call."""

    def __init__(self, mask_key: bytes) -> None:
        super().__init__(mask_key)
        self.add_sizes: list[int] = []
        self.write_calls = 0

    def add(self, entries: Any) -> None:
        batch = list(entries)
        self.add_sizes.append(len(batch))
        super().add(batch)

    def write(self, path: Any) -> int:
        self.write_calls += 1
        return super().write(path)


def counting_writer(cfg: dict[str, Any]) -> CountingWriter:
    return CountingWriter(
        vault_writer_for_config(cfg, key_provider=THROWAWAY_KEY_PROVIDER)._mask_key
    )


# ---------------------------------------------------------------------------
# T1: admission and declines (decision function, then column-level, then end to end)
# ---------------------------------------------------------------------------


class _Profile:
    relationships = (object(),)


def _graph() -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("id",),
        child_table="child",
        child_columns=("parent_id",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _cyclic() -> RelationshipGraph:
    e1 = RelationshipEdge("a", ("id",), "b", ("ra",), "na", OrphanPolicy.PRESERVE)
    e2 = RelationshipEdge("b", ("id",), "a", ("rb",), "nb", OrphanPolicy.PRESERVE)
    return RelationshipGraph(edges=(e1, e2), ordering=())


def decide(**kw: Any) -> tuple[str, str]:
    base: dict[str, Any] = {
        "has_generate_table": False,
        "has_mask_table": True,
        "validators": [],
        "fidelity_report": False,
        "vault_writer": object(),
        "execution_mode": "auto",
        "graph": _graph(),
        "out_of_core_compatible": True,
        "largest_table_rows": 100,
        "vault_ooc_safe": True,
        "full_frame_fits_estimate": True,
    }
    base.update(kw)
    return decide_execution_route(_Profile(), **base)


_OOC = ("out_of_core", "byte_estimate_bounded_out_of_core")


def test_safe_vault_job_routes_out_of_core_when_it_fits() -> None:
    assert decide(full_frame_fits_estimate=True) == _OOC


@pytest.mark.parametrize("fits", [False, None])
def test_safe_vault_job_that_does_not_fit_is_rejected(fits: bool | None) -> None:
    # The vault accumulates O(distinct triples) in Python memory, OUTSIDE the OOC spill budget,
    # so a non-fitting or unpriceable vault job cannot be bounded on OOC: it rejects, exactly as a
    # too-big vault job does today (Codex final gate HIGH). A spillable-vault follow-on could lift it.
    with pytest.raises(ExecutionError) as exc:
        decide(full_frame_fits_estimate=fits)
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"


def test_unsafe_vault_job_stays_full_frame_when_it_fits() -> None:
    assert decide(vault_ooc_safe=False) == ("full_frame", "byte_estimate_full_frame_fits")


def test_unsafe_vault_job_that_is_too_big_is_still_rejected() -> None:
    with pytest.raises(ExecutionError) as exc:
        decide(vault_ooc_safe=False, full_frame_fits_estimate=False)
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"


@pytest.mark.parametrize(
    ("extra", "rejects_when_too_big"),
    [
        ({"out_of_core_compatible": False}, True),  # SEQ-eligible but not OOC-compatible
        ({"fk_ooc_dtype_hazard": True}, True),
        ({"graph": _cyclic()}, True),
        ({"validators": ["v"]}, True),
        ({"fidelity_report": True}, True),
        ({"post_validation": True}, True),
        ({"resolved_substrate": "polars"}, True),
        ({"has_generate_table": True}, False),  # outside the byte-estimate scope entirely
        ({"has_mask_table": False}, False),
    ],
    ids=[
        "ooc-incompatible",
        "fk-dtype-hazard",
        "cyclic",
        "validators",
        "fidelity",
        "post-validation",
        "substrate",
        "generate",
        "no-mask-table",
    ],
)
def test_other_disqualifiers_still_beat_the_vault_admission(
    extra: dict[str, Any], rejects_when_too_big: bool
) -> None:
    # Never out-of-core and never sequential (it has no vault collector): fit -> full-frame.
    assert decide(**extra)[0] == "full_frame"
    if rejects_when_too_big:
        with pytest.raises(ExecutionError):
            decide(full_frame_fits_estimate=False, **extra)


def test_rollback_flag_off_restores_the_old_vault_routing() -> None:
    assert decide(use_byte_estimate_routing=False) == ("full_frame", "vault_writer_requested")
    with pytest.raises(ExecutionError):
        decide(use_byte_estimate_routing=False, largest_table_rows=10**9)


@pytest.mark.parametrize("explicit", ["sequential", "out_of_core"])
def test_explicit_modes_still_refuse_a_vault_job(explicit: str) -> None:
    with pytest.raises(ConfigError):
        decide(execution_mode=explicit)


# --- column level: the allowlist, string in / string out, child keys -----------------


def _seed(strategy: str, cfg: dict[str, Any] | None = None, ns: str | None = "n") -> ColumnSeed:
    return ColumnSeed(
        namespace=ns,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple((cfg or {}).items()),
        coherent_with=(),
    )


def _plan(seeds: dict[str, ColumnSeed], table: str = "child") -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x11" * 8,
            per_table=((table, TableSeed(per_column=tuple(seeds.items()), per_group=())),),
        )
    )


def _vault_cfg(table: str, *columns: str) -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": table,
                "columns": [{"name": c, "vault": True, "namespace": "n"} for c in columns],
            }
        ]
    }


def _source(column: str, arrow_type: pa.DataType, table: str = "child") -> dict[str, pa.Table]:
    return {table: pa.table({column: pa.array([None], arrow_type)})}


_NO_EDGES = RelationshipGraph(edges=(), ordering=())
_W = object()


def _safe(seed: ColumnSeed, arrow_type: pa.DataType | None = None, graph: Any = _NO_EDGES) -> bool:
    arrow_type = arrow_type or pa.string()
    return vault_ooc_safe(
        _vault_cfg("child", "c"), _plan({"c": seed}), graph, _source("c", arrow_type), _W
    )


@pytest.mark.parametrize(
    "seed",
    [
        _seed("hash"),
        _seed("categorical", {"categories": ["a", "b"]}),
        _seed("truncate", {"length": 3}),
        _seed("redact", {"redact_with": "XXX"}),
        _seed("redact"),
        _seed("passthrough"),
    ],
    ids=["hash", "categorical", "truncate", "redact", "redact-default", "passthrough"],
)
@pytest.mark.parametrize("arrow_type", [pa.string(), pa.large_string()])
def test_allowlisted_string_columns_are_admitted(seed: ColumnSeed, arrow_type: pa.DataType) -> None:
    assert _safe(seed, arrow_type)


@pytest.mark.parametrize("strategy", ["bucket_perturb", "text_redact", "text_mask", "code_set"])
def test_ooc_compatible_but_unreviewed_strategies_decline(strategy: str) -> None:
    assert not _safe(_seed(strategy))


@pytest.mark.parametrize("strategy", ["fpe", "faker", "date_shift", "bucketize", "nested"])
def test_other_strategies_decline(strategy: str) -> None:
    assert not _safe(_seed(strategy))


@pytest.mark.parametrize("categories", [[1, 2], [1.5, 2.5], ["a", 1]])
def test_categorical_with_non_string_resolved_categories_declines(categories: list[Any]) -> None:
    assert not _safe(_seed("categorical", {"categories": categories}))


@pytest.mark.parametrize("replacement", [7, 7.0, None, True])
def test_redact_with_a_non_string_replacement_declines(replacement: Any) -> None:
    assert not _safe(_seed("redact", {"redact_with": replacement}))


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
@pytest.mark.parametrize("strategy", ["hash", "truncate", "redact", "passthrough"])
def test_non_string_binary_source_declines(strategy: str, arrow_type: pa.DataType) -> None:
    assert not _safe(_seed(strategy), arrow_type)


def test_binary_source_is_admitted_only_when_the_output_is_a_string() -> None:
    assert _safe(_seed("hash"), pa.binary())
    assert not _safe(_seed("passthrough"), pa.binary())  # output stays binary


def test_a_missing_seed_source_or_column_declines() -> None:
    cfg, plan = _vault_cfg("child", "c"), _plan({"c": _seed("hash")})
    assert not vault_ooc_safe(cfg, _plan({}), _NO_EDGES, _source("c", pa.string()), _W)
    assert not vault_ooc_safe(cfg, plan, _NO_EDGES, {}, _W)
    assert not vault_ooc_safe(cfg, plan, _NO_EDGES, _source("other", pa.string()), _W)


def test_one_declined_column_declines_the_whole_job() -> None:
    cfg = _vault_cfg("child", "a", "b")
    plan = _plan({"a": _seed("hash"), "b": _seed("text_redact")})
    src = {"child": pa.table({"a": ["x"], "b": ["y"]})}
    assert not vault_ooc_safe(cfg, plan, _NO_EDGES, src, _W)
    assert vault_ooc_safe(cfg, _plan({"a": _seed("hash"), "b": _seed("hash")}), _NO_EDGES, src, _W)


def test_no_writer_or_no_vaulted_columns() -> None:
    cfg, plan = _vault_cfg("child", "c"), _plan({"c": _seed("hash")})
    assert not vault_ooc_safe(cfg, plan, _NO_EDGES, _source("c", pa.string()), None)
    assert vault_ooc_safe({"tables": []}, plan, _NO_EDGES, {}, _W)  # nothing to collect


# --- T3: incoming child-key columns (single, composite, overlap, multi-parent, self-ref) ----


def _edge(pt: str, pc: tuple[str, ...], ct: str, cc: tuple[str, ...]) -> RelationshipEdge:
    return RelationshipEdge(pt, pc, ct, cc, "ns", OrphanPolicy.PRESERVE)


def _two_table_safe(
    vaulted: str, edges: tuple[RelationshipEdge, ...], table: str = "child"
) -> bool:
    cols = {"a": _seed("hash"), "b": _seed("hash"), "k": _seed("hash")}
    src = {
        t: pa.table({"a": ["x"], "b": ["y"], "k": ["z"], "id": ["i"]})
        for t in ("parent", "child", "mid", "p2")
    }
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x11" * 8,
            per_table=tuple(
                (t, TableSeed(per_column=tuple(cols.items()), per_group=()))
                for t in ("parent", "child", "mid", "p2")
            ),
        )
    )
    graph = RelationshipGraph(edges=edges, ordering=())
    return vault_ooc_safe(_vault_cfg(table, vaulted), plan, graph, src, _W)


def test_single_child_key_declines_and_the_parent_key_is_admitted() -> None:
    edges = (_edge("parent", ("a",), "child", ("b",)),)
    assert not _two_table_safe("b", edges, "child")
    assert _two_table_safe("a", edges, "parent")  # parent keys are the common vaulted column
    assert _two_table_safe("k", edges, "child")


def test_every_component_of_a_composite_child_key_declines() -> None:
    edges = (_edge("parent", ("a", "b"), "child", ("a", "b")),)
    assert not _two_table_safe("a", edges, "child")
    assert not _two_table_safe("b", edges, "child")  # the second component, not just the first
    assert _two_table_safe("k", edges, "child")


def test_a_parent_key_that_is_also_a_child_key_declines() -> None:
    # parent -> mid.a, then mid.a is the parent key of mid -> child: mid.a is a child key.
    edges = (_edge("parent", ("a",), "mid", ("a",)), _edge("mid", ("a",), "child", ("b",)))
    assert not _two_table_safe("a", edges, "mid")


def test_a_child_key_of_any_of_several_parents_declines() -> None:
    edges = (_edge("parent", ("a",), "child", ("a",)), _edge("p2", ("id",), "child", ("b",)))
    assert not _two_table_safe("a", edges, "child")
    assert not _two_table_safe("b", edges, "child")  # the second edge, not just the first
    assert _two_table_safe("k", edges, "child")


def test_a_self_referencing_child_key_declines() -> None:
    edges = (_edge("child", ("a",), "child", ("b",)),)
    assert not _two_table_safe("b", edges, "child")
    assert _two_table_safe("a", edges, "child")


# --- end to end: the real pipeline picks the route -------------------------------------


def test_admitted_job_runs_out_of_core_end_to_end(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    result, _ = run(cfg, tables)
    assert mode(result) == _OOC


def _declined_job(
    tmp: Path,
    parent_cols: list[dict[str, Any]],
    extra: dict[str, pa.Array] | None = None,
    *,
    vault_child_key: bool = False,
    rows: int = 12,
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    ids = [f"p{i}" for i in range(rows)]
    parent = {"id": pa.array(ids, pa.string()), "v": pa.array([f"v{i}" for i in range(rows)])}
    parent.update(extra or {})
    tables = {
        "parent": pa.table(parent),
        "child": pa.table({"parent_id": pa.array(ids, pa.string())}),
    }
    columns = {
        "parent": [_h("id", "ns"), *parent_cols],
        "child": [_h("parent_id", "ns", **({"vault": True} if vault_child_key else {}))],
    }
    return job(tmp, tables, columns), tables


def _declined_cases(tmp: Path, **kw: Any) -> dict[str, tuple[dict[str, Any], dict[str, pa.Table]]]:
    ints = {"n": pa.array(list(range(kw.get("rows", 12))), pa.int64())}
    floats = {"n": pa.array([float(i) for i in range(kw.get("rows", 12))], pa.float64())}
    text_redact = {"name": "v", "strategy": "text_redact", "namespace": "vn", "vault": True}
    specs: dict[str, Any] = {
        "numeric-redact": ([_redact("v", "vn", 7, vault=True)], None, False),
        "numeric-categories": ([_cat("v", "vn", [1, 2], vault=True)], None, False),
        "text-redact": ([text_redact], None, False),
        "int-source": ([_h("n", "nn", vault=True)], ints, False),
        "float-source": ([_trunc("n", "nn", vault=True)], floats, False),
        "child-key": ([], None, True),
    }
    out = {}
    for name, (cols, extra, child_key) in specs.items():
        sub = tmp / name
        sub.mkdir()
        out[name] = _declined_job(sub, cols, extra, vault_child_key=child_key, **kw)
    return out


_DECLINED = [
    "numeric-redact",
    "numeric-categories",
    "text-redact",
    "int-source",
    "float-source",
    "child-key",
]


@pytest.mark.parametrize("case", _DECLINED)
def test_declined_jobs_stay_full_frame_and_keep_the_oracle_vault(case: str, tmp_path: Path) -> None:
    cfg, tables = _declined_cases(tmp_path)[case]
    result, writer = run(cfg, tables)
    assert mode(result) == ("full_frame", "byte_estimate_full_frame_fits")
    _, oracle_writer = run(cfg, tables, full=True)
    assert_same_vault(writer, oracle_writer, tmp_path)


def test_a_too_big_vault_job_rejects_and_a_fitting_one_runs_out_of_core(
    tmp_path: Path,
) -> None:
    # The vault accumulates O(distinct triples) in Python memory, OUTSIDE the OOC spill budget, so a
    # vault job that does not fit full-frame cannot be bounded on OOC: it rejects, whether the vaulted
    # column is admission-safe or not (Codex final gate HIGH). A FITTING safe-vault job takes OOC.
    budget = 64 * 1024 * 1024  # 64 MB floor; 200k rows is over it, so full-frame does not fit.
    big = 200_000
    # (a) a too-big DECLINED (unsafe, numeric-redact) vault job rejects.
    declined = _declined_cases(tmp_path, rows=big)["numeric-redact"]
    with pytest.raises(ExecutionError) as exc:
        run(*declined, out_of_core_budget_bytes=budget)
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"
    # (b) a too-big SAFE vault job ALSO rejects (the vault cannot be bounded on OOC) -- previously
    # this was wrongly admitted to OOC. (A FITTING safe vault job routing OOC is covered by the
    # many `standard()`-based tests above.)
    big_safe_dir = tmp_path / "toobig_safe"
    big_safe_dir.mkdir()
    big_safe = _declined_job(big_safe_dir, [_redact("v", "vn", "XXX", vault=True)], rows=big)
    with pytest.raises(ExecutionError) as exc2:
        run(*big_safe, out_of_core_budget_bytes=budget)
    assert exc2.value.code == "fk_full_frame_oom_risk_rejected"


def test_the_rollback_flag_runs_an_admitted_job_full_frame(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    result, writer = run(cfg, tables, use_byte_estimate_routing=False)
    assert mode(result)[0] == "full_frame"
    ooc, ooc_writer = run(cfg, tables)
    assert_same_vault(writer, ooc_writer, tmp_path)


# ---------------------------------------------------------------------------
# T2: vault parity with the full-frame oracle
# ---------------------------------------------------------------------------


def test_nfc_agnostic_vault_is_identical_to_the_full_frame_oracle(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    ooc, ooc_writer = run(cfg, tables)
    oracle, oracle_writer = run(cfg, tables, full=True)
    assert mode(ooc)[0] == "out_of_core" and mode(oracle)[0] == "full_frame"
    assert_same_vault(ooc_writer, oracle_writer, tmp_path)
    mapping, ambiguous = vault_of(ooc_writer, tmp_path, "ooc")
    # Non-trivial: real triples, an all-null column that contributed nothing, and ambiguity.
    assert len(mapping) > 30
    assert not any(ns == "empty_ns" for ns, _ in mapping)
    assert ambiguous >= 2  # the 2-category tier and the single-token secret collide
    assert ambiguous == vault_of(oracle_writer, tmp_path, "full")[1]
    # Nulls are skipped, never stringified.
    assert not any(src == "None" for src in mapping.values())


def test_a_vaulted_parent_key_is_recoverable_from_the_ooc_output(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    result, writer = run(cfg, tables)
    mapping, _ = vault_of(writer, tmp_path, "rt")
    masked_ids = result.outputs["parent"].column("id").to_pylist()
    assert [mapping[("ns", m)] for m in masked_ids] == tables["parent"].column("id").to_pylist()


def test_all_null_vaulted_column_adds_no_entries(tmp_path: Path) -> None:
    only_empty = [_h("id", "ns"), _h("empty", "empty_ns", vault=True)]
    cfg, tables = standard(tmp_path, parent_vault=only_empty)
    cfg["tables"][1]["columns"] = [c for c in cfg["tables"][1]["columns"] if "vault" not in c]
    ooc, writer = run(cfg, tables)
    assert mode(ooc)[0] == "out_of_core"
    assert writer._entries == set()


def test_repeated_and_conflicting_sources_drop_as_ambiguous_like_the_oracle(tmp_path: Path) -> None:
    n = 40
    ids = [f"p{i}" for i in range(n)]
    tables = {
        "parent": pa.table({"id": pa.array(ids), "k": pa.array([f"s{i}" for i in range(n)])}),
        "child": pa.table({"parent_id": pa.array(ids)}),
    }
    columns = {
        "parent": [_h("id", "ns"), _cat("k", "kn", ["a", "b", "c"], vault=True)],
        "child": [_h("parent_id", "ns")],
    }
    cfg = job(tmp_path, tables, columns)
    ooc, writer = run(cfg, tables)
    oracle, oracle_writer = run(cfg, tables, full=True)
    assert mode(ooc)[0] == "out_of_core"
    assert_same_vault(writer, oracle_writer, tmp_path)
    assert vault_of(writer, tmp_path, "x")[1] == 3  # all three masked values are ambiguous


@pytest.mark.skipif(
    not _companion_loads()
    or tuple(int(p) for p in unicodedata.unidata_version.split(".")) >= (16, 0, 0),
    reason="needs the abi-3 companion and a Python Unicode table older than 16",
)
class TestNfcEnvelope:
    """hash/categorical on NFC-divergent text: the OOC vault is consistent with the OOC output."""

    def _job(self, tmp: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
        n = 8
        ids = [f"p{i}" for i in range(n)]
        pool = [f"cat{i}" for i in range(64)]
        tables = {
            "parent": pa.table(
                {
                    "id": pa.array(ids),
                    "email": pa.array([SENSITIVE, "plain", *[f"e{i}" for i in range(n - 2)]]),
                    "tier": pa.array([SENSITIVE, "plain", *[f"t{i}" for i in range(n - 2)]]),
                }
            ),
            "child": pa.table({"parent_id": pa.array(ids)}),
        }
        columns = {
            "parent": [
                _h("id", "ns"),
                _h("email", "email_ns", vault=True),
                _cat("tier", "tier_ns", pool, vault=True),
            ],
            "child": [_h("parent_id", "ns")],
        }
        return job(tmp, tables, columns), tables

    def test_vault_keys_are_the_ooc_runs_own_masked_output_and_recover(
        self, tmp_path: Path
    ) -> None:
        cfg, tables = self._job(tmp_path)
        result, writer = run(cfg, tables)
        assert mode(result)[0] == "out_of_core"
        mapping, ambiguous = vault_of(writer, tmp_path, "nfc")
        assert ambiguous == 0
        out = result.outputs["parent"]
        for column, ns in (("email", "email_ns"), ("tier", "tier_ns")):
            masked = out.column(column).to_pylist()
            sources = tables["parent"].column(column).to_pylist()
            assert [mapping[(ns, m)] for m in masked] == sources  # every row, incl. the NFC one
        recovered = unmask_pipeline(
            cfg,
            dict(result.outputs),
            vault_path=str(tmp_path / "nfc.vault"),
            key_provider=THROWAWAY_KEY_PROVIDER,
        )
        statuses = {r.column: r.status for r in recovered.columns}
        assert statuses["email"] == "vault_reversed"
        assert (
            recovered.outputs["parent"].column("email").to_pylist()
            == tables["parent"].column("email").to_pylist()
        )

    def test_the_only_difference_from_the_oracle_is_the_nfc_rows(self, tmp_path: Path) -> None:
        cfg, tables = self._job(tmp_path)
        _, ooc_writer = run(cfg, tables)
        _, oracle_writer = run(cfg, tables, full=True)
        only_ooc = ooc_writer._entries - oracle_writer._entries
        only_oracle = oracle_writer._entries - ooc_writer._entries
        # The envelope: exactly the NFC-divergent source, per vaulted column, and nothing else.
        assert {src for _, _, src in only_ooc} == {SENSITIVE}
        assert {src for _, _, src in only_oracle} == {SENSITIVE}
        assert len(only_ooc) == len(only_oracle) <= 2


# ---------------------------------------------------------------------------
# T4: both OOC drivers collect
# ---------------------------------------------------------------------------


def _sink_run(
    tmp: Path, cfg: dict[str, Any], tables: dict[str, pa.Table], **kw: Any
) -> tuple[Any, Any]:
    sink = ParquetTransactionalSink(tmp / "sink_out")
    return run(cfg, tables, sink=sink, **kw)


def test_the_in_memory_driver_collects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    spy = DriverSpy(monkeypatch)
    result, writer = run(cfg, tables)  # no sink: the batch driver for every table
    assert mode(result)[0] == "out_of_core"
    assert spy.reorder == [] and sorted(spy.batch) == ["child", "parent"]
    monkeypatch.undo()
    _, oracle_writer = run(cfg, tables, full=True)
    assert_same_vault(writer, oracle_writer, tmp_path)


def test_the_reorder_sink_driver_collects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    spy = DriverSpy(monkeypatch)
    result, writer = _sink_run(tmp_path, cfg, tables, out_of_core_reorder_threshold_rows=1)
    assert mode(result)[0] == "out_of_core"
    assert spy.reorder == ["child"] and spy.reorder_joiners >= 1  # witness: the reorder driver ran
    assert spy.batch == ["parent"]
    monkeypatch.undo()
    _, oracle_writer = run(cfg, tables, full=True)
    assert_same_vault(writer, oracle_writer, tmp_path)
    # The child's vaulted column came through the reorder driver, not the parent's.
    assert any(ns == "memo_ns" for ns, _, _ in writer._entries)


def test_the_sink_batch_driver_collects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    spy = DriverSpy(monkeypatch)
    result, writer = _sink_run(tmp_path, cfg, tables)  # default threshold: batch driver
    assert mode(result)[0] == "out_of_core"
    assert spy.reorder == [] and sorted(spy.batch) == ["child", "parent"]
    monkeypatch.undo()
    _, oracle_writer = run(cfg, tables, full=True)
    assert_same_vault(writer, oracle_writer, tmp_path)


# ---------------------------------------------------------------------------
# T5: a failed run persists nothing
# ---------------------------------------------------------------------------


def _caller(
    cfg: dict[str, Any], tables: dict[str, pa.Table], path: Path, **kw: Any
) -> CountingWriter:
    """The CLI's contract (`decoy run`): write() is called only after run_pipeline returns."""
    writer = counting_writer(cfg)
    run(cfg, tables, writer=writer, **kw)
    writer.write(path)
    return writer


def test_a_successful_run_is_persisted_by_the_caller(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    path = tmp_path / "ok.vault"
    writer = _caller(cfg, tables, path)
    assert writer.write_calls == 1 and path.exists()
    assert len(writer.add_sizes) >= 2  # more than one add(): batches were collected incrementally


def test_a_late_masking_failure_persists_no_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, tables = standard(tmp_path)
    real, calls = _runner.mask_batch, {"n": 0}

    def late_failure(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the child's batch, after the parent already add()ed
            raise ExecutionError(code="injected_late_failure", message="boom")
        return real(*a, **k)

    monkeypatch.setattr(_runner, "mask_batch", late_failure)
    writer, path = counting_writer(cfg), tmp_path / "late.vault"
    with pytest.raises(ExecutionError) as exc:
        run(cfg, tables, writer=writer)
        writer.write(path)  # the caller's write(): unreachable when the run raises
    assert exc.value.code == "injected_late_failure"
    assert writer.add_sizes, "the parent batch had already been added before the failure"
    assert writer.write_calls == 0 and not path.exists()


@pytest.mark.parametrize("driver", ["batch", "reorder"])
def test_an_orphan_failure_persists_no_vault(
    driver: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    tables["child"] = tables["child"].set_column(
        1, "parent_id", pa.array([f"p{i}" for i in range(29)] + ["no-such-parent"])
    )
    cfg = job(tmp_path, tables, {t["name"]: t["columns"] for t in cfg["tables"]}, orphan="fail")
    writer, path = counting_writer(cfg), tmp_path / "orphan.vault"
    kw: dict[str, Any] = {"out_of_core_reorder_threshold_rows": 1} if driver == "reorder" else {}
    spy = DriverSpy(monkeypatch)
    with pytest.raises(ExecutionError):
        _sink_run(tmp_path, cfg, tables, writer=writer, **kw)
        writer.write(path)
    assert (spy.reorder == ["child"]) is (driver == "reorder")
    assert writer.add_sizes  # the parent (and, for reorder, the child's mask step) had added
    assert writer.write_calls == 0 and not path.exists()


# ---------------------------------------------------------------------------
# T6: masked output unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reorder", [False, True])
def test_the_vault_hook_does_not_perturb_masked_output(
    reorder: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard(tmp_path)
    kw: dict[str, Any] = {"out_of_core_reorder_threshold_rows": 1} if reorder else {}
    with_vault, _ = _sink_run(tmp_path, cfg, tables, **kw)
    got = {p.stem: pq.read_table(p) for p in sorted((tmp_path / "sink_out").glob("*.parquet"))}
    shutil.rmtree(tmp_path / "sink_out")
    baseline = run_pipeline(
        cfg,
        tables,
        engine_version="d3b",
        key_provider=THROWAWAY_KEY_PROVIDER,
        execution_mode="out_of_core",
        out_of_core_budget_bytes=BUDGET,
        sink=ParquetTransactionalSink(tmp_path / "sink_out"),
        **kw,
    )
    ref = {p.stem: pq.read_table(p) for p in sorted((tmp_path / "sink_out").glob("*.parquet"))}
    assert mode(with_vault)[0] == mode(baseline)[0] == "out_of_core"
    assert set(got) == set(ref) == {"parent", "child"}
    for name in got:
        report = sc.compare_strict(ref[name], got[name])
        assert all(report.values()), (name, report)


def test_resident_masked_output_is_identical_with_and_without_the_vault(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    with_vault, _ = run(cfg, tables)
    without = run_pipeline(
        cfg,
        tables,
        engine_version="d3b",
        key_provider=THROWAWAY_KEY_PROVIDER,
        execution_mode="out_of_core",
        out_of_core_budget_bytes=BUDGET,
    )
    for name in ("parent", "child"):
        report = sc.compare_strict(without.outputs[name], with_vault.outputs[name])
        assert all(report.values()), (name, report)


# ---------------------------------------------------------------------------
# T7: per-batch collection, deduplicated
# ---------------------------------------------------------------------------


def test_vault_is_collected_per_batch_and_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decoy_engine.execution.out_of_core as ooc_pkg

    real = ooc_pkg.run_fk_out_of_core

    def small_batches(*a: Any, **k: Any) -> Any:
        return real(*a, **{**k, "batch_rows": 7})

    monkeypatch.setattr(ooc_pkg, "run_fk_out_of_core", small_batches)
    cfg, tables = standard(tmp_path)  # 30 rows per table
    writer = counting_writer(cfg)
    result, _ = run(cfg, tables, writer=writer)
    assert mode(result)[0] == "out_of_core"
    vaulted_per_table = {"parent": 6, "child": 1}
    # One add() per batch per table, each bounded by batch_rows x vaulted columns: no
    # whole-table vault is ever materialized.
    assert len(writer.add_sizes) == 2 * math.ceil(30 / 7)
    assert max(writer.add_sizes) <= 7 * max(vaulted_per_table.values())
    # Dedup: the writer holds distinct triples, fewer than the entries it was handed.
    assert len(writer._entries) < sum(writer.add_sizes)
    monkeypatch.undo()
    _, oracle_writer = run(cfg, tables, full=True)
    assert writer._entries == oracle_writer._entries


# ---------------------------------------------------------------------------
# T-neg: fpe + vault stays a config reject; the writer guard is unchanged
# ---------------------------------------------------------------------------


def test_fpe_with_vault_is_still_a_config_reject(tmp_path: Path) -> None:
    from decoy_engine.plan import PlanCompileError

    cfg, tables = standard(tmp_path)
    parent = cfg["tables"][0]["columns"]
    parent[:] = [c for c in parent if c["name"] != "note"]
    parent.append({"name": "note", "strategy": "fpe", "namespace": "f", "vault": True})
    with pytest.raises(PlanCompileError) as exc:
        run(cfg, tables)
    assert exc.value.code == "vault_strategy_reversible"


def test_an_unkeyed_vault_writer_is_still_rejected_on_the_ooc_route(tmp_path: Path) -> None:
    cfg, tables = standard(tmp_path)
    with pytest.raises(Exception) as exc:
        run(cfg, tables, writer=VaultWriter(b"\x00" * 8))
    assert getattr(exc.value, "code", None) == "vault_key_mismatch"


def test_direct_callers_of_the_runner_get_the_same_vault_guards() -> None:
    # `run_fk_out_of_core` is public; callers that skip `run_pipeline` skip its DE-02 guard.
    from decoy_engine.execution.out_of_core._vault_collect import assert_vault_inputs

    key = b"\x01" * 8
    assert_vault_inputs(None, None, key)  # no writer: nothing to guard
    assert_vault_inputs(VaultWriter(key), {"tables": []}, key)
    with pytest.raises(ExecutionError) as no_config:
        assert_vault_inputs(VaultWriter(key), None, key)
    assert no_config.value.code == "out_of_core_vault_config_required"
    with pytest.raises(Exception) as unkeyed:
        assert_vault_inputs(VaultWriter(b"\x02" * 8), {"tables": []}, key)
    assert getattr(unkeyed.value, "code", None) == "vault_key_mismatch"
