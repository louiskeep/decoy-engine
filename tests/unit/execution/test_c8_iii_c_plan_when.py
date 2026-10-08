"""C8-iii-c acceptance test 2: plan-level validation runs before any write or handler call.

Plan: docs/plans/2026-10-07-c8-iii-c-rawdict-when.md (rev 3), section 2c. The invalid Plan is
a VALID compiled Plan rebuilt with `dataclasses.replace`, so no guard is bypassed and no
deserialization is involved, except in the deserialization tests, which run on their own.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml

from decoy_engine.errors import ValidationError
from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan import plan_from_yaml, plan_to_yaml
from decoy_engine.plan._serialize import _plan_from_dict
from tests.unit.execution import _c8_iii_c_support as sup

CODE = "when_outside_closed_grammar"
BAD = "s.notnull()"


class _SpyHandler:
    """Delegates to the real handler and records every `run`."""

    def __init__(self, real: Any, calls: list[str], name: str) -> None:
        self._real = real
        self._calls = calls
        self._name = name

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self._calls.append(self._name)
        return self._real.run(*args, **kwargs)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._real, attr)


def _spied_adapter() -> tuple[PandasExecutionAdapter, list[str]]:
    adapter = PandasExecutionAdapter()
    calls: list[str] = []
    for name, handler in list(adapter._handlers.items()):
        adapter._handlers[name] = _SpyHandler(handler, calls, name)
    return adapter, calls


def _assert_typed(exc: ValidationError, table: str = "b", column: str = "s") -> None:
    assert exc.code == CODE
    assert BAD not in str(exc)
    assert table in str(exc) and column in str(exc)


# --- (i) deserialization -----------------------------------------------------


def _doc(plan: Any) -> dict[str, Any]:
    return yaml.safe_load(plan_to_yaml(plan))


def test_a_stored_two_table_plan_with_a_bad_second_table_fails_at_load(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    doc = _doc(job.plan)
    doc["seed_envelope"]["per_table"]["b"]["per_column"]["s"]["when"] = BAD
    with pytest.raises(ValidationError) as info:
        plan_from_yaml(yaml.safe_dump(doc, sort_keys=False))
    _assert_typed(info.value)


def test_the_dict_loader_rejects_the_same_plan(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    doc = _doc(job.plan)
    doc["seed_envelope"]["per_table"]["b"]["per_column"]["s"]["when"] = BAD
    with pytest.raises(ValidationError) as info:
        _plan_from_dict(doc)
    _assert_typed(info.value)


@pytest.mark.parametrize("value", [5, True, ["x > 1"], "   "], ids=["int", "bool", "list", "blank"])
def test_a_stored_plan_with_a_non_grammar_scalar_when_fails_at_load(
    tmp_path: Path, value: Any
) -> None:
    job = sup.two_table_job(tmp_path)
    doc = _doc(job.plan)
    doc["seed_envelope"]["per_table"]["a"]["per_column"]["s"]["when"] = value
    with pytest.raises(ValidationError) as info:
        plan_from_yaml(yaml.safe_dump(doc, sort_keys=False))
    assert info.value.code == CODE


def test_a_stored_plan_with_a_grammar_when_round_trips(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    doc = _doc(job.plan)
    doc["seed_envelope"]["per_table"]["a"]["per_column"]["s"]["when"] = "x > 2"
    plan = plan_from_yaml(yaml.safe_dump(doc, sort_keys=False))
    assert sup.column_seed(plan, "a", "s").when == "x > 2"


# --- (ii) entrypoints --------------------------------------------------------


def _sequential_kwargs(job: sup.Job) -> dict[str, Any]:
    return {
        "registry": job.registry,
        "relationship_graph": job.graph,
        "namespace_registry": job.namespaces,
    }


def test_run_sequential_rejects_before_any_sink_write_or_handler_call(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    bad = sup.plan_with_when(job.plan, "b", "s", BAD)
    adapter, calls = _spied_adapter()
    writes: list[str] = []
    with pytest.raises(ValidationError) as info:
        adapter.run_sequential(
            bad,
            lambda name: job.sources[name],
            sink=lambda name, table: writes.append(name),
            **_sequential_kwargs(job),
        )
    _assert_typed(info.value)
    assert writes == []
    assert calls == []


def test_run_rejects_before_any_handler_call(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    bad = sup.plan_with_when(job.plan, "b", "s", BAD)
    adapter, calls = _spied_adapter()
    with pytest.raises(ValidationError) as info:
        adapter.run(bad, dict(job.sources), **_sequential_kwargs(job))
    _assert_typed(info.value)
    assert calls == []


def test_run_single_for_table_a_rejects_a_bad_seed_on_table_b(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    bad = sup.plan_with_when(job.plan, "b", "s", BAD)
    adapter, calls = _spied_adapter()
    with pytest.raises(ValidationError) as info:
        adapter.run_single(bad, job.sources["a"], table="a", **_sequential_kwargs(job))
    _assert_typed(info.value)
    assert calls == []


def test_run_rejects_a_bad_seed_for_a_table_absent_from_the_sources(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    bad = sup.plan_with_extra_seed(job.plan, "ghost", "s", BAD)
    adapter, calls = _spied_adapter()
    with pytest.raises(ValidationError) as info:
        adapter.run(bad, {"a": job.sources["a"]}, **_sequential_kwargs(job))
    _assert_typed(info.value, table="ghost")
    assert calls == []


def test_run_sequential_rejects_a_bad_seed_for_a_table_it_never_loads(tmp_path: Path) -> None:
    job = sup.two_table_job(tmp_path)
    bad = sup.plan_with_extra_seed(job.plan, "ghost", "s", BAD)
    adapter, calls = _spied_adapter()
    writes: list[str] = []
    with pytest.raises(ValidationError) as info:
        adapter.run_sequential(
            bad,
            lambda name: job.sources[name],
            sink=lambda name, table: writes.append(name),
            **_sequential_kwargs(job),
        )
    _assert_typed(info.value, table="ghost")
    assert writes == [] and calls == []


def test_generate_tables_rejects_before_any_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.generation import _plan_entry
    from decoy_engine.generation.synthesize import generate_tables
    from decoy_engine.plan import compile_plan
    from tests.unit._dps_helpers import empty_profile

    config = {
        "global_settings": {"seed": 1},
        "tables": [
            {
                "name": "g",
                "row_count": 3,
                "generate_columns": [{"name": "n", "type": "sequence", "start": 1}],
            }
        ],
    }
    plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
    template = sup.ColumnSeed(
        namespace=None,
        strategy="redact",
        provider=None,
        backend_type="builtin",  # type: ignore[arg-type]
        backend_version="1",
        cardinality_mode="reuse",  # type: ignore[arg-type]
        when=BAD,
    )
    table_seed = sup.TableSeed(per_column=(("s", template),))
    env = dataclasses.replace(plan.seed_envelope, per_table=(("ghost", table_seed),))
    bad = dataclasses.replace(plan, seed_envelope=env)

    calls: list[Any] = []
    monkeypatch.setattr(
        _plan_entry, "_generate_tables_from_config", lambda *a, **k: calls.append(a) or {}
    )
    with pytest.raises(ValidationError) as info:
        generate_tables(bad)
    _assert_typed(info.value, table="ghost")
    assert calls == []
    assert generate_tables(plan) == {}  # the valid plan still reaches the (stubbed) provider
    assert len(calls) == 1


# --- (iii) every node kind and an absent table -------------------------------


def _fk_plan(tmp_path: Path) -> Any:
    """A parent/child FK job: the child key column is an FK-resolved node."""
    import pyarrow.parquet as pq

    ids = [f"p{i}" for i in range(6)]
    parent = pa.table({"id": pa.array(ids)})
    child = pa.table({"id": pa.array([f"c{i}" for i in range(6)]), "parent_id": pa.array(ids)})
    sources = {"parent": parent, "child": child}
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {},
        "targets": {},
        "tables": [
            {"name": "parent", "columns": [_fk_col("id")]},
            {"name": "child", "columns": [_fk_col("parent_id")]},
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
    for name, table in sources.items():
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path)
        cfg["sources"][name] = {"type": "file", "path": str(path), "format": "parquet"}
        cfg["targets"][name] = {
            "type": "file",
            "path": str(tmp_path / f"{name}.out.parquet"),
            "format": "parquet",
        }
    from decoy_engine.config import PipelineConfig

    cfg = PipelineConfig.model_validate(cfg).model_dump()
    return sup.compile_job(cfg)[0]


def _fk_col(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "parent_ns",
    }


def _composite_plan(tmp_path: Path) -> Any:
    from tests.unit.execution import _multi_table_support as mt

    def col(name: str, others: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "strategy": "faker",
            "provider": "composite_name_email",
            "deterministic": True,
            "namespace": "ne",
            "coherent_with": others,
            "cardinality_mode": "reuse",
            "provider_config": {"pool_size": 20},
        }

    table = pa.table(
        {
            "first_name": pa.array([f"F{i}" for i in range(6)]),
            "last_name": pa.array([f"L{i}" for i in range(6)]),
            "email": pa.array([f"e{i}@x.com" for i in range(6)]),
        }
    )
    cols = [
        col("first_name", ["last_name", "email"]),
        col("last_name", ["first_name", "email"]),
        col("email", ["first_name", "last_name"]),
    ]
    cfg, _ = mt.build_job(tmp_path, {"t": (cols, table)})
    return sup.compile_job(cfg)[0]


def test_validate_plan_when_reaches_an_fk_resolved_node(tmp_path: Path) -> None:
    from decoy_engine.expressions._when_parser import validate_plan_when

    plan = _fk_plan(tmp_path)
    validate_plan_when(plan)  # the valid plan passes
    bad = sup.plan_with_when(plan, "child", "parent_id", BAD)
    with pytest.raises(ValidationError) as info:
        validate_plan_when(bad)
    _assert_typed(info.value, table="child", column="parent_id")


def test_validate_plan_when_reaches_a_composite_node(tmp_path: Path) -> None:
    from decoy_engine.expressions._when_parser import validate_plan_when

    plan = _composite_plan(tmp_path)
    validate_plan_when(plan)
    bad = sup.plan_with_when(plan, "t", "email", BAD)
    with pytest.raises(ValidationError) as info:
        validate_plan_when(bad)
    _assert_typed(info.value, table="t", column="email")


def test_validate_plan_when_reaches_a_table_absent_from_the_sources(tmp_path: Path) -> None:
    from decoy_engine.expressions._when_parser import validate_plan_when

    job = sup.two_table_job(tmp_path)
    bad = sup.plan_with_extra_seed(job.plan, "ghost", "s", BAD)
    with pytest.raises(ValidationError) as info:
        validate_plan_when(bad)
    _assert_typed(info.value, table="ghost")


@pytest.mark.parametrize("value", [None, "x > 1", "s == 'q' or x in [1, 2]"], ids=["none", "g1", "g2"])
def test_validate_plan_when_accepts_the_grammar_and_none(tmp_path: Path, value: Any) -> None:
    from decoy_engine.expressions._when_parser import validate_plan_when

    job = sup.two_table_job(tmp_path)
    validate_plan_when(sup.plan_with_when(job.plan, "a", "s", value))


@pytest.mark.parametrize("value", [5, True, ["x > 1"], "   ", ""], ids=["int", "bool", "list", "ws", "empty"])
def test_validate_plan_when_rejects_a_non_grammar_scalar(tmp_path: Path, value: Any) -> None:
    from decoy_engine.expressions._when_parser import validate_plan_when

    job = sup.two_table_job(tmp_path)
    with pytest.raises(ValidationError) as info:
        validate_plan_when(sup.plan_with_when(job.plan, "a", "s", value))
    assert info.value.code == CODE
