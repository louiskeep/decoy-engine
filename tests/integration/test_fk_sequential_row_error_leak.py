"""S2 (engine "Finish Open-Ended Surfaces" program) THE blocker proof.

`run_pipeline`'s ROLLBACK routing (`execution_mode="auto"` with
`use_byte_estimate_routing=False`, which these tests pin) routes a
relationship-bearing pure-mask job through `run_sequential` (see `_pipeline.py`,
`_sequential_eligible`); TB-5's default byte-estimate routing would size these
tiny fixtures as fitting full_frame, so the pin is what keeps them on the
sequential path under test. Because `run_sequential` is a public-entry-point FK
mask path, the S1 honesty-pack
fail-loud/quarantine guarantee MUST hold there exactly as it holds on the
full-frame `run()` path (mirrors `tests/integration/test_when_gate_row_error_leak.py`
and `tests/integration/test_row_errors_e2e.py`, but with a two-table FK job
routed via a `ParquetTransactionalSink`).

Shape: a parent/child FK pair (`parent.id` -> `child.parent_id`, one edge, no
orphans). The PARENT has a `bucketize` column with one uncoercible cell
("badX"). Asserts:
  (a) leak closed: the raw "badX" is absent from the committed parquet output.
  (b) quarantine JSONL carries the real bad value.
  (c) the innocent row is preserved; exactly one row removed.
  (d) fail-loud BEFORE commit: with no covering quarantine trigger,
      `run_pipeline` raises `RowErrorsFailedError` and the sink's target
      directory is never published (transactional abort, nothing committed).

Note: `profile_source` profiles a table by reading `config["sources"][name]`
straight off disk (see `profile/_source.py` module docstring), so every
table needs a real backing file even though `run_pipeline`'s `sources` kwarg
carries the in-memory Arrow tables actually masked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._pipeline import run_pipeline

_AGE = ["10", "20", "30", "40", "50", "badX"]
_IDS = ["p0", "p1", "p2", "p3", "p4", "p5"]


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


def _parent_table(*, gated: bool) -> pa.Table:
    cols: dict[str, Any] = {
        "id": pa.array(_IDS, type=pa.string()),
        "age": pa.array(_AGE, type=pa.string()),
    }
    if gated:
        # dennis's exact reproduction shape: keep=[0,0,0,1,1,1], the bad cell is
        # the last GATED row; "30" is an innocent UNMATCHED (keep=0) row.
        cols["keep"] = [0, 0, 0, 1, 1, 1]
    return pa.table(cols)


def _child_table() -> pa.Table:
    return pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
            "parent_id": pa.array(_IDS, type=pa.string()),
        }
    )


def _fk_config(
    tmp_path: Path,
    *,
    gated: bool = False,
    when: str | None = None,
    quarantine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent_src = _write_source(tmp_path, _parent_table(gated=gated), "parent")
    child_src = _write_source(tmp_path, _child_table(), "child")

    age_col: dict[str, Any] = {
        "name": "age",
        "strategy": "bucketize",
        "provider_config": {"width": 10},
    }
    parent_columns = [_faker_col("id", "parent_ns"), age_col]
    if gated:
        age_col["when"] = when
        parent_columns.append({"name": "keep", "strategy": "passthrough"})

    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "s2-fk-sequential-leak", "seed": 42},
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
            {"name": "parent", "columns": parent_columns},
            {"name": "child", "columns": [_faker_col("parent_id", "parent_ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "parent_ns",
            }
        ],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


class TestFkSequentialLeakClosure:
    """Ungated: `age` has no `when`, so every row is a mask candidate."""

    def test_leak_closed_and_innocent_row_preserved(self, tmp_path: Path) -> None:
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _fk_config(
            tmp_path,
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        result = run_pipeline(
            config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
        )

        # The sequential route was taken (auto-eligible FK pure-mask job):
        # result.outputs is empty because a sink was supplied.
        assert result.outputs == {}
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert result.quality_metrics["execution"]["route_reason"] == "pure_mask_fk"

        # (a) leak closed: "badX" absent from the committed parquet.
        out_parent = pq.read_table(tmp_path / "out" / "parent.parquet")
        out_age = out_parent.column("age").to_pylist()
        assert "badX" not in out_age

        # (c) innocent row preserved; exactly one row removed.
        assert "30" in out_age
        assert out_parent.num_rows == 5

        # (b) quarantine JSONL carries the real bad value.
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"
        assert records[0]["_quarantine_trigger"] == "format_error"

    def test_no_quarantine_fails_loud_before_commit(self, tmp_path: Path) -> None:
        config = _fk_config(tmp_path)  # no quarantine block: format_error is uncovered
        sources = _sources(config)
        target = tmp_path / "out2"
        sink = ParquetTransactionalSink(target)

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        # (d) fail-loud before commit: nothing published.
        assert not target.exists()
        recs = [r for r in exc_info.value.records if r.table == "parent"]
        assert len(recs) == 1
        # Full-table position of "badX" (index 5).
        assert recs[0].row_index == 5
        assert recs[0].trigger == "format_error"
        # No cell value leaks into the exception message (trap T3).
        assert "badX" not in str(exc_info.value)


class TestFkSequentialLeakClosureWhenGated:
    """Gated variant: `age` carries a `when` predicate, proving the S1
    subset-index remap (full-table row_index, not gated-subset-relative) also
    holds on the sequential path."""

    def test_leak_closed_and_innocent_row_preserved(self, tmp_path: Path) -> None:
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _fk_config(
            tmp_path,
            gated=True,
            when="keep == 1",
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        result = run_pipeline(
            config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
        )
        assert result.outputs == {}

        out_parent = pq.read_table(tmp_path / "out" / "parent.parquet")
        out_age = out_parent.column("age").to_pylist()
        assert "badX" not in out_age
        # "30" is the innocent UNMATCHED (keep=0) row; must not be deleted by a
        # mis-indexed filter.
        assert "30" in out_age
        assert out_parent.num_rows == 5

        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"

    def test_no_quarantine_fails_loud_full_table_index(self, tmp_path: Path) -> None:
        config = _fk_config(tmp_path, gated=True, when="keep == 1")
        sources = _sources(config)
        target = tmp_path / "out2"
        sink = ParquetTransactionalSink(target)

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        assert not target.exists()
        recs = [r for r in exc_info.value.records if r.table == "parent"]
        assert len(recs) == 1
        # The full-table position of "badX" is 5, not the gated-subset position 2.
        assert recs[0].row_index == 5


_KEY_DATES = [
    "2020-01-01",
    "2020-02-01",
    "2020-03-01",
    "2020-04-01",
    "2020-05-01",
    "notadate",
]


def _key_error_config(
    tmp_path: Path,
    *,
    orphan_policy: str = "preserve",
    quarantine: dict[str, Any] | None = None,
    extra_orphan_child: bool = False,
) -> dict[str, Any]:
    """S2 remediation guide 7.1: the FK KEY column itself (`id`) is masked by
    `date_shift` (a row-error-emitting strategy), unlike the SAFE cases above
    which mask the key via faker (never errors). One child row per parent row,
    `parent_id` carrying the same values, so the child leaks the raw errored
    key 1:1 if the EXCLUDE-then-CASCADE fix is not in place."""
    child_parent_ids = list(_KEY_DATES)
    child_ids = [f"c{i}" for i in range(len(_KEY_DATES))]
    if extra_orphan_child:
        # A genuine orphan: a child key that exists in NO parent row at all.
        child_ids.append("c_orphan")
        child_parent_ids.append("2099-12-31")

    parent = pa.table({"id": pa.array(_KEY_DATES, type=pa.string())})
    child = pa.table(
        {
            "id": pa.array(child_ids, type=pa.string()),
            "parent_id": pa.array(child_parent_ids, type=pa.string()),
        }
    )
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")

    id_col: dict[str, Any] = {
        "name": "id",
        "strategy": "date_shift",
        "provider_config": {"min_days": 1, "max_days": 30},
        "namespace": "parent_ns",
    }
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "s2-fk-key-error-leak", "seed": 42},
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
            {"name": "parent", "columns": [id_col]},
            {"name": "child", "columns": [_faker_col("parent_id", "parent_ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": orphan_policy,
                "namespace": "parent_ns",
            }
        ],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


class TestFkKeyColumnErrorLeakClosure:
    """S2 remediation guide 7.1: THE blocker proof. A row-errored FK KEY
    column (not a non-key column, as in the SAFE cases above) must never
    leak its raw value through a child FK, on either execution path."""

    @pytest.mark.parametrize("execution_mode", ["full_frame", "sequential"])
    def test_raw_key_absent_from_both_outputs(self, tmp_path: Path, execution_mode: str) -> None:
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _key_error_config(
            tmp_path,
            orphan_policy="preserve",
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = (
            ParquetTransactionalSink(tmp_path / "out") if execution_mode == "sequential" else None
        )

        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            execution_mode=execution_mode,
            sink=sink,
        )

        if execution_mode == "sequential":
            parent_ids = pq.read_table(tmp_path / "out" / "parent.parquet").column("id").to_pylist()
            child_parent_ids = (
                pq.read_table(tmp_path / "out" / "child.parquet").column("parent_id").to_pylist()
            )
        else:
            parent_ids = result.outputs["parent"].column("id").to_pylist()
            child_parent_ids = result.outputs["child"].column("parent_id").to_pylist()

        # (a) leak closed on the parent side (pre-existing quarantine behavior).
        assert "notadate" not in parent_ids
        # (b) THE assertion: raw errored key absent from the CHILD output too.
        # This is the one that FAILS on 56ca3a9 before the fix.
        assert "notadate" not in child_parent_ids
        # (c) exactly one parent row and one child row removed.
        assert len(parent_ids) == 5
        assert len(child_parent_ids) == 5

        # (d) quarantine JSONL carries both a parent entry and a cascaded
        # child entry (masked=None, same trigger, attributed to "child").
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        parent_recs = [r for r in records if r["_source_table"] == "parent"]
        child_recs = [r for r in records if r["_source_table"] == "child"]
        assert len(parent_recs) == 1
        assert parent_recs[0]["id"] == "notadate"
        assert parent_recs[0]["_quarantine_trigger"] == "format_error"
        assert len(child_recs) == 1
        assert child_recs[0]["parent_id"] is None
        assert child_recs[0]["_quarantine_trigger"] == "format_error"
        assert "parent-key" in child_recs[0]["_quarantine_reason"]

    @pytest.mark.parametrize("execution_mode", ["full_frame", "sequential"])
    def test_fail_loud_without_quarantine_no_raw_key_in_exception(
        self, tmp_path: Path, execution_mode: str
    ) -> None:
        config = _key_error_config(tmp_path, orphan_policy="preserve")  # no quarantine block
        sources = _sources(config)
        target = tmp_path / "out2"
        sink = ParquetTransactionalSink(target) if execution_mode == "sequential" else None

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(
                config,
                sources,
                engine_version="0.1.0",
                execution_mode=execution_mode,
                sink=sink,
            )

        if execution_mode == "sequential":
            assert not target.exists()
        # Trap T3: no cell value leaks into the exception message.
        assert "notadate" not in str(exc_info.value)
        parent_recs = [r for r in exc_info.value.records if r.table == "parent"]
        assert len(parent_recs) == 1
        assert parent_recs[0].row_index == 5
        assert parent_recs[0].trigger == "format_error"

    @pytest.mark.parametrize("orphan_policy", ["fail", "remap"])
    @pytest.mark.parametrize("execution_mode", ["full_frame", "sequential"])
    def test_policy_sweep_raw_key_absent_from_child(
        self, tmp_path: Path, execution_mode: str, orphan_policy: str
    ) -> None:
        """S2 remediation guide section 4: for EVERY orphan_policy, a child of
        a row-errored parent key is cascade-quarantined (covered), never
        raised as an orphan_fk_violation and never remapped through the
        failing strategy."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _key_error_config(
            tmp_path,
            orphan_policy=orphan_policy,
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = (
            ParquetTransactionalSink(tmp_path / "out") if execution_mode == "sequential" else None
        )

        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            execution_mode=execution_mode,
            sink=sink,
        )

        if execution_mode == "sequential":
            child_parent_ids = (
                pq.read_table(tmp_path / "out" / "child.parquet").column("parent_id").to_pylist()
            )
        else:
            child_parent_ids = result.outputs["child"].column("parent_id").to_pylist()

        assert "notadate" not in child_parent_ids
        assert len(child_parent_ids) == 5

    @pytest.mark.parametrize("orphan_policy", ["preserve", "remap", "fail"])
    def test_genuine_orphan_untouched_by_the_fix(self, tmp_path: Path, orphan_policy: str) -> None:
        """A genuine orphan (a child key absent from EVERY parent row, not a
        row-errored one) must keep its pre-fix behavior exactly: PRESERVE
        keeps it, REMAP masks it via the parent strategy, FAIL raises."""
        config = _key_error_config(
            tmp_path,
            orphan_policy=orphan_policy,
            quarantine={
                "enabled": True,
                "output_path": str(tmp_path / "quarantine.jsonl"),
                "triggers": ["format_error"],
            },
            extra_orphan_child=True,
        )
        sources = _sources(config)

        if orphan_policy == "fail":
            from decoy_engine.execution._errors import ExecutionError

            with pytest.raises(ExecutionError) as exc_info:
                run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
            assert exc_info.value.code == "orphan_fk_violation"
            return

        result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
        child_parent_ids = result.outputs["child"].column("parent_id").to_pylist()
        # The row-errored key is still absent (the fix holds alongside a
        # genuine orphan in the same job).
        assert "notadate" not in child_parent_ids
        if orphan_policy == "preserve":
            # PRESERVE keeps the genuine orphan's source key unmasked.
            assert "2099-12-31" in child_parent_ids
        else:  # remap
            # REMAP masks the genuine orphan via the parent's date_shift
            # strategy; it must NOT equal the source key.
            assert "2099-12-31" not in child_parent_ids


class TestSequentialMultiTableQuarantineJsonlNotClobbered:
    """LOW-2 (S2 remediation guide section 7.5/8): the sequential path writes
    ONE quarantine JSONL after the whole loop (not per-table), specifically to
    avoid the truncating `_write_jsonl("w")` clobbering an earlier table's
    entries. A parent table with its own covered `format_error` (on a NON-key
    column) AND a child table with its own covered `format_error` (on a
    non-FK column) must BOTH appear in the single output file."""

    def test_both_tables_entries_present_in_one_jsonl(self, tmp_path: Path) -> None:
        parent = pa.table(
            {
                "id": pa.array(_IDS, type=pa.string()),
                "age": pa.array(_AGE, type=pa.string()),  # bad cell "badX" at index 5
            }
        )
        child = pa.table(
            {
                "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
                "parent_id": pa.array(_IDS, type=pa.string()),
                # A second, non-FK column with its own bad cell.
                "note": pa.array(["1", "2", "3", "4", "5", "badY"], type=pa.string()),
            }
        )
        parent_src = _write_source(tmp_path, parent, "parent")
        child_src = _write_source(tmp_path, child, "child")
        qpath = str(tmp_path / "quarantine.jsonl")

        config: dict[str, Any] = {
            "version": 1,
            "global_settings": {"job_name": "s2-multi-table-quarantine", "seed": 42},
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
                        _faker_col("id", "parent_ns"),
                        {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                    ],
                },
                {
                    "name": "child",
                    "columns": [
                        _faker_col("parent_id", "parent_ns"),
                        {
                            "name": "note",
                            "strategy": "bucketize",
                            "provider_config": {"width": 10},
                        },
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["parent_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "parent_ns",
                }
            ],
            "quarantine": {"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        }
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        result = run_pipeline(
            config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"

        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        source_tables = {r["_source_table"] for r in records}
        assert source_tables == {"parent", "child"}
        parent_recs = [r for r in records if r["_source_table"] == "parent"]
        child_recs = [r for r in records if r["_source_table"] == "child"]
        assert len(parent_recs) == 1
        assert parent_recs[0]["age"] == "badX"
        assert len(child_recs) == 1
        assert child_recs[0]["note"] == "badY"


def _self_fk_config(
    tmp_path: Path,
    *,
    ids: list[str],
    manager_ids: list[str | None],
    orphan_policy: str = "preserve",
    quarantine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """S2 remediation guide r3 section 8: one table `employees` whose FK
    child (`manager_id`) references its own parent key (`id`) -- a
    self-referential FK. `id` is masked by `date_shift` (a row-error-emitting
    strategy); `manager_id` is declared as an FK child of `employees.id`."""
    employees = pa.table(
        {
            "id": pa.array(ids, type=pa.string()),
            "manager_id": pa.array(manager_ids, type=pa.string()),
        }
    )
    src = _write_source(tmp_path, employees, "employees")
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "s2-self-fk-leak", "seed": 42},
        "sources": {"employees": {"type": "file", "path": src, "format": "parquet"}},
        "targets": {
            "employees": {
                "type": "file",
                "path": str(tmp_path / "employees.out.parquet"),
                "format": "parquet",
            }
        },
        "tables": [
            {
                "name": "employees",
                "columns": [
                    {
                        "name": "id",
                        "strategy": "date_shift",
                        "provider_config": {"min_days": 1, "max_days": 30},
                        "namespace": "employee_ns",
                    },
                    _faker_col("manager_id", "employee_ns"),
                ],
            }
        ],
        "relationships": [
            {
                "parent": {"table": "employees", "columns": ["id"]},
                "children": [{"table": "employees", "columns": ["manager_id"]}],
                "orphan_policy": orphan_policy,
                "namespace": "employee_ns",
            }
        ],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


class TestSelfRefFkKeyErrorLeakClosure:
    """S2 remediation guide r3 section 8.1: THE round-3 blocker proof. A
    table that is its own FK parent (`employees.id` <- `employees.manager_id`)
    must never leak its raw errored key through the self-FK, on either
    execution path. This FAILED on `10e8ade` for `execution_mode="sequential"`
    (manager_id resolved to the raw "notadate") before the section-3 per-node
    fold fix; it PASSES after."""

    _IDS = ["notadate", "2020-01-01"]
    _MANAGER_IDS: list[str | None] = [None, "notadate"]

    @pytest.mark.parametrize("execution_mode", ["full_frame", "sequential"])
    def test_self_ref_leak_closed_both_modes(self, tmp_path: Path, execution_mode: str) -> None:
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _self_fk_config(
            tmp_path,
            ids=self._IDS,
            manager_ids=self._MANAGER_IDS,
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = (
            ParquetTransactionalSink(tmp_path / "out") if execution_mode == "sequential" else None
        )

        result = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode=execution_mode, sink=sink
        )

        if execution_mode == "sequential":
            out = pq.read_table(tmp_path / "out" / "employees.parquet")
        else:
            out = result.outputs["employees"]
        ids = out.column("id").to_pylist()
        manager_ids = out.column("manager_id").to_pylist()

        # Raw errored key absent from BOTH the parent key column and the
        # self-referencing FK child column.
        assert "notadate" not in ids
        assert "notadate" not in manager_ids
        # The failing parent row and its only referrer are both removed
        # (Cam decision 1 / guide section 2): the table empties.
        assert out.num_rows == 0

        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 2
        direct_error = [r for r in records if r["id"] == "notadate"]
        cascaded = [r for r in records if "parent-key" in r["_quarantine_reason"]]
        assert len(direct_error) == 1
        assert direct_error[0]["_quarantine_trigger"] == "format_error"
        assert len(cascaded) == 1
        assert cascaded[0]["manager_id"] is None
        assert cascaded[0]["_quarantine_trigger"] == "format_error"

    def test_self_ref_full_frame_sequential_equivalence(self, tmp_path: Path) -> None:
        """Cam MEDIUM (guide section 8.2): full_frame and sequential must
        agree on the self-ref case, not just each be individually leak-free.
        Both paths run in-memory (no sink) so `ExecutionResult.outputs` is
        directly comparable."""
        quarantine = {
            "enabled": True,
            "output_path": str(tmp_path / "quarantine.jsonl"),
            "triggers": ["format_error"],
        }
        config_full = _self_fk_config(
            tmp_path, ids=self._IDS, manager_ids=self._MANAGER_IDS, quarantine=quarantine
        )
        sources_full = _sources(config_full)
        result_full = run_pipeline(
            config_full, sources_full, engine_version="0.1.0", execution_mode="full_frame"
        )

        tmp_path_seq = tmp_path / "seq"
        tmp_path_seq.mkdir()
        quarantine_seq = {
            "enabled": True,
            "output_path": str(tmp_path_seq / "quarantine.jsonl"),
            "triggers": ["format_error"],
        }
        config_seq = _self_fk_config(
            tmp_path_seq, ids=self._IDS, manager_ids=self._MANAGER_IDS, quarantine=quarantine_seq
        )
        sources_seq = _sources(config_seq)
        result_seq = run_pipeline(
            config_seq, sources_seq, engine_version="0.1.0", execution_mode="sequential"
        )

        assert result_full.outputs["employees"].equals(result_seq.outputs["employees"])
        assert result_full.outputs["employees"].num_rows == 0

    @pytest.mark.parametrize("execution_mode", ["full_frame", "sequential"])
    @pytest.mark.parametrize("orphan_policy", ["preserve", "warn", "fail", "remap"])
    def test_self_ref_orphan_policy_sweep_only_errored_referrer_cascades(
        self, tmp_path: Path, execution_mode: str, orphan_policy: str
    ) -> None:
        """S2 remediation guide section 4 + r3 section 8.3: a variant that
        does NOT empty the table. Row 1 references a CLEAN parent key (in
        fact its own, self-referencing) and must survive with its correctly
        masked value under every orphan_policy; only row 2 (which references
        the errored key) cascades. Row 0 (its own key errored) is always
        removed."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _self_fk_config(
            tmp_path,
            ids=["notadate", "2020-01-01", "2020-03-01"],
            manager_ids=[None, "2020-01-01", "notadate"],
            orphan_policy=orphan_policy,
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = (
            ParquetTransactionalSink(tmp_path / "out") if execution_mode == "sequential" else None
        )

        result = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode=execution_mode, sink=sink
        )

        if execution_mode == "sequential":
            out = pq.read_table(tmp_path / "out" / "employees.parquet")
        else:
            out = result.outputs["employees"]
        ids = out.column("id").to_pylist()
        manager_ids = out.column("manager_id").to_pylist()

        # Raw errored key never leaks, under any orphan_policy.
        assert "notadate" not in ids
        assert "notadate" not in manager_ids
        # Only row 1 (the clean, self-referencing row) survives; row 0 (its
        # own key errored) and row 2 (references the errored key) cascade.
        assert out.num_rows == 1
        assert ids[0] == manager_ids[0]  # the surviving row's self-reference resolved correctly


def _dupwhen_config(tmp_path: Path) -> dict[str, Any]:
    """S2 remediation guide r3 section 5.1 / 8.4 shape: parent `id` has TWO
    rows sharing the raw value "notadate" -- row 0 errors under `date_shift`
    (uncoercible, quarantined out), row 1 is `when`-gate-SKIPPED (keep=0) and
    survives with its raw "notadate" untouched by the user's own choice. A
    child FK references "notadate" and resolves via the identity-map
    contract to the surviving when-gate-unmasked row's raw value."""
    parent = pa.table(
        {
            "id": pa.array(["notadate", "notadate", "2020-01-01"], type=pa.string()),
            "keep": pa.array([1, 0, 1], type=pa.int64()),
        }
    )
    child = pa.table({"parent_id": pa.array(["notadate"], type=pa.string())})
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "s2-dupwhen-accepted-limitation", "seed": 42},
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
                    {
                        "name": "id",
                        "strategy": "date_shift",
                        "provider_config": {"min_days": 1, "max_days": 30},
                        "namespace": "dup_ns",
                        "when": "keep == 1",
                    },
                    {"name": "keep", "strategy": "passthrough"},
                ],
            },
            {"name": "child", "columns": [_faker_col("parent_id", "dup_ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "dup_ns",
            }
        ],
        "quarantine": {
            "enabled": True,
            "output_path": str(tmp_path / "quarantine.jsonl"),
            "triggers": ["format_error"],
        },
    }


class TestWhenGatedDuplicateKeyAcceptedLimitation:
    """S2 remediation guide r3 section 5 / 8.4 (Cam decision 2c): PINS the
    accepted when-gate limitation as intentional, not a bug. When a `when`
    gate leaves a parent FK-key row unmasked AND that same raw key value
    ALSO appears on a different parent row that row-errored, a child
    referencing that key resolves to the raw when-gate-unmasked value via
    the identity-map contract (FK-resolution precedence 1). This is NOT a
    quarantine escape: the raw value is present in the child ONLY because
    it is ALSO present in the PARENT output (the user's own `when` gate left
    it unmasked), so net-new exposure is NIL. If a future change makes the
    child value absent, THIS test fails and forces a conscious decision --
    do not "fix" it without a product/security call (see the guide)."""

    @pytest.mark.parametrize("execution_mode", ["full_frame", "sequential"])
    def test_child_inherits_raw_value_already_present_in_parent(
        self, tmp_path: Path, execution_mode: str
    ) -> None:
        config = _dupwhen_config(tmp_path)
        sources = _sources(config)
        sink = (
            ParquetTransactionalSink(tmp_path / "out") if execution_mode == "sequential" else None
        )

        result = run_pipeline(
            config, sources, engine_version="0.1.0", execution_mode=execution_mode, sink=sink
        )

        if execution_mode == "sequential":
            parent_out = pq.read_table(tmp_path / "out" / "parent.parquet").column("id").to_pylist()
            child_out = (
                pq.read_table(tmp_path / "out" / "child.parquet").column("parent_id").to_pylist()
            )
        else:
            parent_out = result.outputs["parent"].column("id").to_pylist()
            child_out = result.outputs["child"].column("parent_id").to_pylist()

        # The accepted behavior (a PIN, not a bug): the child inherits the
        # raw when-gate-unmasked value.
        assert "notadate" in child_out
        # Proof of NIL net exposure: the same raw value is ALSO present in
        # the PARENT output (row 1, when-gate-skipped) -- the child value is
        # not new information, it mirrors what the user already chose to
        # leave unmasked in the parent.
        assert "notadate" in parent_out
