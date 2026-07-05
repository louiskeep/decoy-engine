"""S2 (engine "Finish Open-Ended Surfaces" program): `run_pipeline` routing.

Covers the DoD items from the S2 build guide that are not the row-error
leak-closure proof (that lives in
`tests/integration/test_fk_sequential_row_error_leak.py`):

  - sequential-vs-full-frame byte equivalence for an eligible FK job
    (3.2 -- the whole point of Option 2: same bytes, lower peak memory).
  - no regression for non-FK / mixed generate+mask jobs (3.3): they take the
    untouched full-frame branch by construction.
  - per-config telemetry honesty (3.4, dennis hot-spot): `loaded_fully_in_memory`
    is only False when a lazy `source_loader` was actually supplied.
  - the `execution_mode="sequential"` override raising `ConfigError` when the
    job is not sequential-eligible (fail-closed, never silently ignored).

Every table needs a real backing file: `profile_source` profiles a table by
reading `config["sources"][name]` off disk, independent of the in-memory
`sources` dict `run_pipeline` actually masks (see
`test_fk_sequential_row_error_leak.py`'s module docstring for the same note).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._pipeline import _sequential_eligible, run_pipeline

_N = 20


def _faker_col(name: str, namespace: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": namespace,
    }


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _fk_tables() -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "age": pa.array([str(i * 10) for i in range(_N)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    return parent, child


def _fk_pure_mask_config(tmp_path: Path) -> dict[str, Any]:
    """A relationship-bearing pure-mask job: no generate tables, no
    validators, no fidelity_report -- sequential-eligible by construction."""
    parent, child = _fk_tables()
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "s2-routing-fk", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
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
                "columns": [
                    _faker_col("id", "ns"),
                    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                ],
            },
            {"name": "child", "columns": [_faker_col("parent_id", "ns")]},
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


def _fk_sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _no_relationships_config(tmp_path: Path) -> dict[str, Any]:
    """A single mask table with no relationships block: never eligible."""
    src = pa.table({"email": pa.array([f"u{i}@x.com" for i in range(5)], type=pa.string())})
    src_path = _write_source(tmp_path, src, "t")
    return {
        "version": 1,
        "global_settings": {"job_name": "s2-routing-no-fk", "seed": 7},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
        },
        "tables": [{"name": "t", "columns": [_faker_col("email", "ns")]}],
        "relationships": [],
    }


def _generate_plus_mask_config(tmp_path: Path) -> dict[str, Any]:
    """An FK-bearing job that ALSO has a generate table: disqualified even
    though relationships are declared."""
    parent, child = _fk_tables()
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "s2-routing-mixed", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
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
            "extra": {
                "type": "file",
                "path": str(tmp_path / "extra.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {"name": "parent", "columns": [_faker_col("id", "ns")]},
            {"name": "child", "columns": [_faker_col("parent_id", "ns")]},
            {
                "name": "extra",
                "row_count": 3,
                "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
            },
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


# --------------------------------------------------------------------------
# 3.1 unit: the predicate itself
# --------------------------------------------------------------------------


class _FakeProfile:
    def __init__(self, relationships: tuple[Any, ...]) -> None:
        self.relationships = relationships


class TestSequentialEligiblePredicate:
    def test_no_relationships_disqualifies(self) -> None:
        eligible, reason = _sequential_eligible(
            _FakeProfile(()),
            has_generate_table=False,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
        )
        assert (eligible, reason) == (False, "no_relationships")

    def test_generate_plus_mask_disqualifies(self) -> None:
        eligible, reason = _sequential_eligible(
            _FakeProfile((object(),)),
            has_generate_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
        )
        assert (eligible, reason) == (False, "generate_plus_mask")

    def test_validators_disqualifies(self) -> None:
        eligible, reason = _sequential_eligible(
            _FakeProfile((object(),)),
            has_generate_table=False,
            validators=[{"name": "luhn"}],
            fidelity_report=False,
            vault_writer=None,
        )
        assert (eligible, reason) == (False, "validators_present")

    def test_fidelity_report_disqualifies(self) -> None:
        eligible, reason = _sequential_eligible(
            _FakeProfile((object(),)),
            has_generate_table=False,
            validators=[],
            fidelity_report=True,
            vault_writer=None,
        )
        assert (eligible, reason) == (False, "fidelity_report_requested")

    def test_vault_writer_disqualifies(self) -> None:
        eligible, reason = _sequential_eligible(
            _FakeProfile((object(),)),
            has_generate_table=False,
            validators=[],
            fidelity_report=False,
            vault_writer=object(),
        )
        assert (eligible, reason) == (False, "vault_writer_requested")

    def test_pure_mask_fk_eligible(self) -> None:
        eligible, reason = _sequential_eligible(
            _FakeProfile((object(),)),
            has_generate_table=False,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
        )
        assert (eligible, reason) == (True, "pure_mask_fk")


# --------------------------------------------------------------------------
# 3.2 sequential-vs-full-frame byte equivalence
# --------------------------------------------------------------------------


class TestSequentialVsFullFrameEquivalence:
    def test_in_memory_outputs_byte_identical(self, tmp_path: Path) -> None:
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)

        full = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
        seq = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="sequential")

        assert full.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert seq.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert set(full.outputs) == set(seq.outputs) == {"parent", "child"}
        for table in full.outputs:
            assert seq.outputs[table].equals(full.outputs[table]), f"{table} differs"
        assert {(w.code, getattr(w, "detail", None)) for w in full.warnings} == {
            (w.code, getattr(w, "detail", None)) for w in seq.warnings
        }

    def test_sink_output_byte_identical_to_full_frame(self, tmp_path: Path) -> None:
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)

        full = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
        sink = ParquetTransactionalSink(tmp_path / "seq_out")
        seq = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode="sequential", sink=sink
        )
        assert seq.outputs == {}

        for table in full.outputs:
            sunk = pq.read_table(tmp_path / "seq_out" / f"{table}.parquet")
            assert sunk.equals(full.outputs[table]), f"{table} sink output differs"

    def test_auto_mode_selects_sequential_for_eligible_fk_job(self, tmp_path: Path) -> None:
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0")  # execution_mode="auto"
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert result.quality_metrics["execution"]["route_reason"] == "pure_mask_fk"


# --------------------------------------------------------------------------
# 3.3 non-FK / mixed no-regression
# --------------------------------------------------------------------------


class TestNonFkNoRegression:
    def test_no_relationships_job_takes_full_frame_branch(self, tmp_path: Path) -> None:
        config = _no_relationships_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0")
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert result.quality_metrics["execution"]["route_reason"] == "no_relationships"

    def test_generate_plus_mask_job_takes_full_frame_branch(self, tmp_path: Path) -> None:
        config = _generate_plus_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0")
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert result.quality_metrics["execution"]["route_reason"] == "generate_plus_mask"
        assert "extra" in result.outputs  # generate table still produced
        assert result.outputs["extra"].num_rows == 3

    def test_sequential_forced_on_ineligible_job_raises_config_error(self, tmp_path: Path) -> None:
        config = _no_relationships_config(tmp_path)
        sources = _fk_sources(config)
        with pytest.raises(ConfigError, match="no_relationships"):
            run_pipeline(config, sources, engine_version="0.1.0", execution_mode="sequential")

    def test_forced_sequential_without_mask_table_raises_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NIT (S2 remediation guide section 8): `execution_mode='sequential'`
        must fail closed rather than silently falling through to full-frame
        when the job has no mask-kind table. This combination (eligible per
        `_sequential_eligible` AND zero mask tables) cannot arise through the
        public schema -- a generate table already disqualifies via
        `generate_plus_mask`, and a job with zero tables has no relationships
        either -- so `_sequential_eligible` is monkeypatched to isolate the
        new guard from that unreachable-in-practice precondition."""
        import decoy_engine.execution._pipeline as pipeline_mod

        monkeypatch.setattr(
            pipeline_mod, "_sequential_eligible", lambda *a, **k: (True, "pure_mask_fk")
        )

        config: dict[str, Any] = {
            "version": 1,
            "global_settings": {"job_name": "s2-routing-nit", "seed": 7},
            "sources": {},
            "targets": {
                "seq": {
                    "type": "file",
                    "path": str(tmp_path / "seq.out.parquet"),
                    "format": "parquet",
                }
            },
            "tables": [
                {
                    "name": "seq",
                    "row_count": 3,
                    "generate_columns": [{"name": "n", "type": "sequence", "start": 1, "step": 1}],
                }
            ],
            "relationships": [],
        }
        with pytest.raises(ConfigError, match="no mask-kind table"):
            run_pipeline(config, {}, engine_version="0.1.0", execution_mode="sequential")


# --------------------------------------------------------------------------
# 3.4 telemetry honesty (dennis hot-spot)
# --------------------------------------------------------------------------


class TestTelemetryHonesty:
    def test_full_frame_claims_fully_resident_no_eviction(self, tmp_path: Path) -> None:
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
        exec_meta = result.quality_metrics["execution"]
        assert exec_meta["loaded_fully_in_memory"] is True
        assert exec_meta["eviction"] == "none"
        assert exec_meta["outputs_streamed"] is False

    def test_sequential_no_loader_still_claims_fully_resident_inputs(self, tmp_path: Path) -> None:
        """THE honesty assertion: sequential eviction bounds the pandas
        working set, but with no lazy loader the Arrow inputs are still
        fully resident (run_pipeline was handed a materialized dict)."""
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)
        result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="sequential")
        exec_meta = result.quality_metrics["execution"]
        assert exec_meta["execution_mode"] == "sequential"
        assert exec_meta["eviction"] == "per_table"
        assert exec_meta["outputs_streamed"] is False
        assert exec_meta["loaded_fully_in_memory"] is True

    def test_sequential_with_lazy_loader_and_nonempty_sources_still_resident(
        self, tmp_path: Path
    ) -> None:
        """MEDIUM (S2 remediation guide section 8): `run_pipeline` always builds
        `caller_sources = dict(sources)`, so a NON-empty `sources` dict means the
        inputs ARE resident even though a lazy `source_loader` was ALSO supplied
        (the loader is simply unused in this call shape). The honesty fix reports
        residency from `sources`, not merely from whether a loader was passed."""
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)

        def lazy_loader(table: str) -> pa.Table:
            return sources[table]

        sink = ParquetTransactionalSink(tmp_path / "bounded_out")
        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            execution_mode="sequential",
            sink=sink,
            source_loader=lazy_loader,
        )
        exec_meta = result.quality_metrics["execution"]
        assert exec_meta["loaded_fully_in_memory"] is True
        assert exec_meta["outputs_streamed"] is True

    def test_sequential_with_lazy_loader_and_empty_sources_claims_bounded(
        self, tmp_path: Path
    ) -> None:
        """The one configuration that actually bounds input residency: a lazy
        `source_loader` supplied AND `sources` empty/omitted."""
        config = _fk_pure_mask_config(tmp_path)
        sources = _fk_sources(config)

        def lazy_loader(table: str) -> pa.Table:
            return sources[table]

        sink = ParquetTransactionalSink(tmp_path / "bounded_out")
        result = run_pipeline(
            config,
            sources=None,
            engine_version="0.1.0",
            execution_mode="sequential",
            sink=sink,
            source_loader=lazy_loader,
        )
        exec_meta = result.quality_metrics["execution"]
        assert exec_meta["loaded_fully_in_memory"] is False
        assert exec_meta["outputs_streamed"] is True


# --------------------------------------------------------------------------
# byte-parity: _parent_map(key_error_rows=None) is a strict no-op
# --------------------------------------------------------------------------


class TestParentMapKeyErrorExclusionNoOp:
    """S2 remediation guide sections 5 and 7.2: `_parent_map` must build the
    byte-identical map to before the fix when there is no key-error index
    (the overwhelmingly common case -- no parent-key row-errors)."""

    def test_no_op_when_key_error_rows_none_or_empty(self) -> None:
        import pandas as pd

        from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
        from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

        adapter = PandasExecutionAdapter()
        edge = RelationshipEdge(
            parent_table="parent",
            parent_columns=("id",),
            child_table="child",
            child_columns=("parent_id",),
            namespace="ns",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
        frame = pd.DataFrame({"id": ["p0", "p1", "p2"]})
        frames = {"parent": frame}
        source_snapshots = {("parent", "id"): frame["id"].copy()}

        pre_fix_shape = adapter._parent_map(edge, frames, source_snapshots, {})
        with_none = adapter._parent_map(
            edge, frames, source_snapshots, {}, key_error_rows=None, errored_keys_cache=None
        )
        with_empty = adapter._parent_map(
            edge, frames, source_snapshots, {}, key_error_rows={}, errored_keys_cache={}
        )

        assert pre_fix_shape == with_none == with_empty
        assert pre_fix_shape == {("p0",): ("p0",), ("p1",): ("p1",), ("p2",): ("p2",)}
