"""C6c-i acceptance: text_redact as an ARROW_PYTHON operator on the chunked native route.

Native chunked output must equal the pandas-oracle chunked output (values, Arrow type,
metadata). Every admitted case also proves the table really took the native route with the
`arrow_python` backend, so a silent fallback to the oracle cannot pass the parity check.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from decoy_engine.execution._planner import _whole_column_state_rejections
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._chunked_evidence import plan_column_backends
from decoy_engine.execution.native._chunked_schema_rule import build_schema_rule
from decoy_engine.execution.native._real_type_admission import string_source_type_rejection
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import Run, assert_same_as_oracle, run_one, run_pair
from tests.native._c6c_i_support import (
    ADMITTED_CONFIGS,
    CORPUS,
    EXCLUDED_CONFIGS,
    PASS_THROUGH_CONFIGS,
    SHAPES,
    tr_col,
)
from tests.native._chunked_date_shift_support import run_outcome
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    faker_col,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    split,
    truncate,
)


def source(values: list[str | None], *, typ: pa.DataType | None = None) -> pa.Table:
    return pa.table(
        {
            "s": pa.array(values, typ or pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


def _columns(run: Run) -> dict[str, dict[str, Any]]:
    return {c["column"]: c for c in aggregate_chunked_route_evidence(run.sink)["columns"]}


def _assert_native_arrow_python(run: Run, column: str = "s") -> None:
    evidence = run.ev[0]
    assert evidence.native_admitted is True, evidence.reroute_reason
    assert evidence.reroute_reason is None
    assert {n.column: n.route for n in evidence.node_routes}[column] == "native_kernel"
    col = _columns(run)[column]
    assert col["planned_backend"] == "arrow_python"
    assert col["executed_backend"] == "arrow_python"
    assert evidence.compiled_kernel_executed is False


# ---------------------------------------------------------------------------
# 1. Parity matrix against the oracle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("config", sorted(ADMITTED_CONFIGS))
def test_native_chunked_equals_oracle_chunked(config: str, shape: str, size: int) -> None:
    chunks = split(source(SHAPES[shape]), size) or [source([])]
    native, forced = run_pair([tr_col(**ADMITTED_CONFIGS[config]), passthrough("p")], chunks)
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native)
    # Explicitly string-typed on every chunk, whatever the chunk holds.
    assert all(chunk.schema.field("s").type == pa.string() for chunk in native.out)


def test_empty_all_null_and_valued_chunks_interleaved_are_identical() -> None:
    chunks = [
        source(["ssn 123-45-6789", "a@b.com"]),
        source([]),
        source([None, None, None]),
        source(["", "no pii"]),
        source([]),
    ]
    native, forced = run_pair([tr_col(), passthrough("p")], chunks)
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native)
    assert {chunk.schema.field("s").type for chunk in native.out} == {pa.string()}


def test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs() -> None:
    values = [None if i % 11 == 3 else CORPUS[i % len(CORPUS)] for i in range(20_007)]
    native, forced = run_pair(
        [tr_col(label_token=True), passthrough("p")], split(source(values), 5_000)
    )
    assert len(native.out) == 5
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native)


def test_two_text_redact_columns_with_different_configs_each_match_the_oracle() -> None:
    table = pa.table(
        {
            "a": pa.array(CORPUS, pa.string()),
            "b": pa.array(CORPUS, pa.string()),
            "p": pa.array(range(len(CORPUS)), pa.int64()),
        }
    )
    columns = [
        tr_col("a", detectors=["email"]),
        tr_col("b", label_token=True),
        passthrough("p"),
    ]
    native, forced = run_pair(columns, split(table, 9))
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native, "a")
    _assert_native_arrow_python(native, "b")


# ---------------------------------------------------------------------------
# 2. Table-level lift: one text_redact column no longer vetoes its neighbours.
# ---------------------------------------------------------------------------


def test_text_redact_beside_scalar_operators_keeps_the_table_native() -> None:
    table = pa.table(
        {
            "r": pa.array([f"r{i}" for i in range(9)], pa.string()),
            "t": pa.array([f"abcdef{i}" for i in range(9)], pa.string()),
            "s": pa.array(CORPUS[:9], pa.string()),
            "p": pa.array(range(9), pa.int64()),
        }
    )
    columns = [redact("r"), truncate("t"), tr_col(), passthrough("p")]
    run = run_one(make_config(columns), split(table, 4))
    evidence = run.ev[0]
    assert evidence.native_admitted is True, evidence.reroute_reason
    assert {n.column: n.route for n in evidence.node_routes} == {
        "r": "native_kernel",
        "t": "native_kernel",
        "s": "native_kernel",
        "p": "native_kernel",
    }
    assert {c: v["executed_backend"] for c, v in _columns(run).items()}["s"] == "arrow_python"


@NEEDS_COMPANION
def test_text_redact_beside_hash_and_faker_keeps_each_on_its_own_backend() -> None:
    n = 12
    table = pa.table(
        {
            "h": pa.array([f"user{i % 4}@x.com" for i in range(n)], pa.string()),
            "f": pa.array([f"first{i % 5}" for i in range(n)], pa.string()),
            "s": pa.array([CORPUS[i] for i in range(n)], pa.string()),
        }
    )
    columns = [hash_col("h"), faker_col("f"), tr_col()]
    run = run_one(make_config(columns), split(table, 5))
    evidence = run.ev[0]
    assert evidence.native_admitted is True, evidence.reroute_reason
    backends = {c: v["executed_backend"] for c, v in _columns(run).items()}
    assert backends == {"h": "rust_companion", "f": "rust_pool_select", "s": "arrow_python"}
    assert evidence.compiled_kernel_executed is True


def test_the_evidence_planner_plans_arrow_python_for_text_redact() -> None:
    from decoy_engine.execution._chunked_profile import first_chunk_profile

    config = make_config([tr_col(), passthrough("p")])
    profile = first_chunk_profile(source(CORPUS), table=TABLE, engine_version=ENGINE_VERSION)
    plans = plan_column_backends(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )
    assert {p.column: p.planned_backend for p in plans}["s"] == "arrow_python"


# ---------------------------------------------------------------------------
# 3. Excluded configs stay on the oracle and produce today's output.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PASS_THROUGH_CONFIGS))
def test_excluded_configs_stay_on_the_oracle_and_leave_the_column_unchanged(name: str) -> None:
    cfg, _code = PASS_THROUGH_CONFIGS[name]
    table = source(CORPUS)
    chunks = split(table, 7)
    native, forced = run_pair([tr_col(**cfg), passthrough("p")], chunks)
    evidence = native.ev[0]
    assert evidence.native_admitted is False
    assert "fallback_policy_not_native:s:python_only" in (evidence.reroute_reason or "")
    assert evidence.kernel_calls == {}
    assert_same_as_oracle(native, forced, expect_native=False)
    # These configs are silent pass-through in the oracle: the source value and type survive.
    got = [v for chunk in native.out for v in chunk.column("s").to_pylist()]
    assert got == CORPUS
    assert {chunk.schema.field("s").type for chunk in native.out} == {pa.string()}


# ---------------------------------------------------------------------------
# 4. Non-string source.
# ---------------------------------------------------------------------------


def test_the_real_type_gate_names_text_redact_and_every_non_string_type() -> None:
    schema = pa.schema(
        [("a", pa.string()), ("b", pa.large_string()), ("c", pa.int64()), ("d", pa.null())]
    )
    assert string_source_type_rejection("text_redact", "a", schema) is None
    assert string_source_type_rejection("text_redact", "b", schema) == (
        "text_redact_source_type_not_string:b:large_string"
    )
    assert string_source_type_rejection("text_redact", "c", schema) == (
        "text_redact_source_type_not_string:c:int64"
    )


def test_an_int64_source_takes_the_oracle_leg_with_the_real_type_code() -> None:
    table = pa.table(
        {
            "s": pa.array([1, 22, 333, None, 5], pa.int64()),
            "p": pa.array(range(5), pa.int64()),
        }
    )
    native, forced = run_pair([tr_col(), passthrough("p")], split(table, 2))
    evidence = native.ev[0]
    assert evidence.native_admitted is False
    assert "text_redact_source_type_not_string:s:int64" in (evidence.reroute_reason or "")
    assert evidence.kernel_calls == {}
    assert_same_as_oracle(native, forced, expect_native=False)


def test_a_large_string_source_takes_the_oracle_leg() -> None:
    chunks = split(source(CORPUS[:9], typ=pa.large_string()), 4)
    native, forced = run_pair([tr_col(), passthrough("p")], chunks)
    assert native.ev[0].native_admitted is False
    assert "text_redact_source_type_not_string:s:large_string" in (
        native.ev[0].reroute_reason or ""
    )
    assert_same_as_oracle(native, forced, expect_native=False)


def _auto_config(tmp_path: Path, typ: str, **cfg: Any) -> dict[str, Any]:
    config = make_config([tr_col(**cfg), passthrough("p")])
    path = str(tmp_path / f"{typ}.parquet")
    pq.write_table(_auto_source(typ), path)
    config["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    config["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    return config


def _auto_source(typ: str) -> pa.Table:
    column = {
        "string": pa.array(CORPUS[:10], pa.string()),
        "int64": pa.array([i * 11 for i in range(10)], pa.int64()),
    }[typ]
    return pa.table({"s": column, "p": pa.array(range(10), pa.int64())})


def _auto(config: dict[str, Any], typ: str, **kw: Any) -> Any:
    return run_pipeline(
        copy.deepcopy(config),
        {TABLE: _auto_source(typ)},
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **kw,
    )


@pytest.mark.parametrize("typ", ["string", "int64"])
def test_the_auto_router_equals_the_full_frame_run_for_string_and_non_string_sources(
    tmp_path: Path, typ: str
) -> None:
    config = _auto_config(tmp_path, typ)
    auto = _auto(config, typ, auto_chunk_threshold_rows=3, chunk_size_rows=4)
    full = _auto(config, typ, auto_chunk=False)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
    chunked = auto.quality_metrics["chunked_route"]
    assert chunked["native_admitted"] is (typ == "string")
    a, f = auto.outputs[TABLE], full.outputs[TABLE]
    assert a.column("s").to_pylist() == f.column("s").to_pylist()
    assert a.schema.field("s").type == f.schema.field("s").type == pa.string()


# ---------------------------------------------------------------------------
# 5. `when:` stays off native through the existing code.
# ---------------------------------------------------------------------------


def test_a_when_predicate_keeps_text_redact_off_the_native_route() -> None:
    config = make_config([tr_col(), passthrough("p")])
    config["tables"][0]["columns"][0]["when"] = "s != ''"
    chunks = split(source(CORPUS), 6)
    run = run_one(config, chunks)
    assert run.ev[0].native_admitted is False
    assert "when_predicate_not_native" in (run.ev[0].reroute_reason or "")
    plain = run_one(make_config([tr_col(), passthrough("p")]), chunks)
    assert [t.column("s").to_pylist() for t in run.out] == [
        t.column("s").to_pylist() for t in plain.out
    ]
    joined = "; ".join(_whole_column_state_rejections(config, table=TABLE))
    assert "when_predicate_not_chunk_stable" in joined


# ---------------------------------------------------------------------------
# 7. Evidence.
# ---------------------------------------------------------------------------


def test_evidence_matches_redact_and_claims_no_compiled_work() -> None:
    chunks = split(source(CORPUS), 9)
    run = run_one(
        make_config([tr_col(), redact("r0"), passthrough("p")]),
        [c.append_column("r0", pa.array(["x"] * c.num_rows, pa.string())) for c in chunks],
    )
    evidence = run.ev[0]
    assert evidence.native_admitted is True
    assert evidence.compiled_kernel_executed is False
    assert evidence.pool_select_executed is False
    # One ordinary call per chunk per column: an operator call counter, not compiled work.
    assert evidence.kernel_calls["text_redact"] == len(chunks)
    assert evidence.kernel_calls["redact"] == len(chunks)
    cols = _columns(run)
    for name in ("s", "r0"):
        assert cols[name]["planned_backend"] == cols[name]["executed_backend"] == "arrow_python"
        assert cols[name]["calls"] == len(chunks)
    agg = aggregate_chunked_route_evidence(run.sink)
    assert not any("kernel_idle" in c and c["kernel_idle"] for c in agg["columns"])


# ---------------------------------------------------------------------------
# 8. Determinism: no dependence on the mask key or the job seed.
# ---------------------------------------------------------------------------


def _text_values(config: dict[str, Any], *, secret: bytes | None = None) -> list[Any]:
    outcome = run_outcome(config, split(source(CORPUS), 6), secret=secret)
    assert outcome.error is None, outcome.error
    return [v for t in outcome.out for v in t.column("s").to_pylist()]


def test_output_is_reproducible_and_independent_of_the_mask_key_and_job_seed() -> None:
    columns = [tr_col(label_token=True), passthrough("p")]
    base = _text_values(make_config(columns))
    assert base == _text_values(make_config(columns))
    assert base == _text_values(make_config(columns), secret=bytes(range(1, 33)))
    assert base == _text_values(make_config(columns, global_settings={"seed": 7}))


# ---------------------------------------------------------------------------
# Config-gated string pin (3d), observable through a rejected config.
# ---------------------------------------------------------------------------


def _rule(column: dict[str, Any], first: pa.Table) -> Any:
    config = make_config([column, passthrough("p")])
    return build_schema_rule(config, table=TABLE, first=first, registry=get_default_registry())


def test_an_admitted_text_redact_config_is_string_pinned() -> None:
    assert "s" in _rule(tr_col(), source(CORPUS)).string_columns
    assert "s" in _rule(tr_col(detectors=["email"], label_token=True), source([])).string_columns


@pytest.mark.parametrize("name", sorted(EXCLUDED_CONFIGS))
def test_a_rejected_text_redact_config_is_not_string_pinned(name: str) -> None:
    cfg, _code = EXCLUDED_CONFIGS[name]
    assert "s" not in _rule(tr_col(**cfg), source(CORPUS)).string_columns


def test_a_text_redact_column_with_a_when_predicate_is_not_string_pinned() -> None:
    column = {**tr_col(), "when": "p > 1"}
    config = make_config([tr_col(), passthrough("p")])
    config["tables"][0]["columns"][0]["when"] = column["when"]
    rule = build_schema_rule(
        config, table=TABLE, first=source(CORPUS), registry=get_default_registry()
    )
    assert "s" not in rule.string_columns


def test_a_rejected_config_over_an_int64_source_keeps_the_oracle_type_when_chunked() -> None:
    """A non-string token is a silent pass-through in the oracle, so the int64 column comes
    back int64. A pin that admitted the rejected config would cast it to string."""
    table = pa.table(
        {"s": pa.array([1, 2, 3, 4, 5, 6], pa.int64()), "p": pa.array(range(6), pa.int64())}
    )
    run = run_one(make_config([tr_col(token=7), passthrough("p")]), split(table, 4))
    assert run.ev[0].native_admitted is False
    assert {chunk.schema.field("s").type for chunk in run.out} == {pa.int64()}
    assert [v for c in run.out for v in c.column("s").to_pylist()] == [1, 2, 3, 4, 5, 6]


def test_a_string_source_all_null_column_is_a_string_on_the_chunked_route() -> None:
    """Whole-frame gives Arrow `null` for an entirely null or empty text_redact column; the
    chunked route pins `string` so every chunk of a column shares one schema."""
    run = run_one(make_config([tr_col(), passthrough("p")]), [source([None, None, None])])
    assert run.out[0].schema.field("s").type == pa.string()
