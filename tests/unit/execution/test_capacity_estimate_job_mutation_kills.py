"""Mutation-kill oracles for `estimate_job_capacity` and its helpers
(`_resolved_config_copy`, `_not_applicable`) -- the OOC-FK capacity-checker
entrypoint (`decoy preflight` / `decoy run` gate).

Companion to `test_capacity_estimate_job.py`: that file proves the documented
end-to-end behaviours; this one pins the machine fields (verdict enum, route
label, refusal `code`, byte estimates) at the branch granularity a mutation
sweep probes, plus direct unit oracles for the two path-resolution helpers.

Every expected value is HARDCODED, never recomputed from a capacity constant
or helper -- the point is to catch a silent drift in those constants, so the
oracle must not import the number it is checking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import capacity as capacity_mod
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.capacity import _resolved_config_copy, estimate_job_capacity
from decoy_engine.execution.out_of_core._capacity_eval import CapacityEstimate, CapacityVerdict

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


# --------------------------------------------------------------------------
# fixture builders (self-contained; mirror test_capacity_estimate_job.py)
# --------------------------------------------------------------------------
def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _write_parquet(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _write_csv(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.csv"
    table.to_pandas().to_csv(p, index=False)
    return str(p)


def _parent_child_tables(n: int = 40) -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    return parent, child


def _ooc_config(
    tmp_path: Path,
    *,
    parent_fmt: str = "parquet",
    child_fmt: str = "parquet",
    tables: tuple[pa.Table, pa.Table] | None = None,
) -> dict[str, Any]:
    parent, child = tables if tables is not None else _parent_child_tables()
    parent_src = (
        _write_parquet(tmp_path, parent, "parent")
        if parent_fmt == "parquet"
        else _write_csv(tmp_path, parent, "parent")
    )
    child_src = (
        _write_parquet(tmp_path, child, "child")
        if child_fmt == "parquet"
        else _write_csv(tmp_path, child, "child")
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "capacity-kill-test", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": parent_fmt},
            "child": {"type": "file", "path": child_src, "format": child_fmt},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [_hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }


def _three_level_config(tmp_path: Path, *, parent_rows: int) -> dict[str, Any]:
    """grandparent <- parent <- child, so `parent` is BOTH a build table (has
    an outgoing edge) AND has one incoming edge -- the only shape where the
    build-phase `live` count (`1 if sink else incoming+1`) differs between
    sink=True (1) and sink=False (2). A single-level parent has zero incoming
    edges, so both collapse to live=1 and cannot distinguish the sink flag."""
    gp = pa.table(
        {
            "gid": pa.array([f"g{i}" for i in range(40)], type=pa.string()),
            "gnote": pa.array([f"gs{i}" for i in range(40)], type=pa.string()),
        }
    )
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(parent_rows)], type=pa.string()),
            "gp_id": pa.array([f"g{i % 40}" for i in range(parent_rows)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(40)], type=pa.string()),
            "parent_id": pa.array([f"p{i % parent_rows}" for i in range(40)], type=pa.string()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "capacity-3lvl", "seed": 7},
        "sources": {
            "grandparent": {
                "type": "file",
                "path": _write_parquet(tmp_path, gp, "grandparent"),
                "format": "parquet",
            },
            "parent": {
                "type": "file",
                "path": _write_parquet(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write_parquet(tmp_path, child, "child"),
                "format": "parquet",
            },
        },
        "targets": {
            n: {"type": "file", "path": str(tmp_path / f"{n}.out.parquet"), "format": "parquet"}
            for n in ("grandparent", "parent", "child")
        },
        "tables": [
            {
                "name": "grandparent",
                "columns": [_hash_col("gid", "gns"), {"name": "gnote", "strategy": "redact"}],
            },
            {"name": "parent", "columns": [_hash_col("id", "ns"), _hash_col("gp_id", "gns")]},
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "grandparent", "columns": ["gid"]},
                "children": [{"table": "parent", "columns": ["gp_id"]}],
                "orphan_policy": "preserve",
                "namespace": "gns",
            },
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            },
        ],
    }


def _star_config(tmp_path: Path, *, fan_in: int, parent_rows: int = 40) -> dict[str, Any]:
    """One child table carrying `fan_in` FK columns, each to its own parent, so
    the child has `fan_in` incoming edges. `_max_concurrent_ooc_instances`
    returns `fan_in`, which `resolve_ooc_memory_limit` divides the budget by to
    size the per-connection `memory_limit` -- at a near-floor budget a large
    `fan_in` makes that split un-sizeable and `resolve_ooc_memory_limit` raises
    `out_of_core_fanin_exceeds_budget` BEFORE the capacity evaluator runs."""
    sources: dict[str, Any] = {}
    tables: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []
    child_cols: dict[str, Any] = {"cid": pa.array([f"c{i}" for i in range(40)], type=pa.string())}
    child_tbl_cols: list[dict[str, Any]] = [_hash_col("cid", "cns")]
    for k in range(fan_in):
        pn = f"par{k}"
        pt = pa.table(
            {
                "id": pa.array([f"p{i}" for i in range(parent_rows)], type=pa.string()),
                "note": pa.array([f"s{i}" for i in range(parent_rows)], type=pa.string()),
            }
        )
        sources[pn] = {
            "type": "file",
            "path": _write_parquet(tmp_path, pt, pn),
            "format": "parquet",
        }
        tables.append(
            {
                "name": pn,
                "columns": [_hash_col("id", f"ns{k}"), {"name": "note", "strategy": "redact"}],
            }
        )
        child_cols[f"fk{k}"] = pa.array(
            [f"p{i % parent_rows}" for i in range(40)], type=pa.string()
        )
        child_tbl_cols.append(_hash_col(f"fk{k}", f"ns{k}"))
        rels.append(
            {
                "parent": {"table": pn, "columns": ["id"]},
                "children": [{"table": "child", "columns": [f"fk{k}"]}],
                "orphan_policy": "preserve",
                "namespace": f"ns{k}",
            }
        )
    sources["child"] = {
        "type": "file",
        "path": _write_parquet(tmp_path, pa.table(child_cols), "child"),
        "format": "parquet",
    }
    tables.append({"name": "child", "columns": child_tbl_cols})
    return {
        "version": 1,
        "global_settings": {"job_name": "capacity-star", "seed": 7},
        "sources": sources,
        "targets": {
            n: {"type": "file", "path": str(tmp_path / f"{n}.out.parquet"), "format": "parquet"}
            for n in sources
        },
        "tables": tables,
        "relationships": rels,
    }


def _generate_plus_mask_config(tmp_path: Path) -> dict[str, Any]:
    """The `_ooc_config` pure-mask FK pair PLUS one standalone GENERATE table,
    so `has_generate_table` is True. A generate+mask job is out-of-core
    INELIGIBLE (the sequential path masks table by table; generate tables never
    run through it), so it is full-frame-bound and routes `full_frame` at the
    real threshold -> NOT_APPLICABLE."""
    config = _ooc_config(tmp_path)
    config["targets"]["extra"] = {
        "type": "file",
        "path": str(tmp_path / "extra.out.parquet"),
        "format": "parquet",
    }
    config["tables"].append(
        {
            "name": "extra",
            "row_count": 3,
            "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
        }
    )
    return config


def _reject_code_config(tmp_path: Path) -> dict[str, Any]:
    """A job that is BOTH out-of-core-INCOMPATIBLE (a `faker` payload -> a real,
    non-None `out_of_core_reject_code`) AND not sequential-eligible (validators
    present), so at a lowered reject threshold it is rejected-before-read. Its
    reject message interpolates both `out_of_core_reject_code` and the
    sequential reason, so nulling either kwarg into the router changes it."""
    config = _ooc_config(tmp_path)
    config["tables"][0]["columns"] = [
        _hash_col("id", "ns"),
        {"name": "note", "strategy": "faker", "provider": "person_email"},
    ]
    config["validators"] = [{"type": "row_count", "table": "parent"}]
    return config


@pytest.fixture
def low_threshold(monkeypatch):
    real_decide = capacity_mod.decide_execution_route

    def _patched(*args, **kwargs):
        kwargs.setdefault("out_of_core_threshold_rows", 10)
        kwargs.setdefault("full_frame_reject_rows", 10)
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(capacity_mod, "decide_execution_route", _patched)


# --------------------------------------------------------------------------
# _resolved_config_copy: direct helper oracles
# --------------------------------------------------------------------------
class TestResolvedConfigCopy:
    def test_non_dict_sources_returned_unchanged(self, tmp_path: Path) -> None:
        # `not isinstance(sources, dict) or not sources` early-returns the
        # config verbatim when `sources` is not a dict. A `or`->`and` flip
        # (mut_5) makes it fall through and call `.items()` on the string.
        cfg = {"version": 1, "sources": "not-a-dict-at-all"}
        out = _resolved_config_copy(cfg, tmp_path)
        assert out is cfg
        assert out["sources"] == "not-a-dict-at-all"

    def test_non_dict_descriptor_passed_through_untouched(self, tmp_path: Path) -> None:
        # A non-dict source descriptor must be copied through verbatim. The
        # `isinstance(descriptor, dict) and ...` guard short-circuits; an
        # `and`->`or` flip (mut_10) calls `.get()` on the bare string.
        cfg = {"version": 1, "sources": {"t": "descriptor-is-a-string"}}
        out = _resolved_config_copy(cfg, tmp_path)
        assert out["sources"]["t"] == "descriptor-is-a-string"

    def test_non_file_source_path_is_not_resolved(self, tmp_path: Path) -> None:
        # Only `type == "file"` string paths get resolved. A precedence break
        # (mut_9, `and ... == "file" or isinstance(path, str)`) would resolve
        # a NON-file source's relative path to an absolute one.
        cfg = {
            "version": 1,
            "sources": {"db": {"type": "database", "path": "relative/only.db"}},
        }
        out = _resolved_config_copy(cfg, tmp_path)
        assert out["sources"]["db"]["path"] == "relative/only.db"

    def test_file_source_relative_path_becomes_absolute(self, tmp_path: Path) -> None:
        # The positive path: a file source with a relative path is rewritten
        # to an absolute string under base_dir.
        cfg = {
            "version": 1,
            "sources": {"f": {"type": "file", "path": "sub/data.parquet", "format": "parquet"}},
        }
        out = _resolved_config_copy(cfg, tmp_path)
        resolved = out["sources"]["f"]["path"]
        assert Path(resolved).is_absolute()
        assert resolved == str((tmp_path / "sub/data.parquet").resolve())
        # original dict not mutated in place
        assert cfg["sources"]["f"]["path"] == "sub/data.parquet"


# --------------------------------------------------------------------------
# NOT_APPLICABLE branches: verdict + route + code + message-presence
# --------------------------------------------------------------------------
class TestNotApplicableMachineFields:
    def test_no_relationships_fields(self, tmp_path: Path) -> None:
        config = {
            "version": 1,
            "global_settings": {"job_name": "no-fk", "seed": 1},
            "sources": {},
            "targets": {},
            "tables": [],
        }
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.code is None
        assert est.route == "none"
        assert est.needed_bytes is None
        assert est.available_bytes is None
        # The message is the exact CLI-rendered contract string for this verdict
        # (the docstring calls `message` "the public contract a CLI caller
        # renders"), so pin it exactly: `None`, an XX-wrap, or a case flip all
        # change what the operator sees.
        assert est.message == (
            "this job declares no relationships; the out-of-core-FK capacity check "
            "only applies to jobs with FK relationships."
        )

    def test_route_not_ooc_fields(self, tmp_path: Path) -> None:
        # `faker` is out-of-core-INCOMPATIBLE, so the row-count route lands on
        # `sequential` and stays NOT_APPLICABLE (no probe ambiguity).
        parent, child = _parent_child_tables()
        config = _ooc_config(tmp_path, tables=(parent, child))
        config["tables"][0]["columns"] = [
            _hash_col("id", "ns"),
            {"name": "note", "strategy": "faker", "provider": "person_email"},
        ]
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.route == "sequential"
        # Exact rendered contract message (route interpolated via {route!r}).
        assert est.message == (
            "this job's execution route is 'sequential', not out-of-core-FK; v1's "
            "capacity check only covers the out-of-core-FK route."
        )


# --------------------------------------------------------------------------
# routing-guard reject fold: NOT_APPLICABLE with the rejected_before_read label
# --------------------------------------------------------------------------
class TestRejectBeforeReadLabel:
    def test_reject_code_folds_with_correct_route_and_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        config = _ooc_config(tmp_path)

        def _reject(*_a: Any, **_k: Any) -> Any:
            raise ExecutionError(
                code="fk_full_frame_oom_risk_rejected",
                message="full-frame OOM risk (test double).",
            )

        monkeypatch.setattr(capacity_mod, "decide_execution_route", _reject)
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        # route label is the fixed sentinel (mut_131 None, mut_135/136 case/wrap).
        assert est.route == "rejected_before_read"
        # Exact rendered contract message (reject code + reason interpolated).
        assert est.message == (
            "this job would be rejected before read by the engine's routing guard "
            "(fk_full_frame_oom_risk_rejected): full-frame OOM risk (test double). "
            "The out-of-core-FK capacity check only applies to a job that actually "
            "reaches that route."
        )

    def test_byte_probe_reject_is_swallowed_not_propagated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The SECOND (byte-estimate) decide_execution_route call: a genuine
        # reject-before-read there is the worst case and is swallowed
        # (byte_route=None -> route stays as the 1st call's non-ooc answer),
        # never re-raised. `not in`->`in` (mut_190) would re-raise it.
        config = _ooc_config(tmp_path)

        def _fake(*_a: Any, **kwargs: Any) -> Any:
            if kwargs.get("use_byte_estimate_routing"):
                raise ExecutionError(
                    code="fk_full_frame_oom_risk_rejected_estimated",
                    message="byte-estimate reject (test double).",
                )
            return ("sequential", "row-count route chose sequential (test double).")

        monkeypatch.setattr(capacity_mod, "decide_execution_route", _fake)
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.route == "sequential"


# --------------------------------------------------------------------------
# UNKNOWN (P1-1 probe promotion): below-threshold but ooc-compatible
# --------------------------------------------------------------------------
class TestProbePromotionUnknown:
    def test_below_threshold_ooc_compatible_is_unknown_out_of_core(self, tmp_path: Path) -> None:
        # No low_threshold: 40 rows < the real 5M threshold, so the row-count
        # route is `sequential`, but the byte-estimate probe (2nd call, byte
        # routing ON, full_frame_fits_estimate=False) promotes it to
        # out_of_core, forcing UNKNOWN (the anti-under-refusal downgrade).
        config = _ooc_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.UNKNOWN
        assert est.route == "out_of_core"
        # The downgrade rewrites the message to the exact anti-under-refusal
        # explanation (then appends the wrapped inner reason). Pin the static
        # prefix exactly so any XX-wrap / case flip of that contract text fails.
        assert est.message is not None
        assert est.message.startswith(
            "this job is out-of-core-FK-compatible but its row-count-only "
            "route is not out-of-core (likely below out_of_core_threshold_"
            "rows); a real `decoy run` (byte-estimate routing on by "
            "default) can ignore that threshold and route it to "
            "out-of-core-FK regardless of size. This checker cannot "
            "compute the real byte-level estimate without materializing "
            "resident source data (R6), so it cannot confirm which route "
            "a real run would take; capacity is not checked with "
            "confidence for this job. ("
        )


# --------------------------------------------------------------------------
# FIT / INSUFFICIENT / UNKNOWN through the real routing + budget math
# --------------------------------------------------------------------------
class TestPricedVerdicts:
    def test_fit_fields(self, tmp_path: Path, low_threshold) -> None:
        config = _ooc_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.FIT
        assert est.route == "out_of_core"
        assert est.code is None

    def test_insufficient_fields(self, tmp_path: Path, low_threshold) -> None:
        big_parent, big_child = _parent_child_tables(300_000)
        config = _ooc_config(tmp_path, tables=(big_parent, big_child))
        est = estimate_job_capacity(config, tmp_path, budget_bytes=1 * _MIB)
        assert est.verdict is CapacityVerdict.INSUFFICIENT
        assert est.route == "out_of_core"
        assert est.code in {"out_of_core_insufficient_memory", "out_of_core_fanin_exceeds_budget"}
        assert est.needed_bytes is None or est.needed_bytes > 0

    def test_csv_parent_forces_unknown(self, tmp_path: Path, low_threshold) -> None:
        config = _ooc_config(tmp_path, parent_fmt="csv", child_fmt="csv")
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.UNKNOWN
        assert "parent" in est.message


# --------------------------------------------------------------------------
# sink flag correctness in the priced floor/cap (needs a build table that
# also has an incoming edge -> the only shape where sink True/False diverge).
# --------------------------------------------------------------------------
class TestSinkFalseInBuildFloor:
    def test_mid_level_build_table_priced_with_incoming_fanin(
        self, tmp_path: Path, low_threshold
    ) -> None:
        # grandparent<-parent<-child; `parent` has 300k rows AND one incoming
        # edge. At a 128 MiB budget the parent's ~82 MB build floor exceeds
        # the sink=False build cap (~64 MB, live=incoming+1=2) but would clear
        # a sink=True cap (~128 MB, live=1). estimate_job_capacity ALWAYS
        # passes sink=False (decoy run never streams to a sink), so the honest
        # verdict here is INSUFFICIENT; a sink=True mutation (mut_250) flips it
        # to FIT.
        config = _three_level_config(tmp_path, parent_rows=300_000)
        est = estimate_job_capacity(config, tmp_path, budget_bytes=128 * _MIB)
        assert est.verdict is CapacityVerdict.INSUFFICIENT
        assert est.binding_table == "parent"
        assert est.code == "out_of_core_insufficient_memory"


# --------------------------------------------------------------------------
# corrupt source: typed ExecutionError with a real (non-None) message
# --------------------------------------------------------------------------
class TestCorruptSourceMessage:
    def test_typed_code_and_message_present(self, tmp_path: Path) -> None:
        config = _ooc_config(tmp_path)
        (tmp_path / "child.parquet").write_bytes(b"NOT_A_PARQUET_FILE_corrupt")
        with pytest.raises(ExecutionError) as ei:
            estimate_job_capacity(config, tmp_path)
        assert ei.value.code == "capacity_source_unprofilable"
        # The message wraps the raw reader error in a fixed, operator-facing
        # explanation. Its static head/tail are contract text (the middle
        # interpolates the reader exception), so pin them exactly: a nulled,
        # dropped, XX-wrapped, or case-flipped head/tail all change what a
        # `decoy preflight` caller renders for a bad source file.
        assert ei.value.message is not None
        assert ei.value.message.startswith(
            "a declared source could not be read or parsed while estimating capacity ("
        )
        assert ei.value.message.endswith(
            "); the file may be truncated, corrupt, or not the declared format."
        )


# --------------------------------------------------------------------------
# validators force the full-frame path -> rejected-before-read NOT_APPLICABLE.
# The estimator reads validators from the config via `config.get("validators")
# or []`; dropping them (None, `and []`, or a corrupted key lookup) would let
# the job route to out_of_core and report FIT instead.
# --------------------------------------------------------------------------
class TestValidatorsForceRejectBeforeRead:
    def test_validators_job_is_not_applicable_not_fit(self, tmp_path: Path, low_threshold) -> None:
        config = _ooc_config(tmp_path)
        config["validators"] = [{"type": "row_count", "table": "parent"}]
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        # A real validators job requires a full-frame fidelity pass, which the
        # routing guard rejects before read -> NOT_APPLICABLE. If the validators
        # list is dropped (mut_93 None, mut_119 `and []`, mut_120/121/122 wrong
        # key -> []), the job routes to out_of_core and reports FIT instead.
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.route == "rejected_before_read"
        assert est.verdict is not CapacityVerdict.FIT


# --------------------------------------------------------------------------
# fan-in budget guard: at a near-floor budget a high FK fan-in makes the
# per-connection memory split un-sizeable, so resolve_ooc_memory_limit raises
# `out_of_core_fanin_exceeds_budget` up front -- and, with an EXPLICIT budget,
# that exception propagates (only host-RAM-undetection is folded to UNKNOWN).
# The `max_concurrent_instances` argument is what carries the fan-in into that
# guard; nulling/removing it (mut_224/232/234) sends the default instead, so
# the resolve step no longer raises and the job is mispriced as a verdict.
# --------------------------------------------------------------------------
class TestHighFanInBudgetGuardPropagates:
    def test_high_fanin_tiny_budget_raises_fanin_code(self, tmp_path: Path, low_threshold) -> None:
        config = _star_config(tmp_path, fan_in=68)
        with pytest.raises(ExecutionError) as ei:
            estimate_job_capacity(config, tmp_path, budget_bytes=64 * _MIB)
        assert ei.value.code == "out_of_core_fanin_exceeds_budget"


# --------------------------------------------------------------------------
# Full-struct golden oracles for the kwargs passed INTO decide_execution_route.
#
# Both `decide_execution_route` call sites in `estimate_job_capacity` (the
# row-count-only decision and the byte-estimate probe) forward a long kwarg
# list; a surviving mutation class nulls one of those argument values. Nulling
# a load-bearing kwarg re-routes the job, which changes the FULL returned
# `CapacityEstimate`. These oracles pin every field of that struct for a set of
# job shapes chosen so each kwarg is load-bearing in at least one of them, so a
# null substitution differs from the pinned golden.
#
# Every golden was read off the REAL (unmutated) code and hardcoded here as a
# literal (not recomputed from a constant), so a silent drift in the routing or
# the memory model fails these too. Explicit budgets pin `available_bytes` /
# `cap_bytes` deterministically instead of depending on the host's detected RAM.
# --------------------------------------------------------------------------
class TestRouteKwargFullStructKills:
    def test_fit_out_of_core_full_struct(self, tmp_path: Path, low_threshold) -> None:
        # out_of_core route, FIT. Nulling has_mask_table / out_of_core_compatible
        # / largest_table_rows / resolved_substrate on the row-count call all
        # drop the job off the out_of_core route (to sequential -> NOT_APPLICABLE
        # or a raised route error), so the full struct diverges from this golden.
        config = _ooc_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est == CapacityEstimate(
            verdict=CapacityVerdict.FIT,
            code=None,
            needed_bytes=3221225472,
            available_bytes=68719476736,
            route="out_of_core",
            message="capacity check passes; no table nears its build cap.",
            warned=False,
            binding_table="parent",
            floor_bytes=25173424,
            cap_bytes=65536000000,
        )

    def test_insufficient_out_of_core_full_struct(self, tmp_path: Path, low_threshold) -> None:
        # out_of_core route, INSUFFICIENT (300k-row parent, 1 MiB budget floored
        # to the 64 MiB minimum). Same route-carrying kwargs are load-bearing;
        # this shape additionally pins the INSUFFICIENT code / needed_bytes /
        # floor_bytes / cap_bytes / binding_table the FIT shape cannot.
        big_parent, big_child = _parent_child_tables(300_000)
        config = _ooc_config(tmp_path, tables=(big_parent, big_child))
        est = estimate_job_capacity(config, tmp_path, budget_bytes=1 * _MIB)
        assert est == CapacityEstimate(
            verdict=CapacityVerdict.INSUFFICIENT,
            code="out_of_core_insufficient_memory",
            needed_bytes=3221225472,
            available_bytes=67108864,
            route="out_of_core",
            message=(
                "predicted resident floor ~0.08 GiB for table 'parent' exceeds the "
                "actual build cap ~0.06 GiB it would receive; this job needs "
                "approximately 3 GB of memory (a host/cgroup ceiling that size). "
                "Increase host/cgroup memory or reduce table size."
            ),
            warned=False,
            binding_table="parent",
            floor_bytes=82165824,
            cap_bytes=64000000,
        )

    def test_probe_promoted_unknown_full_struct(self, tmp_path: Path) -> None:
        # No low_threshold: 40 rows is below the real out_of_core_threshold_rows,
        # so the row-count call routes `sequential` and the byte-estimate PROBE
        # call promotes it to out_of_core, forcing the UNKNOWN downgrade. This is
        # the shape that makes the SECOND (byte-estimate) call's kwargs
        # load-bearing: nulling has_mask_table / out_of_core_compatible /
        # use_byte_estimate_routing / resolved_substrate there, or flipping
        # full_frame_fits_estimate, stops the promotion and the verdict/route/
        # message all change.
        config = _ooc_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est == CapacityEstimate(
            verdict=CapacityVerdict.UNKNOWN,
            code=None,
            needed_bytes=3221225472,
            available_bytes=68719476736,
            route="out_of_core",
            message=(
                "this job is out-of-core-FK-compatible but its row-count-only "
                "route is not out-of-core (likely below out_of_core_threshold_"
                "rows); a real `decoy run` (byte-estimate routing on by "
                "default) can ignore that threshold and route it to "
                "out-of-core-FK regardless of size. This checker cannot "
                "compute the real byte-level estimate without materializing "
                "resident source data (R6), so it cannot confirm which route "
                "a real run would take; capacity is not checked with "
                "confidence for this job. (capacity check passes; no table "
                "nears its build cap.)"
            ),
            warned=False,
            binding_table="parent",
            floor_bytes=25173424,
            cap_bytes=65536000000,
        )

    def test_generate_plus_mask_full_struct(self, tmp_path: Path) -> None:
        # has_generate_table is True. No low_threshold, so the full-frame-bound
        # generate+mask job routes `full_frame` -> NOT_APPLICABLE. Nulling
        # has_generate_table on the row-count call makes the job look pure-mask,
        # which reroutes it (to sequential here) and changes the struct.
        config = _generate_plus_mask_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est == CapacityEstimate(
            verdict=CapacityVerdict.NOT_APPLICABLE,
            code=None,
            needed_bytes=None,
            available_bytes=None,
            route="full_frame",
            message=(
                "this job's execution route is 'full_frame', not out-of-core-FK; "
                "v1's capacity check only covers the out-of-core-FK route."
            ),
            warned=False,
            binding_table=None,
            floor_bytes=0,
            cap_bytes=None,
        )

    def test_validators_compatible_probe_path_full_struct(self, tmp_path: Path) -> None:
        # validators present (not sequential-eligible) but out-of-core-COMPATIBLE
        # strategies, small, no low_threshold: the row-count call routes
        # full_frame, and because the job IS out_of_core_compatible the
        # byte-estimate probe call IS entered (it re-reads validators). Nulling
        # validators on EITHER call makes the job sequential-eligible and
        # promotes it (probe -> out_of_core -> UNKNOWN), diverging from this
        # NOT_APPLICABLE golden -- this is the shape that reaches the probe
        # call's `validators` kwarg.
        config = _ooc_config(tmp_path)
        config["validators"] = [{"type": "row_count", "table": "parent"}]
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est == CapacityEstimate(
            verdict=CapacityVerdict.NOT_APPLICABLE,
            code=None,
            needed_bytes=None,
            available_bytes=None,
            route="full_frame",
            message=(
                "this job's execution route is 'full_frame', not out-of-core-FK; "
                "v1's capacity check only covers the out-of-core-FK route."
            ),
            warned=False,
            binding_table=None,
            floor_bytes=0,
            cap_bytes=None,
        )

    def test_reject_before_read_full_struct(self, tmp_path: Path, low_threshold) -> None:
        # A large (at the lowered threshold), out-of-core-INCOMPATIBLE (faker),
        # not-sequential-eligible (validators) job is rejected-before-read. The
        # reject message interpolates out_of_core_reject_code
        # ("out_of_core_faker_pool_unsupported"), the sequential reason
        # ("validators_present"), the row count (40), and depends on
        # largest_table_rows_exact (True -> the non-estimated reject code). This
        # golden pins all of them, so nulling out_of_core_reject_code /
        # validators / largest_table_rows_exact / largest_table_rows on the
        # row-count call changes the rendered message or the route.
        config = _reject_code_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est == CapacityEstimate(
            verdict=CapacityVerdict.NOT_APPLICABLE,
            code=None,
            needed_bytes=None,
            available_bytes=None,
            route="rejected_before_read",
            message=(
                "this job would be rejected before read by the engine's routing "
                "guard (fk_full_frame_oom_risk_rejected): FK job rejected before "
                "read: largest mask table has 40 rows, at or above the full-frame "
                "reject threshold (10); full-frame FK masking at this scale would "
                "risk an out-of-memory kill. No bounded route applies -- the job "
                "is not out-of-core-eligible (out_of_core_faker_pool_unsupported) "
                "and not sequential-eligible (validators_present). Reduce the job "
                "size, make it out-of-core-eligible (supported strategies + an "
                "acyclic single-parent FK graph), or force "
                "execution_mode='full_frame' to override at your own memory risk. "
                "The out-of-core-FK capacity check only applies to a job that "
                "actually reaches that route."
            ),
            warned=False,
            binding_table=None,
            floor_bytes=0,
            cap_bytes=None,
        )
