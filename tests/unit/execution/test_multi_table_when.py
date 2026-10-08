"""B7 acceptance test 3: a table dispatches exactly when it would auto-chunk alone.

Plan: docs/plans/2026-10-01-multi-table-dispatch.md (revision 3), guarantee 2 and Design 3.
Each case is one table of a two-table job: a standard anchor table that always dispatches,
and the table under test. The table under test dispatches exactly when the same table, as a
single-table `run_pipeline` call with the same knobs, routes chunked, and the recorded reason
equals that single-table call's `auto_chunk.reason`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _chunked, run_pipeline
from tests.unit.execution import _auto_chunk_strategies as strategies
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_support as mt

pytestmark = pytest.mark.filterwarnings("ignore")

N = mt.BIG


class Case:
    """One table under test: its columns, source, and how the job hands the source over."""

    def __init__(
        self,
        columns: list[dict[str, Any]],
        table: pa.Table,
        *,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        kind: str = "resident",
        expect_dispatched: bool | None = None,
    ) -> None:
        self.columns = columns
        self.table = table
        self.mutate = mutate
        self.kind = kind
        self.expect_dispatched = expect_dispatched


def _fixture_case(key: str) -> Case:
    columns, data = strategies.STRATEGY_FIXTURES[key]
    return Case(columns, pa.table(data))


def _sized(n: int) -> Case:
    return Case(mt.std_columns("t_ns"), mt.string_table(n, "t"))


def _when_case() -> Case:
    # A bool with nulls changes pandas dtype per chunk, so the table is still declined.
    flags = pa.array([None if i % 4 == 0 else i % 3 == 0 for i in range(N)], pa.bool_())
    table = pa.table({"val": pa.array([f"v{i}" for i in range(N)]), "amount": flags})
    return Case(
        [support.redact_col("val"), support.pass_col("amount")],
        table,
        mutate=lambda cfg: _inject_when(cfg, "val"),
        expect_dispatched=False,
    )


def _inject_when(cfg: dict[str, Any], column: str) -> None:
    for table in cfg["tables"]:
        if table["name"] == "tbl":
            for col in table["columns"]:
                if col["name"] == column:
                    col["when"] = "amount == True"


def _composite_case() -> Case:
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
            "first_name": pa.array([f"F{i}" for i in range(N)]),
            "last_name": pa.array([f"L{i}" for i in range(N)]),
            "email": pa.array([f"e{i}@x.com" for i in range(N)]),
        }
    )
    return Case(
        [
            col("first_name", ["last_name", "email"]),
            col("last_name", ["first_name", "email"]),
            col("email", ["first_name", "last_name"]),
        ],
        table,
        expect_dispatched=False,
    )


def _fpe_join_group_case() -> Case:
    cols = [
        {
            "name": name,
            "strategy": "fpe",
            "namespace": "phone_ns",
            "provider_config": {"charset": "digits", "fpe_join_group": "phone_e164"},
        }
        for name in ("phone", "mobile")
    ]
    table = pa.table(
        {
            "phone": pa.array([f"{i:09d}" for i in range(N)]),
            "mobile": pa.array([f"{i + 7:09d}" for i in range(N)]),
        }
    )
    return Case(cols, table, expect_dispatched=False)


def _int_nulls_case() -> Case:
    vals: list[Any] = list(range(N))
    vals[7] = None
    table = pa.table(
        {"val": pa.array([f"v{i}" for i in range(N)]), "amount": pa.array(vals, pa.int64())}
    )
    return Case(
        [support.hash_col("val", "t_ns"), support.pass_col("amount")],
        table,
        expect_dispatched=False,
    )


def _bucketize_nulls_case() -> Case:
    vals: list[Any] = [i * 13 % 997 for i in range(N)]
    vals[N - 1] = None
    cols = [{"name": "val", "strategy": "bucketize", "provider_config": {"width": 50}}]
    return Case(cols, pa.table({"val": pa.array(vals, pa.int64())}), expect_dispatched=False)


def _lazy_case() -> Case:
    return Case(
        mt.std_columns("t_ns"), mt.string_table(N, "t"), kind="lazy", expect_dispatched=True
    )


def _loader_case() -> Case:
    return Case(
        mt.std_columns("t_ns"), mt.string_table(N, "t"), kind="loader", expect_dispatched=False
    )


def _pandas_metadata_case() -> Case:
    df = pd.DataFrame(
        {
            "h": pd.array([f"u{i}@x.example" for i in range(N)], dtype="string"),
            "r": pd.array([f"s{i}" for i in range(N)], dtype="string"),
        }
    )
    return Case(
        mt.std_columns("t_ns"),
        pa.Table.from_pandas(df, preserve_index=False),
        expect_dispatched=True,
    )


def _date64_case() -> Case:
    day = 86_400_000
    table = pa.table(
        {
            **mt.string_table(N, "t").to_pydict(),
            "d": pa.array([i * day for i in range(N)], pa.date64()),
        }
    )
    return Case([*mt.std_columns("t_ns"), support.pass_col("d")], table, expect_dispatched=True)


def _time64_case() -> Case:
    table = pa.table(
        {
            **mt.string_table(N, "t").to_pydict(),
            "tm": pa.array([i * 1000 for i in range(N)], pa.time64("ns")),
        }
    )
    return Case([*mt.std_columns("t_ns"), support.pass_col("tm")], table, expect_dispatched=True)


def _deterministic_shuffle_case() -> Case:
    cols = [{"name": "val", "strategy": "shuffle", "deterministic": True, "namespace": "sh_ns"}]
    return Case(
        cols, pa.table({"val": pa.array([f"x{i}" for i in range(N)])}), expect_dispatched=False
    )


def _formula_case() -> Case:
    cols = [{"name": "val", "strategy": "formula", "provider_config": {"formula": "value + 1"}}]
    return Case(cols, pa.table({"val": pa.array(list(range(N)))}), expect_dispatched=False)


CASES: dict[str, Callable[[], Case]] = {
    **{f"fixture:{key}": (lambda k=key: _fixture_case(k)) for key in strategies.STRATEGY_FIXTURES},
    "deterministic_shuffle": _deterministic_shuffle_case,
    "formula": _formula_case,
    "when_column": _when_case,
    "composite_bundle": _composite_case,
    "fpe_join_group": _fpe_join_group_case,
    "int_with_nulls": _int_nulls_case,
    "bucketize_null_bearing": _bucketize_nulls_case,
    "below_threshold": lambda: _sized(mt.THRESHOLD - 1),
    "at_threshold": lambda: _sized(mt.THRESHOLD),
    "lazy_source": _lazy_case,
    "source_loader_only": _loader_case,
    "pandas_metadata_string_dtype": _pandas_metadata_case,
    "date64": _date64_case,
    "time64_ns": _time64_case,
}


def test_the_fixture_cases_cover_the_live_admission_set() -> None:
    live = _chunked._CHUNK_ADMITTED_STRATEGIES | _chunked.CHUNK_CONDITIONAL_STRATEGIES
    assert {key.split(":")[0] for key in strategies.STRATEGY_FIXTURES} == set(live)


def _hand_over(
    case: Case, cfg: dict[str, Any], sources: dict[str, Any], tmp_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return `(sources, extra run kwargs)` carrying the table under test as the case says."""
    from decoy_engine.profile._readers import LazySource

    extra: dict[str, Any] = {}
    handed = dict(sources)
    if case.kind == "lazy":
        handed["tbl"] = LazySource(Path(cfg["sources"]["tbl"]["path"]))
    elif case.kind == "loader":
        del handed["tbl"]
        extra["source_loader"] = lambda name: sources[name]
    return handed, extra


def _single_alone(case: Case, cfg: dict[str, Any], sources: dict[str, Any], tmp_path: Path) -> Any:
    handed, extra = _hand_over(case, cfg, sources, tmp_path)
    only = mt.restrict(cfg, "tbl")
    only_sources = {"tbl": handed["tbl"]} if "tbl" in handed else {}
    return run_pipeline(only, sources=only_sources, **mt.kw(explain_plan=True, **extra))


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_table_dispatches_exactly_when_it_would_auto_chunk_alone(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = CASES[name]()
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "anchor": (mt.std_columns("anchor_ns"), mt.string_table(N, "a")),
            "tbl": (case.columns, case.table),
        },
    )
    if case.mutate is not None:
        case.mutate(cfg)
    alone = _single_alone(case, cfg, sources, tmp_path)
    alone_block = alone.quality_metrics.get("auto_chunk", {})
    alone_chunked = alone_block.get("mode") == "chunked"
    if case.expect_dispatched is not None:
        assert alone_chunked == case.expect_dispatched, "the single-table rule moved"

    handed, extra = _hand_over(case, cfg, sources, tmp_path)
    calls = mt.spy_split(monkeypatch)
    got = run_pipeline(cfg, sources=handed, **mt.kw(**extra))
    block = got.quality_metrics.get("auto_chunk", {})
    entries = {t["table"]: t for t in block.get("tables", [])}
    assert block.get("mode") == "chunked" and "anchor" in mt.dispatched_tables(got)
    assert len(calls) == 1
    assert entries["tbl"]["dispatched"] == alone_chunked, name
    assert entries["tbl"]["reason"] == alone_block["reason"], name
    if alone_chunked:
        assert entries["tbl"]["lane"] == "dispatcher"
        assert entries["tbl"]["lane_reason"] is None
    # The recorded reason of a table left out is the single-table `rejections["chunked"]`.
    if not alone_chunked:
        rejections = alone.quality_metrics["execution_plan"]["rejections"]
        assert entries["tbl"]["reason"] == rejections["chunked"]


def test_split_off_has_no_dispatched_table_today(tmp_path: Path) -> None:
    """Red-before anchor: without the split a multi-table job dispatches nothing."""
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "anchor": (mt.std_columns("anchor_ns"), mt.string_table(N, "a")),
            "tbl": (mt.std_columns("t_ns"), mt.string_table(N, "t")),
        },
    )
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert off.quality_metrics.get("auto_chunk", {}).get("mode") != "chunked"
    assert "chunked_route_by_table" not in off.quality_metrics
    on = run_pipeline(cfg, sources=sources, **mt.kw())
    assert mt.dispatched_tables(on) == ["anchor", "tbl"]


def test_the_pandas_metadata_and_date_cases_dispatch_without_the_byte_estimate_workaround(
    tmp_path: Path,
) -> None:
    """The byte-estimate defect B2 recorded is fixed on main; `date64` and `time64[ns]` run
    with the default `use_byte_estimate_routing=True` and dispatch on the dispatcher lane."""
    for builder in (_date64_case, _time64_case, _pandas_metadata_case):
        case = builder()
        workdir = tmp_path / builder.__name__
        workdir.mkdir()
        cfg, sources = mt.build_job(
            workdir,
            {
                "anchor": (mt.std_columns("anchor_ns"), mt.string_table(N, "a")),
                "tbl": (case.columns, case.table),
            },
        )
        got = run_pipeline(cfg, sources=sources, **mt.kw())
        entries = {t["table"]: t for t in got.quality_metrics["auto_chunk"]["tables"]}
        assert entries["tbl"]["dispatched"] is True, builder.__name__
        assert entries["tbl"]["lane"] == "dispatcher"
        assert entries["tbl"]["lane_reason"] is None
