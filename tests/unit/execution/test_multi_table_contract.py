"""B7 acceptance tests 4, 4b and 5: the output contract and the side channels of a split job.

Plan: docs/plans/2026-10-01-multi-table-dispatch.md (revision 3), guarantee 3 and section 5.
A dispatched table carries the output the same table gets as a single-table auto-chunk job
(B2's Arrow-exact shape). A full-frame-group table keeps today's shape. One result may hold
both. The tests pin each table against the right reference, never against a weakened one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pytest

from decoy_engine.execution import run_pipeline
from tests.unit.execution import _auto_chunk_matrix as matrix
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_support as mt

pytestmark = pytest.mark.filterwarnings("ignore")

N = mt.BIG


class Job:
    """A multi-table job: its tables, which of them must dispatch, and the output type rules."""

    def __init__(
        self,
        tables: dict[str, mt.TableSpec],
        dispatched: set[str],
        *,
        string_output: dict[str, set[str]] | None = None,
        masked: dict[str, set[str]] | None = None,
        faker_all_null: dict[str, set[str]] | None = None,
        extra_sources: dict[str, pa.Table] | None = None,
        extra_kw: dict[str, Any] | None = None,
    ) -> None:
        self.tables = tables
        self.dispatched = dispatched
        self.string_output = string_output or {}
        self.masked = masked or {}
        self.faker_all_null = faker_all_null or {}
        self.extra_sources = extra_sources or {}
        self.extra_kw = extra_kw or {}


def _std_pair(
    big_first: bool = True,
) -> dict[str, mt.TableSpec]:
    big = (mt.std_columns("big_ns"), mt.string_table(N, "b"))
    small = (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s"))
    return {"big": big, "small": small} if big_first else {"small": small, "big": big}


def _with_x(
    x: pa.Array,
    xcfg: dict[str, Any] | None,
    field: pa.Field | None = None,
    *,
    base_table: pa.Table | None = None,
) -> mt.TableSpec:
    cols = {**support.string_source(len(x)), "x": x}
    fields = {"x": field} if field is not None else {}
    table = support.table_of(cols, fields)
    return (
        [support.hash_col("h", "x_h_ns"), support.redact_col("r")] + ([xcfg] if xcfg else []),
        table,
    )


def _x_job(
    x: pa.Array,
    xcfg: dict[str, Any] | None,
    *,
    field: pa.Field | None = None,
    string_x: bool = False,
    faker_x: bool = False,
) -> Job:
    spec = _with_x(x, xcfg, field)
    small = (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s"))
    string_output = {"big": {"h", "r"} | ({"x"} if string_x else set())}
    return Job(
        {"big": spec, "small": small},
        {"big"},
        string_output=string_output,
        faker_all_null={"big": {"x"}} if faker_x else {},
    )


def _chunk0_null_hash() -> Job:
    mask = np.zeros(N, dtype=bool)
    mask[: mt.CHUNK] = True
    x = pc.if_else(
        pa.array(mask), pa.scalar(None, pa.string()), pa.array([f"k{i}" for i in range(N)])
    )
    return _x_job(x, support.hash_col("x", "x_ns"), string_x=True)


def _all_null_hash() -> Job:
    return _x_job(pa.nulls(N, pa.string()), support.hash_col("x", "x_ns"), string_x=True)


def _bool_nulls_later() -> Job:
    values: list[Any] = [i % 3 == 0 for i in range(N)]
    for i in (mt.CHUNK + 2, N - 1):
        values[i] = None
    return _x_job(pa.array(values, pa.bool_()), support.pass_col("x"))


def _non_nullable_field() -> Job:
    return _x_job(
        pa.array([f"k{i}" for i in range(N)]),
        support.pass_col("x"),
        field=pa.field("x", pa.string(), nullable=False),
    )


def _field_metadata() -> Job:
    return _x_job(
        pa.array([f"k{i}" for i in range(N)]),
        support.pass_col("x"),
        field=pa.field("x", pa.string(), metadata={b"owner": b"b7"}),
    )


def _faker_all_null() -> Job:
    return _x_job(pa.nulls(N, pa.string()), support.faker_col("x"), faker_x=True)


def _unconfigured() -> Job:
    return _x_job(pa.array([f"k{i}" for i in range(N)]), None)


def _extra_source() -> Job:
    job = Job(_std_pair(), {"big"})
    job.extra_sources = {"lookup": pa.table({"k": pa.array(["a", "b"])})}
    return job


def _reordered_sources() -> Job:
    return Job(_std_pair(big_first=False), {"big"})


def _three_tables() -> Job:
    return Job(
        {
            "a": (mt.std_columns("a_ns"), mt.string_table(N, "a")),
            "b": (mt.std_columns("b_ns"), mt.string_table(mt.SMALL, "b")),
            "c": (mt.std_columns("c_ns"), mt.string_table(N + 7, "c")),
            "d": (mt.std_columns("d_ns"), mt.string_table(mt.SMALL, "d")),
        },
        {"a", "c"},
    )


def _uneven_last_chunk() -> Job:
    return Job(
        {
            "big": (mt.std_columns("big_ns"), mt.string_table(mt.CHUNK * 2 + 1, "b")),
            "small": (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s")),
        },
        {"big"},
    )


_PASS_TYPES = [
    t
    for t in matrix.BUILDERS
    # An all-null source column is its own case below, and a non-aligned time64[ns] column
    # cannot be converted by the full-frame run either, so it has no split-off reference.
    if t not in {"null", "time64_ns_nonaligned"}
]


def _pass_type_job(typ: str) -> Job:
    spec = _with_x(matrix.BUILDERS[typ](), support.pass_col("x"))
    small = (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s"))
    return Job({"big": spec, "small": small}, {"big"}, string_output={"big": {"h", "r"}})


CASES: dict[str, Callable[[], Job]] = {
    "standard_pair": lambda: Job(_std_pair(), {"big"}),
    "uneven_last_chunk": _uneven_last_chunk,
    "four_tables_two_dispatched": _three_tables,
    "chunk0_null_string_output": _chunk0_null_hash,
    "all_null_string_output": _all_null_hash,
    "bool_passthrough_nulls_after_chunk0": _bool_nulls_later,
    "non_nullable_source_field": _non_nullable_field,
    "source_field_metadata": _field_metadata,
    "native_faker_all_null": _faker_all_null,
    "unconfigured_column_warn_policy": _unconfigured,
    "extra_caller_source": _extra_source,
    "sources_ordered_unlike_tables": _reordered_sources,
    **{f"passthrough_{t}": (lambda t=t: _pass_type_job(t)) for t in _PASS_TYPES},
    "passthrough_null_typed": lambda: _pass_type_job_null(),
}


def _pass_type_job_null() -> Job:
    spec = _with_x(pa.nulls(N), support.pass_col("x"))
    small = (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s"))
    return Job({"big": spec, "small": small}, {"big"}, string_output={"big": {"h", "r"}})


def _run_job(job: Job, tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    cfg, sources = mt.build_job(tmp_path, job.tables)
    sources = {**sources, **job.extra_sources}
    return cfg, sources


def _companion_state(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if state == "absent":
        support.remove_companion(monkeypatch)
    elif not support.COMPANION_PRESENT:
        pytest.skip("compiled companion not installed")


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("companion", ["present", "absent"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_split_job_output_contract(
    case: str,
    companion: str,
    threads: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _companion_state(companion, monkeypatch)
    job = CASES[case]()
    cfg, sources = _run_job(job, tmp_path)
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw(), native_threads=threads))
    got = run_pipeline(cfg, sources=sources, **mt.kw(native_threads=threads))

    # (e) the dispatch decision and the B1 route are pinned, so a case cannot pass by
    # quietly not dispatching.
    assert set(mt.dispatched_tables(got)) == job.dispatched, case
    for name in job.dispatched:
        route = mt.route_of(got, name)
        assert route is not None and route["native_admitted"] in (True, False)

    # (a)
    assert list(got.outputs) == list(off.outputs)
    refs = {
        name: mt.single_reference(cfg, sources, name, native_threads=threads).outputs[name]
        for name in job.dispatched
    }
    for name, table in got.outputs.items():
        if name in job.dispatched:
            # (c) against the single-table reference, exactly.
            assert table.equals(refs[name], check_metadata=True), (case, name)
            # (d) against the split-off run, column by column.
            # B2 guarantee 3 (b): an all-null native Faker column is `string` only when B1's
            # native route ran; on the oracle route its type equals the split-off type.
            faker = job.faker_all_null.get(name, set())
            faker_native = faker if companion == "present" else set()
            faker_on_oracle = set() if companion == "present" else faker
            standard = {"h", "r"} & set(sources[name].column_names)
            support.check_contract(
                table,
                off.outputs[name],
                None,
                sources[name],
                string_output=job.string_output.get(name, standard),
                masked=job.masked.get(name, set()) | faker_on_oracle,
                faker_native_all_null=faker_native,
            )
        else:
            # (b) today's shape, pandas metadata included.
            assert table.equals(off.outputs[name], check_metadata=True), (case, name)


def test_every_dispatched_table_of_the_standard_job_is_native_when_the_companion_is_present(
    tmp_path: Path,
) -> None:
    if not support.COMPANION_PRESENT:
        pytest.skip("compiled companion not installed")
    job = CASES["standard_pair"]()
    cfg, sources = _run_job(job, tmp_path)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    route = mt.route_of(got, "big")
    assert route is not None and route["native_admitted"] is True
    assert all(c["executed_backend"] != "pandas_oracle" for c in route["columns"])


def test_companion_absent_reroutes_the_hash_table_to_the_oracle_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.remove_companion(monkeypatch)
    cfg, sources = _run_job(CASES["standard_pair"](), tmp_path)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    route = mt.route_of(got, "big")
    assert route is not None
    assert route["native_admitted"] is False
    assert route["reroute_reason"].startswith("crypto_extension_unavailable")


def test_an_unconfigured_column_reroutes_the_table_with_uncovered_columns(
    tmp_path: Path,
) -> None:
    job = CASES["unconfigured_column_warn_policy"]()
    cfg, sources = _run_job(job, tmp_path)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    route = mt.route_of(got, "big")
    assert route is not None
    assert route["native_admitted"] is False
    assert "uncovered_columns" in route["reroute_reason"]


# ---------------------------------------------------------------------------
# Test 4b: both shapes in one result.
# ---------------------------------------------------------------------------


def _pandas_shaped(n: int, tag: str) -> pa.Table:
    import pandas as pd

    df = pd.DataFrame(
        {
            "h": [f"{tag}{i}@x.example" for i in range(n)],
            "keep": [f"keep-{tag}{i}" for i in range(n)],
        }
    )
    table = pa.Table.from_pandas(df, preserve_index=False)
    assert table.schema.metadata and b"pandas" in table.schema.metadata
    return table.set_column(
        table.schema.get_field_index("keep"),
        "keep",
        table.column("keep").cast(pa.large_string()),
    )


def test_a_mixed_job_returns_each_table_in_its_own_shape(tmp_path: Path) -> None:
    cols = lambda ns: [support.hash_col("h", ns), support.pass_col("keep")]
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (cols("big_ns"), _pandas_shaped(N, "b")),
            "small": (cols("small_ns"), _pandas_shaped(mt.SMALL, "s")),
        },
    )
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert mt.dispatched_tables(got) == ["big"]
    big, small = got.outputs["big"], got.outputs["small"]
    assert big.schema.metadata is None
    assert big.schema.field("keep").type == pa.large_string()
    assert small.schema.metadata is not None and b"pandas" in small.schema.metadata
    assert small.equals(off.outputs["small"], check_metadata=True)
    assert small.schema.field("keep").type == pa.string()
    assert big.column("h").to_pylist() == off.outputs["big"].column("h").to_pylist()
    assert small.column("h").to_pylist() == off.outputs["small"].column("h").to_pylist()
    # With the split off, both tables carry today's shape.
    for name in ("big", "small"):
        meta = off.outputs[name].schema.metadata
        assert meta is not None and b"pandas" in meta
        assert off.outputs[name].schema.field("keep").type == pa.string()


# ---------------------------------------------------------------------------
# Test 5: side channels.
# ---------------------------------------------------------------------------


def _key(item: Any) -> str:
    return json.dumps(
        {
            "code": getattr(item, "code", None),
            "provider": getattr(item, "provider", None),
            "column": getattr(item, "column", None),
            "detail": getattr(item, "detail", None),
        },
        sort_keys=True,
        default=str,
    )


def _fpe_join_table(tag: str, n: int) -> mt.TableSpec:
    cols = [
        {
            "name": name,
            "strategy": "fpe",
            "namespace": f"{tag}_ns",
            "provider_config": {"charset": "digits", "fpe_join_group": f"grp_{tag}"},
        }
        for name in ("phone", "mobile")
    ]
    table = pa.table(
        {
            "phone": pa.array([f"{i:09d}" for i in range(n)]),
            "mobile": pa.array([f"{i + 7:09d}" for i in range(n)]),
        }
    )
    return cols, table


def test_warnings_timings_and_corpora_are_permutations_of_the_split_off_run(
    tmp_path: Path,
) -> None:
    from tests.unit.execution import _auto_chunk_strategies as strategies

    code_cols, data = strategies.STRATEGY_FIXTURES["code_set:mask"]
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "codes": (code_cols, pa.table(data)),
            "anchor": (mt.std_columns("anchor_ns"), mt.string_table(N, "a")),
            "fpe_a": _fpe_join_table("a", mt.SMALL),
            "fpe_b": _fpe_join_table("b", mt.SMALL),
        },
    )
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert set(mt.dispatched_tables(got)) == {"codes", "anchor"}
    # One warning emitted by two tables is kept twice, as the adapter keeps it today.
    assert sum(w.code == "fpe_join_group_active" for w in off.warnings) >= 2
    assert sorted(map(_key, got.warnings)) == sorted(map(_key, off.warnings))
    assert mt.timing_keys(got) == mt.timing_keys(off)
    assert got.quality_metrics["code_set_corpora"] == off.quality_metrics["code_set_corpora"]
    assert got.quality_metrics["code_set_corpora"]


def test_fidelity_and_post_validation_equal_the_split_off_run_and_each_references_report(
    tmp_path: Path,
) -> None:
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (mt.std_columns("big_ns"), mt.string_table(N, "b")),
            "small": (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s")),
        },
    )
    knobs = {"fidelity_report": True, "post_validation": True, "now_iso": "2026-10-02T00:00:00Z"}
    got = run_pipeline(cfg, sources=sources, **mt.kw(**knobs))
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw(), **knobs))
    assert mt.dispatched_tables(got) == ["big"]
    # The dispatched output has the split-off schema apart from schema metadata.
    assert got.outputs["big"].schema.equals(off.outputs["big"].schema, check_metadata=False)
    for key in ("fidelity_reports", "quality_summary", "failed_checks"):
        assert mt.strip_elapsed(got.quality_metrics.get(key)) == mt.strip_elapsed(
            off.quality_metrics.get(key)
        ), key
    ref = mt.single_reference(cfg, sources, "big", **knobs)
    assert (
        got.quality_metrics["fidelity_reports"]["big"]
        == ref.quality_metrics["fidelity_reports"]["big"]
    )
