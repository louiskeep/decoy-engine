"""Phase F FR0: the unified generation core through the production mixed route's stitch and publish.

Plan-gate #10. FR0 changed schema declaration: a column the core generates as all-null, and a
zero-row table, are now declared `string` instead of the type Arrow would infer (`null`). In a
mixed job that table then crosses the final stitch, where "mask output wins ties", and the full
frame route echoes generate output through pandas first. This file drives the real
`run_pipeline` route for a generated-parent / masked-child job and compares CANONICAL ARROW
values and SCHEMA METADATA (field type, nullability, field metadata), never serialized Parquet
bytes, so a changed `generate_tables` alone cannot satisfy it.

Two production paths are covered:

* the admitted mixed route (`mixed_generate_mask_chunked`): resident return and a recording sink;
* the `pregenerated` execution: a runtime decline (a nullable generate column is not stable
  through the oracle's pandas echo) hands the already-built generate outputs to the full-frame
  continuation, which echoes and stitches them. Generation must run exactly once.

The oracle for both is `execution_mode="full_frame"`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.generation import synthesize
from tests.parity._f2_mixed_support import (
    CHILD,
    PARENT,
    RecordingSink,
    child_table,
    mixed_config,
    oracle,
    run,
)

KEYS = [str(i % 5) for i in range(40)]
ID = {"name": "id", "type": "sequence", "start": 0, "step": 1}
COUNT = {"name": "n", "type": "categorical", "categories": [1, 2, 3]}
KIND = {"name": "kind", "type": "categorical", "categories": ["a", "b", "c"]}
ALL_NULL = {"name": "allnull", "type": "faker", "faker_type": "city", "null_probability": 1.0}


def _job(tmp_path: Path, columns: list[dict[str, Any]], rows: int) -> tuple[dict, pa.Table]:
    child = child_table(pa.array(KEYS, pa.string()))
    return mixed_config(tmp_path, child, parent_columns=columns, parent_rows=rows), child


def _field_view(table: pa.Table) -> list[tuple[str, str, bool, dict[bytes, bytes] | None]]:
    """Name, type, nullability and field-level metadata for every column, in order."""
    return [(f.name, str(f.type), f.nullable, f.metadata) for f in table.schema]


def _values(table: pa.Table) -> dict[str, list[Any]]:
    return {name: table.column(name).to_pylist() for name in table.column_names}


def _assert_same_values_and_fields(got: dict[str, pa.Table], want: dict[str, pa.Table]) -> None:
    """Canonical values, column order, types, nullability and field metadata must match the oracle."""
    assert list(got) == list(want)
    for name in want:
        assert _values(got[name]) == _values(want[name]), name
        assert got[name].num_rows == want[name].num_rows, name
        for g, w in zip(_field_view(got[name]), _field_view(want[name]), strict=True):
            assert g == w, (name, g, w)


# ---- the admitted mixed route -----------------------------------------------------------------


def test_mixed_route_matches_the_oracle_in_values_and_schema(tmp_path: Path) -> None:
    config, child = _job(tmp_path, [ID, COUNT, KIND], rows=5)
    got = run(config, {CHILD: child})
    assert got.quality_metrics["execution"]["route_reason"] == "mixed_generate_mask_chunked"
    want = oracle(config, {CHILD: child})
    _assert_same_values_and_fields(got.outputs, want.outputs)
    assert [str(f.type) for f in got.outputs[PARENT].schema] == ["string", "int64", "string"]
    # The mixed route publishes the core's own schema; only the oracle's echo adds pandas metadata.
    assert got.outputs[PARENT].schema.metadata is None


def test_mixed_route_sink_publication_matches_the_oracle(tmp_path: Path) -> None:
    config, child = _job(tmp_path, [ID, COUNT, KIND], rows=5)
    sink = RecordingSink()
    run(config, {CHILD: child}, sink=sink)
    want = oracle(config, {CHILD: child}).outputs
    assert sink.published is not None
    _assert_same_values_and_fields(sink.published, want)


def test_real_parquet_publication_keeps_the_declared_schema(tmp_path: Path) -> None:
    config, child = _job(tmp_path, [ID, COUNT, KIND], rows=5)
    target = tmp_path / "published"
    run(config, {CHILD: child}, sink=ParquetTransactionalSink(target))
    want = oracle(config, {CHILD: child}).outputs
    read = {name: pq.read_table(target / f"{name}.parquet") for name in want}
    _assert_same_values_and_fields(read, want)


def test_a_zero_row_generated_parent_matches_the_oracle_exactly(tmp_path: Path) -> None:
    """FR0 declares a zero-row core table's columns `string`, but the mixed route must still
    publish what the full_frame oracle does. Admission declines a zero-row generated parent, as
    it did when Arrow inferred `null` for its key, so the job runs the pregenerated full-frame
    continuation and the output equals the oracle in values AND schema types (`null`)."""
    config, child = _job(tmp_path, [ID, KIND], rows=0)
    declared = generate_tables_via_plan_for(config)[PARENT]
    assert [str(f.type) for f in declared.schema] == ["string", "string"]
    assert declared.num_rows == 0

    got = run(config, {CHILD: child})
    assert got.quality_metrics["execution"]["route_reason"] == (
        "mixed_runtime_declined:mixed-fk-cross-generate-unsupported"
    )
    want = oracle(config, {CHILD: child})
    _assert_same_values_and_fields(got.outputs, want.outputs)
    assert [str(f.type) for f in got.outputs[PARENT].schema] == ["null", "null"]
    assert got.outputs[PARENT].num_rows == 0

    sink = RecordingSink()
    run(config, {CHILD: child}, sink=sink)
    assert sink.calls == []
    _assert_same_values_and_fields(got.outputs, oracle(config, {CHILD: child}).outputs)


def generate_tables_via_plan_for(config: dict[str, Any]) -> dict[str, pa.Table]:
    """The core's own output for the job's generate tables, before any stitching."""
    from decoy_engine.plan import compile_plan
    from tests.parity import _f0_fixtures as fx

    plan = compile_plan(config, fx._profile(), decoy_engine_version="fr0")
    return synthesize.generate_tables(plan)


# ---- the pregenerated execution ----------------------------------------------------------------


def test_pregenerated_path_stitches_the_oracle_schema_and_generates_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An all-null generate column declines the mixed route at runtime. The built outputs go to
    the full-frame continuation as `pregenerated`, are echoed through pandas, and the echoed
    copy wins the stitch tie. Published values and schema must equal the oracle's, and the core
    must not run a second time."""
    config, child = _job(tmp_path, [ID, ALL_NULL, KIND], rows=5)

    declared = generate_tables_via_plan_for(config)[PARENT]
    assert str(declared.schema.field("allnull").type) == "string"  # the core declares it
    assert declared.column("allnull").null_count == 5

    calls: list[int] = []
    real = synthesize.generate_tables

    def counting(*args: Any, **kwargs: Any) -> dict[str, pa.Table]:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(synthesize, "generate_tables", counting)
    got = run(config, {CHILD: child})
    assert got.quality_metrics["execution"]["route_reason"] == (
        "mixed_runtime_declined:generation-shape-unsupported"
    )
    assert calls == [1], "pregenerated outputs must be reused, not regenerated"

    monkeypatch.setattr(synthesize, "generate_tables", real)
    want = oracle(config, {CHILD: child})
    _assert_same_values_and_fields(got.outputs, want.outputs)
    assert got.outputs[PARENT].column("allnull").to_pylist() == [None] * 5
    # Mask output wins ties: the echoed copy, not the core's declared `string`, is published,
    # the same as before FR0 (pandas infers `null` for an all-null object column).
    assert str(got.outputs[PARENT].schema.field("allnull").type) == "null"
    assert str(got.outputs[PARENT].schema.field("id").type) == "string"
    assert got.outputs[PARENT].schema.metadata is not None
    assert b"pandas" in got.outputs[PARENT].schema.metadata


def test_pregenerated_path_does_not_touch_the_sink(tmp_path: Path) -> None:
    """The declined route returns its outputs in the result, as the full-frame route does; the
    sink sees no write, so the returned tables are the publication."""
    config, child = _job(tmp_path, [ID, ALL_NULL, KIND], rows=5)
    sink = RecordingSink()
    got = run(config, {CHILD: child}, sink=sink)
    assert sink.calls == []
    _assert_same_values_and_fields(got.outputs, oracle(config, {CHILD: child}).outputs)
